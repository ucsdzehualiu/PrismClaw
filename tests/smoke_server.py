"""Server 冒烟：经 FastAPI + SSE 走一次完整对话，含 HITL 确认（/approve）流程。

用法：python tests/smoke_server.py
前置：server.py 已在 127.0.0.1:8765 运行。
"""

import asyncio
import json
import sys

import aiohttp

BASE = "http://127.0.0.1:8765"
SID = "smoke_srv"


async def main():
    events = []
    confirm_seen = asyncio.Event()
    done_seen = asyncio.Event()

    async with aiohttp.ClientSession() as s:
        # 1) 健康检查
        async with s.get(BASE + "/api/status") as r:
            st = await r.json()
            print("[status]", {k: st.get(k) for k in ("llm", "features", "session_count") if k in st})

        async def parse_stream(resp):
            async for raw in resp.content:
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if not data:
                    continue
                try:
                    ev = json.loads(data)
                except Exception:
                    continue
                events.append(ev)
                if ev.get("type") == "status":
                    ph = ev.get("phase")
                    if ph == "confirm":
                        confirm_seen.set()
                    if ph == "done":
                        done_seen.set()

        async def read_stream(payload):
            async with s.post(BASE + "/chat", json=payload) as resp:
                await parse_stream(resp)

        async def approver():
            try:
                await asyncio.wait_for(confirm_seen.wait(), 120)
            except asyncio.TimeoutError:
                print("[approver] 未等到确认事件")
                return
            # /approve 会驱动续跑，续跑内容（工具结果 + 最终文本 + done）出现在这条流上
            async with s.post(BASE + "/chat", json={
                "session_id": SID,
                "content": [{"type": "text", "text": "/approve"}],
            }) as r:
                await parse_stream(r)

        await asyncio.gather(
            read_stream({"session_id": SID, "content": [
                {"type": "text", "text": "用 run_shell 执行命令 echo srv-smoke，然后告诉我输出。"}]}),
            approver(),
        )

    phases = [e.get("phase") for e in events if e.get("type") == "status"]
    texts = []
    for e in events:
        for c in e.get("contents", []) or []:
            if c.get("type") == "text":
                texts.append(c.get("content", ""))
            if c.get("type") == "tool_result":
                texts.append(f"[tool_result:{c.get('name')}] {c.get('output','')}")
    print("[phases]", phases)
    print("[saw confirm]", confirm_seen.is_set(), " [saw done]", done_seen.is_set())
    print("[output]", " | ".join(t for t in texts if t)[:500])
    ok = confirm_seen.is_set() and done_seen.is_set()
    print("OK 冒烟通过" if ok else "WARN 冒烟未完全通过")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    asyncio.run(main())
