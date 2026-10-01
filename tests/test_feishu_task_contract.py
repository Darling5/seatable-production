# -*- coding: utf-8 -*-
"""飞书任务（Task）适配器契约测试。

**全部离线**：用一个记录型假传输层替代 ``_TaskCli``，不碰网络、不碰任何真实清单。
所有断言对应的是**实测确认过的协议事实**（见 ``adapters/feishu_task.py`` 顶部
docstring 与 ``probes/feishu_task_*.py`` 四轮探测），不是照文档猜的。

重点钉住的是**这个后端特有的静默失败面**：
  ① ``tasks.patch`` 的 ``update_fields`` 是**固定 14 项白名单**，``members`` 不在其中
     → 成员必须走 ``members add/remove``，走 patch 一定失败；
  ② ``update_fields`` 列了就必须给非空值，body 里有而名单里没有的字段会被**静默忽略**
     → 适配器让两者同源，从结构上不可能不一致；
  ③ 列表接口 ``tasklists.tasks`` 只回 **7 个摘要字段**，拿它当行会让所有详情列
     看起来「本来就是空的」 → ``list_rows`` 必须逐条补详情；
  ④ 内置列与自定义字段**可能同名**（本机真有一个叫「状态」的自定义字段）
     → 静默覆盖会让列名不变而值换了来源。
"""
import json
import os
import sys
import unittest
from unittest.mock import patch

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

from adapters import factory, schema  # noqa: E402
from adapters.base import (CAP_BATCH_WRITE, CAP_DELETE, CAP_LINK,  # noqa: E402
                           CAP_LINK_READ, CAP_QUERY_PUSHDOWN, CAP_READ,
                           CAP_SCHEMA_MANAGE, CAP_SERVER_ROW_ID, CAP_UPDATE,
                           CAP_WRITE, Unsupported, capabilities_of)
from adapters.feishu import FeishuError  # noqa: E402
from adapters.feishu_task import (_BUILTIN_COLUMNS, _HIGH_RISK, _PATCHABLE,  # noqa: E402
                                  _VALUE_KEY, FeishuTaskAdapter, _TaskCli)


def _raise_feishu(code):
    """让假传输层抛一个带指定 code 的 FeishuError（模拟服务端错误码）。"""
    raise FeishuError("模拟错误 code=%s" % code, code=code)

LISTS = {"项目": "L-proj", "付款": "L-pay"}
CF = {"L-proj": [
    {"guid": "cf-pri", "name": "优先级", "type": "single_select",
     "single_select_setting": {"options": [
         {"name": "高", "guid": "opt-hi"}, {"name": "低", "guid": "opt-lo"}]}},
    {"guid": "cf-budget", "name": "项目预算", "type": "number"},
    {"guid": "cf-people", "name": "配合人员", "type": "member",
     "member_setting": {"multi": False}},
    # ⚠️ 与内置列「状态」同名 —— 真实数据里就是这么撞的
    {"guid": "cf-state", "name": "状态", "type": "single_select",
     "single_select_setting": {"options": [
         {"name": "进行中", "guid": "opt-doing"},
         {"name": "已完成", "guid": "opt-done"}]}},
]}


class _FakeTransport:
    """按 ``cmd`` 路由的假传输层；记录每一次 (cmd, args, body)。"""

    def __init__(self, lists=None, cf=None, tasks=None, brief=None):
        self.lists = dict(LISTS if lists is None else lists)
        self.cf = dict(CF if cf is None else cf)
        self.tasks = dict(tasks if tasks is None else tasks)   # guid -> task dict
        self.brief = brief                                     # 列表接口的摘要项
        self.calls = []
        self.overrides = {}                                    # cmd -> 值或 callable

    def call(self, cmd, args, body=None, timeout=None):
        self.calls.append((cmd, list(args), body))
        ov = self.overrides.get(cmd)
        if callable(ov):
            return ov(args, body)
        if ov is not None:
            return ov
        if cmd == "tasklists.list":
            return {"items": [{"guid": g, "name": n} for n, g in self.lists.items()]}
        if cmd == "tasklists.tasks":
            guid = args[args.index("--tasklist-guid") + 1]
            items = self.brief
            if items is None:
                items = [{"guid": g, "summary": t.get("summary"),
                          "completed_at": t.get("completed_at", "0"),
                          "members": t.get("members", [])}
                         for g, t in self.tasks.items()
                         if guid in [x.get("tasklist_guid")
                                     for x in (t.get("tasklists") or [])]]
            return {"items": items, "has_more": False, "page_token": None}
        if cmd == "tasks.get":
            guid = args[args.index("--task-guid") + 1]
            t = self.tasks.get(guid)
            return {"task": t} if t else {}
        if cmd == "custom_fields.list":
            rid = args[args.index("--resource-id") + 1]
            return {"items": self.cf.get(rid, [])}
        if cmd == "tasks.create":
            guid = "T-new"
            self.tasks[guid] = dict(body or {}, guid=guid,
                                    tasklists=body.get("tasklists"))
            return {"task": {"guid": guid}}
        if cmd == "tasks.patch":
            return {"task": {"guid": args[args.index("--task-guid") + 1]}}
        if cmd in ("members.add", "members.remove"):
            return {"task": {"guid": args[args.index("--task-guid") + 1]}}
        if cmd == "tasks.delete":
            return {}
        if cmd == "tasklists.create":
            return {"tasklist": {"guid": "L-new", "name": (body or {}).get("name")}}
        if cmd == "tasklists.patch":
            return {"tasklist": {"guid": "L-proj"}}
        if cmd == "custom_fields.create":
            return {"custom_field": {"guid": "cf-new",
                                     "name": (body or {}).get("name")}}
        if cmd == "sections.list":
            return {"items": [{"guid": "S1", "name": "研发", "is_default": False}]}
        if cmd == "subtasks.list":
            return {"items": [{"guid": "ST1", "summary": "子任务"}]}
        return {}

    def of(self, cmd):
        return [c for c in self.calls if c[0] == cmd]


