"""PrismHarness 工具系统。

基于 AgentScope Toolkit 的注册模式：
- 文件操作 (view/write/insert)
- Shell 命令
- 联网搜索
- 技能系统集成
- 定时任务 (可选)
"""

import asyncio
import functools
import json
import os
from collections import defaultdict
from datetime import datetime
from typing import List

from bs4 import BeautifulSoup
from agentscope.tool import Toolkit, ToolResponse
from agentscope.tool import (
    insert_text_file,
    view_text_file,
    write_text_file,
)
from agentscope.message import Msg, TextBlock
from agentscope.model import OpenAIChatModel

from conf import FLAGS


# ---- Shell 工具：显式走 PowerShell 或 Git Bash（Windows 根因修复）----
# agentscope 自带的 execute_shell_command 用 asyncio.create_subprocess_shell，
# 在 Windows 上走 COMSPEC=cmd.exe，而 system prompt 让模型写 PowerShell 语法，
# 两者不匹配 → 命令静默失败、空 stdout → agent 失去方向、卡住不动。
# 这里用 create_subprocess_exec 显式调解释器，语法和 prompt 一致。
# 很多安装脚本是 bash（curl|bash），Windows 上用 Git Bash 跑。

_GIT_BASH = r"C:\Program Files\Git\bin\bash.exe"


async def run_shell(command: str, timeout: int = 120, shell: str = "powershell") -> ToolResponse:
    """执行一条命令并返回 returncode / stdout / stderr。

    运行环境是 Windows。默认走 **PowerShell**；安装脚本等 bash 内容用 shell="bash"（Git Bash）。

    Args:
        command: 命令字符串（按 shell 语法）
        timeout: 超时秒数（默认 120）
        shell: "powershell"（默认）或 "bash"
    """
    command = (command or "").strip()
    if not command:
        return ToolResponse(content=[TextBlock(type="text", text="错误：命令为空，不要调用空命令。")])

    shell = (shell or "powershell").strip().lower()

    # 软提示：记录重复调用，但不阻止执行
    _repeat_note = _note_repeat("run_shell", f"{shell}:{command}")

    if shell == "bash":
        if not os.path.isfile(_GIT_BASH):
            return ToolResponse(content=[TextBlock(type="text", text=(
                f"错误：找不到 Git Bash（{_GIT_BASH}）。本机未安装 Git for Windows，无法执行 bash 脚本。"
            ))])
        argv = [_GIT_BASH, "-lc", command]
    else:
        argv = ["powershell", "-NoProfile", "-NonInteractive", "-Command", command]

    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError:
        return ToolResponse(content=[TextBlock(type="text", text=f"错误：找不到解释器：{argv[0]}。")])

    try:
        # 直接 communicate() 会同时消费 stdout/stderr，避免大输出把管道写满后
        # wait() 和 communicate() 相互等待造成死锁。
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(),
            timeout=timeout,
        )
        stdout_str = stdout.decode("utf-8", "replace")
        stderr_str = stderr.decode("utf-8", "replace")
        returncode = proc.returncode
    except asyncio.TimeoutError:
        returncode = -1
        stdout_str, stderr_str = "", ""
        try:
            proc.terminate()
            try:
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(),
                    timeout=5,
                )
                stdout_str = stdout.decode("utf-8", "replace")
                stderr_str = stderr.decode("utf-8", "replace")
            except asyncio.TimeoutError:
                proc.kill()
                stdout, stderr = await proc.communicate()
                stdout_str = stdout.decode("utf-8", "replace")
                stderr_str = stderr.decode("utf-8", "replace")
        except ProcessLookupError:
            stdout_str, stderr_str = "", ""
        stderr_str = (stderr_str + "\n" if stderr_str else "") + \
            f"TimeoutError: 命令执行超过 {timeout} 秒。"

    text = (
        f"<returncode>{returncode}</returncode>"
        f"<stdout>{stdout_str}</stdout>"
        f"<stderr>{stderr_str}</stderr>"
        f"{_repeat_note}"
    )
    return ToolResponse(content=[TextBlock(type="text", text=text)])

# ---- Prompt 模板 ----

