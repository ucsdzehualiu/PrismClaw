"""团队编排（pipeline / roundtable / 扁平并行）的确定性回归测试。

不调用任何 LLM：把 build_team_agent / build_lead_agent 替换成假代理，
只验证「阶段顺序、并行、上下文累积、事件流、落盘、容错」这些编排逻辑。

运行：python tests/test_pipeline.py
"""

import asyncio
import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import team as t  # noqa: E402
except ModuleNotFoundError as e:
    # 最容易被绊到的坑：用系统 python 跑，没激活 launch.bat 那个 conda 环境
    missing = getattr(e, "name", "") or ""
    print("无法导入团队模块：%s（%s）" % (missing, e))
    if "agentscope" in missing:
        print("→ 请用 launch.bat 里的 conda 环境跑测试，例如：")
        print('   C:\\Users\\<你>\\.conda\\envs\\ai-course\\python.exe tests/test_pipeline.py')
        print("   或先 `conda activate ai-course` 再 `python tests/test_pipeline.py`")
    sys.exit(2)
    raise

PASS, FAIL = [], []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("  ✓ " if cond else "  ✗ ") + name + (f"  [{extra}]" if extra and not cond else ""))


class FakeReply:
    def __init__(self, text):
        self._t = text

    def get_text_content(self):
        return self._t


class FakeAgent:
    """可 await 的假代理；记录它收到的输入，供断言上下文传递。"""

    def __init__(self, name, text):
        self.name = name
        self.text = text
        self.seen_inputs = []

    async def __call__(self, msg):
        self.seen_inputs.append(msg.get_text_content() if hasattr(msg, "get_text_content") else str(msg))
        return FakeReply(self.text)

    def set_console_output_enabled(self, _b):
        pass


_ORIG = {}


def install_fakes():
    """替换代理构造器：成员回显「<名字>的输出」，并记录收到的输入。"""
    if not _ORIG:  # 只在第一次安装前保存真身，便于后续还原
        _ORIG.update({
            "build_team_agent": t.build_team_agent,
            "build_lead_agent": t.build_lead_agent,
            "run_design_agent": t._run_design_agent,
        })
    seen = {"inputs": []}

    def build_member(member, cfg, workspace_dir):
        name = member.get("name", "?")

        async def _f():
            a = FakeAgent(name, f"{name}的产出")
            seen["inputs"].append((name, a))
            return a

        return _f()

    async def build_lead(team, cfg, workspace_dir):
        a = FakeAgent("lead", "LEAD汇总报告")
        seen["inputs"].append(("lead", a))
        return a

    # roundtable 的风控/决策阶段直接调 _run_design_agent（自己建模型），一并替换。
    async def design(prompt, name, task, input_text, cfg, workspace_dir, timeout):
        return f"{name}的产出"

    t.build_team_agent = build_member
    t.build_lead_agent = build_lead
    t._run_design_agent = design
    return seen


def restore_fakes():
    """把被 install_fakes 换掉的真实构造器还原回去。"""
    if _ORIG:
        t.build_team_agent = _ORIG["build_team_agent"]
        t.build_lead_agent = _ORIG["build_lead_agent"]
        t._run_design_agent = _ORIG["run_design_agent"]


def pipe_spec():
    return {
        "name": "测试管线",
        "type": "pipeline",
        "final": "last",
        "stages": [
            {"id": "s1", "label": "扫描", "include_prev": False,
             "task": "对 {task} 做扫描",
             "members": [{"name": "甲", "prompt": "p"}, {"name": "乙", "prompt": "p"}]},
            {"id": "s2", "label": "汇合",
             "members": [{"name": "丙", "prompt": "p"}]},
        ],
    }