def _task(guid="T1", **kw):
    t = {"guid": guid, "summary": "写文档", "description": "详细描述",
         "completed_at": "0", "status": "todo", "is_milestone": False,
         "members": [{"id": "ou_a", "role": "assignee", "type": "user"},
                     {"id": "ou_b", "role": "follower", "type": "user"}],
         "tasklists": [{"tasklist_guid": "L-proj"}],
         "subtask_count": 2, "created_at": "1767856959370",
         "updated_at": "1767856968853", "url": "https://x/1",
         "parent_task_guid": "", "dependencies": [], "custom_fields": [],
         "due": {"timestamp": "1767916800000", "is_all_day": True},
         "start": {"timestamp": "1767657600000", "is_all_day": True}}
    t.update(kw)
    return t


def _adapter(lists=None, cf=None, tasks=None, brief=None):
    a = FeishuTaskAdapter()
    a._t = _FakeTransport(lists, cf, tasks if tasks is not None
                          else {"T1": _task()}, brief)
    return a


# ══════════════════════════════════════════════════════════════
# 1. 注册与能力声明
# ══════════════════════════════════════════════════════════════

class RegistrationTests(unittest.TestCase):
    def test_backend_is_registered(self):
        self.assertIn("feishu_task", factory.list_backends())

    def test_registry_entry_is_explicit(self):
        info = factory.BACKENDS["feishu_task"]
        self.assertEqual(info["label"], "飞书任务")
        # 飞书任务是账号级的：清单是方法参数，不是「命名实例」
        self.assertEqual(info["required_keys"], ())
        self.assertFalse(info["named_bases"])
        self.assertIn("verify", info["deploy"])

    def test_capabilities_exclude_what_it_cannot_do(self):
        caps = FeishuTaskAdapter().capabilities()
        for c in (CAP_READ, CAP_WRITE, CAP_UPDATE, CAP_DELETE,
                  CAP_SCHEMA_MANAGE, CAP_SERVER_ROW_ID):
            self.assertIn(c, caps)
        # 任务没有批量创建接口 → 不许声明 batch_write
        self.assertNotIn(CAP_BATCH_WRITE, caps)
        # 没有关联列模型 → 一律不声明（否则上层会以为 link() 可用）
        self.assertNotIn(CAP_LINK, caps)
        self.assertNotIn(CAP_LINK_READ, caps)
        self.assertNotIn(CAP_QUERY_PUSHDOWN, caps)

    def test_capabilities_are_readable_before_auth(self):
        caps = capabilities_of(FeishuTaskAdapter())
        self.assertIn(CAP_READ, caps)

    def test_factory_can_build_it_without_any_config(self):
        a = factory.get_adapter({"backend": "feishu_task"})
        self.assertEqual(a.backend, "feishu_task")

    def test_factory_shares_cli_path_and_identity_with_feishu_section(self):
        a = factory.get_adapter({"backend": "feishu_task",
                                 "feishu": {"cli_path": "C:/x/lark-cli.exe",
                                            "identity": "user"}})
        self.assertEqual(a.cli_path, "C:/x/lark-cli.exe")
        self.assertEqual(a.identity, "user")

    def test_feishu_task_section_overrides_feishu_section(self):
        a = factory.get_adapter({"backend": "feishu_task",
                                 "feishu": {"cli_path": "A"},
                                 "feishu_task": {"cli_path": "B"}})
        self.assertEqual(a.cli_path, "B")


# ══════════════════════════════════════════════════════════════
# 2. 列：内置 + 自定义 + 重名规则
# ══════════════════════════════════════════════════════════════

