# -*- coding: utf-8 -*-
"""test_trusted_execution.py — 可信执行层 v1 的离线验收测试（2026-09-26）。

**全部用内存假数据，不连任何真实业务表 / 微信库 / SeaTable。**
覆盖业主点名的四件事：

  1. 授权：高置信 ≠ 授权 —— 请求里写着「置信度=高」也照样写不进去；
     只有显式授权（授权文件 / 已通过的审批）才放行，且有表范围/有效期/次数；
  2. 去重：同一幂等键重复写 → skipped_reuse，不产生第二行；
  3. 恢复：--resume 跳过已成功的步骤，失败步骤重跑；
  4. 读回失败：云端 HTTP 200 但列静默丢失 → verify_failed →
     写进账本 → 发布门禁拒绝发布（端到端串联验证）。
"""
import json
import os
import sys
import tempfile
import unittest
from dataclasses import replace

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

from application import authorization as AUTH          # noqa: E402
from application import contracts as C                 # noqa: E402
from application import gates as GATES                 # noqa: E402
from application.dataservice import DataService, WriteRequest  # noqa: E402
from application.runner import WorkflowRunner          # noqa: E402
from workflows import daily_refresh, evening_full      # noqa: E402


class FakeAdapter:
    """纯内存适配器。

    ``drop_cols`` 模拟 SeaTable 那个真实坑：列名写错时接口 HTTP 200、
    返回 success，但列**静默不落库**（SKILL.md §12）。
    """

    def __init__(self, drop_cols=()):
        self.rows = {}
        self._n = 0
        self.drop_cols = set(drop_cols)
        self.calls = []

    def _rid(self):
        self._n += 1
        return "row-%d" % self._n

    def append_row(self, table, data):
        rid = self._rid()
        clean = {k: v for k, v in data.items() if k not in self.drop_cols}
        self.rows[rid] = dict(clean)
        self.rows[rid]["__row_id__"] = rid
        self.calls.append(("append", table, rid))
        return rid

    def update_row(self, table, rid, data):
        self.rows.setdefault(rid, {"__row_id__": rid})
        clean = {k: v for k, v in data.items() if k not in self.drop_cols}
        self.rows[rid].update(clean)
        self.calls.append(("update", table, rid))

    def list_rows(self, table):
        return [dict(r) for r in self.rows.values()]


def _ds(tmp, adapter):
    return DataService(adapter_factory=lambda _name: adapter, data_dir=tmp)


