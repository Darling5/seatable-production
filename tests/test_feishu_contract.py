# -*- coding: utf-8 -*-
"""飞书多维表格适配器契约测试（第 1 期）。

**全部离线**：用一个记录型假传输层替代 ``_CliTransport`` / ``_ApiTransport``，
不碰网络、不碰任何真实 Base。所有断言对应的是**实测确认过的协议事实**
（见 adapters/feishu.py 顶部 docstring 与 probes/ 下的探测脚本），不是照文档猜的。

重点用用例钉住三类「看起来成功」的静默失败：
  ① ``+record-batch-update`` 对不存在的行 **rc=0**，只在 ``record_not_found`` 里报——
     适配器必须据此报错；
  ② 服务端 ``ignored_fields`` 表示字段被静默丢弃——适配器必须据此报错；
  ③ ``link_append`` 在飞书上不实现，必须抛 ``Unsupported`` 而不是悄悄读-改-写。
"""
import json
import os
import sys
import unittest
from unittest.mock import patch

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

from adapters import factory, schema  # noqa: E402
from adapters.base import (CAP_LINK, CAP_LINK_READ, CAP_SCHEMA_MANAGE,  # noqa: E402
                           Unsupported, capabilities_of)
from adapters.feishu import FeishuAdapter, FeishuError, find_lark_cli  # noqa: E402


# ══════════════════════════════════════════════════════════════
# 假传输层
# ══════════════════════════════════════════════════════════════

def _field(name, ftype="text", fid=None, **kw):
    f = {"id": fid or ("fld_" + name), "name": name, "type": ftype}
    f.update(kw)
    return f


class _FakeTransport:
    """按子命令返回预置 data 段；所有调用都记录下来供断言。"""

    def __init__(self, tables=None, fields=None, records=None):
        self.tables = tables if tables is not None else {"项目": "tbl1", "生产计划": "tbl2"}
        self.fields = fields or {"tbl1": [_field("名称")]}
        self.records = records or {}
        self.calls = []          # [(cmd, args, json_body)]
        self.responses = {}      # cmd -> data 或 callable(args, body) -> data

    def call(self, args, json_body=None, timeout=None):
        cmd = args[0]
        self.calls.append((cmd, list(args), json_body))
        fn = self.responses.get(cmd)
        if callable(fn):
            return fn(args, json_body)
        if cmd == "+table-list":
            return {"tables": [{"id": v, "name": k} for k, v in self.tables.items()],
                    "total": len(self.tables)}
        if cmd == "+field-list":
            tid = args[args.index("--table-id") + 1]
            return {"fields": self.fields.get(tid, [])}
        if cmd == "+record-list":
            tid = args[args.index("--table-id") + 1]
            return self.records.get(tid, {"data": [], "fields": [],
                                          "record_id_list": [], "has_more": False})
        if cmd in ("+record-batch-create", "+record-batch-update", "+record-delete"):
            return {"record_id_list": [], "record_not_found": []}
        if cmd == "+table-create":
            return {"table": {"id": "tblNew", "name": args[args.index("--name") + 1]}}
        return {}

    def calls_of(self, cmd):
        return [c for c in self.calls if c[0] == cmd]


def _adapter(tables=None, fields=None, records=None):
    a = FeishuAdapter("basetok", base_name="production")
    a._t = _FakeTransport(tables, fields, records)
    a._kind_used = "cli"
    return a


# ══════════════════════════════════════════════════════════════
# 1. 单元格翻译
# ══════════════════════════════════════════════════════════════

