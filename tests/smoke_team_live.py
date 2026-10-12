"""真实 LLM 团队编排冒烟：最小 pipeline 团队，跑一次 run_team_stream。

用法：python tests/smoke_team_live.py [provider]   （默认 ali）
"""

import asyncio
import copy
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from model_config import load_config  # noqa: E402


async def main():
    provider = sys.argv[1] if len(sys.argv) > 1 else "ali"
    import team as team_mod

    cfg = copy.deepcopy(load_config())
    cfg["llm"]["provider"] = provider

    spec = {
        "name": "冒烟团队",
        "type": "pipeline",
        "final": "last",
        "stages": [
            {"id": "s1", "label": "单阶段",
             "members": [{"name": "甲", "prompt": "你是简洁的分析师。", "role": "分析"}]},
        ],
    }

    events = []

    async def emit(ev):
        events.append(ev)

    report, results = await asyncio.wait_for(
        team_mod.run_team_stream(spec, "用一句话说明什么是冒烟测试。", emit, cfg, "workspace",
                                 run_dir=None, member_timeout=120, max_concurrent=1),
        timeout=240,
    )
    phases = [e.get("phase") for e in events]
    print("[team phases]", phases[:12])
    print("[members]", [r.get("member") for r in results])
    print("[report]", (report or "")[:300])
    ok = bool(report) and any(e.get("phase") == "done" for e in events)
    print("OK 团队冒烟通过" if ok else "WARN 团队冒烟未完全通过")


if __name__ == "__main__":
    asyncio.run(main())
