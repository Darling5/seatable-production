# -*- coding: utf-8 -*-
"""适配器契约测试（第 0 期「多底座地基」）。

这些用例锁的是**跨后端契约**本身，不是某一个后端的实现细节。改坏它们意味着
「同一个上层代码换后端就崩」——那正是这一期要消灭的东西。

覆盖：
  1. 三层契约：必修方法 / 能力声明 / 可选方法兜底
  2. `capabilities_of` 对鸭子类型适配器的保守推断（含「不许瞎猜关联能力」）
  3. LocalAdapter 兑现新契约（ensure_table / table_exists / get_row / append_rows /
     link 整体替换 vs link_append 追加 vs link_one_way 单向）
  4. SeaTableAdapter 的**只用只读实测事实**实现的部分：link_id 索引、关联列读取、
     单行读取、批量新增分片、link_id 校验
  5. `schema` 中立类型词表与方向敏感的 link_id_for
  6. **可插拔性证明**：注册一个全新后端，工厂不改一行就能路由过去

全部离线：SeaTable 相关一律 patch 掉 requests，不碰线上。
"""
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

from adapters import schema  # noqa: E402
from adapters.base import (CAP_BATCH_WRITE, CAP_DELETE, CAP_LINK,  # noqa: E402
                           CAP_LINK_READ, CAP_OPTIMISTIC_LOCK, CAP_QUERY_PUSHDOWN,
                           CAP_READ, CAP_SCHEMA_MANAGE, CAP_UPDATE, CAP_WRITE,
                           BaseAdapter, Unsupported, backend_of, capabilities_of, supports)
from adapters.local import LocalAdapter  # noqa: E402
from adapters.seatable import SeaTableAdapter, _row_ids_of  # noqa: E402


# ══════════════════════════════════════════════════════════════
# 1. 三层契约
# ══════════════════════════════════════════════════════════════

class _Minimal(BaseAdapter):
    """只实现 8 个必修方法的最小适配器 —— 用来验证「可选方法必须显式不支持」。"""

    backend = "minimal"

    def auth(self):
        pass

    def list_rows(self, table):
        return self._rows.get(table, [])

    def get_metadata(self, table):
        return {"table_name": table, "columns": []}

    def append_row(self, table, data):
        self._rows.setdefault(table, []).append(dict(data, __row_id__="r%d" % len(self._rows[table])))
        return self._rows[table][-1]["__row_id__"]

    def update_row(self, table, row_id, data):
        pass

    def delete_rows(self, table, row_ids):
        pass

    def link(self, table, other_table, link_id, row_id, other_row_ids):
        pass

    def list_linked(self, table, row_id, link_id):
        return []

    def __init__(self):
        self._rows = {}


class ContractLayerTests(unittest.TestCase):
    def test_abstract_methods_are_enforced(self):
        """少实现一个必修方法就 new 不出来 —— 这是最外层的防线。"""
        for missing in ("auth", "list_rows", "get_metadata", "append_row",
                        "update_row", "delete_rows", "link", "list_linked"):
            attrs = {k: v for k, v in _Minimal.__dict__.items()
                     if k in ("auth", "list_rows", "get_metadata", "append_row",
                              "update_row", "delete_rows", "link", "list_linked")}
            attrs.pop(missing, None)
            broken = type("Broken_" + missing, (BaseAdapter,), attrs)
            with self.assertRaises(TypeError, msg="缺 %s 竟然能实例化" % missing):
                broken()

    def test_optional_methods_raise_unsupported_not_silent_empty(self):
        """可选方法拿不到正确答案时**必须报错**，不许静默返回空/None。

        这是本仓库踩过的坑：SeaTable 的 list_linked 曾是 `return []`，于是
        「查关联」永远成功、永远返回空、永远不报错。
        """
        a = _Minimal()
        with self.assertRaises(Unsupported):
            a.ensure_table("新表")
        with self.assertRaises(Unsupported):
            a.link_append("A", "B", "L1", "r1", ["x"])
        with self.assertRaises(Unsupported):
            a.link_one_way("A", "B", "L1", "r1", ["x"])

    def test_unsupported_is_a_notimplementederror(self):
        """老代码里 `except NotImplementedError` 必须还能兜住。"""
        self.assertTrue(issubclass(Unsupported, NotImplementedError))

    def test_version_of_default_is_none_not_zero(self):
        """None = 「没有版本概念」，不能被当成版本 0。"""
        self.assertIsNone(_Minimal().version_of("T", "r1"))

    def test_get_row_default_scans_and_returns_none_when_missing(self):
        a = _Minimal()
        rid = a.append_row("T", {"名称": "甲"})
        self.assertEqual(a.get_row("T", rid)["名称"], "甲")
        self.assertIsNone(a.get_row("T", "不存在的行"))

    def test_append_rows_default_preserves_order(self):
        a = _Minimal()
        ids = a.append_rows("T", [{"i": 1}, {"i": 2}, {"i": 3}])
        self.assertEqual(len(ids), 3)
        rows = a.list_rows("T")
        self.assertEqual([r["i"] for r in rows], [1, 2, 3])
        self.assertEqual(ids, [r["__row_id__"] for r in rows])

    def test_base_query_is_in_memory(self):
        a = _Minimal()
        a.append_rows("T", [{"k": "a"}, {"k": "b"}, {"k": "ab"}])
        self.assertEqual(len(a.query("T", {"k": "a"})), 1)
        self.assertEqual(len(a.query("T", {"k": "*a"})), 2)   # 前缀 * = 包含
        self.assertEqual(len(a.query("T")), 3)
        self.assertFalse(supports(a, CAP_QUERY_PUSHDOWN))