class FeishuCellTests(unittest.TestCase):
    def test_flat_cell_truncates_iso_datetime_to_date(self):
        """飞书回的是带毫秒和时区的 ISO；下游按 YYYY-MM-DD 用。"""
        self.assertEqual(FeishuAdapter._flat_cell("2026-05-07T00:00:00.000+08:00", "datetime"),
                         "2026-05-07")

    def test_flat_cell_flattens_single_select_but_keeps_multi(self):
        self.assertEqual(FeishuAdapter._flat_cell(["进行中"], "select", False), "进行中")
        self.assertEqual(FeishuAdapter._flat_cell(["A", "B"], "multiselect", True), ["A", "B"])
        self.assertEqual(FeishuAdapter._flat_cell([], "select", False), "")

    def test_flat_cell_none_becomes_empty_string_not_none(self):
        """给 None 会让下游拼出字面量 "None"（SeaTable 路径是「键不存在」）。"""
        for t in ("text", "number", "checkbox", "attachment", "link"):
            self.assertEqual(FeishuAdapter._flat_cell(None, t), "")

    def test_flat_cell_keeps_number_bool_and_object_arrays(self):
        self.assertEqual(FeishuAdapter._flat_cell(12.5, "number"), 12.5)
        self.assertIs(FeishuAdapter._flat_cell(True, "checkbox"), True)
        att = [{"file_token": "t", "name": "a.pdf", "size": 1}]
        self.assertEqual(FeishuAdapter._flat_cell(att, "attachment"), att)

    def test_to_cell_wraps_select_into_array(self):
        self.assertEqual(FeishuAdapter._to_cell("进行中", "select"), ["进行中"])
        self.assertEqual(FeishuAdapter._to_cell(["A", "B"], "multiselect", True), ["A", "B"])
        self.assertEqual(FeishuAdapter._to_cell(["A", "B"], "select", False), ["A"])

    def test_to_cell_parses_string_booleans(self):
        """``bool("false") is True`` —— 字符串必须显式翻译，否则「假」会写成「真」。"""
        self.assertIs(FeishuAdapter._to_cell("false", "checkbox"), False)
        self.assertIs(FeishuAdapter._to_cell("true", "checkbox"), True)
        self.assertIs(FeishuAdapter._to_cell("否", "checkbox"), False)

    def test_to_cell_coerces_number_strings(self):
        self.assertEqual(FeishuAdapter._to_cell("1,234.5", "number"), 1234.5)

    def test_to_cell_leaves_unparsable_number_alone(self):
        """解析不了就原样交给服务端报错 —— 绝不悄悄变成 0。"""
        self.assertEqual(FeishuAdapter._to_cell("abc", "number"), "abc")

    def test_to_cell_wraps_link_ids(self):
        self.assertEqual(FeishuAdapter._to_cell("rec1", "link"), [{"id": "rec1"}])
        self.assertEqual(FeishuAdapter._to_cell(["rec1", "rec2"], "link"),
                         [{"id": "rec1"}, {"id": "rec2"}])

    def test_to_cell_empties_become_null(self):
        for v in (None, "", [], ()):
            self.assertIsNone(FeishuAdapter._to_cell(v, "text"))


class FeishuReadonlyGuardTests(unittest.TestCase):
    def test_readonly_columns_are_dropped_client_side(self):
        fmap = {"名称": _field("名称"),
                "公式列": _field("公式列", "formula"),
                "查找列": _field("查找列", "lookup"),
                "编号": _field("编号", "auto_number"),
                "创建时间": _field("创建时间", "created_time")}
        a = _adapter()
        rec = a._to_record({"名称": "x", "公式列": "y", "查找列": "z",
                            "编号": "1", "创建时间": "t"}, fmap)
        self.assertEqual(rec, {"名称": "x"})

    def test_unknown_column_raises_instead_of_silently_dropping(self):
        """列名写错是最常见的错；静默丢掉会让「我写了但它没存」无从查起。"""
        a = _adapter()
        with self.assertRaises(FeishuError) as ctx:
            a._to_record({"名称": "x", "不存在的列": 1}, {"名称": _field("名称")})
        self.assertIn("不存在的列", str(ctx.exception))

    def test_row_id_key_is_never_sent_as_a_column(self):
        a = _adapter()
        self.assertEqual(a._to_record({"名称": "x", "__row_id__": "rec1"},
                                      {"名称": _field("名称")}), {"名称": "x"})


# ══════════════════════════════════════════════════════════════
# 2. 读
# ══════════════════════════════════════════════════════════════

