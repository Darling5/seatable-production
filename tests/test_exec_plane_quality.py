# -*- coding: utf-8 -*-
"""test_exec_plane_quality.py — `G24` 质量闸回归测试（批次 2）。

## 不变量

`G24`：**OQC 不合格 ⟹ 不得入库、不得发货。**

## 这套用例真正在防什么

`G24` 的判定逻辑本身很短，短到「看一眼就觉得对」。所以本文件几乎全部的篇幅
花在**三类不靠读代码能看出来的错误**上 —— 它们都不是「算错了」，
而是「结论对不上现实」：

  1. **死分支**：`schema.G24_REASON_CN` 里登记了一个**永远取不到**的码。
     ★ 这是批次 1 亲手掐掉过一次的东西（`R_INSPECTION_FAILED`），
       而它的症状是「一切正常」—— 所以只能靠
       `TestEveryCodeReachable` 把每个码**逐个逼出来**。
  2. **第三态被折叠**：把「判不了」要么折成「通过」（假指标）、
     要么折成「没依据的拒绝」（把「数据没填全」伪装成「货不合格」）。
     ★ 本批的结论是二者**都错**，且不同判据要不同处置：
       主判据判不了 ⇒ 拒绝；附加判据判不了 ⇒ 放行但标注。见 `TestThirdState`。
  3. **结论不可复现 / 用错那条记录**：「旧的对、新的不对」被误放行，
     或同一份数据两次调用结论不同。见 `TestGateBehaviour` 末三条。

## 命名约定

一律用中性占位料号与编号（`ITM-*` / `INS-*` / `MRB-*`），
不出现任何真实供应商、客户、型号或人名 ——
与去敏守卫（`tests/test_smoke.py [09b]`）同一纪律。
测试文件**同样在扫描面内**：任何「形态合规」的真实值都会被自己报红。
"""

import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

from application import authorization as AUTH                  # noqa: E402
from application import contracts as C                         # noqa: E402
from application.exec_plane import ExecPlane                   # noqa: E402
from application.exec_plane import quality as Q                # noqa: E402
from application.exec_plane import schema as S                 # noqa: E402

APPLY = C.MODE_APPLY
PREVIEW = C.MODE_PREVIEW

NOW = "2026-10-03T12:00:00"
LATER = "2026-10-03T13:00:00"
EARLIER = "2026-10-03T11:00:00"

ITEM = "ITM-LNK-0001"
ITEM2 = "ITM-LNK-0002"
INS1 = "INS-20261003-0001"
INS2 = "INS-20261003-0002"
MRB1 = "MRB-20261003-0001"


# ── 行构造（纯数据，不碰存储）────────────────────────────────────────
def insp(*, iid=INS1, item=ITEM, itype=S.QC_IQC, verdict=S.QC_PASS,
         qty=5, at=NOW, ref="", **kw):
    row = {"inspection_id": iid, "item_id": item, "inspection_type": itype,
           "verdict": verdict, "qty_inspected": qty, "inspected_at": at}
    if ref:
        row["ref_id"] = ref
    row.update(kw)
    return row


def mrb(*, mid=MRB1, iid=INS1, item=ITEM, decision=S.MRB_CONCESSION, at=NOW, **kw):
    row = {"mrb_id": mid, "inspection_id": iid, "item_id": item,
           "decision": decision, "decided_at": at}
    row.update(kw)
    return row


def txn(*, tid="IVT-20261003-0001", item=ITEM, direction=S.TXN_IN, qty=10,
        at=NOW, **kw):
    row = {"txn_id": tid, "item_id": item, "direction": direction,
           "qty": qty, "txn_at": at}
    row.update(kw)
    return row


