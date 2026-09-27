# -*- coding: utf-8 -*-
"""test_write_safety.py — 三期审计所证缺陷的回归测试（2026-09-27）。

每条测试都对应一个**在临时目录里实测复现过**的问题；不再依赖任何真实
配置、真实 SeaTable、真实微信库，全部为内存/临时文件假数据。

覆盖：
  · 授权：一次性授权不可复用；使用次数持久化在跨实例之间；撤销即失效；
          空 JSON 不再变成「无限授权」；授权绑定路由/目标行/载荷摘要。
  · 写入：写后超时（结果未知）不盲目重发；读回失败不冒充「幂等复用」；
          0/False 不再与空值混淆；非法动作不落成 append。
  · 门禁：账本格式不合规（未知状态/重复 step_id）一律拒绝；发布必须
          与产物哈希绑定；发布时刻账本已可用（步骤文件路径）。
  · 执行器：续跑上下文不一致不复用；expect_artifacts 缺失判失败；
          非阻断步骤失败不连坐下游与发布。
  · 工作流：引用的脚本真实存在（重组后不再跑挂）；evening_write 以结构化
          摘要判退出码；wxmatch 部分失败不再被标成「已授权写入」。
"""
import csv
import json
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

from application import authorization as AUTH          # noqa: E402
from application import contracts as C                 # noqa: E402
from application import gates as GATES                 # noqa: E402
from application.dataservice import DataService, WriteRequest  # noqa: E402
from application.runner import WorkflowRunner          # noqa: E402


class FakeAdapter:
    """内存适配器：可模拟「列静默丢失」「响应丢失但已落库」。"""

    def __init__(self, drop_cols=(), lose_response=False, root="mem"):
        self.rows = {}
        self._n = 0
        self.drop_cols = set(drop_cols)
        self.lose_response = lose_response
        self.calls = []
        self.root = root          # 用于 target 身份，模拟不同后端

    def _rid(self):
        self._n += 1
        return "row-%d" % self._n

    def append_row(self, table, data):
        rid = self._rid()
        self.rows[rid] = {k: v for k, v in data.items() if k not in self.drop_cols}
        self.rows[rid]["__row_id__"] = rid
        self.calls.append(("append", table, rid))
        if self.lose_response:
            # 真实场景：请求已到达服务端并落库，但响应在网络上丢了
            self.lose_response = False
            raise TimeoutError("connection reset after server committed")
        return rid

    def update_row(self, table, rid, data):
        self.rows.setdefault(rid, {"__row_id__": rid})
        self.rows[rid].update({k: v for k, v in data.items() if k not in self.drop_cols})
        self.calls.append(("update", table, rid))

    def list_rows(self, table):
        if self.lose_response is None:      # 不需要
            pass
        return [dict(r) for r in self.rows.values()]


class TempCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="wsafety_")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def ds(self, adapter, data_dir=None):
        return DataService(adapter_factory=lambda _n: adapter,
                           data_dir=data_dir or self.tmp)

    def ledger(self, data_dir=None):
        path = os.path.join(data_dir or self.tmp, "write_ledger.csv")
        if not os.path.exists(path):
            return []
        with open(path, encoding="utf-8-sig", newline="") as f:
            return list(csv.DictReader(f))


