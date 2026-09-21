"""PrismHarness 团队编排（Teams / 多智能体圆桌）。

镜像 Claude 官方多智能体编排模式：
一个 lead 把任务分发给多个带独立人格/提示词/颜色的 specialist 成员，
成员各自用精简 ReActAgent **并行** 独立运行，收集带标注的结果后，
lead 用独立 ReActAgent 综合产出最终决策报告。每次运行落盘 JSON + Markdown
记录（session_logs/<sid>/teams/run_<ts>/），可随时回滚（git 友好）。

关键纪律：团队成员 / lead 都用「精简 ReActAgent」（同 tools.run_subagent），
**绝不**传 `_prism_harness_sess` —— 它们内部写文件/跑 shell 完全自主，
不触发主会话的 HITL 人工确认（只有主代理自己的工具写操作走确认）。
"""

import asyncio
import json
import os
import re
import time

from model_config import build_model, load_config
from tools import build_toolkit

# Agentscope 把成员任务被取消（CancelledError 断在 tool 执行中）时，会返回这个
# 预设文案（_react_agent.handle_interrupt），且状态仍是 ok —— 成员看似成功、
# 实际没产出分析。冒烟常见（并发成员共享一个远端端点，初始取消/超时看门狗易命中）。
# 检出即判定为"被打断"，走重建重试，避免把垃圾输出当真结果。
INTERRUPTED_SENTINEL = "I noticed that you have interrupted me."


# 默认 lead 系统提示（团队 JSON 里没配 lead_prompt 时兜底）
DEFAULT_LEAD_PROMPT = (
    "你是本团队的牵头人/主席。综合以下各位成员的独立意见，"
    "输出结构化的最终决策报告：\n"
    "1. 总体结论\n2. 支持要点（引用各成员观点）\n3. 风险与分歧\n4. 明确决策/建议"
)

TEAMS_DIR_NAME = "teams"

# 项目根目录：无论从哪启动，内置团队都定位到 team.py 所在目录下的 teams/。
_BASE_DIR = os.path.dirname(os.path.abspath(__file__))


# -------------------- 配置读写 --------------------

def get_teams_dirs(workspace_dir: str = "workspace") -> list:
    """团队搜索目录，按优先级排序：用户工作区（可写） > 仓库内置（随仓库分发）。

    与 skills 的加载顺序一致——workspace 里同名团队覆盖仓库内置版本，
    用户改过的团队不会被升级/重新分发覆盖。
    """
    return [
        os.path.join(workspace_dir, TEAMS_DIR_NAME),
        os.path.join(_BASE_DIR, TEAMS_DIR_NAME),
    ]


def get_teams_dir(workspace_dir: str = "workspace") -> str:
    """用户可写的团队目录（新建/保存都落这里，仓库内置目录只读）。"""
    return get_teams_dirs(workspace_dir)[0]


def find_team_path(slug: str, workspace_dir: str = "workspace"):
    """按优先级找到团队 JSON 的绝对路径；找不到返回 None。"""
    fn = _slugify(slug) + ".json"
    for d in get_teams_dirs(workspace_dir):
        path = os.path.join(d, fn)
        if os.path.isfile(path):
            return path
    return None


def team_members(spec: dict) -> list:
    """取团队的扁平成员列表（供 UI 显示与计数）。

    普通团队用 spec["members"]；pipeline 团队若没写 members，就把各阶段的
    成员按阶段顺序摊平，保证「成员数」在任何团队类型下都有意义。
    """
    if not isinstance(spec, dict):
        return []
    if isinstance(spec.get("members"), list) and spec["members"]:
        return spec["members"]
    flat = []
    for st in spec.get("stages") or []:
        if isinstance(st, dict):
            flat.extend(st.get("members") or [])
    return flat


def _slugify(name: str) -> str:
    """团队名 → 文件 slug。保留非 ASCII（中文名原样可作文件名），
    去掉路径分隔符等不安全字符。空名回退 team。"""
    name = (name or "").strip()
    name = name.replace("\\", "_").replace("/", "_").replace(":", "_")
    name = re.sub(r"[<>\"|?*]", "_", name)
    return name or "team"


def _sanitize_filename(name: str) -> str:
    """成员名 → 安全文件名（去路径分隔符），用作 members/<name>.json。"""
    return re.sub(r'[\\/:*?"<>|]', "_", (name or "").strip()) or "member"


def list_teams(workspace_dir: str = "workspace") -> list:
    """返回 [{slug, name, role_count, mtime, builtin, type}]，workspace 同名优先。"""
    out = []
    seen = set()
    for d in get_teams_dirs(workspace_dir):
        if not os.path.isdir(d):
            continue
        builtin = (d == get_teams_dirs(workspace_dir)[-1])
        for fn in sorted(os.listdir(d)):
            if not fn.endswith(".json"):
                continue
            slug = fn[: -len(".json")]
            if slug in seen:
                continue  # workspace 已经提供同名团队 → 内置版本被覆盖
            seen.add(slug)
            path = os.path.join(d, fn)
            try:
                spec = json.load(open(path, "r", encoding="utf-8"))
            except Exception:
                spec = {}
            out.append({
                "slug": slug,
                "name": spec.get("name", slug),
                "type": spec.get("type") or "parallel",
                "role_count": len(team_members(spec)),
                "stage_count": len(spec.get("stages") or []),
                "builtin": builtin,
                # 移植来源（源专家包目录名 + 版本）：团队文件名是中文显示名，
                # 留这个字段才能反查它来自哪个外部包（如 相信光么 ← believe-in-light）
                "source": spec.get("source") or "",
                "source_version": spec.get("source_version") or "",
                "mtime": os.path.getmtime(path),
            })
    out.sort(key=lambda t: t["name"])
    return out