# ════════════════════════════════════════════════════════════════════
# [A] 词表与方向映射自洽
# ════════════════════════════════════════════════════════════════════
class TestWordTables(unittest.TestCase):

    def test_每个原因码都有中文说明(self):
        for code in S.G24_REASON_CN:
            self.assertTrue(str(S.G24_REASON_CN[code]).strip(),
                            "原因码 %s 没有中文说明 —— 调用方拿到它不知道该干什么" % code)

    def test_方向到检验类型的映射是三对全的(self):
        """`in → IQC` / `out → OQC`，且**只认这一种**。"""
        self.assertEqual(Q.source_types(S.GATE_INBOUND), (S.QC_IQC,))
        self.assertEqual(Q.source_types(S.GATE_OUTBOUND), (S.QC_OQC,))
        # 每个方向恰好认一种，且都在词表内
        for d in S.GATE_DIRECTIONS:
            types = Q.source_types(d)
            self.assertEqual(len(types), 1, "%s 认了 %d 种检验，对应关系不唯一" % (d, len(types)))
            for t in types:
                self.assertIn(t, S.QC_TYPES)

    def test_IPQC两个方向都不作数(self):
        """★ 过程检验是车间内部手段，**不能**当作来料合格或出货合格。

        这是 `G24` 最容易漏的一条：三种检验长得太像，很容易被合并成
        「只要有检验合格就能动」—— 那会让「过程检验合格」被拿去当「来料合格」用。
        """
        for d in S.GATE_DIRECTIONS:
            self.assertNotIn(S.QC_IPQC, Q.source_types(d),
                             "%s 认了 IPQC —— 过程检验不得作为放行依据" % d)
        # 但它必须在词表里：写检验单时要能登记过程检验
        self.assertIn(S.QC_IPQC, S.QC_TYPES)

    def test_方向非法必须抛异常而不是静默返回空(self):
        """★ 静默返回空元组，会把**参数写错**伪装成**货物没检验**。

        前者是「代码调错了」，后者是「货没检」，要修的东西完全不同 ——
        返回空元组会让后续逻辑一路走到「没有可用检验 → 拒绝」，
        于是一个类型错误最终表现为一个业务结论。
        """
        for bad in ("", "inbound2", "IN", "入库", None):
            with self.assertRaises(ValueError, msg="方向 %r 没被拒绝" % (bad,)):
                Q.source_types(bad)

    def test_不存在检验不合格的汇总码(self):
        """★ 反死分支：`R_INSPECTION_FAILED` 这样的码**不该存在**。

        「检验不合格」之后的下一步动作**必然是**去处理 MRB
        （补裁定 / 改取值 / 改裁定），所以那条路只会落到三个 MRB 码之一。
        若再设一个「不合格」汇总码，它**永远取不到** ——
        `__all__` 里有一个取不到的码，本身就是假指标。
        """
        names = [n for n in S.__all__ if n.startswith("R_")]
        self.assertNotIn("R_INSPECTION_FAILED", names,
                         "又出现了取不到的汇总码 —— 不合格之后的动作是处理 MRB，不是报「不合格」")

    def test_本层每个原因码都必须有判定分支(self):
        """扫 `__all__` 里的**全部** `R_*`：每一个都得在码表里查得到。

        ★ 覆盖 G24 ∪ G25 两张表 —— 一个码只要导出了却没人产出，
          就是孤码（死分支）。这条与「每个码都能被取到」是一对：
          前者查「导出的码有没有分支」，后者查「码表里的码有没有路径」，
          两头都堵上才不会漏。
        """
        known = set(S.G24_REASON_CN) | set(S.G25_REASON_CN)
        for n in [x for x in S.__all__ if x.startswith("R_")]:
            self.assertIn(getattr(S, n), known,
                          "%s 不在任何码表里 —— 孤码，没有判定分支会产出它" % n)
        self.assertEqual(len(known), len(S.G24_REASON_CN) + len(S.G25_REASON_CN),
                         "G24 与 G25 的原因码撞了值 —— 两张表必须互不相交")

    def test_检验类型与闸门方向是两个字段不要合并(self):
        """类型答「哪一阶段」，方向答「走哪条路」，二者不是一回事。"""
        self.assertNotIn(S.GATE_INBOUND, S.QC_TYPES)
        self.assertNotIn(S.GATE_OUTBOUND, S.QC_TYPES)
        self.assertNotIn(S.QC_IQC, S.GATE_DIRECTIONS)
        # 二者的对应关系**只**由 GATE_SOURCE_TYPES 一处表达
        self.assertEqual(set(S.GATE_SOURCE_TYPES), set(S.GATE_DIRECTIONS))

    def test_放行类结论与放行类裁定不得混(self):
        """检验结论的「放行类」与 MRB 裁定的「放行类」是两组词，别互相套用。"""
        for v in S.QC_RELEASING_VERDICTS:
            self.assertIn(v, S.QC_VERDICTS)
        for d in S.MRB_RELEASING:
            self.assertIn(d, S.MRB_DECISIONS)
        # ★ 返工与退货**不在**放行类里：说好要返工就直接用，是 G24 最典型的破口
        for d in (S.MRB_RETURN, S.MRB_REWORK, S.MRB_SCRAP):
            self.assertNotIn(d, S.MRB_RELEASING)


