"""真实 LLM 冒烟：跑一次完整 agent 轮次，触发工具确认（HITL 停泊）并续跑。

用法：python tests/smoke_live.py [provider]   （默认 ali）
"""

import asyncio
import copy
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from model_config import build_model, load_config  # noqa: E402
from tools import build_toolkit  # noqa: E402
from agentscope.agent import Agent, ReActConfig, InjectionConfig  # noqa: E402
from agentscope.message import UserMsg  # noqa: E402
from agentscope.event import (  # noqa: E402
    RequireUserConfirmEvent, UserConfirmResultEvent, ConfirmResult, ReplyEndEvent,
)


async def main():
    provider = sys.argv[1] if len(sys.argv) > 1 else "ali"
    cfg = copy.deepcopy(load_config())
    cfg["llm"]["provider"] = provider

    model = build_model(cfg, stream=True)
    toolkit = await build_toolkit("workspace", interactive=True)
    agent = Agent(
        name="PrismHarness",
        system_prompt="你是助手。用户让你运行命令时，直接调用 run_shell 工具执行。",
        model=model,
        toolkit=toolkit,
        react_config=ReActConfig(max_iters=10),
        injection_config=InjectionConfig(inject_runtime_state=False),
    )

    inputs = UserMsg(name="user", content="请用 run_shell 执行命令 `echo hello-prism`，然后把输出告诉我。")
    confirms = 0
    for _round in range(6):
        pending = None
        text = []
        tool_results = []
        async for event in agent.reply_stream(inputs):
            if isinstance(event, RequireUserConfirmEvent):
                pending = event
            elif isinstance(event, ReplyEndEvent):
                pass
        if pending is None:
            break
        confirms += 1
        print(f"[HITL] 需要确认 {len(pending.tool_calls)} 个工具：",
              [tc.name for tc in pending.tool_calls])
        inputs = UserConfirmResultEvent(
            reply_id=pending.reply_id,
            confirm_results=[
                ConfirmResult(confirmed=True, tool_call=tc, rules=tc.suggested_rules or None)
                for tc in pending.tool_calls
            ],
        )

    # 打印最终回复
    final_text = ""
    for m in reversed(agent.state.context):
        t = m.get_text_content() if hasattr(m, "get_text_content") else ""
        if t:
            final_text = t
            break
    print("[HITL] 确认次数 =", confirms)
    print("[FINAL]", final_text[:400])
    print("OK 完成" if confirms >= 1 else "WARN 未触发确认（模型可能没用工具）")


if __name__ == "__main__":
    asyncio.run(main())
