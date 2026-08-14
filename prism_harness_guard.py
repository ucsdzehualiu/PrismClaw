"""PrismHarnessGuard — HITL 工具调用确认。

基于 ReActAgent 扩展，实现工具调用的人工确认：
- _acting：高风险工具 → 往 memory 加"等待确认"的 ToolResultBlock → add_pending_tool → return None
  （假装执行完，ReActAgent 进入下一轮 reasoning）
- _reasoning：有 pending → PENDING 发确认卡片 / APPROVED 重放 tool_use / REJECTED 通知模型
  （靠轮询 pending 状态实现"等待"，不挂起协程、不抛异常）
- approve/reject 只改 pending.status，下一轮 _reasoning 自然读到
"""

import time
from typing import Any, Optional

from agentscope.message import Msg, ToolUseBlock, ToolResultBlock, TextBlock

from conf import GUARD_TOOLS, GUARD_TIMEOUT


class PendingStatus:
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class PendingToolUse:
    def __init__(self, tool_use: dict):
        self.tool_use = tool_use
        self.status = PendingStatus.PENDING
        self.created_at = time.time()

    @property
    def name(self) -> str:
        return self.tool_use.get("name", "unknown")

    @property
    def input(self) -> Any:
        return self.tool_use.get("input", {})

    def is_expired(self, timeout: float) -> bool:
        """等待超过 timeout 秒仍未确认 → 视为超时（仅在 status==PENDING 时判断）。"""
        return time.time() - self.created_at > timeout

    def resolve(self, approved: bool) -> None:
        """供 session.resolve_pending() 调用，设置状态以唤醒轮询。"""
        self.status = PendingStatus.APPROVED if approved else PendingStatus.REJECTED


TOOL_REJECTED_TEMPLATE = """
(内部信息，非用户输入，禁止直接透露给用户)
用户拒绝了 {tool_name}({tool_input}) 的执行请求。请判断是否还有其他未完成的步骤需要推进，或直接向用户说明情况。
"""


def _fake_result_mark(tool_use_id: str) -> str:
    """给拦截时写入的假 tool_result 打上可精确清理的 mark。"""
    return f"PRISM_HARNESS_HITL_FAKE:{tool_use_id}"


class PrismHarnessGuardMixin:
    """重写 _reasoning 和 _acting，靠 pending 状态轮询实现确认后执行。"""

    def __init__(self, *args, **kwargs) -> None:
        self._prism_harness_sess = kwargs.pop("_prism_harness_sess", None)
        super().__init__(*args, **kwargs)

    async def _reasoning(self, tool_choice=None) -> Msg:
        while True:
            pending = await self._prism_harness_sess.get_pending_tool() if self._prism_harness_sess else None

            if not pending:
                return await super()._reasoning(tool_choice)

            # 超时兜底：pending 等待超过 GUARD_TIMEOUT 仍无人确认 → 自动按拒绝处理，
            # 走下方 REJECTED 分支 pop 掉并通知模型，避免 stale 确认卡永久卡住后续对话。
            if pending.status == PendingStatus.PENDING and pending.is_expired(GUARD_TIMEOUT):
                pending.status = PendingStatus.REJECTED

            tool_name = pending.name
            tool_input = pending.input

            if pending.status == PendingStatus.PENDING:
                # 发确认卡片，return msg（不调 super()._reasoning）
                content = (
                    f"🔒 **工具调用需要确认**\n\n"
                    f"**工具:** `{tool_name}`\n"
                    f"**参数:** `{tool_input}`\n\n"
                    f"请输入:\n"
                    f"- `/approve` — 允许执行\n"
                    f"- `/reject` — 拒绝执行"
                )
                msg = Msg(
                    role="assistant",
                    content=[TextBlock(type="text", text=content)],
                    name="prism_harness_guard",
                )
                await self.memory.add(msg)
                await self.print(msg, last=True)
                return msg

            elif pending.status == PendingStatus.APPROVED:
                # 移除该工具对应的假 tool_result，然后复用原始 tool_use id。
                # 原始 tool_use 已经在 memory 里，不需要再 add，否则会重复一条。
                tool_use_block = pending.tool_use
                await self.memory.delete_by_mark(
                    _fake_result_mark(tool_use_block["id"]),
                )
                msg = Msg(
                    role="assistant",
                    content=[tool_use_block],
                    name="prism_harness_guard",
                )
                return msg

            elif pending.status == PendingStatus.REJECTED:
                await self.memory.delete_by_mark(
                    _fake_result_mark(pending.tool_use["id"]),
                )
                await self._prism_harness_sess.pop_pending_tool()
                next_pending = await self._prism_harness_sess.get_pending_tool() if self._prism_harness_sess else None
                if next_pending:
                    continue  # 还有下一个待确认
                # 没有了，通知模型重新思考
                hint = TOOL_REJECTED_TEMPLATE.format(tool_name=tool_name, tool_input=tool_input)
                await self.memory.add(
                    Msg(role="user", content=[TextBlock(type="text", text=hint)], name="prism_harness_guard"),
                    marks=["TOOL_REJECTED"],
                )
                try:
                    return await super()._reasoning(tool_choice)
                finally:
                    await self.memory.delete_by_mark("TOOL_REJECTED")

    async def _acting(self, tool_call: ToolUseBlock) -> Optional[dict]:
        pending = await self._prism_harness_sess.get_pending_tool() if self._prism_harness_sess else None

        # 已批准的 pending 工具直接执行（ID 匹配）
        if pending and pending.tool_use.get("id") == tool_call.get("id") and pending.status == PendingStatus.APPROVED:
            await self._prism_harness_sess.pop_pending_tool()
            return await super()._acting(tool_call)

        # auto_approve 模式：跳过确认，直接执行高风险工具
        if getattr(self._prism_harness_sess, "auto_approve", False):
            return await super()._acting(tool_call)

        # 高风险工具 → 假装执行完，入队 pending，return None
        if tool_call.get("name") in GUARD_TOOLS:
            tool_res_msg = Msg(
                "system",
                [
                    ToolResultBlock(
                        type="tool_result",
                        id=tool_call["id"],
                        name=tool_call["name"],
                        output=f"⏳ [等待确认] {tool_call['name']}({tool_call.get('input', {})})",
                    ),
                ],
                "system",
            )
            await self.memory.add(
                tool_res_msg,
                marks=[_fake_result_mark(tool_call["id"])],
            )
            await self.print(tool_res_msg, last=True)
            if self._prism_harness_sess:
                await self._prism_harness_sess.add_pending_tool(PendingToolUse(dict(tool_call)))
            return None

        # 非高风险工具：直接执行
        return await super()._acting(tool_call)
