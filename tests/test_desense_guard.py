# -*- coding: utf-8 -*-
"""test_desense_guard.py — 去敏守卫的 2 字 needle 硬化回归测试（任务 #141 / 2026-10-03）。

## 背景

计划文件 §14.6 第 6 项原写「守卫硬化：CJK needle 长度 ≥3」，起因是一例假阳性：
某条 2 字 needle（来自 `cust_alias` 的别名）撞上了正常中文句子里**偶然的两字重叠**。

**该方案已被实测推翻**（本文件把推翻依据固化成断言）：

  · 真值表里的 2 字 needle 分两类：一部分是更长真名的**实际简称**（有更长形式兜底），
    另一部分是**独立简称**（没有更长形式，删掉就等于完全失去覆盖）。
  · 实测 `length>=3` 会让一批**真实业务简称句全部漏扫** —— 真名可以照常推上
    PUBLIC 仓库。这是「用真值覆盖换假阳性消除」，方向完全错。
  · 假阳性的**根因不是长度，是中文没有词边界**：实测十余句正常技术中文里
    大半误报（2 字 needle 落在了更长通用词组的**内部**）。

具体条数与实测句子见 `config.yaml`（已 gitignore）与计划文件；**本文件一律用
中性词举例**，不写真名 —— 测试文件同样在扫描面内，写真名会被守卫当场抓住。

## 采用的方案

**一条 needle 都不删**，改为对「被登记片段包裹」的出现逐次豁免：

    entities:
      fp_context:
        <2字needle>: [<包裹它的更长通用词组>, ...]

外加**反绕过自检** `_validate_fp_context`（这是本机制唯一的后门面）。

## 本文件锁什么

  [A] `_extract_needles` —— 阈值必须仍是 `>= 2`（**源码级守卫**：有人改回 >=3 就红）；
      且 `fp_context` 的键/值不得污染 needle 集合（否则包裹片段自己会报红）。
  [B] `_needle_hits` —— **逐次出现**判定：同一 needle 在「被包裹」处豁免、
      在「裸出现」处必须报红。**不得退化为整条 needle 白名单。**
  [C] `_validate_fp_context` —— 反绕过：未严格更长 / 不含 needle / 空列表 /
      needle 不在真值表，四条都必须在注入反例时真的报红。

> ⚠️ 本文件举例用的中性词（「联动」/「联动控制」/「闭合」/「微电」/「峰谷」/「甲乙」）
> 都是通用技术词，**刻意避开了真值表里任何 needle 的子串**。改举例词前请先跑
> `tests/test_smoke.py` —— 举例词撞上 needle 会被去敏守卫报红。
"""

import importlib.util
import io
import os
import re
import sys
import unittest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)


