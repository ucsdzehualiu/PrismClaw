# PrismHarness

> 一个基于 **AgentScope 2.0** 框架构建的透明 AI Agent。
> 核心能力：**每轮上下文可视化**（看清模型这一轮“看到了什么”）+ **HITL 工具确认**（写文件 / 跑命令前必须人工确认）+ **实时流式与可打断**（当前在想什么、调什么工具、随时中断）。

## 功能特性

- **上下文透明化**：每一轮对话，把模型实际看到的 System Prompt 分块、Token 用量、历史消息、工具调用、以及完整 Prompt 全部结构化呈现，前端提供“完整 Prompt 检视器”。
- **HITL 工具确认**：写文件 / 插入文件 / 执行命令 / 下载文件 等高风险操作，执行前弹确认卡片，你 `/approve` 才真正运行；超时自动拒绝，避免进程挂起。
- **实时流式 + 可打断**：基于 FastAPI + SSE，文本 / 工具调用 / 结果实时流式回传；界面停止按钮或 `/stop` 随时中断生成。
- **内置工具集**：命令执行（PowerShell / Git Bash）、网页抓取（含 GitHub README 自动解析）、联网搜索（国内可用，天气 / 必应兜底）、技能管理、文件下载。
- **多轮上下文真实累积 + 自动压缩**：单会话复用同一 Agent 实例，上下文持续累积；超过模型窗口阈值（`llm.context_size × compress_threshold`）时，框架自动把旧历史压成摘要，面板可见。
- **人格系统**：通过 `workspace/` 下的 AGENTS / SOUL / USER / IDENTITY 文件定制 Agent 人格，可在界面读取与更新。
- **会话透明日志**：每轮生成可读 Markdown + 结构化 JSON 快照，落盘到 `session_logs/`。
- **团队编排（多智能体）**：把任务分发给多名带独立人格/提示词的成员，按**阶段**执行后汇总成报告。三种类型：**并行**（成员同时分析 → lead 汇总）、**圆桌**（分析 → 风控 → 决策）、**多阶段 pipeline**（自定义阶段序列，阶段内并行、阶段间传递产出，`final:"last"` 末阶段即交付物 / `final:"lead"` 再由主理人汇总）。点输入框左侧「🎭 团队」选一个团队即可运行（团队菜单是浮层，选中后**复用同一个输入框**填任务，回车即跑），或发 `/team <团队名> <任务>`。项目自带 13 套团队（其中 12 套从外部专家团移植，见 [`PORTING.md`](PORTING.md)），团队配置放 `teams/`（内置）或 `workspace/teams/`（自建，同名覆盖内置），结果落盘到 `session_logs/<sid>/teams/run_<时间>/`。

## 快速开始

> 📖 **第一次用先看 [`USAGE.md`](USAGE.md)** —— 启动、怎么用团队、怎么跑测试、常见问题，都在那一份里。

### 1. 配置 LLM

```bash
# 从模板创建配置文件
cp config.example.yaml config.yaml

# 编辑 config.yaml，填入你的 API 信息
# 目前支持 OpenAI 兼容接口，示例：
#   - 阿里云百炼：api_base 填 maas 地址，model 填 qwen3.6-35b-a3b
#   - 本地 vLLM：api_base 填内网地址，model 填你的模型名
```

**config.yaml 不会被提交到 Git**（已在 .gitignore 中），仓库只保留 `config.example.yaml` 模板。

### 2. 启动

> **环境要求**：AgentScope 2.0 需要 **Python ≥ 3.11**（Windows 上用 conda 环境 `prism2`；`launch.bat` 会自动激活，找不到时回落到旧的 `ai-course`）。

```bash
# 方式一：双击启动（推荐，Windows）
双击 launch.bat
# 自动：激活环境 → 清理旧进程 → 清 __pycache__ → 起服务 → 端口就绪后开浏览器

# 方式二：手动
pip install -r requirements.txt
python server.py
# 浏览器打开 http://localhost:8765
```

