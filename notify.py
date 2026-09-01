#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""notify.py — 通知触达通道（第二大脑的嘴巴）。

设计：进程解耦。哨兵/摘要等生产者只写发件箱 data/wechat_intake/notify_outbox.json，
本模块负责把发件箱变成标准「待发送清单」。真正发送有两条路：
  路1（推荐，AI 会话内）：自动化/对话中的 AI 读取 --dump 输出，
     经 MCP agent-mail 工具（ListMessages/SendMessage）发送，发完 --mark-sent。
  路2（人工）：--dump 拿到内容，自己复制到任何渠道。

命令：
  python notify.py dump [--all]      # 列出未发送通知（--all 含已发送，JSON 行）
  python notify.py mark-sent --ids 1,2   # 标记已发送
  python notify.py send --subject "..." --body "..."  # 手动追加一条到发件箱
  python notify.py status            # 发件箱概览

发件箱条目：{id, subject, body, level(hot|warm|info), created_at, sent}
"""
import argparse
import json
import os
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
INTAKE_DIR = os.path.join(HERE, "data", "wechat_intake")
OUTBOX = os.path.join(INTAKE_DIR, "notify_outbox.json")


def _load():
    if not os.path.exists(OUTBOX):
        return []
    try:
        return json.load(open(OUTBOX, encoding="utf-8"))
    except Exception:
        return []


def _save(box):
    os.makedirs(INTAKE_DIR, exist_ok=True)
    json.dump(box[-200:], open(OUTBOX, "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)


def _next_id(box):
    return max([b.get("id", 0) for b in box], default=0) + 1


def enqueue(subject, body, level="info"):
    box = _load()
    box.append({"id": _next_id(box), "subject": subject, "body": body,
                "level": level, "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "sent": False})
    _save(box)
    return box[-1]["id"]


def cmd_dump(show_all=False):
    box = _load()
    items = box if show_all else [b for b in box if not b.get("sent")]
    if not items:
        print("（发件箱空）")
        return
    for b in items:
        print(json.dumps(b, ensure_ascii=False))


def cmd_mark_sent(ids):
    box = _load()
    n = 0
    for b in box:
        if b.get("id") in ids and not b.get("sent"):
            b["sent"] = True
            b["sent_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            n += 1
    _save(box)
    print("[ok] 已标记 %d 条为已发送" % n)


def cmd_send(subject, body, level):
    i = enqueue(subject, body, level)
    print("[ok] 已追加发件箱 #%d：%s" % (i, subject))


def cmd_status():
    box = _load()
    pend = [b for b in box if not b.get("sent")]
    print("== 发件箱 ==")
    print("  未发送 %d / 共 %d 条" % (len(pend), len(box)))
    for b in pend[:10]:
        print("  #%d [%s] %s（%s）" % (b.get("id", 0), b.get("level", ""),
                                       b.get("subject", ""), b.get("created_at", "")))
    if len(pend) > 10:
        print("  …（其余 %d 条）" % (len(pend) - 10))
    print("  发送路径：AI 会话读 `notify.py dump` → agent-mail 发送 → `notify.py mark-sent`")


def main():
    ap = argparse.ArgumentParser(description="通知发件箱")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("dump")
    p.add_argument("--all", action="store_true")
    p = sub.add_parser("mark-sent")
    p.add_argument("--ids", required=True, help="逗号分隔的通知 id")
    p = sub.add_parser("send")
    p.add_argument("--subject", required=True)
    p.add_argument("--body", required=True)
    p.add_argument("--level", default="info", choices=["hot", "warm", "info"])
    sub.add_parser("status")
    a = ap.parse_args()
    if a.cmd == "dump":
        cmd_dump(a.all)
    elif a.cmd == "mark-sent":
        cmd_mark_sent([int(x) for x in a.ids.split(",") if x.strip().isdigit()])
    elif a.cmd == "send":
        cmd_send(a.subject, a.body, a.level)
    else:
        cmd_status()


if __name__ == "__main__":
    main()
