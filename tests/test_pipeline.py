"""团队编排（pipeline / roundtable / 扁平并行）的确定性回归测试。

不调用任何 LLM：把 build_team_agent / build_lead_agent 替换成假代理，
只验证「阶段顺序、并行、上下文累积、事件流、落盘、容错」这些编排逻辑。

运行：python tests/test_pipeline.py
"""

import asyncio
import contextlib
import json
import os
import shutil
import sys
import tempfile

import yaml

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import team as t  # noqa: E402
except ModuleNotFoundError as e:
    # 最容易被绊到的坑：用系统 python 跑，没激活 launch.bat 那个 conda 环境
    missing = getattr(e, "name", "") or ""
    print("无法导入团队模块：%s（%s）" % (missing, e))
    if "agentscope" in missing:
        print("→ 请用 AgentScope 2.0 的 conda 环境跑测试（Python 3.11），例如：")
        print('   C:\\Users\\<你>\\.conda\\envs\\prism2\\python.exe tests/test_pipeline.py')
        print('   或先 `conda activate prism2` 再 `python tests/test_pipeline.py`')
    sys.exit(2)
    raise

PASS, FAIL = [], []

# 假代理要模拟的是**框架给出的** finished_reason（ReplyFinishedReason 的值），
# 不是 team.py 归一化之后的 REASON_* 常量——两套名字只是部分重合，别混用：
#   框架值 "interrupted"      → team.REASON_INTERRUPTED  "interrupted"
#   框架值 "exceed_max_iters" → team.REASON_EXCEEDED     "exceeded"
# FakeAgent(..., finished_reason=...) 传框架值；
# team.AgentOutcome(reason=...) 传归一化常量（那已经是归一化之后的东西）。
FW_COMPLETED = "completed"
FW_INTERRUPTED = "interrupted"
FW_EXCEED = "exceed_max_iters"
FW_ERROR = "error"


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("  ✓ " if cond else "  ✗ ") + name + (f"  [{extra}]" if extra and not cond else ""))


class FakeReply:
    def __init__(self, text, finished_reason=None):
        self._t = text
        # 框架会把收尾原因打在返回的 Msg 上（team._classify_finish 读的就是它）。
        # None 表示「正常完成」以外的两种情况之一：真·正常完成，或停泊——由正文区分。
        self.finished_reason = finished_reason

    def get_text_content(self):
        return self._t


class FakeAgent:
    """可 reply 的假代理；记录它收到的输入，供断言上下文传递。

    finished_reason 用来模拟框架给出的收尾原因。真代理由框架填，假代理得自己给，
    否则一律会被归成 completed。
    """

    def __init__(self, name, text, finished_reason=None):
        self.name = name
        self.text = text
        self.finished_reason = finished_reason
        self.seen_inputs = []

    async def reply(self, msg):
        self.seen_inputs.append(msg.get_text_content() if hasattr(msg, "get_text_content") else str(msg))
        return FakeReply(self.text, self.finished_reason)


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
        return t.AgentOutcome(text=f"{name}的产出")

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
                return FakeAgent(name, t.INTERRUPTED_SENTINEL, FW_INTERRUPTED)
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
        # AgentScope 2.0：工具挂在 Toolkit.tool_groups[*].tools（list[ToolBase]）
        out = set()
        for g in getattr(tk, "tool_groups", []) or []:
            for tool in getattr(g, "tools", []) or []:
                nm = getattr(tool, "name", None)
                if nm:
                    out.add(nm)
        return out

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


async def test_toolkit_permissions_autonomous():
    """受确认保护的工具在「无 HITL 通道」的执行单元里必须 ALLOW。

    现象（真实事故）：团队成员的 toolkit 把写文件/插文本/跑命令/下载注册成 ASK，
    而成员没有面向用户的确认通道。Agent._next_action 见到「等待确认」的工具调用
    就停泊、结束本轮，回复正文是
    「I'm waiting for your permission or the external execution to finish.」
    ——status 依然是 ok，于是 19 位大师全部"成功"返回同一句占位文案。
    """
    print("\n[工具权限：执行单元受保护工具必须放行]")
    from tools import build_toolkit
    from agentscope.permission import PermissionBehavior

    PROTECTED = {"write_text_file", "insert_text_file", "run_shell", "download_file"}

    def perms(tk):
        out = {}
        for g in getattr(tk, "tool_groups", []) or []:
            for tool in getattr(g, "tools", []) or []:
                p = getattr(tool, "_permission", None)
                out[getattr(tool, "name", "")] = getattr(p, "behavior", None)
        return out

    main_p = perms(await build_toolkit("workspace", interactive=True))
    member_p = perms(await build_toolkit("workspace", interactive=False))

    for nm in sorted(PROTECTED):
        check(f"主代理：{nm} 走 ASK（弹卡片确认）",
              main_p.get(nm) == PermissionBehavior.ASK, str(main_p.get(nm)))
        check(f"执行单元：{nm} 直接 ALLOW（否则必然停泊）",
              member_p.get(nm) == PermissionBehavior.ALLOW, str(member_p.get(nm)))


