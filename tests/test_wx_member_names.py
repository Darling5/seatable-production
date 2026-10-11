# -*- coding: utf-8 -*-
"""test_wx_member_names.py — 微信人名取值口径（2026-10-11 修）。

## 背景：用户反馈「微信情报台人名没读对」

微信 `contact` 行有**两个**显示名字段：

  · `nick_name` —— 对方自己设的**微信昵称 / 群昵称**（`黑王子`、`Echo~`、`🔆`、`#`）
  · `remark`    —— **我方给他设的备注**（`涛哥`、`刘老师`、`何老师-差我13060`）

情报台原先取 `nick_name`，于是摘要里出现的是对方的网名而不是我方认人的备注。
实测（本机 `contact.db`，14897 行）：有备注的 637 人中 **564 人备注≠昵称**；
在 68 个关注群的 158 名去重成员里，有备注的 41 人中 **33 人（80.5%）两者不同**。

## 本文件锁三道

  [A] **口径**：`_member_display` 必须 remark 优先、nick_name 兜底；
      且 `_name_source()` 可切回 nickname（备注带随手附注时的逃生门）。
  [B] **缓存失效**：★ 这是本 bug 最阴的一环 —— `member_names.json` 是按旧口径
      落盘的，只改代码不清缓存会「最长 3 天看起来改了没用」。
      故旧格式缓存必须被**判为不可用**。缓存文件里带 `__fmt__` 标记。
  [C] **不再分叉**：`wxwatch._member_names` 必须委托给 `wechat_intake` 的实现 ——
      两处曾各写一份、都取 nick_name，是同一个根因的两个实例。

> ⚠️ 本文件一律用**中性示例名**，不写真实人名/昵称 —— 测试文件同样在去敏守卫
>   的扫描面内（`git ls-files`）。
"""

import json
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

from wx import wechat_intake as wi  # noqa: E402


class _FakeDb(object):
    """最小 wdb 替身：只提供 get_groups()。"""

    def __init__(self, groups):
        self._g = groups

    def get_groups(self):
        return self._g


class TestNameSource(unittest.TestCase):
    """[A] 取值口径。"""

    def setUp(self):
        self._orig_cfg = wi._wechat_cfg

    def tearDown(self):
        wi._wechat_cfg = self._orig_cfg

    def _set_source(self, v):
        wi._wechat_cfg = lambda: ({"sender_name_source": v} if v is not None else {})

    def test_默认取备注(self):
        self._set_source(None)
        self.assertEqual(wi._name_source(), "remark")

    def test_可切回昵称(self):
        for v in ("nickname", "nick", "昵称"):
            self._set_source(v)
            self.assertEqual(wi._name_source(), "nickname", "配置值 %r 未被识别" % v)

    def test_有备注时必须用备注(self):
        """★ 核心断言：备注存在就用备注 —— 这正是「人名读对」的定义。"""
        self._set_source(None)
        m = {"username": "u1", "nick_name": "网名甲", "remark": "真名甲"}
        self.assertEqual(wi._member_display(m), "真名甲")

    def test_无备注时退回昵称(self):
        self._set_source(None)
        m = {"username": "u1", "nick_name": "网名甲", "remark": ""}
        self.assertEqual(wi._member_display(m), "网名甲")
        self.assertEqual(
            wi._member_display({"username": "u1", "nick_name": "网名甲"}),
            "网名甲")
        self.assertEqual(
            wi._member_display({"username": "u1", "nick_name": "网名甲",
                                "remark": None}),
            "网名甲")

    def test_备注只有空白不算有备注(self):
        self._set_source(None)
        m = {"username": "u1", "nick_name": "网名甲", "remark": "   "}
        self.assertEqual(wi._member_display(m), "网名甲",
                         "全空白 remark 被当成有效备注 —— 会显示成空白人名")

    def test_切换后行为真的反转(self):
        m = {"username": "u1", "nick_name": "网名甲", "remark": "真名甲"}
        self._set_source(None)
        self.assertEqual(wi._member_display(m), "真名甲")
        self._set_source("nickname")
        self.assertEqual(wi._member_display(m), "网名甲",
                         "切到 nickname 后仍返回备注 —— 配置项形同虚设")

    def test_两个字段都没有则返回空(self):
        self._set_source(None)
        self.assertEqual(wi._member_display({"username": "u1"}), "")
        self.assertEqual(wi._member_display({}), "")
        self.assertEqual(wi._member_display(None), "")


