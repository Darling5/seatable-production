"""
生产驾驶舱自动化 · 通用桥接核心
================================
把 seatable_sync.py / partdb_sync.py / cockpit.py 这三个 wrapper 放到你的
WorkBuddy「生产交付」项目目录里，它们会自动调用真实的 seatable-production
技能目录中的同名脚本。

真实技能目录解析顺序：
  1. 环境变量 SEATABLE_PRODUCTION_DIR（最优先，跨机器通用）
  2. ~/.workbuddy/skills/seatable-production（WorkBuddy 默认技能位置）
  3. 相对本文件 ../seatable-production

设计目的：让自动化能挂在「生产交付」项目分组下运行，而真实脚本/数据
仍留在 seatable-production 技能目录，升级技能不会被改写到项目里。
"""
import os
import sys
import subprocess


def run_target():
    here = os.path.dirname(os.path.abspath(__file__))
    env = os.environ.get("SEATABLE_PRODUCTION_DIR")
    candidates = [
        env,
        os.path.expanduser(r"~\.workbuddy\skills\seatable-production"),
        os.path.join(here, "..", "seatable-production"),
    ]
    skill_dir = None
    for c in candidates:
        if c and os.path.isdir(c):
            skill_dir = os.path.abspath(c)
            break

    if not skill_dir:
        sys.stderr.write(
            "找不到 seatable-production 技能目录；\n"
            "请设置环境变量 SEATABLE_PRODUCTION_DIR 指向该目录后重试。\n"
        )
        sys.exit(2)

    target = os.path.join(skill_dir, os.path.basename(sys.argv[0]))
    if not os.path.isfile(target):
        sys.stderr.write(f"目标脚本不存在: {target}\n")
        sys.exit(2)

    sys.exit(subprocess.call([sys.executable, target], cwd=skill_dir))


if __name__ == "__main__":
    run_target()