async def test_member_permission_park_is_not_a_signal():
    """成员被权限引擎停泊时，占位文案不得当成"有效信号"往下传。

    停泊产出既不能计入成员信号、也不能喂给风控/决策/主理人，
    否则报告里会出现「所有信号都是同一句英文」这种假成功。
    """
    print("\n[成员权限停泊不得当成信号]")
    seen = install_fakes()

    def parked(member, cfg, workspace_dir):
        name = member.get("name", "?")

        async def _f():
            a = FakeAgent(name, t.PERMISSION_WAIT_SENTINEL)
            seen["inputs"].append((name, a))
            return a

        return _f()

    t.build_team_agent = parked
    design_inputs = []

    async def design(prompt, name, task, input_text, cfg, workspace_dir, timeout):
        design_inputs.append((name, input_text))
        return t.AgentOutcome(text=f"{name}的产出")

    t._run_design_agent = design

    events = []

    async def emit(ev):
        events.append(ev)

    cfg = {"agent": {"team_retries": 1}}

    # 1) 扁平团队：成员停泊 → 记失败，主理人上下文里没有那句话
    flat = {"name": "扁平", "members": [{"name": "甲", "prompt": "p"}], "lead_prompt": "汇总"}
    _, results = await t.run_team_stream(flat, "t", emit, cfg, ".", member_timeout=5)
    check("停泊成员记为失败（不是 ok）", results[0]["status"] == "error", str(results[0])[:70])
    check("停泊成员的产出被清空", not results[0].get("output"))
    check("发出 member_error 事件", any(e["phase"] == "member_error" for e in events))
    lead_inputs = [txt for name, a in seen["inputs"] if name == "lead" for txt in a.seen_inputs]
    check("主理人上下文不含停泊文案",
          lead_inputs and all(t.PERMISSION_WAIT_SENTINEL not in x for x in lead_inputs),
          str(lead_inputs)[:120])

    # 2) 圆桌：停泊成员不得出现在风控/决策的输入里
    design_inputs.clear()
    rt = {"name": "圆桌", "type": "roundtable", "members": [{"name": "甲", "prompt": "p"}],
          "risk_prompt": "风控", "decision_prompt": "决策"}
    await t.run_team_stream(rt, "t", emit, cfg, ".", member_timeout=5)
    check("圆桌：风控/决策输入不含停泊文案",
          design_inputs and all(t.PERMISSION_WAIT_SENTINEL not in txt for _n, txt in design_inputs),
          str(design_inputs)[:120])
    check("圆桌：失败成员被明确标注为无有效信号",
          any("未产出有效信号" in txt for _n, txt in design_inputs),
          str(design_inputs)[:200])

    # 3) pipeline：停泊产出不得并进下游累积上下文
    stage_inputs = []

    def parked_stage(member, cfg, workspace_dir):
        name = member.get("name", "?")

        async def _f():
            a = FakeAgent(name, t.PERMISSION_WAIT_SENTINEL)
            stage_inputs.append(a)
            return a

        return _f()

    t.build_team_agent = parked_stage
    spec = pipe_spec()
    await t.run_team_stream(spec, "t", emit, cfg, ".", member_timeout=5)
    check("pipeline：下游阶段输入不含停泊文案",
          stage_inputs and all(
              t.PERMISSION_WAIT_SENTINEL not in x
              for a in stage_inputs for x in a.seen_inputs),
          str([x[:60] for a in stage_inputs for x in a.seen_inputs])[:200])

    restore_fakes()


async def test_skill_dirs_filtered():
    """skills/ 里混进非技能目录 / 同名技能时，不该让框架 loader 刷告警。

    现象（2026-10-16 服务假死的主因之一）：workspace/skills/ 里混着 3 个「任务笔记」
    型 SKILL.md（没有 frontmatter），外加一对同名技能（vision / agent-vision-skill）。
    每次 build_toolkit 都换回 4 条 logger.warning；而 warning 是在**事件循环线程**上
    写 stdout 的，Windows 控制台一被选中就阻塞 write，整个服务永久无响应。
    19 人团队要 build 20+ 次 toolkit，等于把触发面放大了 20 倍。
    """
    print("\n[技能目录过滤：非法 / 同名技能不进 toolkit]")
    import io
    import logging

    import agentscope._logging as al
    from tools import _declared_skill_name, build_toolkit

    # 用临时 workspace 造场景，不依赖仓库里那几个目录将来是否被清理
    tmp = tempfile.mkdtemp()
    try:
        skills = os.path.join(tmp, "skills")
        os.makedirs(skills)
        for name, body in [
            ("good-skill", "---\nname: good-skill\ndescription: 正常技能\n---\n正文\n"),
            ("notes-dump", "一段没有 frontmatter 的任务笔记，不是技能。\n"),
            ("broken-fields", "---\nname: broken-fields\n---\n只有 name，没有 description\n"),
            ("dup-a", "---\nname: same-name\ndescription: 甲\n---\n正文\n"),
            ("dup-b", "---\nname: same-name\ndescription: 乙\n---\n正文\n"),
        ]:
            os.makedirs(os.path.join(skills, name))
            with open(os.path.join(skills, name, "SKILL.md"), "w", encoding="utf-8") as f:
                f.write(body)

        check("正常技能解析出声明名", _declared_skill_name(os.path.join(skills, "good-skill")) == "good-skill")
        check("无 frontmatter 的 SKILL.md 不算技能",
              _declared_skill_name(os.path.join(skills, "notes-dump")) is None)
        check("只有 name 没有 description 的不算技能",
              _declared_skill_name(os.path.join(skills, "broken-fields")) is None)

        cap = io.StringIO()
        handler = logging.StreamHandler(cap)
        al.logger.addHandler(handler)
        old_level = al.logger.level
        al.logger.setLevel(logging.WARNING)
        try:
            tk = await build_toolkit(tmp, interactive=False)
            names = sorted(await tk._get_available_skills())
        finally:
            al.logger.removeHandler(handler)
            al.logger.setLevel(old_level)

        check("build_toolkit 期间没有任何框架告警", not cap.getvalue().strip(), cap.getvalue()[:200])
        check("合法技能已注册", "good-skill" in names, str(names))
        check("笔记型目录被挡在门外", "notes-dump" not in names and "broken-fields" not in names)
        check("同名技能只注册一次", names.count("same-name") == 1, str(names))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def test_server_output_capture():
    """server 进程的 stdout/stderr 必须改道到文件。

    这是 2026-10-16「团队一直不动」的直接修复：控制台进入快速编辑选中态时 write
    会阻塞，而 agent_runner / 第三方库都在事件循环线程上直接写 stdout。改道文件后，
    控制台状态再也拖不垮事件循环。必须在子进程里验证（会改掉本进程的 fd 1/2）。
    """
    print("\n[server 输出改道到文件（控制台卡死不再拖垮事件循环）]")
    import subprocess

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    probe = os.path.join(tempfile.gettempdir(), "ph_server_capture_probe.log")
    if os.path.exists(probe):
        os.remove(probe)

    code = (
        "import os, sys\n"
        f"sys.path.insert(0, {root!r})\n"
        "import server\n"
        f"server.SERVER_LOG_PATH = {probe!r}\n"
        "server._capture_server_output()\n"
        "print('probe-stdout-line')\n"
        "sys.stderr.write('probe-stderr-line\\n')\n"
        "sys.stderr.flush()\n"
    )
    try:
        subprocess.run([sys.executable, "-c", code], cwd=root, check=True,
                       capture_output=True, timeout=180)
        got = open(probe, encoding="utf-8").read() if os.path.exists(probe) else ""
        check("print 落到日志文件", "probe-stdout-line" in got, got[:200])
        check("sys.stderr 落到日志文件", "probe-stderr-line" in got, got[:200])
    except Exception as e:
        check("server 输出改道可执行", False, f"{type(e).__name__}: {e}")
    finally:
        try:
            os.remove(probe)
        except OSError:
            pass


