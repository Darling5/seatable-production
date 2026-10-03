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
_generalized_ok = _SMOKE._generalized_ok
_NEEDLE_SKIP_KEYS = _SMOKE._NEEDLE_SKIP_KEYS
# 形态型判据（2026-10-03 批次 1 新增）
_FORM_RULES = _SMOKE._FORM_RULES
_form_violations = _SMOKE._form_violations
_baseline_report = _SMOKE._baseline_report
_is_placeholder_num = _SMOKE._is_placeholder_num

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

    def test_泛化短形白名单不得被当_needle(self):
        """★ `generalized_ok` 是**显式登记**的放宽项，不是第二个 generic_ok。

        它必须同时满足两件事：
          ① 不进 needle 集合 —— 否则仓库里那些已泛化的短形会被自己的守卫报红；
          ② 仍能被 `_generalized_ok` 取出并打印 —— 放宽必须可见。

        对照 `test_两字主体名必须被收录`：同样的词放在 `self_brand` 里就是 needle，
        放进 `generalized_ok` 才是豁免 —— 差别只在**键**，所以键名必须语义清楚。
        """
        ent = {"generalized_ok": ["甲乙", "丙丁"],
               "cust_alias": {"戊己": ["戊己"]}}
        needles, _ = _extract_needles(ent)
        for n in ("甲乙", "丙丁"):
            self.assertNotIn(n, needles, "泛化短形 %s 被当成了 needle" % n)
        self.assertIn("戊己", needles, "同表其它键仍应正常收录")
        self.assertEqual(_generalized_ok(ent), ["甲乙", "丙丁"])
        self.assertEqual(_generalized_ok({}), [], "无配置时必须返回空，而不是 None")

    def test_跳过键清单不得被悄悄缩短(self):
        """三把跳过键各自对应一类放宽，少一个就会出现「报红 / 看不见」的错向。

        `generic_ok` / `fp_context` / `generalized_ok` 三者语义不同，
        合并或删除都会让某类放宽变得隐式。
        """
        for k in ("generic_ok", "fp_context", "generalized_ok"):
            self.assertIn(k, _NEEDLE_SKIP_KEYS, "跳过键 %s 不见了" % k)


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


