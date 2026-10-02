# 生产驾驶舱 · 每日自动化部署说明

本目录用于把「生产交付驾驶舱」的**每日 9 点自动更新**分发给团队。
自动化本身运行在 WorkBuddy 平台侧（不在 git 里），所以这里提供：
可复用的**桥接脚本** + 一份**自动化配置模板**，团队 clone 后照着建即可。

---

## 1. 为什么需要桥接脚本（automations/bridge/）

能力脚本（sync/seatable_sync.py / sync/partdb_sync.py / cockpit/cockpit.py）和 `data/` 都在
`seatable-production` 技能目录里，而我们希望自动化挂在 WorkBuddy 的
**「生产交付」项目**分组下运行 —— 这两个目录不是同一个。

桥接脚本（共 4 个文件）放在你的「生产交付」项目目录里，运行时自动转发到
真实的 seatable-production 技能目录，做到：
- UI 上自动化归属在「生产交付」分组；
- 脚本/数据仍在技能目录，升级技能不会被改写到项目里。

## 2. 真实技能目录的解析顺序（通用、跨机器）

`bridge_common.py` 按以下顺序定位 seatable-production（**先到先得，且目录内必须
有 `SKILL.md` 才作数**）：
1. 环境变量 `SEATABLE_PRODUCTION_DIR`（最优先，推荐团队显式设置）
2. `~/.workbuddy/skills/seatable-production*`（WorkBuddy 默认技能位置）
3. 相对 `../seatable-production*`

第 2、3 条用的是**通配**而不是写死的目录名，原因是技能目录名可能带版本号
（`seatable-production-1.8.0`），而且**升级后旧的无版本目录常被留成空壳**。
`-` 的排序大于结尾，所以反向排序时版本化目录天然优先；再用「含 `SKILL.md`」
把空壳目录挡掉。效果：技能从 1.8.0 升到 1.9.0（或换回无版本名）时，
`bridge_common.py` 与 wrapper 都不用改。

## 3. 部署步骤（团队照做）

1. **放置桥接脚本**：把 `automations/bridge/` 下的 **4 个文件**平铺复制到你的
   WorkBuddy「生产交付」项目目录：
   - `bridge_common.py`（通用核心，不单独运行）
   - `cockpit.py` / `seatable_sync.py` / `partdb_sync.py`（三个 wrapper，各 3 行）

   ⚠️ wrapper 的**文件名必须与技能目录内的脚本同名**（`cockpit.py` 对应
   `cockpit/cockpit.py`，`seatable_sync.py` 对应 `sync/seatable_sync.py`）——
   `bridge_common` 是拿 `sys.argv[0]` 的 basename 去查映射表的。
   2026-09-26 仓库整理后脚本按域归组，映射表见 `bridge_common.SUBDIR_BY_WRAPPER`。
2. **（可选但推荐）设置环境变量**：
   `SEATABLE_PRODUCTION_DIR = <你机器上 seatable-production 技能目录的绝对路径>`
3. **在 WorkBuddy 新建每日自动化**，配置如下：
   - 名称：`生产驾驶舱·每日9点自动重建部署`
   - 调度（rrule）：`FREQ=DAILY;BYHOUR=9;BYMINUTE=0`
   - 状态：`ACTIVE`
   - 工作目录（cwds）：填你的「生产交付」项目目录（决定 UI 分组归属）。
     实测平台**一次只接受一个工作目录**（逗号分隔、传 JSON 数组都不生效，只会
     留第一项），所以**不要**把技能目录也塞进 cwds —— 脚本一律在 Prompt 里用
     `cd <技能目录> && python ...` 显式切换（调度器默认 cwd 不保证是技能目录）。
   - Prompt：见下方「自动化 Prompt 模板」
4. **首次运行验证**：手动触发一次，确认能从项目目录调通 wrapper 并生成
   `项目管理驾驶舱.html`。

## 4. 每日自动化 Prompt 模板

> **v2.0（2026-09-12）起，数据链路（同步/取数/核对/预测/摘要/驾驶舱）由
> `python workflows/workflow.py run daily --mode apply` 统一执行**，Prompt 只保留三件事：
> ① 启动工作流；② AI 语义步骤（微信事件登记、CRM 识别写入、群聊 AI 总结）；
> ③ 发布、通知与播报。完整模板见 `automations/daily-prompt-v2.md`。
> 播报以 `data/runs/<run_id>/final.json` 为唯一数据源——某一步是否真的跑了，
> 看 JSON，不看 AI 的记忆。失败可用 `--resume <run_id>` 断点续跑。

> 下方为 v1.x 旧模板（已被 v2.0 取代，留作参考）：

> 把下方的 `{{SEATABLE_PRODUCTION_DIR}}` 替换为你的实际技能目录绝对路径。
> 模板只规定流程，不包含真实 token、UUID，也不等于授权自动写入或删除数据。