REASONING_HINT_TEMPLATE = """
(内部信息，非用户输入，不要直接引用这段文字)

当前时间：{current_time}

行动准则：
- 涉及实时信息（天气/股价/新闻/知识）→ 调 web_search
- 需要读取某个具体网页/文档/API → 用 web_fetch(url) 抓取正文
- 涉及文件操作 → 先 view_text_file 查看，再 write/insert
- 写入文件或执行Shell → 直接调工具，系统会自动弹确认卡片，你不要自己写"需要确认吗"

不要做的事：
- 未调工具就回答需要实时数据的问题
- 编造工具调用结果
- 同一问题反复调相同工具
- 调高风险工具前自言自语"需要确认"——确认是系统弹卡片的事，你只管调工具
"""

AGENT_SYS_PROMPT_TEMPLATE = """你是 PrismHarness，一个透明、高效的 AI 助手。

## 核心原则
1. **透明度高于一切** — 每次操作都清晰告知用户，包括为什么调用工具、工具返回了什么
2. **效率优先** — 识别无依赖的工具调用，一次性并发执行
3. **安全边界** — 文件写入和 Shell 命令执行由系统自动弹确认卡片，你不需要自己问用户
4. **实事求是** — 不编造数据、不夸大结果

## 工具调用策略
- 并行优先：无依赖的操作必须并发
- 调用前检查参数完整性和合法性
- 组合使用多个工具完成复杂任务
- **避免无意义重复**：同一 URL / query 已经成功获取结果的，优先基于已有结果继续，不要换词再搜同一主题。如果确实因为超时/不完整需要重试，可以再调一次。工具返回结果末尾会附重复提醒。
- **拿到结果就停**：工具返回了有效数据后，**立即停止调用工具**，直接整理回复用户。不要「确认一下」、「再看一遍」——已经有结果了还看什么？
- 工具调用硬上限：单轮最多 20 步，超限自动终止

## 响应风格
- 简洁有力���结论先行
- Markdown 格式输出
- 代码块标注语言

{extra_prompt}

### 📥 下载与安装
- `download_file(url, save_name)` — 下载任意网络文件到 `workspace/downloads/`（受控，会请求确认）。
- `run_shell(command, timeout, shell)` — 执行系统命令（受控，会请求确认）。
- **运行环境是 Windows**，`run_shell` 有两种模式：
  - `shell="powershell"`（默认）— 直接写 PowerShell 语法，不要自己包 `powershell -Command`。
  - `shell="bash"` — 走 Git Bash，用于执行 bash 脚本（如 `curl URL | bash`、`bash install.sh`、`#!/usr/bin/env bash` 开头的文件）。
- **铁规则**：bash 语法（`while [[ ]]`、`$#`、`set -euo pipefail` 等）**绝不**塞进 PowerShell，反之亦然。拿到 `.sh` 脚本 → `run_shell("bash 脚本路径", shell="bash")`。
- **不要发空命令**。`command` 必须是有内容的完整命令。
- **安装前先检查**（重要）：执行任何安装/下载命令前，先检查目标是否已存在（如 `node_modules/` 目录、`pip show`、`npm ls` 等）。已存在且可用的直接告诉用户，不要重复安装。
- **非交互模式**：所有 install/uninstall 命令必须带确认标志，否则会卡住等待输入。`pip install/uninstall` 加 `-y`，`conda install/uninstall` 加 `-y`，`npm install/uninstall` 一般不需要额外标志。

## 人格设定

### IDENTITY.md
{identity_md}

### AGENTS.md
{agents_md}

### SOUL.md
{soul_md}

### USER.md
{user_md}
"""


def load_persona_file(filename: str, workspace_dir: str = "workspace") -> str:
    """加载人格设定文件。"""
    filepath = os.path.join(workspace_dir, filename)
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            return f.read().strip()
    except FileNotFoundError:
        return "(未定义)"


def format_system_prompt(extra_prompts: List[str], workspace_dir: str = "workspace") -> str:
    """生成完整的 System Prompt，注入人格定义 + 技能正文。"""
    identity_md = load_persona_file("IDENTITY.md", workspace_dir)
    agents_md = load_persona_file("AGENTS.md", workspace_dir)
    soul_md = load_persona_file("SOUL.md", workspace_dir)
    user_md = load_persona_file("USER.md", workspace_dir)
    return AGENT_SYS_PROMPT_TEMPLATE.format(
        identity_md=identity_md,
        agents_md=agents_md,
        soul_md=soul_md,
        user_md=user_md,
        extra_prompt="\n".join(extra_prompts),
    )


# ---- Web 搜索 / 实时数据工具 ----

import re