async def test_pipeline_basic():
    print("\n[pipeline 基本流程]")
    seen = install_fakes()
    events = []

    async def emit(ev):
        events.append(ev)

    tmp = tempfile.mkdtemp()
    try:
        report, results = await t.run_pipeline_stream(
            pipe_spec(), "研究英伟达", emit, {"agent": {}}, tmp,
            run_dir=os.path.join(tmp, "run"), member_timeout=5, max_concurrent=4,
        )
        phases = [e["phase"] for e in events]
        check("事件含 start/stage_start/stage_done/done",
              phases[0] == "start" and "stage_start" in phases
              and "stage_done" in phases and phases[-1] == "done", str(phases))
        check("两个阶段按序开始",
              [e["stage"] for e in events if e["phase"] == "stage_start"] == ["s1", "s2"])
        check("阶段1 两位成员并行产出", sum(1 for e in events if e["phase"] == "member_done") == 3)
        check("results 含 3 条产出", len(results) == 3, str(len(results)))
        check("最后阶段产出即报告", report == "丙的产出", report)
        stage1_inputs = [i for n, a in seen["inputs"] for i in a.seen_inputs if n in ("甲", "乙")]
        check("阶段1 输入含阶段任务且无上游",
              all("做扫描" in i and "的产出" not in i for i in stage1_inputs), str(stage1_inputs)[:200])
        s2 = [a for n, a in seen["inputs"] if n == "丙"][0]
        check("阶段2 输入带上了阶段1 的产出",
              "甲的产出" in s2.seen_inputs[0] and "乙的产出" in s2.seen_inputs[0])
        # 落盘
        rd = os.path.join(tmp, "run")
        check("落盘 config.json + SUMMARY.md + members",
              os.path.isfile(os.path.join(rd, "config.json"))
              and os.path.isfile(os.path.join(rd, "SUMMARY.md"))
              and len(os.listdir(os.path.join(rd, "members"))) == 3)
        md = open(os.path.join(rd, "SUMMARY.md"), encoding="utf-8").read()
        check("SUMMARY.md 含阶段名与最终报告", "扫描" in md and "LEAD汇总报告" not in md and "丙的产出" in md)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def test_pipeline_lead_final():
    print("\n[pipeline final=lead]")
    install_fakes()
    spec = pipe_spec()
    spec["final"] = "lead"
    spec["lead_prompt"] = "汇总"
    events = []

    async def emit(ev):
        events.append(ev)

    report, _ = await t.run_pipeline_stream(spec, "t", emit, {"agent": {}}, ".", member_timeout=5)
    check("final=lead 时走主理人汇总", report == "LEAD汇总报告", report)
    check("含 synthesis 事件", any(e["phase"] == "synthesis_start" for e in events))


async def test_pipeline_error_tolerance():
    print("\n[pipeline 容错：成员抛错不中断]")
    install_fakes()

    # build_team_agent 是同步函数、返回可 await 的对象；这里让它直接抛错。
    def boom(member, cfg, workspace_dir):
        raise RuntimeError("成员炸了")

    t.build_team_agent = boom
    events = []

    async def emit(ev):
        events.append(ev)

    report, results = await t.run_pipeline_stream(
        pipe_spec(), "t", emit, {"agent": {}}, ".", member_timeout=5)
    check("成员失败被记为 error 而非抛出", all(r["status"] == "error" for r in results))
    check("失败事件已发出", any(e["phase"] == "member_error" for e in events))
    check("流程仍走到 done", events[-1]["phase"] == "done")


async def test_pipeline_retry_on_interrupt():
    print("\n[pipeline 打断重试]")
    install_fakes()
    calls = {"n": 0}

    def flaky(member, cfg, workspace_dir):
        name = member.get("name", "?")

        async def _f():
            calls["n"] += 1
            if calls["n"] == 1:
                return FakeAgent(name, t.INTERRUPTED_SENTINEL)
            return FakeAgent(name, f"{name}的产出")

        return _f()

    t.build_team_agent = flaky
    spec = {"name": "x", "type": "pipeline", "final": "last",
            "stages": [{"id": "s1", "members": [{"name": "甲", "prompt": "p"}]}]}
    events = []

    async def emit(ev):
        events.append(ev)

    _, results = await t.run_pipeline_stream(
        spec, "t", emit, {"agent": {"team_retries": 2}}, ".", member_timeout=5)
    check("被打断后重建重试拿到真结果", results[0]["status"] == "ok" and "的产出" in results[0]["output"])
    check("重试事件已发出", any(e["phase"] == "member_retrying" for e in events))


