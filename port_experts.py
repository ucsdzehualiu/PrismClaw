"""把 专家团/ 下的外部专家团队（CodeBuddy/WorkBuddy Team Plugin 格式）移植为
PrismHarness 的团队配置与技能。

外部格式：`专家团/<team>/.codebuddy-plugin/plugin.json` + `agents/<id>.md`
        + `skills/<name>/SKILL.md`（技能）
目标格式：`teams/<团队名>.json`（harness 团队 spec）
        + `skills/<技能名>/`（技能，仓库级共享）

设计要点
- 幂等：重复运行覆盖生成物，可随时重跑。
- 保真：成员 prompt 取 agents/<id>.md 的正文（frontmatter 之外），一字不改；
  角色名/职业/配色映射到 harness 的 name/role/color 字段。
- 冲突以 harness 为准：外部 md 里讲 TeamCreate/Agent/SendMessage 的编排协议由
  harness 的 team.py 承担，故不进 prompt；多阶段编排改用 spec 的 stages 表达，
  由 team.py 的 pipeline 执行器按阶段顺序驱动（确定性，不依赖模型自觉）。
- 技能拷贝跳过媒体与超大字库等二进制（见 SKIP_EXT / MAX_FILE_BYTES）——这些是
  文档截图/演示动图，不影响技能在 LLM 侧的可用性；跳过数量会打印出来。

用法：
    python port_experts.py                      # 自动找源目录（见 DEFAULT_SRC_DIRS）
    python port_experts.py D:/expert-src        # 从别的目录生成
    PH_EXPERT_SRC=D:/expert-src python port_experts.py
"""

import hashlib
import json
import os
import re
import shutil
import sys

try:
    import yaml
except ImportError:  # pragma: no cover
    print("需要 PyYAML：pip install PyYAML")
    sys.exit(1)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# 源目录候选（按顺序找第一个存在的）：仓库内 `专家团/` → WorkBuddy 插件缓存。
# 用命令行参数或环境变量可覆盖。源包不必放在仓库里。
DEFAULT_SRC_DIRS = [
    os.path.join(BASE_DIR, "专家团"),
    os.path.join(os.path.expanduser("~"), ".workbuddy", "plugins", "cache", "experts"),
]


def _pick_src_dir() -> str:
    arg = sys.argv[1].strip() if len(sys.argv) > 1 and sys.argv[1].strip() else ""
    if arg:
        return arg
    env = (os.environ.get("PH_EXPERT_SRC") or "").strip()
    if env:
        return env
    for d in DEFAULT_SRC_DIRS:
        if os.path.isdir(d):
            return d
    return DEFAULT_SRC_DIRS[0]


SRC_DIR = _pick_src_dir()
TEAMS_OUT = os.path.join(BASE_DIR, "teams")
SKILLS_OUT = os.path.join(BASE_DIR, "skills")

# 技能拷贝时跳过的媒体/二进制扩展名（文档截图、演示动图、字体、包等）
SKIP_EXT = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".ico", ".tiff",
    ".mp4", ".mov", ".webm", ".avi", ".mkv",
    ".ttf", ".otf", ".woff", ".woff2", ".eot",
    ".zip", ".7z", ".rar", ".tar", ".gz", ".pdf", ".exe", ".dll", ".so",
    ".pyc", ".pyo", ".map",
}
MAX_FILE_BYTES = 8 * 1024 * 1024  # 单文件上限（防意外拷进巨型产物）

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?", re.DOTALL)

_PALETTE = [
    "#4f8ef7", "#34c759", "#ff9f0a", "#ff375f", "#af52de",
    "#5ac8fa", "#ffd60a", "#30d158", "#bf5af2", "#64d2ff",
]


# -------------------- 源文件读取 --------------------

_ROOT_CACHE = {}


def team_root(team_dir: str) -> str:
    """团队包的实际根目录。

    两种布局都要支持：
    - 扁平：  专家团/<team>/.codebuddy-plugin/plugin.json
    - 带版本：专家团/<team>/<version>/.codebuddy-plugin/plugin.json
    """
    if team_dir in _ROOT_CACHE:
        return _ROOT_CACHE[team_dir]
    root = os.path.join(SRC_DIR, team_dir)
    if not os.path.isdir(os.path.join(root, ".codebuddy-plugin")):
        try:
            for sub in sorted(os.listdir(root)):
                cand = os.path.join(root, sub)
                if os.path.isdir(os.path.join(cand, ".codebuddy-plugin")):
                    root = cand
                    break
        except OSError:
            pass
    _ROOT_CACHE[team_dir] = root
    return root


def load_plugin(team_dir: str) -> dict:
    path = os.path.join(team_root(team_dir), ".codebuddy-plugin", "plugin.json")
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def read_agent_md(team_dir: str, agent_id: str) -> dict:
    """读取 agents/<id>.md，返回 {frontmatter, body}。"""
    path = os.path.join(team_root(team_dir), "agents", agent_id + ".md")
    with open(path, "r", encoding="utf-8") as f:
        raw = f.read()
    m = _FRONTMATTER_RE.match(raw)
    fm, body = {}, raw
    if m:
        try:
            fm = yaml.safe_load(m.group(1)) or {}
        except Exception:
            fm = {}
        body = raw[m.end():]
    if not isinstance(fm, dict):
        fm = {}
    return {"frontmatter": fm, "body": body.strip()}


def roster(plugin: dict) -> dict:
    """plugin.json members[] → {agent_id: {zh, en, profession_zh, role}}。"""
    out = {}
    for m in plugin.get("members", []) or []:
        if not isinstance(m, dict) or not m.get("id"):
            continue
        name = m.get("name") or {}
        prof = m.get("profession") or {}
        out[m["id"]] = {
            "zh": (name.get("zh") if isinstance(name, dict) else name) or m["id"],
            "profession_zh": (prof.get("zh") if isinstance(prof, dict) else prof) or "",
            "role": m.get("role") or "member",
        }
    return out


def _zh(value):
    if isinstance(value, dict):
        return value.get("zh") or value.get("en") or ""
    return value or ""


def _fallback_color(seed: str) -> str:
    h = 0
    for ch in (seed or "x"):
        h = (h * 31 + ord(ch)) & 0xFFFFFFFF
    return _PALETTE[h % len(_PALETTE)]


def member_spec(team_dir: str, agent_id: str) -> dict:
    """把一位外部 agent 转成 harness 团队成员 dict（name/role/color/prompt）。"""
    info = roster(load_plugin(team_dir)).get(agent_id, {})
    md = read_agent_md(team_dir, agent_id)
    fm = md["frontmatter"]
    name = info.get("zh") or _zh(fm.get("displayName")) or fm.get("name") or agent_id
    role = info.get("profession_zh") or _zh(fm.get("profession")) \
        or _first_line(fm.get("description", "")) or ""
    return {
        "name": name,
        "role": role,
        "color": fm.get("color") or _fallback_color(agent_id),
        "prompt": md["body"],
    }


