#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
deploy.py — 生产交付协同助手 · 一键全自动部署

目标：新用户拿到仓库后，只需要「提供资料」，剩下全部自动完成。

资料从哪来（优先级从高到低，给到哪层用到哪层）：
  1. 命令行参数   python deploy.py --seatable-token XXX --seatable-uuid YYY ...
  2. 环境变量     SEATABLE_TOKEN / SEATABLE_UUID / SEATABLE_SERVER / PARTDB_URL / PARTDB_TOKEN / WECHAT_DB_PATH
  3. 资料文件     python deploy.py --profile deploy.yaml   （复制 deploy.yaml.example 填好即可）
  4. 交互问答     什么都不给且终端可交互 → 逐项问（直接回车跳过可选项）
  5. --demo       什么都不给 → 本地零配置演示模式，全自动跑通全链路

自动完成（幂等，可重复执行）：
  S1 环境检测     Python 版本 / pip
  S2 依赖自装     requests（缺了自动 pip install；--venv 建独立虚拟环境）
  S3 生成配置     写 config.yaml；已有配置做「外科手术式」更新，口令/微信/行情段原样保留
  S4 连通验证     SeaTable（dry-run）/ PartDB（HTTP 探活）/ 微信数据库路径
  S5 数据初始化   云端业务表同步到本地 + 物料监控清单生成
  S6 驾驶舱       生成 项目管理驾驶舱.html
  S7 开局体检     doctor.py，把「还缺什么」讲清楚
  S8 部署报告     控制台表格 + data/deploy-report.md + 下一步指引

常用姿势：
  python deploy.py --demo                              # 零资料演示，60 秒看全貌
  python deploy.py --profile deploy.yaml               # 填好资料文件，一键投产
  python deploy.py --seatable-token T --seatable-uuid U --partdb-url http://... --partdb-token K
  python deploy.py --skip-deps --skip-sync             # 只重建配置+驾驶舱
  python deploy.py --dry-run                           # 只看将做什么，不落盘