async def test_flat_and_roundtable_dispatch():
    print("\n[扁平并行 / roundtable 分发]")
    install_fakes()
    flat = {"name": "扁平", "members": [{"name": "甲", "prompt": "p"}, {"name": "乙", "prompt": "p"}],
            "lead_prompt": "汇总"}
    events = []

    async def emit(ev):
        events.append(ev)

    report, results = await t.run_team_stream(flat, "t", emit, {"agent": {}}, ".", member_timeout=5)
    check("扁平团队：lead 汇总为报告", report == "LEAD汇总报告", report)
    check("扁平团队：两位成员产出", len(results) == 2)

    rt = {"name": "圆桌", "type": "roundtable", "members": [{"name": "甲", "prompt": "p"}],
          "risk_prompt": "风控", "decision_prompt": "决策"}
    events.clear()
    report2, results2 = await t.run_team_stream(rt, "t", emit, {"agent": {}}, ".", member_timeout=5)
    check("roundtable：决策报告为最终产出", report2 == "portfolio-manager的产出", report2)
    check("roundtable：含 stage 事件", any(e.get("stage") == "analysis" for e in events))
    # 前端「阶段 x/y」依赖 stage_start 带上 index/total
    ss = [e for e in events if e.get("phase") == "stage_start"]
    check("roundtable 阶段事件带 index/total",
          [e.get("index") for e in ss] == [0, 1, 2] and all(e.get("total") == 3 for e in ss),
          str([(e.get("index"), e.get("total")) for e in ss]))

    # 无 risk_prompt 的圆桌只有 2 个阶段，total 要跟着变
    rt2 = {"name": "圆桌2", "type": "roundtable", "members": [{"name": "甲", "prompt": "p"}],
           "decision_prompt": "决策"}
    events.clear()
    await t.run_team_stream(rt2, "t", emit, {"agent": {}}, ".", member_timeout=5)
    ss2 = [e for e in events if e.get("phase") == "stage_start"]
    check("无风控时 roundtable total=2",
          [e.get("index") for e in ss2] == [0, 1] and all(e.get("total") == 2 for e in ss2),
          str([(e.get("index"), e.get("total")) for e in ss2]))

    # pipeline 的阶段事件同样带序号，且 total 等于阶段数
    events.clear()
    await t.run_team_stream(pipe_spec(), "t", emit, {"agent": {}}, ".", member_timeout=5)
    ps = [e for e in events if e.get("phase") == "stage_start"]
    check("pipeline 阶段事件带 index/total",
          [e.get("index") for e in ps] == [0, 1] and all(e.get("total") == 2 for e in ps),
          str([(e.get("index"), e.get("total")) for e in ps]))


async def test_load_save_delete():
    print("\n[团队读写：workspace 优先 + 内置回退]")
    tmp = tempfile.mkdtemp()
    try:
        os.makedirs(os.path.join(tmp, "teams"))
        spec = {"name": "本地团队", "members": [{"name": "a", "prompt": "p"}]}
        r = t.save_team(spec, tmp)
        check("保存成功", r.get("status") == "success", str(r))
        loaded = t.load_team("本地团队", tmp)
        check("读回一致", loaded and loaded["name"] == "本地团队")
        check("覆盖内置同名", t.load_team("本团队不存在", tmp) is None)
        check("删除成功", t.delete_team("本地团队", tmp) and t.load_team("本地团队", tmp) is None)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def test_builtin_teams_execute():
    """把 teams/ 下每个内置团队的 spec 都真跑一遍（用假代理，不调模型）。

    作用：任何团队 spec 写错（阶段缺 members、prompt 为空、类型不认识）都会在这里暴露；
    port_experts.py 新增/改动团队后，这里是最后一道闸。
    """
    print("\n[所有内置团队 spec 可执行]")
    install_fakes()
    events = []

    async def emit(ev):
        events.append(ev)

    metas = [x for x in t.list_teams("workspace") if x["builtin"]]
    problems = []
    for meta in metas:
        spec = t.load_team(meta["slug"], "workspace")
        if not spec:
            problems.append((meta["name"], "加载失败"))
            continue
        events.clear()
        try:
            report, results = await t.run_team_stream(
                spec, "冒烟任务", emit, {"agent": {}}, ".", member_timeout=5)
        except Exception as e:
            problems.append((meta["name"], f"抛异常 {type(e).__name__}: {e}"))
            continue
        if not (report or "").strip():
            problems.append((meta["name"], "报告为空"))
            continue
        errs = [r.get("member") for r in results if r.get("status") != "ok"]
        if errs:
            problems.append((meta["name"], "成员失败: " + "、".join(str(e) for e in errs)))
            continue
        if not any(e.get("phase") == "done" for e in events):
            problems.append((meta["name"], "未发出 done 事件"))
            continue
    check(f"{len(metas)} 个内置团队全部执行成功", not problems, str(problems[:5]))
    if metas:
        print("     已覆盖:", "、".join(m["name"] for m in metas))

    # 每个移植团队都要能反查回源专家包（团队文件名是中文显示名，光看名字对不回去）
    no_src = [m["name"] for m in metas
              if not (t.load_team(m["slug"], "workspace") or {}).get("source")]
    check("每个内置团队都记录了移植来源(source)", not no_src, str(no_src))
    check("list_teams 暴露了 source 字段",
          all("source" in m for m in metas))


