# PrismHarness 使用与测试指南

面向使用者：怎么把它跑起来、怎么用团队、怎么自己验证它没坏。
（想了解专家团是怎么移植进来的 → 看 [`PORTING.md`](PORTING.md)）

---

## 一、启动

**Windows 推荐：双击 `launch.bat`**
它会依次：清掉上一轮的旧进程 → 清 `__pycache__` → 起 server → 端口就绪后自动打开浏览器。

**手动启动：**

```bash
conda activate ai-course     # 或任意装了 requirements.txt 的 Python 环境
python server.py
# 浏览器打开 http://localhost:8765
```

> ⚠️ 一定要用 `launch.bat` 里那个 conda 环境（本例是 `ai-course`）。
> 系统自带的 python 没装 `agentscope`，会直接报 `No module named 'agentscope'`。

**配置模型**：打开页面 → 右上 ⚙️ 设置 → 填 `api_base` / `api_key` / `model` → 点「测试」确认连通 → 保存（下一轮对话立即生效，不用重启）。

---

## 二、普通对话

直接在输入框打字，Enter 发送，Shift+Enter 换行。右侧两块面板会实时展开：

- **上下文轨迹**：模型这一步实际「看到」了什么（System Prompt、历史、工具定义、Token 用量）
- **工具轨迹**：按执行顺序列出每次工具调用与结果

写文件 / 跑命令这类高风险操作会弹**确认卡片**，点「允许」才真正执行；也可在输入框发 `/approve`、`/reject`。

---

## 三、用团队（核心功能）

团队 = 多个带独立人格的「成员」按阶段协作，最后汇总成一份报告。

### 三种触发方式（任选）

1. **点按钮（推荐）**：输入框左侧 **🎭 团队** → 弹出团队列表（覆盖层菜单，不影响输入框）→ 点一个团队
   → 输入框**上方出现一条模式条**（团队名 + 类型/规模），输入框变成任务框、按钮变「🚀 运行」
   → 输入任务，**回车即运行**。发完自动退出团队模式（避免下一条普通消息被误当成团队任务）。
   按 **Esc** 或点模式条上的 **✕** 可随时退出。
2. **发命令**：输入框直接发
   ```
   /team A股研究团队 当前是否适合加仓？
   ```
   （格式：`/team <团队名> <任务>`，团队名就是菜单里显示的名字）
3. **设置页**：⚙️ 设置 → 团队 → 可以看/改/新建团队 JSON

> 全程只有**一个**输入框：团队菜单是浮层，选中后**复用**同一个输入框，不会再冒出一个任务输入框。

### 运行时会看到什么

对话区顶部出现一块 **🎭 面板**：

```
🎭 多阶段管线（7 位成员） · 阶段 2/4 · 已用时 43s        ▾ 收起成员
✅ 🔭 三端扫描   ⏳ 🔗 因果验证   ○ 🎯 评级输出
龚几端   ✅   ✅ 1243 B   ▼ 展开
阴果验   ⏳   ⏳ 分析中…
```

- **阶段条**：每个阶段的进度（✅完成 / ⏳进行中 / ○未开始）
- **状态栏**（右上角小徽章）：跟着显示 `🎭 因果验证 · 阶段 2/4`
- **成员行**：点一下展开该成员的产出摘要
- **跑完自动收起**成员明细（成员 ≥5 时），避免把报告挤到屏幕外 → 点「展开成员」看明细

最终报告会作为一条普通消息出现在下方（若含 `[xxx报告]` 标记会自动渲染成可折叠卡片）。

### 想中途停掉

点右上角 **⏹ 停止**（它调后端 `GET /stop`）。

> 注意：停止按钮是**独立的 HTTP 请求**，所以能在团队跑到一半时打断。
> 别指望在输入框打 `/stop`——那条消息要等当前这轮跑完才会被处理。

### 结果存哪

```
session_logs/<会话ID>/teams/run_<时间戳>/
├── config.json      本次用的团队配置 + 任务
├── members/*.json   每位成员的完整产出（多阶段时文件名带阶段前缀）
├── SUMMARY.md       可读的运行记录（按阶段分组）
└── report.json      最终报告
```

### 有哪些团队

**内置（`teams/`，随仓库分发，13 个）**

| 团队 | 类型 | 规模 | 适合 |
|---|---|---|---|
| 评审团 | 并行 | 2 成员 | **冒烟/试跑（最快，几秒）** |
| 鹏城信息AI专家 | 单阶段 | 1 成员 | 单人金融市场综合分析 |
| A股研究团队 | 多阶段 2 | 7 成员 | A股多维度研判 + 风险诊断 |
| 相信光么 | 多阶段 4 | 6 成员 | 光模块产业链信号监控 |
| AI尽调专家团 | 多阶段 5 | 8 成员 | 银行对公授信尽调 |
| 一人公司专家团 | 多阶段 7 | 8 成员 | 一人公司九阶段共创（运营期按需触发） |
| 卡尔的人感PPT专家团 | 多阶段 5 | 6 成员 | PPT 大纲→渲染→演讲→质检 |
| 腾讯云上云迁移专家团 | 多阶段 4 | 7 成员 | 上云迁移方案 |
| 专业文档生成团队 | 多阶段 4 | 4 成员 | 施工设计说明/技术方案/招投标/手册等长文档 |
| 腾讯自选股股票投研专家团 | 单阶段 6 人并行 | 6 成员 | 六视角股票圆桌（产业/信号/估值/逆向/财报/短线）|
| 深度研究团队 | 多阶段 7 | 7 成员 | 带引用的深度研究报告 |
| 交易分析团队 | 多阶段 7 | 12 成员 | 多空辩论式交易决策 |
| 投资大师专家团 | 圆桌 3 阶段 | 19+2 成员 | 19 位大师并行 + 风控 + 组合决策 |

