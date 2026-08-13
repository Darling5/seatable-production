# -*- coding: utf-8 -*-
"""采购流水线统一入口：合同 → 备料 → 库存审核 → 采购分组 → SeaTable → PDF。

设计原则：人工审核关卡不可绕过；写 SeaTable 必须 --yes。

  python run.py prepare  <run_id> --contract 合同.pdf --bom 示例产品=bom.xlsx
  python run.py init                                  # 创建本地客户配置模板
  python run.py audit    <run_id>                     # 使用已选库存源生成审核表（人工改）
  python run.py plan     <run_id>                     # 读审核结果 → 采购预览
  python run.py submit   <run_id> --plan 4G小卡 --yes  # 写 SeaTable + 双向关联
  python run.py pdf      <run_id>                     # 生成采购订单 PDF
  python run.py all      <run_id> --contract ... --bom ...   # 跑到审核关卡为止
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import inventory  # noqa: E402
import plan as plan_mod  # noqa: E402
import po_pdf  # noqa: E402
import preflight  # noqa: E402
import prepare  # noqa: E402
import init_customer  # noqa: E402


def main():
    ap = argparse.ArgumentParser(prog="run.py", description="采购流水线")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("preflight", help="体检是否已具备只丢合同的条件")
    sub.add_parser("init", help="创建被忽略的客户配置模板与资料目录")

    p = sub.add_parser("prepare", help="解析合同+BOM，合并备料")
    p.add_argument("run_id")
    p.add_argument("--contract", required=True)
    p.add_argument("--bom", action="append", default=[],
                   help="产品名=BOM文件，可多次")

    p = sub.add_parser("audit", help="按已配置库存源核库存，出人工审核表")
    p.add_argument("run_id")

    p = sub.add_parser("plan", help="按供应商分组，出采购预览")
    p.add_argument("run_id")
    p.add_argument("--plan", default=None)

    p = sub.add_parser("submit", help="写入 SeaTable 并关联生产计划")
    p.add_argument("run_id")
    p.add_argument("--plan", default=None)
    p.add_argument("--yes", action="store_true")

    p = sub.add_parser("pdf", help="生成采购订单 PDF")
    p.add_argument("run_id")
    p.add_argument("--plan", default=None)

    p = sub.add_parser("all", help="prepare+audit，停在人工审核关卡")
    p.add_argument("run_id")
    p.add_argument("--contract", required=True)
    p.add_argument("--bom", action="append", default=[])

    a = ap.parse_args()
    if a.cmd == "init":
        init_customer.run()
    elif a.cmd == "preflight":
        if not preflight.run():
            raise SystemExit(1)
    elif a.cmd == "prepare":
        prepare.run(a.run_id, a.contract, a.bom)
    elif a.cmd == "audit":
        inventory.run(a.run_id)
    elif a.cmd == "plan":
        plan_mod.build(a.run_id, a.plan)
    elif a.cmd == "submit":
        plan_mod.submit(a.run_id, a.plan, a.yes)
    elif a.cmd == "pdf":
        po_pdf.run(a.run_id, a.plan)
    elif a.cmd == "all":
        prepare.run(a.run_id, a.contract, a.bom)
        inventory.run(a.run_id)
        print("\n>>> 已停在人工审核关卡。审核 库存审核表.csv 后执行："
              f"\n    python run.py plan {a.run_id}")


if __name__ == "__main__":
    main()