class ColumnTests(unittest.TestCase):
    def test_metadata_has_builtin_and_custom_columns(self):
        cols = {c["name"] for c in _adapter().get_metadata("项目")["columns"]}
        for n, _t, _k, _w in _BUILTIN_COLUMNS:
            self.assertIn(n, cols)
        self.assertIn("优先级", cols)
        self.assertIn("项目预算", cols)

    def test_builtin_column_collision_renames_the_custom_one(self):
        """⚠️ 真实数据里「状态」既是内置列、又是自定义字段 —— 不许静默覆盖。"""
        md = _adapter().get_metadata("项目")
        by = {c["name"]: c for c in md["columns"]}
        self.assertIn("状态", by)
        self.assertIn("状态(自定义)", by)
        # 内置那个仍是 status、只读；自定义那个是 select、可写且带选项
        self.assertEqual(by["状态"]["key"], "status")
        self.assertFalse(by["状态"]["writable"])
        self.assertNotIn("custom", by["状态"])
        self.assertTrue(by["状态(自定义)"]["custom"])
        self.assertEqual(by["状态(自定义)"]["key"], "cf-state")
        self.assertEqual(by["状态(自定义)"]["origin_name"], "状态")
        self.assertEqual(by["状态(自定义)"]["options"], ["进行中", "已完成"])

    def test_collision_after_rename_raises_instead_of_stack_suffixes(self):
        """两个自定义字段本身就重名 → 按名字取值歧义，必须抛而不是编后缀。"""
        cf = {"L-proj": [
            {"guid": "cf-a", "name": "状态", "type": "text"},
            {"guid": "cf-b", "name": "状态(自定义)", "type": "text"}]}
        with self.assertRaises(KeyError) as cm:
            _adapter(cf=cf).get_metadata("项目")
        self.assertIn("歧义", str(cm.exception))

    def test_two_custom_fields_with_the_same_name_raise(self):
        cf = {"L-proj": [{"guid": "cf-a", "name": "同名字段", "type": "text"},
                         {"guid": "cf-b", "name": "同名字段", "type": "text"}]}
        with self.assertRaises(KeyError) as cm:
            _adapter(cf=cf).get_metadata("项目")
        self.assertIn("歧义", str(cm.exception))

    def test_readonly_columns_are_marked(self):
        by = {c["name"]: c for c in _adapter().get_metadata("项目")["columns"]}
        for n in ("状态", "前置任务", "父任务", "子任务数", "创建时间",
                  "更新时间", "任务链接"):
            self.assertFalse(by[n]["writable"], n)
        for n in ("摘要", "描述", "截止时间", "负责人", "里程碑"):
            self.assertTrue(by[n]["writable"], n)

    def test_unknown_custom_field_type_is_refused_not_guessed(self):
        cf = {"L-proj": [{"guid": "cf-x", "name": "怪字段", "type": "unknown_kind"}]}
        with self.assertRaises(Unsupported) as cm:
            _adapter(cf=cf).get_metadata("项目")
        self.assertIn("未实测", str(cm.exception))

    def test_table_can_be_guid_or_name(self):
        a = _adapter()
        self.assertEqual(a.get_metadata("项目")["table_guid"], "L-proj")
        self.assertEqual(a.get_metadata("L-proj")["table_guid"], "L-proj")

    def test_unknown_table_lists_available_ones(self):
        with self.assertRaises(KeyError) as cm:
            _adapter().get_metadata("不存在的清单")
        msg = str(cm.exception)
        self.assertIn("项目", msg)
        self.assertIn("付款", msg)

    def test_empty_table_name_is_refused(self):
        with self.assertRaises(ValueError):
            _adapter().get_metadata("")

    def test_table_exists_does_not_raise(self):
        a = _adapter()
        self.assertTrue(a.table_exists("项目"))
        self.assertFalse(a.table_exists("没有这个"))

    def test_list_tables_returns_names(self):
        self.assertEqual(_adapter().list_tables(), ["付款", "项目"])


# ══════════════════════════════════════════════════════════════
# 3. 读：形状与「不许静默留空」
# ══════════════════════════════════════════════════════════════

