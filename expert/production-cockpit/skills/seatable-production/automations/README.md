# 生产驾驶舱 · 每日自动化部署说明

本目录用于把「生产交付驾驶舱」的**每日 9 点自动更新**分发给团队。
自动化本身运行在 WorkBuddy 平台侧（不在 git 里），所以这里提供：
可复用的**桥接脚本** + 一份**自动化配置模板**，团队 clone 后照着建即可。

---

## 1. 为什么需要桥接脚本（automations/bridge/）

能力脚本（seatable_sync.py / partdb_sync.py / cockpit.py）和 `data/` 都在
`seatable-production` 技能目录里，而我们希望自动化挂在 WorkBuddy 的
**「生产交付」项目**分组下运行 —— 这两个目录不是同一个。

桥接脚本（共 4 个文件）放在你的「生产交付」项目目录里，运行时自动转发到
真实的 seatable-production 技能目录，做到：
- UI 上自动化归属在「生产交付」分组；
- 脚本/数据仍在技能目录，升级技能不会被改写到项目里。

## 2. 真实技能目录的解析顺序（通用、跨机器）

`bridge_common.py` 按以下顺序定位 seatable-production：
1. 环境变量 `SEATABLE_PRODUCTION_DIR`（最优先，推荐团队显式设置）
2. `~/.workbuddy/skills/seatable-production`（WorkBuddy 默认技能位置）
3. 相对 `../seatable-production`

## 3. 部署步骤（团队照做）

1. **放置桥接脚本**：把 `automations/bridge/` 下的 4 个文件
   （`bridge_common.py` + `seatable_sync.py` / `partdb_sync.py` / `cockpit.py`）
   复制到你的 WorkBuddy「生产交付」项目目录（即自动化 cwds 的第一个目录）。
2. **（可选但推荐）设置环境变量**：
   `SEATABLE_PRODUCTION_DIR = <你机器上 seatable-production 技能目录的绝对路径>`
3. **在 WorkBuddy 新建每日自动化**，配置如下：
   - 名称：`生产驾驶舱·每日9点自动重建部署`
   - 调度（rrule）：`FREQ=DAILY;BYHOUR=9;BYMINUTE=0`
   - 状态：`ACTIVE`
   - 工作目录（cwds，按顺序）：
     1. 你的「生产交付」项目目录（决定 UI 分组归属）
     2. seatable-production 技能目录（保证脚本/数据可访问）
   - Prompt：见下方「自动化 Prompt 模板」
4. **首次运行验证**：手动触发一次，确认能从项目目录调通 wrapper 并生成
   `项目管理驾驶舱.html`。

## 4. 每日自动化 Prompt 模板

> 把下方的 `{{SEATABLE_PRODUCTION_DIR}}` 替换为你的实际技能目录绝对路径
> （或留作说明，因为 cwds 已保证脚本可从项目目录调起）。模板只规定流程，
> 不包含真实 token、UUID，也不等于授权自动写入或删除数据。