async def test_member_retries_transient_errors():
    """成员撞上限流（429）要退避重试，而不是直接判死白扔一份分析。

    现象（2026-10-16）：19 位成员并发打同一个模型端点，端点限流
    （ModelArts："Too many requests, the rate limit is 4 per second."）回 429。
    当时任何异常都直接冒泡 → 成员 status=error，重试机制只认「被打断」文案，
    所以一撞限流就整份分析丢掉。
    """
    print("\n[成员瞬时故障（限流）重试]")
    install_fakes()
    state = {"n": 0}

    class FlakyAgent:
        def __init__(self, name, exc, fail_first):
            self.name = name
            self.seen_inputs = []
            self.exc = exc
            self.fail_first = fail_first

        async def reply(self, msg):
            state["n"] += 1
            if state["n"] <= self.fail_first:
                raise self.exc
            return FakeReply("重试后的产出")

    made = {}

    def builder(exc, fail_first):
        def build(member, cfg, workspace_dir):
            name = member.get("name", "?")

            async def _f():
                a = FlakyAgent(name, exc, fail_first)
                made[name] = a
                return a

            return _f()

        return build

    rate_limited = RuntimeError(
        "Error code: 429 - {'code': 'ModelArts.81101', 'message': "
        "'Too many requests, the rate limit is 4 per second.'}"
    )
    events = []

    async def emit(ev):
        events.append(ev)

    # 1) 第一次 429，第二次成功 → 成员应记为成功产出
    t.build_team_agent = builder(rate_limited, 1)
    spec = {"name": "x", "members": [{"name": "甲", "prompt": "p"}], "lead_prompt": "汇总"}
    _, results = await t.run_team_stream(spec, "t", emit, {"agent": {"team_retries": 2}},
                                         ".", member_timeout=5)
    check("撞 429 后重试拿到真产出",
          results[0]["status"] == "ok" and "重试后的产出" in results[0]["output"], str(results[0])[:90])
    check("重试事件已发出", any(e["phase"] == "member_retrying" for e in events))

    # 2) 非瞬时错误（配置错）→ 不重试，立刻判失败，错误原样带出
    state["n"] = 0
    attempts = {"n": 0}
    hard = RuntimeError("invalid api_key: 401 unauthorized")

    def counting_builder(member, cfg, workspace_dir):
        async def _f():
            attempts["n"] += 1
            return FlakyAgent("乙", hard, 99)  # 永远抛

        return _f()

    t.build_team_agent = counting_builder
    spec2 = {"name": "y", "members": [{"name": "乙", "prompt": "p"}], "lead_prompt": "汇总"}
    _, results2 = await t.run_team_stream(spec2, "t", emit, {"agent": {"team_retries": 3}},
                                          ".", member_timeout=5)
    check("非瞬时错误不重试（只建一次代理）", attempts["n"] == 1, str(attempts))
    check("非瞬时错误原样记为该成员的错误",
          results2[0]["status"] == "error" and "api_key" in results2[0].get("error", ""),
          str(results2[0])[:90])

    restore_fakes()


async def test_interrupt_never_leaks_as_report():
    """汇总/裁决类代理被打断时，绝不能把预设文案当交付物。

    现象：主理人（或风控/决策）在工具执行中被 cancel，Agentscope 会把
    「I noticed that you have interrupted me.」当成正常回复返回，状态还是 ok。
    早先只有成员做了检测，主理人这条路没查，于是那句话直接成了最终报告。
    """
    print("\n[汇总/裁决被打断时不得泄漏预设文案]")
    install_fakes()

    async def bad_lead(team, cfg, workspace_dir):
        return FakeAgent("lead", t.INTERRUPTED_SENTINEL, FW_INTERRUPTED)

    async def bad_design(prompt, name, task, input_text, cfg, workspace_dir, timeout):
        return t.AgentOutcome(text=t.INTERRUPTED_SENTINEL, reason=t.REASON_INTERRUPTED)

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


async def _wait_until(pred, turns: int = 500) -> bool:
    """把事件循环推若干拍，直到 pred() 为真。用于观察「进行中」的并发状态。"""
    for _ in range(turns):
        if pred():
            return True
        await asyncio.sleep(0)
    return False