# ────────────────────────────────────────────────────────────────────
# 1. 授权：次数、撤销、空文件
# ────────────────────────────────────────────────────────────────────
class TestGrantConsumption(TempCase):
    def test_one_use_grant_cannot_be_reused(self):
        """A01：一次性授权写第二次必须被拦（旧实现每次都 save(used=1)，永远用得完）。"""
        adapter = FakeAdapter()
        ds = self.ds(adapter)
        grant = AUTH.manual_grant(tables=("IC采购记录",), actions=("append",),
                                  max_uses=1, actor="老板")
        r1 = ds.write(WriteRequest(table="IC采购记录", row={"状态": "已到货"},
                                   route="production", idem_key="k1", grant=grant),
                      mode=C.MODE_APPLY)
        r2 = ds.write(WriteRequest(table="IC采购记录", row={"状态": "已到货"},
                                   route="production", idem_key="k2", grant=grant),
                      mode=C.MODE_APPLY)
        self.assertEqual(r1.status, "written")
        self.assertEqual(r2.status, "blocked")
        self.assertIn("用尽", r2.message)
        self.assertEqual(len(adapter.rows), 1)      # 第二次真的没写

    def test_usage_persists_across_instances(self):
        """次数是持久状态：另一个 DataService 实例也必须看见已用次数。"""
        adapter = FakeAdapter()
        grant = AUTH.manual_grant(tables=("IC采购记录",), actions=("append",),
                                  max_uses=1, actor="老板")
        r1 = self.ds(adapter).write(
            WriteRequest(table="IC采购记录", row={"状态": "已到货"}, route="production",
                         idem_key="p1", grant=grant), mode=C.MODE_APPLY)
        r2 = self.ds(adapter).write(
            WriteRequest(table="IC采购记录", row={"状态": "已到货"}, route="production",
                         idem_key="p2", grant=grant), mode=C.MODE_APPLY)
        self.assertEqual(r1.status, "written")
        self.assertEqual(r2.status, "blocked")

    def test_revoked_grant_file_blocks(self):
        """撤销授权＝删文件：已构造的授权对象不能再用来写。"""
        adapter = FakeAdapter()
        store = AUTH.GrantStore(os.path.join(self.tmp, AUTH.GRANTS_DIR_NAME))
        grant = AUTH.manual_grant(tables=("IC采购记录",), actions=("append",),
                                  max_uses=5, actor="老板")
        path = store.save(grant)
        loaded = AUTH.GrantStore.load_file(path)
        self.assertIsNotNone(loaded)
        os.unlink(path)
        r = self.ds(adapter).write(
            WriteRequest(table="IC采购记录", row={"状态": "已到货"}, route="production",
                         idem_key="rv1", grant=loaded), mode=C.MODE_APPLY)
        self.assertEqual(r.status, "blocked")
        self.assertEqual(len(adapter.rows), 0)

    def test_empty_json_is_not_an_unlimited_grant(self):
        """A07：{} 曾经被 permissive 默认值填成「不限表、不限次、不过期」的通行证。"""
        self.assertIsNone(AUTH.GrantStore.load_file(self._write_json({})))
        bad = self._write_json({"tables": ["生产计划"], "actions": ["append"],
                                "max_uses": "五"})
        self.assertIsNone(AUTH.GrantStore.load_file(bad))

    def test_grant_is_bound_to_route_and_row(self):
        """授权限定路由/目标行后，不能被拿去写别的 Base、别的行。"""
        adapter = FakeAdapter()
        ds = self.ds(adapter)
        grant = AUTH.manual_grant(tables=("IC采购记录",), actions=("update",),
                                  routes=("production",), max_uses=5)
        wrong_route = AUTH.WriteGrant(
            grant_id=grant.grant_id, tables=grant.tables, actions=grant.actions,
            routes=("tasks",), max_uses=5, issued_at=grant.issued_at)
        r = ds.write(WriteRequest(table="IC采购记录", row={"状态": "已到货"},
                                  route="production", action="update", row_id="r9",
                                  idem_key="rt1", grant=wrong_route), mode=C.MODE_APPLY)
        self.assertEqual(r.status, "blocked")
        self.assertIn("路由", r.message)

    def _write_json(self, payload):
        path = os.path.join(self.tmp, "grant-%d.json" % len(os.listdir(self.tmp)))
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        return path


