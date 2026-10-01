# -*- coding: utf-8 -*-
"""禅道适配器契约测试（第 3 期）。

**全部离线**：用一个「按 (method, path) 回应」的假传输层替代 ``_HttpTransport``，
不碰网络、不碰真实禅道。

与 ``tests/test_feishu_contract.py`` / ``test_jiandaoyun_contract.py`` 的差别：
  · 飞书那份钉的是**实测确认过的协议事实**；
  · 简道云那份钉的是**官方文档的推断被正确实现**（无实例可测）；
  · 这一份钉的是**实测确认过的协议事实**，而且实测过的东西比飞书那份更凶 ——
    禅道有三处「HTTP 200 但其实是失败/是空/是 HTML」，
    只看 HTTP 状态码的实现**每一条都能静默拿到错数据**。

重点用用例钉住的守卫（全部有实测依据，见 ``adapters/zentao.py`` 模块 docstring）：
  ① 资源不存在 → **HTTP 200** + ``status: "fail"`` → 必须判失败（看 body 不看状态码）；
  ② 部分端点列表 GET → **HTTP 200 + 0 字节** → 必须报错，不能返回 ``[]`` 冒充「没有」；
  ③ ``/productplans`` 之类 → **HTTP 200 + 一整页 HTML** → 必须报错，不能当 JSON 硬解析；
  ④ ``/bugs`` ``/testcases`` ``/testtasks`` 的响应里混着一堆别的数组，
     业务数组键名各不相同（``bugs`` / ``cases`` / ``tasks``）→ 必须按精确键名取；
  ⑤ 实测翻页参数疑似不生效 → 同一页读第二遍必须报错，不能静默返回第一页；
  ⑥ 实体固定 → 建表/关联接口必须如实抛 ``Unsupported``。
"""
import json
import os
import sys
import unittest
from unittest.mock import patch

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

from adapters import factory, schema  # noqa: E402
from adapters.base import (CAP_BATCH_WRITE, CAP_DELETE, CAP_IDEMPOTENT,  # noqa: E402
                           CAP_LINK, CAP_LINK_READ, CAP_OPTIMISTIC_LOCK,
                           CAP_QUERY_PUSHDOWN, CAP_READ, CAP_SCHEMA_MANAGE,
                           CAP_SERVER_ROW_ID, CAP_UPDATE, CAP_WRITE,
                           Unsupported, capabilities_of)
from adapters.zentao import (_ENUMS, _MAX_PAGES, _PAGE, ALIASES,  # noqa: E402
                             ENTITY_SPECS, ZentaoAdapter, ZentaoError,
                             _enum_code, _enum_label, entity_tables,
                             neutral_type)

BASE = "http://zentao.test/api.php/v2"
TOKEN = "tk-test-0001"          # 假令牌，不是任何真实禅道 token


# ══════════════════════════════════════════════════════════════════
# 假传输层
# ══════════════════════════════════════════════════════════════════

class _FakeTransport:
    """按 ``(method, path)`` 回应的假传输层。

    ``routes`` 的值可以是：
      · dict        → 序列化成 JSON，HTTP 200
      · ``(int, str)`` → 原样返回（用来造 401 / 空响应体 / HTML）
      · str         → HTTP 200 + 该字符串（原样，**不**序列化）
      · callable(query, body) → 递归解析其结果（用来模拟真翻页）
      · Exception 实例 → 直接抛

    没有登记的路由会抛 ``AssertionError`` —— 这是刻意的：**测试没预期的请求
    本身就是 bug**（例如「只读列应该被剔除」的用例里，一旦发请求就该炸）。
    """

    def __init__(self, routes=None):
        self.routes = dict(routes or {})
        self.calls = []          # [(method, path, query, body)]
        self.token = TOKEN
        self.closed = False

    def request(self, method, path, query=None, body=None):
        self.calls.append((method, path, dict(query or {}), body))
        fn = self.routes.get((method, path))
        if fn is None:
            raise AssertionError("测试未登记 %s %s 的响应（实际发生了这个请求）"
                                 % (method, path))
        if isinstance(fn, Exception):
            raise fn
        if callable(fn):
            fn = fn(dict(query or {}), body)
        if isinstance(fn, tuple):
            return fn
        if isinstance(fn, str):
            return 200, fn
        return 200, json.dumps(fn, ensure_ascii=False)

    # 供断言
    def paths(self):
        return [c[1] for c in self.calls]

    def body_of(self, method, path):
        for m, p, _q, b in self.calls:
            if m == method and p == path:
                return b
        raise AssertionError("没有发生过 %s %s" % (method, path))

    def close(self):
        self.closed = True


class _ExplodingTransport:
    """任何请求都炸。用来证明「能力声明 / 元数据 / 逻辑表名」不联网。"""

    def request(self, *a, **kw):
        raise AssertionError("这一步不该联网（声明式能力/本地映射表）")

    def close(self):
        pass


def _ok(**kw):
    """成功信封：``{"status":"success", <资源复数>: [...], "pager": {...}}``。"""
    d = {"status": "success"}
    d.update(kw)
    return d


def _fail(msg="Project does not exist."):
    """失败信封 —— 注意它配的是 **HTTP 200**（实测）。"""
    return {"status": "fail", "message": msg}


def _pager(rec_total, page, size, page_total=None):
    return {"offset": (page - 1) * size, "recTotal": rec_total, "recPerPage": size,
            "pageCookie": None, "pageTotal": page_total,
            "pageID": page, "moduleName": "x", "methodName": "browse", "params": None}