def _load_smoke():
    """按路径加载 test_smoke.py（tests/ 不是包，不能 `import test_smoke`）。"""
    path = os.path.join(HERE, "tests", "test_smoke.py")
    spec = importlib.util.spec_from_file_location("_guard_smoke", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_SMOKE = _load_smoke()
_extract_needles = _SMOKE._extract_needles
_needle_hits = _SMOKE._needle_hits
_covered_spans = _SMOKE._covered_spans
_validate_fp_context = _SMOKE._validate_fp_context

# 中性举例词（避开了真值表所有 needle 的子串）
W = "联动控制"      # 包裹片段
N = "联动"          # 被包裹的 2 字 needle


class TestExtractNeedles(unittest.TestCase):
    """[A] 真值表提取：阈值与 fp_context 隔离。"""

    def test_源码不得重新引入长度硬化(self):
        """★ 源码级守卫：锁定 #141 的决定 —— 阈值必须仍是 `>= 2`。

        若有人把阈值改回 `>= 3`（原方案），这条立刻报红：
        实测那会让一批真实业务简称句全部漏扫。

        自检：正则必须真的匹配到，否则本守卫本身失效
        （历史上「守卫假绿」的教训 —— 判据要写因果关系，别写现象特征）。
        """
        with io.open(os.path.join(HERE, "tests", "test_smoke.py"),
                     encoding="utf-8") as _fh:
            src = _fh.read()
        m = re.search(r"if len\(s\) >= (\d+) and s not in _ok", src)
        self.assertIsNotNone(
            m, "未找到 needle 提取条件 —— 结构已变，本源码级守卫失效，必须同步更新")
        self.assertEqual(m.group(1), "2",
                         "守卫阈值被改为 >=%s；length>=3 已实测推翻（会漏扫真实简称）"
                         % m.group(1))

    def test_fp_context_不得污染_needle_集合(self):
        """fp_context 的键与包裹片段都不能变成 needle。

        否则 `联动: [联动控制]` 会让「联动控制」自己成为 needle ——
        文档里一写就报红，机制自相矛盾。
        """
        ent = {
            "cust_alias": {"联动": ["联动"]},
            "fp_context": {"联动": ["联动控制", "联动复核"]},
        }
        needles, fpctx = _extract_needles(ent)
        self.assertIn(N, needles)
        self.assertNotIn(W, needles)
        self.assertNotIn("联动复核", needles)
        self.assertEqual(fpctx, {"联动": ["联动控制", "联动复核"]})

    def test_generic_ok_仍然跳过(self):
        ent = {"generic_ok": ["磁铁"], "product_alias": {"甲乙磁业": ["磁铁"]}}
        needles, _ = _extract_needles(ent)
        self.assertIn("甲乙磁业", needles)
        self.assertNotIn("磁铁", needles)

    def test_两字主体名必须被收录(self):
        """2 字 needle 是本机制的存在理由，一条都不能被长度过滤掉。"""
        ent = {"self_brand": ["甲乙", "丙丁"], "cust_alias": {"戊己": ["戊己"]}}
        needles, _ = _extract_needles(ent)
        for n in ("甲乙", "丙丁", "戊己"):
            self.assertIn(n, needles, "2 字主体名 %s 被过滤掉了" % n)


class TestNeedleHits(unittest.TestCase):
    """[B] 逐次出现判定 —— 防「整条 needle 白名单」退化。"""

    def test_被包裹的出现豁免(self):
        self.assertEqual(_needle_hits("三台设备联动控制", N, [W]), [])

    def test_裸出现仍报红(self):
        hits = _needle_hits("联动那边确认了交期", N, [W])
        self.assertEqual(hits, [0], "裸出现的真名简称必须报红")

    def test_同一needle混合出现只报裸的(self):
        """★ 本机制最关键的一条：不得退化为整条放行。

        needle 出现两次 —— 一次在「联动控制」内（豁免），
        一次是裸的真名（必须报红）。只报 1 处，且报的是裸的那处。
        """
        t = "设备联动控制；联动那边确认了交期"
        self.assertEqual(t.count(N), 2, "语料构造错误：应恰好出现 2 次")
        hits = _needle_hits(t, N, [W])
        self.assertEqual(len(hits), 1, "整条放行退化了：%r" % hits)
        self.assertEqual(hits[0], t.rfind(N), "报红的不是裸出现的那处")
        self.assertEqual(t[hits[0] - 1], "；",
                         "报红位置紧邻「控制」→ 说明逐次判定失效")

    def test_无配置时行为不变(self):
        """fp_context 为空时，判定必须与硬化前**逐字节等价**（不能引入新行为）。"""
        t = "设备联动控制"
        self.assertEqual(_needle_hits(t, N, None), [2])
        self.assertEqual(_needle_hits(t, N, []), [2])

    def test_多个包裹片段(self):
        t = "联动控制与联动复核"
        self.assertEqual(_needle_hits(t, N, ["联动控制", "联动复核"]), [])

    def test_结构同形的正反例合集(self):
        """用中性词复现「假阳性可豁免 / 真名必须抓到」两类场景。

        选词都是通用技术词，与真实场景结构同形（2 字 needle + 更长通用词组包裹）。
        """
        fp = [("三台设备联动控制", "联动", ["联动控制"]),
              ("开工前联动复核", "联动", ["联动复核"]),
              ("开关全部闭合到位", "闭合", ["全部闭合"]),
              ("低功耗微电源方案", "微电", ["微电源"]),
              ("按峰谷电价计费", "峰谷", ["峰谷电价"])]
        for text, needle, wraps in fp:
            self.assertEqual(_needle_hits(text, needle, wraps), [],
                             "假阳性未被豁免：%s / %s" % (text, needle))
        tp = [("联动那边确认了交期", "联动"),
              ("闭合的合同待签", "闭合"),
              ("微电今天来电话", "微电"),
              ("峰谷要加订两百台", "峰谷")]
        for text, needle in tp:
            self.assertTrue(_needle_hits(text, needle, [W]),
                            "真名简称被漏扫：%s / %s" % (text, needle))


class TestCoveredSpans(unittest.TestCase):
    """区间合并的边界行为。"""

    def test_无片段(self):
        self.assertEqual(_covered_spans("任意文本", []), [])
        self.assertEqual(_covered_spans("任意文本", None), [])

    def test_重叠片段被合并(self):
        # 「联动控制」[0,4) 与「动控制系统」[2,6) 重叠 → 合并为 [0,6)
        # （「联动控制系统」共 6 字：联0 动1 控2 制3 系4 统5）
        self.assertEqual(_covered_spans("联动控制系统", ["联动控制", "动控制系统"]),
                         [(0, 6)])

    def test_不重叠片段不合并(self):
        spans = _covered_spans("联动控制…动控制系统", ["联动控制", "动控制系统"])
        self.assertEqual(len(spans), 2)

    def test_同一片段多处出现(self):
        spans = _covered_spans("联动控制和联动控制", ["联动控制"])
        self.assertEqual(spans, [(0, 4), (5, 9)])


class TestValidateFpContext(unittest.TestCase):
    """[C] 反绕过 —— 每条都必须**真的会失败**（注入反例 → 报红）。"""

    NEEDLES = {"联动", "甲乙"}

    def test_合法配置无问题(self):
        self.assertEqual(_validate_fp_context({"联动": [W]}, self.NEEDLES), [])

    def test_包裹片段与needle等长被拒(self):
        """`联动: [联动]` = 整条放行 —— 必须拒绝。"""
        bad = _validate_fp_context({"联动": ["联动"]}, self.NEEDLES)
        self.assertEqual(len(bad), 1, bad)
        self.assertIn("未严格更长", bad[0])

    def test_包裹片段更短被拒(self):
        bad = _validate_fp_context({"联动": ["联"]}, self.NEEDLES)
        self.assertTrue(bad)
        self.assertIn("未严格更长", bad[0])

    def test_包裹片段不含needle被拒(self):
        """死配置：永远不生效，却让人以为已处置 —— 必须拒绝。"""
        bad = _validate_fp_context({"联动": ["级联复核"]}, self.NEEDLES)
        self.assertEqual(len(bad), 1, bad)
        self.assertIn("死配置", bad[0])

    def test_空列表被拒(self):
        bad = _validate_fp_context({"联动": []}, self.NEEDLES)
        self.assertTrue(bad)
        self.assertIn("整条放行", bad[0])

    def test_needle不在真值表被拒(self):
        bad = _validate_fp_context({"不存在的主体": ["不存在的主体啊"]}, self.NEEDLES)
        self.assertEqual(len(bad), 1, bad)
        self.assertIn("不在真值表里", bad[0])

    def test_自己写一个通配式豁免会被拒(self):
        """模拟「想用配置一口气放行」的写法：把整条 needle 当包裹片段。"""
        for evil in ({"甲乙": ["甲乙"]}, {"甲乙": ["乙"]}, {"甲乙": [""]},
                     {"甲乙": []}, {"甲乙": ["不相关"]}):
            self.assertTrue(_validate_fp_context(evil, self.NEEDLES),
                            "反绕过失效，这条配置被放行了：%r" % (evil,))


if __name__ == "__main__":
    unittest.main(verbosity=2)
