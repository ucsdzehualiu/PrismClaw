# PrismHarness → AgentScope 2.0.9 迁移计划

> 状态：**已完成主体迁移并通过冒烟**（分支 `agentscope-2-migration`）。
> - 环境：conda `prism2`（Python 3.11）+ `agentscope[service]==2.0.9`（清华镜像安装）。
> - 改动文件：`model_config.py`（2.0 模型构造）、`tools.py`（2.0 Toolkit/FunctionTool/ToolChunk + 自实现文件工具 + 权限）、`session.py`（停泊队列）、`prism_harness_agent.py`（2.0 Agent + 事件流→SSE + HITL 停泊/续跑）、`team.py`（2.0 Agent 构造）、删 `prism_harness_guard.py`；`server.py` 无需改（沿用会话方法名）。
> - 验证：`tests/test_pipeline.py` 44/44；`tests/smoke2.py` 10/10；真实 LLM 冒烟 `smoke_live.py`（单轮 HITL 确认→执行）、`smoke_team_live.py`（团队端到端）、`smoke_server.py`（FastAPI+SSE `/approve` 续跑）全部通过。
> - 前端契约保持不变，`index.html` 无需改。

## 0. 目标与边界

1. 框架从 **AgentScope 1.0.18 / Python 3.10** 全量迁移到 **AgentScope 2.0.9 / Python 3.11+**。
2. HITL 改用框架自带的 **permission 引擎 + `RequireUserConfirmEvent`** 事件流，
   删掉现有 `PrismHarnessGuardMixin` 覆写 `_reasoning`/`_acting` + 假 tool_result + 文案字符串匹配的做法。
3. 重构**多代理 / 团队圆桌**编排层。
4. **保持前端 SSE 契约尽量稳定**（`index.html` 尽量少改；确有必要再动）。
5. 自测 + 冒烟（无真实 LLM 时用 FakeModel/FakeAgent 回归 + 有网关时做真实冒烟）。

## 1. 环境

- conda env：`prism2`（Python 3.11）
- 依赖：`agentscope[service]==2.0.9`（含 fastapi/uvicorn/apscheduler）+ `PyYAML aiofiles aiohttp beautifulsoup4`
- `requirements.txt` 改为钉 `agentscope[service]==2.0.9`、Python `>=3.11`
- 启动脚本 `launch.bat` / `cleanup.py` 改激活 `prism2`

## 2. 关键 API 映射（1.0 → 2.0）

| 1.0（现状） | 2.0（目标） |
|---|---|
| `agentscope.agent.ReActAgent` | `agentscope.agent.Agent`（`ReActConfig` 配 max_iters） |
| `agent._sys_prompt` 私有属性 | `Agent(system_prompt=...)` + `on_system_prompt` 中间件 |
| `InMemoryMemory` / `agent.memory` | `AgentState.context`（`state.`） |
| `OpenAIChatFormatter()`（挂 agent） | formatter 挂 **model**：`OpenAIChatModel(formatter=...)` |
| `agent(inputs)` + `pipeline.stream_printing_messages` | `agent.reply_stream(inputs)` → 事件流 |
| `Msg(role=..., content=[TextBlock(...)])`（dict block） | `Msg`/`UserMsg`/`AssistantMsg` + 块对象（`TextBlock` 等） |
| `ToolUseBlock` | `ToolCallBlock` |
| `Toolkit.register_tool_function(fn)` | `Toolkit(tools=[FunctionTool(fn, ...)])` / `toolkit.add_tool(...)` |
| `ToolResponse(content=[TextBlock(...)])` | `ToolResponse`（2.0 结构，待 cookbook 确认） |
| `OpenAITokenCounter(model_name=)` + `count(list[dict])` | `model.count_tokens(...)` 内置 |
| `OpenAIChatModel(model_name=, api_key=, client_kwargs=, generate_kwargs=)` | `OpenAIChatModel(credential=OpenAICredential(api_key,base_url), model=, parameters=Parameters(temperature,...), context_size=, extra_body=)` |
| 覆写 `_reasoning`/`_acting` 做确认 | `PermissionEngine` + 工具级 `PermissionDecision(ASK)` / 规则；`on_check_permission` 中间件 |
| `register_instance_hook("pre_reasoning",...)` | `MiddlewareBase.on_system_prompt / on_reasoning / on_acting` 等 |
| session 里 pending 假结果队列 | `AgentState` 里 tool call `ASKING` 挂起；用 `UserConfirmResultEvent` 续跑 |

