"""PrismHarness Server — FastAPI + SSE 流式服务。

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
import platform
import re
import sys
import urllib.request

import yaml
import uvicorn
from datetime import datetime
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

import conf
from conf import console
from prism_harness_agent import get_or_create_agent, SESS_MGR
from model_config import load_config
from tools import load_persona_file

# 项目根目录：无论从哪里启动 server，都定位到 server.py 所在目录
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
WORKSPACE_DIR = os.path.join(BASE_DIR, "workspace")
CONFIG_PATH = os.path.join(BASE_DIR, "config.yaml")

# 配置文件里可安全通过设置页编辑的字段白名单（其余字段原样保留）
CONFIG_EDITABLE = {
    "llm": {"provider", "temperature", "enable_thinking"},
    "providers": {"api_key", "api_base", "model", "enable_thinking"},
    "agent": {"max_iters", "parallel_tool_calls",
              "token_budget", "compress_threshold", "keep_recent_messages"},
}


class ChatRequest(BaseModel):
    session_id: str = "default"
    content: list = []
    deepresearch: bool = False


# Load config
cfg = load_config(CONFIG_PATH)
server_cfg = cfg.get("server", {})
SESS_MGR.expires = float(cfg.get("session", {}).get("expires", 300))
SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def apply_features(cfg: dict):
    """把 config.yaml 的 features 段覆盖到 conf.FLAGS / GUARD_TOOLS（热生效）。"""
    feats = cfg.get("features", {}) or {}
    for k, v in feats.items():
        if k in conf.FLAGS and isinstance(v, bool):
            conf.FLAGS[k] = v
    gt = feats.get("guard_tools")
    if isinstance(gt, list):
        conf.GUARD_TOOLS[:] = [g for g in gt if isinstance(g, str)]


def reload_cfg():
    """重新从 config.yaml 读取配置，并同步会话过期时间、功能开关。"""
    global cfg, server_cfg
    cfg = load_config(CONFIG_PATH)
    server_cfg = cfg.get("server", {})
    SESS_MGR.expires = float(cfg.get("session", {}).get("expires", 300))
    apply_features(cfg)
    return cfg


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


app = FastAPI(title="PrismHarness")

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
    # 开发期禁止缓存 HTML：否则改完前端、同一标签页刷新仍加载旧页面，
    # 表现为"上下文面板有 Prompt/工具块、却缺实际请求参数块"等只改了前端却看不到的现象。
    return FileResponse(
        os.path.join(BASE_DIR, "index.html"),
        headers={"Cache-Control": "no-store, must-revalidate"},
    )


@app.get("/get_personas")
async def get_personas():
    return {
        "agents": load_persona_file("AGENTS.md", WORKSPACE_DIR),
        "soul": load_persona_file("SOUL.md", WORKSPACE_DIR),
        "user": load_persona_file("USER.md", WORKSPACE_DIR),
        "identity": load_persona_file("IDENTITY.md", WORKSPACE_DIR),
    }


@app.post("/update_persona")
async def update_persona(request: Request):
    FILE_MAP = {"agents": "AGENTS.md", "soul": "SOUL.md", "user": "USER.md",
                "identity": "IDENTITY.md"}
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


@app.get("/api/config")
async def get_config():
    """返回设置页可编辑的配置子集（用于在界面里改模型/API Key 等）。"""
    return {
        "llm": cfg.get("llm", {}),
        "providers": cfg.get("providers", {}),
        "agent": cfg.get("agent", {}),
        "editable": CONFIG_EDITABLE,
    }


def _mask_dict(src, allowed):
    """按白名单字段过滤 dict。providers 是嵌套结构，需逐 provider 过滤。"""
    if not isinstance(src, dict):
        return {}
    out = {}
    for k in allowed:
        if k in src:
            out[k] = src[k]
    return out


@app.post("/api/config")
async def update_config(request: Request):
    """把设置页提交的配置写回 config.yaml（只改白名单字段），并热重载。

    模型/API Key 等改动对「新发起的请求」立即生效（agent_runner 每轮会重读配置）。
    已创建的会话无需重启即可在下一轮消息生效；只有正在执行中的请求不受影响。
    """
    try:
        body = await request.json()
    except Exception:
        return {"status": "error", "message": "无效的 JSON 请求体"}

    if not isinstance(body, dict):
        return {"status": "error", "message": "配置必须是对象"}

    # 从磁盘读最新配置，保证不覆盖其他字段（context/workspace/server/session 等）
    on_disk = {}
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            on_disk = yaml.safe_load(f) or {}

    editable = CONFIG_EDITABLE

    # llm（顶层标量）
    if isinstance(body.get("llm"), dict):
        llm = on_disk.get("llm", {}) or {}
        llm.update(_mask_dict(body["llm"], editable["llm"]))
        on_disk["llm"] = llm

    # providers（嵌套：每个 provider 过滤白名单字段）
    if isinstance(body.get("providers"), dict):
        provs = on_disk.get("providers", {}) or {}
        for name, pcfg in body["providers"].items():
            if not isinstance(pcfg, dict):
                continue
            current = provs.get(name, {}) or {}
            current.update(_mask_dict(pcfg, editable["providers"]))
            provs[name] = current
        # 删除 provider（删除标记）
        for name in body.get("delete_providers", []) or []:
            if name in provs:
                provs.pop(name, None)
        all_names = [n for n in provs if n and not str(n).startswith("#")]
        if all_names:
            cur = on_disk.get("llm", {}).get("provider", "")
            if cur not in all_names:
                on_disk["llm"] = dict(on_disk.get("llm", {}))
                on_disk["llm"]["provider"] = all_names[0]
        on_disk["providers"] = provs

    # agent（顶层标量）
    if isinstance(body.get("agent"), dict):
        agent = on_disk.get("agent", {}) or {}
        agent.update(_mask_dict(body["agent"], editable["agent"]))
        on_disk["agent"] = agent

    # 写回 config.yaml（保留原注释会被去掉——用 yaml.dump 会重排；接受该取舍）
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            yaml.safe_dump(on_disk, f, allow_unicode=True, sort_keys=False, default_flow_style=False)
    except Exception as e:
        return {"status": "error", "message": f"写入 config.yaml 失败: {e}"}

    reload_cfg()
    return {"status": "success", "message": "已保存并热重载配置"}


@app.get("/api/status")
async def get_status():
    """配置概览 + 环境/依赖检测。"""
    prov = cfg.get("providers", {}).get(cfg.get("llm", {}).get("provider", ""), {}) or {}
    flag_names = sorted(conf.FLAGS.keys())
    return {
        "llm": {
            "provider": cfg.get("llm", {}).get("provider", ""),
            "model": prov.get("model", ""),
            "api_base": prov.get("api_base", ""),
            "has_api_key": bool(prov.get("api_key")),
            "temperature": cfg.get("llm", {}).get("temperature", 0.0),
            "enable_thinking": cfg.get("llm", {}).get("enable_thinking", False),
        },
        "env": {
            "python": platform.python_version(),
            "python_exe": sys.executable,
            "config_writable": os.access(CONFIG_PATH, os.W_OK) if os.path.exists(CONFIG_PATH) else True,
            "config_modified": datetime.fromtimestamp(os.path.getmtime(CONFIG_PATH)).strftime("%Y-%m-%d %H:%M:%S") if os.path.exists(CONFIG_PATH) else "-",
        },
        "features": {
            "enabled": [k for k in flag_names if conf.FLAGS.get(k, True)],
            "total": len(flag_names),
        },
        "session_count": len(getattr(SESS_MGR, "sessions", {}) or {}),
        "guard_count": len(conf.GUARD_TOOLS),
    }


@app.get("/api/flags")
async def get_flags():
    return {
        "flags": dict(conf.FLAGS),
        "guard_tools": list(conf.GUARD_TOOLS),
        "guard_timeout": conf.GUARD_TIMEOUT,
    }


@app.post("/api/flags")
async def update_flags(request: Request):
    try:
        body = await request.json()
    except Exception:
        return {"status": "error", "message": "无效 JSON 请求体"}
    on_disk = {}
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            on_disk = yaml.safe_load(f) or {}
    feats = on_disk.get("features", {}) or {}
    flags = body.get("flags") if isinstance(body.get("flags"), dict) else {}
    for k, v in flags.items():
        if k in conf.FLAGS and isinstance(v, bool):
            conf.FLAGS[k] = v
            if v is True:
                feats.pop(k, None)  # 默认即开启 → 不用保留覆盖
            else:
                feats[k] = False
    guard = body.get("guard_tools")
    if isinstance(guard, list):
        conf.GUARD_TOOLS[:] = [g for g in guard if isinstance(g, str)]
        feats["guard_tools"] = list(conf.GUARD_TOOLS)
    on_disk["features"] = feats
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            yaml.safe_dump(on_disk, f, allow_unicode=True, sort_keys=False, default_flow_style=False)
    except Exception as e:
        return {"status": "error", "message": f"写入 config.yaml 失败: {e}"}
    return {"status": "success", "flags": dict(conf.FLAGS), "guard_tools": list(conf.GUARD_TOOLS)}


@app.post("/api/test_model")
async def test_model(request: Request):
    """用当前/指定配置发一个最小 chat 请求，验证 api_base + key 连通性。"""
    try:
        body = await request.json() or {}
    except Exception:
        body = {}
    provider = body.get("provider") or cfg.get("llm", {}).get("provider", "")
    pcfg = cfg.get("providers", {}).get(provider, {}) or {}
    api_base = body.get("api_base") or pcfg.get("api_base", "")
    api_key = body.get("api_key") if body.get("api_key") not in (None, "") else pcfg.get("api_key", "")
    model = body.get("model") or pcfg.get("model", "")
    if not api_base or not model:
        return {"status": "error", "message": "缺少 api_base 或 model"}
    url = api_base.rstrip("/") + "/chat/completions"
    payload = {"model": model,
               "messages": [{"role": "user", "content": "ping"}],
               "max_tokens": 16, "stream": False}
    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), method="POST")
    req.add_header("Content-Type", "application/json")
    if api_key and api_key != "EMPTY":
        req.add_header("Authorization", "Bearer " + api_key)
    import time as _t
    t0 = _t.time()
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw = resp.read().decode("utf-8", "replace")
        dt = round((_t.time() - t0) * 1000)
        obj = json.loads(raw)
        reply = obj.get("choices", [{}])[0].get("message", {}).get("content", "")
        return {"status": "success", "latency_ms": dt, "model": obj.get("model", model),
                "reply": (reply or "")[:200]}
    except Exception as e:
        return {"status": "error", "message": f"{type(e).__name__}: {e}"}


LOG_DIR = os.path.join(BASE_DIR, "session_logs")


@app.get("/api/logs")
async def list_logs():
    out = []
    if os.path.isdir(LOG_DIR):
        for name in sorted(os.listdir(LOG_DIR)):
            p = os.path.join(LOG_DIR, name)
            if not os.path.isdir(p):
                continue
            files = []
            try:
                for root, _, fnames in os.walk(p):
                    rel = os.path.relpath(root, p)
                    for fn in sorted(fnames):
                        fp = os.path.join(root, fn)
                        try:
                            files.append({
                                "name": fn if rel == "." else os.path.join(rel, fn),
                                "size": os.path.getsize(fp),
                                "mtime": datetime.fromtimestamp(os.path.getmtime(fp)).strftime("%Y-%m-%d %H:%M:%S"),
                            })
                        except OSError:
                            continue
            except OSError:
                continue
            out.append({
                "name": name,
                "mtime": datetime.fromtimestamp(os.path.getmtime(p)).strftime("%Y-%m-%d %H:%M:%S"),
                "files": files,
            })
    return {"logs": out, "dir": LOG_DIR}


@app.get("/api/log")
async def read_log(session: str, file: str = ""):
    if not re.match(r"^[A-Za-z0-9_-]{1,64}$", session):
        return {"status": "error", "message": "非法 session 名"}
    p = os.path.join(LOG_DIR, session)
    if not os.path.isdir(p):
        return {"status": "error", "message": "会话不存在"}
    if not (file.endswith(".md") or file.endswith(".json") or file.endswith(".jsonl")):
        return {"status": "error", "message": "仅支持查看 .md / .json / .jsonl"}
    if ".." in file.replace("\\", "/").split("/"):
        return {"status": "error", "message": "非法文件名"}
    fp = os.path.join(p, file)
    if not os.path.isfile(fp):
        return {"status": "error", "message": "文件不存在"}
    with open(fp, "r", encoding="utf-8") as f:
        return {"status": "success", "name": file, "content": f.read()}


@app.get("/api/session/transcript")
async def session_transcript(session: str):
    """从 session_logs 的 snapshots 重建某会话的对话记录，供前端恢复空/丢失的会话。

    注意：每轮快照的 messages 是「累积历史」，且只存截断到 160 字符的 preview；
    旧实现取第一条 user/assistant，会导致多轮会话每一轮都错位成第一轮内容。
    这里改为：user 取每轮真实的新输入 user_input，assistant 取该轮 full_messages
    里最后一条 assistant（完整、不截断）；老快照无 full_messages 时降级用
    messages[].preview 的最后一条 assistant。
    """
    if not re.match(r"^[A-Za-z0-9_-]{1,64}$", session):
        return {"status": "error", "message": "非法 session 名"}
    p = os.path.join(LOG_DIR, session)
    if not os.path.isdir(p):
        return {"status": "error", "message": "会话不存在"}
    snap_dir = os.path.join(p, "snapshots")

    def _text_of(m):
        if not isinstance(m, dict):
            return ""
        return str(m.get("preview") or m.get("content") or m.get("text") or "").strip()

    rounds = []
    if os.path.isdir(snap_dir):
        for fn in sorted(os.listdir(snap_dir)):
            if not fn.startswith("round_") or not fn.endswith(".json"):
                continue
            try:
                with open(os.path.join(snap_dir, fn), "r", encoding="utf-8") as f:
                    d = json.load(f)
            except Exception:
                continue
            # 本轮真实的新输入（快照单独记录，最可靠、不错位）
            user_text = str(d.get("user_input") or "").strip()

            # 助手回复：优先取 full_messages 里最后一条 assistant（完整内容）
            asst_text = ""
            fm = d.get("full_messages") or []
            if isinstance(fm, list):
                for m in fm:
                    if isinstance(m, dict) and m.get("role") == "assistant":
                        t = _text_of(m)
                        if t:
                            asst_text = t
            # 老快照无 full_messages → 降级用 preview 里最后一条 assistant
            if not asst_text:
                for m in (d.get("messages") or []):
                    if isinstance(m, dict) and m.get("role") == "assistant":
                        t = _text_of(m)
                        if t:
                            asst_text = t
            # 兜底：仍无 user 就用该轮第一条 user preview
            if not user_text:
                for m in (d.get("messages") or []):
                    if isinstance(m, dict) and m.get("role") == "user":
                        t = _text_of(m)
                        if t:
                            user_text = t
                            break
            mm = re.search(r"(\d+)", fn)
            rn = int(mm.group(1)) if mm else (d.get("round") or 0)
            if user_text or asst_text:
                rounds.append({"round": rn, "user": user_text, "assistant": asst_text})
    if not rounds:
        return {"status": "error", "message": "该会话没有可恢复的对话记录"}
    rounds.sort(key=lambda r: r["round"])
    title = (rounds[0].get("user") or "恢复的会话")[:20]
    return {"status": "success", "session": session, "title": title, "rounds": rounds}


@app.post("/api/logs/delete")
async def delete_log(request: Request):
    import shutil
    try:
        body = await request.json()
    except Exception:
        body = {}
    session = body.get("session", "")
    if not re.match(r"^[A-Za-z0-9_-]{1,64}$", session):
        return {"status": "error", "message": "非法 session 名"}
    # 同时清理内存会话，避免 agent 任务/内存残留
    await SESS_MGR.delete_session(session)
    p = os.path.join(LOG_DIR, session)
    if os.path.isdir(p):
        shutil.rmtree(p, ignore_errors=True)
    console("[delete-log]", session)
    return {"status": "success"}


@app.post("/api/session/delete")
async def delete_session(request: Request):
    """删除会话：内存会话 + session_logs 日志目录。"""
    import shutil
    try:
        body = await request.json()
    except Exception:
        body = {}
    session = body.get("session", "")
    if not re.match(r"^[A-Za-z0-9_-]{1,64}$", session):
        return {"status": "error", "message": "非法 session 名"}
    await SESS_MGR.delete_session(session)
    p = os.path.join(LOG_DIR, session)
    if os.path.isdir(p):
        shutil.rmtree(p, ignore_errors=True)
    console("[delete-session]", session)
    return {"status": "success"}


@app.post("/chat")
async def chat(request: ChatRequest):
    session_id = validate_session_id(request.session_id)
    console("[chat]", session_id)
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
    console("[stop]", session_id, request_id)
    await sess.cancel_request(request_id)
    return {"status": "canceled"}


@app.post("/resolve")
async def resolve(request: Request):
    """HITL 工具确认：resolve 当前 pending 的状态（不开新 SSE 流）。

    只改 pending.status、不 pop；下一轮 guard._reasoning 读到 APPROVED/REJECTED
    后继续执行/拒绝。agent 后续输出继续从触发确认的那个原请求的 SSE 流出来。
    """
    body = await request.json()
    session_id = validate_session_id(body.get("session_id", ""))
    sess = await SESS_MGR.get_or_create_session(session_id, create=False)
    if sess is None:
        return {"status": "not_found"}
    console("[resolve]", session_id)
    # /approve_all：开启自动放行，并放行当前堵着的全部 pending
    if body.get("auto_approve"):
        sess.auto_approve = True
        n = await sess.approve_all_pending()
        return {"status": "resolved", "had_pending": n > 0}
    # /approve_all off：关闭自动放行
    if body.get("auto_approve_off"):
        sess.auto_approve = False
        return {"status": "ok"}
    # /approve 或 /reject
    approved = bool(body.get("approved", False))
    had = await sess.resolve_pending(approved)
    return {"status": "resolved", "had_pending": had}


if __name__ == "__main__":
    host = server_cfg.get("host", "127.0.0.1")
    port = server_cfg.get("port", 8765)
    console("[start]", f"http://{host}:{port}")
    uvicorn.run(app, host=host, port=port)
