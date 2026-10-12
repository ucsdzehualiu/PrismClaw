"""PrismHarness 会话管理（AgentScope 2.0）。

会话与全局会话管理器：
- 每个 session 独立请求队列（asyncio.Queue）
- HITL「停泊」队列（PrismHarnessGuard 已由 2.0 权限引擎取代）：
  工具确认 / 结构化提问时，agent.reply_stream 会停泊（park），
  session 记录停泊信息并等待用户决定，再用 UserConfirmResultEvent /
  ExternalExecutionResultEvent 续跑同一轮对话。
- 会话过期自动回收
- 请求级打断（cancel）
"""

import asyncio
import json
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional


class SessionStatus(str, Enum):
    ACTIVE = "ACTIVE"
    INACTIVE = "INACTIVE"


class PendingStatus(str, Enum):
    """待确认/待回答状态。"""

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class PendingConfirm:
    """一次被 agent 停泊（park）的待办：工具确认或结构化提问。

    AgentScope 2.0 里 agent 遇到需要人工介入时不会挂起协程，而是 yield 一个
    RequireUserConfirmEvent / RequireExternalExecutionEvent 后让 reply_stream 返回
    （"停泊"）。这里记录停泊信息（reply_id + 待处理 tool_calls），
    并提供一个 Event 供停泊的 streaming 协程 await；用户 /approve 等只改状态 + set 事件，
    停泊协程醒来后用对应事件续跑——不轮询、不塞假 tool_result。
    """

    def __init__(self, reply_id: str, tool_calls: list, kind: str = "confirm"):
        self.reply_id = reply_id
        self.tool_calls = list(tool_calls or [])   # list[ToolCallBlock]
        self.kind = kind                            # "confirm" | "external"
        self.status = PendingStatus.PENDING
        self.created_at = time.time()
        self.answer: Any = None                     # external（ask_user_question）的回答
        self._event = asyncio.Event()

    @property
    def name(self) -> str:
        return self.tool_calls[0].name if self.tool_calls else "unknown"

    @property
    def input(self) -> Any:
        """解析首个 tool_call 的 input（2.0 里 input 是 JSON 字符串）。"""
        if not self.tool_calls:
            return {}
        raw = getattr(self.tool_calls[0], "input", "")
        if isinstance(raw, str):
            try:
                return json.loads(raw)
            except Exception:
                return {"raw": raw}
        return raw or {}

    def is_expired(self, timeout: float) -> bool:
        return time.time() - self.created_at > timeout

    def resolve(self, approved: bool) -> None:
        self.status = PendingStatus.APPROVED if approved else PendingStatus.REJECTED
        self._event.set()

    def set_answer(self, answer: Any) -> None:
        """外部工具（结构化提问）的回答：置 APPROVED 并唤醒。"""
        self.answer = answer
        self.status = PendingStatus.APPROVED
        self._event.set()

    async def wait(self) -> None:
        await self._event.wait()


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

    async def resolve_head(self, approved: bool) -> bool:
        """唤醒队首停泊：只改状态并 set 事件，停泊协程随后自行 pop 并续跑。"""
        async with self.lock:
            if not self.pending_confirms:
                return False
            self.pending_confirms[0].resolve(approved)
            return True

    async def answer_head(self, answer: Any) -> bool:
        """给队首的结构化提问写入回答并唤醒。"""
        async with self.lock:
            if not self.pending_confirms:
                return False
            self.pending_confirms[0].set_answer(answer)
            return True

    async def approve_all_pending(self) -> int:
        """把当前全部停泊一次性置为 APPROVED（供 /approve_all）。"""
        async with self.lock:
            n = 0
            for p in self.pending_confirms:
                p.resolve(True)
                n += 1
            return n

    # ---- 兼容旧接口名 ----
    async def get_pending_tool(self):
        return await self.get_pending()

    async def approve_head_pending(self) -> bool:
        return await self.resolve_head(True)

    async def reject_head_pending(self) -> bool:
        return await self.resolve_head(False)

    async def resolve_pending(self, approved: bool) -> bool:
        return await self.resolve_head(approved)

    async def answer_pending(self, answer: Any) -> bool:
        return await self.answer_head(answer)

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
