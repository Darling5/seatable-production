# -*- coding: utf-8 -*-
"""test_exec_plane.py — 执行平面（`L_exec`）回归测试（批次 1：G21 / G22 / G23）。

## 为什么这一层值得单独一套用例

`docs/invariant-coverage.md` §D 的结论是：**「阻碍两者的不是能力，而是实体。」**
`G21` / `G22` / `G23` 的判据在二期早就有了（到货口径、齐套口径、部分到货口径），
缺的是能挂载它们的**实体**与**前置校验**。本批把它们造出来，
所以测试的重点不是「函数算得对」，而是：

  · **前置不满足时，一个字都不许写**（不是「返回一个 False 就完事」）；
  · **判定结论必须能被上层看见**，特别是第三态「判不了」不得静默折叠成「通过」；
  · **本层不许自造枚举值** —— 路由 / 写入动作 / ID 前缀 / 表名，
    凡是有共享注册表的，一律取那边的值。★ 这条是本批实测出来的三个真缺陷的直接回归
    （见下面 `TestNoSelfInventedEnums`）。

## 本文件锁什么

  [A] **不自造枚举值**：路由 ∈ `ROUTE_POLICIES`、动作 ∈ {append, update}、
      前缀并集 == `alignment.KNOWN_PREFIXES`、表名不撞二期、文件名唯一。
  [B] **G21** BOM 结构：无环 / 版本内父件唯一 / 用量守恒 / 替代料显式受控
      —— 每条性质都有「注入反例必须报红」用例。
  [C] **G23** 付款前置：付款 ⟹ 存在覆盖该笔付款的到货验收记录。
  [D] **G22** 齐套开工：开工 ⟹ 齐套 = 100% ∨ 显式缺料放行授权。
  [E] **门面（service）**：preview 不写 / 无授权 blocked 且零写入 / 乐观锁 / 状态机。

## 命名约定

用例里一律用**中性占位料号与编号**（`ITM-*` / `PUR-*` / `WKO-*`），
不出现任何真实供应商、客户或人名 —— 与去敏守卫（`tests/test_smoke.py [09b]`）同一纪律。
"""

import datetime
import os
import re
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

from application import authorization as AUTH                  # noqa: E402
from application import contracts as C                         # noqa: E402
from application.decision_support import alignment as A        # noqa: E402
from application.exec_plane import ExecPlane                   # noqa: E402
from application.exec_plane import bom as BOM                  # noqa: E402
from application.exec_plane import procurement as PROC         # noqa: E402
from application.exec_plane import schema as S                 # noqa: E402
from application.exec_plane import work_order as WO            # noqa: E402
from application.exec_plane.store import ExecTableStore        # noqa: E402
from application.project_brain import actions as ACT           # noqa: E402
from application.project_brain import ids as PBIDS             # noqa: E402
from application.project_brain import schema as PBS            # noqa: E402

_NOW = datetime.datetime(2026, 10, 3, 12, 0, 0)
APPLY = C.MODE_APPLY
PREVIEW = C.MODE_PREVIEW

PRJ = "PRJ-20261003-0001"
PO = "PUR-20261003-0001"
FG = "ITM-FG"
CT = "ITM-CT"


