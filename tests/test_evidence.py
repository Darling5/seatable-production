#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""微信图片证据模块离线测试；不访问微信、SeaTable 或真实图片。"""
from datetime import datetime, timedelta, timezone
import json
import os
import sys
import tempfile
from pathlib import Path

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
import evidence  # noqa: E402


def test_metadata_first(tmp):
    image = tmp / "fake.jpg"
    image.write_bytes(b"not a real uploaded image")
    rec = evidence.image_metadata(image, summary="快递单号 KY123", event_id="WX-1")
    assert rec["summary"] == "快递单号 KY123"
    assert rec["event_id"] == "WX-1"
    assert rec["sha256"]
    assert image.exists()


def test_scan_rules():
    now = datetime(2026, 9, 10, tzinfo=timezone.utc)
    old = (now - timedelta(days=91)).isoformat()
    records = [
        {"evidence_id": "closed", "created_at": old, "status": "已闭环"},
        {"evidence_id": "open", "created_at": old, "status": "待处理"},
        {"evidence_id": "long", "created_at": old, "status": "已闭环", "long_term_retention": True},
        {"evidence_id": "new", "created_at": (now - timedelta(days=2)).isoformat(), "status": "已闭环"},
    ]
    got = evidence.scan_candidates(records, now=now)
    assert [r["evidence_id"] for r in got] == ["closed"]
    assert "candidate_reason" in got[0]


def test_index_and_prune_requires_yes(tmp):
    root = tmp / "wechat_intake"
    root.mkdir()
    image = root / "old.png"
    image.write_bytes(b"offline fixture")
    now = datetime.now(timezone.utc)
    rec = evidence.image_metadata(image, summary="old", status="已闭环", created_at=(now - timedelta(days=100)).isoformat())
    evidence.write_index(root, [rec])
    # dry-run is non-destructive
    got = evidence.prune(root, now=now, yes=False)
    assert len(got) == 1 and image.exists()
    assert len(evidence.read_index(root)) == 1
    # explicit confirmation removes local file and index entry
    got = evidence.prune(root, now=now, yes=True)
    assert len(got) == 1 and not image.exists()
    assert evidence.read_index(root) == []


def test_cli_scan_is_candidate_only(tmp):
    root = tmp / "root"
    root.mkdir()
    meta = root / "record.json"
    meta.write_text(json.dumps({"evidence_id": "x", "kind": "image", "created_at": "2020-01-01T00:00:00Z", "status": "done", "path": str(root / "missing.jpg")}), encoding="utf-8")
    assert evidence.main(["scan", "--root", str(root), "--json"]) == 0


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="evidence_test_") as d:
        p = Path(d)
        test_metadata_first(p)
        test_scan_rules()
        test_index_and_prune_requires_yes(p)
        test_cli_scan_is_candidate_only(p)
    print("ALL EVIDENCE TESTS PASSED")