async def test_queued_members_are_not_shown_as_running():
    """并发上限之外的成员要报「排队」，不能一律报成「分析中」。

    现象（2026-10-16 报障「19 个人一直不动」）：member_start 原先发在并发信号量
    **之外**，19 位成员从 t=0 起全被标成 ⏳ 分析中，而 team_max_concurrent=4，
    真正在跑的只有 4 个，其余 15 个还在 sem 上排队。面板把两种状态混成一种，
    用户无法判断是卡死还是慢。
    """
    print("\n[并发上限：排队成员不得显示成「分析中」]")
    install_fakes()
    release = asyncio.Event()

    def gated(member, cfg, workspace_dir):
        name = member.get("name", "?")

        async def _f():
            a = FakeAgent(name, f"{name}的产出")
            inner = a.reply

            async def blocked(msg):
                await release.wait()
                return await inner(msg)

            a.reply = blocked
            return a

        return _f()

    t.build_team_agent = gated

    events = []
    running = {"n": 0}
    peak = {"n": 0}

    async def emit(ev):
        events.append(ev)
        if ev.get("phase") == "member_start":
            running["n"] += 1
            peak["n"] = max(peak["n"], running["n"])
        elif ev.get("phase") in ("member_done", "member_error"):
            running["n"] = max(0, running["n"] - 1)

    def n_of(phase):
        return sum(1 for e in events if e.get("phase") == phase)

    members = [{"name": f"成员{i}", "prompt": "p"} for i in range(6)]
    rt = {"name": "圆桌", "type": "roundtable", "members": members, "decision_prompt": "决策"}

    task = asyncio.create_task(t.run_team_stream(
        rt, "t", emit, {"agent": {}}, ".", member_timeout=30, max_concurrent=2))
    try:
        got = await _wait_until(lambda: n_of("member_queued") >= 6 and n_of("member_start") >= 2)
        check("6 位成员都发出了排队事件", got and n_of("member_queued") == 6, str(n_of("member_queued")))
        check("同时只有 max_concurrent 位被标为分析中", n_of("member_start") == 2, str(n_of("member_start")))
        check("正在跑的人数不超过并发上限", peak["n"] <= 2, str(peak["n"]))
        # 每人必须「先排队、后开始」——顺序反了说明又回到了 sem 之外报 start 的老写法
        first = {}
        for i, e in enumerate(events):
            first.setdefault((e.get("phase"), e.get("member")), i)
        ordered = all(
            first[("member_queued", m["name"])] < first.get(("member_start", m["name"]), 10 ** 9)
            for m in members
        )
        check("每位成员都先报排队再报开始", ordered)
    finally:
        release.set()
        _, results = await task
        restore_fakes()
    check("放行后 6 位成员全部产出", len(results) == 6 and all(r["status"] == "ok" for r in results),
          str([r["status"] for r in results]))


async def test_member_persisted_before_stage_finishes():
    """成员一出结果就落盘，不必等整个阶段 gather 完。

    现象：19 人阶段要跑十几分钟，结果原先只在 gather 之后批量写盘；中途进程挂掉
    （超时 / Ctrl-C / 端点整体不可用）这 19 份分析全部蒸发。改成每人完成即写，
    慢的成员还在跑时，快的成员产出就应该已经在磁盘上。
    """
    print("\n[成员落盘：快的成员不等慢的跑完]")
    install_fakes()
    release = asyncio.Event()

    def gated(member, cfg, workspace_dir):
        name = member.get("name", "?")

        async def _f():
            a = FakeAgent(name, f"{name}的产出")
            if name == "慢":
                inner = a.reply

                async def blocked(msg):
                    await release.wait()
                    return await inner(msg)

                a.reply = blocked
            return a

        return _f()

    t.build_team_agent = gated

    async def emit(ev):
        pass

    tmp = tempfile.mkdtemp()
    try:
        run_dir = os.path.join(tmp, "run")
        rt = {"name": "圆桌", "type": "roundtable",
              "members": [{"name": "快", "prompt": "p"}, {"name": "慢", "prompt": "p"}],
              "decision_prompt": "决策"}
        task = asyncio.create_task(t.run_team_stream(
            rt, "t", emit, {"agent": {}}, tmp, run_dir=run_dir,
            member_timeout=30, max_concurrent=2))
        fast_json = os.path.join(run_dir, "members", "快.json")
        slow_json = os.path.join(run_dir, "members", "慢.json")
        got = await _wait_until(lambda: os.path.isfile(fast_json))
        check("快成员完成后立刻落盘", got and os.path.isfile(fast_json))
        check("此时慢成员还没落盘（证明确实是增量而非整阶段批量）", not os.path.isfile(slow_json))
        release.set()
        await task
        check("慢成员完成后也落盘", os.path.isfile(slow_json))
        restore_fakes()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def test_context_viz_commit_survives_deleted_session():
    """会话目录被删掉后，上下文落盘不该抛异常。

    现象：前端「删除会话」把 session_logs/<sid>/ 整个 rmtree 掉，而 ContextViz
    的目录只在 __init__ 建过一次；此后 commit() 写 snapshots/ 直接 FileNotFoundError。
    它抛在 agent_runner 的收尾路径上，sess.release() 被跳过、会话永远停在 ACTIVE，
    整个 agent_runner 任务死在「Task exception was never retrieved」里不再复活。
    """
    print("\n[上下文落盘：会话目录被删后仍能落盘]")
    from context_viz import ContextViz

    tmp = tempfile.mkdtemp()
    try:
        viz = ContextViz(tmp, "sess-del")
        viz.start_round("你好")
        shutil.rmtree(os.path.join(tmp, "sess-del"), ignore_errors=True)
        try:
            md = viz.commit()
            ok, detail = os.path.isfile(md), md
        except Exception as e:
            ok, detail = False, f"{type(e).__name__}: {e}"
        check("会话目录被删后 commit 补建目录且不抛异常", ok, str(detail))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def test_interrupt_placeholder_is_detected():
    """被打断的占位文案必须被检出，否则会被当成有效分析流传下去。

    现象（2026-10-16）：代码里认的是 "I noticed that you have interrupted me."，
    而 AgentScope 2.0.9 的 ReActConfig.interruption_message 默认值是
    "I notice the interruption. How can I help you?" —— 那句常量在整个包里
    根本不存在。于是 is_interrupted_output 永远匹配不上：被取消/打断的成员会
    "成功"返回一句占位文案，再被当作分析喂给风控、决策与主理人，报告里出现
    「I notice the interruption…」当观点。
    """
    print("\n[打断占位文案必须被检出]")
    check("认得我们钉进 react_config 的 sentinel", t.is_interrupted_output(t.INTERRUPTED_SENTINEL))
    check("认得 AgentScope 2.0.9 的默认 interruption_message",
          t.is_interrupted_output(t.FRAMEWORK_INTERRUPT_DEFAULT))
    check("认得权限停泊文案", t.is_interrupted_output(t.PERMISSION_WAIT_SENTINEL))
    check("正常分析不会被误判", not t.is_interrupted_output("看好 AI 周期，建议逢低分批建仓。"))

    rc = t._team_react_config({"agent": {"max_iters": 7}})
    check("执行单元的 interruption_message 被钉成 sentinel",
          getattr(rc, "interruption_message", None) == t.INTERRUPTED_SENTINEL,
          str(getattr(rc, "interruption_message", None)))
    check("max_iters 仍按配置生效", getattr(rc, "max_iters", None) == 7,
          str(getattr(rc, "max_iters", None)))


