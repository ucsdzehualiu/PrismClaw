# 专家团移植说明（专家团/ → PrismHarness）

把 `专家团/` 下 10 个外部专家团队（CodeBuddy/WorkBuddy Team Plugin 格式）移植进本 harness。
移植由 [`port_experts.py`](port_experts.py) 生成，可重复运行；本文件记录映射规则与已知取舍。

## 源包 ↔ 团队文件对照

团队文件用的是**中文显示名**，光看文件名对不回源目录（这正是「以为没移植」的常见原因）。
每个团队 JSON 里都记了 `source` / `source_version` 字段，可直接反查：

| 源专家包目录 | 版本 | 生成的团队文件 | 类型 | 成员 |
|---|---|---|---|---|
| `dd-due-diligence-team` | 1.0.7 | `teams/AI尽调专家团.json` | pipeline | 8 |
| `a-share-analysis` | 1.0.1 | `teams/A股研究团队.json` | pipeline | 7 |
| `opc-team` | 1.0.0 | `teams/一人公司专家团.json` | pipeline | 8 |
| `openspec-doc-team` | 1.1.0 | `teams/专业文档生成团队.json` | pipeline | 4 |
| `trading-agent` | 1.0.0 | `teams/交易分析团队.json` | pipeline | 12 |
| `humanize-ppt-team` | 0.2.0 | `teams/卡尔的人感PPT专家团.json` | pipeline | 6 |
| `investment-masters-team` | 1.0.0 | `teams/投资大师专家团.json` | roundtable | 19 |
| `gpt-researcher-team` | 2.0.0 | `teams/深度研究团队.json` | pipeline | 7 |
| `believe-in-light` | 1.0.1 | `teams/相信光么.json` | pipeline | 6 |
| `migraq-team` | 1.1.2 | `teams/腾讯云上云迁移专家团.json` | pipeline | 7 |
| `stock-partner-team` | 1.0.7 | `teams/腾讯自选股股票投研专家团.json` | pipeline | 6 |
| `awesome-finance-skills` | 1.0.0 | `teams/鹏城信息AI专家.json` | pipeline | 1 |

（界面上也能看到：设置页团队列表、聊天区团队菜单，鼠标悬停即显示来自哪个源包。）

## 用法

```bash
python port_experts.py                  # 自动找源目录（见下）→ 重新生成 teams/ 与 skills/
python port_experts.py D:/expert-src    # 指定源目录
PH_EXPERT_SRC=D:/expert-src python port_experts.py
```

**源目录按顺序自动查找**：

1. 仓库根 `专家团/`（如果还在）
2. `~/.workbuddy/plugins/cache/experts`（WorkBuddy 装插件后的缓存目录）

源包不必放在仓库里，用参数/环境变量可指向任意位置。已验证：从 WorkBuddy 缓存生成的结果
与从仓库内 `专家团/` 生成**逐字节一致**。

**新增团队会自动认领**：源目录里出现 `TEAM_PLANS` 里没有的团队时，脚本会按
「成员并行分析 + 主理人汇总」兜底生成（所有成员 prompt 原样保留），并打印醒目提示；
若该团队原本是多阶段 SOP，把它的阶段编排补进 `port_experts.py` 的 `TEAM_PLANS` 即可细化。

### 提取完整性（删源包前请对照）

已逐人核对：**12 个源团队共 101 位专家，其中 90 位非主理人专家的 prompt 全部已进入
`teams/*.json`，零缺口**。以下是**有意不提取**的内容：

| 内容 | 为什么不要 |
|---|---|
| 11 位主理人的原始 md | 其正文是 `TeamCreate / Agent / SendMessage` 编排协议，在 harness 里由 `team.py` 的确定性 pipeline 承担；产物里是为各团队重写的 `lead_prompt` |
| `rules/*.md` | 插件激活清单（「用户说这些话时启用本团队」），属宿主平台集成元数据，非专家知识 |
| 各团队 `README.md` | 上架包说明与花名册，花名册信息已进团队 JSON |
| `avatars/*.png` | 无头像 UI |
| 技能里被 `SKIP_EXT` 跳过的媒体（截图/演示动图/字体） | 文档素材，不影响技能在 LLM 侧的可用性 |
| `stock-partner-team/bin/init_task.py` | 原厂埋点上报脚本（自称 reporting / telemetry），本 harness 不采集这类数据 |
| `<team>/<version>/` 版本目录 | 宿主留的缓存副本，与团队根内容逐字节相同 |
| `.downloaded_at` / `.in_use/` / `.mcp.json` | 宿主元数据 / MCP 连接配置，本 harness 不适用 |

