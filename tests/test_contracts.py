# -*- coding: utf-8 -*-
"""application/contracts.py 的离线单测（零 I/O，跑得飞快）。"""
import datetime
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from application import contracts as C


def _ctx(mode=C.MODE_PREVIEW, yes=False):
    return C.RunContext(run_id="test-run", workflow="test", mode=mode, yes=yes)


def _step(side_effect=C.SIDE_READ_ONLY, write_mode=None, fn=None):
    return C.StepSpec(
        id="s", name="step", run=fn or (lambda ctx: C.StepResult(step_id="s")),
        side_effect=side_effect, write_mode=write_mode)


class TestGates(unittest.TestCase):
    """三道闸：destructive 要 --yes；online_write 要 apply；approval_required 要 --yes。"""

    def test_read_only_always_allowed(self):
        self.assertEqual(_step().allowed_in(_ctx()), (True, ""))
        self.assertEqual(_step().allowed_in(_ctx(mode=C.MODE_APPLY)), (True, ""))

    def test_local_append_allowed_in_preview(self):
        # local_append（本地快照/台账/草稿）在 preview 也放行——
        # 这正是 preview 能产出草稿的原因；只有 online/destructive 才被拦。
        s = _step(side_effect=C.SIDE_LOCAL_APPEND)
        ok, why = s.allowed_in(_ctx())
        self.assertTrue(ok)
        self.assertEqual(why, "")
        ok2, _ = s.allowed_in(_ctx(mode=C.MODE_APPLY))
        self.assertTrue(ok2)

    def test_online_write_needs_apply(self):
        s = _step(side_effect=C.SIDE_ONLINE_WRITE)
        ok, why = s.allowed_in(_ctx())
        self.assertFalse(ok)
        self.assertIn("preview", why)

    def test_online_write_approval_needs_yes(self):
        s = _step(side_effect=C.SIDE_ONLINE_WRITE, write_mode=C.WRITE_APPROVAL_REQUIRED)
        ok, why = s.allowed_in(_ctx(mode=C.MODE_APPLY))
        self.assertFalse(ok)
        self.assertIn("人工确认", why)
        ok, _ = s.allowed_in(_ctx(mode=C.MODE_APPLY, yes=True))
        self.assertTrue(ok)

    def test_crm_auto_with_ledger_needs_apply_only(self):
        # crm：auto_with_ledger 在 apply 下不需要 --yes（v1.9 原则）
        s = _step(side_effect=C.SIDE_ONLINE_WRITE, write_mode=C.WRITE_AUTO_WITH_LEDGER)
        ok, _ = s.allowed_in(_ctx(mode=C.MODE_APPLY))
        self.assertTrue(ok)
        ok2, _ = s.allowed_in(_ctx())  # preview 仍拦截
        self.assertFalse(ok2)

    def test_destructive_needs_yes_even_in_apply(self):
        s = _step(side_effect=C.SIDE_DESTRUCTIVE)
        ok, why = s.allowed_in(_ctx(mode=C.MODE_APPLY))
        self.assertFalse(ok)
        self.assertIn("--yes", why)
        ok, _ = s.allowed_in(_ctx(mode=C.MODE_APPLY, yes=True))
        self.assertTrue(ok)

    def test_route_policies_v19_principle(self):
        self.assertEqual(C.ROUTE_POLICIES["production"], C.WRITE_APPROVAL_REQUIRED)
        self.assertEqual(C.ROUTE_POLICIES["tasks"], C.WRITE_APPROVAL_REQUIRED)
        self.assertEqual(C.ROUTE_POLICIES["crm"], C.WRITE_AUTO_WITH_LEDGER)


class TestObjectIds(unittest.TestCase):
    def test_prefix_and_date(self):
        now = datetime.datetime(2026, 9, 12, 9, 0)
        self.assertEqual(C.new_object_id("project", 7, now), "PRJ-20260912-0007")
        self.assertTrue(C.new_object_id("customer", 0, now).startswith("CUS-20260912-"))

    def test_unknown_kind_raises(self):
        with self.assertRaises(ValueError):
            C.new_object_id("不存在的��型")

    def test_all_twelve_kinds(self):
        for kind in ("customer", "lead", "opportunity", "requirement", "solution",
                     "quote", "contract", "project", "production_order",
                     "purchase_order", "shipment", "after_sales"):
            self.assertTrue(C.new_object_id(kind))


class TestStateMachine(unittest.TestCase):
    def test_normal_chain(self):
        chain = ["lead", "opportunity", "requirement_confirming", "solution_confirming",
                 "quotation_confirming", "contract_pending", "won_and_funded",
                 "procurement", "in_production", "quality_check", "ready_to_ship",
                 "delivering", "acceptance", "closed"]
        for a, b in zip(chain, chain[1:]):
            self.assertTrue(C.can_transition(a, b), "%s→%s 应合法" % (a, b))
            self.assertEqual(C.transition_severity(a, b), "normal")

    def test_skip_and_rollback_visible_not_silent(self):
        # 跳步：opportunity → in_production
        self.assertTrue(C.can_transition("opportunity", "in_production"))
        self.assertEqual(C.transition_severity("opportunity", "in_production"), "skip")
        # 回退：in_production → procurement
        self.assertTrue(C.can_transition("in_production", "procurement"))
        self.assertEqual(C.transition_severity("in_production", "procurement"), "rollback")

    def test_illegal(self):
        self.assertFalse(C.can_transition("closed", "lead"))
        self.assertFalse(C.can_transition("cancelled", "procurement"))
        self.assertEqual(C.transition_severity("closed", "lead"), "illegal")

    def test_cancel_anytime_after_sales_paths(self):
        self.assertTrue(C.can_transition("acceptance", "after_sales"))
        self.assertTrue(C.can_transition("after_sales", "closed"))
        self.assertTrue(C.can_transition("acceptance", "closed"))


class TestResultShapes(unittest.TestCase):
    def test_step_result_helpers(self):
        r = C.StepResult(step_id="crm_dispatch")
        r.ok(counts={"created_leads": 2})
        self.assertEqual(r.status, C.STATUS_SUCCESS)
        r2 = C.StepResult(step_id="x").fail("boom")
        self.assertEqual(r2.status, C.STATUS_FAILED)
        self.assertEqual(r2.error, "boom")

    def test_run_result_summary(self):
        rr = C.RunResult(run_id="r", workflow="daily", steps=[
            {"status": "success"}, {"status": "success"}, {"status": "failed"},
            {"status": "skipped"}, {"status": "blocked"}])
        self.assertEqual(rr.summary, {"total": 5, "success": 2, "failed": 1,
                                      "skipped": 1, "blocked": 1})

    def test_approval_render(self):
        a = C.ApprovalRequest(approval_id="ap-1", object_type="项目",
                              object_id="PRJ-20260912-0001",
                              before={"合同交期": "2026-09-30"},
                              after={"合同交期": "2026-10-15"}, reason="客户电话要求延期")
        text = a.render()
        self.assertIn("2026-09-30", text)
        self.assertIn("2026-10-15", text)
        self.assertIn("客户电话要求延期", text)

    def test_run_id_shape(self):
        rid = C.new_run_id("daily", datetime.datetime(2026, 9, 12, 9, 0, 0))
        self.assertRegex(rid, r"^daily-20260912-0900-[0-9a-f]{4}$")


if __name__ == "__main__":
    unittest.main()