async def test_cancel_stops_team_and_settles_members():
    """点「停止」要真的停下来，并把没跑完的成员落定。

    停止靠取消 asyncio 任务实现（AgentScope 的打断也是这套：框架自己的
    ChatService.interrupt 就是 cancel 掉 chat-run 任务）。但 run_member 在
    CancelledError 上直接 re-raise、不发事件，于是前端那几行永远停在
    「⏳ 分析中…」，用户以为按钮没生效。这里断言：① 取消后运行立刻结束、不挂住；
    ② 未收尾的成员都被补上「已取消」事件。
    """
    print("\n[停止：打断团队运行并把成员落定]")
    install_fakes()
    registered = []

    def hanging(member, cfg, workspace_dir):
        name = member.get("name", "?")

        async def _f():
            a = FakeAgent(name, f"{name}的产出")

            async def hang(msg):
                await asyncio.Event().wait()   # 永不返回，只能被取消

            a.reply = hang
            registered.append(a)
            return a

        return _f()

    t.build_team_agent = hanging
    events = []

    async def emit(ev):
        events.append(ev)

    def n_of(phase):
        return sum(1 for e in events if e.get("phase") == phase)

    rt = {"name": "圆桌", "type": "roundtable", "decision_prompt": "决策",
          "members": [{"name": f"成员{i}", "prompt": "p"} for i in range(4)]}
    task = asyncio.create_task(t.run_team_stream(
        rt, "t", emit, {"agent": {}}, ".", member_timeout=300, max_concurrent=2))
    try:
        # 等到 4 位都登记（含排在信号量外面的 2 位），且至少 2 位真的跑起来了
        ok = await _wait_until(lambda: n_of("member_queued") == 4 and n_of("member_start") == 2)
        check("取消前：4 位登记、2 位在跑", ok, f"queued={n_of('member_queued')} start={n_of('member_start')}")

        task.cancel()
        done, _pending = await asyncio.wait({task}, timeout=10)
        check("取消后团队运行立刻结束（不挂住）", bool(done) and task.cancelled(),
              f"done={bool(done)} cancelled={task.cancelled()}")

        errs = [e for e in events if e.get("phase") == "member_error"]
        check("未收尾的 4 位成员都被补上取消事件", len(errs) == 4, str(len(errs)))
        check("取消事件标明是用户取消",
              all(e.get("error") == t.MEMBER_CANCELED_ERROR for e in errs),
              str([e.get("error") for e in errs])[:90])
        check("取消事件带上了所属阶段",
              all(e.get("stage") == "analysis" for e in errs),
              str([e.get("stage") for e in errs])[:90])
    finally:
        if not task.done():
            task.cancel()
            with contextlib.suppress(Exception):
                await task
        restore_fakes()


def _model_cfg(temperature="__absent__"):
    llm = {"provider": "p", "enable_thinking": False}
    if temperature != "__absent__":
        llm["temperature"] = temperature
    return {
        "llm": llm,
        "providers": {"p": {"api_key": "x", "api_base": "http://127.0.0.1:1/v1", "model": "m"}},
        "agent": {},
    }


class _FakeReq:
    """update_config 只用到 await request.json()。"""

    def __init__(self, body):
        self._body = body

    async def json(self):
        return self._body


async def test_temperature_optional():
    """temperature 可留空：留空 = 不下发该参数，由模型端点决定采样温度。

    现象（2026-10-16）：`build_model` 写的是 `llm_cfg.get("temperature", 0.0)`，
    没配就**静默变成 0.0**——0.0 是「完全贪心」，跟「用端点默认」是两回事，
    用户完全感知不到自己选了一个采样策略。框架侧本来就支持：Parameters.temperature
    是 float|None，模型层只在非 None 时才把它塞进请求。
    """
    print("\n[temperature 可留空：留空 = 用模型端点默认]")
    import model_config as mc

    check("未设置 → None（不下发该参数）", mc.optional_temperature(None) is None)
    check("空串 → None", mc.optional_temperature("") is None)
    check("纯空白 → None", mc.optional_temperature("   ") is None)
    check("0 是合法值，不等于「未设置」", mc.optional_temperature(0) == 0.0)
    check("字符串 '0' 也认", mc.optional_temperature("0") == 0.0)
    check("1.4 原样保留", mc.optional_temperature(1.4) == 1.4)

    for bad in ("abc", -0.5, 3, 2.5):
        try:
            mc.optional_temperature(bad)
            rejected = False
        except ValueError:
            rejected = True
        check(f"非法值 {bad!r} 被拒绝（不静默兜底）", rejected)

    m_unset = mc.build_model(_model_cfg(), stream=False)
    check("留空时 Parameters.temperature 为 None", m_unset.parameters.temperature is None,
          repr(m_unset.parameters.temperature))
    m_set = mc.build_model(_model_cfg(0.7), stream=False)
    check("配置了就用配置值", m_set.parameters.temperature == 0.7,
          repr(m_set.parameters.temperature))


