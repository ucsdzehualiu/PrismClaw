"""PrismClaw 会话管理。

会话与全局会话管理器：
- 每个 session 独立请求队列（asyncio.Queue）
- pending_tool 队列（PrismClawGuard 确认）
- 会话过期自动回收
- 请求级打断（cancel）
"""

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional

from prismclaw_guard import PendingToolUse


class SessionStatus(str, Enum):
    ACTIVE = "ACTIVE"
    INACTIVE = "INACTIVE"


@dataclass
class AgentRequest:
    """一次 Agent 请求。"""
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    session_id: str = ""
    content: Any = None
    canceled: bool = False

    def __post_init__(self):
        self.response_queue: asyncio.Queue = asyncio.Queue()
        self.stream_task: Optional[asyncio.Task] = None

    async def cancel(self):
        self.canceled = True
        if self.stream_task and not self.stream_task.done():
            self.stream_task.cancel()


class Session:
    """单个会话，管理请求队列和 pending 工具。"""

    def __init__(self, session_id: str, expires: float = 300):
        self.session_id = session_id
        self.lock = asyncio.Lock()
        self.cond = asyncio.Condition(self.lock)
        self.req_queue: asyncio.Queue = asyncio.Queue()
        self.last_activate = time.time()
        self.expires = expires
        self.status = SessionStatus.ACTIVE
        self.pending_req: Dict[str, AgentRequest] = {}
        self.pending_tool_calls: List[PendingToolUse] = []

    async def add_pending_tool(self, pending: PendingToolUse):
        async with self.lock:
            self.pending_tool_calls.append(pending)

    async def get_pending_tool(self) -> Optional[PendingToolUse]:
        async with self.lock:
            return self.pending_tool_calls[0] if self.pending_tool_calls else None

    async def pop_pending_tool(self) -> Optional[PendingToolUse]:
        async with self.lock:
            return self.pending_tool_calls.pop(0) if self.pending_tool_calls else None

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


class SessionManager:
    """全局会话管理器。"""

    def __init__(self, expires: float = 300):
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

    def temp_session(self) -> Session:
        return Session(str(uuid.uuid4()), expires=self.expires)
