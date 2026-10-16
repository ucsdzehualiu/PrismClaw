"""真实 LLM 冒烟：团队成员必须能真正执行工具（而不是被权限引擎停泊）。

背景（2026-10-15 事故）：团队成员 / lead / 风控 / 决策都用
`build_toolkit(interactive=False)`，但那时的实现把写文件/插文本/跑命令/下载
注册成 PermissionBehavior.ASK。这些执行单元没有面向用户的确认通道，于是一调
工具就停泊，Agent._next_action 把
「I'm waiting for your permission or the external execution to finish.」
当正常回复交出来（status 依然 ok）——投资大师团 19 位成员全部"成功"返回同一句
英文，圆桌报告里一条分析都没有。

本冒烟做的事：
  1) 直接构造一个真实团队成员，命令它必须用 run_shell 跑 `echo 42 2026`，
     断言：工具调用真的执行了（state.context 里有 run_shell 的 tool_call +
     tool_result），输出不是停泊文案，且真的含命令结果。
  2) 跑一次小规模圆桌（2 成员 + 风控 + 决策），断言没有任何成员被判失败、
     决策产出是真实文本而不是停泊文案。

用法：python tests/smoke_team_tools_live.py [provider]   （默认取 config.yaml）
"""

import asyncio
import copy
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from model_config import load_config  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("  OK  " if cond else " FAIL ") + name + (f"  [{extra}]" if extra and not cond else ""))


def tool_calls_of(agent):
    """从 agent.state.context 里收集本代理发出的工具调用名与工具结果文本。"""
    calls, results = [], []
    for msg in getattr(agent.state, "context", []) or []:
        if not hasattr(msg, "get_content_blocks"):
            continue
        for blk in msg.get_content_blocks("tool_call"):
            calls.append(getattr(blk, "name", ""))
        for blk in msg.get_content_blocks("tool_result"):
            out = getattr(blk, "output", None)
            results.append(str(getattr(out, "content", out)))
    return calls, results


async def part1_member_can_execute_tools(cfg):
    print("\n[1) 团队成员真的能跑工具]")
    import team as t

    member = {
        "name": "命令执行员",
        "role": "执行",
        "prompt": (
            "你必须调用 run_shell 工具执行命令 `echo 42 2026`，"
            "然后在回答里原样写出该命令的标准输出。"
            "禁止凭记忆或猜测作答，禁止说无法执行命令。"
        ),
    }
    agent = await t.build_team_agent(member, cfg, "workspace")
    from agentscope.message import UserMsg

    reply = await asyncio.wait_for(
        agent.reply(UserMsg(name="user", content="用 run_shell 跑 `echo 42 2026` 并报告输出。")),
        timeout=180,
    )
    text = reply.get_text_content() or ""
    calls, results = tool_calls_of(agent)

    print("     工具调用:", calls)
    print("     输出片段:", text.replace("\n", " ")[:200])
    check("成员真的发起了 run_shell 调用", "run_shell" in calls, str(calls))
    check("输出不是权限停泊文案", t.PERMISSION_WAIT_SENTINEL not in text, text[:80])
    check("输出不是被打断文案", not t.is_interrupted_output(text), text[:80])
    # Windows 的 echo 会带 \r\n，别按原样匹配 "42 2026"
    check("命令真的执行了（tool_result 含 42 与 2026）",
          any("42" in r and "2026" in r for r in results), str(results)[:160])
    check("最终回答含命令结果", "42" in text and "2026" in text, text[:120])


async def part2_small_roundtable(cfg):
    print("\n[2) 小规模圆桌：成员不再集体停泊]")
    import team as t

    spec = {
        "name": "工具冒烟圆桌",
        "type": "roundtable",
        "members": [
            {"name": "算术员甲", "role": "计算",
             "prompt": "调用 run_shell 执行 `echo 6*7`，报告输出。"},
            {"name": "算术员乙", "role": "计算",
             "prompt": "调用 run_shell 执行 `echo 3*11`，报告输出。"},
        ],
        "risk_prompt": "你是风控，一句话点评上述信号的可靠性。",
        "decision_prompt": "综合信号，用一句话给出结论。",
    }
    events = []

    async def emit(ev):
        events.append(ev)

    # run_team_stream 对 roundtable 返回 (决策报告, 成员结果) 两元组
    report, results = await asyncio.wait_for(
        t.run_team_stream(spec, "用工具算出两个乘积", emit, cfg, "workspace",
                          run_dir=None, member_timeout=180, max_concurrent=2),
        timeout=600,
    )
    bad = [r for r in results if r.get("status") != "ok"]
    print("     成员状态:", [(r["member"], r["status"]) for r in results])
    print("     决策产出:", (report or "").replace("\n", " ")[:200])
    check("所有成员都产出成功（无停泊/中断判失败）", not bad, str(bad)[:200])
    check("没有成员产出停泊文案",
          all(t.PERMISSION_WAIT_SENTINEL not in (r.get("output") or "") for r in results))
    check("决策报告是真实文本", bool((report or "").strip())
          and t.PERMISSION_WAIT_SENTINEL not in report, (report or "")[:80])
    check("发出了 done 事件", any(e.get("phase") == "done" for e in events))


async def main():
    cfg = copy.deepcopy(load_config())
    if len(sys.argv) > 1:
        cfg["llm"]["provider"] = sys.argv[1]
    print("provider:", cfg["llm"]["provider"],
          "model:", cfg["providers"][cfg["llm"]["provider"]]["model"])

    await part1_member_can_execute_tools(cfg)
    await part2_small_roundtable(cfg)

    print(f"\nPASS {len(PASS)}  FAIL {len(FAIL)}")
    if FAIL:
        for f in FAIL:
            print("  FAILED:", f)
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
