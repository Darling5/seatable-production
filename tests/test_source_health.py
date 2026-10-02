# -*- coding: utf-8 -*-
"""驾驶舱「数据来源健康」模型（cockpit/cockpit.py 的 SOURCE_SPEC + _load_source_health）。

这一层取代了原先**5 套各自为政的「数据陈旧」判据**中的 3 套，因此必须有测试钉住：
  ① `SOURCE_SPEC` 完整性（每条 8 个字段、key 唯一、ok_h < bad_h）；
  ② 每个源的 `step_id` 必须**真实存在于对应工作流的 DAG** 里
     —— 写错一个字母，降级标注就会静默失效（永不命中）；
  ③ 三档分级（ok / warn / bad）与「产物缺失 = bad」；
  ④ 无内嵌时间戳的 CSV 必须退到文件 mtime 兜底；
  ⑤ `_parse_ts` 返回**带时区**时间（曾因 naive/aware 混算直接 TypeError 崩掉整页）；
  ⑥ `_latest_run_summary` 按**时间**取最近一次运行，不按目录名字典序
     （字典序会把 `daily-` 排在 `evening-` 前，就是 G5 那个坑）。

全程使用临时目录，不读真实 data/、不写任何文件。
"""
import csv
import datetime
import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cockpit import cockpit as CK
from application import contracts as C

TZ = CK._TZ


def _write_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)


def _write_ts(path, ts_path, ts):
    """按 SOURCE_SPEC 里**真实的** ts_path（可能是点路径）造一份产物。

    必须用真实字段名 —— 用臆造的字段会让 _dotted_get 取不到值、静默退回 mtime，
    于是「分级」测出来永远是 ok（本文件第一版就这么错过一次）。
    """
    obj = {}
    cur = obj
    parts = str(ts_path).split(".")
    for p in parts[:-1]:
        cur[p] = {}
        cur = cur[p]
    cur[parts[-1]] = ts
    _write_json(path, obj)


def _touch_old(path, hours_ago, now):
    """把文件 mtime 设成 hours_ago 小时前。"""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write("x\n")
    ts = (now - datetime.timedelta(hours=hours_ago)).timestamp()
    os.utime(path, (ts, ts))


class SourceSpecCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="srchealth_")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.now = datetime.datetime.now(TZ)
        self.today = self.now.date()

    # ── 造一个「全是新鲜产物」的最小 base ──
    def _base_all_fresh(self):
        for key, name, rel, ts_path, ok_h, bad_h, step_id, _ in CK.SOURCE_SPEC:
            path = os.path.join(self.tmp, "data", rel.replace("/", os.sep))
            if ts_path:
                _write_ts(path, ts_path, self.now.strftime("%Y-%m-%d %H:%M:%S"))
            else:
                _touch_old(path, 0.1, self.now)
        return self.tmp

    def _levels(self, base):
        sh = CK._load_source_health(self.today, base=base)
        return {s["key"]: s for s in sh["sources"]}, sh


# ────────────────────────────────────────────────────────────────────
# ① SOURCE_SPEC 完整性
# ────────────────────────────────────────────────────────────────────
class TestSourceSpecIntegrity(unittest.TestCase):
    def test_每条都有_8_个字段(self):
        for spec in CK.SOURCE_SPEC:
            self.assertEqual(len(spec), 8, "SOURCE_SPEC 每条必须是 8 元组：%r" % (spec,))

    def test_key_唯一(self):
        keys = [s[0] for s in CK.SOURCE_SPEC]
        self.assertEqual(len(keys), len(set(keys)), "数据源 key 重复：%s" % keys)

    def test_ok_阈值必须小于_bad_阈值(self):
        for key, _n, _rel, _ts, ok_h, bad_h, _sid, _c in CK.SOURCE_SPEC:
            self.assertLess(ok_h, bad_h, "%s 的 ok(%s) 必须小于 bad(%s)" % (key, ok_h, bad_h))

    def test_每个源都要写明不可用后果(self):
        for spec in CK.SOURCE_SPEC:
            self.assertTrue(str(spec[7]).strip(), "%s 缺少「不可用后果」说明" % spec[0])

    def test_声称对应某个步骤的源_该步骤必须真实存在于_DAG(self):
        """拼错 step_id → 降级标注永不命中（静默失效），必须当场拦下。"""
        from workflows.daily_refresh import build_steps as daily_steps
        from workflows.evening_full import build_steps as evening_steps
        real = {s.id for s in daily_steps()} | {s.id for s in evening_steps()}
        for key, _n, _rel, _ts, _ok, _bad, step_id, _c in CK.SOURCE_SPEC:
            if step_id:
                self.assertIn(step_id, real,
                              "源 %s 声明的 step_id=%r 不在任何 DAG 里" % (key, step_id))

    def test_seatable_阈值取_16_24(self):
        """业主 2026-10-02 定：daily 9:00 与 evening 19:00 最长间隔 14h，
        沿用旧的 6h 会让面板每天大部分时间常亮 amber，稀释告警价值。"""
        spec = {s[0]: s for s in CK.SOURCE_SPEC}["seatable"]
        self.assertEqual((spec[4], spec[5]), (16, 24))