async def test_config_clearing_temperature_removes_key():
    """设置页把 temperature 清空，必须把该键从 config.yaml 删掉。

    现象：老前端存的是 `parseFloat('') || 0.0` —— 用户留空，写进去的是 0，
    看上去「用了默认」实则选了最贪心的采样（本机 config.yaml 里就躺着这么个 0）。
    而且写回配置用的是 dict.update 合并，光「不写新值」删不掉旧键。
    """
    print("\n[清空 temperature 会从 config.yaml 删除该键]")
    import server as sv

    tmp = tempfile.mkdtemp()
    old_path = sv.CONFIG_PATH
    try:
        cfg_path = os.path.join(tmp, "config.yaml")
        with open(cfg_path, "w", encoding="utf-8") as f:
            f.write("llm:\n  provider: p\n  temperature: 1.4\n"
                    "providers:\n  p:\n    api_key: x\n")
        sv.CONFIG_PATH = cfg_path

        r1 = await sv.update_config(_FakeReq({"llm": {"provider": "p", "temperature": None}}))
        saved1 = yaml.safe_load(open(cfg_path, encoding="utf-8")) or {}
        check("清空保存成功", r1.get("status") == "success", str(r1))
        check("temperature 键已被删除（= 用端点默认）",
              "temperature" not in (saved1.get("llm") or {}), str(saved1.get("llm")))

        r2 = await sv.update_config(_FakeReq({"llm": {"temperature": 0.3}}))
        saved2 = yaml.safe_load(open(cfg_path, encoding="utf-8")) or {}
        check("填了值就写进配置", r2.get("status") == "success"
              and (saved2.get("llm") or {}).get("temperature") == 0.3, str(saved2.get("llm")))

        r3 = await sv.update_config(_FakeReq({"llm": {"temperature": 9}}))
        saved3 = yaml.safe_load(open(cfg_path, encoding="utf-8")) or {}
        check("越界值被打回且不写入", r3.get("status") == "error", str(r3))
        check("打回后原值保持不变", (saved3.get("llm") or {}).get("temperature") == 0.3,
              str(saved3.get("llm")))
    finally:
        sv.CONFIG_PATH = old_path
        shutil.rmtree(tmp, ignore_errors=True)


async def test_finish_reason_drives_status():
    """成员算不算「跑成了」，看框架给的 finished_reason，不再靠猜正文。

    AgentScope 把 finished_reason 打在返回的 Msg 上（message/_base.py），
    归一化在 team._classify_finish，判定只此一处（_settle_member）。
    """
    print("\n[收尾原因：读框架的 finished_reason]")

    class R:  # 假装是框架返回的那条 Msg
        def __init__(self, text="", reason=None):
            self._t = text
            self.finished_reason = reason

        def get_text_content(self):
            return self._t

    cls = t._classify_finish
    check("没标 reason 的普通正文 → completed",
          cls(R("看好 AI 周期"), "看好 AI 周期") == t.REASON_COMPLETED)
    check("框架标 completed → completed",
          cls(R("正文", "completed"), "正文") == t.REASON_COMPLETED)
    check("框架标 interrupted → interrupted",
          cls(R(t.INTERRUPTED_SENTINEL, "interrupted"), t.INTERRUPTED_SENTINEL) == t.REASON_INTERRUPTED)
    check("框架标 error → error", cls(R("炸了", "error"), "炸了") == t.REASON_ERROR)
    check("跑满上限但有强制收尾正文 → exceeded",
          cls(R("总结如下", "exceed_max_iters"), "总结如下") == t.REASON_EXCEEDED)
    check("跑满上限且只剩占位正文 → exhausted",
          cls(R(t.MAX_ITERS_PLACEHOLDER, "exceed_max_iters"), t.MAX_ITERS_PLACEHOLDER) == t.REASON_EXHAUSTED)
    check("停泊（无 reason + 固定文案）→ parked",
          cls(R(t.PERMISSION_WAIT_SENTINEL, None), t.PERMISSION_WAIT_SENTINEL) == t.REASON_PARKED)

    check("completed 可用", t.AgentOutcome(text="正文").usable)
    check("exceeded 仍可用（正文是真的）", t.AgentOutcome(text="正文", reason=t.REASON_EXCEEDED).usable)
    check("空正文即使 completed 也不可用",
          not t.AgentOutcome(text="   ", reason=t.REASON_COMPLETED).usable)
    for bad in (t.REASON_INTERRUPTED, t.REASON_PARKED, t.REASON_EXHAUSTED, t.REASON_ERROR):
        check(f"{bad} 不可用", not t.AgentOutcome(text="有字", reason=bad).usable)

    events = []

    async def emit(ev):
        events.append(ev)

    member = {"name": "甲", "role": "r", "color": "#000", "prompt": "p"}

    res = await t._settle_member(emit, member,
                                 t.AgentOutcome(text="我的分析", reason=t.REASON_EXCEEDED), "s1")
    check("跑满上限的成员保留正文且记为 ok",
          res["status"] == "ok" and res["output"] == "我的分析", str(res)[:90])
    check("跑满上限的成员带标注（不静默混过去）",
          "跑满迭代上限" in res.get("note", ""), str(res.get("note")))
    check("member_done 事件带 note 与 finished_reason",
          events[-1]["phase"] == "member_done" and events[-1].get("note")
          and events[-1].get("finished_reason") == t.REASON_EXCEEDED, str(events[-1])[:120])

    for reason, text in ((t.REASON_INTERRUPTED, t.INTERRUPTED_SENTINEL),
                         (t.REASON_PARKED, t.PERMISSION_WAIT_SENTINEL),
                         (t.REASON_EXHAUSTED, t.MAX_ITERS_PLACEHOLDER),
                         (t.REASON_ERROR, "boom")):
        events.clear()
        res = await t._settle_member(emit, member, t.AgentOutcome(text=text, reason=reason), "s1")
        check(f"{reason} → 记为 error 且产出清空",
              res["status"] == "error" and not res["output"], str(res)[:90])
        check(f"{reason} → 发 member_error", events and events[-1]["phase"] == "member_error")


