# -*- coding: utf-8 -*-
"""workflows/daily_refresh.py + workflow.py CLI 的离线测试。

只验证 DAG 结构与 CLI 参数解析，不真跑业务脚本（那属于在线冒烟）。
"""
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from application import contracts as C
from workflows import workflow
from workflows.daily_refresh import build_steps, WORKFLOWS


class TestDailyDag(unittest.TestCase):
    def setUp(self):
        self.steps = build_steps()

    def test_workflow_registered(self):
        self.assertIn("daily", WORKFLOWS)

    def test_expected_steps_present(self):
        ids = {s.id for s in self.steps}
        self.assertEqual(ids, {
            "seatable_sync", "partdb_sync", "wechat_pull", "wechat_summary",
            "wxmatch_scan", "alerts", "foresee", "foresee_review",
            "loop_trigger", "loop_sync", "daily_brief", "cockpit"})

    def test_no_online_write_or_destructive(self):
        """每日刷新只允许本地写入 + loop_sync 的 CRM 镜像——
        loop_sync 与 crm 路由同策略（auto_with_ledger：自动写入 + 幂等 + 可撤销），
        production/tasks 的云端写入仍走候选制，DAG 不碰。"""
        for s in self.steps:
            if s.id == "loop_sync":
                self.assertEqual(s.side_effect, C.SIDE_ONLINE_WRITE)
                continue
            self.assertIn(s.side_effect, (C.SIDE_LOCAL_APPEND, C.SIDE_READ_ONLY),
                          "%s 副作用越界：%s" % (s.id, s.side_effect))

    def test_loop_sync_step_shape(self):
        """loop_sync：CRM 镜像写入，幂等 upsert，依赖 seatable_sync。

        blocking=False（2026-10-03 收口）：它是**叶子步骤**（全 DAG 无任何步骤
        depends_on 它），且 CRM 镜像不是驾驶舱的展示必要项 —— 一个云端后端挂掉
        不该带走整链。失败时整次判 degraded（可发布 + 强制告警），
        而不是 failed（拒绝发布）。判据与 partdb_sync / loop_trigger 一致。
        """
        by = {s.id: s for s in self.steps}
        ls = by["loop_sync"]
        self.assertEqual(ls.side_effect, C.SIDE_ONLINE_WRITE)
        self.assertEqual(ls.failure_policy, "continue")
        self.assertFalse(ls.blocking)
        self.assertIn("seatable_sync", ls.depends_on)

    def test_sync_steps_are_abort(self):
        """SeaTable 是主数据源 —— 它没了整条链就没有可信输入，必须 abort。

        （2026-10-02 拆分）原先本测试同时断言 partdb_sync 也是 abort，
        但 PartDB 已改为**降级**路径：见下面 test_partdb_sync_degrades_not_aborts。
        """
        by = {s.id: s for s in self.steps}
        self.assertEqual(by["seatable_sync"].failure_policy, "abort")
        self.assertTrue(by["seatable_sync"].blocking)

    def test_partdb_sync_degrades_not_aborts(self):
        """PartDB 挂掉不能把整条链带走（业主 2026-10-02 的 plan B 判据）。

        正确行为：本地快照沿用上一份 + 结构告警 + 驾驶舱仍出，整次判 degraded
        （可见但不拦发布），**不是** abort、**不是** blocking。
        """
        by = {s.id: s for s in self.steps}
        pd = by["partdb_sync"]
        self.assertEqual(pd.failure_policy, "continue")
        self.assertFalse(pd.blocking)
        self.assertEqual(pd.retry, 1)

    def test_loop_trigger_runs_before_loop_sync(self):
        """loop_trigger（来单线索 → 控制平面案件）必须排在 loop_sync（镜像 CRM）之前，
        否则镜像里永远看不到本日新建的案件。"""
        order = [s.id for s in self.steps]
        self.assertLess(order.index("loop_trigger"), order.index("loop_sync"))
        by = {s.id: s for s in self.steps}
        self.assertIn("loop_trigger", by["loop_sync"].depends_on)

    def test_loop_trigger_shape(self):
        """loop_trigger 是**降级候选**：它在业务表之后跑，失败只影响控制平面。

        blocking=False + failure_policy=continue 是刻意的 —— 它一旦被判 blocking，
        就违反了「一个库挂掉不能把整条链路带走」。退出码 3（skipped）由脚本自身
        在「CRM 不可用、本次没扫成」时给出，避免被误记 success（假指标）。
        """
        by = {s.id: s for s in self.steps}
        lt = by["loop_trigger"]
        self.assertEqual(lt.side_effect, C.SIDE_LOCAL_APPEND)
        self.assertFalse(lt.blocking)
        self.assertEqual(lt.failure_policy, "continue")
        self.assertIn("seatable_sync", lt.depends_on)

    def test_dependencies_sane(self):
        by = {s.id: s for s in self.steps}
        self.assertIn("seatable_sync", by["wechat_pull"].depends_on)
        self.assertIn("wechat_pull", by["wechat_summary"].depends_on)
        self.assertIn("foresee", by["foresee_review"].depends_on)
        self.assertIn("alerts", by["daily_brief"].depends_on)
        self.assertIn("foresee", by["cockpit"].depends_on)
        # 同步失败时驾驶舱必须被阻断（旧数据刷新 UI 是误导）
        self.assertIn("seatable_sync", by["cockpit"].depends_on)
        self.assertIn("partdb_sync", by["cockpit"].depends_on)

    def test_all_deps_resolvable(self):
        ids = {s.id for s in self.steps}
        for s in self.steps:
            for d in s.depends_on:
                self.assertIn(d, ids)


