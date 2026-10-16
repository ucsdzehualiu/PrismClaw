"""PrismHarness 团队编排（Teams / 多智能体圆桌）。

镜像 Claude 官方多智能体编排模式：
一个 lead 把任务分发给多个带独立人格/提示词/颜色的 specialist 成员，
成员各自用精简 ReActAgent **并行** 独立运行，收集带标注的结果后，
lead 用独立 ReActAgent 综合产出最终决策报告。每次运行落盘 JSON + Markdown
记录（session_logs/<sid>/teams/run_<ts>/），可随时回滚（git 友好）。

关键纪律：团队成员 / lead 都用「精简 ReActAgent」（同 tools.run_subagent），
**绝不**传 `_prism_harness_sess` —— 它们内部写文件/跑 shell 完全自主，
不触发主会话的 HITL 人工确认（只有主代理自己的工具写操作走确认）。
配套约束：它们的 toolkit 必须走 `build_toolkit(interactive=False)`，受保护工具
在那里打 ALLOW 而不是 ASK——没有用户可确认时，ASK 会让代理停泊并交回占位文案。
"""

import asyncio
import json
import os
import re
import time
from dataclasses import dataclass

from model_config import build_model, load_config
from tools import build_toolkit

# Agentscope 把成员任务被取消（CancelledError 断在 tool 执行中）时，会返回这个
# 预设文案（_react_agent.handle_interrupt），且状态仍是 ok —— 成员看似成功、
# 实际没产出分析。冒烟常见（并发成员共享一个远端端点，初始取消/超时看门狗易命中）。
# 检出即判定为"被打断"，走重建重试，避免把垃圾输出当真结果。
INTERRUPTED_SENTINEL = "I noticed that you have interrupted me."

# AgentScope 2.0.9 里 ReActConfig.interruption_message 的**默认值**。被 CancelledError
# 打断时，回复正文就是这句（agent/_agent.py 里 content=self.react_config.interruption_message）。
# 注意：2.0.9 的包里根本没有 INTERRUPTED_SENTINEL 那句——只有这个默认值。所以团队里的
# 执行单元一律用 _team_react_config() 把 interruption_message 钉成 INTERRUPTED_SENTINEL
# （见下），同时检测也认这个默认值兜底，免得框架换默认值时检测又悄悄失效。
FRAMEWORK_INTERRUPT_DEFAULT = "I notice the interruption. How can I help you?"

# 2.0 权限引擎的「停泊文案」：工具调用等待用户确认（ASK）或外部执行，而没人
# 能确认时，Agent._next_action 会结束本轮并把这句话当回复返回，status 依然是
# ok。执行单元（团队成员/子代理）没有确认通道，一旦命中就必然空手而归——
# 把它当有效信号的话，19 位成员会"成功"返回同一句话，报告里一条分析都没有。
PERMISSION_WAIT_SENTINEL = (
    "I'm waiting for your permission or the external execution to finish."
)

# 成员产出被判定为预设文案（中断 / 权限停泊）时记成失败，而不是"有效信号"：
# 下游的风险/决策阶段与 lead 汇总据此跳过它，不把占位文案当观点引用。
MEMBER_STALL_ERROR = "成员未产出有效分析（工具调用被打断或等待权限确认而停泊），已判定为失败。"

# 用户点「停止」时，执行单元被判定为取消（而非失败/成功）。
MEMBER_CANCELED_ERROR = "已被用户取消。"

# 跑满 max_iters 时，框架会给 agent 一次「强制收尾」调用（提示它总结已有发现、
# 不要再调工具）。若那次也没出文本，框架就退回这句占位正文——那就等于没产出。
MAX_ITERS_PLACEHOLDER = "The maximum reasoning-acting iterations are exceeded."
MEMBER_EXHAUSTED_ERROR = "成员跑满迭代上限且未产出结论（连强制收尾都没出文本）。"

