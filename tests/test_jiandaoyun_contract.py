# -*- coding: utf-8 -*-
"""简道云适配器契约测试（第 2 期）。

**全部离线**：用一个按路径回应的假传输层替代 ``_HttpTransport``，不碰网络。

⚠️ 与 ``tests/test_feishu_contract.py`` 的**根本差别**：飞书那份测试断言的是
「实测确认过的协议事实」；这一份断言的是「``adapters/jiandaoyun.py`` 里那些
**来自官方文档的推断**被正确实现了」。所以这里能证明的是
「代码与设计一致」，**不能**证明「设计符合真实服务端」。后者只能靠
``tools/verify_jiandaoyun.py`` 在真实环境里跑出来 —— 本机没有那个环境。

重点用用例钉住三类静默失败（都是文档白纸黑字写了的坑）：
  ① **时间格式不被接受时服务端不报错，只写成空值** → 适配器必须发送前拦截 + 回显校验；
  ② **成员/部门控件 id 不存在不报错、必填也留空** → 适配器必须拒绝写而不是硬写；
  ③ ``success_count`` 与提交条数不符时的**部分成功** → 必须报错，不能返回带洞的 id 列表。
"""
import datetime
import os
import sys
import unittest
from unittest.mock import patch

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

from adapters import factory, schema  # noqa: E402
from adapters.base import (CAP_BATCH_WRITE, CAP_LINK, CAP_LINK_READ,  # noqa: E402
                           CAP_QUERY_PUSHDOWN, CAP_READ, CAP_SCHEMA_MANAGE,
                           CAP_SERVER_ROW_ID, Unsupported, capabilities_of)
from adapters.jiandaoyun import (JiandaoyunAdapter, JiandaoyunError,  # noqa: E402
                                 _flat_cell, _from_utc, _same, _to_utc,
                                 _to_value, _unwrap, _unwrap_cell)

APP_ID = "app-test-0001"
API_KEY = "key-test-0001"          # 假凭据，不是任何真实 APIKey
ENTRY = "entry-0001"


# ══════════════════════════════════════════════════════════════════
# 假传输层
# ══════════════════════════════════════════════════════════════════

def _w(name, label, wtype, **kw):
    w = {"name": name, "label": label, "type": wtype}
    w.update(kw)
    return w


WIDGETS = [
    _w("_widget_a", "名称", "text"),
    _w("_widget_b", "数量", "number"),
    _w("_widget_c", "日期", "datetime"),
    _w("_widget_d", "状态", "radiogroup"),
    _w("_widget_e", "标签", "checkboxgroup"),
    _w("_widget_f", "流水号", "sn"),            # 只读：服务端算
    _w("_widget_g", "负责人", "user"),          # 形状未验证
]

ROWS = [
    {"_id": "d1", "_widget_a": {"value": "甲"}, "_widget_b": {"value": 1},
     "_widget_c": {"value": "2020-04-14T17:00:00.000Z"}, "createTime": {"value": "x"}},
    {"_id": "d2", "_widget_a": {"value": "乙"}, "_widget_b": {"value": 2},
     "_widget_c": {"value": "2020-04-14T07:32:22.000Z"}},
    {"_id": "d3", "_widget_a": {"value": "丙"}, "_widget_b": {"value": 3}},
    {"_id": "d4", "_widget_a": {"value": "丁"}, "_widget_b": {"value": 4}},
    {"_id": "d5", "_widget_a": {"value": "戊"}, "_widget_b": {"value": 5}},
]