**打断 / 确认**：
- 界面右上角 **停止按钮** 或 `GET /stop` → 中断当前生成。
- 高风险工具调用会弹出 **允许 / 拒绝** 卡片；也可在输入框发 `/approve`、`/reject`。

## 架构总览

```
浏览器 (index.html) ──SSE──► FastAPI 服务 (server.py)
                                           │
                                           ▼
                              agent_runner() 每会话一个后台循环
                              (prism_harness_agent.py)
                                ├─ AgentScope Agent（ReAct 循环 + 中间件）
                                │   ├─ on_system_prompt 中间件（动态注入 persona/技能）
                                │   └─ 权限引擎（受保护工具注册期打 ASK → 停泊 / 续跑）
                                ├─ build_toolkit() 工具包 (tools.py)
                                │   └─ 受保护工具：主代理 ASK（停泊/续跑）；执行单元 ALLOW
                                ├─ run_team_stream() 团队编排 (team.py)
                                │   ├─ 并行团队：成员并行分析 → lead 汇总
                                │   ├─ 圆桌 roundtable：分析 → 风控 → 决策
                                │   └─ pipeline：按 stages 逐阶段执行（阶段内并行，阶段间传产出）
                                └─ ContextViz 上下文快照引擎 (context_viz.py)
                                       │              │              │
                                  Session 管理    config.yaml     workspace/
                                  (session.py)   model_config.py  (人格/技能/团队)
```

**一轮对话数据流**：
1. 浏览器发 `POST /chat`，server 把请求塞进 `Session` 的请求队列。
2. `agent_runner` 取出请求，交给 `PrismHarnessAgent` 跑 ReAct 循环。
3. 每一步 AgentScope 产出的消息（文本 / 工具调用 / 工具结果）被 `streaming()` 捕获，转成 SSE 事件推回浏览器。
4. 遇到高风险工具，2.0 权限引擎判 ASK → agent 停泊（本轮结束）、弹确认卡片；你 `/approve` 用**新的一轮**续跑，才真正执行。
5. 每轮结束，`ContextViz` 生成上下文快照（JSON + Markdown）落盘到 `session_logs/`。

### 三个设计要点

- **语义交给模型，确定性交给代码。** 意图理解靠 System Prompt + 技能描述（`SKILL.md` 的 `description` 写成触发条件）让模型自路由；而权限、编排、打断这些「必须正确」的事一律用代码兜底，不过模型。所以没有独立的意图分类器——那会引入一个新的失败点。
- **HITL 是声明式的，不是拦截式的。** 受保护工具在**注册期**打 `PermissionDecision(ASK)`，权限是数据；停泊/续跑用框架的一等公民事件协议（`RequireUserConfirmEvent` / `UserConfirmResultEvent`），不覆写私有方法、不伪造 tool_result。
- **编排要确定性、要可测。** 12 套外部专家团是固定 SOP，所以自研 `team.py` 按 `stages` 确定性驱动，而不是用框架里「leader 临场决定派谁」的 `TeamPipeline`——代价是少了临场弹性，换来的是**能脱开 LLM 做 150 项回归测试**。

## 文件说明

### 启动与配置
- `launch.bat` — Windows 启动器，清理上一轮残留后再启动，端口就绪后自动开浏览器。
- `cleanup.py` — 清理脚本，杀掉 8765 端口旧进程 + 删 `__pycache__`。
- `server.out.log` — server 进程的 stdout/stderr（启动时自动改道到这里，见「抗卡死」）。
- `config.example.yaml` — 配置文件模板，复制为 `config.yaml` 后填入你的 API Key。
- `config.yaml` — 运行配置（**不提交到 Git**，含 API Key）。
- `conf.py` — 功能开关；另含 `GUARD_TOOLS` / `GUARD_TIMEOUT`（现仅供设置页展示，实际权限在 `tools.py` **注册期**声明）。
- `model_config.py` — 基于 AgentScope 2.0 `OpenAIChatModel` 构建模型实例（credential / parameters / extra_body 三段式，支持多 provider 切换）。
- `requirements.txt` — Python 依赖。

