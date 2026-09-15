#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
sync_expert.py — 维护专家包内嵌的「壳」技能档案（v1.8.3 架构收口后）。

背景：本仓库同时是「技能」和「专家包」。v1.8.3 前专家包
`expert/production-cockpit/skills/seatable-production/` 内嵌了 51 文件全量副本，
每改一个文件要同步两份，曾发生 5 文件漂移。v1.8.3 起改为**壳模式**：
内嵌目录只放一个转介 SKILL.md（真源在主技能目录），漂移面 = 0。

壳 SKILL.md 的内容是「真源路径 + 触发条件 + 命令卡转介」，由本脚本从
模板生成；如果有人手改了壳文件，--check 会报漂移。

用法：
    python sync_expert.py          # 重建/修复壳（以模板为准覆盖）
    python sync_expert.py --check  # 只检查壳是否被改动（CI 用，不改文件）
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_DEST = os.path.join(_HERE, "expert", "production-cockpit", "skills", "seatable-production")

_TRUE_SOURCE = "C:/Users/11430/.workbuddy/skills/seatable-production-1.8.0"

# 壳里允许存在的文件（其余出现即视为漂移残留，sync 时删除）
SHELL_FILES = {"SKILL.md"}

# 壳模板：每次 sync 重新生成，保证主技能路径等关键信息不失真
_SHELL = """---
name: seatable-production
description: 生产交付协同（SeaTable 22 张业务表读写、项目/生产/采购/发货/售后全链路核对、驾驶舱生成）。当用户提到生产数据、项目状态、采购记录、发货、库存、驾驶舱时触发。本目录是专家包内嵌档案，真源在主技能 {ts}——执行命令前先读主技能 SKILL.md。
---

# seatable-production（专家内嵌档案 · 真源转介）

## ⚠️ 本目录不是真源

**技能真源（全部脚本、references、测试、data/）在：**

```
{ts}/
```

**本目录故意只留一个文件**（v1.8.3 架构收口）：技能完整内容不再复制到专家包，
避免「改一个文件同步两份」的漂移问题。历史上曾发生 5 文件漂移，改壳后漂移面 = 0。

## 专家触发本技能时怎么做

1. **读主技能真源**：`Read {ts}/SKILL.md`
   （约 40KB：铁律、表级规则、命令路由表）
2. **按需读 references/**（主技能目录下）：
   - `references/wx-intake-and-check.md` — 微信情报 / 消息↔SeaTable 核对 / 风险预测
   - `references/won-deal.md` — CRM 赢单转生产立项
   - `references/cockpit-advanced.md` — 驾驶舱在线模式 / 前端增强 / 架构收口 / 业务闭环
   - `references/changelog.md` — 完整版本历史与踩坑归档
3. **所有命令在主技能目录执行**（脚本、config.yaml、data/ 快照都在那）：
   ```bash
   cd {ts} && python op.py listrows 项目表
   ```
4. **查价类需求转介独立子技能 price-sensor**（薄指针，脚本同在主技能目录）。

## 快速命令卡（最常用 6 条）

```bash
cd {ts}

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
"""


def _shell_content():
    return _SHELL.replace("{ts}", _TRUE_SOURCE)


def main():
    check_only = "--check" in sys.argv
    if not os.path.isdir(_DEST):
        print("[skip] 未找到 expert 内嵌副本目录，无需同步。")
        return 0

    issues = []
    shell = _shell_content()

    # 1) SKILL.md 必须存在且与模板一致
    skill = os.path.join(_DEST, "SKILL.md")
    if not os.path.exists(skill):
        issues.append(("missing", "SKILL.md"))
    elif open(skill, encoding="utf-8").read() != shell:
        issues.append(("drifted", "SKILL.md"))

    # 2) 壳里不允许出现 SHELL_FILES 之外的文件
    for root, dirs, files in os.walk(_DEST):
        if ".git" in root:
            continue
        for f in files:
            rel = os.path.relpath(os.path.join(root, f), _DEST).replace(os.sep, "/")
            if rel not in SHELL_FILES:
                issues.append(("extra", rel))

    if check_only:
        if issues:
            print("专家壳档案异常，请运行 `python sync_expert.py` 修复：")
            for kind, rel in issues:
                tag = {"missing": "[缺失]", "drifted": "[被改动]", "extra": "[残留]"}[kind]
                print("  %s %s" % (tag, rel))
            return 1
        print("OK 专家壳档案完整（单文件转介模式）")
        return 0

    # 修复模式：重写壳 + 清残留
    fixed = []
    os.makedirs(_DEST, exist_ok=True)
    if not os.path.exists(skill) or open(skill, encoding="utf-8").read() != shell:
        open(skill, "w", encoding="utf-8", newline="\n").write(shell)
        fixed.append("SKILL.md（重写为模板）")
    for root, dirs, files in os.walk(_DEST, topdown=False):
        if ".git" in root:
            continue
        for f in files:
            rel = os.path.relpath(os.path.join(root, f), _DEST).replace(os.sep, "/")
            if rel not in SHELL_FILES:
                os.remove(os.path.join(root, f))
                fixed.append(rel + "（删除残留）")
        if root != _DEST and not os.listdir(root):
            os.rmdir(root)

    if fixed:
        print("已修复专家壳档案 %d 处：" % len(fixed))
        for f in fixed:
            print("  · " + f)
    else:
        print("OK 专家壳档案完整（单文件转介模式）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