# ────────────────────────────────────────────────────────────────────
# 2. 写入：不确定结果、读回失败、0/False、非法动作
# ────────────────────────────────────────────────────────────────────
class TestWriteOutcomes(TempCase):
    def test_commit_then_timeout_never_duplicates(self):
        """A02：服务端已落库但响应丢失 → outcome_unknown，重试不得新增第二行。"""
        adapter = FakeAdapter(lose_response=True)
        ds = self.ds(adapter)
        r1 = ds.write(WriteRequest(table="销售线索表", row={"客户名称": "客户U"},
                                   route="crm", idem_key="to1"), mode=C.MODE_APPLY)
        self.assertEqual(r1.status, "outcome_unknown")
        self.assertEqual(len(adapter.rows), 1)
        r2 = ds.write(WriteRequest(table="销售线索表", row={"客户名称": "客户U"},
                                   route="crm", idem_key="to1"), mode=C.MODE_APPLY)
        # 第二次：仍找不到 row_id，只能承认未知，绝不能盲目重发
        self.assertIn(r2.status, ("outcome_unknown", "verify_failed"))
        self.assertEqual(len(adapter.rows), 1)      # 关键：没有第二行
        self.assertEqual(len(adapter.calls), 1)

    def test_readback_failure_is_not_success_on_retry(self):
        """A03：读回失败后重跑，不得被当成「幂等复用＝成功」。"""
        adapter = FakeAdapter(drop_cols={"状态"})
        ds = self.ds(adapter)
        r1 = ds.write(WriteRequest(table="IC采购记录", row={"状态": "已到货"},
                                   route="crm", idem_key="rb1"), mode=C.MODE_APPLY)
        self.assertEqual(r1.status, "verify_failed")
        r2 = ds.write(WriteRequest(table="IC采购记录", row={"状态": "已到货"},
                                   route="crm", idem_key="rb1"), mode=C.MODE_APPLY)
        self.assertEqual(r2.status, "verify_failed")
        self.assertFalse(r2.ok)                     # ok 绝不为 True
        self.assertEqual(len(adapter.rows), 1)

    def test_zero_is_not_conflated_with_empty(self):
        """A11：want=0 / got=None 必须判失败；want=0 / got=0 必须判通过。"""
        adapter = FakeAdapter()
        ds = self.ds(adapter)
        r = ds.write(WriteRequest(table="生产计划", row={"数量": 0}, route="crm",
                                  idem_key="z1"), mode=C.MODE_APPLY)
        self.assertEqual(r.status, "written")       # 计数器真实为 0，写进去了

        dropped = FakeAdapter(drop_cols={"数量"})
        r2 = self.ds(dropped).write(
            WriteRequest(table="生产计划", row={"数量": 0}, route="crm", idem_key="z2"),
            mode=C.MODE_APPLY)
        self.assertEqual(r2.status, "verify_failed")   # 0 被静默丢掉，必须抓住

        bools = FakeAdapter()
        r3 = self.ds(bools).write(
            WriteRequest(table="生产计划", row={"已确认": False}, route="crm",
                         idem_key="z3"), mode=C.MODE_APPLY)
        self.assertEqual(r3.status, "written")

    def test_invalid_action_is_blocked(self):
        """A12：非法动作不许落成 append（旧实现会按 append 写下去）。"""
        adapter = FakeAdapter()
        r = self.ds(adapter).write(
            WriteRequest(table="生产计划", row={"生产产品": "4G小卡"},
                         route="production", action="delete",
                         grant=AUTH.manual_grant(tables=("生产计划",), max_uses=0)),
            mode=C.MODE_APPLY)
        self.assertEqual(r.status, "blocked")
        self.assertEqual(len(adapter.rows), 0)
        self.assertEqual(adapter.calls, [])

    def test_preview_and_bad_payload_leave_no_trace(self):
        adapter = FakeAdapter()
        ds = self.ds(adapter)
        self.assertEqual(ds.write(WriteRequest(table="生产计划", row={"a": 1}),
                                  mode=C.MODE_PREVIEW).status, "candidate")
        self.assertEqual(ds.write(WriteRequest(table="生产计划", row={},
                                               route="crm"), mode=C.MODE_APPLY).status,
                         "blocked")
        self.assertEqual(ds.write(WriteRequest(table="生产计划", row={"a": 1},
                                               route="crm", verify_fields=("不存在",)),
                                  mode=C.MODE_APPLY).status, "blocked")
        self.assertEqual(adapter.calls, [])

    def test_legacy_ledger_entry_reverifies_instead_of_rewriting(self):
        """旧版台账（无操作表）里的幂等键：只核验 row_id，不重写。"""
        adapter = FakeAdapter()
        adapter.rows["row-1"] = {"__row_id__": "row-1", "客户名称": "客户U"}
        path = os.path.join(self.tmp, "write_ledger.csv")
        with open(path, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f)
            w.writerow(["时间", "路由", "动作", "表", "row_id", "幂等键",
                        "读回验证", "内容摘要", "执行者", "备注"])
            w.writerow(["2026-09-01 10:00:00", "crm", "append", "销售线索表",
                        "row-1", "legacy1", "通过", "客户名称=客户U", "automation", ""])
        r = self.ds(adapter).write(
            WriteRequest(table="销售线索表", row={"客户名称": "客户U"}, route="crm",
                         idem_key="legacy1"), mode=C.MODE_APPLY)
        self.assertEqual(r.status, "skipped_reuse")
        self.assertTrue(r.verified)
        self.assertEqual(len(adapter.rows), 1)
        self.assertEqual(adapter.calls, [])


