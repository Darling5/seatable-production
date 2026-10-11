# -*- coding: utf-8 -*-
"""test_wx_sender_attribution.py — 群消息发言人归属（2026-10-11 修）。

## 背景：「人名没读对」的**真正主因**

`wa_db._sender_id_index()` 读的是 `message_resource.db` 的
`SenderName2Id`，但 `real_sender_id` 的真源是**消息分片库自己的 `Name2Id`**
（`Name2Id(user_name TEXT PRIMARY KEY, is_session INTEGER)`）。
两张表在同一个 WeChat 数据目录里并存，rowid 空间**毫无关系**。

实测（本机，128457 条正文自带发送者账号前缀的群消息，前缀即真值）：

| 查表方式 | 命中 | 不符 | 查无 |
|---|---|---|---|
| `message_0.db.Name2Id[rowid]` | **128457（100.0%）** | 0 | 0 |
| `message_resource.db.SenderName2Id[rowid]`（旧代码） | 0 | 117639 | 10818 |

⇒ 旧代码把**每一条**群消息的发言人都安到了不相干的人头上，
于是摘要里出现「对方从没说过的话」。

## 附带的第二个错前提

旧代码把 `real_sender_id == 2` 当成"本人"（`_resolve_sender` 里直接返回
`self_nick`，`_msg_row_to_dict` 里干脆留空 → 落到「群友」）。
这是错的：`Name2Id` 的 rowid 由插入顺序决定 —— 本机 id=2 其实是一个群聊。
**本人只能靠「解析出的 username == self.wxid」判定。**

> ⚠️ 本文件一律用**中性示例名**（`demo_*` / `示例*`），不写真实人名或账号。
"""

import os
import re
import sqlite3
import sys
import unittest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

from wx import wa_db  # noqa: E402
from wx import wechat_intake as wi  # noqa: E402

WA_DB_SRC = os.path.join(HERE, "wx", "wa_db.py")


def _read_src(path=WA_DB_SRC):
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def _shard(rows, sender_rows=None):
    """造一个最小"消息分片库"内存连接。

    rows        = [(rowid, user_name)] 写进 Name2Id（真源表）
    sender_rows = 可选的 [(rowid, user_name)] 写进 SenderName2Id（干扰表）
    """
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row      # 与 _open() 一致：调用方按列名取 r["real_sender_id"]
    conn.execute("CREATE TABLE Name2Id(user_name TEXT PRIMARY KEY, is_session INTEGER)")
    for rid, u in rows:
        conn.execute("INSERT INTO Name2Id(rowid, user_name, is_session) VALUES (?,?,0)",
                     (rid, u))
    if sender_rows is not None:
        conn.execute("CREATE TABLE SenderName2Id(user_name TEXT PRIMARY KEY, "
                     "update_time INTEGER)")
        for rid, u in sender_rows:
            conn.execute("INSERT INTO SenderName2Id(rowid, user_name, update_time) "
                         "VALUES (?,?,0)", (rid, u))
    conn.commit()
    return conn


def _fake_db(account="wxid_selfdemo_a1b2", shard_pairs=None):
    """不跑 __init__ 的 WeChatDB 替身（不碰磁盘、不解密）。"""
    db = object.__new__(wa_db.WeChatDB)
    db.account = account
    db._sender_id_cache = None
    db._message_dbs = lambda: []            # 并集兜底为空 → 逼出"本分片"行为
    db._shards = list(shard_pairs or [])
    return db


class _Row(dict):
    """仿 sqlite3.Row：既支持 r["k"] 也支持 r[0]（_msg_row_to_dict 用键取）。"""


def _msg_row(sid, content="示例正文", local_id=1, seq=1):
    return _Row(local_id=local_id, local_type=1, real_sender_id=sid,
                create_time=1700000000, message_content=content,
                source=None, packed_info_data=None, compress_content=None,
                sort_seq=seq)