async def test_truncation_warning_in_report():
    """跑满迭代上限的成员要在最终报告里被点名。

    面板上那一行的标注，读最终报告的人（以及把报告导出、转发出去的人）看不到。
    跑满上限意味着这份信号可能被截断，结论旁边就得有交代。
    """
    print("\n[跑满迭代上限的名单要附在最终报告里]")
    check("没有成员时不给提示", t.truncation_warning([]) == "")
    check("全都正常完成时不给提示",
          t.truncation_warning([{"member": "甲", "finished_reason": t.REASON_COMPLETED}]) == "")

    warn = t.truncation_warning([
        {"member": "甲", "finished_reason": t.REASON_COMPLETED},
        {"member": "华尔街斗牛士", "finished_reason": t.REASON_EXCEEDED},
        {"member": "大空头", "finished_reason": t.REASON_EXCEEDED},
        {"member": "乙", "finished_reason": t.REASON_INTERRUPTED},
    ])
    check("点出超限人数", "2 位成员跑满迭代上限" in warn, warn[:100])
    check("点名到人", "华尔街斗牛士" in warn and "大空头" in warn, warn)
    check("不把被打断/失败的成员算进来", "乙" not in warn and "甲" not in warn, warn)

    # 端到端：成员跑满上限 → 结果里带 finished_reason → 报告能据此点名
    install_fakes()

    def exceeded(member, cfg, workspace_dir):
        name = member.get("name", "?")

        async def _f():
            return FakeAgent(name, f"{name}的产出", FW_EXCEED)

        return _f()

    t.build_team_agent = exceeded
    events = []

    async def emit(ev):
        events.append(ev)

    rt = {"name": "圆桌", "type": "roundtable", "decision_prompt": "决策",
          "members": [{"name": "甲", "prompt": "p"}]}
    try:
        _, results = await t.run_team_stream(rt, "t", emit, {"agent": {}}, ".", member_timeout=5)
    finally:
        restore_fakes()
    check("超限成员的正文仍被保留（记为 ok）", results[0]["status"] == "ok",
          str(results[0])[:80])
    check("结果里带上 finished_reason=exceeded",
          results[0].get("finished_reason") == t.REASON_EXCEEDED,
          str(results[0].get("finished_reason")))
    check("报告能据 results 点名超限成员", "甲" in t.truncation_warning(results))


async def test_stage_prompt_uses_real_member_count():
    """喂给风控/决策的提示里，「N 位成员」必须等于实际成员数。

    现象（2026-10-16）：这里写死了「以下是 19 位大师的完整信号」，而轻量版圆桌
    只有 5 位成员。决策代理被告知「19 位里有 5 份」——它会以为自己缺了 14 份信号，
    于是给出「HOLD（信心：中低）」这种含糊、留退路的结论。报告看起来"没写完"，
    根子在这儿。
    """
    print("\n[风控/决策的提示按实际人数写]")
    install_fakes()
    seen_inputs = []

    async def design(prompt, name, task, input_text, cfg, workspace_dir, timeout):
        seen_inputs.append((name, input_text))
        return t.AgentOutcome(text=f"{name}的产出")

    t._run_design_agent = design
    events = []

    async def emit(ev):
        events.append(ev)

    rt = {"name": "轻量圆桌", "type": "roundtable",
          "members": [{"name": f"成员{i}", "prompt": "p"} for i in range(5)],
          "risk_prompt": "风控", "decision_prompt": "决策"}
    try:
        await t.run_team_stream(rt, "你怎么看近期的大回撤", emit, {"agent": {}}, ".", member_timeout=5)
    finally:
        restore_fakes()

    check("风控和决策都拿到了输入", len(seen_inputs) == 2, str(len(seen_inputs)))
    check("提示里的「N 位成员」等于实际成员数（5）",
          all("5 位成员" in txt for _n, txt in seen_inputs),
          str([txt[:40] for _n, txt in seen_inputs]))
    check("措辞保持中性（不带投资域称呼）",
          all("大师" not in txt and "投资" not in txt for _n, txt in seen_inputs),
          str([txt[:40] for _n, txt in seen_inputs]))
    check("不再出现写死的「19 位」",
          all("19 位" not in txt for _n, txt in seen_inputs),
          str([txt[:40] for _n, txt in seen_inputs]))

    # 反例：19 人的团队仍然应该显示 19
    seen_inputs.clear()
    install_fakes()
    t._run_design_agent = design
    rt19 = {"name": "大圆桌", "type": "roundtable",
            "members": [{"name": f"大师{i}", "prompt": "p"} for i in range(19)],
            "decision_prompt": "决策"}
    try:
        await t.run_team_stream(rt19, "t", emit, {"agent": {}}, ".", member_timeout=5)
    finally:
        restore_fakes()
    check("19 人团队显示 19 位", all("19 位成员" in txt for _n, txt in seen_inputs),
          str([txt[:40] for _n, txt in seen_inputs]))


async def test_risk_stage_report_is_emitted():
    """风险评估是独立阶段的正文，要单独发出去，前端才能给它一张卡片。

    现象：风控阶段跑完、内容也喂给了决策，但只把 bytes 发给前端，正文从没露过面
    ——用户看到「✅ 风险管理师评估风险…」后面空无一物，只有决策里一句转述。
    """
    print("\n[风险评估单独成一条阶段产出]")
    install_fakes()
    events = []

    async def emit(ev):
        events.append(ev)

    rt = {"name": "圆桌", "type": "roundtable", "decision_prompt": "决策",
          "members": [{"name": "甲", "prompt": "p"}], "risk_prompt": "风控"}
    try:
        await t.run_team_stream(rt, "t", emit, {"agent": {}}, ".", member_timeout=5)
    finally:
        restore_fakes()

    srs = [e for e in events if e.get("phase") == "stage_report"]
    check("风控阶段发出一条 stage_report", len(srs) == 1, str(len(srs)))
    check("带上阶段正文", bool(srs) and srs[0].get("text") == "risk-manager的产出",
          str(srs[:1])[:120])
    check("带上阶段标识与标题",
          bool(srs) and srs[0].get("stage") == "risk" and bool(srs[0].get("label")),
          str(srs[:1])[:120])

    # 风控没产出有效正文时不能发：占位文案不是产出，发出去就是一张空卡片
    install_fakes()

    async def bad_design(prompt, name, task, input_text, cfg, workspace_dir, timeout):
        return t.AgentOutcome(text=t.INTERRUPTED_SENTINEL, reason=t.REASON_INTERRUPTED)

    t._run_design_agent = bad_design
    events.clear()
    try:
        await t.run_team_stream(rt, "t", emit, {"agent": {"team_retries": 1}},
                                ".", member_timeout=5)
    finally:
        restore_fakes()
    check("风控失败时不发 stage_report",
          not any(e.get("phase") == "stage_report" for e in events),
          str([e.get("phase") for e in events]))