# ────────────────────────────────────────────────────────────────────
# 1. 授权：高置信 ≠ 授权
# ────────────────────────────────────────────────────────────────────
class TestAuthority(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="trusted_auth_")
        self.adapter = FakeAdapter()
        self.ds = _ds(self.tmp, self.adapter)

    def test_confidence_is_not_authorization(self):
        """核心断言：请求里写着「置信度=高」，没有授权照样写不进去。"""
        r = self.ds.write(
            WriteRequest(table="IC采购记录", row={"状态": "已到货", "置信度": "高"},
                         route="production", actor="automation"),
            mode=C.MODE_APPLY)
        self.assertEqual(r.status, "blocked")
        self.assertIn("授权", r.message)
        self.assertEqual(len(self.adapter.rows), 0)

    def test_grant_allows_write(self):
        self.adapter.rows["r1"] = {"__row_id__": "r1", "状态": "已下单"}
        grant = AUTH.manual_grant(tables=("IC采购记录",), actions=("update",), actor="老板")
        r = self.ds.write(
            WriteRequest(table="IC采购记录", row={"状态": "已到货"}, route="production",
                         action="update", row_id="r1", grant=grant),
            mode=C.MODE_APPLY)
        self.assertEqual(r.status, "written")
        self.assertTrue(r.verified)

    def test_grant_scope_enforced_by_table(self):
        grant = AUTH.manual_grant(tables=("组装料采购记录",), actions=("append",))
        r = self.ds.write(
            WriteRequest(table="IC采购记录", row={"状态": "已到货"},
                         route="production", grant=grant),
            mode=C.MODE_APPLY)
        self.assertEqual(r.status, "blocked")
        self.assertIn("不含表", r.message)
        self.assertEqual(len(self.adapter.rows), 0)

    def test_grant_expired(self):
        g = replace(AUTH.manual_grant(tables=("IC采购记录",)),
                    expires_at="2020-01-01T00:00:00")
        ok, why = g.covers("IC采购记录", "append")
        self.assertFalse(ok)
        self.assertIn("过期", why)

    def test_grant_max_uses(self):
        g = replace(AUTH.manual_grant(tables=("IC采购记录",), max_uses=1), used=1)
        ok, why = g.covers("IC采购记录", "append")
        self.assertFalse(ok)
        self.assertIn("用尽", why)

    def test_unapproved_request_cannot_become_grant(self):
        """未通过的审批不能转成授权 —— 必须报错，不能静默降级。"""
        ap = C.ApprovalRequest(approval_id="AP-1", object_type="项目", object_id="P1",
                               status="pending")
        with self.assertRaises(PermissionError):
            AUTH.grant_from_approval(ap)

    def test_approved_request_becomes_grant(self):
        ap = C.ApprovalRequest(approval_id="AP-2", object_type="项目", object_id="P1",
                               status="approved", approver="老板", reason="核对无误")
        g = AUTH.grant_from_approval(ap, tables=("IC采购记录",))
        self.assertEqual(g.source, AUTH.SOURCE_APPROVAL)
        self.assertEqual(g.actor, "老板")
        self.assertEqual(g.approval_id, "AP-2")
        ok, _ = g.covers("IC采购记录", "update")
        self.assertTrue(ok)

    def test_preview_leaves_no_trace(self):
        r = self.ds.write(
            WriteRequest(table="IC采购记录", row={"状态": "已到货"}, route="production"),
            mode=C.MODE_PREVIEW)
        self.assertEqual(r.status, "candidate")
        self.assertEqual(len(self.adapter.rows), 0)


# ────────────────────────────────────────────────────────────────────
# 2. 去重：幂等键
# ────────────────────────────────────────────────────────────────────
class TestDedup(unittest.TestCase):
    def test_same_idem_key_written_once(self):
        tmp = tempfile.mkdtemp(prefix="trusted_dedup_")
        adapter = FakeAdapter()
        ds = _ds(tmp, adapter)
        grant = AUTH.manual_grant(tables=("IC采购记录",), actions=("append",))

        def req():
            return WriteRequest(table="IC采购记录", row={"状态": "已到货"},
                                route="production", idem_key="wxmatch:WX-A-001:0",
                                grant=grant)

        r1 = ds.write(req(), mode=C.MODE_APPLY)
        r2 = ds.write(req(), mode=C.MODE_APPLY)
        self.assertEqual(r1.status, "written")
        self.assertEqual(r2.status, "skipped_reuse")
        self.assertEqual(len(adapter.rows), 1)      # 第二遍没写第二行
        self.assertEqual(len(adapter.calls), 1)

    def test_different_keys_both_written(self):
        tmp = tempfile.mkdtemp(prefix="trusted_dedup2_")
        adapter = FakeAdapter()
        ds = _ds(tmp, adapter)
        grant = AUTH.manual_grant(tables=("IC采购记录",))
        for k in ("k1", "k2"):
            ds.write(WriteRequest(table="IC采购记录", row={"状态": "已到货"},
                                  route="production", idem_key=k, grant=grant),
                     mode=C.MODE_APPLY)
        self.assertEqual(len(adapter.rows), 2)


