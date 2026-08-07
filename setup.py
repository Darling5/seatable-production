#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
setup.py — 生产交付协同助手 · 引导式安装向导

为什么需要它：
  技能本体刻意「零配置」（无 config.yaml 时自动用本地 CSV/Excel），
  但首次使用时应该把「要不要接 SeaTable / PartDB / MySQL」这件事显式问你一遍，
  而不是让你自己翻 config.yaml.example 改。本向导就是把"问 token"补上。

用法：
  交互：      python setup.py
  零配置：    python setup.py --local
  SeaTable：  python setup.py --seatable --token XXX --uuid YYY [--server URL] [--partdb-url U --partdb-token T]
  MySQL：     python setup.py --mysql --user U --db D [--host 127.0.0.1 --port 3306 --password P]
              （MySQL 适配器尚未实现，向导会先写好配置骨架，运行时退回 local）

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
        "partdb": {"enabled": False, "url": "", "token": ""},
    }


def _build_seatable(token, server, uuid, partdb_url="", partdb_token=""):
    return {
        "backend": "seatable",
        "local": {"data_dir": "data", "format": "csv"},
        "seatable": {"api_token": token, "server": server, "base_uuid": uuid},
        "partdb": {
            "enabled": bool(partdb_url and partdb_token),
            "url": partdb_url,
            "token": partdb_token,
        },
    }


def _build_mysql(host, port, user, password, db):
    # 适配器待实现；先写好配置骨架，后续 factory 接入即生效
    return {
        "backend": "mysql",
        "local": {"data_dir": "data", "format": "csv"},
        "mysql": {
            "host": host,
            "port": int(port),
            "user": user,
            "password": password,
            "database": db,
        },
        "partdb": {"enabled": False, "url": "", "token": ""},
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
    p.add_argument("--mysql", action="store_true", help="配置 MySQL 后端（适配器待实现）")
    p.add_argument("--token")
    p.add_argument("--uuid")
    p.add_argument("--server", default="https://cloud.seatable.cn")
    p.add_argument("--partdb-url", default="")
    p.add_argument("--partdb-token", default="")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", default=3306)
    p.add_argument("--user")
    p.add_argument("--password")
    p.add_argument("--db")
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
    elif a.mysql:
        if not (a.user and a.db):
            print("[error] --mysql 需要 --user 和 --db")
            sys.exit(1)
        cfg = _build_mysql(a.host, a.port, a.user, a.password or "", a.db)
        print("[warn] MySQL 适配器尚未实现，已写好配置骨架；当前运行时会退回 local。需要我实现适配器可告知。")
    else:
        # ── 交互模式 ──
        print("=== 生产交付协同助手 · 安装向导 ===")
        print("请选择数据后端：")
        print("  1) 本地 CSV/Excel（零配置，推荐，不需要任何账号）")
        print("  2) SeaTable 云（需要 Base 的 API Token 和 dtable_uuid）")
        print("  3) MySQL（需先有一台运行中的 MySQL 服务）")
        print("  0) 退出，我自己手动改 config.yaml")
        ch = _ask("请输入 [1/2/3/0，默认 1]: ", "1")
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
        elif ch == "3":
            host = _ask("MySQL host [默认 127.0.0.1]: ", "127.0.0.1")
            port = _ask("MySQL port [默认 3306]: ", "3306")
            user = _ask("MySQL user: ")
            password = _ask("MySQL password: ")
            db = _ask("MySQL database: ")
            cfg = _build_mysql(host, int(port), user, password, db)
            print("[warn] MySQL 适配器尚未实现，已写好配置骨架；当前运行时会退回 local。需要我实现适配器可告知。")
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
