#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""离线文档契约测试：自动化模板必须保留双 Base、附件与删除保护规则。"""
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def text(rel):
    return (ROOT / rel).read_text(encoding="utf-8")


def require(doc, *needles):
    missing = [x for x in needles if x not in doc]
    assert not missing, "文档缺少关键词: " + ", ".join(missing)


def test_automation_contract():
    doc = text("automations/README.md")
    require(doc, "production", "tasks", "普通文件优先文本化", "超过 90 天", "--yes")
    assert "每日任务不得运行 evidence.py prune ... --yes" in doc
    assert "自动化只扫描和报告候选，永远不带 `--yes`" in doc


def test_summary_contract():
    doc = text("references/wx-ai-summary-prompt.md")
    require(doc, "双 Base 分流", "图片证据允许上传", "普通文件优先文本化", "目标Base", "--yes")


def test_public_docs_contract():
    for rel in ("README.md", "SKILL.md"):
        doc = text(rel)
        require(doc, "production", "tasks", "普通文件", "90 天", "--yes")


def test_command_help_is_offline_and_explicit():
    proc = subprocess.run(
        [sys.executable, str(ROOT / "evidence.py"), "prune", "--help"],
        cwd=str(ROOT), capture_output=True, text=True, check=False,
    )
    output = proc.stdout + proc.stderr
    assert proc.returncode == 0
    require(output, "--days", "--yes", "确认删除")


if __name__ == "__main__":
    test_automation_contract()
    test_summary_contract()
    test_public_docs_contract()
    test_command_help_is_offline_and_explicit()
    print("ALL AUTOMATION DOC TESTS PASSED")