class FeishuReadTests(unittest.TestCase):
    FIELDS = [_field("名称", "text", "f1"),
              _field("状态", "select", "f2", multiple=False),
              _field("标签", "select", "f3", multiple=True),
              _field("日期", "datetime", "f4")]

    def _recs(self, rows, ids, has_more=False):
        return {"data": rows, "fields": [f["name"] for f in self.FIELDS],
                "record_id_list": ids, "has_more": has_more}

    def test_get_metadata_folds_select_multiple_into_multiselect(self):
        a = _adapter(fields={"tbl1": self.FIELDS})
        cols = a.get_metadata("项目")["columns"]
        self.assertEqual([c["name"] for c in cols], ["名称", "状态", "标签", "日期"])
        self.assertEqual([c["type"] for c in cols],
                         ["text", "select", "multiselect", "datetime"])
        self.assertEqual(cols[0]["key"], "f1")

    def test_table_id_resolution_lists_available_names_on_miss(self):
        """报错必须一次说清「现有表有哪些」，否则只能靠猜。"""
        a = _adapter()
        with self.assertRaises(KeyError) as ctx:
            a._table_id("不存在")
        self.assertIn("项目", str(ctx.exception))
        self.assertIn("生产计划", str(ctx.exception))

    def test_table_exists_is_name_based(self):
        a = _adapter()
        self.assertTrue(a.table_exists("项目"))
        self.assertFalse(a.table_exists("没有这张表"))

    def test_list_rows_translates_cells_and_keeps_row_id(self):
        rows = [["甲", ["进行中"], ["A", "B"], "2026-05-07T00:00:00.000+08:00"]]
        a = _adapter(fields={"tbl1": self.FIELDS},
                     records={"tbl1": self._recs(rows, ["rec1"])})
        out = a.list_rows("项目")
        self.assertEqual(out, [{"名称": "甲", "状态": "进行中", "标签": ["A", "B"],
                                "日期": "2026-05-07", "__row_id__": "rec1"}])

    def test_list_rows_pages_until_has_more_is_false(self):
        """标称每页 200 行：满页且 has_more 时应继续拉，不满页即停。"""
        full = [["x"] * 4] * 200
        pages = [
            {"data": full, "fields": [f["name"] for f in self.FIELDS],
             "record_id_list": ["r%d" % i for i in range(200)], "has_more": True},
            {"data": full[:50], "fields": [f["name"] for f in self.FIELDS],
             "record_id_list": ["z%d" % i for i in range(50)], "has_more": False},
        ]
        a = _adapter(fields={"tbl1": self.FIELDS})
        seq = {"i": 0}

        def resp(_args, _body):
            r = pages[seq["i"]]
            seq["i"] += 1
            return r

        a._t.responses["+record-list"] = resp
        out = a.list_rows("项目")
        self.assertEqual(len(out), 250)
        self.assertEqual(len(a._t.calls_of("+record-list")), 2)

    def test_list_rows_stops_when_has_more_lies(self):
        """has_more 为真却一行都不给 → 必须停下，否则是死循环。"""
        a = _adapter(fields={"tbl1": self.FIELDS})
        a._t.responses["+record-list"] = lambda a_, b_: {
            "data": [], "fields": ["名称"], "record_id_list": [], "has_more": True}
        self.assertEqual(a.list_rows("项目"), [])
        self.assertEqual(len(a._t.calls_of("+record-list")), 1)

    def test_get_row_falls_back_to_scan_when_fast_path_is_untrusted(self):
        """``+record-get`` 对真实存在的行也可能回 record_not_found（实测），
        此时必须退回全表扫描，而不是回答「找不到」。"""
        a = _adapter(fields={"tbl1": self.FIELDS},
                     records={"tbl1": self._recs([["甲", ["进行中"], [], None]], ["rec1"])})
        a._t.responses["+record-get"] = lambda a_, b_: {
            "data": [["甲", ["进行中"], [], None]], "fields": [f["name"] for f in self.FIELDS],
            "record_id_list": ["rec1"], "record_not_found": ["rec1"]}
        self.assertEqual(a.get_row("项目", "rec1")["名称"], "甲")

    def test_get_row_returns_none_for_missing_row(self):
        a = _adapter(fields={"tbl1": self.FIELDS},
                     records={"tbl1": self._recs([["甲", [], [], None]], ["rec1"])})
        a._t.responses["+record-get"] = lambda a_, b_: {
            "data": [[None, None, None, None]], "fields": [f["name"] for f in self.FIELDS],
            "record_id_list": ["recX"], "record_not_found": ["recX"]}
        self.assertIsNone(a.get_row("项目", "recX"))


# ══════════════════════════════════════════════════════════════
# 3. 写
# ══════════════════════════════════════════════════════════════

