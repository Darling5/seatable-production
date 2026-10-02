# -*- coding: utf-8 -*-
"""loop_trigger.py — 微信来单 → 业务闭环案件的自动触发器。

链路：微信消息 →（crm_dispatch lead，已有）→ CRM 销售线索表
      → 本脚本扫描无案件的线索 → 生成/创建控制平面案件（CUS/LED/OPP 三对象）

原则：
  · 默认 preview：只列出将创建的案件计划，--yes 才落控制平面。
  · 幂等：客户名 + 产品 → start_case 幂等键，重复扫描零重复创建。
  · **「已有案件」必须看案件是否完整**（2026-10-02 修 G9）：只凭 customer 对象存在就判
    「复用」，会让建案中途崩掉留下的空壳永久卡死（repaired 分支变死代码）。
    现在缺任一 `CASE_SEED_KINDS` 就继续走 start_case → 补建（preview 如实报「待补」。）
  · 单向触发：本脚本只建控制平面案件，不写 CRM 线索表（那条路由 crm_dispatch 管）。
  · **退出码**（2026-10-02 修，接入 DAG 前的前置加固）：
      0 = 扫完（含「确实没有新线索」）
      3 = **CRM 侧压根没扫成**（未配置 Base / 无「销售线索表」）→ skipped，不是成功
      其他非 0 = 失败（例如 auth 报错）→ failed，会被重试
    修之前这两种情况都 `return 0`，接进 DAG 会被记成 success，
    驾驶舱显示「来单扫描成功」而实际一条线索都没扫 —— 典型假指标。

用法：
  python workflows/loop_trigger.py  # preview：列出待触发的来单案件
  python workflows/loop_trigger.py --yes  # 实际创建控制平面案件
"""
from __future__ import annotations

import argparse
import os
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

from adapters.base import CAP_READ, backend_of, supports
from domain.order_to_cash import Service

DEFAULT_DATA_DIR = os.path.join(HERE, "data", "business_loop")


def _existing_cases(svc: Service) -> dict[str, dict]:
    """控制平面里已建档的客户名 -> ``{"root_id": …, "kinds": {对象类型}}``。

    **不能只认 customer 对象就判「已有案件」**（2026-10-02 修 G9）。
    建案是分多次 append 的（customer → lead → opportunity → evidence …），
    中途崩掉会留下「有 customer 行、缺 lead/opportunity」的**残缺空壳**。
    旧实现（`_existing_customers`）据此直接判「已有案件，复用」并 continue，
    于是 `trigger()` 里那个 `repaired` 分支**永远不可达（死代码）**，
    `Service._repair_case()` 在任何真实调用路径上都跑不到 —— 该客户名永久卡死。
    （这个坑在 `domain/order_to_cash.py` 的注释里被描述过，但当时没修调用方。）

    现在把「已有哪些种子对象类型」一起返回，由调用方决定「复用」还是「补建」。
    """
    from domain.order_to_cash import CASE_SEED_KINDS
    kinds_by_root: dict[str, set] = {}
    root_by_name: dict[str, str] = {}
    for row in svc.store.read("objects"):
        rid = str(row.get("root_id") or "")
        kind = str(row.get("object_type") or "")
        if rid and kind:
            kinds_by_root.setdefault(rid, set()).add(kind)
        if kind == "customer":
            name = str(row.get("summary") or "").strip()
            if name:
                root_by_name[name] = rid
    out = {}
    for name, rid in root_by_name.items():
        kinds = kinds_by_root.get(rid, set())
        out[name] = {"root_id": rid, "kinds": kinds,
                     "missing": tuple(k for k in CASE_SEED_KINDS if k not in kinds)}
    return out


