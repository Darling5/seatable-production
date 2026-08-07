#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
生产交付协同助手 — 统一数据操作 CLI（后端无关）。

所有增删改查都走这里，SKILL.md 不直接碰任何存储细节，也不出现任何凭证。
用法示例：
  python3 op.py list 生产计划
  python3 op.py list 生产计划 --where 状态=进行中
  python3 op.py append 生产计划 '{"生产产品":"4G小卡","数量":100,"关联项目":"项目A"}'
  python3 op.py update 生产计划 row_3 '{"状态":"已完成"}'
  python3 op.py delete 生产计划 row_3
  python3 op.py link 生产计划 PCB下单记录 row_3 row_7
  python3 op.py linked 生产计划 row_3
  python3 op.py meta 生产计划
  python3 op.py resolve-link 生产计划 PCB下单记录
  python3 op.py export-excel 生产数据.xlsx
  python3 op.py partdb-search 电容 10
  python3 op.py partdb-shortage 22 100
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from adapters.factory import load_config, get_adapter, get_partdb  # noqa: E402
from adapters import schema  # noqa: E402


def _print_rows(rows):
    if not rows:
        print("(空)")
        return
    # 打印为表格
    cols = []
    for r in rows:
        for k in r.keys():
            if k != "__row_id__" and k not in cols:
                cols.append(k)
    header = ["__row_id__"] + cols
    print("\t".join(header))
    for r in rows:
        print("\t".join(str(r.get(c, "")) for c in header))


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--config", default=None)
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list").add_argument("table")
    sp = sub.add_parser("query"); sp.add_argument("table"); sp.add_argument("--where", action="append", default=[])
    sp = sub.add_parser("append"); sp.add_argument("table"); sp.add_argument("json")
    sp = sub.add_parser("update"); sp.add_argument("table"); sp.add_argument("row_id"); sp.add_argument("json")
    sp = sub.add_parser("delete"); sp.add_argument("table"); sp.add_argument("row_ids", nargs="+")
    sp = sub.add_parser("link"); sp.add_argument("table"); sp.add_argument("other"); sp.add_argument("row_id"); sp.add_argument("other_row_ids", nargs="+")
    sp = sub.add_parser("linked"); sp.add_argument("table"); sp.add_argument("row_id")
    sub.add_parser("meta").add_argument("table")
    sp = sub.add_parser("resolve-link"); sp.add_argument("table"); sp.add_argument("other")
    sp = sub.add_parser("export-excel"); sp.add_argument("out", nargs="?", default="生产数据.xlsx")
    sp = sub.add_parser("partdb-search"); sp.add_argument("keyword"); sp.add_argument("limit", nargs="?", type=int, default=20)
    sp = sub.add_parser("partdb-shortage"); sp.add_argument("project_id", type=int); sp.add_argument("qty", type=int)

    args = p.parse_args()
    cfg = load_config(args.config)
    adapter = get_adapter(cfg)
    adapter.auth()

    if args.cmd == "list":
        _print_rows(adapter.list_rows(args.table))
    elif args.cmd == "query":
        filters = {}
        for w in args.where:
            k, _, v = w.partition("=")
            filters[k] = v
        _print_rows(adapter.query(args.table, filters))
    elif args.cmd == "append":
        data = json.loads(args.json)
        rid = adapter.append_row(args.table, data)
        print(f"OK row_id={rid}")
    elif args.cmd == "update":
        adapter.update_row(args.table, args.row_id, json.loads(args.json))
        print("OK")
    elif args.cmd == "delete":
        adapter.delete_rows(args.table, args.row_ids)
        print("OK")
    elif args.cmd == "link":
        lid = schema.link_id_for(args.table, args.other)
        adapter.link(args.table, args.other, lid, args.row_id, args.other_row_ids)
        print(f"OK linked {args.table}:{args.row_id} <-> {args.other}:{args.other_row_ids}")
    elif args.cmd == "linked":
        print(json.dumps(adapter.list_linked(args.table, args.row_id, ""), ensure_ascii=False))
    elif args.cmd == "meta":
        print(json.dumps(adapter.get_metadata(args.table), ensure_ascii=False, indent=2))
    elif args.cmd == "resolve-link":
        print(schema.link_id_for(args.table, args.other) or "(local 无独立 link_id，写关联时自动解析)")
    elif args.cmd == "export-excel":
        try:
            from openpyxl import Workbook
        except Exception:
            print("导出 Excel 需要 openpyxl：pip install openpyxl", file=sys.stderr); sys.exit(1)
        wb = Workbook(); wb.remove(wb.active)
        for t in schema.TABLES:
            ws = wb.create_sheet(title=t[:31])
            rows = adapter.list_rows(t)
            if not rows:
                ws.append(["(空)"]); continue
            cols = [c for c in rows[0].keys() if c != "__row_id__"]
            ws.append(["__row_id__"] + cols)
            for r in rows:
                ws.append([r.get("__row_id__", "")] + [r.get(c, "") for c in cols])
        out = args.out if os.path.isabs(args.out) else os.path.join(os.path.dirname(os.path.abspath(__file__)), args.out)
        wb.save(out)
        print(f"OK 导出到 {out}")
    elif args.cmd == "partdb-search":
        pd = get_partdb(cfg)
        if not pd:
            print("PartDB 未启用（config.yaml 中 partdb.enabled=false 或留空）。已跳过缺料检查。")
            return
        for p in pd.search_parts(args.keyword, args.limit):
            print(f"{p.get('name')} | 料号:{p.get('ipn')} | 库存:{p.get('total_instock')}")
    elif args.cmd == "partdb-shortage":
        pd = get_partdb(cfg)
        if not pd:
            print("PartDB 未启用，无法做缺料检查。")
            return
        for s in pd.shortage(args.project_id, args.qty):
            print(f"缺料: {s['name']} 料号:{s['ipn']} 需{s['need']} 现有{s['stock']} 缺口{s['gap']}")


if __name__ == "__main__":
    main()