# ════════════════════════════════════════════════════════════════════
# [B] 每个原因码都必须真的会被返回（反死分支）
# ════════════════════════════════════════════════════════════════════
class TestEveryCodeReachable(unittest.TestCase):
    """★ 把 `G24_REASON_CN` 的每一个码**逐个逼出来**，再断言一个不漏。

    只测「我写的那几条路径对不对」是不够的：那样能发现算错，
    发现不了「某个码永远取不到」。而后者不会让任何用例变红 ——
    它只会让人以为有一条防线存在。
    """

    # (场景说明, 期望码, (direction, item_id, inspections, mrbs))
    CASES = [
        ("连物品都没给 → 判不了",
         S.R_TARGET_UNKNOWN, (S.GATE_INBOUND, "",
                              [insp()], [])),
        ("一条检验都没有",
         S.R_NO_INSPECTION, (S.GATE_INBOUND, ITEM, [], [])),
        ("只有 IPQC，方向认 IQC → 类型不对",
         S.R_INSPECTION_TYPE_MISMATCH,
         (S.GATE_INBOUND, ITEM, [insp(itype=S.QC_IPQC)], [])),
        ("结论还是待检",
         S.R_INSPECTION_PENDING, (S.GATE_INBOUND, ITEM, [insp(verdict=S.QC_PENDING)], [])),
        ("结论取值不在词表内",
         S.R_VERDICT_INVALID, (S.GATE_INBOUND, ITEM, [insp(verdict="也许吧")], [])),
        ("合格但没填抽检数 → 覆盖面判不了",
         S.R_COVERAGE_UNKNOWN, (S.GATE_INBOUND, ITEM, [insp(qty="")], [])),
        ("不合格且没有 MRB 裁定",
         S.R_MRB_MISSING, (S.GATE_INBOUND, ITEM, [insp(verdict=S.QC_FAIL)], [])),
        ("不合格，MRB 裁定取值不在词表内",
         S.R_MRB_INVALID,
         (S.GATE_INBOUND, ITEM, [insp(verdict=S.QC_FAIL)], [mrb(decision="再看看")])),
        ("不合格，MRB 裁定是返工（非放行类）",
         S.R_MRB_NOT_RELEASING,
         (S.GATE_INBOUND, ITEM, [insp(verdict=S.QC_FAIL)], [mrb(decision=S.MRB_REWORK)])),
    ]

    def test_码表里的每一个码都会被某条路径产出(self):
        reached = {}
        for why, want, (d, item, insps, mrbs) in self.CASES:
            got = Q.check_gate(d, item, insps, mrbs)
            self.assertEqual(got["code"], want, "%s：期望 %s，实际 %s" % (why, want, got["code"]))
            reached[want] = why
        missing = sorted(set(S.G24_REASON_CN) - set(reached))
        self.assertEqual(missing, [],
                         "这些码**没有任何路径能取到**（死分支）：%s" % missing)
        extra = sorted(set(reached) - set(S.G24_REASON_CN))
        self.assertEqual(extra, [], "产出了码表里没有的码：%s" % extra)

    def test_每个码都必须能被单独取到(self):
        """反过来说：一个码只被一条路径顺便产出，不算「可达」。

        这条与上一条的区别：上一条断言「全集覆盖」，这一条断言
        「同一场景只产出这一个码」—— 防止某个码被别的情况掩盖。
        """
        produced = set()
        for _why, want, (d, item, insps, mrbs) in self.CASES:
            produced.add(Q.check_gate(d, item, insps, mrbs)["code"])
        self.assertEqual(produced, set(S.G24_REASON_CN))