class _FakeTransport:
    """按 ``path`` 回应的假传输层。记录每次调用供断言。"""

    def __init__(self, forms=None, widgets=None, rows=None):
        self.forms = (forms if forms is not None
                      else [{"name": "生产计划", "app_id": APP_ID, "entry_id": ENTRY},
                            {"name": "项目", "app_id": APP_ID, "entry_id": "entry-0002"}])
        self.widgets = widgets if widgets is not None else {ENTRY: list(WIDGETS)}
        self.rows = rows if rows is not None else {ENTRY: list(ROWS)}
        self.calls = []
        self.responses = {}          # path -> dict 或 callable(body) -> dict

    # 供断言
    def calls_of(self, path):
        return [c for c in self.calls if c[0] == path]

    def call(self, path, body=None):
        self.calls.append((path, dict(body or {})))
        return _unwrap(self._call_once(path, body or {}))

    def _call_once(self, path, body):
        """真正生成响应；外层 ``call`` 统一过 ``_unwrap``，与真传输层的契约一致
        （真传输层返回的是**剥掉外层信封后**的 payload）。"""
        fn = self.responses.get(path)
        if callable(fn):
            return fn(body or {})
        if fn is not None:
            return fn
        if path == "app/entry/list":
            limit = (body or {}).get("limit", 100)
            skip = (body or {}).get("skip", 0)
            return {"forms": self.forms[skip:skip + limit]}
        if path == "app/entry/widget/list":
            eid = (body or {}).get("entry_id")
            ws = self.widgets.get(eid)
            if ws is None:
                raise JiandaoyunError("表单不存在：%r" % (eid,), code=3000, msg="form not found")
            return {"widgets": ws, "sysWidgets": [{"name": "createTime"},
                                                  {"name": "updateTime"}]}
        if path == "app/entry/data/list":
            eid = (body or {}).get("entry_id")
            rows = self.rows.get(eid, [])
            cur = (body or {}).get("data_id")
            start = 0
            if cur:
                ids = [r.get("_id") for r in rows]
                start = ids.index(cur) + 1 if cur in ids else len(rows)
            limit = (body or {}).get("limit", 100)
            return {"data_list": rows[start:start + limit]}
        if path == "app/entry/data/batch_create":
            n = len((body or {}).get("data_list") or [])
            return {"status": "success", "success_count": n,
                    "success_ids": ["new%d" % i for i in range(n)]}
        if path == "app/entry/data/update":
            return {"data": {"_id": (body or {}).get("data_id")}}
        if path == "app/entry/data/batch_delete":
            n = len((body or {}).get("data_ids") or [])
            return {"status": "success", "success_count": n}
        return {}


def _adapter(forms=None, widgets=None, rows=None):
    a = JiandaoyunAdapter(APP_ID, API_KEY, app_name="production")
    a._t = _FakeTransport(forms, widgets, rows)
    return a


# ══════════════════════════════════════════════════════════════════
# 1. 时间翻译 —— 写方向
# ══════════════════════════════════════════════════════════════════

