#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""application/dataservice.py（v2.0 P0-4）的单元测试。

零网络：StubAdapter 模拟 SeaTable 行为，重点覆盖：
  1. 路由策略：production/tasks 永远出候选不直写；crm 在 apply 下自动写
  2. preview 模式：任何路由都不落写入
  3. 读回验证：字段一致通过；中文列名静默丢列（HTTP 200 但读回缺失）被抓住
  4. 幂等键：同键重写 -> skipped_reuse
  5. update 缺 row_id -> blocked
  6. 台账：每次写入留痕，verify_failed 也记
"""
import csv
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

from application import contracts as C                     # noqa: E402
from application.dataservice import (DataService,         # noqa: E402
                                     WriteRequest, WriteResult)


class StubAdapter:
    """模拟 SeaTable：中文列名写错时静默丢列（这是真实坑，必须能复现）。"""
    def __init__(self):
        self.rows = {}
        self._n = 0
        self.valid_cols = set()   # 表内真实存在的列

    def _new_rid(self):
        self._n += 1
        return "r%d" % self._n

    def append_row(self, table, row):
        rid = self._new_rid()
        # 只保留真实存在的列 —— 复现「HTTP 200 但列没写进去」
        clean = {k: v for k, v in row.items() if k in self.valid_cols}
        self.rows[rid] = dict(clean)
        self.rows[rid]["__row_id__"] = rid
        return rid

    def update_row(self, table, rid, row):
        if rid not in self.rows:
            return
        clean = {k: v for k, v in row.items() if k in self.valid_cols}
        self.rows[rid].update(clean)

    def list_rows(self, table):
        return [dict(r) for r in self.rows.values()]


def _ds(tmp, stub):
    return DataService(adapter_factory=lambda name: stub, data_dir=tmp)


def _req(**kw):
    d = dict(table="销售线索表", row={"客户名称": "云南亚雄", "联系人": "张三"},
             route="crm", actor="test")
    d.update(kw)
    return WriteRequest(**d)


class TestRoutePolicies(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ds_test_")
        self.stub = StubAdapter()
        self.stub.valid_cols = {"客户名称", "联系人", "跟进状态"}
        self.ds = _ds(self.tmp, self.stub)

    def test_crm_write_in_apply(self):
        r = self.ds.write(_req(), mode=C.MODE_APPLY)
        self.assertEqual(r.status, "written")
        self.assertTrue(r.verified)
        self.assertEqual(len(self.stub.rows), 1)

    def test_crm_preview_no_write(self):
        r = self.ds.write(_req(), mode=C.MODE_PREVIEW)
        self.assertEqual(r.status, "candidate")
        self.assertEqual(len(self.stub.rows), 0)

    def test_production_always_candidate(self):
        r = self.ds.write(_req(route="production"), mode=C.MODE_APPLY)
        self.assertEqual(r.status, "candidate")
        self.assertEqual(len(self.stub.rows), 0)   # 没写

    def test_tasks_always_candidate(self):
        r = self.ds.write(_req(route="tasks"), mode=C.MODE_APPLY)
        self.assertEqual(r.status, "candidate")

    def test_unknown_route_blocked(self):
        r = self.ds.write(_req(route=" nowhere"), mode=C.MODE_APPLY)
        self.assertEqual(r.status, "blocked")


class TestReadbackVerification(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ds_test_")
        self.stub = StubAdapter()
        self.ds = _ds(self.tmp, self.stub)

    def test_silent_column_drop_detected(self):
        """中文列名写错：append 成功返回 row_id，但列没进表 -> verify_failed。"""
        self.stub.valid_cols = {"客户名称"}  # 「联系人」列不存在
        r = self.ds.write(_req(), mode=C.MODE_APPLY)
        self.assertEqual(r.status, "verify_failed")
        self.assertFalse(r.verified)
        self.assertIn("联系人", r.message)   # 点名丢的列

    def test_normal_write_passes(self):
        self.stub.valid_cols = {"客户名称", "联系人"}
        r = self.ds.write(_req(), mode=C.MODE_APPLY)
        self.assertEqual(r.status, "written")
        self.assertTrue(r.verified)

    def test_missing_row_detected(self):
        """append 返回 rid 但 list_rows 查不到（云端延迟/异常）-> verify_failed。"""
        self.stub.valid_cols = {"客户名称", "联系人"}
        orig = self.stub.append_row

        def append_drop(table, row):
            orig(table, row)
            return "r_phantom"   # 返回一个不存在的 rid
        self.stub.append_row = append_drop
        r = self.ds.write(_req(), mode=C.MODE_APPLY)
        self.assertEqual(r.status, "verify_failed")

    def test_list_dict_fields_skipped(self):
        """链接列/多选（list/dict 值）只查存在性，不硬比。"""
        self.stub.valid_cols = {"客户名称", "标签"}
        req = _req(row={"客户名称": "客户X", "标签": ["重点", "新客"]})
        r = self.ds.write(req, mode=C.MODE_APPLY)
        self.assertEqual(r.status, "written")


class TestIdempotency(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ds_test_")
        self.stub = StubAdapter()
        self.stub.valid_cols = {"客户名称", "联系人"}
        self.ds = _ds(self.tmp, self.stub)

    def test_same_key_skipped(self):
        r1 = self.ds.write(_req(idem_key="k1"), mode=C.MODE_APPLY)
        r2 = self.ds.write(_req(idem_key="k1"), mode=C.MODE_APPLY)
        self.assertEqual(r1.status, "written")
        self.assertEqual(r2.status, "skipped_reuse")
        self.assertEqual(len(self.stub.rows), 1)

    def test_no_key_writes_twice(self):
        self.ds.write(_req(), mode=C.MODE_APPLY)
        self.ds.write(_req(), mode=C.MODE_APPLY)
        self.assertEqual(len(self.stub.rows), 2)   # 旧行为：无键不去重


class TestUpdateAndLedger(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ds_test_")
        self.stub = StubAdapter()
        # _req() 默认行含「客户名称/联系人」，update 用例用「跟进状态」——全开
        self.stub.valid_cols = {"客户名称", "联系人", "跟进状态"}
        self.ds = _ds(self.tmp, self.stub)

    def test_update_without_row_id_blocked(self):
        r = self.ds.write(_req(action="update", row={"跟进状态": "商务谈判"}),
                          mode=C.MODE_APPLY)
        self.assertEqual(r.status, "blocked")

    def test_update_with_row_id(self):
        rid = self.stub.append_row("销售线索表", {"客户名称": "客户A"})
        r = self.ds.write(_req(action="update", row={"跟进状态": "商务谈判"},
                               row_id=rid), mode=C.MODE_APPLY)
        self.assertEqual(r.status, "written")
        self.assertEqual(self.stub.rows[rid]["跟进状态"], "商务谈判")

    def test_ledger_written(self):
        self.ds.write(_req(), mode=C.MODE_APPLY)
        path = os.path.join(self.tmp, "write_ledger.csv")
        self.assertTrue(os.path.exists(path))
        with open(path, encoding="utf-8-sig", newline="") as f:
            rows = list(csv.DictReader(f))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["路由"], "crm")
        self.assertEqual(rows[0]["读回验证"], "通过")

    def test_ledger_records_verify_failure(self):
        self.stub.valid_cols = set()   # 全列都丢 -> verify_failed
        self.ds.write(_req(), mode=C.MODE_APPLY)
        path = os.path.join(self.tmp, "write_ledger.csv")
        with open(path, encoding="utf-8-sig", newline="") as f:
            rows = list(csv.DictReader(f))
        self.assertEqual(rows[0]["读回验证"], "失败")


class TestContractsSanity(unittest.TestCase):
    def test_route_policies_present(self):
        self.assertEqual(C.ROUTE_POLICIES["production"], C.WRITE_APPROVAL_REQUIRED)
        self.assertEqual(C.ROUTE_POLICIES["crm"], C.WRITE_AUTO_WITH_LEDGER)


if __name__ == "__main__":
    unittest.main(verbosity=2, exit=False)