class ReadTests(unittest.TestCase):
    def test_completed_flag_comes_from_completed_at_not_from_a_bool(self):
        a = _adapter(tasks={"T1": _task(completed_at="0")})
        self.assertFalse(a.get_row("项目", "T1")["完成状态"])
        a = _adapter(tasks={"T1": _task(completed_at="1790819225000",
                                        status="done")})
        self.assertTrue(a.get_row("项目", "T1")["完成状态"])

    def test_dates_are_flattened_to_yyyy_mm_dd(self):
        r = _adapter().get_row("项目", "T1")
        self.assertEqual(r["截止时间"], "2026-01-09")
        self.assertEqual(r["开始时间"], "2026-01-06")
        self.assertEqual(r["创建时间"], "2026-01-08")
        self.assertEqual(r["完成时间"], "")          # completed_at == "0" → 空，不是 1970

    def test_members_are_split_by_role_into_id_lists(self):
        r = _adapter().get_row("项目", "T1")
        self.assertEqual(r["负责人"], ["ou_a"])
        self.assertEqual(r["关注人"], ["ou_b"])

    def test_empty_values_are_empty_not_none(self):
        r = _adapter().get_row("项目", "T1")
        self.assertEqual(r["父任务"], "")
        self.assertEqual(r["前置任务"], [])

    def test_absent_description_is_empty_string_not_none(self):
        a = _adapter(tasks={"T1": _task(description=None)})
        self.assertEqual(a.get_row("项目", "T1")["描述"], "")

    def test_missing_task_returns_none(self):
        self.assertIsNone(_adapter().get_row("项目", "没有这个"))

    def test_not_found_error_is_translated_to_none(self):
        """⚠️ 实测：tasks.get 对不存在的 guid **抛 1470404**，不是回空 data。
        契约要求 get_row 找不到返回 None —— 必须把这个错误码翻译掉。

        这一条是**真实验证时才发现**的：离线 mock 回空 data，掩盖了真实行为。
        """
        a = _adapter()
        a._t.overrides["tasks.get"] = lambda args, body: _raise_feishu(1470404)
        self.assertIsNone(a.get_row("项目", "T-ghost"))

    def test_other_errors_from_get_are_not_swallowed(self):
        """不能把所有失败都当成「没有」—— 权限错、限流等必须照常抛。"""
        a = _adapter()
        a._t.overrides["tasks.get"] = lambda args, body: _raise_feishu(999999)
        with self.assertRaises(FeishuError):
            a.get_row("项目", "T1")

    def test_list_rows_translates_not_found_into_a_clear_error(self):
        """列表说有一行、取详情时已消失 —— 不静默跳过，报明确的错。"""
        a = _adapter(brief=[{"guid": "T-ghost", "summary": "幽灵"}])
        a._t.overrides["tasks.get"] = lambda args, body: _raise_feishu(1470404)
        with self.assertRaises(FeishuError) as cm:
            a.list_rows("项目")
        self.assertIn("取不到", str(cm.exception))

    def test_all_advertised_columns_are_present_on_every_row(self):
        """get_metadata 声明的列，list_rows 的行里必须**都有键**。

        否则「任务没填」会表现成「键不存在」，而「键不存在」在别处又可能意味着
        「没取」—— 两者必须区分得开。
        """
        a = _adapter()
        names = [c["name"] for c in a.get_metadata("项目")["columns"]]
        row = a.list_rows("项目")[0]
        for n in names:
            self.assertIn(n, row, n)

    def test_custom_member_field_is_flattened_to_ids(self):
        t = _task(custom_fields=[{"guid": "cf-people", "name": "配合人员",
                                  "type": "member",
                                  "member_value": [{"id": "ou_p", "type": "user"}]}])
        r = _adapter(tasks={"T1": t}).get_row("项目", "T1")
        self.assertEqual(r["配合人员"], ["ou_p"])

    def test_custom_select_is_translated_to_option_name(self):
        t = _task(custom_fields=[{"guid": "cf-pri", "name": "优先级",
                                  "type": "single_select",
                                  "single_select_value": "opt-hi"}])
        r = _adapter(tasks={"T1": t}).get_row("项目", "T1")
        self.assertEqual(r["优先级"], "高")

    def test_untranslatable_option_guid_is_visibly_marked(self):
        """选项被删/隐藏时翻不出名字 —— 必须看得出来是「没翻译出来」。"""
        t = _task(custom_fields=[{"guid": "cf-pri", "name": "优先级",
                                  "type": "single_select",
                                  "single_select_value": "opt-ghost"}])
        r = _adapter(tasks={"T1": t}).get_row("项目", "T1")
        self.assertEqual(r["优先级"], "?opt-ghost")

    def test_custom_field_absent_from_task_is_empty_not_missing(self):
        r = _adapter().get_row("项目", "T1")
        self.assertEqual(r["优先级"], "")
        self.assertEqual(r["项目预算"], "")

    def test_list_rows_fetches_detail_for_every_row(self):
        """列表接口只有 7 个字段 → 必须逐条补详情（N+1 次调用）。"""
        tasks = {"T1": _task("T1"), "T2": _task("T2"), "T3": _task("T3")}
        a = _adapter(tasks=tasks)
        rows = a.list_rows("项目")
        self.assertEqual(len(rows), 3)
        self.assertEqual(len(a._t.of("tasks.get")), 3)
        # 详情字段真的取到了（列表接口给不出 description）
        for r in rows:
            self.assertEqual(r["描述"], "详细描述")

    def test_list_rows_refuses_to_silently_drop_a_row_it_cannot_fetch(self):
        """列表说有一行、详情取不到 —— 不许静默跳过（那会少数据且不报错）。"""
        a = _adapter(brief=[{"guid": "T-ghost", "summary": "幽灵"}])
        with self.assertRaises(FeishuError) as cm:
            a.list_rows("项目")
        self.assertIn("没回 task 对象", str(cm.exception))

    def test_list_rows_brief_is_one_call_and_omits_detail_columns(self):
        a = _adapter(brief=[{"guid": "T1", "summary": "写文档",
                             "completed_at": "0", "members": [], "due": None,
                             "start": None, "subtask_count": 1}])
        rows = a.list_rows_brief("项目")
        self.assertEqual(len(a._t.of("tasks.get")), 0)
        self.assertEqual(len(a._t.of("tasklists.tasks")), 1)
        self.assertEqual(rows[0]["摘要"], "写文档")
        # 缺的列**键不存在**，而不是给个空占位（与 list_rows 的「齐全但慢」区分）
        self.assertNotIn("描述", rows[0])
        self.assertNotIn("状态", rows[0])


# ══════════════════════════════════════════════════════════════
# 4. 写：patch 白名单 / members 走另一条路 / 孤儿防护
# ══════════════════════════════════════════════════════════════