class TimeToUtcTests(unittest.TestCase):
    """写方向：下游的北京时间墙上时间 → 简道云要的 RFC3339 UTC。"""

    def test_date_only_becomes_previous_day_16_utc(self):
        """东八区 5 月 7 日 00:00 == UTC 5 月 6 日 16:00。

        这条最容易写错：顺手写成 ``2026-05-07T00:00:00Z`` 会让记录整体偏 8 小时。
        """
        self.assertEqual(_to_utc("2026-05-07"), "2026-05-06T16:00:00.000Z")

    def test_local_datetime_is_converted(self):
        self.assertEqual(_to_utc("2026-05-07 08:30:00"), "2026-05-07T00:30:00.000Z")
        self.assertEqual(_to_utc("2026-05-07 08:30"), "2026-05-07T00:30:00.000Z")

    def test_explicit_zone_is_respected(self):
        self.assertEqual(_to_utc("2026-05-07T00:00:00Z"), "2026-05-07T00:00:00.000Z")
        self.assertEqual(_to_utc("2026-05-07T08:00:00+08:00"), "2026-05-07T00:00:00.000Z")

    def test_official_rfc3339_sample_with_9_digit_fraction_is_accepted(self):
        """官方可写格式样例 ``'2020-06-04 14:41:54.767135400+08:00'``。

        9 位小数秒会让部分 Python 版本的 ``fromisoformat`` 直接抛错 ——
        若不先归一化，就会把**官方声明支持**的格式误判成不支持。
        """
        self.assertEqual(_to_utc("2020-06-04 14:41:54.767135400+08:00"),
                         "2020-06-04T06:41:54.000Z")

    def test_objects_and_timestamps(self):
        self.assertEqual(_to_utc(datetime.date(2026, 5, 7)), "2026-05-06T16:00:00.000Z")
        self.assertEqual(_to_utc(datetime.datetime(2026, 5, 7, 8, 0)),
                         "2026-05-07T00:00:00.000Z")
        self.assertEqual(_to_utc(0), "1970-01-01T00:00:00.000Z")

    def test_unsupported_format_raises_instead_of_being_sent(self):
        """**本期最关键的一条守卫。**

        官方原文：「注:不允许的输入会转为空值传入」—— 即格式写错**不报错**，
        只是日期悄悄变成空。所以适配器必须在发送前拦下来。
        """
        for bad in ("2021/10/10 10:10:10", "2021/10/10", "10/10/2021",
                    "  不是日期  ", "Fri, 07 May 2026 08:00:00 GMT",
                    "2026年5月7日", "20260507"):
            with self.assertRaises(JiandaoyunError):
                _to_utc(bad)

    def test_omitted_seconds_are_tolerated(self):
        """``2026-05-07T08:00``（省了秒）是能解析的合法墙上时间，不拦。

        拦得太狠会误伤正常调用方；这里的界线是「**解析得出**就放行，
        解析不出就报错」—— 因为服务端对解析不出的输入是静默写空。
        """
        self.assertEqual(_to_utc("2026-05-07T08:00"), "2026-05-07T00:00:00.000Z")

    def test_invalid_calendar_date_raises(self):
        with self.assertRaises(JiandaoyunError):
            _to_utc("2026-02-30")

    def test_bool_is_not_a_time(self):
        with self.assertRaises(JiandaoyunError):
            _to_utc(True)


# ══════════════════════════════════════════════════════════════════
# 2. 时间翻译 —— 读方向
# ══════════════════════════════════════════════════════════════════

class TimeFromUtcTests(unittest.TestCase):
    """读方向：简道云的 UTC 值 → 下游的 ``YYYY-MM-DD``（东八区）。"""

    def test_converts_to_beijing_date(self):
        self.assertEqual(_from_utc("2020-04-14T07:32:22.000Z"), "2020-04-14")

    def test_cross_day_is_not_a_naive_truncation(self):
        """``[:10]`` 截断会给出**早一天**的日期 —— 必须真做时区转换。

        ``2020-04-14T17:00:00Z`` 的北京时间是 04-15 01:00；截断得到 04-14。
        跨日的记录（每天 16:00 UTC 之后写入的）会整批静默算错。
        """
        self.assertEqual(_from_utc("2020-04-14T17:00:00.000Z"), "2020-04-15")
        self.assertEqual(_from_utc("2020-04-14T16:00:00.000Z"), "2020-04-15")

    def test_millisecond_timestamp(self):
        self.assertEqual(_from_utc(0), "1970-01-01")

    def test_naive_string_is_read_as_utc(self):
        """无时区标注按 UTC 理解（官方称该字段是 UTC 标准时间）。"""
        self.assertEqual(_from_utc("2020-04-14T17:00:00"), "2020-04-15")

    def test_unparsable_is_returned_as_is(self):
        """认不出来就原样返回 —— 不丢信息，也不假装解析成功给个错日期。"""
        self.assertEqual(_from_utc("看不出来"), "看不出来")

    def test_flat_cell_none_is_empty_string(self):
        for t in ("text", "number", "datetime", "checkboxgroup"):
            self.assertEqual(_flat_cell(None, t), "")


# ══════════════════════════════════════════════════════════════════
# 3. 单元格翻译
# ══════════════════════════════════════════════════════════════════

