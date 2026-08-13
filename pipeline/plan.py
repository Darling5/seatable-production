# -*- coding: utf-8 -*-
"""审核后 → 按供应商分组 → IC采购记录预览 / 提交 SeaTable。

铁律：只接收 审核决定=已批准 的行；未审核的一律不进采购。
提交必须显式 --yes，否则只出预览不落地。
"""
import csv
import os

from core import (SeaTable, load, load_cfg, load_rules, money, run_dir, save, save_csv,
                  today)

APPROVED = ("已批准", "已确认", "approved", "OK", "ok")


def approved_rows(mats):
    out, pending = [], []
    for m in mats:
        d = str(m.get("审核决定") or "").strip()
        if d in APPROVED:
            out.append(m)
        elif d.startswith("建议采购") or d.startswith("待人工"):
            pending.append(m)
    return out, pending


def num(v):
    try:
        return float(str(v).replace(",", "") or 0)
    except (TypeError, ValueError):
        return 0.0


def md_table(rows):
    """物料清单写成 Markdown 表格（SeaTable 长文本列渲染）。"""
    head = "| IPN | 型号 | 封装 | 数量 | 单价 | 金额 |\n| --- | --- | --- | --- | --- | --- |"
    body = []
    for r in rows:
        q, p = num(r.get("purchase_qty")), num(r.get("unit_price"))
        body.append("| {} | {} | {} | {:g} | {} | {} |".format(
            r.get("ipn") or "", r.get("model") or "", r.get("footprint") or "",
            q, money(p) if p else "待询价", money(q * p) if p else "待询价"))
    return head + "\n" + "\n".join(body)


def group_by_supplier(rows, rules):
    black = rules.get("supplier_blacklist", [])
    groups, skipped = {}, []
    for r in rows:
        sup = str(r.get("supplier") or "").strip()
        if not sup:
            skipped.append({**r, "原因": "缺供应商，无法下单"})
            continue
        if any(b in sup for b in black):
            skipped.append({**r, "原因": f"供应商 {sup} 走独立流程"})
            continue
        if num(r.get("purchase_qty")) <= 0:
            skipped.append({**r, "原因": "采购数量为0"})
            continue
        groups.setdefault(sup, []).append(r)
    return groups, skipped


def load_audit_rows(run_id):
    """人工审核以 CSV 为准；CSV 不存在时才回退 JSON。"""
    csv_path = os.path.join(run_dir(run_id, create=False), "库存审核表.csv")
    if os.path.exists(csv_path):
        with open(csv_path, encoding="utf-8-sig", newline="") as f:
            return list(csv.DictReader(f))
    audit = load(run_id, "audit.json")
    return audit["materials"] if isinstance(audit, dict) else audit


def build(run_id, plan_name=None):
    rules = load_rules()
    mats = load_audit_rows(run_id)
    ok, pending = approved_rows(mats)
    if pending:
        print(f"[提醒] 有 {len(pending)} 行仍未审核（审核决定仍为待人工/建议采购），本次不会采购。")
    if not ok:
        print("[停止] 没有任何 审核决定=已批准 的物料。请在 库存审核表.csv 标记后重跑。")
        return None
    groups, skipped = group_by_supplier(ok, rules)
    orders = []
    for sup, rows in sorted(groups.items()):
        total = sum(num(r.get("purchase_qty")) * num(r.get("unit_price")) for r in rows)
        missing = [r.get("model") for r in rows if not num(r.get("unit_price"))]
        orders.append({
            "supplier": sup, "items": rows, "amount": round(total, 2),
            "kinds": len(rows),
            "qty": sum(num(r.get("purchase_qty")) for r in rows),
            "price_missing": missing,
            "material_md": md_table(rows),
            "status": "未下单",
        })
    payload = {"run_id": run_id, "plan": plan_name, "date": today(),
               "orders": orders, "skipped": skipped}
    save(run_id, "orders.json", payload)
    save_csv(run_id, "采购分组明细.csv",
             [{"供应商": o["supplier"], "品类数": o["kinds"], "总数量": o["qty"],
               "金额": o["amount"], "缺价物料": ",".join(x or "" for x in o["price_missing"])}
              for o in orders],
             ["供应商", "品类数", "总数量", "金额", "缺价物料"])
    print(f"\n===== IC采购记录预览（{len(orders)} 个供应商） =====")
    for o in orders:
        flag = f"  ⚠ 缺单价 {len(o['price_missing'])} 项" if o["price_missing"] else ""
        print(f"\n供应商：{o['supplier']}   品类 {o['kinds']}   数量 {o['qty']:g}   "
              f"金额 {money(o['amount'])}{flag}")
        print(o["material_md"])
    if skipped:
        print(f"\n[未纳入 {len(skipped)} 项]")
        for s in skipped[:20]:
            print(f"  - {s.get('model') or s.get('ipn')}：{s['原因']}")
    print("\n以上为预览，尚未写入 SeaTable。确认无误后加 --yes 提交。")
    return payload


def submit(run_id, plan_name, yes=False):
    cfg, rules = load_cfg(), load_rules()
    data = load(run_id, "orders.json")
    orders = data["orders"]
    if not yes:
        print("[未提交] 缺 --yes，仅预览。")
        return
    st = SeaTable(cfg)
    ic_table = rules["seatable"]["ic_table"]
    plan_table = rules["seatable"]["plan_table"]
    cols = {c["name"] for c in st.table(ic_table)["columns"]}

    def pick(logical, default=None):
        for cand in rules["seatable"]["field_candidates"].get(logical, []):
            if cand in cols:
                return cand
        return default

    f_mat, f_sup = pick("material"), pick("supplier")
    f_amt, f_status = pick("amount"), pick("status")
    if not f_mat or not f_sup:
        print(f"[错误] 表 {ic_table} 未找到物料/供应商列，实际列：{sorted(cols)}")
        return
    plan_rows = st.list_rows(plan_table)
    plan_id = None
    if plan_name:
        hit = [r for r in plan_rows
               if plan_name in str(r.get("生产产品") or "")
               or plan_name in str(r.get("生产计划编号") or "")]
        if len(hit) != 1:
            print(f"[错误] 生产计划 “{plan_name}” 匹配到 {len(hit)} 条，需唯一。可选：")
            for r in plan_rows[-10:]:
                print(f"  - {r.get('生产计划编号')} / {r.get('生产产品')}")
            return
        plan_id = hit[0]["__row_id__"]
    written = []
    for o in orders:
        row = {f_mat: o["material_md"], f_sup: o["supplier"]}
        if f_amt:
            row[f_amt] = o["amount"]
        if f_status:
            row[f_status] = o["status"]
        rid = st.append(ic_table, row)
        if rid and plan_id:
            st.link(ic_table, plan_table, rid, [plan_id])
        written.append({"supplier": o["supplier"], "row_id": rid,
                        "linked": bool(plan_id)})
        print(f"[已写入] {o['supplier']} → row {rid}"
              + ("（已双向关联生产计划）" if plan_id else "（未关联，缺 --plan）"))
    save(run_id, "submitted.json", {"table": ic_table, "plan": plan_name,
                                     "rows": written})
    return written
