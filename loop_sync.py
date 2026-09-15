# -*- coding: utf-8 -*-
"""loop_sync.py — 把本地业务闭环控制平面（data/business_loop/*.csv）同步到 SeaTable CRM 库。

这是「控制平面 → 真实云端表」的最后一公里：
  本地 CSV（objects/transitions/evidence/approvals）
    → CRM Base 四张表：业务对象台账 / 状态轨迹 / 证据链 / 审批记录

设计要点：
  · 幂等 upsert：按主键（object_id / transition_id / event_id / approval_id）比对，
    新增则 append，已有则按整行比对、有变化才 update——重复运行零重复、零噪音。
  · 建表幂等：表不存在时自动创建（列定义来自 adapters/schema.py），已存在则跳过。
  · 安全闸门：默认 dry-run；--yes 才真正写云端。与 business_loop 的 preview/apply 同风格。

用法：
  python loop_sync.py            # dry-run：打印将建表/新增/更新的行数
  python loop_sync.py --yes      # 实际写入 SeaTable CRM 库
  python loop_sync.py --tables objects   # 只同步一张控制平面表
"""
from __future__ import annotations

import argparse
import json
import os
import sys

from adapters import schema
from domain.order_to_cash import BusinessStore

# 控制平面表名 -> (本地 CSV 表名, 云端 SeaTable 表名, 主键列, 列定义常量)
MAPPING = {
    "objects":     ("objects",     "业务对象台账", "object_id",     "BUSINESS_OBJECT_COLUMNS"),
    "transitions": ("transitions", "状态轨迹",     "transition_id", "STATE_TRANSITION_COLUMNS"),
    "evidence":    ("evidence",    "证据链",       "event_id",      "EVIDENCE_CHAIN_COLUMNS"),
    "approvals":   ("approvals",   "审批记录",     "approval_id",   "APPROVAL_RECORD_COLUMNS"),
}

DEFAULT_DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "data", "business_loop")


def _columns_of(cloud_table: str) -> list[dict]:
    """云端表列定义（建表用）。所有列统一用 text：控制平面是台账，不做类型魔法。"""
    cols = getattr(schema, MAPPING[_local_name(cloud_table)][3])
    return [{"column_name": c, "column_type": "text"} for c in cols]


def _local_name(cloud_table: str) -> str:
    for local, (_l, cloud, _k, _c) in MAPPING.items():
        if cloud == cloud_table:
            return local
    raise KeyError("未知云端表：%s" % cloud_table)


def ensure_tables(adapter, apply: bool = False) -> dict:
    """确保四张云端表存在。返回 {云端表名: "created"/"exists"}。"""
    adapter.auth()
    adapter._ensure_meta()
    existing = {t["name"] for t in adapter._meta["tables"]}
    result = {}
    for local, (_l, cloud, _key, _cols) in MAPPING.items():
        if cloud in existing:
            result[cloud] = "exists"
            continue
        if not apply:
            result[cloud] = "would_create"
            continue
        import requests
        r = requests.post(
            adapter._base() + "/tables/",
            headers={**adapter._h, "Content-Type": "application/json"},
            json={"table_name": cloud, "columns": _columns_of(cloud)}, timeout=30)
        r.raise_for_status()
        adapter._meta = None  # 失效缓存，下次重拉
        result[cloud] = "created"
    return result


def _row_signature(row: dict, fields: list[str]) -> str:
    return json.dumps({f: str(row.get(f, "") or "") for f in fields},
                      ensure_ascii=False, sort_keys=False)


def sync_table(adapter, cloud_table: str, local_rows: list[dict],
               apply: bool = False) -> dict:
    """单表幂等 upsert。返回统计。"""
    _l, cloud, key, cols_attr = MAPPING[_local_name(cloud_table)]
    fields = list(getattr(schema, cols_attr))
    cloud_rows = adapter.list_rows(cloud_table)
    by_key = {r.get(key, ""): r for r in cloud_rows if r.get(key)}

    to_append, to_update, unchanged = [], [], 0
    for row in local_rows:
        k = str(row.get(key, "") or "")
        if not k:
            continue
        existing = by_key.get(k)
        payload = {f: str(row.get(f, "") or "") for f in fields if f != "__row_id__"}
        if existing is None:
            to_append.append(payload)
        elif _row_signature(existing, fields) != _row_signature(payload, fields):
            to_update.append((existing["__row_id__"], payload))
        else:
            unchanged += 1

    stats = {"table": cloud, "local": len(local_rows), "cloud": len(cloud_rows),
             "append": len(to_append), "update": len(to_update), "unchanged": unchanged}
    if apply:
        for payload in to_append:
            adapter.append_row(cloud, payload)
        for row_id, payload in to_update:
            adapter.update_row(cloud, row_id, payload)
    return stats


def sync(apply: bool = False, only: set[str] | None = None,
         data_dir: str = DEFAULT_DATA_DIR, adapter=None) -> dict:
    """主入口。adapter 缺省时从 config.yaml 解析 CRM Base。"""
    if adapter is None:
        from adapters.factory import get_adapter
        adapter = get_adapter(base_name="crm")
        if not hasattr(adapter, "auth"):  # 退回 local 模式了
            return {"error": "未配置 CRM Base（seatable.bases.crm），无法同步云端。"}

    store = BusinessStore(data_dir)
    tables_status = ensure_tables(adapter, apply=apply)
    report = {"tables": tables_status, "stats": [], "applied": apply}
    for local in MAPPING:
        if only and local not in only:
            continue
        rows = store.read(local)
        cloud = MAPPING[local][1]
        if tables_status.get(cloud) == "would_create":
            # dry-run 且表尚未建：云端必然为空，全部计为新增
            report["stats"].append({"table": cloud, "local": len(rows), "cloud": 0,
                                    "append": len(rows), "update": 0, "unchanged": 0})
            continue
        # 表已存在或刚建好（空表）→ 正常 upsert（list_rows 对空表返回 []）
        report["stats"].append(sync_table(adapter, cloud, rows, apply=apply))
    return report


def main():
    ap = argparse.ArgumentParser(description="控制平面 → SeaTable CRM 同步")
    ap.add_argument("--yes", action="store_true", help="实际写入（默认 dry-run）")
    ap.add_argument("--tables", help="只同步指定控制平面表：objects,transitions,evidence,approvals")
    ap.add_argument("--data-dir", default=DEFAULT_DATA_DIR)
    args = ap.parse_args()
    only = set(t.strip() for t in args.tables.split(",")) if args.tables else None

    report = sync(apply=args.yes, only=only, data_dir=args.data_dir)
    if "error" in report:
        print("[!] %s" % report["error"])
        sys.exit(2)

    mode = "APPLY" if args.yes else "DRY-RUN"
    print("→ 控制平面 → SeaTable CRM（%s）" % mode)
    for cloud, st in report["tables"].items():
        mark = {"exists": "✓ 已存在", "created": "＋ 已创建",
                "would_create": "＋ 将创建"}[st]
        print("  %s %s" % (mark, cloud))
    total_new = total_upd = 0
    for st in report["stats"]:
        print("  · %-8s 本地 %-3d 云端 %-3d | 新增 %-3d 更新 %-3d 不变 %-3d"
              % (st["table"], st["local"], st["cloud"], st["append"], st["update"], st["unchanged"]))
        total_new += st["append"]
        total_upd += st["update"]
    if args.yes:
        print("✅ 同步完成：新增 %d 行，更新 %d 行" % (total_new, total_upd))
    else:
        print("（dry-run，加 --yes 执行写入）")


if __name__ == "__main__":
    main()
