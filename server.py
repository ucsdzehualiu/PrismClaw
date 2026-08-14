"""PrismClaw Harness Server — FastAPI + SSE 流式服务。

基于 FastAPI + SSE 的精简服务：
- POST /chat — SSE 流式对话
- GET /stop — 打断请求
- GET / — Web 界面
- GET /get_personas — 读取人格文件
- POST /update_persona — 更新人格文件
"""

import asyncio
import json
import os
import re
import sys

import uvicorn
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

from prismclaw_agent import get_or_create_agent, SESS_MGR
from model_config import load_config
from tools import load_persona_file

# 项目根目录：无论从哪里启动 server，都定位到 server.py 所在目录
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
WORKSPACE_DIR = os.path.join(BASE_DIR, "workspace")


class ChatRequest(BaseModel):
    session_id: str = "default"
    content: list = []
    deepresearch: bool = False


# Load config
cfg = load_config()
server_cfg = cfg.get("server", {})
SESS_MGR.expires = float(cfg.get("session", {}).get("expires", 300))
SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def validate_session_id(session_id: str) -> str:
    """只允许安全的短 session id，避免日志/会话键被目录穿越。"""
    session_id = str(session_id or "").strip()
    if not SESSION_ID_RE.match(session_id):
        from fastapi import HTTPException

        raise HTTPException(
            status_code=400,
            detail="session_id must match [A-Za-z0-9_-]{1,64}",
        )
    return session_id


app = FastAPI(title="PrismClaw Harness")

app.add_middleware(
    CORSMiddleware,
    allow_origins=server_cfg.get(
        "cors_origins",
        ["http://localhost:8765", "http://127.0.0.1:8765"],
    ),
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/")
async def index():
    return FileResponse(os.path.join(BASE_DIR, "index.html"))


@app.get("/get_personas")
async def get_personas():
    return {
        "agents": load_persona_file("AGENTS.md", WORKSPACE_DIR),
        "soul": load_persona_file("SOUL.md", WORKSPACE_DIR),
        "user": load_persona_file("USER.md", WORKSPACE_DIR),
    }


@app.post("/update_persona")
async def update_persona(request: Request):
    FILE_MAP = {"agents": "AGENTS.md", "soul": "SOUL.md", "user": "USER.md"}
    body = await request.json()
    target = body.get("target", "")
    content = body.get("content", "")
    filename = FILE_MAP.get(target)
    if not filename:
        return {"status": "error", "message": f"unknown target: {target}"}
    filepath = os.path.join(WORKSPACE_DIR, filename)
    os.makedirs(os.path.dirname(filepath), exist_ok=True)
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(content)
    return {"status": "success"}


@app.post("/chat")
async def chat(request: ChatRequest):
    session_id = validate_session_id(request.session_id)
    queue_ok = False
    for _ in range(3):
        sess = await get_or_create_agent(session_id, workspace_dir=WORKSPACE_DIR)
        from session import AgentRequest
        agent_req = AgentRequest(
            session_id=session_id,
            content=request.content,
        )
        if await sess.add_request(agent_req):
            queue_ok = True
            break
        await asyncio.sleep(0.5)

    if not queue_ok:
        return {"error": "queue_error"}

    async def event_generator():
        yield f"data: {json.dumps({'request_id': agent_req.id})}\n\n"

        while True:
            msg = await agent_req.response_queue.get()
            if msg is None:
                break
            yield f"data: {json.dumps(msg, ensure_ascii=False)}\n\n"

    return StreamingResponse(event_generator(), media_type="text/event-stream")


@app.get("/stop")
async def stop(session_id: str, request_id: str):
    session_id = validate_session_id(session_id)
    sess = await SESS_MGR.get_or_create_session(session_id, create=False)
    if sess is None:
        return {"status": "not_found"}
    await sess.cancel_request(request_id)
    return {"status": "canceled"}


@app.post("/resolve")
async def resolve(request: Request):
    """HITL 工具确认：resolve 当前 pending 的 Future，不开新 SSE 流。

    agent 后续输出继续从触发确认的那个原请求的 SSE 流出来。
    """
    body = await request.json()
    session_id = validate_session_id(body.get("session_id", ""))
    sess = await SESS_MGR.get_or_create_session(session_id, create=False)
    if sess is None:
        return {"status": "not_found"}
    # /approve_all：开启自动放行，并放行当前堵着的
    if body.get("auto_approve"):
        sess.auto_approve = True
        pending = await sess.resolve_pending(True)
        return {"status": "resolved", "had_pending": pending is not None}
    # /approve_all off：关闭自动放行
    if body.get("auto_approve_off"):
        sess.auto_approve = False
        return {"status": "ok"}
    # /approve 或 /reject
    approved = bool(body.get("approved", False))
    pending = await sess.resolve_pending(approved)
    return {"status": "resolved", "had_pending": pending is not None}


if __name__ == "__main__":
    uvicorn.run(
        app,
        host=server_cfg.get("host", "127.0.0.1"),
        port=server_cfg.get("port", 8765),
    )