def _extract_text_from_chunk(chunk_content) -> str:
    """从 dsv4 流式 chunk.content 中提取纯文本。

    注意：dsv4 的流式输出中，每个 chunk.content 是**累计全文**
    （不是增量 delta），所以必须只取最后一个 chunk 的完整内容，
    绝不能跨 chunk 拼接，否则会出现 '抱歉抱歉我无法...' 的重复文本。
    """
    if isinstance(chunk_content, list):
        text = ""
        for c in chunk_content:
            if isinstance(c, dict):
                t = c.get("text", "")
                if t:
                    text = t  # 覆盖，保留累计全文
            elif isinstance(c, str):
                text = c
        return text
    elif isinstance(chunk_content, str):
        return chunk_content
    return ""


def _extract_city(query: str) -> str:
    """从查询中提取城市名（中文）。"""
    # 句式：深圳天气 / 查询北京的实时天气 / 上海 今天气温 / 北京
    m = re.search(
        r"(?:查询|问|查一下|帮我查|请问|搜)?\s*"
        r"([^\u7684\u5b9e\u65f6\u5929\u6c14\u6e29\u5ea6\u591a\u5c11\u8863\u7d2b\u5916\u7ebf\u4eca\u660e\u67e5\u95ee\u5e2e\u8bf7\u641c\u5f53\u524d\u600e\u4e48\u5982\u4f55]{2,6})"
        r"\s*(?:的)?\s*(?:实时|今天|现在|当前)?\s*"
        r"(?:天气|气温|温度|多少度|穿衣|紫外线)",
        query,
    )
    if m:
        return m.group(1).strip()
    # 兜底：提取第一个连续中文词（2-6字，排除疑问/动词）
    candidates = re.findall(r"[\u4e00-\u9fa5]{2,6}", query)
    stop = {"今天", "明天", "实时", "查询", "请问", "帮我", "我想", "现在", "当前",
            "情况", "怎么", "如何", "述职", "这个", "那个", "什么"}
    for c in candidates:
        if c not in stop:
            return c.strip()
    return ""


# 英文天气描述 → 中文映射
_WEATHER_ZH = {
    "clear": "晴", "sunny": "晴", "fair": "晴",
    "partly cloudy": "局部多云", "cloudy": "多云", "overcast": "阴",
    "mist": "薄雾", "fog": "雾", "haze": "霾", "smoky haze": "雾霾",
    "light rain": "小雨", "rain": "雨", "heavy rain": "大雨", "showers": "阵雨",
    "thunderstorm": "雷阵雨", "storm": "暴风雨",
    "light snow": "小雪", "snow": "雪", "heavy snow": "大雪",
    "windy": "大风", "blustery": "阵风", "patchy rain possible": "可能有零星小雨",
    "patchy snow possible": "可能有零星小雪", "freezing fog": "冻雾",
}


def _weather_to_zh(en_desc: str) -> str:
    """将英文天气描述转为中文（大小写不敏感匹配）。"""
    if not en_desc:
        return "未知"
    key = en_desc.strip().lower()
    if key in _WEATHER_ZH:
        return _WEATHER_ZH[key]
    # 部分匹配（如 "Patchy light rain" 含 "light rain"）
    for en, zh in _WEATHER_ZH.items():
        if en in key:
            return zh
    return en_desc  # 未命中则返回原文


