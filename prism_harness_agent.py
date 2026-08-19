"""PrismHarness 核心。

基于 AgentScope ReActAgent + PrismHarnessGuard Mixin，注入：
- 推理提示 Hook (ReasoningHint) — 每次推理前注入规则提醒
- 上下文可视化 Hook — 每轮生成 ContextSnapshot
- 会话活跃保持 Hook — 防止超时
"""

import asyncio
import inspect
import json
import os
import sys
import traceback
from datetime import datetime

from agentscope.agent import ReActAgent
from agentscope.formatter import OpenAIChatFormatter
from agentscope.memory import InMemoryMemory
from agentscope.message import Msg
from agentscope.pipeline import stream_printing_messages

from prism_harness_guard import PrismHarnessGuardMixin
from session import Session, SessionManager, AgentRequest, SessionStatus
from tools import (
    build_toolkit,
    format_system_prompt,
    REASONING_HINT_TEMPLATE,
)
from model_config import build_model, build_token_counter, load_config
from context_viz import ContextViz, TokenStats
from conf import FLAGS, console


# ---- Mixin 组装：PrismHarnessAgent = PrismHarnessGuard + ReActAgent ----

class PrismHarnessAgent(PrismHarnessGuardMixin, ReActAgent):
    """PrismHarness = ReActAgent + HITL 工具确认。"""
    pass