## 3. HITL 新流程（核心）

参考 `agentscope/console/_console.py`：

1. `agent.reply_stream(UserMsg(...))` 消费事件；命中受保护工具时，
   框架发 `RequireUserConfirmEvent(tool_calls=[...])`，这些 tool call 停在 `ToolCallState.ASKING`。
2. 后端把确认卡片推给前端（沿用现有 SSE `phase: confirm` 信封），
   会话里记录 `reply_id` + 待确认 `tool_calls`。
3. 用户 `/approve` `/reject` `/approve_all` → 构造
   `UserConfirmResultEvent(reply_id=..., confirm_results=[ConfirmResult(confirmed=bool, tool_call=..., rules=...)])`
   → 再次 `agent.reply_stream(event)` 续跑（批准则执行工具、拒绝则注入拒绝结果并继续推理）。
4. `ask_user_question`：沿用内置 `AskUser` 工具 / 或自定义工具 + 同一确认通道。
5. 超时：会话层仍保留 `GUARD_TIMEOUT` 兜底 → 超时按“拒绝”构造结果。

受保护工具改为在注册时打 `PermissionDecision(behavior=ASK, ...)`，不再靠 `GUARD_TOOLS` 名单做字符串/名称拦截。

## 4. 文件级改造清单

- `model_config.py`：`build_model` → 2.0（credential/parameters/extra_body/context_size）；去掉 `build_token_counter`。
- `tools.py`：`build_toolkit` → 2.0 Toolkit + `FunctionTool`；`ToolResponse` 适配；受保护工具打 ASK 权限；技能注册 `Toolkit(skills_or_loaders=...)`。
- `prism_harness_agent.py`：`Agent(...)`；中间件（动态 system prompt 注入 personas / 推理提示 / thinking 清理）；`agent_runner` 改为消费 `reply_stream` 事件流并映射成现有 SSE 信封；`ModelContextProbe` 适配 2.0 model 调用。
- `prism_harness_guard.py`：**删除**（被 permission 引擎取代）。
- `session.py`：`PendingToolUse` 改为承载 2.0 `ToolCallBlock` + `reply_id`；确定/拒绝走 `UserConfirmResultEvent`。
- `server.py`：`/chat` SSE、`/resolve` 适配新续跑路径。
- `team.py`：成员/lead agent 用 2.0 `Agent` 构造；编排层重构（多阶段管线 / 圆桌）。
- `context_viz.py`：适配 2.0 消息/块。
- `conf.py`：`GUARD_TOOLS` → 权限配置；去掉死配置 cron/sandbox（或接 2.0 内置）。
- `tests/`：回归基线适配 2.0（FakeAgent 替换点需重写）。

## 5. 迁移顺序（增量可测）

1. 环境就绪 + 最小 2.0 冒烟（构造 model + Agent + 一个工具，跑一次 reply）。
2. `model_config.py` + 消息/工具适配层。
3. `tools.py`（纯工具，无 agent 依赖）。
4. `prism_harness_agent.py` 单代理主循环 + HITL。
5. `server.py` + 前端契约回归。
6. `team.py` 多代理/圆桌重构。
7. 全量回归 + 冒烟。

## 6. 风险

- 2.0 与 1.0 **不兼容**，是重写而非改配置；面广（agent/tools/team/server）。
- 无真实 LLM 时难端到端验证 → 以 FakeModel + 事件流断言为主，真实冒烟依赖网关可达。
- 前端 SSE 契约一旦漂移，`index.html` 需同步改。