async def test_toolkit_interactive():
    """只有主代理能弹卡片提问。

    团队成员/子代理没有 HITL 会话，guard 不会拦截 ask_user_question；
    若照样注册，它们调完只拿到一句占位文本，就会**误以为已经问过用户**
    （表现为报告里写「我在卡片里放了四个问题」，而卡片从未出现）。
    """
    print("\n[工具集：只有主代理能弹卡片提问]")
    from tools import build_toolkit

    def names(tk):
        fn = getattr(tk, "tools", None) or {}
        if isinstance(fn, dict):
            return set(fn.keys())
        return {str(getattr(x, "name", x)) for x in (fn or [])}

    main_tools = names(await build_toolkit("workspace", interactive=True))
    member_tools = names(await build_toolkit("workspace", interactive=False))
    check("主代理有 ask_user_question（能弹卡片）", "ask_user_question" in main_tools)
    check("团队成员没有 ask_user_question（不会误以为问过用户）",
          "ask_user_question" not in member_tools)
    # 成员若能再拉起团队/子代理，就是嵌套编排，成本失控
    check("团队成员没有 run_team（不会嵌套编排）", "run_team" not in member_tools)
    check("团队成员没有 subagent（不会无限套娃）", "subagent" not in member_tools)
    check("执行类工具保持不变（读写/搜索/命令仍在）",
          {"view_text_file", "write_text_file", "run_shell", "web_search"} <= member_tools,
          str(sorted(member_tools)))
    check("主代理与成员的差集恰好是那三个编排/交互工具",
          main_tools - member_tools == {"ask_user_question", "run_team", "subagent"},
          str(main_tools ^ member_tools))

    # 真实构造一个团队成员，确认它拿到的工具集里确实没有该工具
    # （先把假代理换回真身，否则测的是假代理，等于没测）
    restore_fakes()
    try:
        import team as _t
        from model_config import load_config
        cfg = load_config("config.yaml")
        agent = await _t.build_team_agent({"name": "自检成员", "prompt": "p"}, cfg, "workspace")
        real = names(agent.toolkit)
        check("真实构造的团队成员也没有该工具", "ask_user_question" not in real)
    except Exception as e:
        print(f"     （跳过真实构造检查：{type(e).__name__}: {e}）")


async def test_interrupt_never_leaks_as_report():
    """汇总/裁决类代理被打断时，绝不能把预设文案当交付物。

    现象：主理人（或风控/决策）在工具执行中被 cancel，Agentscope 会把
    「I noticed that you have interrupted me.」当成正常回复返回，状态还是 ok。
    早先只有成员做了检测，主理人这条路没查，于是那句话直接成了最终报告。
    """
    print("\n[汇总/裁决被打断时不得泄漏预设文案]")
    install_fakes()

    async def bad_lead(team, cfg, workspace_dir):
        return FakeAgent("lead", t.INTERRUPTED_SENTINEL)

    async def bad_design(prompt, name, task, input_text, cfg, workspace_dir, timeout):
        return t.INTERRUPTED_SENTINEL

    events = []

    async def emit(ev):
        events.append(ev)

    cfg = {"agent": {"team_retries": 2}}

    # 1) 扁平团队：主理人汇总被打断
    t.build_lead_agent = bad_lead
    flat = {"name": "扁平", "members": [{"name": "甲", "prompt": "p"}], "lead_prompt": "汇总"}
    report, _ = await t.run_team_stream(flat, "t", emit, cfg, ".", member_timeout=5)
    check("扁平团队：报告不含『被打断』文案", not t.is_interrupted_output(report))
    check("扁平团队：降级为列出成员产出", "甲的产出" in report, report[:80])

    # 2) pipeline final=lead：主理人汇总被打断
    spec = pipe_spec()
    spec["final"] = "lead"
    spec["lead_prompt"] = "汇总"
    events.clear()
    report2, _ = await t.run_team_stream(spec, "t", emit, cfg, ".", member_timeout=5)
    check("pipeline：报告不含『被打断』文案", not t.is_interrupted_output(report2))
    check("pipeline：退回末阶段产出（不丢内容）", "丙的产出" in report2, report2[:80])
    check("pipeline：发出了中断告警事件",
          any(e.get("member") == "主理人" for e in events))

    # 3) roundtable：风控与决策都被打断
    install_fakes()
    t._run_design_agent = bad_design
    rt = {"name": "圆桌", "type": "roundtable", "members": [{"name": "甲", "prompt": "p"}],
          "risk_prompt": "风控", "decision_prompt": "决策"}
    events.clear()
    report3, _ = await t.run_team_stream(rt, "t", emit, cfg, ".", member_timeout=5)
    check("roundtable：报告不含『被打断』文案", not t.is_interrupted_output(report3))
    check("roundtable：降级产出含成员信号", "甲的产出" in report3, report3[:80])

    restore_fakes()


async def main():
    await test_pipeline_basic()
    await test_pipeline_lead_final()
    await test_pipeline_error_tolerance()
    await test_pipeline_retry_on_interrupt()
    await test_flat_and_roundtable_dispatch()
    await test_load_save_delete()
    await test_builtin_teams_execute()
    await test_interrupt_never_leaks_as_report()
    await test_toolkit_interactive()
    print(f"\n通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    if FAIL:
        for f in FAIL:
            print("  FAILED:", f)
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
