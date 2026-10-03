#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_smoke.py — 零依赖冒烟测试（不需要 pytest，直接 python test_smoke.py）。

覆盖：
  1. 所有模块可导入、HTML 模板占位符可完整替换
  2. 资源域计算：负载率 / 超载 / 闲置 / 排程冲突 / 人工成本
  3. 演示数据能稳定产出「超载 + 闲置 + 冲突」三种信号（否则驾驶舱首次打开是死页面）
  4. 口令从 config 读取，且明文不出现在产物 HTML 里
  5. 每条 next_action 的 cat 都能在驾驶舱里找到落点（否则「去处理」按钮消失）

强制在临时目录跑，绝不读写真实 data/。
"""
import datetime
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

# 测试已归入 tests/，模块与 data/ 在上一级仓库根目录
_HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _HERE)

_FAIL = []
_PASS = [0]


def check(cond, msg):
    if cond:
        _PASS[0] += 1
    else:
        _FAIL.append(msg)
        print("  FAIL: " + msg)


# 文本类扩展名才做去敏扫描；二进制/图片直接跳过
_BINARY_EXT = (".png", ".jpg", ".jpeg", ".gif", ".ico", ".pdf", ".xlsx", ".xls",
               ".zip", ".bundle", ".woff", ".woff2", ".ttf")


# ★ 2026-10-03 修复「假绿」缺口：`git ls-files` 默认 core.quotepath=true，
#   会把非 ASCII 路径转义成 `"a/\345\205\250.md"`（带引号 + C 八进制）。
#   旧实现直接 os.path.join(_HERE, p) → isfile 恒 False → **所有中文名文件被
#   静默踢出扫描面**。实测：加入 8 份中文名方案文档后扫描面仍恒为 206、
#   守卫一路全绿 —— 等于新增文件根本没被去敏检查过。
#   改法：`-z`（NUL 分隔，绝不转义）+ 显式 `core.quotepath=false` 双保险；
#   并在 [09b] 加一条「非 ASCII 路径必须全在扫描面内」的定点自检。
_LAST_TRACKED_PATHS = []   # 入库面原始路径（供 [09b] 做漏扫自检）


def _tracked_text_files():
    """返回「会被提交的文本文件」绝对路径列表。

    优先 `git ls-files -z`（精确等于入库面，天然排除 config.yaml / data/ 等
    gitignore 项）；无 git 时退化为目录遍历 + 跳过本地文件。
    """
    global _LAST_TRACKED_PATHS
    try:
        out = subprocess.run(
            ["git", "-C", _HERE, "-c", "core.quotepath=false", "ls-files", "-z"],
            capture_output=True, timeout=60)
        if out.returncode == 0 and out.stdout.strip(b"\x00"):
            paths = [p for p in
                     out.stdout.decode("utf-8", "replace").split("\x00") if p]
            _LAST_TRACKED_PATHS = paths
            res = []
            for p in paths:
                if p.endswith(_BINARY_EXT):
                    continue
                fp = os.path.join(_HERE, p)
                if os.path.isfile(fp):
                    res.append(fp)
            if res:
                return res
    except Exception:
        pass
    _LAST_TRACKED_PATHS = []
    _skip = {".git", "data", "__pycache__", ".venv", "node_modules", ".workbuddy"}
    res = []
    for root, dirs, files in os.walk(_HERE):
        dirs[:] = [d for d in dirs if d not in _skip]
        for fn in files:
            if fn.endswith(_BINARY_EXT):
                continue
            if fn.startswith("config") and fn.endswith((".yaml", ".yml")):
                continue
            res.append(os.path.join(root, fn))
    return res


def _covered_spans(text, wraps):
    """返回 text 中被 wraps 任一字符串覆盖的字符区间（已合并去重叠）。

    `wraps` = 该 needle 的「包裹片段」列表，来自 config.yaml::entities.fp_context。
    用途：中文没有词边界（空格）可依，2 字 needle 会撞上更长词组的**内部**。
    登记包裹片段 = 显式声明「这个更长词组是通用词，其中的 needle 出现属于偶然重叠」。
    （机制说明一律用中性词举例，**不写真名** —— 注释也在扫描面内。）
    """
    spans = []
    for _w in (wraps or []):
        _w = str(_w)
        if not _w:
            continue
        _i = text.find(_w)
        while _i >= 0:
            spans.append((_i, _i + len(_w)))
            _i = text.find(_w, _i + 1)
    if not spans:
        return []
    spans.sort()
    merged = [list(spans[0])]
    for _a, _b in spans[1:]:
        if _a <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], _b)
        else:
            merged.append([_a, _b])
    return [(a, b) for a, b in merged]


def _needle_hits(text, needle, wraps=None):
    """返回 needle 在 text 中**未被登记片段包裹**的出现位置列表。

    逐次出现判定，不是「此 needle 整体放行」。以中性词举例（needle=「联动」，
    包裹片段=「联动控制」，两者结构同形于真实场景）：
      「联动控制」里 → 被包裹 → 豁免；
      「联动那边确认了交期」里 → 未被包裹 → **仍然报红**。
    这个区别是防止 fp_context 变成「整条 needle 白名单」的关键。
    """
    spans = _covered_spans(text, wraps)
    out, _i, _n = [], text.find(needle), len(needle)
    while _i >= 0:
        if not any(_a <= _i and _i + _n <= _b for _a, _b in spans):
            out.append(_i)
        _i = text.find(needle, _i + 1)
    return out


# `entities` 里**不参与 needle 采集**的顶层键。
#   · generic_ok      —— 通用描述词（品类词等），本来就不是主体名
#   · fp_context      —— 逐次豁免配置（见 `_validate_fp_context`）
#   · generalized_ok  —— ★ 2026-10-03 新增：**已确认的泛化短形**
#                        某些真名当初被泛化成了「短形」，形态仍像主体名，
#                        于是每次审计都会被重新质疑一遍。单开一键显式登记，
#                        并由守卫输出**逐项打印**（本仓库规矩：放宽必须可见）。
#                        ⚠️ 别把它当第二个 generic_ok 随手加东西 ——
#                        加进去等于声明「这个字符串以后永不作为 needle」。
_NEEDLE_SKIP_KEYS = ("generic_ok", "fp_context", "generalized_ok")


def _generalized_ok(entities):
    """已确认的「泛化短形」白名单（显式登记，不入 needle、不报红）。

    ★ 为什么不塞进 `generic_ok`：语义不同（那是通用描述词），而且混在一起
    会让**放宽变得不可见**。这里由调用方（守卫输出）逐项打印。
    """
    return [str(x) for x in ((entities or {}).get("generalized_ok") or [])]


def _extract_needles(entities):
    """从 config.yaml::entities 抽出真值表 needle 与 fp_context。返回 (needles, fpctx)。

    规则（与历史实现一致，勿改阈值）：
      · 递归走 **dict 的 keys 与 values**、list 的元素；
      · 跳过 `_NEEDLE_SKIP_KEYS`（`generic_ok` 通用描述词白名单 /
        `fp_context` / `generalized_ok` 已确认的泛化短形）；
      · 长度 >= 2 才收录；
      · 白名单项、以 ①②③ 开头的「排除理由」文案不收录。

    ★ 阈值是 `>= 2`，**实测不可提到 >= 3**（见第 #141 号任务，值在 config.yaml）：
      86 条 needle 中 26 条是 2 字，其中 18 条是更长真名的**实际简称**、
      8 条是独立简称（无更长形式兜底）。
      提到 >= 3 会让「⟨2字简称⟩那边确认了交期」这类**真实业务简称句 8/8 漏扫**。
      假阳性的根因是中文无词边界，不是长度 → 用 fp_context 逐次豁免解决。
    """
    _ok = set((entities or {}).get("generic_ok") or [])
    _ok |= set(_generalized_ok(entities))
    _fpctx = (entities or {}).get("fp_context") or {}
    needles = set()
    stack = [v for k, v in (entities or {}).items()
             if k not in _NEEDLE_SKIP_KEYS]
    while stack:
        x = stack.pop()
        if isinstance(x, dict):
            stack.extend(x.keys())
            stack.extend(x.values())
        elif isinstance(x, list):
            stack.extend(x)
        else:
            s = str(x).strip()
            if len(s) >= 2 and s not in _ok and s[0] not in "①②③":
                needles.add(s)
    return needles, _fpctx


def _validate_fp_context(fpctx, needles):
    """校验 fp_context 配置本身是否合法。返回问题列表（空 = 合法）。

    这是 fp_context 机制**唯一的后门面**，必须逐条挡住「用配置改写判据」：
      ① 包裹片段必须**严格更长** —— 否则 `联动: [联动]` 等于整条放行；
      ② 包裹片段必须**含该 needle** —— 否则是永不生效的死配置（伪装成已处置）；
      ③ 包裹片段不得为空列表 —— 同上，等于整条放行；
      ④ needle 必须真在真值表里 —— 否则是在豁免一个不存在的东西。
    """
    bad = []
    for _n, _ws in sorted((fpctx or {}).items()):
        if _n not in needles:
            bad.append("%s（不在真值表里）" % _n)
            continue
        if not _ws:
            bad.append("%s（包裹片段为空 = 整条放行）" % _n)
            continue
        for _w in _ws:
            _w = str(_w)
            if len(_w) <= len(_n):
                bad.append("%s ← %s（未严格更长 → 等于全放行）" % (_n, _w))
            elif _n not in _w:
                bad.append("%s ← %s（不含该 needle → 死配置）" % (_n, _w))
    return bad


# ── 形态型去敏判据（2026-10-03 批次 1 新增）────────────────────────────────
# 真值表那 86 条 needle 全是**字面量型**（具体公司名 / 人名 / 别名），
# 对「形态型」真实数据完全失明。实测证据（本批次真实踩到）：
#
#   新增 `structure-redesign/` 两份文档含
#     · 一整行真实发货记录（产品型号 + 数量 + 真实顺丰运单号）
#     · 7 个真实产品型号
#   而去敏守卫**一路全绿**（真值表 86 条 × 225 文件 → 0 命中）。
#   ⇒ 「守卫通过」不等于「没有真实数据」—— 两者覆盖的根本不是同一类东西。
#
# 设计：字面量型与形态型**并存**，各自独立报红，FAIL 消息标明属哪一种。
#
#   · `()`  = **零容忍**：任何命中都报红。
#   · 非空集合 = 「已登记的既有命中」。这是给「本批次之前就已推上 PUBLIC、
#     但暂不属本批次修复范围」的数据留的**可见**通道 —— 目的是让放行
#     可复核，而**不是**静默跳过（新增的照样报红）。
#     ★ 2026-10-03 现状：**四类基线全部为空**（任务 #151 已把既有暴露清干净：
#       部件号 → `P0xxx` / 项目码 → `⟨项目码⟩`）。后续若需再登记，请一并
#       更新 `tests/test_desense_guard.py::test_基线当前必须为空` 的理由。
#
# 刻意**不含**「长数字串」这条规则：实测语料里有 15 处 ≥12 位独立数字
# （epoch 毫秒时间戳 + SQLite 字节串），信噪比太差，加了它只会被当噪音忽略。
_FORM_RULES = (
    ("快递单号", re.compile(r"\b(?:SF|JD|YT|ZT|YD|EMS)\d{10,}\b"),
     (),
     "真实运单号；占位请用 `SF1234567890` 这类明显示例号"),
    ("产品型号", re.compile(r"\bZD[A-Za-z0-9-][A-Za-z0-9_-]{1,40}\b"),
     (),
     "自家产品型号；泛化写作 `⟨型号A⟩`"),
    ("项目码", re.compile(r"\bZX\d{2}[A-Za-z0-9_.-]*\b"),
     (),
     "项目 / PCB 型号版本码；泛化写作 `⟨项目码⟩`"),
    ("部件号", re.compile(r"(?<![A-Za-z0-9])P0\d{3}(?![0-9])"),
     (),
     "PartDB 内部部件号；泛化写作 `P0xxx`"),
)

# 明显是「示例号」的数字段：全同位（`1111111111`）或顺序串（`1234567890`）。
# 刻意做成**显式枚举**而不是启发式 —— 放行名单必须一眼可审计。
_PLACEHOLDER_NUMS = ("1234567890", "123456789012", "0123456789")


def _is_placeholder_num(digits):
    """数字段是否明显是示例号（全同位 / 顺序串）。"""
    return len(set(digits)) == 1 or digits in _PLACEHOLDER_NUMS


def _form_violations(corpus, rules=None):
    """扫「形态型」真实数据，返回问题串列表（空 = 干净）。

    抽成函数是为了能被 `tests/test_desense_guard.py` 直接单测 ——
    否则「规则到底会不会报红」只能靠不可重跑的端到端观察
    （历史上「守卫假绿」正是这么来的）。
    """
    bad = []
    for name, rx, baseline, _hint in (rules or _FORM_RULES):
        for rel in sorted(corpus):
            text = corpus[rel]
            for m in sorted(set(rx.findall(text))):
                if name == "快递单号" and _is_placeholder_num(m[2:]):
                    continue
                if m in baseline:
                    continue
                p = text.find(m)
                ln = text.count("\n", 0, p) + 1
                a, b = max(0, p - 12), min(len(text), p + len(m) + 12)
                bad.append("[%s] %s:%d 「%s」← …%s…"
                           % (name, rel, ln, m, text[a:b].replace("\n", "⏎")))
    return bad


def _baseline_report(corpus, rules=None):
    """返回 (已用基线项, 失效基线项)。

    ★ 失效项（登记了但语料里已不存在）只**告警不报红**：
    它放行的是一个不存在的字符串 ⇒ 实际放宽为零，无安全影响，只是不整洁。
    （真正危险的是反向 —— 基线没登记却命中的新数据，那由 `_form_violations` 报红。）
    """
    used, stale = [], []
    for name, rx, baseline, _hint in (rules or _FORM_RULES):
        blob = "\n".join(corpus.get(r, "") for r in sorted(corpus))
        found = set(rx.findall(blob))
        for b in baseline:
            (used if b in found else stale).append("%s:%s" % (name, b))
    return used, stale


def main():
    tmp = tempfile.mkdtemp(prefix="cockpit_test_")
    data = os.path.join(tmp, "data")
    os.makedirs(data)
    try:
        import adapters.factory as F
        from adapters.local import LocalAdapter
        from adapters import schema

        real = F.get_adapter
        F.get_adapter = lambda config=None: LocalAdapter(data)
        try:
            from tools import seed_demo
            seed_demo.get_adapter = F.get_adapter
            seed_demo.main()

            ad = LocalAdapter(data)
            ad.auth()
            today = datetime.date.today()
            plans = ad.list_rows("生产计划")

            print("\n[1] 表结构")
            check(len(schema.TABLES) == 22, "TABLES 应为 22 张，实际 %d" % len(schema.TABLES))
            check("工作日志" in schema.TABLES and "阶段轨迹" in schema.TABLES, "第二大脑表未注册")
            check("资源" in schema.TABLES and "资源分配" in schema.TABLES, "资源域表未注册")
            check(schema.link_id_for("资源", "资源分配") == "RsAl", "资源↔资源分配 link_id 解析失败")
            check(schema.columns_of("资源") is not None, "columns_of('资源') 返回 None")
            check(len(schema.validate_enum("资源", {"类型": "外星人"})) == 1, "枚举软校验未生效")
            check(len(schema.validate_enum("资源", {"类型": "人员"})) == 0, "合法枚举被误报")

            print("[2] 资源域计算")
            sys.path.insert(0, os.path.join(_HERE, "cockpit"))
            import cockpit
            res = cockpit.compute_resources(ad, today, plans)
            check(res is not None, "compute_resources 返回 None（演示资源数据未被读到）")
            if res:
                check(res["week"]["workdays"] == 5, "本周工作日应为 5，实际 %s" % res["week"]["workdays"])
                check(len(res["over"]) >= 1, "演示数据未产生「超载」信号")
                check(len(res["idle"]) >= 1, "演示数据未产生「闲置」信号")
                check(len(res["conflicts"]) >= 1, "演示数据未产生「排程冲突」信号")
                check(res["labor_cost"] > 0, "人工成本为 0")
                check(len(res["plan_cost"]) > 0, "未按生产计划归集人工成本")
                over = res["over"][0]
                check(over["load"] > 100, "超载资源负载率应 >100%%，实际 %s" % over["load"])
                # 已完成的分配计成本但不占本周负载
                idle = res["idle"][0]
                check(idle["week_alloc"] == 0, "闲置资源本周投入应为 0")

            print("[3] 空资源时模块应隐藏")
            empty_dir = os.path.join(tmp, "empty")
            os.makedirs(empty_dir)
            ad2 = LocalAdapter(empty_dir)
            ad2.auth()
            check(cockpit.compute_resources(ad2, today, []) is None,
                  "无资源数据时应返回 None（隐藏模块），而不是空表")

            print("[4] 模型与行动建议")
            model = cockpit.compute(ad, today)
            check(model.get("resource") is not None, "model 缺少 resource")
            for k in ("res_load", "res_over", "res_conflict", "labor_cost"):
                check(k in model["kpi"], "KPI 缺少 %s" % k)
            acts = model["next_actions"]
            check(any(a.get("cat") == "resource" for a in acts), "未生成资源类行动建议")
            # 每条建议都要有 pri / cat / text，且 cat 在驾驶舱有映射
            # （KNOWN 必须与 cockpit.py 的 CAT_SEC 一致：v1.3 加了 wechat，v1.5 加了 market，
            #  v1.6 又把 market 类建议翻倍（物料+原料），v1.7 加了 risk（风险雷达），漏登记会导致测试 FAIL 而非静默漏报）
            KNOWN = {"purchase", "warehouse", "delivery", "production", "sales",
                     "boss", "resource", "wechat", "market", "risk"}
            for a in acts:
                check(a.get("pri") in ("高", "中", "提示"), "行动建议优先级非法: %r" % a.get("pri"))
                check(a.get("cat") in KNOWN, "行动建议 cat 未在 CAT_SEC 中登记: %r" % a.get("cat"))
                check(bool(a.get("text")), "行动建议缺少文案")

            print("[5] HTML 产物")
            html = cockpit.HTML.replace("__MODEL__", json.dumps(model, ensure_ascii=False))
            for k, v in cockpit.ICONS.items():
                html = html.replace("__" + k + "__", v)
            html = html.replace("__PW_BLOB__", json.dumps(cockpit.PW_BLOB))
            import re
            left = sorted(set(re.findall(r"__[A-Z][A-Z0-9_]{2,}__", html)))
            check(not left, "HTML 中存在未替换占位符: %s" % left)
            check("sec-Rs" in html, "HTML 中缺少资源区")
            check(".rs-track" in html, "缺少资源进度条样式（数字会被填充条盖住）")
            # 口令明文不得出现
            check(cockpit.ADMIN_PASSWORD not in html,
                  "管理员口令明文出现在 HTML 中（应为 base64 混淆）")
            for pw in cockpit.ROLE_PASSWORDS.values():
                check(pw not in html, "角色口令明文出现在 HTML 中")
            # 每个 CAT_SEC 已登记的类别都能在 JS 里找到
            check('resource:["Rs"' in html.replace(" ", "").replace("\n", "") or
                  'resource:["Rs","G","T"]' in html, "CAT_SEC 未登记 resource 类别")

            print("[6] 口令生成幂等")
            a1, r1 = cockpit._load_pw()
            a2, r2 = cockpit._load_pw()
            check(a1 == a2 and r1 == r2, "两次读取口令不一致（每次生成会导致分享出去的口令失效）")

            print("[7] 录入分级（第二大脑的安全底线）")
            from domain import intake

            def mk(op, table, data, row_id=None):
                return intake.assess(intake.Intent(op, table, data, row_id), ad)

            # 低风险：补备注 → 自动
            check(mk("update", "项目", {"备注": "客户口头反馈"}).risk == intake.AUTO,
                  "补备注被误判为高风险，日常录入会被卡死")
            # 高风险：改交期/金额 → 必须确认
            check(mk("update", "项目", {"合同交期": "2026-09-01"}).risk == intake.CONFIRM,
                  "改合同交期未被拦截 —— 关键数据可能被静默改写")
            check(mk("update", "项目", {"合同总价": 1}).risk == intake.CONFIRM,
                  "改合同总价未被拦截")
            # 高风险：在采购表新建 → 等于花钱
            check(mk("append", "PCB下单记录", {"生产计划": "X"}).risk == intake.CONFIRM,
                  "新建采购记录未被拦截")
            # 记忆表纯追加 → 自动
            check(mk("append", "工作日志", {"原话": "随便说两句"}).risk == intake.AUTO,
                  "写工作日志不该需要确认，否则没人愿意记")
            # 改动前的值必须被展示出来，否则「审核」是盲审
            p = ad.list_rows("项目")[0]
            it = mk("update", "项目", {"合同交期": "2026-12-31"}, p["__row_id__"])
            check(any("→" in w for w in it.warnings), "未展示 改动前→改动后，无法审核")

            # 阶段：合法推进自动、跳步/回退需确认
            check(mk("stage", "项目", {"原阶段": "立项", "新阶段": "研发"}).risk == intake.AUTO,
                  "合法阶段推进被误拦")
            jump = mk("stage", "项目", {"原阶段": "立项", "新阶段": "量产"})
            check(jump.risk == intake.CONFIRM, "阶段跳步未被拦截")
            check(any("跳步" in w for w in jump.warnings), "跳步未给出说明")
            back = mk("stage", "项目", {"原阶段": "试产", "新阶段": "打样"})
            check(back.risk == intake.AUTO, "试产→打样 是合法返工路径，不该拦")
            # 空的原阶段绝不能让跳步检查失效（曾经的真实缺陷）
            check(schema.stage_jump_warning("立项", "量产") is not None,
                  "stage_jump_warning 对跳步失灵")

            # 工序 / 生命周期两套枚举必须互不误伤
            check(len(schema.validate_enum("生产计划", {"阶段": "贴片"})) == 0,
                  "工序值『贴片』被生命周期枚举误报")
            check(len(schema.validate_enum("项目", {"阶段": "打样"})) == 0,
                  "生命周期值『打样』被误报")

            print("[8] 混合批次不可半写")
            batch = [mk("update", "项目", {"备注": "低风险"}),
                     mk("update", "项目", {"合同交期": "2026-10-01"})]
            auto, confirm = intake.split(batch)
            check(len(auto) == 1 and len(confirm) == 1, "分组结果不符")
            plan = intake.render_plan(batch)
            check("需要你确认" in plan and "合同交期" in plan, "确认清单未列出关键改动")
            print("[9] 可移植性：不得写死任何一家公司的供应商")
            from tools import doctor as _doc
            for _t, _d in schema.TABLE_DEFAULTS.items():
                for _c in ("供应商", "组装厂", "贴片厂"):
                    check(not _d.get(_c),
                          "「%s.%s」写死了默认供应商『%s』——别家公司装上会用错"
                          % (_t, _c, _d.get(_c)))
            # 用户配置能覆盖默认值
            md = schema.merged_defaults("外壳采购记录",
                                        {"defaults": {"外壳采购记录": {"供应商": "张三厂"}}})
            check(md.get("供应商") == "张三厂", "config 的 defaults 未生效")
            check(md.get("采购时间") == "__TODAY__", "覆盖 defaults 时把内置默认值弄丢了")
            # ── 去敏守卫：抓形态 + 扫全量 + 用真值表反向覆盖 ────────────
            # 历史教训（2026-09-26 复盘）：旧守卫是「15 个硬编码真名的黑名单
            # + 只扫 SKILL.md」，仓库里 28 个被跟踪文件、287 行真名全部漏过，
            # 而 CI 一路绿灯 —— 名单外的东西压根不在射程内，守卫自己还把
            # 真名提交进了仓库。现在：① 抓形态不抓名单 ② 扫入库面全量
            # ③ 真值表反向覆盖（新增真名只改 config，守卫自动生效）。
            print("[09b] 去敏守卫（全仓库）")
            _files = _tracked_text_files()
            check(len(_files) > 50, "守卫只扫到 %d 个文件，明显偏少（目录解析错？）" % len(_files))
            # ★ 定点自检（2026-10-03 补）：入库面路径不得被 git 引号转义。
            #   症状：`core.quotepath=true` 时中文路径变成 "a/\345\205\250.md"，
            #   os.path.isfile 恒 False → 该文件**静默漏扫**。实测 8 份中文名
            #   文档入库后扫描面纹丝不动（206）、守卫照样全绿 = 假绿。
            #   ⚠️ 别用「路径里有非 ASCII 字符」当判据 —— 转义后全是 ASCII，
            #   那条件恒不成立（第一版自检就是这么失效的，自测时抓到）。
            _quoted = [p for p in _LAST_TRACKED_PATHS if p.startswith('"')]
            check(not _quoted,
                  "入库面路径被引号转义（core.quotepath 未关）→ 会使 %d 个文件静默漏扫：%s"
                  % (len(_quoted), "；".join(_quoted[:3])))
            # ★ 定点自检（兜底）：扫描面不得明显小于入库面的非二进制规模。
            _nonbin = [p for p in _LAST_TRACKED_PATHS if not p.endswith(_BINARY_EXT)]
            check(len(_files) >= len(_nonbin) - 2,
                  "扫描面 %d 远小于入库面非二进制 %d —— 有路径未解析（中文名被漏扫？）"
                  % (len(_files), len(_nonbin)))
            _corpus = {}
            for _f in _files:
                try:
                    _rel = os.path.relpath(_f, _HERE).replace("\\", "/")
                    _corpus[_rel] = io.open(_f, encoding="utf-8").read()
                except Exception:
                    pass  # 非 UTF-8 文本直接跳过

            # (1) 手机号形态（测试占位号放行）
            _TEL_OK = ("13800000000", "13711111111", "13911111111")
            _bad = []
            for _rel, _t in _corpus.items():
                for _m in sorted(set(re.findall(r"(?<!\d)1[3-9]\d{9}(?!\d)", _t))):
                    if _m not in _TEL_OK:
                        _bad.append("%s→%s" % (_rel, _m[:3] + "****" + _m[-2:]))
            check(not _bad, "出现真实手机号：%s" % "；".join(_bad[:5]))

            # (2) 未泛化的完整企业名
            #     放行：含「示例/某/测试」的占位串、以及纯后缀词表项
            #     （`股份有限公司`/`科技有限公司` 等是匹配词表，不是主体名）
            _CORP_OK = re.compile(r"示例|某|测试")
            _SUFFIX_ONLY = re.compile(
                r"^(?:股份|有限责任|科技|电子|实业|贸易|网络|信息|智能|材料)?有限公司$")
            _bad = []
            for _rel, _t in _corpus.items():
                for _m in sorted(set(re.findall(r"[\u4e00-\u9fa5]{2,14}(?:有限公司|股份有限公司)", _t))):
                    if _CORP_OK.search(_m) or _SUFFIX_ONLY.match(_m):
                        continue
                    _bad.append("%s→%s" % (_rel, _m))
            check(not _bad, "出现未泛化企业名：%s" % "；".join(_bad[:5]))

            # (2b) 形态型真实数据：运单号 / 产品型号 / 项目码 / 部件号
            #      （2026-10-03 批次 1 补 —— 起因见 `_FORM_RULES` 上方注释：
            #       真实发货行与 7 个真实型号曾完整通过字面量型守卫。）
            _fbad = _form_violations(_corpus)
            check(not _fbad, "出现形态型真实数据：%s" % "；".join(_fbad[:4]))
            _fused, _fstale = _baseline_report(_corpus)
            print("       形态型规则 %d 条 × %d 文件 → %d 命中（已登记既有 %d 项%s）"
                  % (len(_FORM_RULES), len(_corpus), len(_fbad), len(_fused),
                     ("；★ 失效基线 %s（已不存在，建议清掉）"
                      % "、".join(_fstale)) if _fstale else ""))

            # (3) 真值表反向覆盖：config.yaml（已 gitignore）里的真实主体名
            #     一个都不该出现在被跟踪文件里。CI 上无此文件 → 自动跳过。
            _cfg = os.path.join(_HERE, "config.yaml")
            if not os.path.exists(_cfg):
                print("       （无本地 config.yaml，跳过真值表检查）")
            else:
                _ent = {}
                try:
                    import yaml
                    _ent = (yaml.safe_load(io.open(_cfg, encoding="utf-8").read())
                            or {}).get("entities") or {}
                except Exception as _e:
                    print("       config.yaml 解析失败，跳过：%s" % _e)
                # ── fp_context（2026-10-03 硬化，计划 §14.6 第 6 项 / 任务 #141）──
                # 原方案「CJK needle 长度 ≥3」已实测推翻：86 条 needle 里 26 条是 2 字，
                # 其中 18 条是更长真名的**实际简称**、8 条是无更长形式兜底的独立简称。
                # 实测 length≥3 会使「⟨2字简称⟩那边确认了交期」等
                # **真实业务简称句 8/8 全部漏扫** → 真名可直接推上 PUBLIC。
                # 而假阳性的根因不是长度，是**中文无词边界**：
                # 实测 12 句正常技术中文里 8 句误报（2 字 needle 撞上更长通用词组的内部）。
                # 故改为：**一条 needle 都不删**，只对「被登记片段包裹」的出现放行。
                # 提取逻辑见 `_extract_needles`（已抽成函数以便单测）。
                _needles, _FPCTX = _extract_needles(_ent)
                if _ent:
                    check(len(_needles) >= 20,
                          "真值表只解析出 %d 条 needle，entities 结构可能已变" % len(_needles))

                    # ★ 定点自检：fp_context 不得被用来改写判据本身。
                    #   逐条规则见 `_validate_fp_context` 的 docstring；
                    #   单独成函数是为了能被 tests/test_desense_guard.py 直接测。
                    _badfp = _validate_fp_context(_FPCTX, _needles)
                    check(not _badfp, "fp_context 可被用来绕过守卫：%s" % "；".join(_badfp[:4]))

                    _hits, _exc, _exc_n = [], 0, set()
                    for _rel in sorted(_corpus):
                        _t = _corpus[_rel]
                        for _n in sorted(_needles):
                            if _n not in _t:
                                continue
                            _w = _FPCTX.get(_n)
                            _pos = _needle_hits(_t, _n, _w)
                            if _w:
                                _tot = _t.count(_n)
                                if len(_pos) < _tot:
                                    _exc += _tot - len(_pos)
                                    _exc_n.add(_n)
                            # ★ FAIL 消息带「文件:行」+ 上下文窗口：是真名还是
                            #   「偶然撞词」，一眼可判，不必再人工反查是哪个词撞的。
                            for _p in _pos[:2]:
                                _ln = _t.count("\n", 0, _p) + 1
                                _a, _b = max(0, _p - 12), min(len(_t), _p + len(_n) + 12)
                                _hits.append("%s:%d 「%s」← …%s…"
                                             % (_rel, _ln, _n,
                                                _t[_a:_b].replace("\n", "⏎")))
                    check(not _hits,
                          "被跟踪文件里出现真实主体名：%s" % "；".join(_hits[:4]))
                    print("       真值表 %d 条 × %d 文件 → %d 命中%s"
                          % (len(_needles), len(_corpus), len(_hits),
                             ("（fp_context 豁免 %d 处 / %d 条 needle）"
                              % (_exc, len(_exc_n))) if _exc else ""))
                    # ★ 放宽必须可见：显式登记的泛化短形逐项打印出来，
                    #   不让它变成一条无人复核的静默豁免。
                    _gen = _generalized_ok(_ent)
                    if _gen:
                        print("       已确认泛化短形 %d 项（显式登记、不入 needle）：%s"
                              % (len(_gen), "、".join(_gen)))

            print("[10] 开局体检 doctor")
            f_empty = _doc.check_inventory({})
            check(len(f_empty) == 1 and f_empty[0].level == _doc.BLOCK,
                  "未配置库存源应报 BLOCK（缺料推算是核心能力）")
            check(f_empty[0].fix and "inventory.source" in f_empty[0].fix,
                  "库存源的修复建议应给出明确配置项")
            # 每条体检结论都必须给出「怎么办」，否则等于没说
            allf = _doc.run(ad, {})
            check(allf, "体检对演示库应至少给出若干结论")
            for _f in allf:
                check(bool(_f.fix), "体检项「%s」没有给出下一步操作" % _f.title)
            check(_doc.has_blocker(allf), "库存源未配时应判定为存在致命问题")
            check("体检" in _doc.render(allf), "体检报告渲染异常")
            # 有数据 + 有效库存源时，不该再报「数据库是空的」
            ok_f = _doc.run(ad, {"inventory": {"source": "file", "file": {"path": "stock.xlsx"}}})
            check(not any("空的" in x.title for x in ok_f),
                  "演示库有数据，却仍报「数据库是空的」")

        finally:
            F.get_adapter = real
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n" + "=" * 60)
    if _FAIL:
        print("FAILED %d / %d" % (len(_FAIL), len(_FAIL) + _PASS[0]))
        for f in _FAIL:
            print("  · " + f)
        return 1
    print("ALL %d CHECKS PASSED" % _PASS[0])
    return 0


if __name__ == "__main__":
    sys.exit(main())
