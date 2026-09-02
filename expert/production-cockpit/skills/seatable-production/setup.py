#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
setup.py — 生产交付协同助手 · 引导式安装向导

为什么需要它：
  技能本体刻意「零配置」（无 config.yaml 时自动用本地 CSV/Excel），
  但首次使用时应该把「要不要接 SeaTable / PartDB」这件事显式问你一遍，
  而不是让你自己翻 config.yaml.example 改。本向导就是把"问 token"补上。

用法：
  交互：      python setup.py
  零配置：    python setup.py --local
  SeaTable：  python setup.py --seatable --token XXX --uuid YYY [--server URL]
  库存：      首次运行 python pipeline/run.py init 后，在 config.yaml 选择 PartDB 或 Excel/CSV

写出的 config.yaml 含凭证，按 .gitignore 排除，不会提交到公开仓库。
"""
import os
import sys
import argparse

SKILL_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG = os.path.join(SKILL_DIR, "config.yaml")


def _ask(prompt, default=""):
    try:
        val = input(prompt).strip()
    except EOFError:
        val = ""
    return val or default


def _build_local():
    return {
        "backend": "local",
        "local": {"data_dir": "data", "format": "csv"},
        "inventory": {"source": "file", "file": {
            "path": "pipeline/customer/inventory/库存导出.xlsx",
            "stock_is_confirmed": False,
        }},
        "partdb": {"enabled": False, "url": "", "token": ""},
    }


def _build_seatable(token, server, uuid, partdb_url="", partdb_token=""):
    inventory = {"source": "file", "file": {
        "path": "pipeline/customer/inventory/库存导出.xlsx",
        "stock_is_confirmed": False,
    }}
    if partdb_url and partdb_token:
        inventory = {"source": "partdb"}
    return {
        "backend": "seatable",
        "local": {"data_dir": "data", "format": "csv"},
        "seatable": {"api_token": token, "server": server, "base_uuid": uuid},
        "inventory": inventory,
        "partdb": {
            "enabled": bool(partdb_url and partdb_token),
            "url": partdb_url,
            "token": partdb_token,
        },
    }


def _dump(cfg):
    lines = ["# 生产交付协同助手 配置文件（由 setup.py 生成）", ""]

    def rec(obj, indent=0):
        pad = "  " * indent
        if isinstance(obj, dict):
            for k, v in obj.items():
                if isinstance(v, dict):
                    lines.append(f"{pad}{k}:")
                    rec(v, indent + 1)
                else:
                    if isinstance(v, str) and v == "":
                        lines.append(f'{pad}{k}: ""')
                    else:
                        lines.append(f"{pad}{k}: {v}")
        else:
            lines.append(f"{pad}{obj}")

    rec(cfg)
    return "\n".join(lines) + "\n"


def main():
    p = argparse.ArgumentParser(description="生产交付协同助手 安装向导")
    p.add_argument("--local", action="store_true", help="直接写本地零配置")
    p.add_argument("--seatable", action="store_true", help="配置 SeaTable 云后端")
    p.add_argument("--token")
    p.add_argument("--uuid")
    p.add_argument("--server", default="https://cloud.seatable.cn")
    p.add_argument("--partdb-url", default="")
    p.add_argument("--partdb-token", default="")
    p.add_argument("--force", action="store_true", help="覆盖已存在的 config.yaml")
    a = p.parse_args()

    if os.path.exists(CONFIG) and not a.force:
        print(f"[提示] 已存在 config.yaml（{CONFIG}），未修改。用 --force 覆盖。")
        return

    cfg = None

    if a.local:
        cfg = _build_local()
        print("[ok] 使用本地零配置（CSV/Excel）。")
    elif a.seatable:
        if not (a.token and a.uuid):
            print("[error] --seatable 需要同时提供 --token 和 --uuid")
            sys.exit(1)
        cfg = _build_seatable(a.token, a.server, a.uuid, a.partdb_url, a.partdb_token)
        print("[ok] 已配置 SeaTable 云后端。")
    else:
        # ── 交互模式 ──
        print("=== 生产交付协同助手 · 安装向导 ===")
        print("请选择数据后端：")
        print("  1) 本地 CSV/Excel（零配置，推荐，不需要任何账号）")
        print("  2) SeaTable 云（需要 Base 的 API Token 和 dtable_uuid）")
        print("  0) 退出，我自己手动改 config.yaml")
        ch = _ask("请输入 [1/2/0，默认 1]: ", "1")
        if ch == "0":
            print("已退出，未修改任何配置。")
            return
        if ch == "1":
            cfg = _build_local()
            print("[ok] 本地零配置。")
        elif ch == "2":
            token = _ask("SeaTable API Token: ")
            uuid = _ask("Base dtable_uuid: ")
            server = _ask("Server [默认 https://cloud.seatable.cn]: ", "https://cloud.seatable.cn")
            pu = _ask("PartDB URL（没有可留空）: ")
            pt = _ask("PartDB Token（没有可留空）: ")
            cfg = _build_seatable(token, server, uuid, pu, pt)
            print("[ok] 已配置 SeaTable" + (" + PartDB" if pu and pt else "") + "。")
        else:
            print("无效选择，退出。")
            return

    with open(CONFIG, "w", encoding="utf-8") as f:
        f.write(_dump(cfg))
    print(f"[ok] 已写入配置：{CONFIG}")
    print("下一步：")
    print("  python seed_demo.py   # 写入演示数据（可选，真实数据请走 seatable_sync.py）")
    print("  python cockpit.py      # 生成 项目管理驾驶舱.html")


if __name__ == "__main__":
    main()