# ────────────────────────────────────────────────────────────────────
# 3. 发布门禁：格式、绑定、发布时刻账本可用
# ────────────────────────────────────────────────────────────────────
class TestPublishGate(TempCase):
    CRIT = GATES.CRITICAL_STEP_IDS

    def _steps(self, **over):
        steps = [{"step_id": s, "status": C.STATUS_SUCCESS, "error": None, "counts": {},
                  "blocking": True} for s in self.CRIT]
        for k, v in over.items():
            steps.append({"step_id": k, "status": v[0], "error": v[1] if len(v) > 1 else None,
                          "counts": v[2] if len(v) > 2 else {}, "blocking": True})
        return steps

    def test_unknown_status_blocks(self):
        """A10：状态不在已知终态内 → 账本不可信 → 拒绝发布（旧实现直接放行）。"""
        steps = self._steps()
        steps.append({"step_id": "cockpit", "status": "DONE", "counts": {}})
        g = GATES.evaluate_dict({"run_id": "r1", "steps": steps})
        self.assertFalse(g.allowed)
        self.assertTrue(any("不在已知终态" in r for r in g.reasons))

    def test_missing_or_duplicate_step_id_blocks(self):
        steps = self._steps()
        steps.append({"status": C.STATUS_SUCCESS, "counts": {}})
        self.assertFalse(GATES.evaluate_dict({"run_id": "r1", "steps": steps}).allowed)
        steps = self._steps()
        steps.append({"step_id": "cockpit", "status": C.STATUS_SUCCESS, "counts": {}})
        steps.append({"step_id": "cockpit", "status": C.STATUS_SUCCESS, "counts": {}})
        self.assertFalse(GATES.evaluate_dict({"run_id": "r1", "steps": steps}).allowed)

    def test_bad_verify_failed_counter_blocks(self):
        steps = self._steps()
        steps[-1]["counts"] = {"verify_failed": -1}
        g = GATES.evaluate_dict({"run_id": "r1", "steps": steps})
        self.assertFalse(g.allowed)

    def test_failed_final_status_blocks(self):
        g = GATES.evaluate_dict({"run_id": "r1", "status": C.STATUS_FAILED,
                                 "steps": self._steps()})
        self.assertFalse(g.allowed)

    def test_gate_works_from_step_files_while_run_in_progress(self):
        """A05：发布步骤执行时 final.json 还没写，门禁必须能凭步骤文件判定。"""
        run_dir = os.path.join(self.tmp, "runs", "evening-1")
        os.makedirs(run_dir)
        for i, sid in enumerate(self.CRIT, 1):
            with open(os.path.join(run_dir, "%02d_%s.json" % (i, sid)), "w",
                      encoding="utf-8") as f:
                json.dump({"step_id": sid, "status": C.STATUS_SUCCESS, "counts": {},
                           "blocking": True}, f, ensure_ascii=False)
        g = GATES.evaluate("evening-1", os.path.join(self.tmp, "runs"))
        self.assertTrue(g.allowed, g.reasons)
        # final.json 尚不存在 —— 正是发布步骤运行时的真实状态

    def test_non_blocking_failure_warns_but_allows(self):
        """A08：显式声明非阻断的失败（库存源 502）不连坐发布，但必须告警。"""
        steps = self._steps(partdb_snap=(C.STATUS_FAILED, "502 Bad Gateway"))
        steps[-1]["blocking"] = False
        g = GATES.evaluate_dict({"run_id": "r1", "steps": steps})
        self.assertTrue(g.allowed, g.reasons)
        self.assertTrue(any("partdb_snap" in w for w in g.warnings))

    def test_artifact_must_be_bound_and_unmodified(self):
        """A06：上传的文件必须是本次运行产出的那一份，改过就拒。"""
        run_dir = os.path.join(self.tmp, "runs", "evening-2")
        os.makedirs(run_dir)
        html = os.path.join(self.tmp, "项目管理驾驶舱.html")
        with open(html, "w", encoding="utf-8") as f:
            f.write("<html>v1</html>")
        steps = [{"step_id": s, "status": C.STATUS_SUCCESS, "counts": {}}
                 for s in self.CRIT]
        steps.append({"step_id": "cockpit", "status": C.STATUS_SUCCESS, "counts": {},
                      "artifacts": [html],
                      "artifact_hashes": {os.path.abspath(html): GATES.sha256_file(html)}})
        for i, s in enumerate(steps, 1):
            with open(os.path.join(run_dir, "%02d_%s.json" % (i, s["step_id"])), "w",
                      encoding="utf-8") as f:
                json.dump(s, f, ensure_ascii=False)
        runs = os.path.join(self.tmp, "runs")
        self.assertTrue(GATES.evaluate("evening-2", runs, artifact=html).allowed)
        with open(html, "w", encoding="utf-8") as f:
            f.write("<html>篡改过的</html>")
        g = GATES.evaluate("evening-2", runs, artifact=html)
        self.assertFalse(g.allowed)
        self.assertTrue(any("不一致" in r for r in g.reasons))

    def test_unbound_artifact_is_refused(self):
        run_dir = os.path.join(self.tmp, "runs", "evening-3")
        os.makedirs(run_dir)
        html = os.path.join(self.tmp, "other.html")
        with open(html, "w", encoding="utf-8") as f:
            f.write("<html>x</html>")
        for i, s in enumerate(self.CRIT, 1):
            with open(os.path.join(run_dir, "%02d_%s.json" % (i, s)), "w",
                      encoding="utf-8") as f:
                json.dump({"step_id": s, "status": C.STATUS_SUCCESS, "counts": {}}, f)
        g = GATES.evaluate("evening-3", os.path.join(self.tmp, "runs"), artifact=html)
        self.assertFalse(g.allowed)
        self.assertTrue(any("没有记录产物" in r for r in g.reasons))

    def test_corrupt_ledger_blocks(self):
        run_dir = os.path.join(self.tmp, "runs", "evening-4")
        os.makedirs(run_dir)
        with open(os.path.join(run_dir, "01_wechat_collect.json"), "w",
                  encoding="utf-8") as f:
            f.write("{ not json")
        g = GATES.evaluate("evening-4", os.path.join(self.tmp, "runs"))
        self.assertFalse(g.allowed)