def _first_line(text) -> str:
    if not isinstance(text, str):
        return ""
    t = text.strip()
    return t.splitlines()[0].strip() if t else ""


def lead_prompt_of(team_dir: str) -> str:
    """主理人 md 正文（仅 final == 'lead' 的团队需要）。"""
    plugin = load_plugin(team_dir)
    lead_id = (plugin.get("teamInfo") or {}).get("leadAgent") or plugin.get("agentName")
    if not lead_id:
        return ""
    return read_agent_md(team_dir, lead_id)["body"]


def stage(sid, label, team_dir, agent_ids, task="", include_prev=True, prev_header=""):
    """构造一个 pipeline 阶段（阶段内成员并行）。"""
    st = {
        "id": sid,
        "label": label,
        "members": [member_spec(team_dir, aid) for aid in agent_ids],
    }
    if task:
        st["task"] = task
    if not include_prev:
        st["include_prev"] = False
    if prev_header:
        st["prev_header"] = prev_header
    return st


# -------------------- 技能移植 --------------------

def _iter_files(root: str):
    for dirpath, _dirnames, filenames in os.walk(root):
        for fn in filenames:
            yield os.path.join(dirpath, fn)


def _same_file(a: str, b: str) -> bool:
    """比较两个文件内容是否相同（先比大小，再比 md5）。"""
    try:
        if os.path.getsize(a) != os.path.getsize(b):
            return False
        return hashlib.md5(open(a, "rb").read()).hexdigest() == \
            hashlib.md5(open(b, "rb").read()).hexdigest()
    except OSError:
        return False


def copy_skill(src: str, dst_name: str, log: list) -> int:
    """把外部技能目录拷到 skills/<dst_name>/，跳过媒体/超大二进制。

    已存在时**按内容刷新**——源包是权威版本，源更新了产物就必须跟着更新
    （早先「已存在即跳过」会导致源包改版后产物静默过期）。
    同一次运行内同名技能只拷一次（多个团队共用同一技能）。
    """
    dst = os.path.join(SKILLS_OUT, dst_name)
    existed = os.path.isdir(dst)
    copied, updated, skipped = 0, 0, []
    for path in _iter_files(src):
        ext = os.path.splitext(path)[1].lower()
        rel = os.path.relpath(path, src)
        if ext in SKIP_EXT:
            skipped.append(rel)
            continue
        try:
            if os.path.getsize(path) > MAX_FILE_BYTES:
                skipped.append(rel + "(超大)")
                continue
        except OSError:
            continue
        target = os.path.join(dst, rel)
        if os.path.isfile(target) and _same_file(path, target):
            copied += 1
            continue
        if os.path.isfile(target):
            updated += 1
        os.makedirs(os.path.dirname(target), exist_ok=True)
        shutil.copy2(path, target)
        copied += 1
    note = f"（跳过 {len(skipped)} 个媒体/超大文件）" if skipped else ""
    if existed and updated:
        log.append(f"  · 技能 {dst_name}：{copied} 个文件，其中 {updated} 个已更新{note}")
    elif existed:
        log.append(f"  · 技能 {dst_name}：{copied} 个文件，内容已是最新")
    else:
        log.append(f"  · 技能 {dst_name}：拷贝 {copied} 个文件{note}")
    return copied


def port_skills(log: list) -> None:
    """移植所有团队的全部技能（同名只拷一次；源包更新则刷新产物）。"""
    os.makedirs(SKILLS_OUT, exist_ok=True)
    seen = set()
    for team_dir in sorted(os.listdir(SRC_DIR)):
        skill_root = os.path.join(team_root(team_dir), "skills")
        if not os.path.isdir(skill_root):
            continue
        for skill_name in sorted(os.listdir(skill_root)):
            skill_path = os.path.join(skill_root, skill_name)
            if not os.path.isdir(skill_path) or skill_name in seen:
                continue
            seen.add(skill_name)
            copy_skill(skill_path, skill_name, log)


def _annotate(spec: dict, team_dir: str) -> dict:
    """在产物里记下来源（源目录名 + 版本），便于反查、升级对照。

    团队文件用的是中文显示名，光看文件名对不回源目录（如 相信光么 ← believe-in-light）。
    """
    try:
        ver = load_plugin(team_dir).get("version", "")
    except Exception:
        ver = ""
    out = {"source": team_dir}
    if ver:
        out["source_version"] = ver
    out.update(spec)
    return out


# 有些技能内联了第三方代码，分发时必须随附其许可证。
# 例：westock 的 scripts/*.js 打包了 ajv / uri-js / require-in-the-middle，
# 授权书在 a-share-analysis/license/ 下——代码拷了、许可证也得跟着走。
_LICENSE_SKILL_MAP = {
    "a-share-analysis": ["westock"],
}


def port_team_licenses(log: list) -> None:
    """把团队自带的 license/ 搬到对应技能目录下（合规要求，不是可选装饰）。"""
    for team_dir, skill_names in _LICENSE_SKILL_MAP.items():
        lic_dir = os.path.join(team_root(team_dir), "license")
        if not os.path.isdir(lic_dir):  # 有的包把 license/ 放在版本目录之外
            lic_dir = os.path.join(SRC_DIR, team_dir, "license")
        if not os.path.isdir(lic_dir):
            continue
        for skill_name in skill_names:
            dst_skill = os.path.join(SKILLS_OUT, skill_name)
            if not os.path.isdir(dst_skill):
                continue
            dst = os.path.join(dst_skill, "license")
            os.makedirs(dst, exist_ok=True)
            n = 0
            for fn in sorted(os.listdir(lic_dir)):
                src = os.path.join(lic_dir, fn)
                if os.path.isfile(src):
                    shutil.copy2(src, os.path.join(dst, fn))
                    n += 1
            if n:
                log.append(f"  · 许可证 -> skills/{skill_name}/license/（{n} 份，内联第三方代码的授权书）")


