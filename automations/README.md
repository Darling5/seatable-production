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

## 4. 自动化 Prompt 模板

> 把下方的 `{{SEATABLE_PRODUCTION_DIR}}` 替换为你的实际技能目录绝对路径
> （或留作说明，因为 cwds 已保证脚本可从项目目录调起）。

```
你是「生产交付驾驶舱」专家。请执行每日 9 点例行数据更新任务：

1. 在技能目录 {{SEATABLE_PRODUCTION_DIR}} 下同步最新数据：
   - 运行 python seatable_sync.py 拉取 15 张 SeaTable 云表快照；
   - 运行 python partdb_sync.py 拉取 PartDB 实时库存与缺料。
2. 运行 python cockpit.py 读取 data/ 重新生成「项目管理驾驶舱.html」（单文件、内联 CSS/JS，零外部依赖；访问口令取自 config.yaml 的 cockpit 段，重生成即生效）。
3. 生成简短摘要播报（交期达成率、缺料预警、在制品瓶颈、现金流/应收款等关键指标）。
4. 读取技能目录下的 config.yaml 的 cockpit 段，在播报末尾附「当前生效访问口令清单」：
   - 主口令(admin_password)：<值>
   - 各角色口令(role_passwords)：boss / warehouse / purchase / production / sales 各自的值
   - 注明「口令取自 config.yaml，重生成即生效；若你已轮换请以此为准」。提醒：口令仅本地/私聊查看，勿推送到群聊以免泄露。

重要：对外稳定链接（如 *.bj6.agentos-app.net）由 WorkBuddy 的 HTML 发布功能原地更新，请用本轮最新生成的「项目管理驾驶舱.html」重新发布到该链接即可，不要走 CloudStudio 沙箱部署（那会生成新的 app.workbuddy.link 域名，与稳定链接无关）。若同步或生成失败，记录错误原因并说明已完成/未完成步骤，不要静默中断。
```

## 5. 安全须知

- **口令**：驾驶舱访问口令在仓库外的 `config.yaml`（已被 .gitignore 忽略），
  不会进 git；但每日自动化的播报文本里会附带当前口令，请勿把播报推送到群聊。
- **`.workbuddy/` 目录**（含自动化 memory、项目 memory、业务指标）已被
  `.gitignore` 忽略，**切勿 `git add` 提交或 push**，以免泄露口令与业务数据。
- 仓库已忽略：`config*.yaml`、`data/`、`*驾驶舱*.html`、`cockpit_passwords.json`。

## 6. 维护者本机实际配置（参考，非强制）

- 本机「生产交付」项目目录：`C:\Users\11430\WorkBuddy\2026-08-14-14-34-58`
  （里面是 3 个硬编码转发到技能目录的 wrapper；团队建议改用上方通用版）
- 技能目录：`C:\Users\11430\.workbuddy\skills\seatable-production`
- 自动化 cwds：`[生产交付目录, 技能目录]`；status：`ACTIVE`
