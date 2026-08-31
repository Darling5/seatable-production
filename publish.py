#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
publish.py — 把本地驾驶舱 HTML 发布/覆盖更新到 WorkBuddy 团队资料库。

为什么要它：
  驾驶舱此前是「本地生成 → 手动发文件」，老板/采购看不到、口令校验可绕过。
  发布到团队空间后：链接固定（收藏一次永远最新）、权限走服务端协作者角色、
  每日更新自动留版本历史。

用法：
  python publish.py                    # 发布/覆盖更新到 config.yaml publish 段的节点
  python publish.py --html 路径.html    # 指定其他 HTML（默认 项目管理驾驶舱.html）
  python publish.py --setup            # 首次发布：在指定空间创建节点并回写 node_id
  python publish.py --status           # 只看当前发布配置，不上传

鉴权：WorkBuddy 客户端模式下由 AI（自动化任务）先取 open platform token，
通过环境变量 WB_TOKEN 传入；或用 --token-stdin。没有 token 时提示需要
在 WorkBuddy 会话中执行。

失败语义：上传失败不抛异常退出码 2，提示本地 HTML 仍可用作兜底；每日
自动化不因发布失败而中断其它步骤。
"""
import argparse
import json
import os
import subprocess
import sys

SKILL_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(SKILL_DIR, "config.yaml")

# 资料库 skill 固定安装位置（WorkBuddy 内置插件）
LIB_CANDIDATES = [
    os.path.join(os.path.expanduser("~"), ".workbuddy", "plugins", "cache",
                 "workbuddy-builtin", "skill-library"),
]

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def _load_publish_cfg():
    """从 config.yaml 读 publish 段（极简解析，无需 PyYAML）。"""
    if not os.path.exists(CONFIG_PATH):
        return {}
    cfg = {}
    in_pub = False
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        for line in f:
            raw = line.rstrip("\n")
            if not raw.strip() or raw.strip().startswith("#"):
                continue
            if raw.startswith("publish:") and not raw.startswith(" "):
                in_pub = True
                continue
            if in_pub:
                if raw.startswith(" ") and ":" in raw:
                    k, _, v = raw.strip().partition(":")
                    cfg[k.strip()] = v.strip().strip('"').strip("'")
                elif not raw.startswith(" "):
                    break
    return cfg


def _save_publish_cfg(space_id, node_id, html_name):
    """把 publish 段写回 config.yaml（已有段则原位更新键值，无则追加）。"""
    lines = []
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            lines = f.read().splitlines()
    new_section = [
        "",
        "# ── 驾驶舱发布（WorkBuddy 团队资料库）──",
        "publish:",
        "  space_id: %s" % space_id,
        "  node_id: %s" % node_id,
        '  html: "%s"' % html_name,
        "  url: https://www.workbuddy.cn/space/d/%s" % node_id,
    ]
    # 原位更新：找到 publish: 段则替换其中三个键，其余保留
    out, i, in_pub, replaced = [], 0, False, False
    while i < len(lines):
        line = lines[i]
        if line.startswith("publish:") and not line.startswith(" "):
            in_pub, replaced = True, True
            out.extend(new_section)
            i += 1
            while i < len(lines) and (lines[i].startswith(" ") or not lines[i].strip()):
                i += 1
            continue
        out.append(line)
        i += 1
    if not replaced:
        out.extend(new_section)
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        f.write("\n".join(out) + "\n")


def _find_lib_dir():
    for c in LIB_CANDIDATES:
        if os.path.isdir(c):
            versions = sorted(os.listdir(c))
            for v in reversed(versions):
                d = os.path.join(c, v)
                if os.path.isfile(os.path.join(d, "space_api.py")):
                    return d
    return None


def main():
    ap = argparse.ArgumentParser(description="发布驾驶舱到 WorkBuddy 团队资料库")
    ap.add_argument("--html", default=os.path.join(SKILL_DIR, "项目管理驾驶舱.html"))
    ap.add_argument("--setup", action="store_true",
                    help="首次发布：创建节点并回写 config.yaml 的 publish 段")
    ap.add_argument("--space-id", dest="space_id", help="首次发布指定目标空间")
    ap.add_argument("--status", action="store_true", help="只看配置不上传")
    ap.add_argument("--token-stdin", dest="token_stdin", action="store_true")
    a = ap.parse_args()

    pcfg = _load_publish_cfg()
    if a.status:
        if not pcfg:
            print("[publish] config.yaml 尚无 publish 段；先运行 --setup --space-id <空间ID> 首次发布。")
            return 0
        print("[publish] 空间: %s" % pcfg.get("space_id"))
        print("[publish] 节点: %s" % pcfg.get("node_id"))
        print("[publish] 链接: %s" % pcfg.get("url"))
        print("[publish] 文件: %s" % os.path.join(SKILL_DIR, pcfg.get("html", "项目管理驾驶舱.html")))
        return 0

    html = a.html if os.path.isabs(a.html) else os.path.join(SKILL_DIR, a.html)
    if not os.path.exists(html):
        print("[error] HTML 不存在：%s（先运行 cockpit.py 生成）" % html)
        return 1

    lib = _find_lib_dir()
    if not lib:
        print("[error] 未找到 WorkBuddy 资料库 skill（skill-library）。发布步骤需要在 WorkBuddy 环境内执行。")
        return 2

    # token：--token-stdin 或 WB_TOKEN 环境变量
    token = ""
    if a.token_stdin:
        token = sys.stdin.readline().strip()
    token = token or os.environ.get("WB_TOKEN", "").strip()
    if not token:
        print("[error] 缺少 open platform token。请在 WorkBuddy 会话中执行，"
              "由 AI 调 connect_open_platform 取得后通过 --token-stdin 或 WB_TOKEN 传入。")
        return 2

    space_id = a.space_id or pcfg.get("space_id")
    node_id = pcfg.get("node_id")
    if not space_id:
        print("[error] 缺少 space_id。首次发布：--setup --space-id <空间ID>；"
              "或先在 config.yaml 配好 publish 段。")
        return 1
    if not node_id and not a.setup:
        print("[error] config.yaml 的 publish 段缺 node_id。首次发布请加 --setup。")
        return 1

    cmd = [sys.executable, os.path.join(lib, "page", "import_html.py"), html,
           "--space-id", space_id, "--token-stdin"]
    if node_id:
        cmd += ["--node-block-id", node_id]
    try:
        r = subprocess.run(cmd, input=token + "\n", capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=300)
    except Exception as e:
        print("[error] 上传异常：%s。本地 HTML 仍可用作兜底。" % str(e)[:200])
        return 2
    out = (r.stdout or "") + (r.stderr or "")
    m = None
    for line in out.splitlines():
        if line.startswith("KS_IMPORT_OK"):
            m = line[len("KS_IMPORT_OK"):].strip()
            break
    if m:
        try:
            data = json.loads(m)
        except Exception:
            data = {}
        new_node = data.get("node_block_id") or ""
        print("[ok] 已发布: %s" % data.get("url"))
        if new_node and new_node != node_id:
            _save_publish_cfg(space_id, new_node,
                              os.path.relpath(html, SKILL_DIR).replace("\\", "/"))
            print("[ok] node_id 已回写 config.yaml publish 段")
        return 0
    print("[error] 上传失败：%s。本地 HTML 仍可用作兜底。" % out.strip()[-300:])
    return 2


if __name__ == "__main__":
    sys.exit(main())