# ────────────────────────────────────────────────────────────────────
# 4. 执行器：续跑身份、声明产物、非阻断依赖
# ────────────────────────────────────────────────────────────────────
class TestRunnerGuards(TempCase):
    def _ctx(self, run_id, **kw):
        return C.RunContext(run_id=run_id, workflow="t", skill_dir=self.tmp,
                            mode=C.MODE_APPLY, **kw)

    def _steps(self, calls, artifact=None):
        def mk(sid, **kw):
            def _run(ctx):
                calls.append(sid)
                res = C.StepResult(step_id=sid)
                if artifact:
                    with open(os.path.join(self.tmp, artifact), "w",
                              encoding="utf-8") as f:
                        f.write("payload-%s" % sid)
                return res.ok()
            return C.StepSpec(id=sid, name=sid, run=_run, **kw)
        return [mk("s1"), mk("s2", depends_on=("s1",))]

    def test_resume_refuses_different_context(self):
        """A09：换了模式/工作流续跑，不许沿用上一次的「成功」。"""
        runs = os.path.join(self.tmp, "runs")
        calls = []
        WorkflowRunner(self._ctx("a"), self._steps(calls), runs_dir=runs).run()
        calls.clear()
        other = C.RunContext(run_id="b", workflow="t", skill_dir=self.tmp,
                             mode=C.MODE_PREVIEW, resume_of="a")
        rr = WorkflowRunner(other, self._steps(calls), runs_dir=runs).run()
        self.assertEqual(calls, ["s1", "s2"])       # 全部重跑，一步都没被复用
        self.assertNotEqual(rr.status, None)

    def test_resume_reuses_when_context_matches(self):
        runs = os.path.join(self.tmp, "runs")
        calls = []
        WorkflowRunner(self._ctx("a"), self._steps(calls), runs_dir=runs).run()
        calls.clear()
        rr = WorkflowRunner(self._ctx("b", resume_of="a"), self._steps(calls),
                            runs_dir=runs).run()
        self.assertEqual(calls, [])                 # 两步都复用
        self.assertEqual(rr.status, C.STATUS_SUCCESS)

    def test_missing_declared_artifact_fails_step(self):
        """声明了产物却没生成 → 判失败（否则门禁会为不存在的文件放行）。"""
        runs = os.path.join(self.tmp, "runs")
        calls = []

        def _run(ctx):
            calls.append("cockpit")
            return C.StepResult(step_id="cockpit").ok()

        spec = C.StepSpec(id="cockpit", name="cockpit", run=_run,
                          side_effect=C.SIDE_LOCAL_APPEND,
                          expect_artifacts=("项目管理驾驶舱.html",))
        rr = WorkflowRunner(self._ctx("c"), [spec], runs_dir=runs).run()
        self.assertEqual(rr.steps[0]["status"], C.STATUS_FAILED)
        self.assertIn("声明产物未生成", rr.steps[0]["error"] or "")

    def test_present_artifact_is_hashed_into_ledger(self):
        runs = os.path.join(self.tmp, "runs")
        calls = []
        steps = self._steps(calls, artifact="项目管理驾驶舱.html")
        steps[0].expect_artifacts = ("项目管理驾驶舱.html",)
        rr = WorkflowRunner(self._ctx("d"), steps, runs_dir=runs).run()
        hashes = GATES.artifact_hashes(rr.steps)
        self.assertIn(os.path.abspath(os.path.join(self.tmp, "项目管理驾驶舱.html")),
                      hashes)

    def test_non_blocking_dependency_failure_does_not_block_downstream(self):
        """A08：blocking=False 的依赖失败，下游照跑（这才是真降级）。"""
        runs = os.path.join(self.tmp, "runs")

        def fail(ctx):
            return C.StepResult(step_id="snap").fail("502")

        def ok(ctx):
            return C.StepResult(step_id="after").ok()

        rr = WorkflowRunner(self._ctx("e"), [
            C.StepSpec(id="snap", name="snap", run=fail, blocking=False),
            C.StepSpec(id="after", name="after", run=ok, depends_on=("snap",)),
        ], runs_dir=runs).run()
        by = {s["step_id"]: s for s in rr.steps}
        self.assertEqual(by["snap"]["status"], C.STATUS_FAILED)
        self.assertEqual(by["after"]["status"], C.STATUS_SUCCESS)   # 没被连坐

    def test_blocking_dependency_failure_blocks_downstream(self):
        runs = os.path.join(self.tmp, "runs")

        def fail(ctx):
            return C.StepResult(step_id="snap").fail("boom")

        def ok(ctx):
            return C.StepResult(step_id="after").ok()

        rr = WorkflowRunner(self._ctx("f"), [
            C.StepSpec(id="snap", name="snap", run=fail),
            C.StepSpec(id="after", name="after", run=ok, depends_on=("snap",)),
        ], runs_dir=runs).run()
        by = {s["step_id"]: s for s in rr.steps}
        self.assertEqual(by["after"]["status"], C.STATUS_BLOCKED)


