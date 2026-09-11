#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""CRM 自动录入引擎（v1.9 执行原则：自动写入 + 台账核对）。

用户宗旨（2026-09-11 定稿）：「你把所有相关信息列出来，我只负责核对，
其他的写入动作能自动化的全自动化」。本模块因此改变 v1.8 的
preview_only 原则：对 CRM Base（销售线索/跟进记录）执行**自动写入**，
但每一步都写本地台账 data/crm_dispatch_ledger.csv 供事后核对与回滚。

能力：
1. lead(来单)     —— 新客户来单：自动查重 → 建/复用销售线索 → 自动关联跟进记录
2. follow(跟进)   —— 为已有线索追加跟进记录（报价、谈判、投标结果等）
3. ledger         —— 打印本地台账（写入历史，供人工核对）
4. status         —— 读取线索当前状态（跟进状态/最新跟进时间）

写入规则（从 2026-09-11 云南亚雄实操沉淀）：
- 行写入必须用**中文列名**（列 key 会静默失败 HTTP 200 + 0 行）
- link 关联的 link_id 取自列定义 data.link_id（如 aAjT），不是列 key
- 销售线索表必填：客户名称/联系人/联系方式；跟进记录必填：跟进内容
- 单选字段直接写中文选项名，API 自动匹配
- 查重键：客户名称（精确）+ 联系方式（精确）
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import os
import sys
from collections.abc import Mapping, Sequence
from typing import Any

HERE = os.path.dirname(os.path.abspath(__file__))
LEDGER_FILE = os.path.join(HERE, "data", "crm_dispatch_ledger.csv")
LEDGER_FIELDS = ["时间", "动作", "客户名称", "row_id", "表格", "内容摘要", "撤销", "备注"]

# CRM Base 固定结构（2026-09-11 实测）
LEAD_TABLE = "销售线索表"
FOLLOW_TABLE = "客户线索跟进记录"
LEAD_LINK_ID = "aAjT"  # 销售线索表↔跟进记录 link（列定义实测值）

LEAD_FIELDS = {  # 销售线索表可写字段（中文名）
    "客户名称", "客户类型", "联系人", "联系方式",
}
FOLLOW_FIELDS = {  # 跟进记录可写字段（中文名）
    "跟进人姓名（统计用）", "跟进状态", "本次跟进内容", "下次跟进日期",
}
FOLLOW_STATUS = (  # 跟进状态单选合法值
    "初步沟通", "产品演示", "体验测试", "准备采购", "商务谈判",
    "停止跟进", "低频跟进", "已成交",
)


def _now() -> str:
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _get_adapter():
    from adapters.factory import load_config, get_adapter
    cfg = load_config()
    a = get_adapter(cfg, base_name="crm")
    a.auth()
    return a


