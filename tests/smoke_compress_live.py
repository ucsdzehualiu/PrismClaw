"""真实 LLM 上下文压缩冒烟：把模型 context_size 调小，发一条长消息，
验证框架自带的上下文压缩真的触发，且 PrismHarness 的上下文快照如实反映。

用法：python tests/smoke_compress_live.py [provider]   （默认 ds）
"""

import asyncio
import copy
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from model_config import load_config  # noqa: E402

CTX_SIZE = 1600  # 小窗口，便于触发压缩


async def main():
    provider = sys.argv[1] if len(sys.argv) > 1 else "ds"

    from agentscope.agent import Agent, ContextConfig, ReActConfig
    from agentscope.message import UserMsg

    from context_viz import ContextViz
    from model_config import build_model
    from prism_harness_agent import _emit_context_snapshot

    cfg = copy.deepcopy(load_config())
    cfg["llm"]["provider"] = provider
    cfg["llm"]["context_size"] = CTX_SIZE
    model = build_model(cfg, stream=True)  # 与应用一致：生产用流式模型

    agent = Agent(
        name="compress-smoke",
        system_prompt="你是一个简洁的助手。",
        model=model,
        context_config=ContextConfig(trigger_ratio=0.8, reserve_ratio=0.1),
        react_config=ReActConfig(max_iters=2),
    )

    filler = "以下是需要你记住的背景资料。\n" + ("这是一段用于填充上下文的长文本，内容各不相同。\n" * 260)
    before = len(agent.state.context)

    try:
        await agent.reply(UserMsg("user", filler))
    except Exception as e:
        print("[ERROR] reply 失败：", type(e).__name__, e)
        sys.exit(2)

    # —— 第 1 项：框架确实压缩了 ——
    summary = agent.state.summary or ""
    ok_framework = bool(summary)
    print("[框架] summary 字数:", len(summary), "| context:", before, "->", len(agent.state.context))

    # —— 第 2 项：PrismHarness 的快照如实上报（走真实的 _emit_context_snapshot）——
    ok_snapshot = False
    ev = None
    try:
        with tempfile.TemporaryDirectory() as td:
            viz = ContextViz(td, "compress-smoke")
            viz.start_round(filler[:50])
            q = asyncio.Queue()
            await _emit_context_snapshot(q, agent, viz, filler[:50], 420000)
            ev = q.get_nowait().get("data", {})
        ts = ev.get("token_stats", {})
        print("[快照] budget=%s threshold=%s compressed=%s summary_chars=%s pct=%s" % (
            ts.get("budget"), ts.get("threshold"), ts.get("compressed"),
            ts.get("summary_chars"), ts.get("pct")))
        ok_snapshot = (
            ts.get("compressed") is True
            and ts.get("budget") == CTX_SIZE
            and ts.get("threshold") == int(0.8 * CTX_SIZE)
            and bool(ev.get("compression_summary"))
        )
    except Exception as e:
        print("[ERROR] 快照捕获失败：", type(e).__name__, e)

    print("OK 压缩已触发（框架）" if ok_framework else "WARN 框架未触发压缩")
    print("OK 快照如实上报" if ok_snapshot else "WARN 快照未正确上报压缩")
    sys.exit(0 if (ok_framework and ok_snapshot) else 1)


if __name__ == "__main__":
    asyncio.run(main())