def _prompt_content_to_text(content) -> str:
    """把 OpenAI 格式消息的 content（str 或 list[block]）转成可读文本原样呈现。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for b in content:
            if isinstance(b, dict):
                t = b.get("type", "")
                if t == "text":
                    parts.append(str(b.get("text", "")))
                elif t == "tool_use":
                    name = b.get("name", "")
                    inp = _prompt_content_to_text(b.get("input", b.get("raw_input", "")))
                    parts.append(f"[工具调用] {name} 参数: {inp}")
                elif t == "tool_result":
                    parts.append(f"[工具结果]\n{_prompt_content_to_text(b.get('output', b.get('content', '')))}")
                else:
                    parts.append(str(b))
            else:
                parts.append(str(b))
        return "\n\n".join(p for p in parts if p)
    if content is None:
        return ""
    return str(content)


def _prompt_to_readable(prompt) -> list:
    """把将要发给 LLM 的 prompt（list[dict]，OpenAI 格式）整理成可展示结构。

    不做任何过滤/改写，只把 content 块与 tool_calls 转成可读文本——上下文是怎么样就怎么样。
    """
    if not isinstance(prompt, list):
        return []
    out = []
    for m in prompt:
        if not isinstance(m, dict):
            continue
        role = m.get("role", "unknown")
        text = _prompt_content_to_text(m.get("content"))
        # OpenAI 函数调用：assistant 消息 content=null，工具调用放在 tool_calls 字段
        tcs = m.get("tool_calls")
        if tcs:
            parts = []
            for tc in tcs:
                fn = tc.get("function", {})
                parts.append(f"[工具调用] {fn.get('name', '')} 参数: {fn.get('arguments', '')}")
            join = "\n\n".join(parts)
            text = f"{text}\n\n{join}" if text else join
        out.append({"role": role, "content": text})
    return out


def _is_internal_hint_message(message: dict) -> bool:
    """过滤实时上下文里的临时内部提示，避免右侧短暂出现第二个 user。"""
    content = message.get("content", "")
    return (
        message.get("role") == "user"
        and "内部信息，非用户输入" in content
    )


class ModelContextProbe:
    """包一层 model：每次真正调 LLM 前，把将要发送的上下文（原样）交给 sink 展示。

    - __call__(prompt, **kwargs)：先把 prompt 交给 sink（前端展示 + viz 记录），再透传给真实模型。
    - 其余属性（stream 等）透传到内层 model，不影响 AgentScope 原有行为。
    """

    def __init__(self, model, ctx_sink: dict):
        object.__setattr__(self, "_probe_model", model)
        object.__setattr__(self, "_probe_sink", ctx_sink)

    def __getattr__(self, name):
        return getattr(object.__getattribute__(self, "_probe_model"), name)

    def __setattr__(self, name, value):
        # stream 等属性写到内层 model，让 AgentScope 的开关逻辑照常生效
        setattr(object.__getattribute__(self, "_probe_model"), name, value)

    async def __call__(self, prompt, **kwargs):
        sink = object.__getattribute__(self, "_probe_sink")
        emit = sink.get("emit")
        if emit is not None:
            try:
                await emit(prompt)
            except Exception:
                pass  # 展示失败不能影响主流程
        return await object.__getattribute__(self, "_probe_model")(prompt, **kwargs)


# ---- Agent 生命周期 ----

async def register_reasoning_hint(agent: ReActAgent):
    """注册推理前提示 Hook。"""
    async def add_hint(agent: ReActAgent, kwargs):
        now = datetime.now()
        weekday_map = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
        weekday = weekday_map[now.weekday()]
        current_time = now.strftime(f"%Y年%m月%d日 {weekday} %H:%M:%S")
        hint = REASONING_HINT_TEMPLATE.format(current_time=current_time)
        await agent.memory.add(
            Msg(name="inner_hint", content=hint, role="user"),
            marks=["MY_REASONING_HINT"],
        )

    async def remove_hint(agent: ReActAgent, kwargs, output=None):
        await agent.memory.delete_by_mark("MY_REASONING_HINT")

    agent.register_instance_hook("pre_reasoning", "add_hint", add_hint)
    agent.register_instance_hook("post_reasoning", "remove_hint", remove_hint)


async def register_keepalive(agent: ReActAgent, sess: Session):
    """注册会话活跃保持 Hook。"""
    async def activate(agent: ReActAgent, kwargs, output=None):
        await sess.activate()
    for hook in ["pre_reasoning", "pre_acting", "post_acting", "post_reasoning"]:
        agent.register_instance_hook(hook, "keepalive", activate)


async def register_thinking_cleanup(agent: ReActAgent):
    """每轮推理前清掉 ThinkingBlock，防 formatter warning + 省 token。"""
    async def strip_thinking(agent: ReActAgent, kwargs):
        try:
            gm = agent.memory.get_memory()
            if inspect.iscoroutine(gm):
                gm = await gm
            for m in (gm or []):
                content = getattr(m, "content", None)
                if isinstance(content, list):
                    # 过滤 thinking 块（dict 形式或 ThinkingBlock 对象）
                    new_content = [c for c in content if (
                        getattr(c, "type", "") if hasattr(c, "type")
                        else c.get("type", "") if isinstance(c, dict)
                        else ""
                    ) != "thinking"]
                    if len(new_content) != len(content):
                        m.content = new_content
        except Exception:
            pass

    agent.register_instance_hook("pre_reasoning", "strip_thinking", strip_thinking)


# ---- 魔术命令处理 ----

async def handle_magic_command(request: AgentRequest, sess: Session):
    """处理 /approve /reject /approve_all 等魔术命令。

    返回 (已处理, 确认文案)：
    - 已处理=True 表示这是魔术命令，调用方据此决定是否重新驱动 agent；
    - 确认文案用于「无 pending」时的即时回执（不调 LLM）。
    """
    text = ""
    if isinstance(request.content, list):
        for block in request.content:
            if isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text", "")
                break
    elif isinstance(request.content, str):
        text = request.content

    text = text.strip()
    if not text.startswith("/"):
        return False, ""

    cmd = text[1:]
    if cmd == "approve":
        if await sess.approve_head_pending():
            return True, ""
        return True, "当前没有等待确认的工具调用。"
    elif cmd == "reject":
        if await sess.reject_head_pending():
            return True, ""
        return True, "当前没有等待确认的工具调用。"
    elif cmd == "approve_all":
        sess.auto_approve = True
        # 一次性放行队列里所有 pending（并行/批量工具会产生多个 pending，
        # 之前只 APPROVED 队首，导致后面仍逐个弹确认卡、用户被迫反复批准）。
        n = await sess.approve_all_pending()
        if n:
            return True, ""
        return True, "已开启自动放行模式（后续高风险工具将直接执行）。"
    elif cmd == "approve_all off":
        sess.auto_approve = False
        return True, "已关闭自动放行模式。"
    elif cmd.startswith("answer"):
        # /answer <json> —— 用户回答了结构化提问（ask_user_question）
        raw = cmd[len("answer"):].strip()
        answer = None
        if raw:
            try:
                answer = json.loads(raw)
            except Exception:
                answer = {"free_text": raw}
        if answer is not None and await sess.answer_pending(answer):
            return True, ""
        return True, "当前没有等待回答的问题。"
    return False, ""  # 不认识的 / 命令，交给 agent 处理


# ---- Agent Runner（每会话一个后台任务） ----

async def agent_runner(sess: Session, cfg: dict, workspace_dir: str):
    """每个 session 的后台 Agent 循环。"""
    ctx_cfg = cfg.get("context", {})
    token_budget = ctx_cfg.get("token_budget", 32000)
    max_iters = cfg.get("agent", {}).get("max_iters", 50)
    log_dir = cfg.get("logging", {}).get("dir", "session_logs")

    viz = ContextViz(log_dir, sess.session_id)
    console("[session-start]", sess.session_id)

    # 整个会话复用同一个 Agent（保留 memory，实现多轮上下文真实累积）
    toolkit = await build_toolkit(workspace_dir)
    system_prompt = format_system_prompt([], workspace_dir)
    model = build_model(cfg, stream=True)
    token_counter = build_token_counter(cfg)
    compression_config = None
    # 记忆压缩关闭（enable=False）：agentscope 的 _compress_memory_if_needed 在
    # 遍历 memory 时若遇到缺少 "type" 键的 content block 会直接 KeyError 崩溃，
    # 导致该会话的 agent 轮次中断、后续请求卡死（本地 vLLM 200k 上下文 + 较高
    # token_budget，压缩阈值远超模型上限，本身基本不会触发，关闭零损失却消除崩溃源）。
    if ctx_cfg.get("compress_threshold"):
        compression_config = ReActAgent.CompressionConfig(
            enable=False,
            agent_token_counter=token_counter,
            trigger_threshold=int(
                token_budget * float(ctx_cfg.get("compress_threshold", 0.8)),
            ),
            keep_recent=int(ctx_cfg.get("keep_recent_messages", 6)),
        )
    # 包一层 model：每次真正调 API 前，把将要发送的上下文（原样）推向前端透明展示。
    # ctx_sink 是会话级可变对象，每轮请求把 emit 指到当轮 SSE 流（见下方 streaming）。
    ctx_sink: dict = {}
    probe = ModelContextProbe(model, ctx_sink)
    agent = PrismHarnessAgent(
        name="PrismHarness",
        sys_prompt=system_prompt,
        model=probe,
        formatter=OpenAIChatFormatter(),
        toolkit=toolkit,
        memory=InMemoryMemory(),
        max_iters=max_iters,
        parallel_tool_calls=cfg.get("agent", {}).get("parallel_tool_calls", True),
        compression_config=compression_config,
        _prism_harness_sess=sess,
    )
    # 控制台输出：默认关闭，避免终端刷屏；流式输出走 msg_queue，不受影响。
    agent.set_console_output_enabled(
        bool(cfg.get("agent", {}).get("console_output", False)),
    )

    # 记录当前 LLM 配置签名，用于每轮热重载判断是否需要重建 model。
    last_llm_sig = None

    def apply_llm_config(new_cfg: dict):
        """若 LLM 配置（provider/endpoint/model/api_key/thinking/temperature）变化，
        则重建 model/token_counter 并热替换到 probe 与 agent 上，下一轮请求立即生效。"""
        nonlocal model, token_counter, compression_config, last_llm_sig
        sig = (
            new_cfg.get("llm", {}).get("provider"),
            new_cfg.get("providers", {}),
        )
        if sig == last_llm_sig:
            return False
        last_llm_sig = sig
        model = build_model(new_cfg, stream=True)
        token_counter = build_token_counter(new_cfg)
        probe._probe_model = model
        # 防御性替换：这些属性若不存在则跳过，不阻塞主流程
        for attr in ("token_counter", "compression_config", "max_iters"):
            if hasattr(agent, attr):
                try:
                    setattr(agent, attr, getattr(agent, attr))
                except Exception:
                    pass
        agent.token_counter = token_counter
        compression_config = None
        n_ctx = new_cfg.get("context", {})
        if n_ctx.get("compress_threshold") and hasattr(agent, "compression_config"):
            compression_config = ReActAgent.CompressionConfig(
                enable=False,  # 见上：关闭以规避 agentscope 压缩遇畸形 block 的 KeyError 崩溃
                agent_token_counter=token_counter,
                trigger_threshold=int(
                    new_cfg.get("context", {}).get("token_budget", 32000)
                    * float(n_ctx.get("compress_threshold", 0.8)),
                ),
                keep_recent=int(n_ctx.get("keep_recent_messages", 6)),
            )
            try:
                agent.compression_config = compression_config
            except Exception:
                pass
        return True

    apply_llm_config(cfg)  # 初始化签名
    await register_reasoning_hint(agent)
    await register_keepalive(agent, sess)
    await register_thinking_cleanup(agent)

    while True:
        request, status = await sess.get_request()
        if status == SessionStatus.INACTIVE:
            console("[session-end]", sess.session_id)
            viz.commit()  # 最后一次落盘（同步方法，返回 md 路径）
            await sess.release()
            break

        await sess.activate()

        # 热重载：每轮请求前重读 config.yaml，若模型/API Key 等 LLM 配置有变，
        # 立即重建 model 并热替换——设置页改完下一轮消息马上生效，无需重启。
        try:
            fresh_cfg = load_config()
            apply_llm_config(fresh_cfg)
        except Exception:
            pass  # 读/建失败则沿用现有模型，不中断本轮回话

        # 每轮重建 toolkit，让 manage_skill 新建的技能无需重启即可出现在
        # ReActAgent.sys_prompt 动态附加的官方 Skills 提示词中。
        agent.toolkit = await build_toolkit(workspace_dir)

        # 魔术命令
        is_magic, magic_ack = await handle_magic_command(request, sess)
        pending_exists = bool(await sess.get_pending_tool()) if is_magic else False

        # 独立魔法命令（无 pending）：直接返回确认，不调 LLM
        if is_magic and not pending_exists:
            response_q = request.response_queue
            await response_q.put({
                "type": "status",
                "phase": "thinking",
                "step": 0,
                "detail": magic_ack or "已执行",
            })
            await response_q.put({"type": "status", "phase": "done", "detail": "完成"})
            await response_q.put(None)
            await sess.finish_request(request)
            continue

        try:
            response_q = request.response_queue

            # 开始新一轮上下文可视化
            user_text = ""
            if isinstance(request.content, list):
                user_text = " ".join(
                    b.get("text", "") for b in request.content if b.get("type") == "text"
                )
            elif isinstance(request.content, str):
                user_text = request.content
            viz.start_round(user_text)

            # 热更新式：每轮与 LLM 交互前，从当前文件重建 system prompt（persona/技能/画像），
            # 并热更新到 agent —— 改 USER.md/AGENTS.md/SOUL.md 后下一轮立即真实生效；
            # 配合上下文探头，右侧光谱面板展示的也正是这份最新实际发送的 system prompt。
            system_prompt = format_system_prompt([], workspace_dir)
            agent._sys_prompt = system_prompt

            # 估算 System Prompt tokens（CJK 友好：每字 ~1 token）
            sys_chars = len(system_prompt)
            sys_tokens_est = sys_chars  # CJK 简化估算

            # 魔术命令（/approve /approve_all 等）：命令文本不要写进 memory。
            # 用 None 重新驱动 agent（reply(None) 不会 add 任何消息），
            # 让 guard._reasoning 读到已 APPROVED/REJECTED 的 pending 继续执行。
            inputs = (
                None
                if is_magic
                else Msg(name="user", content=request.content, role="user")
            )

            q = asyncio.Queue()

            # 把"本轮真正发送给 LLM 的上下文"灌进右侧光谱面板。
            # ModelContextProbe 每次调 API 前会回调 emit，构造 context_view 事件实时刷新面板，
            # full_messages 用真实 prompt 全量（不截断），一轮内多次调 API 会更新同一个轮块。
            async def emit_context(prompt):
                readable = [
                    m
                    for m in _prompt_to_readable(prompt)
                    if not _is_internal_hint_message(m)
                ]
                await q.put({
                    "type": "context_view",
                    "data": {
                        "round": viz.round_num,
                        "timestamp": datetime.now().isoformat(),
                        "user_input": user_text,
                        "full_messages": [
                            {"role": m["role"], "content": m["content"], "chars": len(m["content"])}
                            for m in readable
                        ],
                    },
                    "msg_id": "ctx",
                })
            ctx_sink["emit"] = emit_context

            async def streaming():
                step = 0
                tool_id_to_step = {}  # tool_use_id → step number
                viz_records = {}      # tool_use_id → ToolCallRecord (用于回填参数/结果)
                seen_tool_ids = set()  # 已记录的 tool_use_id（去重：流式会重复发送同一块）

                # ---- 心跳：绝不静默。每 7s 告诉前端"我还在"，并带当前阶段 ----
                current_phase = {"text": "正在思考…"}
                heartbeat_stop = asyncio.Event()
                async def heartbeat():
                    tick = 0
                    while not heartbeat_stop.is_set():
                        try:
                            await asyncio.wait_for(heartbeat_stop.wait(), timeout=7)
                        except asyncio.TimeoutError:
                            pass
                        if heartbeat_stop.is_set():
                            break
                        tick += 1
                        await q.put({
                            "type": "status",
                            "phase": "heartbeat",
                            "detail": f"⏳ 仍在工作（{tick}）— {current_phase['text']}",
                        })
                hb_task = asyncio.create_task(heartbeat())

                terminal_phase = "done"      # 终态：done / max_iters / canceled / error / paused
                try:
                    if request.canceled:
                        terminal_phase = "canceled"
                        return
                    # 实时状态：开始推理
                    await q.put({"type": "status", "phase": "thinking", "step": 0,
                                 "detail": "正在思考…"})
                    async for msg, last in stream_printing_messages(
                        agents=[agent], coroutine_task=agent(inputs)
                    ):
                        if request.canceled:
                            await q.put({"cancel": True, "last": True, "contents": []})
                            terminal_phase = "canceled"
                            return
                        msg_id = getattr(msg, "id", None)
                        msg_ret = {
                            "msg_id": msg_id,
                            "last": last,
                            "contents": [],
                        }

                        for content in msg.content:
                            if isinstance(content, dict):
                                ctype = content.get("type", "")
                                # dsv4 thinking 推理过程 → 实时推给前端
                                if ctype == "thinking":
                                    reasoning = content.get("thinking", "")
                                    if reasoning:
                                        preview = reasoning[-120:].replace("\n", " ").strip()
                                        current_phase["text"] = f"🧠 推理… {preview}"
                                        await q.put({
                                            "type": "status",
                                            "phase": "reasoning",
                                            "detail": reasoning,
                                            "preview": preview,
                                        })
                                    continue
                                if ctype == "text":
                                    raw_text = content.get("text", "")
                                    # 更新当前阶段：让心跳显示模型在说什么（截前80字）
                                    if raw_text and isinstance(raw_text, str) and len(raw_text) > 2:
                                        preview = raw_text[:80].replace("\n", " ").strip()
                                        current_phase["text"] = f"推理中… {preview}"
                                    # 结构化提问 → 推 question 事件（携带问题数据，前端渲染选择卡片）
                                    if raw_text and "需要你的选择" in raw_text:
                                        current_phase["text"] = "等待你回答"
                                        pending = await sess.get_pending_tool()
                                        questions = []
                                        if pending and pending.name == "ask_user_question" and isinstance(pending.input, dict):
                                            questions = pending.input.get("questions", [])
                                        await q.put({
                                            "type": "status",
                                            "phase": "question",
                                            "detail": raw_text,
                                            "questions": questions,
                                        })
                                        continue
                                    # HITL 等待确认 → 推专门的 confirm 事件，前端直接渲染卡片（不重复发文本）
                                    if raw_text and "工具调用需要确认" in raw_text and "🔒" in raw_text:
                                        current_phase["text"] = "等待你确认工具调用"
                                        await q.put({
                                            "type": "status",
                                            "phase": "confirm",
                                            "detail": raw_text,
                                        })
                                        continue  # 跳过文本块，前端已通过 status 事件渲染卡片
                                    elif raw_text and "等待确认" in raw_text:
                                        current_phase["text"] = "等待你确认工具调用"
                                        await q.put({
                                            "type": "status",
                                            "phase": "pending",
                                            "detail": "🔒 等待你确认工具调用（见下方「允许/拒绝」按钮）",
                                        })
                                        continue  # 跳过文本块
                                    # AgentScope may return list/dict instead of str
                                    if isinstance(raw_text, list):
                                        raw_text = " ".join(
                                            t.get("text", "") if isinstance(t, dict) else str(t)
                                            for t in raw_text
                                        )
                                    elif not isinstance(raw_text, str):
                                        raw_text = str(raw_text)
                                    msg_ret["contents"].append({
                                        "type": "text",
                                        "content": raw_text,
                                    })
                                elif ctype == "tool_use":
                                    tool_id = content.get("id", "")
                                    # 优先 input，缺失时尝试 raw_input
                                    tool_input = content.get("input") or {}
                                    if not tool_input and content.get("raw_input"):
                                        try:
                                            tool_input = json.loads(content["raw_input"])
                                        except Exception:
                                            tool_input = {}
                                    # 诊断日志：看 dsv4 流式 tool_use 块的 input/raw_input 到底是什么
                                    if tool_id not in seen_tool_ids:
                                        pass  # 去重由下方 seen_tool_ids.add 处理
                                    if tool_id not in seen_tool_ids:
                                        # 新工具调用（AgentScope 流式会重复发送同一 tool_use 块，需去重）
                                        seen_tool_ids.add(tool_id)
                                        step += 1
                                        tool_id_to_step[tool_id] = step
                                        current_phase["text"] = f"调用工具 {content.get('name', '')}（第 {step} 步）"
                                        # 实时状态：正在调用工具（仅首次出现时发一次）
                                        await q.put({
                                            "type": "status",
                                            "phase": "acting",
                                            "step": step,
                                            "detail": f"正在调用工具：{content.get('name', '')}（第 {step} 步）",
                                            "tool_name": content.get("name", ""),
                                        })
                                        rec = viz.add_tool_call(
                                            step,
                                            content.get("name", ""),
                                            tool_input,
                                        )
                                        viz_records[tool_id] = rec
                                    else:
                                        # 同一调用的后续流式块：用更完整的参数覆盖（流式后块 input 更全）
                                        if tool_id in viz_records and tool_input:
                                            viz.update_tool_input(tool_id_to_step[tool_id], tool_input)
                                    msg_ret["contents"].append({
                                        "type": "tool_use",
                                        "tool_use_id": content.get("id", ""),
                                        "name": content.get("name", ""),
                                        "input": tool_input,
                                    })
                                elif ctype == "tool_result":
                                    raw_output = content.get("output", "")
                                    # Extract readable text from ToolResponse format
                                    if isinstance(raw_output, list):
                                        parts = []
                                        for item in raw_output:
                                            if isinstance(item, dict):
                                                t = item.get("text", item.get("content", ""))
                                                if isinstance(t, list):
                                                    t = " ".join(
                                                        x.get("text", str(x)) if isinstance(x, dict) else str(x)
                                                        for x in t
                                                    )
                                                parts.append(str(t))
                                            else:
                                                parts.append(str(item))
                                        raw_output = "\n".join(parts)
                                    elif isinstance(raw_output, dict):
                                        raw_output = raw_output.get("text", str(raw_output))
                                    elif not isinstance(raw_output, str):
                                        raw_output = str(raw_output)
                                    msg_ret["contents"].append({
                                        "type": "tool_result",
                                        "tool_use_id": content.get("id", ""),
                                        "name": content.get("name", ""),
                                        "output": raw_output,
                                    })
                                    # 更新 viz 中的工具结果
                                    tid = content.get("id", "")
                                    if tid in tool_id_to_step:
                                        viz.update_tool_result(tool_id_to_step[tid], raw_output)
                                        # 若此前 input 为空，用 raw_input 回填
                                        if tid in viz_records and not viz_records[tid].params and content.get("raw_input"):
                                            try:
                                                viz.update_tool_input(tool_id_to_step[tid], json.loads(content["raw_input"]))
                                            except Exception:
                                                pass

                        await q.put(msg_ret)

                    # 循环正常结束。判断是否因 max_iters 用尽而停（而非模型主动结束）。
                    # ReActAgent 到上限时不抛异常，静默返回——这正是"卡住不动"的来源。
                    # 用工具调用步数 vs max_iters 近似判断：若逼近上限，标记 max_iters。
                    if step >= max_iters:
                        terminal_phase = "max_iters"

                    # 推送 Token 统计和上下文快照
                    # InMemoryMemory.get_memory() 在 AgentScope 1.0.x 是协程，必须 await
                    mem_msgs = []
                    try:
                        gm = agent.memory.get_memory()
                        if inspect.iscoroutine(gm):
                            gm = await gm
                        mem_msgs = gm or []
                    except Exception:
                        for attr in ("_messages", "_memory", "messages", "content"):
                            mem_val = getattr(agent.memory, attr, None)
                            if mem_val and isinstance(mem_val, list):
                                mem_msgs = mem_val
                                break
                    # 填充完整 System Prompt 与历史消息（供前端展示 LLM 实际所见）
                    # ReActAgent.sys_prompt 会动态附加 toolkit 技能提示词，必须取属性，
                    # 而不是只取 format_system_prompt() 的裸输出。
                    actual_system = agent.sys_prompt

                    # 优先使用官方 token counter 统计真实上下文。失败时保留旧字符估算，
                    # 避免展示层因编码兼容问题阻断主流程。
                    try:
                        system_prompt_for_count = await agent.formatter.format(
                            [
                                Msg("system", actual_system, "system"),
                            ],
                        )
                        sys_tokens_est = await token_counter.count(
                            system_prompt_for_count,
                        )
                        prompt_for_count = await agent.formatter.format(
                            [
                                Msg("system", actual_system, "system"),
                                *mem_msgs,
                            ],
                        )
                        total_est = await token_counter.count(prompt_for_count)
                    except Exception:
                        hist_tokens_est = 0
                        for m in mem_msgs:
                            m_content = getattr(m, "content", "")
                            if isinstance(m_content, list):
                                for c in m_content:
                                    if isinstance(c, dict):
                                        hist_tokens_est += len(
                                            str(c.get("text", c.get("output", ""))),
                                        )
                                    else:
                                        hist_tokens_est += len(str(c))
                            else:
                                hist_tokens_est += len(str(m_content))
                        total_est = sys_tokens_est + hist_tokens_est

                    viz.set_tokens(TokenStats(
                        system_tokens=sys_tokens_est,
                        history_tokens=max(total_est - sys_tokens_est, 0),
                        input_tokens=len(user_text),
                        total_tokens=total_est,
                        budget=token_budget,
                        pct=round(total_est / token_budget * 100, 1) if token_budget else 0,
                    ))
                    viz.set_system_context(actual_system, mem_msgs)

                    # 推送上下文快照事件
                    await q.put({"type": "context_view", "data": viz.to_event(), "msg_id": "ctx"})

                except asyncio.CancelledError:
                    terminal_phase = "canceled"
                    await q.put({"cancel": True, "last": True, "contents": []})
                except Exception as e:
                    console("[error]", e)
                    traceback.print_exc()
                    terminal_phase = "error"
                    await q.put({
                        "type": "status",
                        "phase": "error",
                        "detail": f"出错了：{e}",
                    })
                    await q.put({
                        "error": str(e),
                        "last": True,
                        "contents": [],
                    })
                finally:
                    # 停心跳
                    heartbeat_stop.set()
                    try:
                        await asyncio.wait_for(hb_task, timeout=2)
                    except (asyncio.TimeoutError, asyncio.CancelledError):
                        hb_task.cancel()
                    # 推终态：绝不静默。让前端明确知道这一轮为什么结束。
                    if terminal_phase == "paused":
                        # 暂停不算结束，不推 done；心跳已停，等 /approve 后新一轮会重新起心跳
                        pass
                    elif terminal_phase == "max_iters":
                        await q.put({
                            "type": "status",
                            "phase": "max_iters",
                            "detail": f"已达工具调用上限（{max_iters} 步），本轮结束。如需继续请告诉我下一步。",
                        })
                    elif terminal_phase != "done" and terminal_phase != "canceled":
                        # done / canceled 已在对应分支推过内容，这里只补一个收尾状态
                        await q.put({
                            "type": "status",
                            "phase": terminal_phase,
                            "detail": "本轮结束。",
                        })
                    if terminal_phase != "paused":
                        await q.put({"type": "status", "phase": "done", "detail": "完成"})
                    await q.put(None)

            request.stream_task = asyncio.create_task(streaming())
            request._cancel_q = q  # 取消时直接往这里灌 cancel 事件，不等 LLM 迭代

            # 单轮整体超时兜底：streaming 内部若因上游异常（如 agentscope 对畸形
            # block 崩溃、模型流挂起等）迟迟不 put(None)，这里必须强制终止本轮，
            # 否则 agent_runner 会永久 await q.get() → 该会话此后所有请求都卡死
            # （表现为"发消息没反应/卡住不动"）。
            round_timeout = float(cfg.get("agent", {}).get("round_timeout", 600))
            while True:
                try:
                    msg = await asyncio.wait_for(q.get(), timeout=round_timeout)
                except asyncio.TimeoutError:
                    request.stream_task.cancel()
                    try:
                        await asyncio.wait_for(request.stream_task, timeout=3)
                    except (asyncio.CancelledError, asyncio.TimeoutError):
                        pass
                    except Exception:
                        pass
                    await response_q.put({
                        "type": "status",
                        "phase": "error",
                        "detail": f"本轮处理超时（>{round_timeout:.0f}s），已强制终止。请重试。",
                    })
                    await response_q.put({
                        "type": "error",
                        "last": True,
                        "contents": [],
                    })
                    break
                await response_q.put(msg)
                if msg is None:
                    break

            # 本轮完成，落盘
            viz.commit()

        except Exception as e:
            console("[runner-error]", e)
            traceback.print_exc()
        finally:
            await response_q.put(None)
            await sess.finish_request(request)


# ---- 工厂函数 ----

SESS_MGR = SessionManager(expires=300)


async def get_or_create_agent(session_id: str, cfg: dict = None, workspace_dir: str = "workspace") -> Session:
    """获取或创建会话（自动启动 agent_runner 后台任务）。"""
    if cfg is None:
        cfg = load_config()
    SESS_MGR.expires = float(cfg.get("session", {}).get("expires", 300))
    return await SESS_MGR.get_or_create_session(
        session_id,
        create=True,
        session_main=lambda s: agent_runner(s, cfg, workspace_dir),
    )