def _paged(list_key, rows, size=100):
    """模拟**真的**在服务端翻页（用来证明正常路径能读完多页）。"""
    def fn(query, _body):
        s = int(query.get("recPerPage") or size) or size
        p = int(query.get("pageID") or 1)
        start = (p - 1) * s
        total = len(rows)
        page_total = max(1, (total + s - 1) // s)
        return _ok(**{list_key: rows[start:start + s],
                      "pager": _pager(total, p, s, page_total)})
    return fn


def _stuck(list_key, rows, rec_total=1000, page_total=10):
    """模拟**实测到的**「翻页参数不生效」：永远回第一页，但 pager 说还有更多。"""
    def fn(_query, _body):
        return _ok(**{list_key: rows,
                      "pager": _pager(rec_total, 1, 100, page_total)})
    return fn


def _adapter(routes=None):
    """构造带假传输层的适配器。``routes`` 可以直接给一个传输层对象。"""
    t = routes if hasattr(routes, "request") else _FakeTransport(routes)
    return ZentaoAdapter(base=BASE, token=TOKEN, site="production", transport=t)


# ══════════════════════════════════════════════════════════════════
# 样例数据（字段名取自 OpenAPI 规范的响应 schema）
# ══════════════════════════════════════════════════════════════════

PROJECT = {
    "id": 3, "name": "甲项目", "model": "scrum", "begin": "2026-01-05",
    "end": "2026-06-30", "parent": 1, "PM": "zhangsan", "code": "P3",
    "status": "doing", "realBegan": "2026-01-06", "realEnd": "", "progress": "40",
}
USER_ROW = {"id": 1, "account": "admin", "realname": "管理员", "visions": "rnd",
            "role": "admin", "last": "2026-09-30 12:00:00", "deleted": "0"}
BUG_ROW = {"id": 7, "title": "真 Bug", "status": "active", "productID": 1}
CASE_ROW = {"id": 5, "title": "真用例", "type": "feature", "productID": 1}
TESTTASK_ROW = {"id": 4, "name": "测试单甲", "type": "integrate", "status": "wait"}
TASK_ROW = {"id": 15, "name": "工序A", "executionID": 2, "status": "wait",
            "pri": 3, "assignedTo": "lisi"}


# ══════════════════════════════════════════════════════════════════
# 1. 三处「HTTP 200 但其实是失败」——本期最重要的守卫
# ══════════════════════════════════════════════════════════════════

class Http200ButFailureTests(unittest.TestCase):
    """实测：禅道失败时**不一定改 HTTP 状态码**。只看状态码 = 静默拿到错数据。"""

    def test_status_fail_with_http_200_is_a_failure(self):
        """资源不存在 = HTTP 200 + ``status: "fail"`` —— 必须判失败。

        这是本适配器最重要的守卫。若退回成「只看 HTTP 状态码」，
        ``_call`` 会**把失败当成功返回**，上层拿到一个没有业务数组的 dict
        继续往下跑 —— 典型的静默错。
        """
        t = _FakeTransport({("GET", "/projects/999"): _fail()})
        a = _adapter(t)
        with self.assertRaises(ZentaoError) as c:
            a._call("GET", "/projects/999")
        e = c.exception
        self.assertIn("调用失败", str(e))          # 不是「返回的不是 JSON」那类
        self.assertEqual(e.zentao_status, "fail")
        self.assertEqual(e.http_status, 200)       # ← 关键：HTTP 是 200
        self.assertIn("does not exist", str(e))    # 真正的原因在 message 里

    def test_update_on_missing_row_raises(self):
        """走公开路径再确认一次：改一行不存在的记录必须报错，**不能静默成功**。"""
        t = _FakeTransport({("PUT", "/projects/999"): _fail("Project does not exist.")})
        a = _adapter(t)
        with self.assertRaises(ZentaoError):
            a.update_row("项目", "999", {"名称": "新名字"})

    def test_delete_on_missing_row_raises(self):
        t = _FakeTransport({("DELETE", "/projects/9"): _fail("Project does not exist.")})
        a = _adapter(t)
        with self.assertRaises(ZentaoError):
            a.delete_rows("项目", ["9"])

    def test_message_field_is_reported(self):
        """``message`` 是真正的原因 —— 而 OpenAPI 规范里**根本没有这个字段**。

        只照规范实现的话，错误信息只剩 ``status: fail``，排查时等于没线索。
        """
        t = _FakeTransport({("GET", "/projects/1"): _fail("Not allowed to visit project.")})
        a = _adapter(t)
        with self.assertRaises(ZentaoError) as c:
            a._call("GET", "/projects/1")
        self.assertIn("Not allowed to visit project.", str(c.exception))


class EmptyBodyAndHtmlTests(unittest.TestCase):
    """实测：部分端点回 0 字节，部分端点回整页 HTML。两者都**不是**空数据。"""

    def test_empty_body_raises_not_empty_list(self):
        """HTTP 200 + 0 字节 → 报错，**不能**返回 ``[]`` 冒充「这张表是空的」。

        判据是错误文案：若退回成「先 json.loads 再兜底」，空串会走 JSONDecodeError
        分支，报的是「返回的不是 JSON」—— 那就把「端点不可用」说成了「格式不对」，
        排查方向直接偏掉。
        """
        t = _FakeTransport({("GET", "/projects"): (200, "")})
        a = _adapter(t)
        with self.assertRaises(ZentaoError) as c:
            a.list_rows("项目")
        self.assertIn("空响应体", str(c.exception))

    def test_whitespace_only_body_also_raises(self):
        t = _FakeTransport({("GET", "/projects"): (200, "   \n  ")})
        with self.assertRaises(ZentaoError) as c:
            _adapter(t).list_rows("项目")
        self.assertIn("空响应体", str(c.exception))

    def test_html_body_raises_with_html_wording(self):
        """实测 ``/productplans`` 的 GET 回落到 web 路由，返回一整页 HTML。"""
        html = "<!DOCTYPE html><html class=\"theme-default\"><head></head></html>"
        t = _FakeTransport({("GET", "/projects"): (200, html)})
        with self.assertRaises(ZentaoError) as c:
            _adapter(t).list_rows("项目")
        self.assertIn("HTML", str(c.exception))

    def test_non_json_text_raises(self):
        t = _FakeTransport({("GET", "/projects"): (200, "502 Bad Gateway")})
        with self.assertRaises(ZentaoError) as c:
            _adapter(t).list_rows("项目")
        self.assertIn("不是 JSON", str(c.exception))

    def test_json_array_body_raises(self):
        """顶层是数组而不是对象 —— 也判错，不硬着头皮往下走。"""
        t = _FakeTransport({("GET", "/projects"): (200, "[1,2,3]")})
        with self.assertRaises(ZentaoError) as c:
            _adapter(t).list_rows("项目")
        self.assertIn("不是对象", str(c.exception))

    def test_401_is_reported_as_auth_failure(self):
        t = _FakeTransport({("GET", "/projects"):
                            (401, '{"status":"fail","message":"Not allowed"}')})
        with self.assertRaises(ZentaoError) as c:
            _adapter(t).list_rows("项目")
        self.assertIn("认证失败", str(c.exception))
        self.assertEqual(c.exception.http_status, 401)


# ══════════════════════════════════════════════════════════════════
# 2. 精确 list_key —— 响应里混着一堆别的数组
# ══════════════════════════════════════════════════════════════════

class ListKeyTests(unittest.TestCase):
    """实测：``/bugs`` ``/testcases`` ``/testtasks`` 的响应里塞满了表单初始化
    数组，业务数组键名**各不相同**（``bugs`` / ``cases`` / ``tasks``）。
    按「取第一个数组」解析必然拿错 —— 而且拿错的还是**别的实体**的数据。
    """

    def test_bugs_uses_the_bugs_array_not_other_arrays(self):
        t = _FakeTransport({("GET", "/bugs"): _ok(
            products=[{"id": 1, "name": "不该被读到的产品"}],
            executions=[{"id": 2, "name": "不该被读到的执行"}],
            tasks=[{"id": 99, "title": "不该被读到的任务"}],
            bugs=[BUG_ROW], pager=_pager(1, 1, 100, 1))})
        rows = _adapter(t).list_rows("Bug")
        self.assertEqual([r["__row_id__"] for r in rows], [7])
        self.assertEqual(rows[0]["标题"], "真 Bug")

    def test_testcases_uses_cases_key(self):
        t = _FakeTransport({("GET", "/testcases"): _ok(
            products=[{"id": 1}], modules=[{"id": 2}], iscenes=[{"id": 3}],
            stories=[{"id": 4}], cases=[CASE_ROW], pager=_pager(1, 1, 100, 1))})
        rows = _adapter(t).list_rows("测试用例")
        self.assertEqual([r["__row_id__"] for r in rows], [5])
        self.assertEqual(rows[0]["标题"], "真用例")
        self.assertEqual(rows[0]["用例类型"], "功能测试")     # 枚举已翻成中文

    def test_testtasks_uses_tasks_key(self):
        """⚠️ 测试单的数组键就叫 ``tasks`` —— 与「任务」实体同名。

        写得糙一点（拿第一个数组）的实现会在这里把测试单读成别的东西。
        """
        t = _FakeTransport({("GET", "/testtasks"): _ok(
            products=[{"id": 1}], multipleSprints=[{"id": 2}],
            tasks=[TESTTASK_ROW], pager=_pager(1, 1, 100, 1))})
        rows = _adapter(t).list_rows("测试单")
        self.assertEqual([r["__row_id__"] for r in rows], [4])
        self.assertEqual(rows[0]["名称"], "测试单甲")
        self.assertEqual(rows[0]["类型"], "集成测试")
        self.assertEqual(rows[0]["状态"], "未开始")

    def test_missing_declared_key_raises_with_actual_keys(self):
        """约定的键名不在响应里 → 报错并列出**实际有哪些键**，不猜。"""
        t = _FakeTransport({("GET", "/testcases"): _ok(
            products=[{"id": 1}], modules=[{"id": 2}], pager=_pager(0, 1, 100, 1))})
        with self.assertRaises(ZentaoError) as c:
            _adapter(t).list_rows("测试用例")
        msg = str(c.exception)
        self.assertIn("cases", msg)          # 声明要的键名
        self.assertIn("products", msg)       # 实际有的键名


# ══════════════════════════════════════════════════════════════════
# 3. 翻页：三重保护
# ══════════════════════════════════════════════════════════════════

class PagingTests(unittest.TestCase):

    def test_multi_page_is_read_completely(self):
        rows = [{"id": i, "name": "P%d" % i} for i in range(250)]
        t = _FakeTransport({("GET", "/projects"): _paged("projects", rows)})
        got = _adapter(t).list_rows("项目")
        self.assertEqual(len(got), 250)
        self.assertEqual([r["__row_id__"] for r in got], list(range(250)))
        self.assertEqual(len(t.paths()), 3)          # 100 + 100 + 50

    def test_stuck_paging_raises_instead_of_returning_first_page(self):
        """实测：``limit=1&page=2`` 与 ``recPerPage=1&pageID=2`` 都回 ``pageID=1``。

        若不在判重处拦住，就是「读第一页 → 再读一次第一页 → 服务端说有更多 →
        继续读第一页…」，最后**成功返回一份只有第一页的数据**，上层看不出少了。
        """
        t = _FakeTransport({("GET", "/projects"):
                            _stuck("projects", [PROJECT], rec_total=1000)})
        with self.assertRaises(ZentaoError) as c:
            _adapter(t).list_rows("项目")
        self.assertIn("翻页参数没有生效", str(c.exception))

    def test_stuck_paging_does_not_yield_duplicated_rows_first(self):
        """判重必须在 **yield 之前**：否则同一页被产出两遍，
        调用方若「捕获异常后继续用已拿到的行」，拿到的是**重复**数据 ——
        条数看着对，内容却是重播的第一页，比少数据更难发现。
        """
        from adapters.zentao import ENTITY_SPECS
        t = _FakeTransport({("GET", "/projects"):
                            _stuck("projects", [PROJECT], rec_total=1000)})
        a = _adapter(t)
        seen = []
        with self.assertRaises(ZentaoError):
            for raw in a._iter_rows("项目", ENTITY_SPECS["项目"]):
                seen.append(raw["id"])
        self.assertEqual(seen, [3], "同一页被产出了 %d 次" % len(seen))

    def test_max_pages_guard(self):
        """服务端永远给「看起来还有下一页」且每页 id 都不同 → 必须中止。"""
        def fn(query, _body):
            p = int(query.get("pageID") or 1)
            return _ok(projects=[{"id": "%d-%d" % (p, i)} for i in range(_PAGE)],
                       pager={})
        t = _FakeTransport({("GET", "/projects"): fn})
        a = _adapter(t)
        from adapters.zentao import ENTITY_SPECS
        n = 0
        with self.assertRaises(ZentaoError) as c:
            for _ in a._iter_rows("项目", ENTITY_SPECS["项目"]):
                n += 1
        self.assertIn("翻页超过", str(c.exception))
        self.assertEqual(n, _MAX_PAGES * _PAGE)

    def test_empty_page_stops_cleanly(self):
        t = _FakeTransport({("GET", "/projects"):
                            _ok(projects=[], pager=_pager(0, 1, 100, 1))})
        self.assertEqual(_adapter(t).list_rows("项目"), [])


# ══════════════════════════════════════════════════════════════════
# 4. 不可列表的实体 —— 读不了就是读不了
# ══════════════════════════════════════════════════════════════════

class NotListableTests(unittest.TestCase):
    """实测 ``/tasks`` ``/stories`` ``/requirements`` ``/epics`` 的列表 GET
    回 **HTTP 200 + 0 字节**。适配器把这两个实体标成 ``listable=False``。
    """

    def test_listing_tasks_raises_unsupported_with_reason(self):
        t = _FakeTransport({})          # 任何请求都会炸 —— 证明它根本没发请求
        a = _adapter(t)
        with self.assertRaises(Unsupported) as c:
            a.list_rows("任务")
        self.assertIn("空响应体", str(c.exception))      # 原因是成文的，不是「未知」
        self.assertEqual(t.calls, [])

    def test_listing_stories_raises_unsupported(self):
        t = _FakeTransport({})
        with self.assertRaises(Unsupported):
            _adapter(t).list_rows("需求")

    def test_get_row_uses_detail_endpoint_for_not_listable(self):
        """不可列表 ≠ 读不了：按 id 取详情仍然可用。"""
        t = _FakeTransport({("GET", "/tasks/15"):
                            _ok(task=TASK_ROW)})
        r = _adapter(t).get_row("任务", "15")
        self.assertIsNotNone(r)
        self.assertEqual(r["__row_id__"], 15)
        self.assertEqual(r["名称"], "工序A")
        self.assertEqual(r["所属执行"], 2)

    def test_get_row_missing_returns_none_not_raise(self):
        """契约是「找不到返回 None」——所以这里**必须**不能复用 ``_call``
        （``_call`` 会把 ``status: fail`` 抛成异常）。"""
        t = _FakeTransport({("GET", "/tasks/999"): _fail("Task does not exist.")})
        self.assertIsNone(_adapter(t).get_row("任务", "999"))

    def test_get_row_other_failure_raises(self):
        t = _FakeTransport({("GET", "/tasks/999"): _fail("No permission to visit task.")})
        with self.assertRaises(ZentaoError) as c:
            _adapter(t).get_row("任务", "999")
        self.assertIn("No permission", str(c.exception))


# ══════════════════════════════════════════════════════════════════
# 5. 写路径：三类成文转换
# ══════════════════════════════════════════════════════════════════

class PayloadTests(unittest.TestCase):

    def test_readonly_columns_are_dropped_so_read_modify_write_works(self):
        """最常见的使用方式是「读回来的整行改一个字段再写回去」，
        整行里天然带着 ``状态``/``进度`` 这些只读列。为它们报错会让这条路
        彻底走不通，所以**成文剔除**（列是枚举出来的，不是「拿不准就丢」）。
        """
        t = _FakeTransport({("PUT", "/projects/3"): _ok()})
        a = _adapter(t)
        a.update_row("项目", "3", {"名称": "新名字", "状态": "closed", "进度": "50"})
        self.assertEqual(t.body_of("PUT", "/projects/3"), {"name": "新名字"})

    def test_all_readonly_update_sends_no_request(self):
        t = _FakeTransport({})
        a = _adapter(t)
        a.update_row("项目", "3", {"状态": "closed", "进度": "50"})
        self.assertEqual(t.calls, [])

    def test_row_id_marker_is_ignored(self):
        t = _FakeTransport({("PUT", "/projects/3"): _ok()})
        a = _adapter(t)
        a.update_row("项目", "3", {"__row_id__": 3, "名称": "新名字"})
        self.assertEqual(t.body_of("PUT", "/projects/3"), {"name": "新名字"})

    def test_unknown_column_raises_and_lists_available(self):
        """列名写错是最常见的错 —— 在客户端拦下并列出可用列，不丢给服务端。"""
        t = _FakeTransport({})
        with self.assertRaises(ZentaoError) as c:
            _adapter(t).update_row("项目", "3", {"工期": 5})
        msg = str(c.exception)
        self.assertIn("工期", msg)
        self.assertIn("名称", msg)          # 可用列里有「名称」
        self.assertEqual(t.calls, [])

    def test_empty_values_are_not_sent(self):
        """空值不发送 —— 不覆盖服务端既有值。

        禅道对空值的处理**未验证**：发过去有可能写成 ``0000-00-00``，所以不赌。
        """
        t = _FakeTransport({("PUT", "/projects/3"): _ok()})
        _adapter(t).update_row("项目", "3",
                               {"名称": "甲", "负责人": "", "计划开始": None,
                                "关联产品": []})
        self.assertEqual(t.body_of("PUT", "/projects/3"), {"name": "甲"})

    def test_enum_label_is_converted_to_code_on_write(self):
        t = _FakeTransport({("PUT", "/projects/3"): _ok()})
        _adapter(t).update_row("项目", "3", {"管理方式": "敏捷"})
        self.assertEqual(t.body_of("PUT", "/projects/3"), {"model": "scrum"})

    def test_unknown_enum_label_passes_through(self):
        """认不出的标签**原样放行**，让服务端去报它自己的错 —— 不猜一个代码。"""
        t = _FakeTransport({("PUT", "/projects/3"): _ok()})
        _adapter(t).update_row("项目", "3", {"管理方式": "量子敏捷"})
        self.assertEqual(t.body_of("PUT", "/projects/3"), {"model": "量子敏捷"})

    def test_number_coercion_and_refusal(self):
        t = _FakeTransport({("PUT", "/executions/2"): _ok()})
        a = _adapter(t)
        a.update_row("执行", "2", {"可用工作日": "12"})
        self.assertEqual(t.body_of("PUT", "/executions/2"), {"days": 12.0})

        def boom():
            a.update_row("执行", "2", {"可用工作日": "十二天"})
        with self.assertRaises(ZentaoError) as c:
            boom()
        self.assertIn("数值列", str(c.exception))

    def test_array_column(self):
        t = _FakeTransport({("PUT", "/projects/3"): _ok()})
        a = _adapter(t)
        a.update_row("项目", "3", {"关联产品": ["1", 2]})
        self.assertEqual(t.body_of("PUT", "/projects/3"), {"products": ["1", 2]})
        # 空数组 → 不发送（与「清空」区分不开，故不赌）
        a.update_row("项目", "3", {"关联产品": []})
        self.assertEqual(len([c for c in t.calls if c[0] == "PUT"]), 1)

    def test_create_requires_required_columns(self):
        t = _FakeTransport({})
        with self.assertRaises(ZentaoError) as c:
            _adapter(t).append_row("项目", {"管理方式": "scrum"})
        self.assertIn("名称", str(c.exception))
        self.assertEqual(t.calls, [])

    def test_create_treats_empty_required_value_as_missing(self):
        """传了 ``{"名称": ""}`` 等于没传 —— 必填校验按**实际会发出的字段**判。"""
        t = _FakeTransport({})
        with self.assertRaises(ZentaoError) as c:
            _adapter(t).append_row("项目", {"名称": ""})
        self.assertIn("名称", str(c.exception))
        self.assertEqual(t.calls, [])

    def test_create_returns_server_id_as_string(self):
        """实测 ``POST`` 回的 ``id`` 是**数字**（``{"id":1,...}``），
        而列表 GET 回的 ``id`` 是**字符串** —— 适配器统一 str() 化，
        否则 ``get_row``/``update_row`` 按字符串比对就找不到刚建的行。
        """
        t = _FakeTransport({("POST", "/projects"): _ok(id=88, message="保存成功")})
        rid = _adapter(t).append_row("项目", {"名称": "新项目", "计划完成": "2026-12-31"})
        self.assertEqual(rid, "88")
        self.assertIsInstance(rid, str)
        self.assertEqual(t.body_of("POST", "/projects"),
                         {"name": "新项目", "end": "2026-12-31"})

    def test_create_without_id_in_response_raises(self):
        """返回成功但没有 id → 报错。返回 None 会让「建好了但找不回来」。"""
        t = _FakeTransport({("POST", "/projects"):
                            _ok(message="保存成功")})     # 没有 id
        with self.assertRaises(ZentaoError) as c:
            _adapter(t).append_row("项目", {"名称": "新项目", "计划完成": "2026-12-31"})
        self.assertIn("没有 id", str(c.exception))

    def test_append_rows_is_not_declared_atomic(self):
        """禅道没有批量接口 → ``append_rows`` 逐条 POST，且**不**声明 batch_write。"""
        row = {"名称": "A", "计划完成": "2026-12-31"}
        t = _FakeTransport({("POST", "/projects"): _ok(id=1)})
        a = _adapter(t)
        self.assertEqual(len(a.append_rows("项目", [row, dict(row, 名称="B")])), 2)
        self.assertEqual(len([c for c in t.calls if c[0] == "POST"]), 2)
        self.assertNotIn(CAP_BATCH_WRITE, a.capabilities())

    def test_update_requires_row_id(self):
        t = _FakeTransport({})
        with self.assertRaises(ZentaoError) as c:
            _adapter(t).update_row("项目", "", {"名称": "甲"})
        self.assertIn("row_id", str(c.exception))
        self.assertEqual(t.calls, [])


class MeasuredWriteFactsTests(unittest.TestCase):
    """实测确认过的写路径事实（``probes/zentao_write_probe.py``，2026-10-01）。

    这些用例钉的是**服务端真实行为**，而不是「我们打算怎么处理」——
    每一条都能在模块 docstring 的第 12~20 条里找到出处。
    """

    def test_third_envelope_result_fail_with_dict_message(self):
        """⚠️ **第三种信封**：字段级校验错误是
        ``{"result":"fail","message":{"end":["『计划完成』不能为空。"]}}`` ——
        **连 ``status`` 键都没有**。

        判据「``status`` 不是 success 就报错」天然覆盖了它（``None != "success"``），
        但文案必须自己格式化，否则只能打出一句 ``status=None`` 加一个裸 dict ——
        而那个 dict 里写着「哪个字段错了」。
        """
        t = _FakeTransport({("POST", "/projects"): (200, json.dumps(
            {"result": "fail", "message": {"end": ["『计划完成』不能为空。"]}},
            ensure_ascii=False))})
        with self.assertRaises(ZentaoError) as c:
            _adapter(t).append_row("项目", {"名称": "甲", "计划完成": "2026-12-31"})
        e = c.exception
        msg = str(e)
        self.assertIn("计划完成", msg)              # 字段名出来了
        self.assertIn("不能为空", msg)              # 原因出来了
        self.assertNotIn("status=None", msg)        # 不再是没法读的裸 dict
        self.assertEqual(e.zentao_status, "fail")   # result 被当成 status 上报
        self.assertEqual(e.http_status, 200)

    def test_format_failure_handles_all_shapes(self):
        """三种 ``message`` 形态都要能读：字典 / 字符串 / 干脆没有。"""
        a = _adapter(_FakeTransport({}))
        self.assertEqual(
            a._format_failure({"result": "fail",
                               "message": {"begin": ["『计划开始』不能为空。"]}}),
            "字段校验失败 —— begin：『计划开始』不能为空。")
        self.assertEqual(a._format_failure({"status": "fail",
                                            "message": "Project does not exist."}),
                         "Project does not exist.")
        self.assertIn("原始键", a._format_failure({"status": "fail"}))

    def test_create_project_minimum_is_name_plus_end(self):
        """实测：只给 ``name`` → 服务端报「『计划完成』不能为空」；
        ``name + end`` 就成功（``model`` 默认 scrum、``begin`` 自动填当天）。

        所以「项目」的必填取**实测最小集**，而不是规范里那份更严的
        （规范还要 model/begin/workflowGroup，其中 workflowGroup 规范自己都写着
        「付费版功能，开源版可以不填」）。
        """
        spec = ENTITY_SPECS["项目"]
        self.assertEqual(spec["required"], ("名称", "计划完成"))
        self.assertEqual(spec["required_source"], "measured")
        self.assertIn("workflowGroup", spec["spec_required"])

        t = _FakeTransport({("POST", "/projects"): _ok(id=1)})
        rid = _adapter(t).append_row("项目", {"名称": "甲", "计划完成": "2026-12-31"})
        self.assertEqual(rid, "1")

    def test_spec_required_is_the_source_for_the_other_entities(self):
        """必填清单**必须标出它从哪来** —— 规范说的 vs 实测的，不能混。

        第二轮实测改了几处（见 adapters/zentao.py 第 27~31 条）：
          · 需求/Bug/测试用例/测试单 的「所属产品」**必须走 query** → 判据变实测
          · 测试单规范漏了 ``status``；产品计划规范漏了 ``begin``+``end``
          · 业务需求/用户需求 规范漏了 reviewer，且它必须是**数组**
        """
        measured = {
            "项目": ("名称", "计划完成"),                       # 规范更严（第 18 条）
            "需求": ("标题", "所属产品", "评审人"),              # 第 27/30 条
            "Bug": ("标题", "所属产品", "影响版本"),             # 第 27 条
            "测试用例": ("标题", "所属产品"),                    # 第 27 条
            "测试单": ("名称", "所属产品", "提测构建", "状态", "开始日期", "结束日期"),  # 第 27/31 条
            "产品计划": ("名称", "所属产品", "计划开始", "计划完成"),   # 第 31 条
            "业务需求": ("标题", "所属产品", "评审人"),           # 第 30 条
            "用户需求": ("标题", "所属产品", "评审人"),           # 第 30 条
            "发布": ("名称", "所属产品", "所属应用", "包含构建", "计划发布日期"),  # 第 28 条
            "应用": ("名称", "所属产品", "是否集成应用", "集成子应用"),            # 第 28 条
        }
        from_spec = {
            "项目集": ("名称", "计划开始", "计划完成"),
            "执行": ("名称", "所属项目", "计划开始", "计划完成"),
            "任务": ("名称", "所属执行"),
            "产品": ("名称",),
            "用户": ("用户名", "姓名", "密码"),
            "构建": ("名称", "所属执行", "所属产品", "所属应用", "构建者", "打包日期"),
        }
        for name, req in measured.items():
            spec = ENTITY_SPECS[name]
            self.assertEqual(spec["required"], req, name)
            self.assertEqual(spec["required_source"], "measured", name)
        for name, req in from_spec.items():
            spec = ENTITY_SPECS[name]
            self.assertEqual(spec["required"], req, name)
            self.assertEqual(spec["required_source"], "spec", name)

        # 每个实体都必须标来源，且只有这两种 —— 第三种来源意味着有人凭感觉填了。
        for name, spec in ENTITY_SPECS.items():
            self.assertIn(spec.get("required_source"), ("spec", "measured"), name)

        # **结构性铁律**：父参数必须走 query 的实体，其必填判据一定是实测的
        # ——因为规范恰恰把这些字段声明在 body 里（第 27 条，规范错）。
        for name, spec in ENTITY_SPECS.items():
            if spec.get("parent_query"):
                self.assertEqual(spec["required_source"], "measured", name)
                # 且那个父字段必须真的是一个可写列
                lab = spec["parent_query"][0]
                self.assertIn(lab, {c[0] for c in spec["columns"]}, name)
                self.assertIn(lab, spec["required"], name)

    def test_required_labels_all_exist_as_columns(self):
        """必填列名必须都是真列 —— 否则客户端校验会永远失败（拦死所有创建）。
        这是最容易悄悄写错的一处（``spec_required`` 用的是**禅道字段名**，
        两者不能混）。"""
        for name, spec in ENTITY_SPECS.items():
            labels = {c[0] for c in spec["columns"]}
            for lab in spec["required"]:
                self.assertIn(lab, labels, "%s 的必填列 %s 不在列里" % (name, lab))

    def test_link_columns_all_exist_as_columns(self):
        """``link_cols`` 里的中文列名也必须都是真列 —— 否则 ``link_columns()``
        会报出一个「照着写却写不进去」的列名。"""
        for name, spec in ENTITY_SPECS.items():
            labels = {c[0] for c in spec["columns"]}
            for lab, _field, _other in spec.get("link_cols") or ():
                self.assertIn(lab, labels, "%s 的关联列 %s 不在列里" % (name, lab))

    def test_execution_exposes_products_column(self):
        """实测规范里执行的 POST 接受 ``products``；``link_cols`` 也声明了它 ——
        两边必须一致（以前 ``link_cols`` 提到了一个不存在的列）。"""
        labels = [c[0] for c in ENTITY_SPECS["执行"]["columns"]]
        self.assertIn("关联产品", labels)
        self.assertIn("关联产品", _adapter(_FakeTransport({})).link_columns("执行"))

    def test_empty_string_is_not_sent_because_server_rejects_it(self):
        """实测 ``PUT /projects/1 {"begin": ""}`` → 整条被拒（``result: fail``）且原值不变。

        所以「空值不发送」是被实测支持的；代价是适配器无法清空字段（成文在
        docstring 里）。这条用例保证那个「不发送」的决定不会被顺手改掉。
        """
        t = _FakeTransport({("PUT", "/projects/1"): _ok()})
        _adapter(t).update_row("项目", "1", {"计划开始": "", "名称": "甲"})
        self.assertEqual(t.body_of("PUT", "/projects/1"), {"name": "甲"})

    def test_delete_missing_id_is_a_failure(self):
        """实测删不存在的 id 会回 ``status: fail`` —— 不能当成「已经清掉了」。"""
        t = _FakeTransport({("DELETE", "/projects/999999"): _fail()})
        with self.assertRaises(ZentaoError) as c:
            _adapter(t).delete_rows("项目", ["999999"])
        self.assertIn("does not exist", str(c.exception))

    def test_description_of_a_real_write_envelope(self):
        """实测成功信封长这样（含 ``load``、``message``）：必须能被 ``_call`` 接受。"""
        real = {"message": "保存成功",
                "load": "/zentao/index.php?m=project&f=view&t=json&projectID=1",
                "status": "success"}
        t = _FakeTransport({("PUT", "/projects/1"): real})
        _adapter(t).update_row("项目", "1", {"名称": "甲"})   # 不抛即通过

    def test_dict_message_appears_with_status_fail_too(self):
        """字典形态的 ``message`` **不**只配 ``result: fail`` —— 实测还有一种
        ``{"status":"fail","message":{…}}``。两种都必须被格式化（而不是打成裸 dict）。"""
        t = _FakeTransport({("POST", "/projects"): {
            "status": "fail",
            "message": {"name": ["『名称』不能为空。"]}}})
        with self.assertRaises(ZentaoError) as c:
            _adapter(t).append_row("项目", {"名称": "甲", "计划完成": "2026-12-31"})
        self.assertIn("字段校验失败", str(c.exception))
        self.assertIn("不能为空", str(c.exception))
        self.assertEqual(c.exception.zentao_status, "fail")

    def test_envelope_of_distinguishes_three_shapes(self):
        """三种信封必须能被区分 —— 不区分的话读日志的人会以为是适配器自己出的错。"""
        a = _adapter(_FakeTransport({}))
        self.assertEqual(a._envelope_of({"status": "success"}), "status='success'")
        self.assertIn("result='fail'", a._envelope_of({"result": "fail"}))
        self.assertIn("无 status 键", a._envelope_of({"result": "fail"}))
        self.assertIn("既无", a._envelope_of({}))

    def test_project_name_quirk_hint_is_appended(self):
        """⚠️ 实测的一处「答非所问」：项目名用过（哪怕已删）就不可复用，
        而报错说的是「『产品名称』已经有…」。适配器必须把这句话翻成人话，
        否则读到它的人会去查产品，方向完全错。
        """
        t = _FakeTransport({("POST", "/projects"): {
            "status": "fail",
            "message": {"name": ["『产品名称』已经有『X』这条记录了。"]}}})
        with self.assertRaises(ZentaoError) as c:
            _adapter(t).append_row("项目", {"名称": "X", "计划完成": "2026-12-31"})
        msg = str(c.exception)
        self.assertIn("提示·实测", msg)
        self.assertIn("换个名字即可", msg)         # 可操作的结论
        self.assertIn("第 23 条", msg)             # 指回出处

    def test_hint_not_appended_to_unrelated_errors(self):
        """提示只在那个特定错误上出现 —— 不能变成每句报错后面都挂一段噪声。"""
        t = _FakeTransport({("GET", "/projects/999"): _fail()})
        with self.assertRaises(ZentaoError) as c:
            _adapter(t)._call("GET", "/projects/999")
        self.assertNotIn("提示·实测", str(c.exception))


class DeleteTests(unittest.TestCase):

    def test_delete_sends_no_body(self):
        """实测：``DELETE /<资源>/:id`` 不带请求体（规范里没有 requestBody）。"""
        t = _FakeTransport({("DELETE", "/projects/3"): _ok(),
                            ("DELETE", "/projects/4"): _ok()})
        _adapter(t).delete_rows("项目", ["3", "4", ""])
        self.assertEqual(t.paths(), ["/projects/3", "/projects/4"])
        for m, _p, _q, b in t.calls:
            self.assertEqual(m, "DELETE")
            self.assertIsNone(b)

    def test_delete_empty_list_sends_nothing(self):
        t = _FakeTransport({})
        _adapter(t).delete_rows("项目", [])
        self.assertEqual(t.calls, [])


# ══════════════════════════════════════════════════════════════════
# 6. 实体映射层
# ══════════════════════════════════════════════════════════════════

class MappingTests(unittest.TestCase):

    def test_aliases_resolve_to_entities(self):
        self.assertEqual(ALIASES["生产计划"], "执行")
        self.assertEqual(ALIASES["生产工序"], "任务")
        self.assertEqual(ALIASES["缺陷"], "Bug")

    def test_metadata_reports_alias_and_entity(self):
        a = _adapter(_FakeTransport({}))
        m = a.get_metadata("生产计划")
        self.assertEqual(m["table_name"], "执行")
        self.assertEqual(m["requested_name"], "生产计划")
        self.assertTrue(m["listable"])

    def test_metadata_marks_readonly_columns(self):
        a = _adapter(_FakeTransport({}))
        by = {c["name"]: c for c in a.get_metadata("项目")["columns"]}
        self.assertNotIn("readonly", by["名称"])
        self.assertTrue(by["状态"].get("readonly"))
        self.assertTrue(by["进度"].get("readonly"))

    def test_metadata_exposes_enum_options_only_where_spec_gives_them(self):
        a = _adapter(_FakeTransport({}))
        by = {c["name"]: c for c in a.get_metadata("项目")["columns"]}
        self.assertEqual(by["管理方式"]["options"]["scrum"], "敏捷")
        # 规范**没给**取值域的字段（项目 status）一律不造标签
        self.assertNotIn("options", by["状态"])

    def test_unknown_table_raises_and_explains_what_zentao_cannot_hold(self):
        a = _adapter(_FakeTransport({}))
        with self.assertRaises(KeyError) as c:
            a.list_rows("IC采购记录")
        msg = str(c.exception)
        self.assertIn("禅道承载不了", msg)
        self.assertIn("seatable", msg)

    def test_table_exists_reads_local_mapping_only(self):
        a = _adapter(_ExplodingTransport())
        self.assertTrue(a.table_exists("项目"))
        self.assertTrue(a.table_exists("生产工序"))     # 别名也算存在
        self.assertFalse(a.table_exists("采购记录"))

    def test_list_tables_contains_entities_and_aliases(self):
        a = _adapter(_ExplodingTransport())
        tables = a.list_tables()
        for t in ("项目", "执行", "任务", "产品", "需求", "Bug", "用户"):
            self.assertIn(t, tables)
        for t in ALIASES:
            self.assertIn(t, tables)
        self.assertEqual(tables, sorted(tables))

    def test_entity_tables_matches_specs(self):
        self.assertEqual(entity_tables(), sorted(ENTITY_SPECS))

    def test_no_network_for_declarations_and_metadata(self):
        """能力声明 / 逻辑表名 / 元数据 / 存在性判断**全部不联网**。

        为什么这条重要：上层会在**建连之前**读 ``capabilities()`` 来决定
        「要不要建连、建哪种连」。它一旦偷偷认证，就把分叉逻辑变成了网络故障点。
        """
        a = _adapter(_ExplodingTransport())
        self.assertTrue(a.capabilities())
        self.assertTrue(a.list_tables())
        self.assertTrue(a.get_metadata("项目")["columns"])
        self.assertTrue(a.table_exists("项目"))
        self.assertTrue(a.link_columns("任务"))   # 也是本地映射，不联网


# ══════════════════════════════════════════════════════════════════
# 7. 刻意不支持的能力
# ══════════════════════════════════════════════════════════════════

class UnsupportedTests(unittest.TestCase):

    def test_ensure_table_raises(self):
        a = _adapter(_FakeTransport({}))
        with self.assertRaises(Unsupported) as c:
            a.ensure_table("生产计划", [{"name": "名称", "type": "text"}])
        self.assertIn("没有建表", str(c.exception))

    def test_link_raises_not_silently_writes_foreign_key(self):
        """不降级成「写那个外键」：``link_id`` 在禅道里无意义，
        拿它去猜字段，猜错就是把数据挂到别的业务线上。"""
        a = _adapter(_FakeTransport({}))
        with self.assertRaises(Unsupported) as c:
            a.link("任务", "执行", "任意link_id", "15", ["2"])
        self.assertIn("update_row", str(c.exception))

    def test_list_linked_raises_instead_of_returning_empty(self):
        """本仓库吃过 ``return []`` 空桩的亏（SeaTable 的 list_linked 曾永远成功、
        永远返回空、永远不报错）。这里是成文的 Unsupported。"""
        a = _adapter(_FakeTransport({}))
        with self.assertRaises(Unsupported):
            a.list_linked("任务", "15", "")
        with self.assertRaises(Unsupported):
            a.list_linked("任务", "15", "任意link_id")

    def test_link_append_and_link_one_way_raise(self):
        a = _adapter(_FakeTransport({}))
        with self.assertRaises(Unsupported):
            a.link_append("任务", "执行", "l", "15", ["2"])
        with self.assertRaises(Unsupported):
            a.link_one_way("任务", "执行", "l", "15", ["2"])

    def test_link_columns_is_a_real_answer_not_a_stub(self):
        """``link_columns`` 是**扩展方法**：空列表是真实答案（该实体没有外键列），
        所以它不能抛 Unsupported，但要能报出外键列名。"""
        a = _adapter(_FakeTransport({}))
        self.assertIn("所属执行", a.link_columns("任务"))
        self.assertEqual(a.link_columns("用户"), [])


class CapabilityTests(unittest.TestCase):

    def test_declared_caps(self):
        caps = capabilities_of(_adapter(_FakeTransport({})))
        for c in (CAP_READ, CAP_WRITE, CAP_UPDATE, CAP_DELETE, CAP_SERVER_ROW_ID):
            self.assertIn(c, caps)

    def test_deliberately_absent_caps(self):
        caps = capabilities_of(_adapter(_FakeTransport({})))
        for c in (CAP_BATCH_WRITE, CAP_LINK, CAP_LINK_READ, CAP_SCHEMA_MANAGE,
                  CAP_QUERY_PUSHDOWN, CAP_IDEMPOTENT, CAP_OPTIMISTIC_LOCK):
            self.assertNotIn(c, caps)

    def test_backend_name(self):
        self.assertEqual(_adapter(_FakeTransport({})).backend, "zentao")

    def test_describe_never_leaks_the_token(self):
        a = ZentaoAdapter(base=BASE, token="SUPER-SECRET-TOKEN")
        self.assertNotIn("SUPER-SECRET-TOKEN", a.describe())
        self.assertIn("auth=token", a.describe())

    def test_query_falls_back_to_in_memory_filter_on_flat_values(self):
        """不声明 query_pushdown → 走基类内存过滤，且过滤的是**中文列名**。"""
        t = _FakeTransport({("GET", "/projects"): _ok(
            projects=[PROJECT, dict(PROJECT, id=4, name="乙项目", model="waterfall")],
            pager=_pager(2, 1, 100, 1))})
        rows = _adapter(t).query("项目", {"管理方式": "敏捷"})
        self.assertEqual([r["__row_id__"] for r in rows], [3])


# ══════════════════════════════════════════════════════════════════
# 8. 枚举与类型翻译
# ══════════════════════════════════════════════════════════════════

class EnumTests(unittest.TestCase):

    def test_unknown_code_passes_through_unchanged(self):
        """规范没给取值域、或给了但出现了没见过的值 → **原样透传**。

        猜一个「已关闭」比裸的 ``closed`` 更容易让人误信 —— 所以不猜。
        """
        self.assertEqual(_enum_label("项目", "status", "doing"), "doing")
        self.assertEqual(_enum_label("产品", "type", "未来类型"), "未来类型")

    def test_round_trip(self):
        for table, field, codes in (
                ("项目", "model", ("scrum", "waterfall", "kanban")),
                ("产品", "type", ("normal", "branch", "platform")),
                ("Bug", "type", ("codeerror", "others"))):
            for code in codes:
                label = _enum_label(table, field, code)
                self.assertNotEqual(label, code)
                self.assertEqual(_enum_code(table, field, label), code)

    def test_non_string_passes_through(self):
        self.assertEqual(_enum_label("项目", "model", None), None)
        self.assertEqual(_enum_code("项目", "model", 5), 5)

    def test_read_translates_enum_and_keeps_untranslated_fields_raw(self):
        t = _FakeTransport({("GET", "/users"): _ok(
            users=[USER_ROW], pager=_pager(1, 1, 100, 1))})
        r = _adapter(t).list_rows("用户")[0]
        self.assertEqual(r["界面类型"], "研发综合界面")       # rnd → 中文
        self.assertEqual(r["职位"], "admin")                  # 规范没给取值域 → 原样

    def test_enums_only_cover_fields_the_spec_documents(self):
        """每个枚举项都要能在规范描述里找到出处 —— 这里只校验语言一致性：
        值必须是规范里的字面量、标签必须是人能看懂的中文。"""
        for table, fields in _ENUMS.items():
            self.assertIn(table, ENTITY_SPECS)
            for field, mapping in fields.items():
                self.assertIn(field, [c[1] for c in ENTITY_SPECS[table]["columns"]])
                for code, label in mapping.items():
                    self.assertTrue(code.isascii(), "%s.%s: %r" % (table, field, code))
                    self.assertNotEqual(code, label)

    def test_neutral_type_mapping(self):
        self.assertEqual(neutral_type("array"), "multiselect")
        self.assertEqual(neutral_type("secret"), "text")
        self.assertEqual(neutral_type("longtext"), "longtext")
        self.assertIn(neutral_type("select"), schema.NEUTRAL_TYPES)
        # 认不出的原样返回，绝不静默降级成 text
        self.assertEqual(neutral_type("weird"), "weird")


class SchemaDecisionTests(unittest.TestCase):

    def test_zentao_is_deliberately_absent_from_backend_types(self):
        """**刻意没有** ``BACKEND_TYPES["zentao"]`` —— 这不是漏了。

        禅道实体与字段都固定、REST v2 没有建表/加列接口，
        没有任何一条代码路径会把中立类型发给禅道
        （``ZentaoAdapter.ensure_table`` 直接抛 ``Unsupported``）。
        硬凑一张翻译表只会让人以为禅道能建列。
        """
        self.assertNotIn("zentao", schema.BACKEND_TYPES)
        with self.assertRaises(Unsupported):
            schema.backend_type("zentao", "text")
        # 而「读向」的对应关系是有的，且必须落在中立词表内
        self.assertEqual(neutral_type("array"), "multiselect")

    def test_every_declared_column_type_maps_to_a_neutral_type(self):
        for ename, spec in ENTITY_SPECS.items():
            for _label, _field, ctype, _rw in spec["columns"]:
                self.assertIn(neutral_type(ctype), schema.NEUTRAL_TYPES,
                              "%s.%s" % (ename, ctype))


# ══════════════════════════════════════════════════════════════════
# 9. 认证
# ══════════════════════════════════════════════════════════════════

class AuthTests(unittest.TestCase):

    PROBE_OK = _ok(projects=[], pager=_pager(0, 1, 1, 1))

    def test_token_probe_success(self):
        t = _FakeTransport({("GET", "/projects"): self.PROBE_OK})
        a = ZentaoAdapter(base=BASE, token=TOKEN, transport=t)
        a.auth()
        self.assertEqual(t.calls[0][0], "GET")
        self.assertEqual(a._probe_note, "")

    def test_business_failure_is_not_an_auth_failure(self):
        """探活判据刻意宽松：**只有认证类失败才算失败**。

        业务类失败（如某资源没权限）说明**凭据是好的**，不该在建连阶段就炸；
        原因记进 ``probe_note`` 供诊断。
        """
        t = _FakeTransport({("GET", "/projects"):
                            _fail("No permission to visit projects.")})
        a = ZentaoAdapter(base=BASE, token=TOKEN, transport=t)
        a.auth()
        self.assertIn("No permission", a._probe_note)

    def test_auth_failure_raises(self):
        t = _FakeTransport({("GET", "/projects"):
                            (401, '{"status":"fail","message":"Not allowed"}')})
        with self.assertRaises(ZentaoError) as c:
            ZentaoAdapter(base=BASE, token=TOKEN, transport=t).auth()
        self.assertIn("认证失败", str(c.exception))

    def test_not_allowed_in_body_raises_even_on_http_200(self):
        t = _FakeTransport({("GET", "/projects"):
                            _fail("Not allowed to visit this resource.")})
        with self.assertRaises(ZentaoError):
            ZentaoAdapter(base=BASE, token=TOKEN, transport=t).auth()

    def test_missing_credentials_raises_with_instructions(self):
        with self.assertRaises(ZentaoError) as c:
            ZentaoAdapter(base=BASE, transport=_FakeTransport({})).auth()
        self.assertIn("token", str(c.exception))
        self.assertIn("/user/login", str(c.exception))

    def test_login_uses_singular_path(self):
        """⚠️ 实测 ``/users/login``（复数，OpenAPI 里写的那个）**不存在**，
        真实路径是 ``/user/login``（单数）。这条钉子是为了防止有人
        「照规范修正」把它改回复数。
        """
        t = _FakeTransport({("POST", "/user/login"):
                            _ok(token="tk-from-login", user={"account": "zentao"}),
                            ("GET", "/projects"): self.PROBE_OK})
        a = ZentaoAdapter(base=BASE, account="zentao", password="pw", transport=t)
        a.auth()
        self.assertEqual(a.token, "tk-from-login")
        self.assertEqual(t.token, "tk-from-login")      # 传输层也要拿到
        self.assertIn(("POST", "/user/login"), [(m, p) for m, p, _q, _b in t.calls])
        self.assertNotIn("/users/login", t.paths())

    def test_login_failure_raises(self):
        t = _FakeTransport({("POST", "/user/login"): _fail("Wrong password.")})
        with self.assertRaises(ZentaoError) as c:
            ZentaoAdapter(base=BASE, account="a", password="b", transport=t).auth()
        self.assertIn("登录失败", str(c.exception))
        self.assertIn("Wrong password.", str(c.exception))

    def test_login_success_without_token_raises(self):
        t = _FakeTransport({("POST", "/user/login"): _ok(user={"account": "a"})})
        with self.assertRaises(ZentaoError) as c:
            ZentaoAdapter(base=BASE, account="a", password="b", transport=t).auth()
        self.assertIn("没有 token", str(c.exception))

    def test_lazy_auth_builds_transport_and_probes_once(self):
        """没有注入传输层时，第一次 ``_call`` 会自己 ``auth()``（含探活），
        上层不必先手动认证 —— 但**只**建一次连。"""
        t = _FakeTransport({("GET", "/projects"): self.PROBE_OK})
        a = ZentaoAdapter(base=BASE, token=TOKEN)
        with patch("adapters.zentao._HttpTransport", return_value=t) as mk:
            rows = a.list_rows("项目")
        self.assertEqual(rows, [])
        mk.assert_called_once()
        # 第一次是 auth() 的探活，第二次才是真正的列表读
        self.assertEqual(t.paths(), ["/projects", "/projects"])
        self.assertEqual(t.calls[0][2], {"recPerPage": 1, "pageID": 1})   # 探活
        self.assertEqual(t.calls[1][2]["recPerPage"], _PAGE)             # 列表


# ══════════════════════════════════════════════════════════════════
# 10. 可插拔注册（判据：注册完不需要改工厂一行代码）
# ══════════════════════════════════════════════════════════════════

class RegistrationTests(unittest.TestCase):

    def test_registered_in_backend_registry(self):
        self.assertIn("zentao", factory.list_backends())
        info = factory.BACKENDS["zentao"]
        self.assertEqual(info["label"], "禅道")
        self.assertTrue(info["named_bases"])
        self.assertEqual(info["required_keys"], ("base_url",))
        self.assertTrue(info["default_server"].endswith("/api.php/v2"))
        self.assertEqual(factory.backend_deploy_hooks("zentao")["verify"][0],
                         "verify_zentao.py")

    def test_key_aliases_are_accepted(self):
        cfg = {"backend": "zentao",
               "zentao": {"token": "t",
                          "bases": {"a": {"url": "http://a/api.php/v2"},
                                    "b": {"base": "http://b/api.php/v2"},
                                    "c": {"site_url": "http://c/api.php/v2"}}}}
        for name, want in (("a", "http://a/api.php/v2"),
                           ("b", "http://b/api.php/v2"),
                           ("c", "http://c/api.php/v2")):
            sel = factory.get_base_config(cfg, base_name=name, backend="zentao")
            self.assertEqual(sel["base_url"], want)

    def test_adapter_built_through_the_registry(self):
        """走 ``factory.get_adapter`` 构造 —— 证明第 ③ 步「登记一行」足够。"""
        cfg = {"backend": "zentao",
               "zentao": {"token": "tk-x",
                          "bases": {"production": {"base_url": BASE}}}}
        a = factory.get_adapter(cfg, base_name="production")
        self.assertIsInstance(a, ZentaoAdapter)
        self.assertEqual(a.base, BASE)
        self.assertEqual(a.token, "tk-x")
        self.assertEqual(a.site, "production")
        self.assertTrue(a.no_proxy)          # 默认 True：绕开沙箱注入的代理

    def test_no_proxy_can_be_turned_off(self):
        cfg = {"backend": "zentao",
               "zentao": {"token": "t", "no_proxy": False, "proxy": "http://127.0.0.1:7897",
                          "bases": {"production": {"base_url": BASE}}}}
        a = factory.get_adapter(cfg, base_name="production")
        self.assertFalse(a.no_proxy)
        self.assertEqual(a.proxy, "http://127.0.0.1:7897")

    def test_missing_base_url_falls_back_loudly_not_silently(self):
        """缺 ``base_url`` → 不静默退回 local（写入路径会硬失败）。"""
        cfg = {"backend": "zentao", "zentao": {"token": "t",
                                               "bases": {"production": {}}}}
        with self.assertRaises(RuntimeError):
            factory.get_adapter(cfg, base_name="production", strict=True)

    def test_verify_script_exists(self):
        import importlib.util
        p = os.path.join(HERE, "tools", "verify_zentao.py")
        self.assertTrue(os.path.exists(p), "部署钩子声明的 verify 脚本必须存在")
        spec = importlib.util.spec_from_file_location("verify_zentao", p)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        self.assertTrue(callable(mod.main))
        # ⚠️ 写测试用的名字**必须带时间戳**：实测项目名的重名检查把已删除的产品
        #    也算在内，固定名字会让第二次运行必然失败。
        mk = mod.run_mark()
        self.assertTrue(mk.startswith(mod.MARK))
        self.assertGreater(len(mk), len(mod.MARK))
        self.assertEqual(len(mk), len(mod.MARK) + 1 + 14)     # _YYYYmmddHHMMSS
        # 只读闸门只放行 GET —— 「本脚本只发 GET」是断言，不是承诺
        seen = []

        class _Inner:
            token = ""

            def request(self, method, path, query=None, body=None):
                seen.append(method)
                return 200, "{}"

            def close(self):
                pass

        guard = mod._MethodGuard(_Inner(), ("GET",))
        guard.request("GET", "/projects")
        with self.assertRaises(AssertionError):
            guard.request("POST", "/projects", body={"name": "x"})
        with self.assertRaises(AssertionError):
            guard.request("DELETE", "/projects/1")
        self.assertEqual(seen, ["GET"])
        self.assertEqual(guard.blocked, [("POST", "/projects"), ("DELETE", "/projects/1")])

    def test_write_test_refuses_reference_typed_required_columns(self):
        """必填列是**引用**（number/select/array/secret）时写测试必须跳过而不是造值 ——
        造一个数字等于把记录挂到陌生业务线上。"""
        import importlib.util
        p = os.path.join(HERE, "tools", "verify_zentao.py")
        spec = importlib.util.spec_from_file_location("verify_zentao2", p)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        self.assertEqual(mod._safe_value("text", "M"), "M")
        self.assertEqual(mod._safe_value("longtext", "M"), "M")
        self.assertEqual(mod._safe_value("date", "M"),
                         (__import__("datetime").date.today()
                          + __import__("datetime").timedelta(days=365)).strftime("%Y-%m-%d"))
        for t in ("number", "select", "array", "secret", "datetime"):
            self.assertIsNone(mod._safe_value(t, "M"), t)
        # 级联清理的表清单必须是有依据的（实测：建项目会顺手建同名产品）
        self.assertIn("产品", mod.CASCADE_WATCH)

    def test_no_real_credentials_in_this_file(self):
        """本文件里的 token 必须明显是假的 —— 防止有人把真令牌粘进测试。"""
        with open(os.path.abspath(__file__), encoding="utf-8") as f:
            body = f.read()
        self.assertIn(TOKEN, body)
        self.assertTrue(TOKEN.startswith("tk-test-"))


class ParentQueryTests(unittest.TestCase):
    """第 27 条：``productID`` 必须走 **query string**，放 body 会被当成「没传」。

    而官方 OpenAPI **恰恰把它声明为 requestBody 里的 integer 属性** —— 规范错了。
    这条守卫要是没了，所有按产品写入的调用都会拿到
    ``Missing required parameter: productID.``，而这个错让人根本想不到是「位置错了」。
    """

    def test_parent_query_goes_to_query_string_not_body(self):
        t = _FakeTransport({("POST", "/bugs"): {"status": "success", "id": 11}})
        a = _adapter(t)
        a.append_row("Bug", {"标题": "甲", "所属产品": 9, "影响版本": "trunk"})
        method, path, query, body = t.calls[0]
        self.assertEqual(method, "POST")
        self.assertEqual(query, {"productID": 9})
        self.assertNotIn("productID", body, "父参数**不能**同时出现在 body 里")
        self.assertEqual(body.get("title"), "甲")
        # 规范说 openedBuild 是 array → 裸值必须被包成列表（第 31 条同源的形状问题）
        self.assertEqual(body.get("openedBuild"), ["trunk"])

    def test_missing_parent_query_is_refused_before_any_request(self):
        t = _FakeTransport({})       # 登记为空：一旦发请求就会 AssertionError
        a = _adapter(t)
        with self.assertRaises(ZentaoError) as cm:
            a.append_row("需求", {"标题": "没有产品"})
        self.assertIn("所属产品", str(cm.exception))
        self.assertIn("query", str(cm.exception))
        self.assertEqual(t.calls, [], "客户端校验必须在发请求之前拦下")

    def test_update_row_does_not_send_parent_query(self):
        """``update_row`` 不发父参数 —— 实测**没有**验过 PUT 也要它，
        与其猜一个，不如让服务端在真需要时报它自己的错（信息明确、可诊断）。"""
        t = _FakeTransport({("PUT", "/bugs/7"): {"status": "success"}})
        a = _adapter(t)
        a.update_row("Bug", "7", {"标题": "改标题"})
        self.assertEqual(t.calls[0][2], {})

    def test_all_parent_query_entities_are_marked_and_have_the_column(self):
        """**结构性铁律**：parent_query 指向的字段必须真的是可写列，且服务端确实要它。

        防止有人「把 parent_query 抄到别的实体上」而不去想那个实体到底要不要。
        """
        have = {n for n, s in ENTITY_SPECS.items() if s.get("parent_query")}
        self.assertEqual(have, {"需求", "Bug", "测试用例", "测试单",
                                "产品计划", "发布", "业务需求", "用户需求", "应用"})
        for name in have:
            spec = ENTITY_SPECS[name]
            label, field = spec["parent_query"]
            cols = {c[0]: c[1] for c in spec["columns"]}
            self.assertEqual(cols.get(label), field, name)
            self.assertIn(label, spec["required"], name)
            # 走 query 的是「产品」这个父资源 —— 禅道里产品系实体才是这样
            self.assertEqual(field, "productID", name)

    def test_task_parent_goes_through_body_not_query(self):
        """任务是**反例**：它的父字段 ``executionID`` 实测走 **body**。

        没有把这条钉住的话，很容易「看到 productID 走 query 就把所有父字段都改成 query」，
        那会让任务的写入全挂。
        """
        t = _FakeTransport({("POST", "/tasks"): {"status": "success", "id": 3}})
        a = _adapter(t)
        a.append_row("任务", {"名称": "工序", "所属执行": 2})
        _m, _p, query, body = t.calls[0]
        self.assertEqual(query, {})
        self.assertEqual(body.get("executionID"), 2)
        self.assertNotIn("parent_query", ENTITY_SPECS["任务"])


class ChildListTests(unittest.TestCase):
    """第 39 条：顶层列表回 0 字节的实体，**经父资源都能读到**。

    这是「读不了就是读不了」与「其实有正路」的分界线 —— 没有这条能力，
    任务/需求/业务需求/用户需求/产品计划/发布/应用 在适配器里就全是只写不读。
    """

    def test_list_children_uses_parent_path(self):
        t = _FakeTransport({
            ("GET", "/executions/2/tasks"): _ok(
                tasks=[TASK_ROW], pager=_pager(1, 1, 100, 1)),
        })
        a = _adapter(t)
        rows = a.list_children("任务", 2)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["名称"], "工序A")
        self.assertEqual(rows[0]["__row_id__"], 15)
        self.assertEqual(t.calls[0][1], "/executions/2/tasks")

    def test_list_children_requires_parent_id(self):
        a = _adapter(_FakeTransport({}))
        with self.assertRaises(ZentaoError):
            a.list_children("任务", "")

    def test_children_paging_terminates_on_page_total_not_on_empty_page(self):
        """第 26 条：超范围页码会被**钳制到最后一页**，本页永远不为空。

        所以「读到空页就停」会死循环、「本页与上页重复就报错」会误报。
        必须用 ``pager.pageTotal``。这里造一个「永远回同一页」的服务端来证明
        终止不是靠内容判断的。
        """
        hops = []

        def stuck(query, _body):
            hops.append(int(query.get("pageID") or 1))
            return _ok(tasks=[TASK_ROW],
                       pager=_pager(rec_total=1, page=1, size=1, page_total=1))
        t = _FakeTransport({("GET", "/executions/2/tasks"): stuck})
        a = _adapter(t)
        rows = a.list_children("任务", 2)
        self.assertEqual(len(rows), 1)
        self.assertEqual(hops, [1], "pageTotal=1 时读到第 1 页就该收工")

    def test_children_wrong_array_key_raises(self):
        t = _FakeTransport({
            ("GET", "/executions/2/tasks"): _ok(somethingElse=[], pager=_pager(0, 1, 100, 0)),
        })
        a = _adapter(t)
        with self.assertRaises(ZentaoError) as cm:
            a.list_children("任务", 2)
        self.assertIn("tasks", str(cm.exception))

    def test_top_level_list_unsupported_message_points_to_children(self):
        """顶层读不了时，报错必须**指出正路** —— 只说「不支持」等于把人堵死。"""
        a = _adapter(_FakeTransport({}))
        with self.assertRaises(Unsupported) as cm:
            a.list_rows("任务")
        msg = str(cm.exception)
        self.assertIn("list_children", msg)
        self.assertIn("执行", msg)

    def test_every_children_of_points_at_a_real_listable_parent(self):
        """**结构性铁律**：``children_of`` 的三段都必须对得上。

        ``(父实体, 子字段, 数组键)`` —— 注意**第二段是「子实体自己的列」**
        （``任务.executionID`` 指向执行），不是父实体的列。
        写错的话 ``list_children`` 会抛一个指不到病根的错。
        """
        for name, spec in ENTITY_SPECS.items():
            co = spec.get("children_of")
            if not co:
                continue
            self.assertEqual(len(co), 3, name)
            parent, field, arr_key = co
            self.assertIn(parent, ENTITY_SPECS, name)
            self.assertTrue(arr_key and isinstance(arr_key, str), name)
            # 父实体必须自己读得到（否则「经父读子」这条路是死的）
            self.assertTrue(ENTITY_SPECS[parent].get("listable"),
                            "父实体「%s」自己都不可列表" % parent)
            # 第二段必须是**子实体自己**的列
            child_cols = {c[1] for c in spec["columns"]}
            self.assertIn(field, child_cols,
                          "%s 的 children_of 字段 %s 不是它自己的列" % (name, field))
            # 该字段必须也是声明过的可写列（用于建关联）
            self.assertIn(field, {c[1] for c in spec["columns"] if c[3] == "W"}, name)

    def test_children_of_is_declared_for_every_not_listable_entity_that_has_one(self):
        """**反向**：声明了 ``children_of`` 的实体，要么顶层读得了（多一条路），
        要么 ``not_listable_why`` 必须写清楚为什么 —— 不许两者都空。"""
        for name, spec in ENTITY_SPECS.items():
            if spec.get("listable"):
                continue
            self.assertTrue(spec.get("not_listable_why"), "%s 少了 not_listable_why" % name)
            self.assertTrue(spec.get("children_of"),
                            "%s 既不可列表、又没有 children_of —— 那它就是纯只写实体" % name)