def load_team(slug: str, workspace_dir: str = "workspace"):
    """按 slug 读取团队 spec（workspace 优先，其次仓库内置）；不存在返回 None。"""
    path = find_team_path(slug, workspace_dir)
    if not path:
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def delete_team(slug: str, workspace_dir: str = "workspace") -> bool:
    """删除团队文件（workspace 优先；若只存在于仓库内置目录，则删内置那份）。

    返回是否真的删掉了文件。
    """
    path = find_team_path(slug, workspace_dir)
    if not path:
        return False
    try:
        os.remove(path)
        return True
    except OSError:
        return False


def _validate_member(m, where: str) -> str:
    if not isinstance(m, dict):
        return f"{where} 必须是对象"
    if not m.get("name"):
        return f"{where} 缺少 name"
    if not m.get("prompt"):
        return f"{where}（{m.get('name')}）缺少 prompt"
    return ""


def validate_team(spec: dict) -> str:
    """校验团队 spec；返回出错信息字符串，合法返回空串。

    支持三种形态：
    - 普通团队：members 为并行成员数组
    - roundtable：members + risk_prompt/decision_prompt 两阶段圆桌
    - pipeline：stages 为有序阶段数组，每个阶段内 members 并行
    """
    if not isinstance(spec, dict):
        return "团队必须是 JSON 对象"
    if not spec.get("name"):
        return "缺少 name（团队名）"

    if spec.get("type") == "pipeline":
        stages = spec.get("stages")
        if not isinstance(stages, list) or not stages:
            return "pipeline 团队需要非空 stages 数组"
        for i, st in enumerate(stages):
            if not isinstance(st, dict):
                return f"阶段[{i}] 必须是对象"
            label = st.get("label") or st.get("id") or f"#{i}"
            ms = st.get("members")
            if not isinstance(ms, list) or not ms:
                return f"阶段[{i}]（{label}）缺少非空 members"
            for j, m in enumerate(ms):
                err = _validate_member(m, f"阶段[{i}]成员[{j}]")
                if err:
                    return err
        return ""

    members = spec.get("members")
    if not isinstance(members, list) or not members:
        return "members 必须是非空数组"
    for i, m in enumerate(members):
        err = _validate_member(m, f"成员[{i}]")
        if err:
            return err
    return ""