async def _fetch_weather(city: str) -> str:
    """获取真实天气数据。依次尝试多个免 key 数据源，返回结构化文本。"""
    import aiohttp

    # 数据源 1：wttr.in (JSON, 但 content-type 非标准，需 text+json.loads)
    try:
        async with aiohttp.ClientSession() as session:
            url = f"https://wttr.in/{city}?format=j1"
            async with session.get(
                url,
                headers={"User-Agent": "curl/8.0"},
                timeout=aiohttp.ClientTimeout(total=8),
            ) as resp:
                if resp.status == 200:
                    import json as _json
                    data = _json.loads(await resp.text())
                    cur = data.get("current_condition", [{}])[0]
                    w0 = data.get("weather", [{}])[0]
                    en_desc = cur.get("weatherDesc", [{}])[0].get("value", "")
                    desc = _weather_to_zh(en_desc)
                    line = (
                        f"{city}当前天气：\n"
                        f"- 天气状况：{desc}\n"
                        f"- 当前温度：{cur.get('temp_C')}°C\n"
                        f"- 体感温度：{cur.get('FeelsLikeC')}°C\n"
                        f"- 湿度：{cur.get('humidity')}%\n"
                        f"- 风速：{cur.get('windspeedKmph')} km/h\n"
                        f"- 今日最高/最低：{w0.get('maxtempC')}°C / {w0.get('mintempC')}°C"
                    )
                    return line
    except Exception:
        pass

    # 数据源 2：api.vvhan.com (中文免 key)
    try:
        async with aiohttp.ClientSession() as session:
            url = f"https://api.vvhan.com/api/weather?city={city}"
            async with session.get(
                url,
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=aiohttp.ClientTimeout(total=8),
            ) as resp:
                if resp.status == 200:
                    import json as _json
                    data = _json.loads(await resp.text())
                    if data.get("code") == 1 and data.get("data"):
                        d = data["data"]
                        line = (
                            f"{city}当前天气：\n"
                            f"- 天气状况：{d.get('weath', d.get('weather', '未知'))}\n"
                            f"- 当前温度：{d.get('temp', '未知')}°C\n"
                            f"- 最高/最低：{d.get('temphigh', '?')}°C / {d.get('templow', '?')}°C\n"
                            f"- 风力：{d.get('wind', '未知')}"
                        )
                        return line
    except Exception:
        pass

    return ""


def _html_to_text(html: str) -> str:
    """HTML → 可读纯文本，使用 BeautifulSoup 而不是脆弱的正则。"""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "svg"]):
        tag.decompose()
    text = soup.get_text("\n")
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line).strip()


# ---- 防重复调用（软提示：不阻止，只追加提醒。AgentScope 风格——靠上下文历史让 LLM 自己判断） ----
_call_counts = defaultdict(int)  # (tool_name, param_key) → 次数


def _note_repeat(tool_name: str, param_key: str) -> str:
    """记录调用次数。超过 1 次返回提醒文字（不阻止执行），首次返回空串。"""
    _call_counts[(tool_name, param_key)] += 1
    if _call_counts[(tool_name, param_key)] > 1:
        return f"\n[提醒] 已第 {_call_counts[(tool_name, param_key)] - 1} 次重复调用 {tool_name}，请基于已有上下文判断是否必要。"
    return ""


async def web_fetch(url: str, max_chars: int = 6000) -> ToolResponse:
    """抓取一个网页，返回正文文本（HTML 转纯文本，国内可直连）。

    用于读取文章、文档、API 返回等任意 http/https 页面。
    不依赖任何被墙服务。GitHub 仓库链接自动用 API 取 README。

    Args:
        url: 要抓取的网页地址
        max_chars: 返回正文的最多字符数（默认 6000，防止过长）
    """
    import aiohttp

    url = (url or "").strip()
    if not url or not url.startswith(("http://", "https://")):
        return ToolResponse(content=[TextBlock(type="text", text="错误：请提供合法的 http/https 网址。")])

    # 软提示：记录重复调用，但不阻止执行
    _repeat_note = _note_repeat("web_fetch", url)

    # GitHub 仓库链接 → 用 GitHub API 一次性获取 README
    gh_repo = re.match(r"https?://github\.com/([^/]+)/([^/]+)/?$", url)
    if gh_repo:
        import base64
        owner, repo = gh_repo.group(1), gh_repo.group(2).rstrip("/")
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"https://api.github.com/repos/{owner}/{repo}/readme",
                    headers={"User-Agent": "Mozilla/5.0", "Accept": "application/vnd.github.v3+json"},
                    timeout=aiohttp.ClientTimeout(total=12),
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        readme_text = base64.b64decode(data.get("content", "")).decode("utf-8", errors="ignore")
                        if len(readme_text) > max_chars:
                            readme_text = readme_text[:max_chars] + f"\n\n...(已截断)"
                        return ToolResponse(content=[TextBlock(type="text", text=(
                            f"来源（GitHub API - {owner}/{repo} README）：\n\n{readme_text}{_repeat_note}"
                        ))])
                    return ToolResponse(content=[TextBlock(type="text", text=f"GitHub API 返回 HTTP {resp.status}。{_repeat_note}")])
        except Exception as e:
            return ToolResponse(content=[TextBlock(type="text", text=f"GitHub API 请求失败：{e}")])

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status != 200:
                    return ToolResponse(content=[TextBlock(type="text", text=f"抓取失败：HTTP {resp.status}")])
                raw = await resp.text(errors="ignore")
        text = _html_to_text(raw)
        if len(text) > max_chars:
            text = text[:max_chars] + f"\n\n...(已截断，原文共 {len(text)} 字)"
        return ToolResponse(content=[TextBlock(type="text", text=(
            f"来源：{url}\n\n{text}{_repeat_note}" if text else f"来源：{url}\n\n（页面无可见正文）{_repeat_note}"
        ))])
    except Exception as e:
        return ToolResponse(content=[TextBlock(type="text", text=f"抓取出错：{e}")])