# ════════════════════════════════════════════════════════════════════
# [C] 三态：判不了的两条路必须**处置不同**
# ════════════════════════════════════════════════════════════════════
class TestThirdState(unittest.TestCase):
    """★ 本批最关键的一组 —— 「判不了」不能一律拒绝，也不能一律放行。

        · 主判据判不了（连对象都没有 / 结论取值坏了）⇒ **拒绝**（fail-closed）
        · 附加判据判不了（合格但抽检数没填）      ⇒ **放行 + 标注**

    第二种的正当性写在 `quality.py` 模块 docstring：`G24` 只禁止
    「不合格」时放行；拿「没填抽检数」去拒绝，实现的是比不变量更严的规则，
    而它的坏下场是逼人随手填个 `1` —— 那等于亲手制造假数据。
    """

    def test_合格但抽检数没填必须放行且标注(self):
        got = Q.check_inbound(ITEM, [insp(qty="")])
        self.assertTrue(got["allowed"],
                        "覆盖面判不了不该拦住一批明确合格的货：%s" % got["message"])
        self.assertFalse(got["checked"], "『判不了』必须显式可见（checked=False）")
        self.assertEqual(got["code"], S.R_COVERAGE_UNKNOWN)
        self.assertIn("判不了", got["message"])

    def test_抽检数为零同样只是标注(self):
        """「填了零」与「没填」处置相同，但提示要说清是哪一种。"""
        got = Q.check_inbound(ITEM, [insp(qty=0)])
        self.assertTrue(got["allowed"])
        self.assertFalse(got["checked"])
        self.assertIn("填的是", got["message"])

    def test_没给物品必须拒绝(self):
        got = Q.check_inbound("", [insp()])
        self.assertFalse(got["allowed"], "连对象都定位不到就不能放行")
        self.assertFalse(got["checked"], "这是『判不了』，不是『明确不合格』")
        self.assertEqual(got["code"], S.R_TARGET_UNKNOWN)

    def test_结论取值非法必须拒绝(self):
        """取值坏了说明**可能本就是不合格**，所以必须 fail-closed。"""
        got = Q.check_inbound(ITEM, [insp(verdict="差不多吧")])
        self.assertFalse(got["allowed"])
        self.assertFalse(got["checked"])
        self.assertEqual(got["code"], S.R_VERDICT_INVALID)

    def test_明确拒绝与判不了可以从_checked_分开(self):
        """两种「不放行」的 `checked` 必须不同 —— 否则上层无法区分。"""
        blocked = Q.check_inbound(ITEM, [])                       # 明确：没检验
        unknown = Q.check_inbound("", [insp()])                   # 判不了：没对象
        self.assertFalse(blocked["allowed"])
        self.assertFalse(unknown["allowed"])
        self.assertTrue(blocked["checked"], "「没检验」是明确结论，不是判不了")
        self.assertFalse(unknown["checked"], "「没给物品」是判不了，不是明确拒绝")

    def test_第二态必须给出下一步动作(self):
        """明确拒绝的 `code` 要能告诉调用方**接下来做什么**。"""
        got = Q.check_inbound(ITEM, [])
        self.assertEqual(got["code"], S.R_NO_INSPECTION)
        self.assertTrue(S.G24_REASON_CN[got["code"]].strip())
        self.assertIn(S.GATE_DIRECTION_CN[S.GATE_INBOUND], got["message"])