class TestMemberNamesBuild(unittest.TestCase):
    """[A+C] 建表时真的用了备注。"""

    def setUp(self):
        self._orig_cfg = wi._wechat_cfg
        self._orig_dir = wi.INTAKE_DIR
        self._tmp = tempfile.mkdtemp(prefix="wxnames_")
        wi.INTAKE_DIR = self._tmp
        wi._wechat_cfg = lambda: {}

    def tearDown(self):
        wi._wechat_cfg = self._orig_cfg
        wi.INTAKE_DIR = self._orig_dir

    def test_建表取备注而非昵称(self):
        db = _FakeDb([{"members": [
            {"username": "u1", "nick_name": "网名甲", "remark": "真名甲"},
            {"username": "u2", "nick_name": "网名乙", "remark": ""},
        ]}])
        names = wi._wx4_member_names(db)
        self.assertEqual(names.get("u1"), "真名甲")
        self.assertEqual(names.get("u2"), "网名乙")
        self.assertNotIn(wi._NAMES_FMT_KEY, names,
                         "格式标记泄漏进了调用方看到的映射里")

    def test_没有名字的成员不入表(self):
        db = _FakeDb([{"members": [
            {"username": "u3", "nick_name": "", "remark": ""},
            {"username": "", "nick_name": "网名丙", "remark": ""},
        ]}])
        self.assertEqual(wi._wx4_member_names(db), {})


class TestNamesCacheInvalidation(unittest.TestCase):
    """[B] 缓存必须带格式标记，旧缓存必须作废。

    ★ 这条是本 bug 最容易被漏掉的一半：只改了口径、没清缓存，
      `member_names.json` 会把**按昵称算好的旧名字**继续喂给摘要，
      最长 3 天内看起来「改了完全没用」。
    """

    def setUp(self):
        self._orig_dir = wi.INTAKE_DIR
        self._orig_cfg = wi._wechat_cfg
        self._tmp = tempfile.mkdtemp(prefix="wxnames_cache_")
        wi.INTAKE_DIR = self._tmp
        wi._wechat_cfg = lambda: {}
        self.path = os.path.join(self._tmp, "member_names.json")

    def tearDown(self):
        wi.INTAKE_DIR = self._orig_dir
        wi._wechat_cfg = self._orig_cfg

    def _write_raw(self, obj):
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False)

    def test_旧格式缓存必须被判不可用(self):
        """★ 旧缓存 = 不带 `__fmt__` 的扁平 dict（按 nick_name 落盘）。"""
        self._write_raw({"u1": "网名甲"})
        self.assertIsNone(wi._names_cache_read(self.path),
                          "旧格式缓存被接受了 —— 改了口径却不生效，最长 3 天白改")

    def test_格式标记不匹配也必须作废(self):
        self._write_raw({wi._NAMES_FMT_KEY: "v1-something-old", "u1": "网名甲"})
        self.assertIsNone(wi._names_cache_read(self.path))

    def test_新格式缓存可复用(self):
        wi._names_cache_write(self.path, {"u1": "真名甲"})
        got = wi._names_cache_read(self.path)
        self.assertEqual(got, {"u1": "真名甲"})

    def test_落盘内容确实带标记(self):
        wi._names_cache_write(self.path, {"u1": "真名甲"})
        with open(self.path, encoding="utf-8") as f:
            raw = json.load(f)
        self.assertEqual(raw.get(wi._NAMES_FMT_KEY), wi._NAMES_FMT,
                         "缓存没写格式标记 —— 下次改口径又会静默失效")
        self.assertEqual(raw.get("u1"), "真名甲")

    def test_过期缓存判不可用(self):
        wi._names_cache_write(self.path, {"u1": "真名甲"})
        old = __import__("time").time() - 86400 * 30
        os.utime(self.path, (old, old))
        self.assertIsNone(wi._names_cache_read(self.path))

    def test_缓存损坏不得抛异常(self):
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("{不是合法 json")
        self.assertIsNone(wi._names_cache_read(self.path))

    def test_缓存不可用时会重建为新口径(self):
        """端到端：旧缓存存在 → 仍须重建出备注版。"""
        self._write_raw({"u1": "网名甲"})          # 旧口径落盘
        db = _FakeDb([{"members": [
            {"username": "u1", "nick_name": "网名甲", "remark": "真名甲"}]}])
        names = wi._wx4_member_names(db)
        self.assertEqual(names.get("u1"), "真名甲",
                         "旧缓存把旧名字盖回来了 —— 用户会以为修复无效")


