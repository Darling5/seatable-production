#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""op.py 驾驶舱刷新解耦（v2.0 P0-1）的单元测试。

不触真实 SeaTable：用 StubAdapter 替身，验证
  1. 默认不刷新（架构目标：写入归写入，渲染归 workflow/cockpit）
  2. --refresh 显式开启
  3. 环境变量 SEATABLE_AUTO_REFRESH_COCKPIT=1 兜底开启
  4. 刷新失败不影响写入成功（子进程异常只告警）
"""
import os
import sys
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class _Args:
    """模拟 argparse Namespace。"""
    def __init__(self, refresh=False, out=None, config=None):
        self.refresh = refresh
        self.out = out
        self.config = config


class _StubAdapter:
    """只实现 _apply_and_refresh 用到的 append_row。"""
    def __init__(self):
        self.written = []

    def append_row(self, table, row):
        self.written.append((table, row))
        return "row_test_%d" % len(self.written)


class TestRefreshSwitch(unittest.TestCase):
    def setUp(self):
        # 每个用例都在干净的环境变量下跑
        self._env_patcher = mock.patch.dict(os.environ, {},
                                            clear=False)
        os.environ.pop("SEATABLE_AUTO_REFRESH_COCKPIT", None)
        self.addCleanup(self._cleanup_env)

    def _cleanup_env(self):
        os.environ.pop("SEATABLE_AUTO_REFRESH_COCKPIT", None)

    def _no_subprocess(self):
        """让 subprocess.run 抛异常，模拟「刷新失败」。"""
        import subprocess
        return mock.patch("subprocess.run", side_effect=OSError("boom"))

    # ── 开关判定 ──────────────────────────────────────────
    def test_default_off(self):
        import op
        self.assertFalse(op._should_refresh_cockpit(_Args()))

    def test_flag_on(self):
        import op
        self.assertTrue(op._should_refresh_cockpit(_Args(refresh=True)))

    def test_env_on(self):
        import op
        os.environ["SEATABLE_AUTO_REFRESH_COCKPIT"] = "1"
        self.assertTrue(op._should_refresh_cockpit(_Args()))

    def test_env_variants(self):
        import op
        for v in ("true", "YES", "on", " 1 "):
            os.environ["SEATABLE_AUTO_REFRESH_COCKPIT"] = v
            self.assertTrue(op._should_refresh_cockpit(_Args()), msg=v)
        os.environ["SEATABLE_AUTO_REFRESH_COCKPIT"] = "0"
        self.assertFalse(op._should_refresh_cockpit(_Args()))
        os.environ["SEATABLE_AUTO_REFRESH_COCKPIT"] = "false"
        self.assertFalse(op._should_refresh_cockpit(_Args()))

    # ── 行为判定 ──────────────────────────────────────────
    def test_write_succeeds_without_refresh(self):
        """默认：写入成功、提示解耦说明、不调子进程。"""
        import op
        a = _StubAdapter()
        with mock.patch("subprocess.run") as run:
            with mock.patch("builtins.print"):
                op._apply_and_refresh(a, _Args(), "生产计划", {"生产产品": "X"})
        self.assertEqual(len(a.written), 1)
        run.assert_not_called()

    def test_refresh_invoked_when_flag(self):
        """--refresh：写入后调用 cockpit 子进程。"""
        import op
        a = _StubAdapter()
        with mock.patch("subprocess.run") as run:
            with mock.patch("builtins.print"):
                op._apply_and_refresh(a, _Args(refresh=True, out="o.html"),
                                      "生产计划", {"生产产品": "X"})
        self.assertEqual(len(a.written), 1)
        run.assert_called_once()
        cmd = run.call_args[0][0]
        self.assertIn("cockpit.py", cmd[1])

    def test_refresh_failure_does_not_fail_write(self):
        """刷新子进程抛异常：写入结果保留，只输出 warn。"""
        import op
        a = _StubAdapter()
        with self._no_subprocess():
            with mock.patch("builtins.print") as pr:
                op._apply_and_refresh(a, _Args(refresh=True), "生产计划",
                                      {"生产产品": "X"})
        self.assertEqual(len(a.written), 1)
        joined = "\n".join(str(c) for c in pr.call_args_list)
        self.assertIn("warn", joined)

    def test_intake_refresh_gate(self):
        """_refresh_cockpit（intake 路径）：默认直接 return，不碰子进程。"""
        import op
        with mock.patch("subprocess.run") as run:
            with mock.patch("builtins.print"):
                op._refresh_cockpit(_Args())
        run.assert_not_called()
        with mock.patch("subprocess.run") as run:
            with mock.patch("builtins.print"):
                op._refresh_cockpit(_Args(refresh=True))
        run.assert_called_once()

    def test_env_overrides_in_intake_path(self):
        """环境变量对 intake 路径同样生效。"""
        import op
        os.environ["SEATABLE_AUTO_REFRESH_COCKPIT"] = "1"
        with mock.patch("subprocess.run") as run:
            with mock.patch("builtins.print"):
                op._refresh_cockpit(_Args())
        run.assert_called_once()


if __name__ == "__main__":
    unittest.main(verbosity=2, exit=False)
