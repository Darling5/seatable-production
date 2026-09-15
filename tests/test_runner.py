# -*- coding: utf-8 -*-
"""application/runner.py 的离线单测：DAG 拓扑、三道闸、失败传播、resume、落盘。"""
import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from application import contracts as C
from application.runner import WorkflowRunner, make_step


class BaseRunnerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="runner_test_")
        self.ctx = C.RunContext(run_id="run-x", workflow="test", mode=C.MODE_APPLY,
                                skill_dir=self.tmp)
        self.runs_dir = os.path.join(self.tmp, "runs")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _step(self, sid, status=C.STATUS_SUCCESS, deps=(), effect=C.SIDE_LOCAL_APPEND,
              policy="continue", fn=None):
        def _run(ctx):
            if fn:
                fn()
            return C.StepResult(step_id=sid).ok() if status == C.STATUS_SUCCESS \
                else C.StepResult(step_id=sid).fail("boom")
        return C.StepSpec(id=sid, name=sid, run=_run, depends_on=deps,
                          side_effect=effect, failure_policy=policy)


class TestDag(BaseRunnerTest):
    def test_topological_order(self):
        steps = [self._step("c", deps=("b",)),
                 self._step("a"),
                 self._step("b", deps=("a",))]
        rr = WorkflowRunner(self.ctx, steps, self.runs_dir).run()
        order = [s["step_id"] for s in rr.steps]
        self.assertEqual(order.index("a") < order.index("b") < order.index("c"), True)

    def test_cycle_rejected(self):
        steps = [self._step("a", deps=("b",)), self._step("b", deps=("a",))]
        with self.assertRaises(ValueError):
            WorkflowRunner(self.ctx, steps, self.runs_dir)

    def test_duplicate_id_rejected(self):
        with self.assertRaises(ValueError):
            WorkflowRunner(self.ctx, [self._step("a"), self._step("a")], self.runs_dir)

    def test_unknown_dep_rejected(self):
        with self.assertRaises(ValueError):
            WorkflowRunner(self.ctx, [self._step("a", deps=("zzz",))], self.runs_dir)


class TestGates(BaseRunnerTest):
    def test_preview_blocks_online_write(self):
        self.ctx.mode = C.MODE_PREVIEW
        steps = [self._step("crm", effect=C.SIDE_ONLINE_WRITE)]
        rr = WorkflowRunner(self.ctx, steps, self.runs_dir).run()
        self.assertEqual(rr.steps[0]["status"], C.STATUS_BLOCKED)
        self.assertIn("preview", rr.steps[0]["error"])

    def test_destructive_without_yes_blocked(self):
        steps = [self._step("prune", effect=C.SIDE_DESTRUCTIVE)]
        rr = WorkflowRunner(self.ctx, steps, self.runs_dir).run()
        self.assertEqual(rr.steps[0]["status"], C.STATUS_BLOCKED)
        self.assertIn("--yes", rr.steps[0]["error"])

    def test_destructive_with_yes_runs(self):
        self.ctx.yes = True
        steps = [self._step("prune", effect=C.SIDE_DESTRUCTIVE)]
        rr = WorkflowRunner(self.ctx, steps, self.runs_dir).run()
        self.assertEqual(rr.steps[0]["status"], C.STATUS_SUCCESS)


class TestFailure(BaseRunnerTest):
    def test_failure_blocks_downstream(self):
        steps = [self._step("sync", status=C.STATUS_FAILED),
                 self._step("pull", deps=("sync",))]
        rr = WorkflowRunner(self.ctx, steps, self.runs_dir).run()
        self.assertEqual(rr.steps[0]["status"], C.STATUS_FAILED)
        self.assertEqual(rr.steps[1]["status"], C.STATUS_BLOCKED)

    def test_abort_policy_blocks_everything_after(self):
        steps = [self._step("a", status=C.STATUS_FAILED, policy="abort"),
                 self._step("b"),                      # 无依赖，也会被 abort 拦
                 self._step("c", deps=("b",))]
        rr = WorkflowRunner(self.ctx, steps, self.runs_dir).run()
        self.assertEqual([s["status"] for s in rr.steps],
                         [C.STATUS_FAILED, C.STATUS_BLOCKED, C.STATUS_BLOCKED])

    def test_continue_policy_lets_independent_steps_run(self):
        steps = [self._step("a", status=C.STATUS_FAILED),
                 self._step("b")]
        rr = WorkflowRunner(self.ctx, steps, self.runs_dir).run()
        self.assertEqual(rr.steps[1]["status"], C.STATUS_SUCCESS)

    def test_exception_in_step_is_caught(self):
        def boom(ctx):
            raise RuntimeError("炸了")
        steps = [C.StepSpec(id="x", name="x", run=boom)]
        rr = WorkflowRunner(self.ctx, steps, self.runs_dir).run()
        self.assertEqual(rr.steps[0]["status"], C.STATUS_FAILED)
        self.assertIn("RuntimeError", rr.steps[0]["error"])

    def test_retry(self):
        calls = {"n": 0}

        def flaky(ctx):
            calls["n"] += 1
            if calls["n"] < 3:
                raise RuntimeError("第一次和第二次失败")
            return C.StepResult(step_id="flaky").ok()
        steps = [C.StepSpec(id="flaky", name="flaky", run=flaky, retry=2)]
        rr = WorkflowRunner(self.ctx, steps, self.runs_dir).run()
        self.assertEqual(calls["n"], 3)
        self.assertEqual(rr.steps[0]["status"], C.STATUS_SUCCESS)


