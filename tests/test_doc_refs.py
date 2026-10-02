# -*- coding: utf-8 -*-
"""test_doc_refs.py — 文档里的 `file::symbol` 引用必须可核对。

## 为什么值得一条自动守卫

`docs/invariant-coverage.md` 的填表说明里写着它的验证方法：

> 把文件里所有 `` `path::symbol` `` 抽出来，逐个 `os.path.isfile`
> + 在文件正文中检索符号末段。

**这件事已经手工做过两次，两次都抓到同一类错误**：写引用时只写了文件名、
漏了目录前缀（第一次：`project_brain/memory.py`、`evidence_ledger.py`；
第二次：批次 1 更新矩阵时又写了 `evidence_ledger.py::is_arrival_evidence`
与 `decision_support/graph.py::DependencyGraph`）。

靠人记得回验 = 迟早不回验。这条守卫把它固定下来：
**文档里的每一个引用指向的必须是一个真实存在的、且确实包含该符号的文件。**

## 解析规则（三级，逐级回退）

1. 相对**仓库根**（`application/project_brain/context.py`）；
2. 相对**文档所在目录**（`docs/contracts/foo.md` 里写 `bar.md`）；
3. **全仓唯一同名文件**。★ 只在同名文件**唯一**时才接受 ——
   否则 `service.py::xxx` 这种引用在仓库里有 3 个候选、指向不明，
   必须写成全路径。这一条正是「漏目录前缀」的自动检出点。

## 本文件不检查什么

  · 不检查引用的**语义**是否正确（写对了文件但对错了概念，机器判不了）；
  · 不检查 `§`、纯文字描述、外部 URL —— 只认 `` `path::symbol` `` 这一种形态。
"""

import glob
import os
import re
import sys
import unittest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

# `path::symbol` —— 文件后缀白名单，避免把 `foo.bar::baz` 之类误当引用
REF_RE = re.compile(
    r"`([A-Za-z0-9_./\-]+\.(?:py|md|yaml|yml|json|html))::([A-Za-z0-9_:.]+)`")

SUFFIX_RE = re.compile(r"\.(?:py|md|yaml|yml|json|html)$")


def _tracked_files():
    """仓库里的**跟踪文件**清单（与守卫同一来源，避免扫到 .venv / data 产物）。"""
    out = []
    for root, dirs, names in os.walk(HERE):
        dirs[:] = [d for d in dirs
                   if d not in (".git", ".venv", "__pycache__", "node_modules",
                                "data", "ux-shots", "probes", "copies")]
        for n in names:
            out.append(os.path.relpath(os.path.join(root, n), HERE).replace("\\", "/"))
    return out


class DocReferenceGuardTest(unittest.TestCase):
    """文档引用可核对性（全仓库 `docs/**/*.md`）。"""

    @classmethod
    def setUpClass(cls):
        cls.files = _tracked_files()
        cls.by_base = {}
        for rel in cls.files:
            cls.by_base.setdefault(os.path.basename(rel), []).append(rel)

    # ── 解析器（单独测，保证守卫自己不会静默失效）──────────────
    def resolve(self, doc_path: str, ref: str):
        """把引用解析成仓库内真实文件路径；解析不了返回 None，歧义返回 'AMBIGUOUS'。"""
        doc_dir = os.path.dirname(doc_path)
        for cand in (os.path.join(HERE, ref), os.path.join(HERE, doc_dir, ref)):
            if os.path.isfile(cand):
                return os.path.relpath(cand, HERE).replace("\\", "/")
        hits = self.by_base.get(os.path.basename(ref), [])
        if len(hits) == 1:
            return hits[0]
        return "AMBIGUOUS" if len(hits) > 1 else None

    def test_解析器自身行为(self):
        """解析器的三种结果都要能复现 —— 否则下面那条断言可能是「永远绿」。"""
        doc = "docs/invariant-coverage.md"
        self.assertEqual(self.resolve(doc, "application/contracts.py"),
                         "application/contracts.py")
        then = self.resolve(doc, "invariant-coverage.md")
        self.assertEqual(then, "docs/invariant-coverage.md")   # 相对文档目录
        self.assertEqual(self.resolve(doc, "application/exec_plane/service.py"),
                         "application/exec_plane/service.py")
        self.assertEqual(self.resolve(doc, "service.py"), "AMBIGUOUS",
                         "同名文件有多个时必须判歧义（这正是漏目录前缀的检出点）")
        self.assertIsNone(self.resolve(doc, "no_such_file_xyz.py"))
        # 反向自检：正则确实能抓到引用形态
        self.assertEqual(REF_RE.findall("见 `a/b.py::sym`"),
                         [("a/b.py", "sym")])

    # ── 正式守卫 ─────────────────────────────────────────────
    def test_文档引用全部可核对(self):
        """★ 主断言：每一条 `` `path::symbol` `` 都必须落到真实文件且符号在其中。"""
        docs = sorted(glob.glob(os.path.join(HERE, "docs", "**", "*.md"),
                                recursive=True))
        self.assertTrue(docs, "没扫到任何文档 —— 守卫失效了")
        total = 0
        bad = []
        for full in docs:
            rel_doc = os.path.relpath(full, HERE).replace("\\", "/")
            with open(full, "r", encoding="utf-8", errors="ignore") as fh:
                src = fh.read()
            seen = set()
            for ref, sym in REF_RE.findall(src):
                if (ref, sym) in seen:
                    continue
                seen.add((ref, sym))
                total += 1
                where = self.resolve(rel_doc, ref)
                if where == "AMBIGUOUS":
                    bad.append("%s：`%s::%s` —— 同名文件有多个，必须写全路径"
                               % (rel_doc, ref, sym))
                    continue
                if where is None:
                    bad.append("%s：`%s::%s` —— 文件不存在" % (rel_doc, ref, sym))
                    continue
                leaf = sym.split("::")[-1].split(".")[-1]
                with open(os.path.join(HERE, where), "r", encoding="utf-8",
                          errors="ignore") as fh:
                    body = fh.read()
                if not leaf or leaf not in body:
                    bad.append("%s：`%s::%s` —— %s 里找不到符号 %r"
                               % (rel_doc, ref, sym, where, leaf))
        self.assertGreater(total, 60, "引用数骤降，可能正则或扫描范围坏了")
        self.assertEqual(bad, [], "文档引用无法核对（%d/%d 条）：\n  %s"
                         % (len(bad), total, "\n  ".join(bad)))

    def test_引用的文件不得落在被忽略目录(self):
        """引用的目标应当是可提交的源码，而不是 `.venv` / `data` 里的产物。"""
        skip = ("/.venv/", "/data/", "/__pycache__/", "/node_modules/")
        docs = sorted(glob.glob(os.path.join(HERE, "docs", "**", "*.md"),
                                recursive=True))
        bad = []
        for full in docs:
            rel_doc = os.path.relpath(full, HERE).replace("\\", "/")
            with open(full, "r", encoding="utf-8", errors="ignore") as fh:
                src = fh.read()
            for ref, sym in set(REF_RE.findall(src)):
                if any(s in "/" + ref for s in skip):
                    bad.append("%s：`%s::%s`" % (rel_doc, ref, sym))
        self.assertEqual(bad, [], "引用指向了被忽略目录：%s" % bad)


if __name__ == "__main__":
    unittest.main(verbosity=2)