### 核心模块
- `prism_harness_agent.py` — 2.0 `Agent` 的构造与每会话后台运行循环 `agent_runner()`：把 `reply_stream` 事件流映射回前端既有 SSE 信封，负责心跳、工具调用按 id 归并、magic 命令（`/approve` `/reject` `/team`）与 **HITL 停泊 / 续跑**（`_stream_one_reply` / `_build_resume_inputs`）。
- `tools.py` — 工具系统：基于 AgentScope `Toolkit` 注册全部内置工具（命令执行 / 网页抓取 / 联网搜索 / 技能管理 / 文件下载）。受保护工具（写文件 / 插文本 / 跑命令 / 下载）在**注册期**打 `PermissionDecision`：**主代理**（`interactive=True`，有前端可确认）打 `ASK`；**执行单元**（`interactive=False`：团队成员 / lead / 风控 / 决策 / 子代理）打 `ALLOW`——它们没有确认通道，打 ASK 会让代理一调工具就停泊（只读工具 `is_read_only=True` 由引擎自动放行）。另负责生成系统提示词。
- `context_viz.py` — 上下文可视化引擎（`ContextViz`），每轮生成结构化上下文快照供前端透明化展示与落盘。
- `session.py` — 会话管理（`Session` / `SessionManager`），请求队列、HITL 停泊队列、超时回收。
  > 1.0 的 `prism_harness_guard.py`（覆写 `_reasoning` / `_acting` + 假 tool_result）已随 2.0 迁移**删除**，HITL 改由框架权限引擎承担。
- `server.py` — FastAPI 服务，提供 Web 界面与 SSE 流式对话接口。
- `index.html` — Web 界面（三栏：对话 / 上下文光谱 / 工具时间线）。
- `team.py` — 团队编排（`run_team_stream`）：并行 / 圆桌 / 多阶段 pipeline 三种类型，含打断重试、上下文累积与增量落盘。成员的产出若是框架预设文案（被打断 / 权限停泊）会判为**失败**而不是有效信号，绝不喂给下游的风控 / 决策 / 主理人。
- `port_experts.py` — 移植工具：把 `专家团/` 下外部专家团队生成为 `teams/` + `skills/`（见 [`PORTING.md`](PORTING.md)）。
- `tests/test_pipeline.py` — 团队编排的确定性回归测试（不调 LLM，验证阶段顺序/并行/上下文传递/容错/落盘）。
- `tests/test_console_widget.js` — 团队面板渲染的无头回归测试（Node，抽 index.html 的代码段 + DOM 桩，验证面板 HTML）。
- `tests/test_markdown.js` — markdown 渲染器的无头回归测试（Node，表格 / 标题 / 引用 / 分隔线 / 代码围栏 / 转义）。
- `tests/test_report_card.js` — 报告卡片的无头回归测试（Node，产出标记识别、正文清理、标题取名）。
- `tests/test_stage_report.js` — 阶段产出卡片（圆桌的风控评估）的无头回归测试（Node，含事件路由）。
- `tests/test_console_browser.js` — 真实浏览器冒烟（CDP 驱动本机 Chrome/Edge，抓 console 报错并在真 DOM 里验证面板）。加 `PH_LIVE=1` 会真的点「🎭 团队」跑一次（会调模型）。

```bash
python tests/test_pipeline.py                     # 编排逻辑（免费）
node tests/test_console_widget.js                 # 面板渲染（免费）
node tests/test_markdown.js                       # markdown 渲染（免费）
node tests/test_report_card.js                    # 报告卡片（免费）
node tests/test_stage_report.js                   # 阶段产出卡片（免费）
node tests/test_console_browser.js                # 真实浏览器（免费，需本机有 Chrome/Edge）
PH_LIVE=1 node tests/test_console_browser.js      # 真实浏览器 + 真跑一次团队（调模型）

python tests/smoke2.py                            # 导入 / 工具包 / Agent 构造 / 权限上下文（免费）
python tests/smoke_live.py                        # 真实 LLM 单轮 HITL 确认→执行（需模型）
python tests/smoke_team_live.py                   # 真实 LLM 团队端到端（需模型）
python tests/smoke_team_tools_live.py             # 真实 LLM：成员真的能跑工具、不因权限停泊（需模型）
python tests/smoke_server.py                      # FastAPI + SSE /approve 续跑（需 server 在跑）
```

