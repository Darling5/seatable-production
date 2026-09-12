# -*- coding: utf-8 -*-
"""won_deal.py 赢单转化引擎的离线测试（stub 适配器，零网络）。"""
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import won_deal as wd


class StubAdapter:
    """内存适配器：模拟 append_row + list_rows + auth。"""

    def __init__(self, tables=None):
        self.tables = tables or {wd.PROFILE_TABLE: [], wd.CONTRACT_TABLE: [],
                                 wd.PROJECT_TABLE: []}
        self._n = 0
        self.fail_verify = False  # 测试读回验证用

    def auth(self):
        pass

    def list_rows(self, table):
        return [dict(r) for r in self.tables.get(table, [])]

    def append_row(self, table, data):
        self._n += 1
        rid = "row_%d" % self._n
        row = dict(data)
        row["__row_id__"] = rid
        if self.fail_verify:
            row = {k: (v + "_x" if k in ("客户名称",) else v) for k, v in row.items()}
        self.tables.setdefault(table, []).append(row)
        return rid


def make_args(**kw):
    base = dict(customer="云南亚雄科技", product="天然气人员定位", amount=464250.0,
                delivery_days=90, payment="50%,30%,20%", contact="柴义",
                phone="17862926609")
    base.update(kw)
    return type("A", (), base)()


class TestParsePayments(unittest.TestCase):
    def test_percent(self):
        p = wd._parse_payments("50%,30%,20%", 464250)
        self.assertEqual(p["下单付"], 232125.0)
        self.assertEqual(p["收货付"], 139275.0)
        self.assertEqual(p["验收付"], 92850.0)
        self.assertEqual(p["待收"], 464250.0)
        self.assertEqual(p["实收"], 0)

    def test_absolute(self):
        p = wd._parse_payments("232125,139275,92850", 464250)
        self.assertEqual(p["下单付"], 232125.0)

    def test_mismatch_rejected(self):
        with self.assertRaises(ValueError):
            wd._parse_payments("50%,30%,10%", 100000)

    def test_wrong_segments_rejected(self):
        with self.assertRaises(ValueError):
            wd._parse_payments("50%,50%", 100000)


class TestBuildPlan(unittest.TestCase):
    def test_plan_shape(self):
        plan = wd.build_plan("云南亚雄科技", "天然气人员定位", 464250, 90,
                             "50%,30%,20%", "柴义", "17862926609")
        self.assertEqual(plan["profile"]["客户名称"], "云南亚雄科技")
        self.assertEqual(plan["profile"]["联系人"], "柴义")
        self.assertEqual(plan["contract"]["关联销售线索"], "云南亚雄科技")
        self.assertEqual(plan["contract"]["合同金额"], 464250)
        self.assertEqual(plan["project"]["项目"], "云南亚雄科技-天然气人员定位")
        self.assertEqual(plan["project"]["状态"], "计划中")
        self.assertEqual(plan["project"]["阶段"], "立项")
        self.assertEqual(plan["project"]["待收"], 464250)
        # 交期 = 今天 + 90 天
        import datetime as dt
        want = (dt.date.today() + dt.timedelta(days=90)).isoformat()
        self.assertEqual(plan["project"]["合同交期"], want)

    def test_extra_profile_unknown_field_rejected(self):
        with self.assertRaises(ValueError):
            wd.build_plan("X", "Y", 100, 10, "100%,0%,0%",
                          extra_profile={"不存在的列": "v"})

    def test_extra_profile_merged(self):
        plan = wd.build_plan("X", "Y", 100, 10, "100%,0%,0%",
                             extra_profile={"客户地址": "云南省昆明市", "发票抬头": "云南亚雄科技有限公司"})
        self.assertEqual(plan["profile"]["客户地址"], "云南省昆明市")


class TestNextNumbers(unittest.TestCase):
    def test_project_no_same_day_increments(self):
        import datetime as dt
        today = dt.date.today().strftime("%Y%m%d")
        rows = [{"项目编号": today + "-001"}, {"项目编号": today + "-002"},
                {"项目编号": "20200101-999"}]
        self.assertEqual(wd._next_project_no(rows), today + "-003")

    def test_project_no_fresh_day(self):
        self.assertRegex(wd._next_project_no([]), r"^\d{8}-001$")

    def test_contract_no_increments(self):
        rows = [{"合同编号": "HTBH-0012"}, {"合同编号": "HTBH-0007"}]
        self.assertEqual(wd._next_contract_no(rows), "HTBH-0013")

    def test_contract_no_fresh(self):
        self.assertEqual(wd._next_contract_no([]), "HTBH-0001")


