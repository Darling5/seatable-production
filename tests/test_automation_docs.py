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
    # evidence.py 已归入 domain/（仓库结构整理 cb2bd63），此处断言需同步跟进新路径。
    assert "每日任务不得运行 domain/evidence.py prune ... --yes" in doc
    assert "自动化只扫描和报告候选，永远不带 `--yes`" in doc


def test_summary_contract():
    doc = text("references/wx-ai-summary-prompt.md")
    require(doc, "双 Base 分流", "图片证据允许上传", "普通文件优先文本化", "目标Base", "--yes")


def test_public_docs_contract():
    # v2.0.0（906b840）把 README 重写为「门面」，证据留存细节（普通文件优先文本化 /
    # 90 天候选）下沉到 SKILL.md。契约因此分层，而不是两份文档抄同一串关键词：
    #   README    —— 对外门面：双 Base 分流 + 破坏性操作必须显式 --yes
    #   SKILL.md  —— 操作口径：分流 + 证据留存 + --yes 全套
    require(text("README.md"), "production", "tasks", "--yes")
    require(text("SKILL.md"), "production", "tasks", "普通文件", "90 天", "--yes")


def test_command_help_is_offline_and_explicit():
    proc = subprocess.run(
        [sys.executable, str(ROOT / "domain" / "evidence.py"), "prune", "--help"],
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