class CellTests(unittest.TestCase):
    def test_unwrap_cell(self):
        self.assertEqual(_unwrap_cell({"value": "甲"}), "甲")
        self.assertEqual(_unwrap_cell("甲"), "甲")
        # address 这类自带 JSON 的控件：多键时不剥（保留结构）
        self.assertEqual(_unwrap_cell({"value": "深圳", "province": "粤"}),
                         {"value": "深圳", "province": "粤"})

    def test_unwrap_envelope(self):
        self.assertEqual(_unwrap({"code": 0, "data": {"forms": []}}), {"forms": []})
        self.assertEqual(_unwrap({"forms": []}), {"forms": []})
        # data/update 的裸响应 {"data": {记录}} 也应当被剥一层
        self.assertEqual(_unwrap({"data": {"_id": "d1"}}), {"_id": "d1"})

    def test_flat_cell_array_widgets(self):
        self.assertEqual(_flat_cell(["A", "B"], "checkboxgroup"), ["A", "B"])
        self.assertEqual(_flat_cell("A", "checkboxgroup"), ["A"])
        self.assertEqual(_flat_cell("", "checkboxgroup"), [])

    def test_flat_cell_keeps_number_and_text(self):
        self.assertEqual(_flat_cell(12.5, "number"), 12.5)
        self.assertEqual(_flat_cell("进行中", "radiogroup"), "进行中")

    def test_to_value_scalar_and_array(self):
        self.assertEqual(_to_value("进行中", "radiogroup", "状态"), "进行中")
        self.assertEqual(_to_value(["A", "B"], "combocheck", "标签"), ["A", "B"])
        self.assertEqual(_to_value(None, "text", "名称"), "")
        self.assertEqual(_to_value([], "checkboxgroup", "标签"), [])

    def test_to_value_bad_number_raises_not_silently_zero(self):
        with self.assertRaises(JiandaoyunError):
            _to_value("很多", "number", "数量")

    def test_prewrapped_value_passes_through(self):
        """显式逃生通道：调用方自己包好 ``{"value": …}`` 时原样放行。"""
        self.assertEqual(_to_value({"value": {"anything": 1}}, "user", "负责人"),
                         {"anything": 1})

    def test_same_is_lenient(self):
        self.assertTrue(_same(["B", "A"], ["A", "B"]))
        self.assertTrue(_same(12.5, 12.5))
        self.assertTrue(_same(" 甲 ", "甲"))
        self.assertFalse(_same("2026-05-07", ""))


# ══════════════════════════════════════════════════════════════════
# 4. 读
# ══════════════════════════════════════════════════════════════════