class TestWrongTableIsTheRootCause(unittest.TestCase):
    """[A] 真源必须是 Name2Id，不是 SenderName2Id —— 本 bug 的核心。"""

    def test_两张表并存时必须取_Name2Id(self):
        """★ 关键反证：同一分片里两表给出不同答案，必须取 Name2Id 的。"""
        conn = _shard([(7, "demo_right")], sender_rows=[(7, "demo_wrong")])
        db = _fake_db()
        self.assertEqual(db._sender_username(conn, 7), "demo_right",
                         "real_sender_id 又去读 SenderName2Id 了")

    def test_索引函数同样只认_Name2Id(self):
        conn = _shard([(7, "demo_right")], sender_rows=[(7, "demo_wrong")])
        db = _fake_db()
        self.assertEqual(db._sender_id_index(conn).get(7), "demo_right")

    def test_源码不得再出现_from_SenderName2Id(self):
        """SQL 层面锁死：不许再查 SenderName2Id。"""
        src = _read_src()
        bad = re.findall(r"FROM\s+SenderName2Id", src)
        self.assertEqual(bad, [], "又按 SenderName2Id 解析发送者了（旧 bug 回归）")

    def test_源码必须查_Name2Id(self):
        src = _read_src()
        self.assertIn("FROM Name2Id", src)

    def test_导入期不再预先构建全局索引(self):
        """导出路径曾一次性建全局 sender_index —— 那是错的（id 是分片局部的）。"""
        src = _read_src()
        self.assertNotIn("sender_index = self._sender_id_index()", src)

    def test_空表_不能把整批解成空(self):
        conn = _shard([])
        db = _fake_db()
        self.assertEqual(db._sender_username(conn, 5), "")


class TestPerShardPriority(unittest.TestCase):
    """[B] real_sender_id 是**分片局部** id：不同分片同号不同人。"""

    def test_同号不同人时以本分片为准(self):
        a = _shard([(9, "demo_shard_a")])
        b = _shard([(9, "demo_shard_b")])
        db = _fake_db()
        self.assertEqual(db._sender_username(a, 9), "demo_shard_a")
        self.assertEqual(db._sender_username(b, 9), "demo_shard_b")

    def test_本分片查不到才回落到并集(self):
        a = _shard([(1, "demo_a")])
        b = _shard([(9, "demo_from_b")])
        db = _fake_db()
        # 并集由 _message_dbs 驱动；这里直接注入缓存模拟"其它分片已建好"
        db._sender_id_cache = {9: "demo_from_b"}
        self.assertEqual(db._sender_username(a, 9), "demo_from_b")
        del b

    def test_非法_sender_id_返回空而不是抛异常(self):
        conn = _shard([(1, "demo_a")])
        db = _fake_db()
        for bad in (None, "", "abc", -3, 0):
            self.assertEqual(db._sender_username(conn, bad), "")


class TestSenderIdTwoIsNotSelf(unittest.TestCase):
    """[C] `sender_id == 2` 不是"本人" —— 旧代码的第二个错前提。"""

    def test_id2_必须由表解析而不是当成本人(self):
        conn = _shard([(2, "demo_someone")])
        db = _fake_db()
        idx = db._sender_id_index(conn)
        self.assertEqual(
            db._resolve_sender(2, idx, {}, "我"), "demo_someone",
            "又把 id==2 当作本人了")

    def test_源码不得把_2_写成魔法数字(self):
        src = _read_src()
        self.assertNotIn("sender_id in (2, \"2\")", src)
        self.assertNotIn("sender_id != 2", src)

    def test_消息字典不得因_id2_被清空(self):
        db = _fake_db()
        d = db._msg_row_to_dict(_msg_row(2), "demo_someone")
        self.assertEqual(d["sender_username"], "demo_someone")
        self.assertEqual(d["sender_id"], 2)

    def test_消息字典单独调用遇_id2_也要解析出来(self):
        """★ 旧代码在这里把 id==2 的发送者**留空**（→ 摘要显示「群友」）。"""
        db = _fake_db()
        db._sender_id_cache = {2: "demo_someone"}
        d = db._msg_row_to_dict(_msg_row(2))
        self.assertEqual(d["sender_username"], "demo_someone",
                         "id==2 又被当成本人/未知而留空了")


