#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""驾驶舱口令查询（仅限本地/私聊手动使用）。

v2.0 架构原则：口令**永不**进入每日自动播报。
自动化提示词不再包含「读 config.yaml 附口令清单」的步骤；
用户需要口令时，手动运行本命令（对话中当面查看）。

用法：
  python passwords.py show           # 显示当前生效口令（admin + 各角色）
  python passwords.py check          # 只检查口令是否齐全，不显示明文
  python passwords.py rotate         # 轮换全部口令并写回 config.yaml

说明：
  - 口令存于 config.yaml 的 cockpit 段（已 gitignore，不进版本库）；
  - rotate 后需要重跑 python cockpit.py，重生成 HTML 才会生效；
  - 显示结果请勿复制到群聊/邮件。
"""
import argparse
import os
import random
import string
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from adapters.factory import load_config, _SKILL_DIR  # noqa: E402

ROLES = ("boss", "warehouse", "purchase", "production", "sales")
ROLE_CN = {"boss": "老板", "warehouse": "仓库", "purchase": "采购",
           "production": "生产经理", "sales": "销售"}


def _current(cfg=None):
    """返回 (admin, {role: pw})，不生成、不写回。"""
    cfg = cfg or load_config() or {}
    cp = cfg.get("cockpit") or {}
    admin = (cp.get("admin_password") or "").strip()
    roles = {r: (cp.get("role_passwords") or {}).get(r, "") or "" for r in ROLES}
    return admin, roles


def _gen():
    alphabet = string.ascii_letters + string.digits
    return "".join(random.SystemRandom().choice(alphabet) for _ in range(10))


def cmd_show():
    admin, roles = _current()
    if not admin:
        print("config.yaml 尚未配置 cockpit.admin_password。"
              "运行 python cockpit.py 会自动生成一套，或用本脚本 rotate。")
        sys.exit(1)
    print("== 驾驶舱访问口令（取自 config.yaml，重生成驾驶舱后生效）==")
    print("  管理员(admin)：%s   可看全部角色、可切换" % admin)
    for r in ROLES:
        pw = roles.get(r) or "(未设置)"
        print("  %s(%s)：%s" % (ROLE_CN.get(r, r), r, pw))
    print()
    print("⚠️ 仅本地/私聊查看���勿推送到群聊、勿写进任何自动播报。")


def cmd_check():
    admin, roles = _current()
    missing = []
    if not admin:
        missing.append("admin_password")
    missing += ["role_passwords.%s" % r for r in ROLES if not roles.get(r)]
    if missing:
        print("口令不完整，缺：%s" % ", ".join(missing))
        print("补救：python passwords.py rotate（或运行 python cockpit.py 自动补齐）")
        sys.exit(2)
    print("口令齐全：admin + %d 个角色。明文用 passwords.py show 查看。" % len(ROLES))


def cmd_rotate():
    admin, roles = _current()
    new_admin = _gen()
    new_roles = {r: _gen() for r in ROLES}
    cfg_path = os.path.join(_SKILL_DIR, "config.yaml")
    block = ["", "# 驾驶舱访问口令（%s 轮换）" % _today(),
             "cockpit:", '  admin_password: "%s"' % new_admin, "  role_passwords:"]
    block += ['    %s: "%s"' % (r, new_roles[r]) for r in ROLES]
    text = ""
    if os.path.exists(cfg_path):
        with open(cfg_path, "r", encoding="utf-8") as f:
            text = f.read()
    if "cockpit:" in text:
        # 已有段落：只替换口令行，不动其他内容
        import re
        text = re.sub(r'cockpit:\s*\n\s*admin_password:\s*"[^"]*"',
                      'cockpit:\n  admin_password: "%s"' % new_admin, text)
        for r in ROLES:
            text = re.sub(r'(\n\s*%s:\s*)"[^"]*"' % r,
                          r'\g<1>"%s"' % new_roles[r], text)
        with open(cfg_path, "w", encoding="utf-8") as f:
            f.write(text)
    else:
        with open(cfg_path, "a" if text else "w", encoding="utf-8") as f:
            if not text:
                f.write("backend: local\n")
            f.write("\n".join(block) + "\n")
    print("已轮换并写回 config.yaml：")
    print("  管理员 %s" % new_admin)
    for r in ROLES:
        print("  %s %s" % (r, new_roles[r]))
    print()
    print("下一步：python cockpit.py 重生成驾驶舱 HTML，新口令才生效。")


def _today():
    import datetime
    return datetime.date.today().isoformat()


def main():
    p = argparse.ArgumentParser(description="驾驶舱口令手动查询/轮换（不进自动播报）")
    sub = p.add_subparsers(dest="cmd")
    sub.add_parser("show", help="显示当前口令（仅本地/私聊）")
    sub.add_parser("check", help="检查口令完整性（不显示明文）")
    sub.add_parser("rotate", help="轮换全部口令并写回 config.yaml")
    args = p.parse_args()
    if args.cmd == "show":
        cmd_show()
    elif args.cmd == "check":
        cmd_check()
    elif args.cmd == "rotate":
        cmd_rotate()
    else:
        p.print_help()


if __name__ == "__main__":
    main()
