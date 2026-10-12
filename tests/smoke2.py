"""PrismHarness 2.0 迁移冒烟：不碰真实 LLM，只验证导入、工具包、Agent 构造、事件流类型。

运行：python tests/smoke2.py
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PASS, FAIL = [], []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("  OK  " if cond else " FAIL ") + name + (f"  [{extra}]" if extra and not cond else ""))


async def main():
    # 1) 版本
    import agentscope
    print("agentscope", agentscope.__version__)
    check("agentscope>=2", agentscope.__version__.startswith("2."))

    # 2) 关键模块导入
    try:
        import model_config  # noqa: F401
        import tools  # noqa: F401
        import session  # noqa: F401
        import conf  # noqa: F401
        check("import model_config/tools/session/conf", True)
    except Exception as e:
        check("import model_config/tools/session/conf", False, repr(e))
        return

    try:
        import prism_harness_agent  # noqa: F401
        check("import prism_harness_agent", True)
    except Exception as e:
        check("import prism_harness_agent", False, repr(e))

    try:
        import server  # noqa: F401
        check("import server", True)
    except Exception as e:
        check("import server", False, repr(e))

    # 3) 工具包：注册名 + 受保护工具的权限
    from tools import build_toolkit
    from conf import GUARD_TOOLS
    try:
        tk = await build_toolkit("workspace", interactive=True)
        schemas = await tk.get_tool_schemas()
        names = sorted(s.get("function", {}).get("name", s.get("name", "?")) for s in schemas)
        print("  tools:", names)
        check("toolkit builds", len(names) > 0)
        check("has run_shell/view_text_file/write_text_file", {"run_shell", "view_text_file", "write_text_file"} <= set(names))
        check("has ask_user_question (interactive)", "ask_user_question" in names)
        tk2 = await build_toolkit("workspace", interactive=False)
        schemas2 = await tk2.get_tool_schemas()
        names2 = sorted(s.get("function", {}).get("name", s.get("name", "?")) for s in schemas2)
        check("team toolkit excludes ask_user_question", "ask_user_question" not in names2)
    except Exception as e:
        import traceback
        traceback.print_exc()
        check("toolkit builds", False, repr(e))

    # 4) Agent 构造（用假 base_url，不联网）
    try:
        from model_config import build_model
        from agentscope.agent import Agent, ReActConfig
        cfg = {
            "llm": {"provider": "p", "temperature": 0.0},
            "providers": {"p": {"api_key": "sk-xxx", "api_base": "http://127.0.0.1:1/v1", "model": "dummy"}},
        }
        model = build_model(cfg, stream=True)
        tk = await build_toolkit("workspace", interactive=True)
        agent = Agent(name="T", system_prompt="hi", model=model, toolkit=tk,
                      react_config=ReActConfig(max_iters=5))
        check("Agent constructs", agent.name == "T" and hasattr(agent, "state"))
        check("AgentState.permission_context present", agent.state.permission_context is not None)
    except Exception as e:
        import traceback
        traceback.print_exc()
        check("Agent constructs", False, repr(e))

    print(f"\nPASS {len(PASS)}  FAIL {len(FAIL)}")
    if FAIL:
        print("FAILED:", FAIL)
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
