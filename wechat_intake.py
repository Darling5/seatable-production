#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""wechat_intake.py — 微信情报反哺（读本地微信库 → 提取事件 → 确认后写业务表）。

数据链路（零封号风险，只读本地文件，不与微信服务器通信）：
  win-wechat-summary（PyWxDump）解密生成 merge_all.db
    → 本脚本 pull 拉取监控群的新消息（书签续读，不重复）
    → AI（自动化里的专家）把消息拆成「微信事件」add-event 登记（状态=待确认）
    → 驾驶舱「微信情报台」展示待确认清单
    → 你在对话里说「确认第 N 条」→ approve 直接写 SeaTable 云端业务表
    → 写入结果回填事件行，全程留痕

命令：
  python wechat_intake.py doctor            # 体检：找库、看表结构、给指引
  python wechat_intake.py groups            # 列出所有群聊（挑监控对象）
  python wechat_intake.py pull              # 拉监控群新消息 -> data/wechat_intake/latest.json/.md
  python wechat_intake.py add-event --group 群 --sender 人 --category 分类
                      --text "原文" [--intent '{"op":"update",...}']
  python wechat_intake.py list [--status 待确认]
  python wechat_intake.py approve <编号>    # 确认事件并执行意图（写 SeaTable 云端）
  python wechat_intake.py ignore <编号>     # 忽略（不写业务表，仅留痕）
  python wechat_intake.py clear-demo        # 清掉示例事件

事件分类：交期变更 / 价格变动 / 停产通知 / 催货 / 进度 / 库存 / 其他
意图格式（同 op.py intake）：{"op":"update|append|log","table":"项目","row_id":"...",
                            "reason":"依据原话","data":{列:值}}
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


# ---------------------------------------------------------------- doctor/groups
def cmd_doctor():
    cfg = _wechat_cfg()
    print("== 微信情报体检 ==")
    print("config.wechat.enabled = %s" % cfg.get("enabled", "(未配置，默认开)"))
    print("config.wechat.db_path = %s" % (cfg.get("db_path") or "(空 → 自动搜索)"))
    print("config.wechat.watch_groups = %s" % (cfg.get("watch_groups") or "(空 → 所有群)"))
    dbs = _candidate_dbs()
    if not dbs:
        print("\n[!] 没找到 merge_all.db。请先完成 win-wechat-summary 安装：")
        print("    1) https://github.com/yanyan1115/win-wechat-summary/releases 下载 WeChat-Summary.exe")
        print("    2) 微信 PC 版保持登录，双击运行，UAC 提权点「是」")
        print("    3) 首次向导里点「自动检测微信账号」，然后点「同步」生成合并库")
        print("    4) 把 merge_all.db 的完整路径填到本技能 config.yaml 的 wechat.db_path")
        print("       （或把 win-wechat-summary 装到上面脚本会自动搜索的位置）")
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
    dbs = _candidate_dbs()
    if not dbs:
        print("[skip] 未找到微信数据库（先装 win-wechat-summary 并同步一次）——不影响其余步骤")
        return
    db = dbs[0]
    watch = cfg.get("watch_groups") or []
    max_hours = float(cfg.get("max_hours") or 48)
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
        bm = _bookmarks()
        groups = _all_groups(con)
        wanted = []
        for g in groups:
            if not watch or g["name"] in watch or g["id"] in watch \
                    or any(w in g["name"] for w in watch if isinstance(w, str)):
                wanted.append(g)
        now_ts = _now().timestamp()
        result = {"pulled_at": _now().strftime("%Y-%m-%d %H:%M:%S"), "db": db, "groups": []}
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
                    "SELECT %s FROM %s WHERE %s ORDER BY %s LIMIT 2000" % (sel, _q(t), cond, order),
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
        _save_bookmarks(bm)
        # 落盘 latest.json + latest.md
        os.makedirs(INTAKE_DIR, exist_ok=True)
        with open(os.path.join(INTAKE_DIR, "latest.json"), "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=1)
        lines = ["# 微信情报拉取 %s" % result["pulled_at"], ""]
        total = 0
        for g in result["groups"]:
            total += g["count"]
            lines.append("## %s（%d 条）" % (g["name"], g["count"]))
            for m in g["messages"]:
                lines.append("- [%s %s] %s：%s" % (m["date"], m["time"], m["sender"], m["text"]))
            lines.append("")
        with open(os.path.join(INTAKE_DIR, "latest.md"), "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
        print("[ok] 拉取完成：%d 个监控群 · 新消息 %d 条" % (
            len(result["groups"]), total))
        print("     明细：data/wechat_intake/latest.json / latest.md（书签已推进，下次续读）")
        if total == 0:
            print("     （无新消息）")
    finally:
        con.close()
        if tmp:
            try:
                os.remove(tmp)
            except Exception:
                pass


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
    a = ap.parse_args()
    {"doctor": cmd_doctor, "groups": cmd_groups, "pull": cmd_pull,
     "add-event": lambda: cmd_add_event(a.group, a.sender, a.category, a.text,
                                         a.intent, a.date, a.time_),
     "list": lambda: cmd_list(a.status),
     "approve": lambda: cmd_approve(a.no),
     "ignore": lambda: cmd_ignore(a.no),
     "clear-demo": cmd_clear_demo}[a.cmd]()


if __name__ == "__main__":
    main()