# ════════════════════════════════════════════════════════════════════
# [D] 判定行为（纯函数）
# ════════════════════════════════════════════════════════════════════
class TestGateBehaviour(unittest.TestCase):

    def test_合格放行(self):
        got = Q.check_inbound(ITEM, [insp(qty=8)])
        self.assertTrue(got["allowed"])
        self.assertTrue(got["checked"])
        self.assertEqual(got["code"], "")
        self.assertIn(S.QC_IQC, got["message"])

    def test_不合格无裁定必须拒绝(self):
        got = Q.check_inbound(ITEM, [insp(verdict=S.QC_FAIL)])
        self.assertFalse(got["allowed"])
        self.assertEqual(got["code"], S.R_MRB_MISSING)

    def test_让步接收可以放行(self):
        got = Q.check_inbound(ITEM, [insp(verdict=S.QC_FAIL)],
                              [mrb(decision=S.MRB_CONCESSION)])
        self.assertTrue(got["allowed"])
        self.assertEqual(got["mrb_decision"], S.MRB_CONCESSION)

    def test_直接使用可以放行(self):
        got = Q.check_inbound(ITEM, [insp(verdict=S.QC_FAIL)],
                              [mrb(decision=S.MRB_USE_AS_IS)])
        self.assertTrue(got["allowed"])

    def test_返工不得作为放行依据(self):
        """★ 说好要返工就直接用，是 `G24` 最典型的破口。"""
        got = Q.check_inbound(ITEM, [insp(verdict=S.QC_FAIL)],
                              [mrb(decision=S.MRB_REWORK)])
        self.assertFalse(got["allowed"])
        self.assertEqual(got["code"], S.R_MRB_NOT_RELEASING)

    def test_报废与退货同样不得放行(self):
        for d in (S.MRB_SCRAP, S.MRB_RETURN):
            got = Q.check_inbound(ITEM, [insp(verdict=S.QC_FAIL)], [mrb(decision=d)])
            self.assertFalse(got["allowed"], "%s 竟然放行了" % d)

    def test_旧记录合格新记录不合格必须拒绝(self):
        """★ 回归：判定必须用**过滤后的最新**那条，不是「任何一条合格的」。

        否则「上周合格、本周不合格」会被放行 —— 而它恰恰是最该拦住的情况：
        返修后又检出来的问题，正是靠「新的那条」才看得见。
        """
        got = Q.check_inbound(ITEM, [
            insp(iid=INS1, verdict=S.QC_PASS, at=EARLIER),
            insp(iid=INS2, verdict=S.QC_FAIL, at=LATER),
        ])
        self.assertFalse(got["allowed"], "用旧记录放行了新检出的不合格品")
        self.assertEqual(got["inspection_id"], INS2, "取的不是最新那条")

    def test_旧记录不合格新记录合格可以放行(self):
        """反向：返工后重检合格，就该放行 —— 否则「重新检验」这条路走不通。"""
        got = Q.check_inbound(ITEM, [
            insp(iid=INS1, verdict=S.QC_FAIL, at=EARLIER),
            insp(iid=INS2, verdict=S.QC_PASS, at=LATER),
        ])
        self.assertTrue(got["allowed"])
        self.assertEqual(got["inspection_id"], INS2)

    def test_同一时刻多条记录结论必须稳定(self):
        """★ 同一份数据两次调用，结论必须一致（与输入顺序无关）。

        只用时间排序，会在「时间相同」的多条记录上给出取决于输入顺序的结果，
        于是同一批货在两次查询里得到两个答案 —— 那是一种最难发现的不可复现。
        """
        a = insp(iid=INS1, verdict=S.QC_PASS, at=NOW)
        b = insp(iid=INS2, verdict=S.QC_FAIL, at=NOW)
        first = Q.check_inbound(ITEM, [a, b])
        second = Q.check_inbound(ITEM, [b, a])
        self.assertEqual(first["inspection_id"], second["inspection_id"])
        self.assertEqual(first["code"], second["code"])

    def test_别批货的检验不得放行本批(self):
        """★ `ref_id` 收窄：挂了别的批次号的检验，不能拿来放行本批。"""
        got = Q.check_inbound(ITEM, [insp(ref="BATCH-X")], ref_id="BATCH-Y")
        self.assertFalse(got["allowed"],
                         "拿了别批货的检验结论放行本批 —— BATCH-X 放行了 BATCH-Y")
        self.assertEqual(got["code"], S.R_NO_INSPECTION)
        # 反向：批次对得上就正常放行
        self.assertTrue(Q.check_inbound(ITEM, [insp(ref="BATCH-X")],
                                       ref_id="BATCH-X")["allowed"])

    def test_检验单没挂批次时仍作数(self):
        """老数据常见「检验单不挂 ref」—— 不能因为没挂就当它不存在。

        收窄的方向只能是「挂了就必须对」，不能是「没挂就不算」。
        """
        self.assertTrue(Q.check_inbound(ITEM, [insp()], ref_id="BATCH-Y")["allowed"])

    def test_别的物料的检验不得放行本物品(self):
        got = Q.check_inbound(ITEM2, [insp(item=ITEM)])
        self.assertFalse(got["allowed"])
        self.assertEqual(got["code"], S.R_NO_INSPECTION)

    def test_有记录但类型不对时要报出类型(self):
        """★ 报「没有检验」会让人去补一张**已经有的**单子。

        这类错误要能被明确识别成「有记录，只是类型不对」，
        所以 `inspections_of` 刻意**不按类型过滤**。
        """
        got = Q.check_inbound(ITEM, [insp(itype=S.QC_OQC)])
        self.assertEqual(got["code"], S.R_INSPECTION_TYPE_MISMATCH)
        self.assertEqual(got["inspection_type"], S.QC_OQC)
        self.assertIn(S.QC_IQC, got["message"])
        self.assertIn("现有的是", got["message"])

    def test_出货闸门不认来料检验(self):
        """同一份 IQC 记录：入库认，出货不认 —— 两者问的不是一个问题。"""
        rows = [insp(itype=S.QC_IQC)]
        self.assertTrue(Q.check_inbound(ITEM, rows)["allowed"])
        got = Q.check_outbound(ITEM, rows)
        self.assertFalse(got["allowed"])
        self.assertEqual(got["code"], S.R_INSPECTION_TYPE_MISMATCH)

    def test_MRB_挂物品时不挂检验单也找得到(self):
        """`_for_inspection` 的回落路径：只挂 item_id 的裁定也算数。"""
        got = Q.check_inbound(ITEM, [insp(verdict=S.QC_FAIL)],
                              [mrb(iid="", decision=S.MRB_CONCESSION)])
        self.assertTrue(got["allowed"])

    def test_否定性裁定不得串到别的检验单上(self):
        """★ 另一张检验单的「退货」裁定，不能拿来判这张单子。"""
        got = Q.check_inbound(ITEM, [insp(iid=INS1, verdict=S.QC_FAIL)],
                              [mrb(mid=MRB1, iid=INS2, decision=S.MRB_SCRAP)])
        self.assertFalse(got["allowed"])
        self.assertEqual(got["code"], S.R_MRB_MISSING,
                         "拿了别的检验单的裁定")

    def test_结论字段齐全(self):
        for row in (Q.check_inbound(ITEM, [insp()]),
                    Q.check_inbound(ITEM, []),
                    Q.check_inbound("", [insp()])):
            for k in ("gate", "item_id", "allowed", "checked", "code", "message",
                      "inspection_id", "inspection_type", "inspection_verdict",
                      "mrb_id", "mrb_decision", "as_of", "source_types"):
                self.assertIn(k, row, "结论缺字段 %s" % k)


