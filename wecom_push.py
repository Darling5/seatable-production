#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""wecom_push.py — 企微 AI Bot 推送通道（主动触达，无需确认流程）。

协议：WorkBuddy 内置 wecom-cli（企业微信 AI Bot）。
  - 授权凭证存 ~/.config/wecom（wecom-cli auth init 扫码后自动生成）
  - 发送目标：授权人本人（identity whoami 的 userid 可直接作 chat_id）
  - 消息格式：markdown（20480 字节上限），支持加粗/换行

命令：
  python wecom_push.py check          # 体检：CLI 路径 + 授权状态
  python wecom_push.py whoami         # 显示授权人身份
  python wecom_push.py test           # 发一条测试消息给授权人
  python wecom_push.py push --subject "标题" --body "正文"   # 推送一条
  python wecom_push.py flush-outbox   # 把 notify.py 发件箱未发送条目全部推送并标记

定位：自动化任务（每日 9 点）与 wxwatch 高危告警的首选推送出口；
Agent Mail 作为兜底（企微失败时回退邮件）。
"""
import argparse
import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
INTAKE_DIR = os.path.join(HERE, "data", "wechat_intake")
OUTBOX = os.path.join(INTAKE_DIR, "notify_outbox.json")
WECOM_HOME = os.path.expanduser("~/.config/wecom")

# CLI 定位：优先 PATH，其次 WorkBuddy node 目录（npm install -g 装在这里）
_NODE_BIN = r"C:\Users\11430\.workbuddy\binaries\node\versions\22.22.2-2"
_CLI_JS = os.path.join(_NODE_BIN, "node_modules", "@wecom", "cli", "bin", "wecom.js")
_NODE_EXE = os.path.join(_NODE_BIN, "node.exe")


def _cli_env():
    env = dict(os.environ)
    env["PATH"] = _NODE_BIN + os.pathsep + env.get("PATH", "")
    return env


def run_cli(args, timeout=30):
    """执行 wecom-cli，返回 (ok, stdout)。优先 PATH 里的 wecom-cli，失败则用 node 直跑 cli.js。"""
    cmds = [["wecom-cli"] + args]
    if os.path.exists(_CLI_JS) and os.path.exists(_NODE_EXE):
        cmds.append([_NODE_EXE, _CLI_JS] + args)
    last_err = ""
    for cmd in cmds:
        try:
            r = subprocess.run(cmd, capture_output=True, text=True,
                               encoding="utf-8", errors="replace",
                               env=_cli_env(), timeout=timeout)
            if r.returncode == 0 or (r.stdout and "authorized" in r.stdout):
                return True, (r.stdout or "") + (r.stderr or "")
            last_err = (r.stdout or "") + (r.stderr or "")
        except FileNotFoundError:
            last_err = "wecom-cli not found in PATH"
        except subprocess.TimeoutExpired:
            last_err = "timeout"
    return False, last_err


def is_authorized():
    ok, out = run_cli(["auth", "show"], timeout=15)
    return ok and "authorized" in out and "unauthorized" not in out


def get_userid():
    """返回授权人 userid（可直接作 chat_id）。"""
    ok, out = run_cli(["identity", "whoami"], timeout=20)
    if not ok:
        return None, out
    # whoami 输出 JSON：extra_identity_context 里含「授权真人用户身份：名字：xx\nID：<uid>」
    # 注意 \n 在 JSON 字符串里是字面量 "\\n"，正则用 \\n 匹配
    import re
    m = re.search(r"授权真人用户身份：\\n名字：([^\\]+)\\nID：([A-Za-z0-9_\-]+)", out)
    if m:
        return m.group(2).strip(), out
    m = re.search(r"授权真人用户身份：\s*名字：([^\n]+)\s*ID：([A-Za-z0-9_\-]+)", out)
    if m:
        return m.group(2).strip(), out
    # 兜底：JSON 字段
    try:
        start = out.find("{")
        if start >= 0:
            d = json.loads(out[start:out.rfind("}") + 1])
            uid = d.get("userid") or d.get("user_id") or d.get("id")
            if uid:
                return uid, out
    except Exception:
        pass
    return None, out


def push_markdown(content, chat_id=None):
    """发送 markdown 消息。chat_id 缺省 = 授权人本人。返回 (ok, detail)。"""
    if chat_id is None:
        chat_id, detail = get_userid()
        if not chat_id:
            return False, "无法获取授权人 userid: " + detail[:200]
    payload = {
        "chat_id": chat_id,
        "msg_type": "markdown",
        "markdown": {"content": content},
    }
    ok, out = run_cli(["message", "aibot", "send", "--json",
                       json.dumps(payload, ensure_ascii=False)], timeout=30)
    return ok, out


def cmd_check():
    print("== 企微推送通道体检 ==")
    print("  CLI 路径: %s" % (_CLI_JS if os.path.exists(_CLI_JS) else "(PATH 中查找)"))
    print("  凭证目录: %s -> %s" % (WECOM_HOME, "存在" if os.path.isdir(WECOM_HOME) else "不存在（未授权）"))
    ok, out = run_cli(["--version"], timeout=15)
    print("  CLI 版本: %s" % (out.strip().splitlines()[0] if ok else "不可用"))
    print("  授权状态: %s" % ("✅ 已授权" if is_authorized() else "❌ 未授权（跑 wecom-cli auth init 扫码）"))
    if not is_authorized():
        print("\n[!] 授权指引：")
        print("    1) 确保手机装了企业微信 App 并登录")
        print("    2) 运行: python wecom_push.py authorize")
        print("    3) 用企微扫描输出中的二维码/链接，CLI 自动完成绑定")


def cmd_authorize():
    ok, out = run_cli(["auth", "init", "--noninteractive", "--no-browser"], timeout=300)
    print(out[:2000])
    if is_authorized():
        print("\n[ok] 授权成功")
    else:
        print("\n[!] 授权未完成（超时或未扫码）")


def cmd_test():
    if not is_authorized():
        print("[skip] 未授权，先运行 python wecom_push.py authorize")
        return
    uid, _ = get_userid()
    print("授权人: %s" % uid)
    ok, out = push_markdown("**【联调测试】** 企微推送通道已接通。\n\n以后站会摘要 / 高危告警会直接推到你的企业微信。\n_（本条为测试消息）_")
    print("发送: %s" % ("✅ 成功" if ok else "❌ 失败"))
    if not ok:
        print(out[:400])


def cmd_push(subject, body):
    if not is_authorized():
        print("[skip] 未授权")
        return
    content = "**%s**\n\n%s" % (subject, body)
    ok, out = push_markdown(content)
    print("发送: %s" % ("✅ 成功" if ok else "❌ 失败"))
    if not ok:
        print(out[:400])


def cmd_flush_outbox():
    """把 notify.py 发件箱未发送条目推到企微，成功后标记。"""
    if not is_authorized():
        print("[skip] 未授权，企微通道不可用（回退邮件路径）")
        return
    if not os.path.exists(OUTBOX):
        print("（发件箱空）")
        return
    box = json.load(open(OUTBOX, encoding="utf-8"))
    pend = [b for b in box if not b.get("sent")]
    if not pend:
        print("（无待发送）")
        return
    sent_ids = []
    fail = 0
    for b in pend:
        content = "**%s**\n\n%s\n\n_%s · %s_" % (
            b.get("subject", ""), b.get("body", ""),
            b.get("level", ""), b.get("created_at", ""))
        ok, out = push_markdown(content[:20000])
        if ok:
            sent_ids.append(b.get("id"))
            b["sent"] = True
            b["sent_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
            b["sent_via"] = "wecom"
        else:
            fail += 1
            print("  ❌ #%s %s: %s" % (b.get("id"), b.get("subject", ""), out[:120]))
    json.dump(box, open(OUTBOX, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print("[ok] 企微推送完成：成功 %d 条 / 失败 %d 条（失败项保留在发件箱，走邮件兜底）"
          % (len(sent_ids), fail))


def main():
    ap = argparse.ArgumentParser(description="企微 AI Bot 推送")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check")
    sub.add_parser("authorize")
    sub.add_parser("whoami")
    sub.add_parser("test")
    p = sub.add_parser("push")
    p.add_argument("--subject", required=True)
    # --body 与 --body-file 二选一；长中文正文一律走 --body-file，
    # 命令行传参在 Windows 上会踩长度上限和引号转义坑。
    p.add_argument("--body", default=None)
    p.add_argument("--body-file", default=None,
                   help="从文件读取正文（UTF-8），长文本请用这个")
    sub.add_parser("flush-outbox")
    a = ap.parse_args()
    if a.cmd == "check":
        cmd_check()
    elif a.cmd == "authorize":
        cmd_authorize()
    elif a.cmd == "whoami":
        uid, out = get_userid()
        print(uid or "(获取失败)")
        if not uid:
            print(out[:300])
    elif a.cmd == "test":
        cmd_test()
    elif a.cmd == "push":
        body = a.body
        if a.body_file:
            with open(a.body_file, encoding="utf-8") as f:
                body = f.read().strip()
        if not body:
            ap.error("需要 --body 或 --body-file（且内容不能为空）")
        cmd_push(a.subject, body)
    else:
        cmd_flush_outbox()


if __name__ == "__main__":
    main()