async def _bing_cn_search(query: str) -> str:
    """必应中国搜索（国内可直连，替代被墙的 DuckDuckGo）。"""
    import aiohttp
    from urllib.parse import quote

    try:
        async with aiohttp.ClientSession() as session:
            url = f"https://cn.bing.com/search?q={quote(query)}&count=8"
            async with session.get(
                url,
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36",
                    "Accept-Language": "zh-CN,zh;q=0.9",
                },
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                html = await resp.text(errors="ignore")
        soup = BeautifulSoup(html, "html.parser")
        results = []
        for item in soup.select("li.b_algo")[:6]:
            a = item.select_one("h2 a")
            if not a:
                continue
            link = a.get("href", "")
            title = a.get_text(" ", strip=True)
            # 跳过 bing 内部链接
            if "bing.com/" in link and "/search" in link:
                continue
            if not title:
                continue
            results.append(f"{len(results)+1}. {title}\n   链接: {link}")
        if results:
            return "搜索结果（必应中国）：\n" + "\n".join(results)
    except Exception:
        pass
    return ""


async def _llm_fallback(query: str) -> str:
    """兜底：用 LLM 基于常识回答（流式累积已修复，仅取最后完整文本）。"""
    from model_config import build_model, load_config

    model = build_model(load_config(), stream=True)
    now = datetime.now()
    weekday_map = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
    current_time = now.strftime(f"%Y年%m月%d日 {weekday_map[now.weekday()]} %H:%M")
    prompt = (
        f"当前时间：{current_time}\n"
        f"请基于你的知识简洁回答（不知道就直说不知道，不要编造精确数字）：{query}\n"
        f"回答不超过80字。"
    )
    response = await model([{"role": "user", "content": prompt}])
    last_text = ""
    async for chunk in response:
        t = _extract_text_from_chunk(chunk.content)
        if t:
            last_text = t  # 累计全文，最后一个 chunk 即完整内容
    return last_text.strip() or "（未能获取相关信息）"


async def web_search(query: str) -> ToolResponse:
    """联网搜索 / 实时数据获取（国内可用，不走 DuckDuckGo）。

    - 天气类 → 直连天气 API 获取真实数据
    - 其他 → 必应中国搜索（cn.bing.com，国内可直连）
    - 均失败 → LLM 常识兜底（如实标注非实时）

    Args:
        query: 搜索关键词或问题
    """
    query = (query or "").strip()
    if not query:
        return ToolResponse(content=[TextBlock(type="text", text="错误：搜索词为空。")])

    # 软提示：记录重复调用，但不阻止执行
    _repeat_note = _note_repeat("web_search", query)

    # 1) 天气类 → 直连天气 API（真实数据）
    if re.search(r"天气|气温|温度|weather|temperature|多少度|穿衣|紫外线", query, re.I):
        city = _extract_city(query)
        if city:
            weather = await _fetch_weather(city)
            if weather:
                return ToolResponse(content=[TextBlock(type="text", text=weather + _repeat_note)])

    # 2) 通用搜索 → 必应中国（国内可直连）
    results = await _bing_cn_search(query)
    if results:
        return ToolResponse(content=[TextBlock(type="text", text=results + _repeat_note)])

    # 3) 兜底：LLM 常识
    answer = await _llm_fallback(query)
    return ToolResponse(content=[TextBlock(type="text", text=answer + _repeat_note)])


# ---- 技能安装边界说明（诚实告知，不假装能装市场技能） ----