def _ledger_append(action: str, customer: str, row_id: str, table: str,
                   summary: str, note: str = "") -> None:
    os.makedirs(os.path.dirname(LEDGER_FILE), exist_ok=True)
    new_file = not os.path.exists(LEDGER_FILE)
    with open(LEDGER_FILE, "a", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        if new_file:
            w.writerow(LEDGER_FIELDS)
        w.writerow([_now(), action, customer, row_id, table, summary, "", note])


def _clean_fields(data: Mapping[str, Any], allowed: set[str]) -> dict[str, Any]:
    return {k: v for k, v in (data or {}).items()
            if k in allowed and v not in (None, "")}


def find_lead(a, customer: str, phone: str = "") -> dict[str, Any] | None:
    """按客户名称（精确）+ 联系方式查重。返回线索行或 None。"""
    for row in a.list_rows(LEAD_TABLE):
        if str(row.get("客户名称", "")).strip() == customer.strip():
            if not phone or not str(row.get("联系方式", "")).strip() \
               or str(row.get("联系方式", "")).strip() == phone.strip():
                return row
    return None


def create_lead(a, data: Mapping[str, Any]) -> dict[str, Any]:
    """自动创建销售线索（已查重）。返回 {"row_id", "action"}。"""
    fields = _clean_fields(data, LEAD_FIELDS)
    if not fields.get("客户名称"):
        return {"error": "缺少客户名称"}
    existing = find_lead(a, fields["客户名称"], fields.get("联系方式", ""))
    if existing:
        return {"row_id": existing.get("__row_id__"), "action": "reused",
                "note": "客户已存在，复用现有线索"}
    row_id = a.append_row(LEAD_TABLE, fields)
    if not row_id:
        return {"error": "写入失败（返回空 row_id）"}
    _ledger_append("create_lead", fields.get("客户名称", ""), row_id,
                   LEAD_TABLE, json.dumps(fields, ensure_ascii=False)[:200])
    return {"row_id": row_id, "action": "created"}


def add_follow(a, lead_row_id: str, data: Mapping[str, Any]) -> dict[str, Any]:
    """为线索追加跟进记录并自动关联。"""
    fields = _clean_fields(data, FOLLOW_FIELDS)
    if not fields.get("本次跟进内容"):
        return {"error": "缺少本次跟进内容"}
    status = str(fields.get("跟进状态", "")).strip()
    if status and status not in FOLLOW_STATUS:
        return {"error": "跟进状态非法：%r（合法：%s）" % (status, "|".join(FOLLOW_STATUS))}
    follow_id = a.append_row(FOLLOW_TABLE, fields)
    if not follow_id:
        return {"error": "跟进记录写入失败"}
    # 自动关联：只用单向调用（跟进记录→销售线索）。
    # ⚠️ adapter.link() 的双向调用会把线索的「跟进记录」列表整体替换，
    #    导致历史跟进记录的关联被冲掉（2026-09-11 实测踩坑）。
    #    单向设置跟进记录的「客户线索」字段，SeaTable 自动维护反向追加。
    _link_single(a, follow_id, lead_row_id)
    _ledger_append("add_follow", "", follow_id, FOLLOW_TABLE,
                   fields.get("本次跟进内容", "")[:200],
                   "lead_row_id=" + lead_row_id)
    return {"row_id": follow_id, "action": "created"}


def _link_single(a, follow_row_id: str, lead_row_id: str) -> None:
    """单向设置跟进记录的「客户线索」关联（不动线索侧，避免替换历史）。

    测试桩适配器（带 stub_mode 标记）走内存路径。
    """
    if getattr(a, "stub_mode", False):
        a.link(FOLLOW_TABLE, LEAD_TABLE, LEAD_LINK_ID, follow_row_id, [lead_row_id])
        return
    import requests
    r = requests.put(a._base() + "/links/", headers={**a._h, "Content-Type": "application/json"},
                     json={
                         "link_id": LEAD_LINK_ID,
                         "table_name": FOLLOW_TABLE,
                         "other_table_name": LEAD_TABLE,
                         "row_id_list": [follow_row_id],
                         "other_rows_ids_map": {follow_row_id: [lead_row_id]},
                     }, timeout=30)
    r.raise_for_status()


def cmd_lead(args) -> int:
    a = _get_adapter()
    data = json.loads(args.data)
    result = create_lead(a, data)
    print(json.dumps(result, ensure_ascii=False))
    return 1 if result.get("error") else 0


def cmd_follow(args) -> int:
    a = _get_adapter()
    data = json.loads(args.data)
    lead = find_lead(a, args.customer)
    if not lead:
        print(json.dumps({"error": "线索不存在，请先 lead 建立线索"}, ensure_ascii=False))
        return 1
    result = add_follow(a, lead["__row_id__"], data)
    if result.get("action") == "created":
        result["lead_row_id"] = lead["__row_id__"]
        result["customer"] = args.customer
    print(json.dumps(result, ensure_ascii=False))
    return 1 if result.get("error") else 0


def cmd_ledger(_args) -> int:
    if not os.path.exists(LEDGER_FILE):
        print("（台账为空）")
        return 0
    with open(LEDGER_FILE, encoding="utf-8-sig") as f:
        rows = list(csv.reader(f))
    print("CRM 自动写入台账（%d 条）：" % (len(rows) - 1))
    for r in rows:
        print(" | ".join(c[:36] for c in r))
    return 0


def cmd_status(args) -> int:
    a = _get_adapter()
    lead = find_lead(a, args.customer)
    if not lead:
        print(json.dumps({"error": "线索不存在"}, ensure_ascii=False))
        return 1
    out = {
        "客户名称": lead.get("客户名称", ""),
        "row_id": lead.get("__row_id__"),
        "最新跟进状态": str(lead.get("最新跟进状态", "")),
        "跟进记录数": len(lead.get("跟进记录") or []),
    }
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="CRM 自动录入引擎（自动写入 + 台账核对）")
    sub = ap.add_subparsers(dest="command", required=True)

    p = sub.add_parser("lead", help="新客户来单：查重→建线索（重复则复用）")
    p.add_argument("--data", required=True,
                   help='线索 JSON，如 {"客户名称":"X","联系人":"Y","联系方式":"Z"}')
    p.set_defaults(func=cmd_lead)

    p = sub.add_parser("follow", help="追加跟进记录并自动关联线索")
    p.add_argument("--customer", required=True, help="客户名称（定位线索）")
    p.add_argument("--data", required=True,
                   help='跟进 JSON，如 {"跟进状态":"商务谈判","本次跟进内容":"..."}')
    p.set_defaults(func=cmd_follow)

    p = sub.add_parser("ledger", help="查看本地写入台账")
    p.set_defaults(func=cmd_ledger)

    p = sub.add_parser("status", help="查看线索当前状态")
    p.add_argument("--customer", required=True)
    p.set_defaults(func=cmd_status)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