class ReadTests(unittest.TestCase):
    def test_list_tables_sorted(self):
        self.assertEqual(_adapter().list_tables(), ["生产计划", "项目"])

    def test_table_exists_does_not_raise(self):
        a = _adapter()
        self.assertTrue(a.table_exists("生产计划"))
        self.assertFalse(a.table_exists("没有的表"))

    def test_unknown_table_raises_with_available_names(self):
        with self.assertRaises(KeyError) as ctx:
            _adapter().get_metadata("没有的表")
        self.assertIn("生产计划", str(ctx.exception))

    def test_metadata_has_neutral_type_readonly_and_sys(self):
        cols = {c["name"]: c for c in _adapter().get_metadata("生产计划")["columns"]}
        self.assertEqual(cols["名称"]["key"], "_widget_a")
        self.assertEqual(cols["名称"]["neutral"], "text")
        self.assertEqual(cols["日期"]["neutral"], "datetime")
        self.assertTrue(cols["流水号"].get("readonly"))       # sn 是服务端算的
        self.assertTrue(cols["创建时间"].get("readonly"))      # 系统字段
        self.assertEqual(cols["创建时间"]["key"], "createTime")

    def test_list_rows_translates_labels_and_row_id(self):
        rows = _adapter().list_rows("生产计划")
        self.assertEqual(len(rows), 5)
        self.assertEqual(rows[0]["__row_id__"], "d1")
        self.assertEqual(rows[0]["名称"], "甲")
        self.assertEqual(rows[0]["数量"], 1)
        self.assertEqual(rows[0]["创建时间"], "x")            # 系统字段用中文名带出

    def test_list_rows_converts_timezone(self):
        """日期列读出来必须是**北京时间**的日期，不是 UTC 的。"""
        rows = {r["__row_id__"]: r for r in _adapter().list_rows("生产计划")}
        self.assertEqual(rows["d1"]["日期"], "2020-04-15")     # UTC 04-14T17:00 → 北京 04-15
        self.assertEqual(rows["d2"]["日期"], "2020-04-14")

    def test_paging_follows_data_id_cursor(self):
        with patch("adapters.jiandaoyun._PAGE", 2):
            rows = _adapter().list_rows("生产计划")
        self.assertEqual([r["__row_id__"] for r in rows], ["d1", "d2", "d3", "d4", "d5"])

    def test_paging_refuses_to_loop_forever(self):
        """游标不前进时必须报错 —— 宁可报错也不能死循环。"""
        a = _adapter()
        stuck = {"data_list": list(ROWS[:2])}
        a._t.responses["app/entry/data/list"] = lambda body: stuck
        with patch("adapters.jiandaoyun._PAGE", 2):
            with self.assertRaises(JiandaoyunError) as ctx:
                a.list_rows("生产计划")
        self.assertIn("翻页未前进", str(ctx.exception))

    def test_missing_data_list_raises_not_empty(self):
        """服务端没按预期给 ``data_list`` 时报错，不返回空冒充「这个表没数据」。"""
        a = _adapter()
        a._t.responses["app/entry/data/list"] = lambda body: {"unexpected": 1}
        with self.assertRaises(JiandaoyunError) as ctx:
            a.list_rows("生产计划")
        self.assertIn("未返回记录列表", str(ctx.exception))

    def test_get_row_finds_and_misses(self):
        a = _adapter()
        self.assertEqual(a.get_row("生产计划", "d3")["名称"], "丙")
        self.assertIsNone(a.get_row("生产计划", "没有这个id"))

    def test_get_row_stops_early(self):
        """单行读取要提前退出：找到就停，不该把整表翻完。"""
        with patch("adapters.jiandaoyun._PAGE", 2):
            a = _adapter()
            a.get_row("生产计划", "d1")
            self.assertEqual(len(a._t.calls_of("app/entry/data/list")), 1)


# ══════════════════════════════════════════════════════════════════
# 5. 写
# ══════════════════════════════════════════════════════════════════

