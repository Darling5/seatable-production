# -*- coding: utf-8 -*-
"""采购流水线可移植性回归（直接 python pipeline/test_portability.py）。"""
import csv
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from inventory import match_part
from inventory_sources import FileInventorySource
from po_pdf import validate_formal_order


FAIL = []


def check(condition, message):
    if not condition:
        FAIL.append(message)
        print("FAIL: " + message)


def main():
    with tempfile.TemporaryDirectory(prefix="pipeline_portability_") as tmp:
        stock = os.path.join(tmp, "stock.csv")
        with open(stock, "w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=["物料编码", "物料名称", "库存", "仓位", "单价", "供应商"])
            writer.writeheader()
            writer.writerow({"物料编码": "C-001", "物料名称": "10uF 电容", "库存": "25",
                             "仓位": "A-01", "单价": "0.12", "供应商": "演示供应商"})
            writer.writerow({"物料编码": "C-001", "物料名称": "10uF 电容", "库存": "5",
                             "仓位": "A-02", "单价": "0.10", "供应商": "演示供应商"})

        cfg = {"inventory": {"source": "file", "file": {"path": stock}}}
        parts = FileInventorySource(cfg).load_parts()
        check(len(parts) == 1, "文件库存源应按料号合并重复行")
        part = parts[0]
        check(part["unconfirmed_stock"] == 30, "通用库存列必须默认归为未确认库存")
        check(part["confirmed_stock"] == 0, "未标记确认的文件库存不得直接承诺")
        check(part["unit_price"] == 0.10, "同料号应采用最低有效单价")
        check(part["locations"] == ["A-01", "A-02"], "应保留并去重所有仓位")
        found, method = match_part({"ipn": "C-001", "model": "10uF 电容"}, parts)
        check(found is part and method == "IPN精确", "中文型号与 IPN 应能稳定匹配")

        confirmed_cfg = {"inventory": {"source": "file", "file": {
            "path": stock, "stock_is_confirmed": True}}}
        confirmed = FileInventorySource(confirmed_cfg).load_parts()[0]
        check(confirmed["confirmed_stock"] == 30, "显式确认的通用库存必须可用于抵扣缺口")

    try:
        validate_formal_order({"name": "采购方", "buyer": "采购", "phone": "1",
                               "address": "地址", "payment_terms": "付款"},
                              [{"supplier": "供应商", "price_missing": ["物料"]}])
        check(False, "缺单价订单不得生成正式 PDF")
    except SystemExit:
        pass

    validate_formal_order({"name": "采购方", "buyer": "采购", "phone": "1",
                           "address": "地址", "payment_terms": "付款"},
                          [{"supplier": "供应商", "price_missing": []}])

    if FAIL:
        print("FAILED %d checks" % len(FAIL))
        return 1
    print("ALL PORTABILITY CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
