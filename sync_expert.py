#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
sync_expert.py — 把主库的技能文件同步到 expert 内嵌副本。

背景：本仓库同时是「技能」和「专家包」。专家包
`expert/production-cockpit/skills/seatable-production/` 里内嵌了一份技能副本，
发布专家时会被打包带走。两边一旦漂移，用户装了专家却拿到旧技能。

用法：
    python sync_expert.py          # 同步并报告
    python sync_expert.py --check  # 只检查是否漂移（CI 用，不改文件）

只同步代码与文档，**绝不同步** data/ config.yaml 等本地数据与凭证。
"""
import filecmp
import os
import shutil
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_DEST = os.path.join(_HERE, "expert", "production-cockpit", "skills", "seatable-production")

# 需要保持一致的文件（相对主库根目录）
FILES = [
    "SKILL.md", "README.md", "LICENSE",
    "cockpit.py", "op.py", "setup.py", "seed_demo.py", "test_smoke.py",
    "intake.py", "doctor.py",
    "market.py", "suppliers.py", "test_market_sync.py",
    "commodities.py", "test_commodities.py",
    "wechat_intake.py", "wxmatch.py", "test_wxmatch.py",
    "wxengine/wa_db.py",
    "seatable_sync.py", "partdb_sync.py", "backfill_seatable.py",
    "foresee.py",
    "config.yaml.example",
    "adapters/__init__.py", "adapters/factory.py", "adapters/schema.py",
    "adapters/local.py", "adapters/seatable.py", "adapters/partdb.py",
    "docs/manual.md", "docs/usage-guide.html",
]

# 绝不同步：本地数据、凭证、缓存、产物
# 注意用「路径段精确匹配」，不能用子串——config.yaml.example 是要同步的模板，
# 而 config.yaml 是真凭证，两者只差一个后缀。
NEVER_EXACT = {"config.yaml", "config.yml", "cockpit_passwords.json"}
NEVER_PREFIX = ("data/", "__pycache__/")


def _is_forbidden(rel):
    if rel in NEVER_EXACT:
        return True
    if any(rel.startswith(p) for p in NEVER_PREFIX):
        return True
    return "驾驶舱" in rel and rel.endswith(".html")


def _iter_targets():
    for rel in FILES:
        src = os.path.join(_HERE, rel)
        if not os.path.exists(src):
            continue
        yield rel, src, os.path.join(_DEST, rel.replace("/", os.sep))


def main():
    check_only = "--check" in sys.argv
    if not os.path.isdir(_DEST):
        print("[skip] 未找到 expert 内嵌副本目录，无需同步。")
        return 0

    for rel in FILES:
        assert not _is_forbidden(rel), "危险：%s 命中禁同步清单" % rel

    drift, copied, missing = [], [], []
    for rel, src, dst in _iter_targets():
        if not os.path.exists(dst):
            missing.append(rel)
        elif not filecmp.cmp(src, dst, shallow=False):
            drift.append(rel)
        else:
            continue
        if not check_only:
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(src, dst)
            copied.append(rel)

    if check_only:
        if drift or missing:
            print("专家内嵌副本已漂移，请运行 `python sync_expert.py` 同步：")
            for r in missing:
                print("  [缺失] " + r)
            for r in drift:
                print("  [不一致] " + r)
            return 1
        print("OK 专家内嵌副本与主库一致")
        return 0

    if copied:
        print("已同步 %d 个文件到专家内嵌副本：" % len(copied))
        for r in copied:
            print("  · " + r)
    else:
        print("OK 无需同步，两边已一致")
    return 0


if __name__ == "__main__":
    sys.exit(main())
