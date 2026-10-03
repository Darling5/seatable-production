# -*- coding: utf-8 -*-
"""test_exec_plane_inventory.py — `G25` 库存守恒回归测试（批次 2）。

## 不变量

`G25`：**Σ入库 − Σ出库 = 结存。**

## 这套用例真正在防什么

`G25` 的算式只有一行，误以为它简单是最大的风险。它其实是一次**对账**，
而对账有三类失败方式，每一类都不表现为「算错」：

  1. **左边算不全却照样出结论**：某笔流水方向不认识、或数量没填。
     ★ 这时 `Σ` 是个**残缺的和**，拿它去比对会得到「差异 37」这种
       看着挺具体、其实毫无意义的数字。所以这两条必须先拦成「判不了」。
  2. **基准不成立**：没有快照 / 快照里这个物料没有结存。
     ★ 尤其要防「自己跟自己比」—— 若「结存」由本层流水反推，
       `G25` 就退化成 `x = x`，一条永远通过的不变量（比没有更糟）。
  3. **优先级选错**：既有确凿差异、又有些项判不了时报错了那一个，
     导致真正的问题被另一条信息盖住。

## 命名约定

一律用中性占位料号与编号（`ITM-*` / `IVT-*` / `IVS-*`），
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
from application.exec_plane import inventory as INV            # noqa: E402
from application.exec_plane import schema as S                 # noqa: E402

APPLY = C.MODE_APPLY
PREVIEW = C.MODE_PREVIEW

ITEM = "ITM-LNK-0001"
ITEM2 = "ITM-LNK-0002"
ITEM3 = "ITM-LNK-0003"

D1 = "2026-10-01"
D2 = "2026-10-03"


# ── 行构造（纯数据，不碰存储）────────────────────────────────────────
def txn(*, tid="IVT-20261003-0001", item=ITEM, direction=S.TXN_IN, qty=10,
        at=D2, **kw):
    row = {"txn_id": tid, "item_id": item, "direction": direction,
           "qty": qty, "txn_at": at}
    row.update(kw)
    return row


def snap(*, sid="IVS-20261003-0001", item=ITEM, qty=0, as_of=D2, **kw):
    row = {"snapshot_id": sid, "item_id": item, "qty_on_hand": qty, "as_of": as_of}
    row.update(kw)
    return row


# ════════════════════════════════════════════════════════════════════
# [A] 词表
# ════════════════════════════════════════════════════════════════════
class TestWordTables(unittest.TestCase):

    def test_每个原因码都有中文说明(self):
        for code in S.G25_REASON_CN:
            self.assertTrue(str(S.G25_REASON_CN[code]).strip(),
                            "原因码 %s 没有中文说明" % code)

    def test_方向词表只有进出两个(self):
        self.assertEqual(set(S.TXN_DIRECTIONS), {S.TXN_IN, S.TXN_OUT})
        for d in S.TXN_DIRECTIONS:
            self.assertTrue(S.TXN_DIRECTION_CN[d].strip())

    def test_方向与检验闸门方向不是同一组词(self):
        """★ 库存流水的 in/out 是**货物方向**，闸门的 inbound/outbound 是**检验阶段**。

        两者字面接近、概念相邻，最容易被人当成一回事而直接互相赋值 ——
        一旦混用，「出库闸门」可能被问到「入库」上，而那是**静默**发生的
        （两个词表都合法，只是问错了门）。
        """
        self.assertEqual(set(S.TXN_DIRECTIONS) & set(S.GATE_DIRECTIONS), set(),
                         "两组方向词表撞了值 —— 它们语义不同，不该共用字符串")
        self.assertEqual(len(S.GATE_SOURCE_TYPES), len(S.GATE_DIRECTIONS))

    def test_本层每个原因码都必须有判定分支(self):
        known = set(S.G24_REASON_CN) | set(S.G25_REASON_CN)
        for n in [x for x in S.__all__ if x.startswith("R_")]:
            self.assertIn(getattr(S, n), known,
                          "%s 不在任何码表里 —— 孤码" % n)


# ════════════════════════════════════════════════════════════════════
# [B] 每个原因码都必须真的会被返回（反死分支）
# ════════════════════════════════════════════════════════════════════
class TestEveryCodeReachable(unittest.TestCase):

    CASES = [
        ("整份快照都没有 → 判不了",
         S.R_NO_SNAPSHOT, ([txn(qty=10)], [])),
        ("流水方向不认识 → 求和算不全",
         S.R_UNKNOWN_DIRECTION, ([txn(direction="transfer", qty=10)], [snap(qty=10)])),
        ("流水没填数量 → 求和算不全",
         S.R_QTY_UNKNOWN, ([txn(qty="")], [snap(qty=10)])),
        # ★ 这一个物料的结存**必须取 0**：取非零就同时构成「另一个物料有结存却没流水」，
        #   那是一条**确凿差异**，会按优先级盖住「缺结存」。
        #   本用例只想验后者，所以把前者的干扰去干净。
        ("这个物料在快照里没有结存 → 判不了",
         S.R_SNAPSHOT_INCOMPLETE, ([txn(qty=10)], [snap(item=ITEM2, qty=0)])),
        ("Σ 与结存不一致 → 不守恒",
         S.R_BALANCE_MISMATCH, ([txn(qty=10)], [snap(qty=7)])),
    ]

    def test_码表里的每一个码都会被某条路径产出(self):
        reached = {}
        for why, want, (txns, snaps) in self.CASES:
            got = INV.check_balance(txns, snaps)
            self.assertEqual(got["code"], want, "%s：期望 %s，实际 %s" % (why, want, got["code"]))
            reached[want] = why
        missing = sorted(set(S.G25_REASON_CN) - set(reached))
        self.assertEqual(missing, [], "这些码没有任何路径能取到（死分支）：%s" % missing)

    def test_每个码都必须能被单独取到(self):
        produced = set(INV.check_balance(t, s)["code"] for _w, _c, (t, s) in self.CASES)
        self.assertEqual(produced, set(S.G25_REASON_CN))


# ════════════════════════════════════════════════════════════════════
# [C] 汇总：求和算不算得全
# ════════════════════════════════════════════════════════════════════
class TestAggregate(unittest.TestCase):

    def test_按物料分组算进出与净额(self):
        got = INV.aggregate_txns([txn(tid="IVT-20261003-0001", qty=10),
                                  txn(tid="IVT-20261003-0002", direction=S.TXN_OUT, qty=3),
                                  txn(tid="IVT-20261003-0003", item=ITEM2, qty=5)])
        self.assertEqual(got["by_item"][ITEM][S.TXN_IN], 10)
        self.assertEqual(got["by_item"][ITEM][S.TXN_OUT], 3)
        self.assertEqual(got["by_item"][ITEM]["net"], 7)
        self.assertEqual(got["by_item"][ITEM]["count"], 2)
        self.assertEqual(got["by_item"][ITEM2]["net"], 5)
        self.assertEqual(got["count"], 3)

    def test_未知方向的行被单列且不参与求和(self):
        """★ 若不单列，`Σ` 会静默少掉一笔，差异看起来还挺具体。"""
        got = INV.aggregate_txns([txn(tid="IVT-20261003-0001", qty=10),
                                  txn(tid="IVT-20261003-0002", direction="transfer", qty=99)])
        self.assertEqual(len(got["unknown_direction"]), 1)
        self.assertEqual(got["by_item"][ITEM][S.TXN_IN], 10, "未知方向的行被算进去了")
        # 报的是**原始行**，不是计数 —— 报计数没法修
        self.assertEqual(got["unknown_direction"][0]["txn_id"], "IVT-20261003-0002")

    def test_缺数量的行被单列且不参与求和(self):
        for bad in (None, "", "N/A"):
            got = INV.aggregate_txns([txn(tid="IVT-20261003-0001", qty=10),
                                      txn(tid="IVT-20261003-0002", qty=bad)])
            self.assertEqual(len(got["missing_qty"]), 1, "qty=%r 没被识别为缺数量" % (bad,))
            self.assertEqual(got["by_item"][ITEM][S.TXN_IN], 10)

    def test_零数量是有效的不是缺数量(self):
        """★ 「填了零」与「没填」含义相反，不能混。

        把 `0` 当成缺数量，会让一笔**明确的零数量流水**变成「判不了」，
        于是一份本来能对上的账被判成判不了。
        """
        got = INV.aggregate_txns([txn(qty=0), txn(tid="IVT-20261003-0002", qty=10)])
        self.assertEqual(got["missing_qty"], [])
        self.assertEqual(got["by_item"][ITEM][S.TXN_IN], 10)
        self.assertEqual(got["by_item"][ITEM]["count"], 2)

    def test_没有物品号的流水被单列(self):
        got = INV.aggregate_txns([txn(item="", qty=10)])
        self.assertEqual(len(got["missing_qty"]), 1)
        self.assertEqual(got["by_item"], {})

    def test_可按物料过滤(self):
        rows = [txn(tid="IVT-20261003-0001", item=ITEM, qty=10),
                txn(tid="IVT-20261003-0002", item=ITEM2, qty=5)]
        got = INV.aggregate_txns(rows, item_id=ITEM)
        self.assertEqual(list(got["by_item"]), [ITEM])
        self.assertEqual(got["count"], 1)

    def test_结果顺序稳定(self):
        """同一批数据两次汇总，明细顺序必须一致。"""
        rows = [txn(tid="IVT-20261003-0001", qty=1),
                txn(tid="IVT-20261003-0002", direction="transfer", qty=1),
                txn(tid="IVT-20261003-0003", qty="")]
        a = INV.aggregate_txns(rows)
        b = INV.aggregate_txns(list(reversed(rows)))
        self.assertEqual([r["txn_id"] for r in a["unknown_direction"]],
                         [r["txn_id"] for r in b["unknown_direction"]])
        self.assertEqual([r["txn_id"] for r in a["missing_qty"]],
                         [r["txn_id"] for r in b["missing_qty"]])


# ════════════════════════════════════════════════════════════════════
# [D] 对账
# ════════════════════════════════════════════════════════════════════
class TestBalance(unittest.TestCase):

    def test_守恒时必须为真(self):
        got = INV.check_balance([txn(qty=10), txn(tid="IVT-20261003-0002",
                                                  direction=S.TXN_OUT, qty=3)],
                                [snap(qty=7)])
        self.assertTrue(got["balanced"])
        self.assertTrue(got["checked"])
        self.assertEqual(got["code"], "")
        self.assertIn("守恒", got["message"])

    def test_差一件也必须报红(self):
        got = INV.check_balance([txn(qty=10)], [snap(qty=9)])
        self.assertFalse(got["balanced"])
        self.assertTrue(got["checked"], "账实不符是**确凿**结论，不是判不了")
        self.assertEqual(got["code"], S.R_BALANCE_MISMATCH)
        self.assertEqual(got["items"][0]["diff"], 1.0)

    def test_容差必须紧(self):
        """★ 数量是整数件，`TOL` 只吸收浮点误差。

        有人为了让用例过而把它放宽到 0.01 —— 那就等于宣布「差一点点也算守恒」，
        而不变量里没有任何「一点点」。
        """
        self.assertLessEqual(INV.TOL, 1e-6)
        got = INV.check_balance([txn(qty=1.001)], [snap(qty=1)])
        self.assertEqual(got["code"], S.R_BALANCE_MISMATCH, "0.001 的差异被容差吞了")

    def test_浮点误差不算差异(self):
        got = INV.check_balance([txn(qty=0.1), txn(tid="IVT-20261003-0002", qty=0.2)],
                                [snap(qty=0.3)])
        self.assertTrue(got["balanced"], "0.1+0.2 的二进制浮点误差被当成账实不符")

    def test_没有快照判不了且不得报守恒(self):
        got = INV.check_balance([txn(qty=10)], [])
        self.assertFalse(got["balanced"], "没有基准却报了守恒 —— fail-closed 失守")
        self.assertFalse(got["checked"])
        self.assertEqual(got["code"], S.R_NO_SNAPSHOT)

    def test_快照全没有物品号也算没有基准(self):
        got = INV.check_balance([txn(qty=10)], [snap(item="", qty=5)])
        self.assertEqual(got["code"], S.R_NO_SNAPSHOT)
        self.assertIn("物品号", got["message"])

    def test_快照结存全为空也算没有基准(self):
        got = INV.check_balance([txn(qty=10)], [snap(qty="")])
        self.assertEqual(got["code"], S.R_NO_SNAPSHOT)
        self.assertIn("结存", got["message"])

    def test_方向不认识时必须判不了(self):
        """★ 最危险的一种：`Σ` 缺了一笔却照样出结论，差异看起来还挺具体。"""
        got = INV.check_balance([txn(tid="IVT-20261003-0001", qty=10),
                                 txn(tid="IVT-20261003-0002", direction="transfer", qty=7)],
                                [snap(qty=10)])
        self.assertFalse(got["balanced"])
        self.assertFalse(got["checked"])
        self.assertEqual(got["code"], S.R_UNKNOWN_DIRECTION)
        self.assertIn("IVT-20261003-0002", got["message"], "没指出是哪一笔")

    def test_缺数量时必须判不了(self):
        got = INV.check_balance([txn(qty="")], [snap(qty=10)])
        self.assertFalse(got["balanced"])
        self.assertFalse(got["checked"])
        self.assertEqual(got["code"], S.R_QTY_UNKNOWN)

    def test_快照缺这个物料时必须判不了(self):
        """★ 快照里得有这个物料才谈得上对账。

        注意这里的快照行取 `qty=0`：若取非零，就同时构成
        「另一个物料有结存却没流水」那条**确凿差异**，它会按优先级盖住本条。
        """
        got = INV.check_balance([txn(item=ITEM, qty=10)], [snap(item=ITEM2, qty=0)])
        self.assertFalse(got["balanced"])
        self.assertFalse(got["checked"])
        self.assertEqual(got["code"], S.R_SNAPSHOT_INCOMPLETE)
        self.assertIn(ITEM, got["message"])

    def test_快照有流水没有是账实不符不是判不了(self):
        """★ 「结存 50 但流水一笔没有」不是信息不足，是账实不符。"""
        got = INV.check_balance([], [snap(item=ITEM, qty=50)])
        self.assertEqual(got["code"], S.R_BALANCE_MISMATCH)
        self.assertTrue(got["checked"])
        self.assertEqual(got["items"][0]["expected"], 0.0)
        self.assertEqual(got["items"][0]["diff"], -50.0)

    def test_快照为零流水也为零算守恒(self):
        got = INV.check_balance([], [snap(qty=0)])
        self.assertTrue(got["balanced"], "两边都是零就是守恒")

    def test_确凿差异优先于判不了但两者都要可见(self):
        """★ 优先级：确凿差异 > 判不了，且**另一档必须同时报出项数**。

        否则「A 项不符 + B 项判不了」只会显示前者，后者被静默吞掉 ——
        而「还有一批货压根没法对账」是必须有人知道的事。
        """
        got = INV.check_balance(
            [txn(tid="IVT-20261003-0001", item=ITEM, qty=10),
             txn(tid="IVT-20261003-0002", item=ITEM3, qty=4)],
            [snap(item=ITEM, qty=7), snap(sid="IVS-20261003-0002", item=ITEM2, qty=1)])
        self.assertEqual(got["code"], S.R_BALANCE_MISMATCH, "确凿差异被判不了盖住了")
        self.assertTrue(got["checked"])
        self.assertIn("判不了", got["message"], "另一档（判不了）被静默吞掉了")
        self.assertIn(ITEM3, got["message"])
        # 逐项明细里两种状态都得在
        status = {i["item_id"]: i["status"] for i in got["items"]}
        self.assertEqual(status[ITEM], "mismatch")
        self.assertEqual(status[ITEM3], "no_snapshot")

    def test_逐项明细给出算式两端(self):
        got = INV.check_balance([txn(qty=10), txn(tid="IVT-20261003-0002",
                                                   direction=S.TXN_OUT, qty=4)],
                                [snap(qty=6)])
        it = got["items"][0]
        self.assertEqual(it["in"], 10)
        self.assertEqual(it["out"], 4)
        self.assertEqual(it["expected"], 6)
        self.assertEqual(it["on_hand"], 6)
        self.assertEqual(it["status"], "balanced")
        self.assertEqual(it["code"], "")

    def test_快照结存读不出来时报判不了(self):
        got = INV.check_balance([txn(item=ITEM, qty=10), txn(tid="IVT-20261003-0002",
                                                             item=ITEM2, qty=1)],
                                [snap(item=ITEM, qty=10), snap(sid="IVS-20261003-0002",
                                                               item=ITEM2, qty="")])
        self.assertFalse(got["balanced"])
        self.assertEqual(got["code"], S.R_SNAPSHOT_INCOMPLETE)
        self.assertIn(ITEM2, got["message"])

    def test_多物料全对才算守恒(self):
        rows = [txn(tid="IVT-20261003-0001", item=ITEM, qty=10),
                txn(tid="IVT-20261003-0002", item=ITEM2, qty=2)]
        self.assertTrue(INV.check_balance(rows, [snap(item=ITEM, qty=10),
                                                 snap(sid="IVS-20261003-0002",
                                                      item=ITEM2, qty=2)])["balanced"])
        # 其中一个差 1 就不守恒
        got = INV.check_balance(rows, [snap(item=ITEM, qty=10),
                                       snap(sid="IVS-20261003-0002", item=ITEM2, qty=3)])
        self.assertEqual(got["code"], S.R_BALANCE_MISMATCH)

    def test_问单个物料时它没快照是缺结存不是整份没有(self):
        """★ 回归：`item_id` 过滤后为空，会被误报成「一个快照都没有」。

        两者的下一步动作完全不同：一个是「补这一行结存」，
        另一个是「把整份快照接进来」。报错了会让人白干一整天。
        """
        snaps = [snap(item=ITEM2, qty=10)]
        got = INV.check_balance([txn(item=ITEM, qty=10)], snaps, item_id=ITEM)
        self.assertEqual(got["code"], S.R_SNAPSHOT_INCOMPLETE,
                         "把「这个物料没结存」误报成了「整份快照都没有」")

    def test_问单个物料时正常返回(self):
        rows = [txn(tid="IVT-20261003-0001", item=ITEM, qty=10),
                txn(tid="IVT-20261003-0002", item=ITEM2, qty=9)]
        snaps = [snap(item=ITEM, qty=10), snap(sid="IVS-20261003-0002", item=ITEM2, qty=1)]
        got = INV.check_balance(rows, snaps, item_id=ITEM)
        self.assertTrue(got["balanced"], "只问 ITEM 时不该被 ITEM2 的差异连坐")
        self.assertEqual([i["item_id"] for i in got["items"]], [ITEM])

    def test_同一物料多条快照取最新那条(self):
        got = INV.check_balance([txn(qty=10)],
                                [snap(sid="IVS-20261001-0001", qty=3, as_of=D1),
                                 snap(sid="IVS-20261003-0002", qty=10, as_of=D2)])
        self.assertTrue(got["balanced"], "取的不是最新的那条快照")

    def test_as_of_取对账基准的日期(self):
        got = INV.check_balance([txn(qty=10)],
                                [snap(qty=10, as_of=D2), snap(sid="IVS-20261001-0002",
                                                              item=ITEM2, qty=0, as_of=D1)])
        self.assertEqual(got["as_of"], D2)

    def test_结论字段齐全(self):
        for row in (INV.check_balance([txn(qty=10)], [snap(qty=10)]),
                    INV.check_balance([], []),
                    INV.check_balance([txn(direction="x")], [snap(qty=1)])):
            for k in ("item_id", "balanced", "checked", "code", "message",
                      "items", "as_of"):
                self.assertIn(k, row, "结论缺字段 %s" % k)

    def test_判不了与不守恒可以用_checked_分开(self):
        mismatch = INV.check_balance([txn(qty=10)], [snap(qty=9)])
        unknown = INV.check_balance([txn(qty=10)], [])
        self.assertTrue(mismatch["checked"])
        self.assertFalse(unknown["checked"])
        self.assertFalse(mismatch["balanced"])
        self.assertFalse(unknown["balanced"])


# ════════════════════════════════════════════════════════════════════
# [E] 门面：与 G24 串起来
# ════════════════════════════════════════════════════════════════════
class TestFacade(unittest.TestCase):
    """`G25` 是**对账**，不拦任何写入（`G24` 才拦）。

    把它做成写入门禁会逼出一堆「为了写进去而凑数」的流水 ——
    那正好毁掉对账赖以成立的那份数据。
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ep_inv_")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.ep = ExecPlane(root=self.tmp)
        self.grant = AUTH.manual_grant(
            tables=tuple(S.TABLE_NAMES), actions=(S.ACTION_APPEND, S.ACTION_UPDATE),
            actor="测试", reason="单元测试")

    def add_snap(self, **kw):
        # ★ 主键必须逐条唯一：默认值复用会让第二条**覆盖**第一条
        #   （store 按 __row_id__ 折叠），于是「两笔流水」变成「一笔」，
        #   对账结果看着像账实不符，其实是夹具自己把数据写重了。
        kw.setdefault("sid", self._next_id("IVS"))
        return self.ep.add_inventory_snapshot(snap(**kw), mode=APPLY, grant=self.grant)

    def move(self, direction, *, item=ITEM, qty=10, **kw):
        kw.setdefault("tid", self._next_id("IVT"))
        return self.ep.add_inventory_txn(txn(direction=direction, item=item, qty=qty, **kw),
                                         mode=APPLY, grant=self.grant)

    def _next_id(self, prefix):
        self._seq = getattr(self, "_seq", 0) + 1
        return "%s-20261003-%04d" % (prefix, self._seq)

    def ok_inspection(self, itype, **kw):
        row = {"inspection_id": kw.pop("iid", None) or self._next_id("INS"),
               "item_id": kw.pop("item", ITEM), "inspection_type": itype,
               "verdict": S.QC_PASS, "qty_inspected": 8, "inspected_at": D2}
        row.update(kw)
        return self.ep.add_inspection(row, mode=APPLY, grant=self.grant)

    # ── 快照写入与口径归一 ────────────────────────────────────
    def test_快照表已登记在表名映射里(self):
        self.assertIn(S.TB_INVENTORY_SNAPSHOT, S.TABLE_FILES)
        self.assertIn(S.TB_INVENTORY_SNAPSHOT, S.TABLE_NAMES)
        self.assertEqual(len(set(S.TABLE_FILES.values())), len(S.TABLE_FILES),
                         "文件名有重名 —— 两张表会写到同一个文件里")

    def test_total_instock_在入口归一到_qty_on_hand(self):
        """★ 归一化发生在**入口**，判定层只认一套口径。

        若判定层也认 `total_instock`，同一个字段就有了两种可用写法 ——
        一旦两处口径漂移，谁也说不出哪个对（`G30` 防双口径）。
        """
        res = self.ep.add_inventory_snapshot(
            {"snapshot_id": "IVS-20261003-0001", "item_id": ITEM,
             "total_instock": 12, "as_of": D2}, mode=APPLY, grant=self.grant)
        self.assertTrue(res.ok, res.message)
        row = self.ep.inventory_snapshots()[0]
        self.assertEqual(row["qty_on_hand"], 12)
        self.assertNotIn("total_instock", row, "旧列名没被换掉，两种写法同时存在了")

    def test_没给快照ID必须报错而不是静默写入(self):
        """主键缺失沿用 `_key()` 的既有行为：**抛异常**（与 `add_item` /
        `add_payment` / `add_work_order` 一致），不是静默落到一个空主键上。

        ★ 一致性有价值：四个 `add_*` 里三个抛、一个不抛，调用方就得逐个记。
        """
        before = len(self.ep.inventory_snapshots())
        with self.assertRaises(ValueError):
            self.ep.add_inventory_snapshot({"item_id": ITEM, "qty_on_hand": 1},
                                           mode=APPLY, grant=self.grant)
        self.assertEqual(len(self.ep.inventory_snapshots()), before)

    # ── 对账 ──────────────────────────────────────────────────
    def test_全链守恒(self):
        self.ok_inspection(S.QC_IQC)
        self.assertTrue(self.move(S.TXN_IN, qty=10).ok)
        self.ok_inspection(S.QC_OQC, iid="INS-20261003-0002")
        self.assertTrue(self.move(S.TXN_OUT, qty=4).ok)
        self.assertTrue(self.add_snap(qty=6).ok)
        got = self.ep.inventory_balance()
        self.assertTrue(got["balanced"], got["message"])
        self.assertEqual(got["as_of"], D2)

    def test_没有快照时判不了而不是守恒(self):
        self.ok_inspection(S.QC_IQC)
        self.assertTrue(self.move(S.TXN_IN, qty=10).ok)
        got = self.ep.inventory_balance()
        self.assertFalse(got["balanced"], "没有快照却报了守恒")
        self.assertEqual(got["code"], S.R_NO_SNAPSHOT)

    def test_流水账与快照对不上时报出来(self):
        self.ok_inspection(S.QC_IQC)
        self.assertTrue(self.move(S.TXN_IN, qty=10).ok)
        self.add_snap(qty=8, sid="IVS-20261003-0001")
        got = self.ep.inventory_balance()
        self.assertEqual(got["code"], S.R_BALANCE_MISMATCH)
        self.assertIn(ITEM, got["message"])

    def test_库存流水写入受_G24_闸门约束(self):
        """★ 两个不变量的交界面：**没有质检合格，货就进不了库**，
        自然也谈不上守恒 —— 这正是「先过闸、再记账」的顺序。"""
        before = len(self.ep.inventory_txns())
        res = self.move(S.TXN_IN, qty=10)
        self.assertEqual(res.status, "precondition_failed")
        self.assertEqual(len(self.ep.inventory_txns()), before)
        # 补上检验后就能入账
        self.ok_inspection(S.QC_IQC)
        self.assertTrue(self.move(S.TXN_IN, qty=10).ok)

    def test_对账结论带_as_of(self):
        self.ok_inspection(S.QC_IQC)
        self.move(S.TXN_IN, qty=10)
        got = self.ep.inventory_balance()
        self.assertTrue(got["as_of"], "对账结论没带 as_of")
        self.assertEqual(got["as_of"], D2)

    def test_快照没标日期时退回流水时间(self):
        self.ok_inspection(S.QC_IQC)
        self.move(S.TXN_IN, qty=10, at="2026-10-05")
        self.add_snap(qty=10, as_of="")
        got = self.ep.inventory_balance()
        self.assertEqual(got["as_of"], "2026-10-05", "没退回流水时间")

    def test_两处时间都没有就留空不编造(self):
        self.ok_inspection(S.QC_IQC)
        self.move(S.TXN_IN, qty=10, at="")
        self.add_snap(qty=10, as_of="")
        self.assertEqual(self.ep.inventory_balance()["as_of"], "",
                         "两处都取不到却编了一个时间")

    def test_对账不受授权影响也不写入(self):
        """`G25` 是读数之间的比对，不写东西。"""
        before = {t: len(self.ep.rows(t)) for t in S.TABLE_NAMES}
        self.ep.inventory_balance()
        after = {t: len(self.ep.rows(t)) for t in S.TABLE_NAMES}
        self.assertEqual(before, after)

    def test_自己跟自己比必须被识别(self):
        """★ 反面教材：若「结存」由本层流水反推，`G25` 会退化成 `x = x`。

        这里断言的是「对账基准必须来自外部快照表」这一结构事实：
        没有任何流水时，对账结果是**判不了**，而不是「守恒」。
        若哪天有人图省事让它用流水自造结存，这条会立刻变红。
        """
        got = self.ep.inventory_balance()
        self.assertFalse(got["balanced"])
        self.assertEqual(got["code"], S.R_NO_SNAPSHOT)

    def test_逐项明细可查(self):
        self.ok_inspection(S.QC_IQC)
        self.move(S.TXN_IN, qty=10)
        self.add_snap(qty=7)
        it = self.ep.inventory_balance()["items"][0]
        self.assertEqual((it["in"], it["out"], it["on_hand"], it["diff"]),
                         (10.0, 0.0, 7.0, 3.0))


if __name__ == "__main__":
    unittest.main(verbosity=2)