# ---- 收尾原因：取自框架，不再靠猜文案 ----
# AgentScope 把 finished_reason 直接打在**返回的那条 Msg 上**
# （message/_base.py: `finished_reason: ReplyFinishedReason | None`），所以
# agent.reply() 的返回值自带「这次是正常结束 / 被打断 / 跑满上限 / 出错」。
# 早先只按回复正文猜，框架一换默认文案（2.0.9 就换了）检测就整体失灵。
#
# 注意两套名字别混：下面是**归一化之后**的常量（本模块对外的词汇）；
# _classify_finish 里比对的则是**框架的** ReplyFinishedReason 值
# （"completed" / "interrupted" / "exceed_max_iters" / "error"）。
# 两者只有一部分恰好同名（interrupted、error 一样；框架的 exceed_max_iters
# 被归一成 exceeded），所以不能拿这边的常量去当框架的输入。
REASON_COMPLETED = "completed"
REASON_INTERRUPTED = "interrupted"
REASON_EXCEEDED = "exceeded"      # 跑满上限，但拿到了强制收尾的正文（可用，需标注）
REASON_EXHAUSTED = "exhausted"    # 跑满上限，连强制收尾都没出文本（不可用）
REASON_PARKED = "parked"          # 停泊：等权限确认 / 外部执行
REASON_ERROR = "error"            # 框架以 ERROR 收尾


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


async def _announce_member(emit, member: dict, phase: str, stage=None):
    """成员状态事件（member_queued / member_start）的唯一构造点。

    两种状态的**发出位置**是重点：member_queued 在并发信号量之外发，
    member_start 在抢到名额之后发。早先只在 sem 之前发一句 member_start，
    于是 19 个成员从 t=0 起全被标成「⏳ 分析中…」，而 team_max_concurrent
    （默认 4）之外的那十几个其实还在排队——面板把「排队」和「真正在跑」混成
    同一种样子，用户看到的就是「19 个人一直不动」，分不清是卡死还是慢。
    """
    ev = {
        "phase": phase,
        "member": member.get("name", "?"),
        "role": member.get("role", ""),
        "color": member.get("color", "#888888"),
    }
    if stage:
        ev["stage"] = stage
    await emit(ev)


async def _emit_canceled_members(emit, running: dict):
    """被外部取消时，给尚未收尾的成员补一条「已取消」事件。

    用户的「停止」是靠取消 asyncio 任务实现的（AgentScope 的打断也是这套：
    框架自己的 ChatService.interrupt 就是 cancel 掉 chat-run 任务）。但 run_member
    在 CancelledError 上直接 re-raise、不发任何事件，于是前端那几行永远停在
    「⏳ 分析中…」——用户点了停止，面板看起来毫无反应，以为按钮没生效。
    running 里放的是「已登记、还没收尾」的成员；这里把它们逐条落定，面板才会静下来。

    本函数只在取消路径上跑，尽力而为：客户端通常已经 abort 掉 SSE，事件发不出去
    也无所谓，前端另有本地收尾（见 index.html 的 stopGeneration）。
    """
    for mid in list(running):
        stage = running.pop(mid, None)
        ev = {"phase": "member_error", "member": mid, "error": MEMBER_CANCELED_ERROR}
        if stage:
            ev["stage"] = stage
        await emit(ev)


def _persist_member(run_dir, filename_base: str, result: dict) -> None:
    """成员一出结果就单独落盘。

    圆桌/管线的成员结果原先要等整个阶段 gather 完才批量写盘：19 人阶段跑 20 分钟，
    中途进程一挂（超时、Ctrl-C、端点整体不可用）这 19 份分析全部蒸发，磁盘上连
    痕都留不下。改成每人完成即写一份；文件名与阶段结束时的批量写盘保持一致，
    后者再写一遍是幂等覆盖。
    """
    if not run_dir:
        return
    try:
        d = os.path.join(run_dir, "members")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, _sanitize_filename(filename_base) + ".json"),
                  "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


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
    from agentscope.agent import Agent

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
    member_agent = Agent(
        name=_sanitize_filename(member.get("name", "member")),
        system_prompt=sys_prompt,
        model=model,
        toolkit=await build_toolkit(workspace_dir, interactive=False),
        react_config=_team_react_config(cfg),
    )
    return member_agent