### 工作区
- `workspace/` — Agent 人格文件（AGENTS / SOUL / USER / IDENTITY）、用户技能目录（skills/）与自建团队（teams/）。
- `teams/` — 仓库级内置团队配置（随仓库分发，`workspace/teams/` 同名覆盖）。
- `skills/` — 仓库级共享技能目录（随仓库分发，`workspace/skills/` 同名覆盖）。

## 核心机制

### HITL 工具确认（AgentScope 2.0 权限引擎）
1. 受保护工具在 `tools.py` 注册时打 `PermissionDecision(behavior=ASK)`；只读工具标 `is_read_only=True`，引擎自动放行、不打扰用户。
2. 模型要调它 → 权限引擎判 ASK → agent yield `RequireUserConfirmEvent` 并**停泊（本轮结束）**，确认卡片（🔒 + 参数）推给前端。
3. 你 `/approve` → 走**新的一轮** `/chat`，`_build_resume_inputs` 构造 `UserConfirmResultEvent` 续跑；批准则真正执行、拒绝则注入 DENIED 结果。
4. `/approve_all` 置 `auto_approve`，本轮内自动放行；批准可带 `suggested_rules` 实现「以后都允许」。
5. 停泊**不占协程**（不是挂起等待），所以不会死锁；`GUARD_TIMEOUT` 仅作兜底。
6. **ASK 只发给主代理。** 团队成员 / lead / 风控 / 决策 / 子代理走 `build_toolkit(interactive=False)`，那里受保护工具打 `ALLOW`。它们没有可确认的用户——一旦打成 ASK，代理会在 `_next_action` 停泊并把这句当回复返回：`I'm waiting for your permission or the external execution to finish.`（status 仍是 ok）。2026-10-15 的「19 位大师全部返回同一句英文」就是这个原因；`team.py` 现在也把这句话判为失败而不是成员信号。

> 结构化提问 `ask_user_question`（= 2.0 内置 `AskUser` 改名）走另一条通道：`RequireExternalExecutionEvent` → 前端弹选择题 → `/answer` 续跑。

### 防死循环（多重保险）
- 只读工具标 `is_read_only=True`，由权限引擎直接放行、结果正常回传，避免反复打扰用户。
- 同一 URL / query 的工具调用有次数上限，禁止重复。
- 单轮 ReAct 迭代硬上限（`max_iters`，config.yaml 默认 30），到顶强制停。
- 系统提示中明确“拿到结果就停、不重复调用”等铁规则。

### 抗卡死（2026-10-16 补）
- **server 输出改道文件。** `python server.py` 启动时会把本进程 fd 1/2 重定向到 `server.out.log`。原因：Windows 控制台一旦进入「快速编辑」选中态，向它 `write` 就会**阻塞**；而 `console()` 与第三方库的 `logging` 都是在**事件循环线程**上直接写 stdout/stderr 的，于是事件循环永久卡死在 `write()` 里——TCP 能连、拿不到任何 HTTP 响应，界面表现就是「一直不动」。改道文件后控制台状态再也拖不垮服务。
- **技能目录先过滤再交给框架。** `build_toolkit` 只把带 `name`+`description` frontmatter 的合法技能交给 AgentScope，并按**声明的技能名**去重。否则 skills/ 里混着的任务笔记、同名技能会让加载器每次 build 都刷 warning（19 人团队 = 20+ 次），既吵又正是上面那个卡死的触发源。
- **瞬时故障退避重试。** 成员撞上端点限流（如 ModelArts `429 ... rate limit is 4 per second`）会退避 2s/4s/8s（带抖动）重建代理重试，而不是直接把整份分析判死；非瞬时异常（配置错等）立即抛出，不浪费重试。注意：成员自己的 `member_timeout` 预算耗尽**不**重试——那会变成静默再等 10 分钟，正是「看起来卡住」的观感。
- **团队路径也有心跳。** 每 10s 推一条 `phase: heartbeat`，带上「已完成 N，失败 M」，成员跑几分钟也不会看起来像死了。