class TestSelfComesFromWxid(unittest.TestCase):
    """[D] 本人只能靠 wxid 相等判定。"""

    def test_解析出的账号等于本人账号时显示本人名(self):
        db = _fake_db(account="wxid_selfdemo_a1b2")   # wxid == wxid_selfdemo
        idx = {3: "wxid_selfdemo"}
        self.assertEqual(db._resolve_sender(3, idx, {}, "我"), "我")

    def test_别人不能被误判成本人(self):
        db = _fake_db(account="wxid_selfdemo_a1b2")
        idx = {3: "demo_other"}
        self.assertEqual(db._resolve_sender(3, idx, {"demo_other": "示例甲"}, "我"),
                         "示例甲")

    def test_wxid_属性去掉末尾四位哈希(self):
        db = _fake_db(account="wxid_selfdemo_a1b2")
        self.assertEqual(db.wxid, "wxid_selfdemo")


class TestResolutionInsideShardConn(unittest.TestCase):
    """[E] get_messages / get_new_messages 必须在该条消息所属分片内解析。"""

    def _wire(self, db, conn, table):
        db._run_msg_query = lambda user, build: build(conn, table)

    def test_get_messages_用本分片索引(self):
        conn = _shard([(4, "demo_right")], sender_rows=[(4, "demo_wrong")])
        conn.execute("CREATE TABLE Msg_x(local_id INT, local_type INT, "
                     "real_sender_id INT, create_time INT, message_content TEXT, "
                     "source BLOB, packed_info_data BLOB, compress_content BLOB, "
                     "sort_seq INT)")
        conn.execute("INSERT INTO Msg_x VALUES (1,1,4,1700000000,'示例正文',"
                     "NULL,NULL,NULL,1)")
        conn.commit()
        db = _fake_db()
        db._nickname_index = lambda: {}
        db.get_nickname = lambda x: ""
        self._wire(db, conn, "Msg_x")
        got = db.get_messages("demo@chatroom", limit=10)
        self.assertEqual(got[0]["sender_username"], "demo_right")

    def test_get_new_messages_用本分片索引(self):
        conn = _shard([(4, "demo_right")], sender_rows=[(4, "demo_wrong")])
        conn.execute("CREATE TABLE Msg_x(local_id INT, local_type INT, "
                     "real_sender_id INT, create_time INT, message_content TEXT, "
                     "source BLOB, packed_info_data BLOB, compress_content BLOB, "
                     "sort_seq INT)")
        conn.execute("INSERT INTO Msg_x VALUES (2,1,4,1700000000,'示例正文2',"
                     "NULL,NULL,NULL,2)")
        conn.commit()
        db = _fake_db()
        db.get_nickname = lambda x: ""
        self._wire(db, conn, "Msg_x")
        got = db.get_new_messages("demo@chatroom", since_seq=0)
        self.assertEqual(got[0]["sender_username"], "demo_right")

    def test_get_message_row_用本分片索引(self):
        conn = _shard([(4, "demo_right")], sender_rows=[(4, "demo_wrong")])
        conn.execute("CREATE TABLE Msg_x(local_id INT, local_type INT, server_id INT, "
                     "real_sender_id INT, create_time INT, message_content TEXT, "
                     "source BLOB, packed_info_data BLOB, compress_content BLOB, "
                     "sort_seq INT)")
        conn.execute("INSERT INTO Msg_x VALUES (3,1,0,4,1700000000,'示例正文3',"
                     "NULL,NULL,NULL,3)")
        conn.commit()
        db = _fake_db()
        db.get_nickname = lambda x: ""
        self._wire(db, conn, "Msg_x")
        got = db.get_message_row("demo@chatroom", 3)
        self.assertEqual(got["sender_username"], "demo_right")

    def test_未带conn的旧调用方走并集兜底且不崩(self):
        db = _fake_db()
        db._sender_id_cache = {6: "demo_union"}
        d = db._msg_row_to_dict(_msg_row(6))
        self.assertEqual(d["sender_username"], "demo_union")