class CreateIdRecoveryTests(unittest.TestCase):
    """第 33 条：``POST /epics`` ``/requirements`` ``/systems`` 成功**但不回 id**。

    不处理的话，调用方拿到的是「建好了但不知道是哪一条」—— 无法更新、无法删除，
    而且**看不出哪里不对**（响应里确实写着 success）。
    """

    def _children_fn(self):
        state = {"n": 0}

        def fn(_query, _body):
            state["n"] += 1
            rows = [{"id": "1", "title": "旧业务需求"}]
            if state["n"] >= 2:                      # POST 之后再读，多出一条
                rows = rows + [{"id": "2", "title": "新业务需求"}]
            return _ok(epics=rows, pager=_pager(len(rows), 1, 100, 1))
        return fn

    def test_id_is_recovered_by_diffing_children(self):
        t = _FakeTransport({
            ("POST", "/epics"): {"status": "success", "message": "保存成功",
                                 "load": "/zentao/index.php?m=product&f=browse&t=json"},
            ("GET", "/products/9/epics"): self._children_fn(),
        })
        a = _adapter(t)
        rid = a.append_row("业务需求",
                           {"标题": "新业务需求", "所属产品": 9, "评审人": ["admin"]})
        self.assertEqual(rid, "2")
        # 反查用的是 id 差集，不是标题匹配 —— 标题重复时也能对
        gets = [c for c in t.calls if c[0] == "GET"]
        self.assertEqual(len(gets), 2, "应当在 POST 前后各读一次子列表")

    def test_recovery_matches_by_title_when_several_are_new(self):
        """一次多出几条时，用标题再筛一次；仍然拿不准就报错，**不猜**。"""
        state = {"n": 0}

        def fn(_query, _body):
            state["n"] += 1
            if state["n"] == 1:
                return _ok(epics=[], pager=_pager(0, 1, 100, 0))
            return _ok(epics=[{"id": "8", "title": "别的"},
                              {"id": "9", "title": "目标"}],
                       pager=_pager(2, 1, 100, 1))
        t = _FakeTransport({
            ("POST", "/epics"): {"status": "success"},
            ("GET", "/products/9/epics"): fn,
        })
        a = _adapter(t)
        rid = a.append_row("业务需求",
                           {"标题": "目标", "所属产品": 9, "评审人": ["admin"]})
        self.assertEqual(rid, "9")

    def test_ambiguous_recovery_raises_instead_of_guessing(self):
        state = {"n": 0}

        def fn(_query, _body):
            state["n"] += 1
            rows = [] if state["n"] == 1 else [{"id": "8", "title": "甲"},
                                               {"id": "9", "title": "乙"}]
            return _ok(epics=rows, pager=_pager(len(rows), 1, 100, 1))
        t = _FakeTransport({
            ("POST", "/epics"): {"status": "success"},
            ("GET", "/products/9/epics"): fn,
        })
        a = _adapter(t)
        with self.assertRaises(ZentaoError) as cm:
            a.append_row("业务需求",
                         {"标题": "丙", "所属产品": 9, "评审人": ["admin"]})
        self.assertIn("不猜", str(cm.exception))

    def test_entities_that_do_not_return_id_are_declared(self):
        """**结构性铁律**：声明了 ``create_id_via`` 的实体必须①不可顶层列表
        （否则直接查列表就行）②有 ``children_of``（否则没法反查）。"""
        declared = {n for n, s in ENTITY_SPECS.items() if s.get("create_id_via")}
        self.assertEqual(declared, {"业务需求", "用户需求", "应用"})
        for name in declared:
            spec = ENTITY_SPECS[name]
            self.assertTrue(spec.get("children_of"), name)
            parent, _field, arr_key = spec["children_of"]
            # create_id_via = (父列的**中文列名**, 子列表数组键) —— 两处都要对得上
            self.assertEqual(spec["create_id_via"][0], spec["parent_query"][0], name)
            self.assertEqual(spec["create_id_via"][1], arr_key, name)
            self.assertEqual(parent, "产品", name)


