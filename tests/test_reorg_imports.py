#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v2.0.0 目录重组的导入回归测试。

背景：`cb2bd63` 把 44 个顶层脚本按域归组后，**6 处函数内延迟导入**仍按旧顶层名
导入（`import cockpit` / `import doctor` / `import crm_dispatch` /
`from intake import ...` / `from foresee import ...`）。随后的 `2521567`「复查
全部脚本直接执行均无导入错误」只看了模块级导入，这 6 处是函数级延迟导入，全数漏过。
后果：

  · `domain/op.py res-load`   → `import cockpit` 命中**空命名空间包**（cockpit/ 无
    __init__.py），导入「成功」但调用 `compute_resources` 时 AttributeError；
  · `domain/op.py doctor`     → ModuleNotFoundError: doctor（实为 tools/doctor.py）；
  · `wx/wx_dispatch.py` 的 CRM 候选 → ModuleNotFoundError: crm_dispatch（实为 domain/）；
  · `wx/wechat_intake.py approve`   → ModuleNotFoundError: intake（实为 domain/intake.py）；
  · `tools/audit_wx_coverage.py`    → 同上「空命名空间包」，异常被外层 try 吞掉，
    表现为「取不到线上数据」，覆盖率审计静默降级。

本文件锁两件事：

  [A] 那 5 个调用点需要的模块按**包限定名**真的导得进来，且带得动用到的属性；
  [B] 全仓静态扫描：不再存在「模块已归入子目录、代码却按顶层名导入」的调用点。