"""
import argparse
import datetime
import json
import os
import re
import shutil
import subprocess
import sys

SKILL_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(SKILL_DIR, "config.yaml")
PROFILE_EXAMPLE = os.path.join(SKILL_DIR, "deploy.yaml.example")
REPORT_PATH = os.path.join(SKILL_DIR, "data", "deploy-report.md")

# ── 控制台输出（Windows GBK 终端兜底）─────────────────────────
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def _ok(msg):
    print("  [ok] " + msg)


def _fail(msg):
    print("  [FAIL] " + msg)


def _info(msg):
    print("  [i] " + msg)


# ── 步骤记录 ─────────────────────────────────────────────────
class Step:
    def __init__(self, name):
        self.name = name
        self.status = "…"   # ok / FAIL / skip
        self.detail = ""

    def done(self, detail=""):
        self.status, self.detail = "ok", detail
        return self

    def fail(self, detail=""):
        self.status, self.detail = "FAIL", detail
        return self

    def skip(self, detail=""):
        self.status, self.detail = "skip", detail
        return self


STEPS = []


def report_table():
    lines = ["步骤".ljust(6) + "名称".ljust(16) + "结果", "-" * 46]
    mark = {"ok": "[OK]  完成", "FAIL": "[FAIL]失败", "skip": "[--]  跳过", "…": "[..]  未执行"}
    for i, s in enumerate(STEPS, 1):
        lines.append(("S%d" % i).ljust(6) + s.name.ljust(16) + mark.get(s.status, s.status))
    det = [s for s in STEPS if s.detail]
    if det:
        lines.append("-" * 46)
        lines.append("明细：")
        for s in det:
            lines.append("  · %s：%s" % (s.name, s.detail))
    return "\n".join(lines)


# ── 资料收集 ─────────────────────────────────────────────────
PROFILE_KEYS = [
    # (键名, 环境变量, CLI 参数, 是否敏感, 说明)
    ("seatable_token", "SEATABLE_TOKEN", "--seatable-token", True, "SeaTable API Token"),
    ("seatable_uuid", "SEATABLE_UUID", "--seatable-uuid", True, "SeaTable Base dtable_uuid"),
    ("seatable_server", "SEATABLE_SERVER", "--seatable-server", False, "SeaTable 服务器"),
    ("partdb_url", "PARTDB_URL", "--partdb-url", False, "PartDB API 地址"),
    ("partdb_token", "PARTDB_TOKEN", "--partdb-token", True, "PartDB API Token"),
    ("wechat_db_path", "WECHAT_DB_PATH", "--wechat-db", False, "微信 merge_all.db 路径"),
]


def _read_profile_file(path):
    """资料文件：平面 key: value 格式，兼容极简解析（无 PyYAML 也能跑）。"""
    out = {}
    with open(path, "r", encoding="utf-8") as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            key, _, val = line.partition(":")
            val = val.strip().strip('"').strip("'")
            if val:
                out[key.strip()] = val
    return out


def _ask(prompt, default=""):
    try:
        val = input(prompt).strip()
    except EOFError:
        val = ""
    return val or default


def collect_profile(args):
    """按 CLI > 环境变量 > 资料文件 > 交互 合并资料。"""
    prof = {}
    if args.profile:
        if not os.path.exists(args.profile):
            raise SystemExit("[错误] 资料文件不存在：%s" % args.profile)
        prof.update(_read_profile_file(args.profile))
        _info("已读取资料文件：%s（%d 项）" % (args.profile, len(prof)))
    env_hit = 0
    for key, env, _flag, _s, _d in PROFILE_KEYS:
        val = os.environ.get(env, "").strip()
        if val:
            prof[key] = val
            env_hit += 1
    if env_hit:
        _info("已从环境变量读取 %d 项资料" % env_hit)
    # CLI 参数覆盖
    for key, _env, flag, _s, _d in PROFILE_KEYS:
        attr = flag.lstrip("-").replace("-", "_")
        val = getattr(args, attr, None)
        if val:
            prof[key] = val
    # 交互兜底（无资料且终端可交互且非 demo）
    if not prof and not args.demo and not args.yes and sys.stdin is not None and sys.stdin.isatty():
        print("\n== 未检测到任何资料，进入问答模式（直接回车 = 跳过该项）==")
        answers = {
            "seatable_token": _ask("SeaTable API Token（跳过则用本地 CSV）: "),
            "seatable_uuid": _ask("SeaTable Base dtable_uuid: "),
            "seatable_server": _ask("SeaTable 服务器 [https://cloud.seatable.cn]: ", "https://cloud.seatable.cn"),
            "partdb_url": _ask("PartDB API 地址（可跳过）: "),
            "partdb_token": _ask("PartDB API Token（可跳过）: "),
            "wechat_db_path": _ask("微信 merge_all.db 路径（可跳过）: "),
        }
        prof.update({k: v for k, v in answers.items() if v})
        if not any(prof.values()):
            print("未提供任何资料，自动切换 --demo 本地演示模式。")
            args.demo = True
    return prof


# ── S1 环境检测 ──────────────────────────────────────────────
def step_env_check(args):
    s = Step("环境检测")
    STEPS.append(s)
    if sys.version_info < (3, 8):
        return s.fail("Python %d.%d 过旧，需要 ≥3.8" % sys.version_info[:2])
    return s.done("Python %d.%d.%d" % sys.version_info[:3])


# ── S2 依赖自装 ──────────────────────────────────────────────
def _pip_install(packages):
    cmd = [sys.executable, "-m", "pip", "install", "--quiet"] + packages
    r = subprocess.run(cmd, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    return r.returncode == 0, (r.stderr or r.stdout or "").strip()[-300:]


def step_deps(args):
    s = Step("依赖自装")
    STEPS.append(s)
    if args.skip_deps:
        return s.skip("--skip-deps")
    missing = []
    for mod in ("requests", "yaml"):
        try:
            __import__(mod)
        except ImportError:
            missing.append("pyyaml" if mod == "yaml" else mod)
    if not missing:
        return s.done("requests / pyyaml 均已就绪")
    _info("缺少依赖：%s → 自动安装" % ", ".join(missing))
    ok, err = _pip_install(missing)
    if ok:
        return s.done("已安装 " + ", ".join(missing))
    return s.fail("pip 安装失败：%s" % err)


# ── S3 生成 config.yaml ──────────────────────────────────────
_SURGICAL = [
    # (正则[按行匹配], 替换模板)  —— 只动这几行，其余（口令/微信/行情/默认值）原样保留
    # 注意：值不加引号（seatable_sync.py 的极简解析器历史上不剥引号，保持与生产 config 一致的无引号约定）
    (re.compile(r"^backend\s*:.*$"), "backend: {backend}"),
    (re.compile(r"^(\s*)api_token\s*:.*$"), "\\1api_token: {seatable_token}"),
    (re.compile(r"^(\s*)base_uuid\s*:.*$"), "\\1base_uuid: {seatable_uuid}"),
    (re.compile(r"^(\s*)server\s*:.*$"), "\\1server: {seatable_server}"),
    (re.compile(r"^(\s*)enabled\s*:.*$"), "\\1enabled: {partdb_enabled}"),
    (re.compile(r"^(\s*)url\s*:.*$"), "\\1url: {partdb_url}"),
    (re.compile(r"^(\s*)token\s*:.*$"), "\\1token: {partdb_token}"),
    (re.compile(r"^(\s*)db_path\s*:.*$"), "\\1db_path: {wechat_db_path}"),
]


def _fresh_config(prof, backend):
    """全新生成（无已有 config.yaml 时）。"""
    token = prof.get("seatable_token", "")
    uuid = prof.get("seatable_uuid", "")
    server = prof.get("seatable_server") or "https://cloud.seatable.cn"
    partdb_url = prof.get("partdb_url", "")
    partdb_token = prof.get("partdb_token", "")
    lines = [
        "# 生产交付协同助手 配置文件（由 deploy.py 全自动生成，已 gitignore 不会提交）",
        "backend: %s" % backend,
        "",
        "local:",
        "  data_dir: data",
        "  format: csv",
        "",
        "seatable:",
        "  api_token: %s" % token,
        "  server: %s" % server,
        "  base_uuid: %s" % uuid,
        "",
        "inventory:",
        "  source: %s" % ("partdb" if (partdb_url and partdb_token) else "file"),
        "  file:",
        '    path: "pipeline/customer/inventory/库存导出.xlsx"',
        "    stock_is_confirmed: false",
        "",
        "partdb:",
        "  enabled: %s" % ("true" if (partdb_url and partdb_token) else "false"),
        "  url: %s" % partdb_url,
        "  token: %s" % partdb_token,
        "",
        "# ── 微信情报反哺（win-wechat-summary 产出的 merge_all.db）──",
        "wechat:",
        "  enabled: true",
        "  db_path: %s" % prof.get("wechat_db_path", ""),
        "  watch_groups: []",
        "  max_hours: 48",
        "",
        "# ── 物料行情监控 ──",
        "market:",
        "  alert_threshold: 10",
        "  check_day: Monday",
        "  channels: [立创商城, 华秋商城, 原厂公告]",
        "",
        "# ── 驾驶舱访问口令（首次生成后建议自行修改）──",
        "cockpit:",
        '  admin_password: "%s"' % _gen_password(),
        "",
    ]
    return "\n".join(lines) + "\n"


def _gen_password(n=12):
    import secrets
    import string
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(n))


def step_config(args, prof, backend):
    s = Step("生成 config.yaml")
    STEPS.append(s)
    if args.dry_run:
        return s.skip("dry-run 只预览", )
    if os.path.exists(CONFIG_PATH):
        # 已有配置 → 外科手术式更新，保留口令/微信/行情等其余内容
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            text = f.read()
        vals = {
            "backend": backend,
            "seatable_token": prof.get("seatable_token", ""),
            "seatable_uuid": prof.get("seatable_uuid", ""),
            "seatable_server": prof.get("seatable_server") or "https://cloud.seatable.cn",
            "partdb_enabled": "true" if (prof.get("partdb_url") and prof.get("partdb_token")) else "false",
            "partdb_url": prof.get("partdb_url", ""),
            "partdb_token": prof.get("partdb_token", ""),
            "wechat_db_path": prof.get("wechat_db_path", ""),
        }
        changed = []
        for pat, tpl in _SURGICAL:
            new_text, n = pat.subn(lambda m, _t=tpl: _t.format(**vals), text, count=1)
            if n:
                changed.append(tpl.split(":")[0].strip().lstrip("\\1").replace("\\1", ""))
            text = new_text
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            f.write(text)
        return s.done("更新已有配置（保留口令/微信/行情段），backend=%s" % backend)
    # 全新生成
    text = _fresh_config(prof, backend)
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        f.write(text)
    return s.done("全新生成配置，backend=%s，驾驶舱口令已自动创建" % backend)


# ── S4 连通验证 ──────────────────────────────────────────────
def _run_py(script, extra_args=None, timeout=600):
    cmd = [sys.executable, os.path.join(SKILL_DIR, script)] + (extra_args or [])
    r = subprocess.run(cmd, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", cwd=SKILL_DIR, timeout=timeout)
    tail = ((r.stdout or "") + (r.stderr or "")).strip().splitlines()
    return r.returncode, "\n".join(tail[-5:])


def step_verify(args, prof, backend):
    s = Step("连通验证")
    STEPS.append(s)
    if args.dry_run:
        return s.skip("dry-run")
    notes = []
    # SeaTable
    if backend == "seatable":
        rc, tail = _run_py("seatable_sync.py", ["--dry-run"], timeout=120)
        if rc == 0:
            notes.append("SeaTable ✓（dry-run 通过）")
        else:
            notes.append("SeaTable ✗：%s" % tail)
            s.fail("；".join(notes))
            return s
    else:
        notes.append("SeaTable：未配置，本地 CSV 模式")
    # PartDB
    if prof.get("partdb_url") and prof.get("partdb_token"):
        try:
            import requests
            r = requests.get(prof["partdb_url"], timeout=8)
            notes.append("PartDB ✓（HTTP %d）" % r.status_code)
        except Exception as e:
            notes.append("PartDB ✗：%s" % str(e)[:120])
    else:
        notes.append("PartDB：未配置（可选）")
    # 微信 DB
    wdb = prof.get("wechat_db_path", "")
    if wdb:
        notes.append("微信DB ✓" if os.path.exists(wdb) else "微信DB ✗（路径不存在，后续 pull 会再自动找）")
    else:
        notes.append("微信DB：未配置（可选）")
    if any("✗" in n for n in notes):
        return s.fail("；".join(notes))
    return s.done("；".join(notes))


# ── S5 数据初始化 ────────────────────────────────────────────
def step_init_data(args, backend):
    s = Step("数据初始化")
    STEPS.append(s)
    if args.dry_run:
        return s.skip("dry-run")
    if args.skip_sync:
        return s.skip("--skip-sync")
    notes = []
    if backend == "seatable":
        rc, tail = _run_py("seatable_sync.py", timeout=900)
        if rc == 0:
            notes.append("云端业务表已同步到本地 data/")
        else:
            return s.fail("seatable_sync 失败：%s" % tail)
    else:
        notes.append("本地模式：使用 data/ 现有 CSV")
    rc, tail = _run_py("market.py", ["watchlist", "--refresh"], timeout=300)
    if rc == 0:
        notes.append("物料监控清单已生成")
    else:
        notes.append("物料清单跳过：%s" % tail.splitlines()[-1] if tail else "未知原因")
    return s.done("；".join(notes))


# ── S6 驾驶舱 ────────────────────────────────────────────────
def step_cockpit(args):
    s = Step("驾驶舱生成")
    STEPS.append(s)
    if args.dry_run:
        return s.skip("dry-run")
    if args.no_cockpit:
        return s.skip("--no-cockpit")
    rc, tail = _run_py("cockpit.py", timeout=300)
    if rc == 0:
        html = os.path.join(SKILL_DIR, "项目管理驾驶舱.html")
        size = "%.1f KB" % (os.path.getsize(html) / 1024) if os.path.exists(html) else "?"
        return s.done("项目管理驾驶舱.html（%s）" % size)
    return s.fail(tail)


# ── S7 开局体检 ──────────────────────────────────────────────
def step_doctor(args):
    s = Step("开局体检")
    STEPS.append(s)
    if args.dry_run or args.skip_doctor:
        return s.skip("dry-run" if args.dry_run else "--skip-doctor")
    rc, tail = _run_py("doctor.py", timeout=120)
    if rc == 0:
        return s.done("体检完成（详见上方输出；BLOCK 项才需要立即处理）")
    return s.done("体检已运行（脚本退出码 %d，输出见上）" % rc)


# ── S8 报告 ─────────────────────────────────────────────────
def step_report(args, prof, backend):
    s = Step("部署报告")
    STEPS.append(s)
    if args.dry_run:
        return s.skip("dry-run")
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    failed = [x for x in STEPS[:-1] if x.status == "FAIL"]
    status = "部署完成，有失败项需处理" if failed else "部署成功"
    body = ["# 生产交付协同助手 · 部署报告", "",
            "- 时间：%s" % now,
            "- 模式：%s" % ("本地演示（demo）" if args.demo else backend),
            "- 结果：%s" % status, "",
            "```", report_table(), "```", "",
            "## 下一步", ""]
    if backend == "seatable":
        body += ["- 每日例行：建议注册定时任务（WorkBuddy 自动化 / cron）依次执行",
                 "  `seatable_sync.py → partdb_sync.py → wechat_intake.py pull → market.py watchlist --refresh → cockpit.py`"]
    else:
        body += ["- 录入真实数据：`python op.py 录入` 或直接编辑 data/*.csv",
                 "- 接入云端：准备好 SeaTable Token 后重跑 `python deploy.py --seatable-token ... --seatable-uuid ...`"]
    body += ["- 微信情报（可选）：`python wechat_intake.py doctor`",
             "- 物料行情（可选）：`python market.py snapshot --model 型号 --price 价格 --lifecycle 在产`",
             "", "配置文件（含凭证，勿提交）：`config.yaml`", ""]
    os.makedirs(os.path.dirname(REPORT_PATH), exist_ok=True)
    with open(REPORT_PATH, "w", encoding="utf-8") as f:
        f.write("\n".join(body))
    return s.done("已写入 data/deploy-report.md")


# ── main ────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser(
        description="生产交付协同助手 · 一键全自动部署（资料给到哪层用到哪层）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="示例：\n"
               "  python deploy.py --demo\n"
               "  python deploy.py --profile deploy.yaml\n"
               "  python deploy.py --seatable-token T --seatable-uuid U --partdb-url http://x --partdb-token K")
    ap.add_argument("--profile", help="资料文件（复制 deploy.yaml.example 填好）")
    ap.add_argument("--demo", action="store_true", help="零资料本地演示模式")
    ap.add_argument("--seatable-token", dest="seatable_token")
    ap.add_argument("--seatable-uuid", dest="seatable_uuid")
    ap.add_argument("--seatable-server", dest="seatable_server")
    ap.add_argument("--partdb-url", dest="partdb_url")
    ap.add_argument("--partdb-token", dest="partdb_token")
    ap.add_argument("--wechat-db", dest="wechat_db_path")
    ap.add_argument("--skip-deps", action="store_true", help="跳过依赖安装")
    ap.add_argument("--skip-sync", action="store_true", help="跳过数据同步")
    ap.add_argument("--skip-doctor", action="store_true", help="跳过开局体检")
    ap.add_argument("--no-cockpit", action="store_true", help="不生成驾驶舱")
    ap.add_argument("--dry-run", action="store_true", help="只预览步骤，不写任何文件")
    ap.add_argument("--yes", action="store_true", help="免交互（CI/无人值守）")
    args = ap.parse_args()

    print("=" * 62)
    print(" 生产交付协同助手 · 一键全自动部署")
    print("=" * 62)

    prof = collect_profile(args)
    backend = "seatable" if (prof.get("seatable_token") and prof.get("seatable_uuid")) else "local"
    if args.demo:
        backend = "local"
        prof = {}
    mode = "本地演示（demo）" if args.demo else backend
    print("部署模式：%s%s" % (mode, "（dry-run 预览）" if args.dry_run else ""))
    print("-" * 62)

    step_env_check(args)
    step_deps(args)
    step_config(args, prof, backend)
    step_verify(args, prof, backend)
    step_init_data(args, backend)
    step_cockpit(args)
    step_doctor(args)
    step_report(args, prof, backend)

    print("-" * 62)
    print(report_table())
    print("-" * 62)
    failed = [x for x in STEPS if x.status == "FAIL"]
    if failed:
        print("部署完成，但 %d 个步骤失败，请按明细处理后重跑（幂等，重跑安全）。" % len(failed))
        sys.exit(1)
    print("部署完成 ✓  打开「项目管理驾驶舱.html」即可查看；报告见 data/deploy-report.md")
    if not os.path.exists(PROFILE_EXAMPLE):
        _info("提示：可把 deploy.yaml.example 复制为 deploy.yaml 填入资料，团队任何人 clone 后一条命令即可复现本部署。")


if __name__ == "__main__":
    main()