class ActionTests(unittest.TestCase):
    """第 41 条：状态流转动作 —— 「专业 PM 软件」与「一张表」的分水岭。

    用 ``update_row`` 把 ``status`` 改成 ``doing`` 是做不出动作的副作用的
    （记工时、写实际开始/完成时间、按服务端规则算剩余）。
    """

    def test_actions_of_lists_measured_required_fields(self):
        a = _adapter(_FakeTransport({}))
        acts = {x["动作"]: x for x in a.actions_of("任务")}
        self.assertEqual(set(acts), {"启动", "完成", "激活", "关闭"})
        self.assertEqual(acts["启动"]["路径"], "start")
        self.assertEqual(acts["启动"]["必填"], ["消耗工时", "预计剩余"])
        self.assertEqual(acts["完成"]["必填"], ["实际开始", "本次消耗", "实际完成"])
        self.assertEqual(acts["关闭"]["必填"], [])

    def test_actions_of_is_empty_for_entities_without_actions(self):
        """**真的没有** —— 不是读不到。禅道只为部分实体提供流转子路径。"""
        a = _adapter(_FakeTransport({}))
        for name in ("产品", "项目", "项目集", "执行", "用户", "构建", "发布", "应用"):
            self.assertEqual(a.actions_of(name), [], name)

    def test_run_action_maps_labels_to_measured_fields(self):
        t = _FakeTransport({("PUT", "/tasks/15/start"): {"status": "success"}})
        a = _adapter(t)
        a.run_action("任务", 15, "启动", {"消耗工时": "2", "预计剩余": "6"})
        method, path, query, body = t.calls[0]
        self.assertEqual((method, path), ("PUT", "/tasks/15/start"))
        self.assertEqual(body, {"consumed": "2", "left": "6"})
        self.assertEqual(query, {})

    def test_run_action_refuses_when_measured_required_is_missing(self):
        """必填是**实测**出来的（规范里一个都没写）→ 客户端必须拦，
        否则服务端只会回一句 `"总计消耗"和"预计剩余"不能同时为0`，很难对上是哪个字段。"""
        t = _FakeTransport({})
        a = _adapter(t)
        with self.assertRaises(ZentaoError) as cm:
            a.run_action("任务", 15, "完成", {"本次消耗": "1"})
        self.assertIn("实际开始", str(cm.exception))
        self.assertIn("实际完成", str(cm.exception))
        self.assertEqual(t.calls, [])

    def test_run_action_unknown_action_raises_and_lists_available(self):
        t = _FakeTransport({})
        a = _adapter(t)
        with self.assertRaises(Unsupported) as cm:
            a.run_action("任务", 15, "起飞", {})
        self.assertIn("启动", str(cm.exception))
        self.assertEqual(t.calls, [])

    def test_run_action_accepts_the_raw_path_segment_too(self):
        t = _FakeTransport({("PUT", "/bugs/7/close"): {"status": "success"}})
        a = _adapter(t)
        a.run_action("Bug", "7", "close")          # 也可以用禅道原路径段
        self.assertEqual(t.calls[0][1], "/bugs/7/close")

    def test_run_action_wraps_array_typed_action_fields(self):
        """``openedBuild`` 实测要数组 —— 传裸值必须被包起来，否则服务端报「不能为空」。"""
        t = _FakeTransport({("PUT", "/bugs/7/activate"): {"status": "success"}})
        a = _adapter(t)
        a.run_action("Bug", "7", "激活", {"影响版本": "trunk"})
        self.assertEqual(t.calls[0][3], {"openedBuild": ["trunk"]})

    def test_run_action_rejects_unknown_field_names(self):
        t = _FakeTransport({})
        a = _adapter(t)
        with self.assertRaises(ZentaoError) as cm:
            a.run_action("任务", 15, "关闭", {"随便一个列": "x"})
        self.assertIn("随便一个列", str(cm.exception))
        self.assertEqual(t.calls, [])

    def test_run_action_requires_row_id(self):
        a = _adapter(_FakeTransport({}))
        with self.assertRaises(ZentaoError):
            a.run_action("任务", "", "关闭", {})

    def test_close_action_sends_empty_body(self):
        """实测「关闭」空 body 即可 —— 不要自作聪明塞一个 ``{"status":"closed"}``，
        那是个未知字段，服务端会**静默忽略**（第 13 条）。"""
        t = _FakeTransport({("PUT", "/tasks/15/close"): {"status": "success"}})
        a = _adapter(t)
        a.run_action("任务", 15, "关闭")
        self.assertEqual(t.calls[0][3], {})

    def test_action_fields_are_not_entity_columns(self):
        """**动作专属**字段不进 ``columns`` —— 它们只在某个动作的请求体里有意义，
        混进列会让「这个实体有哪些列」变糊，也会让 ``update_row`` 误以为可以随便改。

        ⚠️ 例外是 ``openedBuild``：它**既是** Bug 的真列（影响版本），
        **又是** ``激活`` 动作的必填 —— 这是真实重叠，不是设计缺陷。
        所以这里比的是「词表减去真列」之后的差集。
        """
        from adapters.zentao import ACTION_FIELDS
        all_cols = {c[1] for spec in ENTITY_SPECS.values() for c in spec["columns"]}
        # openedBuild 本来就该在两处都出现
        self.assertIn("openedBuild", all_cols)
        for f in ACTION_FIELDS.values():
            if f == "openedBuild":
                continue
            self.assertNotIn(f, all_cols,
                             "动作专属字段 %s 混进了某个实体的列" % f)

    def test_every_measured_action_required_field_is_in_the_vocabulary(self):
        """动作的必填清单只能引用词表里的字段 —— 写错的字面量会让必填校验
        **永远失败**（拦死所有动作调用），而且报错指向一个不存在的字段名。"""
        from adapters.zentao import ACTION_FIELDS
        for name, spec in ENTITY_SPECS.items():
            for _lab, _act, req, _desc in (spec.get("actions") or ()):
                for f in req:
                    self.assertIn(f, ACTION_FIELDS, "%s 的必填 %s 不在词表里" % (name, f))