# ────────────────────────────────────────────────────────────────────
# ② 三档分级
# ────────────────────────────────────────────────────────────────────
class TestGrading(SourceSpecCase):
    def test_全新鲜时全部_ok(self):
        base = self._base_all_fresh()
        by, sh = self._levels(base)
        bad = {k: v["level"] for k, v in by.items() if v["level"] != "ok"}
        self.assertEqual(bad, {}, "刚写完的产物竟然不是 ok：%s" % bad)
        self.assertEqual(sh["worst"], "ok")
        self.assertEqual(sh["n_bad"], 0)
        self.assertEqual(sh["n_warn"], 0)

    def test_产物缺失判_bad(self):
        base = self._base_all_fresh()
        victim = "seatable"
        spec = next(s for s in CK.SOURCE_SPEC if s[0] == victim)
        os.remove(os.path.join(base, "data", spec[2].replace("/", os.sep)))
        by, sh = self._levels(base)
        self.assertEqual(by[victim]["level"], "bad")
        self.assertEqual(by[victim]["note"], "产物不存在")
        self.assertIsNone(by[victim]["age_h"])
        self.assertEqual(sh["worst"], "bad")
        self.assertEqual(sh["n_bad"], 1)

    def test_超过_ok_未超_bad_判_warn(self):
        base = self._base_all_fresh()
        spec = next(s for s in CK.SOURCE_SPEC if s[0] == "seatable")
        path = os.path.join(base, "data", spec[2].replace("/", os.sep))
        ts = (self.now - datetime.timedelta(hours=spec[4] + 1)).strftime("%Y-%m-%d %H:%M:%S")
        _write_ts(path, spec[3], ts)
        by, sh = self._levels(base)
        self.assertEqual(by["seatable"]["level"], "warn")
        self.assertEqual(by["seatable"]["note"], "数据偏旧")
        self.assertEqual(sh["worst"], "warn")
        self.assertEqual((sh["n_warn"], sh["n_bad"]), (1, 0))

    def test_超过_bad_判_bad(self):
        base = self._base_all_fresh()
        spec = next(s for s in CK.SOURCE_SPEC if s[0] == "seatable")
        path = os.path.join(base, "data", spec[2].replace("/", os.sep))
        ts = (self.now - datetime.timedelta(hours=spec[5] + 1)).strftime("%Y-%m-%d %H:%M:%S")
        _write_ts(path, spec[3], ts)
        by, _sh = self._levels(base)
        self.assertEqual(by["seatable"]["level"], "bad")
        self.assertEqual(by["seatable"]["note"], "数据过旧")

    def test_分级计数自洽(self):
        base = self._base_all_fresh()
        by, sh = self._levels(base)
        self.assertEqual(sh["n_ok"] + sh["n_warn"] + sh["n_bad"], len(CK.SOURCE_SPEC))

    def test_无时间戳的_CSV_退回_mtime(self):
        """核对结果.csv / 原料行情记录.csv / 业务闭环 objects.csv 都没有时间戳字段，
        必须能用 mtime 兜底判级（否则它们永远是「产物不存在」式的 bad）。"""
        base = self._base_all_fresh()
        sh = CK._load_source_health(self.today, base=base)
        by = {s["key"]: s for s in sh["sources"]}
        for key in ("wxmatch", "commodities", "business_loop"):
            self.assertIsNotNone(by[key]["age_h"], "%s 没拿到 age_h（mtime 兜底失效）" % key)
            self.assertTrue(str(by[key]["at"]).strip(), "%s 的 at 为空" % key)


