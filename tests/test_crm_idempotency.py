#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""crm_dispatch.py 统一幂等键（v2.0 P0-3）的单元测试。

用临时目录替代真实台账文件，验证：
  1. 同 idem_key 重复写入 -> idempotent_reuse，不重复调 append_row
  2. 不同 idem_key 正常写入
  3. 无 idem_key（旧行为）不受影响，照样写
  4. 旧格式台账（无幂等键列）兼容：能读、能追加
  5. --idem-key 显式优先于自动生成
"""
import csv
import os
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)


class _StubAdapter:
    stub_mode = True  # 让 _link_single 走内存路径，不发 HTTP

    def __init__(self, valid_cols=None):
        self.appends = []
        self.links = []
        self.rows = {}
        self._n = 0
        # 模拟真实云端：只接受表中真实存在的列（缺列 = HTTP 200 静默丢）
        self.valid_cols = valid_cols if valid_cols is not None else \
            {"客户名称", "客户类型", "联系人", "联系方式",
             "跟进人姓名（统计用）", "跟进状态", "本次跟进内容", "下次跟进日期"}

    def append_row(self, table, row):
        self._n += 1
        rid = "row_%d" % self._n
        clean = {k: v for k, v in row.items() if k in self.valid_cols}
        self.rows[rid] = dict(clean)
        self.rows[rid]["__row_id__"] = rid
        self.appends.append((table, row))
        return rid

    def list_rows(self, table):
        return [dict(r) for r in self.rows.values()]

    def link(self, table, other, link_id, row_id, other_row_ids):
        self.links.append((table, other, row_id, other_row_ids))


class TestIdempotencyKeys(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="crm_ledger_")
        self._patch_ledger()
        self.addCleanup(self._cleanup)

    def _patch_ledger(self):
        import crm_dispatch as cd
        self.cd = cd
        self._orig = cd.LEDGER_FILE
        cd.LEDGER_FILE = os.path.join(self.tmp, "crm_dispatch_ledger.csv")

    def _cleanup(self):
        self.cd.LEDGER_FILE = self._orig

    def _write_old_ledger(self):
        """旧格式台账：首行没有幂等键列。"""
        with open(self.cd.LEDGER_FILE, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f)
            w.writerow(["时间", "动作", "客户名称", "row_id", "表格", "内容摘要", "撤销", "备注"])
            w.writerow(["2026-09-10 09:00:00", "create_lead", "老客户", "r_old",
                        "销售线索表", "{...}", "", ""])

    def test_same_key_second_write_skipped(self):
        a = _StubAdapter()
        data = {"客户名称": "云南亚雄", "联系人": "张三"}
        r1 = self.cd.create_lead(a, data, idem_key="crm-lead:2026-09-12|云南亚雄")
        r2 = self.cd.create_lead(a, data, idem_key="crm-lead:2026-09-12|云南亚雄")
        self.assertEqual(r1["action"], "created")
        self.assertEqual(r2["action"], "idempotent_reuse")
        self.assertEqual(len(a.appends), 1)  # 只写了一次

    def test_different_key_writes(self):
        a = _StubAdapter()
        r1 = self.cd.create_lead(a, {"客户名称": "客户A"}, idem_key="k1")
        r2 = self.cd.create_lead(a, {"客户名称": "客户B"}, idem_key="k2")
        self.assertEqual(r1["action"], "created")
        self.assertEqual(r2["action"], "created")
        self.assertEqual(len(a.appends), 2)

    def test_no_key_backward_compatible(self):
        a = _StubAdapter()
        r = self.cd.create_lead(a, {"客户名称": "客户C"})
        self.assertEqual(r["action"], "created")
        self.assertEqual(len(a.appends), 1)

    def test_follow_idempotent(self):
        a = _StubAdapter()
        data = {"跟进状态": "商务谈判", "本次跟进内容": "客户要求周五前报价"}
        r1 = self.cd.add_follow(a, "lead_r1", data, idem_key="crm-follow:d|X|报价")
        r2 = self.cd.add_follow(a, "lead_r1", data, idem_key="crm-follow:d|X|报价")
        self.assertEqual(r1["action"], "created")
        self.assertEqual(r2["action"], "idempotent_reuse")
        self.assertEqual(len(a.appends), 1)

    def test_old_ledger_compatible(self):
        """旧台账存在时：读取不炸，新增行带幂等键，且命中判定只看新键。"""
        self._write_old_ledger()
        a = _StubAdapter()
        r = self.cd.create_lead(a, {"客户名称": "新客户"}, idem_key="k-new")
        self.assertEqual(r["action"], "created")
        # 再跑一次同键 -> 拦截
        r2 = self.cd.create_lead(a, {"客户名称": "新客户"}, idem_key="k-new")
        self.assertEqual(r2["action"], "idempotent_reuse")

    def test_key_helpers(self):
        self.assertEqual(self.cd._idem_key("crm-follow", " A ", "", "B"),
                         "crm-follow:A|B")
        self.assertTrue(self.cd._idem_key("x", ""))
        self.assertFalse(self.cd._ledger_key_used("never:seen"))

    def test_cli_auto_key(self):
        """CLI 自动键：同一天同客户同内容 -> 同键。"""
        args = mock.Mock(idem_key="")
        k1 = self.cd._cli_idem_key(args, "crm-follow", "客户A", "内容X")
        k2 = self.cd._cli_idem_key(args, "crm-follow", "客户A", "内容X")
        self.assertEqual(k1, k2)
        args2 = mock.Mock(idem_key="explicit")
        self.assertEqual(self.cd._cli_idem_key(args2, "p"), "explicit")

    def test_ledger_row_has_key_column(self):
        a = _StubAdapter()
        self.cd.create_lead(a, {"客户名称": "客户D"}, idem_key="k-ledger")
        with open(self.cd.LEDGER_FILE, encoding="utf-8-sig", newline="") as f:
            rows = list(csv.DictReader(f))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].get("幂等键"), "k-ledger")


class TestReadbackVerification(unittest.TestCase):
    """v2.0 写链迁移 DataService 后：读回验证生效。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="crm_ledger_")
        import crm_dispatch as cd
        self.cd = cd
        self._orig = cd.LEDGER_FILE
        cd.LEDGER_FILE = os.path.join(self.tmp, "crm_dispatch_ledger.csv")
        self.addCleanup(setattr, cd, "LEDGER_FILE", self._orig)

    def test_column_drop_detected_in_lead(self):
        """「联系人」列在云端不存在：append 成功但读回缺列 -> 报错不记台账。"""
        a = _StubAdapter(valid_cols={"客户名称"})  # 联系人列缺失
        r = self.cd.create_lead(a, {"客户名称": "客户X", "联系人": "张三"})
        self.assertIn("error", r)
        self.assertIn("读回验证失败", r["error"])
        # 未记台账：失败写入不入账
        self.assertFalse(os.path.exists(self.cd.LEDGER_FILE))

    def test_column_drop_detected_in_follow(self):
        a = _StubAdapter(valid_cols={"本次跟进内容"})  # 跟进状态列缺失
        r = self.cd.add_follow(a, "lead_r1", {
            "跟进状态": "商务谈判", "本次跟进内容": "客户要求周五前报价"})
        self.assertIn("error", r)
        self.assertIn("读回验证失败", r["error"])
        self.assertEqual(a.links, [])   # 没走到关联步骤

    def test_normal_write_still_works(self):
        a = _StubAdapter()  # 全列齐
        r = self.cd.create_lead(a, {"客户名称": "客户Y", "联系人": "李四"})
        self.assertEqual(r["action"], "created")
        # 台账已记
        self.assertTrue(os.path.exists(self.cd.LEDGER_FILE))
        with open(self.cd.LEDGER_FILE, encoding="utf-8-sig", newline="") as f:
            rows = list(csv.DictReader(f))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["动作"], "create_lead")

    def test_follow_links_after_verify(self):
        """验证通过后才做单向关联（写链顺序：写→验→关联→台账）。"""
        a = _StubAdapter()
        r = self.cd.add_follow(a, "lead_r9", {
            "跟进状态": "商务谈判", "本次跟进内容": "客户要求周五前报价"})
        self.assertEqual(r["action"], "created")
        self.assertEqual(len(a.links), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2, exit=False)
