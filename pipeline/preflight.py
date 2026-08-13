# -*- coding: utf-8 -*-
"""采购流水线开工体检：回答“现在能否只丢合同就出正式采购单”。"""
import os

from core import PartDB, SeaTable, load_cfg, load_rules


def is_placeholder(value):
    s = str(value or "").strip()
    return not s or "____" in s or s in ("采购部", "待填写", "-")


def run():
    cfg, rules = load_cfg(), load_rules()
    blockers, warnings, passed = [], [], []

    company = rules.get("company") or {}
    for key, label in (("name", "采购方公司全称"), ("buyer", "采购联系人/部门"),
                       ("phone", "采购方联系电话"), ("address", "采购方地址")):
        if is_placeholder(company.get(key)):
            blockers.append(f"rules.yaml company.{key}：缺{label}")
    if not is_placeholder(company.get("payment_terms")):
        passed.append("采购订单付款条款已配置")
    else:
        blockers.append("rules.yaml company.payment_terms：缺付款条款")

    products = rules.get("products") or []
    if not products:
        blockers.append("rules.yaml products：未配置合同产品与标准 BOM")
    for p in products:
        name, path = p.get("name"), p.get("bom")
        if not path:
            blockers.append(f"产品“{name}”未配置标准 BOM 路径")
            continue
        if not os.path.isabs(path):
            path = os.path.join(os.path.dirname(os.path.abspath(__file__)), path)
        if not os.path.exists(path):
            blockers.append(f"产品“{name}”的 BOM 文件不存在：{path}")
        else:
            passed.append(f"产品“{name}”标准 BOM 已绑定")

    try:
        db = PartDB(cfg)
        parts = db.all_parts()
        passed.append(f"PartDB 连通（{len(parts)} 个零件）")
    except Exception as e:
        blockers.append(f"PartDB 不可用：{e}")

    try:
        st = SeaTable(cfg)
        ic = (rules.get("seatable") or {}).get("ic_table", "IC采购记录")
        plan = (rules.get("seatable") or {}).get("plan_table", "生产计划")
        cols = {c["name"] for c in st.table(ic)["columns"]}
        for c in ("供应商", "状态", "采购花销"):
            if c not in cols:
                warnings.append(f"SeaTable {ic} 缺列“{c}”")
        lid = st.link_id(ic, plan)
        passed.append(f"SeaTable 连通，{ic}↔{plan} 关联 {lid}")
        if not st.select_options(ic, "供应商"):
            warnings.append("SeaTable 供应商列没有选项，提交时无法校验供应商")
    except Exception as e:
        blockers.append(f"SeaTable 不可用：{e}")

    print("===== 采购流水线开工体检 =====")
    for x in passed:
        print(f"[通过] {x}")
    for x in warnings:
        print(f"[提醒] {x}")
    for x in blockers:
        print(f"[阻断] {x}")
    if blockers:
        print(f"\n结论：尚不能只丢合同就出正式采购单；需补 {len(blockers)} 项阻断配置。")
        return False
    print("\n结论：可以。丢合同后可自动跑到库存审核，人工批准后生成正式采购单。")
    return True
