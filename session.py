"""PrismHarness 会话管理。

会话与全局会话管理器：
- 每个 session 独立请求队列（asyncio.Queue）
- pending_tool 队列（PrismHarnessGuard 确认）
- 会话过期自动回收
- 请求级打断（cancel）
"""

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional


class SessionStatus(str, Enum):
    ACTIVE = "ACTIVE"
    INACTIVE = "INACTIVE"


class PendingStatus(str, Enum):
    """待确认工具的状态。"""

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class PendingToolUse:
    """一个待确认的工具调用。

    确认/拒绝只改 status，下一轮 PrismHarnessGuard._reasoning 轮询读取；
    真正执行由 _acting 匹配到 APPROVED 后 pop 掉。这里不挂协程、不抛异常。
    """

    def __init__(self, tool_use: dict):
        self.tool_use = tool_use
        self.status = PendingStatus.PENDING
        self.created_at = time.time()
        self.answer = None  # ask_user_question 的用户回答（dict）

    @property
    def name(self) -> str:
        return self.tool_use.get("name", "unknown")

    @property
    def input(self) -> Any:
        return self.tool_use.get("input", {})

    def is_expired(self, timeout: float) -> bool:
        """等待超过 timeout 秒仍未确认 → 视为超时（仅在 PENDING 时判断）。"""
        return time.time() - self.created_at > timeout

    def resolve(self, approved: bool) -> None:
        """设置状态以唤醒轮询（不 pop，pop 由 _acting 执行时完成）。"""
        self.status = (
            PendingStatus.APPROVED if approved else PendingStatus.REJECTED
        )


@dataclass
class AgentRequest:
    """一次 Agent 请求。"""
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    session_id: str = ""
    content: Any = None
    canceled: bool = False
    _cancel_q: Any = field(default=None, init=False, repr=False)

    def __post_init__(self):
        self.response_queue: asyncio.Queue = asyncio.Queue()
        self.stream_task: Optional[asyncio.Task] = None

    async def cancel(self):
        self.canceled = True
        # 直接往 streaming 内部队列灌 cancel 事件，不等 LLM 流式走到检查点
        if self._cancel_q is not None:
            try:
                self._cancel_q.put_nowait({"cancel": True, "last": True, "contents": []})
                self._cancel_q.put_nowait(None)
            except Exception:
                pass
        if self.stream_task and not self.stream_task.done():
            self.stream_task.cancel()