```
你是「生产交付驾驶舱」专家。请执行每日 9 点例行数据更新任务：

1. 在技能目录 {{SEATABLE_PRODUCTION_DIR}} 下同步最新数据：
   - 运行 python sync/seatable_sync.py 拉取生产业务 Base 的 SeaTable 快照；
   - 运行 python sync/partdb_sync.py 拉取 PartDB 实时库存与缺料。
2. 拉取并总结微信监控群消息：
   - 运行 python wx/wechat_intake.py pull；
   - 运行 python wx/wechat_intake.py summary --hours 24 --out data/wechat_intake/summary_24h.md；
   - 按 references/wx-ai-summary-prompt.md 生成 AI 总结和「待确认候选」，不要在自动化中执行 approve。
3. 对微信候选做双 Base 分流，逐条输出「目标 Base / 目标表 / 关键字段 / 原文依据」：
   - production：项目、生产、采购、库存、发货、质量等业务事实或业务记录候选；
   - tasks：待办、提醒、追问、负责人、截止日期、未闭环事项等执行事项候选；
   - 同一条消息同时包含业务事实和行动要求时允许拆成两条，分别进入 production/tasks；禁止为了省事把所有事项写进默认 Base。
   - 所有候选保持待确认；没有人工确认不得跨 Base 写入。
4. 处理消息证据：
   - 图片证据允许保留原图并在人工确认后上传到目标记录的图片/附件列，同时保留 OCR/视觉摘要、来源群、发送人、时间、哈希；“允许上传”不等于每日自动上传。
   - PDF、Word、Excel、文本等普通文件优先文本化，先保存提取文本/摘要和来源；只有文本化失败、必须核验版式/签章或用户明确要求时，才把原文件列为可上传证据。
5. 运行 python wx/wxmatch.py scan 做消息与业务表只读核对；高置信项也只生成预填意图，等人工确认。
6. 运行 python workflows/alerts.py run、python domain/foresee.py、python domain/foresee.py review，刷新异常与预测。
7. 运行 python workflows/daily_brief.py --push 生成站会摘要和发件箱消息。
8. 运行 python cockpit/cockpit.py 读取 data/ 重新生成「项目管理驾驶舱.html」（单文件、内联 CSS/JS，零外部依赖；访问口令取自 config.yaml 的 cockpit 段，重生成即生效）。
9. 生成简短摘要播报，除交期、缺料、在制品、现金流外，还要报告 production/tasks 两类微信候选数量、图片证据候选数、普通文件文本化成功/失败数；不得把待确认候选说成“已写入”。
10. 读取技能目录下的 config.yaml 的 cockpit 段，在私聊播报末尾附「当前生效访问口令清单」：
    - 主口令(admin_password)：<值>
    - 各角色口令(role_passwords)：boss / warehouse / purchase / production / sales 各自的值
    - 注明「口令取自 config.yaml，重生成即生效；若你已轮换请以此为准」。口令仅本地/私聊查看，勿推送群聊。

重要：
- 每日任务不得运行 domain/evidence.py prune ... --yes；证据清理由季度任务先列候选，再由人工单独确认。
- 对外稳定链接（如 *.bj6.agentos-app.net）由 WorkBuddy 的 HTML 发布功能原地更新，请用本轮最新生成的「项目管理驾驶舱.html」重新发布到该链接，不要走 CloudStudio 沙箱部署。
- 若同步、文本化、生成或发布失败，记录错误原因并说明已完成/未完成步骤，不要静默中断，也不要因某一步失败改写真实配置。
```

## 4.5 每晚 19:00 全量自动化（可信执行层 v1，2026-09-26）

09:00 与 19:00 的分工：

| 时间 | 工作流 | 做什么 | 不做什么 |
|---|---|---|---|
| 09:00 | `daily` | 轻同步：补 19:00→09:00 的群聊增量 → 生成看板 → **发布（带门禁）** | 不 OCR、不写业务表、不重建 |
| 19:00 | `evening` | 全量：采集→OCR→核对→授权写入及读回→快照→风险→生成→发布（带门禁） | — |

> **9:00 的发布同样带门禁**（Prompt 里显式传 `--gate <本次 run_id>`）。历史上这条
> 命令漏写 `--gate`，发布器只能自己去猜「最近一次运行」，在 `daily-` 与 `evening-`
> 目录混放时长期猜错 → 09:00 的发布**天天被拒**，而 Prompt 当时把失败合理化成
> 「不影响本地兜底」。已修正为必须显式传 run_id（G5，2026-10-02）。

新建自动化：

- 名称：`生产驾驶舱·每晚19点全量重建发布`
- 调度（rrule）：`FREQ=DAILY;BYHOUR=19;BYMINUTE=0`
- 状态：`ACTIVE`
- 工作目录（cwds）：同 09:00 那条（**单个**项目目录；技能目录靠 Prompt 里 `cd` 切换）
- Prompt：见 `automations/evening-prompt.md`