```
你是「生产交付驾驶舱」专家。请执行每日 9 点例行数据更新任务：

1. 在技能目录 {{SEATABLE_PRODUCTION_DIR}} 下同步最新数据：
   - 运行 python seatable_sync.py 拉取生产业务 Base 的 SeaTable 快照；
   - 运行 python partdb_sync.py 拉取 PartDB 实时库存与缺料。
2. 拉取并总结微信监控群消息：
   - 运行 python wechat_intake.py pull；
   - 运行 python wechat_intake.py summary --hours 24 --out data/wechat_intake/summary_24h.md；
   - 按 references/wx-ai-summary-prompt.md 生成 AI 总结和「待确认候选」，不要在自动化中执行 approve。
3. 对微信候选做双 Base 分流，逐条输出「目标 Base / 目标表 / 关键字段 / 原文依据」：
   - production：项目、生产、采购、库存、发货、质量等业务事实或业务记录候选；
   - tasks：待办、提醒、追问、负责人、截止日期、未闭环事项等执行事项候选；
   - 同一条消息同时包含业务事实和行动要求时允许拆成两条，分别进入 production/tasks；禁止为了省事把所有事项写进默认 Base。
   - 所有候选保持待确认；没有人工确认不得跨 Base 写入。
4. 处理消息证据：
   - 图片证据允许保留原图并在人工确认后上传到目标记录的图片/附件列，同时保留 OCR/视觉摘要、来源群、发送人、时间、哈希；“允许上传”不等于每日自动上传。
   - PDF、Word、Excel、文本等普通文件优先文本化，先保存提取文本/摘要和来源；只有文本化失败、必须核验版式/签章或用户明确要求时，才把原文件列为可上传证据。
5. 运行 python wxmatch.py scan 做消息与业务表只读核对；高置信项也只生成预填意图，等人工确认。
6. 运行 python alerts.py run、python foresee.py、python foresee.py review，刷新异常与预测。
7. 运行 python daily_brief.py --push 生成站会摘要和发件箱消息。
8. 运行 python cockpit.py 读取 data/ 重新生成「项目管理驾驶舱.html」（单文件、内联 CSS/JS，零外部依赖；访问口令取自 config.yaml 的 cockpit 段，重生成即生效）。
9. 生成简短摘要播报，除交期、缺料、在制品、现金流外，还要报告 production/tasks 两类微信候选数量、图片证据候选数、普通文件文本化成功/失败数；不得把待确认候选说成“已写入”。
10. 读取技能目录下的 config.yaml 的 cockpit 段，在私聊播报末尾附「当前生效访问口令清单」：
    - 主口令(admin_password)：<值>
    - 各角色口令(role_passwords)：boss / warehouse / purchase / production / sales 各自的值
    - 注明「口令取自 config.yaml，重生成即生效；若你已轮换请以此为准」。口令仅本地/私聊查看，勿推送群聊。

重要：
- 每日任务不得运行 evidence.py prune ... --yes；证据清理由季度任务先列候选，再由人工单独确认。
- 对外稳定链接（如 *.bj6.agentos-app.net）由 WorkBuddy 的 HTML 发布功能原地更新，请用本轮最新生成的「项目管理驾驶舱.html」重新发布到该链接，不要走 CloudStudio 沙箱部署。
- 若同步、文本化、生成或发布失败，记录错误原因并说明已完成/未完成步骤，不要静默中断，也不要因某一步失败改写真实配置。
```

## 5. 季度证据清理 Prompt 模板

建议每季度首个工作日运行一次。**自动化只扫描和报告候选，永远不带 `--yes`**：

```
你是「生产交付驾驶舱」专家。请执行季度微信证据保留检查：

1. 在技能目录 {{SEATABLE_PRODUCTION_DIR}} 运行：
   python evidence.py scan --root data/wechat_intake --days 90 --json
2. 只把同时满足以下条件的证据列为清理候选：超过 90 天、关联事项已闭环、未标记长期保留。
3. 按「证据编号 / 关联事项 / 日期 / 状态 / 路径 / 候选原因」输出清单，等待人工审核。
4. 不删除文件、不删元数据、不修改 SeaTable，不执行任何带 --yes 的命令。
5. 人工明确批准具体候选后，才允许在单独的人工会话运行：
   python evidence.py prune --root data/wechat_intake --days 90 --yes
   执行后报告实际删除项；未获批准则保持原状。

永不列入候选：未闭环、日期不明、90 天内、已标记长期/永久保留的证据。
```

> `scan` 和不带 `--yes` 的 `prune` 都只预览；真正删除必须显式输入 `--yes`。
> “季度”是复核频率，“90 天”是候选年龄阈值，两者不要混为一谈。

## 6. 安全须知

- **口令**：驾驶舱访问口令在仓库外的 `config.yaml`（已被 .gitignore 忽略），
  不会进 git；但每日自动化的播报文本里会附带当前口令，请勿把播报推送到群聊。
- **`.workbuddy/` 目录**（含自动化 memory、项目 memory、业务指标）已被
  `.gitignore` 忽略，**切勿 `git add` 提交或 push**，以免泄露口令与业务数据。
- 仓库已忽略：`config*.yaml`、`data/`、`*驾驶舱*.html`、`cockpit_passwords.json`。

## 7. 维护者本机实际配置（参考，非强制）

- 本机「生产交付」项目目录：`C:\Users\11430\WorkBuddy\2026-08-14-14-34-58`
  （里面是 3 个硬编码转发到技能目录的 wrapper；团队建议改用上方通用版）
- 技能目录：`C:\Users\11430\.workbuddy\skills\seatable-production`
- 自动化 cwds：`[生产交付目录, 技能目录]`；status：`ACTIVE`