class TestCli(unittest.TestCase):
    """CLI 参数解析（不触发执行）。"""

    def _parse(self, argv):
        import argparse
        from workflows.workflow import main  # noqa: F401 — 复用其 parser 逻辑太重，直接独立构建
        # 简化：直接调用 main 会执行，这里只验证 choices 合法性
        return argv

    def test_workflow_module_importable(self):
        from workflows import workflow  # noqa: F401
        self.assertTrue(hasattr(workflow, "cmd_run"))

    def test_daily_callable(self):
        """build_steps 可重复调用且无状态残留。"""
        a = build_steps()
        b = build_steps()
        self.assertEqual([s.id for s in a], [s.id for s in b])


class TestRunExitCode(unittest.TestCase):
    """`cmd_run` 的**工作流级**退出码三档（2026-10-03 收口）。

    为什么要单独钉住它：全仓**没有任何消费者**读这个退出码（实测无 `$?` /
    `%ERRORLEVEL%` / `returncode` 分支），所以它是**纯语义契约** —— 一旦被改回去，
    不会有任何东西报错，自动化只能看到"非 0"，`degraded` 与 `failed` 重新变得
    不可区分，"降级"就白做了。

    ⚠️ 勿与**步骤脚本级**退出码混淆（`application/runner.py::make_step`：0 / 3 / 其它），
    那是另一套命名空间；`cockpit/publish.py` 在步骤级用 2 表示"上传失败"。
    """

    def _rc(self, status):
        """离线取一次 `cmd_run` 的返回值：不真跑工作流，只喂一个假结果。"""
        import argparse
        import contextlib
        import io
        fake = type("_FakeRunResult", (), {
            "status": status,
            "steps": [{"step_id": "x", "status": status, "error": ""}],
            "summary": {},
        })()
        args = argparse.Namespace(
            workflow="daily", mode="preview", actor="test", yes=False,
            run_id="daily-test-00000000-0000", resume=None)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            with patch.object(workflow, "run_workflow", return_value=fake):
                return workflow.cmd_run(args)

    def test_success_是_0(self):
        self.assertEqual(self._rc(C.STATUS_SUCCESS), 0)

    def test_degraded_是_2(self):
        """降级必须与硬失败分开 —— 这是运行级三态改造的最后一公里。"""
        self.assertEqual(self._rc(C.STATUS_DEGRADED), 2)

    def test_failed_是_1(self):
        self.assertEqual(self._rc(C.STATUS_FAILED), 1)

    def test_未知状态保守按失败(self):
        """未知状态不许被当成成功，也不许被当成降级。"""
        self.assertEqual(self._rc("zz_unknown_status"), 1)

    def test_三档互不相同(self):
        """三档退出码必须两两可区分（曾经 degraded 与 failed 同为 1）。"""
        codes = [self._rc(s) for s in
                 (C.STATUS_SUCCESS, C.STATUS_DEGRADED, C.STATUS_FAILED)]
        self.assertEqual(len(set(codes)), 3, "退出码无法区分三档：%r" % (codes,))


if __name__ == "__main__":
    unittest.main()
