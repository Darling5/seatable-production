#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""wechat_intake.py — 微信情报反哺（读本地微信库 → 提取事件 → 确认后写业务表）。

数据链路（零封号风险，只读本地文件/进程内存，不与微信服务器通信）：
  引擎A wechatauto（微信 4.x 主路径，默认）：
    直接读 xwechat_files/db_storage 下的 SQLCipher4 加密库——
    密钥从运行中的 Weixin.exe 内存提取（Config.Cipher 扫描 + cfg 主密钥派生），
    无需任何第三方 GUI 工具，微信保持登录即可。
  引擎B merge_all.db（微信 3.x 回退）：
    win-wechat-summary（PyWxDump）解密生成的合并库。
    → 本脚本 pull 拉取监控群的新消息（书签续读，不重复）
    → AI（自动化里的专家）把消息拆成「微信事件」add-event 登记（状态=���确认）
    → 驾驶舱「微信情报台」展示待确认清单
    → 你在对话里说「确认第 N 条」→ approve 直接写 SeaTable 云端业务表
    → 写入结果回填事件行，全程留痕

命令：
  python wechat_intake.py doctor            # 体检：找库、提密钥、给指引
  python wechat_intake.py groups            # 列出所有群聊（挑监控对象）
  python wechat_intake.py pull              # 拉监控群新消息 -> data/wechat_intake/latest.json/.md
  python wechat_intake.py summary           # 群聊摘要：按时间窗口导出对话+统计+需关注
                      [--group 群名] [--hours 24] [--limit 2000] [--all] [--json] [--out FILE]
                      # --group 可多次/逗号分隔、支持部分匹配；不给则用 watch_groups
  python wechat_intake.py add-event --group 群 --sender 人 --category 分类
                      --text "原文" [--intent '{"op":"update",...}']
  python wechat_intake.py list [--status 待确认]
  python wechat_intake.py approve <编号>    # 确认事件并执行意图（写 SeaTable 云端）
  python wechat_intake.py ignore <编号>     # 忽略（不写业务表，仅留痕）
  python wechat_intake.py clear-demo        # 清掉示例事件

事件分类：交期变更 / 价格变动 / 停产通知 / 催货 / 进度 / 库存 / 其他
意图格式（同 op.py intake）：{"op":"update|append|log","table":"项目","row_id":"...",
                            "reason":"依据原话","data":{列:值}}

