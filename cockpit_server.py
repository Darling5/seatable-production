#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""cockpit_server.py — 驾驶舱本地伴生服务器（消灭「复制粘贴等重刷」割裂感）

问题：驾驶舱是纯静态快照 HTML。确认微信事件 / 补录数据都要
  「复制 → 切 WorkBuddy → 粘贴 → 等跑完 → 重新生成 HTML」三段割裂。

方案：本机起一个 localhost 服务器（默认 127.0.0.1:8790，可 --port 改），
  驾驶舱页面加载时探测 /api/ping，在线则把写操作按钮直连本机 API：
    POST /api/wx/approve   {ids:[...]}   → wechat_intake.py approve <编号>（逐条，留痕不变）
    POST /api/wx/ignore    {ids:[...]}   → wechat_intake.py ignore <编号>
    POST /api/backfill     {text:...}    → 补录文本回传（服务端存 data/补录回传.csv）
    POST /api/refresh      {}            → 重新跑 cockpit.py，返回新快照时间
    GET  /api/ping                       → 在线探测
    GET  /                                 → 直接服务最新生成的驾驶舱 HTML

安全边界（不变）：
  · 只绑 127.0.0.1，绝不上公网；
  · 写操作全部走既有 CLI 子进程（wechat_intake approve 的确认/留痕逻辑单份不复制）；
  · refresh 只重新渲染本地快照，不写云端；
  · 不带任何口令/token 到 URL 或日志。

用法：
  python cockpit_server.py            # 前台跑，Ctrl+C 停
  python cockpit_server.py --port 8801
  然后浏览器开 http://127.0.0.1:8790/
"""
import argparse
import json
import os
import re
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

_SKILL_DIR = os.path.dirname(os.path.abspath(__file__))
HTML_PATH = os.path.join(_SKILL_DIR, "项目管理驾驶舱.html")
BACKFILL_CSV = os.path.join(_SKILL_DIR, "data", "补录回传.csv")
PY = sys.executable


def _run_cli(args, timeout=120):
    """跑技能 CLI，返回 (exit, stdout+stderr)。所有写操作的唯一通道。"""
    try:
        r = subprocess.run([PY] + args, cwd=_SKILL_DIR, capture_output=True,
                           text=True, encoding="utf-8", errors="replace", timeout=timeout)
        return r.returncode, (r.stdout or "") + (r.stderr or "")
    except subprocess.TimeoutExpired:
        return 124, "[timeout] 命令超时（%ss）：%s" % (timeout, " ".join(args))
    except Exception as e:
        return 1, "[error] %s" % e


def _wx_ids(payload):
    """提取并校验事件编号列表（防注入：只允许 WXxxx-001 格式）。"""
    ids = payload.get("ids") or []
    if not isinstance(ids, list):
        return []
    return [i for i in ids if isinstance(i, str) and re.fullmatch(r"[A-Za-z0-9\-]{3,40}", i)]


class Handler(BaseHTTPRequestHandler):
    server_version = "CockpitLocal/1.0"

    # ---------- 基础 ----------
    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        if n <= 0 or n > 2 * 1024 * 1024:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:
            return {}

    def log_message(self, fmt, *args):
        sys.stderr.write("[cockpit-server] %s %s\n" % (self.address_string(), fmt % args))

    # ---------- GET ----------
    def do_GET(self):
        if self.path == "/api/ping":
            self._json({"ok": True, "server": "cockpit-local", "time": time.strftime("%Y-%m-%d %H:%M:%S")})
            return
        if self.path == "/" or self.path.startswith("/index"):
            if os.path.exists(HTML_PATH):
                self._serve_html()
            else:
                self._json({"ok": False, "error": "驾驶舱 HTML 未生成，先运行 python cockpit.py"}, 404)
            return
        if self.path == "/api/status":
            st = {"html_exists": os.path.exists(HTML_PATH)}
            if st["html_exists"]:
                st["snapshot_time"] = time.strftime("%Y-%m-%d %H:%M:%S",
                                                    time.localtime(os.path.getmtime(HTML_PATH)))
            self._json({"ok": True, **st})
            return
        self._json({"ok": False, "error": "not found"}, 404)

    def _serve_html(self):
        try:
            body = open(HTML_PATH, "rb").read()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        except Exception as e:
            self._json({"ok": False, "error": str(e)}, 500)

    # ---------- POST ----------
    def do_POST(self):
        payload = self._read_body()
        if self.path == "/api/wx/approve":
            ids = _wx_ids(payload)
            if not ids:
                self._json({"ok": False, "error": "没有合法的事件编号"}, 400)
                return
            out, code = [], 0
            for no in ids:
                c, t = _run_cli(["wechat_intake.py", "approve", no])
                out.append({"id": no, "exit": c, "log": t[-1500:]})
                code = max(code, c) if c != 0 else code
            self._json({"ok": code == 0, "results": out})
            return
        if self.path == "/api/wx/ignore":
            ids = _wx_ids(payload)
            if not ids:
                self._json({"ok": False, "error": "没有合法的事件编号"}, 400)
                return
            out = []
            for no in ids:
                c, t = _run_cli(["wechat_intake.py", "ignore", no])
                out.append({"id": no, "exit": c, "log": t[-1500:]})
            self._json({"ok": all(x["exit"] == 0 for x in out), "results": out})
            return
        if self.path == "/api/backfill":
            text = str(payload.get("text") or "").strip()
            if not text:
                self._json({"ok": False, "error": "内容为空"}, 400)
                return
            os.makedirs(os.path.dirname(BACKFILL_CSV), exist_ok=True)
            new_file = not os.path.exists(BACKFILL_CSV)
            import csv
            with open(BACKFILL_CSV, "a", newline="", encoding="utf-8-sig") as f:
                w = csv.writer(f)
                if new_file:
                    w.writerow(["回传时间", "内容"])
                w.writerow([time.strftime("%Y-%m-%d %H:%M:%S"), text])
            self._json({"ok": True, "saved": len(text), "path": BACKFILL_CSV})
            return
        if self.path == "/api/refresh":
            c, t = _run_cli(["cockpit.py"], timeout=300)
            self._json({"ok": c == 0, "exit": c, "log": t[-2000:],
                        "snapshot_time": time.strftime("%Y-%m-%d %H:%M:%S",
                                                       time.localtime(os.path.getmtime(HTML_PATH)))
                        if os.path.exists(HTML_PATH) else None})
            return
        self._json({"ok": False, "error": "not found"}, 404)


def main():
    ap = argparse.ArgumentParser(description="驾驶舱本地伴生服务器")
    ap.add_argument("--port", type=int, default=8801)
    ap.add_argument("--no-open", action="store_true", help="不自动开浏览器")
    args = ap.parse_args()

    # 端口占用自动顺延（默认 8801，占用则试 8802/8803…最多 +9）
    import socket
    port = args.port
    for _ in range(10):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex(("127.0.0.1", port)) != 0:
                break  # 端口空闲
        port += 1
    else:
        print("[error] 8801–8810 端口都被占用，请用 --port 指定其他端口")
        sys.exit(1)

    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = "http://127.0.0.1:%d/" % port
    print("驾驶舱本地服务器已启动：%s" % url)
    print("  · 在线模式下驾驶舱按钮直连本机 API（确认事件 / 忽略 / 刷新快照）")
    print("  · 只监听 127.0.0.1，不对外；Ctrl+C 停止")
    if not args.no_open:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")


if __name__ == "__main__":
    main()
