# -*- coding: utf-8 -*-
"""test_cross_layer_ids.py — 跨层 ID 形态一致性回归测试（批次 1 / G29 前置，2026-10-03）。

## 背景：一个实测出来的真缺口

`G29`（跨层 ID 一致）在覆盖矩阵里标「半闭」，缺口动作写的是
**「随 G14–G28 逐层建立，每层接入时补跨层 ID 一致性用例」**。
批次 1 建 `PO → GR → 付款单` 这条线时，**第一次真的去核**就核出了问题：

    12 类业务对象（`application/contracts.py::_ID_PREFIX`）里，
    **11 类被跨层校验器拒绝**（`application/decision_support/alignment.py::is_valid_id`）：

      · 3 类因前缀**只有 2 个字母**（`MO` / `PO` / `AS`）——
        `ID_RE` 要求 `[A-Z]{3}`，两段形态直接不匹配；
      · 8 类因**根本没登记**进 `KNOWN_PREFIXES`
        （`CTR` / `CUS` / `LED` / `OPP` / `QUO` / `REQ` / `SHP` / `SOL`）——
        形态没问题，只是名单里没有。

    只有 `project`(PRJ) 一类是通的。也就是说：**「三层引用同一实体 ID」这件事，
    在真正接入之前一直是纸面约定** —— 一旦 L_exec 引用 PO，校验器会把合法 ID
    判成非法。

## 修法

  · `contracts._ID_PREFIX` 的三处 2 字母前缀改规范 3 字母：
    `MO → MFO`（生产订单）、`PO → PUR`（采购订单）、`AS → AFS`（售后）；
  · 旧前缀进 `contracts.LEGACY_ID_PREFIX`，**只读兼容**（改前缀不该让历史行变非法）；
  · `alignment.KNOWN_PREFIXES` 补成两张前缀表的**并集**（26 个）；
  · 新增 `is_canonical_id`（严格，新写入用）与 `is_legacy_id`（只读兼容）。

## 本文件锁什么

  [A] **交叉一致性**（核心）：`KNOWN_PREFIXES` 必须恒等于
      `contracts._ID_PREFIX.values()` ∪ `project_brain.ids.KIND_PREFIX.values()`
      ∪ `exec_plane.schema.KIND_PREFIX.values()`。
      任何一侧加了前缀而另一侧没同步 → 立刻报红。
      这是防 E12 那类「双口径漂移」的同款做法 —— 不靠人记得同步。
      ★ 2026-10-03 批次 1 实测：`exec_plane` 新建 7 个实体时**真的忘了登记**，
        执行平面全部 ID 被跨层校验拒绝 —— 症状与本次修的 G29 缺口一模一样。
        抓到它的是 `tests/test_exec_plane.py` 的并集断言（本文件同时收紧）。
  [B] **形态**：规范前缀一律 3 字母、历史前缀一律 2 字母、两者不重叠、
      且历史名单必须与 `contracts.LEGACY_ID_PREFIX` 一一对应。
  [C] **端到端可校验**：三类表的**每一个** kind 生成出来的 ID，都必须过
      `alignment.is_canonical_id` —— 这条就是本次缺口的直接回归。
  [D] **新写入绝不产出历史形态**：`new_object_id` 的输出不得落在 `LEGACY_PREFIXES` 里。
"""

import datetime
import os
import re
import sys
import unittest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

from application import contracts as C                      # noqa: E402
from application.decision_support import alignment as A     # noqa: E402
from application.exec_plane import schema as ES             # noqa: E402
from application.project_brain import ids as I              # noqa: E402

_NOW = datetime.datetime(2026, 10, 3, 12, 0, 0)
_THREE = re.compile(r"^[A-Z]{3}$")
_TWO = re.compile(r"^[A-Z]{2}$")

# 全部「会生成 ID 的前缀表」。新增一层就往这里加一行 —— 加漏的层会被 [A] 抓住。
PREFIX_TABLES = (("contracts._ID_PREFIX", C._ID_PREFIX),
                 ("project_brain.ids.KIND_PREFIX", I.KIND_PREFIX),
                 ("exec_plane.schema.KIND_PREFIX", ES.KIND_PREFIX))

# 各层的 ID 生成函数（与 PREFIX_TABLES 一一对应）
_NEW_ID_FN = {
    "contracts._ID_PREFIX": lambda kind: C.new_object_id(kind, seq=7, now=_NOW),
    "project_brain.ids.KIND_PREFIX": lambda kind: I.new_id(kind, seq=7, now=_NOW),
    "exec_plane.schema.KIND_PREFIX": lambda kind: ES.new_id(kind, seq=7, now=_NOW),
}


def _new_id_for(table_name: str, kind: str) -> str:
    return _NEW_ID_FN[table_name](kind)


class TestCrossLayerIDConsistency(unittest.TestCase):
    """[A] 三张前缀表必须互为同一套事实。"""

    def test_KNOWN_PREFIXES_恒等于全部前缀表并集(self):
        """★ 核心漂移守卫。

        任何一侧新增 kind/前缀而 `alignment.KNOWN_PREFIXES` 没跟上 →
        新前缀会被跨层校验静默拒绝（就是本次缺口的样子）。
        """
        expect = set()
        for _name, table in PREFIX_TABLES:
            expect |= set(table.values())
        actual = set(A.KNOWN_PREFIXES)
        self.assertEqual(
            actual, expect,
            "接缝层前缀表与业务层/记忆层/执行层不一致\n"
            "  仅在各方前缀表里（会被跨层校验拒绝）：%s\n"
            "  仅在接缝层有（幽灵前缀）：%s"
            % (sorted(expect - actual), sorted(actual - expect)))

    def test_历史前缀名单与_contracts_一一对应(self):
        self.assertEqual(set(A.LEGACY_PREFIXES),
                         set(C.LEGACY_ID_PREFIX.values()))

    def test_每个_kind_都已登记前缀(self):
        """反向：各表里的 kind 不得存在空/缺失前缀。"""
        for name, table in PREFIX_TABLES:
            for kind, pfx in table.items():
                self.assertTrue(str(pfx or "").strip(),
                                "%s 里 %s 没有前缀" % (name, kind))