# ══════════════════════════════════════════════════════════════
# 2. 能力声明（含鸭子类型适配器）
# ══════════════════════════════════════════════════════════════

class _Duck:
    """不继承 BaseAdapter 的鸭子类型适配器（如 project_brain 的 LocalMemoryAdapter）。"""

    CAPS = frozenset({CAP_READ, CAP_WRITE, CAP_SCHEMA_MANAGE})

    def list_rows(self, table):
        return []

    def get_metadata(self, table):
        return {}


class _DuckNoCaps:
    def list_rows(self, table):
        return []

    def get_metadata(self, table):
        return {}

    def append_row(self, table, data):
        return "x"

    def link(self, table, other_table, link_id, row_id, other_row_ids):
        pass

    def list_linked(self, table, row_id, link_id):
        return []


class CapabilityTests(unittest.TestCase):
    def test_declared_caps_win_for_duck_typed_adapter(self):
        """声明了 CAPS 但没继承 BaseAdapter 的适配器，声明必须被尊重。

        （回归：`capabilities_of` 首版只看 `capabilities()` 方法，于是这类适配器
        的 CAPS 被忽略、被判成「不支持建表」，能力声明形同虚设。）
        """
        self.assertIn(CAP_SCHEMA_MANAGE, capabilities_of(_Duck()))

    def test_inference_is_conservative_about_link(self):
        """**不许**由方法存在性推断关联能力。

        `_DuckNoCaps` 有 link() / list_linked()，但后者是个 `return []` 空桩 ——
        光看方法在不在分辨不出「真实现」和「空桩」。宁可判它不支持，也不要判它
        支持然后拿到假结果。
        """
        caps = capabilities_of(_DuckNoCaps())
        self.assertIn(CAP_READ, caps)
        self.assertIn(CAP_WRITE, caps)
        self.assertNotIn(CAP_LINK, caps)
        self.assertNotIn(CAP_LINK_READ, caps)

    def test_local_adapter_does_not_claim_what_it_cannot_do(self):
        a = LocalAdapter(tempfile.mkdtemp(prefix="adapter_caps_"))
        caps = capabilities_of(a)
        self.assertIn(CAP_SCHEMA_MANAGE, caps)
        self.assertIn(CAP_LINK_READ, caps)      # 真实现（__links__.json）
        self.assertIn(CAP_BATCH_WRITE, caps)
        self.assertNotIn(CAP_QUERY_PUSHDOWN, caps)       # query 永远是内存过滤
        self.assertNotIn(CAP_OPTIMISTIC_LOCK, caps)      # 没有版本号

    def test_capability_call_is_side_effect_free(self):
        """能力声明必须能在**建连之前**读（上层靠它决定要不要建连）。"""
        a = SeaTableAdapter("t", "https://nope.invalid", "u")
        with patch("adapters.seatable.requests.get",
                   side_effect=AssertionError("能力声明不该联网")):
            caps = a.capabilities()
        self.assertIn(CAP_SCHEMA_MANAGE, caps)
        self.assertIn(CAP_LINK_READ, caps)
        self.assertNotIn(CAP_OPTIMISTIC_LOCK, caps)

    def test_backend_of_falls_back_to_classname(self):
        self.assertEqual(backend_of(_DuckNoCaps()), "_ducknocaps")
        self.assertEqual(backend_of(LocalAdapter(tempfile.mkdtemp())), "local")


# ══════════════════════════════════════════════════════════════
# 3. LocalAdapter 兑现新契约
# ══════════════════════════════════════════════════════════════

class LocalContractTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="adapter_local_")
        self.a = LocalAdapter(self.tmp)

    def test_table_exists_is_file_based_not_metadata_based(self):
        """读一张不存在的表不会抛异常 → 绝不能拿它当「表存在」的证据。"""
        self.assertFalse(self.a.table_exists("没有这张表"))
        self.assertEqual(self.a.list_rows("没有这张表"), [])
        self.a.ensure_table("新表", [{"name": "编号", "type": "text"}])
        self.assertTrue(self.a.table_exists("新表"))

    def test_ensure_table_is_idempotent_and_keeps_header(self):
        self.assertEqual(self.a.ensure_table("T", [{"name": "编号"}, {"name": "名称"}]), "created")
        self.assertEqual(self.a.ensure_table("T"), "exists")
        # 空表也必须能报出自己的列（否则「建了表」和「没建表」表现完全一样）
        self.assertEqual([c["name"] for c in self.a.get_metadata("T")["columns"]],
                         ["编号", "名称"])
        self.assertEqual(self.a.list_rows("T"), [])

    def test_append_rows_returns_ids_in_order_and_is_single_write(self):
        rows = [{"编号": "第%d号" % i} for i in range(1, 6)]
        ids = self.a.append_rows("T", rows)
        self.assertEqual(len(ids), 5)
        self.assertEqual([r["编号"] for r in self.a.list_rows("T")],
                         ["第1号", "第2号", "第3号", "第4号", "第5号"])
        self.assertEqual(self.a.append_rows("T", []), [])

    def test_link_replaces_but_link_append_keeps_history(self):
        self.a.ensure_table("甲")
        self.a.ensure_table("乙")
        self.a.link("甲", "乙", "L1", "r1", ["b1"])
        self.assertEqual(self.a.list_linked("甲", "r1", "L1"), ["b1"])
        # link() 是整体替换语义（与 SeaTable 的 PUT /links/ 对齐）
        self.a.link("甲", "乙", "L1", "r1", ["b2"])
        self.assertEqual(self.a.list_linked("甲", "r1", "L1"), ["b2"])
        # link_append() 保留历史
        self.assertEqual(self.a.link_append("甲", "乙", "L1", "r1", ["b3"]), ["b2", "b3"])
        self.assertEqual(self.a.link_append("甲", "乙", "L1", "r1", ["b2"]), ["b2", "b3"])  # 去重

    def test_list_linked_empty_link_id_is_union(self):
        self.a.ensure_table("甲")
        self.a.ensure_table("乙")
        self.a.ensure_table("丙")
        self.a.link("甲", "乙", "L1", "r1", ["b1"])
        self.a.link("甲", "丙", "L2", "r1", ["c1"])
        self.assertEqual(self.a.list_linked("甲", "r1", "L1"), ["b1"])
        self.assertEqual(sorted(self.a.list_linked("甲", "r1", "")), ["b1", "c1"])

    def test_link_one_way_does_not_touch_the_other_side(self):
        """单向关联只写源侧 —— 这是 crm_dispatch 需要的语义。

        用双向 link() 做「子记录挂到父记录」会把父记录侧的历史关联整体替换掉，
        而且不报错。本用例钉住 link_one_way 不会碰对方。
        """
        self.a.ensure_table("父")
        self.a.ensure_table("子")
        # 父记录先挂好两个子记录（模拟历史）
        self.a.link("父", "子", "L1", "p1", ["c1", "c2"])
        # 再新建一个子记录并单向挂到父
        self.a.link_one_way("子", "父", "L1", "c3", ["p1"])
        # 子侧看到了父
        self.assertEqual(self.a.list_linked("子", "c3", "L1"), ["p1"])
        # 父侧的历史**没有**被冲掉，而是多了 c3
        self.assertEqual(self.a.list_linked("父", "p1", "L1"), ["c1", "c2", "c3"])


# ══════════════════════════════════════════════════════════════
# 4. SeaTableAdapter：只用只读实测到的事实
# ══════════════════════════════════════════════════════════════

def _meta_with_links():
    """构造与真实 Base 同构的 metadata。

    两个关键事实都来自**只读探测**，不是猜测：
      · 顶层**没有 `links` 键**（只有 format_version / tables / version），link_id 只
        存在于每个 link 列的 `data.link_id` 里 —— 旧实现在这里读 `meta["links"]`，
        恒为空列表，于是 `_resolve_link_id` 永远抛 KeyError，是一条从未走通的死路。
      · 双向关联会在**两张表上各建一个同 link_id 的列**（`3Fld` 在 `生产计划.关联项目`
        与 `项目.生产计划` 上各一份；`wana` 在 `项目.阶段` 与 `生产计划.项目` 上各一份）。
        所以「项目 ↔ 生产计划」这一对表之间**天然有两条**关联，方向无法自动判定。
    """
    return {
        "format_version": 1,
        "version": 7,
        "tables": [
            {"_id": "t1", "name": "生产计划", "columns": [
                {"name": "关联项目", "key": "k1", "type": "link",
                 "data": {"link_id": "3Fld", "table_id": "t1", "other_table_id": "t2",
                          "is_multiple": True}},
                {"name": "项目", "key": "k8", "type": "link",
                 "data": {"link_id": "wana", "table_id": "t1", "other_table_id": "t2",
                          "is_multiple": True}},
                {"name": "贴片生产记录", "key": "k2", "type": "link",
                 "data": {"link_id": "1T1Q", "table_id": "t1", "other_table_id": "t3",
                          "is_multiple": True}},
                # ⚠️ 大小写混用：真实 Base 里这 4 个关联列的 `type` 是**大写 `LINK`**
                #    （只读实测 production Base：`type=="link"` 36 列、`type=="LINK"` 4 列）。
                #    早先 `_link_index()` 精确比较 `!= "link"` → 这些列被静默跳过。
                {"name": "工时记录", "key": "k9", "type": "LINK",
                 "data": {"link_id": "W1Q1", "table_id": "t1", "other_table_id": "t4",
                          "is_multiple": True}},
                # link-formula 是**计算列**、`data` 里没有 link_id，不是可真写的关联列 → 必须排除。
                {"name": "IC采购支出", "key": "k10", "type": "link-formula", "data": {}},
                {"name": "状态", "key": "k3", "type": "single-select",
                 "data": {"options": [{"id": "58668", "name": "在产"}]}},
                # 真实 Base 里 select 类列也存在大写形态（`_cell_meta` 的类型判断同样要大小写不敏感）
                {"name": "优先级", "key": "k11", "type": "SINGLE-SELECT",
                 "data": {"options": [{"id": "9", "name": "高"}]}},
                # 真实 Base 实测：`工时记录.开始时间/结束时间` 的类型是**大写 `DATE`**
                {"name": "开始时间", "key": "k12", "type": "DATE", "data": {}},
                {"name": "交货时间", "key": "_mtime", "type": "mtime", "data": {}},
                {"name": "名称", "key": "k4", "type": "text", "data": {}},
            ]},
            {"_id": "t2", "name": "项目", "columns": [
                {"name": "阶段", "key": "k5", "type": "link",
                 "data": {"link_id": "wana", "table_id": "t2", "other_table_id": "t1",
                          "is_multiple": True}},
                {"name": "生产计划", "key": "k6", "type": "link",
                 "data": {"link_id": "3Fld", "table_id": "t2", "other_table_id": "t1",
                          "is_multiple": True}},
            ]},
            {"_id": "t3", "name": "贴片生产记录", "columns": [
                {"name": "生产计划", "key": "k7", "type": "link",
                 "data": {"link_id": "1T1Q", "table_id": "t3", "other_table_id": "t1",
                          "is_multiple": True}},
            ]},
            # 大写 LINK 的**双向**两侧，与真实 Base 同构（W1Q1 / Hh3j 各在两张表上各有一列）。
            # 真实 Base 实测：这 4 列的 `data.other_table_id` 都是 None —— 所以解析绝不能依赖它。
            {"_id": "t4", "name": "工时记录", "columns": [
                {"name": "关联生产计划", "key": "m1", "type": "LINK",
                 "data": {"link_id": "W1Q1", "table_id": "t4", "is_multiple": True}},
                {"name": "关联工序", "key": "m2", "type": "LINK",
                 "data": {"link_id": "Hh3j", "table_id": "t4", "is_multiple": True}},
                {"name": "工时", "key": "m3", "type": "number", "data": {}},
            ]},
            {"_id": "t5", "name": "生产工序", "columns": [
                {"name": "工时记录", "key": "n1", "type": "LINK",
                 "data": {"link_id": "Hh3j", "table_id": "t5", "is_multiple": True}},
            ]},
        ],
    }