class WriteTests(unittest.TestCase):
    def test_append_row_requires_a_summary(self):
        with self.assertRaises(ValueError) as cm:
            _adapter().append_row("项目", {"描述": "没有摘要"})
        self.assertIn("摘要", str(cm.exception))

    def test_append_row_forces_the_tasklist_so_no_orphan(self):
        """实测：不带 tasklists 也能建成 → 任务变成谁也找不到的孤儿。必须强制。"""
        a = _adapter()
        a.append_row("项目", {"摘要": "新任务"})
        (_cmd, _args, body), = a._t.of("tasks.create")
        self.assertEqual(body["tasklists"], [{"tasklist_guid": "L-proj"}])

    def test_append_row_returns_guid_from_the_wrapped_key(self):
        """实测：回的是 data.task.guid，**不是** data.guid。"""
        self.assertEqual(_adapter().append_row("项目", {"摘要": "x"}), "T-new")

    def test_append_row_refuses_unknown_column(self):
        with self.assertRaises(ValueError) as cm:
            _adapter().append_row("项目", {"摘要": "x", "不存在的列": 1})
        self.assertIn("不存在的列", str(cm.exception))

    def test_append_row_records_skipped_readonly_columns(self):
        a = _adapter()
        a.append_row("项目", {"摘要": "x", "子任务数": 9, "任务链接": "http://y"})
        self.assertEqual(sorted(a.last_skipped), ["任务链接", "子任务数"])
        (_c, _a, body), = a._t.of("tasks.create")
        self.assertNotIn("subtask_count", body)
        self.assertNotIn("url", body)

    def test_append_row_maps_select_by_option_name_to_guid(self):
        """实测：单选必须给选项 guid，给名字会被拒；对外仍用名字。"""
        a = _adapter()
        a.append_row("项目", {"摘要": "x", "优先级": "高"})
        (_c, _a, body), = a._t.of("tasks.create")
        self.assertEqual(body["custom_fields"],
                         [{"guid": "cf-pri", "single_select_value": "opt-hi"}])

    def test_unknown_option_is_refused_with_the_valid_ones_listed(self):
        with self.assertRaises(ValueError) as cm:
            _adapter().append_row("项目", {"摘要": "x", "优先级": "超高"})
        msg = str(cm.exception)
        self.assertIn("超高", msg)
        self.assertIn("高", msg)
        self.assertIn("低", msg)

    def test_append_row_maps_member_custom_field_to_member_value(self):
        a = _adapter()
        a.append_row("项目", {"摘要": "x", "配合人员": ["ou_p"]})
        (_c, _a, body), = a._t.of("tasks.create")
        self.assertEqual(body["custom_fields"],
                         [{"guid": "cf-people",
                           "member_value": [{"id": "ou_p", "type": "user"}]}])

    def test_append_row_maps_follower_to_member_role(self):
        """新建任务时「关注人」→ members 里 role=follower。

        ⚠️ 这条是**反向验证抓出来的缺口**：原版只覆盖了「负责人」，
        `append_row` 里 `@follower` 那两行改了也全绿 —— 等于没测。
        """
        a = _adapter()
        a.append_row("项目", {"摘要": "新任务",
                              "负责人": ["ou_a"], "关注人": ["ou_b"]})
        (_c, _a, body), = a._t.of("tasks.create")
        self.assertEqual(sorted((m["role"], m["id"]) for m in body["members"]),
                         [("assignee", "ou_a"), ("follower", "ou_b")])

    def test_append_row_translates_dates_to_server_cell(self):
        a = _adapter()
        a.append_row("项目", {"摘要": "x", "截止时间": "2026-03-05"})
        (_c, _a, body), = a._t.of("tasks.create")
        self.assertEqual(body["due"]["is_all_day"], True)
        self.assertTrue(int(body["due"]["timestamp"]) > 0)

    def test_update_row_keeps_body_and_update_fields_in_sync(self):
        """⚠️ 实测的两个坑（列了不给值 → 整条失败；给了没列 → 静默忽略）
        在这里从结构上不可能发生：两者由同一个 dict 派生。"""
        a = _adapter()
        a.update_row("项目", "T1", {"摘要": "改后", "描述": "新描述"})
        (_c, _a, body), = a._t.of("tasks.patch")
        self.assertEqual(sorted(body["update_fields"]), sorted(body["task"]))
        self.assertEqual(set(body["update_fields"]) <= _PATCHABLE, True)

    def test_update_row_never_sends_members_through_patch(self):
        """实测：members **不在** update_fields 白名单里，走 patch 必失败。"""
        a = _adapter()
        a.update_row("项目", "T1", {"负责人": ["ou_new"]})

    def test_update_row_sends_follower_through_members_not_patch(self):
        """「关注人」必须和「负责人」走同一条 members 通道。

        ⚠️ 这条是**反向验证抓出来的缺口**：原版这条路径只测了「负责人」，
        把 `_to_body` 里 `@follower` 分支偷偷改成写进 ``patch["members"]``，
        测试依然全绿 —— 而实测里 patch 体带 members 会被服务端直接拒。
        """
        a = _adapter()
        a.update_row("项目", "T1", {"关注人": ["ou_new"]})
        self.assertEqual(a._t.of("tasks.patch"), [])
        (cmd, _args, body), = a._t.of("members.add")
        self.assertEqual(cmd, "members.add")
        self.assertEqual(body["members"],
                         [{"id": "ou_new", "role": "follower", "type": "user"}])
        self.assertEqual(a._t.of("tasks.patch"), [])
        (cmd, _args, body), = a._t.of("members.add")
        self.assertEqual(cmd, "members.add")
        self.assertEqual(body["members"],
                         [{"id": "ou_new", "role": "follower", "type": "user"}])

    def test_update_row_removes_members_no_longer_wanted(self):
        a = _adapter()
        a.update_row("项目", "T1", {"负责人": []})       # 清空负责人
        (_c, _a, body), = a._t.of("members.remove")
        self.assertEqual(body["members"],
                         [{"id": "ou_a", "role": "assignee", "type": "user"}])

    def test_update_row_does_not_touch_roles_it_was_not_given(self):
        """只传负责人时，关注人**不动** —— 「没提到」不等于「清空」。"""
        a = _adapter()
        a.update_row("项目", "T1", {"负责人": ["ou_a"]})
        self.assertEqual(a._t.of("members.remove"), [])
        self.assertEqual(a._t.of("members.add"), [])

    def test_update_row_sends_both_add_and_remove_when_replacing(self):
        a = _adapter()
        a.update_row("项目", "T1", {"负责人": ["ou_new"]})
        self.assertEqual(len(a._t.of("members.add")), 1)
        self.assertEqual(len(a._t.of("members.remove")), 1)

    def test_update_row_replaces_followers_only(self):
        """T1 现有 assignee=ou_a / follower=ou_b，只改关注人 → **不能碰 assignee**。"""
        a = _adapter()
        a.update_row("项目", "T1", {"关注人": ["ou_new"]})
        added = [m for (_c, _a, b) in a._t.of("members.add") for m in b["members"]]
        removed = [m for (_c, _a, b) in a._t.of("members.remove") for m in b["members"]]
        self.assertEqual(added, [{"id": "ou_new", "role": "follower", "type": "user"}])
        self.assertEqual(removed, [{"id": "ou_b", "role": "follower", "type": "user"}])

    def test_update_row_can_set_both_roles_at_once(self):
        a = _adapter()
        a.update_row("项目", "T1", {"负责人": ["ou_a"], "关注人": ["ou_b", "ou_c"]})
        added = [m for (_c, _a, b) in a._t.of("members.add") for m in b["members"]]
        self.assertEqual(added, [{"id": "ou_c", "role": "follower", "type": "user"}])
        self.assertEqual(a._t.of("members.remove"), [])   # 都是已有成员，无需摘
        self.assertEqual(a._t.of("tasks.patch"), [])      # 成员改动一条 patch 都不发

    def test_update_row_skips_readonly_and_records_it(self):
        a = _adapter()
        a.update_row("项目", "T1", {"摘要": "改后", "子任务数": 5})
        self.assertEqual(a.last_skipped, ["子任务数"])
        (_c, _a, body), = a._t.of("tasks.patch")
        self.assertEqual(body["task"], {"summary": "改后"})

    def test_update_row_with_only_readonly_columns_makes_no_call(self):
        a = _adapter()
        a.update_row("项目", "T1", {"子任务数": 5})
        self.assertEqual(a._t.of("tasks.patch"), [])
        self.assertEqual(a._t.of("members.add"), [])

    def test_update_row_refuses_unknown_column(self):
        with self.assertRaises(ValueError):
            _adapter().update_row("项目", "T1", {"没有这列": 1})

    def test_update_row_can_complete_and_uncomplete(self):
        a = _adapter()
        a.update_row("项目", "T1", {"完成状态": True})
        (_c, _a, body), = a._t.of("tasks.patch")
        self.assertEqual(sorted(body["update_fields"]), ["completed_at"])
        self.assertTrue(int(body["task"]["completed_at"]) > 0)

        b = _adapter()
        b.update_row("项目", "T1", {"完成状态": False})
        (_c, _a, body), = b._t.of("tasks.patch")
        self.assertEqual(body["task"]["completed_at"], "0")

    def test_update_row_never_emits_a_field_outside_the_whitelist(self):
        """把所有可写列都写一遍，断言发出去的键全在白名单里。"""
        a = _adapter()
        a.update_row("项目", "T1", {
            "摘要": "s", "描述": "d", "截止时间": "2026-03-05",
            "开始时间": "2026-03-01", "里程碑": True, "完成状态": True,
            "优先级": "低", "项目预算": "500", "配合人员": ["ou_p"]})
        (_c, _a, body), = a._t.of("tasks.patch")
        self.assertEqual(set(body["update_fields"]) - _PATCHABLE, set())

    def test_delete_rows_calls_delete_once_per_row(self):
        a = _adapter()
        a.delete_rows("项目", ["T1", "T2"])
        got = [c[1][c[1].index("--task-guid") + 1] for c in a._t.of("tasks.delete")]
        self.assertEqual(got, ["T1", "T2"])

    def test_delete_rows_ignores_empty_ids(self):
        a = _adapter()
        a.delete_rows("项目", ["", None, "T1"])
        self.assertEqual(len(a._t.of("tasks.delete")), 1)