class FeishuWriteTests(unittest.TestCase):
    FIELDS = [_field("名称", "text", "f1"), _field("状态", "select", "f2", multiple=False)]

    def test_append_rows_chunks_at_200(self):
        a = _adapter(fields={"tbl1": self.FIELDS})
        a._t.responses["+record-batch-create"] = (
            lambda args, body: {"record_id_list": ["r%d" % i
                                                   for i in range(len(body["create_records"]))]})
        ids = a.append_rows("项目", [{"名称": "x%d" % i} for i in range(450)])
        chunks = [len(c[2]["create_records"]) for c in a._t.calls_of("+record-batch-create")]
        self.assertEqual(chunks, [200, 200, 50])
        self.assertEqual(len(ids), 450)

    def test_append_rows_backfills_none_when_server_returns_fewer_ids(self):
        a = _adapter(fields={"tbl1": self.FIELDS})
        a._t.responses["+record-batch-create"] = lambda a_, b_: {"record_id_list": ["only"]}
        ids = a.append_rows("项目", [{"名称": "a"}, {"名称": "b"}])
        self.assertEqual(ids, [None, None])   # 宁可全 None，也不错位

    def test_append_rows_raises_on_ignored_fields(self):
        """服务端说「这些字段我忽略了」= 写漏了。必须报错，不能当成功。"""
        a = _adapter(fields={"tbl1": self.FIELDS})
        a._t.responses["+record-batch-create"] = lambda a_, b_: {
            "record_id_list": ["r1"], "ignored_fields": ["公式列"]}
        with self.assertRaises(FeishuError) as ctx:
            a.append_rows("项目", [{"名称": "a"}])
        self.assertIn("公式列", str(ctx.exception))

    def test_update_row_raises_when_record_not_found_persists(self):
        """实测：不存在/不可见的 record_id **rc=0**，只在 record_not_found 里报。
        只看退出码 = 把「没写进去」当成「写好了」。"""
        a = _adapter(fields={"tbl1": self.FIELDS})
        a._t.responses["+record-batch-update"] = lambda a_, b_: {
            "record_id_list": [], "record_not_found": ["recX"]}
        with patch("adapters.feishu.time.sleep"), self.assertRaises(FeishuError) as ctx:
            a.update_row("项目", "recX", {"名称": "a"})
        self.assertIn("record_not_found", str(ctx.exception))

    def test_update_row_retries_then_succeeds_within_settle_window(self):
        """实测：新建记录后 ~3s 内更新会误报 record_not_found，之后成功。"""
        a = _adapter(fields={"tbl1": self.FIELDS})
        seen = {"n": 0}

        def resp(_, body):
            seen["n"] += 1
            if seen["n"] == 1:
                return {"record_id_list": [], "record_not_found": ["rec1"]}
            return {"record_id_list": ["rec1"], "record_not_found": []}

        a._t.responses["+record-batch-update"] = resp
        with patch("adapters.feishu.time.sleep"):
            a.update_row("项目", "rec1", {"名称": "a"})
        self.assertEqual(seen["n"], 2)

    def test_update_row_with_only_readonly_columns_is_a_noop_not_an_api_call(self):
        fmap = {"名称": _field("名称"), "公式列": _field("公式列", "formula")}
        a = _adapter(fields={"tbl1": [fmap["名称"], fmap["公式列"]]})
        a.update_row("项目", "rec1", {"公式列": "x"})
        self.assertEqual(a._t.calls_of("+record-batch-update"), [])

    def test_delete_rows_raises_on_record_not_found(self):
        """删不掉就不能当删掉了 —— 否则上层会以为已清理，留下幽灵数据。"""
        a = _adapter(fields={"tbl1": self.FIELDS})
        a._t.responses["+record-delete"] = lambda a_, b_: {
            "deleted": True, "record_not_found": ["recX"]}
        with self.assertRaises(FeishuError):
            a.delete_rows("项目", ["recX"])

    def test_delete_rows_chunks_at_200(self):
        a = _adapter(fields={"tbl1": self.FIELDS})
        a.delete_rows("项目", ["r%d" % i for i in range(250)])
        chunks = [len(c[2]["record_id_list"]) for c in a._t.calls_of("+record-delete")]
        self.assertEqual(chunks, [200, 50])


# ══════════════════════════════════════════════════════════════
# 4. 关联
# ══════════════════════════════════════════════════════════════