class _Resp:
    def __init__(self, payload=None, status_code=200, text=""):
        self.payload = payload if payload is not None else {}
        self.status_code = status_code
        self.text = text

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError("HTTP %s" % self.status_code)

    def json(self):
        return self.payload


class SeaTableContractTests(unittest.TestCase):
    def setUp(self):
        self.a = SeaTableAdapter("tok", "https://seatable.invalid", "uuid-1", base_name="production")
        self.a._access = "access-x"
        self.a._server = "https://gw.invalid"
        self.a._meta = _meta_with_links()

    # ── 测试工具：按 URL 分派的假 requests.get ──────────────
    def _fake_get(self, table, rows_body=None, one_body=None, one_status=200):
        """真实调用序列里，`list_rows`/`get_row` 会先打 `GET /columns/`（拉列定义与选项
        映射），再打 `GET /rows/`。只塞一个响应会 StopIteration —— 首版就是这么踩的。
        这里按 URL 分派，让替身贴近真实调用序。
        """
        cols = next(t for t in self.a._meta["tables"] if t["name"] == table)["columns"]

        def fake_get(url, headers=None, params=None, timeout=None):
            if url.endswith("/columns/"):
                return _Resp({"columns": cols})
            if url.rstrip("/").endswith("/rows"):          # 列表接口
                return _Resp(rows_body if rows_body is not None else {"rows": []})
            return _Resp(one_body if one_body is not None else {},
                         status_code=one_status, text="not found")

        return fake_get

    # ── link_id 解析（旧实现在这里读 meta["links"]，恒为空 → 必死）──────
    def test_link_id_resolves_from_columns_not_from_metadata_links(self):
        self.assertNotIn("links", self.a._meta)          # 前提：真实 metadata 没有 links
        # 唯一一条关联 → 按表名就能定
        self.assertEqual(self.a._resolve_link_id("生产计划", "贴片生产记录"), "1T1Q")
        # 两条关联 → 必须显式指定该表上的列名
        self.assertEqual(self.a._resolve_link_id("生产计划", "项目", column="关联项目"), "3Fld")
        self.assertEqual(self.a._resolve_link_id("生产计划", "项目", column="项目"), "wana")
        self.assertEqual(self.a._resolve_link_id("项目", "生产计划", column="阶段"), "wana")

    def test_link_id_ambiguity_raises_instead_of_guessing(self):
        """同一对表有多条关联列时必须报错，不许静默取第一条。

        这是最容易查错的一类 bug：不报错、数据看起来也在，只是挂到了另一条业务线上。
        """
        with self.assertRaises(KeyError) as ctx:
            self.a._resolve_link_id("生产计划", "项目")
        msg = str(ctx.exception)
        self.assertIn("条关联列", msg)
        self.assertIn("3Fld", msg)          # 报错里要列出候选供人选择
        self.assertIn("wana", msg)
        self.assertIn("column=", msg)       # 并且要告诉人怎么解决

    def test_link_id_resolution_ignores_other_table_id(self):
        """判定不能依赖 `data.other_table_id`。

        只读实测发现：双向关联的**反向列**上 `other_table_id` 并不稳定地指向对方表。
        所以候选判定改为「这个 link_id 覆盖了哪两张表」。这里把 other_table_id 全部
        改成垃圾值，解析结果必须不变。
        """
        for t in self.a._meta["tables"]:
            for c in t["columns"]:
                if c["type"] == "link":
                    c["data"]["other_table_id"] = "垃圾值"
        self.a._linkidx = None
        self.a._linkmap = None
        self.assertEqual(self.a._resolve_link_id("生产计划", "贴片生产记录"), "1T1Q")
        self.assertEqual(self.a._resolve_link_id("生产计划", "项目", column="关联项目"), "3Fld")

    def test_link_id_resolution_unknown_pair_raises(self):
        with self.assertRaises(KeyError) as ctx:
            self.a._resolve_link_id("生产计划", "没有这张表")
        self.assertIn("表不存在", str(ctx.exception))

    def test_unknown_link_id_is_rejected_loudly(self):
        """调用方传了本 Base 里不存在的 link_id（如 schema 里的过期值）必须报错。"""
        with self.assertRaises(KeyError) as ctx:
            self.a._assert_link_id_known("RsAl")
        self.assertIn("RsAl", str(ctx.exception))

    def test_list_rows_flattens_select_and_keeps_mtime_column(self):
        body = {"rows": [{"_id": "r1", "k3": "58668", "_mtime": "2026-05-07T00:00:00+08:00",
                          "k4": "甲"}]}
        with patch("adapters.seatable.requests.get",
                   side_effect=self._fake_get("生产计划", rows_body=body)):
            rows = self.a.list_rows("生产计划")
        self.assertEqual(rows[0]["状态"], "在产")                 # 选项 id → 中文名
        self.assertEqual(rows[0]["交货时间"], "2026-05-07")       # mtime 列按定义映射 + 截日期
        self.assertEqual(rows[0]["__row_id__"], "r1")

    def test_get_row_uses_single_row_endpoint_and_returns_none_on_404(self):
        get = self._fake_get("生产计划", one_body={"_id": "r1", "k4": "甲", "k3": "58668"})
        with patch("adapters.seatable.requests.get", side_effect=get) as req:
            row = self.a.get_row("生产计划", "r1")
        self.assertEqual(row["名称"], "甲")
        self.assertEqual(row["__row_id__"], "r1")
        urls = [c.args[0] for c in req.call_args_list]
        self.assertTrue(any(u.rstrip("/").endswith("/rows/r1") for u in urls),
                        "应当走单行接口：%s" % urls)   # 而不是拉全表

        with patch("adapters.seatable.requests.get",
                   side_effect=self._fake_get("生产计划", one_status=404)):
            self.assertIsNone(self.a.get_row("生产计划", "没有这行"))

    def test_list_linked_reads_link_column_and_raises_when_unknown(self):
        """读关联必须走「读该行 + 抽 link 列」—— 因为 GET /links/ 是 405。"""
        payload = {"_id": "r1", "k1": [{"row_id": "p9", "display_value": "项目X"}]}
        with patch("adapters.seatable.requests.get",
                   side_effect=self._fake_get("生产计划", one_body=payload)):
            self.assertEqual(self.a.list_linked("生产计划", "r1", "3Fld"), ["p9"])

        # 同一个行、link_id 指定为另一条 → 读的是另一个列（这里是空的）
        with patch("adapters.seatable.requests.get",
                   side_effect=self._fake_get("生产计划", one_body={"_id": "r1"})):
            self.assertEqual(self.a.list_linked("生产计划", "r1", "1T1Q"), [])

        # link_id 空 → 所有关联列的并集
        payload3 = {"_id": "r1",
                    "k1": [{"row_id": "p9", "display_value": "x"}],
                    "k2": [{"row_id": "s1", "display_value": "y"}]}
        with patch("adapters.seatable.requests.get",
                   side_effect=self._fake_get("生产计划", one_body=payload3)):
            self.assertEqual(sorted(self.a.list_linked("生产计划", "r1", "")), ["p9", "s1"])

        # 表上根本没有这个 link_id → 显式报错，不是静默空列表
        with self.assertRaises(KeyError):
            self.a.list_linked("生产计划", "r1", "ZZZZ")

    def test_link_columns_raises_for_unknown_table(self):
        with self.assertRaises(KeyError) as ctx:
            self.a.link_columns("没有这张表")
        self.assertIn("表不存在", str(ctx.exception))

    def test_append_rows_chunks_and_keeps_length_aligned(self):
        from adapters import seatable as st
        orig = st._BATCH_LIMIT
        st._BATCH_LIMIT = 2
        try:
            calls = []

            def fake_post(url, headers=None, json=None, timeout=None):
                calls.append(json["rows"])
                return _Resp({"inserted_row_count": len(json["rows"]),
                              "row_ids": [{"_id": "id%d" % i} for i in range(len(json["rows"]))]})

            with patch("adapters.seatable.requests.post", side_effect=fake_post):
                ids = self.a.append_rows("生产计划", [{"i": i} for i in range(5)])
            self.assertEqual([len(c) for c in calls], [2, 2, 1])   # 分片
            self.assertEqual(len(ids), 5)                          # 等长
        finally:
            st._BATCH_LIMIT = orig

    def test_append_rows_backfills_none_when_server_returns_fewer_ids(self):
        """服务端 id 数量对不上时宁可填 None，也不能错位（错位会把 A 的 id 记到 B）。"""
        with patch("adapters.seatable.requests.post",
                   side_effect=[_Resp({"inserted_row_count": 2, "row_ids": [{"_id": "only-one"}]})]):
            ids = self.a.append_rows("生产计划", [{"i": 1}, {"i": 2}])
        self.assertEqual(ids, [None, None])

    def test_ensure_table_returns_exists_without_network_when_present(self):
        with patch("adapters.seatable.requests.post",
                   side_effect=AssertionError("表已存在不该发建表请求")):
            self.assertEqual(self.a.ensure_table("生产计划"), "exists")

    def test_ensure_table_translates_neutral_types(self):
        with patch("adapters.seatable.requests.post",
                   side_effect=[_Resp({"table_name": "新表"})]) as post:
            got = self.a.ensure_table("新表", [{"name": "名称", "type": "text"},
                                              {"name": "状态", "type": "select"},
                                              {"name": "日期", "type": "datetime"}])
        self.assertEqual(got, "created")
        sent = post.call_args.kwargs["json"]["columns"]
        # 上层只写中立类型名；后端负责翻译成原生类型
        self.assertEqual(sent, [{"column_name": "名称", "column_type": "text"},
                                {"column_name": "状态", "column_type": "single-select"},
                                {"column_name": "日期", "column_type": "date"}])

    def test_ensure_table_raises_on_body_error_with_http_200(self):
        """SeaTable 会「HTTP 200 + body 报错」，必须识别，不能只看状态码。"""
        with patch("adapters.seatable.requests.post",
                   side_effect=[_Resp({"error_message": "table already exists"})]):
            with self.assertRaises(RuntimeError) as ctx:
                self.a.ensure_table("新表")
        self.assertIn("already exists", str(ctx.exception))

    def test_row_ids_of_tolerates_response_shapes(self):
        self.assertEqual(_row_ids_of([{"row_id": "a", "display_value": "x"}]), ["a"])
        self.assertEqual(_row_ids_of(["a", "b"]), ["a", "b"])
        self.assertEqual(_row_ids_of("a"), ["a"])
        self.assertEqual(_row_ids_of(None), [])
        self.assertEqual(_row_ids_of([]), [])
        self.assertEqual(_row_ids_of([{"_id": "c"}]), ["c"])