# ────────────────────────────────────────────────────────────────────
# 3. 恢复：resume 跳过已成功步骤
# ────────────────────────────────────────────────────────────────────
class TestResume(unittest.TestCase):
    def _steps(self, calls, fail_ids=()):
        def mk(sid):
            def _run(ctx):
                calls.append(sid)
                res = C.StepResult(step_id=sid)
                return res.fail("boom") if sid in fail_ids else res.ok()
            return C.StepSpec(id=sid, name=sid, run=_run)
        return [mk("s1"), mk("s2"), mk("s3")]

    def test_resume_skips_succeeded_reruns_failed(self):
        tmp = tempfile.mkdtemp(prefix="trusted_resume_")
        runs = os.path.join(tmp, "runs")
        calls = []
        ctx1 = C.RunContext(run_id="run-a", workflow="t", skill_dir=tmp, mode=C.MODE_APPLY)
        rr1 = WorkflowRunner(ctx1, self._steps(calls, fail_ids={"s2"}), runs_dir=runs).run()
        self.assertEqual(rr1.status, C.STATUS_FAILED)
        self.assertEqual(calls, ["s1", "s2", "s3"])

        calls.clear()
        ctx2 = C.RunContext(run_id="run-b", workflow="t", skill_dir=tmp,
                            mode=C.MODE_APPLY, resume_of="run-a")
        rr2 = WorkflowRunner(ctx2, self._steps(calls), runs_dir=runs).run()
        # s1 上次成功 → 不再执行；s2/s3 重跑
        self.assertNotIn("s1", calls)
        self.assertIn("s2", calls)
        self.assertEqual(rr2.status, C.STATUS_SUCCESS)

    def test_abort_policy_blocks_downstream(self):
        tmp = tempfile.mkdtemp(prefix="trusted_abort_")
        calls = []

        def mk(sid, policy):
            def _run(ctx):
                calls.append(sid)
                res = C.StepResult(step_id=sid)
                return res.fail("first step died") if sid == "a" else res.ok()
            return C.StepSpec(id=sid, name=sid, run=_run,
                              depends_on=("a",) if sid == "b" else (),
                              failure_policy=policy)

        rr = WorkflowRunner(
            C.RunContext(run_id="run-c", workflow="t", skill_dir=tmp, mode=C.MODE_APPLY),
            [mk("a", "abort"), mk("b", "continue")],
            runs_dir=os.path.join(tmp, "runs")).run()
        by = {s["step_id"]: s for s in rr.steps}
        self.assertEqual(by["a"]["status"], C.STATUS_FAILED)
        self.assertEqual(by["b"]["status"], C.STATUS_BLOCKED)
        self.assertNotIn("b", calls)          # 依赖失败 → 根本没跑


# ────────────────────────────────────────────────────────────────────
# 4. 读回失败 → 账本 → 发布门禁（端到端）
# ────────────────────────────────────────────────────────────────────
class TestReadbackAndPublishGate(unittest.TestCase):
    def _ok_steps(self):
        return [{"step_id": s, "status": C.STATUS_SUCCESS, "error": None, "counts": {}}
                for s in GATES.CRITICAL_STEP_IDS]

    def test_ok_steps_allow_publish(self):
        g = GATES.evaluate_dict({"run_id": "evening-1", "steps": self._ok_steps()})
        self.assertTrue(g.allowed, g.reasons)

    def test_skipped_is_not_failure(self):
        steps = self._ok_steps()
        steps.append({"step_id": "publish", "status": C.STATUS_SKIPPED,
                      "error": None, "counts": {}})
        g = GATES.evaluate_dict({"run_id": "evening-1", "steps": steps})
        self.assertTrue(g.allowed, g.reasons)

    def test_missing_critical_blocks(self):
        steps = [s for s in self._ok_steps() if s["step_id"] != "wxmedia_ocr"]
        g = GATES.evaluate_dict({"run_id": "evening-1", "steps": steps})
        self.assertFalse(g.allowed)
        self.assertTrue(any("wxmedia_ocr" in r for r in g.reasons))

    def test_any_failed_step_blocks(self):
        steps = self._ok_steps()
        steps.append({"step_id": "partdb_snap", "status": C.STATUS_FAILED,
                      "error": "502 Bad Gateway", "counts": {}})
        g = GATES.evaluate_dict({"run_id": "evening-1", "steps": steps})
        self.assertFalse(g.allowed)
        self.assertTrue(any("partdb_snap" in r for r in g.reasons))

    def test_verify_failed_blocks(self):
        steps = self._ok_steps()
        for s in steps:
            if s["step_id"] == "evening_write":
                s["counts"] = {"verify_failed": 2}
        g = GATES.evaluate_dict({"run_id": "evening-1", "steps": steps})
        self.assertFalse(g.allowed)
        self.assertTrue(any("读回验证失败" in r for r in g.reasons))

    def test_readback_failure_end_to_end(self):
        """端到端：静默丢列 → verify_failed → 门禁拒绝发布。"""
        tmp = tempfile.mkdtemp(prefix="trusted_e2e_")
        adapter = FakeAdapter(drop_cols={"状态"})       # 模拟列没落库
        ds = _ds(tmp, adapter)
        grant = AUTH.manual_grant(tables=("IC采购记录",), actions=("append",))
        r = ds.write(WriteRequest(table="IC采购记录", row={"状态": "已到货"},
                                  route="production", idem_key="e2e-1", grant=grant),
                     mode=C.MODE_APPLY)
        self.assertEqual(r.status, "verify_failed")
        self.assertFalse(r.verified)
        self.assertIn("读回验证失败", r.message)

        steps = self._ok_steps()
        for s in steps:
            if s["step_id"] == "evening_write":
                s["status"] = C.STATUS_FAILED
                s["error"] = "1 条读回验证失败"
                s["counts"] = {"verify_failed": 1}
        g = GATES.evaluate_dict({"run_id": "e2e", "steps": steps})
        self.assertFalse(g.allowed)
        self.assertIn("发布门禁", g.render())

    def test_missing_ledger_blocks_publish(self):
        """没有账本就发布 —— 直接拒绝（这是最该拦住的场景）。"""
        g = GATES.evaluate("no-such-run", runs_dir=tempfile.mkdtemp(prefix="trusted_noledger_"))
        self.assertFalse(g.allowed)


