#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""wxwatch.py — 微信实时监听哨兵（第二大脑的耳朵）。

基于 wxengine 的 Listener（1 秒轮询 + WAL 增量）监听监控群的新消息：
  命中关键词 → 自动登记「微信事件」（状态=待确认，同 wechat_intake 事件表）
  高危关键词（交期/涨价/停产/催货）→ 写通知发件箱 data/notify_outbox.json，
  由 AI 会话侧经 Agent Mail 推送（进程解耦，哨兵只管听和写，不碰网络 API）。

命令：
  python wxwatch.py once [--minutes 5]   # 单次扫描：过去 N 分钟的消息（测试/低频模式）
  python wxwatch.py watch                # 常驻监听（Ctrl+C 退出），间隔走 config
  python wxwatch.py status               # 查看哨兵状态（事件数/发件箱积压）

关键词分两级：
  高危（立即通知）：交期、延期、推迟、涨价、涨价、停产、缺货、断供、催货、催料
  一般（仅登记事件）：价格、报价、库存、到货、发货、进度、交付、付款、尾款

依赖：微信 4.x PC 版保持登录；pip install cryptography
"""
import argparse
import json
import os
import sys
import time
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

DATA = os.path.join(HERE, "data")
INTAKE_DIR = os.path.join(DATA, "wechat_intake")
OUTBOX = os.path.join(INTAKE_DIR, "notify_outbox.json")
STATE = os.path.join(INTAKE_DIR, "wxwatch_state.json")
LOG = os.path.join(INTAKE_DIR, "wxwatch.log")

import wechat_intake as wi  # 复用引擎加载、事件表、配置

# 高危：立即写发件箱
HOT_KEYWORDS = [
    "交期", "延期", "推迟", "拖延", "延后",
    "涨价", "提价", "涨价了", "要涨",
    "停产", "断供", "缺货", "缺料", "停线",
    "催货", "催料", "催一下", "快点发", "什么时候能到",
]
# 一般：只登记事件，不通知
WARM_KEYWORDS = [
    "价格", "报价", "单价", "优惠",
    "库存", "到货", "发货", "出库", "入库",
    "进度", "交付", "出货", "完工",
    "付款", "尾款", "定金", "收款",
    "良率", "不良", "坏点", "返修",
]

# 分类推断
CATEGORY_RULES = [
    (("交期", "延期", "推迟", "延后", "拖延"), "交期变更"),
    (("涨价", "提价", "要涨", "降价", "价格", "报价", "单价"), "价格变动"),
    (("停产", "断供", "停线"), "停产通知"),
    (("催货", "催料", "快点发", "什么时候能到"), "催货"),
    (("库存", "缺货", "缺料"), "库存"),
    (("到货", "发货", "出货", "进度", "交付", "完工"), "进度"),
]


def _classify(text):
    for kws, cat in CATEGORY_RULES:
        if any(k in text for k in kws):
            return cat
    return "其他"


def _match_level(text):
    hot = [k for k in HOT_KEYWORDS if k in text]
    if hot:
        return "hot", hot
    warm = [k for k in WARM_KEYWORDS if k in text]
    if warm:
        return "warm", warm
    return None, []


def _load_state():
    if os.path.exists(STATE):
        try:
            return json.load(open(STATE, encoding="utf-8"))
        except Exception:
            pass
    return {"watched": {}, "events": 0, "notified": 0, "started_at": ""}


def _save_state(st):
    os.makedirs(INTAKE_DIR, exist_ok=True)
    json.dump(st, open(STATE, "w", encoding="utf-8"), ensure_ascii=False, indent=1)


def _append_log(line):
    os.makedirs(INTAKE_DIR, exist_ok=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write("[%s] %s\n" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), line))


def _enqueue_notify(subject, body, level="hot"):
    """写通知发件箱；hot 级别同时直接推送企微（秒级触达），失败不影响发件箱。"""
    box = []
    if os.path.exists(OUTBOX):
        try:
            box = json.load(open(OUTBOX, encoding="utf-8"))
        except Exception:
            box = []
    box.append({
        # 必须带 id，与 notify.py 的 _next_id 逻辑一致。缺 id 的条目一旦未发送，
        # `notify.py mark-sent --ids` 就定位不到它，会永远卡在发件箱里。
        "id": max([b.get("id", 0) for b in box], default=0) + 1,
        "subject": subject,
        "body": body,
        "level": level,
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "sent": False,
    })
    os.makedirs(INTAKE_DIR, exist_ok=True)
    json.dump(box[-200:], open(OUTBOX, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    if level == "hot":
        try:
            import wecom_push
            ok, _ = wecom_push.push_markdown("**%s**\n\n%s" % (subject, body))
            if ok:
                box[-1]["sent"] = True
                box[-1]["sent_via"] = "wecom"
                box[-1]["sent_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                json.dump(box[-200:], open(OUTBOX, "w", encoding="utf-8"),
                          ensure_ascii=False, indent=1)
                _append_log("[wecom] 高危已直推企微：%s" % subject)
        except Exception as e:
            _append_log("[wecom] 直推失败（保留发件箱走兜底）：%s" % e)


def _group_names(wdb):
    try:
        return {g["username"]: g["name"] for g in wdb.get_groups()}
    except Exception:
        return {}


def _handle_message(msg, group_names, member_names, st, notify_hot=True):
    """处理一条新消息：分类 → 登记事件 → 高危写发件箱。"""
    if msg.get("type") != "文本":
        return None
    text = (msg.get("content") or "").strip()
    if not text or text.startswith("<"):
        return None
    user = msg.get("username", "")
    if "@chatroom" not in user:
        return None
    gname = group_names.get(user, user)
    level, kws = _match_level(text)
    if not level:
        return None
    cat = _classify(text)
    su = msg.get("sender_username") or ""
    # 别名映射优先（config.yaml wechat.sender_aliases）：同一人多账号/多昵称
    # 统一成一个名字（如 jixu911 昵称 Dylan-刘 -> 刘俊良），便于推送里一眼认人
    aliases = (wi.load_config().get("wechat") or {}).get("sender_aliases") or {}
    sender = aliases.get(su) or member_names.get(su) or (su if su else "群友")
    ts = msg.get("create_time") or 0
    dt = datetime.fromtimestamp(ts) if ts else datetime.now()
    # 登记事件（复用 wechat_intake 的事件表，待确认状态）
    try:
        wi.cmd_add_event(
            group=gname, sender=sender, category=cat, text=text[:400],
            date=dt.strftime("%Y-%m-%d"), time_=dt.strftime("%H:%M"))
        st["events"] += 1
    except Exception as e:
        _append_log("add-event 失败：%s" % e)
        return None
    if level == "hot" and notify_hot:
        _enqueue_notify(
            subject="【微信情报·%s】%s" % (cat, gname[:20]),
            body="[%s %s] %s：\n%s\n\n（事件已登记待确认，回复「确认 WX... 事件」即可写入业务表）"
                 % (dt.strftime("%m-%d"), dt.strftime("%H:%M"), sender, text[:300]))
        st["notified"] += 1
    _append_log("[%s] %s %s %s" % (level, gname, sender, text[:60]))
    return {"group": gname, "sender": sender, "category": cat, "level": level, "text": text[:80]}


def _member_names(wdb):
    p = os.path.join(INTAKE_DIR, "member_names.json")
    if os.path.exists(p):
        try:
            return json.load(open(p, encoding="utf-8"))
        except Exception:
            pass
    m = {}
    try:
        for g in wdb.get_groups():
            for mem in g.get("members") or []:
                if mem.get("username") and mem.get("nick_name"):
                    m[mem["username"]] = mem["nick_name"]
    except Exception:
        pass
    return m


def cmd_once(minutes):
    """单次扫描：过去 N 分钟监控群的消息（用于测试与低频定时任务）。"""
    wdb = wi._load_wx4()
    if not wdb:
        print("[skip] 引擎A不可用（微信4.x未登录）", flush=True)
        return
    cfg = wi._wechat_cfg()
    watch = cfg.get("watch_groups") or []
    groups = wdb.get_groups()
    wanted = [g for g in groups if not watch or g["name"] in watch
              or g["username"] in watch or any(w in g["name"] for w in watch if isinstance(w, str))]
    names = {g["username"]: g["name"] for g in wanted}
    members = _member_names(wdb)
    since_ms = (time.time() - minutes * 60) * 1000
    st = _load_state()
    st["started_at"] = st.get("started_at") or datetime.now().strftime("%Y-%m-%d %H:%M")
    hits = []
    for g in wanted:
        try:
            msgs = [m for m in wdb.get_messages(g["username"], limit=100)
                    if (m.get("sort_seq") or 0) >= since_ms]
        except Exception:
            continue
        msgs.reverse()
        for m in msgs:
            r = _handle_message(m, names, members, st)
            if r:
                hits.append(r)
    _save_state(st)
    print("[ok] 扫描 %d 个监控群 · 过去 %d 分钟 · 命中 %d 条（登记事件 %d，通知 %d）"
          % (len(wanted), minutes, len(hits), st["events"], st["notified"]))
    for h in hits[:10]:
        print("  [%s][%s] %s · %s：%s" % (
            "高" if h["level"] == "hot" else "般", h["category"],
            h["group"][:16], h["sender"], h["text"][:40]))
    if len(hits) > 10:
        print("  …（其余 %d 条见 data/wechat_intake/微信事件.csv 与 wxwatch.log）" % (len(hits) - 10))


def cmd_watch():
    """常驻监听：Listener 1 秒轮询，自动发现新会话。"""
    wdb = wi._load_wx4()
    if not wdb:
        print("[skip] 引擎A不可用（微信4.x未登录）", flush=True)
        return
    sys.path.insert(0, os.path.join(HERE, "wxengine"))
    from wa_db import Listener
    cfg = wi._wechat_cfg()
    watch = cfg.get("watch_groups") or []
    groups = wdb.get_groups()
    wanted = [g for g in groups if not watch or g["name"] in watch
              or g["username"] in watch or any(w in g["name"] for w in watch if isinstance(w, str))]
    names = {g["username"]: g["name"] for g in wanted}
    members = _member_names(wdb)
    st = _load_state()
    st["started_at"] = datetime.now().strftime("%Y-%m-%d %H:%M")
    _save_state(st)
    listener = Listener(wdb, interval=1.0)
    stats = {"n": 0}

    def on_msg(msg, _listener):
        if "@chatroom" not in (msg.get("username") or ""):
            return
        if names and msg.get("username") not in names:
            return
        r = _handle_message(msg, names, members, st)
        if r:
            stats["n"] += 1
            _save_state(st)

    for g in wanted:
        listener.add_listener(g["username"], on_msg)
    listener.add_all(on_msg, discover=True)
    print("[ok] 哨兵启动：%d 个监控群 · 1 秒轮询 · Ctrl+C 退出" % len(wanted), flush=True)
    print("     高危关键词=%d 个（命中即通知）；日志 data/wechat_intake/wxwatch.log" % len(HOT_KEYWORDS), flush=True)
    listener.start()
    try:
        while True:
            time.sleep(30)
    except KeyboardInterrupt:
        print("\n[ok] 哨兵停止。本次共命中 %d 条" % stats["n"], flush=True)
    finally:
        listener.stop()
        _save_state(st)


def cmd_status():
    st = _load_state()
    print("== 哨兵状态 ==", flush=True)
    print("  启动时间：%s" % (st.get("started_at") or "（未启动）"))
    print("  累计登记事件：%d" % st.get("events", 0))
    print("  累计通知：%d" % st.get("notified", 0))
    if os.path.exists(OUTBOX):
        try:
            box = json.load(open(OUTBOX, encoding="utf-8"))
            pend = [b for b in box if not b.get("sent")]
            print("  发件箱：%d 条未发送 / 共 %d 条" % (len(pend), len(box)))
            for b in pend[:5]:
                print("    · [%s] %s" % (b.get("created_at", "")[-8:], b.get("subject", "")))
        except Exception:
            pass
    # 最近日志
    if os.path.exists(LOG):
        lines = open(LOG, encoding="utf-8").read().strip().split("\n")
        print("  最近命中：", flush=True)
        for l in lines[-5:]:
            print("    %s" % l[:100], flush=True)


def main():
    ap = argparse.ArgumentParser(description="微信实时监听哨兵")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("once")
    p.add_argument("--minutes", type=int, default=5)
    sub.add_parser("watch")
    sub.add_parser("status")
    a = ap.parse_args()
    {"once": lambda: cmd_once(a.minutes),
     "watch": cmd_watch, "status": cmd_status}[a.cmd]()


if __name__ == "__main__":
    main()
