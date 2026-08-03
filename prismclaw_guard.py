"""PrismClawGuard — HITL 工具调用确认。

基于 ReActAgent 扩展，实现工具调用的人工确认：
- _acting：高风险工具 → 往 memory 加"等待确认"的 ToolResultBlock → add_pending_tool → return None
  （假装执行完，ReActAgent 进入下一轮 reasoning）
- _reasoning：有 pending → PENDING 发确认卡片 / APPROVED 重放 tool_use / REJECTED 通知模型
  （靠轮询 pending 状态实现"等待"，不挂起协程、不抛异常）
- approve/reject 只改 pending.status，下一轮 _reasoning 自然读到
"""

import uuid
from typing import Any, Optional

from agentscope.message import Msg, ToolUseBlock, ToolResultBlock, TextBlock

from conf import GUARD_TOOLS


class PendingStatus:
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class PendingToolUse:
    def __init__(self, tool_use: dict):
        self.tool_use = tool_use
        self.status = PendingStatus.PENDING

    @property
    def name(self) -> str:
        return self.tool_use.get("name", "unknown")

    @property
    def input(self) -> Any:
        return self.tool_use.get("input", {})


TOOL_REJECTED_TEMPLATE = """
(内部信息，非用户输入，禁止直接透露给用户)
用户拒绝了 {tool_name}({tool_input}) 的执行请求。请判断是否还有其他未完成的步骤需要推进，或直接向用户说明情况。
"""


class PrismClawGuardMixin:
    """重写 _reasoning 和 _acting，靠 pending 状态轮询实现确认后执行。"""

    def __init__(self, *args, **kwargs) -> None:
        self._prismclaw_sess = kwargs.pop("_prismclaw_sess", None)
        super().__init__(*args, **kwargs)

    async def _reasoning(self, tool_choice=None) -> Msg:
        while True:
            pending = await self._prismclaw_sess.get_pending_tool() if self._prismclaw_sess else None

            if not pending:
                return await super()._reasoning(tool_choice)

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
                    name="prismclaw_guard",
                )
                await self.memory.add(msg)
                await self.print(msg, last=True)
                return msg

            elif pending.status == PendingStatus.APPROVED:
                # 重放原 tool_use，改新 id（不能和之前 denied 的重复）
                # 直接改 pending.tool_use 的 id（引用），_acting 靠 id 匹配
                tool_use_block = pending.tool_use
                tool_use_block["id"] = str(uuid.uuid4())
                msg = Msg(
                    role="assistant",
                    content=[tool_use_block],
                    name="prismclaw_guard",
                )
                await self.memory.add(msg)
                return msg

            elif pending.status == PendingStatus.REJECTED:
                await self._prismclaw_sess.pop_pending_tool()
                next_pending = await self._prismclaw_sess.get_pending_tool() if self._prismclaw_sess else None
                if next_pending:
                    continue  # 还有下一个待确认
                # 没有了，通知模型重新思考
                hint = TOOL_REJECTED_TEMPLATE.format(tool_name=tool_name, tool_input=tool_input)
                await self.memory.add(
                    Msg(role="user", content=[TextBlock(type="text", text=hint)], name="prismclaw_guard"),
                    marks=["TOOL_REJECTED"],
                )
                try:
                    return await super()._reasoning(tool_choice)
                finally:
                    await self.memory.delete_by_mark("TOOL_REJECTED")

    async def _acting(self, tool_call: ToolUseBlock) -> Optional[dict]:
        pending = await self._prismclaw_sess.get_pending_tool() if self._prismclaw_sess else None

        # 已批准的 pending 工具直接执行（ID 匹配）
        if pending and pending.tool_use.get("id") == tool_call.get("id") and pending.status == PendingStatus.APPROVED:
            await self._prismclaw_sess.pop_pending_tool()
            return await super()._acting(tool_call)

        # auto_approve 模式：跳过确认，直接执行高风险工具
        if getattr(self._prismclaw_sess, "auto_approve", False):
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
            await self.memory.add(tool_res_msg)
            await self.print(tool_res_msg, last=True)
            if self._prismclaw_sess:
                await self._prismclaw_sess.add_pending_tool(PendingToolUse(dict(tool_call)))
            return None

        # 非高风险工具：直接执行
        return await super()._acting(tool_call)