def _scan_leads() -> tuple[list[dict], object, str]:
    """拉取 CRM 销售线索表并按客户名去重（保留首条）。

    返回 ``(线索列表, adapter, 不可用原因)``。**原因非空 = 这次压根没扫成**，
    与「扫了但确实没有新线索」是两件完全不同的事，调用方必须区分（见 main 的退出码）。
    """
    from adapters.factory import get_adapter
    adapter = get_adapter(base_name="crm")
    if backend_of(adapter) == "local":   # 工厂退回 local 模式了
        return [], adapter, "未配置 CRM Base，无法扫描来单线索"
    adapter.auth()
    try:
        leads = adapter.list_rows("销售线索表")
    except KeyError:
        return [], adapter, "CRM 库无「销售线索表」"
    seen, unique = set(), []
    for lead in leads:
        name = str(lead.get("客户名称", "") or "").strip()
        if name and name not in seen:
            seen.add(name)
            unique.append(lead)
    return unique, adapter, ""


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
    existing = _existing_cases(svc)
    leads, adapter, unavailable = _scan_leads()
    follow_map = {}
    if leads and supports(adapter, CAP_READ):
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
               "repaired": [], "skipped": 0, "dead": [], "unavailable": unavailable}
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
        ex = existing.get(params["customer"])
        if ex and not ex["missing"]:
            # 案件完整 → 复用（幂等，不重复开案）
            results["reused"].append({"customer": params["customer"],
                                      "root_id": ex["root_id"]})
            continue
        # 走到这里有两种情况：① 从没建过案；② **残缺空壳**（缺 lead/opportunity）。
        # ② 必须继续往下调 start_case —— 它内部按幂等键命中后会走 _repair_case 补建，
        # 这正是 loop_trigger 里 repaired 分支存在的意义（修 G9：此前被上面的
        # 「已有案件」短路，该分支是死代码，残缺案件永久留疤）。
        outcome = svc.start_case(params["customer"], params["product"], owner=owner,
                                 source_event_id=params["source_event_id"],
                                 contact=params["contact"], phone=params["phone"],
                                 approved=apply)
        st = outcome.get("status")
        if st == "reused":
            results["reused"].append({"customer": params["customer"],
                                      "root_id": outcome.get("root_id", "")})
        elif apply and st == "repaired":
            # 半写自愈：该客户上次建案中止，本次补齐了缺的对象
            # （见 domain/order_to_cash.py 的 Service._repair_case）
            results["repaired"].append({"customer": params["customer"],
                                        "root_id": outcome.get("root_id", ""),
                                        "repaired": outcome.get("repaired", [])})
        elif apply and st == "created":
            results["created"].append({"customer": params["customer"],
                                       "root_id": outcome.get("root_id", "")})
        else:
            results["planned"].append({"customer": params["customer"],
                                       "product": params["product"],
                                       "idempotency_key": outcome.get("idempotency_key", ""),
                                       "repair_missing": tuple(outcome.get("repair_missing") or ())})
    return results


def main():
    ap = argparse.ArgumentParser(description="微信来单线索 → 业务闭环案件触发器")
    ap.add_argument("--yes", action="store_true", help="实际创建（默认 preview）")
    ap.add_argument("--data-dir", default=DEFAULT_DATA_DIR)
    ap.add_argument("--owner", default="项目经理")
    args = ap.parse_args()

    r = trigger(apply=args.yes, data_dir=args.data_dir, owner=args.owner)
    if r.get("unavailable"):
        # 区分「CRM 扫不成」与「扫了但没有新线索」。
        # 退出码 3 = skipped（runner.py:273-277 的约定），绝不会被记成 success。
        print("[skip] %s —— 本次**未扫描任何来单线索**（注意：这不是"
              "「没有新来单」，是没扫成）。" % r["unavailable"])
        sys.exit(3)
    print("→ 来单触发扫描（%s）" % ("APPLY" if args.yes else "PREVIEW"))
    print("  扫描线索 %d 条 | 已有案件 %d | 跳过 %d | 死单过滤 %d"
          % (r["scanned"], len(r["reused"]), r["skipped"], len(r["dead"])))
    for item in r["dead"]:
        print("  ✗ 死单过滤 %s（%s）" % (item["customer"], item["hint"]))
    for item in r["reused"]:
        print("  ✓ 已有案件 %s（%s）" % (item["root_id"], item["customer"]))
    for item in r["repaired"]:
        print("  ✚ 自愈补齐案件 %s（%s）：补出 %s"
              % (item["root_id"], item["customer"],
                 "、".join(item.get("repaired") or []) or "—"))
    for item in r["planned"]:
        if item.get("repair_missing"):
            print("  ✚ 待补残缺案件 %s：缺 %s（加 --yes 自愈补齐）"
                  % (item["customer"], "、".join(item["repair_missing"])))
        else:
            print("  ＋ 待建案件 %s：%s（加 --yes 创建）" % (item["customer"], item["product"]))
    for item in r["created"]:
        print("  ＋ 已建案件 %s（%s）" % (item["root_id"], item["customer"]))
    if not (r["planned"] or r["created"]):
        print("  （无新来单，控制平面与 CRM 线索已对齐）")


if __name__ == "__main__":
    main()
