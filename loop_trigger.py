# -*- coding: utf-8 -*-
"""loop_trigger.py — 微信来单 → 业务闭环案件的自动触发器。

链路：微信消息 →（crm_dispatch lead，已有）→ CRM 销售线索表
      → 本脚本扫描无案件的线索 → 生成/创建控制平面案件（CUS/LED/OPP 三对象）

原则：
  · 默认 preview：只列出将创建的案件计划，--yes 才落控制平面。
  · 幂等：客户名 + 产品 → start_case 幂等键，重复扫描零重复创建。
  · 单向触发：本脚本只建控制平面案件，不写 CRM 线索表（那条路由 crm_dispatch 管）。

用法：
  python loop_trigger.py            # preview：列出待触发的来单案件
  python loop_trigger.py --yes      # 实际创建控制平面案件
"""
from __future__ import annotations

import argparse
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from domain.order_to_cash import Service

DEFAULT_DATA_DIR = os.path.join(HERE, "data", "business_loop")


def _existing_customers(svc: Service) -> dict[str, str]:
    """控制平面里已建档的客户名 -> root_id（避免重复开案）。"""
    out = {}
    for row in svc.store.read("objects"):
        if row.get("object_type") == "customer" and row.get("summary"):
            out.setdefault(row["summary"].strip(), row.get("root_id", ""))
    return out


def _scan_leads() -> tuple[list[dict], object]:
    """拉取 CRM 销售线索表并按客户名去重（保留首条）。返回 (线索列表, adapter)。"""
    from adapters.factory import get_adapter
    adapter = get_adapter(base_name="crm")
    if not hasattr(adapter, "auth"):
        print("[!] 未配置 CRM Base，无法扫描来单线索。")
        return [], adapter
    adapter.auth()
    try:
        leads = adapter.list_rows("销售线索表")
    except KeyError:
        print("[!] CRM 库无「销售线索表」。")
        return [], adapter
    seen, unique = set(), []
    for lead in leads:
        name = str(lead.get("客户名称", "") or "").strip()
        if name and name not in seen:
            seen.add(name)
            unique.append(lead)
    return unique, adapter


def _latest_follow_text(adapter, lead_row_id: str) -> str:
    """从跟进记录表提取该线索最近一次跟进内容（截断 60 字，作产品意向线索）。"""
    try:
        rows = adapter.list_rows("客户线索跟进记录")
    except KeyError:
        return ""
    best = ""
    for row in rows:
        links = row.get("客户线索") or []
        if not any(isinstance(l, dict) and str(l.get("row_id")) == lead_row_id
                   for l in links):
            continue
        text = str(row.get("本次跟进内容", "") or "").strip()
        if text:
            best = text  # 表按编号排序，后出现的视为更新
    return best.replace("\n", " ")[:60]


def _lead_to_case(lead: dict, follow_text: str = "") -> dict | None:
    """线索行 → 案件参数。客户名必填；产品意向优先取跟进内容，其次备注。"""
    customer = str(lead.get("客户名称", "") or "").strip()
    if not customer:
        return None
    product = (follow_text or str(lead.get("备注", "") or "")).strip()[:60]
    if not product:
        product = "待确认"
    return {
        "customer": customer,
        "product": product,
        "contact": str(lead.get("联系人", "") or ""),
        "phone": str(lead.get("联系方式", "") or ""),
        "source_event_id": "crm-lead:%s" % (lead.get("__row_id__", "") or customer),
    }


DEAD_HINTS = ("终止", "无效", "放弃", "已流失")

def trigger(apply: bool = False, data_dir: str = DEFAULT_DATA_DIR,
            owner: str = "项目经理") -> dict:
    svc = Service(data_dir)
    existing = _existing_customers(svc)
    leads, adapter = _scan_leads()
    follow_map = {}
    if leads and hasattr(adapter, "list_rows"):
        # 一次性拉跟进记录，按线索 row_id 建索引（避免逐条重复拉全表）
        try:
            follow_rows = adapter.list_rows("客户线索跟进记录")
            for row in follow_rows:
                for link in (row.get("客户线索") or []):
                    if isinstance(link, dict) and link.get("row_id"):
                        text = str(row.get("本次跟进内容", "") or "").strip()
                        if text:
                            follow_map[str(link["row_id"])] = text
        except (KeyError, Exception):
            pass
    results = {"scanned": 0, "planned": [], "created": [], "reused": [],
               "skipped": 0, "dead": []}
    for lead in leads:
        results["scanned"] += 1
        follow_text = follow_map.get(str(lead.get("__row_id__", "")), "")
        params = _lead_to_case(lead, follow_text.replace("\n", " ")[:60])
        if params is None:
            results["skipped"] += 1
            continue
        if any(h in params["product"] for h in DEAD_HINTS):
            results["dead"].append({"customer": params["customer"],
                                    "hint": params["product"][:30]})
            continue
        if params["customer"] in existing:
            results["reused"].append({"customer": params["customer"],
                                      "root_id": existing[params["customer"]]})
            continue
        outcome = svc.start_case(params["customer"], params["product"], owner=owner,
                                 source_event_id=params["source_event_id"],
                                 contact=params["contact"], phone=params["phone"],
                                 approved=apply)
        if outcome.get("status") == "reused":
            results["reused"].append({"customer": params["customer"],
                                      "root_id": outcome.get("root_id", "")})
        elif apply and outcome.get("status") == "created":
            results["created"].append({"customer": params["customer"],
                                       "root_id": outcome.get("root_id", "")})
        else:
            results["planned"].append({"customer": params["customer"],
                                       "product": params["product"],
                                       "idempotency_key": outcome.get("idempotency_key", "")})
    return results


def main():
    ap = argparse.ArgumentParser(description="微信来单线索 → 业务闭环案件触发器")
    ap.add_argument("--yes", action="store_true", help="实际创建（默认 preview）")
    ap.add_argument("--data-dir", default=DEFAULT_DATA_DIR)
    ap.add_argument("--owner", default="项目经理")
    args = ap.parse_args()

    r = trigger(apply=args.yes, data_dir=args.data_dir, owner=args.owner)
    print("→ 来单触发扫描（%s）" % ("APPLY" if args.yes else "PREVIEW"))
    print("  扫描线索 %d 条 | 已有案件 %d | 跳过 %d | 死单过滤 %d"
          % (r["scanned"], len(r["reused"]), r["skipped"], len(r["dead"])))
    for item in r["dead"]:
        print("  ✗ 死单过滤 %s（%s）" % (item["customer"], item["hint"]))
    for item in r["reused"]:
        print("  ✓ 已有案件 %s（%s）" % (item["root_id"], item["customer"]))
    for item in r["planned"]:
        print("  ＋ 待建案件 %s：%s（加 --yes 创建）" % (item["customer"], item["product"]))
    for item in r["created"]:
        print("  ＋ 已建案件 %s（%s）" % (item["root_id"], item["customer"]))
    if not (r["planned"] or r["created"]):
        print("  （无新来单，控制平面与 CRM 线索已对齐）")


if __name__ == "__main__":
    main()