async def build_lead_agent(team: dict, cfg: dict, workspace_dir: str):
    """构造 lead 的「精简 Agent」（无 HITL）。"""
    from agentscope.agent import Agent

    lead_prompt = team.get("lead_prompt") or DEFAULT_LEAD_PROMPT
    model = build_model(cfg, stream=False)
    lead_agent = Agent(
        name="team-lead",
        system_prompt=lead_prompt,
        model=model,
        toolkit=await build_toolkit(workspace_dir, interactive=False),
        react_config=_team_react_config(cfg),
    )
    return lead_agent


# -------------------- 运行编排 --------------------

@dataclass(frozen=True)
class AgentOutcome:
    """一次 agent 调用的结构化结果：正文 + 框架给出的收尾原因。"""

    text: str = ""
    reason: str = REASON_COMPLETED

    @property
    def usable(self) -> bool:
        """这段文本能否当成有效产出往下传。

        「跑满上限但拿到了强制收尾正文」算可用——那是 agent 自己总结的东西，
        丢掉才是浪费；只是要额外标注它可能不完整（见 _exceeded_note）。
        """
        return self.reason in (REASON_COMPLETED, REASON_EXCEEDED) and bool(self.text.strip())


def _classify_finish(reply, text: str) -> str:
    """把框架的 finished_reason 归一成 REASON_* 常量。

    绝大多数情况直接读框架打在 Msg 上的 finished_reason，不再对正文做关键词匹配。
    只有两处不得不看文案，且都是框架写死的字面量：

    - `exceed_max_iters` 有两种收尾方式：框架先做一次强制收尾调用，拿到正文就用
      （真的产出，只是上下文到顶了）；连那次都没出文本，才退回
      MAX_ITERS_PLACEHOLDER。框架在这两种情况下都标 EXCEED_MAX_ITERS，
      只能靠正文区分「有料」和「占位」。
    - 停泊（等权限确认 / 外部执行）那条 Exit 压根不带 finished_reason
      （agent/_agent.py 里 `Exit(exit_events=None, exit_msg=...)`），
      只能认那句固定文案。
    """
    raw = getattr(reply, "finished_reason", None)
    value = getattr(raw, "value", raw)  # ReplyFinishedReason 是 StrEnum
    if value == "interrupted":
        return REASON_INTERRUPTED
    if value == "exceed_max_iters":
        return REASON_EXHAUSTED if text.strip() == MAX_ITERS_PLACEHOLDER else REASON_EXCEEDED
    if value == "error":
        return REASON_ERROR
    if value is None and text.strip() == PERMISSION_WAIT_SENTINEL:
        return REASON_PARKED
    return REASON_COMPLETED


async def _run_agent_once(agent, text: str, timeout: float = 300.0) -> AgentOutcome:
    """跑一次精简代理，返回结构化结果（正文 + 框架给的收尾原因）。"""
    from agentscope.message import UserMsg

    reply = await asyncio.wait_for(
        agent.reply(UserMsg(name="user", content=text)),
        timeout=float(timeout),
    )
    txt = reply.get_text_content() if hasattr(reply, "get_text_content") else str(reply)
    txt = (txt or "").strip()
    return AgentOutcome(text=txt, reason=_classify_finish(reply, txt))


def _stall_error_text(outcome: AgentOutcome) -> str:
    """把「不可用」的收尾原因翻成给用户看的失败说明。"""
    if outcome.reason == REASON_EXHAUSTED:
        return MEMBER_EXHAUSTED_ERROR
    return MEMBER_STALL_ERROR


