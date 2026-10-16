"""PrismHarness 核心（AgentScope 2.0）。

AgentScope 2.0 把 ReActAgent + Memory + Formatter 合并成一个 `Agent`，
对话状态挂在 `agent.state`（AgentState），模型输出是事件流（reply_stream）。
本模块：

- 用官方 `Agent` + 中间件（on_system_prompt 动态注入 persona/技能）。
- HITL 走框架自带权限引擎：受保护工具在注册时打 ASK 权限，agent yield
  RequireUserConfirmEvent 后**停泊并结束本轮**（与 1.0 的交互一致：确认卡片
  结束本轮，用户 /approve 再用 UserConfirmResultEvent 驱动下一轮续跑）。
- 结构化提问（ask_user_question）走 RequireExternalExecutionEvent /
  ExternalExecutionResultEvent。
- 把 2.0 事件流映射成前端既有的 SSE 信封（status/contents/...），前端无需大改。
"""

import asyncio
import json
import os
import time
import traceback
from datetime import datetime

from agentscope.agent import Agent, ReActConfig, ContextConfig, InjectionConfig
from agentscope.message import (
    Msg,
    UserMsg,
    TextBlock,
    ToolResultBlock,
)
from agentscope.middleware import MiddlewareBase
from agentscope.event import (
    RequireUserConfirmEvent,
    RequireExternalExecutionEvent,
    UserConfirmResultEvent,
    ExternalExecutionResultEvent,
    ConfirmResult,
    ReplyStartEvent,
    ReplyEndEvent,
    ThinkingBlockDeltaEvent,
    TextBlockDeltaEvent,
    TextBlockEndEvent,
    ToolCallStartEvent,
    ToolCallDeltaEvent,
    ToolCallEndEvent,
    ToolResultStartEvent,
    ToolResultTextDeltaEvent,
    ToolResultEndEvent,
)

from session import Session, SessionManager, AgentRequest, SessionStatus, PendingConfirm
from tools import (
    build_toolkit,
    format_system_prompt,
)
from model_config import build_model, load_config
from context_viz import ContextViz, TokenStats
from conf import FLAGS, console, GUARD_TIMEOUT


# =====================================================================
# 请求级探针：拦截 OpenAI client 的 create()，拿到 100% 真实的请求 payload
# =====================================================================

def _block_text(block) -> str:
    if block is None:
        return ""
    if isinstance(block, str):
        return block
    if isinstance(block, dict):
        t = block.get("type", "")
        if t == "text":
            return str(block.get("text", ""))
        if t == "thinking":
            return str(block.get("thinking", ""))
        return str(block.get("text", ""))
    t = getattr(block, "type", "")
    if t == "text":
        return str(getattr(block, "text", ""))
    if t == "thinking":
        return str(getattr(block, "thinking", ""))
    return str(getattr(block, "text", ""))


def _msg_text(msg) -> str:
    content = getattr(msg, "content", None)
    if content is None and isinstance(msg, dict):
        content = msg.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(_block_text(b) for b in content)
    return str(content) if content is not None else ""


def _is_internal_hint_msg(msg) -> bool:
    role = getattr(msg, "role", None) or (msg.get("role") if isinstance(msg, dict) else None)
    return role == "user" and "内部信息，非用户输入" in _msg_text(msg)


def _extract_payload(kwargs: dict) -> dict:
    eb = kwargs.get("extra_body") or {}
    ct = eb.get("chat_template_kwargs") or {}
    return {
        "params": {
            "model": kwargs.get("model"),
            "temperature": kwargs.get("temperature"),
            "top_p": kwargs.get("top_p"),
            "max_tokens": kwargs.get("max_tokens") or kwargs.get("max_completion_tokens"),
            "stream": kwargs.get("stream"),
            "reasoning_effort": kwargs.get("reasoning_effort"),
            "thinking": ct.get("enable_thinking"),
            "tool_choice": kwargs.get("tool_choice"),
        },
        "messages": kwargs.get("messages") or [],
        "tools": kwargs.get("tools") or [],
        "raw_keys": sorted(kwargs.keys()),
    }