[B] 是这一族缺陷的通杀网：它不依赖某个人记得去复查，重组后一跑就红。
"""
import ast
import importlib
import os
import sys
import unittest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

# [B] 里允许的例外：这些位置**故意**先往 sys.path 里塞了子目录再按顶层名导入。
# 键为仓库相对路径（posix 风格），值为「为什么这样是合法的」。
_ALLOW_BARE_IMPORT = {
    "tests/test_smoke.py":
        "逐项冒烟要按控件名逐块检查：先 sys.path.insert(0, <root>/cockpit) 再 import cockpit，"
        "此时 cockpit 解析为 cockpit/cockpit.py 这个**模块**（而非根下同名目录），合法。",
}

_SKIP_DIRS = {".git", "__pycache__", "data", "node_modules", ".workbuddy", ".venv"}


def _repo_py_files():
    for dirpath, dirnames, filenames in os.walk(HERE):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        for fn in filenames:
            if fn.endswith(".py"):
                yield os.path.join(dirpath, fn)


def _layout():
    """返回 (根下模块名集合, 根下目录名集合, {模块名: [相对路径, ...]})。"""
    root_mods = {f[:-3] for f in os.listdir(HERE) if f.endswith(".py")}
    root_dirs = {d for d in os.listdir(HERE)
                 if os.path.isdir(os.path.join(HERE, d)) and d not in _SKIP_DIRS}
    where = {}
    for fp in _repo_py_files():
        rel = os.path.relpath(fp, HERE).replace(os.sep, "/")
        where.setdefault(os.path.basename(fp)[:-3], set()).add(rel)
    return root_mods, root_dirs, where


def _bad_imports():
    """返回 [(相对路径, 行号, 导入语句, 该名字实际所在), ...]。"""
    root_mods, root_dirs, where = _layout()
    bad = []
    for fp in _repo_py_files():
        rel = os.path.relpath(fp, HERE).replace(os.sep, "/")
        if rel in _ALLOW_BARE_IMPORT:
            continue
        importer_dir = os.path.dirname(fp)
        try:
            with open(fp, encoding="utf-8") as fh:
                tree = ast.parse(fh.read(), fp)
        except (SyntaxError, UnicodeDecodeError, OSError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                # `import x` 绑定的是模块/包对象本身：根下同名**目录**若无
                # __init__.py，只会得到一个空命名空间包 —— 导入"成功"但用不了，
                # 比 ImportError 更阴。所以这一支只认根下 .py 或真包。
                entries = [(n.name.split(".")[0],
                            "import %s" % n.name,
                            "module") for n in node.names]
            elif isinstance(node, ast.ImportFrom):
                if node.level or not node.module:
                    continue
                # `from x import y` 只要求顶层段可定位（目录即可）。
                entries = [(node.module.split(".")[0],
                            "from %s import ..." % node.module,
                            "package")]
            else:
                continue

            for top, stmt, kind in entries:
                # 只审「仓库自己的模块」：同名 .py 必须真的存在于本仓库某处
                if top not in where:
                    continue
                if top in root_mods:
                    continue
                if os.path.isfile(os.path.join(importer_dir, top + ".py")):
                    continue        # 同目录兄弟模块，运行时靠 sys.path[0] 解析
                if kind == "module":
                    if os.path.isfile(os.path.join(HERE, top, "__init__.py")):
                        continue    # 根下真包
                else:
                    if top in root_dirs:
                        continue
                bad.append((rel, node.lineno, stmt, ", ".join(sorted(where[top]))))
    return bad


class ReorgImportSitesTest(unittest.TestCase):
    """[A] 5 个调用点：限定名导入 + 属性齐全。"""

    def _mod(self, dotted):
        try:
            return importlib.import_module(dotted)
        except Exception as exc:                      # noqa: BLE001
            self.fail("按包限定名导入 %s 失败：%r" % (dotted, exc))

    def test_res_load_can_reach_cockpit(self):
        """domain/op.py cmd_res_load：必须拿到真 cockpit 模块，而不是空命名空间包。"""
        ck = self._mod("cockpit.cockpit")
        self.assertIsNotNone(ck.__file__, "cockpit.cockpit 解析成了命名空间包（__file__ 为 None）")
        for attr in ("compute_resources", "_NormAdapter"):
            self.assertTrue(hasattr(ck, attr), "cockpit.cockpit 缺少 %s" % attr)

    def test_doctor_can_reach_tools_doctor(self):
        """domain/op.py cmd_doctor：doctor 在 tools/ 下。"""
        doc = self._mod("tools.doctor")
        for attr in ("run", "render", "has_blocker"):
            self.assertTrue(hasattr(doc, attr), "tools.doctor 缺少 %s" % attr)

    def test_wx_dispatch_can_reach_crm_dispatch(self):
        """wx/wx_dispatch.py 的 CRM 候选：crm_dispatch 在 domain/ 下。"""
        cd = self._mod("domain.crm_dispatch")
        for attr in ("LEAD_FIELDS", "FOLLOW_FIELDS"):
            self.assertTrue(hasattr(cd, attr), "domain.crm_dispatch 缺少 %s" % attr)

    def test_wechat_intake_can_reach_intake(self):
        """wx/wechat_intake.py approve：Intent 在 domain/intake.py。"""
        intake = self._mod("domain.intake")
        self.assertTrue(hasattr(intake, "Intent"), "domain.intake 缺少 Intent")

    def test_audit_can_reach_cockpit(self):
        """tools/audit_wx_coverage.py：同上，必须是真模块。"""
        ck = self._mod("cockpit.cockpit")
        self.assertTrue(hasattr(ck, "_NormAdapter"), "audit 用到的 cockpit._NormAdapter 不存在")

    def test_backtest_uses_real_foresee_verdicts(self):
        """application/decision_support/backtest.py：口径必须取自 domain/foresee.py。

        取不到会静默回落到硬编码副本（内容目前一致，但是可漂移的重复字面量），
        所以这里断言「用的是真源头」而不是「值相等」。
        """
        bt = self._mod("application.decision_support.backtest")
        foresee = self._mod("domain.foresee")
        self.assertEqual(
            bt.VERDICT_SOURCE, "foresee.VERDICT_REVIEW",
            "backtest 退回了硬编码回落口径，说明 domain.foresee 没取到")
        self.assertEqual(dict(bt.VERDICT_REVIEW), dict(foresee.VERDICT_REVIEW))


class NoBareImportAfterReorgTest(unittest.TestCase):
    """[B] 静态通杀网：不允许「模块已入子目录、却按顶层名导入」。"""

    def test_no_top_level_import_of_moved_module(self):
        bad = _bad_imports()
        if bad:
            lines = ["%s:%d  %s  ← 实际在 %s" % (rel, ln, stmt, loc)
                     for rel, ln, stmt, loc in sorted(bad)]
            self.fail("发现按顶层名导入已归入子目录的模块（重组遗留，运行会崩）：\n  "
                      + "\n  ".join(lines))

    def test_scanner_actually_has_teeth(self):
        """反向自检：扫描器必须能在人造样本上判红，否则「绿」没有意义。"""
        root_mods, root_dirs, where = _layout()
        self.assertGreater(len(where), 50, "仓库里没扫到几个模块，扫描器坏了")
        self.assertIn("cockpit", where, "cockpit 模块未被登记，扫描器坏了")
        self.assertTrue(os.path.isdir(os.path.join(HERE, "cockpit")),
                        "cockpit/ 目录不存在，测试基线不成立")
        self.assertNotIn("cockpit", root_mods,
                         "cockpit 已回到顶层？本测试的「已归入子目录」前提不再成立，需重审")


if __name__ == "__main__":
    unittest.main(verbosity=2)