def port_believe_in_light_engine(log: list) -> None:
    """believe-in-light 没有 SKILL.md——它的「技能」是包内的确定性 Python 引擎
    （references/ 下的 .py + calibration.json）加一份引擎契约文档。单独打成一个技能包。

    注意：这些文件在 team_root 里（有的版本外面套了版本目录），必须走 team_root 解析，
    不能写死包根路径。
    """
    team_dir = "believe-in-light"
    root = team_root(team_dir)
    src = os.path.join(root, "references")
    if not os.path.isdir(src):
        return
    dst = os.path.join(SKILLS_OUT, "believe-in-light-engine")
    os.makedirs(dst, exist_ok=True)
    n = 0
    for path in _iter_files(src):
        if os.path.splitext(path)[1].lower() in SKIP_EXT:
            continue
        shutil.copy2(path, os.path.join(dst, os.path.basename(path)))
        n += 1
    contract = os.path.join(root, "believe-in-light_引擎契约.md")
    body = ""
    if os.path.isfile(contract):
        with open(contract, "r", encoding="utf-8") as f:
            body = f.read().strip()
    skill_md = (
        "---\n"
        "name: believe-in-light-engine\n"
        "description: 相信光么·光模块产业链信号监控的确定性计算引擎（景气度/置信度/评级/"
        "自进化落盘）。当需要按指标表扫描三端信号、验证因果链、计算景气度与置信度、"
        "生成九宫格评级报告或做季度校准回写时使用。\n"
        "---\n\n"
        "# 相信光么 · 引擎契约与脚本\n\n"
        "本目录是 believe-in-light 专家团的确定性引擎：脚本在 `references/`（随本技能拷入），"
        "运行方式为 `python <脚本名> <参数>`。\n\n"
        "**铁律**：方向裁决唯一权威在因果验证；权重/景气度/置信度一律以 "
        "`weight_engine.py` 输出为准，禁止在 LLM 内重算。\n\n"
        "---\n\n" + body + "\n"
    )
    with open(os.path.join(dst, "SKILL.md"), "w", encoding="utf-8") as f:
        f.write(skill_md)
    log.append(f"  · 技能 believe-in-light-engine：拷贝 {n} 个脚本 + SKILL.md（由引擎契约生成）")


# -------------------- 团队移植 --------------------

