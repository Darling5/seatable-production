# 作为 WorkBuddy 项目导入

本仓库 = `seatable-production` 技能 + `cockpit.py` 驾驶舱生成器。
按以下步骤即可在 WorkBuddy「我的项目」中获得一个完整的「生产交付协同」项目
（含 SeaTable / PartDB 技能 + 项目管理驾驶舱工作台）。

## 方式一：GitHub 链接一键导入（推荐）

1. 复制本仓库地址：`https://github.com/Darling5/seatable-production`
2. 在 WorkBuddy「我的项目」中粘贴链接 → 新建项目。
3. 项目创建后自动挂载 `seatable-production` 技能（仓库根的 `SKILL.md` 会被识别）。
4. 在项目中运行 `python cockpit.py`，生成 `项目管理驾驶舱.html` 工作台。
5. 项目创建者在 UI 中设置管理员 / 成员；把不同 `#role` 分享链接发给对应成员。

> 若导入器需要特定清单文件，见仓库根 `workbuddy.project.yaml`
> （当前为拟定格式，按 WorkBuddy 官方导入规范校正即可）。

## 方式二：手动搭建

1. 安装技能：把本仓库放到 `~/.workbuddy/skills/seatable-production/`。
2. 配置 `config.yaml`（local / seatable / partdb 三种后端，详见 `SKILL.md`）。
3. 生成驾驶舱：`python cockpit.py` → 产出 `项目管理驾驶舱.html`。
4. 部署：`workbuddy_cloudstudio_deploy` 部署为在线工作台，
   或把 HTML 作为项目工作台资源直接打开。

## 工作台与技能的「联动」说明

- 驾驶舱由技能内的 `cockpit.py` 生成，读取与技能**同一份** `data/` 与 `config.yaml`，
  因此**技能数据一旦更新**（`op.py` / `seatable_sync.py` / 每日 9 点自动任务），
  重新生成驾驶舱即同步，无需手动搬数据。
- 5 个角色视图（老板 / 仓库 / 采购 / 生产经理 / 销售）各自独立口令，
  分享链接带 `#role` 锚点，对方打开直接落在自己视角。
- 管理员口令（`ZHWL8888`）可看全部角色、可切换、可一键轮换口令。

## 数据安全

- 仓库**不含真实业务数据**：`data/` 与 `config.yaml`（含 token）按 `.gitignore` 排除。
- 请勿把生成的、含真实数据的 `项目管理驾驶舱.html` 提交到公开仓库。
- 驾驶舱口令为客户端 base64 混淆（防误看，非真加密）；公共仓库的默认口令应自行替换。
