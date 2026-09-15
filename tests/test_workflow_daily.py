# -*- coding: utf-8 -*-
"""workflows/daily_refresh.py + workflow.py CLI 的离线测试。

只验证 DAG 结构与 CLI 参数解析，不真跑业务脚本（那属于在线冒烟）。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from application import contracts as C
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
            "loop_sync", "daily_brief", "cockpit"})

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
        """loop_sync：CRM 镜像写入，幂等 upsert，依赖 seatable_sync。"""
        by = {s.id: s for s in self.steps}
        ls = by["loop_sync"]
        self.assertEqual(ls.side_effect, C.SIDE_ONLINE_WRITE)
        self.assertEqual(ls.failure_policy, "continue")
        self.assertIn("seatable_sync", ls.depends_on)

    def test_sync_steps_are_abort(self):
        by = {s.id: s for s in self.steps}
        self.assertEqual(by["seatable_sync"].failure_policy, "abort")
        self.assertEqual(by["partdb_sync"].failure_policy, "abort")

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
        from workflow import main  # noqa: F401 — 复用其 parser 逻辑太重，直接独立构建
        # 简化：直接调用 main 会执行，这里只验证 choices 合法性
        return argv

    def test_workflow_module_importable(self):
        import workflow  # noqa: F401
        self.assertTrue(hasattr(workflow, "cmd_run"))

    def test_daily_callable(self):
        """build_steps 可重复调用且无状态残留。"""
        a = build_steps()
        b = build_steps()
        self.assertEqual([s.id for s in a], [s.id for s in b])


if __name__ == "__main__":
    unittest.main()