class FeishuLinkTests(unittest.TestCase):
    """实测事实：``bidirectional=false`` 的关联字段在对方表**没有反向列**，
    所以飞书上根本做不到「两表各记一次」的双向关联。"""

    FIELDS_ONE_WAY = [_field("标题", "text", "f1"),
                      _field("关联A", "link", "f2", link_table="tbl1", bidirectional=False)]
    FIELDS_TWO_WAY = [_field("标题", "text", "f1"),
                      _field("关联A", "link", "f2", link_table="tbl1", bidirectional=True)]

    def test_link_refuses_one_way_field_instead_of_silently_writing_one_side(self):
        a = _adapter(tables={"项目": "tbl1", "挂靠": "tbl2"},
                     fields={"tbl2": self.FIELDS_ONE_WAY})
        with self.assertRaises(Unsupported) as ctx:
            a.link("挂靠", "项目", "关联A", "rec1", ["recA"])
        self.assertIn("link_one_way", str(ctx.exception))

    def test_link_writes_when_field_is_bidirectional(self):
        a = _adapter(tables={"项目": "tbl1", "挂靠": "tbl2"},
                     fields={"tbl2": self.FIELDS_TWO_WAY})
        a.link("挂靠", "项目", "关联A", "rec1", ["recA"])
        body = a._t.calls_of("+record-batch-update")[0][2]
        self.assertEqual(body["update_records"]["rec1"]["关联A"], [{"id": "recA"}])

    def test_link_one_way_writes_only_that_column(self):
        a = _adapter(tables={"项目": "tbl1", "挂靠": "tbl2"},
                     fields={"tbl2": self.FIELDS_ONE_WAY})
        a.link_one_way("挂靠", "项目", "关联A", "rec1", ["recA", "recB"])
        body = a._t.calls_of("+record-batch-update")[0][2]
        self.assertEqual(body["update_records"]["rec1"],
                         {"关联A": [{"id": "recA"}, {"id": "recB"}]})

    def test_link_append_is_unsupported_on_feishu(self):
        """读-改-写在飞书上有真实风险（写后有可见性延迟，读到旧值会整体覆盖），
        所以刻意沿用基类的 Unsupported，而不是悄悄实现一个不安全的版本。"""
        a = _adapter(tables={"项目": "tbl1", "挂靠": "tbl2"},
                     fields={"tbl2": self.FIELDS_ONE_WAY})
        with self.assertRaises(Unsupported):
            a.link_append("挂靠", "项目", "关联A", "rec1", ["recA"])

    def test_link_columns_lists_names(self):
        a = _adapter(tables={"项目": "tbl1", "挂靠": "tbl2"},
                     fields={"tbl2": self.FIELDS_TWO_WAY})
        self.assertEqual(a.link_columns("挂靠"), ["关联A"])

    def test_ambiguous_link_pair_requires_explicit_column(self):
        fields = [_field("关联A", "link", "f1", link_table="tbl1", bidirectional=True),
                  _field("关联B", "link", "f2", link_table="tbl1", bidirectional=True)]
        a = _adapter(tables={"项目": "tbl1", "挂靠": "tbl2"}, fields={"tbl2": fields})
        with self.assertRaises(KeyError) as ctx:
            a._resolve_link_field("挂靠", "项目", "")
        self.assertIn("必须显式指定", str(ctx.exception))

    def test_unknown_link_column_raises_with_existing_names(self):
        a = _adapter(tables={"项目": "tbl1", "挂靠": "tbl2"},
                     fields={"tbl2": self.FIELDS_TWO_WAY})
        with self.assertRaises(KeyError) as ctx:
            a._resolve_link_field("挂靠", "项目", "不存在的列")
        self.assertIn("关联A", str(ctx.exception))

    def test_list_linked_reads_ids_from_link_cells(self):
        fields = self.FIELDS_TWO_WAY
        recs = {"data": [["挂甲", [{"id": "recA"}, {"id": "recB"}]]],
                "fields": ["标题", "关联A"], "record_id_list": ["rec1"], "has_more": False}
        a = _adapter(tables={"项目": "tbl1", "挂靠": "tbl2"},
                     fields={"tbl2": fields}, records={"tbl2": recs})
        a._t.responses["+record-get"] = lambda a_, b_: {
            "data": [["挂甲", [{"id": "recA"}, {"id": "recB"}]]],
            "fields": ["标题", "关联A"], "record_id_list": ["rec1"], "record_not_found": []}
        self.assertEqual(a.list_linked("挂靠", "rec1", "关联A"), ["recA", "recB"])

    def test_list_linked_empty_list_means_really_empty(self):
        """空列表只能代表「确实没有关联」。"""
        a = _adapter(tables={"项目": "tbl1", "挂靠": "tbl2"},
                     fields={"tbl2": self.FIELDS_TWO_WAY},
                     records={"tbl2": {"data": [["挂甲", []]], "fields": ["标题", "关联A"],
                                       "record_id_list": ["rec1"], "has_more": False}})
        a._t.responses["+record-get"] = lambda a_, b_: {
            "data": [["挂甲", []]], "fields": ["标题", "关联A"],
            "record_id_list": ["rec1"], "record_not_found": []}
        self.assertEqual(a.list_linked("挂靠", "rec1", "关联A"), [])

    def test_list_linked_raises_when_row_is_missing(self):
        """行都不存在时报「没有关联」是误导 —— 必须抛错区分。"""
        a = _adapter(tables={"项目": "tbl1", "挂靠": "tbl2"},
                     fields={"tbl2": self.FIELDS_TWO_WAY},
                     records={"tbl2": {"data": [], "fields": ["标题", "关联A"],
                                       "record_id_list": [], "has_more": False}})
        a._t.responses["+record-get"] = lambda a_, b_: {
            "data": [], "fields": ["标题", "关联A"],
            "record_id_list": ["recX"], "record_not_found": ["recX"]}
        with self.assertRaises(KeyError):
            a.list_linked("挂靠", "recX", "关联A")

    def test_list_linked_does_not_return_empty_for_table_without_links(self):
        """没有关联列 ≠ 没有关联。空桩式返回是本仓库踩过的坑。"""
        a = _adapter(fields={"tbl1": [_field("名称")]})
        with self.assertRaises(KeyError):
            a.list_linked("项目", "rec1", "")