# ════════════════════════════════════════════════════════════════════
# [E] 门面（service）：G24 的落点在「货物移动」上
# ════════════════════════════════════════════════════════════════════
class TestFacade(unittest.TestCase):
    """`G24` 说「不得**入库**、不得**发货**」—— 约束的是货物移动这个动作。

    所以闸门挂在**库存流水**上（`add_inventory_txn` 按方向选闸门），
    而 `add_goods_receipt`（到货验收）**刻意不加** `G24` 闸：
    验收答「东西到了没有」(`G23`)，质检答「这东西合格不合格」(`G24`)。
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ep_qc_")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.ep = ExecPlane(root=self.tmp)
        self.grant = AUTH.manual_grant(
            tables=tuple(S.TABLE_NAMES), actions=(S.ACTION_APPEND, S.ACTION_UPDATE),
            actor="测试", reason="单元测试")

    # ── 便捷 ──────────────────────────────────────────────────
    def counts(self):
        return {t: len(self.ep.rows(t)) for t in S.TABLE_NAMES}

    def add_insp(self, **kw):
        return self.ep.add_inspection(insp(**kw), mode=APPLY, grant=self.grant)

    def add_mrb(self, **kw):
        return self.ep.add_mrb(mrb(**kw), mode=APPLY, grant=self.grant)

    def move(self, direction, *, item=ITEM, qty=10, **kw):
        return self.ep.add_inventory_txn(txn(direction=direction, item=item, qty=qty, **kw),
                                         mode=APPLY, grant=self.grant)

    # ── 检验单写入 ────────────────────────────────────────────
    def test_检验类型非法一个字都不写(self):
        before = self.counts()
        res = self.ep.add_inspection(insp(itype="大概检过"), mode=APPLY, grant=self.grant)
        self.assertEqual(res.status, "precondition_failed")
        self.assertIn(S.QC_IQC, res.message)
        self.assertEqual(self.counts(), before, "校验不过却写了东西")

    def test_检验结论非法一个字都不写(self):
        before = self.counts()
        res = self.ep.add_inspection(insp(verdict="还行"), mode=APPLY, grant=self.grant)
        self.assertEqual(res.status, "precondition_failed")
        self.assertEqual(self.counts(), before)

    def test_不强制填抽检数(self):
        """★ 强制填等于宣布第三态不存在 —— 而它恰恰最需要被看见。"""
        res = self.add_insp(qty="")
        self.assertTrue(res.ok, "连登记都不许，那第三态就永远不可能出现")
        self.assertEqual(len(self.ep.inspections()), 1)

    def test_过程检验可以登记(self):
        self.assertTrue(self.add_insp(itype=S.QC_IPQC).ok,
                        "IPQC 不作放行依据，但必须能登记")

    # ── MRB 写入 ──────────────────────────────────────────────
    def test_MRB_引用不存在的检验单必须拒绝(self):
        """★ 一个写错号的 MRB 是**永远不会被找到**的。

        它不会报错，只会让闸门一直报「没有裁定」—— 而人手里明明有一条。
        把这种错挡在写入那一刻，比让人对着一个说不通的拒绝信息排查便宜得多。
        """
        before = self.counts()
        res = self.ep.add_mrb(mrb(iid="INS-20261003-9999", decision=S.MRB_CONCESSION),
                              mode=APPLY, grant=self.grant)
        self.assertEqual(res.status, "precondition_failed")
        self.assertIn("不存在", res.message)
        self.assertEqual(self.counts(), before, "引用了不存在的检验单却写进去了")

    def test_MRB_裁定非法必须拒绝(self):
        self.add_insp(verdict=S.QC_FAIL)
        before = self.counts()
        res = self.ep.add_mrb(mrb(decision="再说"), mode=APPLY, grant=self.grant)
        self.assertEqual(res.status, "precondition_failed")
        self.assertIn(S.MRB_CONCESSION, res.message)
        self.assertEqual(self.counts(), before)

    def test_MRB_两只都不挂必须拒绝(self):
        before = self.counts()
        res = self.ep.add_mrb(mrb(iid="", item=""), mode=APPLY, grant=self.grant)
        self.assertEqual(res.status, "precondition_failed")
        self.assertEqual(self.counts(), before)

    def test_正常_MRB_可以登记并按检验单找回(self):
        self.add_insp(verdict=S.QC_FAIL)
        self.assertTrue(self.add_mrb(decision=S.MRB_CONCESSION).ok)
        got = self.ep.check_inbound(ITEM)
        self.assertTrue(got["allowed"], "登记好的裁定没被找回来")

    # ── 库存流水：G24 真正生效的地方 ──────────────────────────
    def test_流水方向非法用既有机制拒绝不自造码(self):
        res = self.ep.add_inventory_txn(txn(direction="transfer"),
                                        mode=APPLY, grant=self.grant)
        self.assertEqual(res.status, "precondition_failed")
        self.assertIn(S.TXN_IN, res.message)
        # ★ 用 `precondition_failed` 这个**既有**状态，而不是为它新造一个原因码：
        #   新造的码只能由本函数产出（判定层没有对应分支），那就是孤码。
        self.assertNotIn("unknown_direction",
                         " ".join(getattr(S, n) for n in S.__all__ if n.startswith("R_")),
                         "为「方向非法」新造了原因码 —— 没有判定分支会产出它")

    def test_入库没有_IQC_必须拒绝且零写入(self):
        """★★ `G24` 核心：没有来料检验合格，货**不得入库**。"""
        before = self.counts()
        res = self.move(S.TXN_IN)
        self.assertEqual(res.status, "precondition_failed")
        self.assertIn(S.GATE_DIRECTION_CN[S.GATE_INBOUND], res.message)
        self.assertEqual(self.counts(), before, "闸门没过却写进了流水")

    def test_入库有_IQC_合格才放行(self):
        self.add_insp(itype=S.QC_IQC, verdict=S.QC_PASS, qty=8)
        res = self.move(S.TXN_IN)
        self.assertTrue(res.ok, res.message)
        self.assertEqual(len(self.ep.inventory_txns()), 1)

    def test_出货只认_OQC_不认_IQC(self):
        """★ 回归：拿了来料检验去放行出货 —— 两者问的不是一个问题。"""
        self.add_insp(itype=S.QC_IQC, verdict=S.QC_PASS, qty=8)
        before = self.counts()
        res = self.move(S.TXN_OUT)
        self.assertEqual(res.status, "precondition_failed")
        self.assertIn(S.GATE_DIRECTION_CN[S.GATE_OUTBOUND], res.message)
        self.assertEqual(self.counts(), before, "拿了 IQC 放行出货")

    def test_出货没有_OQC_必须拒绝(self):
        before = self.counts()
        res = self.move(S.TXN_OUT)
        self.assertEqual(res.status, "precondition_failed")
        self.assertEqual(self.counts(), before)

    def test_出货有_OQC_合格才放行(self):
        self.add_insp(itype=S.QC_OQC, verdict=S.QC_PASS, qty=3)
        self.assertTrue(self.move(S.TXN_OUT).ok)

    def test_不出货也可以不合格品入库(self):
        """反向确认：闸门**只**认方向对应的那种检验。

        一批被 OQC 判不合格的成品，只要它有 IQC 合格记录，
        「入库」这个动作本身不受 `G24` 阻拦 —— 拦住它的是 OQC 那一道门。
        """
        self.add_insp(itype=S.QC_IQC, verdict=S.QC_PASS, qty=8)
        self.add_insp(iid=INS2, itype=S.QC_OQC, verdict=S.QC_FAIL)
        self.assertTrue(self.move(S.TXN_IN).ok, "入库被 OQC 的结论拦了 —— 方向串了")

    def test_被判不合格的货不得出货(self):
        self.add_insp(itype=S.QC_OQC, verdict=S.QC_FAIL)
        before = self.counts()
        res = self.move(S.TXN_OUT)
        self.assertEqual(res.status, "precondition_failed")
        self.assertIn("MRB", res.message, "拒绝理由没说清下一步要补 MRB 裁定")
        self.assertEqual(res.evidence["detail"]["code"], S.R_MRB_MISSING)
        self.assertEqual(self.counts(), before)

    def test_不合格但让步接收可以出货(self):
        self.add_insp(itype=S.QC_OQC, verdict=S.QC_FAIL)
        self.add_mrb(decision=S.MRB_CONCESSION)
        self.assertTrue(self.move(S.TXN_OUT).ok, "让步接收之后仍然出不了货")

    def test_返工裁定出不了货(self):
        self.add_insp(itype=S.QC_OQC, verdict=S.QC_FAIL)
        self.add_mrb(decision=S.MRB_REWORK)
        before = self.counts()
        res = self.move(S.TXN_OUT)
        self.assertEqual(res.status, "precondition_failed")
        self.assertIn(S.MRB_DECISION_CN[S.MRB_REWORK], res.message)
        self.assertEqual(self.counts(), before, "返工的货被放出去了")

    def test_判不了的覆盖面必须挂到写入结果上(self):
        """★★ 「判不了」不得只活在函数返回值里。

        若它不随写入结果带出去，上层拿到的是一个干净的成功结果 ——
        「判不了」被**静默折叠成通过**，正是最不该发生的假指标。
        """
        self.add_insp(itype=S.QC_IQC, verdict=S.QC_PASS, qty="")
        res = self.move(S.TXN_IN)
        self.assertTrue(res.ok)
        slot = res.evidence["__verdict__"]["G24"]
        self.assertFalse(slot["checked"], "覆盖面判不了没有带出来")
        self.assertEqual(slot["code"], S.R_COVERAGE_UNKNOWN)

    def test_判定结论必须带_as_of(self):
        """`DEV-KICKOFF §7`：每步带 ID、带 `as_of`。

        ★ 新实体必须一起遵守 —— 不补 `_AS_OF_FIELDS` 的话它会**静默为空**，
          不报错，只是永远答不上「这个结论是按哪一刻的数据算的」。
        """
        self.add_insp(itype=S.QC_IQC, verdict=S.QC_PASS, qty=8)
        got = self.ep.check_inbound(ITEM)
        self.assertTrue(got["as_of"], "判定结论没带 as_of")
        self.assertGreaterEqual(got["as_of"], NOW)

    def test_门面两个方向的方法都在(self):
        self.add_insp(itype=S.QC_IQC, verdict=S.QC_PASS, qty=1)
        self.assertTrue(self.ep.check_inbound(ITEM)["allowed"])
        self.assertFalse(self.ep.check_outbound(ITEM)["allowed"])
        self.assertEqual(self.ep.check_outbound(ITEM)["source_types"], (S.QC_OQC,))

    def test_到货验收不加_G24_闸(self):
        """★ 验收（`G23`）与质检（`G24`）是两条判据，别合并。

        `add_goods_receipt` 必须**不**受质检记录影响 ——
        一旦把它们绑在一起，「验收通过」就会被读成「质量合格」。
        """
        from application.exec_plane import procurement as PROC
        row = PROC.build_gr_row(po_id="PUR-20261003-0001", project_id="PRJ-20261003-0001",
                                item_id=ITEM, claim_type=S.CLAIM_ARRIVED,
                                quantity_received=10, quantity_ordered=10,
                                state=S.GR_ARRIVED)
        res = self.ep.add_goods_receipt(row, mode=APPLY, grant=self.grant)
        self.assertTrue(res.ok, "到货验收被质检闸拦住了 —— 两条判据被混在一起了")
        self.assertEqual(len(self.ep.goods_receipts()), 1)

    def test_preview_不写入(self):
        self.add_insp(itype=S.QC_IQC, verdict=S.QC_PASS, qty=8)
        res = self.ep.add_inventory_txn(txn(direction=S.TXN_IN), mode=PREVIEW,
                                        grant=self.grant)
        self.assertEqual(res.status, "candidate")
        self.assertEqual(len(self.ep.inventory_txns()), 0)

    def test_无授权时不写入(self):
        self.add_insp(itype=S.QC_IQC, verdict=S.QC_PASS, qty=8)
        res = self.ep.add_inventory_txn(txn(direction=S.TXN_IN), mode=APPLY)
        self.assertEqual(res.status, "blocked")
        self.assertEqual(len(self.ep.inventory_txns()), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