依赖（引擎A）：pip install cryptography
"""
import argparse
import csv
import glob
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
INTAKE_DIR = os.path.join(DATA, "wechat_intake")
BOOKMARK = os.path.join(INTAKE_DIR, "bookmark.json")
EVENTS_PATH = os.path.join(DATA, "微信事件.csv")
GROUPS_CACHE = os.path.join(INTAKE_DIR, "groups.json")
KEYS_CACHE = os.path.join(INTAKE_DIR, "master_key.json")  # 引擎A主密钥缓存

EVENT_COLS = ["事件编号", "日期", "时间", "来源群", "发送人", "分类", "原文",
              "意图", "状态", "确认时间", "写入结果"]
CATEGORIES = ["交期变更", "价格变动", "停产通知", "催货", "进度", "库存", "其他"]

sys.path.insert(0, HERE)
from adapters.factory import load_config  # noqa: E402


def _now():
    return datetime.now()


def _read_csv(path):
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        return [dict(r) for r in csv.DictReader(f)]


def _write_csv(path, cols, rows):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for r in rows:
            w.writerow({c: r.get(c, "") for c in cols})


# ---------------------------------------------------------------- 配置与找库
def _wechat_cfg():
    cfg = load_config() or {}
    return cfg.get("wechat") or {}


def _candidate_dbs():
    """按优先级搜 merge_all.db：配置指定 > 环境变量 > 常见安装位置。"""
    cands = []
    explicit = (_wechat_cfg().get("db_path") or "").strip()
    if explicit:
        cands.append(explicit)
    env = os.environ.get("WXDUMP_DB", "").strip()
    if env:
        cands.append(env)
    home = os.path.expanduser("~")
    patterns = [
        os.path.join(home, "win-wechat-summary", "wxdump_work", "*", "merge_all.db"),
        os.path.join(home, "win-wechat-summary", "wxdump_work", "merge_all.db"),
        os.path.join(home, "Desktop", "win-wechat-summary", "wxdump_work", "*", "merge_all.db"),
        os.path.join(home, "Desktop", "*", "wxdump_work", "*", "merge_all.db"),
        os.path.join(home, "Downloads", "win-wechat-summary", "wxdump_work", "*", "merge_all.db"),
        os.path.join(home, "Downloads", "WeChat-Summary*", "wxdump_work", "*", "merge_all.db"),
        "D:/win-wechat-summary/wxdump_work/*/merge_all.db",
        "D:/WeChat-Summary*/wxdump_work/*/merge_all.db",
        os.path.join(home, ".wechat-summary", "wxdump_work", "*", "merge_all.db"),
    ]
    for pat in patterns:
        cands.extend(glob.glob(pat))
    seen, out = set(), []
    for c in cands:
        c = os.path.abspath(c)
        if c not in seen and os.path.exists(c):
            seen.add(c)
            out.append(c)
    return out


def _open_ro(db_path):
    """只读打开；被占用（WAL 锁）时复制临时副本再读。"""
    try:
        con = sqlite3.connect("file:%s?mode=ro" % db_path.replace("?", "%3f").replace("#", "%23"),
                              uri=True, timeout=5)
        con.execute("SELECT count(*) FROM sqlite_master").fetchone()
        return con, None
    except Exception as e:
        tmp = os.path.join(tempfile.gettempdir(), "wx_merge_copy_%d.db" % int(_now().timestamp()))
        try:
            shutil.copy2(db_path, tmp)
            con = sqlite3.connect("file:%s?mode=ro" % tmp.replace("?", "%3f"), uri=True)
            return con, tmp
        except Exception:
            raise SystemExit("[错误] 无法读取 %s：%s" % (db_path, e))


def _cols_lower(con, table):
    return {r[1].lower(): r[1] for r in con.execute("PRAGMA table_info(%s)" % _q(table))}


def _q(name):
    return '"%s"' % name.replace('"', '""')


def _find_msg_table(con):
    """找消息主表：含 strtalker + strcontent 的表。"""
    tables = [r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")]
    best = None
    for t in tables:
        cl = _cols_lower(con, t)
        if "strtalker" in cl and "strcontent" in cl:
            # 优先叫 MSG 的
            if t.upper() == "MSG":
                return t, cl
            best = best or (t, cl)
    return best or (None, None)


def _find_contact_map(con):
    """尽力构建 wxid/群id -> 昵称 映射。"""
    tables = [r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")]
    mapping = {}
    for t in tables:
        cl = _cols_lower(con, t)
        if "username" in cl and "nickname" in cl:
            try:
                for un, nn in con.execute(
                        "SELECT %s, %s FROM %s" % (_q(cl["username"]), _q(cl["nickname"]), _q(t))):
                    if un and nn:
                        mapping[str(un)] = str(nn)
            except Exception:
                pass
    # Name2Id 表（id -> wxid）也顺便带上
    return mapping


# ---------------------------------------------------------------- 引擎A：微信4.x 直读（主路径）
_WX4 = None  # WeChatDB 单例（进程内复用，避免重复解密 19 个库）


def _load_wx4():
    """加载 wechatauto 引擎（微信 4.x）。返回 WeChatDB 单例；不可用返回 None。"""
    global _WX4
    if _WX4 is not None:
        return _WX4
    try:
        sys.path.insert(0, os.path.join(HERE, "wxengine"))
        import wa_db  # noqa: E402
    except Exception as e:
        print("[warn] 引擎A(wechatauto)加载失败：%s" % e)
        return None
    cfg = _wechat_cfg()
    kwargs = {}
    db_dir = (cfg.get("db_dir") or "").strip()   # 微信4.x数据根目录（含 wxid_xxx 子目录）
    if db_dir:
        kwargs["db_dir"] = db_dir
    # 主密钥缓存：微信没在运行时也能解密
    master = None
    if os.path.exists(KEYS_CACHE):
        try:
            with open(KEYS_CACHE, "r", encoding="utf-8") as f:
                master = json.load(f).get("master_key")
        except Exception:
            master = None
    if master:
        kwargs["master_key"] = master
    # keys.json 缓存放 data 目录（默认在 temp，重启易丢）
    kwargs["workdir"] = os.path.join(INTAKE_DIR, "wx4_work")
    try:
        db = wa_db.WeChatDB(**kwargs)
    except Exception as e:
        print("[warn] 引擎A初始化失败（微信未登录或版本不支持）：%s" % e)
        return None
    # 提取成功后回存主密钥
    try:
        if db.master_key:
            os.makedirs(INTAKE_DIR, exist_ok=True)
            with open(KEYS_CACHE, "w", encoding="utf-8") as f:
                json.dump({"master_key": db.master_key, "wxid": db.wxid,
                           "saved_at": _now().strftime("%Y-%m-%d %H:%M")}, f, ensure_ascii=False)
    except Exception:
        pass
    _WX4 = db
    return db


def _wx4_member_names(wdb):
    """群成员 username -> 昵称映射（来自全部群成员表，群里非好友也能显示昵称）。"""
    cache_path = os.path.join(INTAKE_DIR, "member_names.json")
    if os.path.exists(cache_path):
        try:
            with open(cache_path, "r", encoding="utf-8") as f:
                d = json.load(f)
            if _now().timestamp() - os.path.getmtime(cache_path) < 86400 * 3:
                return d
        except Exception:
            pass
    mapping = {}
    try:
        for g in wdb.get_groups():
            for m in g.get("members") or []:
                if m.get("username") and m.get("nick_name"):
                    mapping[m["username"]] = m["nick_name"]
    except Exception:
        pass
    try:
        os.makedirs(INTAKE_DIR, exist_ok=True)
        with open(cache_path, "w", encoding="utf-8") as f:
            json.dump(mapping, f, ensure_ascii=False)
    except Exception:
        pass
    return mapping


def _wx4_pull(wdb, watch, max_hours, bm):
    """引擎A拉取：书签按 sort_seq(毫秒) 续读，不重复、不漏。"""
    members = _wx4_member_names(wdb)
    groups = wdb.get_groups()
    wanted = []
    for g in groups:
        if not watch or g["name"] in watch or g["username"] in watch \
                or any(w in g["name"] for w in watch if isinstance(w, str)):
            wanted.append(g)
    now_ms = _now().timestamp() * 1000
    result = {"engine": "wx4(微信4.x直读)", "pulled_at": _now().strftime("%Y-%m-%d %H:%M:%S"),
              "groups": []}
    for g in wanted:
        key = g["username"]
        marks = bm.get(key) or {}
        last_seq = int(marks.get("seq", 0) or 0)
        if last_seq:
            msgs_raw = wdb.get_new_messages(key, since_seq=last_seq, limit=2000)
        else:
            # 首次拉取：只回看 max_hours 小时
            msgs_raw = wdb.get_messages(key, limit=500)
            msgs_raw = [m for m in msgs_raw
                        if (m.get("create_time") or 0) >= now_ms / 1000 - max_hours * 3600]
            msgs_raw.reverse()
        msgs = []
        max_seq = last_seq
        for m in msgs_raw:
            if m.get("type") != "文本":
                continue
            text = (m.get("content") or "").strip()
            if not text or text.startswith("<") or "sysmsg" in text[:30].lower():
                continue
            su = m.get("sender_username") or ""
            sender = members.get(su) or (su if su else "群友")
            ts = m.get("create_time") or 0
            dt = datetime.fromtimestamp(ts) if ts else _now()
            seq = m.get("sort_seq") or 0
            msgs.append({"ts": int(ts), "time": dt.strftime("%H:%M"),
                         "date": dt.strftime("%Y-%m-%d"), "sender": sender,
                         "text": text[:2000]})
            if seq > max_seq:
                max_seq = seq
        if max_seq > last_seq:
            bm[key] = {"seq": max_seq, "name": g["name"]}
        result["groups"].append({
            "id": key, "name": g["name"], "count": len(msgs), "messages": msgs})
    return result


# ---------------------------------------------------------------- doctor/groups
def cmd_doctor():
    cfg = _wechat_cfg()
    print("== 微信情报体检 ==")
    print("config.wechat.enabled = %s" % cfg.get("enabled", "(未配置，默认开)"))
    print("config.wechat.db_dir   = %s（引擎A：微信4.x数据根目录，空=自动探测）"
          % (cfg.get("db_dir") or "(空)"))
    print("config.wechat.db_path  = %s（引擎B：merge_all.db 路径，空=自动搜索）"
          % (cfg.get("db_path") or "(空)"))
    print("config.wechat.watch_groups = %s" % (cfg.get("watch_groups") or "(空 → 所有群)"))
    # 引擎A
    print("\n-- 引擎A：微信4.x 直读（主路径，无需第三方工具）--")
    wdb = _load_wx4()
    if wdb:
        keyed = sum(1 for rel, _, _ in wdb._db_files if rel in wdb._keys)
        print("[ok] 账号：%s（%s）" % (wdb.account, wdb.wxid))
        print("     数据目录：%s" % wdb.account_dir)
        print("     数据库：%d 个，已解密 %d 个" % (len(wdb._db_files), keyed))
        if keyed < len(wdb._db_files):
            print("     [!] 部分库未解密：微信需保持登录运行（密钥从进程内存提取）")
        gs = wdb.get_groups()
        print("     群聊：%d 个" % len(gs))
        print("\n下一步：python wechat_intake.py groups  挑监控群")
        return
    print("[!] 引擎A不可用（微信4.x未登录 / 版本不支持 / 缺 cryptography）")
    # 引擎B
    print("\n-- 引擎B：merge_all.db（微信3.x 回退）--")
    dbs = _candidate_dbs()
    if not dbs:
        print("[!] 没找到 merge_all.db。引擎B需要 win-wechat-summary（只支持微信3.x）。")
        print("    微信4.x 用户请走引擎A：保持微信 PC 版登录后重跑 doctor。")
        return
    for db in dbs:
        print("\n[ok] 找到库：%s（%.1f MB，修改于 %s）" % (
            db, os.path.getsize(db) / 1048576,
            datetime.fromtimestamp(os.path.getmtime(db)).strftime("%Y-%m-%d %H:%M")))
    con, tmp = _open_ro(dbs[0])
    try:
        t, cl = _find_msg_table(con)
        print("    消息表：%s（列：%s）" % (t, ", ".join(sorted(cl.keys())[:14])))
        if not t:
            print("    [!] 该库没有消息表（可能同步未完成），请在工具里点一次「同步」")
        contact = _find_contact_map(con)
        print("    联系人昵称映射：%d 条" % len(contact))
        print("\n下一步：python wechat_intake.py groups  挑监控群")
    finally:
        con.close()
        if tmp:
            try:
                os.remove(tmp)
            except Exception:
                pass


def _all_groups(con):
    t, cl = _find_msg_table(con)
    if not t:
        raise SystemExit("[错误] 库里没有消息表（MSG）；先在 win-wechat-summary 里点「同步」")
    talker = cl["strtalker"]
    where = "%s LIKE '%%@chatroom'" % _q(talker)
    rows = con.execute(
        "SELECT %s, count(*) FROM %s WHERE %s GROUP BY %s ORDER BY 2 DESC" % (
            _q(talker), _q(t), where, _q(talker))).fetchall()
    contact = _find_contact_map(con)
    out = []
    for rid, cnt in rows:
        out.append({"id": rid, "name": contact.get(rid, rid), "count": cnt})
    return out


def cmd_groups():
    # 优先引擎A
    wdb = _load_wx4()
    if wdb:
        gs = [{"id": g["username"], "name": g["name"], "count": g.get("member_count", 0)}
              for g in wdb.get_groups()]
        gs.sort(key=lambda x: x["name"])
        os.makedirs(INTAKE_DIR, exist_ok=True)
        with open(GROUPS_CACHE, "w", encoding="utf-8") as f:
            json.dump(gs, f, ensure_ascii=False, indent=1)
        print("引擎A(微信4.x直读) · 共 %d 个群（已缓存到 groups.json）。" % len(gs))
        print("把要监控的群名/群id 填进 config.yaml 的 wechat.watch_groups，留空=全部群：")
        for g in gs[:60]:
            print("  %-40s %6d 人  %s" % (g["name"][:40], g["count"], g["id"]))
        if len(gs) > 60:
            print("  …（其余 %d 个群见 groups.json）" % (len(gs) - 60))
        return
    # 引擎B回退
    dbs = _candidate_dbs()
    if not dbs:
        print("[!] 未找到微信库，先运行 python wechat_intake.py doctor 看指引")
        return
    con, tmp = _open_ro(dbs[0])
    try:
        gs = _all_groups(con)
        os.makedirs(INTAKE_DIR, exist_ok=True)
        with open(GROUPS_CACHE, "w", encoding="utf-8") as f:
            json.dump(gs, f, ensure_ascii=False, indent=1)
        print("共 %d 个群（已缓存到 groups.json）。把要监控的群名/群id 填进 "
              "config.yaml 的 wechat.watch_groups，留空=全部群：" % len(gs))
        for g in gs[:60]:
            print("  %-40s %6d 条  %s" % (g["name"][:40], g["count"], g["id"]))
        if len(gs) > 60:
            print("  …（其余 %d 个群见 groups.json）" % (len(gs) - 60))
    finally:
        con.close()
        if tmp:
            try:
                os.remove(tmp)
            except Exception:
                pass


# ---------------------------------------------------------------- pull
def _bookmarks():
    if os.path.exists(BOOKMARK):
        try:
            with open(BOOKMARK, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def _save_bookmarks(bm):
    os.makedirs(INTAKE_DIR, exist_ok=True)
    with open(BOOKMARK, "w", encoding="utf-8") as f:
        json.dump(bm, f, ensure_ascii=False, indent=1)


def _sender_name(talkerid_val, extra_val, contact):
    """尽力识别发送者：BytesExtra 里的 wxid → 昵称；失败用 TalkerId/未知。"""
    try:
        be = bytes(extra_val) if extra_val else b""
    except Exception:
        be = b""
    if be:
        m = re.search(rb"wxid_[A-Za-z0-9_\-]+", be)
        if m:
            wxid = m.group(0).decode("ascii", "ignore")
            return contact.get(wxid, wxid)
        # 非微信id的原始字符串（群里非好友显示的串）
        m = re.search(rb"[\x20-\x7e]{6,40}", be)
        if m:
            s = m.group(0).decode("ascii", "ignore")
            if "chatroom" not in s:
                return contact.get(s, s)
    if talkerid_val:
        return contact.get(str(talkerid_val), "群友")
    return "群友"


def cmd_pull():
    cfg = _wechat_cfg()
    if str(cfg.get("enabled", True)).lower() in ("false", "0", "no"):
        print("[skip] config.yaml wechat.enabled=false，微信情报未启用")
        return
    watch = cfg.get("watch_groups") or []
    max_hours = float(cfg.get("max_hours") or 48)
    bm = _bookmarks()
    result = None
    # ---- 引擎A：微信4.x 直读（主路径）----
    wdb = _load_wx4()
    if wdb:
        try:
            result = _wx4_pull(wdb, watch, max_hours, bm)
        except Exception as e:
            print("[warn] 引擎A拉取失败，回退引擎B：%s" % e)
            result = None
    # ---- 引擎B：merge_all.db（微信3.x 回退）----
    if result is None:
        dbs = _candidate_dbs()
        if not dbs:
            print("[skip] 微信数据源均不可用（引擎A需微信4.x登录中；引擎B需 merge_all.db）"
                  "——不影响其余步骤")
            return
        db = dbs[0]
        con, tmp = _open_ro(db)
        try:
            t, cl = _find_msg_table(con)
            if not t:
                print("[skip] 库里没有消息表，请在 win-wechat-summary 里点「同步」")
                return
            contact = _find_contact_map(con)
            talker_c, content_c = cl["strtalker"], cl["strcontent"]
            time_c = cl.get("createtime") or cl.get("createtime")
            if not time_c:
                for k in cl:
                    if "time" in k:
                        time_c = cl[k]
                        break
            localid_c = cl.get("localid") or cl.get("id")
            type_c = cl.get("type")
            extra_c = cl.get("bytesextra")
            talkerid_c = cl.get("talkerid")
            groups = _all_groups(con)
            wanted = []
            for g in groups:
                if not watch or g["name"] in watch or g["id"] in watch \
                        or any(w in g["name"] for w in watch if isinstance(w, str)):
                    wanted.append(g)
            now_ts = _now().timestamp()
            result = {"engine": "merge_all.db(微信3.x)", "pulled_at":
                      _now().strftime("%Y-%m-%d %H:%M:%S"), "db": db, "groups": []}
            for g in wanted:
                key = g["id"]
                marks = bm.get(key) or {}
                last_ts = float(marks.get("ts", 0) or 0)
                if not last_ts:
                    last_ts = now_ts - max_hours * 3600
                last_lid = int(marks.get("lid", 0) or 0)
                sel = "%s, %s" % (_q(time_c), _q(content_c))
                sel += (", " + _q(localid_c)) if localid_c else ""
                sel += (", " + _q(talkerid_c)) if talkerid_c else ""
                sel += (", " + _q(extra_c)) if extra_c else ""
                cond = "%s=? AND %s>?" % (_q(talker_c), _q(time_c))
                params = [key, last_ts]
                if last_lid:
                    cond += " AND (%s IS NULL OR %s>?)" % (_q(localid_c), _q(localid_c))
                    params.append(last_lid)
                if type_c:
                    cond += " AND %s=1" % _q(type_c)  # 只取文本消息
                order = "%s ASC" % (_q(localid_c) if localid_c else _q(time_c))
                msgs = []
                max_ts, max_lid = last_ts, last_lid
                for row in con.execute(
                        "SELECT %s FROM %s WHERE %s ORDER BY %s LIMIT 2000" % (
                            sel, _q(t), cond, order),
                        params):
                    ts = float(row[0] or 0)
                    text = (row[1] or "").strip()
                    lid = int(row[2]) if localid_c and row[2] else 0
                    if not text or text.startswith("<") or "sysmsg" in text[:30].lower():
                        continue
                    talkerid_val = None
                    extra_val = None
                    idx = 2 + (1 if localid_c else 0)
                    if talkerid_c:
                        talkerid_val = row[idx]
                        idx += 1
                    if extra_c:
                        extra_val = row[idx]
                    sender = _sender_name(talkerid_val, extra_val, contact)
                    dt = datetime.fromtimestamp(ts)
                    msgs.append({
                        "ts": int(ts), "time": dt.strftime("%H:%M"),
                        "date": dt.strftime("%Y-%m-%d"), "sender": sender, "text": text[:2000]})
                    if (ts, lid) > (max_ts, max_lid):
                        max_ts, max_lid = ts, lid
                if msgs:
                    bm[key] = {"ts": max_ts, "lid": max_lid, "name": g["name"]}
                result["groups"].append({
                    "id": key, "name": g["name"], "count": len(msgs), "messages": msgs})
        finally:
            con.close()
            if tmp:
                try:
                    os.remove(tmp)
                except Exception:
                    pass
    _save_bookmarks(bm)
    # 落盘 latest.json + latest.md
    os.makedirs(INTAKE_DIR, exist_ok=True)
    with open(os.path.join(INTAKE_DIR, "latest.json"), "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=1)
    lines = ["# 微信情报拉取 %s（%s）" % (result["pulled_at"], result.get("engine", "")), ""]
    total = 0
    for g in result["groups"]:
        total += g["count"]
        lines.append("## %s（%d 条）" % (g["name"], g["count"]))
        for m in g["messages"]:
            lines.append("- [%s %s] %s：%s" % (m["date"], m["time"], m["sender"], m["text"]))
        lines.append("")
    with open(os.path.join(INTAKE_DIR, "latest.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print("[ok] 拉取完成（%s）：%d 个监控群 · 新消息 %d 条" % (
        result.get("engine", ""), len(result["groups"]), total))
    print("     明细：data/wechat_intake/latest.json / latest.md（书签已推进，下次续读）")
    if total == 0:
        print("     （无新消息）")


# ---------------------------------------------------------------- 事件登记与确认
def _next_event_no(rows):
    today = _now().strftime("%Y%m%d")
    prefix = "WX%s-" % today
    n = 0
    for r in rows:
        no = r.get("事件编号", "")
        if no.startswith(prefix):
            try:
                n = max(n, int(no.split("-")[-1]))
            except Exception:
                pass
    return "%s%03d" % (prefix, n + 1)


def cmd_add_event(group, sender, category, text, intent="", date=None, time_=None):
    rows = _read_csv(EVENTS_PATH)
    if category not in CATEGORIES:
        print("[warn] 分类「%s」不在 %s，仍按原样登记" % (category, "/".join(CATEGORIES)))
    if intent:
        try:
            json.loads(intent)
        except Exception:
            print("[warn] 意图不是合法 JSON，已按纯文本登记（后续可手动补）")
            intent = ""
    now = _now()
    row = {
        "事件编号": _next_event_no(rows),
        "日期": date or now.strftime("%Y-%m-%d"),
        "时间": time_ or now.strftime("%H:%M"),
        "来源群": group, "发送人": sender, "分类": category,
        "原文": text, "意图": intent, "状态": "待确认", "确认时间": "", "写入结果": "",
    }
    rows.append(row)
    _write_csv(EVENTS_PATH, EVENT_COLS, rows)
    print("[ok] 事件已登记（待确认）：%s [%s] %s" % (row["事件编号"], category, text[:50]))


def cmd_list(status=None):
    rows = _read_csv(EVENTS_PATH)
    if status:
        rows = [r for r in rows if r.get("状态") == status]
    if not rows:
        print("（没有%s事件）" % (status or ""))
        return
    for r in rows[-50:]:
        print("%s %-12s %-10s %-8s %-6s %s" % (
            r.get("事件编号", ""), r.get("日期", ""), (r.get("来源群", ""))[:10],
            r.get("分类", ""), r.get("状态", ""), r.get("原文", "")[:36]))


def _get_adapter_for_business():
    """业务表写入优先 SeaTable 云端（防本地写被次日同步覆盖）；无配置退回本地。"""
    cfg = load_config() or {}
    sc = cfg.get("seatable") or {}
    if sc.get("api_token") and sc.get("base_uuid"):
        try:
            from adapters.seatable import SeaTableAdapter
            return SeaTableAdapter(sc["api_token"], sc.get("server") or "https://cloud.seatable.cn",
                                   sc["base_uuid"]), "seatable云"
        except Exception as e:
            print("[warn] SeaTable 初始化失败，退回本地：%s" % e)
    from adapters.local import LocalAdapter
    return LocalAdapter(os.path.join(HERE, "data"), cfg), "local"


def cmd_approve(no):
    rows = _read_csv(EVENTS_PATH)
    hits = [r for r in rows if r.get("事件编号") == no]
    if not hits:
        print("[skip] 没有事件 %s（先 list 看编号）" % no)
        return
    ev = hits[0]
    if ev.get("状态") != "待确认":
        print("[skip] 事件 %s 状态已是「%s」" % (no, ev.get("状态")))
        return
    print("确认事件：%s [%s] %s" % (no, ev.get("分类"), ev.get("原文", "")[:60]))
    intent_raw = (ev.get("意图") or "").strip()
    results = []
    if not intent_raw or intent_raw in ("{}", "[]"):
        results.append("无结构化意图，仅确认留痕（未写业务表）")
    else:
        try:
            intents = json.loads(intent_raw)
            if isinstance(intents, dict):
                intents = [intents]
        except Exception as e:
            print("[错误] 意图 JSON 解析失败：%s（事件保持待确认）" % e)
            return
        adapter, where = _get_adapter_for_business()
        print("  写入目标：%s" % where)
        from intake import Intent
        for it in intents:
            try:
                obj = Intent(it.get("op", "log"), it.get("table", "工作日志"),
                             it.get("data") or {}, it.get("row_id"), it.get("reason", ""))
                if obj.op == "log":
                    rid = adapter.append_row("工作日志", obj.data or
                                             {"日期": _now().strftime("%Y-%m-%d"),
                                              "原话": ev.get("原文", ""), "类型": "进度"})
                    results.append("已记工作日志 row=%s" % rid)
                elif obj.op == "append":
                    rid = adapter.append_row(obj.table, obj.data)
                    results.append("已写入「%s」row=%s" % (obj.table, rid))
                elif obj.op == "update":
                    if not obj.row_id:
                        results.append("跳过 update：意图缺 row_id")
                        continue
                    adapter.update_row(obj.table, obj.row_id, obj.data)
                    results.append("已更新「%s」%s：%s" % (obj.table, obj.row_id,
                                                          ",".join(obj.data.keys())))
                else:
                    results.append("未知 op=%s 跳过" % obj.op)
            except Exception as e:
                results.append("失败（%s）：%s" % (it.get("table"), e))
    for r in rows:
        if r.get("事件编号") == no:
            r["状态"] = "已确认"
            r["确认时间"] = _now().strftime("%Y-%m-%d %H:%M")
            r["写入结果"] = "；".join(results)[:500]
    _write_csv(EVENTS_PATH, EVENT_COLS, rows)
    for x in results:
        print("  · %s" % x)
    print("[ok] 事件 %s 已确认，结果已回填" % no)


def cmd_ignore(no):
    rows = _read_csv(EVENTS_PATH)
    for r in rows:
        if r.get("事件编号") == no and r.get("状态") == "待确认":
            r["状态"] = "已忽略"
            r["确认时间"] = _now().strftime("%Y-%m-%d %H:%M")
    _write_csv(EVENTS_PATH, EVENT_COLS, rows)
    print("[ok] 事件 %s 已忽略（留痕不写业务表）" % no)


def cmd_clear_demo():
    rows = _read_csv(EVENTS_PATH)
    keep = [r for r in rows if not (r.get("来源群", "").startswith("示例")
                                    and r.get("状态") == "待确认")]
    removed = len(rows) - len(keep)
    _write_csv(EVENTS_PATH, EVENT_COLS, keep)
    print("[ok] 已清除 %d 条示例事件" % removed)


# ---------------------------------------------------------------- summary
# 「需关注」关键词：命中即在摘要里单独拎出来，避免人工翻全量对话。
# 只做粗分类标注，不下结论——是否真需要处理由 AI/人判断。
FOCUS_RULES = [
    ("交期", ["交期", "延期", "delay", "推迟", "发货", "出货", "几号到",
             "什么时候到", "交付", "排产"]),
    ("价格", ["涨价", "降价", "报价", "单价", "调价", "含税", "税点", "结算",
             "付款", "尾款"]),
    ("供应", ["缺料", "缺货", "停产", "断供", "备货", "库存", "到料", "催货",
             "催单", "补料", "欠料"]),
    ("决策", ["确认", "定下来", "就这样", "拍板", "同意", "批准", "驳回",
             "改单", "按这个", "可以了"]),
    ("风险", ["不良", "返工", "退货", "投诉", "异常", "故障", "报废", "客诉",
             "报废", "问题", "报错"]),
]


def _match_focus(text):
    """返回该条消息命中的关注类别（可能多个）。"""
    low = (text or "").lower()
    return [cat for cat, kws in FOCUS_RULES if any(k in low for k in kws)]


def _resolve_sender(wdb, members, su, cache):
    """wxid -> 可读昵称。别名映射 > 群成员表 > 联系人表 > 退回 wxid。

    sender_aliases（config.yaml wechat 段）优先级最高：同一人多个账号/多个昵称
    （如企微号显示刘俊良、个人号昵称 Dylan-刘）时，统一聚合到一个名字下。
    member_names.json 只覆盖 get_groups() 返回的群成员，群里不活跃/非好友的人
    经常查不到，所以必须用 get_nickname 兜底（实测可把 wxid_xxx 解成人名）。
    结果按会话缓存，避免同一发言人反复查库。
    """
    if not su:
        return "群友"
    if su in cache:
        return cache[su]
    alias = (_wechat_cfg().get("sender_aliases") or {}).get(su)
    if alias:
        cache[su] = alias
        return alias
    name = members.get(su)
    if not name:
        try:
            name = (wdb.get_nickname(su) or "").strip()
        except Exception:
            name = ""
    name = name or su
    cache[su] = name
    return name


# 微信把「发送者账号」冗余拼在正文前，形如 "helei270640: 你告诉他拆不了"。
# 摘要里再显示一遍发言人就没必要了，这里剥掉。
# 只匹配纯 ASCII 账号（无中文、无空格），避免误伤 "OK: 收到" 这类正常文本。
# 实测分隔符是换行不是空格（"wxid_xxx:\n正文"），全角冒号也见过，两种都收。
_SENDER_PREFIX = re.compile(r"^[A-Za-z0-9_.\-@]{3,40}[:：]\s*")


def _strip_sender_prefix(text):
    m = _SENDER_PREFIX.match(text or "")
    if not m:
        return text
    rest = text[m.end():].strip()
    return rest or text


def _summary_targets(wdb, group_args, all_groups):
    """解析要摘要的群。返回 (群列表, 空则报错原因)。"""
    groups = wdb.get_groups()
    if all_groups:
        return groups, None
    want = []
    for g in (group_args or []):
        want.extend([w.strip() for w in str(g).split(",") if w.strip()])
    if not want:
        want = [w for w in (_wechat_cfg().get("watch_groups") or []) if w]
        if not want:
            return [], "未指定群且 config.yaml 的 watch_groups 为空。"\
                       "用 --group 指定，或 --all 扫全部群。"
    picked = []
    for g in groups:
        name = g.get("name") or ""
        if name in want or g.get("username") in want \
                or any(w in name for w in want):
            picked.append(g)
    if not picked:
        return [], "没有匹配到任何群（要匹配：%s）。"\
                   "用 python wechat_intake.py groups 看准确群名。" % "、".join(want)
    return picked, None


def _render_summary_md(res, skip_empty=True):
    """把 summary 结果渲染成 Markdown（给人看，也方便 AI 直接读）。

    skip_empty（默认 True）：0 条消息的群不单独出章节，改为末尾一行汇总。
    监控群动辄几十个，绝大多数当天没消息——逐个输出空章节会占掉近三成篇幅，
    而 AI 读这些内容时纯属噪声。
    """
    groups = res["groups"]
    empty = [g for g in groups if not g["messages"]]
    nonempty = [g for g in groups if g["messages"]]
    shown = nonempty if skip_empty else groups
    L = ["# 微信群聊摘要",
         "",
         "- 时间窗口：%s ~ %s（近 %s 小时）" % (res["from"], res["to"], res["window_hours"]),
         "- 生成时间：%s" % res["generated_at"],
         # 注意用 nonempty 而非 shown：--keep-empty 时 shown 就是全量群，
         # 拿它当有消息数会把 0 条群也算进去（实测显示成「有消息 4 / 无消息 3」）。
         "- 群数：%d（有消息 %d / 无消息 %d）" % (len(groups), len(nonempty), len(empty)),
         ""]
    total_msg = sum(g["count"] for g in groups)
    total_focus = sum(len(g["focus"]) for g in groups)
    L.append("- 消息总数：%d，其中需关注 %d 条" % (total_msg, total_focus))
    L.append("")

    for g in shown:
        L.append("---")
        L.append("")
        L.append("## %s（%d 条）" % (g["name"], g["count"]))
        if g.get("truncated"):
            L.append("")
            L.append("> ⚠️ 已达扫描上限（--limit %d），窗口内可能还有更早消息未覆盖，"
                     "如需完整请调大 --limit。" % g["limit"])
        L.append("")
        if not g["messages"]:
            L.append("_该时间窗口内没有消息。_")
            L.append("")
            continue
        L.append("**发言分布**：" + "、".join(
            "%s %d" % (n, c) for n, c in g["stats"].items()))
        L.append("")
        if g["focus"]:
            L.append("### 需关注")
            L.append("")
            for m in g["focus"]:
                L.append("- [%s %s] **%s**（%s）：%s" % (
                    m["date"], m["time"], m["sender"],
                    "/".join(m["focus"]),
                    m["text"][:200].replace("\n", " ")))
            L.append("")
        L.append("### 对话记录")
        L.append("")
        for m in g["messages"]:
            tag = "" if m["type"] == "文本" else " <%s>" % m["type"]
            L.append("- `[%s %s]` **%s**%s：%s" % (
                m["date"], m["time"], m["sender"], tag,
                m["text"].replace("\n", " ")))
        L.append("")

    if skip_empty and empty:
        L.append("---")
        L.append("")
        names = "、".join(g["name"] for g in empty)
        if len(names) > 400:
            names = names[:400] + "…"
        L.append("_另有 %d 个监控群在该窗口内无消息：%s_" % (len(empty), names))
        L.append("")
    return "\n".join(L)


def cmd_summary(group=None, hours=24, limit=2000, all_groups=False,
                as_json=False, out=None, keep_empty=False):
    """群聊摘要：按时间窗口导出指定群的对话记录 + 统计 + 需关注标注。

    与 pull 的区别：pull 是增量事件流（书签续读，只取文本、只进待确认），
    summary 是一次性回溯（按小时窗口，保留全部消息类型，供人工/AI 速读）。

    keep_empty=False（默认）时，0 条消息的群不单独出章节，只在末尾汇总。
    """
    wdb = _load_wx4()
    if not wdb:
        print("[!] 引擎A不可用：保持微信 PC 版登录后重试；"
              "若报缺 cryptography，请给当前解释器 pip install cryptography pyyaml")
        return
    groups, err = _summary_targets(wdb, group, all_groups)
    if err:
        print("[!] " + err)
        return

    members = _wx4_member_names(wdb)
    nick_cache = {}                         # wxid -> 昵称，按本次运行缓存
    now = _now()
    cutoff = now.timestamp() - hours * 3600
    res = {
        "engine": "wx4(微信4.x直读)",
        "window_hours": hours,
        "from": datetime.fromtimestamp(cutoff).strftime("%Y-%m-%d %H:%M"),
        "to": now.strftime("%Y-%m-%d %H:%M"),
        "generated_at": now.strftime("%Y-%m-%d %H:%M:%S"),
        "groups": [],
    }

    for g in groups:
        try:
            raw = wdb.get_messages(g["username"], limit=limit)
        except Exception as e:
            print("[warn] 读取群「%s」失败：%s" % (g.get("name"), e))
            continue
        msgs = []
        for m in raw:                      # get_messages 返回倒序（最新在前）
            ts = int(m.get("create_time") or 0)
            if ts < cutoff:
                break
            if ts > now.timestamp() + 3600:   # 未来时间戳 = 脏数据
                continue
            mtype = m.get("type") or ""
            text = (m.get("content") or "").strip()
            # 系统提示、「[文本]」空占位一律不进摘要（后者是解析失败的脏行）
            if mtype == "系统消息" or not text or text == "[文本]":
                continue
            if mtype == "文本" and (
                    text.startswith("<") or "sysmsg" in text[:30].lower()):
                continue                    # 撤回提示 / 系统 XML
            text = _strip_sender_prefix(text)
            su = m.get("sender_username") or ""
            dt = datetime.fromtimestamp(ts)
            msgs.append({
                "ts": ts,
                "time": dt.strftime("%H:%M"),
                "date": dt.strftime("%Y-%m-%d"),
                "sender": _resolve_sender(wdb, members, su, nick_cache),
                "type": mtype,
                "text": text[:2000],
                "focus": _match_focus(text),
            })
        truncated = bool(raw) and len(raw) >= limit and bool(msgs) \
            and msgs[-1]["ts"] >= cutoff
        msgs.reverse()                      # 正序：从早到晚

        stats = {}
        for m in msgs:
            stats[m["sender"]] = stats.get(m["sender"], 0) + 1
        res["groups"].append({
            "id": g["username"], "name": g["name"], "count": len(msgs),
            "limit": limit, "truncated": truncated,
            "stats": dict(sorted(stats.items(), key=lambda kv: -kv[1])),
            "focus": [m for m in msgs if m["focus"]],
            "messages": msgs,
        })

    if as_json:
        payload = json.dumps(res, ensure_ascii=False, indent=2)
    else:
        # JSON 始终保留全部群（程序消费要完整数据）；Markdown 默认折叠空群
        payload = _render_summary_md(res, skip_empty=not keep_empty)
    if out:
        os.makedirs(os.path.dirname(os.path.abspath(out)) or ".", exist_ok=True)
        with open(out, "w", encoding="utf-8") as f:
            f.write(payload)
        print("[ok] 已写入 %s（%d 群 / %d 条消息）" % (
            out, len(res["groups"]), sum(g["count"] for g in res["groups"])))
    else:
        print(payload)


def main():
    ap = argparse.ArgumentParser(description="微信情报反哺")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("doctor")
    sub.add_parser("groups")
    sub.add_parser("pull")
    p = sub.add_parser("add-event")
    p.add_argument("--group", required=True)
    p.add_argument("--sender", default="")
    p.add_argument("--category", required=True, choices=CATEGORIES)
    p.add_argument("--text", required=True)
    p.add_argument("--intent", default="")
    p.add_argument("--date", default=None)
    p.add_argument("--time", dest="time_", default=None)
    p = sub.add_parser("list")
    p.add_argument("--status", default=None)
    p = sub.add_parser("approve")
    p.add_argument("no")
    p = sub.add_parser("ignore")
    p.add_argument("no")
    sub.add_parser("clear-demo")
    p = sub.add_parser("summary")
    p.add_argument("--group", action="append", default=[],
                   help="群名（可多次指定，也可逗号分隔，支持部分匹配）；"
                        "不给则用 config.yaml 的 watch_groups")
    p.add_argument("--hours", type=float, default=24, help="时间窗口小时数，默认 24")
    p.add_argument("--limit", type=int, default=2000,
                   help="每群最多扫描的消息条数，默认 2000")
    p.add_argument("--all", dest="all_groups", action="store_true",
                   help="摘要全部群（忽略 watch_groups）")
    p.add_argument("--json", dest="as_json", action="store_true",
                   help="输出 JSON 而非 Markdown")
    p.add_argument("--keep-empty", action="store_true",
                   help="保留 0 条消息的群章节（默认折叠为末尾汇总一行）")
    p.add_argument("--out", default=None, help="写入文件（默认打印到 stdout）")
    a = ap.parse_args()
    {"doctor": cmd_doctor, "groups": cmd_groups, "pull": cmd_pull,
     "add-event": lambda: cmd_add_event(a.group, a.sender, a.category, a.text,
                                         a.intent, a.date, a.time_),
     "list": lambda: cmd_list(a.status),
     "approve": lambda: cmd_approve(a.no),
     "ignore": lambda: cmd_ignore(a.no),
     "clear-demo": cmd_clear_demo,
     "summary": lambda: cmd_summary(a.group, a.hours, a.limit, a.all_groups,
                                    a.as_json, a.out, a.keep_empty)}[a.cmd]()


if __name__ == "__main__":
    main()
