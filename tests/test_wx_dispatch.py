#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""wx_dispatch 离线测试：不读取配置、不访问线上、不写业务数据。"""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
import wx_dispatch as wd  # noqa: E402


def test_explicit_production_preserves_intent():
    event = {
        "event_id": "WX-001", "source": "客户群", "text": "客户要求延期",
        "intents": [{"target": "production", "op": "update", "table": "项目",
                     "data": {"合同交期": "2026-10-01"}}],
    }
    result = wd.build_preview(event)
    assert result["candidate_count"] == 1
    c = result["candidates"][0]
    assert c["base"] == "production"
    assert c["table"] == "项目"
    assert c["data"] == {"合同交期": "2026-10-01"}
    assert c["preview_only"] is True and c["write_online"] is False


def test_task_mapping_and_unknown_fields_retained():
    event = {"event_id": "WX-002", "intents": [{"base": "tasks", "op": "append",
        "data": {"任务名称": "催交期", "owner": "张三", "dept": "采购",
                 "priority": "高", "description": "本周确认", "deadline": "2026-09-20",
                 "状态": "待办", "custom": "保留在原意图"}}]}
    c = wd.dispatch_event(event)[0]
    assert c["fields"] == {"名称": "催交期", "负责人": "张三", "部门": "采购",
                             "优先级": "高", "详情": "本周确认", "执行时间": "",
                             "计划完成时间": "2026-09-20", "状态": "待办"}
    assert c["intent"]["data"]["custom"] == "保留在原意图"


def test_dual_route_candidates_and_stable_keys():
    event = {"event_id": "WX-003", "text": "到货并提醒测试", "intents": [{
        "target": ["production", "tasks"], "op": "append",
        "table": "项目", "data": {"名称": "A", "负责人": "李四"}}]}
    a = wd.build_preview(event)
    b = wd.build_preview(json.loads(json.dumps(event, ensure_ascii=False)))
    assert [x["base"] for x in a["candidates"]] == ["production", "tasks"]
    assert [x["dedupe_key"] for x in a["candidates"]] == [x["dedupe_key"] for x in b["candidates"]]
    assert len({x["dedupe_key"] for x in a["candidates"]}) == 2


def test_unrouted_never_defaults():
    result = wd.build_preview({"event_id": "WX-004", "intents": [{"op": "log", "data": {"x": 1}}]})
    assert result["candidate_count"] == 0
    assert result["unrouted"][0]["reason"] == "缺少明确 target/base"


def test_evidence_metadata_is_reference_only(tmp_path):
    event = {"event_id": "WX-005", "evidence": [{"evidence_id": "img-1", "path": str(tmp_path / "a.jpg"),
                                                     "sha256": "abc"}],
             "intents": [{"target": "tasks", "data": {"名称": "核对图片"}}]}
    c = wd.dispatch_event(event)[0]
    assert c["evidence"][0]["evidence_id"] == "img-1"
    assert c["evidence"][0]["upload"] is False
    assert c["evidence"][0]["upload_status"] == "not_uploaded"


def test_cli_preview_stdin():
    payload = {"event_id": "WX-006", "intents": [{"target": "tasks", "data": {"name": "CLI test"}}]}
    proc = subprocess.run([sys.executable, str(HERE / "wx_dispatch.py"), "preview", "-"],
                          input=json.dumps(payload), text=True, capture_output=True, check=True)
    result = json.loads(proc.stdout)
    assert result["candidate_count"] == 1
    assert result["candidates"][0]["fields"]["名称"] == "CLI test"


if __name__ == "__main__":
    tests = [v for k, v in globals().items() if k.startswith("test_")]
    with tempfile.TemporaryDirectory() as d:
        for fn in tests:
            # tmp_path-dependent test gets a pathlib fixture manually.
            if fn.__name__ == "test_evidence_metadata_is_reference_only":
                fn(Path(d))
            else:
                fn()
    print("ALL WX DISPATCH TESTS PASSED")