def _exceeded_note(outcome: AgentOutcome) -> str:
    """跑满上限但仍有正文时的标注；其余情况返回空串。"""
    if outcome.reason == REASON_EXCEEDED:
        return "⚠ 跑满迭代上限，以上为强制收尾产出，可能不完整"
    return ""


async def _settle_member(emit, member: dict, outcome: AgentOutcome, stage, extra=None):
    """成员跑完后的统一收尾：发事件 + 拼结果字典，三套编排共用。

    「可用 / 停泊 / 跑满上限」的判定只此一处。这类判定一旦在扁平、圆桌、管线里
    各写一遍，就必然出现「三份里改了一份」的漂移——上一轮 interrupt 检测失效
    正是这么来的。
    """
    mid = member.get("name", "?")
    result = {
        "member": mid,
        "role": member.get("role", ""),
        "color": member.get("color", "#888888"),
        "prompt": member.get("prompt", ""),
        "finished_reason": outcome.reason,
    }
    result.update(extra or {})

    if outcome.usable:
        note = _exceeded_note(outcome)
        ev = {
            "phase": "member_done", "member": mid, "bytes": len(outcome.text),
            "finished_reason": outcome.reason, "note": note,
        }
        if stage:
            ev["stage"] = stage
            # 摘要供前端"成员中间产物"展示（截断，避免灌爆小组件/主对话）
            ev["summary"] = outcome.text[:900] + ("…" if len(outcome.text) > 900 else "")
        await emit(ev)
        return {**result, "output": outcome.text, "status": "ok", "note": note}

    err = _stall_error_text(outcome)
    ev = {"phase": "member_error", "member": mid, "error": err,
          "finished_reason": outcome.reason}
    if stage:
        ev["stage"] = stage
    await emit(ev)
    return {**result, "output": "", "error": err, "status": "error"}


def is_interrupted_output(out: str) -> bool:
    """兜底用的纯文案检测（只用在汇总产物上）。

    正常路径已经改成读框架的 finished_reason（见 _classify_finish）；这个函数
    留给「已经把一堆成员产出拼成一大段文本」之后再快速扫一眼的场合——
    那时没有结构化的 reason 可用。认两种预设文案：被打断、权限停泊。
    """
    if not isinstance(out, str):
        return False
    return (
        INTERRUPTED_SENTINEL in out
        or FRAMEWORK_INTERRUPT_DEFAULT in out
        or PERMISSION_WAIT_SENTINEL in out
    )


def _team_react_config(cfg: dict):
    """执行单元（团队成员 / 主理人 / 风控 / 裁决）共用的 ReAct 配置。

    interruption_message 显式钉成 INTERRUPTED_SENTINEL：被 CancelledError 打断时，
    AgentScope 的回复正文**就是** `react_config.interruption_message`。它的默认值随
    版本漂移，而 is_interrupted_output 要认的就是这句话——不钉死的话，一旦框架换了
    默认值（2.0.9 就已经和代码里的常量对不上了），被打断的成员就会把占位文案当作
    有效分析喂给下游，报告里出现一句「I notice the interruption…」当观点。

    interruption_raise_cancelled_error 保持默认 False：框架会自己收尾（把未完成的
    工具调用补成 INTERRUPTED 结果），协程正常返回占位文案而不是抛异常——这正是
    上面那句检测能生效的前提。
    """
    from agentscope.agent import ReActConfig

    return ReActConfig(
        max_iters=int(cfg.get("agent", {}).get("max_iters", 30)),
        interruption_message=INTERRUPTED_SENTINEL,
    )


# 瞬时故障（可以「等一下再试」）：端点限流 / 过载 / 网关抖动 / 超时。
# 19 位成员并发打同一个模型端点时最常撞上——例如 ModelArts 回
# "429 ... Too many requests, the rate limit is 4 per second."。
# 早先这类异常直接把成员判死，整份分析白扔；其实退避几秒重试就能救回来。
_TRANSIENT_ERROR_RE = re.compile(
    r"\b(429|500|502|503|504)\b"
    r"|rate limit|too many requests|overloaded|timed out|timeout",
    re.IGNORECASE,
)