# ══════════════════════════════════════════════════════════════
# 5. 关联：如实说做不到
# ══════════════════════════════════════════════════════════════

class LinkTests(unittest.TestCase):
    def test_link_raises_unsupported(self):
        with self.assertRaises(Unsupported):
            _adapter().link("项目", "付款", "某关联", "T1", ["T2"])

    def test_list_linked_raises_unsupported_instead_of_returning_empty(self):
        """返回空列表会与「真的没有关联」无法区分 —— 必须抛。"""
        with self.assertRaises(Unsupported):
            _adapter().list_linked("项目", "T1", "")

    def test_link_errors_explain_why(self):
        try:
            _adapter().link("项目", "付款", "x", "T1", [])
        except Unsupported as e:
            self.assertIn("dependencies", str(e))
        else:
            self.fail("应当抛 Unsupported")


# ══════════════════════════════════════════════════════════════
# 6. 表结构
# ══════════════════════════════════════════════════════════════

class EnsureTableTests(unittest.TestCase):
    def test_existing_list_returns_exists_without_creating(self):
        a = _adapter()
        self.assertEqual(a.ensure_table("项目"), "exists")
        self.assertEqual(a._t.of("tasklists.create"), [])

    def test_new_list_is_created(self):
        a = _adapter()
        self.assertEqual(a.ensure_table("新清单"), "created")
        (_c, _a, body), = a._t.of("tasklists.create")
        self.assertEqual(body["name"], "新清单")

    def test_builtin_columns_are_not_recreated_as_custom_fields(self):
        a = _adapter()
        a.ensure_table("新清单", [{"name": "摘要", "type": "text"},
                                  {"name": "自定义列", "type": "text"}])
        got = [c[2]["name"] for c in a._t.of("custom_fields.create")]
        self.assertEqual(got, ["自定义列"])

    def test_select_column_carries_its_options(self):
        a = _adapter()
        a.ensure_table("新清单", [{"name": "阶段", "type": "select",
                                   "options": ["甲", "乙"]}])
        (_c, _a, body), = a._t.of("custom_fields.create")
        self.assertEqual(body["type"], "single_select")
        self.assertEqual(body["single_select_setting"]["options"],
                         [{"name": "甲"}, {"name": "乙"}])

    def test_number_column_without_symbol_does_not_use_custom_format(self):
        """实测：format='custom' 时 custom_symbol 必填，否则直接 1470400。"""
        a = _adapter()
        a.ensure_table("新清单", [{"name": "数量", "type": "number"}])
        (_c, _a, body), = a._t.of("custom_fields.create")
        self.assertEqual(body["number_setting"]["format"], "plain")

    def test_number_column_with_symbol_uses_custom_format(self):
        a = _adapter()
        a.ensure_table("新清单", [{"name": "工时", "type": "number",
                                   "symbol": "人天"}])
        (_c, _a, body), = a._t.of("custom_fields.create")
        self.assertEqual(body["number_setting"]["format"], "custom")
        self.assertEqual(body["number_setting"]["custom_symbol"], "人天")

    def test_bool_column_is_refused_not_silently_downgraded(self):
        """任务自定义字段没有布尔类型 —— 写成 text 会造出语义错误的列。"""
        with self.assertRaises(Unsupported):
            _adapter().ensure_table("新清单", [{"name": "已完成", "type": "bool"}])


