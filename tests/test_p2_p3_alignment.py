# -*- coding: utf-8 -*-
"""tests/test_p2_p3_alignment.py — 二期 / 三期**跨期契约**测试。

这个文件跟 `test_decision_support.py` 的区别，是它**同时加载两期的真实模块**：

  · 用真实的二期 `ProjectBrain` 回放联合夹具 `alignment-p2-scenario.json`；
  · 用真实的二期 `get_project_context` 读上下文；
  · 喂给三期的 `check_alignment` / `human_prediction_records` / `DecisionSupport`；
  · 用二期的 `ids.validate_id` 反过来校验三期产出的 `plan_id` 是否合规。

为什么必须这样测：**单期测试永远测不出「两期说的不是一回事」**。
本次对齐实跑发现的五个缺陷（`plan_id` 格式不合格、二期投影丢字段、证据缺 plan_id、
`data_as_of` 口径错、两期顶层包同名冲突）全部是在这个文件的基础上暴露出来的 ——
单看任何一期，它们都"通过"。

期望值一律**手工推导后写死**，不用被测算法反推。
二期模块不可用时相关用例 skip 并说明原因，**不静默通过**。
"""
from __future__ import annotations

import datetime as _dt
import importlib
import json
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from application.decision_support import alignment as ALIGN          # noqa: E402
from application.decision_support import schema as S                  # noqa: E402
from application.decision_support.service import DecisionSupport      # noqa: E402
from application.decision_support.alignment import (SEV_ERROR, SEV_INFO,  # noqa: E402
                                                    SEV_WARN)

EXAMPLES = os.path.join(ROOT, "docs", "contracts", "examples")
SCENARIO = os.path.join(EXAMPLES, "alignment-p2-scenario.json")
PLANNING = os.path.join(EXAMPLES, "decision-support-planning-sample.json")
MOCK_CONTEXT = os.path.join(EXAMPLES, "decision-support-context-mock.json")
FIELD_MAP_JSON = os.path.join(EXAMPLES, "alignment-field-map.json")

# ── 联合夹具的手工推导期望值（与算法无关，写死）──
PID = "PRJ-20260928-0001"
SID = "SNP-20260928-0830"
PLAN_A = "PLN-20260928-A001"
PLAN_B = "PLN-20260928-B001"
PLAN_C = "PLN-20260928-C001"
HUMAN_PRED_DATE = "2026-09-30"
HUMAN_PRED_AT = "2026-09-25T10:06:00"
ACTION_DUE = "2026-09-30"          # 由「下周三」相对 2026-09-24（周四）解析而来
CONFLICT_ASPECT = "A_material_available_at"
CONFLICT_VALUES = ["2026-09-29", "2026-09-30"]
DATA_AS_OF = "2026-09-26 09:08:00"   # 夹具里最晚一条**采集**时间（msg-2005）
DATA_AS_OF_ISO = "2026-09-26T09:08:00"
OBS_TEXT = "产线称 A 批物料"          # 未核实观察的原文，不得进三期产出

BRAIN, BRAIN_KIND, BRAIN_ERR = ALIGN.import_brain()
HAS_BRAIN = BRAIN is not None
SKIP_MSG = ("二期 application.project_brain 不可导入（%s：%s）。"
            "合并分支或让 aligner 能定位兄弟 worktree 后重跑。"
            % (BRAIN_KIND, BRAIN_ERR[:160]))


