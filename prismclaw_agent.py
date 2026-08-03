"""PrismClaw Agent 核心。

基于 AgentScope ReActAgent + PrismClawGuard Mixin，注入：
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

from prismclaw_guard import PrismClawGuardMixin, PendingStatus
from session import Session, SessionManager, AgentRequest, SessionStatus
from tools import (
    build_toolkit,
    format_system_prompt,
    REASONING_HINT_TEMPLATE,
)
from model_config import build_model, load_config
from context_viz import ContextViz, TokenStats
from conf import FLAGS


# ---- Mixin 组装：PrismClawAgent = PrismClawGuard + ReActAgent ----

class PrismClawAgent(PrismClawGuardMixin, ReActAgent):
    """PrismClaw Agent = ReActAgent + HITL 工具确认。"""
    pass


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
    返回 True 表示已处理（需要重新驱动 agent 继续），False 表示普通消息。"""
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
        return False

    cmd = text[1:]
    if cmd == "approve":
        pending = await sess.get_pending_tool()
        if pending:
            pending.status = PendingStatus.APPROVED
            return True  # 有 pending → 需要重新驱动 agent
        return True  # 没有 pending → 忽略（静默返回）
    elif cmd == "reject":
        pending = await sess.get_pending_tool()
        if pending:
            pending.status = PendingStatus.REJECTED
            return True
        return True
    elif cmd == "approve_all":
        sess.auto_approve = True
        pending = await sess.get_pending_tool()
        if pending:
            pending.status = PendingStatus.APPROVED
        return True  # 不管有没有 pending，都要返回（避免 LLM 空转）
    elif cmd == "approve_all off":
        sess.auto_approve = False
        return True
    return False  # 不认识的 / 命令，交给 agent 处理


# ---- Agent Runner（每会话一个后台任务） ----

async def agent_runner(sess: Session, cfg: dict, workspace_dir: str):
    """每个 session 的后台 Agent 循环。"""
    ctx_cfg = cfg.get("context", {})
    token_budget = ctx_cfg.get("token_budget", 32000)
    max_iters = cfg.get("agent", {}).get("max_iters", 50)
    log_dir = cfg.get("logging", {}).get("dir", "session_logs")

    viz = ContextViz(log_dir, sess.session_id)

    # 整个会话复用同一个 Agent（保留 memory，实现多轮上下文真实累积）
    toolkit = await build_toolkit(workspace_dir)
    system_prompt = format_system_prompt([], workspace_dir)
    model = build_model(cfg, stream=True)
    agent = PrismClawAgent(
        name="PrismClaw",
        sys_prompt=system_prompt,
        model=model,
        formatter=OpenAIChatFormatter(),
        toolkit=toolkit,
        memory=InMemoryMemory(),
        max_iters=max_iters,
        parallel_tool_calls=cfg.get("agent", {}).get("parallel_tool_calls", True),
        _prismclaw_sess=sess,
    )
    agent.set_console_output_enabled(False)
    await register_reasoning_hint(agent)
    await register_keepalive(agent, sess)
    await register_thinking_cleanup(agent)

    while True:
        request, status = await sess.get_request()
        if status == SessionStatus.INACTIVE:
            viz.commit()  # 最后一次落盘（同步方法，返回 md 路径）
            await sess.release()
            break

        await sess.activate()

        # 魔术命令
        is_magic = await handle_magic_command(request, sess)
        pending_exists = bool(await sess.get_pending_tool()) if is_magic else False

        # 独立魔法命令（无 pending）：直接返回确认，不调 LLM
        if is_magic and not pending_exists:
            response_q = request.response_queue
            await response_q.put({
                "type": "status",
                "phase": "thinking",
                "step": 0,
                "detail": ("已开启自动放行模式" if sess.auto_approve else "已执行"),
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

            # 估算 System Prompt tokens（CJK 友好：每字 ~1 token）
            sys_chars = len(system_prompt)
            sys_tokens_est = sys_chars  # CJK 简化估算

            inputs = Msg(name="user", content=request.content, role="user")

            q = asyncio.Queue()
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
                    hist_tokens_est = 0
                    for m in mem_msgs:
                        m_content = getattr(m, "content", "")
                        if isinstance(m_content, list):
                            for c in m_content:
                                if isinstance(c, dict):
                                    hist_tokens_est += len(str(c.get("text", c.get("output", ""))))
                                else:
                                    hist_tokens_est += len(str(c))
                        else:
                            hist_tokens_est += len(str(m_content))
                    total_est = sys_tokens_est + hist_tokens_est
                    viz.set_tokens(TokenStats(
                        system_tokens=sys_tokens_est,
                        history_tokens=hist_tokens_est,
                        input_tokens=len(user_text),
                        total_tokens=total_est,
                        budget=token_budget,
                        pct=round(total_est / token_budget * 100, 1) if token_budget else 0,
                    ))

                    # 填充完整 System Prompt 与历史消息（供前端展示 LLM 实际所见）
                    # system_prompt 是 format_system_prompt() 裸输出；mem_msgs[0] 含 AgentScope 注入的技能指令
                    actual_system = system_prompt
                    if mem_msgs:
                        fm = mem_msgs[0]
                        if getattr(fm, "role", "") == "system":
                            fc = getattr(fm, "content", "")
                            if isinstance(fc, str) and fc:
                                actual_system = fc
                    viz.set_system_context(actual_system, mem_msgs)

                    # 推送上下文快照事件
                    await q.put({"type": "context_view", "data": viz.to_event(), "msg_id": "ctx"})

                except asyncio.CancelledError:
                    terminal_phase = "canceled"
                    await q.put({"cancel": True, "last": True, "contents": []})
                except Exception as e:
                    print(f"[PrismClaw] Error: {e}\n{traceback.format_exc()}")
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
            while True:
                msg = await q.get()
                await response_q.put(msg)
                if msg is None:
                    break

            # 本轮完成，落盘
            viz.commit()

        except Exception as e:
            print(f"[PrismClaw] Runner error: {e}\n{traceback.format_exc()}")
        finally:
            await response_q.put(None)
            await sess.finish_request(request)


# ---- 工厂函数 ----

SESS_MGR = SessionManager(expires=300)


async def get_or_create_agent(session_id: str, cfg: dict = None, workspace_dir: str = "workspace") -> Session:
    """获取或创建会话（自动启动 agent_runner 后台任务）。"""
    if cfg is None:
        cfg = load_config()
    return await SESS_MGR.get_or_create_session(
        session_id,
        create=True,
        session_main=lambda s: agent_runner(s, cfg, workspace_dir),
    )