# ══════════════════════════════════════════════════════════════
# 7. 传输层：--yes 与信封解析
# ══════════════════════════════════════════════════════════════

class TransportTests(unittest.TestCase):
    def test_high_risk_commands_are_exactly_the_measured_ones(self):
        self.assertEqual(sorted(_HIGH_RISK),
                         sorted(["custom_fields.remove", "sections.delete",
                                 "tasks.delete", "tasklists.delete"]))

    def test_high_risk_command_gets_yes_appended(self):
        """实测：不加 --yes 时服务端什么都没做，CLI 回 rc=10 —— 不算成功。"""
        seen = {}

        def fake_run(cmd, **kw):
            seen["cmd"] = list(cmd)

            class _R:
                returncode = 0
                stdout = json.dumps({"ok": True, "data": {}})
                stderr = ""
            return _R()

        t = _TaskCli("lark-cli", identity="user")
        with patch("adapters.feishu_task.subprocess.run", side_effect=fake_run):
            t.call("tasks.delete", ["tasks", "delete", "--task-guid", "T1"])
        self.assertIn("--yes", seen["cmd"])

    def test_normal_write_does_not_get_yes(self):
        seen = {}

        def fake_run(cmd, **kw):
            seen["cmd"] = list(cmd)

            class _R:
                returncode = 0
                stdout = json.dumps({"ok": True, "data": {}})
                stderr = ""
            return _R()

        t = _TaskCli("lark-cli", identity="user")
        with patch("adapters.feishu_task.subprocess.run", side_effect=fake_run):
            t.call("tasks.patch", ["tasks", "patch", "--task-guid", "T1"])
        self.assertNotIn("--yes", seen["cmd"])

    def test_confirmation_error_surfaces_the_actual_reason(self):
        def fake_run(cmd, **kw):
            class _R:
                returncode = 10
                stdout = ""
                stderr = json.dumps({"ok": False, "error": {
                    "type": "confirmation",
                    "message": "task.tasks.delete requires confirmation"}})
            return _R()

        t = _TaskCli("lark-cli", identity="user")
        with patch("adapters.feishu_task.subprocess.run", side_effect=fake_run):
            with self.assertRaises(Exception) as cm:
                t.call("tasks.delete", ["tasks", "delete", "--task-guid", "T1"])
        self.assertIn("requires confirmation", str(cm.exception))

    def test_identity_and_domain_are_passed_as_argv_not_shell(self):
        """argv 数组直给 CreateProcessW：中文摘要不会被引号/编码折腾。"""
        seen = {}

        def fake_run(cmd, **kw):
            seen["cmd"] = list(cmd)
            seen["shell"] = kw.get("shell")

            class _R:
                returncode = 0
                stdout = json.dumps({"ok": True, "data": {}})
                stderr = ""
            return _R()

        t = _TaskCli("C:/x/lark-cli.exe", identity="user")
        with patch("adapters.feishu_task.subprocess.run", side_effect=fake_run):
            t.call("tasks.get", ["tasks", "get", "--task-guid", "T1"])
        self.assertEqual(seen["cmd"][:2], ["C:/x/lark-cli.exe", "task"])
        self.assertIn("--as", seen["cmd"])
        self.assertEqual(seen["cmd"][seen["cmd"].index("--as") + 1], "user")
        self.assertNotEqual(seen["shell"], True)