# ────────────────────────────────────────────────────────────────────
# 5. 工作流路径、evening_write 退出码、wxmatch 部分失败
# ────────────────────────────────────────────────────────────────────
class TestWorkflowWiring(unittest.TestCase):
    def test_referenced_scripts_exist(self):
        """A04：重建后所有引用的脚本必须真实存在（旧写法会让每一步都跑挂）。"""
        from workflows import daily_refresh, evening_full
        for mod in (daily_refresh, evening_full):
            for key, rel in mod.SCRIPTS.items():
                path = os.path.join(HERE, rel)
                self.assertTrue(os.path.isfile(path),
                                "%s 引用的 %s（%s）不存在" % (mod.__name__, rel, key))
            mod.build_steps()   # 构建期即校验，不抛异常

    def test_evening_write_exit_code_follows_structured_summary(self):
        """evening_write 只看结构化摘要：没有台账新增也能判出失败。"""
        import workflows.evening_write as ew
        from unittest.mock import patch

        tmp = tempfile.mkdtemp(prefix="ew_")
        self.addCleanup(shutil.rmtree, tmp, True)
        orig_store = AUTH.GrantStore
        grant = AUTH.manual_grant(tables=("IC采购记录",), actions=("append",),
                                  max_uses=1, actor="老板")
        orig_store(os.path.join(tmp, AUTH.GRANTS_DIR_NAME)).save(grant)
        cases = [
            ({"wrote_nothing": False, "ok": 1, "failed": 0, "blocked": 0, "unknown": 0,
              "results": {}}, 0),
            ({"wrote_nothing": False, "ok": 0, "failed": 1, "blocked": 1, "unknown": 0,
              "results": {"WX-M-1": [{"ok": False, "stage": "blocked", "msg": "缺授权"}]}}, 1),
            ({"wrote_nothing": False, "ok": 1, "failed": 1, "blocked": 0, "unknown": 1,
              "results": {"WX-M-2": [{"ok": False, "stage": "unknown", "msg": "响应丢失"}]}}, 1),
            ({"wrote_nothing": True, "reason": "no_todo", "failed": 0, "blocked": 0,
              "unknown": 0, "results": {}}, 0),
        ]
        with patch.object(ew, "HERE", tmp), \
                patch.object(AUTH, "GrantStore",
                             lambda *a, **k: orig_store(
                                 os.path.join(tmp, AUTH.GRANTS_DIR_NAME))):
            for summary, want in cases:
                with patch("wx.wxmatch.cmd_apply", return_value=summary):
                    self.assertEqual(ew.main(), want, summary)

    def test_evening_write_skips_without_grant(self):
        import workflows.evening_write as ew
        from unittest.mock import patch
        tmp = tempfile.mkdtemp(prefix="ew_nogrant_")
        self.addCleanup(shutil.rmtree, tmp, True)
        orig_store = AUTH.GrantStore
        with patch.object(ew, "HERE", tmp), \
                patch.object(AUTH, "GrantStore",
                             lambda *a, **k: orig_store(
                                 os.path.join(tmp, AUTH.GRANTS_DIR_NAME))):
            self.assertEqual(ew.main(), 3)      # 没有授权 → skipped