> ✅ 第三方**许可证已随附**：`a-share-analysis/license/` 下的 ajv / uri-js /
> require-in-the-middle 授权书，已随内联了这些代码的 `skills/westock/scripts/*.js`
> 一起拷到 `skills/westock/license/`。删源包不影响这份授权书。

生成的团队在 `teams/`（仓库级内置，随仓库分发），技能在 `skills/`（仓库级共享）。
用户自建团队放 `workspace/teams/`，同名时覆盖内置版本（与 skills 的加载顺序一致）。

在界面里运行：聊天输入框左侧 **🎭 团队** 按钮，或发 `/team <团队名称> <任务>`。

## 两种格式的差异与映射

| 外部（专家团） | 本 harness |
|---|---|
| `agents/<id>.md`（frontmatter + 角色 prompt） | 团队 spec 里的 `members[]`（name/role/color/prompt） |
| `TeamCreate / Agent / SendMessage` 编排协议 | `team.py` 的确定性编排（不依赖模型自觉） |
| 主理人 md 里的「Phase 1→5 / S1→S7」 | 团队 spec 的 `type:"pipeline"` + `stages[]` |
| `skills/<name>/SKILL.md` | `skills/<name>/`（仓库级，供成员按需加载） |
| 多智能体圆桌（分析→风控→决策） | `type:"roundtable"` |

**冲突以 harness 为准**：外部 md 正文里讲 TeamCreate/SendMessage 的编排协议不进 prompt
（编排由 `team.py` 承担）；成员 prompt 正文一字不改，保留其专业角色与输出规范。

## 团队类型说明

- **parallel**（扁平）：成员并行分析 → lead 汇总。适合无阶段依赖的评审团。
- **roundtable**（两阶段圆桌）：分析 → 风控 → 决策。
- **pipeline**（多阶段，本移植新增）：任意阶段序列；**阶段内成员并行**，阶段间把上游产出
  累积传给下游；末阶段产出即最终报告（`final:"last"`），或再由主理人汇总（`final:"lead"`）。

pipeline 的 spec：

```json
{
  "name": "…", "type": "pipeline", "final": "last",
  "stages": [
    {"id": "s1", "label": "并行调研", "include_prev": false,
     "task": "任务：{task}\n\n阶段的额外指令（{task} 会替换为主任务）",
     "members": [{"name": "…", "role": "…", "color": "#…", "prompt": "…"}]},
    {"id": "s2", "label": "汇总裁决", "members": [{"name": "…", "prompt": "…"}]}
  ]
}
```

## 各团队的映射与保真度