SKILL_INSTALL_INFO = """关于「安装技能」的处理边界（请严格遵守）：

1. **从 WorkBuddy 推荐市场安装技能**：本 Agent 运行在本地 server，没有直接调用市场安装接口/鉴权的权限，**无法替你完成市场安装**。正确做法是诚实告诉用户：请在 WorkBuddy 客户端左侧「技能」面板（或「技能管理」）中搜索并点击安装；你可以先用 manage_skill(action="list") 列出本地已装技能，避免重复。
2. **在本地新建一个技能（L2 流程指南）**：可以调用 manage_skill(action="create", name=..., description=..., content=...)，在本工作区 `workspace/skills/<名称>/SKILL.md` 生成一份技能定义，立即生效、无需联网、无需批准。
3. **千万不要**：把"装 skill"误当成需要联网搜索的任务去调 web_search（DuckDuckGo 在国内不可用会失败，且容易产生幻觉编造"已安装"）。遇到不确定，优先用 manage_skill 查询或说明边界。"""

SKILL_TEMPLATE = """---
name: {name}
description: {description}
---

# {name}

（简要说明：这个技能做什么、何时该用它、执行步骤是什么。）

## 何时使用
- 当用户需求匹配以下场景时启用：...

## 执行流程
1. ...
2. ...
3. ...

## 注意事项
- 保持透明：调用任何工具前告知用户意图
- 不编造数据，失败则诚实说明
"""


async def manage_skill(
    action: str,
    name: str = "",
    description: str = "",
    content: str = "",
    workspace_dir: str = "workspace",
) -> ToolResponse:
    """技能管理工具（PrismHarness 原生，用于化解"装 skill"类未知请求）。

    支持三种动作：
    - list:   列出 workspace/skills 下已安装的本地技能（避免重复安装）
    - create: 在 workspace/skills/<name>/SKILL.md 新建一个本地技能，立即生效
    - info:   说明「从推荐市场安装技能」的正确方式（需在 WorkBuddy 客户端完成）

    Args:
        action: 动作，取值 list | create | info
        name: 技能名（create 时必填，仅英文/数字/下划线/连字符，如 my-helper）
        description: 技能一句话描述（create 时建议填写）
        content: 技能正文（create 时的 SKILL.md 内容；留空则按模板生成）
    """
    action = (action or "").strip().lower()
    skills_dir = os.path.join(workspace_dir, "skills")

    if action == "list":
        if not os.path.isdir(skills_dir):
            return ToolResponse(content=[TextBlock(type="text", text="当前没有已安装的本地技能。")])
            return
        items = []
        for d in sorted(os.listdir(skills_dir)):
            p = os.path.join(skills_dir, d)
            if os.path.isdir(p) and os.path.isfile(os.path.join(p, "SKILL.md")):
                items.append(f"- {d}")
        if items:
            return ToolResponse(content=[TextBlock(type="text", text=(
                "已安装的本地技能：\n" + "\n".join(items)
            ))])
        else:
            return ToolResponse(content=[TextBlock(type="text", text="workspace/skills 下暂无技能目录。")])
        return

    if action == "info":
        return ToolResponse(content=[TextBlock(type="text", text=SKILL_INSTALL_INFO)])
        return

    if action == "create":
        if not name:
            return ToolResponse(content=[TextBlock(type="text", text="错误：create 动作需要提供 name（技能名，仅英文/数字/下划线/连字符）。")])
            return
        if not re.match(r"^[a-zA-Z0-9_-]+$", name):
            return ToolResponse(content=[TextBlock(type="text", text="错误：技能名只能包含字母、数字、下划线和连字符。")])
            return
        target = os.path.join(skills_dir, name)
        os.makedirs(target, exist_ok=True)
        skill_md = os.path.join(target, "SKILL.md")
        if not content.strip():
            content = SKILL_TEMPLATE.format(name=name, description=description or name)
        try:
            with open(skill_md, "w", encoding="utf-8") as f:
                f.write(content)
            return ToolResponse(content=[TextBlock(type="text", text=(
                f"✅ 已在 {skill_md} 创建技能「{name}」。\n"
                "下次对话该技能会被自动加载到 System Prompt（无需重启 server）。"
            ))])
        except Exception as e:
            return ToolResponse(content=[TextBlock(type="text", text=f"创建技能失败：{e}")])
        return

    return ToolResponse(content=[TextBlock(type="text", text=f"未知 action: {action}。支持 list / create / info。")])


