"""
生产驾驶舱自动化 · 通用桥接核心
================================
把 cockpit.py / seatable_sync.py / partdb_sync.py 这三个 wrapper 放到你的
WorkBuddy「生产交付」项目目录里，它们会自动调用真实的 seatable-production
技能目录中的对应脚本。

真实技能目录解析顺序（先到先得，且**必须含 SKILL.md** 才作数）：
  1. 环境变量 SEATABLE_PRODUCTION_DIR（最优先，跨机器通用）
  2. ~/.workbuddy/skills/seatable-production*（WorkBuddy 默认技能位置）
  3. 相对本文件 ../seatable-production*

关于第 2 条的写法：技能目录名可能带版本号（seatable-production-1.8.0），
且升级时**旧的无版本目录可能被留成空壳**。所以这里用 glob 通配而不是写死
目录名——「seatable-production-1.8.0」在排序上大于「seatable-production」，
反向排序即版本化目录优先；再用「内含 SKILL.md」把空壳目录挡掉。
这样技能从 1.8.0 升到 1.9.0（或换回无版本名）时，本文件都不用改。

设计目的：让自动化能挂在「生产交付」项目分组下运行，而真实脚本/数据
仍留在 seatable-production 技能目录，升级技能不会被改写到项目里。
"""
import glob
import os
import subprocess
import sys

# wrapper 名 → 脚本在技能目录内的子目录（2026-09-26 仓库按域归组后的位置）
SUBDIR_BY_WRAPPER = {
    "cockpit.py": "cockpit",
    "seatable_sync.py": "sync",
    "partdb_sync.py": "sync",
}


def _skill_dir_candidates():
    """按优先级返回候选技能目录（未做有效性校验）。"""
    here = os.path.dirname(os.path.abspath(__file__))
    out = []
    env = os.environ.get("SEATABLE_PRODUCTION_DIR")
    if env:
        out.append(env)
    for base in (os.path.expanduser(r"~\.workbuddy\skills"),
                 os.path.join(here, "..")):
        if os.path.isdir(base):
            # 反向排序：seatable-production-1.8.0 排在 seatable-production 之前
            out.extend(sorted(glob.glob(os.path.join(base, "seatable-production*")),
                              reverse=True))
    return out


def resolve_skill_dir():
    """定位真实技能目录；返回 None 表示找不到。"""
    cands = _skill_dir_candidates()
    # 首选：目录内有 SKILL.md（真技能目录）
    for c in cands:
        if c and os.path.isfile(os.path.join(c, "SKILL.md")):
            return os.path.abspath(c)
    # 兜底：只要是存在的目录（兼容没有 SKILL.md 的老布局）
    for c in cands:
        if c and os.path.isdir(c):
            return os.path.abspath(c)
    return None


def run_target():
    skill_dir = resolve_skill_dir()
    if not skill_dir:
        sys.stderr.write(
            "找不到 seatable-production 技能目录（已查找：环境变量 "
            "SEATABLE_PRODUCTION_DIR、~/.workbuddy/skills/seatable-production*、"
            "../seatable-production*）；\n"
            "请设置环境变量 SEATABLE_PRODUCTION_DIR 指向该目录后重试。\n"
        )
        sys.exit(2)

    # 按「wrapper 同名脚本」查映射表定位新位置；映射表里没有的兜底查技能目录根。
    name = os.path.basename(sys.argv[0])
    targets = [
        os.path.join(skill_dir, SUBDIR_BY_WRAPPER.get(name, ""), name),
        os.path.join(skill_dir, name),  # 兼容整理前的老布局
    ]
    target = next((p for p in targets if os.path.isfile(p)), None)
    if not target:
        sys.stderr.write(f"目标脚本不存在: {targets[0]}\n")
        sys.exit(2)

    sys.exit(subprocess.call([sys.executable, target], cwd=skill_dir))


if __name__ == "__main__":
    run_target()