# ══════════════════════════════════════════════════════════════
# 5. schema：中立类型 + 方向敏感的 link_id
# ══════════════════════════════════════════════════════════════

class SeaTableLinkTypeCaseTests(unittest.TestCase):
    """link 列的 `type` 字符串**大小写混用**（真实 Base 只读实测）。

    实测 production Base：`type == "link"` 36 列、`type == "LINK"`（**大写**）4 列
    —— `生产计划.工时记录` / `生产工序.工时记录` / `工时记录.关联生产计划` /
    `工时记录.关联工序`，link_id 分别 `W1Q1` / `Hh3j`；另有 9 个 `link-formula`
    （`data` 里**没有 link_id**，是计算列，不是可真写的关联列）。

    早先 `_link_index()` 用精确比较 `!= "link"`，那 4 个大写列被**静默跳过**，后果：
      · `link_columns("工时记录")` 返回 `[]`（真表有 2 个关联列）
      · `known_link_ids()` 只有 18 个（真实 20 个）
      · `_assert_link_id_known("W1Q1")` **谎报「在本 Base 中不存在」** ——
        一个守卫反过来拦截**合法**的 link_id

    这与 `list_linked` 的空桩属于**同一类静默失败**，是本次一并修掉的。
    这组测试钉住修复，防止哪天又被改回精确比较。
    """

    def setUp(self):
        self.a = SeaTableAdapter("tok", "https://seatable.invalid", "uuid-1", base_name="production")
        self.a._access = "access-x"
        self.a._server = "https://gw.invalid"
        self.a._meta = _meta_with_links()

    def test_uppercase_link_columns_are_indexed(self):
        cols = sorted(c["column"] for c in self.a.link_columns("工时记录"))
        self.assertEqual(cols, ["关联工序", "关联生产计划"])

    def test_uppercase_link_ids_are_known(self):
        ids = self.a.known_link_ids()
        self.assertIn("W1Q1", ids)
        self.assertIn("Hh3j", ids)

    def test_uppercase_link_id_is_not_falsely_rejected(self):
        """核心回归：守卫不能把**合法**的 link_id 判成「不存在」。"""
        self.a._assert_link_id_known("W1Q1")     # 不抛错即通过
        self.a._assert_link_id_known("Hh3j")

    def test_uppercase_link_resolves_by_table_name(self):
        self.assertEqual(self.a._resolve_link_id("生产计划", "工时记录"), "W1Q1")
        self.assertEqual(self.a._resolve_link_id("工时记录", "生产工序"), "Hh3j")

    def test_uppercase_link_map_covers_both_sides(self):
        """大小写不同的关联也是双向的：两张表上各有列。"""
        m = self.a.link_map()
        self.assertEqual(m["W1Q1"], {"生产计划": "工时记录", "工时记录": "关联生产计划"})
        self.assertEqual(m["Hh3j"], {"工时记录": "关联工序", "生产工序": "工时记录"})

    def test_link_formula_columns_stay_excluded(self):
        """计算列没有 link_id，混进索引会把 `link_map` 污染成 `{None: ...}`。"""
        self.assertNotIn(None, self.a.link_map())
        self.assertNotIn("IC采购支出",
                         [c["column"] for c in self.a.link_columns("生产计划")])