# ────────────────────────────────────────────────────────────────────
# ③ _parse_ts：必须返回带时区时间（曾直接 TypeError 崩掉整页）
# ────────────────────────────────────────────────────────────────────
class TestParseTs(unittest.TestCase):
    def test_返回带时区_且能与_now_相减(self):
        now = datetime.datetime.now(TZ)
        for text in ("2026-10-02 19:06:47", "2026-10-02 19:06",
                     "2026-10-02T19:06:47", "2026-10-02"):
            dt = CK._parse_ts(text)
            self.assertIsNotNone(dt, "解析失败：%s" % text)
            self.assertIsNotNone(dt.tzinfo, "%s 解析成了 naive → 相减会 TypeError" % text)
            self.assertIsInstance((now - dt).total_seconds(), float)

    def test_带偏移的文本以文本为准(self):
        dt = CK._parse_ts("2026-10-02T19:06:47+00:00")
        self.assertIsNotNone(dt)
        self.assertEqual(dt.utcoffset(), datetime.timedelta(0))

    def test_解析不了返回_None(self):
        for bad in ("", None, "不是时间", "2026/10/02"):
            self.assertIsNone(CK._parse_ts(bad))

    def test_时间戳形态异常不会崩掉整页(self):
        """兜底：产物里放一个畸形时间戳，健康检查必须继续（退回 mtime）。"""
        tmp = tempfile.mkdtemp(prefix="srcts_")
        self.addCleanup(shutil.rmtree, tmp, True)
        now = datetime.datetime.now(TZ)
        for _k, _n, rel, ts_path, _ok, _bad, _s, _c in CK.SOURCE_SPEC:
            path = os.path.join(tmp, "data", rel.replace("/", os.sep))
            if ts_path:
                _write_ts(path, ts_path, now.strftime("%Y-%m-%d %H:%M:%S"))
            else:
                _touch_old(path, 0.1, now)
        spec = next(s for s in CK.SOURCE_SPEC if s[0] == "seatable")
        _write_ts(os.path.join(tmp, "data", spec[2].replace("/", os.sep)),
                  spec[3], "0000-00-00 00:00:00")
        sh = CK._load_source_health(now.date(), base=tmp)      # 不允许抛异常
        by = {s["key"]: s for s in sh["sources"]}
        self.assertIsNotNone(by["seatable"]["age_h"], "畸形时间戳后没有退回 mtime")


# ────────────────────────────────────────────────────────────────────
# ④ 最近一次运行：按时间，不按目录名字典序（G5 同源坑）
# ────────────────────────────────────────────────────────────────────
class TestLatestRun(SourceSpecCase):
    def _run(self, run_id, status="success", steps=None):
        d = os.path.join(self.tmp, "data", "runs", run_id)
        _write_json(os.path.join(d, "final.json"),
                    {"run_id": run_id, "workflow": run_id.split("-")[0], "status": status,
                     "steps": steps or [], "summary": {}, "finished_at": "x"})
        return d

    def test_取时间最新而非字典序最大(self):
        """`daily-20261002-0900` 字典序**小于** `evening-20261001-1900` 之后的所有
        evening 目录，旧的 max(listdir) 会把 10-01 的 evening 当成「最近一次」。
        正确结果必须是 10-02 的 daily。"""
        self._run("evening-20261001-1900-aaaa")
        self._run("evening-20261001-1930-bbbb")
        self._run("daily-20261002-0900-cccc")
        got = CK._latest_run_summary(self.tmp)
        self.assertIsNotNone(got)
        self.assertEqual(got["run_id"], "daily-20261002-0900-cccc")

    def test_没有账本返回_None(self):
        self.assertIsNone(CK._latest_run_summary(self.tmp))

    def test_非步骤_JSON_不会被当成步骤(self):
        """context.json 没有 step_id —— 它绝不能被算进 steps（G2 同源坑）。"""
        d = self._run("daily-20261002-0900-cccc", steps=[
            {"step_id": "seatable_sync", "status": "success", "blocking": True}])
        _write_json(os.path.join(d, "context.json"), {"run_id": "daily-20261002-0900-cccc",
                                                     "workflow": "daily", "mode": "apply"})
        got = CK._latest_run_summary(self.tmp)
        self.assertEqual([s["step_id"] for s in got["steps"]], ["seatable_sync"])

    def test_降级步骤被标注为_degraded(self):
        """非阻断失败 → 该源 degraded=True + 至少 warn（这是「降级可见」的落点）。"""
        base = self._base_all_fresh()
        self._run("daily-20261002-0900-cccc", status="degraded", steps=[
            {"step_id": "partdb_sync", "status": "failed", "blocking": False,
             "error": "HTTP 502"}])
        by, sh = self._levels(base)
        self.assertTrue(by["partdb"]["degraded"])
        self.assertIn(by["partdb"]["level"], ("warn", "bad"))
        self.assertIn("未阻断", by["partdb"]["note"])
        self.assertEqual(sh["run"]["status"], "degraded")

    def test_阻断型失败该源判_bad(self):
        base = self._base_all_fresh()
        self._run("daily-20261002-0900-cccc", status="failed", steps=[
            {"step_id": "partdb_sync", "status": "failed", "blocking": True}])
        by, _sh = self._levels(base)
        self.assertEqual(by["partdb"]["level"], "bad")
        self.assertFalse(by["partdb"]["degraded"])

    def test_运行里的步骤与本源无关时不误标(self):
        """别的源失败不该把无关源拉下水。"""
        base = self._base_all_fresh()
        self._run("daily-20261002-0900-cccc", status="degraded", steps=[
            {"step_id": "partdb_sync", "status": "failed", "blocking": False}])
        by, _sh = self._levels(base)
        self.assertFalse(by["wechat"]["degraded"])
        self.assertEqual(by["wechat"]["level"], "ok")


