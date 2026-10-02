# -*- coding: utf-8 -*-
"""workflows/loop_trigger.py 的离线测试（此前该脚本**全仓库零覆盖**）。

覆盖四件事：
  ① 退出码语义 —— 「CRM 扫不成」必须是 3（skipped），不是 0（success）。
     修之前两种情况都 return 0，接进 DAG 会被记 success，驾驶舱显示
     「来单扫描成功」而实际一条都没扫（典型假指标）。
  ② 幂等 —— 同一客户重复扫描不重复开案。
  ③ 残缺空壳自愈 —— 建案崩在中途留下的「有 customer、无 lead/opportunity」
     空壳必须能被补建（修 FIX-9：此前被「已有案件」短路，永不修复）。
  ④ 死单过滤。

全程只碰临时目录，不触真实控制平面，不触 CRM。
"""
import csv
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from domain.order_to_cash import Service
import workflows.loop_trigger as LT


class _NoRowsAdapter:
    """假 adapter：跟进记录表为空（只用于让 supports(CAP_READ) 分支不炸）。"""

    def list_rows(self, _table):
        return []


def _read_objects(data_dir):
    path = os.path.join(data_dir, "objects.csv")
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def _rewrite_objects(data_dir, rows):
    path = os.path.join(data_dir, "objects.csv")
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)


class LoopTriggerCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="loopt_")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        # 每个用例都还原 _scan_leads，避免污染其它测试
        self._orig_scan = LT._scan_leads
        self.addCleanup(setattr, LT, "_scan_leads", self._orig_scan)

    def _fake_leads(self, *names, device="UWB标签"):
        """把 _scan_leads 换成「扫到这些客户」的假实现。"""
        leads = [{"客户名称": n, "备注": device, "__row_id__": "R-%s" % n} for n in names]
        LT._scan_leads = lambda: (leads, _NoRowsAdapter(), "")

    def _fake_unavailable(self, reason="未配置 CRM Base，无法扫描来单线索"):
        LT._scan_leads = lambda: ([], _NoRowsAdapter(), reason)


# ────────────────────────────────────────────────────────────────────
# ① 退出码：扫不成 ≠ 没有新线索
# ────────────────────────────────────────────────────────────────────
class TestUnavailableExitCode(LoopTriggerCase):
    def test_scan_leads_returns_three_tuple(self):
        """_scan_leads 必须返回 (线索, adapter, 不可用原因) —— 第三项是退出码的依据。"""
        self._fake_unavailable("X")
        out = LT._scan_leads()
        self.assertEqual(len(out), 3)
        self.assertEqual(out[2], "X")

    def test_unavailable_marks_in_result(self):
        """CRM 不可用时 trigger 必须把原因带出来，且一条都不算「扫描成功」。"""
        self._fake_unavailable()
        r = LT.trigger(apply=False, data_dir=self.tmp)
        self.assertTrue(r["unavailable"])
        self.assertEqual(r["scanned"], 0)
        self.assertEqual(r["created"], [])
        self.assertEqual(r["reused"], [])

    def test_main_exits_3_when_crm_unavailable(self):
        """关键：退出码必须是 3（skipped），绝不能是 0（success）。"""
        self._fake_unavailable()
        LT.sys.argv = ["loop_trigger.py", "--yes", "--data-dir", self.tmp]
        with self.assertRaises(SystemExit) as cm:
            LT.main()
        self.assertEqual(cm.exception.code, 3)

    def test_main_exits_0_when_scanned_but_empty(self):
        """「扫了，确实没有新线索」是正常结果 → 退出码 0（与上面严格区分）。"""
        self._fake_leads()
        LT.sys.argv = ["loop_trigger.py", "--yes", "--data-dir", self.tmp]
        self.assertIsNone(LT.main())          # main 不 raise = 退出码 0


# ────────────────────────────────────────────────────────────────────
# ② 幂等：重复扫描不重复开案
# ────────────────────────────────────────────────────────────────────
class TestIdempotency(LoopTriggerCase):
    def test_second_scan_reuses_not_creates(self):
        self._fake_leads("客户甲")
        r1 = LT.trigger(apply=True, data_dir=self.tmp)
        self.assertEqual([x["customer"] for x in r1["created"]], ["客户甲"])
        r2 = LT.trigger(apply=True, data_dir=self.tmp)
        self.assertEqual(r2["created"], [])
        self.assertEqual([x["customer"] for x in r2["reused"]], ["客户甲"])

    def test_完整案件不触发补建(self):
        """完整案件（3 个种子对象齐）→ 只能判 reused，不许误判成「待补」。"""
        self._fake_leads("客户乙")
        LT.trigger(apply=True, data_dir=self.tmp)
        kinds = {r.get("object_type") for r in _read_objects(self.tmp)}
        self.assertEqual(kinds, {"customer", "lead", "opportunity"})
        r = LT.trigger(apply=True, data_dir=self.tmp)
        self.assertEqual(r["reused"][0]["customer"], "客户乙")
        self.assertEqual(r["repaired"], [])
        self.assertEqual(r["planned"], [])

    def test_预览不写盘(self):
        self._fake_leads("客户丙")
        LT.trigger(apply=False, data_dir=self.tmp)
        self.assertEqual(_read_objects(self.tmp), [])