# ════════════════════════════════════════════════════════════════════
# 夹具
# ════════════════════════════════════════════════════════════════════
class _Base(unittest.TestCase):
    """每个用例独立临时目录 + 一份覆盖全部表的授权。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ep_test_")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.ep = ExecPlane(root=self.tmp)
        self.grant = AUTH.manual_grant(
            tables=tuple(S.TABLE_NAMES), actions=(S.ACTION_APPEND, S.ACTION_UPDATE),
            actor="测试", reason="单元测试")

    # ── 便捷构造 ──────────────────────────────────────────────
    def item(self, item_no="LNK-0001", item_kind=BOM.ITEM_RAW):
        return BOM.build_item_row(item_no=item_no, name="占位料", item_kind=item_kind)

    def bom(self, parent, child, *, version="A", status=BOM.BOM_RELEASED,
            qty=1, scrap=0, **kw):
        """造一条「一个父件 + 一行子件」的 BOM，返回 (版本行, 行行)。"""
        v = BOM.build_bom_version_row(parent_item_id=parent, version=version,
                                      status=status, project_id=PRJ)
        ln = BOM.build_bom_line_row(bom_id=v["bom_id"], child_item_id=child,
                                    quantity_per=qty, scrap_rate=scrap, **kw)
        return v, ln

    def put(self, parent, child, *, mode=APPLY, grant=None, **kw):
        v, ln = self.bom(parent, child, **kw)
        return self.ep.put_bom(v, [ln], mode=mode,
                               grant=self.grant if grant is None else grant)

    def gr(self, claim, *, item=CT, got="", ordered="", accepted="",
           state=S.GR_ARRIVED, po=PO):
        return PROC.build_gr_row(po_id=po, project_id=PRJ, item_id=item,
                                 claim_type=claim, quantity_received=got,
                                 quantity_ordered=ordered, quantity_accepted=accepted,
                                 state=state)

    def add_gr(self, **kw):
        return self.ep.add_goods_receipt(self.gr(**kw), mode=APPLY, grant=self.grant)

    def payment(self, *, po=PO, qty="", amount=100):
        return PROC.build_payment_row(po_id=po, project_id=PRJ, amount=amount, quantity=qty)

    def work_order(self, parent=FG, qty=10, **kw):
        return WO.build_work_order_row(project_id=PRJ, parent_item_id=parent,
                                       quantity=qty, **kw)

    def counts(self):
        return {t: len(self.ep.rows(t)) for t in S.TABLE_NAMES}


# ════════════════════════════════════════════════════════════════════
# [A] 不自造枚举值（★ 本批三个真缺陷的直接回归）
# ════════════════════════════════════════════════════════════════════
class TestNoSelfInventedEnums(_Base):
    """凡是仓库里有**共享注册表**的取值，本层必须取那边的值。

    本批初版在三个地方自造了取值，且**三处都是静默失效**
    （不报错、不抛异常，只是每次写入都变成 blocked）：

        ① `WRITE_ROUTE = "local"` —— `ROUTE_POLICIES` 里没有；
        ② `action="item_add"` 等业务动作名 —— 只认 append/update；
        ③ 直接复用 `LocalMemoryAdapter` —— 它的表名表是写死在 import 上的，
           执行平面的表名不在其中，`path_of` 直接 KeyError。

    下面这组用例是这三条的回归。
    """

    def test_写入路由必须在共享注册表里(self):
        self.assertIn(S.WRITE_ROUTE, C.ROUTE_POLICIES,
                      "自造的路由会被 DataService 第一句就判「未知路由」并 blocked")
        # 顺带：路由是「授权策略」的名字，不是「副作用分级」的名字，别把两者混用
        self.assertNotIn(S.WRITE_ROUTE, (C.SIDE_LOCAL_APPEND, C.SIDE_READ_ONLY),
                         "local_append 是 side-effect 分级，不能当路由名用")

    def test_写入动作只能是规范的两个(self):
        self.assertEqual(S.ACTION_APPEND, "append")
        self.assertEqual(S.ACTION_UPDATE, "update")
        # 与二期取同一个值，不是「恰好相同」
        self.assertEqual(S.ACTION_APPEND, PBS.ACTION_APPEND)
        self.assertEqual(S.ACTION_UPDATE, PBS.ACTION_UPDATE)

    def test_路由与二期同源(self):
        self.assertEqual(S.WRITE_ROUTE, PBS.WRITE_ROUTE,
                         "两层本地表用同一套授权策略，不该各写一个字面量")

    def test_service_源码里不出现自造动作名(self):
        """★ 源码级守卫：`service.py` 里 `action=` 的取值只许是规范常量。

        只测行为不够 —— 行为测试会漏掉「新加了一个 add_xxx 但没人调」。
        这条直接扫源码，任何 `action="xxx"` 字面量都会红。
        """
        path = os.path.join(HERE, "application", "exec_plane", "service.py")
        with open(path, "r", encoding="utf-8") as fh:
            src = fh.read()
        literals = re.findall(r"action\s*=\s*([\"'][^\"']*[\"'])", src)
        self.assertEqual(literals, [],
                         "service.py 用了字面量动作名 %s；"
                         "必须用 S.ACTION_APPEND / S.ACTION_UPDATE" % literals)
        # 反向自检：这个正则**确实能抓到**违规写法（防守卫退化成永远绿）
        self.assertEqual(
            re.findall(r"action\s*=\s*([\"'][^\"']*[\"'])", 'x = f(action="item_add")'),
            ['"item_add"'], "正则本身失效，守卫会静默放行")

    def test_非规范动作被门面直接拒绝(self):
        """行为侧兜底：绕过源码扫描也拦得住。"""
        row = self.item()
        with self.assertRaises(ValueError):
            self.ep._write(S.TB_ITEM, row, action="item_add", mode=APPLY,
                           grant=self.grant, row_id=row["item_id"])

    def test_表名不与二期撞车(self):
        """两层的本地库各有各的目录，但表名撞了就会有人误以为同源。"""
        self.assertEqual(set(S.TABLE_NAMES) & set(PBS.TABLE_NAMES), set(),
                         "执行平面与二期不能有同名表")

    def test_每个表名映射到唯一文件名(self):
        files = list(S.TABLE_FILES.values())
        self.assertEqual(len(files), len(set(files)), "表文件名有重复")
        self.assertEqual(len(S.TABLE_NAMES), len(S.TABLE_FILES))

    def test_适配器认识执行平面的表且拒绝未知表(self):
        store = ExecTableStore(self.tmp)
        names = set()
        for t in S.TABLE_NAMES:
            p = store.path_of(t)
            self.assertTrue(p.endswith(".jsonl"))
            names.add(p)
        self.assertEqual(len(names), len(S.TABLE_NAMES))
        with self.assertRaises(KeyError):
            store.path_of("不存在的表")
        # 二期那张表名表里的表，这里也必须**不认识**（防止误落到同一批文件）
        with self.assertRaises(KeyError):
            store.path_of(PBS.TB_MEMORY)

    def test_前缀并集恒等于跨层登记表(self):
        """G29：三张前缀表的并集 == 接缝层的 KNOWN_PREFIXES。

        本层新增了 7 个 kind，任一个没同步就会在这里红 ——
        否则新的 ID 会被跨层校验**静默拒绝**。
        """
        expect = (set(S.KIND_PREFIX.values()) | set(C._ID_PREFIX.values())
                  | set(PBIDS.KIND_PREFIX.values()))
        self.assertEqual(set(A.KNOWN_PREFIXES), expect,
                         "仅在业务层有：%s；仅在接缝层有：%s"
                         % (sorted(expect - set(A.KNOWN_PREFIXES)),
                            sorted(set(A.KNOWN_PREFIXES) - expect)))

    def test_本层新生成_ID_全部跨层合法(self):
        for kind in sorted(S.KIND_PREFIX):
            v = S.new_id(kind, now=_NOW, seq=7)
            self.assertTrue(A.is_canonical_id(v), "%s 生成 %s 过不了跨层校验" % (kind, v))
            self.assertTrue(S.validate_id(kind, v))
            # 前缀必须与本类型自洽：拿别的 kind 的名字去校验同一串必须失败
            for other in sorted(S.KIND_PREFIX):
                if other == kind:
                    continue
                self.assertFalse(S.validate_id(other, v),
                                 "%s 的 ID 被 %s 认了" % (kind, other))

    def test_状态迁移表内部自洽(self):
        for name, states, trans in (("GR", S.GR_STATES, S.GR_TRANSITIONS),
                                    ("PAY", S.PAY_STATES, S.PAY_TRANSITIONS),
                                    ("WO", S.WO_STATES, S.WO_TRANSITIONS)):
            self.assertEqual(set(trans), set(states),
                             "%s 迁移表的键集合必须恒等于状态集" % name)
            for src, dsts in trans.items():
                self.assertNotIn(src, dsts, "%s：%s 不该有自环" % (name, src))
                self.assertTrue(set(dsts) <= set(states),
                                "%s：%s → %s 里有未知状态" % (name, src, dsts))

    def test_迁移表的强制状态写死在测试里(self):
        """★ 「声明了但没强制」必须是一笔明账，不能靠读者猜。

        本批只有工单迁移被真正强制（`release_work_order` 会读它）；
        GR / PAY 的迁移表是**设计结论**，本批没有改它们状态的写路径。
        将来接线时这条会红，提醒把 schema.py 的说明与这里一起改。
        """
        self.assertIn("WO_TRANSITIONS", _read_source("service.py"),
                      "工单迁移表应被 service 使用")
        for name in ("GR_TRANSITIONS", "PAY_TRANSITIONS"):
            self.assertNotIn(name, _read_source("service.py"),
                             "%s 现在被 service 用了 —— 请更新 schema.py 的"
                             "「仅声明」说明与本断言" % name)


def _read_source(*parts):
    path = os.path.join(HERE, "application", "exec_plane", *parts)
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


# ════════════════════════════════════════════════════════════════════
# [B] G21：BOM 结构
# ════════════════════════════════════════════════════════════════════
class TestG21BomStructure(_Base):
    """`G21`：无环、版本内父件唯一、用量守恒、替代料显式受控。"""

    # ── ① 无环 ────────────────────────────────────────────────
    def test_逐步累加成环时最后一次必须被拒且零写入(self):
        """反例：A→B、B→C，再写 C→A。第三次必须被挡，且**库里不能多出行**。"""
        self.assertTrue(self.put("ITM-A", "ITM-B").ok)
        self.assertTrue(self.put("ITM-B", "ITM-C").ok)
        before = self.counts()
        res = self.put("ITM-C", "ITM-A")
        self.assertEqual(res.status, "precondition_failed")
        self.assertEqual(res.evidence["code"], "G21")
        self.assertIn(BOM.BOM_CYCLE, [i["code"] for i in res.evidence["issues"]])
        self.assertEqual(self.counts(), before, "前置不过却写进去了")

    def test_自环必须报红(self):
        res = self.put("ITM-X", "ITM-X")
        self.assertEqual(res.status, "precondition_failed")
        codes = [i["code"] for i in res.evidence["issues"]]
        self.assertIn(BOM.BOM_SELF_REFERENCE, codes)

    def test_放行后校验器应认为结构合规(self):
        """正向：合规写入后 `validate_bom` 必须 ok —— 防「一律报红」的假守卫。"""
        self.assertTrue(self.put(FG, CT).ok)
        v = self.ep.validate_bom()
        self.assertTrue(v["ok"], v["issues"])
        self.assertEqual(v["n_by_code"], {})

    def test_已落库的坏结构会被校验器看见(self):
        """手工绕过 `put_bom` 直接落两条互为父子的 BOM，校验器必须抓到环。"""
        self.ep.store.append_row(S.TB_BOM_VERSION,
                                 {"__row_id__": "B1", "bom_id": "B1",
                                  "parent_item_id": "ITM-A", "version": "A",
                                  "status": BOM.BOM_RELEASED})
        self.ep.store.append_row(S.TB_BOM_VERSION,
                                 {"__row_id__": "B2", "bom_id": "B2",
                                  "parent_item_id": "ITM-B", "version": "A",
                                  "status": BOM.BOM_RELEASED})
        for lid, bid, child in (("L1", "B1", "ITM-B"), ("L2", "B2", "ITM-A")):
            self.ep.store.append_row(S.TB_BOM_LINE,
                                     {"__row_id__": lid, "line_id": lid, "bom_id": bid,
                                      "child_item_id": child, "quantity_per": 1,
                                      "scrap_rate": 0})
        v = self.ep.validate_bom()
        self.assertFalse(v["ok"], "已落库的环没被抓到")
        self.assertIn(BOM.BOM_CYCLE, v["n_by_code"])

    def test_explode_遇环不返回看似完整的展开表(self):
        """★ 环上算出来的用量是错的，绝不能放在 `requirements` 里。"""
        vs = [{"bom_id": "B1", "parent_item_id": "P", "version": "A",
               "status": BOM.BOM_RELEASED},
              {"bom_id": "B2", "parent_item_id": "C", "version": "A",
               "status": BOM.BOM_RELEASED}]
        ls = [{"line_id": "L1", "bom_id": "B1", "child_item_id": "C",
               "quantity_per": 1, "scrap_rate": 0},
              {"line_id": "L2", "bom_id": "B2", "child_item_id": "P",
               "quantity_per": 1, "scrap_rate": 0}]
        e = BOM.explode(vs, ls, "P", quantity=1)
        self.assertFalse(e["ok"])
        self.assertTrue(e["cycles"], "环没被识别")
        self.assertEqual(e["requirements"], {}, "有环时 requirements 必须为空")
        self.assertTrue(e["partial_requirements"], "算到一半的结果应另存并标注")

    # ── ② 版本内父件唯一 ──────────────────────────────────────
    def test_同父件同版本双声明必须报红(self):
        v1 = BOM.build_bom_version_row(parent_item_id="P", version="A",
                                       status=BOM.BOM_RELEASED)
        v2 = BOM.build_bom_version_row(parent_item_id="P", version="A",
                                       status=BOM.BOM_RELEASED)
        out = BOM.validate_bom([v1, v2], [])
        self.assertFalse(out["ok"])
        self.assertIn(BOM.BOM_DUP_PARENT_VERSION, out["n_by_code"])

    def test_同父件不同版本应放行(self):
        v1 = BOM.build_bom_version_row(parent_item_id="P", version="A",
                                       status=BOM.BOM_RELEASED)
        v2 = BOM.build_bom_version_row(parent_item_id="P", version="B",
                                       status=BOM.BOM_RELEASED)
        self.assertTrue(BOM.validate_bom([v1, v2], [])["ok"])

    def test_同版本同子件重复必须报红(self):
        v = BOM.build_bom_version_row(parent_item_id="P", version="A",
                                      status=BOM.BOM_RELEASED)
        a = BOM.build_bom_line_row(bom_id=v["bom_id"], child_item_id="C", quantity_per=1)
        b = BOM.build_bom_line_row(bom_id=v["bom_id"], child_item_id="C", quantity_per=2)
        out = BOM.validate_bom([v], [a, b])
        self.assertIn(BOM.BOM_DUP_CHILD, out["n_by_code"])

    def test_put_bom_行ID重复必须被拒(self):
        v, ln = self.bom(FG, CT)
        dup = dict(ln)                       # 同一个 line_id 再来一次
        res = self.ep.put_bom(v, [ln, dup], mode=APPLY, grant=self.grant)
        self.assertEqual(res.status, "precondition_failed")

    def test_行指向不存在的_BOM_必须报红(self):
        out = BOM.validate_bom([], [{"line_id": "L", "bom_id": "NOPE",
                                     "child_item_id": "C", "quantity_per": 1,
                                     "scrap_rate": 0}])
        self.assertIn(BOM.BOM_LINE_UNKNOWN_BOM, out["n_by_code"])

    def test_版本状态非法必须报红(self):
        out = BOM.validate_bom([{"bom_id": "B", "parent_item_id": "P", "version": "A",
                                 "status": "半成品"}], [])
        self.assertIn(BOM.BOM_VERSION_STATUS_INVALID, out["n_by_code"])

    # ── ③ 用量守恒 ────────────────────────────────────────────
    def test_用量非法必须报红(self):
        v = BOM.build_bom_version_row(parent_item_id="P", version="A",
                                      status=BOM.BOM_RELEASED)
        for bad in (0, -1, -0.5, "abc", "", None):
            ln = {"line_id": "L", "bom_id": v["bom_id"], "child_item_id": "C",
                  "quantity_per": bad, "scrap_rate": 0}
            out = BOM.validate_bom([v], [ln])
            self.assertIn(BOM.BOM_QTY_INVALID, out["n_by_code"], "用量 %r 未被拦" % (bad,))

    def test_损耗率越界或非数字必须报红(self):
        v = BOM.build_bom_version_row(parent_item_id="P", version="A",
                                      status=BOM.BOM_RELEASED)
        for bad in (1.0, 1.5, -0.1, "x"):
            ln = {"line_id": "L", "bom_id": v["bom_id"], "child_item_id": "C",
                  "quantity_per": 1, "scrap_rate": bad}
            self.assertIn(BOM.BOM_SCRAP_INVALID, BOM.validate_bom([v], [ln])["n_by_code"],
                          "损耗率 %r 未被拦" % (bad,))

    def test_损耗率空值视为零损耗(self):
        """空 ≠ 非法：没填损耗率是正常输入，不该报红（否则守卫会被无视）。"""
        v = BOM.build_bom_version_row(parent_item_id="P", version="A",
                                      status=BOM.BOM_RELEASED)
        ln = {"line_id": "L", "bom_id": v["bom_id"], "child_item_id": "C",
              "quantity_per": 1, "scrap_rate": ""}
        self.assertTrue(BOM.validate_bom([v], [ln])["ok"])

    def test_explode_用量数学正确(self):
        """FG --2(损耗10%)--> C1 --3--> C3；另有 C2 用量 1。取 10 台。"""
        vs = [{"bom_id": "B1", "parent_item_id": "FG", "version": "A",
               "status": BOM.BOM_RELEASED},
              {"bom_id": "B2", "parent_item_id": "C1", "version": "A",
               "status": BOM.BOM_RELEASED}]
        ls = [{"line_id": "L1", "bom_id": "B1", "child_item_id": "C1",
               "quantity_per": 2, "scrap_rate": 0.1},
              {"line_id": "L2", "bom_id": "B1", "child_item_id": "C2",
               "quantity_per": 1, "scrap_rate": 0},
              {"line_id": "L3", "bom_id": "B2", "child_item_id": "C3",
               "quantity_per": 3, "scrap_rate": 0}]
        e = BOM.explode(vs, ls, "FG", quantity=10)
        self.assertTrue(e["ok"])
        self.assertAlmostEqual(e["requirements"]["C1"], 22.0)   # 10×2×1.1
        self.assertAlmostEqual(e["requirements"]["C2"], 10.0)
        self.assertAlmostEqual(e["requirements"]["C3"], 66.0)   # 22×3
        # 父件自己不作为需求出现
        self.assertNotIn("FG", e["requirements"])

    def test_explode_跳过作废版本(self):
        vs = [{"bom_id": "B1", "parent_item_id": "FG", "version": "A",
               "status": BOM.BOM_OBSOLETE}]
        ls = [{"line_id": "L1", "bom_id": "B1", "child_item_id": "C",
               "quantity_per": 1, "scrap_rate": 0}]
        self.assertEqual(BOM.explode(vs, ls, "FG")["requirements"], {})

    def test_explode_可按版本过滤(self):
        vs = [{"bom_id": "B1", "parent_item_id": "FG", "version": "A",
               "status": BOM.BOM_RELEASED},
              {"bom_id": "B2", "parent_item_id": "FG", "version": "B",
               "status": BOM.BOM_RELEASED}]
        ls = [{"line_id": "L1", "bom_id": "B1", "child_item_id": "CA",
               "quantity_per": 1, "scrap_rate": 0},
              {"line_id": "L2", "bom_id": "B2", "child_item_id": "CB",
               "quantity_per": 1, "scrap_rate": 0}]
        self.assertEqual(list(BOM.explode(vs, ls, "FG", version="B")["requirements"]),
                         ["CB"])

    # ── ④ 替代料显式受控 ──────────────────────────────────────
    def _lines(self, *specs):
        v = BOM.build_bom_version_row(parent_item_id="P", version="A",
                                      status=BOM.BOM_RELEASED)
        rows = []
        for i, (child, primary, group) in enumerate(specs):
            rows.append({"line_id": "L%d" % i, "bom_id": v["bom_id"],
                         "child_item_id": child, "quantity_per": 1, "scrap_rate": 0,
                         "substitute_group": group, "is_primary": primary})
        return v, rows

    def test_替代组必须恰有一个主料(self):
        v, rows = self._lines(("C1", False, "G"), ("C2", False, "G"))
        self.assertIn(BOM.BOM_SUBSTITUTE_NO_PRIMARY, BOM.validate_bom([v], rows)["n_by_code"])
        v, rows = self._lines(("C1", True, "G"), ("C2", True, "G"))
        self.assertIn(BOM.BOM_SUBSTITUTE_MULTI_PRIMARY,
                      BOM.validate_bom([v], rows)["n_by_code"])

    def test_替代组不得跨父件或跨版本(self):
        v1 = BOM.build_bom_version_row(parent_item_id="P", version="A",
                                       status=BOM.BOM_RELEASED)
        v2 = BOM.build_bom_version_row(parent_item_id="Q", version="A",
                                       status=BOM.BOM_RELEASED)
        rows = [{"line_id": "L1", "bom_id": v1["bom_id"], "child_item_id": "C1",
                 "quantity_per": 1, "scrap_rate": 0, "substitute_group": "G",
                 "is_primary": True},
                {"line_id": "L2", "bom_id": v2["bom_id"], "child_item_id": "C2",
                 "quantity_per": 1, "scrap_rate": 0, "substitute_group": "G",
                 "is_primary": False}]
        self.assertIn(BOM.BOM_SUBSTITUTE_CROSS_PARENT,
                      BOM.validate_bom([v1, v2], rows)["n_by_code"])

    def test_合法替代组应放行(self):
        v, rows = self._lines(("C1", True, "G"), ("C2", False, "G"))
        self.assertTrue(BOM.validate_bom([v], rows)["ok"],
                        BOM.validate_bom([v], rows)["issues"])

    def test_独立用量行不受替代组规则影响(self):
        v, rows = self._lines(("C1", True, ""), ("C2", True, ""))
        self.assertTrue(BOM.validate_bom([v], rows)["ok"])


# ════════════════════════════════════════════════════════════════════
# [C] G23：付款前置
# ════════════════════════════════════════════════════════════════════
class TestG23PaymentPrecondition(_Base):
    """`G23`：付款 ⟹ 存在**覆盖该笔付款的**到货验收记录。"""

    def _code(self, pay, grs):
        ok, code, _ = PROC.check_payment_precondition(pay, grs)
        return ok, code

    # ── 结构 ──────────────────────────────────────────────────
    def test_付款必须能追溯到采购订单(self):
        self.assertEqual(self._code({}, []), (False, PROC.R_NO_PO))
        self.assertEqual(self._code({"po_id": "   "}, []), (False, PROC.R_NO_PO))

    def test_PO号形态非法必须报红(self):
        """★ 回归：本层必须先判形态，否则这条原因码永远取不到（死分支）。"""
        for bad in ("PO-20261003-0001", "ABC123", "PUR-2026100-0001"):
            self.assertEqual(self._code({"po_id": bad}, []),
                             (False, PROC.R_PO_ID_INVALID),
                             "%r 应被判形态非法" % bad)

    def test_历史形态可显式放宽(self):
        """只读兼容：`PO-…` 是已废弃形态，读旧数据时可以放行，但要显式开口。"""
        ok, code, _ = PROC.check_payment_precondition(
            {"po_id": "PO-20261003-0001"}, [], require_canonical_po_id=False)
        self.assertFalse(ok)
        self.assertEqual(code, PROC.R_NO_GOODS_RECEIPT, "放宽形态后应继续往下判 GR")

    def test_无GR不得付款(self):
        self.assertEqual(self._code(self.payment(), []),
                         (False, PROC.R_NO_GOODS_RECEIPT))

    def test_别人的GR不算数(self):
        other = self.gr(S.CLAIM_ARRIVED, got=10, ordered=10, accepted=10,
                        state=S.GR_ACCEPTED, po="PUR-20261003-9999")
        self.assertEqual(self._code(self.payment(), [other]),
                         (False, PROC.R_NO_GOODS_RECEIPT))

    # ── 到货 / 验收口径（全部复用二期）─────────────────────────
    def test_已发货不等于已到货(self):
        """★ 二期铁律：`claimed = shipped` 不证明到货。"""
        for claim in (S.CLAIM_SHIPPED, S.CLAIM_IN_TRANSIT):
            g = self.gr(claim, got=10, ordered=10)
            self.assertEqual(self._code(self.payment(), [g]),
                             (False, PROC.R_GR_NOT_ARRIVED), "口径 %s" % claim)

    def test_部分到货不足以付款(self):
        for g in (self.gr(S.CLAIM_PARTIAL, got=5, ordered=10),
                  self.gr(S.CLAIM_ARRIVED, got=5, ordered=10)):
            self.assertEqual(self._code(self.payment(), [g]),
                             (False, PROC.R_GR_PARTIAL))

    def test_齐套到货但未验收不得付款(self):
        g = self.gr(S.CLAIM_ARRIVED, got=10, ordered=10)
        self.assertEqual(self._code(self.payment(), [g]),
                         (False, PROC.R_GR_NOT_ACCEPTED))

    def test_拒收不得付款(self):
        g = self.gr(S.CLAIM_ARRIVED, got=10, ordered=10, accepted=0,
                    state=S.GR_REJECTED)
        self.assertEqual(self._code(self.payment(), [g]),
                         (False, PROC.R_GR_REJECTED))

    def test_已取消的GR不算数(self):
        g = self.gr(S.CLAIM_ARRIVED, got=10, ordered=10, accepted=10,
                    state=S.GR_CANCELLED)
        self.assertEqual(self._code(self.payment(), [g]),
                         (False, PROC.R_GR_CANCELLED))

    def test_每个原因码都能被单独取到(self):
        """★ 反「死分支」：逐条 rank 都必须可达，否则 `__all__` 里的码是假的。"""
        cases = [
            (dict(claim=S.CLAIM_ARRIVED, got=10, ordered=10, accepted=10,
                  state=S.GR_CANCELLED), PROC.R_GR_CANCELLED),
            (dict(claim=S.CLAIM_ARRIVED, got=10, ordered=10, accepted=0,
                  state=S.GR_REJECTED), PROC.R_GR_REJECTED),
            (dict(claim=S.CLAIM_SHIPPED, got=10, ordered=10), PROC.R_GR_NOT_ARRIVED),
            (dict(claim=S.CLAIM_IN_TRANSIT, got=10, ordered=10), PROC.R_GR_NOT_ARRIVED),
            (dict(claim=S.CLAIM_PARTIAL, got=5, ordered=10), PROC.R_GR_PARTIAL),
            (dict(claim=S.CLAIM_ARRIVED, got=5, ordered=10), PROC.R_GR_PARTIAL),
            (dict(claim=S.CLAIM_ARRIVED, got=10, ordered=10), PROC.R_GR_NOT_ACCEPTED),
            (dict(claim=S.CLAIM_FULL, got="", ordered=""), PROC.R_GR_NOT_ACCEPTED),
        ]
        for i, (kw, expect) in enumerate(cases):
            po = "PUR-20261003-%04d" % (2000 + i)
            g = self.gr(po=po, **kw)
            self.assertEqual(self._code({"po_id": po}, [g]), (False, expect),
                             "case %d (%s) 没报到预期原因码" % (i, kw.get("claim")))
        # 数量不足这条走的是另一条判定
        g = self.gr(S.CLAIM_ARRIVED, got=10, ordered=10, accepted=10,
                    state=S.GR_ACCEPTED)
        v = PROC.check_payment_amount(self.payment(qty=99), [g])
        self.assertTrue(v["checked"])
        self.assertEqual(v["code"], PROC.R_GR_QUANTITY_INSUFFICIENT)

    def test_一个可付的GR不被坏的GR连坐(self):
        good = self.gr(S.CLAIM_ARRIVED, got=10, ordered=10, accepted=10,
                       state=S.GR_ACCEPTED)
        bad = self.gr(S.CLAIM_PARTIAL, got=1, ordered=10)
        ok, code, _ = PROC.check_payment_precondition(self.payment(), [bad, good])
        self.assertTrue(ok, "有可付 GR 却被另一个坏 GR 连坐：%s" % code)

    def test_合法齐套加验收通过应放行(self):
        g = self.gr(S.CLAIM_ARRIVED, got=10, ordered=10, accepted=10,
                    state=S.GR_ACCEPTED)
        ok, code, msg = PROC.check_payment_precondition(self.payment(qty=10), [g])
        self.assertTrue(ok, msg)
        self.assertEqual(code, "")

    # ── 数量覆盖与第三态「判不了」─────────────────────────────
    def test_超付必须报数量不足(self):
        g = self.gr(S.CLAIM_ARRIVED, got=10, ordered=10, accepted=10,
                    state=S.GR_ACCEPTED)
        v = PROC.check_payment_amount(self.payment(qty=11), [g])
        self.assertTrue(v["checked"])
        self.assertFalse(v["ok"])
        self.assertEqual(v["code"], PROC.R_GR_QUANTITY_INSUFFICIENT)

    def test_判不了必须是显式第三态且不阻断(self):
        """★ 「判不了」不许静默当成通过，也不许静默当成不通过。"""
        g = self.gr(S.CLAIM_ARRIVED, got=10, ordered=10, state=S.GR_ACCEPTED)
        v = PROC.check_payment_amount(self.payment(qty=""), [g])     # 付款单没填数量
        self.assertFalse(v["checked"], "没数量却声称已判")
        self.assertTrue(v["ok"], "「判不了」不该阻断")
        self.assertIn("未判定", v["message"])
        # 另一侧：GR 没填已验收数量
        g2 = self.gr(S.CLAIM_ARRIVED, got=10, ordered=10, state=S.GR_ACCEPTED)
        v2 = PROC.check_payment_amount(self.payment(qty=5), [g2])
        self.assertFalse(v2["checked"])

    def test_can_pay_把第三态原样带出(self):
        g = self.gr(S.CLAIM_ARRIVED, got=10, ordered=10, state=S.GR_ACCEPTED)
        out = PROC.can_pay(self.payment(qty=""), [g])
        self.assertTrue(out["allowed"])
        self.assertIs(out["amount"]["checked"], False,
                      "checked=False 被折叠掉了 —— 上层再也看不见「判不了」")

    def test_服务层把第三态挂到写入结果上(self):
        """★ 本批实测缺陷的回归：写入成功但结论不可见 = 假指标。"""
        self.add_gr(claim=S.CLAIM_ARRIVED, got=10, ordered=10, state=S.GR_ACCEPTED)
        res = self.ep.add_payment(self.payment(qty=""), mode=APPLY, grant=self.grant)
        self.assertTrue(res.ok)
        slot = (res.evidence or {}).get("__verdict__", {}).get("G23")
        self.assertIsNotNone(slot, "写入结果里看不到 G23 判定结论")
        self.assertIs(slot["amount"]["checked"], False)
        self.assertIn("未判定", slot["amount"]["message"])

    def test_服务层不放行时不写盘(self):
        res = self.ep.add_payment(self.payment(), mode=APPLY, grant=self.grant)
        self.assertEqual(res.status, "precondition_failed")
        self.assertEqual(res.evidence["code"], "G23")
        self.assertEqual(len(self.ep.payments()), 0, "前置不过却写进去了")

    def test_服务层放行后写入并读回(self):
        self.add_gr(claim=S.CLAIM_ARRIVED, got=10, ordered=10, accepted=10,
                    state=S.GR_ACCEPTED)
        res = self.ep.add_payment(self.payment(qty=10), mode=APPLY, grant=self.grant)
        self.assertTrue(res.ok, res.message)
        self.assertTrue(res.verified)
        self.assertEqual(len(self.ep.payments()), 1)

    def test_enforce_关掉也不隐藏结论(self):
        """`enforce=False` 只用于历史导入，但「不满足」这个事实仍要带出去。"""
        res = self.ep.add_payment(self.payment(), mode=APPLY, grant=self.grant,
                                  enforce=False)
        self.assertTrue(res.ok)
        slot = res.evidence["__verdict__"]["G23"]
        self.assertFalse(slot["allowed"])

    def test_GR_构建器拒绝未知口径(self):
        with self.assertRaises(ValueError):
            PROC.build_gr_row(po_id=PO, project_id=PRJ, claim_type="差不多到了")
        with self.assertRaises(ValueError):
            PROC.build_gr_row(po_id=PO, project_id=PRJ, claim_type=S.CLAIM_ARRIVED,
                              state="半到")

    def test_到货口径常量与二期同源(self):
        """本层不许另立口径表 —— 逐项与二期比对。"""
        self.assertEqual(tuple(S.ARRIVAL_CLAIMS), tuple(PBS.ARRIVAL_CLAIMS))
        for name in ("CLAIM_SHIPPED", "CLAIM_IN_TRANSIT", "CLAIM_ARRIVED",
                     "CLAIM_PARTIAL", "CLAIM_FULL", "CLAIM_ACCEPTED", "CLAIM_REJECTED"):
            self.assertEqual(getattr(S, name), getattr(PBS, name), name)
        self.assertEqual(S.CLAIM_CN, PBS.CLAIM_CN)


# ════════════════════════════════════════════════════════════════════
# [D] G22：齐套开工
# ════════════════════════════════════════════════════════════════════
class TestG22KittingRelease(_Base):
    """`G22`：开工 ⟹ 齐套 = 100% ∨ 显式缺料放行授权。"""

    WO_ROW = {"wo_id": "WKO-20261003-0001", "project_id": PRJ,
              "parent_item_id": FG, "quantity": 10, "state": S.WO_DRAFT,
              "bom_version": ""}

    def setUp(self):
        super().setUp()
        # FG ← 用量 2 × CT  ⇒ 10 台需 CT 20
        self.VS = [{"bom_id": "B1", "parent_item_id": FG, "version": "A",
                    "status": BOM.BOM_RELEASED}]
        self.LS = [{"line_id": "L1", "bom_id": "B1", "child_item_id": CT,
                    "quantity_per": 2, "scrap_rate": 0}]

    def check(self, grs=(), wo=None, approval=None):
        return WO.check_release_precondition(
            dict(self.WO_ROW, **(wo or {})), bom_versions=self.VS,
            bom_lines=self.LS, gr_rows=list(grs), approval=approval)

    def approval(self, **over):
        base = {"status": "approved", "action": WO.OVERRIDE_ACTION, "approver": "审批人",
                "reason": "客户催货", "work_order_id": self.WO_ROW["wo_id"],
                "approval_id": "APR-TEST"}
        base.update(over)
        return base

    # ── 判定顺序 ──────────────────────────────────────────────
    def test_结构不合规要先报结构而不是缺料(self):
        """★ 顺序错了会让人去补一批根本不需要的料。"""
        vs = self.VS + [{"bom_id": "B2", "parent_item_id": CT, "version": "A",
                         "status": BOM.BOM_RELEASED}]
        ls = self.LS + [{"line_id": "L2", "bom_id": "B2", "child_item_id": FG,
                         "quantity_per": 1, "scrap_rate": 0}]
        ok, code, msg = WO.check_release_precondition(
            self.WO_ROW, bom_versions=vs, bom_lines=ls, gr_rows=[])
        self.assertFalse(ok)
        self.assertEqual(code, WO.R_BOM_STRUCTURE_INVALID)

    def test_无需求不得开工(self):
        ok, code, _ = self.check(wo={"parent_item_id": "ITM-UNKNOWN"})
        self.assertEqual((ok, code), (False, WO.R_NO_REQUIREMENTS))
        ok, code, _ = self.check(wo={"quantity": 0})
        self.assertEqual((ok, code), (False, WO.R_NO_REQUIREMENTS))
        ok, code, _ = self.check(wo={"quantity": "很多"})
        self.assertEqual((ok, code), (False, WO.R_NO_REQUIREMENTS))

    def test_工单状态非法要被拦(self):
        ok, code, _ = self.check(wo={"state": "差不多"})
        self.assertEqual((ok, code), (False, WO.R_WO_STATE_INVALID))

    # ── 齐套判定 ──────────────────────────────────────────────
    def test_完全没到货不得开工(self):
        ok, code, msg = self.check()
        self.assertFalse(ok)
        self.assertEqual(code, WO.R_MATERIAL_SHORTAGE)
        self.assertIn("未齐套", msg)

    def test_仅发货不得开工(self):
        ok, code, _ = self.check(grs=[self.gr(S.CLAIM_SHIPPED, got=20, ordered=20)])
        self.assertEqual((ok, code), (False, WO.R_MATERIAL_SHORTAGE))

    def test_部分到货报二期同一个原因码(self):
        """★ 同一条业务事实不能有两个原因码 —— 与二期逐字比对。"""
        ok, code, msg = self.check(grs=[self.gr(S.CLAIM_PARTIAL, got=5, ordered=20)])
        self.assertFalse(ok)
        self.assertEqual(code, WO.R_PARTIAL_NOT_KITTING)
        self.assertEqual(code, ACT.R_PARTIAL_NOT_KITTING, "与二期的字符串不一致")
        self.assertEqual(code, "partial_not_kitting")

    def test_到货量不足按缺料处理(self):
        ok, code, _ = self.check(grs=[self.gr(S.CLAIM_ARRIVED, got=5, ordered=20)])
        self.assertFalse(ok)
        self.assertEqual(code, WO.R_MATERIAL_SHORTAGE)

    def test_齐套到货应放行(self):
        ok, code, msg = self.check(grs=[self.gr(S.CLAIM_ARRIVED, got=20, ordered=20)])
        self.assertTrue(ok, msg)
        self.assertEqual(code, "")

    def test_齐套但已取消不得开工(self):
        ok, code, _ = self.check(grs=[self.gr(S.CLAIM_ARRIVED, got=20, ordered=20,
                                              state=S.GR_CANCELLED)])
        self.assertFalse(ok)
        self.assertEqual(code, WO.R_MATERIAL_SHORTAGE)

    def test_没有item_id的GR不计入(self):
        g = self.gr(S.CLAIM_ARRIVED, got=20, ordered=20)
        g["item_id"] = ""
        ok, _, _ = self.check(grs=[g])
        self.assertFalse(ok)

    # ── claim=full 的定性通道（★ 本批实测缺陷的回归）──────────
    def test_claim_full_无数量按二期口径算齐套(self):
        """★ 二期判据 `is_full_arrival` 对 claim=full **直接**返回 True。

        本层初版因为「取不到数量就跳过」，把它算成「一项没到」——
        等于自己加了一条「必须有数量」的新要求，把二期的口径换掉了。
        """
        g = self.gr(S.CLAIM_FULL, got="", ordered="")
        self.assertTrue(PROC.is_kitting(g), "二期判据说它是齐套")
        ok, code, msg = self.check(grs=[g])
        self.assertTrue(ok, "claim=full 被当成缺料了：%s / %s" % (code, msg))
        self.assertIn("定性声明", msg)

    def test_claim_full_但已取消仍不放行(self):
        ok, _, _ = self.check(grs=[self.gr(S.CLAIM_FULL, got="", ordered="",
                                           state=S.GR_CANCELLED)])
        self.assertFalse(ok)

    def test_定性通道与定量通道不重复计入(self):
        """同一条**记录**只能走一条通道：填了数量的走定量，没填的走定性。"""
        with_qty = self.gr(S.CLAIM_FULL, got=7, ordered=7)
        self.assertEqual(WO.claimed_full_items([with_qty]), set(),
                         "填了数量的 claim=full 不该再走定性通道")
        self.assertEqual(WO.arrived_kitting_by_item([with_qty]), {CT: 7.0})
        no_qty = self.gr(S.CLAIM_FULL, got="", ordered="")
        self.assertEqual(WO.claimed_full_items([no_qty]), {CT})
        self.assertEqual(WO.arrived_kitting_by_item([no_qty]), {})

    def test_定性齐套会被如实带出而不是藏起来(self):
        out = WO.release(self.WO_ROW, bom_versions=self.VS, bom_lines=self.LS,
                         gr_rows=[self.gr(S.CLAIM_FULL, got="", ordered="")])
        self.assertTrue(out["allowed"])
        self.assertEqual(out["satisfied_by_claim"], [CT])
        self.assertEqual(out["patch"]["satisfied_by_claim"], [CT])

    # ── 授权 fail-closed ──────────────────────────────────────
    def test_无授权不得开工(self):
        ok, code, msg = self.check(grs=[self.gr(S.CLAIM_PARTIAL, got=5, ordered=20)])
        self.assertFalse(ok)
        self.assertIn(WO.OV_MISSING, msg)

    def test_无效授权一律视为没授权(self):
        need = [self.gr(S.CLAIM_ARRIVED, got=5, ordered=20)]
        bad = {
            "status 非 approved": self.approval(status="pending"),
            "action 不对": self.approval(action="改成别的动作"),
            "缺 approver": self.approval(approver="   "),
            "缺 reason": self.approval(reason=""),
            "工单号不符": self.approval(work_order_id="WKO-20261003-9999"),
            "项目不符": self.approval(work_order_id="", project_id="PRJ-19990101-0001"),
        }
        for tag, appr in bad.items():
            ok, _, msg = self.check(grs=need, approval=appr)
            self.assertFalse(ok, "「%s」被当成有效授权了" % tag)
            self.assertIn(WO.OV_INVALID, msg)
            self.assertIs(WO.override_verdict(appr, self.WO_ROW)[0], WO.OV_INVALID)

    def test_有效授权放行(self):
        ok, code, msg = self.check(grs=[self.gr(S.CLAIM_ARRIVED, got=5, ordered=20)],
                                   approval=self.approval())
        self.assertTrue(ok, msg)
        self.assertIn("显式缺料放行授权", msg)

    def test_项目级授权范围也算有效(self):
        ok, _, _ = self.check(grs=[self.gr(S.CLAIM_ARRIVED, got=5, ordered=20)],
                              approval=self.approval(work_order_id="", project_id=PRJ))
        self.assertTrue(ok)

    def test_override_三态齐备(self):
        self.assertEqual(WO.override_verdict(None, self.WO_ROW)[0], WO.OV_MISSING)
        self.assertEqual(WO.override_verdict(self.approval(reason=""), self.WO_ROW)[0],
                         WO.OV_INVALID)
        self.assertEqual(WO.override_verdict(self.approval(), self.WO_ROW)[0], WO.OV_VALID)
        # 兼容包装不许与三态判定打架
        self.assertEqual(WO.validate_override(self.approval(), self.WO_ROW), (True, ""))
        self.assertFalse(WO.validate_override(None, self.WO_ROW)[0])

    def test_每个前置原因码都能被单独取到(self):
        """反死分支：`__all__` 里声明的每个码都必须可达。"""
        seen = set()
        seen.add(self.check(wo={"state": "差不多"})[1])
        seen.add(self.check(wo={"parent_item_id": "ITM-UNKNOWN"})[1])
        vs = self.VS + [{"bom_id": "B2", "parent_item_id": CT, "version": "A",
                         "status": BOM.BOM_RELEASED}]
        ls = self.LS + [{"line_id": "L2", "bom_id": "B2", "child_item_id": FG,
                         "quantity_per": 1, "scrap_rate": 0}]
        seen.add(WO.check_release_precondition(self.WO_ROW, bom_versions=vs,
                                               bom_lines=ls, gr_rows=[])[1])
        seen.add(self.check(grs=[self.gr(S.CLAIM_PARTIAL, got=5, ordered=20)])[1])
        seen.add(self.check(grs=[self.gr(S.CLAIM_ARRIVED, got=5, ordered=20)])[1])
        declared = {WO.R_NO_REQUIREMENTS, WO.R_BOM_STRUCTURE_INVALID,
                    WO.R_MATERIAL_SHORTAGE, WO.R_WO_STATE_INVALID,
                    WO.R_PARTIAL_NOT_KITTING}
        self.assertEqual(seen, declared, "有原因码取不到：%s" % sorted(declared - seen))

    def test_放行后就不要再报原因码(self):
        ok, code, msg = self.check(grs=[self.gr(S.CLAIM_ARRIVED, got=20, ordered=20)])
        self.assertTrue(ok)
        self.assertEqual(code, "")


# ════════════════════════════════════════════════════════════════════
# [E] 门面：preview / 授权闸门 / 乐观锁 / 状态机
# ════════════════════════════════════════════════════════════════════
class TestExecPlaneFacade(_Base):

    def test_preview_不写盘(self):
        res = self.ep.add_item(self.item(), mode=PREVIEW)
        self.assertEqual(res.status, "candidate")
        self.assertTrue(res.ok, "preview 的 candidate 应算 ok")
        self.assertEqual(self.counts()[S.TB_ITEM], 0, "preview 却写进去了")

    def test_apply_无授权则一个字都不写(self):
        res = self.ep.add_item(self.item(), mode=APPLY)
        self.assertEqual(res.status, "blocked")
        self.assertFalse(res.ok)
        self.assertEqual(self.counts()[S.TB_ITEM], 0)
        self.assertIn("授权", res.message)

    def test_无授权时连版本都不查(self):
        """顺序是可断言的：无授权 → blocked，而不是 stale。"""
        res = self.ep._write(S.TB_ITEM, self.item(), action=S.ACTION_APPEND,
                             mode=APPLY, row_id="ITM-20261003-0001",
                             expected_version=999)
        self.assertEqual(res.status, "blocked")

    def test_授权范围不含该表则拦下(self):
        narrow = AUTH.manual_grant(tables=(S.TB_ITEM,), actions=(S.ACTION_APPEND,),
                                   actor="测试")
        self.assertTrue(self.ep.add_item(self.item(), mode=APPLY, grant=narrow).ok)
        res = self.ep.add_work_order(self.work_order(), mode=APPLY, grant=narrow)
        self.assertEqual(res.status, "blocked")
        self.assertEqual(len(self.ep.work_orders()), 0)

    def test_apply_写入并读回验证(self):
        res = self.ep.add_item(self.item(), mode=APPLY, grant=self.grant)
        self.assertTrue(res.ok, res.message)
        self.assertTrue(res.verified)
        self.assertEqual(len(self.ep.items()), 1)
        self.assertEqual(self.ep.items()[0]["item_no"], "LNK-0001")

    def test_乐观锁拦下版本冲突(self):
        row = {"__row_id__": "ITM-20261003-0001", "item_id": "ITM-20261003-0001",
               "item_no": "LNK-0001"}
        self.assertTrue(self.ep._write(S.TB_ITEM, row, action=S.ACTION_APPEND,
                                       mode=APPLY, grant=self.grant,
                                       row_id=row["__row_id__"]).ok)
        res = self.ep._write(S.TB_ITEM, row, action=S.ACTION_UPDATE, mode=APPLY,
                             grant=self.grant, row_id=row["__row_id__"],
                             expected_version=99)
        self.assertEqual(res.status, "stale_version")
        self.assertIn("未写入", res.message)

    def test_缺主键直接报错(self):
        with self.assertRaises(ValueError):
            self.ep._write(S.TB_ITEM, {"item_no": "X"}, action=S.ACTION_APPEND,
                           mode=APPLY, grant=self.grant)

    # ── 工单下达 ──────────────────────────────────────────────
    def _wo(self, **kw):
        v, ln = self.bom(FG, CT, qty=2)
        self.ep.put_bom(v, [ln], mode=APPLY, grant=self.grant)
        wo = self.work_order(qty=10, **kw)
        self.ep.add_work_order(wo, mode=APPLY, grant=self.grant)
        return wo

    def test_工单不存在时报_not_found(self):
        res = self.ep.release_work_order("WKO-20261003-0001", mode=APPLY, grant=self.grant)
        self.assertEqual(res.status, "not_found")

    def test_缺料时开工被拒且状态不变(self):
        wo = self._wo()
        res = self.ep.release_work_order(wo["wo_id"], mode=APPLY, grant=self.grant)
        self.assertEqual(res.status, "precondition_failed")
        self.assertEqual(res.evidence["code"], "G22")
        self.assertEqual(self.ep.work_orders()[0]["state"], S.WO_DRAFT,
                         "前置不过却改了状态")

    def test_有效授权时开工并留痕(self):
        wo = self._wo()
        appr = {"status": "approved", "action": WO.OVERRIDE_ACTION, "approver": "审批人",
                "reason": "客户催货", "work_order_id": wo["wo_id"],
                "approval_id": "APR-TEST", "granted_at": "2026-10-03 12:00:00"}
        res = self.ep.release_work_order(wo["wo_id"], mode=APPLY, grant=self.grant,
                                         approval=appr, actor="审批人")
        self.assertTrue(res.ok, res.message)
        row = self.ep.work_orders()[0]
        self.assertEqual(row["state"], S.WO_RELEASED)
        trace = row["release_shortage_override"]
        self.assertEqual(trace["approver"], "审批人")
        self.assertEqual(trace["reason"], "客户催货")
        self.assertEqual(trace["action"], WO.OVERRIDE_ACTION)
        self.assertEqual(trace["approval_id"], "APR-TEST")

    def test_齐套时开工不写授权留痕(self):
        wo = self._wo()
        self.add_gr(claim=S.CLAIM_ARRIVED, got=20, ordered=20)
        appr = {"status": "approved", "action": WO.OVERRIDE_ACTION, "approver": "审批人",
                "reason": "多给了", "work_order_id": wo["wo_id"]}
        res = self.ep.release_work_order(wo["wo_id"], mode=APPLY, grant=self.grant,
                                         approval=appr)
        self.assertTrue(res.ok, res.message)
        row = self.ep.work_orders()[0]
        self.assertEqual(row["state"], S.WO_RELEASED)
        self.assertEqual(row["release_shortage_override"], {},
                         "无缺口时不该写缺料放行留痕")

    def test_重复下达报非法迁移(self):
        wo = self._wo()
        self.add_gr(claim=S.CLAIM_ARRIVED, got=20, ordered=20)
        self.assertTrue(self.ep.release_work_order(wo["wo_id"], mode=APPLY,
                                                   grant=self.grant).ok)
        res = self.ep.release_work_order(wo["wo_id"], mode=APPLY, grant=self.grant)
        self.assertEqual(res.status, "illegal_transition")
        self.assertIn("非法状态迁移", res.message)

    def test_开工结果带出_G22_结论(self):
        wo = self._wo()
        self.add_gr(claim=S.CLAIM_ARRIVED, got=20, ordered=20)
        res = self.ep.release_work_order(wo["wo_id"], mode=APPLY, grant=self.grant)
        slot = res.evidence["__verdict__"]["G22"]
        self.assertTrue(slot["allowed"])
        self.assertEqual(slot["requirements"], {CT: 20.0})
        self.assertEqual(slot["shortages"], [])
        self.assertEqual(slot["override"], WO.OV_MISSING)

    def test_forced_放行必须留下为什么被拒(self):
        """`enforce=False` 是历史导入口子：绝不能留下一个看起来正常的空缺口表。"""
        wo = self._wo()
        res = self.ep.release_work_order(wo["wo_id"], mode=APPLY, grant=self.grant,
                                         enforce=False)
        self.assertTrue(res.ok, res.message)
        row = self.ep.work_orders()[0]
        self.assertEqual(row["state"], S.WO_RELEASED)
        self.assertTrue(row["release_forced"])
        self.assertEqual(row["release_forced_code"], WO.R_MATERIAL_SHORTAGE)
        self.assertIn("未齐套", row["release_forced_message"])

    def test_BOM_结构不合规时开工被拒(self):
        wo = self.work_order(qty=10)
        self.ep.add_work_order(wo, mode=APPLY, grant=self.grant)
        res = self.ep.release_work_order(wo["wo_id"], mode=APPLY, grant=self.grant)
        self.assertEqual(res.status, "precondition_failed")
        self.assertIn(WO.REASON_CN[WO.R_NO_REQUIREMENTS], res.message)

    # ── 隔离性 ────────────────────────────────────────────────
    def test_两个实例互不干扰(self):
        other_dir = tempfile.mkdtemp(prefix="ep_test_other_")
        self.addCleanup(shutil.rmtree, other_dir, True)
        other = ExecPlane(root=other_dir)
        self.ep.add_item(self.item(), mode=APPLY, grant=self.grant)
        self.assertEqual(len(other.items()), 0, "两个执行平面实例串了")

    def test_默认根目录在_data_exec_plane(self):
        """纯计算断言 —— **不实例化**，避免测试在仓库里建出真目录。"""
        got = ExecPlane.default_root(skill_dir=HERE)
        self.assertEqual(got, os.path.join(HERE, "data", "exec_plane"))
        self.assertEqual(os.path.basename(got), "exec_plane")
        # 与二期本地库平级、互不覆盖
        self.assertNotEqual(got, os.path.join(HERE, "data", "project_brain"))
        # 未给 skill_dir 时，按本文件位置回溯三级仍是仓库根
        self.assertEqual(ExecPlane.default_root(),
                         os.path.join(HERE, "data", "exec_plane"))

    # ── 结论必须带 as_of（DEV-KICKOFF §7 交付判据）─────────────
    def test_结论都带_as_of(self):
        """★ 「每步带 ID、带 `as_of`」是阶段一的交付判据，不是可选项。

        `as_of` 是**结论上的字段**（G30 上行新鲜度），所以本层由「读了哪些行」算出它 ——
        这样「这个结论是按哪一刻的数据算出来的」永远答得上来。
        """
        self.assertTrue(self.put(FG, CT).ok)
        self.assertEqual(self.ep.validate_bom()["as_of"],
                         self.ep.bom_versions()[0]["created_at"])
        self.assertEqual(self.ep.explode(FG, quantity=1)["as_of"],
                         max([self.ep.bom_versions()[0]["created_at"],
                              self.ep.bom_lines()[0]["created_at"]]))
        self.add_gr(claim=S.CLAIM_ARRIVED, got=10, ordered=10, accepted=10,
                    state=S.GR_ACCEPTED)
        pay = self.payment(qty=10)
        self.assertEqual(self.ep.can_pay(pay)["as_of"],
                         self.ep.goods_receipts()[0]["created_at"])

    def test_没有数据时_as_of_为空而不是瞎编(self):
        """★ 取不到就是空串 —— 不许拿「现在」冒充数据时间。"""
        self.assertEqual(self.ep.as_of([]), "")
        self.assertEqual(self.ep.validate_bom()["as_of"], "")
        self.assertEqual(self.ep.can_pay(self.payment())["as_of"], "")
        self.assertEqual(ExecPlane.as_of([{}], [{"created_at": ""}]), "")

    def test_as_of_取最新而不是最早(self):
        rows = [{"created_at": "2026-10-01 09:00:00"},
                {"created_at": "2026-10-03 18:30:00"},
                {"created_at": "2026-10-02 12:00:00"}]
        self.assertEqual(ExecPlane.as_of(rows), "2026-10-03 18:30:00")

    def test_工单开工结论带_as_of(self):
        wo = self._wo()
        self.add_gr(claim=S.CLAIM_ARRIVED, got=20, ordered=20)
        res = self.ep.release_work_order(wo["wo_id"], mode=APPLY, grant=self.grant)
        slot = res.evidence["__verdict__"]["G22"]
        self.assertTrue(slot["as_of"], "开工结论没带 as_of")
        self.assertGreaterEqual(slot["as_of"],
                                self.ep.goods_receipts()[0]["created_at"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