# ══════════════════════════════════════════════════════════════
# 5. 建表 / 类型映射 / 能力声明
# ══════════════════════════════════════════════════════════════

class FeishuSchemaTests(unittest.TestCase):
    def test_ensure_table_returns_exists_without_creating(self):
        a = _adapter()
        self.assertEqual(a.ensure_table("项目", [{"name": "名称", "type": "text"}]), "exists")
        self.assertEqual(a._t.calls_of("+table-create"), [])

    def test_ensure_table_declares_multiselect_with_multiple_flag(self):
        """飞书单/多选是同一个 type 加一个 multiple 布尔位，不是两个类型。"""
        a = _adapter(tables={})
        a.ensure_table("新表", [{"name": "名称", "type": "text"},
                                {"name": "标签", "type": "multiselect"}])
        body = a._t.calls_of("+table-create")[0][2]
        self.assertEqual(body["fields"][1], {"name": "标签", "type": "select",
                                             "multiple": True})
        self.assertEqual(body["fields"][0], {"name": "名称", "type": "text"})

    def test_ensure_table_without_columns_is_explicitly_unsupported(self):
        a = _adapter(tables={})
        with self.assertRaises(Unsupported):
            a.ensure_table("新表", [])

    def test_ensure_table_forwards_select_options(self):
        """实测：不带选项建出来的 select 列，写任何值都会报 800030005 ——
        飞书不会替你新增选项。所以列定义里的 options 必须原样转过去。"""
        a = _adapter(tables={})
        a.ensure_table("新表", [{"name": "状态", "type": "select",
                                 "options": ["进行中", "已完成"]},
                                {"name": "标签", "type": "multiselect",
                                 "options": [{"name": "高"}]}])
        body = a._t.calls_of("+table-create")[0][2]
        self.assertEqual(body["fields"][0]["options"],
                         [{"name": "进行中"}, {"name": "已完成"}])
        self.assertEqual(body["fields"][1]["options"], [{"name": "高"}])

    def test_list_tables_is_public_and_sorted(self):
        """没有这个方法，调用方就只能读 _tables 私有属性 —— 换后端必然 AttributeError。"""
        a = _adapter()
        self.assertEqual(a.list_tables(), ["生产计划", "项目"])

    def test_cli_transport_sends_table_create_as_fields_flag_not_json_body(self):
        """实测踩过：``+table-create`` 的 ``--json`` 是 ``--format json`` 的布尔简写，
        把 body 塞过去会得到「positional arguments are not supported」这种指不到病根的错。"""
        from adapters.feishu import _CliTransport
        t = _CliTransport("/fake/lark-cli")
        seen = {}

        def fake_exec(argv, timeout=None):
            seen["argv"] = argv
            return 0, '{"ok":true,"data":{}}', ""

        with patch.object(t, "_exec", side_effect=fake_exec):
            t.call(["+table-create", "--base-token", "b", "--name", "T"],
                   json_body={"name": "T", "fields": [{"name": "A", "type": "text"}]})
        argv = seen["argv"]
        self.assertIn("--fields", argv)
        self.assertNotIn("--json", argv)          # 绝不能落到布尔简写上
        self.assertEqual(json.loads(argv[argv.index("--fields") + 1]),
                         [{"name": "A", "type": "text"}])

    def test_cli_transport_uses_json_at_file_for_record_writes(self):
        from adapters.feishu import _CliTransport
        t = _CliTransport("/fake/lark-cli")
        seen = {}

        def fake_exec(argv, timeout=None):
            seen["argv"] = argv
            return 0, '{"ok":true,"data":{"record_id_list":["r1"]}}', ""

        with patch.object(t, "_exec", side_effect=fake_exec):
            t.call(["+record-batch-create", "--base-token", "b", "--table-id", "t"],
                   json_body={"create_records": [{"A": 1}]})
        argv = seen["argv"]
        self.assertIn("--json", argv)
        self.assertTrue(argv[argv.index("--json") + 1].startswith("@"), argv)

    def test_cli_transport_rejects_undeclared_body_command(self):
        """没声明过怎么传 body 的子命令 → 立刻报错，而不是让 CLI 回一句看不懂的话。"""
        from adapters.feishu import _CliTransport
        t = _CliTransport("/fake/lark-cli")
        with self.assertRaises(FeishuError) as ctx:
            t.call(["+table-list", "--base-token", "b"], json_body={"x": 1})
        self.assertIn("_JSON_BODY_CMDS", str(ctx.exception))

    def test_cli_transport_surfaces_stderr_error_envelope(self):
        """失败信封走 stderr 且 rc=1（实测），必须翻译成带 code/log_id 的异常。"""
        from adapters.feishu import _CliTransport
        t = _CliTransport("/fake/lark-cli")
        err = ('{"ok":false,"error":{"code":800030005,"message":"not_found",'
               '"hint":"Provide an existing option value.","log_id":"L1"}}')
        with patch.object(t, "_exec", return_value=(1, "", err)):
            with self.assertRaises(FeishuError) as ctx:
                t.call(["+table-list", "--base-token", "b"])
        self.assertEqual(ctx.exception.code, 800030005)
        self.assertEqual(ctx.exception.log_id, "L1")

    def test_backend_type_translates_neutral_names(self):
        self.assertEqual(schema.backend_type("feishu", "text"), "text")
        self.assertEqual(schema.backend_type("feishu", "bool"), "checkbox")
        self.assertEqual(schema.backend_type("feishu", "datetime"), "datetime")
        self.assertEqual(schema.backend_type("feishu", "multiselect"), "select")
        self.assertEqual(schema.backend_type("feishu", "attachment"), "attachment")

    def test_caps_do_not_claim_what_feishu_cannot_do(self):
        a = _adapter()
        caps = capabilities_of(a)
        self.assertIn(CAP_LINK, caps)
        self.assertIn(CAP_LINK_READ, caps)
        self.assertIn(CAP_SCHEMA_MANAGE, caps)
        # query_pushdown 只在「总是服务端过滤」时才配声明；本适配器的 query()
        # 走基类内存过滤，所以不能声明。
        self.assertNotIn("query_pushdown", caps)
        self.assertNotIn("idempotent", caps)
        self.assertNotIn("optimistic_lock", caps)

    def test_capability_read_is_side_effect_free(self):
        a = _adapter()
        before = len(a._t.calls)
        for _ in range(3):
            capabilities_of(a)
        self.assertEqual(len(a._t.calls), before)