class TestPrefixShape(unittest.TestCase):
    """[B] 形态：规范 3 字母 / 历史 2 字母 / 不重叠。"""

    def test_规范前缀一律三字母(self):
        bad = [p for p in A.KNOWN_PREFIXES if not _THREE.match(p)]
        self.assertEqual(bad, [],
                         "规范前缀必须是 3 个大写字母，违规：%s" % bad)

    def test_历史前缀一律两字母(self):
        bad = [p for p in A.LEGACY_PREFIXES if not _TWO.match(p)]
        self.assertEqual(bad, [], "历史前缀必须是 2 个大写字母，违规：%s" % bad)

    def test_规范与历史不重叠(self):
        self.assertEqual(set(A.KNOWN_PREFIXES) & set(A.LEGACY_PREFIXES), set(),
                         "同一个前缀不能既是规范又是历史")

    def test_前缀无重复(self):
        for name, table in (("KNOWN_PREFIXES", A.KNOWN_PREFIXES),
                            ("LEGACY_PREFIXES", A.LEGACY_PREFIXES)):
            self.assertEqual(len(table), len(set(table)), "%s 有重复项" % name)

    def test_各层规范前缀全部三字母(self):
        """各层前缀表自己也不得再出现 2 字母（防回退）。"""
        for name, table in PREFIX_TABLES:
            bad = {k: v for k, v in table.items() if not _THREE.match(str(v))}
            self.assertEqual(bad, {}, "%s 里有非 3 字母前缀：%s" % (name, bad))


class TestGeneratedIDsAreCrossLayerValid(unittest.TestCase):
    """[C] 端到端：**每一层**每一个 kind 生成的 ID 都必须过跨层校验。

    刻意写成「遍历层 × 遍历 kind」，而不是给每层抄一遍 ——
    2026-10-03 批次 1 新增 `exec_plane` 这一层时，
    正是这条断言（在 `test_exec_plane.py` 里）当场抓出
    「7 个新前缀没登记进 `KNOWN_PREFIXES`」。
    """

    def test_全部层级_ID_都跨层合法(self):
        """★ 本次缺口的直接回归 —— 修之前业务层这条会红 11 处。"""
        bad = []
        for name, table in PREFIX_TABLES:
            for kind in sorted(table):
                v = _new_id_for(name, kind)
                if not A.is_canonical_id(v):
                    bad.append("%s/%s→%s" % (name, kind, v))
        self.assertEqual(bad, [], "这些 ID 过不了跨层校验：%s" % "；".join(bad))

    def test_全部层级_ID_前缀与类型匹配(self):
        for name, table in PREFIX_TABLES:
            for kind in sorted(table):
                v = _new_id_for(name, kind)
                self.assertTrue(A.is_canonical_id(v, table[kind]),
                                "%s/%s 的 ID %s 前缀不匹配" % (name, kind, v))

    def test_plan_id_仍走单数校验(self):
        """`check_alignment` 只对 plan_id 做单点校验，这条形态不能坏。"""
        self.assertTrue(A.is_canonical_id(I.new_id("plan", seq=1, now=_NOW), "PLN"))


class TestLegacyCompatibility(unittest.TestCase):
    """[D] 历史形态：只读兼容，但新写入绝不产出。"""

    def test_历史ID仍可校验(self):
        for kind, pfx in sorted(C.LEGACY_ID_PREFIX.items()):
            old = "%s-20260913-ab12" % pfx
            self.assertTrue(C.validate_object_id(kind, old),
                            "历史 ID %s 改前缀后变成非法了" % old)
            self.assertTrue(A.is_valid_id(old), "跨层校验应只读兼容 %s" % old)
            self.assertFalse(A.is_canonical_id(old),
                             "历史形态不该被当成规范形态")

    def test_新生成_ID_绝不落在历史前缀(self):
        """★ 防回退：任何一层都不得再吐出 2 字母前缀。"""
        for name, table in PREFIX_TABLES:
            for kind in sorted(table):
                v = _new_id_for(name, kind)
                self.assertNotIn(v.split("-")[0], A.LEGACY_PREFIXES,
                                 "%s/%s 仍生成历史形态 ID：%s" % (name, kind, v))

    def test_前缀表与生成函数表不得脱节(self):
        """`PREFIX_TABLES` 与 `_NEW_ID_FN` 必须一一对应。

        新增一层时只改一处就跑了 → 这条报红，而不是让那条层**被静默跳过**
        （本文件的全部断言都是「遍历 PREFIX_TABLES」，漏登记的层等于没测）。
        """
        self.assertEqual({n for n, _ in PREFIX_TABLES}, set(_NEW_ID_FN),
                         "PREFIX_TABLES 与 _NEW_ID_FN 的层名集合不一致")

    def test_未知前缀一律拒绝(self):
        for bad in ("ZZZ-20261003-0001", "XX-20261003-0001",
                    "PUR-2026100-0001", "PUR-20261003-00012", "PUR202610030001", ""):
            self.assertFalse(A.is_canonical_id(bad), "应拒绝：%r" % bad)

    def test_前缀不匹配时拒绝(self):
        v = C.new_object_id("purchase_order", seq=7, now=_NOW)
        self.assertFalse(A.is_canonical_id(v, "PRJ"), "前缀不符却通过了：%s" % v)


if __name__ == "__main__":
    unittest.main(verbosity=2)
