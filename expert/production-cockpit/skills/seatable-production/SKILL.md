---
name: seatable-production
description: 生产交付协同（SeaTable 22 张业务表读写、项目/生产/采购/发货/售后全链路核对、驾驶舱生成）。当用户提到生产数据、项目状态、采购记录、发货、库存、驾驶舱时触发。本目录是专家包内嵌档案，真源在主技能 C:/Users/11430/.workbuddy/skills/seatable-production-1.8.0——执行命令前先读主技能 SKILL.md。
---

# seatable-production（专家内嵌档案 · 真源转介）

## ⚠️ 本目录不是真源

**技能真源（全部脚本、references、测试、data/）在：**

```
C:/Users/11430/.workbuddy/skills/seatable-production-1.8.0/
```

**本目录故意只留一个文件**（v1.8.3 架构收口）：技能完整内容不再复制到专家包，
避免「改一个文件同步两份」的漂移问题。历史上曾发生 5 文件漂移，改壳后漂移面 = 0。

## 专家触发本技能时怎么做

1. **读主技能真源**：`Read C:/Users/11430/.workbuddy/skills/seatable-production-1.8.0/SKILL.md`
   （约 48KB：铁律、表级规则、命令路由表）
2. **按需读 references/**（主技能目录下）：
   - `references/wx-intake-and-check.md` — 微信情报 / 消息↔SeaTable 核对 / 风险预测
   - `references/won-deal.md` — CRM 赢单转生产立项
   - `references/cockpit-advanced.md` — 驾驶舱在线模式 / 前端增强 / 架构收口 / 业务闭环
   - `references/changelog.md` — 完整版本历史与踩坑归档
3. **按需读 docs/contracts/**（二期/三期契约与样例）：
   - `docs/contracts/project-brain-v1.md` — 二期「单项目第二大脑」
   - `docs/contracts/decision-support-v1.md` — 三期「项目决策辅助」
   - `docs/trusted-execution-v1.md` — 可信执行层（授权 / 账本 / 发布门禁）
4. **所有命令在主技能目录执行**（脚本、config.yaml、data/ 快照都在那）：
   ```bash
   cd C:/Users/11430/.workbuddy/skills/seatable-production-1.8.0 && python domain/op.py list 项目表
   ```
5. **查价类需求转介独立子技能 price-sensor**（薄指针，脚本同在主技能目录）。
6. **驾驶舱生成/部署/在线模式转介独立子技能 cockpit-studio**（薄指针，脚本同在主技能目录）。

## 快速命令卡（最常用 8 条）

> ⚠️ **v2.0.0 起脚本按功能域归组**，命令一律带域前缀（`domain/` `cockpit/` `tools/` …）。
> 仓库根目录只留 `project_brain.py` 与 `decision_support.py` 两个总入口。

```bash
cd C:/Users/11430/.workbuddy/skills/seatable-production-1.8.0

python domain/op.py list 项目表                  # 读表
python domain/op.py update 项目表 <行ID> '{"状态":"已发货"}'   # 写表（列名必须精确）
python cockpit/cockpit.py                        # 生成驾驶舱 HTML（单文件，离线可用）
python workflows/workflow.py run daily --mode preview   # 跑统一工作流（演练，不写任何东西）
python project_brain.py doctor                   # 二期自检
python decision_support.py demo                  # 三期合成样例全链路（只读）
python tools/doctor.py                           # 环境自检
python tests/test_smoke.py                       # 172 项零依赖冒烟检查
```

完整命令速查见主技能 `SKILL.md` §11。

## 边界

- 本目录**不放任何脚本**——放一个就多一分漂移风险
- data/、config.yaml（凭证）永远只在主技能目录，绝不进专家包
- 主技能版本 **v2.0.0**（可信执行层 + 二期第二大脑 + 三期决策辅助 + 按域归组）