class WriteTests(unittest.TestCase):
    def test_payload_shape_and_skips(self):
        a = _adapter()
        meta = a._fields("生产计划")
        p = a._to_payload("生产计划", {"名称": "甲", "数量": 3, "日期": "2026-05-07",
                                   "流水号": "SN001", "创建时间": "2026-01-01"}, meta)
        self.assertEqual(p["_widget_a"], {"value": "甲"})
        self.assertEqual(p["_widget_b"], {"value": 3})
        self.assertEqual(p["_widget_c"], {"value": "2026-05-06T16:00:00.000Z"})
        # 只读（sn）与系统字段被剔除：这是成文转换，不是静默丢弃
        self.assertNotIn("_widget_f", p)
        self.assertNotIn("createTime", p)

    def test_payload_rejects_unknown_column(self):
        a = _adapter()
        with self.assertRaises(JiandaoyunError) as ctx:
            a._to_payload("生产计划", {"没有这列": 1}, a._fields("生产计划"))
        self.assertIn("不存在", str(ctx.exception))

    def test_payload_refuses_unverifiable_widget_instead_of_writing_blank(self):
        """成员/部门控件：官方说「id 不存在不报错、必填也留空」→ 只能拒绝写。

        硬写的最可能结果是把数据悄悄写空且不报错，比一个说得清楚的
        Unsupported 危险得多。
        """
        a = _adapter()
        with self.assertRaises(Unsupported) as ctx:
            a._to_payload("生产计划", {"负责人": "张三"},
                          a._fields("生产计划"))
        self.assertIn("写入形状未经确认", str(ctx.exception))

    def test_duplicate_label_writes_are_refused(self):
        """两个同标签字段 → 必须报错，静默取第一个会把数据写到另一个字段上。"""
        dup = [{"name": "_widget_x", "label": "备注", "type": "text"},
               {"name": "_widget_y", "label": "备注", "type": "text"}]
        a = _adapter(widgets={ENTRY: dup})
        with self.assertRaises(JiandaoyunError) as ctx:
            a._to_payload("生产计划", {"备注": "x"}, a._fields("生产计划"))
        self.assertIn("同名", str(ctx.exception))

    def test_append_row_uses_the_same_batch_path(self):
        a = _adapter()
        rid = a.append_row("生产计划", {"名称": "甲"})
        self.assertEqual(rid, "new0")
        self.assertEqual(len(a._t.calls_of("app/entry/data/batch_create")), 1)

    def test_append_rows_chunks_by_100(self):
        a = _adapter()
        with patch("adapters.jiandaoyun._BATCH", 2):
            ids = a.append_rows("生产计划", [{"名称": str(i)} for i in range(5)])
        self.assertEqual(len(ids), 5)
        self.assertEqual(len(a._t.calls_of("app/entry/data/batch_create")), 3)

    def test_partial_batch_create_raises(self):
        """部分成功必须报错：服务端没说成功的是哪几条，返回带洞的列表等于谎报。"""
        a = _adapter()
        a._t.responses["app/entry/data/batch_create"] = lambda body: {
            "status": "success", "success_count": 1, "success_ids": ["new0"]}
        with self.assertRaises(JiandaoyunError) as ctx:
            a.append_rows("生产计划", [{"名称": "甲"}, {"名称": "乙"}])
        self.assertIn("部分失败", str(ctx.exception))

    def test_missing_success_ids_yields_none_not_misaligned(self):
        a = _adapter()
        a._t.responses["app/entry/data/batch_create"] = lambda body: {
            "status": "success", "success_count": len(body["data_list"]),
            "success_ids": []}
        ids = a.append_rows("生产计划", [{"名称": "甲"}, {"名称": "乙"}])
        self.assertEqual(ids, [None, None])

    def test_update_row_sends_single_record(self):
        a = _adapter()
        a.update_row("生产计划", "d1", {"数量": 9})
        calls = a._t.calls_of("app/entry/data/update")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][1]["data_id"], "d1")
        self.assertEqual(calls[0][1]["data"], {"_widget_b": {"value": 9}})

    def test_update_row_is_noop_when_nothing_writable(self):
        a = _adapter()
        a.update_row("生产计划", "d1", {"流水号": "SN", "创建时间": "x"})
        self.assertEqual(a._t.calls_of("app/entry/data/update"), [])

    def test_update_row_echo_check_catches_silently_blanked_date(self):
        """**静默丢失守卫**：服务端把不被接受的日期写成空值且不报错。

        只有回显校验能发现这件事 —— 没有它，上层会以为日期写进去了。
        """
        a = _adapter()
        a._t.responses["app/entry/data/update"] = lambda body: {
            "data": {"_id": "d1", "_widget_c": {"value": ""}}}
        with self.assertRaises(JiandaoyunError) as ctx:
            a.update_row("生产计划", "d1", {"日期": "2026-05-07"})
        self.assertIn("回显", str(ctx.exception))

    def test_update_row_echo_check_accepts_different_representation(self):
        """回显用的是 UTC ISO，发出去的是北京时间日期 —— 语义相同就不该误报。"""
        a = _adapter()
        a._t.responses["app/entry/data/update"] = lambda body: {
            "data": {"_id": "d1", "_widget_c": {"value": "2026-05-06T16:00:00.000Z"}}}
        a.update_row("生产计划", "d1", {"日期": "2026-05-07"})   # 不抛

    def test_update_row_skips_verification_when_echo_lacks_field(self):
        """回显里没有这一列 → 信息不足，不猜、不误报。"""
        a = _adapter()
        a._t.responses["app/entry/data/update"] = lambda body: {"data": {"_id": "d1"}}
        a.update_row("生产计划", "d1", {"日期": "2026-05-07"})   # 不抛

    def test_delete_rows_checks_success_count(self):
        a = _adapter()
        a.delete_rows("生产计划", ["d1", "d2"])
        a._t.responses["app/entry/data/batch_delete"] = lambda body: {
            "status": "success", "success_count": 1}
        with self.assertRaises(JiandaoyunError):
            a.delete_rows("生产计划", ["d1", "d2"])

    def test_delete_rows_noop_on_empty(self):
        a = _adapter()
        a.delete_rows("生产计划", [])
        self.assertEqual(a._t.calls_of("app/entry/data/batch_delete"), [])