# ══════════════════════════════════════════════════════════════
# 8. 值翻译的边界
# ══════════════════════════════════════════════════════════════

class ValueTranslationTests(unittest.TestCase):
    def test_ms_to_date_treats_zero_and_garbage_as_empty(self):
        self.assertEqual(FeishuTaskAdapter._ms_to_date("0"), "")
        self.assertEqual(FeishuTaskAdapter._ms_to_date(""), "")
        self.assertEqual(FeishuTaskAdapter._ms_to_date(None), "")
        self.assertEqual(FeishuTaskAdapter._ms_to_date("abc"), "")

    def test_date_cell_accepts_the_shapes_we_ourselves_emit(self):
        a = FeishuTaskAdapter()
        self.assertEqual(a._date_cell("2026-03-05")["is_all_day"], True)
        self.assertEqual(a._date_cell("2026-03-05 09:30")["is_all_day"], False)
        self.assertEqual(a._date_cell(1767916800000)["timestamp"], "1767916800000")
        self.assertEqual(
            a._date_cell({"timestamp": "123", "is_all_day": False}),
            {"timestamp": "123", "is_all_day": False})

    def test_date_cell_refuses_unparseable(self):
        with self.assertRaises(ValueError):
            FeishuTaskAdapter()._date_cell("下周三")

    def test_id_list_accepts_all_three_write_shapes(self):
        f = FeishuTaskAdapter._id_list
        self.assertEqual(f("ou_a"), ["ou_a"])
        self.assertEqual(f(["ou_a"]), ["ou_a"])
        self.assertEqual(f([{"id": "ou_a"}]), ["ou_a"])
        self.assertEqual(f(""), [])
        self.assertEqual(f(None), [])

    def test_truthy_handles_the_chinese_forms(self):
        f = FeishuTaskAdapter._truthy
        self.assertTrue(f(True))
        self.assertTrue(f("已完成"))
        self.assertFalse(f(False))
        self.assertFalse(f(""))
        self.assertFalse(f("未完成"))
        self.assertFalse(f("0"))

    def test_value_key_table_covers_every_known_type(self):
        """_CUSTOM_TYPE 里的每个中立类型都必须有值键 —— 否则读写会静默错位。"""
        from adapters.feishu_task import _CUSTOM_TYPE
        for ft, neutral in _CUSTOM_TYPE.items():
            self.assertIn(neutral, _VALUE_KEY, ft)

    def test_option_guid_accepts_a_guid_that_came_from_a_read(self):
        """读回来的是 guid，写回去必须也能用 —— 否则「读-改-写」会崩。"""
        a = _adapter()
        cmap = a._column_map("L-proj")
        self.assertEqual(a._option_guid(cmap["优先级"], "opt-hi"), "opt-hi")
        self.assertEqual(a._option_guid(cmap["优先级"], "高"), "opt-hi")


# ══════════════════════════════════════════════════════════════
# 9. 中立类型词表
# ══════════════════════════════════════════════════════════════

class SchemaTests(unittest.TestCase):
    def test_feishu_task_has_its_own_type_table(self):
        self.assertIn("feishu_task", schema.BACKEND_TYPES)
        # 与多维表格（feishu）不是一套：多选在那边是同一个 type + multiple 位
        self.assertNotEqual(schema.BACKEND_TYPES["feishu_task"],
                            schema.BACKEND_TYPES["feishu"])

    def test_multiselect_is_a_distinct_type_here(self):
        self.assertEqual(schema.backend_type("feishu_task", "multiselect"),
                         "multi_select")
        self.assertEqual(schema.backend_type("feishu_task", "select"),
                         "single_select")

    def test_unsupported_neutral_types_raise_rather_than_downgrade(self):
        for t in ("bool", "attachment"):
            with self.assertRaises((ValueError, Unsupported)):
                schema.backend_type("feishu_task", t)

    def test_config_example_documents_the_backend(self):
        """配置样例里必须能查到 feishu_task，否则用户不知道有这个后端。"""
        p = os.path.join(HERE, "config.yaml.example")
        with open(p, encoding="utf-8") as f:
            self.assertIn("feishu_task", f.read())


if __name__ == "__main__":
    unittest.main(verbosity=2)