class SeaTableOtherTypeCaseTests(unittest.TestCase):
    """除 `link` 外，`date` / `single-select` 的类型字符串**也混用大小写**（真实 Base 只读实测）。

    实测 production Base 的类型直方图里：
      · `date` **40** 列，但 **`DATE` 2 列**（`工时记录.开始时间` / `结束时间`）
      · `single-select` 31 列（本次未发现大写形态，但同属一类风险）
      · 另有 `LONG_TEXT` 2 列、`NUMBER` 1 列

    影响：`_cell_meta()` 的 select 判断与 `_flat_cell()` 的日期截断都是精确比较，
    精确比较会让这些列**静默漏过扁平化** —— 日期留着完整 ISO（`2026-05-07T00:00:00+08:00`），
    而下游是按 `YYYY-MM-DD` 写的。这正是 `_cell_meta` 自己 docstring 里记的那类静默错误。

    ⚠️ 这里和 `link` 那条是**同一类** bug，只是发现得晚一步（由独立验证者提示、我实测确认）。
    """

    def setUp(self):
        self.a = SeaTableAdapter("tok", "https://seatable.invalid", "uuid-1", base_name="production")
        self.a._access = "access-x"
        self.a._server = "https://gw.invalid"
        self.a._meta = _meta_with_links()

    def test_flat_cell_truncates_uppercase_date(self):
        from adapters.seatable import SeaTableAdapter as A
        iso = "2026-05-07T00:00:00+08:00"
        self.assertEqual(A._flat_cell(iso, "DATE", None), "2026-05-07")   # 大写：修复点
        self.assertEqual(A._flat_cell(iso, "date", None), "2026-05-07")   # 小写：不能改坏
        self.assertEqual(A._flat_cell(iso, None, None), iso)             # 无类型：不动

    def test_cell_meta_maps_uppercase_select_options(self):
        """`_cell_meta()` 走的是**实时** `GET /columns/`（不是 `self._meta`），
        且异常被 `except Exception: pass` 吞掉 —— 不 mock 的话会静默返回空 map，
        测试就变成假绿。所以这里必须把 `/columns/` 喂进去。
        """
        cols = next(t for t in self.a._meta["tables"] if t["name"] == "生产计划")["columns"]
        with patch("adapters.seatable.requests.get",
                   side_effect=lambda url, **kw: _Resp({"columns": cols})):
            sel, _types = self.a._cell_meta("生产计划")
        self.assertEqual(sel.get("优先级"), {"9": "高"})     # 大写 SINGLE-SELECT
        self.assertEqual(sel.get("状态"), {"58668": "在产"})  # 小写不受影响

    def test_uppercase_select_and_date_survive_list_rows(self):
        """端到端：大写类型的列也要被正确扁平化。"""
        body = {"rows": [{"_id": "r1", "k11": "9", "k12": "2026-05-07T00:00:00+08:00"}]}
        cols = next(t for t in self.a._meta["tables"] if t["name"] == "生产计划")["columns"]

        def fake_get(url, headers=None, params=None, timeout=None):
            if url.endswith("/columns/"):
                return _Resp({"columns": cols})
            return _Resp(body)

        with patch("adapters.seatable.requests.get", side_effect=fake_get):
            rows = self.a.list_rows("生产计划")
        self.assertEqual(rows[0]["优先级"], "高")            # 选项 id → 中文名
        self.assertEqual(rows[0]["开始时间"], "2026-05-07")  # ISO → YYYY-MM-DD


