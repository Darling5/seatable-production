#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""cockpit_autostart.py — 驾驶舱伴生服务器的开机自启/静默启动器

用法：
  python cockpit_autostart.py install    # 注册开机自启（计划任务，当前用户登录触发）
  python cockpit_autostart.py remove     # 取消开机自启
  python cockpit_autostart.py status     # 查看自启状态 + 服务器是否在跑
  python cockpit_autostart.py start      # 立即启动（等效开机自启效果，经计划任务拉起）
  python cockpit_autostart.py stop       # 停掉正在跑的伴生服务器

原理：
  · Windows 计划任务（schtasks /SC ONLOGON，当前用户，无需管理员），
    登录时以 pythonw.exe 静默运行 cockpit_server_guard.py
  · guard 先探测 8801-8810 是否已有 cockpit-local 在跑（防重复），没有才拉起
  · 挂了自动重启（最多连续 5 次，间隔 30s）；日志在 data/cockpit_server.log
  · 计划任务由系统拉起，进程独立于任何终端/会话，不随会话退出被杀
"""
import json
import os
import subprocess
import sys
import time
import urllib.request

SKILL_DIR = os.path.dirname(os.path.abspath(__file__))
GUARD = os.path.join(SKILL_DIR, "cockpit_server_guard.py")
SERVER = os.path.join(SKILL_DIR, "cockpit_server.py")
TASK_NAME = "CockpitServer"


def _schtasks(*args):
    r = subprocess.run(["schtasks"] + list(args), capture_output=True)
    out = (r.stdout or r.stderr).decode("gbk", errors="replace")
    return r.returncode, out.strip()


def find_pythonw():
    """找一个 pythonw.exe（无窗口）。优先 WorkBuddy 自带（依赖最全），版本号自动扫最新。"""
    wb_versions = os.path.expandvars(r"%USERPROFILE%\.workbuddy\binaries\python\versions")
    if os.path.isdir(wb_versions):
        for name in sorted(os.listdir(wb_versions), reverse=True):
            p = os.path.join(wb_versions, name, "pythonw.exe")
            if os.path.exists(p):
                return p
    p = r"C:\Python311\pythonw.exe"
    if os.path.exists(p):
        return p
    import shutil
    return shutil.which("pythonw")


def is_running(ports=range(8801, 8811)):
    """探测伴生服务器是否已在跑（认 /api/ping 的 server=cockpit-local）。"""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # 绕过系统代理
    for port in ports:
        try:
            with opener.open("http://127.0.0.1:%d/api/ping" % port, timeout=0.8) as r:
                j = json.loads(r.read().decode("utf-8"))
                if j.get("server") == "cockpit-local":
                    return port
        except Exception:
            continue
    return None


def cmd_install():
    pw = find_pythonw()
    if not pw:
        print("[error] 找不到 pythonw.exe，无法注册自启")
        sys.exit(1)
    tr = '"%s" "%s"' % (pw, GUARD)
    code, out = _schtasks("/Create", "/TN", TASK_NAME, "/SC", "ONLOGON", "/TR", tr, "/F")
    if code != 0:
        print("[error] 计划任务创建失败：%s" % out)
        sys.exit(1)
    print("[ok] 已注册开机自启（计划任务 %s，当前用户登录触发，无需管理员）" % TASK_NAME)
    print("     解释器：%s" % pw)
    print("     守门脚本：%s" % GUARD)
    port = is_running()
    if port:
        print("[i] 服务器已在跑（端口 %d），不重复启动" % port)
    else:
        print("[i] 现在就启动一次 …")
        _schtasks("/Run", "/TN", TASK_NAME)
        for _ in range(15):
            time.sleep(1)
            port = is_running()
            if port:
                break
        port = is_running()
        print(("[ok] 已启动：http://127.0.0.1:%d/" % port) if port
              else "[warn] 未探测到服务，重启电脑后生效；或手动 start")
    print("\n效果：每次登录自动后台运行，无窗口；驾驶舱打开即「⚡ 在线直连」。")
    print("取消：python cockpit_autostart.py remove")


def cmd_remove():
    code, out = _schtasks("/Delete", "/TN", TASK_NAME, "/F")
    print("[ok] 已取消开机自启" if code == 0 else "[warn] 删除计划任务失败：%s" % out)
    port = is_running()
    if port:
        print("[i] 服务器还在跑（端口 %d），运行 stop 停止" % port)


def cmd_status():
    code, _ = _schtasks("/Query", "/TN", TASK_NAME)
    print("开机自启：", "已注册（计划任务 %s）" % TASK_NAME if code == 0 else "未注册")
    port = is_running()
    print("伴生服务器：", ("运行中 · http://127.0.0.1:%d/" % port) if port else "未运行")


def cmd_start():
    port = is_running()
    if port:
        print("[skip] 服务器已在跑（端口 %d）" % port)
        return
    code, out = _schtasks("/Run", "/TN", TASK_NAME)
    if code != 0:
        print("[error] 计划任务未注册或运行失败：%s\n先运行 install" % out)
        sys.exit(1)
    for _ in range(15):
        time.sleep(1)
        port = is_running()
        if port:
            print("[ok] 伴生服务器已启动：http://127.0.0.1:%d/" % port)
            return
    print("[warn] 15 秒内未探测到服务，稍后再 status 查看")


def cmd_stop():
    port = is_running()
    if not port:
        print("[skip] 服务器没在跑")
        return
    r = subprocess.run(["netstat", "-ano"], capture_output=True, text=True)
    pids = set()
    for line in (r.stdout or "").splitlines():
        if ":%d" % port in line and "LISTENING" in line:
            parts = line.split()
            if parts:
                pids.add(parts[-1])
    if not pids:
        print("[warn] 没找到监听进程，可能已停")
        return
    for pid in pids:
        subprocess.run(["taskkill", "/PID", pid, "/F"], capture_output=True)
        print("[ok] 已结束进程 %s" % pid)


def main():
    if len(sys.argv) < 2 or sys.argv[1] not in ("install", "remove", "status", "start", "stop"):
        print(__doc__)
        sys.exit(0 if len(sys.argv) < 2 else 1)
    {"install": cmd_install, "remove": cmd_remove, "status": cmd_status,
     "start": cmd_start, "stop": cmd_stop}[sys.argv[1]]()


if __name__ == "__main__":
    main()
