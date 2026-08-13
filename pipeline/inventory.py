# -*- coding: utf-8 -*-
"""逐零件核库存并生成「人工库存审核表」。

采购数量不是直接等于缺口：先输出 audit.json/库存审核表.csv，人工可修改
`审核决定`、`采购数量`、`供应商`、`单价`，后续 plan 步骤只读取审核后的值。
"""
import re

from core import load, load_cfg, load_rules, save, save_csv
from inventory_sources import get_inventory_source


def n(s):
    return re.sub(r"[^A-Z0-9\u4e00-\u9fff]+", "", str(s or "").upper())


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
    source = get_inventory_source(cfg)
    parts = source.load_parts()
    print(f"[{source.name}] 已加载 {len(parts)} 个零件索引，开始逐个核对")
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
            p = source.enrich(p)
            confirmed = float(p.get("confirmed_stock") or 0)
            unconfirmed = float(p.get("unconfirmed_stock") or 0)
            price = p.get("unit_price")
            supplier = p.get("supplier") or ""
            gap = max(0, float(mat["need"]) - confirmed)
            passive = is_passive(mat, rules)
            black = supplier and any(x in supplier for x in rules.get("supplier_blacklist", []))
            decision = "排除-无源器件" if passive else ("排除-独立供应商流程" if black else
                       ("无需采购" if gap <= 0 else "建议采购"))
            row.update({"part_id": p.get("id"), "match": method,
                        "ipn": p.get("ipn") or mat.get("ipn"),
                        "model": p.get("name") or mat.get("model"),
                        "footprint": p.get("footprint") or mat.get("footprint"),
                        "location": ",".join(sorted(set(p.get("locations") or []))),
                        "confirmed_stock": confirmed, "unconfirmed_stock": unconfirmed,
                        "gap": gap, "unit_price": price,
                        "price_supplier": supplier, "supplier": supplier,
                        "purchase_qty": gap if decision == "建议采购" else 0,
                        "amount": gap * price if decision == "建议采购" and price else 0,
                        "审核决定": decision,
                        "审核备注": p.get("note") or ""})
        out.append(row)
        if i % 10 == 0 or i == len(mats):
            print(f"  已核对 {i}/{len(mats)}")
    payload = {"inventory_source": source.name, "instructions": [
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
