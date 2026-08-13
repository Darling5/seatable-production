# -*- coding: utf-8 -*-
"""逐零件核库存并生成「人工库存审核表」。

采购数量不是直接等于缺口：先输出 audit.json/库存审核表.csv，人工可修改
`审核决定`、`采购数量`、`供应商`、`单价`，后续 plan 步骤只读取审核后的值。
"""
import re

from core import PartDB, load, load_cfg, load_rules, part_price, save, save_csv, split_stock


def n(s):
    return re.sub(r"[^A-Z0-9]+", "", str(s or "").upper())


def is_passive(row, rules):
    blob = " ".join(str(row.get(k) or "") for k in ("model", "description", "footprint"))
    return any(k.lower() in blob.lower() for k in rules.get("passive_keywords", []))


def match_part(mat, parts):
    ipn = n(mat.get("ipn"))
    model = n(mat.get("model"))
    if ipn:
        xs = [p for p in parts if n(p.get("ipn")) == ipn]
        if len(xs) == 1:
            return xs[0], "IPN精确"
        if len(xs) > 1:
            return None, f"IPN多候选({len(xs)})"
    if model:
        xs = [p for p in parts if n(p.get("name")) == model]
        if len(xs) == 1:
            return xs[0], "型号精确"
        xs = [p for p in parts if model in n(p.get("name")) or n(p.get("name")) in model]
        if len(xs) == 1:
            return xs[0], "型号模糊唯一"
        if len(xs) > 1:
            return None, f"型号多候选({len(xs)})"
    return None, "未匹配"


def run(run_id):
    cfg, rules = load_cfg(), load_rules()
    mats = load(run_id, "prepared.json")["materials"]
    db = PartDB(cfg)
    parts = db.all_parts()
    print(f"[PartDB] 已拉取 {len(parts)} 个零件索引，开始逐个查详情/批次")
    out = []
    for i, mat in enumerate(mats, 1):
        p, method = match_part(mat, parts)
        row = dict(mat)
        row.update({"part_id": None, "match": method, "location": "",
                    "confirmed_stock": 0, "unconfirmed_stock": 0,
                    "gap": mat["need"], "unit_price": None, "price_supplier": "",
                    "supplier": "", "purchase_qty": 0, "amount": 0,
                    "审核决定": "待人工审核", "审核备注": ""})
        if p:
            detail, lots = db.part_lots(p["id"])
            ok, unk, locs = split_stock(lots)
            # 部分 PartDB 版本详情不内嵌批次：保留 total_instock 为未确认，绝不当承诺库存
            if not lots and detail.get("total_instock"):
                unk = float(detail.get("total_instock") or 0)
                row["审核备注"] = "详情无批次明细；total_instock仅列为未确认"
            price, supplier, _ = part_price(detail)
            gap = max(0, float(mat["need"]) - ok)
            passive = is_passive(mat, rules)
            black = supplier and any(x in supplier for x in rules.get("supplier_blacklist", []))
            decision = "排除-无源器件" if passive else ("排除-独立供应商流程" if black else
                       ("无需采购" if gap <= 0 else "建议采购"))
            row.update({"part_id": p["id"], "match": method,
                        "ipn": detail.get("ipn") or mat.get("ipn"),
                        "model": detail.get("name") or mat.get("model"),
                        "footprint": ((detail.get("footprint") or {}).get("name")
                                      if isinstance(detail.get("footprint"), dict) else mat.get("footprint")),
                        "location": ",".join(sorted(set(locs))),
                        "confirmed_stock": ok, "unconfirmed_stock": unk,
                        "gap": gap, "unit_price": price,
                        "price_supplier": supplier or "", "supplier": supplier or "",
                        "purchase_qty": gap if decision == "建议采购" else 0,
                        "amount": gap * price if decision == "建议采购" and price else 0,
                        "审核决定": decision})
        out.append(row)
        if i % 10 == 0 or i == len(mats):
            print(f"  已核对 {i}/{len(mats)}")
    payload = {"instructions": [
        "人工审核 confirmed_stock 与 unconfirmed_stock；未确认库存不得用于生产承诺。",
        "将需要下单的行审核决定改为 已批准，并核对 supplier/purchase_qty/unit_price。",
        "不采购的行改为 已排除；后续 plan 仅接收审核决定=已批准。",
    ], "materials": out}
    jp = save(run_id, "audit.json", payload)
    cols = ["审核决定", "审核备注", "ipn", "model", "footprint", "need", "location",
            "confirmed_stock", "unconfirmed_stock", "gap", "supplier", "purchase_qty",
            "unit_price", "amount", "part_id", "match", "price_supplier"]
    cp = save_csv(run_id, "库存审核表.csv", out, cols)
    print(f"[审核关卡] 请人工审核：\n  {cp}\n也可直接编辑 JSON：\n  {jp}")
    return out