# ══════════════════════════════════════════════════════════════
# 6. 可插拔性：工厂不改一行就能路由到飞书
# ══════════════════════════════════════════════════════════════

class FeishuRegistryTests(unittest.TestCase):
    def test_feishu_is_registered(self):
        self.assertIn("feishu", factory.list_backends())
        self.assertEqual(factory.list_backends(), sorted(factory.list_backends()))

    def test_named_instances_resolve_and_alias_app_token(self):
        cfg = {"backend": "feishu",
               "feishu": {"bases": {"production": {"app_token": "basetok"}}}}
        a = factory.get_adapter(cfg, base_name="production")
        self.assertIsInstance(a, FeishuAdapter)
        self.assertEqual(a.base_token, "basetok")
        self.assertEqual(a.base_name, "production")

    def test_app_credentials_are_read_from_backend_section_not_per_base(self):
        """app_id/app_secret 是应用级凭据，不该在每个命名 Base 里重复填。"""
        cfg = {"backend": "feishu",
               "feishu": {"app_id": "cli_x", "app_secret": "s", "identity": "bot",
                          "bases": {"production": {"base_token": "bt"}}}}
        a = factory.get_adapter(cfg, base_name="production")
        self.assertEqual((a.app_id, a.app_secret, a.identity), ("cli_x", "s", "bot"))

    def test_missing_base_token_fails_closed_on_write_path(self):
        cfg = {"backend": "feishu", "feishu": {"bases": {"production": {}}}}
        with self.assertRaises(RuntimeError):
            factory.get_adapter(cfg, base_name="production", strict=True)

    def test_all_named_instances_resolve(self):
        cfg = {"backend": "feishu",
               "feishu": {"bases": {"a": {"base_token": "t1"}, "b": {"base_token": "t2"}}}}
        got = factory.get_adapters(cfg)
        self.assertEqual(sorted(got), ["a", "b"])
        self.assertNotEqual(got["a"].base_token, got["b"].base_token)

    def test_feishu_declares_a_read_only_verify_hook(self):
        """部署钩子必须声明**只读**验证 —— 部署流程不该往生产 Base 里建东西。"""
        hooks = factory.backend_deploy_hooks("feishu")
        self.assertIn("verify", hooks)
        script, argv, timeout = hooks["verify"]
        self.assertEqual(script, "verify_feishu.py")
        self.assertIn("--read-only", argv)
        self.assertNotIn("--write-test", argv)
        # init_sync 尚未落地 → 不声明（部署报告会如实写「未声明」）
        self.assertNotIn("init_sync", hooks)

    def test_verify_hook_script_exists(self):
        script = factory.backend_deploy_hooks("feishu")["verify"][0]
        self.assertTrue(os.path.exists(os.path.join(HERE, "tools", script)),
                        "部署钩子指向的脚本必须真的存在，否则部署会跑到一半才炸")

    def test_default_server_is_open_feishu(self):
        self.assertEqual(factory.BACKENDS["feishu"]["default_server"],
                         "https://open.feishu.cn")