# ────────────────────────────────────────────────────────────────────
# ③ 残缺空壳自愈（修 FIX-9 —— 此前 repaired 分支是不可达的死代码）
# ────────────────────────────────────────────────────────────────────
class TestHalfWrittenCaseHeal(LoopTriggerCase):
    def _make_half_written(self, customer="客户丁"):
        """先建完整案件，再把 lead/opportunity 删掉 → 复现「建案崩在中途」的空壳。

        注意 source_event_id 必须与 `_fake_leads` 生成的**完全一致**：
        `start_case` 的幂等键 = token(客户, 产品, source_event_id)，对不上时
        它不会走「幂等命中 → _repair_case」，而是当成新案子又建一套
        → 同一客户出现两个 root_id（这个依赖本身值得固化成测试，见下一个用例）。
        """
        Service(self.tmp).start_case(customer, "UWB标签", owner="项目经理",
                                     source_event_id="crm-lead:R-%s" % customer,
                                     approved=True)
        rows = _read_objects(self.tmp)
        kept = [r for r in rows if r.get("object_type") == "customer"]
        _rewrite_objects(self.tmp, kept)
        self.assertEqual([r.get("object_type") for r in _read_objects(self.tmp)],
                         ["customer"])          # 前置：确实只剩 customer
        return customer

    def test_source_event_id_不稳定时会另建一案_而非补建(self):
        """固化上面那条依赖：幂等键对不上 → 不会补建，而是多出一个 root_id。

        这是既有设计（幂等键含 source_event_id），不是本批引入的缺陷；
        写成测试是为了让它**可见**——将来若改成「按客户名 + 产品」判重，
        本用例会失败，提醒改动者重新评估 FIX-9 的自愈路径。
        """
        cust = "客户庚"
        Service(self.tmp).start_case(cust, "UWB标签", owner="项目经理",
                                     source_event_id="crm-lead:OTHER", approved=True)
        rows = _read_objects(self.tmp)
        _rewrite_objects(self.tmp, [r for r in rows if r.get("object_type") == "customer"])
        self._fake_leads(cust)
        r = LT.trigger(apply=True, data_dir=self.tmp)
        self.assertEqual(r["repaired"], [], "幂等键对不上时不应误报「已补建」")
        self.assertEqual([x["customer"] for x in r["created"]], [cust])
        roots = {r2.get("root_id") for r2 in _read_objects(self.tmp)}
        self.assertEqual(len(roots), 2, "预期出现两个 root_id（该依赖的后果）")

    def test_空壳不再被谎报为复用(self):
        """旧实现：只认 customer 就判「已有案件」→ reused（谎报）。
        新实现：必须报出「待补残缺案件」，且不写盘（preview）。"""
        cust = self._make_half_written()
        self._fake_leads(cust)
        r = LT.trigger(apply=False, data_dir=self.tmp)
        self.assertEqual(r["reused"], [], "空壳案件被错判成「已有案件」→ 永久无法自愈")
        self.assertEqual([x["customer"] for x in r["planned"]], [cust])
        self.assertEqual(tuple(r["planned"][0]["repair_missing"]), ("lead", "opportunity"))
        self.assertEqual([x.get("object_type") for x in _read_objects(self.tmp)],
                         ["customer"], "preview 不允许写盘")

    def test_空壳在_apply_下被自愈补齐且幂等(self):
        cust = self._make_half_written()
        self._fake_leads(cust)
        r = LT.trigger(apply=True, data_dir=self.tmp)
        self.assertEqual([(x["customer"], tuple(x["repaired"])) for x in r["repaired"]],
                         [(cust, ("lead", "opportunity"))])
        after = _read_objects(self.tmp)
        self.assertEqual(sorted(x.get("object_type") for x in after),
                         ["customer", "lead", "opportunity"])
        # 幂等：补建不能把已有对象再来一遍
        self.assertEqual(len(after), 3)

    def test_剩余根对象_bug_不会复现(self):
        """_existing_cases 的 missing 判据要与 CASE_SEED_KINDS 同源，不能各写一份。"""
        from domain.order_to_cash import CASE_SEED_KINDS
        cust = self._make_half_written()
        svc = Service(self.tmp)
        cases = LT._existing_cases(svc)
        self.assertEqual(tuple(cases[cust]["missing"]),
                         tuple(k for k in CASE_SEED_KINDS if k != "customer"))


# ────────────────────────────────────────────────────────────────────
# ④ 死单过滤
# ────────────────────────────────────────────────────────────────────
class TestDeadLeadFilter(LoopTriggerCase):
    def test_死单提示词被过滤且不计入建案(self):
        self._fake_leads("客户戊", device="项目终止 无效线索")
        r = LT.trigger(apply=True, data_dir=self.tmp)
        self.assertEqual([x["customer"] for x in r["dead"]], ["客户戊"])
        self.assertEqual(r["created"], [])
        self.assertEqual(_read_objects(self.tmp), [])

    def test_产品意向缺省为待确认(self):
        self._fake_leads("客户己", device="")
        r = LT.trigger(apply=False, data_dir=self.tmp)
        self.assertEqual(r["planned"][0]["product"], "待确认")


if __name__ == "__main__":
    unittest.main(verbosity=2)