class DeleteQueryTests(unittest.TestCase):
    """第 29 / 36 / 37 条：删除的三处坑。"""

    def test_epics_delete_uses_storyID_query(self):
        """规范写 ``DELETE /epics/:epicID``，实现要 ``?storyID=`` —— 规范错。"""
        t = _FakeTransport({("DELETE", "/epics/2"): {"status": "success"}})
        a = _adapter(t)
        a.delete_rows("业务需求", ["2"])
        method, path, query, _body = t.calls[0]
        self.assertEqual((method, path), ("DELETE", "/epics/2"))
        self.assertEqual(query, {"storyID": "2"})

    def test_requirements_delete_uses_storyID_query(self):
        t = _FakeTransport({("DELETE", "/requirements/5"): {"status": "success"}})
        a = _adapter(t)
        a.delete_rows("用户需求", ["5"])
        self.assertEqual(t.calls[0][2], {"storyID": "5"})

    def test_normal_entities_delete_without_query(self):
        t = _FakeTransport({("DELETE", "/bugs/7"): {"status": "success"}})
        a = _adapter(t)
        a.delete_rows("Bug", ["7"])
        self.assertEqual(t.calls[0][2], {})

    def test_system_delete_raises_unsupported(self):
        """``/systems`` 规范里也只有 POST/PUT —— 没有删除接口。
        发一个注定失败的请求、让上层从报错里猜「是不是没权限」，是更糟的做法。"""
        t = _FakeTransport({})
        a = _adapter(t)
        with self.assertRaises(Unsupported) as cm:
            a.delete_rows("应用", ["3"])
        self.assertIn("DELETE", str(cm.exception))
        self.assertEqual(t.calls, [], "不该发出任何请求")

    def test_only_systems_is_declared_undeletable(self):
        declared = {n for n, s in ENTITY_SPECS.items() if s.get("no_delete_why")}
        self.assertEqual(declared, {"应用"})

    def test_delete_query_entities_have_no_delete_why(self):
        """互斥：能删的才写 delete_query，不能删的写 no_delete_why —— 不许两个都写。"""
        for name, spec in ENTITY_SPECS.items():
            if spec.get("no_delete_why"):
                self.assertFalse(spec.get("delete_query"), name)


