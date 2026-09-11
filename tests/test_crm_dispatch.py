#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""crm_dispatch / wx_dispatch(crm) 离线单元测试。

不打真实 API：crm_dispatch 的 API 部分用桩适配器模拟；wx_dispatch 的
crm 路由只验证字段清洗与 auto_write 标记。
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import json
import tempfile

import crm_dispatch as cd
import wx_dispatch as wd

FAILED = []


def check(cond, msg):
    print(("  PASS  " if cond else "  FAIL  ") + msg)
    if not cond:
        FAILED.append(msg)


class StubAdapter:
    """模拟 CRM 适配器：内存行 + link 记录。"""

    def __init__(self, existing_rows=None):
        self.lead_rows = list(existing_rows or [])
        self.follow_rows = []
        self.links = []  # (follow_row_id, lead_row_id)
        self.stub_mode = True

    def list_rows(self, table):
        if table == cd.LEAD_TABLE:
            return [dict(r) for r in self.lead_rows]
        return [dict(r) for r in self.follow_rows]

    def append_row(self, table, data):
        row = dict(data)
        row["__row_id__"] = "stub-%s-%d" % ("lead" if table == cd.LEAD_TABLE else "follow",
                                            len(self.lead_rows if table == cd.LEAD_TABLE else self.follow_rows) + 1)
        (self.lead_rows if table == cd.LEAD_TABLE else self.follow_rows).append(row)
        return row["__row_id__"]

    def link(self, table, other, link_id, row_id, other_row_ids):
        for rid in other_row_ids:
            self.links.append((row_id, rid))


def main():
    print("[1] wx_dispatch crm 路由")
    preview = wd.build_preview({
        "event_id": "T1", "source": "测试群",
        "intents": [
            {"target": "crm", "op": "lead",
             "data": {"客户名称": "测试客户A", "联系人": "张三", "联系方式": "13800000000",
                      "客户类型": "企业", "非法字段": "应被清洗"}},
            {"target": "crm", "op": "follow",
             "data": {"跟进状态": "商务谈判", "本次跟进内容": "报价 ¥100,000",
                      "非法字段": "应被清洗"}},
            {"target": "crm", "op": "unknown",
             "data": {"客户名称": "X"}},
        ]
    })
    check(preview["candidate_count"] == 3, "应生成 3 条 crm 候选，实际 %d" % preview["candidate_count"])
    lead_c = preview["candidates"][0]
    check(lead_c["auto_write"] is True, "crm 候选应标记 auto_write")
    check("非法字段" not in lead_c["fields"], "lead 候选应清洗非法字段")
    check(lead_c["fields"].get("客户名称") == "测试客户A", "lead 候选保留客户名称")
    follow_c = preview["candidates"][1]
    check(follow_c["field_mapping"]["crm_kind"] == "follow", "op=follow 应识别为 follow")
    check("跟进状态" in follow_c["fields"], "follow 候选保留跟进状态")
    check(preview["unrouted"] == [], "无未路由项")

    print("[2] crm_dispatch 离线逻辑（桩适配器）")
    stub = StubAdapter()
    # 台账写到临时文件避免污染真实数据
    orig_ledger = cd.LEDGER_FILE
    cd.LEDGER_FILE = os.path.join(tempfile.mkdtemp(), "ledger.csv")

    r1 = cd.create_lead(stub, {"客户名称": "客户甲", "联系人": "李四", "联系方式": "13911111111"})
    check(r1.get("action") == "created", "首次建线索应 created")
    r2 = cd.create_lead(stub, {"客户名称": "客户甲", "联系方式": "13911111111"})
    check(r2.get("action") == "reused", "重复客户应 reused")
    r3 = cd.create_lead(stub, {"联系人": "无名"})
    check(r3.get("error"), "缺客户名称应报错")

    f1 = cd.add_follow(stub, r1["row_id"], {"跟进状态": "商务谈判", "本次跟进内容": "首次报价"})
    check(f1.get("action") == "created", "跟进记录应写入")
    check((f1["row_id"], r1["row_id"]) in stub.links, "跟进应自动关联线索")
    f2 = cd.add_follow(stub, r1["row_id"], {"跟进状态": "不存在状态", "本次跟进内容": "x"})
    check(f2.get("error"), "非法跟进状态应报错")
    f3 = cd.add_follow(stub, r1["row_id"], {"跟进状态": "初步沟通"})
    check(f3.get("error"), "缺跟进内容应报错")

    found = cd.find_lead(stub, "客户甲")
    check(found is not None and found.get("联系方式") == "13911111111", "查重应命中")

    print("[3] 台账落盘")
    import csv
    with open(cd.LEDGER_FILE, encoding="utf-8-sig") as fh:
        rows = list(csv.reader(fh))
    check(len(rows) == 3, "台账应有表头+2条写入记录，实际 %d 行" % len(rows))
    check(rows[1][1] == "create_lead", "台账第一条应为 create_lead")
    check(rows[2][1] == "add_follow", "台账第二条应为 add_follow")

    cd.LEDGER_FILE = orig_ledger
    print()
    if FAILED:
        print("FAILED %d 项" % len(FAILED))
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
