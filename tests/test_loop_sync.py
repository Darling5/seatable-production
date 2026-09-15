# -*- coding: utf-8 -*-
"""loop_sync.py 离线单测：FakeAdapter 模拟云端，覆盖建表/幂等 upsert/更新。"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))

from domain.order_to_cash import Service
import loop_sync


class FakeAdapter:
    """模拟 SeaTableAdapter：内存表 + 中文列名读写。"""

    def __init__(self):
        self.tables = {}       # cloud_table_name -> {columns: [names], rows: {row_id: dict}}
        self._seq = 0
        self._meta = None      # 模拟 SeaTableAdapter 的元数据缓存

    def auth(self):
        pass

    def _ensure_meta(self):
        # 从内存表构造与真实 metadata 同构的 _meta
        self._meta = {"tables": [{"name": n, "_id": "fake-%d" % i}
                                 for i, n in enumerate(self.tables)]}

    def list_rows(self, table):
        t = self.tables.get(table)
        if t is None:
            raise KeyError("表不存在：%s" % table)
        out = []
        for row_id, row in t["rows"].items():
            d = dict(row)
            d["__row_id__"] = row_id
            out.append(d)
        return out

    def append_row(self, table, data):
        t = self.tables.get(table)
        if t is None:
            raise KeyError("表不存在：%s" % table)
        self._seq += 1
        row_id = "fake-row-%d" % self._seq
        t["rows"][row_id] = {k: v for k, v in data.items() if k != "__row_id__"}
        return row_id

    def update_row(self, table, row_id, data):
        t = self.tables.get(table)
        if t is None or row_id not in t["rows"]:
            raise KeyError("行不存在：%s" % row_id)
        t["rows"][row_id].update({k: v for k, v in data.items() if k != "__row_id__"})

    # loop_sync.ensure_tables 需要的建表能力
    def _base(self):
        return "fake://dtable"

    _h = {"Authorization": "Bearer fake"}

    def _create_table(self, name, columns):
        self.tables[name] = {"columns": [c["column_name"] for c in columns],
                             "rows": {}}


class EnsureTablesPatcher:
    """把 requests.post 建表调用劫持到 FakeAdapter。"""

    def __init__(self, adapter):
        self.adapter = adapter

    def __call__(self, url, headers=None, json=None, timeout=None, **kw):
        class R:
            status_code = 200
            def raise_for_status(self):
                pass
            def json(self):
                return {"success": True}
        # json = {"table_name": ..., "columns": [...]}
        self.adapter._create_table(json["table_name"], json["columns"])
        return R()


class TestLoopSync(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="loop_sync_")
        self.svc = Service(self.tmp.name)
        self.fake = FakeAdapter()
        # 劫持 loop_sync.ensure_tables 内的 requests.post
        import requests
        self._orig_post = requests.post
        requests.post = EnsureTablesPatcher(self.fake)
        # prepare local data：建一个案件并推进两步
        root = self.svc.start_case("客户A", "产品B", owner="小王",
                                   source_event_id="EV-T1", approved=True)["root_id"]
        self.svc.advance(root, "opportunity", "转商机", approved=True)
        self.svc.advance(root, "requirement_confirming", "进入需求确认", approved=True)
        self.root = root

    def tearDown(self):
        import requests
        requests.post = self._orig_post
        self.tmp.cleanup()

    def test_dry_run_creates_nothing(self):
        report = loop_sync.sync(apply=False, data_dir=self.tmp.name, adapter=self.fake)
        self.assertEqual(set(report["tables"].values()), {"would_create"})
        self.assertEqual(self.fake.tables, {})  # 云端零建表

    def test_apply_creates_tables_and_rows(self):
        report = loop_sync.sync(apply=True, data_dir=self.tmp.name, adapter=self.fake)
        self.assertEqual(set(report["tables"].values()), {"created"})
        self.assertEqual(set(self.fake.tables),
                         {"业务对象台账", "状态轨迹", "证据链", "审批记录"})
        obj_rows = self.fake.list_rows("业务对象台账")
        self.assertGreaterEqual(len(obj_rows), 3)  # customer/lead/opportunity
        ids = {r["object_id"] for r in obj_rows}
        self.assertIn(self.root, ids)

    def test_idempotent_double_apply(self):
        loop_sync.sync(apply=True, data_dir=self.tmp.name, adapter=self.fake)
        report2 = loop_sync.sync(apply=True, data_dir=self.tmp.name, adapter=self.fake)
        for st in report2["stats"]:
            self.assertEqual(st["append"], 0, st)
            self.assertEqual(st["update"], 0, st)
            self.assertGreater(st["unchanged"], 0, st)

    def test_update_flow_propagates_changes(self):
        loop_sync.sync(apply=True, data_dir=self.tmp.name, adapter=self.fake)
        # 本地推进状态 → 云端应出现 update
        self.svc.advance(self.root, "solution_confirming", "需求确认完成", approved=True)
        self.svc.revise(self.root, "solution", "方案 V1：蓝牙定位", "初稿", approved=True)
        report = loop_sync.sync(apply=True, data_dir=self.tmp.name, adapter=self.fake)
        objs = next(s for s in report["stats"] if s["table"] == "业务对象台账")
        self.assertGreaterEqual(objs["update"], 1)   # root 状态变更
        self.assertGreaterEqual(objs["append"], 1)   # solution V1 新对象
        row = next(r for r in self.fake.list_rows("业务对象台账")
                   if r["object_id"] == self.root)
        self.assertEqual(row["state"], "solution_confirming")

    def test_local_only_tables_filter(self):
        report = loop_sync.sync(apply=True, only={"objects"},
                                data_dir=self.tmp.name, adapter=self.fake)
        synced = {s["table"] for s in report["stats"]}
        self.assertEqual(synced, {"业务对象台账"})


if __name__ == "__main__":
    unittest.main()
