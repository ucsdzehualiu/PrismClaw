# PrismHarness

> 一个基于 **AgentScope** 框架构建的透明 AI Agent。
> 核心能力：**每轮上下文可视化**（看清模型这一轮“看到了什么”）+ **HITL 工具确认**（写文件 / 跑命令前必须人工确认）+ **实时流式与可打断**（当前在想什么、调什么工具、随时中断）。

## 功能特性

- **上下文透明化**：每一轮对话，把模型实际看到的 System Prompt 分块、Token 用量、历史消息、工具调用、以及完整 Prompt 全部结构化呈现，前端提供“完整 Prompt 检视器”。
- **HITL 工具确认**：写文件 / 插入文件 / 执行命令 / 下载文件 等高风险操作，执行前弹确认卡片，你 `/approve` 才真正运行；超时自动拒绝，避免进程挂起。
- **实时流式 + 可打断**：基于 FastAPI + SSE，文本 / 工具调用 / 结果实时流式回传；界面停止按钮或 `/stop` 随时中断生成。
- **内置工具集**：命令执行（PowerShell / Git Bash）、网页抓取（含 GitHub README 自动解析）、联网搜索（国内可用，天气 / 必应兜底）、技能管理、文件下载。
- **多轮上下文真实累积**：单会话复用同一 Agent 实例，多轮对话上下文持续累积。
- **人格系统**：通过 `workspace/` 下的 AGENTS / SOUL / USER / IDENTITY 文件定制 Agent 人格，可在界面读取与更新。
- **会话透明日志**：每轮生成可读 Markdown + 结构化 JSON 快照，落盘到 `session_logs/`。

## 快速开始

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

```bash
# 方式一：双击启动（推荐，Windows）
双击 launch.bat
# 自动：清理旧进程 → 清 __pycache__ → 起服务 → 端口就绪后开浏览器

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
                            ├─ PrismHarnessAgent
                            │   = PrismHarnessGuardMixin + ReActAgent (AgentScope)
                            │     ├─ _reasoning (注入提示)
                            │     └─ _acting  (HITL 拦截)
                            │        (prism_harness_guard.py)
                            ├─ build_toolkit() 工具包 (tools.py)
                            └─ ContextViz 上下文快照引擎 (context_viz.py)
                                   │              │              │
                              Session 管理    config.yaml     workspace/
                              (session.py)   model_config.py  (人格/技能)
```

**一轮对话数据流**：
1. 浏览器发 `POST /chat`，server 把请求塞进 `Session` 的请求队列。
2. `agent_runner` 取出请求，交给 `PrismHarnessAgent` 跑 ReAct 循环。
3. 每一步 AgentScope 产出的消息（文本 / 工具调用 / 工具结果）被 `streaming()` 捕获，转成 SSE 事件推回浏览器。
4. 遇到高风险工具，`prism_harness_guard` 拦截、入队 pending、弹确认卡片；你 `/approve` 后下一轮才真正执行。
5. 每轮结束，`ContextViz` 生成上下文快照（JSON + Markdown）落盘到 `session_logs/`。

## 文件说明

### 启动与配置
- `launch.bat` — Windows 启动器，清理上一轮残留后再启动，端口就绪后自动开浏览器。
- `cleanup.py` — 清理脚本，杀掉 8765 端口旧进程 + 删 `__pycache__`。
- `config.example.yaml` — 配置文件模板，复制为 `config.yaml` 后填入你的 API Key。
- `config.yaml` — 运行配置（**不提交到 Git**，含 API Key）。
- `conf.py` — 功能开关，含 HITL 确认工具列表 `GUARD_TOOLS` 与超时 `GUARD_TIMEOUT`。
- `model_config.py` — 基于 AgentScope `OpenAIChatModel` 构建模型实例。
- `requirements.txt` — Python 依赖。

### 核心模块
- `prism_harness_agent.py` — Agent 定义（`PrismHarnessAgent`）+ 每会话后台运行循环 `agent_runner()`，负责流式 SSE 推送、心跳、工具调用去重、magic 命令（`/approve` `/reject`）。
- `prism_harness_guard.py` — HITL 工具确认（`PrismHarnessGuardMixin`），重写 ReActAgent 的 `_reasoning` / `_acting`，靠 pending 状态轮询实现“确认后才执行”。
- `tools.py` — 工具系统，基于 AgentScope `Toolkit` 注册全部内置工具（命令执行 / 网页抓取 / 联网搜索 / 技能管理 / 文件下载）并生成系统提示词。
- `context_viz.py` — 上下文可视化引擎（`ContextViz`），每轮生成结构化上下文快照供前端透明化展示与落盘。
- `session.py` — 会话管理（`Session` / `SessionManager`），请求队列、HITL 确认队列、超时回收。
- `server.py` — FastAPI 服务，提供 Web 界面与 SSE 流式对话接口。
- `index.html` — Web 界面（三栏：对话 / 上下文光谱 / 工具时间线）。

### 工作区
- `workspace/` — Agent 人格文件（AGENTS / SOUL / USER / IDENTITY）与技能目录（skills/）。

## 核心机制

### HITL 工具确认
1. 模型要调高风险工具 → `_acting` 拦截、入队 pending、假装执行完、进入下一轮。
2. 下一轮 `_reasoning` 发现 pending → 发确认卡片（🔒 + 参数 + `/approve`/`/reject`）。
3. 你点允许（`/approve`）→ `pending.status = APPROVED`。
4. 再下一轮 `_reasoning` 重放原 tool_use → `_acting` 匹配到 APPROVED → 真正执行。
5. 超时未确认 → 自动拒绝，进程不挂起。

### 防死循环（多重保险）
- 普通工具在 `_acting` 中直接放行，结果正常回传，避免模型反复重试。
- 同一 URL / query 的工具调用有次数上限，禁止重复。
- 单轮 ReAct 迭代硬上限（`max_iters`，默认 6），到顶强制停。
- 系统提示中明确“拿到结果就停、不重复调用”等铁规则。

### 流式处理（dsv4 适配）
- dsv4 流式 chunk 为累计全文，取最后一个即可。
- `tool_use` 块流式重复发送，按 id 去重；后块参数更完整，覆盖前块。
- 每 7 秒推一条心跳，绝不静默。

## HTTP API

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/` | Web 界面 |
| POST | `/chat` | `{"session_id", "content":[{"type":"text","text":"..."}]}` → SSE 流式 |
| GET | `/stop?session_id=&request_id=` | 打断当前生成 |
| GET | `/get_personas` | 读 AGENTS / SOUL / USER |
| POST | `/update_persona` | `{"target":"soul","content":"..."}` 改人格文件 |

**SSE 事件类型**：`text` / `tool_use` / `tool_result` / `status`（thinking / acting / confirm / heartbeat / done / error / max_iters）/ `context_view` / `cancel` / `error`。

## 已知限制 / 路线图

- `conf.py` 中的 `enable_cron` / `enable_subagent` / `enable_sandbox` 为预留开关，尚未实现对应工具。
- 上下文压缩（`context.compress_threshold`）目前仅做统计展示，未真正裁剪历史。
- RAG / MCP / 长期记忆 / tracing 等高级能力尚未接入，待后续扩展。

## 调试

- 手动测一轮：
  ```bash
  curl -N -X POST http://localhost:8765/chat \
    -H "Content-Type: application/json" \
    -d '{"session_id":"debug","content":[{"type":"text","text":"查看本地已安装的技能"}]}'
  ```
- 改了 `.py` 后直接双击 `launch.bat`（自动清 `__pycache__` + 杀旧进程）。
