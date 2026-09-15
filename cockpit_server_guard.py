#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""cockpit_server_guard.py — 伴生服务器守门脚本（被自启/启动器以 pythonw 静默运行）

职责：
  1. 端口 8801-8810 已有 cockpit-local → 直接退出（防重复）
  2. 没有则拉起 cockpit_server.py 并持续守护：进程意外退出则自动重启（最多连续 5 次）
  3. 日志追加到 data/cockpit_server.log（滚动到 200KB 截断）
"""
import json
import os
import subprocess
import sys
import time
import urllib.request

SKILL_DIR = os.path.dirname(os.path.abspath(__file__))
SERVER = os.path.join(SKILL_DIR, "cockpit_server.py")
LOG = os.path.join(SKILL_DIR, "data", "cockpit_server.log")
MAX_RESTART = 5
RESTART_COOLDOWN = 30  # 秒


def log(msg):
    try:
        os.makedirs(os.path.dirname(LOG), exist_ok=True)
        # 滚动截断
        if os.path.exists(LOG) and os.path.getsize(LOG) > 200 * 1024:
            with open(LOG, "r", encoding="utf-8", errors="replace") as f:
                tail = f.read()[-50 * 1024:]
            with open(LOG, "w", encoding="utf-8") as f:
                f.write("…(滚动截断)…\n" + tail)
        with open(LOG, "a", encoding="utf-8") as f:
            f.write("[%s] %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg))
    except Exception:
        pass


def probe(ports=range(8801, 8811)):
    for port in ports:
        try:
            with urllib.request.urlopen("http://127.0.0.1:%d/api/ping" % port, timeout=0.6) as r:
                j = json.loads(r.read().decode("utf-8"))
                if j.get("server") == "cockpit-local":
                    return port
        except Exception:
            continue
    return None


def main():
    port = probe()
    if port:
        log("guard: 已有实例（端口 %d），退出" % port)
        return

    py = os.path.join(os.path.dirname(sys.executable), "python.exe")
    if not os.path.exists(py):
        py = sys.executable  # pythonw 场景下同目录应有 python.exe；否则兜底

    log("guard: 启动伴生服务器 %s" % SERVER)
    restarts = 0
    while True:
        p = subprocess.Popen([py, SERVER, "--no-open"],
                             cwd=SKILL_DIR,
                             stdout=open(LOG, "a", encoding="utf-8", errors="replace"),
                             stderr=subprocess.STDOUT,
                             creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0)
        code = p.wait()
        # 正常退出码（用户主动 taskkill 是 1/137 等）也记录，但只有非 0 才计数重启
        log("guard: 服务器退出 code=%s" % code)
        if code == 0:
            return
        restarts += 1
        if restarts > MAX_RESTART:
            log("guard: 连续重启超过 %d 次，放弃（查日志找原因）" % MAX_RESTART)
            return
        log("guard: %ds 后第 %d 次重启 …" % (RESTART_COOLDOWN, restarts))
        time.sleep(RESTART_COOLDOWN)


if __name__ == "__main__":
    main()