class TestEngineBContactMapRemarkFirst(unittest.TestCase):
    """[F] 引擎B（旧版微信回退）联系人映射也是 remark 优先 —— 同一类 bug。"""

    @staticmethod
    def _legacy(has_remark=True):
        conn = sqlite3.connect(":memory:")
        if has_remark:
            conn.execute("CREATE TABLE Contact(UserName TEXT, NickName TEXT, Remark TEXT)")
            conn.execute("INSERT INTO Contact VALUES ('demo_u1','示例网名','示例备注')")
            conn.execute("INSERT INTO Contact VALUES ('demo_u2','示例网名2','')")
        else:
            conn.execute("CREATE TABLE Contact(UserName TEXT, NickName TEXT)")
            conn.execute("INSERT INTO Contact VALUES ('demo_u1','示例网名')")
        conn.commit()
        return conn

    def test_有备注时取备注(self):
        m = wi._find_contact_map(self._legacy(True))
        self.assertEqual(m.get("demo_u1"), "示例备注")

    def test_备注为空退回昵称(self):
        m = wi._find_contact_map(self._legacy(True))
        self.assertEqual(m.get("demo_u2"), "示例网名2")

    def test_没有备注列的老库行为不变(self):
        m = wi._find_contact_map(self._legacy(False))
        self.assertEqual(m.get("demo_u1"), "示例网名")

    def test_没有联系人表也不崩(self):
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE Other(a TEXT)")
        conn.commit()
        self.assertEqual(wi._find_contact_map(conn), {})


class TestIntakeSelfDisplay(unittest.TestCase):
    """[G] 本人发言在摘要里显示成「我」，且**换账号自动跟随**。

    本人既没有 remark（微信不允许给自己设备注），昵称又常是网名 ——
    不特殊处理就会在摘要里显示成自己的网名。
    """

    def setUp(self):
        self._orig_cfg = wi._wechat_cfg
        wi._wechat_cfg = lambda: {}

    def tearDown(self):
        wi._wechat_cfg = self._orig_cfg

    @staticmethod
    def _wdb(account_wxid):
        class _W(object):
            wxid = account_wxid

            @staticmethod
            def get_nickname(u):
                return "demo_nick_" + str(u)
        return _W()

    def test_本人发言显示为我(self):
        wdb = self._wdb("wxid_selfdemo")
        self.assertEqual(wi._resolve_sender(wdb, {}, "wxid_selfdemo", {}), "我")

    def test_换登录账号后自动跟随(self):
        wdb = self._wdb("wxid_otherself")
        self.assertEqual(wi._resolve_sender(wdb, {}, "wxid_otherself", {}), "我")
        # 旧账号不再是"本人"
        self.assertEqual(
            wi._resolve_sender(self._wdb("wxid_otherself"), {}, "wxid_selfdemo", {}),
            "demo_nick_wxid_selfdemo")

    def test_self_name_可配置(self):
        wi._wechat_cfg = lambda: {"self_name": "本人"}
        self.assertEqual(
            wi._resolve_sender(self._wdb("wxid_selfdemo"), {}, "wxid_selfdemo", {}), "本人")
        wi._wechat_cfg = lambda: {"self_name": "   "}
        self.assertEqual(
            wi._resolve_sender(self._wdb("wxid_selfdemo"), {}, "wxid_selfdemo", {}), "我",
            "空白 self_name 应退回默认「我」")

    def test_别名优先于本人(self):
        wi._wechat_cfg = lambda: {"sender_aliases": {"wxid_selfdemo": "别名甲"}}
        self.assertEqual(
            wi._resolve_sender(self._wdb("wxid_selfdemo"), {}, "wxid_selfdemo", {}),
            "别名甲")

    def test_没有_wxid_属性的替身不会误判(self):
        class _Bare(object):
            @staticmethod
            def get_nickname(u):
                return "demo_nick"

        self.assertEqual(wi._resolve_sender(_Bare(), {}, "demo_u", {}), "demo_nick")

    def test_空_su_仍是群友(self):
        self.assertEqual(wi._resolve_sender(self._wdb("wxid_selfdemo"), {}, "", {}), "群友")


if __name__ == "__main__":
    unittest.main(verbosity=2)
