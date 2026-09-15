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
   （约 40KB：铁律、表级规则、命令路由表）
2. **按需读 references/**（主技能目录下）：
   - `references/wx-intake-and-check.md` — 微信情报 / 消息↔SeaTable 核对 / 风险预测
   - `references/won-deal.md` — CRM 赢单转生产立项
   - `references/cockpit-advanced.md` — 驾驶舱在线模式 / 前端增强 / 架构收口 / 业务闭环
   - `references/changelog.md` — 完整版本历史与踩坑归档
3. **所有命令在主技能目录执行**（脚本、config.yaml、data/ 快照都在那）：
   ```bash
   cd C:/Users/11430/.workbuddy/skills/seatable-production-1.8.0 && python op.py listrows 项目表
   ```
4. **查价类需求转介独立子技能 price-sensor**（薄指针，脚本同在主技能目录）。

## 快速命令卡（最常用 6 条）

```bash
cd C:/Users/11430/.workbuddy/skills/seatable-production-1.8.0

python op.py listrows 项目表                    # 读表
python op.py update 项目表 <行ID> 状态:已发货    # 写表（列名必须精确）
python cockpit.py                              # 生成驾驶舱 HTML（单文件，离线可用）
python deploy.py --help                        # 部署/发布相关
python doctor.py                               # 环境自检
python tests/test_smoke.py                     # 166 项冒烟测试
```

## 边界

- 本目录**不放任何脚本**——放一个就多一分漂移风险
- data/、config.yaml（凭证）永远只在主技能目录，绝不进专家包
- 主技能版本 v1.8.3（P0 瘦身 + price-sensor 拆分收口）