class FeishuAuthTests(unittest.TestCase):
    def test_missing_cli_and_no_credentials_raises_actionable_error(self):
        a = FeishuAdapter("bt", transport="cli", cli_path="")
        with patch("adapters.feishu.find_lark_cli", return_value=""):
            with self.assertRaises(FeishuError) as ctx:
                a.auth()
        msg = str(ctx.exception)
        self.assertIn("lark-cli", msg)
        self.assertIn("app_secret", msg)

    def test_api_transport_without_credentials_raises(self):
        a = FeishuAdapter("bt", transport="api")
        with self.assertRaises(FeishuError):
            a.auth()

    def test_find_lark_cli_returns_empty_when_absent(self):
        """找不到就返回空串 —— 不猜、不下载、不去 PATH 之外乱翻。"""
        with patch("adapters.feishu.shutil.which", return_value=None), \
             patch("adapters.feishu._CLI_HINTS", ()), \
             patch.dict(os.environ, {"LARK_CLI": ""}, clear=False):
            self.assertEqual(find_lark_cli("/definitely/not/here"), "")

    def test_find_lark_cli_prefers_explicit_path_over_hints(self):
        with patch("adapters.feishu.os.path.exists",
                   side_effect=lambda p: p == "/explicit/lark-cli"):
            self.assertEqual(find_lark_cli("/explicit/lark-cli"),
                             os.path.abspath("/explicit/lark-cli"))