class TestWxmatchPartialBatch(TempCase):
    """部分失败不能被标成「已授权写入」（旧实现用子串 'written' 判定）。"""

    def _prep(self):
        from wx import wxmatch as wm
        tmp = self.tmp
        match = os.path.join(tmp, "核对结果.csv")
        rows = [{
            "核对编号": "WX-M-20260927-001", "日期": "2026-09-27", "类型": "到货",
            "信号来源": "g", "信号内容": "已到货", "匹配结果": "IC采购记录/某料",
            "匹配项目": "", "建议动作": "", "预填意图": json.dumps([
                {"op": "update", "table": "IC采购记录", "row_id": "", "data": {}},
                {"op": "append", "table": "IC采购记录", "data": {"状态": "已到货"}},
            ], ensure_ascii=False), "置信度": "高", "状态": "待确认",
        }]
        with open(match, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.DictWriter(f, fieldnames=wm.MATCH_COLS)
            w.writeheader()
            for r in rows:
                w.writerow({c: r.get(c, "") for c in wm.MATCH_COLS})
        return wm, match

    def test_partial_failure_keeps_row_pending(self):
        from unittest.mock import patch
        wm, match = self._prep()
        old_match, old_data = wm.MATCH_PATH, wm.DATA
        adapter = FakeAdapter()
        self.addCleanup(setattr, wm, "MATCH_PATH", old_match)
        self.addCleanup(setattr, wm, "DATA", old_data)
        wm.MATCH_PATH, wm.DATA = match, self.tmp
        grant = AUTH.manual_grant(tables=("IC采购记录",), actions=("append", "update"),
                                  max_uses=0, actor="老板")
        grant_file = os.path.join(self.tmp, "g.json")
        with open(grant_file, "w", encoding="utf-8") as f:
            json.dump(grant.to_dict(), f, ensure_ascii=False)
        with patch.object(wm, "_get_business_adapter", return_value=(adapter, "fake")):
            summary = wm.cmd_apply(grant_file=grant_file)
        rows = {r["核对编号"]: r for r in wm._read_csv(match)}
        row = rows["WX-M-20260927-001"]
        # 第 1 条意图缺 row_id（结构性问题）、第 2 条成功 → 整条必须保持待确认
        self.assertNotEqual(row["状态"], "已授权写入")
        self.assertEqual(row["状态"], "待确认")
        self.assertEqual(summary["ok"], 0)
        self.assertGreaterEqual(summary["failed"], 1)

    def test_all_intents_ok_marks_written(self):
        from unittest.mock import patch
        wm, match = self._prep()
        old_match, old_data = wm.MATCH_PATH, wm.DATA
        adapter = FakeAdapter()
        self.addCleanup(setattr, wm, "MATCH_PATH", old_match)
        self.addCleanup(setattr, wm, "DATA", old_data)
        wm.MATCH_PATH, wm.DATA = match, self.tmp
        rows = wm._read_csv(match)
        rows[0]["预填意图"] = json.dumps([
            {"op": "append", "table": "IC采购记录", "data": {"状态": "已到货"}}],
            ensure_ascii=False)
        wm._write_csv(match, wm.MATCH_COLS, rows)
        grant = AUTH.manual_grant(tables=("IC采购记录",), actions=("append",),
                                  max_uses=0, actor="老板")
        grant_file = os.path.join(self.tmp, "g2.json")
        with open(grant_file, "w", encoding="utf-8") as f:
            json.dump(grant.to_dict(), f, ensure_ascii=False)
        with patch.object(wm, "_get_business_adapter", return_value=(adapter, "fake")):
            summary = wm.cmd_apply(grant_file=grant_file)
        row = wm._read_csv(match)[0]
        self.assertEqual(row["状态"], "已授权写入")
        self.assertEqual(summary["ok"], 1)
        self.assertEqual(len(adapter.rows), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