### 流式处理
- 2.0 用类型化事件流（`ReplyStartEvent` / `TextBlockDeltaEvent` / `ToolCallEndEvent` / `ToolResultEndEvent`…），由 `prism_harness_agent.py` 映射回前端既有 SSE 信封。
- 工具调用按 `tool_call_id` 归并：增量 delta 累积，到 `ToolCallEndEvent` 才 emit 一次。
- 每 7 秒推一条心跳，绝不静默（团队编排路径每 10s 一条，带进度计数）。

### 上下文压缩（框架自带）
- 真正的容量上限是 **`llm.context_size`（模型上下文窗口）**。当 token 数超过 `context_size × context.compress_threshold`（默认 0.8）时，AgentScope 2.0 会在**每次推理前**自动把较早的历史压成一份结构化摘要（写进 `AgentState.summary`），只保留最近一段原文；不依赖模型"自觉"。
- 压缩结果会出现在下一轮的**上下文面板**（「⚠️ 本轮已触发上下文压缩」+ 摘要全文），并写进 `session_logs/` 的快照 Markdown。
- ⚠️ 想提升容量要改 **`llm.context_size`**（设置页可改），**不是** `context.token_budget`——后者只是面板的展示兜底值。
- 冒烟：`python tests/smoke_compress_live.py [provider]`（把窗口调小 → 发长消息 → 断言压缩触发且快照如实上报）。

## HTTP API

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/` | Web 界面 |
| POST | `/chat` | `{"session_id", "content":[{"type":"text","text":"..."}]}` → SSE 流式 |
| GET | `/stop?session_id=&request_id=` | 打断当前生成 |
| GET | `/get_personas` | 读 AGENTS / SOUL / USER |
| POST | `/update_persona` | `{"target":"soul","content":"..."}` 改人格文件 |
| GET · POST | `/api/config` | 读 / 写可编辑配置子集（白名单字段），写后热重载 |
| GET | `/api/status` | 配置概览 + 环境 / 依赖检测 |
| GET · POST | `/api/flags` | 读 / 写功能开关 |
| POST | `/api/test_model` | 发一个最小请求，验证 api_base / key / model 连通 |
| GET | `/api/logs` · `/api/log?session=&file=` | 列 / 读会话日志 |
| GET | `/api/session/transcript?session=` | 从快照重建对话记录 |
| GET | `/api/teams` | 列出所有已保存团队（slug / name / 成员数） |
| GET | `/api/teams/{slug}` | 读取指定团队 JSON 配置
| POST | `/api/teams/{slug}` | 创建/更新团队（body 为团队 JSON）
| DELETE | `/api/teams/{slug}` | 删除团队

**SSE 事件类型**：`text` / `tool_use` / `tool_result` / `status`（thinking / acting / confirm / heartbeat / done / error / max_iters）/ `context_view` / `cancel` / `error` / `team`（阶段进度 + 成员摘要 + 报告卡片）。

## 已知限制 / 路线图

- `enable_subagent` / `enable_team` / `enable_skills` / `enable_skill_auto_discover` 均已接线；定时任务（cron）与沙箱隔离（sandbox）尚未实现，已从功能开关列表移除。
- 上下文压缩已接入（由 AgentScope 2.0 框架自动执行，见上「上下文压缩」）；容量上限以 `llm.context_size` 为准。
- RAG / MCP / 长期记忆 / tracing 等高级能力尚未接入，待后续扩展。

## 调试

- 手动测一轮：
  ```bash
  curl -N -X POST http://localhost:8765/chat \
    -H "Content-Type: application/json" \
    -d '{"session_id":"debug","content":[{"type":"text","text":"查看本地已安装的技能"}]}'
  ```
- 改了 `.py` 后直接双击 `launch.bat`（自动清 `__pycache__` + 杀旧进程）。