class SuccessButEmptyTests(unittest.TestCase):
    """第 34 条：``status:"success"`` 也可能是「什么都没有」。

    ``GET /epics/3``（不存在）回的是
    ``{"status":"success","load":{"alert":"抱歉，您访问的对象不存在！"}}``。
    只看 ``status`` 的 ``get_row`` 会把它当成「存在但字段全空」——
    比抛异常更糟，因为上层会**照着一行空数据继续走**。
    """

    def test_get_row_returns_none_for_success_with_load_only(self):
        t = _FakeTransport({
            ("GET", "/tasks/999"): {
                "load": {"alert": "抱歉，您访问的对象不存在！",
                         "locate": "/zentao/index.php?m=product&f=index&t=json"},
                "status": "success",
            },
        })
        a = _adapter(t)
        self.assertIsNone(a.get_row("任务", 999))

    def test_get_row_finds_detail_under_resource_key(self):
        t = _FakeTransport({("GET", "/tasks/15"): _ok(task=TASK_ROW)})
        a = _adapter(t)
        row = a.get_row("任务", "15")
        self.assertIsNotNone(row)
        self.assertEqual(row["名称"], "工序A")

    def test_get_row_ignores_load_when_real_data_is_present(self):
        """``load`` 是跳转指令，不是数据 —— 有真数据时不能被它带偏。"""
        t = _FakeTransport({
            ("GET", "/tasks/15"): {"status": "success",
                                   "load": {"locate": "/x"},
                                   "task": TASK_ROW},
        })
        a = _adapter(t)
        row = a.get_row("任务", "15")
        self.assertEqual(row["__row_id__"], 15)

    def test_get_row_raises_when_response_has_no_entity_object(self):
        """既没有业务对象、也没有 ``load`` → 结构不符，必须报错而不是给空行。"""
        t = _FakeTransport({("GET", "/tasks/15"): {"status": "success", "a": 1, "b": 2}})
        a = _adapter(t)
        with self.assertRaises(ZentaoError):
            a.get_row("任务", "15")