# ────────────────────────────────────────────────────────────────────
# 5. 工作流结构：09:00 轻同步 / 19:00 全量
# ────────────────────────────────────────────────────────────────────
class TestWorkflows(unittest.TestCase):
    def test_both_registered(self):
        self.assertIn("daily", daily_refresh.WORKFLOWS)
        self.assertIn("evening", evening_full.WORKFLOWS)

    def test_evening_order_matches_spec(self):
        """顺序必须是：采集→OCR→核对→授权写入→快照→风险→生成发布。"""
        ids = [s.id for s in evening_full.build_steps()]
        self.assertEqual(ids, [
            "wechat_collect", "wxmedia_ocr", "wxmatch_scan", "evening_write",
            "seatable_snap", "partdb_snap", "alerts", "foresee", "foresee_review",
            "cockpit", "publish"])
        self.assertLess(ids.index("wechat_collect"), ids.index("wxmedia_ocr"))
        self.assertLess(ids.index("wxmedia_ocr"), ids.index("wxmatch_scan"))
        self.assertLess(ids.index("wxmatch_scan"), ids.index("evening_write"))
        self.assertLess(ids.index("evening_write"), ids.index("seatable_snap"))
        self.assertLess(ids.index("foresee"), ids.index("cockpit"))
        self.assertLess(ids.index("cockpit"), ids.index("publish"))

    def test_evening_has_single_online_write_step(self):
        ows = [s.id for s in evening_full.build_steps()
               if s.side_effect == C.SIDE_ONLINE_WRITE]
        self.assertEqual(ows, ["evening_write"])

    def test_publish_depends_on_write_and_cockpit(self):
        by = {s.id: s for s in evening_full.build_steps()}
        self.assertEqual(by["publish"].side_effect, C.SIDE_PUBLISH)
        self.assertIn("evening_write", by["publish"].depends_on)
        self.assertIn("cockpit", by["publish"].depends_on)

    def test_preview_blocks_write_and_publish(self):
        ctx = C.RunContext(run_id="x", workflow="evening", mode=C.MODE_PREVIEW)
        by = {s.id: s for s in evening_full.build_steps()}
        ok, _ = by["evening_write"].allowed_in(ctx)
        self.assertFalse(ok)
        ok, _ = by["publish"].allowed_in(ctx)
        self.assertFalse(ok)

    def test_daily_still_light_sync(self):
        """09:00 保持轻同步：不 OCR、不写业务表、不发布。"""
        ids = [s.id for s in daily_refresh.build_steps()]
        self.assertNotIn("wxmedia_ocr", ids)
        self.assertNotIn("evening_write", ids)
        self.assertNotIn("publish", ids)
        self.assertIn("seatable_sync", ids)
        self.assertIn("cockpit", ids)


if __name__ == "__main__":
    unittest.main(verbosity=2)