def _wrap_openai_client(model, sink: dict):
    """包一层 model.client.chat.completions 的 create/stream，拦截真实请求 payload。"""
    client = getattr(model, "client", None)
    if client is None:
        return
    try:
        completions = client.chat.completions
    except Exception:
        return
    orig_create = completions.create
    orig_stream = getattr(completions, "stream", None)

    async def _capture(kwargs):
        emit = sink.get("emit_request_payload")
        if emit is None:
            return
        try:
            sink["step"] = sink.get("step", 0) + 1
            payload = _extract_payload(kwargs)
            payload["step"] = sink["step"]
            await emit(payload)
        except Exception:
            pass

    async def patched_create(**kwargs):
        await _capture(kwargs)
        return await orig_create(**kwargs)

    completions.create = patched_create

    if orig_stream is not None:
        def patched_stream(**kwargs):
            try:
                asyncio.get_event_loop().create_task(_capture(kwargs))
            except Exception:
                pass
            return orig_stream(**kwargs)

        completions.stream = patched_stream


# =====================================================================
# 中间件：动态 system prompt
# =====================================================================

class PersonaPromptMiddleware(MiddlewareBase):
    """每次取 system prompt 时从磁盘重建 persona（IDENTITY/AGENTS/SOUL/USER），
    改文件后下一轮立即生效。"""

    def __init__(self, workspace_dir: str):
        self.workspace_dir = workspace_dir

    async def on_system_prompt(self, agent: Agent, current_prompt: str) -> str:
        try:
            fresh = format_system_prompt([], self.workspace_dir)
        except Exception:
            return current_prompt
        # 保留 2.0 可能已附加的 skills 尾部
        extra = ""
        for kw in ("<agent-skills", "# Skills", "Skills 使用指南"):
            idx = current_prompt.find(kw) if current_prompt else -1
            if idx != -1:
                extra = "\n\n" + current_prompt[idx:]
                break
        return fresh + extra


# =====================================================================
# 魔术命令：只解析，不改 pending（续跑由 agent_runner 用事件驱动）
# =====================================================================