class SchemaContractTests(unittest.TestCase):
    def test_link_id_for_is_direction_sensitive(self):
        """项目 ↔ 生产计划 有两条方向相反的关联，必须各归各。

        回归：旧实现用无序集合比较，两条互相命中，永远只返回先出现的 3Fld。
        """
        self.assertEqual(schema.link_id_for("生产计划", "项目"), "3Fld")
        self.assertEqual(schema.link_id_for("项目", "生产计划"), "wana")
        self.assertIsNone(schema.link_id_for("没有的表", "项目"))

    def test_link_col_of_matches_real_column_names(self):
        self.assertEqual(schema.link_col_of("生产计划", "3Fld"), "关联项目")
        self.assertEqual(schema.link_col_of("项目", "wana"), "阶段")

    def test_backend_type_maps_neutral_names(self):
        self.assertEqual(schema.backend_type("seatable", "text"), "text")
        self.assertEqual(schema.backend_type("seatable", "select"), "single-select")
        self.assertEqual(schema.backend_type("seatable", "multiselect"), "multiple-select")
        self.assertEqual(schema.backend_type("seatable", "bool"), "checkbox")
        self.assertEqual(schema.backend_type("local", "select"), "text")   # 本地一切都是文本

    def test_backend_type_passes_through_native_names(self):
        self.assertEqual(schema.backend_type("seatable", "long-text"), "long-text")

    def test_backend_type_refuses_non_creatable_and_unknown(self):
        for bad in ("link", "formula"):
            with self.assertRaises(Unsupported):
                schema.backend_type("seatable", bad)
        with self.assertRaises(ValueError):
            schema.backend_type("seatable", "根本没有这个类型")   # 不许静默降级成 text
        with self.assertRaises(Unsupported):
            schema.backend_type("还没有的后端", "text")