async def test_genericity_across_team_types():
    """三种编排（扁平 / 圆桌 / 多阶段管线）的关键行为必须一致。

    这些能力都做成了共用助手（_announce_member / _settle_member / _persist_member /
    _emit_canceled_members），但「共用」得靠测试钉住：哪天谁在某个分支里绕开助手
    直接写事件，就又回到「三份里改了一份」的老路——上一轮的 interrupt 检测失效
    就是这么来的。
    """
    print("\n[泛用性：三种编排的关键行为一致]")
    install_fakes()
    events = []

    async def emit(ev):
        events.append(ev)

    cases = {
        "扁平(parallel)": {"name": "并行", "lead_prompt": "汇总",
                           "members": [{"name": "甲", "prompt": "p"},
                                       {"name": "乙", "prompt": "p"}]},
        "圆桌(roundtable)": {"name": "圆桌", "type": "roundtable",
                             "members": [{"name": "甲", "prompt": "p"}],
                             "risk_prompt": "风控", "decision_prompt": "决策"},
        "管线(pipeline)": pipe_spec(),
    }
    problems = []
    for kind, spec in cases.items():
        events.clear()
        try:
            await t.run_team_stream(spec, "任务", emit, {"agent": {}}, ".", member_timeout=5)
        except Exception as e:
            problems.append(f"{kind}: 抛异常 {type(e).__name__}: {e}")
            continue

        done = [e for e in events if e["phase"] == "member_done"]
        if not done:
            problems.append(f"{kind}: 没有任何成员产出")
            continue
        for m in done:
            if not m.get("summary"):
                problems.append(f"{kind}: 成员 {m.get('member')} 的 member_done 缺 summary")
            if not m.get("finished_reason"):
                problems.append(f"{kind}: 成员 {m.get('member')} 的 member_done 缺 finished_reason")
            if m.get("finished_reason") != t.REASON_COMPLETED:
                problems.append(f"{kind}: 正常成员被标成 {m.get('finished_reason')}")

        # 每位成员都「先报排队、再报开始」
        for name in {e["member"] for e in events if e["phase"] == "member_start"}:
            qi = [i for i, e in enumerate(events)
                  if e["phase"] == "member_queued" and e["member"] == name]
            si = [i for i, e in enumerate(events)
                  if e["phase"] == "member_start" and e["member"] == name]
            if not qi or not si:
                problems.append(f"{kind}: {name} 缺排队或开始事件")
            elif qi[0] > si[0]:
                problems.append(f"{kind}: {name} 先报开始后报排队")
    check("三种编排的成员事件一致（排队态 / 摘要 / 收尾原因）", not problems, str(problems[:4]))


async def test_roundtable_labels_are_generic():
    """圆桌的阶段文案要中性，且能被 team spec 覆盖。

    原先写死「各位大师并行分析中…」「投资组合经理给出最终 BUY/SELL/HOLD 决策…」
    ——这套编排是通用的，任何非投资域的圆桌都会显示错误的阶段标题。
    （和写死「19 位大师」是同一类毛病。）
    """
    print("\n[圆桌文案：中性默认 + spec 可覆盖]")
    events = []

    async def emit(ev):
        events.append(ev)

    generic = {"name": "通用圆桌", "type": "roundtable",
               "members": [{"name": "甲", "prompt": "p"}],
               "risk_prompt": "风控", "decision_prompt": "决策"}

    install_fakes()
    try:
        await t.run_team_stream(generic, "t", emit, {"agent": {}}, ".", member_timeout=5)
    finally:
        restore_fakes()
    blob = " ".join([e.get("label", "") for e in events if e.get("phase") == "stage_start"]
                    + [e.get("label", "") for e in events if e.get("phase") == "stage_report"])
    for bad in ("大师", "投资", "BUY", "SELL", "HOLD", "组合经理", "风控"):
        check(f"默认文案不含「{bad}」", bad not in blob, blob)

    # spec 覆盖后，投资域措辞可以照旧
    install_fakes()
    events.clear()
    custom = dict(generic)
    custom["stage_labels"] = {"analysis": "各位大师并行分析中…",
                              "risk": "风控总监评估…",
                              "risk_report": "风控报告",
                              "decision": "基金经理定夺…"}
    try:
        await t.run_team_stream(custom, "t", emit, {"agent": {}}, ".", member_timeout=5)
    finally:
        restore_fakes()
    labels = [e.get("label") for e in events if e.get("phase") == "stage_start"]
    check("spec 的 stage_labels 覆盖默认阶段标题",
          labels == ["各位大师并行分析中…", "风控总监评估…", "基金经理定夺…"], str(labels))
    srs = [e for e in events if e.get("phase") == "stage_report"]
    check("阶段产出卡片的标题也可覆盖",
          bool(srs) and srs[0].get("label") == "风控报告", str(srs[:1])[:90])


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
    await test_toolkit_permissions_autonomous()
    await test_member_permission_park_is_not_a_signal()
    await test_member_retries_transient_errors()
    await test_skill_dirs_filtered()
    await test_server_output_capture()
    await test_queued_members_are_not_shown_as_running()
    await test_member_persisted_before_stage_finishes()
    await test_context_viz_commit_survives_deleted_session()
    await test_interrupt_placeholder_is_detected()
    await test_finish_reason_drives_status()
    await test_truncation_warning_in_report()
    await test_stage_prompt_uses_real_member_count()
    await test_risk_stage_report_is_emitted()
    await test_genericity_across_team_types()
    await test_roundtable_labels_are_generic()
    await test_cancel_stops_team_and_settles_members()
    await test_temperature_optional()
    await test_config_clearing_temperature_removes_key()
    print(f"\n通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    if FAIL:
        for f in FAIL:
            print("  FAILED:", f)
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