async def download_file(
    url: str,
    save_name: str = "",
    workspace_dir: str = "workspace",
) -> ToolResponse:
    """下载一个文件到工作区 workspace/downloads/ 目录（受 HITL 确认保护）。

    用于从网络获取资源（如图片、文档、技能文件等）。
    文件名非法时自动清洗；不覆盖同名文件（先报存在）。

    Args:
        url: 文件下载地址（http/https）
        save_name: 保存文件名（留空则从 URL 推断）
    """
    import aiohttp

    url = (url or "").strip()
    if not url or not url.startswith(("http://", "https://")):
        return ToolResponse(content=[TextBlock(type="text", text="错误：请提供合法的 http/https 下载地址。")])
        return

    # 推断文件名
    if not save_name:
        save_name = url.rstrip("/").split("?")[0].split("#")[0].split("/")[-1]
    save_name = re.sub(r'[\\/:*?"<>|]', "_", save_name) or "download.bin"

    save_dir = os.path.join(workspace_dir, "downloads")
    os.makedirs(save_dir, exist_ok=True)
    save_path = os.path.join(save_dir, save_name)
    if os.path.exists(save_path):
        return ToolResponse(content=[TextBlock(type="text", text=f"文件已存在：{save_path}（未覆盖）。")])
        return

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                url, headers={"User-Agent": "Mozilla/5.0"}, timeout=aiohttp.ClientTimeout(total=30)
            ) as resp:
                if resp.status != 200:
                    return ToolResponse(content=[TextBlock(type="text", text=f"下载失败：HTTP {resp.status}。")])
                    return
                size = 0
                with open(save_path, "wb") as f:
                    async for chunk in resp.content.iter_chunked(8192):
                        f.write(chunk)
                        size += len(chunk)
                return ToolResponse(content=[TextBlock(type="text", text=(
                    f"✅ 已下载到 {save_path}（{size} 字节）。"
                ))])
    except Exception as e:
        # 清理半成品
        try:
            os.remove(save_path)
        except Exception:
            pass
        return ToolResponse(content=[TextBlock(type="text", text=f"下载出错：{e}")])


# install_skill_from_url 已删除——用户反馈"思路不对"。
# 正确流程：web_fetch 拿到安装文档 → 照着文档用 run_shell 真正执行安装命令。


# ---- 技能注册 ----

async def build_toolkit(workspace_dir: str = "workspace") -> Toolkit:
    """构建 Agent 工具包。"""

    toolkit = Toolkit(
        agent_skill_instruction="""# Skills 使用指南

Skills 是预定义的 SOP 流程，存放在 `workspace/skills/` 目录下。

## 使用流程
1. 根据 skill 的 name/description 判断是否需要使用
2. 使用 view_text_file 读取 skill 目录下的 SKILL.md 了解详细指令
3. 按照 SKILL.md 的流程执行具体步骤

## 重要
- Skill 不是 tool，是流程指南
- 具体操作需要通过 tool 完成
- 每个 skill 有独立目录，包含 SKILL.md 和相关文件
""",
        agent_skill_template="- name: {name}  dir: {dir}  desc: {description}",
    )

    # 注册技能（L1 元数据）
    skills_dir = os.path.join(workspace_dir, "skills")
    if os.path.isdir(skills_dir):
        for skill_name in os.listdir(skills_dir):
            skill_path = os.path.join(skills_dir, skill_name)
            if os.path.isdir(skill_path):
                try:
                    toolkit.register_agent_skill(skill_path)
                except Exception:
                    pass

    # 注册内置工具
    if FLAGS.get("enable_view_text_file", True):
        toolkit.register_tool_function(view_text_file)
    if FLAGS.get("enable_write_text_file", True):
        toolkit.register_tool_function(write_text_file)
    if FLAGS.get("enable_insert_text_file", True):
        toolkit.register_tool_function(insert_text_file)
    if FLAGS.get("enable_execute_shell_command", True):
        toolkit.register_tool_function(run_shell)

    # Web 搜索（必应中国，国内可用）+ 网页抓取
    if FLAGS.get("enable_websearch", True):
        toolkit.register_tool_function(web_search)
        toolkit.register_tool_function(web_fetch)

    # 技能管理（化解"装 skill"类未知请求；list/create/info 均为安全动作，不走 HITL 确认）
    if FLAGS.get("enable_skills", True):
        toolkit.register_tool_function(functools.partial(manage_skill, workspace_dir=workspace_dir))

    # 下载 + 从 URL 安装技能（受控文件访问，走 HITL 确认）
    if FLAGS.get("enable_download", True):
        toolkit.register_tool_function(
            functools.partial(download_file, workspace_dir=workspace_dir),
        )

    return toolkit
