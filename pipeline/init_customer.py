# -*- coding: utf-8 -*-
"""创建采购流水线的本地客户资料骨架。"""
import os

from core import PIPE_DIR


LOCAL_RULES = """# 本文件包含公司信息、产品名称与 BOM 路径，已被 .gitignore 忽略。
company:
  name: "请填写采购方公司全称"
  buyer: "请填写采购联系人或部门"
  phone: "请填写联系电话"
  address: "请填写采购地址"
  payment_terms: "请填写付款条款"

# 每个合同产品都要绑定一份标准 BOM。BOM 文件放 pipeline/customer/boms/。
products:
  - name: "示例产品"
    bom: "customer/boms/示例产品.xlsx"
    aliases: ["示例产品"]

# 合同文本中识别产品数量的正则。name 和 qty 两个命名组必填。
contract_quantity_patterns:
  - '(?P<name>示例产品)[^0-9]{0,20}(?P<qty>\\d{1,8})'

# 供应商排除名单是客户私有采购规则；没有就保持空列表。
supplier_blacklist: []
"""


def run():
    customer = os.path.join(PIPE_DIR, "customer")
    for name in ("boms", "inventory", "processes"):
        os.makedirs(os.path.join(customer, name), exist_ok=True)
    rules_path = os.path.join(PIPE_DIR, "rules.local.yaml")
    if os.path.exists(rules_path):
        print(f"[保留] 已存在本地客户规则：{rules_path}")
    else:
        with open(rules_path, "w", encoding="utf-8") as f:
            f.write(LOCAL_RULES)
        print(f"[已创建] 本地客户规则：{rules_path}")
    print(f"[已创建] 客户资料目录：{customer}")
    print("下一步：把 BOM、库存导出和流程文件放入 customer/，填写 rules.local.yaml 和 config.yaml。")
