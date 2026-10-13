"""PrismHarness 会话管理（AgentScope 2.0）。

会话与全局会话管理器：
- 每个 session 独立请求队列（asyncio.Queue）
- HITL「停泊」队列：工具确认 / 结构化提问时 agent.reply_stream 会停泊（park），
  session 记录停泊信息（reply_id + tool_calls）；用户 /approve 以新的一轮
  /chat 进入，由 UserConfirmResultEvent / ExternalExecutionResultEvent 续跑。
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


class PendingConfirm:
    """一次被 agent 停泊（park）的待办：工具确认或结构化提问。

    AgentScope 2.0 里 agent 遇到需要人工介入时不会挂起协程，而是 yield 一个
    RequireUserConfirmEvent / RequireExternalExecutionEvent 后让 reply_stream 返回
    （"停泊"）。这里只记录停泊信息（reply_id + 待处理 tool_calls + 类型），
    真正的续跑由下一轮 /chat 上的 `_build_resume_inputs` 构造对应事件驱动——
    不轮询、不挂起协程、也不塞假 tool_result。
    """

    def __init__(self, reply_id: str, tool_calls: list, kind: str = "confirm"):
        self.reply_id = reply_id
        self.tool_calls = list(tool_calls or [])   # list[ToolCallBlock]
        self.kind = kind                            # "confirm" | "external"
        self.created_at = time.time()


@dataclass
class AgentRequest:
    """一次 Agent 请求。"""
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    session_id: str = ""
    content: Any = None
    canceled: bool = False
    team_run: Any = None  # (/team 名称, 任务) —— 魔术命令解析后挂到这里
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
    """单个会话，管理请求队列与 HITL 停泊。"""

    def __init__(self, session_id: str, expires: float = 1800):
        self.session_id = session_id
        self.lock = asyncio.Lock()
        self.cond = asyncio.Condition(self.lock)
        self.req_queue: asyncio.Queue = asyncio.Queue()
        self.last_activate = time.time()
        self.expires = expires
        self.status = SessionStatus.ACTIVE
        self.pending_req: Dict[str, AgentRequest] = {}
        self.pending_confirms: List[PendingConfirm] = []
        self.auto_approve = False  # /approve_all 模式：跳过所有工具确认
        self.current_response_queue: Optional[asyncio.Queue] = None

    # ---- HITL 停泊 ----

    async def add_pending(self, pending: PendingConfirm) -> PendingConfirm:
        async with self.lock:
            self.pending_confirms.append(pending)
            return pending

    async def get_pending(self) -> Optional[PendingConfirm]:
        async with self.lock:
            return self.pending_confirms[0] if self.pending_confirms else None

    async def pop_pending(self) -> Optional[PendingConfirm]:
        async with self.lock:
            return self.pending_confirms.pop(0) if self.pending_confirms else None

    # ---- 会话生命周期 ----

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
        async with self.lock:
            for req in self.pending_req.values():
                try:
                    await req.cancel()
                except Exception:
                    pass
            self.pending_req.clear()
            self.pending_confirms.clear()

    async def shutdown(self):
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