# ══════════════════════════════════════════════════════════════════
# 6. 刻意不支持的部分
# ══════════════════════════════════════════════════════════════════

class UnsupportedTests(unittest.TestCase):
    def test_no_schema_management(self):
        with self.assertRaises(Unsupported) as ctx:
            _adapter().ensure_table("新表", [{"name": "名称", "type": "text"}])
        self.assertIn("预建", str(ctx.exception))

    def test_no_link_write(self):
        with self.assertRaises(Unsupported):
            _adapter().link("生产计划", "项目", "关联项目", "d1", ["d2"])

    def test_list_linked_raises_instead_of_returning_empty(self):
        """**不许 ``return []``** —— 那会让「没有关联」和「读不了」无法区分。

        本仓库吃过这个亏（SeaTable 的 list_linked 曾是空桩）。
        """
        with self.assertRaises(Unsupported) as ctx:
            _adapter().list_linked("生产计划", "d1", "")
        self.assertIn("显示文本", str(ctx.exception))

    def test_link_append_and_one_way_are_unsupported(self):
        a = _adapter()
        with self.assertRaises(Unsupported):
            a.link_append("生产计划", "项目", "关联项目", "d1", ["d2"])
        with self.assertRaises(Unsupported):
            a.link_one_way("生产计划", "项目", "关联项目", "d1", ["d2"])

    def test_link_columns_is_a_list_not_an_action(self):
        """``link_columns`` 是诊断清单，空列表是真实答案（本表确实没有关联控件）。"""
        self.assertEqual(_adapter().link_columns("生产计划"), [])


# ══════════════════════════════════════════════════════════════════
# 7. 能力声明
# ══════════════════════════════════════════════════════════════════

class CapabilityTests(unittest.TestCase):
    def test_declares_only_what_is_backed(self):
        caps = capabilities_of(_adapter())
        for cap in (CAP_READ, CAP_BATCH_WRITE, CAP_SERVER_ROW_ID):
            self.assertIn(cap, caps)

    def test_does_not_declare_unsupported_capabilities(self):
        caps = capabilities_of(_adapter())
        for cap in (CAP_LINK, CAP_LINK_READ, CAP_SCHEMA_MANAGE, CAP_QUERY_PUSHDOWN,
                    "idempotent", "optimistic_lock"):
            self.assertNotIn(cap, caps, "声明了 %s 却没有兑现它" % cap)

    def test_capabilities_are_readable_without_connecting(self):
        """能力声明必须能在建连之前读 —— 否则「据此决定要不要建连」根本不成立。"""
        a = JiandaoyunAdapter(APP_ID, API_KEY)      # 没有传输层、没认证
        self.assertIn(CAP_READ, a.capabilities())

    def test_describe_has_no_credentials(self):
        d = _adapter().describe()
        self.assertIn("jiandaoyun", d)
        self.assertNotIn(API_KEY, d)


# ══════════════════════════════════════════════════════════════════
# 8. 可插拔：注册完不改工厂一行
# ══════════════════════════════════════════════════════════════════