class TestFormRules(unittest.TestCase):
    """[D] 形态型判据（2026-10-03 批次 1）—— 每条都必须**真的会报红**。

    ★ 反例一律**运行时拼接**，绝不写字面量：
      本文件自己也在扫描面内，写死一个形态合规的号会被自己的规则报红
      （「注释与测试文件同样在扫描面内」是本仓库已踩过的坑）。
    """

    def _scan(self, text):
        return _form_violations({"t.md": text})

    # ── 运单号 ────────────────────────────────────────────────
    def test_真实形态运单号必须报红(self):
        """形态合规且非占位 → 必须报红，否则规则等于没写。"""
        fake = "SF" + "987" + "654" + "3210"
        hits = self._scan("顺丰单号 %s 已发出" % fake)
        self.assertEqual(len(hits), 1, "运单号规则失效：%r" % hits)
        self.assertIn("快递单号", hits[0])

    def test_占位运单号必须放行(self):
        """占位号（顺序串 / 全同位）不得报红，否则守卫会被噪音淹没。"""
        for digits in ("1234567890", "11111111111", "0000000000"):
            self.assertEqual(self._scan("示例 %s" % ("SF" + digits)), [],
                             "占位号被误报：%s" % digits)

    def test_占位判定不得过宽(self):
        """★ 放行名单必须窄 —— 否则规则可被「看起来像占位」的真号绕过。"""
        for digits in ("0215960839395", "9876543210", "1234567891",
                       "12345678901"):
            self.assertFalse(_is_placeholder_num(digits),
                             "非占位号被当占位放行了：%s" % digits)

    # ── 产品型号 ──────────────────────────────────────────────
    def test_真实形态型号必须报红(self):
        for fake in ("ZD" + "-" + "ZTU1",
                     "ZD" + "aiot" + "1",
                     "ZD" + "-" + "XD1-UWB-RTK-BDS-BL-4G"):
            hits = self._scan("产品 %s 已入库" % fake)
            self.assertTrue(hits, "型号规则失效：%s" % fake)
            self.assertIn("产品型号", hits[0])

    def test_泛化占位型号必须放行(self):
        self.assertEqual(self._scan("产品 ⟨型号A⟩ 与 ⟨型号B⟩"), [])

    # ── 基线机制 ─────────────────────────────────────────────
    # ★ 用**合成规则**测基线机制，不依赖真值表里有没有真实值。
    #   历史原因：这三条原先写死了真实部件号 —— 任务 #151 清空基线后，
    #   它们自己就会被守卫报红（教训：测试文件同样在扫描面内）。
    _PART_RX = re.compile(r"(?<![A-Za-z0-9])P0\d{3}(?![0-9])")
    _PART_A = "P" + "0059"       # 运行时拼接：形态合规，但字面量不出现在源码里
    _PART_B = "P" + "0408"

    def _synth(self, baseline):
        return [("部件号", self._PART_RX, baseline, "合成规则（仅供单测）")]

    def test_已登记基线放行(self):
        rules = self._synth((self._PART_A,))
        self.assertEqual(
            _form_violations({"t.md": "部件号 %s 无行情" % self._PART_A}, rules), [])

    def test_未登记的同类必须报红(self):
        """★ 本机制的核心：放行**逐项**登记，不是按形态整类放行。"""
        rules = self._synth((self._PART_A,))      # 只登记了 A
        hits = _form_violations({"t.md": "部件号 %s 无行情" % self._PART_B}, rules)
        self.assertEqual(len(hits), 1, "基线退化为整类放行了：%r" % hits)
        self.assertIn("部件号", hits[0])

    def test_基线失效项被识别(self):
        """登记了但语料里已不存在的项 → 应被报为「失效」（告警，不报红）。"""
        rules = self._synth((self._PART_A, self._PART_B))
        used, stale = _baseline_report({"t.md": "只有 %s 在这里" % self._PART_A}, rules)
        self.assertIn("部件号:%s" % self._PART_A, used)
        self.assertIn("部件号:%s" % self._PART_B, stale)

    def test_基线当前必须为空(self):
        """★ 基线非空 = 仓库里已知存在**未清理**的真实数据。

        现状（2026-10-03 任务 #151 清完之后）应为空。若确实需要重新登记，
        请**连同本条说明一起改** —— 让「重新开一个白名单」必须是有意识的动作，
        而不是顺手加一项。
        """
        for name, _rx, baseline, _hint in _FORM_RULES:
            self.assertEqual(
                tuple(baseline), (),
                "形态型规则「%s」登记了基线 %r —— 意味着仓库里有未清理的真实数据"
                % (name, baseline))

    # ── 规则清单本身 ──────────────────────────────────────────
    def test_四类规则一条都不能少(self):
        """★ 防「悄悄删掉一条规则」—— 删了就等于该类数据重新失明。"""
        names = [r[0] for r in _FORM_RULES]
        for want in ("快递单号", "产品型号", "项目码", "部件号"):
            self.assertIn(want, names, "形态型规则「%s」被删掉了" % want)

    def test_零容忍类不得被塞进基线(self):
        """运单号 / 产品型号是零容忍类：基线必须为空。

        一旦给它们开了基线 = 合法化「继续提交真实型号」——
        那正是本机制要防的事。
        """
        for name, _rx, baseline, _hint in _FORM_RULES:
            if name in ("快递单号", "产品型号"):
                self.assertEqual(baseline, (),
                                 "零容忍类「%s」被开了基线：%r" % (name, baseline))

    def test_长数字串刻意不覆盖(self):
        """把「不覆盖」固化成断言，防止后人误以为它已被守着。

        实测语料有 15 处 ≥12 位独立数字（epoch 毫秒 + SQLite 字节串），
        信噪比太差，故**刻意不建**该规则。这里声明该边界，避免误判。
        """
        self.assertEqual(self._scan("时间戳 1786757548346 是 epoch 毫秒"), [])
        self.assertNotIn("长数字", [r[0] for r in _FORM_RULES])

    def test_命中消息带文件与行号(self):
        fake = "SF" + "987" + "654" + "3210"
        hits = _form_violations({"a/b.md": "第一行\n第二行 %s" % fake})
        self.assertIn("a/b.md:2", hits[0], "FAIL 消息缺少「文件:行」，无法定位：%r" % hits)


if __name__ == "__main__":
    unittest.main(verbosity=2)
