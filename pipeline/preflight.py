# -*- coding: utf-8 -*-
"""采购流水线开工体检：回答“现在能否只丢合同就出正式采购单”。"""
import os

from core import SeaTable, load_cfg, load_rules, pipeline_path
from inventory_sources import get_inventory_source


def is_placeholder(value):
    s = str(value or "").strip()
    markers = ("____", "待填写", "请填写", "示例")
    return not s or any(marker in s for marker in markers) or s in ("采购部", "-")


def run():
    cfg, rules = load_cfg(), load_rules()
    blockers, warnings, passed = [], [], []

    company = rules.get("company") or {}
    for key, label in (("name", "采购方公司全称"), ("buyer", "采购联系人/部门"),
                       ("phone", "采购方联系电话"), ("address", "采购方地址")):
        if is_placeholder(company.get(key)):
            blockers.append(f"rules.local.yaml company.{key}：缺{label}")
    if not is_placeholder(company.get("payment_terms")):
        passed.append("采购订单付款条款已配置")
    else:
        blockers.append("rules.local.yaml company.payment_terms：缺付款条款")

    products = rules.get("products") or []
    if not products:
        blockers.append("rules.local.yaml products：未配置合同产品与标准 BOM")
    if not rules.get("contract_quantity_patterns"):
        blockers.append("rules.local.yaml contract_quantity_patterns：未配置合同数量识别规则")
    for product in products:
        name, path = product.get("name"), product.get("bom")
        if not path:
            blockers.append(f"产品“{name}”未配置标准 BOM 路径")
            continue
        path = pipeline_path(path)
        if not os.path.exists(path):
            blockers.append(f"产品“{name}”的 BOM 文件不存在：{path}")
        else:
            passed.append(f"产品“{name}”标准 BOM 已绑定")

    try:
        source = get_inventory_source(cfg)
        parts = source.load_parts()
        passed.append(f"库存源 {source.name} 可用（{len(parts)} 个零件）")
        if not parts:
            blockers.append(f"库存源 {source.name} 没有零件数据")
    except (Exception, SystemExit) as exc:
        blockers.append(f"库存源不可用：{exc or '初始化失败'}")

    seatable = (cfg or {}).get("seatable") or {}
    seatable_enabled = bool(seatable.get("enabled") or
                            (seatable.get("api_token") and seatable.get("base_uuid")))
    if seatable_enabled:
        try:
            st = SeaTable(cfg)
            ic = (rules.get("seatable") or {}).get("ic_table", "IC采购记录")
            plan = (rules.get("seatable") or {}).get("plan_table", "生产计划")
            cols = {c["name"] for c in st.table(ic)["columns"]}
            for column in ("供应商", "状态", "采购花销"):
                if column not in cols:
                    warnings.append(f"SeaTable {ic} 缺列“{column}”")
            link_id = st.link_id(ic, plan)
            passed.append(f"SeaTable 同步可用，{ic}↔{plan} 关联 {link_id}")
            if not st.select_options(ic, "供应商"):
                warnings.append("SeaTable 供应商列没有选项，提交时无法校验供应商")
        except (Exception, SystemExit) as exc:
            blockers.append(f"已启用 SeaTable，但同步不可用：{exc or '初始化失败'}")
    else:
        warnings.append("未启用 SeaTable；仍可生成正式 PDF，但 submit 同步不可用")

    print("===== 采购流水线开工体检 =====")
    for item in passed:
        print(f"[通过] {item}")
    for item in warnings:
        print(f"[提醒] {item}")
    for item in blockers:
        print(f"[阻断] {item}")
    if blockers:
        print(f"\n结论：尚不能只丢合同就出正式采购单；需补 {len(blockers)} 项阻断配置。")
        return False
    print("\n结论：可以。丢合同后可自动跑到库存审核，人工批准后生成正式采购单。")
    return True