def is_transient_error(exc) -> bool:
    """异常是不是瞬时故障（重试有意义）。

    只看错误文案，**不**把 `TimeoutError` 一概算进来：`_run_agent_once` 的
    `wait_for(timeout)` 超时意味着该成员自己的时间预算已经烧光，重试只会让它再
    静默 10 分钟——那正是「看起来一直不动」的观感。成员预算耗尽就该立刻判失败。
    而 HTTP 层的读超时/连接超时（openai SDK 抛的 APITimeoutError 等）文案里带
    "timed out"，会被下面的正则捕获，照常重试。
    """
    return bool(_TRANSIENT_ERROR_RE.search(str(exc)))


def _retry_backoff(attempt: int) -> float:
    """指数退避 + 抖动：约 2s、4s、8s…（封顶 30s）。

    抖动是必需的：19 个成员同时撞限流，若都等相同秒数，重试还是会同刻扎堆、
    再撞一轮限流。
    """
    import random

    return min(2.0 * (2 ** attempt), 30.0) + random.uniform(0.0, 1.0)


async def _run_with_retry(run_once, retries: int = 2):
    """带重试地跑一次「代理调用」，返回 (AgentOutcome, attempts)。

    重试条件：产出被判为「被打断」（REASON_INTERRUPTED）、或撞上瞬时端点故障
    （429 限流等）。判「打断」现在看框架给的 finished_reason，不再猜文案；
    但重试仍必须重建新代理——被打断时 memory 里可能残留假的 tool 结果。
    非瞬时异常（配置错、参数错）立即抛出，不浪费重试。

    run_once 是个无参协程函数，返回 AgentOutcome。
    """
    outcome = AgentOutcome(reason=REASON_ERROR)
    attempts = 0
    total = max(1, retries)
    for attempt in range(total):
        attempts = attempt + 1
        try:
            outcome = await run_once()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            if not (is_transient_error(e) and attempts < total):
                raise
            await asyncio.sleep(_retry_backoff(attempt))
            continue
        if outcome.reason != REASON_INTERRUPTED:
            return outcome, attempts
        if attempts < total:
            await asyncio.sleep(_retry_backoff(attempt))
    return outcome, attempts


async def _run_agent_with_retry(build_agent, task, timeout, retries: int = 2):
    """构造代理并跑一次，带重试。build_agent 每次调用都必须返回全新代理。"""

    async def run_once():
        return await _run_agent_once(await build_agent(), task, timeout=timeout)

    return await _run_with_retry(run_once, retries)


async def _run_member_with_retry(build_agent, task, timeout, retries: int = 2):
    """成员版，与 _run_agent_with_retry 同契约：返回 (AgentOutcome, attempts)。"""
    return await _run_agent_with_retry(build_agent, task, timeout, retries)


def truncation_warning(results: list) -> str:
    """把「跑满迭代上限」的成员汇总成一段提示，附在最终报告末尾。

    _settle_member 给每个这样的成员打了行内标注，但那只出现在团队面板某一行；
    读最终报告的人（以及把报告导出、转发出去的人）看不到那一行。跑满上限意味着
    这份信号可能被截断，得让结论旁边就有交代。
    """
    hit = [str(r.get("member", "?")) for r in results
           if r.get("finished_reason") == REASON_EXCEEDED]
    if not hit:
        return ""
    cards = "\n".join(f"- {m}" for m in hit)
    return (
        f"\n\n---\n\n⚠️ **以下 {len(hit)} 位成员跑满迭代上限**，其分析为强制收尾产出、"
        f"可能不完整，结论请酌情参考：\n{cards}"
    )