# ══════════════════════════════════════════════════════════════
# 6. 可插拔性证明：注册新后端，工厂不改一行
# ══════════════════════════════════════════════════════════════

class _FakeBackendAdapter:
    """模拟「未来某个底座」的适配器（鸭子类型，不继承 BaseAdapter 也行）。"""

    backend = "feishufake"
    CAPS = frozenset({CAP_READ, CAP_WRITE, CAP_SCHEMA_MANAGE})

    def __init__(self, app_token, table_id, label=None):
        self.app_token = app_token
        self.table_id = table_id
        self.label = label

    def auth(self):
        pass

    def list_rows(self, table):
        return []


class PluggableBackendTests(unittest.TestCase):
    """**这是本期改造成立与否的判据。**

    如果新增一个底座还需要去改 `factory.py` 的 if/else、去改调用方的
    `backend == "seatable"`，那就不叫可插拔。下面这个用例注册一个全新后端，
    全程只动注册表。
    """

    def setUp(self):
        from adapters import factory
        self.F = factory
        self._had = "feishufake" in factory.BACKENDS
        factory.register_backend(
            "feishufake",
            label="飞书多维表格（假）",
            make=lambda sel, cfg: _FakeBackendAdapter(sel["app_token"], sel["table_id"],
                                                      sel.get("name")),
            required_keys=("app_token", "table_id"),
            key_aliases={"app_token": ("token",)},
            named_bases=True,
            default_server="",
        )

    def tearDown(self):
        if not self._had:
            self.F.BACKENDS.pop("feishufake", None)

    def _cfg(self):
        return {
            "backend": "feishufake",
            "feishufake": {
                "default_base": "主表",
                "bases": {
                    "主表": {"app_token": "app-1", "table_id": "tbl-1"},
                    "副表": {"token": "app-2", "table_id": "tbl-2"},   # 走别名
                },
            },
        }

    def test_registered_backend_is_listed(self):
        self.assertIn("feishufake", self.F.list_backends())

    def test_named_instances_resolve_generically(self):
        cfg = self._cfg()
        base = self.F.get_base_config(cfg, "主表")
        self.assertEqual(base["app_token"], "app-1")
        self.assertEqual(self.F.get_base_config(cfg)["name"], "主表")     # default_base
        self.assertIsNone(self.F.get_base_config(cfg, "没有的实例"))

    def test_key_alias_is_normalized(self):
        self.assertEqual(self.F.get_base_config(self._cfg(), "副表")["app_token"], "app-2")

    def test_adapter_is_built_for_the_new_backend(self):
        a = self.F.get_adapter(self._cfg(), base_name="主表")
        self.assertIsInstance(a, _FakeBackendAdapter)
        self.assertEqual((a.app_token, a.table_id, a.label), ("app-1", "tbl-1", "主表"))

    def test_get_adapters_returns_all_named_instances(self):
        got = self.F.get_adapters(self._cfg())
        self.assertEqual(set(got), {"主表", "副表"})
        self.assertEqual(got["副表"].table_id, "tbl-2")

    def test_backend_info_reports_the_registered_label(self):
        name, info = self.F.backend_info(self._cfg())
        self.assertEqual(name, "feishufake")
        self.assertEqual(info["label"], "飞书多维表格（假）")

    def test_unknown_backend_fails_closed_on_write_path(self):
        """未知后端**绝不能**静默退化成 local —— 那会把线上写入变成写本地 CSV。"""
        cfg = {"backend": "还没有实现的后端", "local": {"data_dir": "data"}}
        with self.assertRaises(RuntimeError) as ctx:
            self.F.get_adapter(cfg, strict=True)
        self.assertIn("未知后端", str(ctx.exception))
        # 非写入路径只告警并退回 local（保持历史行为）
        a = self.F.get_adapter(cfg)
        self.assertEqual(backend_of(a), "local")

    def test_missing_required_key_fails_closed(self):
        cfg = {"backend": "feishufake",
               "feishufake": {"bases": {"主表": {"app_token": "app-1"}}}}   # 缺 table_id
        with self.assertRaises(RuntimeError) as ctx:
            self.F.get_adapter(cfg, base_name="主表", strict=True)
        self.assertIn("table_id", str(ctx.exception))

    def test_absent_named_instance_fails_closed(self):
        cfg = self._cfg()
        with self.assertRaises(RuntimeError):
            self.F.get_adapter(cfg, base_name="没有的实例", strict=True)


if __name__ == "__main__":
    unittest.main()