> 「专业文档生成团队」是唯一放宽了截断额度的团队（`stage_output_clip: 24000`）——
> 长文档正文动辄上万字，默认 6000 字会把正文切掉。

**另外两个在你本机（`workspace/teams/`，不进 git）**：投资大师圆桌（19人）、投资圆桌-轻量版（5人）。
同名时 `workspace/teams/` 覆盖 `teams/`——你改的版本不会被升级覆盖。

> 第一次玩建议：**评审团**（几秒出结果，验证链路通不通）→ 再试 **投资圆桌-轻量版** 或 **A股研究团队**。
> 深度研究 / 交易分析 是 7 阶段长流程，一次要十几分钟、二十几次模型调用。

### 加 / 改团队

- **改**：⚙️ 设置 → 团队 → 选团队 → 改 JSON → 保存。保存会写到 `workspace/teams/`（覆盖内置同名版本，`teams/` 里的原文件不动）。
- **加**：点「➕ 新建并行团队」或「➕ 新建多阶段团队」拿到模板，改完保存。
- **删内置团队想恢复**：`python port_experts.py` 重新生成。

---

## 四、怎么测

三道防线，前两道零成本、秒级，第三道是真实浏览器。

**PowerShell（你的默认终端）：**

```powershell
conda activate ai-course            # 关键：test_pipeline.py 需要 agentscope

python tests/test_pipeline.py       # ① 编排逻辑（不调模型）
node   tests/test_console_widget.js # ② 团队面板渲染（不调模型、不用浏览器）
node   tests/test_console_browser.js# ③ 真实浏览器：开页面 + 抓 console 报错

$env:PH_LIVE=1; node tests/test_console_browser.js   # ④ ③ + 真跑一次团队（调模型）
```

**Git Bash / CMD：**

```bash
conda activate ai-course
python tests/test_pipeline.py
node   tests/test_console_widget.js
node   tests/test_console_browser.js
PH_LIVE=1 node tests/test_console_browser.js
```

| 测试 | 验什么 | 耗时 | 花钱 |
|---|---|---|---|
| `test_pipeline.py` | 27 项：三种团队类型的阶段编排、成员报错不中断、被打断后重建重试、上下文累积、落盘文件 | <1s | 否 |
| `test_console_widget.js` | 16 项：面板 HTML（类型/阶段进度/耗时/收起展开/同名成员区分/引号转义） | <1s | 否 |
| `test_console_browser.js` | 12 项：真 Chrome 加载页面、console 无报错、真 DOM 里面板渲染与交互 | ~15s | 否 |
| `PH_LIVE=1` 上面那条 | +5 项：真的点 🎭 → 选团队（评审团）→ 运行，验证面板与报告真的出现 | ~1-2min | 是（3 次小调用） |

**要点**

- `test_pipeline.py` 必须用 conda 环境（要 `agentscope`）。忘了激活的话它会直接告诉你该怎么做，不会甩一堆 traceback。
- 后两个测试用 Node，不需要装任何包（用 Node 内置 WebSocket 驱动 Chrome）。
- `test_console_browser.js` 会**自动起 server**（8765 没在跑时）、跑完自动关掉；它启动 server 用的是 `python`，所以也要在那个 conda 环境里跑，或显式指定：
  ```powershell
  $env:PH_PYTHON="C:\Users\<你>\.conda\envs\ai-course\python.exe"; node tests/test_console_browser.js
  ```
- 换端口：`$env:PH_URL="http://127.0.0.1:9000"`。
- 没装 Chrome/Edge 时，第 ③ 步会提示并跳过（不报错）。

---

## 五、常见问题

| 现象 | 原因 / 怎么办 |
|---|---|
| `No module named 'agentscope'` | 没用对 Python 环境 → 双击 `launch.bat`，或 `conda activate ai-course` |
| 端口 8765 被占用 | 双击 `launch.bat`（会自动清旧进程），或手动 `python cleanup.py` |
| 团队跑很久没动静 | 正常，每个阶段/成员都要调一次模型。看面板的「阶段 x/y」和「已用时」 |
| 某个成员显示 ❌ | 该成员超时或报错，其它成员与后续阶段仍会继续；报告里会标注 |
| 想改团队但不想动内置版 | 直接改，保存会写到 `workspace/teams/`，内置的 `teams/` 不受影响 |
| 团队列表是空的 | 确认 `teams/` 目录在项目根；或已在设置页把团队都删了 |
| 模型连不上 | ⚙️ 设置 → 点「测试」看具体报错（api_base / key / model 三件套） |

---

## 六、目录速查

```
PrismHarness/
├── launch.bat              一键启动（Windows）
├── server.py               FastAPI + SSE 服务
├── prism_harness_agent.py  每会话的 Agent 循环
├── team.py                 团队编排（并行 / 圆桌 / 多阶段管线）
├── tools.py                工具集（含技能注册）
├── index.html              前端控制台
├── teams/                  内置团队配置（随仓库分发）
├── skills/                 内置技能（随仓库分发）
├── workspace/               你的东西：人格文件、自建团队、自建技能、下载
│   ├── AGENTS.md SOUL.md USER.md IDENTITY.md
│   ├── teams/               自建团队（同名覆盖 teams/）
│   └── skills/              自建技能（同名覆盖 skills/）
├── session_logs/            每会话的轨迹与团队运行记录
├── port_experts.py          从 专家团/ 重新生成 teams/ 与 skills/
└── tests/                   三个测试
```