def interrupted_fallback(results: list, what: str) -> str:
    """汇总/裁决类代理没产出可用正文时的降级产出。

    与其把框架的预设文案（被打断 / 停泊 / 跑满上限没文本）交出去，不如把上游已有
    内容原样交代给用户，并说明哪一步没跑成。
    """
    blocks = []
    for r in results:
        if r.get("status") != "ok":
            continue
        head = f"— {r.get('member', '?')}"
        if r.get("role"):
            head += f"（{r['role']}）"
        if r.get("note"):
            head += f" {r['note']}"
        blocks.append(f"{head} —\n{r.get('output', '')}")
    body = "\n\n".join(blocks)
    note = (f"⚠️ {what}未能产出最终汇总。"
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

    # 受限并发：与圆桌 / pipeline 一致，避免一次 gather 太多成员把模型端点撑爆。
    sem = asyncio.Semaphore(max(int(max_concurrent or 4), 1))

    running = {}  # 已登记、尚未收尾的成员 → 所属阶段（单阶段团队为 None）

    async def run_member(m):
        mid = m.get("name", "?")
        running[mid] = None
        await _announce_member(_emit, m, "member_queued")
        async with sem:
            await _announce_member(_emit, m, "member_start")
            try:
                retries = int(cfg.get("agent", {}).get("team_retries", 2))
                outcome, attempts = await _run_member_with_retry(
                    lambda m=m: build_team_agent(m, cfg, workspace_dir),
                    task, member_timeout, retries=retries,
                )
                if attempts > 1:
                    await _emit({"phase": "member_retrying", "member": mid, "attempts": attempts})
                res = await _settle_member(_emit, m, outcome, None)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                await _emit({"phase": "member_error", "member": mid, "error": str(e)})
                res = {
                    "member": mid,
                    "role": m.get("role", ""),
                    "color": m.get("color", "#888888"),
                    "prompt": m.get("prompt", ""),
                    "output": "",
                    "error": str(e),
                    "status": "error",
                }
        _persist_member(run_dir, mid, res)
        running.pop(mid, None)
        return res

    try:
        results = await asyncio.gather(*[run_member(m) for m in members])
    except asyncio.CancelledError:
        await _emit_canceled_members(_emit, running)
        raise

    # lead 汇总（即使有成员失败也继续，用成功的成员结果）
    await _emit({"phase": "synthesis_start"})
    lead_report = ""
    try:
        outcome, _attempts = await _run_agent_with_retry(
            lambda: build_lead_agent(team, cfg, workspace_dir),
            compose_lead_prompt(team, task, results),
            member_timeout,
            retries=int(cfg.get("agent", {}).get("team_retries", 2)),
        )
        if outcome.usable:
            lead_report = outcome.text
        else:
            # 主理人没拿到可用正文（被打断 / 停泊 / 跑满上限又没出文本）：
            # 绝不能把预设文案当报告交出去，退回成员产出。
            lead_report = interrupted_fallback(results, "主理人汇总")
            await _emit({"phase": "member_error", "member": "主理人",
                         "error": _stall_error_text(outcome)})
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
        head = f"— {r.get('member','?')}{'（'+r.get('role','')+'）' if r.get('role') else ''} —"
        if r.get("note"):
            # 跑满迭代上限的成员：正文是真的，但可能被截断——下游得知道
            head += f" {r['note']}"
        lines.append(head)
        lines.append(r.get("output", ""))
        lines.append("")
    if errs:
        lines.append("[以下成员未产出有效信号] " + "、".join(e.get("member", "?") for e in errs))
    lines.append("请基于以上全部信号，给出你的专业输出。")
    return "\n".join(lines)


async def _design_agent_for(prompt: str, name: str, cfg: dict, workspace_dir: str):
    """构造一个"角色专用"精简代理，prompt 即其角色+任务指令。"""
    from agentscope.agent import Agent

    model = build_model(cfg, stream=False)
    a = Agent(
        name=name,
        system_prompt=(prompt or "").strip(),
        model=model,
        toolkit=await build_toolkit(workspace_dir, interactive=False),
        react_config=_team_react_config(cfg),
    )
    return a


async def _run_design_agent_checked(prompt, name, task, input_text, cfg, workspace_dir,
                                     timeout, retries: int = 2):
    """风控/决策这类角色代理的带重试版本。返回 (AgentOutcome, attempts)。

    _run_design_agent 每次调用都会重建代理，所以循环调用天然就是「全新代理重试」。
    """

    async def run_once():
        return await _run_design_agent(prompt, name, task, input_text, cfg, workspace_dir, timeout)

    return await _run_with_retry(run_once, retries)


async def _run_design_agent(prompt: str, name: str, task: str, input_text: str, cfg: dict, workspace_dir: str, timeout: float) -> AgentOutcome:
    """跑一个角色代理（风控 / 决策），输入是拼好的候选文本 + 原任务。"""
    agent = await _design_agent_for(prompt, name, cfg, workspace_dir)
    return await _run_agent_once(agent, f"任务：{task}\n\n{input_text}", timeout=timeout)


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
    """圆桌编排（分析 →［风控］→ 决策）：

    Stage 1 – 并行分析：members[]（人数由团队 spec 决定，投资大师团 19 位、
              轻量团 5 位…）各自独立产出信号。
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

    running = {}  # 已登记、尚未收尾的成员 → 所属阶段

    async def run_member(m):
        mid = m.get("name", "?")
        running[mid] = "analysis"
        await _announce_member(_emit, m, "member_queued", stage="analysis")
        async with sem:
            await _announce_member(_emit, m, "member_start", stage="analysis")
            try:
                # 全新 build_team_agent 重建，避免打断后残留的假 tool 结果。
                retries = int(cfg.get("agent", {}).get("team_retries", 2))
                outcome, attempts = await _run_member_with_retry(
                    lambda m=m: build_team_agent(m, cfg, workspace_dir),
                    task, member_timeout, retries=retries,
                )
                if attempts > 1:
                    await _emit({"phase": "member_retrying", "member": mid,
                                 "attempts": attempts, "stage": "analysis"})
                res = await _settle_member(_emit, m, outcome, "analysis")
            except asyncio.CancelledError:
                raise
            except Exception as e:
                await _emit({"phase": "member_error", "member": mid, "error": str(e), "stage": "analysis"})
                res = {"member": mid, "role": m.get("role", ""), "color": m.get("color", "#888888"),
                       "prompt": m.get("prompt", ""), "output": "", "error": str(e), "status": "error"}
        _persist_member(run_dir, mid, res)
        running.pop(mid, None)
        return res

    await _emit({"phase": "stage_start", "stage": "analysis", "label": "各位大师并行分析中…",
                 "index": 0, "total": _n_stages})
    try:
        results = await asyncio.gather(*[run_member(m) for m in members])
    except asyncio.CancelledError:
        await _emit_canceled_members(_emit, running)
        raise
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
            risk_outcome, _attempts = await _run_design_agent_checked(
                risk_prompt, "risk-manager", task,
                _stage_prompt(task, results, f"以下是 {len(results)} 位大师/分析师的独立信号汇总："),
                cfg, workspace_dir, member_timeout,
                retries=int(cfg.get("agent", {}).get("team_retries", 2)),
            )
            if risk_outcome.usable:
                risk_report = risk_outcome.text
                # 风险评估是独立阶段的正文，单独发出去让前端给它一张卡片。
                # 不塞进最终报告：那会把它和决策混成一份，读者分不清哪段是约束、
                # 哪段是结论。（成员产出另有团队面板承载，这里只有单代理的阶段。）
                await _emit({"phase": "stage_report", "stage": "risk",
                             "label": "风险管理师评估", "text": risk_report})
            else:
                risk_report = "（风险管理师未能产出评估：%s）" % _stall_error_text(risk_outcome)
                await _emit({"phase": "member_error", "member": "risk-manager",
                             "error": _stall_error_text(risk_outcome), "stage": "risk"})
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
        decision_input = _stage_prompt(task, results, f"以下是 {len(results)} 位大师的完整信号：")
        if risk_prompt:
            decision_input += f"\n\n—— 风险管理师评估 ——\n{risk_report}"
        decision_outcome, _attempts = await _run_design_agent_checked(
            decision_prompt, "portfolio-manager", task, decision_input,
            cfg, workspace_dir, member_timeout,
            retries=int(cfg.get("agent", {}).get("team_retries", 2)),
        )
        if decision_outcome.usable:
            decision_report = decision_outcome.text
        else:
            # 决策没拿到可用正文时也要给用户可读的降级产出，而不是那句占位文案
            decision_report = interrupted_fallback(results, "投资组合经理决策")
            if risk_report:
                decision_report += f"\n\n—— 风险管理师评估 ——\n{risk_report}"
            await _emit({"phase": "member_error", "member": "portfolio-manager",
                         "error": _stall_error_text(decision_outcome), "stage": "decision"})
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
        f"## 一、{len(results)} 位大师/分析师信号",
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

    prev: [{"member", "role", "note", "output"}]（按时间先后）
    """
    kept = []
    total = 0
    for b in reversed(prev):  # 从最近的往回取
        seg = f"— {b.get('member', '?')}"
        if b.get("role"):
            seg += f"（{b['role']}）"
        if b.get("note"):
            seg += f" {b['note']}"
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
    running = {}  # 已登记、尚未收尾的成员 → 所属阶段 id（跨阶段累计）

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
            running[mid] = sid
            await _announce_member(_emit, m, "member_queued", stage=sid)
            async with sem:
                await _announce_member(_emit, m, "member_start", stage=sid)
                try:
                    outcome, attempts = await _run_member_with_retry(
                        lambda m=m: build_team_agent(m, cfg, workspace_dir),
                        stage_input, member_timeout, retries=retries,
                    )
                    if attempts > 1:
                        await _emit({"phase": "member_retrying", "member": mid,
                                     "attempts": attempts, "stage": sid})
                    res = await _settle_member(
                        _emit, m, outcome, sid, extra={"stage": sid, "stage_label": label})
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    await _emit({"phase": "member_error", "member": mid,
                                 "error": str(e), "stage": sid})
                    res = {"member": mid, "role": m.get("role", ""),
                           "color": m.get("color", "#888888"), "prompt": m.get("prompt", ""),
                           "output": "", "error": str(e), "status": "error",
                           "stage": sid, "stage_label": label}
            _persist_member(run_dir, f"{sid}__{mid}", res)
            running.pop(mid, None)
            return res

        try:
            outs = list(await asyncio.gather(*[run_member(m) for m in members]))
        except asyncio.CancelledError:
            await _emit_canceled_members(_emit, running)
            raise
        results.extend(outs)
        await _emit({"phase": "stage_done", "stage": sid, "count": len(outs)})

        # 把本阶段产出并入累积上下文（失败项以简短占位保留，供下游知情）
        for o in outs:
            prev.append({
                "member": o["member"],
                "role": o["role"],
                "note": o.get("note", ""),
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
            outcome, _attempts = await _run_agent_with_retry(
                lambda: build_lead_agent(team, cfg, workspace_dir),
                compose_lead_prompt(team, task, results),
                member_timeout, retries=retries,
            )
            if outcome.usable:
                report = outcome.text
            else:
                # 主理人汇总没拿到可用正文 → 退回末阶段产出（若也没有，才用上游成员
                # 产出兜底），绝不把预设文案当交付物。
                report = last_ok or interrupted_fallback(results, "主理人汇总")
                report = ("⚠️ 主理人汇总未产出可用内容，以下为最后一个阶段的产出（可重新运行以获得完整汇总）。\n\n"
                          + report) if last_ok else report
                await _emit({"phase": "member_error", "member": "主理人",
                             "error": _stall_error_text(outcome)})
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