def save_team(spec: dict, workspace_dir: str = "workspace") -> dict:
    """校验并原子写入 team（临时文件 + rename），返回 {status, slug?, message?}。"""
    err = validate_team(spec)
    if err:
        return {"status": "error", "message": err}
    d = get_teams_dir(workspace_dir)
    os.makedirs(d, exist_ok=True)
    # 与 list/load 一致：slug 始终按 name 推导，保证 GET/POST 路由键稳定
    slug = _slugify(spec["name"])
    path = os.path.join(d, slug + ".json")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(spec, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
    return {"status": "success", "slug": slug}


# -------------------- 代理构建 --------------------

async def build_team_agent(member: dict, cfg: dict, workspace_dir: str):
    """构造一个成员的「精简 ReActAgent」（自主、无 HITL）。

    镜像 tools.run_subagent 的构造方式：build_model(stream=False) + build_toolkit
    + OpenAIChatFormatter + InMemoryMemory + 关闭控制台输出。
    """
    from agentscope.agent import ReActAgent
    from agentscope.formatter import OpenAIChatFormatter
    from agentscope.memory import InMemoryMemory

    model_cfg = cfg
    # 可选成员级模型覆盖
    if member.get("provider") or member.get("model"):
        txt = member.get("model") or member.get("provider")
        # 支持 <provider> 或 <provider>@<model> 形式；简单起见：
        # 若给出 provider 且在 cfg.providers 中存在，则临时切过去
        prov = member.get("provider") or ""
        if prov and prov in (model_cfg.get("providers") or {}):
            tmp_cfg = json.loads(json.dumps(cfg))
            tmp_cfg["llm"] = dict(cfg["llm"])
            tmp_cfg["llm"]["provider"] = prov
            model_cfg = tmp_cfg
        # 注意：完整 model 覆盖较复杂（可能不在 providers 里），v1 不做完整覆盖，
        # 仅支持从已有 provider 切换。未知 provider 优雅回退到会话模型。
    model = build_model(model_cfg, stream=False)

    role = member.get("role") or ""
    prompt = member.get("prompt") or ""
    header = (
        f"你是 {member.get('name', '团队成员')}。"
        + (f"你的角色：{role}。" if role else "")
    )
    sys_prompt = f"{header}\n\n{prompt}\n\n请独立完成分派给你的任务，用简洁分点的中文输出你的分析结论。不要反问、不要征询确认。"
    member_agent = ReActAgent(
        name=_sanitize_filename(member.get("name", "member")),
        sys_prompt=sys_prompt,
        model=model,
        formatter=OpenAIChatFormatter(),
        toolkit=await build_toolkit(workspace_dir, interactive=False),
        memory=InMemoryMemory(),
        max_iters=int(cfg.get("agent", {}).get("max_iters", 30)),
        parallel_tool_calls=bool(cfg.get("agent", {}).get("parallel_tool_calls", False)),
    )
    member_agent.set_console_output_enabled(False)
    return member_agent


async def build_lead_agent(team: dict, cfg: dict, workspace_dir: str):
    """构造 lead 的「精简 ReActAgent」（无 HITL）。"""
    from agentscope.agent import ReActAgent
    from agentscope.formatter import OpenAIChatFormatter
    from agentscope.memory import InMemoryMemory

    lead_prompt = team.get("lead_prompt") or DEFAULT_LEAD_PROMPT
    model = build_model(cfg, stream=False)
    lead_agent = ReActAgent(
        name="team-lead",
        sys_prompt=lead_prompt,
        model=model,
        formatter=OpenAIChatFormatter(),
        toolkit=await build_toolkit(workspace_dir, interactive=False),
        memory=InMemoryMemory(),
        max_iters=int(cfg.get("agent", {}).get("max_iters", 30)),
        parallel_tool_calls=bool(cfg.get("agent", {}).get("parallel_tool_calls", False)),
    )
    lead_agent.set_console_output_enabled(False)
    return lead_agent


# -------------------- 运行编排 --------------------

async def _run_agent_once(agent, text: str, timeout: float = 300.0) -> str:
    """跑一次精简代理，返回文本输出。"""
    from agentscope.message import Msg

    reply = await asyncio.wait_for(
        agent(Msg("user", text, "user")),
        timeout=float(timeout),
    )
    txt = reply.get_text_content() if hasattr(reply, "get_text_content") else str(reply)
    return txt or "(无文本输出)"


def is_interrupted_output(out: str) -> bool:
    """判断成员的输出是否为 Agentscope 的"被打断"预设文案（真中断而非真实分析）。"""
    return isinstance(out, str) and INTERRUPTED_SENTINEL in out


async def _run_agent_with_retry(build_agent, task, timeout, retries: int = 2):
    """带重试地跑一个代理（成员 / 主理人 / 风控 / 决策都用它）。

    build_agent: 无参可调，返回一个「全新」代理。被打断（interrupted）时代理的
    memory 里可能残留假的 tool 结果，因此重试必须重建新代理。

    Returns:
        (text, attempts, interrupted) —— interrupted 为 True 表示重试用尽仍是
        那句「被打断」预设文案，调用方**必须**据此降级，绝不能把它当交付物。
    """
    text = ""
    attempts = 0
    for attempt in range(max(1, retries)):
        attempts = attempt + 1
        agent = await build_agent()
        text = await _run_agent_once(agent, task, timeout=timeout)
        if not is_interrupted_output(text):
            return text, attempts, False
    return text, attempts, True


async def _run_member_with_retry(build_agent, task, timeout, retries: int = 2):
    """成员版：返回 (text, attempts)。"""
    text, attempts, _interrupted = await _run_agent_with_retry(
        build_agent, task, timeout, retries)
    return text, attempts


def interrupted_fallback(results: list, what: str) -> str:
    """汇总/裁决类代理被打断时的降级产出。

    与其把「I noticed that you have interrupted me.」交出去，不如把上游已有内容
    原样交代给用户，并说明哪一步没跑成。
    """
    blocks = []
    for r in results:
        if r.get("status") != "ok":
            continue
        head = f"— {r.get('member', '?')}"
        if r.get("role"):
            head += f"（{r['role']}）"
        blocks.append(f"{head} —\n{r.get('output', '')}")
    body = "\n\n".join(blocks)
    note = (f"⚠️ {what}多次被中断，未能产出最终汇总。"
            "以下直接列出上游各成员的产出，供你参考（可重新运行以获得完整汇总）。")
    return f"{note}\n\n{body}" if body else note


def _clip(text: str, limit: int = 6000) -> str:
    """截断超长文本（防止上游产出把下游/lead 的上下文撑爆）。

    只截断极端长的产出（默认 6000 字），正常分析结论远短于此，不受影响。
    """
    text = text or ""
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n…（原文 {len(text)} 字，此处截断）"


def compose_lead_prompt(team: dict, task: str, results: list) -> str:
    """把各成员结果按 name/role 标注拼接给 lead。

    截断额度跟随团队的 stage_output_clip（长文档类团队会调大，避免正文被切掉）。
    收尾指令可用 team["lead_final_instruction"] 覆盖（文档/圆桌类团队不想要「决策报告」口吻）。
    """
    clip = int(team.get("stage_output_clip") or _STAGE_OUTPUT_CLIP)
    lines = [f"待决策任务：{task}", "", "以下是各位成员独立分析的结果：", ""]
    for r in results:
        name = r.get("member", "?")
        role = r.get("role", "")
        lines.append(f"—— {name}{'（' + role + '）' if role else ''} ——")
        if r.get("status") == "error":
            lines.append(f"[此成员执行失败] {r.get('error', '')}")
        else:
            lines.append(_clip(r.get("output", ""), clip))
        lines.append("")
    lines.append(team.get("lead_final_instruction") or "请给出最终决策报告。")
    return "\n".join(lines)


async def run_team_stream(
    team: dict,
    task: str,
    emit,
    cfg: dict,
    workspace_dir: str,
    run_dir: str = None,
    member_timeout: float = 300.0,
    max_concurrent: int = 4,
):
    """编排一次团队运行。

    Args:
        team: 团队 spec（dict，含 members / lead_prompt）。
        task: 分派给所有成员的任务文本。
        emit: async (event: dict) -> None，把 {"type":"team", **event} 送进 SSE。
        cfg: 完整 config（llm/provider/agent）。
        run_dir: 落盘目录（session_logs/<sid>/teams/run_<ts>），None 则不落盘。
        member_timeout: 每个成员的独立超时（秒）。

    Returns:
        (report_text, results, lead_report)
    """
    team_name = team.get("name", "团队")
    members = team.get("members") or []
    team_type = team.get("type")

    # 多阶段管线（任意阶段数）：自动分发
    if team_type == "pipeline":
        return await run_pipeline_stream(
            team, task, emit, cfg, workspace_dir,
            run_dir=run_dir, member_timeout=member_timeout,
            max_concurrent=max_concurrent,
        )

    # 两阶段圆桌（投资大师版）：自动分发
    if team_type == "roundtable":
        decision, results, extra = await run_roundtable_stream(
            team, task, emit, cfg, workspace_dir,
            run_dir=run_dir, member_timeout=member_timeout,
            max_concurrent=max_concurrent,
        )
        return decision, results

    async def _emit(ev):
        if emit:
            try:
                await emit(ev)
            except Exception:
                pass

    await _emit({"phase": "start", "team": team_name, "task": task})

    async def run_member(m):
        mid = m.get("name", "?")
        await _emit({
            "phase": "member_start",
            "member": mid,
            "role": m.get("role", ""),
            "color": m.get("color", "#888888"),
        })
        try:
            retries = int(cfg.get("agent", {}).get("team_retries", 2))
            out, attempts = await _run_member_with_retry(
                lambda m=m: build_team_agent(m, cfg, workspace_dir),
                task, member_timeout, retries=retries,
            )
            if attempts > 1:
                await _emit({"phase": "member_retrying", "member": mid, "attempts": attempts})
            await _emit({"phase": "member_done", "member": mid, "bytes": len(out)})
            return {
                "member": mid,
                "role": m.get("role", ""),
                "color": m.get("color", "#888888"),
                "prompt": m.get("prompt", ""),
                "output": out,
                "status": "ok",
            }
        except asyncio.CancelledError:
            raise
        except Exception as e:
            await _emit({"phase": "member_error", "member": mid, "error": str(e)})
            return {
                "member": mid,
                "role": m.get("role", ""),
                "color": m.get("color", "#888888"),
                "prompt": m.get("prompt", ""),
                "output": "",
                "error": str(e),
                "status": "error",
            }

    results = await asyncio.gather(*[run_member(m) for m in members])

    # lead 汇总（即使有成员失败也继续，用成功的成员结果）
    await _emit({"phase": "synthesis_start"})
    lead_report = ""
    try:
        text, _attempts, interrupted = await _run_agent_with_retry(
            lambda: build_lead_agent(team, cfg, workspace_dir),
            compose_lead_prompt(team, task, results),
            member_timeout,
            retries=int(cfg.get("agent", {}).get("team_retries", 2)),
        )
        if interrupted:
            # 主理人被打断时绝不能把「被打断」文案当报告交出去
            lead_report = interrupted_fallback(results, "主理人汇总")
            await _emit({"phase": "member_error", "member": "主理人", "error": "汇总被中断"})
        else:
            lead_report = text
        await _emit({"phase": "synthesis_done", "bytes": len(lead_report)})
    except asyncio.CancelledError:
        raise
    except Exception as e:
        lead_report = f"（lead 汇总失败：{e}）"
        await _emit({"phase": "synthesis_done", "bytes": len(lead_report)})
    await _emit({"phase": "done", "team": team_name})

    if run_dir:
        try:
            persist_team_run(team, task, results, lead_report, run_dir)
        except Exception:
            pass

    return lead_report, results


# -------------------- 两阶段圆桌编排（投资大师） --------------------

def _safe_json(val, default=None):
    try:
        if isinstance(val, str):
            import json as _j
            return _j.loads(val)
    except Exception:
        pass
    return val


def _stage_prompt(task, results, header_md):
    """把一批成员结果拼成一段候选文本，喂给下游 stage（风控/决策）。"""
    lines = [header_md, "", f"目标/任务：{task}", ""]
    ok = [r for r in results if r.get("status") == "ok"]
    errs = [r for r in results if r.get("status") != "ok"]
    for r in ok:
        lines.append(f"— {r.get('member','?')}{'（'+r.get('role','')+'）' if r.get('role') else ''} —")
        lines.append(r.get("output", ""))
        lines.append("")
    if errs:
        lines.append("[以下成员未产出有效信号] " + "、".join(e.get("member", "?") for e in errs))
    lines.append("请基于以上全部信号，给出你的专业输出。")
    return "\n".join(lines)


async def _design_agent_for(prompt: str, name: str, cfg: dict, workspace_dir: str):
    """构造一个"角色专用"精简代理，prompt 即其角色+任务指令。"""
    from agentscope.agent import ReActAgent
    from agentscope.formatter import OpenAIChatFormatter
    from agentscope.memory import InMemoryMemory

    model = build_model(cfg, stream=False)
    a = ReActAgent(
        name=name,
        sys_prompt=(prompt or "").strip(),
        model=model,
        formatter=OpenAIChatFormatter(),
        toolkit=await build_toolkit(workspace_dir, interactive=False),
        memory=InMemoryMemory(),
        max_iters=int(cfg.get("agent", {}).get("max_iters", 30)),
        parallel_tool_calls=bool(cfg.get("agent", {}).get("parallel_tool_calls", False)),
    )
    a.set_console_output_enabled(False)
    return a


async def _run_design_agent_checked(prompt, name, task, input_text, cfg, workspace_dir,
                                     timeout, retries: int = 2):
    """风控/决策这类角色代理的带重试版本。

    _run_design_agent 每次调用都会重建代理，所以循环调用天然就是「全新代理重试」。
    Returns: (text, interrupted) —— interrupted 时调用方必须降级，不能当结论用。
    """
    text = ""
    for _ in range(max(1, retries)):
        text = await _run_design_agent(prompt, name, task, input_text, cfg, workspace_dir, timeout)
        if not is_interrupted_output(text):
            return text, False
    return text, True


async def _run_design_agent(prompt: str, name: str, task: str, input_text: str, cfg: dict, workspace_dir: str, timeout: float):
    """跑一个角色代理，输入是拼好的候选文本 + 原任务。"""
    from agentscope.message import Msg

    agent = await _design_agent_for(prompt, name, cfg, workspace_dir)
    user_msg = f"任务：{task}\n\n{input_text}"
    reply = await asyncio.wait_for(agent(Msg("user", user_msg, "user")), timeout=float(timeout))
    return reply.get_text_content() if hasattr(reply, "get_text_content") else str(reply)


async def run_roundtable_stream(
    team: dict,
    task: str,
    emit,
    cfg: dict,
    workspace_dir: str,
    run_dir: str = None,
    member_timeout: float = 600.0,
    max_concurrent: int = 4,
):
    """两阶段圆桌编排（投资大师版）：

    Stage 1 – 并行分析：members[]（19 位大师/分析师）各自独立产出信号。
    Stage 2 – 风控：team['risk_prompt']（risk-manager）综合全部信号评估风险。
    Stage 3 – 决策：team['decision_prompt']（portfolio-manager）基于风控产出 BUY/SELL/HOLD。

    事件流额外发 stage_* 事件，供前端"圆桌决策面板"渲染。
    """
    team_name = team.get("name", "团队")
    members = team.get("members") or []

    async def _emit(ev):
        if emit:
            try:
                await emit(ev)
            except Exception:
                pass

    # 阶段总数：分析 + [风控] + 决策。带上 index/total，前端「阶段 x/y」才准。
    _n_stages = 3 if (team.get("risk_prompt") or "") else 2
    await _emit({"phase": "start", "team": team_name, "task": task, "stage": "analysis"})

    # ---- Stage 1：19 位并行分析（受限并发，避免撑爆模型致全体超时）----
    sem = asyncio.Semaphore(max(int(max_concurrent or 4), 1))

    async def run_member(m):
        mid = m.get("name", "?")
        await _emit({
            "phase": "member_start", "member": mid,
            "role": m.get("role", ""), "color": m.get("color", "#888888"),
            "stage": "analysis",
        })
        async with sem:
            try:
                # 打断重试（interrupted 会被 Agentscope 记为 status ok 的预设文案）：
                # 全新 build_team_agent 重建，避免残留假 tool 结果。
                retries = int(cfg.get("agent", {}).get("team_retries", 2))
                out, attempts = await _run_member_with_retry(
                    lambda m=m: build_team_agent(m, cfg, workspace_dir),
                    task, member_timeout, retries=retries,
                )
                if attempts > 1:
                    await _emit({"phase": "member_retrying", "member": mid, "attempts": attempts, "stage": "analysis"})
                await _emit({
                    "phase": "member_done", "member": mid, "bytes": len(out), "stage": "analysis",
                    # 摘要供前端"成员中间产物"展示（截断，避免灌爆小组件/主对话）
                    "summary": (out[:900] + ("…" if len(out) > 900 else "")) if out else "",
                })
                return {"member": mid, "role": m.get("role", ""), "color": m.get("color", "#888888"),
                        "prompt": m.get("prompt", ""), "output": out, "status": "ok"}
            except asyncio.CancelledError:
                raise
            except Exception as e:
                await _emit({"phase": "member_error", "member": mid, "error": str(e), "stage": "analysis"})
                return {"member": mid, "role": m.get("role", ""), "color": m.get("color", "#888888"),
                        "prompt": m.get("prompt", ""), "output": "", "error": str(e), "status": "error"}

    await _emit({"phase": "stage_start", "stage": "analysis", "label": "各位大师并行分析中…",
                 "index": 0, "total": _n_stages})
    results = await asyncio.gather(*[run_member(m) for m in members])
    await _emit({"phase": "stage_done", "stage": "analysis", "count": len(results)})

    # 增量落盘：Stage 1（成员分析）一完成就先把结果写盘，避免后续 stage 超时/中断
    # 时这 19 份宝贵分析全部丢失。末尾的 persist_roundtable_run 会补全 risk/decision。
    if run_dir:
        try:
            os.makedirs(os.path.join(run_dir, "members"), exist_ok=True)
            with open(os.path.join(run_dir, "config.json"), "w", encoding="utf-8") as f:
                json.dump({"team": team, "task": task, "type": "roundtable", "partial": True},
                          f, ensure_ascii=False, indent=2)
            for r in results:
                fn = os.path.join(run_dir, "members", _sanitize_filename(r["member"]) + ".json")
                with open(fn, "w", encoding="utf-8") as f:
                    json.dump(r, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    # ---- Stage 2：风控 risk-manager ----
    risk_report = ""
    risk_prompt = team.get("risk_prompt") or ""
    if risk_prompt:
        await _emit({"phase": "stage_start", "stage": "risk", "label": "风险管理师评估风险…",
                     "index": 1, "total": _n_stages})
        try:
            risk_report, interrupted = await _run_design_agent_checked(
                risk_prompt, "risk-manager", task,
                _stage_prompt(task, results, "以下是 19 位大师/分析师的独立信号汇总："),
                cfg, workspace_dir, member_timeout,
                retries=int(cfg.get("agent", {}).get("team_retries", 2)),
            )
            if interrupted:
                risk_report = "（风险管理师评估多次被中断，本次没有产出风险评估。）"
                await _emit({"phase": "member_error", "member": "risk-manager",
                             "error": "评估被中断", "stage": "risk"})
            await _emit({"phase": "stage_done", "stage": "risk", "bytes": len(risk_report)})
        except asyncio.CancelledError:
            raise
        except Exception as e:
            risk_report = f"（风控评估失败：{e}）"
            await _emit({"phase": "member_error", "member": "risk-manager", "error": str(e), "stage": "risk"})

    # ---- Stage 3：决策 portfolio-manager ----
    decision_report = ""
    decision_prompt = team.get("decision_prompt") or team.get("lead_prompt") or DEFAULT_LEAD_PROMPT
    await _emit({"phase": "stage_start", "stage": "decision", "label": "投资组合经理给出最终 BUY/SELL/HOLD 决策…",
                 "index": _n_stages - 1, "total": _n_stages})
    try:
        decision_input = _stage_prompt(task, results, "以下是 19 位大师的完整信号：")
        if risk_prompt:
            decision_input += f"\n\n—— 风险管理师评估 ——\n{risk_report}"
        decision_report, interrupted = await _run_design_agent_checked(
            decision_prompt, "portfolio-manager", task, decision_input,
            cfg, workspace_dir, member_timeout,
            retries=int(cfg.get("agent", {}).get("team_retries", 2)),
        )
        if interrupted:
            # 决策被打断时也要给用户可读的降级产出，而不是那句「被打断」文案
            decision_report = interrupted_fallback(results, "投资组合经理决策")
            if risk_report:
                decision_report += f"\n\n—— 风险管理师评估 ——\n{risk_report}"
            await _emit({"phase": "member_error", "member": "portfolio-manager",
                         "error": "决策被中断", "stage": "decision"})
        await _emit({"phase": "stage_done", "stage": "decision", "bytes": len(decision_report)})
    except asyncio.CancelledError:
        raise
    except Exception as e:
        decision_report = f"（决策失败：{e}）"
        await _emit({"phase": "member_error", "member": "portfolio-manager", "error": str(e), "stage": "decision"})

    await _emit({"phase": "done", "team": team_name})

    if run_dir:
        try:
            persist_roundtable_run(team, task, results, risk_report, decision_report, run_dir)
        except Exception:
            pass

    return decision_report, results, {"risk": risk_report, "decision": decision_report}


def persist_roundtable_run(team, task, results, risk_report, decision_report, run_dir):
    """两阶段圆桌运行的落盘记录（回滚友好）。"""
    import os
    os.makedirs(os.path.join(run_dir, "members"), exist_ok=True)
    with open(os.path.join(run_dir, "config.json"), "w", encoding="utf-8") as f:
        json.dump({"team": team, "task": task, "type": "roundtable"}, f, ensure_ascii=False, indent=2)
    for r in results:
        fn = os.path.join(run_dir, "members", _sanitize_filename(r["member"]) + ".json")
        with open(fn, "w", encoding="utf-8") as f:
            json.dump(r, f, ensure_ascii=False, indent=2)
    with open(os.path.join(run_dir, "risk.json"), "w", encoding="utf-8") as f:
        json.dump({"report": risk_report}, f, ensure_ascii=False, indent=2)
    with open(os.path.join(run_dir, "decision.json"), "w", encoding="utf-8") as f:
        json.dump({"report": decision_report}, f, ensure_ascii=False, indent=2)
    with open(os.path.join(run_dir, "SUMMARY.md"), "w", encoding="utf-8") as f:
        f.write(render_roundtable_markdown(team, task, results, risk_report, decision_report))


def render_roundtable_markdown(team, task, results, risk_report, decision_report):
    lines = [
        f"# 圆桌决策记录：{team.get('name', '?')}",
        f"目标任务：{task}",
        "",
        "## 一、19 位大师/分析师信号",
        "",
    ]
    for r in results:
        name = r.get("member", "?")
        role = r.get("role", "")
        lines.append(f"### {name}{'（' + role + '）' if role else ''}")
        lines.append(r.get("output", "") if r.get("status") == "ok" else f"[失败] {r.get('error','')}")
        lines.append("")
    lines.append("## 二、风险评估")
    lines.append(risk_report or "（无）")
    lines.append("")
    lines.append("## 三、最终决策（BUY/SELL/HOLD）")
    lines.append(decision_report or "（无）")
    return "\n".join(lines)


# -------------------- 多阶段管线编排（pipeline） --------------------

# 单个上游产出喂给下游时的截断上限；以及拼给某个阶段的总上下文预算。
# 取值宽松：正常分析结论（几百~几千字）不受影响，只拦「几十位成员 × 长文」
# 这种会直接撑爆模型上下文的极端情况。
_STAGE_OUTPUT_CLIP = 6000
_STAGE_CONTEXT_BUDGET = 48000


def _accumulate_context(prev: list, budget: int = _STAGE_CONTEXT_BUDGET) -> str:
    """把上游各成员产出拼成一段上下文；超预算时优先保留较近的产出。

    prev: [{"member", "role", "output"}]（按时间先后）
    """
    kept = []
    total = 0
    for b in reversed(prev):  # 从最近的往回取
        seg = f"— {b.get('member', '?')}"
        if b.get("role"):
            seg += f"（{b['role']}）"
        seg += f" —\n{b.get('output', '')}"
        if kept and total + len(seg) > budget:
            break
        kept.append(seg)
        total += len(seg)
    kept.reverse()
    dropped = len(prev) - len(kept)
    text = "\n\n".join(kept)
    if dropped > 0:
        text = f"（更早的 {dropped} 项上游产出因篇幅省略）\n\n" + text
    return text


def build_stage_input(task: str, stage: dict, prev: list,
                      budget: int = _STAGE_CONTEXT_BUDGET) -> str:
    """构造某个阶段的输入：阶段任务说明 + 上游产出（可选）。

    stage.task 支持 {task} 占位符（会替换为主任务文本）；未写则默认用主任务。
    budget 为该阶段拼接上游产出时的总字数预算（团队可覆盖）。
    """
    parts = []
    stage_task = (stage.get("task") or "").strip()
    if stage_task:
        parts.append(stage_task.replace("{task}", task or "").strip())
    else:
        parts.append(f"任务：{task}")
    if stage.get("include_prev", True) and prev:
        header = stage.get("prev_header") or "以下是上游各成员的产出，请结合它完成你负责的部分："
        parts.append(header + "\n\n" + _accumulate_context(prev, budget))
    return "\n\n".join(p for p in parts if p)


async def run_pipeline_stream(
    team: dict,
    task: str,
    emit,
    cfg: dict,
    workspace_dir: str,
    run_dir: str = None,
    member_timeout: float = 600.0,
    max_concurrent: int = 4,
):
    """多阶段管线编排：按 stages 顺序执行，阶段内成员并行，阶段间产出累积传递。

    团队 spec（type == "pipeline"）形如：
        {
          "name": "...",
          "type": "pipeline",
          "final": "last" | "lead",          # 默认 last（末阶段产出即报告）
          "lead_prompt": "...",              # final == "lead" 时用它做最终汇总
          "stages": [
            {"id": "s1", "label": "初步调研", "task": "...",
             "members": [{"name","role","color","prompt"}], "include_prev": false},
            ...
          ]
        }

    与 roundtable 的差别：阶段数任意、可串联多个「并行组 → 收敛组」，不是固定三段。
    事件流沿用同一套 stage_*/member_* 命名，前端团队面板无需区分。

    Returns:
        (report_text, results)
    """
    team_name = team.get("name", "团队")
    stages = [s for s in (team.get("stages") or []) if isinstance(s, dict)]
    retries = int(cfg.get("agent", {}).get("team_retries", 2))
    # 长文档类团队可在 spec 里调大：单个上游产出喂给下游的截断上限、以及总上下文预算
    out_clip = int(team.get("stage_output_clip") or _STAGE_OUTPUT_CLIP)
    ctx_budget = int(team.get("stage_context_budget") or _STAGE_CONTEXT_BUDGET)
    sem = asyncio.Semaphore(max(int(max_concurrent or 4), 1))
    results = []
    prev = []
    last_ok = ""

    async def _emit(ev):
        if emit:
            try:
                await emit(ev)
            except Exception:
                pass

    await _emit({"phase": "start", "team": team_name, "task": task, "stage": "pipeline"})

    for si, stage in enumerate(stages):
        sid = str(stage.get("id") or f"stage{si + 1}")
        label = stage.get("label") or sid
        members = [m for m in (stage.get("members") or []) if isinstance(m, dict)]
        if not members:
            continue
        stage_input = build_stage_input(task, stage, prev, ctx_budget)

        await _emit({
            "phase": "stage_start", "stage": sid, "label": label,
            "index": si, "total": len(stages),
        })

        async def run_member(m, sid=sid, label=label, stage_input=stage_input):
            mid = m.get("name", "?")
            await _emit({
                "phase": "member_start", "member": mid, "role": m.get("role", ""),
                "color": m.get("color", "#888888"), "stage": sid,
            })
            async with sem:
                try:
                    out, attempts = await _run_member_with_retry(
                        lambda m=m: build_team_agent(m, cfg, workspace_dir),
                        stage_input, member_timeout, retries=retries,
                    )
                    if attempts > 1:
                        await _emit({"phase": "member_retrying", "member": mid,
                                     "attempts": attempts, "stage": sid})
                    await _emit({
                        "phase": "member_done", "member": mid, "bytes": len(out), "stage": sid,
                        "summary": (out[:900] + ("…" if len(out) > 900 else "")) if out else "",
                    })
                    return {"member": mid, "role": m.get("role", ""),
                            "color": m.get("color", "#888888"), "prompt": m.get("prompt", ""),
                            "output": out, "status": "ok", "stage": sid, "stage_label": label}
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    await _emit({"phase": "member_error", "member": mid,
                                 "error": str(e), "stage": sid})
                    return {"member": mid, "role": m.get("role", ""),
                            "color": m.get("color", "#888888"), "prompt": m.get("prompt", ""),
                            "output": "", "error": str(e), "status": "error",
                            "stage": sid, "stage_label": label}

        outs = list(await asyncio.gather(*[run_member(m) for m in members]))
        results.extend(outs)
        await _emit({"phase": "stage_done", "stage": sid, "count": len(outs)})

        # 把本阶段产出并入累积上下文（失败项以简短占位保留，供下游知情）
        for o in outs:
            prev.append({
                "member": o["member"],
                "role": o["role"],
                "output": (_clip(o.get("output") or "", out_clip)
                           if o.get("status") == "ok" else f"[该成员未产出有效结果] {o.get('error', '')}"),
            })
        ok_outs = [o for o in outs if o.get("status") == "ok"]
        if ok_outs:
            # 单成员阶段（如「报告发布员」「交易员」）的正文直接作为该阶段产出，
            # 不加「— 名字 —」抬头，保证最终交付物干净；多成员阶段才标注来源。
            if len(members) == 1:
                last_ok = ok_outs[0]["output"]
            else:
                last_ok = "\n\n".join(
                    f"— {o['member']}{'（' + o['role'] + '）' if o.get('role') else ''} —\n{o['output']}"
                    for o in ok_outs
                )

        # 增量落盘：每个阶段一完成就写盘，后续阶段超时也不丢已有产出
        if run_dir:
            try:
                os.makedirs(os.path.join(run_dir, "members"), exist_ok=True)
                with open(os.path.join(run_dir, "config.json"), "w", encoding="utf-8") as f:
                    json.dump({"team": team, "task": task, "type": "pipeline", "partial": True},
                              f, ensure_ascii=False, indent=2)
                for o in outs:
                    fn = os.path.join(
                        run_dir, "members",
                        _sanitize_filename(f"{sid}__{o['member']}") + ".json",
                    )
                    with open(fn, "w", encoding="utf-8") as f:
                        json.dump(o, f, ensure_ascii=False, indent=2)
            except Exception:
                pass

    # ---- 最终产出 ----
    report = last_ok
    if team.get("final") == "lead":
        await _emit({"phase": "synthesis_start"})
        try:
            text, _attempts, interrupted = await _run_agent_with_retry(
                lambda: build_lead_agent(team, cfg, workspace_dir),
                compose_lead_prompt(team, task, results),
                member_timeout, retries=retries,
            )
            if interrupted:
                # 主理人汇总被打断 → 退回末阶段产出（若也没有，才用上游成员产出兜底），
                # 绝不把「I noticed that you have interrupted me.」当交付物。
                report = last_ok or interrupted_fallback(results, "主理人汇总")
                report = ("⚠️ 主理人汇总多次被中断，以下为最后一个阶段的产出（可重新运行以获得完整汇总）。\n\n"
                          + report) if last_ok else report
                await _emit({"phase": "member_error", "member": "主理人", "error": "汇总被中断"})
            else:
                report = text
            await _emit({"phase": "synthesis_done", "bytes": len(report)})
        except asyncio.CancelledError:
            raise
        except Exception as e:
            report = report or f"（主理人汇总失败：{e}）"
            await _emit({"phase": "synthesis_done", "bytes": len(report)})

    await _emit({"phase": "done", "team": team_name})

    if not (report or "").strip():
        report = ("（本次编排没有产出有效结果：所有阶段/成员均未成功。"
                  "请检查模型连通性与团队成员配置后重试。）")

    if run_dir:
        try:
            persist_pipeline_run(team, task, results, report, run_dir)
        except Exception:
            pass

    return report, results


def persist_pipeline_run(team: dict, task: str, results: list, report: str, run_dir: str):
    """多阶段管线运行的落盘记录（回滚友好）。文件名带阶段前缀避免跨阶段重名。"""
    os.makedirs(os.path.join(run_dir, "members"), exist_ok=True)
    with open(os.path.join(run_dir, "config.json"), "w", encoding="utf-8") as f:
        json.dump({"team": team, "task": task, "type": "pipeline"}, f, ensure_ascii=False, indent=2)
    for r in results:
        fn = os.path.join(
            run_dir, "members",
            _sanitize_filename(f"{r.get('stage', '')}__{r.get('member', 'member')}") + ".json",
        )
        with open(fn, "w", encoding="utf-8") as f:
            json.dump(r, f, ensure_ascii=False, indent=2)
    with open(os.path.join(run_dir, "report.json"), "w", encoding="utf-8") as f:
        json.dump({"report": report}, f, ensure_ascii=False, indent=2)
    with open(os.path.join(run_dir, "SUMMARY.md"), "w", encoding="utf-8") as f:
        f.write(render_pipeline_markdown(team, task, results, report))


def render_pipeline_markdown(team: dict, task: str, results: list, report: str) -> str:
    lines = [
        f"# 团队运行记录：{team.get('name', '?')}",
        f"时间：{time.strftime('%Y-%m-%d %H:%M:%S')}",
        f"任务：{task}",
        "",
    ]
    labels = {}
    for st in team.get("stages") or []:
        if isinstance(st, dict):
            labels[str(st.get("id") or "")] = st.get("label") or st.get("id") or ""
    for r in results:
        label = labels.get(str(r.get("stage") or ""), r.get("stage") or "")
        lines.append(f"## [{label}] {r.get('member', '?')}"
                     f"{'（' + r['role'] + '）' if r.get('role') else ''}")
        if r.get("status") == "error":
            lines.append(f"[执行失败] {r.get('error', '')}")
        else:
            lines.append(r.get("output", ""))
        lines.append("")
    lines.append("## 最终报告")
    lines.append("")
    lines.append(report)
    return "\n".join(lines)


# -------------------- 落盘记录（回滚友好） --------------------

def render_team_markdown(team: dict, task: str, results: list, report: str) -> str:
    lines = [
        f"# 团队运行记录：{team.get('name', '?')}",
        f"时间：{time.strftime('%Y-%m-%d %H:%M:%S')}",
        f"任务：{task}",
        "",
        "## 各成员分析",
        "",
    ]
    for r in results:
        name = r.get("member", "?")
        role = r.get("role", "")
        lines.append(f"### {name}{'（' + role + '）' if role else ''}")
        if r.get("status") == "error":
            lines.append(f"[执行失败] {r.get('error', '')}")
        else:
            lines.append(r.get("output", ""))
        lines.append("")
    lines.append("## 最终决策报告")
    lines.append("")
    lines.append(report)
    return "\n".join(lines)


def persist_team_run(team: dict, task: str, results: list, report: str, run_dir: str):
    """把一次运行写成 JSON(每成员/lead) + Markdown 摘要，便于回滚与浏览。"""
    os.makedirs(os.path.join(run_dir, "members"), exist_ok=True)
    with open(os.path.join(run_dir, "config.json"), "w", encoding="utf-8") as f:
        json.dump({"team": team, "task": task}, f, ensure_ascii=False, indent=2)
    for r in results:
        fn = os.path.join(run_dir, "members", _sanitize_filename(r["member"]) + ".json")
        with open(fn, "w", encoding="utf-8") as f:
            json.dump(r, f, ensure_ascii=False, indent=2)
    with open(os.path.join(run_dir, "lead.json"), "w", encoding="utf-8") as f:
        json.dump({"report": report}, f, ensure_ascii=False, indent=2)
    with open(os.path.join(run_dir, "SUMMARY.md"), "w", encoding="utf-8") as f:
        f.write(render_team_markdown(team, task, results, report))