class TestArtifacts(BaseRunnerTest):
    def test_final_json_written(self):
        steps = [self._step("a"), self._step("b", deps=("a",))]
        rr = WorkflowRunner(self.ctx, steps, self.runs_dir).run()
        final = json.load(open(os.path.join(self.runs_dir, "run-x", "final.json"),
                               encoding="utf-8"))
        self.assertEqual(final["run_id"], "run-x")
        self.assertEqual(final["summary"], {"total": 2, "success": 2,
                                            "failed": 0, "skipped": 0, "blocked": 0})
        # 每步一个 JSON
        files = sorted(os.listdir(os.path.join(self.runs_dir, "run-x")))
        self.assertEqual(files, ["01_a.json", "02_b.json", "final.json"])

    def test_run_status_failed_when_any_failed(self):
        steps = [self._step("a"), self._step("b", status=C.STATUS_FAILED)]
        rr = WorkflowRunner(self.ctx, steps, self.runs_dir).run()
        self.assertEqual(rr.status, C.STATUS_FAILED)


class TestResume(BaseRunnerTest):
    def test_resume_skips_succeeded(self):
        ran = []
        steps = [self._step("a", fn=lambda: ran.append("a")),
                 self._step("b", deps=("a",), fn=lambda: ran.append("b"))]
        WorkflowRunner(self.ctx, steps, self.runs_dir).run()
        # 第二次运行：a 成功 b 失败的旧 run
        ctx2 = C.RunContext(run_id="run-y", workflow="test", mode=C.MODE_APPLY,
                            skill_dir=self.tmp, resume_of="run-x")
        WorkflowRunner(ctx2, steps, self.runs_dir).run()
        self.assertEqual(ran, ["a", "b"])  # 第一次执行
        # resume 时全部已成功 → 不再执行
        self.assertEqual(ran, ["a", "b"])


class TestMakeStep(BaseRunnerTest):
    def test_wraps_subprocess(self):
        import sys as _s
        step = make_step([_s.executable, "-c", "print('hi')"], "py", "python 步骤")
        rr = WorkflowRunner(self.ctx, [step], self.runs_dir).run()
        self.assertEqual(rr.steps[0]["status"], C.STATUS_SUCCESS)

    def test_nonzero_exit_fails(self):
        step = make_step([sys.executable, "-c", "import sys; sys.exit(2)"], "bad", "失败步骤")
        rr = WorkflowRunner(self.ctx, [step], self.runs_dir).run()
        self.assertEqual(rr.steps[0]["status"], C.STATUS_FAILED)
        self.assertIn("exit=2", rr.steps[0]["error"])

    def test_exit3_is_skipped_not_failed(self):
        """退出码 3 = skipped（数据源不可用）：不冒充成功，也不算失败。

        这是 2026-09-13 实测发现的缺口：微信未登录时 wechat_pull 打印 skip
        后返回 0，runner 记 success，但产物是旧文件——验收器当场抓包。
        语义修正：skip 用退出码 3，runner 记 skipped，验收不查其产物新鲜度。
        """
        step = make_step([sys.executable, "-c",
                          "import sys; print('[skip] 数据源不可用'); sys.exit(3)"],
                         "sk", "跳过步骤")
        rr = WorkflowRunner(self.ctx, [step], self.runs_dir).run()
        self.assertEqual(rr.steps[0]["status"], C.STATUS_SKIPPED)
        self.assertNotIn("exit=3", rr.steps[0].get("error") or "")


if __name__ == "__main__":
    unittest.main()