# ────────────────────────────────────────────────────────────────────
# ⑤ 结构契约（前端只处理一种形状）
# ────────────────────────────────────────────────────────────────────
class TestShape(SourceSpecCase):
    def test_返回结构固定(self):
        base = self._base_all_fresh()
        sh = CK._load_source_health(self.today, base=base)
        for k in ("available", "checked_at", "run", "sources",
                  "n_bad", "n_warn", "n_ok", "worst"):
            self.assertIn(k, sh)
        for s in sh["sources"]:
            for k in ("key", "name", "artifact", "at", "age_h", "ok_h", "bad_h",
                      "level", "note", "step_id", "degraded", "consequence"):
                self.assertIn(k, s, "源 %s 缺字段 %s" % (s.get("key"), k))
            self.assertTrue(s["artifact"].startswith("data/"), s["artifact"])

    def test_空_base_不抛异常(self):
        empty = tempfile.mkdtemp(prefix="empty_")
        self.addCleanup(shutil.rmtree, empty, True)
        sh = CK._load_source_health(self.today, base=empty)
        self.assertTrue(sh["available"])
        self.assertIsNone(sh["run"])
        self.assertEqual(sh["n_bad"], len(CK.SOURCE_SPEC))
        self.assertEqual(sh["worst"], "bad")

    def test_损坏的_JSON_不崩整页(self):
        base = self._base_all_fresh()
        spec = next(s for s in CK.SOURCE_SPEC if s[0] == "seatable")
        with open(os.path.join(base, "data", spec[2].replace("/", os.sep)),
                  "w", encoding="utf-8") as f:
            f.write("{ 这不是 json")
        sh = CK._load_source_health(self.today, base=base)     # 不允许抛异常
        self.assertEqual(len(sh["sources"]), len(CK.SOURCE_SPEC))


# ────────────────────────────────────────────────────────────────────
# ⑥ 与**真实数据**的一致性（只读；产物缺失时跳过，不算失败）
# ────────────────────────────────────────────────────────────────────
REAL_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class TestRealDataConsistency(unittest.TestCase):
    """SOURCE_SPEC 声明的「产物路径 + 时间戳字段」必须与磁盘上的真实产物对得上。

    这类错位在本仓库真实发生过：行情链路曾出现「代码写一个文件名、磁盘上是另一个」
    （物料行情记录.csv vs 原料行情记录.csv），结果读取方永远拿到空数据。
    因此对**存在**的产物，断言声明的 ts_path 真的能取到时间戳。
    """

    def test_声明的产物路径与字段名对得上(self):
        mismatched = []
        for key, name, rel, ts_path, _ok, _bad, _sid, _c in CK.SOURCE_SPEC:
            path = os.path.join(REAL_BASE, "data", rel.replace("/", os.sep))
            if not os.path.exists(path):
                continue                      # 产物不存在属正常（该源本就该判 bad）
            if not ts_path:
                continue                      # 无时间戳的源靠 mtime，无需校验字段
            val = CK._dotted_get(CK._read_json_soft(path), ts_path)
            if not val:
                mismatched.append("%s（%s）里取不到字段 %r" % (key, rel, ts_path))
        self.assertEqual(mismatched, [], "SOURCE_SPEC 与真实产物错位：%s" % mismatched)

    def test_无时间戳的源真的没有时间戳字段声明(self):
        """反向：声明 ts_path=None 的源，必须确实是 CSV（没有内嵌时间戳），
        否则就是在用 mtime 兜底一个本来有更准时间戳的产物。"""
        for key, _n, rel, ts_path, _ok, _bad, _sid, _c in CK.SOURCE_SPEC:
            if ts_path:
                continue
            self.assertTrue(rel.lower().endswith(".csv"),
                            "%s 声明无时间戳但产物不是 CSV：%s" % (key, rel))


if __name__ == "__main__":
    unittest.main(verbosity=2)