| 团队（teams/*.json） | 类型 | 阶段 | 说明 |
|---|---|---|---|
| A股研究团队 | pipeline | 6 路并行 → 风险诊断 → 主理人汇编 | 原 6 套 Workflow 收敛为一条完整链路 |
| 鹏城信息AI专家 | pipeline | 单阶段单成员 | 原为单 agent 专家（expertType=agent） |
| 相信光么 | pipeline | 三端扫描 → 因果验证 → 权重校准 → 评级 | 原依赖万得/通达信 MCP，缺失时降级公开数据 |
| AI尽调专家团 | pipeline | 进件 → 财务核验 → 四路分析 → 风险总览 → 报告 | 原依赖 aidd-saas MCP，缺失时按材料+公开信息推理 |
| 深度研究团队 | pipeline | 初调 → 大纲 → 逐章 → 审稿 → 修订 → 框架 → 发布 | 原「逐章循环 + 最多 3 轮审稿」收敛为单轮代表性链路 |
| 卡尔的人感PPT专家团 | pipeline | 大纲 → 双路渲染 → 视频 → 演讲 → 质检 | 渲染/视频依赖 Node/Remotion，需相应环境 |
| 投资大师专家团 | roundtable | 19 位并行 → 风控 → 组合经理决策 | 与 harness 圆桌天然一对一 |
| 腾讯云上云迁移专家团 | pipeline | 产品评估 → 架构/LZ → 交付 → 运维 → 主理人方案 | 原走 CMG/MSP 远端 API，无凭据时按公开知识给方案 |
| 一人公司专家团 | pipeline | 资源→利基→价值→商业模式→MVP→转化→运营期（按需） | 8 位专家全数移植；运营期两阶段无法用线性 stages 表达条件分支，放在末阶段由成员自行判断是否适用（不适用就一句话带过） |
| 交易分析团队 | pipeline | 4 路取证 → 多空辩论 → 研究主管 → 交易员 → 三方风险 → 风险主管 | 完整复现其对抗辩论链路 |
| 专业文档生成团队 | pipeline | 检索 → 生成 → 审核 → 修订 → 总编辑整合交付 | 逐章循环（检索→生成→审核）收敛为单轮长文链路；放宽了截断额度以容纳正文 |
| 腾讯自选股股票投研专家团 | pipeline | 六位专家并行研判 → 投研主编汇编 | 原依赖 westock-mcp 连接器取实时行情，无则降级联网搜索 |

> 后两个团队的外部包**外面套了一层版本目录**（如 `专家团/openspec-doc-team/1.1.0/`），
> `port_experts.py` 的 `team_root()` 会自动解析到真正含 `.codebuddy-plugin` 的那一层。

## 技能移植

- 技能拷入 `skills/`，按技能名去重（`neodata-financial-search` 被 3 个团队共用、
  `westock` 家族被 2 个团队以不同形态提供）。已有同名技能默认跳过，所以重复运行
  不会覆盖你改过的版本。
- **跳过媒体/超大二进制**（文档截图、演示动图、字体、包）：`port_experts.py` 的
  `SKIP_EXT` / `MAX_FILE_BYTES`。这些是文档素材，不影响技能在 LLM 侧的可用性；
  被跳过的文件数会在运行时打印。需要完整素材时，直接从 `专家团/<team>/skills/` 取。
- `believe-in-light` 没有 SKILL.md，其「技能」是 `references/` 下的确定性 Python 引擎
  （weight_engine / rater / self_evolve / …）。已单独打成 `skills/believe-in-light-engine/`
  （脚本 + 由引擎契约生成的 SKILL.md）。
- **未移植** `stock-partner-team` 的 `bin/init_task.py`：那是原包的任务上报/埋点脚本
  （原文档自称 reporting / telemetry），不属于专家能力，本 harness 不采集这类数据。

## 已知取舍（冲突以 harness 为准）

1. **编排确定性**：外部团队靠主理人自觉按 SOP 调度成员；本 harness 由 `team.py` 按 `stages`
   确定性驱动，不依赖模型是否遵循 SOP——更稳，但与原「主理人临场编排」的细节不完全一致。
2. **循环/条件分支**：外部的「逐章循环」「最多 3 轮审稿」「无信号则跳过阶段」等控制流无法在
   线性 stages 里表达，均收敛为单轮代表性链路（见上表说明）。
3. **外部依赖**：MCP（aidd-saas、万得、通达信）、远端 API（CMG/MSP）、Node/Remotion 等在本
   harness 中不默认具备；成员 prompt 里指向这些数据源的指令保留原样，实际取不到数据时
   依 prompt 的降级要求回退到公开信息，并会如实说明数据缺口。
4. **人工确认节点**：外部的「⏸ 阶段0 数据源确认」等人工节点未纳入自动链路；如需，请在任务
   描述里直接写清运行模式。
5. **成员 prompt 中的旧路径**：如 `python ~/.workbuddy/skills/...` 属外部运行时路径，本 harness
   不可用；对应技能已注册到 `skills/`，成员可通过「技能使用指南」按名字发现。