def load(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def p2_sibling(name: str):
    """取二期包内的兄弟模块（authorization / contracts）。

    二期内部用相对导入，所以按**装载时的包名**取，避免拿到三期那份同名模块 ——
    `WriteGrant` 的 isinstance 判定是按类对象比对的，混用两个副本会被判为「没授权」。
    """
    pkg = BRAIN.__name__.rsplit(".", 1)[0]
    return importlib.import_module(pkg + "." + name)


def replay_fixture(tmp) -> dict:
    """用**真实二期引擎**回放联合夹具，返回 context。"""
    auth = p2_sibling("authorization")
    contracts = p2_sibling("contracts")
    brain = BRAIN.ProjectBrain(root=tmp)
    grant = auth.manual_grant(tables=tuple(BRAIN.schema.TABLE_NAMES),
                              actions=("append", "update"),
                              actor="跨期测试", reason="回放联合夹具")
    out = BRAIN.replay(brain, load(SCENARIO), mode=contracts.MODE_APPLY,
                       grant=grant, actor="跨期测试")
    if not out["ok"]:
        raise AssertionError("联合夹具回放失败：%s"
                             % [e for e in out["log"] if e.get("status") == "error"])
    return brain.context(PID, SID)


@unittest.skipUnless(HAS_BRAIN, SKIP_MSG)
class RealBrainAlignmentTest(unittest.TestCase):
    """用真实二期引擎跑通整条跨期链路。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="p2p3_align_")
        cls.ctx = replay_fixture(cls.tmp)
        cls.planning = load(PLANNING)
        cls.report = ALIGN.check_alignment(cls.ctx, cls.planning)

    # ── 1. 契约 §1：八个统一字段必须真的在每条记录上 ──
    def test_every_record_carries_all_unified_fields(self):
        """这条测试就是「二期投影丢字段」那个缺陷的探针。

        夹具不覆盖全部记忆类型（没有 verify 步骤所以没有 fact），
        因此对空桶跳过、但对**出现过的桶**一条都不放过。
        """
        seen = 0
        for bucket in ("observations", "facts", "commitments", "decisions",
                       "predictions", "actions", "evidence_refs"):
            rows = self.ctx.get(bucket) or []
            if not rows:
                continue
            seen += 1
            for r in rows:
                missing = [f for f in ALIGN.UNIFIED_FIELDS if f not in r]
                self.assertEqual(missing, [],
                                 "%s 的记录 %s 缺统一字段 %s"
                                 % (bucket, r.get("memory_id") or r.get("action_id")
                                    or r.get("evidence_id"), missing))
        self.assertGreaterEqual(seen, 4, "夹具应至少产出 4 类记录")

    def test_plan_id_present_on_memory_action_evidence(self):
        """plan_id 是跨期唯一的计划联表键，绝不能空着。"""
        for bucket, key in (("observations", "memory_id"), ("decisions", "memory_id"),
                            ("predictions", "memory_id"), ("actions", "action_id"),
                            ("evidence_refs", "evidence_id")):
            for r in self.ctx.get(bucket) or []:
                self.assertTrue(r.get("plan_id"),
                                "%s.%s 的 plan_id 为空 —— 联表键断链" % (bucket, key))

    def test_plan_ids_pass_p2_validator(self):
        """用**二期的**校验器反过来验三期的 plan_id —— 跨期最硬的一条。"""
        validate = BRAIN.ids.validate_id
        seen = {str(s.get("plan_id") or "") for s in self.planning["steps"]}
        for bucket in ("observations", "actions", "evidence_refs", "decisions",
                       "predictions"):
            for r in self.ctx.get(bucket) or []:
                if r.get("plan_id"):
                    seen.add(str(r["plan_id"]))
        self.assertTrue(seen)
        for pid in sorted(seen):
            self.assertTrue(validate("plan", pid),
                            "%s 不符合二期 ID 规范 PLN-YYYYMMDD-XXXX" % pid)

    def test_snapshot_and_project_match_across_phases(self):
        self.assertEqual(self.ctx["project_id"], self.planning["project_id"])
        self.assertEqual(self.ctx["snapshot_id"], self.planning["snapshot_id"])
        self.assertEqual(self.report["summary"]["project_id"], PID)
        self.assertEqual(self.report["summary"]["snapshot_id"], SID)

    # ── 2. 对齐报告 ──
    def test_alignment_has_no_errors_on_joint_fixture(self):
        self.assertTrue(self.report["ok"], self.report["errors"])

    def test_plans_a_and_b_are_linked_c(self):
        linked = self.report["summary"]["plan_ids_linked"]
        self.assertIn(PLAN_A, linked)
        self.assertIn(PLAN_B, linked)
        self.assertNotIn(PLAN_C, linked)          # 夹具里没有 C 批的任何消息
        codes = {i["code"]: i for i in self.report["items"]}
        self.assertIn("plan_link_unverified", codes)
        self.assertIn(PLAN_C, codes["plan_link_unverified"]["message"])

    def test_as_of_mismatch_flagged(self):
        """计划快照 2026-09-28 08:30 比数据截止 2026-09-26 09:08 晚约 47 小时。"""
        codes = {i["code"]: i for i in self.report["items"]}
        self.assertIn("as_of_mismatch", codes)
        self.assertEqual(codes["as_of_mismatch"]["severity"], SEV_WARN)

    def test_time_format_tolerance_is_reported_not_fatal(self):
        codes = {i["code"]: i for i in self.report["items"]}
        self.assertIn("time_format_non_iso", codes)
        self.assertEqual(codes["time_format_non_iso"]["severity"], SEV_INFO)
        self.assertTrue(self.report["ok"])        # info 不阻断

    def test_unified_fields_incomplete_not_flagged(self):
        """补齐之后不应再出现这个告警（回归探针）。"""
        codes = [i["code"] for i in self.report["items"]]
        self.assertNotIn("unified_fields_incomplete", codes)

    # ── 3. data_as_of 口径（本次修掉的缺陷）──
    def test_data_as_of_is_capture_cutoff_not_now(self):
        # 二期存储层保留来源原始写法（空格），所以原样比对空格形态
        self.assertEqual(self.ctx["data_as_of"], DATA_AS_OF)
        self.assertEqual(ALIGN.normalize_time(self.ctx["data_as_of"]), DATA_AS_OF_ISO)
        self.assertNotEqual(self.ctx["data_as_of"], self.ctx["generated_at"])

    def test_data_as_of_is_not_in_the_future(self):
        """夹具里有「下周三到货」这类**将来**的业务日期，不得把它们当成数据截止。"""
        cutoff = ALIGN.parse_time(self.ctx["data_as_of"])
        gen = ALIGN.parse_time(self.ctx["generated_at"])
        self.assertLessEqual(cutoff, gen)
        self.assertLess(cutoff.date(), _dt.date(2026, 9, 28))

    # ── 4. 冲突 / 时效 / 缺口透传 ──
    def test_conflict_survives_the_crossing(self):
        notes = ALIGN.read_context(self.ctx)
        self.assertEqual(len(notes["unresolved_conflicts"]), 1)
        c = notes["unresolved_conflicts"][0]
        self.assertEqual(c["aspect_key"], CONFLICT_ASPECT)
        self.assertEqual(sorted(c["values"]), CONFLICT_VALUES)
        self.assertFalse(c["resolved"])           # 二期不替人裁决

    def test_stale_source_becomes_caveat(self):
        self.assertEqual(self.ctx["source_freshness"][0]["source_table"], "供应商邮件")
        self.assertTrue(any(s["stale"] for s in self.ctx["source_freshness"]))

    def test_gaps_from_p2_are_read_not_invented(self):
        codes = {g["code"] for g in self.ctx["missing_info"]}
        self.assertIn("unresolved_conflict", codes)
        self.assertIn("stale_source", codes)

    def test_relative_date_resolved_against_message_time(self):
        """「下周三」相对消息时间 2026-09-24（周四）→ 2026-09-30。"""
        act = next(a for a in self.ctx["actions"] if a["title"] == "A 批物料到场并检验")
        self.assertEqual(act["due_date"], ACTION_DUE)
        self.assertEqual(act["plan_id"], PLAN_A)

    def test_evidence_inherits_plan_id_from_action(self):
        """`add_evidence` 曾经把 plan_id 丢了；这里断言它跟随所指向的行动。"""
        ev = next(e for e in self.ctx["evidence_refs"]
                  if e["claim_type"] == "in_transit")
        self.assertEqual(ev["plan_id"], PLAN_A)
        self.assertTrue(ev["action_id"].startswith("ACT-"))

    # ── 5. 预测：人 vs 模型 ──
    def test_human_prediction_is_carried_with_source_tag(self):
        human = ALIGN.human_prediction_records(self.ctx)
        self.assertEqual(len(human), 1)
        h = human[0]
        self.assertEqual(h["prediction_source"], S.PREDICTION_SOURCE_HUMAN)
        self.assertEqual(h["predicted_date"], HUMAN_PRED_DATE)
        self.assertEqual(h["predicted_at"], HUMAN_PRED_AT)
        self.assertTrue(h["source_ref"].startswith("PRD-"))   # 指回二期 memory_id
        self.assertEqual(h["confidence_source"], S.PREDICTION_SOURCE_HUMAN)
        self.assertEqual(h["rules_version"], "")              # 人的判断没有规则版本

    def test_human_prediction_respects_as_of_cutoff(self):
        early = ALIGN.human_prediction_records(self.ctx, as_of="2026-09-24T00:00:00")
        self.assertEqual(early, [])
        late = ALIGN.human_prediction_records(self.ctx, as_of="2026-09-26T00:00:00")
        self.assertEqual(len(late), 1)

    def test_undated_human_prediction_gets_no_date(self):
        ctx = {"contract_version": S.BRAIN_CONTRACT_VERSION, "predictions": [
            {"memory_id": "PRD-20260101-aaaa", "kind": "prediction",
             "captured_at": "2026-09-25T10:00:00", "value": "快了",
             "project_id": PID}]}
        recs = ALIGN.human_prediction_records(ctx)
        self.assertEqual(len(recs), 1)
        self.assertIsNone(recs[0]["predicted_date"])
        self.assertEqual(recs[0]["undetermined_reasons"][0]["code"],
                         "human_prediction_undated")

    def test_merge_keeps_sources_apart(self):
        model = [{"prediction_id": "PDC-1", "plan_id": PLAN_A,
                  "predicted_at": "2026-09-26T09:00:00",
                  "predicted_date": "2026-10-01",
                  "prediction_source": S.PREDICTION_SOURCE_MODEL}]
        human = ALIGN.human_prediction_records(self.ctx)
        merged = ALIGN.merge_prediction_records(model, human)
        self.assertEqual(merged["split"]["counts"],
                         {"human": 1, "model": 1, "total": 2})
        # 两条都留着 —— 同一个计划上「人算的」与「模型算的」不互相覆盖
        self.assertEqual(len(merged["records"]), 2)
        self.assertEqual([r["predicted_date"] for r in merged["records"]],
                         [HUMAN_PRED_DATE, "2026-10-01"])   # 人的预测时点更早
        self.assertEqual([r["prediction_source"] for r in merged["records"]],
                         [S.PREDICTION_SOURCE_HUMAN, S.PREDICTION_SOURCE_MODEL])

    def test_backtest_splits_by_source(self):
        from application.decision_support.backtest import backtest
        human = ALIGN.human_prediction_records(self.ctx)
        rep = backtest(human, samples=[], baseline_samples=[],
                       as_of="2026-09-28T09:00:00")
        self.assertEqual(rep["by_source"]["human"]["n_records"], 1)
        self.assertEqual(rep["by_source"]["model"]["n_records"], 0)
        self.assertIn("不合并", rep["by_source"]["note"])

    # ── 6. 决策守卫 ──
    def test_analyze_references_only_p2_decision_ids(self):
        ds = DecisionSupport(load(PLANNING), context=self.ctx)
        r = ds.analyze()
        guard = ALIGN.check_no_authored_decision_id(
            r, ALIGN.read_context(self.ctx)["decision_ids"])
        self.assertTrue(guard["ok"], guard["authored"])

    def test_authored_decision_id_is_caught(self):
        bad = {"scenarios": [{"required_approvals": [],
                              "decision_id": "DEC-20260928-9999"}]}
        guard = ALIGN.check_no_authored_decision_id(bad, set())
        self.assertFalse(guard["ok"])
        self.assertEqual(guard["authored"][0]["value"], "DEC-20260928-9999")

    # ── 7. analyze() 跨期产物 ──
    def test_analyze_carries_alignment_and_unified(self):
        ds = DecisionSupport(load(PLANNING), context=self.ctx)
        r = ds.analyze()
        self.assertEqual(sorted(r["unified"]), sorted(ALIGN.UNIFIED_FIELDS))
        self.assertEqual(r["unified"]["project_id"], PID)
        self.assertEqual(r["unified"]["snapshot_id"], SID)
        self.assertEqual(r["unified"]["plan_id"], PLAN_A)
        self.assertTrue(r["unified"]["run_id"].startswith("RUN-"))
        self.assertEqual(r["alignment"]["summary"]["context_version"],
                         self.ctx["version"])
        self.assertEqual(len(r["human_predictions"]), 1)
        self.assertEqual(r["execution_gate"]["ds_writes_business_tables"], False)

    def test_phase3_output_carries_no_memory_body(self):
        """三期的产出里不得出现二期的**记忆原文** —— 事实的家只能在二期。

        判据是 `analyze()` 的完整 JSON：出现 observation 的原话即为复制事实。
        人的预测（`human_predictions[].text`）是例外且必须有：复盘时
        「他当时到底怎么说的」不能靠 id 猜，那条记录本身带着 `source_ref` 指回二期。
        """
        ds = DecisionSupport(load(PLANNING), context=self.ctx)
        blob = json.dumps(ds.analyze(), ensure_ascii=False)
        self.assertNotIn(OBS_TEXT, blob)
        self.assertNotIn("供应商称 A 批物料下周三到货", blob)
        self.assertIn(S.PREDICTION_SOURCE_HUMAN, blob)      # 人的预测是带进来的
        self.assertIn("PRD-", blob)                         # 以 memory_id 指回二期

    def test_conflicts_are_slimmed_to_references(self):
        view = ALIGN.read_context(self.ctx)
        c = view["unresolved_conflicts"][0]
        self.assertEqual(sorted(c["values"]), CONFLICT_VALUES)
        self.assertTrue(c["member_ids"])
        for m in c["members"]:
            self.assertNotIn("text", m)          # 成员原文不带过来
            self.assertIn("memory_id", m)

    def test_context_notes_hold_no_memory_text(self):
        from application.decision_support.ports import context_notes
        blob = json.dumps(context_notes(self.ctx), ensure_ascii=False)
        for text in (OBS_TEXT, "测试台优先给 A 批使用"):
            self.assertNotIn(text, blob)
        self.assertTrue(json.loads(blob)["evidence_ids"])

    def test_alignment_does_not_write_any_business_row(self):
        """跨期只读链路跑完，二期存储行数一行不变。"""
        brain = BRAIN.ProjectBrain(root=self.tmp)
        before = {t: len(brain.store.list_rows(t)) for t in BRAIN.schema.TABLE_NAMES}
        ctx = brain.context(PID, SID)
        ALIGN.check_alignment(ctx, load(PLANNING))
        ALIGN.human_prediction_records(ctx)
        DecisionSupport(load(PLANNING), context=ctx).analyze()
        after = {t: len(brain.store.list_rows(t)) for t in BRAIN.schema.TABLE_NAMES}
        self.assertEqual(before, after)

    def test_context_version_is_stable_across_reads(self):
        brain = BRAIN.ProjectBrain(root=self.tmp)
        v1 = brain.context(PID, SID)["version"]
        v2 = brain.context(PID, SID)["version"]
        self.assertEqual(v1, v2)
        self.assertEqual(ALIGN.compare_versions(v1, v2)["code"], "version_match")
        self.assertEqual(ALIGN.compare_versions(v1, v2 + 1)["code"], "version_stale")


class AlignmentRulesTest(unittest.TestCase):
    """不依赖二期：对齐层自身的规则与判定。"""

    def setUp(self):
        self.base_ctx = load(MOCK_CONTEXT)
        self.planning = load(PLANNING)

    # ── 统一字段与 ID ──
    def test_make_unified_key_set_is_exact(self):
        u = ALIGN.make_unified(project_id=PID)
        self.assertEqual(tuple(u), ALIGN.UNIFIED_FIELDS)
        self.assertEqual(len(u), 8)
        self.assertTrue(u["run_id"].startswith("RUN-"))
        self.assertTrue(ALIGN.is_valid_id(u["run_id"], "RUN"))

    def test_new_run_id_format(self):
        rid = ALIGN.new_run_id(_dt.datetime(2026, 9, 28, 8, 30), seq=7)
        self.assertEqual(rid, "RUN-20260928-0007")
        self.assertTrue(ALIGN.is_valid_id(rid))

    def test_is_valid_id_rejects_short_tail(self):
        self.assertFalse(ALIGN.is_valid_id("PLN-20260928-A"))
        self.assertFalse(ALIGN.is_valid_id("PLN-20260928-A001", "SNP"))
        self.assertFalse(ALIGN.is_valid_id("XXX-20260928-A001"))
        self.assertTrue(ALIGN.is_valid_id("PLN-20260928-A001", "PLN"))

    # ── 时间 ──
    def test_parse_time_accepts_both_formats(self):
        space = ALIGN.parse_time("2026-09-24 09:35:00")
        iso = ALIGN.parse_time("2026-09-26T10:59:03")
        self.assertEqual(space, _dt.datetime(2026, 9, 24, 9, 35, 0))
        self.assertEqual(iso, _dt.datetime(2026, 9, 26, 10, 59, 3))

    def test_date_only_resolves_to_0900_not_midnight(self):
        """纯日期若按 00:00 算，会把条件放行时间提前一整个工作日。"""
        dt = ALIGN.parse_time("2026-09-30")
        self.assertEqual(dt, _dt.datetime(2026, 9, 30, ALIGN.DATE_ONLY_HOUR, 0))
        self.assertNotEqual(dt.hour, 0)

    def test_parse_time_returns_none_not_a_guess(self):
        for bad in ("", None, "下周三", "2026-13-45", "昨天"):
            self.assertIsNone(ALIGN.parse_time(bad), bad)

    def test_normalize_time(self):
        self.assertEqual(ALIGN.normalize_time("2026-09-24 09:35:00"),
                         "2026-09-24T09:35:00")
        self.assertEqual(ALIGN.normalize_time("乱写"), "")

    def test_is_iso_t(self):
        self.assertTrue(ALIGN.is_iso_t("2026-09-26T10:59:03"))
        self.assertFalse(ALIGN.is_iso_t("2026-09-24 09:35:00"))
        self.assertTrue(ALIGN.is_iso_t(""))

    # ── 错误路径 ──
    def test_context_absent_is_error(self):
        rep = ALIGN.check_alignment({}, self.planning)
        self.assertFalse(rep["ok"])
        self.assertEqual(rep["errors"][0]["code"], "context_absent")

    def test_context_absent_can_be_downgraded(self):
        rep = ALIGN.check_alignment({}, self.planning, require_context=False)
        self.assertTrue(rep["ok"])

    def test_contract_version_mismatch_is_error(self):
        ctx = dict(self.base_ctx, contract_version="project-brain-v2")
        rep = ALIGN.check_alignment(ctx, self.planning)
        self.assertIn("contract_version_mismatch",
                      [i["code"] for i in rep["errors"]])

    def test_project_id_mismatch_is_error(self):
        ctx = dict(self.base_ctx, project_id="PRJ-20260101-0001")
        rep = ALIGN.check_alignment(ctx, self.planning)
        self.assertIn("project_id_mismatch", [i["code"] for i in rep["errors"]])

    def test_snapshot_id_mismatch_is_error(self):
        ctx = dict(self.base_ctx, snapshot_id="SNP-20260901-0001")
        rep = ALIGN.check_alignment(ctx, self.planning)
        self.assertIn("snapshot_id_mismatch", [i["code"] for i in rep["errors"]])

    def test_plan_id_invalid_format_is_error(self):
        planning = json.loads(json.dumps(self.planning))
        planning["steps"][0]["plan_id"] = "PLN-20260928-A"      # 尾码 1 位
        rep = ALIGN.check_alignment(self.base_ctx, planning)
        self.assertIn("plan_id_invalid_format", [i["code"] for i in rep["errors"]])

    def test_alignment_ok_on_captured_sample(self):
        """把计划限定为 A/B 两批（捕获样本里有对应记忆）→ 应无 error、无 warning。"""
        planning = json.loads(json.dumps(self.planning))
        planning["steps"] = [s for s in planning["steps"]
                             if s.get("plan_id") != PLAN_C]
        rep = ALIGN.check_alignment(self.base_ctx, planning,
                                    as_of_tolerance_hours=72)
        self.assertTrue(rep["ok"], rep["errors"])
        self.assertNotIn("as_of_mismatch", [i["code"] for i in rep["warnings"]])
        codes = [i["code"] for i in rep["items"]]
        self.assertIn("plan_link_ok", codes)
        self.assertIn("align_ok", codes)          # info 不阻断，但仍会报「对齐」

    # ── 版本比对 ──
    def test_compare_versions(self):
        self.assertTrue(ALIGN.compare_versions(16, 16)["ok"])
        bad = ALIGN.compare_versions(16, 17)
        self.assertFalse(bad["ok"])
        self.assertEqual(bad["code"], "version_stale")

    # ── 预测来源 ──
    def test_prediction_source_defaults_and_whitelist(self):
        self.assertEqual(ALIGN.prediction_source_of({}), S.PREDICTION_SOURCE_MODEL)
        self.assertEqual(ALIGN.prediction_source_of({"prediction_source": "瞎写"}),
                         S.PREDICTION_SOURCE_MODEL)
        self.assertEqual(ALIGN.prediction_source_of(
            {"prediction_source": "human"}), S.PREDICTION_SOURCE_HUMAN)

    def test_split_counts(self):
        recs = [{"prediction_source": "human"}, {"prediction_source": "model"},
                {"prediction_source": "model"}, {}]
        out = ALIGN.split_prediction_records(recs)
        self.assertEqual(out["counts"], {"human": 1, "model": 3, "total": 4})


class ContractArtifactTest(unittest.TestCase):
    """文档 / 机器可读映射 / 样例三者不得各说各话。"""

    def test_field_map_json_matches_code(self):
        on_disk = json.load(open(FIELD_MAP_JSON, encoding="utf-8"))
        self.assertEqual(on_disk, json.loads(ALIGN.field_map_json()))

    def test_field_map_covers_all_unified_fields(self):
        fields = {f["field"] for f in ALIGN.FIELD_MAP}
        for f in ALIGN.UNIFIED_FIELDS:
            self.assertIn(f, fields, "字段映射表缺 %s" % f)

    def test_semantic_splits_cover_the_dangerous_terms(self):
        terms = {s["term"] for s in ALIGN.SEMANTIC_SPLITS}
        for t in ("预测", "决策", "状态", "as_of / data_as_of", "version"):
            self.assertIn(t, terms)

    def test_every_semantic_split_has_ruling_and_guard(self):
        for s in ALIGN.SEMANTIC_SPLITS:
            for k in ("term", "brain_side", "ds_side", "ruling", "guard"):
                self.assertTrue(s.get(k), "%s 缺 %s" % (s.get("term"), k))

    def test_alignment_doc_exists_and_names_the_codes(self):
        path = os.path.join(ROOT, "docs", "contracts", "alignment-p2-p3-v1.md")
        self.assertTrue(os.path.exists(path))
        text = open(path, encoding="utf-8").read()
        for code in ("context_absent", "contract_version_mismatch",
                     "project_id_mismatch", "snapshot_id_mismatch",
                     "as_of_mismatch", "plan_id_invalid_format",
                     "plan_link_unverified", "time_format_non_iso",
                     "unified_fields_incomplete"):
            self.assertIn(code, text, "对齐文档没写 %s" % code)
        self.assertIn(ALIGN.ALIGN_VERSION, text)

    def test_mock_sample_is_a_real_capture_not_handwritten(self):
        """mock 必须是真实二期回放的产物：结构上带着二期的全部输出键。"""
        ctx = load(MOCK_CONTEXT)
        for k in ("contract_version", "project_id", "snapshot_id", "version",
                  "data_as_of", "generated_at", "source_freshness", "conflicts",
                  "missing_info", "answers", "counts", "actions", "evidence_refs"):
            self.assertIn(k, ctx)
        self.assertIn("真实", ctx.get("_note", ""))

    def test_execution_gate_declares_no_writes(self):
        self.assertFalse(ALIGN.EXECUTION_GATE["ds_writes_business_tables"])
        self.assertEqual(ALIGN.EXECUTION_GATE["gate_module"],
                         "application.authorization")
        self.assertIn("version", ALIGN.EXECUTION_GATE["pre_execution"])


if __name__ == "__main__":
    unittest.main()
