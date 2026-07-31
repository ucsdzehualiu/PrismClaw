"""PrismClawGuard — HITL 工具调用确认（官方 middleware 实现）。

不再重写 ReActAgent 的 _reasoning/_acting，改为注册一个官方洋葱 middleware：
- 高风险工具（GUARD_TOOLS）→ 推确认卡片给前端，await per-call Future
- 用户 /approve → resolve(True)  → middleware 放行 next_handler（真正执行工具）
- 用户 /reject  → resolve(False) → middleware yield 拒绝说明，跳过执行

并行工具调用时，每个 call_tool_function 各自走 middleware 链，高风险分支挂起
等确认、其他分支继续，符合官方 asyncio.gather 语义。不破坏 memory 的
tool_use/result 配对，不塞假 ToolResultBlock，不改 tool_use id。
"""

import asyncio
from typing import Any

from agentscope.message import TextBlock
from agentscope.tool import ToolResponse

from conf import GUARD_TOOLS
from session import PendingToolUse  # re-export 供兼容


def make_hitl_middleware(sess):
    """生成一个绑定到指定 session 的 HITL middleware。

    Args:
        sess: Session 实例（提供 add_pending_tool / current_response_queue / auto_approve）

    Returns:
        官方签名 middleware: async def(kwargs, next_handler) -> AsyncGenerator[ToolResponse]
    """

    async def hitl_middleware(kwargs: dict, next_handler):
        tool_call = kwargs["tool_call"]
        name = tool_call.get("name", "")

        # 非高风险工具 或 auto_approve 模式 → 直接放行，走原工具函数
        if name not in GUARD_TOOLS or getattr(sess, "auto_approve", False):
            async for chunk in await next_handler(**kwargs):
                yield chunk
            return

        # 高风险工具 → 创建 per-call Future，推确认卡片，await 用户响应
        loop = asyncio.get_event_loop()
        fut: "asyncio.Future[bool]" = loop.create_future()
        pending = PendingToolUse(dict(tool_call), fut)
        await sess.add_pending_tool(pending)

        # 推 confirm status 事件给前端（复用现有 renderConfirmCard）
        rq = getattr(sess, "current_response_queue", None)
        if rq is not None:
            await rq.put({
                "type": "status",
                "phase": "confirm",
                "detail": (
                    f"🔒 **工具调用需要确认**\n\n"
                    f"**工具:** `{name}`\n"
                    f"**参数:** `{tool_call.get('input', {})}`\n\n"
                    f"请输入 `/approve` 允许执行，或 `/reject` 拒绝。"
                ),
            })

        # 挂起该分支；并行场景下其他工具分支继续跑
        try:
            approved = await fut
        except asyncio.CancelledError:
            # 请求被中断（用户点停止/会话释放）→ 视为拒绝，不执行
            approved = False

        if approved:
            # 放行：真正执行工具函数
            async for chunk in await next_handler(**kwargs):
                yield chunk
        else:
            # 拒绝：跳过执行，返回拒绝说明（官方 _acting 会把它正常存进 memory）
            yield ToolResponse(
                content=[TextBlock(
                    type="text",
                    text=f"❌ 用户拒绝了 {name}({tool_call.get('input', {})}) 的执行。",
                )],
            )

    return hitl_middleware