class FullCoverageMappingTests(unittest.TestCase):
    """第二轮把覆盖从 10 个实体扩到 16 个 —— 这里钉住「扩了哪些、没扩哪些」。"""

    def test_entity_set_is_the_measured_16(self):
        self.assertEqual(set(entity_tables()), {
            "项目集", "项目", "执行", "任务", "产品", "需求", "Bug", "测试用例",
            "测试单", "用户",
            "产品计划", "构建", "发布", "业务需求", "用户需求", "应用",
        })

    def test_entities_that_do_not_exist_on_this_build_are_absent(self):
        """``工单``/``反馈``/``文件`` 在本版禅道上 POST 回 0 字节（路由不存在）——
        不建实体映射。**建了只会让人以为能用。**"""
        for name in ("工单", "反馈", "文件", "史诗"):
            self.assertNotIn(name, ENTITY_SPECS, name)
        # 但「史诗」作为别名指向「业务需求」是可以的（敏捷术语的对译）
        self.assertEqual(ALIASES.get("史诗"), "业务需求")

    def test_aliases_all_point_at_real_entities(self):
        """别名指向不存在的实体 → 调用方拿到的是一个指不到病根的 KeyError。"""
        for alias, target in ALIASES.items():
            self.assertIn(target, ENTITY_SPECS, "%s → %s" % (alias, target))

    def test_every_not_listable_entity_says_why(self):
        for name, spec in ENTITY_SPECS.items():
            if not spec.get("listable"):
                self.assertTrue(spec.get("not_listable_why"),
                                "%s 没写 not_listable_why" % name)

    def test_ticket_and_feedback_are_not_claimed_anywhere(self):
        """反向断言：适配器**不许**在任何地方悄悄声称支持工单/反馈。"""
        from adapters.zentao import entity_tables as et
        joined = " ".join(et()) + " " + " ".join(ALIASES)
        for name in ("工单", "反馈"):
            self.assertNotIn(name, joined, name)


if __name__ == "__main__":
    unittest.main(verbosity=2)