class Session:
    """单个会话，管理请求队列和 pending 工具。"""

    def __init__(self, session_id: str, expires: float = 1800):
        self.session_id = session_id
        self.lock = asyncio.Lock()
        self.cond = asyncio.Condition(self.lock)
        self.req_queue: asyncio.Queue = asyncio.Queue()
        self.last_activate = time.time()
        self.expires = expires
        self.status = SessionStatus.ACTIVE
        self.pending_req: Dict[str, AgentRequest] = {}
        self.pending_tool_calls: List[PendingToolUse] = []
        self.auto_approve = False  # /approve_all 模式：跳过所有工具确认
        # 当前活跃请求的 response_queue，供 middleware 推 confirm status 事件
        self.current_response_queue: Optional[asyncio.Queue] = None

    async def add_pending_tool(self, pending: PendingToolUse):
        async with self.lock:
            self.pending_tool_calls.append(pending)

    async def get_pending_tool(self) -> Optional[PendingToolUse]:
        async with self.lock:
            return self.pending_tool_calls[0] if self.pending_tool_calls else None

    async def pop_pending_tool(self) -> Optional[PendingToolUse]:
        async with self.lock:
            return self.pending_tool_calls.pop(0) if self.pending_tool_calls else None

    async def approve_head_pending(self) -> bool:
        """把队首 pending 置为 APPROVED（供 /approve）。不 pop，等 _acting 执行时再 pop。"""
        async with self.lock:
            if not self.pending_tool_calls:
                return False
            self.pending_tool_calls[0].resolve(True)
            return True

    async def reject_head_pending(self) -> bool:
        """把队首 pending 置为 REJECTED（供 /reject）。不 pop，等 _reasoning 处理。"""
        async with self.lock:
            if not self.pending_tool_calls:
                return False
            self.pending_tool_calls[0].resolve(False)
            return True

    async def answer_pending(self, answer) -> bool:
        """给队首的 ask_user_question pending 写入用户回答并置为 APPROVED。"""
        async with self.lock:
            if not self.pending_tool_calls:
                return False
            p = self.pending_tool_calls[0]
            if p.name != "ask_user_question":
                return False
            p.answer = answer
            p.resolve(True)
            return True

    async def approve_all_pending(self) -> int:
        """把所有待确认工具全部置为 APPROVED（供 /approve_all）。

        只放行队首会让并行/批量工具产生的多个 pending 逐个继续弹确认卡，
        表现为"approve_all 后还一直问我要不要执行"。这里一次性全放行。
        返回被置为 APPROVED 的个数。
        """
        async with self.lock:
            n = 0
            for p in self.pending_tool_calls:
                p.resolve(True)
                n += 1
            return n

    async def resolve_pending(self, approved: bool) -> bool:
        """resolve 队首 pending（供 /resolve HTTP 端点）。

        与 /approve /reject 魔术命令保持一致：只改 status、不 pop，
        pop 由 PrismHarnessGuard._acting 真正执行时完成。返回是否有 pending。
        """
        async with self.lock:
            if not self.pending_tool_calls:
                return False
            self.pending_tool_calls[0].resolve(approved)
            return True

    async def activate(self):
        async with self.lock:
            self.last_activate = time.time()

    async def add_request(self, request: AgentRequest) -> bool:
        async with self.cond:
            if self.status == SessionStatus.INACTIVE:
                await request.response_queue.put(None)
                return False
            self.pending_req[request.id] = request
            await self.req_queue.put(request)
            self.cond.notify()
        return True

    async def get_request(self) -> tuple:
        """获取下一个请求，超时则标记 INACTIVE。"""
        async with self.cond:
            while self.req_queue.empty():
                if self.status == SessionStatus.INACTIVE:
                    return None, self.status
                try:
                    await asyncio.wait_for(self.cond.wait(), timeout=1)
                except asyncio.TimeoutError:
                    if time.time() - self.last_activate > self.expires:
                        self.status = SessionStatus.INACTIVE
                        return None, self.status
            return await self.req_queue.get(), self.status

    async def finish_request(self, request: AgentRequest):
        async with self.lock:
            self.pending_req.pop(request.id, None)

    async def cancel_request(self, request_id: str):
        async with self.lock:
            req = self.pending_req.get(request_id)
            if req:
                await req.cancel()

    async def release(self):
        """释放所有资源。"""
        async with self.lock:
            # 取消所有 pending 请求
            for req in self.pending_req.values():
                try:
                    await req.cancel()
                except Exception:
                    pass
            self.pending_req.clear()
            self.pending_tool_calls.clear()

    async def shutdown(self):
        """标记 INACTIVE 并唤醒等待中的 agent_runner，使其退出循环。"""
        async with self.cond:
            self.status = SessionStatus.INACTIVE
            self.cond.notify_all()


class SessionManager:
    """全局会话管理器。"""

    def __init__(self, expires: float = 1800):
        self.manager_lock = asyncio.Lock()
        self.sessions: Dict[str, Session] = {}
        self.expires = expires

    async def get_or_create_session(
        self,
        session_id: str,
        create: bool = True,
        session_main: Optional[Callable] = None,
    ) -> Optional[Session]:
        async with self.manager_lock:
            existing = self.sessions.get(session_id)
            # 会话已过期 → 清理旧会话，重建新会话
            if existing and existing.status == SessionStatus.INACTIVE and create:
                await existing.release()
                del self.sessions[session_id]
                existing = None
            if session_id not in self.sessions and create:
                self.sessions[session_id] = Session(session_id, expires=self.expires)
                if session_main:
                    asyncio.create_task(session_main(self.sessions[session_id]))
            session = self.sessions.get(session_id)
            if session:
                await session.activate()
            return session

    async def delete_session(self, session_id: str):
        async with self.manager_lock:
            sess = self.sessions.pop(session_id, None)
            if sess:
                await sess.release()
                await sess.shutdown()

    def temp_session(self) -> Session:
        return Session(str(uuid.uuid4()), expires=self.expires)