class TestExecute(unittest.TestCase):
    def test_fresh_customer_creates_all_three(self):
        a = StubAdapter()
        plan = wd.build_plan("云南亚雄科技", "天然气人员定位", 464250, 90,
                             "50%,30%,20%", "柴义", "17862926609")
        results = wd.execute(a, plan, "云南亚雄科技")
        by_table = {r["table"]: r for r in results}
        self.assertEqual(by_table[wd.PROFILE_TABLE]["action"], "created")
        self.assertEqual(by_table[wd.CONTRACT_TABLE]["action"], "created")
        self.assertEqual(by_table[wd.PROJECT_TABLE]["action"], "created")
        self.assertEqual(by_table[wd.CONTRACT_TABLE]["合同编号"], "HTBH-0001")
        self.assertTrue(by_table[wd.PROJECT_TABLE]["项目编号"].endswith("-001"))
        # 三表各 1 行
        self.assertEqual(len(a.tables[wd.PROFILE_TABLE]), 1)
        self.assertEqual(len(a.tables[wd.CONTRACT_TABLE]), 1)
        self.assertEqual(len(a.tables[wd.PROJECT_TABLE]), 1)

    def test_reuse_on_second_run(self):
        a = StubAdapter()
        plan = wd.build_plan("云南亚雄科技", "天然气人员定位", 464250, 90,
                             "50%,30%,20%", "柴义", "17862926609")
        wd.execute(a, plan, "云南亚雄科技")
        results = wd.execute(a, plan, "云南亚雄科技")  # 重跑：全部复用
        self.assertEqual([r["action"] for r in results], ["reused", "reused", "reused"])
        # 仍然各只有 1 行——幂等
        self.assertEqual(len(a.tables[wd.PROFILE_TABLE]), 1)
        self.assertEqual(len(a.tables[wd.PROJECT_TABLE]), 1)

    def test_readback_mismatch_raises(self):
        a = StubAdapter()
        a.fail_verify = True  # 模拟「HTTP 200 但数据没落库」
        plan = wd.build_plan("X公司", "产品", 1000, 10, "100%,0%,0%")
        with self.assertRaises(RuntimeError):
            wd.execute(a, plan, "X公司")

    def test_same_customer_different_amount_new_contract(self):
        a = StubAdapter()
        p1 = wd.build_plan("客户A", "产品", 1000, 10, "100%,0%,0%")
        wd.execute(a, p1, "客户A")
        p2 = wd.build_plan("客户A", "产品二期", 2000, 10, "100%,0%,0%")
        results = wd.execute(a, p2, "客户A")
        by_table = {r["table"]: r for r in results}
        self.assertEqual(by_table[wd.PROFILE_TABLE]["action"], "reused")  # 档案复用
        self.assertEqual(by_table[wd.CONTRACT_TABLE]["action"], "created")  # 新合同
        self.assertEqual(by_table[wd.PROJECT_TABLE]["action"], "created")  # 新项目


class TestRenderPlan(unittest.TestCase):
    def test_render_readable(self):
        plan = wd.build_plan("云南亚雄科技", "天然气人员定位", 464250, 90,
                             "50%,30%,20%", "柴义", "17862926609")
        text = wd.render_plan(plan, {})
        self.assertIn("赢单转化写入方案", text)
        self.assertIn("云南亚雄科技-天然气人员定位", text)
        self.assertIn("464250", text)
        self.assertIn("--yes", text)
        # 复用场景
        text2 = wd.render_plan(plan, {"profile": {"客户名称": "云南亚雄科技"},
                                      "project": None})
        self.assertIn("复用不重建", text2)


class TestLedger(unittest.TestCase):
    def test_ledger_append(self):
        import tempfile, shutil
        old = wd.LEDGER_FILE
        tmp = tempfile.mkdtemp()
        try:
            wd.LEDGER_FILE = os.path.join(tmp, "ledger.csv")
            wd._ledger_append("create_profile", "客户A", "row_1", wd.PROFILE_TABLE, "摘要")
            wd._ledger_append("create_project", "客户A", "row_2", wd.PROJECT_TABLE, "摘要2")
            with open(wd.LEDGER_FILE, encoding="utf-8-sig") as f:
                rows = list(csv.reader(f))
            self.assertEqual(len(rows), 3)  # header + 2
            self.assertEqual(rows[1][1], "create_profile")
        finally:
            wd.LEDGER_FILE = old
            shutil.rmtree(tmp, ignore_errors=True)


import csv  # noqa: E402 — TestLedger 用


if __name__ == "__main__":
    unittest.main()