async def handle_magic_command(request: AgentRequest, sess: Session):
    """解析 /approve /reject /approve_all /answer /team。

    返回 (已处理, 立即回执文案)。对 approve/reject/approve_all/answer，
    只把意图记到 request.magic_cmd，真正的续跑由 agent_runner 构造
    UserConfirmResultEvent / ExternalExecutionResultEvent 驱动下一轮。
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
    if cmd in ("approve", "reject", "approve_all"):
        request.magic_cmd = (cmd, None)
        return True, ""
    if cmd == "approve_all off":
        sess.auto_approve = False
        return True, "已关闭自动放行模式。"
    if cmd.startswith("answer"):
        raw = cmd[len("answer"):].strip()
        answer = None
        if raw:
            try:
                answer = json.loads(raw)
            except Exception:
                answer = {"free_text": raw}
        request.magic_cmd = ("answer", answer)
        return True, ""
    if cmd.startswith("team"):
        rest = cmd[len("team"):].strip()
        if not rest:
            return True, "用法：/team <团队名称> <任务>。团队在设置页「团队」里创建。"
        parts = rest.split(" ", 1)
        team_name = parts[0].strip()
        task = parts[1].strip() if len(parts) > 1 else ""
        try:
            import team as team_mod
            if not FLAGS.get("enable_team", True):
                return True, "团队编排功能已关闭（可在「功能开关」中开启）。"
            spec = team_mod.load_team(team_name, "workspace")
            if not spec:
                return True, f"找不到团队「{team_name}」。可点输入框上方「🎭 团队」按钮运行。"
        except Exception as e:
            return True, f"读取团队失败：{e}"
        request.team_run = (team_name, task)
        return True, ""
    return False, ""


async def _build_resume_inputs(sess: Session, request: AgentRequest):
    """根据魔术命令 + 会话停泊，构造续跑 reply_stream 的输入事件。

    返回 None 表示没有可续跑的停泊（调用方只回执）。
    """
    cmd = getattr(request, "magic_cmd", None)
    if cmd is None:
        return None
    kind, answer = cmd
    pc = await sess.get_pending()
    if pc is None:
        return None

    # 结构化提问：只有 /answer 能续跑
    if pc.kind == "external":
        if kind != "answer":
            return None
        await sess.pop_pending()
        ans = answer if answer is not None else {"answers": []}
        res = ToolResultBlock(
            type="tool_result",
            id=getattr(pc.tool_calls[0], "id", ""),
            name=getattr(pc.tool_calls[0], "name", "ask_user_question"),
            output=json.dumps({"answers": ans}, ensure_ascii=False),
        )
        return ExternalExecutionResultEvent(reply_id=pc.reply_id, execution_results=[res])

    # 工具确认：approve / reject / approve_all
    if kind == "answer":
        return None
    await sess.pop_pending()
    approved = kind != "reject"
    if kind == "approve_all":
        sess.auto_approve = True
    return UserConfirmResultEvent(
        reply_id=pc.reply_id,
        confirm_results=[
            ConfirmResult(
                confirmed=approved,
                tool_call=tc,
                rules=(tc.suggested_rules or None) if approved else None,
            )
            for tc in pc.tool_calls
        ],
    )


# =====================================================================
# Agent Runner
# =====================================================================

async def agent_runner(sess: Session, cfg: dict, workspace_dir: str):
    ctx_cfg = cfg.get("context", {})
    token_budget = ctx_cfg.get("token_budget", 420000)
    max_iters = cfg.get("agent", {}).get("max_iters", 50)
    log_dir = cfg.get("logging", {}).get("dir", "session_logs")

    viz = ContextViz(log_dir, sess.session_id)
    console("[session-start]", sess.session_id)

    toolkit = await build_toolkit(workspace_dir)
    system_prompt = format_system_prompt([], workspace_dir)
    model = build_model(cfg, stream=True)

    ctx_sink: dict = {}
    _wrap_openai_client(model, ctx_sink)

    context_config = ContextConfig(
        trigger_ratio=float(ctx_cfg.get("compress_threshold", 0.8)),
        reserve_ratio=float(ctx_cfg.get("reserve_ratio", 0.1)),
    )

    agent = Agent(
        name="PrismHarness",
        system_prompt=system_prompt,
        model=model,
        toolkit=toolkit,
        middlewares=[PersonaPromptMiddleware(workspace_dir)],
        react_config=ReActConfig(max_iters=max_iters),
        context_config=context_config,
        injection_config=InjectionConfig(inject_runtime_state=True, timezone="Asia/Shanghai"),
    )

    last_llm_sig = None

    def apply_llm_config(new_cfg: dict) -> bool:
        nonlocal model, last_llm_sig
        sig = (new_cfg.get("llm", {}).get("provider"), new_cfg.get("providers", {}))
        if sig == last_llm_sig:
            return False
        last_llm_sig = sig
        model = build_model(new_cfg, stream=True)
        _wrap_openai_client(model, ctx_sink)
        agent.model = model
        return True

    apply_llm_config(cfg)

    # 终端控制台输出渲染器：**会话级持久**，跨轮/跨停泊复用。
    # ConsoleRenderer 内部累积回复消息（_accumulate 显式处理"无 ReplyStartEvent 的
    # HITL 续跑"），所以工具调用的状态必须跨 park/resume 保留——每轮 new 一个会丢掉
    # 上一轮的工具调用块，续跑时工具结果无处可挂。它会在 ReplyStartEvent 时按新
    # reply_id 自动重置 _msg，故会话级复用对新一轮也安全。
    console_renderer = None

    while True:
        request, status = await sess.get_request()
        if status == SessionStatus.INACTIVE:
            console("[session-end]", sess.session_id)
            # 上下文字迹落盘失败不该拖垮收尾：早先这里裸调，一旦 commit 抛异常
            # （会话目录被删→FileNotFoundError），sess.release() 就被跳过，
            # 会话永远停在 ACTIVE、[session-end] 只打了一半，agent_runner 任务整个
            # 死在「Task exception was never retrieved」里再也起不来。
            try:
                viz.commit()
            except Exception as e:
                console("[viz-error]", e)
            await sess.release()
            break

        await sess.activate()

        try:
            apply_llm_config(load_config())
        except Exception:
            pass

        agent.toolkit = await build_toolkit(workspace_dir)

        # 终端控制台输出（可选）：AgentScope 2.0 的 Agent 默认不打印任何东西，
        # 需要用官方 ConsoleRenderer 渲染事件流到终端。config.agent.console_output
        # 为 true 时开启（等价 1.0 的 set_console_output_enabled(True)）。
        # 渲染器会话级复用（见上），此处只负责按开关惰性创建/关闭。
        want_console = bool(cfg.get("agent", {}).get("console_output", False))
        if not want_console:
            console_renderer = None
        elif console_renderer is None:
            try:
                from agentscope.console import ConsoleRenderer
                console_renderer = ConsoleRenderer()
            except Exception:
                console_renderer = None

        is_magic, magic_ack = await handle_magic_command(request, sess)

        if getattr(request, "team_run", None):
            await _run_team_path(request, sess, cfg, workspace_dir)
            continue

        try:
            if is_magic:
                resume = await _build_resume_inputs(sess, request)
                if resume is None:
                    response_q = request.response_queue
                    await response_q.put({
                        "type": "status", "phase": "thinking", "step": 0,
                        "detail": magic_ack or "已执行",
                    })
                    await response_q.put({"type": "status", "phase": "done", "detail": "完成"})
                    await response_q.put(None)
                    await sess.finish_request(request)
                    continue
            else:
                resume = None
            await _run_chat_path(request, sess, cfg, workspace_dir, agent, viz, ctx_sink,
                                 token_budget, max_iters, resume, console_renderer)
        except Exception as e:
            console("[runner-error]", e)
            traceback.print_exc()
        finally:
            try:
                await request.response_queue.put(None)
            except Exception:
                pass
            await sess.finish_request(request)


# ---------------------------------------------------------------------
# 单轮 reply：映射事件到 SSE；遇停泊则发卡片并结束本轮
# ---------------------------------------------------------------------

async def _run_chat_path(request, sess, cfg, workspace_dir, agent, viz, ctx_sink,
                         token_budget, max_iters, resume_inputs, console_renderer=None):
    response_q = request.response_queue

    user_text = ""
    if isinstance(request.content, list):
        user_text = " ".join(
            b.get("text", "") for b in request.content
            if isinstance(b, dict) and b.get("type") == "text"
        )
    elif isinstance(request.content, str):
        user_text = request.content

    if resume_inputs is None:
        viz.start_round(user_text)

    q = asyncio.Queue()

    async def emit_request_payload(payload):
        try:
            viz.record_request_payload(payload)
        except Exception:
            pass
        await q.put({"type": "request_payload", "data": payload, "msg_id": "req"})

    ctx_sink["emit_request_payload"] = emit_request_payload

    async def streaming():
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
                await q.put({"type": "status", "phase": "heartbeat",
                             "detail": f"⏳ 仍在工作（{tick}）— {current_phase['text']}"})

        hb_task = asyncio.create_task(heartbeat())
        terminal = "done"
        try:
            inputs = resume_inputs or UserMsg(name="user", content=user_text)
            terminal = await _stream_one_reply(
                agent, inputs, q, sess, request, max_iters, current_phase, viz, console_renderer
            )
        except asyncio.CancelledError:
            terminal = "canceled"
            await q.put({"cancel": True, "last": True, "contents": []})
        except Exception as e:
            console("[error]", e)
            traceback.print_exc()
            terminal = "error"
            await q.put({"type": "status", "phase": "error", "detail": f"出错了：{e}"})
            await q.put({"error": str(e), "last": True, "contents": []})
        finally:
            heartbeat_stop.set()
            try:
                await asyncio.wait_for(hb_task, timeout=2)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                hb_task.cancel()

            if terminal == "max_iters":
                await q.put({"type": "status", "phase": "max_iters",
                             "detail": f"已达工具调用上限（{max_iters} 步），本轮结束。"})
            elif terminal not in ("done", "canceled", "parked_confirm", "parked_external"):
                await q.put({"type": "status", "phase": terminal, "detail": "本轮结束。"})

            # 停泊（确认卡片 / 提问卡片）也照 1.0 收尾推 done：前端据此收起 spinner 并保留卡片，
            # 之后的 /approve、/answer 会用新的一轮把续跑内容推出来。canceled 不推 done。
            if terminal != "canceled":
                await _emit_context_snapshot(q, agent, viz, user_text, token_budget)
                await q.put({"type": "status", "phase": "done", "detail": "完成"})
            await q.put(None)

    request.stream_task = asyncio.create_task(streaming())
    request._cancel_q = q

    round_timeout = float(cfg.get("agent", {}).get("round_timeout", 3600))
    while True:
        try:
            msg = await asyncio.wait_for(q.get(), timeout=round_timeout)
        except asyncio.TimeoutError:
            request.stream_task.cancel()
            try:
                await asyncio.wait_for(request.stream_task, timeout=3)
            except (asyncio.CancelledError, asyncio.TimeoutError, Exception):
                pass
            await response_q.put({"type": "status", "phase": "error",
                                  "detail": f"本轮处理超时（>{round_timeout:.0f}s），已强制终止。"})
            await response_q.put({"error": "timeout", "last": True, "contents": []})
            break
        await response_q.put(msg)
        if msg is None:
            break

    # 落盘失败只该丢一份上下文字迹，不该把这一轮对话判死。
    try:
        viz.commit()
    except Exception as e:
        console("[viz-error]", e)


async def _stream_one_reply(agent, inputs, q, sess, request, max_iters, current_phase, viz=None, renderer=None):
    """跑一次（或若干次自动续跑）reply_stream，把事件映射进 q。

    Returns: "done" / "canceled" / "parked_confirm" / "parked_external" / "max_iters"
    """
    while True:
        text_by_block: dict = {}
        tc_name: dict = {}
        tc_args: dict = {}
        tr_name: dict = {}
        tr_text: dict = {}
        parked_confirm = None
        parked_external = None
        terminal = "done"
        step = 0
        seen_tool_ids = set()

        async for event in agent.reply_stream(inputs):
            if request.canceled:
                await q.put({"cancel": True, "last": True, "contents": []})
                return "canceled"

            # 终端控制台输出（可选）：把事件交给官方 ConsoleRenderer 打印
            if renderer is not None:
                try:
                    renderer.render(event)
                except Exception:
                    pass

            if isinstance(event, ReplyStartEvent):
                await q.put({"type": "status", "phase": "thinking", "step": 0, "detail": "正在思考…"})

            elif isinstance(event, ThinkingBlockDeltaEvent):
                delta = getattr(event, "delta", "") or ""
                if delta:
                    preview = delta[-120:].replace("\n", " ").strip()
                    current_phase["text"] = f"🧠 推理… {preview}"
                    await q.put({"type": "status", "phase": "reasoning",
                                 "detail": delta, "preview": preview})

            elif isinstance(event, TextBlockDeltaEvent):
                text_by_block[event.block_id] = text_by_block.get(event.block_id, "") + (event.delta or "")

            elif isinstance(event, TextBlockEndEvent):
                text = getattr(event, "text", None) or text_by_block.get(event.block_id, "")
                if text:
                    current_phase["text"] = f"推理中… {text[:80].replace(chr(10),' ').strip()}"
                    await q.put({"msg_id": event.reply_id, "last": False,
                                 "contents": [{"type": "text", "content": text}]})

            elif isinstance(event, ToolCallStartEvent):
                tc_name[event.tool_call_id] = event.tool_call_name
                tc_args.setdefault(event.tool_call_id, "")

            elif isinstance(event, ToolCallDeltaEvent):
                tc_args[event.tool_call_id] = tc_args.get(event.tool_call_id, "") + (event.delta or "")

            elif isinstance(event, ToolCallEndEvent):
                tid = event.tool_call_id
                name = tc_name.get(tid, "")
                raw = tc_args.get(tid, "")
                try:
                    args = json.loads(raw) if raw else {}
                except Exception:
                    args = {"raw": raw}
                if tid not in seen_tool_ids:
                    seen_tool_ids.add(tid)
                    step += 1
                    current_phase["text"] = f"调用工具 {name}（第 {step} 步）"
                    await q.put({"type": "status", "phase": "acting", "step": step,
                                 "detail": f"正在调用工具：{name}（第 {step} 步）", "tool_name": name})
                await q.put({"msg_id": event.reply_id, "last": False, "contents": [
                    {"type": "tool_use", "tool_use_id": tid, "name": name, "input": args}
                ]})
                if viz is not None:
                    try:
                        viz.add_tool_call(step, name, args)
                    except Exception:
                        pass

            elif isinstance(event, ToolResultStartEvent):
                tr_name[event.tool_call_id] = event.tool_call_name
                tr_text.setdefault(event.tool_call_id, "")

            elif isinstance(event, ToolResultTextDeltaEvent):
                tr_text[event.tool_call_id] = tr_text.get(event.tool_call_id, "") + (event.delta or "")

            elif isinstance(event, ToolResultEndEvent):
                tid = event.tool_call_id
                out = tr_text.get(tid, "")
                await q.put({"msg_id": event.reply_id, "last": False, "contents": [
                    {"type": "tool_result", "tool_use_id": tid,
                     "name": tr_name.get(tid, ""), "output": out}
                ]})
                if viz is not None:
                    try:
                        viz.update_tool_result(step, out)
                    except Exception:
                        pass

            elif isinstance(event, RequireUserConfirmEvent):
                parked_confirm = event

            elif isinstance(event, RequireExternalExecutionEvent):
                parked_external = event

            elif isinstance(event, ReplyEndEvent):
                reason = getattr(event, "finished_reason", None)
                if "EXCEED_MAX_ITERS" in str(getattr(reason, "value", reason)):
                    terminal = "max_iters"

        # 事件流结束
        if parked_confirm is None and parked_external is None:
            return terminal

        # auto_approve：确认类直接全部放行，本轮内续跑
        if parked_confirm is not None and sess.auto_approve:
            inputs = UserConfirmResultEvent(
                reply_id=parked_confirm.reply_id,
                confirm_results=[
                    ConfirmResult(confirmed=True, tool_call=tc, rules=tc.suggested_rules or None)
                    for tc in parked_confirm.tool_calls
                ],
            )
            continue

        # 停泊：记录 pending，发卡片，结束本轮
        if parked_confirm is not None:
            await sess.add_pending(PendingConfirm(parked_confirm.reply_id, parked_confirm.tool_calls, "confirm"))
            tc = parked_confirm.tool_calls[0]
            name = getattr(tc, "name", "")
            try:
                args = json.loads(getattr(tc, "input", "{}") or "{}")
            except Exception:
                args = {"raw": getattr(tc, "input", "")}
            current_phase["text"] = "等待你确认工具调用"
            card = (f"🔒 **工具调用需要确认**\n\n**工具:** `{name}`\n**参数:** `{args}`\n\n"
                    f"请输入:\n- `/approve` — 允许执行\n- `/reject` — 拒绝执行")
            await q.put({"type": "status", "phase": "confirm", "detail": card})
            return "parked_confirm"

        # 外部工具（结构化提问）
        tool_call = parked_external.tool_calls[0]
        try:
            args = json.loads(getattr(tool_call, "input", "{}") or "{}")
        except Exception:
            args = {}
        await sess.add_pending(PendingConfirm(parked_external.reply_id, parked_external.tool_calls, "external"))
        await q.put({"type": "status", "phase": "question",
                     "detail": "❓ 需要你的选择", "questions": args.get("questions", [])})
        return "parked_external"


async def _emit_context_snapshot(q, agent, viz, user_text, token_budget):
    mem_msgs = []
    try:
        mem_msgs = agent.state.context or []
    except Exception:
        pass
    actual_system = ""
    try:
        actual_system = agent._system_prompt
    except Exception:
        pass

    total_est = 0
    try:
        import inspect as _inspect
        maybe = agent.model.count_tokens(messages=mem_msgs, tools=None)
        total_est = await maybe if _inspect.isawaitable(maybe) else maybe
    except Exception:
        total_est = sum(len(str(getattr(m, "content", ""))) for m in mem_msgs)

    # 真实容量以模型的 context_size 为准（框架压缩阈值 = trigger_ratio × context_size）；
    # config 的 context.token_budget 仅在模型未暴露 context_size 时兜底。
    ctx_size, trigger = 0, 0.8
    try:
        ctx_size = int(getattr(agent.model, "context_size", 0) or 0)
        trigger = float(getattr(agent.context_config, "trigger_ratio", 0.8))
    except Exception:
        pass
    budget = ctx_size or token_budget
    compress_at = int(trigger * ctx_size) if ctx_size else 0

    # 本轮是否发生压缩：框架把压缩摘要写进 AgentState.summary
    summary = ""
    try:
        raw = agent.state.summary
        if isinstance(raw, str):
            summary = raw
        elif raw:
            summary = "\n".join(getattr(b, "text", "") or "" for b in raw).strip()
    except Exception:
        summary = ""

    sys_tokens_est = len(actual_system)
    try:
        viz.set_tokens(TokenStats(
            system_tokens=sys_tokens_est,
            history_tokens=max(total_est - sys_tokens_est, 0),
            input_tokens=len(user_text),
            total_tokens=total_est,
            budget=budget,
            pct=round(total_est / budget * 100, 1) if budget else 0,
            compressed=bool(summary),
        ))
        viz.set_compression(summary, compress_at)
        viz.set_system_context(actual_system, mem_msgs)
        await q.put({"type": "context_view", "data": viz.to_event(), "msg_id": "ctx"})
    except Exception as e:
        console("[viz-error]", e)


# ---------------------------------------------------------------------
# 团队编排路径
# ---------------------------------------------------------------------

async def _run_team_path(request, sess, cfg, workspace_dir):
    response_q = request.response_queue
    team_name, team_task = request.team_run
    try:
        import team as team_mod
        team_spec = team_mod.load_team(team_name, workspace_dir)
        if not team_spec:
            await response_q.put({"type": "status", "phase": "thinking", "step": 0,
                                  "detail": f"找不到团队「{team_name}」。"})
            await response_q.put({"type": "status", "phase": "done", "detail": "完成"})
            await response_q.put(None)
            return

        log_dir = cfg.get("logging", {}).get("dir", "session_logs")
        run_id = "run_" + time.strftime("%Y%m%d_%H%M%S")
        run_dir = os.path.join(log_dir, sess.session_id, "teams", run_id)

        is_multi_stage = team_spec.get("type") in ("roundtable", "pipeline")
        team_timeout = float(cfg.get("agent", {}).get("roundtable_timeout", 1800 if is_multi_stage else 600))
        member_timeout = 600.0 if is_multi_stage else 300.0
        max_concurrent = int(cfg.get("agent", {}).get("team_max_concurrent", 4))

        async def run_team_and_emit():
            await response_q.put({"type": "status", "phase": "thinking", "step": 0,
                                  "detail": f"🎭 启动团队「{team_name}」并行编排…"})
            report, results = await team_mod.run_team_stream(
                team_spec, team_task, emit_team, cfg, workspace_dir,
                run_dir=run_dir, member_timeout=member_timeout, max_concurrent=max_concurrent,
            )
            if team_mod.is_interrupted_output(report):
                report = ("⚠️ 本次编排被中断，未能产出最终报告。建议检查模型端点稳定性后重跑；"
                          "各成员中间产出已落盘在 session_logs 对应目录下。")
            else:
                # 有成员跑满迭代上限时，把名单附在报告里：面板那一行的标注，
                # 读报告的人看不到。
                report = (report or "") + team_mod.truncation_warning(results)
            await response_q.put({"msg_id": "team-report", "last": True,
                                  "contents": [{"type": "text", "content": report}]})

        # 心跳：团队成员各自跑一整轮 ReAct（含联网/命令），中途 SSE 可能安静好几分钟。
        # 没有心跳时状态栏一直停在「启动团队…」，看起来就像卡死了（2026-10-15 报障：
        # 「19 人的一直不动」）。每 10s 报一次进度，慢但在动，用户看得出来。
        hb_stop = asyncio.Event()
        # running/queued 分开记：成员受 team_max_concurrent 限流，19 个里同时只有 4 个
        # 真在跑。只报「已完成 N」时，N 长时间不动看着还是像卡死；报出「运行中 4，排队 15」
        # 才说明它确实在按并发上限推进。
        progress = {"done": 0, "failed": 0, "running": 0, "queued": 0}

        async def emit_team(ev):
            phase = ev.get("phase")
            if phase == "member_queued":
                progress["queued"] += 1
            elif phase == "member_start":
                # queued → running；风控/决策阶段没有 queued 事件，夹到 0 免得变负
                progress["queued"] = max(0, progress["queued"] - 1)
                progress["running"] += 1
            elif phase == "member_done":
                progress["running"] = max(0, progress["running"] - 1)
                progress["done"] += 1
            elif phase == "member_error":
                progress["running"] = max(0, progress["running"] - 1)
                progress["failed"] += 1
            if request.canceled:
                return
            await response_q.put({"type": "team", **ev})

        async def team_heartbeat():
            tick = 0
            while not hb_stop.is_set():
                try:
                    await asyncio.wait_for(hb_stop.wait(), timeout=10)
                except asyncio.TimeoutError:
                    pass
                if hb_stop.is_set():
                    break
                tick += 1
                parts = [f"已完成 {progress['done']}", f"失败 {progress['failed']}"]
                if progress["running"]:
                    parts.append(f"运行中 {progress['running']}")
                if progress["queued"]:
                    parts.append(f"排队 {progress['queued']}")
                await response_q.put({
                    "type": "status", "phase": "heartbeat",
                    "detail": (f"🎭 {team_name} 运行中（{tick * 10}s）— " + "，".join(parts)),
                })

        hb_task = asyncio.create_task(team_heartbeat())

        team_run_task = asyncio.create_task(run_team_and_emit())
        request.stream_task = team_run_task
        try:
            await asyncio.wait_for(team_run_task, timeout=team_timeout)
            if request.canceled:
                await response_q.put({"cancel": True, "last": True, "contents": []})
            else:
                await response_q.put({"type": "status", "phase": "done", "detail": "完成"})
        except asyncio.TimeoutError:
            team_run_task.cancel()
            # 超时同样会丢下一堆没跑完的成员，面板要收尾，不然一直挂着「分析中」
            await response_q.put({"type": "team", "phase": "canceled"})
            await response_q.put({"type": "status", "phase": "error",
                                  "detail": f"团队运行超时（>{team_timeout:.0f}s），已终止。"})
            await response_q.put({"error": "team timeout", "last": True, "contents": []})
        except asyncio.CancelledError:
            if not request.canceled:
                raise
            # 服务端侧的取消（会话释放、被别的请求顶掉等）：通知还连着的客户端收尾面板。
            # 用户点「停止」那条路前端会先 abort SSE、收不到这条，它另行本地收尾；
            # 两条路都覆盖，面板才不会永远停在「分析中」。
            await response_q.put({"type": "team", "phase": "canceled"})
            await response_q.put({"cancel": True, "last": True, "contents": []})
        finally:
            hb_stop.set()
            try:
                await asyncio.wait_for(hb_task, timeout=2)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                hb_task.cancel()
    except asyncio.CancelledError:
        raise
    except Exception as e:
        console("[team-error]", e)
        traceback.print_exc()
        await response_q.put({"type": "status", "phase": "error", "detail": f"团队编排出错：{e}"})
        await response_q.put({"error": str(e), "last": True, "contents": []})
    finally:
        try:
            await response_q.put(None)
        except Exception:
            pass
        await sess.finish_request(request)


# ---- 工厂函数 ----

SESS_MGR = SessionManager(expires=300)


async def get_or_create_agent(session_id: str, cfg: dict = None, workspace_dir: str = "workspace") -> Session:
    if cfg is None:
        cfg = load_config()
    SESS_MGR.expires = float(cfg.get("session", {}).get("expires", 300))
    return await SESS_MGR.get_or_create_session(
        session_id,
        create=True,
        session_main=lambda s: agent_runner(s, cfg, workspace_dir),
    )