def write_team(spec: dict, log: list) -> None:
    os.makedirs(TEAMS_OUT, exist_ok=True)
    name = spec["name"]
    path = os.path.join(TEAMS_OUT, name + ".json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(spec, f, ensure_ascii=False, indent=2)
    n = len(spec.get("members") or []) or sum(
        len(s.get("members") or []) for s in spec.get("stages") or []
    )
    log.append(f"  ✓ teams/{name}.json（{n} 位成员，type={spec.get('type', 'parallel')}）")


# ============ 各团队的 harness spec 定义 ============
# 说明：外部团队多为「先并行分析 → 再串行收敛」的 SOP，这里按 pipeline 的
# stages 逐阶段还原；扁平并行团队（无阶段依赖）直接用普通 members。

A_SHARE = "a-share-analysis"
BELIEVE = "believe-in-light"
DD = "dd-due-diligence-team"
RESEARCH = "gpt-researcher-team"
PPT = "humanize-ppt-team"
MASTERS = "investment-masters-team"
MIGRAQ = "migraq-team"
OPC = "opc-team"
TRADING = "trading-agent"
FINANCE = "awesome-finance-skills"
DOC = "openspec-doc-team"
STOCK = "stock-partner-team"


def plan_a_share() -> dict:
    return {
        "name": "A股研究团队",
        "description": "A股全链路研究：宏观策略 / 盘面解读 / 个股深度 / 估值定价 / 产业链映射 / "
                       "资金追踪 / 风险诊断。先并行多路分析，再由风险诊断师收敛风险，主理人汇编成报告。",
        "type": "pipeline",
        "final": "lead",
        "lead_prompt": (
            "你是 A股研究团队的主理人·研究总监。综合各位分析师（宏观/盘面/个股/估值/产业链/资金）"
            "与风险诊断师的独立产出，输出一份结构化 A股研究报告：\n"
            "1. 核心结论（结论先行，一句话说清方向与理由）\n"
            "2. 多维证据（引用各分析师的量化结论与数据）\n"
            "3. 产业链与资金视角\n"
            "4. 风险提示与分歧（引用风险诊断师意见）\n"
            "5. 明确的应对建议（仓位/节奏/触发条件）\n"
            "分点、数据驱动，不给没有依据的判断。"
        ),
        "stages": [
            stage("research", "🔍 多路并行分析", A_SHARE,
                  ["macro-strategist", "market-reader", "stock-researcher",
                   "valuation-pricer", "industry-mapper", "money-tracker"],
                  task="任务：{task}\n\n请从你负责的维度做一次独立分析，给出量化结论"
                       "（评分/关键价位/数据）与你所在维度的明确观点。不要泛泛而谈。",
                  include_prev=False),
            stage("risk", "🛡️ 风险诊断", A_SHARE, ["risk-doctor"],
                  task="任务：{task}\n\n基于以上各分析师的多维结论，做一次独立风险诊断："
                       "列出主要风险点（按严重度排序，标注触发信号与潜在下行）、"
                       "结论的脆弱之处、以及最坏情景下的应对纪律。"),
        ],
    }


def plan_awesome_finance() -> dict:
    return {
        "name": "鹏城信息AI专家",
        "description": "金融市场综合分析专家：聚合财经新闻与全球股票行情（A股/港股/美股），"
                       "结合情绪分析与时序预测，追踪投资信号演化、可视化传导链路并生成专业研报。"
                       "全部能力由内置工具（联网搜索/网页抓取/读写文件）实现，无外部脚本依赖。",
        "type": "pipeline",
        "final": "last",
        "stages": [
            stage("single", "📊 综合金融市场分析", FINANCE, ["awesome-finance-skills"],
                  task="任务：{task}\n\n请按你的标准流程执行：需求界定 → 数据采集（新闻+行情）→ "
                       "情绪与信号研判 → 趋势预测与传导链路 → 输出结构化研报（Markdown）。"
                       "数据必须来自可核验的公开来源并标注时间；查不到就如实说明，不要编造。",
                  include_prev=False),
        ],
    }


def plan_believe_in_light() -> dict:
    return {
        "name": "相信光么",
        "description": "光模块产业链信号监控：供给/需求/技术三端并行扫描 → 因果链验证 → "
                       "权重校准（确定性计算）→ 九宫格评级 + HTML 监控报告。"
                       "原版依赖万得/通达信 MCP，此处无对应数据源时自动降级为公开互联网数据。",
        "type": "pipeline",
        "final": "last",
        "stages": [
            stage("scan", "🔭 三端信号扫描（并行）", BELIEVE,
                  ["gongji-scanner", "xuqiu-scanner", "jishu-scanner"],
                  task="任务：{task}\n\n按你负责那一端的指标表做扫描。只输出纯观测信号（指标名/"
                       "变动幅度/距离层级/量化值），触发阈值的才输出，未触发则说明「未触发」。"
                       "不要预判方向。数据源优先级：可核验的公开数据 > WebSearch；"
                       "拿不到精确数据时明确标注「精度有限」。",
                  include_prev=False),
            stage("causal", "🔗 因果链验证", BELIEVE, ["causal-verifier"],
                  task="任务：{task}\n\n基于三端触发信号，映射各条因果链并动态判定前序/后序"
                       "（每条链上 chain_index 最大的触发者=后序），给出 effective_sign"
                       "（方向唯一权威）→ 输出 active_signals + chain_health。"),
            stage("weight", "⚖️ 权重校准", BELIEVE, ["weight-calibrator"],
                  task="任务：{task}\n\n基于上游 effective_sign 与触发信号，计算景气度"
                       "（后序Σ距离折扣×命中率×effective_sign）与置信度（C×R×S）。"
                       "给出确定性数值与计算依据；不得凭空估计。"),
            stage("rate", "🎯 评级输出", BELIEVE, ["rater"],
                  task="任务：{task}\n\n汇合景气度 × 置信度，给出九宫格评级（🟢🟡🔴）"
                       "与结构化监控报告（最终评级/景气度/置信度/多链收敛/自进化状态/运行元信息）。"),
        ],
    }


def plan_dd() -> dict:
    return {
        "name": "AI尽调专家团",
        "description": "银行对公授信尽调七步 SOP：进件材料 → 财务核验 → 四路分析（企业画像/财务/"
                       "经营/行业）→ 风险总览 → 最终报告。原版依赖 aidd-saas 等 MCP 取数，"
                       "此处无对应连接器时按现有材料 + 公开信息推理，并在结论中标注数据来源与缺口。",
        "type": "pipeline",
        "final": "last",
        "stages": [
            stage("intake", "📥 进件材料准备", DD, ["intake-material-custodian"],
                  task="任务：{task}\n\n梳理本次尽调需要的材料清单与已具备/缺失情况，"
                       "输出材料充实度评估与后续取数清单。用户未提供材料时，"
                       "给出「需要用户补充什么」的清单。",
                  include_prev=False),
            stage("verify", "🔢 财务数据核验", DD, ["finance-verify-specialist"],
                  task="任务：{task}\n\n基于已有材料做财务数据核验（三表标准化 + 勾稽校验），"
                       "输出核验结论与勾稽差异清单；无财务数据时如实说明并列出待补项。"),
            stage("analyze", "🔬 四路并行分析", DD,
                  ["enterprise-profiling-expert", "financial-analysis-specialist",
                   "business-analysis-expert", "industry-analysis-expert"],
                  task="任务：{task}\n\n从你负责的维度（企业画像/财务/经营/行业）输出专项分析，"
                       "结论必须区分「有据」与「推测」，缺据留白，不要编造数据。"),
            stage("risk", "🛡️ 风险总览与综合研判", DD, ["risk-compliance-expert"],
                  task="任务：{task}\n\n汇总各专项结论中的风险点，输出全量风险清单（分级）"
                       "+ 跨维度关联风险 + 综合研判。"),
            stage("report", "📄 最终尽调报告", DD, ["report-writer"],
                  task="任务：{task}\n\n整合各路结论、风险等级与风险清单，输出一份结构化的"
                       "总体尽调报告（结论有据可溯、缺据留白），末尾附数据来源与局限性说明。"),
        ],
    }


def plan_research() -> dict:
    return {
        "name": "深度研究团队",
        "description": "多源深度研究报告工坊：初始调研 → 规划大纲 → 逐章深度研究（调研→审稿→修订）"
                       "→ 撰写引言结论与参考文献 → 整合发布。产出带超链接引用的专业研究报告。"
                       "此处串行复现其主链路（逐章循环收敛为单轮代表性流程）。",
        "type": "pipeline",
        "final": "last",
        "stages": [
            stage("scout", "🔍 初始调研", RESEARCH, ["topic-researcher"],
                  task="任务：{task}\n\n对课题做广泛初步调研：输出 500-1000 字研究摘要"
                       "（定义背景/主流观点/争议焦点/关键数据/主要参与者/最新进展）"
                       "+ 来源池清单（尽量 8-15 条，带链接）。禁止编造 URL。",
                  include_prev=False),
            stage("outline", "🗂️ 规划大纲", RESEARCH, ["research-planner"],
                  task="任务：{task}\n\n基于上游调研摘要，规划报告章节大纲（默认 ≤5 章，"
                       "不含引言/结论/参考文献），章节间逻辑递进、不重叠。"
                       "输出 JSON：{title, date, sections:[...]}。"),
            stage("draft", "✍️ 逐章深度研究", RESEARCH, ["topic-researcher"],
                  task="任务：{task}\n\n按大纲逐章撰写章节草稿（每章 800-1500 字，结构："
                       "论点→论据→分析→小结），每章聚合 ≥5 个不同来源并全部带 Markdown "
                       "超链接引用。只写确有把握的内容，来源不足要如实说明。"),
            stage("review", "🔎 审稿", RESEARCH, ["draft-reviewer"],
                  task="任务：{task}\n\n按 6 维标准审查上游草稿（来源充分性/事实准确性/"
                       "观点均衡性/内容深度/结构清晰度/格式规范性），输出 PASS 或 REVISE"
                       "（含必须修改项 + 建议改进项）。"),
            stage("revise", "🛠️ 修订", RESEARCH, ["draft-reviser"],
                  task="任务：{task}\n\n根据审稿意见逐条回应并修订（补充真实来源、纠正事实、"
                       "补充反方观点、深化分析），保持未受批评部分不动，输出完整修订稿。"
                       "禁止编造 URL。"),
            stage("framing", "📐 撰写框架", RESEARCH, ["report-writer"],
                  task="任务：{task}\n\n汇总全部章节，撰写 300-500 字引言（带引用）、300-500 字结论"
                       "（带引用）、目录与 APA 格式参考文献列表（去重）。"),
            stage("publish", "📚 整合发布", RESEARCH, ["report-publisher"],
                  task="任务：{task}\n\n把目录/引言/各章正文/结论/参考文献整合为完整 Markdown "
                       "研究报告（不改研究内容，仅整合与格式化）：章节编号连续、"
                       "超链接格式统一、参考文献去重。"),
        ],
    }


def plan_ppt() -> dict:
    return {
        "name": "卡尔的人感PPT专家团",
        "description": "PPT 大纲、生成、视频、演示与交付专家团：AST 大纲导演 → 双路渲染"
                       "（归藏中文稳定版 / 风格探索可上线版）→ 视频动效 → 演讲模式 → 交付质检。",
        "type": "pipeline",
        "final": "last",
        "stages": [
            stage("outline", "🗂️ 大纲导演（生产契约）", PPT, ["outline-director"],
                  task="任务：{task}\n\n产出完整生产契约：deck_brief（受众/场景/页数/风格）、"
                       "ast_outline（结构化大纲）、slide_plan（每页要点）、speaker_intent"
                       "（演讲意图）、asset_manifest（素材清单）、video_slots（视频位，可空）。"
                       "先用最少的问题明确目标，再输出契约。",
                  include_prev=False),
            stage("render", "🎨 页面生产（双路并行）", PPT,
                  ["guizang-renderer", "frontend-slides-renderer"],
                  task="任务：{task}\n\n基于上游生产契约，用你对应的路径生成 HTML 演示文稿："
                       "归藏路径产出中文稳定版单文件 HTML；风格探索路径产出可上线/可导出 PDF 的"
                       "HTML Slides。输出可直接打开的完整 HTML（自包含，相对路径正确）。"),
            stage("video", "🎬 视频与动效", PPT, ["video-motion-agent"],
                  task="任务：{task}\n\n若上游 video_slots 有需求，输出视频方案/分镜与渲染清单；"
                       "无需求则明确说明「本次无视频需求」并给出可选增强建议。"),
            stage("present", "🎤 演讲模式增强", PPT, ["html-ppt-presenter"],
                  task="任务：{task}\n\n基于主交付 HTML 与 speaker_intent，产出演讲者备注/逐字稿"
                       "与 presenter mode 接入方案。"),
            stage("qa", "✅ 交付质检", PPT, ["qa"],
                  task="任务：{task}\n\n对全部产物做最终质检：契约完整性、HTML 可打开、"
                       "视频 fallback、引用资源相对路径、presenter 可用性。输出修复清单 + "
                       "交付 manifest；发现的问题要具体到文件与位置。"),
        ],
    }


def plan_masters() -> dict:
    """投资大师专家团 —— 与 harness 现有 roundtable 类型天然对应：
    19 位大师/分析师并行 → 风险管理师 → 投资组合经理。"""
    plugin = load_plugin(MASTERS)
    analysis_ids = [
        m["id"] for m in plugin["members"]
        if m["role"] == "member" and m["id"] not in ("risk-manager", "portfolio-manager")
    ]
    return {
        "name": "投资大师专家团",
        "description": "AI 对冲基金多大师投资分析：19 位传奇投资大师与专业分析师并行独立研判 → "
                       "风险管理师评估风险与仓位 → 投资组合经理给出最终 BUY/SELL/HOLD 决策报告。",
        "type": "roundtable",
        "lead_prompt": "你是投资组合经理。基于 19 位大师信号 + 风险报告，给出 BUY/SELL/HOLD 最终决策。",
        "risk_prompt": read_agent_md(MASTERS, "risk-manager")["body"],
        "decision_prompt": read_agent_md(MASTERS, "portfolio-manager")["body"],
        "members": [member_spec(MASTERS, aid) for aid in analysis_ids],
    }


def plan_migraq() -> dict:
    return {
        "name": "腾讯云上云迁移专家团",
        "description": "上云迁移全流程：资源扫描与产品评估 → 架构与 Landing Zone 设计 → "
                       "交付实施 → 运维保障 → 综合方案输出。原版通过 CMG/MSP 远程服务取数，"
                       "此处无凭据时按公开产品知识 + 用户提供的环境信息给出方案。",
        "type": "pipeline",
        "final": "lead",
        "lead_prompt": (
            "你是腾讯云上云迁移专家团的首席解决方案专家。综合各专家的产出，输出一份完整的"
            "迁移方案：\n1. 迁移范围与目标（现状 → 目标态）\n2. 产品选型与映射\n"
            "3. 目标架构与 Landing Zone 规划\n4. 迁移实施步骤与分批策略\n"
            "5. 运维保障与回滚预案\n6. 风险清单与 TCO 对比\n"
            "方案要具体可执行；涉及用户环境信息的缺口要明确标注为「待确认」。"
        ),
        "stages": [
            stage("assess", "🔎 资源扫描与产品评估", MIGRAQ,
                  ["product-expert", "fde-engineer"],
                  task="任务：{task}\n\n评估迁移范围、现状资源与约束，给出腾讯云产品映射建议"
                       "与环境评估结论。缺信息就列出「需要用户补充什么」。",
                  include_prev=False),
            stage("design", "🏗️ 架构与 Landing Zone 设计（并行）", MIGRAQ,
                  ["landing-zone-expert", "cloud-architect"],
                  task="任务：{task}\n\n基于上游评估结论，输出目标架构设计与 Landing Zone "
                       "规划（账号体系/网络/安全合规/多环境）。"),
            stage("deliver", "🚚 交付计划与实施方案", MIGRAQ,
                  ["delivery-engineer", "fde-engineer"],
                  task="任务：{task}\n\n基于目标架构，输出迁移实施方案与部署验证步骤"
                       "（分批策略、停机窗口、回滚点、验证清单）。"),
            stage("ops", "🛠️ 运维保障", MIGRAQ, ["ops-engineer"],
                  task="任务：{task}\n\n输出监控告警、稳定性验证与回滚预案，"
                       "明确上线后的运维职责与关键指标。"),
        ],
    }


def plan_opc() -> dict:
    """一人公司专家团：9 阶段共创。建盘期 01-07 线性，运营期 08-09 按需触发。

    运营期（资产沉淀 / 经营复盘）在原包里是「用户感觉产出重复时/卡住时」才触发，
    线性 pipeline 表达不了条件分支，所以放在末阶段由成员自行判断是否适用
    （不适用就一句话带过），再由主理人汇总——这样 8 位专家一个不少，也不会白跑。
    """
    return {
        "name": "一人公司专家团",
        "description": "一人公司方法论九阶段共创：资源盘点 → 利基定位 → 价值主张 → 商业模式 → "
                       "MVP 实验 → 转化闭环（建盘期线性推进）→ 资产沉淀 / 经营复盘（运营期按需触发）。",
        "type": "pipeline",
        "final": "lead",
        "lead_final_instruction": (
            "请**直接输出完整的一人公司建盘方案**（从标题开始）。"
            "不要写「主理人结论」「以下为完整执行方案」之类的开场白或处理说明——"
            "你的输出本身就是交付物。"
        ),
        "lead_prompt": (
            "你是 OPC 一人公司专家团的主理人（首席编排师）。综合各阶段成员的产出，"
            "输出一份完整、可执行的一人公司建盘方案：\n"
            "1. 资源盘点结论（家底、约束与可用杠杆）\n"
            "2. 利基定位（推荐方向 + 理由 + 备选）\n"
            "3. 价值主张（Jobs / Pains / Gains）\n"
            "4. 商业模式要点与高风险假设\n"
            "5. MVP 验证计划（含成功判据）\n"
            "6. 转化路径（触达 → 承接 → 成交）\n"
            "7. 若本次涉及运营期：资产沉淀优先级与经营复盘结论\n"
            "8. 下一步行动清单（30 天内可执行，按优先级排序）\n"
            "忽略标注为「不适用」的环节；直接输出方案，不要复述你的处理过程。"
        ),
        "stages": [
            stage("resource", "① 资源盘点", OPC, ["opc-resource-auditor"],
                  task="任务：{task}\n\n盘点创始人的 8 类资源（经验 / 人脉 / 技能 / 关系 / 渠道 / "
                       "资产 / 时间与金钱约束 / 硬性限制）。\n"
                       "用户没有具体交代的部分，**不要只回一句「请补充信息」就结束**——"
                       "按该类资源的常见情况列出「需要自评的要点 + 对应的自评问题」，"
                       "让这份清单本身就能当自评表用，并标注哪些项一旦确认会显著改变后续方向。\n"
                       "只产出资源清单，不做方向分析。",
                  include_prev=False),
            stage("niche", "② 利基定位", OPC, ["opc-niche-strategist"],
                  task="任务：{task}\n\n基于上游资源清单做利基市场定位：市场地图 + 客户细分，"
                       "用「三环合一 + 六维评分」产出多个利基选项并给出推荐。"),
            stage("value", "③ 价值主张", OPC, ["opc-value-designer"],
                  task="任务：{task}\n\n基于上游利基，用 Jobs/Pains/Gains 拆解设计价值主张，"
                       "产出多个选项并给出推荐。"),
            stage("model", "④ 商业模式", OPC, ["opc-model-architect"],
                  task="任务：{task}\n\n基于上游价值主张，用 Lean Canvas 核心模块设计商业模式，"
                       "标出高风险假设与验证优先级。"),
            stage("mvp", "⑤ MVP 实验", OPC, ["opc-mvp-designer"],
                  task="任务：{task}\n\n基于上游商业模式，定义最小验证假设与验证形式，"
                       "产出可执行的 MVP 实验方案（含成功判据）。"),
            stage("convert", "⑥ 转化闭环", OPC, ["opc-conversion-designer"],
                  task="任务：{task}\n\n基于上游 MVP，设计「触达 → 承接 → 成交」转化路径，"
                       "给出可落地的渠道与动作清单。"),
            stage("ops", "⑦ 运营期（按需）", OPC,
                  ["opc-asset-strategist", "opc-dashboard-reviewer"],
                  task="任务：{task}\n\n先判断本次诉求是否属于「运营期」——"
                       "即已经在运营、需要把可复用成果资产化，或需要一次周期性经营复盘。\n"
                       "- **属于**：按你的职责给出方案（资产化优先级 / 经营复盘与瓶颈识别）。\n"
                       "- **不属于**（用户还在建盘期，问的是定位/价值/商业模式/MVP 等）："
                       "只回复一句「本次为建盘期任务，运营期环节不适用」即可，不要展开。\n"
                       "拿不到真实运营数据就说明缺什么，不要编造数字。"),
        ],
    }


def plan_trading() -> dict:
    return {
        "name": "交易分析团队",
        "description": "多角色辩论式交易分析：四路分析师并行取证 → 多空辩论（多头→空头→研究主管裁决）"
                       "→ 交易员决策 → 三方风险论证（激进/保守/中性）→ 风险主管裁决 → 最终交易决策。",
        "type": "pipeline",
        "final": "last",
        "stages": [
            stage("data", "🔍 四路并行取证", TRADING,
                  ["market-analyst", "fundamentals-analyst", "news-analyst", "sentiment-analyst"],
                  task="任务：{task}\n\n从你负责的维度（技术面/基本面/新闻面/情绪面）做独立分析，"
                       "给出关键数据与你所在维度的明确倾向（偏多/偏空/中性）。"
                       "数据必须可核验并标注时间，查不到就说明。",
                  include_prev=False),
            stage("bull", "🐂 多头论证", TRADING, ["bull-researcher"],
                  task="任务：{task}\n\n基于上游四份报告构建有力的买入论证：增长潜力、竞争优势、"
                       "积极信号汇聚、并预判反驳空头风险。对话式、引用具体数据。"),
            stage("bear", "🐻 空头论证", TRADING, ["bear-researcher"],
                  task="任务：{task}\n\n基于上游四份报告与多头论证，构建有力的卖出/回避论证："
                       "直接回应多头的每个核心论点，指出被高估之处与下行风险。"),
            stage("research", "⚖️ 研究主管裁决", TRADING, ["research-manager"],
                  task="任务：{task}\n\n主持多空辩论，权衡双方论据强弱，输出 [投资计划]"
                       "（含明确 BUY/SELL/HOLD 倾向与理由）。"),
            stage("trader", "💼 交易员决策", TRADING, ["trader"],
                  task="任务：{task}\n\n基于 [投资计划] 与四份报告，输出 [交易员决策]："
                       "FINAL TRANSACTION PROPOSAL、入场价、目标价、止损价与建议仓位。"),
            stage("risk3", "🛡️ 三方风险论证（并行）", TRADING,
                  ["aggressive-risk-analyst", "conservative-risk-analyst", "neutral-risk-analyst"],
                  task="任务：{task}\n\n基于 [交易员决策]，从你的风险立场（激进/保守/中性）"
                       "对该决策做一次论证：指出你认可与反对之处、你要求的调整。"),
            stage("risk", "🎯 风险主管裁决", TRADING, ["risk-manager"],
                  task="任务：{task}\n\n综合三方风险论证与 [交易员决策]，输出 [最终交易决策]："
                       "BUY/SELL/HOLD、明确仓位、止损纪律与主要风险提示。"),
        ],
    }


def plan_doc_team() -> dict:
    """专业文档生成团队：4 角色 6 阶段（需求分析 → 检索 → 生成 → 审核 → 修订 → 汇编交付）。"""
    return {
        "name": "专业文档生成团队",
        "description": "企业级长文档生成：需求梳理与知识检索 → 内容生成 → 质量审核 → 修订定稿 → 整合交付。"
                       "适用于施工图设计说明、技术方案、招投标文件、维修手册、API/系统文档等。"
                       "所有无法确定的具体数值统一标 [待填写]，不编造规范编号或条文。",
        "type": "pipeline",
        "final": "lead",
        # 长文档场景：单份正文可能上万字，放宽上游产出的截断与上下文预算
        "stage_output_clip": 24000,
        "stage_context_budget": 64000,
        "lead_final_instruction": (
            "请**直接输出整合后的完整文档**（从封面/标题开始，到免责声明结束）。"
            "不要写任何『以下是…』『我已审阅…』之类的开场白、处理说明或决策结论——"
            "你的输出本身就是交付物。"
        ),
        "lead_prompt": (
            "你是专业文档生成团队的总编辑。基于上游的检索资料、文档正文与审核结论，完成最终整合与交付：\n"
            "1. 封面信息（文档名称 / 版本 / 日期 / 文档类型）+ 目录\n"
            "2. 前言/引言（按文档类型给标准前言）\n"
            "3. 全部章节正文（保留上游已通过审核的专业内容，不要重写）\n"
            "4. 跨章引用检查（「详见第X章」指向是否正确）+ 全文格式统一（标题层级 / 表格样式 / 编号格式）\n"
            "5. 汇总所有 [待填写] 与审核未决问题到末尾「待完善事项」区\n"
            "6. 结尾附免责声明：本文档由 AI 生成，重要决策请经专业人员核验\n"
            "直接输出完整 Markdown 文档，不要复述你的处理过程。"
        ),
        "stages": [
            stage("research", "① 需求梳理与知识检索", DOC, ["doc-researcher"],
                  task="任务：{task}\n\n先判断这份文档的类型/行业/目标大纲（用户没说明就按该类型与行业的惯例"
                       "合理推断并写明你的假设），再为各章节检索可引用的依据：国标/行标条文、历史案例、"
                       "技术参数范围、关联资料。\n"
                       "结构化输出：① 文档类型与章节大纲 ② 每章的检索资料（标注来源与相关度）"
                       "③ 信息缺口清单。\n"
                       "查不到就如实标注缺失，**禁止编造规范编号、条文号或标准名称**。",
                  include_prev=False),
            stage("draft", "② 内容生成", DOC, ["doc-generator"],
                  task="任务：{task}\n\n基于上游的章节大纲与检索资料，撰写文档完整正文。\n"
                       "要求：Markdown 格式；内容立足检索资料而非凭空发挥；规范引用统一写成"
                       "《规范名称》(编号) 第X.X.X条，并区分强制条文（应/必须）与推荐条文（宜/可）；"
                       "无法确定的具体数值统一写 [待填写]，不要留 XX/___ 之类原始占位符；"
                       "同一项目的参数（规模/等级/型号等）跨章必须一致。\n"
                       "**直接输出文档正文全文**，不要写『以下是初稿』之类的开场白或收尾说明。"),
            stage("audit", "③ 质量审核", DOC, ["doc-auditor"],
                  task="任务：{task}\n\n对上游文档做 6 维审核：逻辑一致性（数量/表格是否匹配）、"
                       "规范符合性（条文编号与版本）、参数合理性、内容完整性、格式规范性、跨章一致性。\n"
                       "输出：结论（PASS 或 REVISE）+ 必须修改项清单（指明章节与具体位置）+ 建议优化项。"
                       "发现编造的规范编号或无依据的数据，必须列为必须修改项。"),
            stage("revise", "④ 修订定稿", DOC, ["doc-generator"],
                  task="任务：{task}\n\n按上游审核意见修订，然后**输出修订后的文档全文**"
                       "（从标题到结尾，与初稿同等的完整篇幅，不是 diff、不是修改说明、"
                       "不要只列改了什么）。\n"
                       "未受批评的部分保持原文不变；审核未提及但确实缺依据的地方，"
                       "标 [待填写] 而不是编造。\n"
                       "如果审核结论是 PASS 或没有实质修改意见，就把全文原样输出一遍即可。"),
        ],
    }


def plan_stock_partner() -> dict:
    """腾讯自选股股票投研专家团：6 位专家并行研判 → 投研主编汇编成 4 模块圆桌报告。"""
    plugin = load_plugin(STOCK)
    member_ids = [m["id"] for m in plugin["members"] if m.get("role") != "lead"]
    return {
        "name": "腾讯自选股股票投研专家团",
        "description": "六位投研专家（产业策略 / 信号 / 估值 / 逆向 / 财报 / 短线）并行独立研判，"
                       "投研主编汇编成「圆桌报告」四模块：结论卡 → 子专家观点 → 深度思考 → 后续关注。"
                       "原版依赖腾讯自选股连接器取实时行情，本环境无该连接器时降级联网搜索，"
                       "行情与财务精度会下降（报告会如实标注）。",
        "type": "pipeline",
        "final": "lead",
        "stage_output_clip": 9000,
        "stage_context_budget": 52000,
        "lead_final_instruction": (
            "请**直接输出完整的圆桌报告**（Markdown，从模块1 结论卡开始）。"
            "不要写任何开场白、处理说明或「以下是我的汇编」之类的话——你的输出本身就是报告。"
        ),
        "lead_prompt": (
            "你是腾讯自选股股票投研专家团的投研主编。六位专家已各自给出独立分析，"
            "请汇编成一份「圆桌报告」——它是给读者的多视角答案产品，不是会议纪要。固定 4 个模块：\n\n"
            "模块1 · 结论卡：① YOU ASKED（原样引用用户问题）② 当前关键数据快照（4-8 个最相关的数字，"
            "带涨跌方向）③ 圆桌综合视角（1-2 句完整陈述句，约 50 字，写清整体态度与共识）"
            "④ 核心分析观点（3-5 条，每条 = 视角标签 + 核心论据 + 该视角的独立判断）"
            "⑤ 圆桌立场分布（按问题类型给票数或仓位带分布）\n\n"
            "模块2 · 子专家观点：本次上场的每位专家一张卡（头衔 → 方法论一句话定调 → 关键数据/证据 → "
            "独立结论），保留其核心产物表（产业链卡位 / PEG 分档 / 信号矩阵 / 龙头止损位 / 支撑位分批 / 财报跟踪）\n\n"
            "模块3 · 深度思考：3a 主持人札记（3-5 条，第一人称，吸收成员洞察后用自己的话说）"
            "+ 3b 主持人 Q&A（3-5 条，每条带标签 🔑关键 / ⚠️易混淆 / 🔍易疏忽 / ⭐重要）\n\n"
            "模块4 · 后续关注：4a 关键变量观察表（变量(含当前值) | 重新评估触发线）"
            "+ 4b 综合视角失效条件（3-5 条，带可证伪的具体阈值，聚焦「什么会让我们错」）\n\n"
            "铁律：\n"
            "- **禁止**给出「建议买入/卖出/加仓/减仓/止损/清仓/抄底/追高/X成仓」等指令性结论；"
            "改用「偏多/偏空/观望/分歧」或按持仓状态、风险偏好分组的差异化参考\n"
            "- 引用行情/财务/资金数据必须标注来源（如 WebSearch:\"…\"）；查不到的数字写「待核实」，禁止编造\n"
            "- 保留分歧：方向性分歧放到模块3的 Q&A 展开，不要强行统一\n"
            "- 结尾附免责声明：> ⚠️ 以上内容由 AI 基于公开信息整理生成，仅供参考，不构成任何投资建议或个股推荐。"
            "投资有风险，决策需谨慎。"
        ),
        "stages": [
            stage("roundtable", "🌊 六位专家并行研判", STOCK, member_ids,
                  task="任务：{task}\n\n"
                       "请**只从你负责的研究维度**做独立分析，不替其他专家发声，也不要等别人的结论。\n"
                       "各成员维度：产业策略师=产业趋势/产业链拆解/景气度排序/牛熊判断；"
                       "信号派首席=四层信号对齐/系统性风险/卖出时机/操作纪律；"
                       "估值分析师=PE Bands/PEG 精确定价/核心+卫星仓位/宏观配置；"
                       "逆向投资人=支撑位分批/情绪极值/深套应对/极简框架；"
                       "财报研究员=财报深挖/季度跟踪/管理层动作；"
                       "短线冲浪手=当周主线/集合竞价/龙头识别/技术止损。\n"
                       "取数说明：本环境**没有**腾讯自选股连接器（westock-mcp），"
                       "请用联网搜索获取公开行情/财务数据，引用时标注来源；查不到的数字写「待核实」，"
                       "**禁止编造**。\n"
                       "输出：800-1500 字结构化报告，含你的分析框架、关键数据（带来源）、独立结论。",
                  include_prev=False),
        ],
    }


TEAM_PLANS = {
    A_SHARE: plan_a_share,
    FINANCE: plan_awesome_finance,
    BELIEVE: plan_believe_in_light,
    DD: plan_dd,
    RESEARCH: plan_research,
    PPT: plan_ppt,
    MASTERS: plan_masters,
    MIGRAQ: plan_migraq,
    OPC: plan_opc,
    TRADING: plan_trading,
    DOC: plan_doc_team,
    STOCK: plan_stock_partner,
}


def discover_teams() -> list:
    """源目录下所有像样的专家团队目录（含有 agents/ 的）。"""
    if not os.path.isdir(SRC_DIR):
        return []
    out = []
    for d in sorted(os.listdir(SRC_DIR)):
        if not os.path.isdir(os.path.join(SRC_DIR, d)):
            continue
        if os.path.isdir(os.path.join(team_root(d), "agents")):
            out.append(d)
    return out


def plan_auto(team_dir: str) -> dict:
    """没有专属阶段编排的团队 → 最保守的兜底映射：成员并行分析 + 主理人汇总。

    所有成员的 prompt 原样保留（专家能力不丢），只是阶段编排用最通用的形态；
    若该团队实际是多阶段 SOP，再把它补进 TEAM_PLANS 细化。
    """
    plugin = load_plugin(team_dir)
    name = (plugin.get("displayName") or {}).get("zh") or plugin.get("name") or team_dir
    desc = ((plugin.get("displayDescription") or {}).get("zh")
            or plugin.get("description") or "")
    lead_id = (plugin.get("teamInfo") or {}).get("leadAgent") or plugin.get("agentName")
    ids = [m["id"] for m in (plugin.get("members") or [])
           if isinstance(m, dict) and m.get("id") and m.get("role") != "lead"]
    if not ids:  # plugin.json 没写 members 的（单 agent 专家等）
        ad = os.path.join(team_root(team_dir), "agents")
        if os.path.isdir(ad):
            ids = [f[:-3] for f in sorted(os.listdir(ad))
                   if f.endswith(".md") and f[:-3] != lead_id]
    return {
        "name": name,
        "description": desc,
        "type": "parallel",
        "lead_prompt": (
            "你是本团队的主理人。综合各位成员从各自专业角度给出的独立分析，"
            "输出一份结构化报告：总体结论、支撑要点（引用成员观点）、风险与分歧、明确的建议。"
            "直接输出报告，不要复述你的处理过程。"
        ),
        "members": [member_spec(team_dir, a) for a in ids],
    }


def build_teams() -> list:
    """返回 [(team_dir, spec, is_auto)]；源目录里新出现的团队会自动兜底移植。"""
    out = []
    for d in discover_teams():
        fn = TEAM_PLANS.get(d)
        if fn:
            out.append((d, _annotate(fn(), d), False))
        else:
            out.append((d, _annotate(plan_auto(d), d), True))
    return out


def main():
    if not os.path.isdir(SRC_DIR):
        print(f"找不到源目录：{SRC_DIR}")
        print("源包不一定要放在仓库里，用参数或环境变量指过去即可：")
        print("    python port_experts.py D:/expert-src")
        print("    PH_EXPERT_SRC=D:/expert-src python port_experts.py")
        print("默认会依次找：" + "、".join(DEFAULT_SRC_DIRS))
        sys.exit(1)
    print(f"源目录：{SRC_DIR}")

    print("== 移植技能 ==")
    log = []
    port_skills(log)
    port_team_licenses(log)
    port_believe_in_light_engine(log)
    print("\n".join(log))

    print("\n== 移植团队 ==")
    log2 = []
    specs = build_teams()
    sys.path.insert(0, BASE_DIR)
    validate = None
    try:
        import team as team_mod  # 依赖 agentscope，仅用于校验生成的 spec
        validate = team_mod.validate_team
    except Exception as e:
        print(f"  （跳过校验：无法导入 team 模块 —— {type(e).__name__}: {e}）")
    auto = []
    for team_dir, spec, is_auto in specs:
        if validate is not None:
            err = validate(spec)
            if err:
                print(f"  ✗ 团队「{spec.get('name')}」校验失败：{err}")
                sys.exit(1)
        if is_auto:
            auto.append(spec.get("name"))
        write_team(spec, log2)
    print("\n".join(log2))
    if auto:
        print("\n  ⚠ 以下团队没有专属阶段编排，已按「成员并行 + 主理人汇总」兜底生成：")
        print("    " + "、".join(str(a) for a in auto))
        print("    若它们原本是多阶段 SOP，可把阶段编排补进 port_experts.py 的 TEAM_PLANS 再细化。")
    print("\n完成。团队在 teams/ 下，技能在 skills/ 下。")


if __name__ == "__main__":
    main()