class RegistrationTests(unittest.TestCase):
    def test_registered_and_listed(self):
        self.assertIn("jiandaoyun", factory.list_backends())
        self.assertEqual(factory.BACKENDS["jiandaoyun"]["label"], "简道云")

    def _cfg(self):
        return {"backend": "jiandaoyun",
                "jiandaoyun": {"api_key": API_KEY, "default_base": "production",
                               "bases": {"production": {"app_id": APP_ID}}}}

    def test_adapter_is_built_by_the_factory(self):
        a = factory.get_adapter(self._cfg(), base_name="production", strict=True)
        self.assertIsInstance(a, JiandaoyunAdapter)
        self.assertEqual((a.app_id, a.api_key, a.app_name),
                         (APP_ID, API_KEY, "production"))

    def test_api_key_alias_is_normalized(self):
        cfg = {"backend": "jiandaoyun",
               "jiandaoyun": {"bases": {"production": {"appId": APP_ID}}}}
        base = factory.get_base_config(cfg, "production")
        self.assertEqual(base["app_id"], APP_ID)

    def test_missing_app_id_fails_closed(self):
        cfg = {"backend": "jiandaoyun",
               "jiandaoyun": {"api_key": API_KEY, "bases": {"production": {}}}}
        with self.assertRaises(RuntimeError) as ctx:
            factory.get_adapter(cfg, base_name="production", strict=True)
        self.assertIn("app_id", str(ctx.exception))

    def test_deploy_hook_is_declared(self):
        hook = factory.backend_deploy_hooks("jiandaoyun")
        self.assertIn("verify", hook)
        self.assertEqual(hook["verify"][0], "verify_jiandaoyun.py")
        # init_sync 刻意未落地 → 不声明（部署报告会写明「未声明」，好过静默跳过）
        self.assertNotIn("init_sync", hook)


# ══════════════════════════════════════════════════════════════════
# 9. schema 类型映射
# ══════════════════════════════════════════════════════════════════

class SchemaTests(unittest.TestCase):
    def test_backend_type_maps_neutral_names(self):
        self.assertEqual(schema.backend_type("jiandaoyun", "text"), "text")
        self.assertEqual(schema.backend_type("jiandaoyun", "longtext"), "textarea")
        self.assertEqual(schema.backend_type("jiandaoyun", "multiselect"), "checkboxgroup")
        self.assertEqual(schema.backend_type("jiandaoyun", "attachment"), "upload")

    def test_bool_is_the_one_semantic_downgrade(self):
        """简道云没有布尔控件，只能降级成单选按钮组（值域从 true/false 变成字符串）。

        这条单独立一个用例：它是本映射表里**唯一换了语义**的一处，
        改动它必须是有意识的决定，不能顺手改掉。
        """
        self.assertEqual(schema.backend_type("jiandaoyun", "bool"), "radiogroup")

    def test_every_neutral_type_has_a_mapping(self):
        """中立类型加了新的却没补映射 → 换后端时会静默出错，这里直接拦住。"""
        table = schema.BACKEND_TYPES["jiandaoyun"]
        missing = [t for t in schema.NEUTRAL_TYPES if t not in table]
        self.assertEqual(missing, [], "中立类型缺少简道云映射：%s" % missing)

    def test_native_names_pass_through(self):
        self.assertEqual(schema.backend_type("jiandaoyun", "textarea"), "textarea")


# ══════════════════════════════════════════════════════════════════
# 10. 验证脚本
# ══════════════════════════════════════════════════════════════════

class VerifyScriptTests(unittest.TestCase):
    def test_script_exists(self):
        self.assertTrue(os.path.exists(
            os.path.join(HERE, "tools", "verify_jiandaoyun.py")))

    def test_unconfigured_run_exits_zero_and_says_skipped(self):
        """没配置凭据是**正常状态**，不是失败；但必须说清楚「跳过了」。

        这条用例同时守住一件事：脚本必须**不读也不写**真实生产配置
        （这里把 load_config 换成空配置，它就该安静退出）。
        """
        import io
        import tools.verify_jiandaoyun as V
        buf = io.StringIO()
        with patch("adapters.factory.load_config", return_value={"backend": "local"}), \
                patch("sys.argv", ["verify_jiandaoyun.py", "--read-only"]), \
                patch("sys.stdout", buf):
            rc = V.main()
        self.assertEqual(rc, 0)
        self.assertIn("未配置，跳过", buf.getvalue())

    def test_write_test_without_table_is_refused(self):
        """简道云没有临时表可用 → 不给 --table 就绝不猜一个表来写。"""
        import io
        import tools.verify_jiandaoyun as V
        with patch("sys.argv", ["verify_jiandaoyun.py", "--write-test"]), \
                patch("sys.stdout", io.StringIO()):
            rc = V.main()
        self.assertEqual(rc, 2)


if __name__ == "__main__":
    unittest.main()
