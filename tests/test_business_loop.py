# -*- coding: utf-8 -*-
import csv, json, os, subprocess, sys, tempfile, unittest
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
from application import contracts as C
from domain.order_to_cash import Service, BusinessStore, OBJECT_SPECS, build_full_scenario, handoff_suggestions


class TestBusinessLoop(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="business_loop_")
        self.svc = Service(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def full_case(self):
        result = self.svc.start_case("测试客户", "测试产品", owner="小王", source_event_id="EV-001", approved=True)
        self.assertEqual(result["status"], "created")
        return result["root_id"]

    def test_all_object_specs_and_ids(self):
        self.assertEqual(set(OBJECT_SPECS), set(C._ID_PREFIX))
        for kind in OBJECT_SPECS:
            self.assertTrue(C.validate_object_id(kind, C.new_object_id(kind)))
        self.assertFalse(C.validate_object_id("project", C.new_object_id("quote")))

    def test_contract_value_objects(self):
        e = C.EvidenceLink("EV-1", source="wechat")
        t = C.StateTransition("project", "PRJ-20260101-abcd", "lead", "opportunity")
        self.assertEqual(e.event_id, "EV-1")
        self.assertEqual(t.severity, "normal")
        with self.assertRaises(Exception):
            e.event_id = "EV-2"

    def test_scenario_is_pure_and_complete(self):
        before = list(Path(self.tmp.name).iterdir())
        scenario = build_full_scenario("客户", "产品")
        self.assertEqual(len(scenario["states"]), len(C.PROJECT_STATES))
        self.assertEqual(before, list(Path(self.tmp.name).iterdir()))

    def test_start_preview_does_not_write(self):
        result = self.svc.start_case("客户", "产品")
        self.assertEqual(result["status"], "plan")
        self.assertEqual(list(Path(self.tmp.name).iterdir()), [])

    def test_start_apply_writes_three_objects_evidence_approval_transition(self):
        result = self.svc.start_case("客户", "产品", source_event_id="EV-1", approved=True)
        self.assertEqual(len(result["objects"]), 3)
        self.assertEqual(len(self.svc.store.read("objects")), 3)
        self.assertEqual(len(self.svc.store.read("evidence")), 1)
        self.assertEqual(len(self.svc.store.read("approvals")), 1)
        self.assertEqual(len(self.svc.store.read("transitions")), 1)
        self.assertEqual(result["objects"][-1]["object_id"], result["root_id"])

    def test_start_is_idempotent(self):
        a = self.svc.start_case("客户", "产品", source_event_id="EV-1", approved=True)
        b = self.svc.start_case("客户", "产品", source_event_id="EV-1", approved=True)
        self.assertEqual(b["status"], "reused")
        self.assertEqual(a["root_id"], b["root_id"])
        self.assertEqual(len(self.svc.store.read("objects")), 3)

    def test_advance_preview_illegal_and_skip(self):
        root = self.full_case()
        p = self.svc.advance(root, "in_production", "加急预览")
        self.assertEqual(p["status"], "plan")
        self.assertEqual(p["severity"], "skip")
        self.assertEqual(self.svc.status(root)["state"], "lead")
        bad = self.svc.advance(root, "closed", "非法提前关闭")
        self.assertEqual(bad["status"], "illegal")

    def test_full_chain_materializes_expected_objects(self):
        root = self.full_case()
        for state in C.PROJECT_STATES[1:14]:
            result = self.svc.advance(root, state, "测试推进 " + state, actor="测试", approved=True)
            self.assertEqual(result["status"], "advanced")
        types = {r["object_type"] for r in self.svc.status(root)["objects"]}
        self.assertTrue(set(OBJECT_SPECS).difference({"after_sales"}).issubset(types))
        self.assertEqual(self.svc.status(root)["state"], "closed")
        self.assertTrue(self.svc.verify(root)["passed"])

    def test_after_sales_path_and_verify(self):
        root = self.full_case()
        self.svc.advance(root, "acceptance", "已验收", approved=True)
        self.svc.advance(root, "after_sales", "客户反馈问题", approved=True)
        self.assertEqual(self.svc.status(root)["state"], "after_sales")
        self.assertTrue(any(r["object_type"] == "after_sales" for r in self.svc.status(root)["objects"]))
        self.assertTrue(self.svc.verify(root)["passed"])
        self.svc.advance(root, "closed", "售后已结案", approved=True)
        self.assertEqual(self.svc.status(root)["state"], "closed")

    def test_revise_versions_keep_old(self):
        root = self.full_case()
        self.svc.advance(root, "requirement_confirming", "需求确认", approved=True)
        a = self.svc.revise(root, "requirement", "V1", "首版", approved=True)
        b = self.svc.revise(root, "requirement", "V2", "客户变更", approved=True)
        # 推进到需求状态时已自动物化 V1；两次显式修订因此产生 V2/V3，旧版全部保留。
        self.assertEqual((a["object"]["version"], b["object"]["version"]), ("2", "3"))
        self.assertEqual(b["object"]["parent_id"], a["object"]["object_id"])
        self.assertEqual(len([r for r in self.svc.status(root)["objects"] if r["object_type"] == "requirement"]), 3)

    def test_status_contains_evidence_and_next_action(self):
        root = self.full_case()
        status = self.svc.status(root)
        self.assertEqual(status["state"], "lead")
        self.assertIn("next_action", status)
        self.assertEqual(len(status["evidence"]), 1)

    def test_handoff_suggestions(self):
        for state in ("won_and_funded", "procurement", "ready_to_ship", "acceptance", "after_sales"):
            self.assertNotEqual(handoff_suggestions(state), "检查当前对象、责任人和下一步")

    def test_csv_headers_and_atomic_store(self):
        self.full_case()
        store = BusinessStore(self.tmp.name)
        for name, (_, fields) in store.TABLES.items():
            path = store.path(name)
            self.assertTrue(path.exists())
            with path.open("r", encoding="utf-8-sig", newline="") as f:
                self.assertEqual(next(csv.reader(f)), fields)
        self.assertFalse(any(p.suffix == ".tmp" for p in Path(self.tmp.name).iterdir()))


class TestBusinessLoopCli(unittest.TestCase):
    def run_cli(self, *args):
        return subprocess.run([sys.executable, str(HERE / "business_loop.py"), *args], capture_output=True, text=True, encoding="utf-8")

    def test_doctor_and_scenario(self):
        with tempfile.TemporaryDirectory() as tmp:
            doctor = self.run_cli("doctor", "--data-dir", tmp, "--json")
            self.assertEqual(doctor.returncode, 0, doctor.stderr)
            data = json.loads(doctor.stdout)
            self.assertTrue(data["passed"])
            scenario = self.run_cli("scenario", "--customer", "客户", "--product", "产品", "--json")
            self.assertEqual(scenario.returncode, 0, scenario.stderr)
            self.assertEqual(len(json.loads(scenario.stdout)["states"]), len(C.PROJECT_STATES))


if __name__ == "__main__":
    unittest.main()
