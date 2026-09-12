#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""workflow.py verify 命令（v2.0 验收工具）的单元测试。

不跑真实工作流：构造最小 final.json + 假产物目录，验证验收判定的
三类逻辑：步骤状态 / 产物存在与新鲜度 / 口令泄露扫描。
"""
import datetime
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

# 运行日期用「今天」：产物 mtime 是真实时间，写死未来日期会被新鲜度检查误杀
_TODAY = datetime.date.today().isoformat()


def _step(sid, status="success", error=""):
    return {"step_id": sid, "status": status, "error": error}


class TestVerify(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="wf_verify_")
        import workflow as wf
        self.wf = wf
        self._orig_runs = wf.RUNS_DIR
        self._orig_here = wf.HERE
        wf.RUNS_DIR = os.path.join(self.tmp, "runs")
        wf.HERE = self.tmp   # 产物相对路径基于 HERE 解析
        os.makedirs(wf.RUNS_DIR, exist_ok=True)
        self.addCleanup(self._restore)

    def _restore(self):
        self.wf.RUNS_DIR = self._orig_runs
        self.wf.HERE = self._orig_here

    def _write_run(self, run_id, steps, finished=None):
        finished = finished or (_TODAY + " 09:30:00")
        d = os.path.join(self.wf.RUNS_DIR, run_id)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "final.json"), "w", encoding="utf-8") as f:
            json.dump({"run_id": run_id, "workflow": "daily", "status": "success",
                       "started_at": finished, "finished_at": finished,
                       "steps": steps, "summary": {}}, f, ensure_ascii=False)

    def _mk_artifact(self, rel, fresh=True):
        path = os.path.join(self.tmp, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write("x")
        if not fresh:
            import time
            old = time.time() - 10 * 86400   # 10 天前
            os.utime(path, (old, old))
        return path

    def _args(self, run_id):
        return mock.Mock(run_id=run_id)

    def test_all_green_with_artifacts(self):
        self._write_run("daily-t1", [
            _step("seatable_sync"), _step("partdb_sync"), _step("wechat_pull"),
            _step("wechat_summary"), _step("wxmatch_scan"), _step("alerts"),
            _step("foresee"), _step("foresee_review"), _step("daily_brief"),
            _step("cockpit")])
        for rel in ["data/项目.csv", "data/生产计划.csv",
                    "data/wechat_intake/latest.json",
                    "data/wechat_intake/summary_24h.md",
                    "data/核对结果.csv", "data/alerts.json", "data/foresee.json",
                    "data/daily_brief_short.md", "项目管理驾驶舱.html"]:
            self._mk_artifact(rel)
        rc = self.wf.cmd_verify(self._args("daily-t1"))
        self.assertEqual(rc, 0)

    def test_missing_artifact_fails(self):
        self._write_run("daily-t2", [_step("alerts")])
        # 不创建 data/alerts.json
        rc = self.wf.cmd_verify(self._args("daily-t2"))
        self.assertEqual(rc, 1)

    def test_stale_artifact_fails(self):
        self._write_run("daily-t3", [_step("alerts")])
        self._mk_artifact("data/alerts.json", fresh=False)   # 10 天前的旧文件
        rc = self.wf.cmd_verify(self._args("daily-t3"))
        self.assertEqual(rc, 1)

    def test_failed_step_fails_verify(self):
        self._write_run("daily-t4", [
            _step("seatable_sync"),
            _step("daily_brief", status="failed", error="exit=1 NameError")])
        rc = self.wf.cmd_verify(self._args("daily-t4"))
        self.assertEqual(rc, 1)

    def test_password_leak_detected(self):
        self._write_run("daily-t5", [_step("alerts")])
        self._mk_artifact("data/alerts.json")
        # 往 final.json 塞口令字段
        p = os.path.join(self.wf.RUNS_DIR, "daily-t5", "final.json")
        with open(p, encoding="utf-8") as f:
            d = json.load(f)
        d["passwords"] = {"admin_password": "ABC123"}
        with open(p, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False)
        rc = self.wf.cmd_verify(self._args("daily-t5"))
        self.assertEqual(rc, 1)

    def test_unknown_run(self):
        rc = self.wf.cmd_verify(self._args("no-such-run"))
        self.assertEqual(rc, 1)

    def test_latest_alias(self):
        self._write_run("daily-zz", [_step("alerts")])
        self._mk_artifact("data/alerts.json")
        # _latest_run_id 应取到它
        self.assertEqual(self.wf._latest_run_id(), "daily-zz")
        rc = self.wf.cmd_verify(self._args("latest"))
        self.assertEqual(rc, 0)

    def test_fresh_enough_window(self):
        a = self._mk_artifact("data/x.json", fresh=True)
        self.assertTrue(self.wf._fresh_enough(a, _TODAY))
        b = self._mk_artifact("data/y.json", fresh=False)
        self.assertFalse(self.wf._fresh_enough(b, _TODAY))


if __name__ == "__main__":
    unittest.main(verbosity=2, exit=False)