class _FakeWdb(object):
    def __init__(self, names):
        self._names = names

    def get_nickname(self, user):
        return self._names.get(user, user)


class TestResolveSender(unittest.TestCase):
    """`_resolve_sender` 的优先级链。"""

    def setUp(self):
        self._orig_cfg = wi._wechat_cfg

    def tearDown(self):
        wi._wechat_cfg = self._orig_cfg

    def test_别名映射优先级最高(self):
        wi._wechat_cfg = lambda: {"sender_aliases": {"u1": "统一名"}}
        got = wi._resolve_sender(_FakeWdb({}), {"u1": "群成员名"}, "u1", {})
        self.assertEqual(got, "统一名")

    def test_群成员表优先于联系人表(self):
        wi._wechat_cfg = lambda: {}
        got = wi._resolve_sender(_FakeWdb({"u1": "联系人名"}),
                                 {"u1": "群成员名"}, "u1", {})
        self.assertEqual(got, "群成员名")

    def test_群成员表查不到时走联系人表(self):
        wi._wechat_cfg = lambda: {}
        got = wi._resolve_sender(_FakeWdb({"u1": "联系人名"}), {}, "u1", {})
        self.assertEqual(got, "联系人名")

    def test_都查不到时退回原id而不是空(self):
        wi._wechat_cfg = lambda: {}
        got = wi._resolve_sender(_FakeWdb({}), {}, "wxid_abcdef", {})
        self.assertEqual(got, "wxid_abcdef")

    def test_空发送人显示群友(self):
        wi._wechat_cfg = lambda: {}
        self.assertEqual(wi._resolve_sender(_FakeWdb({}), {}, "", {}), "群友")

    def test_按会话缓存只查一次(self):
        calls = []

        class Counting(_FakeWdb):
            def get_nickname(self, user):
                calls.append(user)
                return "查到的名"

        wi._wechat_cfg = lambda: {}
        cache = {}
        wi._resolve_sender(Counting({}), {}, "u1", cache)
        wi._resolve_sender(Counting({}), {}, "u1", cache)
        self.assertEqual(len(calls), 1, "同一发送人被查了两次库")

    def test_群成员表查库异常时不崩(self):
        class Boom(_FakeWdb):
            def get_nickname(self, user):
                raise RuntimeError("库不可读")

        wi._wechat_cfg = lambda: {}
        got = wi._resolve_sender(Boom({}), {}, "wxid_xyz", {})
        self.assertEqual(got, "wxid_xyz")


class TestNoFork(unittest.TestCase):
    """[C] `wxwatch` 不得再自己实现一份取名逻辑。"""

    def test_wxwatch_委托给_intake(self):
        from wx import wxwatch as ww
        with open(os.path.join(HERE, "wx", "wxwatch.py"), encoding="utf-8") as f:
            src = f.read()
        self.assertIn("wi._wx4_member_names", src,
                      "wxwatch 又自己建表了 —— 两处口径会分叉（历史根因）")
        self.assertNotIn('mem["nick_name"]', src,
                         "wxwatch 里又有按 nick_name 取名的旧写法")
        self.assertTrue(callable(ww._member_names))

    def test_wxwatch_取的名字与_intake_一致(self):
        from wx import wxwatch as ww
        orig_dir = wi.INTAKE_DIR
        orig_cfg = wi._wechat_cfg
        tmp = tempfile.mkdtemp(prefix="wxnames_fork_")
        wi.INTAKE_DIR = tmp
        wi._wechat_cfg = lambda: {}
        try:
            db = _FakeDb([{"members": [
                {"username": "u1", "nick_name": "网名甲", "remark": "真名甲"}]}])
            self.assertEqual(ww._member_names(db).get("u1"),
                             wi._wx4_member_names(db).get("u1"))
            self.assertEqual(ww._member_names(db).get("u1"), "真名甲")
        finally:
            wi.INTAKE_DIR = orig_dir
            wi._wechat_cfg = orig_cfg


if __name__ == "__main__":
    unittest.main(verbosity=2)