> **发布门禁**：19:00 的发布步骤带 `--gate {run_id}`；09:00 在 Prompt 里显式传
> `--gate <run_id>`。账本里关键阶段（采集 / OCR / 核对 / 授权写入）失败，或出现
> 读回验证失败，发布会被拒绝且**不上传任何内容**。这不是故障，是设计。
>
> **运行级状态三档**（2026-10-02 起）：
>
> | 状态 | 含义 | 门禁 |
> |---|---|---|
> | `success` | 全部步骤 success/skipped | 放行 |
> | `degraded` | 跑完但有**非阻断**失败（如 PartDB 502 → 库存沿用旧快照） | **放行 + 强制告警**，播报必须点名 |
> | `failed` | 有**阻断型**失败，或 DAG 为空 | 拒绝 |
>
> `degraded` 的存在是为了消灭「人工改账本」：此前系统没有「有降级但仍可发布」的
> 合法表达，运营只能把 `final.json` 的 `failed` 手改成 `success`（见
> `data/runs/patch_final_*.py`），结果是真实故障在驾驶舱里彻底消失。
> **账本不可手工修改**；门禁会比对「账本 status」与「按步骤重算的结果」，不一致会告警。
>
> **授权写入**：`data/approvals/` 下没有有效授权时，该步骤自动跳过
> （退出码 3，不算失败）—— 当晚照常出看板，只是不写业务表。
> 授权文件格式与撤销方式见 `docs/trusted-execution-v1.md` §3。
>
> **原则回顾**：高置信 ≠ 授权。置信度只决定「哪些条目进入待授权清单」，
> 永远不决定「是否放行」。

---

## 5. 季度证据清理 Prompt 模板

建议每季度首个工作日运行一次。**自动化只扫描和报告候选，永远不带 `--yes`**：

```
你是「生产交付驾驶舱」专家。请执行季度微信证据保留检查：

1. 在技能目录 {{SEATABLE_PRODUCTION_DIR}} 运行：
   python domain/evidence.py scan --root data/wechat_intake --days 90 --json
2. 只把同时满足以下条件的证据列为清理候选：超过 90 天、关联事项已闭环、未标记长期保留。
3. 按「证据编号 / 关联事项 / 日期 / 状态 / 路径 / 候选原因」输出清单，等待人工审核。
4. 不删除文件、不删元数据、不修改 SeaTable，不执行任何带 --yes 的命令。
5. 人工明确批准具体候选后，才允许在单独的人工会话运行：
   python domain/evidence.py prune --root data/wechat_intake --days 90 --yes
   执行后报告实际删除项；未获批准则保持原状。

永不列入候选：未闭环、日期不明、90 天内、已标记长期/永久保留的证据。
```

> `scan` 和不带 `--yes` 的 `prune` 都只预览；真正删除必须显式输入 `--yes`。
> “季度”是复核频率，“90 天”是候选年龄阈值，两者不要混为一谈。

## 6. 安全须知

- **口令**：驾驶舱访问口令在仓库外的 `config.yaml`（已被 .gitignore 忽略），
  不会进 git；v2.0 起口令已从每日自动播报中移除，只在用户手动运行
  `tools/passwords.py show` 时当面展示，自动化播报文本不再包含口令。
- **`.workbuddy/` 目录**（含自动化 memory、项目 memory、业务指标）已被
  `.gitignore` 忽略，**切勿 `git add` 提交或 push**，以免泄露口令与业务数据。
- 仓库已忽略：`config*.yaml`、`data/`、`*驾驶舱*.html`、`cockpit_passwords.json`。

## 7. 维护者本机实际配置（参考，非强制）

- 本机「生产交付」项目目录：`C:\Users\11430\WorkBuddy\2026-08-14-14-34-58`
  - 2026-09-26 起里面是**通用版**：`bridge_common.py` + 3 个 3 行 wrapper
    （原先的 3 个硬编码 wrapper 指向已被升级留空的旧技能目录，双重失效；
    原件归档在技能仓库 `data/archive/2026-09-26-restructure/`）。
- 技能目录实名：`C:\Users\11430\.workbuddy\skills\seatable-production-1.8.0`
  - ⚠️ `skills/` 下还有一个**同名的空壳目录** `seatable-production`（升级遗留，
    里面 0 个文件）。写自动化路径时别写成它；`bridge_common` 已用「含 SKILL.md」
    的校验把它挡掉。
- 三条 ACTIVE 自动化的 cwds（各一个）：09:00 与周一行情 → 生产交付项目目录；
  19:00 → 微信文件目录 `D:\User\Documents\xwechat_files\...\msg\file`
  （保留原值：该目录是微信数据源，作为 cwd 便于引擎直读）。
