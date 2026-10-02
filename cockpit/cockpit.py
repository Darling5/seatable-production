#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
cockpit.py — 生产·项目管理驾驶舱 生成器。

读取 seatable-production 技能本地库（14 张表），计算「工时 / 成本 / 质量 / 供应链」
四维指标 + 项目总览 + 在制品看板 + 物料库存预警，渲染为单文件、内联 SVG、响应式
HTML 驾驶舱。

用法：
    python cockpit/cockpit.py              # 输出到默认工作区
    python cockpit/cockpit.py 路径/驾驶舱.html  # 自定义输出路径

数据来源：与 op.py 同源的适配器，local / seatable 自动切换。
生成的是「数据快照」：HTML 内嵌当前计算结果；也可用页面内「导入数据」按钮载入
导出的 JSON 快照刷新，无需重跑本脚本。
"""
import os
import sys
import json
import re
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from adapters.factory import get_adapter, load_config  # noqa: E402

_TZ = timezone(timedelta(hours=8))
# 默认输出到「当前工作目录/项目管理驾驶舱.html」；可用参数或环境变量 COCKPIT_OUT 覆盖。
# （不要硬编码某台机器的绝对路径——别人 clone 下来会写到不存在的目录。）
DEFAULT_OUT = os.environ.get("COCKPIT_OUT") or os.path.join(os.getcwd(), "项目管理驾驶舱.html")

# 采购 / 生产执行表中「花销」列与成本类目映射
COST_MAP = [
    ("PCB下单记录", "打板价格", "PCB"),
    ("外壳采购记录", "价格", "外壳"),
    ("IC采购记录", "采购花销", "IC"),
    ("贴片生产记录", "贴片价格", "贴片"),
    ("PCBA半成品采购记录", "采购花销", "PCBA"),
    ("组装料采购记录", "采购花销", "组装料"),
    ("组装记录", "组装价格", "组装"),
    ("成品采购记录", "采购花销", "成品"),
]
# 供应商列回退（不同表叫法不同）
SUPPLIER_COLS = ["供应商", "贴片厂", "组装厂"]

# 项目/生产计划状态归一化（真实库用「已交付/可能延迟/已超期/待客户下单」等，
# demo 用「进行中/计划中/已完成」；统一映射为 计划/进行中/已完成 三桶）
STATUS_DONE = {"已完成", "已交付"}
STATUS_ACTIVE = {"进行中", "可能延迟", "已超期", "待客户下单"}

# ── 排序口径（业主 2026-09-15）：**「计划中」优先** ────────────────────
# 原话：「所有的排序以及状态在计划中为优先排序」。项目表、生产计划表、甘特图**统一**用这套。
# 「暂放」并入「计划中」档（与 KPI 口径一致，见 compute() 的 p_planned 注释）；
# 「暂停」单独一档排第二 —— 它是「本来在排、被按住了」，比已交付更需被看到。
STATUS_ORDER = {"计划中": 0, "暂放": 0, "暂停": 1, "进行中": 2,
                "可能延迟": 3, "已超期": 3, "待客户下单": 3,
                "已完成": 9, "已交付": 9, "已取消": 9}


def status_rank(s):
    """排序档位：计划中 0 · 暂停 1 · 进行中 2 · 其它 3 · 已交付/完成 9（未知 5）。"""
    return STATUS_ORDER.get(str(s or "").strip(), 5)


# ── 生产计划「阶段」列（表格实际值）→ 进度%（业主 2026-09-15 口径）──────
# 业主原话：「当前进度根据表格实际的数据，也就是说每天晚上七点收集的各类数据为准」。
# 于是**不再按日期推算进度**（那是「今天该干到哪」，不是「实际干到哪」），
# 改为读生产计划表「阶段」列的实测值。已有实测值：
#     库存核对 · 备料中 · 贴片 · 组装 · 测试 · 改造中 · 已交付
# 工序命名规范（见「工序」列）：01 库存核对 · 03 外壳采购 · 04 贴片料采购 · 06 贴片 ·
#     08 组装 · 08T 成品采购 · 09 测试 · 10 配置IP端口 · 11 出货。
# ⚠️ 匹配用「**最长键优先**，同长取进度更大者」——否则「组装料采购」会被「组装」抢走
#    （变成 80% 而不是 45%），「贴片料采购」会被「贴片」抢走。
STAGE_PCT = {
    "已交付": 100, "已完成": 100,
    "出货": 98, "发货": 98,
    "配置": 95, "烧录": 95, "固件": 95,
    "测试": 92, "质检": 92,
    "成品采购": 86, "成品": 86,
    "组装": 80,
    "贴片生产": 65, "贴片": 65, "SMT": 65,
    "PCBA半成品": 58, "PCBA": 58,
    "IC采购": 48, "IC": 48,
    "组装料采购": 45, "组装料": 45,
    "贴片料采购": 38, "贴片料": 38,
    "外壳采购": 30, "外壳": 30, "备料": 30,
    "钢网": 28,
    "PCB下单": 25, "PCB": 25, "打板": 25,
    "库存核对": 10, "核对": 10,
    "改造": 70,
    "方案": 8, "立项": 3,
}


def stage_progress(stage, done):
    """生产计划表「阶段」实测值 → 进度%。已交付恒 100；认不出的阶段返回 0（宁可低报）。"""
    if done:
        return 100
    s = re.sub(r"[\s\-_·、,，。；;()（）\[\]【】/\\|]+", "", str(stage or "")).lower()
    if not s:
        return 0
    hits = [(len(k), v) for k, v in STAGE_PCT.items() if k.lower() in s]
    return max(hits)[1] if hits else 0


# ── 历史工期基线：无交期计划的**推算**依据（业主 2026-09-15 口径）────────
# 样本 = 已交付计划（状态 ∈ STATUS_DONE）的「花费天数」实测值。
# 2026-09-15 实测：n=27 · 中位 **28** · p75 37 · p90 49 · min 3 · max 94 天
# （与 foresee.py 的「立项→交货时间（自动记录）」口径**逐值吻合** 28/37/49，互为印证）。
# 为什么取**中位**而不是 p75：交期是「典型多久能做完」的期望值，中位最无偏；
# p75/p90 作为「保守上界」放进 tooltip，用于自己判断风险，不冒充交期。
HIST_FALLBACK_DAYS = 28   # 样本为空时的兜底工期


def hist_cycle(plans):
    """已交付计划的真实工期分位（天）→ 无交期计划用它推算交期。"""
    vals = []
    for p in plans:
        if p.get("状态") not in STATUS_DONE:
            continue
        try:
            v = float(str(p.get("花费天数")).strip())
        except Exception:
            continue
        if v > 0:
            vals.append(v)
    if not vals:
        return {"n": 0, "median": None, "p75": None, "p90": None}
    vals.sort()

    def q(x):
        return vals[min(len(vals) - 1, max(0, int(round(x * (len(vals) - 1)))))]

    return {"n": len(vals), "median": round(vals[len(vals) // 2]) if len(vals) % 2 else
            round((vals[len(vals) // 2 - 1] + vals[len(vals) // 2]) / 2),
            "p75": q(.75), "p90": q(.9), "min": vals[0], "max": vals[-1]}



def _num(v):
    if v is None:
        return 0.0
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, list):
        # 多选/链接列：逐个尝试，取第一个能转成数字的值
        for x in v:
            n = _num(x)
            if n:
                return n
        return 0.0
    s = str(v).replace("¥", "").replace(",", "").replace(" ", "").strip()
    if s == "":
        return 0.0
    try:
        return float(s)
    except ValueError:
        return 0.0


def _text(v):
    """SeaTable 云端直连时，链接列/多选列返回 list（或 dict）；
    本地 CSV 是字符串。统一压平成可显示、可做字典键的字符串。"""
    if v is None:
        return ""
    if isinstance(v, list):
        return "/".join(t for t in (_text(x) for x in v) if t)
    if isinstance(v, dict):
        return str(v.get("name") or v.get("text") or v.get("display_value") or "")
    return str(v).strip()


def _norm_rows(rows):
    """把云端返回的 list/dict 单元格统一压平为标量，下游按本地 CSV 习惯处理。"""
    out = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        out.append({k: (_text(v) if isinstance(v, (list, dict)) else v)
                    for k, v in row.items()})
    return out


class _NormAdapter:
    """归一化代理：list_rows 结果经过 _norm_rows；其余方法原样透传。"""

    def __init__(self, inner):
        self._inner = inner

    def list_rows(self, name, *args, **kwargs):
        return _norm_rows(self._inner.list_rows(name, *args, **kwargs))

    def __getattr__(self, item):
        return getattr(self._inner, item)


def _date(v):
    if not v:
        return None
    s = _text(v).strip()
    if not s:
        return None
    # 云端直连的日期列是完整 ISO（2026-05-07T00:00:00+08:00）；本地 CSV 是 2026-05-07。
    # 统一截到日期部分再解析，否则 strptime 全部失败 → 甘特图为空、剩余天数为 null。
    if "T" in s:
        s = s.split("T", 1)[0]
    elif " " in s:
        s = s.split(" ", 1)[0]
    for fmt in ("%Y-%m-%d", "%Y/%m/%d"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def _supplier(row):
    for c in SUPPLIER_COLS:
        if row.get(c):
            return _text(row[c]) or "未知"
    return "未知"


def _rid(row):
    """提取 SeaTable 行 ID（首列 __row_id__ 可能带 BOM）。"""
    for k, v in row.items():
        if k.replace("﻿", "") == "__row_id__":
            return v
    return None


def _load_partdb():
    """读取 partdb_sync.py 生成的真实快照；不存在返回 None。"""
    snap = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "partdb_snapshot.json")
    if not os.path.exists(snap):
        return None
    try:
        with open(snap, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _load_sync_meta():
    """读取 seatable_sync.py 写入的同步标记；存在说明业务表已是云端真实数据。"""
    meta = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "_sync_meta.json")
    if not os.path.exists(meta):
        return None
    try:
        with open(meta, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _read_local_csv(name):
    """读 data/ 下的本地专用 CSV（monitor/情报数据，不受云端同步影响）。"""
    import csv as _csv
    p = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", name)
    if not os.path.exists(p):
        return []
    try:
        with open(p, "r", encoding="utf-8-sig", newline="") as f:
            return [dict(r) for r in _csv.DictReader(f)]
    except Exception:
        return []


def _load_market():
    """物料行情模型（market.py 维护：监控清单 + 行情快照）。无数据返回 None。"""
    watch = _read_local_csv("物料监控清单.csv")
    hist = _read_local_csv("物料行情记录.csv")
    if not watch and not hist:
        return None
    try:
        th = float((load_config().get("market") or {}).get("alert_threshold") or 10)
    except Exception:
        th = 10.0
    bad_lc = {"NRND", "EOL停产", "停产", "EOL"}
    by_model = {}
    for s in hist:
        by_model.setdefault(s.get("物料型号", ""), []).append(s)
    rows, alerts = [], []
    for w in watch:
        if str(w.get("启用", "1")) in ("0", "否", "false"):
            continue
        m = w.get("物料型号", "")
        snaps = by_model.get(m, [])
        latest = snaps[-1] if snaps else None
        priced = [s for s in snaps if _num(s.get("单价")) > 0]
        buy = _num(w.get("上次采购价"))
        cur = _num((latest or {}).get("单价"))
        vs_buy = round((cur - buy) / buy * 100, 1) if (buy > 0 and cur > 0) else None
        lc = (latest or {}).get("生命周期", "") or "未知"
        chg = (latest or {}).get("涨跌幅", "")
        if lc in bad_lc:
            alerts.append({"type": "停产", "model": m,
                           "text": "生命周期 %s —— 确认替代料 / 最后采购窗口" % lc})
        if vs_buy is not None and abs(vs_buy) >= th:
            alerts.append({"type": "涨跌", "model": m,
                           "text": "现价较上次采购价 %+.1f%%（阈值 ±%.0f%%）" % (vs_buy, th)})
        try:
            if chg not in ("", None) and abs(float(chg)) >= th:
                alerts.append({"type": "涨跌", "model": m,
                               "text": "最新快照环比 %+.1f%%" % float(chg)})
        except (TypeError, ValueError):
            pass
        rows.append({
            "model": m, "category": w.get("类别", ""),
            "buy": w.get("上次采购价", ""), "buy_date": w.get("上次采购日期", ""),
            "price": (latest or {}).get("单价", ""), "channel": (latest or {}).get("渠道", ""),
            "date": (latest or {}).get("日期", ""), "lifecycle": lc, "vs_buy": vs_buy,
            "chg": chg, "hist": [_num(s.get("单价")) for s in priced][-12:],
        })
    return {"rows": rows, "alerts": alerts, "threshold": th,
            "watch_count": len(watch), "hist_count": len(hist)}


def _mille(v):
    """数字加千分位，用于原料行情（元/吨这类数字动辄六位，不加分隔符读不清）。"""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return str(v or "")
    return ("{:,.0f}".format(f)) if abs(f) >= 1000 else ("{:g}".format(f))


def _load_commodities(days=None):
    """原料行情模型（commodities.py 维护：data/原料行情记录.csv）。无数据返回 None。

    与物料行情（_load_market）是两条线：那个看单个型号贵没贵/停没停产，
    这个看上游原料（金/银/铜/锡/塑料）的成本走向，决定报价基调。

    口径纪律：期货「连续」与「具体合约」是两个口径，只按 (原料, 口径) 分组，
    绝不跨口径拼序列——否则会算出假涨跌（与元器件「串渠道假涨跌」同源）。

    阈值/天数从 config.yaml 的 commodities 段读，与 commodities.py 的 CLI 同一真源。
    """
    rows_all = _read_local_csv("原料行情记录.csv")
    if not rows_all:
        return None
    try:
        th = float((load_config().get("market") or {}).get("alert_threshold") or 10)
    except Exception:
        th = 10.0
    # 阈值与回看天数统一取自 commodities 段（与 commodities.py CLI 同一真源，
    # 免得命令行显示 ±3% 而驾驶舱按 ±5% 算，两边数字对不上）
    raw_cfg = (load_config() or {}).get("commodities") or {}
    try:
        raw_th = float(raw_cfg.get("alert_threshold") or 3)
    except (TypeError, ValueError):
        raw_th = 3.0
    if days is None:
        try:
            days = int(raw_cfg.get("trend_days") or 30)
        except (TypeError, ValueError):
            days = 30
    days = max(2, int(days))

    cutoff = (datetime.now().date() - timedelta(days=days)).isoformat()
    groups = {}
    for r in rows_all:
        key = (r.get("原料", "") or "", r.get("口径", "") or "连续")
        groups.setdefault(key, []).append(r)

    rows, alerts = [], []
    for (name, basis), items in groups.items():
        items = sorted(items, key=lambda x: str(x.get("日期", "")))
        win = [x for x in items if str(x.get("日期", "")) >= cutoff]
        priced = [x for x in items if _num(x.get("单价")) > 0]
        if not priced:
            continue
        latest = priced[-1]
        series = [_num(x.get("单价")) for x in win if _num(x.get("单价")) > 0]
        first = series[0] if len(series) > 1 else None
        cur = _num(latest.get("单价"))
        pct = round((cur - first) / first * 100, 1) if (first and first > 0 and cur > 0) else None
        rows.append({
            "name": name, "category": latest.get("类别", ""), "basis": basis,
            "price": latest.get("单价", ""), "unit": latest.get("单位", ""),
            "date": latest.get("日期", ""), "source": latest.get("来源", ""),
            "note": latest.get("备注", ""), "pct": pct,
            "hist": series[-30:],
        })
        if pct is not None and abs(pct) >= raw_th:
            alerts.append({
                "type": "涨" if pct > 0 else "跌", "name": name, "pct": pct,
                "text": "%s 口径近 %d 天 %+.1f%%（%s → %s %s，阈值 ±%.0f%%）"
                        % (basis, days, pct, _mille(first), _mille(cur),
                           latest.get("单位", ""), raw_th),
            })
    # 按波动幅度降序：驾驶舱「下一步行动」只带前 2 条，必须是动静最大的两个
    rows.sort(key=lambda r: (abs(r["pct"]) if r["pct"] is not None else -1), reverse=True)
    alerts.sort(key=lambda a: abs(a.get("pct") or 0), reverse=True)
    return {"rows": rows, "alerts": alerts, "threshold": raw_th,
            "days": days, "series_count": len(rows)}


def _load_foresee():
    """风险预测模型（foresee.py 维护：data/foresee.json）。无数据返回 None。

    三路预测：合同倒排（历史周期分位→环节最晚开始日）、供应商交期画像（承诺vs实际偏差→buffer）、
    缺料预警（BOM缺口×在途→必须立刻下单）。只读消费，缺失时驾驶舱不渲染该 section。
    """
    p = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "foresee.json")
    if not os.path.exists(p):
        return None
    try:
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


# ── 业务闭环控制平面（LTC）：状态中文名 + 主链顺序 ─────────────────────────
# 与 application/contracts.py 的 PROJECT_STATES 逐项对应。这里只加「人话」，
# 不动契约本身 —— 契约是代码层事实，这一层是展示层。
LOOP_STATE_ZH = {
    "lead": "线索", "opportunity": "商机",
    "requirement_confirming": "需求确认", "solution_confirming": "方案确认",
    "quotation_confirming": "报价确认", "contract_pending": "待签合同",
    "won_and_funded": "立项回款", "procurement": "采购",
    "in_production": "在产", "quality_check": "质检",
    "ready_to_ship": "待发货", "delivering": "发货中",
    "acceptance": "验收", "closed": "结案",
    "cancelled": "已取消", "after_sales": "售后",
}
LOOP_MAIN = ("lead", "opportunity", "requirement_confirming", "solution_confirming",
             "quotation_confirming", "contract_pending", "won_and_funded",
             "procurement", "in_production", "quality_check", "ready_to_ship",
             "delivering", "acceptance", "closed")
LOOP_SIDE = ("cancelled", "after_sales")
LOOP_TERMINAL = {"closed", "cancelled"}
# 状态停留超过这么多自然日 → 算「卡住」，进超时告警
LOOP_STUCK_DAYS = 7
# 控制平面 CSV 超过这么多天没更新 → 提示「已停更」。不用 0：跨一个周末本来就正常。
LOOP_STALE_DAYS = 3
# 越过「立项回款」之后才会出现的对象类型 —— 用来识别「预演空壳」
LOOP_ADVANCED_TYPES = ("contract", "project", "production_order", "purchase_order",
                       "shipment", "after_sales")


def _load_loop(today):
    """业务闭环控制平面（LTC 全链案件）模型。

    数据源：``data/business_loop/*.csv`` —— ``domain/order_to_cash.py`` 的 BusinessStore
    落盘，**不在 SeaTable 里**。驾驶舱此前只读 SeaTable，所以「微信来单 → CRM → 商机 →
    需求/方案/报价版本 → 合同 → 立项 → 采购 → 排产 → 生产 → 出货 → 验收回款 → 售后」
    这条链一直**看不见**。本函数把它接上：16 态漏斗 + 案件卡 + 状态停留时长。

    两条诚实标注（都是实测事实，不糊过去）：
      · ``stale``    —— CSV 超过 LOOP_STALE_DAYS 未更新时置位，界面须显式提示；
      · ``scenario`` —— 案件的对象构成完全对称、且无一越过 ``won_and_funded`` 时，
                        判定为「一次性预演空壳」，不是真实业务流。
    """
    objs = _read_local_csv(os.path.join("business_loop", "objects.csv"))
    if not objs:
        return None
    trans = _read_local_csv(os.path.join("business_loop", "transitions.csv"))
    evid = _read_local_csv(os.path.join("business_loop", "evidence.csv"))
    appr = _read_local_csv(os.path.join("business_loop", "approvals.csv"))

    # ── 数据新鲜度：控制平面还在被写入吗 ──
    src = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "data", "business_loop", "objects.csv")
    try:
        mt = datetime.fromtimestamp(os.path.getmtime(src), _TZ)
        updated_at = mt.strftime("%Y-%m-%d %H:%M")
        stale_days = (today - mt.date()).days
    except OSError:
        updated_at, stale_days = "未知", None

    # ── 案件根对象（root_id == object_id 的那一行）──
    roots = [r for r in objs if _text(r.get("object_id")) == _text(r.get("root_id"))]

    # ── 对象类型构成（用于识别「预演空壳」）──
    types = {}
    for r in objs:
        t = _text(r.get("object_type"))
        types[t] = types.get(t, 0) + 1
    advanced = sum(n for t, n in types.items() if t in LOOP_ADVANCED_TYPES)

    # ── 索引：证据/审批按对象计数，状态轨迹按案件取末次 ──
    ev_n, ap_n = {}, {}
    for e in evid:
        k = _text(e.get("related_object_id"))
        ev_n[k] = ev_n.get(k, 0) + 1
    for a in appr:
        k = _text(a.get("object_id"))
        ap_n[k] = ap_n.get(k, 0) + 1
    tr_by_root = {}
    for t in trans:
        tr_by_root.setdefault(_text(t.get("root_id")), []).append(t)

    by_state = {}
    cases = []
    for r in roots:
        rid = _text(r.get("object_id"))
        state = _text(r.get("state"))
        by_state[state] = by_state.get(state, 0) + 1
        members = [x for x in objs if _text(x.get("root_id")) == rid]
        mids = [_text(x.get("object_id")) for x in members]
        customer = next((_text(x.get("summary")) for x in members
                         if _text(x.get("object_type")) == "customer"), "")
        product = next((_text(x.get("summary")) for x in members
                        if _text(x.get("object_type")) in ("lead", "opportunity")), "")
        hist = sorted(tr_by_root.get(rid, []), key=lambda x: _text(x.get("created_at")))
        # 停留时长：末次状态迁移的时间（无迁移则回退到建档时间）
        since = _date(hist[-1].get("created_at")) if hist else None
        if since is None:
            since = _date(r.get("created_at"))
        dwell = (today - since).days if since else None
        due = _date(r.get("due_date"))
        left = (due - today).days if due else None
        terminal = state in LOOP_TERMINAL
        stuck = (dwell is not None and dwell > LOOP_STUCK_DAYS and not terminal)
        cases.append({
            "root_id": rid,
            "customer": customer or "（未命名客户）",
            "product": product or "—",
            "state": state,
            "state_zh": LOOP_STATE_ZH.get(state, state or "?"),
            "owner": _text(r.get("owner")) or "—",
            "next_action": _text(r.get("next_action")) or "—",
            "due_date": due.strftime("%Y-%m-%d") if due else "",
            "days_left": left,
            "dwell": dwell,
            "stuck": stuck,
            "terminal": terminal,
            "objects_n": len(members),
            "evidence_n": sum(ev_n.get(i, 0) for i in mids),
            "approvals_n": sum(ap_n.get(i, 0) for i in mids),
            "has_track": bool(hist),
            "updated_at": _text(r.get("updated_at"))[:10],
        })

    # 最需要先看的排前面：逾期 > 卡住 > 停留久
    cases.sort(key=lambda c: ((c["days_left"] if c["days_left"] is not None else 9999),
                              -1 if c["stuck"] else 0,
                              -(c["dwell"] if c["dwell"] is not None else -1)))

    overdue = [c for c in cases if c["days_left"] is not None and c["days_left"] < 0
               and not c["terminal"]]
    stuck = [c for c in cases if c["stuck"] and c not in overdue]

    # ── 16 态漏斗：案件数（按根对象计，不是按全部对象计）──
    funnel = [{"state": s, "zh": LOOP_STATE_ZH.get(s, s), "n": by_state.get(s, 0),
               "main": s in LOOP_MAIN} for s in list(LOOP_MAIN) + list(LOOP_SIDE)]
    advanced_n = sum(f["n"] for f in funnel if not f["main"])

    # 平均停留：只看有 dwell 的案件，且按状态分组
    dwell_by_state = {}
    for c in cases:
        if c["dwell"] is None:
            continue
        dwell_by_state.setdefault(c["state"], []).append(c["dwell"])
    dwell_avg = {k: int(round(sum(v) / len(v))) for k, v in dwell_by_state.items() if v}

    # ── 识别「预演空壳」────────────────────────────────────────────────────
    # 两个条件**同时**成立才算。只用「没走到立项」太弱 —— 一个刚开的真实业务
    # 本来就可能还没走到。第二条才是播种特征：人工一单一单建出来的对象构成
    # 不可能齐到 80% 以上都是同一套。
    #   ① 全库无一对象越过「立项回款」（无合同/项目/生产订单/采购订单/发货/售后）
    #   ② ≥80% 的案件恰好只含 <客户+线索+商机> 这「三件套」，构成完全一致
    seed_n = 0
    for r in roots:
        rid = _text(r.get("object_id"))
        ts = {_text(x.get("object_type")) for x in objs if _text(x.get("root_id")) == rid}
        if ts == {"customer", "lead", "opportunity"}:
            seed_n += 1
    seeded = len(roots) >= 5 and seed_n >= len(roots) * 0.8
    scenario = bool(advanced == 0 and seeded)

    return {
        "available": True,
        "updated_at": updated_at,
        "stale_days": stale_days,
        "stale": (stale_days is not None and stale_days > LOOP_STALE_DAYS),
        "scenario": scenario,
        "seeded_cases": seed_n,
        "case_count": len(cases),
        "object_count": len(objs),
        "transition_count": len(trans),
        "evidence_count": len(evid),
        "approval_count": len(appr),
        "types": types,
        "funnel": funnel,
        "max_funnel": max([f["n"] for f in funnel] or [0]),
        "cases": cases,
        "overdue": overdue,
        "stuck": stuck,
        "dwell_avg": dwell_avg,
        "advanced_n": advanced_n,
        "stuck_days": LOOP_STUCK_DAYS,
    }


# ────────────────────────────────────────────────────────────────────
# 数据来源健康度（全链降级 / Plan B 专项，2026-10-02）
#
# 背景：全链原有 **5 套互不通用的「数据陈旧」判据**各自为政 ——
#   ① 本文件 LOOP_STALE_DAYS（只看文件 mtime，只管控制平面 CSV）
#   ② 本文件前端 syncFreshness()（硬编码 6h/24h，只认 SeaTable 单源）
#   ③ application/project_brain/schema.py:210 DEFAULT_STALE_AFTER_HOURS=24（与驾驶舱不通）
#   ④ domain/market.py:472 cadence 7/15/30 天（那是省 API 配额，不是陈旧告警）
#   ⑤ workflows/workflow.py:196-204 _fresh_enough（mtime / 1 天，属验收器）
# 下面用一张 SOURCE_SPEC 统一 ①②③④（⑤ 属验收器，本批不动），并把
# 「最近一次运行里该源对应的步骤是成功 / 降级 / 失败」并到同一张表上。
#
# 两条**刻意**的设计取舍：
#   · 零侵入：不改任何业务脚本、不新增 _source_health.json —— 需要的时间戳
#     **都已经存在**（_sync_meta.json:synced_at / partdb_snapshot.json:generated_at /
#     wechat_intake/latest.json:pulled_at / foresee.json:generated_at）；
#     3 个无时间戳的 CSV 用文件 mtime 兜底（与 LOOP_STALE_DAYS 既有做法一致）。
#   · 驾驶舱运行在 cockpit 步骤内，**本次 run 的 final.json 还没写**（runner 在所有
#     步骤跑完后才落盘），所以只能读「最近一次已完成运行」的账本。面板上如实标注
#     是哪一个 run —— 不假装那是「本次」。
# ────────────────────────────────────────────────────────────────────
SOURCE_SPEC = (
    # (key, 显示名, 相对 data/ 的产物路径, 时间戳字段, ok 小时, bad 小时,
    #  对应 step_id, 该源不可用时的后果)
    #
    # SeaTable 的 ok 档取 16h（业主 2026-10-02 定）：daily 9:00 与 evening 19:00
    # 间隔最长 14h，沿用旧的 6h 会让驾驶舱每天大部分时间常亮 amber，稀释告警价值。
    ("seatable", "SeaTable 业务表", "_sync_meta.json", "synced_at", 16, 24,
     "seatable_sync", "业务表是全部计算的主数据源，不可降级（失败即中止）"),
    ("partdb", "PartDB 库存", "partdb_snapshot.json", "generated_at", 24, 72,
     "partdb_sync", "缺料与库存沿用上一份快照"),
    ("wechat", "微信情报", "wechat_intake/latest.json", "pulled_at", 24, 48,
     "wechat_pull", "群聊事件与图片 OCR 无新增"),
    ("foresee", "风险预测", "foresee.json", "generated_at", 24, 48,
     "foresee", "风险雷达沿用上一份"),
    ("wxmatch", "消息核对", "核对结果.csv", None, 24, 48,
     "wxmatch_scan", "核对结果沿用上一份"),
    ("commodities", "原料行情", "原料行情记录.csv", None, 168, 720, None,
     "上游原料行情停更（采集节奏 7~30 天，见 domain/market.py）"),
    # 控制平面沿用 LOOP_STALE_DAYS 这一个源，避免同一个阈值写两遍：
    # 3 天 = 72h 判偏旧，6 天 = 168h 判过旧。
    ("business_loop", "业务闭环控制平面", "business_loop/objects.csv", None,
     LOOP_STALE_DAYS * 24, LOOP_STALE_DAYS * 48,
     "loop_trigger", "全链案件台账停更"),
)

_LEVEL_RANK = {"ok": 0, "warn": 1, "bad": 2}


def _read_json_soft(path):
    """读 JSON，失败一律返回 {}（健康检查不能因为某个产物损坏就崩掉整页）。"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _dotted_get(obj, path):
    """按点路径取值（如 'shortage.snapshot_at'）；取不到或为空返回 None。"""
    cur = obj
    for part in str(path or "").split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur or None


def _parse_ts(value):
    """解析产物里几种已知的时间戳写法 → **带时区** datetime；解析不了返回 None。

    返回值的 tzinfo 恒为 ``_TZ``（本仓库所有产物时间戳都按东八区写）：调用方拿它
    直接和 ``datetime.now(_TZ)`` 相减，不会再踩 naive/aware 混算的 TypeError。
    文本自带偏移（如 ``...T10:00:00+08:00``）时以文本为准。
    """
    text = str(value or "").strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M",
                "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S%z",
                "%Y-%m-%d %H:%M:%S%z", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(text, fmt)
        except ValueError:
            continue
        return dt if dt.tzinfo is not None else dt.replace(tzinfo=_TZ)
    return None


def _latest_run_summary(base):
    """读**最近一次已完成运行**的 final.json（含各步 status / blocking）。

    「最近」的排序键复用 ``application.gates.run_sort_key`` —— 与发布门禁同一份
    逻辑，不再另造一套（``daily-`` < ``evening-`` 的字典序坑见 gates.run_sort_key）。
    """
    runs = os.path.join(base, "data", "runs")
    if not os.path.isdir(runs):
        return None
    cands = [os.path.join(runs, n) for n in os.listdir(runs)
             if os.path.exists(os.path.join(runs, n, "final.json"))]
    if not cands:
        return None
    if base not in sys.path:
        sys.path.insert(0, base)
    try:
        from application.gates import run_sort_key   # 与门禁共用，避免第二套实现
        latest = max(cands, key=run_sort_key)
    except Exception:
        try:
            latest = max(cands, key=os.path.getmtime)
        except OSError:
            latest = sorted(cands)[-1]
    data = _read_json_soft(os.path.join(latest, "final.json"))
    if not data:
        return None
    return {
        "run_id": data.get("run_id") or os.path.basename(latest),
        "workflow": data.get("workflow") or "",
        "status": data.get("status") or "",
        "finished_at": data.get("finished_at") or "",
        "summary": data.get("summary") or {},
        "steps": [{"step_id": s.get("step_id"), "status": s.get("status"),
                   "blocking": s.get("blocking", True),
                   "error": (s.get("error") or "").replace("\n", " ")[:90]}
                  for s in (data.get("steps") or [])],
    }


def _load_source_health(today, base=None):
    """全链数据来源健康度：统一陈旧判据 + 最近一次运行的降级状态。

    始终返回结构完整的 dict（sources 可能为空），让前端只需处理一种形状。
    ``base`` 仅在测试中注入（默认=本技能目录）；生产路径不传，行为不变。
    """
    base = base or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    now = datetime.now(_TZ)
    run = _latest_run_summary(base)
    steps = {s.get("step_id"): s for s in (run or {}).get("steps") or []}
    run_id = (run or {}).get("run_id") or ""

    out = []
    for key, name, rel, ts_path, ok_h, bad_h, step_id, consequence in SOURCE_SPEC:
        path = os.path.join(base, "data", rel.replace("/", os.sep))
        at, age_h = None, None
        if os.path.exists(path):
            if ts_path:
                at = _dotted_get(_read_json_soft(path), ts_path)
                dt = _parse_ts(at)
                if dt is not None:
                    try:
                        age_h = max(0.0, (now - dt).total_seconds() / 3600.0)
                    except (TypeError, OverflowError):
                        age_h = None     # 时间戳形态异常 → 走下面的 mtime 兜底
            if age_h is None:            # 无时间戳字段 / 解析失败 → 退回文件 mtime
                try:
                    mt = datetime.fromtimestamp(os.path.getmtime(path), _TZ)
                except OSError:
                    mt = None
                if mt is not None:
                    at = mt.strftime("%Y-%m-%d %H:%M")
                    age_h = max(0.0, (now - mt).total_seconds() / 3600.0)
        if age_h is None:
            level, note = "bad", "产物不存在"
        elif age_h > bad_h:
            level, note = "bad", "数据过旧"
        elif age_h > ok_h:
            level, note = "warn", "数据偏旧"
        else:
            level, note = "ok", ""

        degraded = False
        st = steps.get(step_id) if step_id else None
        if st:
            sstat = str(st.get("status") or "")
            if sstat == "failed" and not st.get("blocking", True):
                # 真实的降级：该步骤失败但声明为非阻断（下游照跑、数据沿用）
                degraded = True
                note = ("最近一次运行 %s 里该步骤失败但**未阻断** —— 当前数据是沿用的"
                        % (run_id or "?"))
                if _LEVEL_RANK[level] < _LEVEL_RANK["warn"]:
                    level = "warn"
            elif sstat in ("failed", "blocked"):
                level = "bad"
                note = "最近一次运行 %s 里该步骤 %s" % (run_id or "?", sstat)

        out.append({
            "key": key, "name": name, "artifact": "data/" + rel,
            "at": at, "age_h": (round(age_h, 1) if age_h is not None else None),
            "ok_h": ok_h, "bad_h": bad_h, "level": level, "note": note,
            "step_id": step_id or "", "degraded": degraded,
            "consequence": consequence,
        })

    n_bad = sum(1 for x in out if x["level"] == "bad")
    n_warn = sum(1 for x in out if x["level"] == "warn")
    return {
        "available": True,
        "checked_at": now.strftime("%Y-%m-%d %H:%M"),
        "run": run,
        "sources": out,
        "n_bad": n_bad, "n_warn": n_warn, "n_ok": len(out) - n_bad - n_warn,
        "worst": "bad" if n_bad else ("warn" if n_warn else "ok"),
    }


def _load_wxmatch():
    """消息↔SeaTable 核对模型（wxmatch.py 维护：data/核对结果.csv）。无数据返回 None。

    四类核对：收款 / 下单 / 客户合同PDF / 供应商合同。
    铁律：核对引擎只读不写库——高置信项仅生成「预填意图」，人确认后才落 SeaTable。
    驾驶舱只做展示与指引，不提供任何直接写库按钮。
    """
    rows = _read_local_csv("核对结果.csv")
    if not rows:
        return None
    pending = [r for r in rows if r.get("状态") in ("", "待核对", "待确认")]
    done = [r for r in rows if r.get("状态") not in ("", "待核对", "待确认")]
    by_type, by_conf = {}, {}
    for r in pending:
        by_type[r.get("类型", "其他")] = by_type.get(r.get("类型", "其他"), 0) + 1
        c = r.get("置信度", "低") or "低"
        by_conf[c] = by_conf.get(c, 0) + 1
    # 展示顺序：高置信（可确认意图）在前，其次中（有匹配项目待人工看），最后低
    conf_rank = {"高": 0, "中": 1, "低": 2}
    pending.sort(key=lambda r: (conf_rank.get(r.get("置信度", "低") or "低", 3),
                                str(r.get("日期", ""))), reverse=False)
    # 「下一步行动」素材：高置信收款项红字提示；中置信合同项给登记建议
    hi_pay = [r for r in pending
              if r.get("置信度") == "高" and "收款" in (r.get("类型") or "")]
    # 优先级 = 高置信收款 > 中置信有匹配项目（漏登记类）> 低置信
    act_ready = [r for r in pending
                 if r.get("置信度") == "高" or (r.get("置信度") == "中" and r.get("匹配项目"))]
    return {
        "pending": pending, "pending_count": len(pending),
        "done_count": len(done), "total": len(rows),
        "by_type": by_type, "by_conf": by_conf,
        "hi_pay_count": len(hi_pay), "act_ready": act_ready[:12],
        "scan_date": str(rows[-1].get("日期", ""))[:10] if rows else "",
    }


def _load_wechat(today):
    """微信情报模型（wechat_intake.py 维护：data/微信事件.csv）。无数据返回 None。"""
    events = _read_local_csv("微信事件.csv")
    if not events:
        return None
    pending = [e for e in events if e.get("状态") == "待确认"]
    week_ago = (today - timedelta(days=7)).isoformat()
    by_cat = {}
    for e in events:
        d = str(e.get("日期", ""))[:10]
        if d >= week_ago and e.get("分类"):
            by_cat[e["分类"]] = by_cat.get(e["分类"], 0) + 1
    recent = events[-40:][::-1]
    return {
        "pending": pending, "pending_count": len(pending),
        "today_count": sum(1 for e in events if str(e.get("日期", ""))[:10] == today.isoformat()),
        "by_cat": by_cat, "recent": recent,
    }


def _load_mindmaps():
    """流程思维导图模型（extract_mindmaps.py 维护：data/思维导图.json）。无数据返回 None。

    来源：业务方在 XMind 里维护的 7 张流程导图（生产/采购/研发/建模/库存/返修/品宣）。
    导图是「流程应该怎么走」的规范描述，用于和驾驶舱里的「实际在跑什么」对照。
    """
    p = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "思维导图.json")
    if not os.path.exists(p):
        return None
    try:
        with open(p, "r", encoding="utf-8") as f:
            d = json.load(f)
    except Exception:
        return None
    if not d.get("maps"):
        return None
    return d


def _overlap_days(a_start, a_end, b_start, b_end):
    """两个闭区间的重叠天数（含首尾）；任一端缺失按 0 处理。"""
    if not (a_start and a_end and b_start and b_end):
        return 0
    lo = max(a_start, b_start)
    hi = min(a_end, b_end)
    return (hi - lo).days + 1 if hi >= lo else 0


def compute_resources(adapter, today, plans):
    """资源域计算：负载率 / 冲突检测 / 成本归集。

    对标 MS Project 的「资源工作表 + 资源使用状况」：
      - 负载率 = 本周已分配投入量 / (日产能 × 本周工作日)
      - 超载   = 负载率 > 100%，即同一时间被安排了超过其产能的活
      - 冲突   = 同一资源在重叠日期区间内被分配到多个进行中的任务
      - 成本   = Σ(投入量 × 日费率)，按资源与按生产计划两个口径归集
    """
    try:
        resources = adapter.list_rows("资源")
        allocs = adapter.list_rows("资源分配")
    except Exception:
        resources, allocs = [], []
    if not resources and not allocs:
        return None  # 未启用资源管理 → 驾驶舱隐藏该模块

    # 本周窗口（周一~周日），用于算「当前负载」
    wk_start = today - timedelta(days=today.weekday())
    wk_end = wk_start + timedelta(days=6)
    workdays = sum(1 for i in range(7) if (wk_start + timedelta(days=i)).weekday() < 5)

    plan_name = {}
    for p in plans:
        rid = _rid(p)
        nm = p.get("生产产品") or p.get("生产计划编号") or ""
        if rid:
            plan_name[str(rid)] = nm
        if p.get("生产计划编号"):
            plan_name[str(p["生产计划编号"])] = nm

    ACTIVE_ALLOC = {"计划中", "进行中"}
    by_res = {}
    for r in resources:
        nm = (r.get("姓名") or "").strip()
        if not nm:
            continue
        by_res[nm] = {
            "name": nm,
            "type": (r.get("类型") or "人员").strip(),
            "stage": (r.get("所属工序") or "").strip(),
            "cap": _num(r.get("日产能")) or 1.0,
            "unit": (r.get("单位") or "人日").strip(),
            "rate": _num(r.get("日费率")),
            "status": (r.get("在岗状态") or "在岗").strip(),
            "alloc": 0.0, "week_alloc": 0.0, "cost": 0.0,
            "tasks": [], "conflicts": [],
        }

    alloc_rows = []
    for a in allocs:
        res = (a.get("资源") or "").strip()
        if not res:
            continue
        st, en = _date(a.get("开始日期")), _date(a.get("结束日期"))
        qty = _num(a.get("投入量"))
        status = (a.get("状态") or "计划中").strip()
        plan_ref = str(a.get("生产计划") or "").strip()
        alloc_rows.append({
            "res": res, "plan": plan_name.get(plan_ref, plan_ref), "plan_ref": plan_ref,
            "stage": (a.get("工序") or "").strip(), "qty": qty, "status": status,
            "start": st, "end": en,
            "start_s": st.isoformat() if st else "", "end_s": en.isoformat() if en else "",
        })

    # 资源不存在于「资源」表但出现在分配里 → 补一条影子资源，避免漏算
    for a in alloc_rows:
        if a["res"] not in by_res:
            by_res[a["res"]] = {
                "name": a["res"], "type": "人员", "stage": a["stage"], "cap": 1.0,
                "unit": "人日", "rate": 0.0, "status": "未登记",
                "alloc": 0.0, "week_alloc": 0.0, "cost": 0.0, "tasks": [], "conflicts": [],
            }

    for a in alloc_rows:
        R = by_res[a["res"]]
        if a["status"] in ACTIVE_ALLOC:
            R["alloc"] += a["qty"]
            R["week_alloc"] += _overlap_days(a["start"], a["end"], wk_start, wk_end) \
                * (a["qty"] / max((a["end"] - a["start"]).days + 1, 1) if a["start"] and a["end"] else 0)
        R["cost"] += a["qty"] * R["rate"]
        R["tasks"].append({"plan": a["plan"], "stage": a["stage"], "qty": a["qty"],
                           "status": a["status"], "start": a["start_s"], "end": a["end_s"]})

    # 冲突：同一资源、两条进行中分配的日期区间重叠
    for nm, R in by_res.items():
        act = [a for a in alloc_rows if a["res"] == nm and a["status"] in ACTIVE_ALLOC]
        for i in range(len(act)):
            for j in range(i + 1, len(act)):
                d = _overlap_days(act[i]["start"], act[i]["end"], act[j]["start"], act[j]["end"])
                if d > 0:
                    R["conflicts"].append({
                        "a": act[i]["plan"] or "（未关联计划）", "b": act[j]["plan"] or "（未关联计划）",
                        "days": d,
                    })

    rows = []
    for R in by_res.values():
        capacity = R["cap"] * workdays
        load = round(R["week_alloc"] / capacity * 100, 1) if capacity else 0.0
        rows.append({
            "name": R["name"], "type": R["type"], "stage": R["stage"],
            "cap": R["cap"], "unit": R["unit"], "rate": R["rate"], "status": R["status"],
            "alloc": round(R["alloc"], 2), "week_alloc": round(R["week_alloc"], 2),
            "capacity": round(capacity, 2), "load": load,
            "cost": round(R["cost"], 2),
            "over": load > 100, "idle": load == 0 and R["status"] == "在岗",
            "tasks": sorted(R["tasks"], key=lambda t: t["start"] or "9999"),
            "conflicts": R["conflicts"],
        })
    rows.sort(key=lambda x: -x["load"])

    # 按生产计划归集人工成本
    by_plan = {}
    for a in alloc_rows:
        key = a["plan"] or "（未关联计划）"
        rate = by_res[a["res"]]["rate"]
        b = by_plan.setdefault(key, {"plan": key, "qty": 0.0, "cost": 0.0, "people": set()})
        b["qty"] += a["qty"]
        b["cost"] += a["qty"] * rate
        b["people"].add(a["res"])
    plan_cost = sorted(
        [{"plan": v["plan"], "qty": round(v["qty"], 2), "cost": round(v["cost"], 2),
          "people": len(v["people"])} for v in by_plan.values()],
        key=lambda x: -x["cost"])

    over = [r for r in rows if r["over"]]
    idle = [r for r in rows if r["idle"]]
    conflicts = [{"name": r["name"], **c} for r in rows for c in r["conflicts"]]
    loads = [r["load"] for r in rows if r["status"] == "在岗"]
    return {
        "week": {"start": wk_start.isoformat(), "end": wk_end.isoformat(), "workdays": workdays},
        "rows": rows, "plan_cost": plan_cost,
        "total": len(rows), "on_duty": sum(1 for r in rows if r["status"] == "在岗"),
        "over": over, "idle": idle, "conflicts": conflicts,
        "labor_cost": round(sum(r["cost"] for r in rows), 2),
        "avg_load": round(sum(loads) / len(loads), 1) if loads else 0.0,
    }


def compute(adapter, today):
    projects = adapter.list_rows("项目")
    plans = adapter.list_rows("生产计划")
    repairs = adapter.list_rows("维修记录")
    shipments = adapter.list_rows("发货清单")
    inv = adapter.list_rows("库存核对记录")
    processes = adapter.list_rows("生产工序")

    # 历史工期基线：无交期计划的**推算**依据（业主 2026-09-15 口径）。
    # 全页共用这一个口径 —— 甘特图与「生产计划全表」都引用它，避免两处各算一遍算出两个数。
    hist = hist_cycle(plans)
    est_days = hist["median"] or HIST_FALLBACK_DAYS

    # ── 项目指标 ──
    p_total = len(projects)
    p_active = sum(1 for p in projects if p.get("状态") in STATUS_ACTIVE)
    # 「暂放」按业主口径归入「计划中」桶（否则 1+10+22=33 ≠ 36，有 3 个项目会凭空消失）
    p_planned = sum(1 for p in projects if p.get("状态") in ("计划中", "暂放"))
    p_done = sum(1 for p in projects if p.get("状态") in STATUS_DONE)
    contract = sum(_num(p.get("合同总价")) for p in projects)
    received = sum(_num(p.get("实收")) for p in projects)
    receivable = contract - received

    proj_overview = []
    for p in projects:
        c = _num(p.get("合同总价"))
        r = _num(p.get("实收"))
        proj_overview.append({
            "name": p.get("项目", ""),
            "status": p.get("状态", ""),
            "contract": c,
            "received": r,
            "receivable": c - r,
            "due": p.get("合同交期") or "",
        })

    # ── 应收口径对照：「表内待收」(SeaTable 公式列) vs 「应收」(看板现算 = 合同总价 − 实收) ──
    # 两者本应逐项相等，实测有 3 个项目对不上（合计差 ¥150,043）。逐项列出并给出成因，
    # 避免看板上「待收 96 万 / 应收 111 万」两个数并存却没人知道差在哪。
    recv_stored = sum(_num(p.get("待收")) for p in projects)
    recv_diff_rows = []
    for p in projects:
        _c = _num(p.get("合同总价"))
        _r = _num(p.get("实收"))
        _d = _num(p.get("待收"))
        _calc = _c - _r
        if abs(_d - _calc) <= 1:
            continue
        if _d > 0 and _calc > 0:
            _why = "元级舍入误差（公式列 vs 现算）"
        else:
            _why = "表内「待收」为空/0，实际仍有未收 ¥%s —— 建议补录或核对付款" % format(_calc, ",.0f")
        recv_diff_rows.append({
            "no": _text(p.get("项目编号")), "name": _text(p.get("项目")),
            "status": _text(p.get("状态")),
            "contract": round(_c, 2), "received": round(_r, 2),
            "stored": round(_d, 2), "calc": round(_calc, 2),
            "diff": round(_d - _calc, 2), "why": _why,
        })
    recv_diff_rows.sort(key=lambda x: -abs(x["diff"]))
    recv_check = {
        "kpi": round(receivable, 2), "stored": round(recv_stored, 2),
        "diff": round(recv_stored - receivable, 2),
        "rows": recv_diff_rows, "n": len(recv_diff_rows),
    }

    # ── 成本归集 ──
    category_cost = {c[2]: 0.0 for c in COST_MAP}
    plan_cost = {}
    supplier_stat = {}   # name -> {orders, ontime, late, pending, cost}
    purchase_rows = []   # 用于逾期/准时率
    for table, col, cat in COST_MAP:
        for row in adapter.list_rows(table):
            amt = _num(row.get(col))
            if amt <= 0:
                continue
            category_cost[cat] += amt
            pn = _text(row.get("生产计划", "")) or "(未关联)"
            plan_cost[pn] = plan_cost.get(pn, 0.0) + amt
            sup = "PCB打板" if table == "PCB下单记录" else _supplier(row)
            # 准时率（仅对有交期与到货/逾期信息的单计入）
            eta_s = row.get("下单时间")
            lead = _num(row.get("交期"))
            arr = _date(row.get("到货时间")) if table != "PCB下单记录" else _date(row.get("最终完成时间"))
            eta = _date(eta_s)
            if eta and lead:
                st = supplier_stat.setdefault(sup, {"orders": 0, "ontime": 0, "late": 0, "pending": 0, "cost": 0.0})
                st["orders"] += 1
                st["cost"] += amt
                eta_date = eta + timedelta(days=int(lead))
                if arr:
                    if arr <= eta_date:
                        st["ontime"] += 1
                    else:
                        st["late"] += 1
                elif eta_date < today:
                    st["late"] += 1  # 已逾期未到货
                else:
                    st["pending"] += 1
            purchase_rows.append({"table": table, "cat": cat, "row": row, "amt": amt,
                                  "supplier": sup, "arr": arr, "eta": eta, "lead": lead})

    total_cost = sum(category_cost.values())

    # 台账口径：生产计划「生产总花销」汇总（含人工/辅料，比采购明细更全）
    ledger_cost = sum(_num(p.get("生产总花销")) for p in plans)
    # 单片成本：生产计划「此次单片成本」均值（真实业务填写的单价基准）
    unit_costs = [_num(p.get("此次单片成本")) for p in plans if _num(p.get("此次单片成本")) > 0]
    unit_cost = round(sum(unit_costs) / len(unit_costs), 2) if unit_costs else 0.0

    # 项目级成本（通过生产计划关联）
    plan_to_proj = {p.get("生产产品", ""): p.get("关联项目", "") for p in plans}
    proj_cost = {}
    for pn, amt in plan_cost.items():
        pj = plan_to_proj.get(pn, "")
        proj_cost[pj] = proj_cost.get(pj, 0.0) + amt
    total_profit = contract - total_cost
    margin = (total_profit / contract * 100) if contract else 0.0
    budget_margin = 30.0  # 目标毛利率

    # 成本结构（降序，仅保留 >0）
    category = [{"name": k, "value": round(v, 2)}
                for k, v in category_cost.items() if v > 0]
    category.sort(key=lambda x: x["value"], reverse=True)

    # ── 在制品看板 ──
    wip = []
    stage_dist = {}
    for p in plans:
        stage = p.get("阶段", "未定义")
        stage_dist[stage] = stage_dist.get(stage, 0) + 1
        if p.get("状态") in STATUS_DONE:
            continue
        due = _date(p.get("合同交期"))
        remain = (due - today).days if due else None
        wip.append({
            "product": p.get("生产产品", ""),
            "qty": _num(p.get("数量")),
            "stage": stage,
            "status": p.get("状态", ""),
            "start": p.get("立项日期") or "",
            "due": p.get("合同交期") or "",
            "remain": remain,
            "overdue": (remain is not None and remain < 0),
        })
    wip.sort(key=lambda x: (x["remain"] is None, x["remain"] if x["remain"] is not None else 0))

    # ── 工时 ──
    # 真实库未填「完货日期」，改用生产计划表「花费天数」(业务实际填写的工序耗时)
    # 作为实际生产周期；「合同交期 − 立项日期」为允许周期，实际 ≤ 允许 即视为交期达成。
    total_plans = len(plans)
    ontime = 0
    dated = 0
    cycles = []
    for p in plans:
        sd = _date(p.get("立项日期"))
        cd = _date(p.get("合同交期"))
        spend = _num(p.get("花费天数"))   # 实际花费天数
        if spend > 0:
            cycles.append({"product": p.get("生产产品", ""), "days": int(spend)})
            if sd and cd:
                allow = (cd - sd).days
                dated += 1
                if spend <= allow:
                    ontime += 1
    # 无「花费天数」则无法判定周期达成率，显示 N/A 而非 0%
    ontime_rate = (ontime / dated * 100) if dated else None
    avg_cycle = round(sum(c["days"] for c in cycles) / len(cycles), 1) if cycles else 0

    # 产线实时流转：在产计划（状态≠已交付）当前所处工序（生产计划.阶段）的分布
    # 注意：绝不能用「生产工序」主表的「当前流程」计数——该表是工序目录（每工序仅一行），
    # 计数恒为 1，无法表达“卡在哪个工序”。正确口径是按在产计划的实际阶段聚合。
    flow_dist = {}
    for r in plans:
        if (r.get("状态") or "").strip() == "已交付":
            continue
        st = (r.get("阶段") or "未定义").strip() or "未定义"
        flow_dist[st] = flow_dist.get(st, 0) + 1

    # ── 质量 ──
    shipped = sum(_num(s.get("发货数量")) for s in shipments) or len(shipments)
    repair_total = len(repairs)
    repair_rate = (repair_total / shipped * 100) if shipped else 0.0
    smt_rows = adapter.list_rows("贴片生产记录")
    # ⚠️ 良率分母**只能**统计「良品数量已录」的行 —— 2026-10-02 修。
    # 真实库里有 2 行只填了贴片数量（20260808-001 投 3000、20260919-001 投 1000）
    # 而良品数量为空，旧写法把这两行的投入计进分母，算出 30.4% 的**假良率**
    # （剔除后真实 94.4%）。这与「驾驶舱导航角标把积压算成今日待办」是同一类 bug：
    # 分子有数、分母缺数的行，必须整行剔除，并把剔除量暴露给前端标注「N/M 批已录」。
    smt_pairs = []
    smt_missing_qty = 0
    smt_missing_n = 0
    for r in smt_rows:
        raw_good = r.get("良品数量")
        q = _num(r.get("贴片数量"))
        if raw_good in (None, "") or q <= 0:
            if q > 0:
                smt_missing_n += 1
                smt_missing_qty += q
            continue
        smt_pairs.append((q, _num(raw_good)))
    smt_yield = 0.0
    if smt_pairs:
        tot = sum(a for a, _b in smt_pairs)
        good = sum(_b for _a, _b in smt_pairs)
        smt_yield = (good / tot * 100) if tot else 0.0
    asm_rows = adapter.list_rows("组装记录")
    asm_yields = [_num(r.get("组装良品率")) for r in asm_rows if _num(r.get("组装良品率")) > 0]
    # 真实库 2 条组装记录的良品率列均为空(#DIV/0) → 无数据，标注「未录入」而非 0%
    asm_yield = round(sum(asm_yields) / len(asm_yields), 1) if asm_yields else None
    repair_overdue = 0
    repair_days = []
    repair_list = []
    for r in repairs:
        rt = _date(r.get("返修时间"))
        ft = _date(r.get("完成时间"))
        req = _num(r.get("要求交期"))
        if rt and ft:
            d = (ft - rt).days
            repair_days.append(d)
        done = ft is not None
        overdue = (not done) and rt and req and (rt + timedelta(days=int(req)) < today)
        if overdue:
            repair_overdue += 1
        repair_list.append({
            "proj": r.get("相关项目", ""),
            "item": r.get("维修清单", ""),
            "back": r.get("返修时间", ""),
            "req": int(req),
            "done": done,
            "overdue": bool(overdue),
        })
    repair_avg = round(sum(repair_days) / len(repair_days), 1) if repair_days else 0

    # ── 供应链：采购逾期 + 供应商准时率 ──
    overdue_list = []
    for pr in purchase_rows:
        eta = pr["eta"]
        lead = pr["lead"]
        arr = pr["arr"]
        if eta and lead:
            eta_date = eta + timedelta(days=int(lead))
            if arr is None and eta_date < today:
                overdue_list.append({
                    "supplier": pr["supplier"],
                    "material": pr["row"].get("物料名称") or pr["row"].get("外壳名称") or pr["row"].get("PCB型号版本") or "",
                    "plan": pr["row"].get("生产计划", ""),
                    "eta": eta_date.isoformat(),
                    "days": (today - eta_date).days,
                })
    overdue_list.sort(key=lambda x: x["days"], reverse=True)

    supplier = []
    for name, st in supplier_stat.items():
        rated = st["ontime"] + st["late"]
        rate = (st["ontime"] / rated * 100) if rated else None
        supplier.append({
            "name": name,
            "orders": st["orders"],
            "ontime": st["ontime"],
            "late": st["late"],
            "pending": st["pending"],
            "cost": round(st["cost"], 2),
            "rate": round(rate, 1) if rate is not None else None,
        })
    supplier.sort(key=lambda x: (x["rate"] is None, x["rate"] if x["rate"] is not None else 0))

    # ── 物料库存预警（来自库存核对记录；PartDB 缺料检查未配置则跳过）──
    inventory_warn = []
    for r in inv:
        fin = r.get("最终完成时间")
        if not fin or str(r.get("核对结果", "")).strip() in ("", "待核对", "异常"):
            inventory_warn.append({
                "plan": r.get("链接其他记录", ""),
                "result": r.get("核对结果", "") or "待核对",
                "time": fin or "",
            })

    # ── 现金流预测（30/60/90 天）──
    def bucket_of(d):
        if d is None:
            return None
        delta = (d - today).days
        if delta < 0:
            return "逾期"
        if delta <= 30:
            return "30"
        if delta <= 60:
            return "60"
        if delta <= 90:
            return "90"
        return ">90"

    cash = {"逾期": {"in": 0.0, "out": 0.0}, "30": {"in": 0.0, "out": 0.0},
            "60": {"in": 0.0, "out": 0.0}, "90": {"in": 0.0, "out": 0.0}}
    for p in projects:
        amt = _num(p.get("合同总价")) - _num(p.get("实收"))
        if amt <= 0:
            continue
        b = bucket_of(_date(p.get("合同交期")))
        if b and b in cash:
            cash[b]["in"] += amt
    for pr in purchase_rows:
        if pr["arr"] is None and pr["eta"] and pr["lead"]:
            eta_date = pr["eta"] + timedelta(days=int(pr["lead"]))
            b = bucket_of(eta_date)
            if b and b in cash:
                cash[b]["out"] += pr["amt"]
    cashflow = []
    for k in ["逾期", "30", "60", "90"]:
        cashflow.append({"bucket": k, "in": round(cash[k]["in"], 2),
                         "out": round(cash[k]["out"], 2),
                         "net": round(cash[k]["in"] - cash[k]["out"], 2)})

    # 应收款明细
    receivable_list = [p for p in proj_overview if p["receivable"] > 0]
    # 合同交期可能为空（None/""）——排序键统一转字符串，避免 None 与 str 比较抛 TypeError
    receivable_list.sort(key=lambda x: str(x.get("due") or ""))

    # ── 各计划在 9 张环节表里的**实际记录数**（业主口径：进度以「表格实际数据」为准）──
    # 环节表用「生产计划」链接列指向计划；云端只回 [{row_id, display_value}]，
    # 而 display_value 是**产品名**（会重名 —— 实测有 4 条计划都叫「蓝牙信标」）
    # → 只能靠 row_id 归属，不能用名字。
    # ⚠️ 这不参与进度百分比（阶段列的语义更直接），但作为「这条计划底下到底登记了什么」
    #    放进甘特 tooltip —— 台账全空时一眼能看出「进度是虚的」。
    STAGE_TABLES = [("库存核对记录", "库存核对"), ("PCB下单记录", "PCB下单"),
                    ("外壳采购记录", "外壳采购"), ("IC采购记录", "IC采购"),
                    ("贴片生产记录", "贴片生产"), ("PCBA半成品采购记录", "PCBA半成品"),
                    ("组装料采购记录", "组装料采购"), ("组装记录", "组装"),
                    ("成品采购记录", "成品采购")]
    plan_stages = {}
    for _t, _label in STAGE_TABLES:
        try:
            for _row in adapter.list_rows(_t):
                _v = _row.get("生产计划")
                if not isinstance(_v, list):
                    continue
                for _x in _v:
                    if not (isinstance(_x, dict) and _x.get("row_id")):
                        continue
                    _d = plan_stages.setdefault(_x["row_id"], {})
                    _d[_label] = _d.get(_label, 0) + 1
        except Exception:
            pass  # 某张环节表读不到不该让整个甘特图塌掉

    # ── 甘特图数据（立项 → 交期；进度取表格实测阶段）──
    # 业主 2026-09-15 三条口径：
    #   ① 没填「合同交期」的 → **用历史数据推算**（原先沉底为「待补交期」的做法作废）
    #   ② 当前进度 → **按表格实际数据**（生产计划表「阶段」列，即每晚 19:00 同步后的快照），
    #      不再按日期推算 —— 日期推的是「今天该干到哪」，不是「实际干到哪」
    #   ③ 排序 → **「计划中」优先**（见 STATUS_ORDER）
    # ⚠️ 推算是**只读展示**：绝不写回 SeaTable（写回会把业主的「交期待收款后回填」规则搞脏）。
    # （历史基线 hist / est_days 已在函数开头算好，全页共用）
    gantt = []
    pend_count = 0
    for p in plans:
        sd = _date(p.get("立项日期"))
        cd = _date(p.get("合同交期"))
        if not sd:
            continue  # 连立项日期都没有 → 无法定位到时间轴，跳过
        status = p.get("状态", "")
        done = status in STATUS_DONE
        est = cd is None
        est_cd = sd + timedelta(days=int(est_days))   # ③ 历史推算交期（始终算，即便有合同交期也留作对标）
        if est:
            cd = est_cd
            pend_count += 1
        span = (cd - sd).days
        gantt.append({
            "name": p.get("生产产品", "") or p.get("生产计划编号", "") or "(未命名)",
            "status": status,
            "start": sd.isoformat(), "end": cd.isoformat(),
            # 业主 2026-09-15：「甘特图要显示三种状态」——
            #   ① 实际状态 = 进度条本身（进度的实测填充 + done/overdue 配色）
            #   ② 合同交期 = due（**承诺**，实心菱形；没填时为 null）
            #   ③ 历史推算交期 = due_est（**我们对标用的**，空心菱形；恒有值）
            # 三种同轴并列，谁早谁晚一眼可比 —— 这正是「合同日期是否比历史惯例更紧」的判据。
            "due": None if est else cd.isoformat(),
            "due_est": est_cd.isoformat(),
            "done": done, "overdue": bool((not done) and cd < today),
            "progress": stage_progress(p.get("阶段"), done),
            "est": est, "pending": False, "days": span, "row_id": _rid(p),
            "stage": _text(p.get("阶段")),
            "stages": plan_stages.get(_rid(p), {}),
        })
    # 排序：**计划中优先** → 暂停 → 进行中 → … → 已交付；同档按立项日（越早越靠前）
    gantt.sort(key=lambda x: (status_rank(x["status"]), x["start"], x["name"]))

    # ── 下一步行动建议（按优先级推导）──
    pd = _load_partdb()
    actions = []
    if overdue_list:
        top = overdue_list[0]
        actions.append({"pri": "高", "cat": "purchase", "text": f"跟进 {len(overdue_list)} 笔逾期采购，最紧急：{top['supplier']} 的 {top['material'] or '物料'} 已逾期 {top['days']} 天，尽快催收/换源"})
    if pd:
        b = pd.get("bom") or {}
        sh = b.get("shortage") or []
        if sh:
            zero = sum(1 for x in sh if x.get("confirmed") == 0)
            actions.append({"pri": "高", "cat": "warehouse", "text": f"补料：{b.get('project_name', '在产项目')} 有 {len(sh)} 种物料缺口（{zero} 种零确认库存），尽快下达采购单"})
    od_wip = [w for w in wip if w["overdue"]]
    if od_wip:
        w = od_wip[0]
        actions.append({"pri": "高", "cat": "delivery", "text": f"推进逾期未交付：{w['product']} 已逾期 {-w['remain']} 天，优先排产/协调产能"})
    nd = [w for w in wip if w["remain"] is not None and 0 < w["remain"] <= 7]
    if nd:
        w = nd[0]
        actions.append({"pri": "中", "cat": "delivery", "text": f"临近交期：{w['product']} 仅剩 {w['remain']} 天，确保本周内完工"})
    if repair_overdue:
        actions.append({"pri": "中", "cat": "production", "text": f"处理 {repair_overdue} 笔超期维修单，避免客户投诉升级"})
    if receivable_list:
        r = receivable_list[0]
        actions.append({"pri": "中", "cat": "sales", "text": f"催收应收：最早到期「{r['name']}」应收 ¥{r['receivable']:,.0f}（交期 {r['due']}）"})
    if flow_dist:
        bn = max(flow_dist.items(), key=lambda x: x[1])
        actions.append({"pri": "提示", "cat": "production", "text": f"产能瓶颈：{bn[0]} 环节积压 {bn[1]} 个在产计划，建议增配资源或并行处理"})
    if asm_yield is None:
        actions.append({"pri": "提示", "cat": "production", "text": "补录组装良品率：当前组装记录均未填，质量维度暂不完整"})

    # ── 资源域（人/设备负载、冲突、人工成本）──
    res_model = compute_resources(adapter, today, plans)
    if res_model:
        if res_model["over"]:
            t = res_model["over"][0]
            actions.insert(0, {"pri": "高", "cat": "resource",
                               "text": f"资源超载：{t['name']} 本周负载 {t['load']:.0f}%（已排 {t['week_alloc']}{t['unit']} / 产能 {t['capacity']}{t['unit']}），需改期或加人"})
        if res_model["conflicts"]:
            c = res_model["conflicts"][0]
            actions.insert(0, {"pri": "高", "cat": "resource",
                               "text": f"排程冲突：{c['name']} 在「{c['a']}」与「{c['b']}」上有 {c['days']} 天重叠，需错开档期"})
        if res_model["idle"]:
            nm = "、".join(r["name"] for r in res_model["idle"][:3])
            actions.append({"pri": "中", "cat": "resource",
                            "text": f"资源闲置：{nm} 本周暂无任务分配，可承接新单或支援瓶颈工序"})

    # ── 微信情报 & 物料行情（本地专用数据，wechat_intake.py / market.py 维护）──
    wechat_model = _load_wechat(today)
    market_model = _load_market()
    # 原料行情（commodities.py 维护 data/原料行情记录.csv；缺失则 None）
    commodities_model = _load_commodities()
    # 消息↔SeaTable 核对（wxmatch.py 维护 data/核对结果.csv；缺失则 None）
    wxmatch_model = _load_wxmatch()
    # 风险预测（foresee.py 维护 data/foresee.json；缺失则 None）
    foresee_model = _load_foresee()
    # 业务闭环控制平面（order_to_cash.py 维护 data/business_loop/*.csv；缺失则 None）
    # ★ L2-0 接线：这条「微信来单 → … → 售后」的全链一直落在本地 CSV 里，
    #   驾驶舱只读 SeaTable，所以业主自己看不到它跑到哪了。这里把它接上。
    loop_model = _load_loop(today)
    # 数据来源健康度（全链降级 / Plan B 专项）：统一陈旧判据 + 最近一次运行的降级状态
    source_health = _load_source_health(today)
    # 风险预测 → 行动建议：已逾期/高风险置顶为高优，缺料必须立刻下单次之
    if foresee_model:
        fb = foresee_model.get("backward") or {}
        for a in (fb.get("act_now") or [])[:5]:
            txt = "风险雷达：%s %s 剩%d天，环节已过最晚开始日（%s）" % (
                a["plan"], a["product"][:14], a["days_left"], "、".join(a["do_now"][:2]))
            actions.insert(0, {"pri": "高" if a["days_left"] < 0 else "中", "cat": "risk",
                               "text": txt})
        for e in (foresee_model.get("shortage", {}).get("must_order") or [])[:3]:
            actions.append({"pri": "高" if e["verdict"] == "必须立刻下单" else "中", "cat": "risk",
                            "text": "风险雷达：%s 缺料 %d 项（共%d件，零库存 %d 项）%s" % (
                                e["product"][:16], e["gap_items"], e["total_gap"],
                                e["zero_stock_items"], e["verdict"])})
    if wechat_model and wechat_model["pending_count"]:
        cats = "/".join(sorted({e.get("分类", "") for e in wechat_model["pending"] if e.get("分类")}))
        actions.insert(0, {"pri": "高", "cat": "wechat",
                           "text": "微信情报待确认 %d 条（%s）：在「微信情报台」核对后写入业务表"
                                   % (wechat_model["pending_count"], cats or "未分类")})
    if market_model:
        for a in market_model["alerts"]:
            actions.insert(0 if a["type"] == "停产" else len(actions),
                           {"pri": "高" if a["type"] == "停产" else "中", "cat": "market",
                            "text": "物料行情：%s %s" % (a["model"], a["text"])})

    # ── 业务闭环控制平面 → 行动建议（L2-0b 状态超时告警）──────────────────
    # 放在最后一组：insert(0) 是「后插入者站最前」，全链逾期比行情告警更该先看到。
    #
    # ⚠️ 两条自我约束（否则会造出假指标 —— 这正是业主反复要求杜绝的）：
    #   ① 控制平面是**预演空壳**（无一越过立项回款）时，绝不把它的「停滞」当业务待办。
    #      那是没接入增量入口造成的，不是有人在拖单；混进「今天要处理」就是谎报。
    #   ② 数据**已停更**时，「停留 N 天」被停更期灌水（N = 距末次写入的天数），
    #      拿它当「这单卡了 N 天」是错的 → 只保留有绝对日期锚点的「逾期」。
    #   两条都不成立时才发业务告警；否则只留一条**提示级**说明（不计入角标、
    #   不进「今天要处理」，只在「行动建议」里可见）。
    if loop_model:
        if not loop_model["scenario"]:
            for c in loop_model["overdue"][:3]:
                actions.insert(0, {"pri": "高", "cat": "sales",
                                   "text": "全链逾期：%s「%s」卡在「%s」%d 天（应于 %s 推进）%s" % (
                                       c["customer"][:12], c["product"][:16], c["state_zh"],
                                       -c["days_left"], c["due_date"], c["next_action"][:30])})
            if not loop_model["stale"]:
                for c in loop_model["stuck"][:2]:
                    actions.append({"pri": "中", "cat": "sales",
                                    "text": "全链停滞：%s「%s」在「%s」停留 %d 天未推进，建议确认卡点" % (
                                        c["customer"][:12], c["product"][:16], c["state_zh"], c["dwell"])})
        else:
            _why = ("已停更 %d 天（末次 %s）" % (loop_model["stale_days"], loop_model["updated_at"])
                    if loop_model["stale"] else "数据为预演批次")
            actions.append({"pri": "提示", "cat": "sales",
                            "text": "全链案件（%d 单）未计入今日待办：控制平面%s，且无一越过「立项回款」。"
                                    "详见「项目 › 全链案件」与「分析 › 来源健康」。"
                                    % (loop_model["case_count"], _why)})

    # ── 数据来源健康度 → 行动建议（全链降级 / Plan B 专项）──────────────
    # 与上一段同一套「防假指标」双闸门：确有源过旧/降级才发业务告警，
    # 否则只发一条**提示级**（不计角标、不进「今天要处理」）。
    if source_health and source_health.get("worst") != "ok":
        _bad = [s for s in source_health["sources"] if s["level"] == "bad"]
        _warn = [s for s in source_health["sources"] if s["level"] == "warn"]
        if _bad:
            actions.insert(0, {"pri": "中", "cat": "risk",
                               "text": "数据来源过旧 %d 个：%s —— 看板相关结论可能基于旧数据，"
                                       "详见「分析 › 来源健康」" % (
                                           len(_bad),
                                           "、".join(s["name"] for s in _bad[:3]))})
        elif _warn:
            actions.append({"pri": "提示", "cat": "risk",
                            "text": "数据来源偏旧 %d 个：%s（详见「分析 › 来源健康」）" % (
                                len(_warn), "、".join(s["name"] for s in _warn[:3]))})
    # 核对引擎：高置信收款=钱的事必须红字置顶；中置信有匹配项目=漏登记，中优
    if wxmatch_model:
        if wxmatch_model["hi_pay_count"]:
            actions.insert(0, {"pri": "高", "cat": "wechat",
                               "text": "收款核对：%d 条高置信收款信号待确认（匹配到项目待收金额，"
                                       "确认后写入实收）" % wxmatch_model["hi_pay_count"]})
        ready = [r for r in wxmatch_model["act_ready"]
                 if "收款" not in (r.get("类型") or "")][:3]
        for r in ready:
            tgt = r.get("匹配项目") or r.get("信号内容", "")
            actions.append({"pri": "中", "cat": "wechat",
                            "text": "核对：%s %s" % ((tgt or "")[:34], (r.get("建议动作") or "")[:40])})
    # 原料行情波动 = 成本走向信号，不是停线事件，一律中优、且最多带 2 条免得刷屏
    if commodities_model:
        for a in commodities_model["alerts"][:2]:
            actions.append({"pri": "中", "cat": "market",
                            "text": "原料行情：%s %s" % (a["name"], a["text"])})

    if not actions:
        actions.append({"pri": "提示", "cat": "boss", "text": "暂无紧急事项，保持当前节奏即可 ✔"})

    # ── 补录缺失字段（供 HTML 内直接行内填写，存本地后复制发回推送云端）──
    # 真实库这些列为空：生产计划「计划开始/完成/实际完成/放行状态」、组装记录「组装良品率」。
    # 确定性可推的日期先预填（计划开始≈立项、计划完成≈合同交期、已交付计划实际完成=立项+花费天数），
    # 需用户拍板的「实际完成/放行状态/组装良品率」留空。
    backfill = {"生产计划": [], "组装记录": []}
    for p in plans:
        sd = p.get("立项日期", "")
        cd = p.get("合同交期", "")
        spend = _num(p.get("花费天数"))
        act = ""
        if p.get("状态") in STATUS_DONE and sd and spend > 0:
            d = _date(sd)
            if d:
                act = (d + timedelta(days=int(spend))).isoformat()
        backfill["生产计划"].append({
            "row_id": _rid(p),
            "name": p.get("生产产品", ""),
            "计划开始日期": sd if str(sd).strip() not in ("", "None") else "",
            "计划完成日期": cd if str(cd).strip() not in ("", "None") else "",
            "实际完成日期": act,
            "放行状态": "",
        })
    for r in asm_rows:
        backfill["组装记录"].append({
            "row_id": _rid(r),
            "name": r.get("组装编号", ""),
            "组装良品率": "",
        })

    # ── 项目全表 / 生产计划全表（「项目矩阵」板块用）──
    # 逐列落值供 HTML 端排序/搜索；超长 markdown 字段（产品需求/合同/生产详情）刻意不进 HTML。
    def _cells(row, cols):
        o = {}
        for c in cols:
            v = row.get(c)
            if isinstance(v, (list, tuple)):
                v = "、".join(str(x) for x in v if x not in (None, ""))
            if v is None:
                v = ""
            v = str(v).strip()
            if len(v) > 60:
                v = v[:60] + "…"
            o[c] = v
        return o

    def _links(v):
        if isinstance(v, (list, tuple)):
            return len([x for x in v if str(x).strip()])
        return 1 if str(v or "").strip() else 0

    # 「阶段」是 link 列，云端只给回 linked row 的 display_value（形如 673602），对人不具可读性，故不进表
    PROJ_COLS = ["项目编号", "项目", "状态", "合同总价", "签订日期",
                 "下单付", "收货付", "验收付", "实收", "待收", "合同交期",
                 "剩余（天）", "花费天数", "完货日期", "生产花销"]
    projects_full = []
    for p in projects:
        r = _cells(p, PROJ_COLS)
        for c in ("合同总价", "实收", "待收", "生产花销"):
            r[c] = round(_num(r[c]), 2)
        r["_计划"] = _links(p.get("生产计划"))
        r["_发货"] = _links(p.get("发货清单"))
        r["_维修"] = _links(p.get("维修记录"))
        projects_full.append(r)

    PLAN_COLS = ["生产计划编号", "生产产品", "数量", "状态", "阶段", "关联项目",
                 "立项日期", "合同交期", "花费天数", "生产总花销", "此次单片成本",
                 "批次号", "版本号"]
    plans_full = []
    for p in plans:
        r = _cells(p, PLAN_COLS)
        r["数量"] = _num(r["数量"])
        r["花费天数"] = _num(r["花费天数"])
        r["生产总花销"] = round(_num(r["生产总花销"]), 2)
        r["此次单片成本"] = round(_num(r["此次单片成本"]), 2)
        # 交期是不是「推算」的（表里没填）——供全表把「合同交期」列显示成 ≈ 值，与甘特图口径一致
        _sd, _cd = _date(p.get("立项日期")), _date(p.get("合同交期"))
        r["_est"] = bool(_sd and _cd is None)
        r["_due_est"] = (_sd + timedelta(days=int(est_days))).isoformat() if r["_est"] else ""
        r["__row_id__"] = p.get("__row_id__")
        plans_full.append(r)
    # 业主 2026-09-15：**「计划中」优先**（原为 SeaTable 行序 —— 已交付的老计划会把在产压在下面）
    plans_full.sort(key=lambda r: (status_rank(r.get("状态")),
                                   str(r.get("立项日期") or ""), str(r.get("生产计划编号") or "")))
    # 项目表同一口径（「暂放」并入「计划中」档，与 KPI 一致）
    projects_full.sort(key=lambda r: (status_rank(r.get("状态")),
                                      str(r.get("合同交期") or ""), str(r.get("项目编号") or "")))

    return {
        "snapshot": today.isoformat(),
        "isDemo": not bool(_load_sync_meta()),
        "synced_at": (_load_sync_meta() or {}).get("synced_at"),
        "base_name": (_load_sync_meta() or {}).get("base_name"),
        "partdb_at": (lambda p: p.get("generated_at") if p else None)(_load_partdb()),
        "kpi": {
            "projects": p_total, "active": p_active, "planned": p_planned, "done": p_done,
            "contract": round(contract, 2), "received": round(received, 2),
            "receivable": round(receivable, 2), "cost": round(total_cost, 2),
            "ledger_cost": round(ledger_cost, 2), "unit_cost": unit_cost,
            "exec_rate": round(received / contract * 100, 1) if contract else 0.0,
            "profit": round(total_profit, 2), "margin": round(margin, 1),
            "ontime_rate": round(ontime_rate, 1) if ontime_rate is not None else None, "purchase_overdue": len(overdue_list),
            "wip": len(wip),
            "res_load": (res_model or {}).get("avg_load"),
            "res_over": len((res_model or {}).get("over", [])),
            "res_conflict": len((res_model or {}).get("conflicts", [])),
            "labor_cost": (res_model or {}).get("labor_cost"),
            "partdb": bool(_load_partdb()),
            "bom_shortage": (lambda p: (p.get("bom") or {}).get("shortage", []) and len((p.get("bom") or {}).get("shortage", [])) or 0)(_load_partdb()) if _load_partdb() else 0,
        },
        "projects": proj_overview,
        "recv_check": recv_check,
        "wip": wip,
        "time": {
            "ontime_rate": round(ontime_rate, 1) if ontime_rate is not None else None, "ontime": ontime, "dated": dated, "total": total_plans,
            "avg_cycle": avg_cycle, "cycle_list": cycles, "stage_dist": stage_dist, "flow_dist": flow_dist,
        },
        "cost": {
            "contract": round(contract, 2), "cost": round(total_cost, 2),
            "ledger_cost": round(ledger_cost, 2), "unit_cost": unit_cost,
            "profit": round(total_profit, 2), "margin": round(margin, 1),
            "budget_margin": budget_margin,
            "category": category, "receivable_list": receivable_list, "cashflow": cashflow,
        },
        "quality": {
            "repair_rate": round(repair_rate, 2), "shipped": shipped, "repair_total": repair_total,
            "smt_yield": round(smt_yield, 1), "asm_yield": asm_yield,
            "repair_overdue": repair_overdue, "repair_avg": repair_avg, "repair_list": repair_list,
            # 良率**样本完整度**：前端据此标注「11/13 批已录 · 2 批待补录（投 4000 片）」。
            # 不暴露这个，看板上的 94.4% 就是个没有分母说明的数字。
            "smt_batches": len(smt_pairs), "smt_rows": len(smt_rows),
            "smt_missing_n": smt_missing_n, "smt_missing_qty": int(smt_missing_qty),
        },
        "supply": {
            "overdue_list": overdue_list, "supplier": supplier,
            "inventory_warn": inventory_warn, "process_count": len(processes),
        },
        "gantt": gantt,
        "gantt_pending": pend_count,
        # 无交期计划的**推算依据**（前端用它解释「这条交期怎么来的」，而非让数字凭空出现）
        "gantt_hist": hist,
        "gantt_est_days": est_days,
        # 项目矩阵板块：项目表全字段 / 生产计划全字段 / 流程思维导图
        "projects_full": projects_full,
        "plans_full": plans_full,
        "mindmaps": _load_mindmaps(),
        "next_actions": actions,
        "backfill": backfill,
        # 资源域（人/设备负载、冲突、人工成本）；未启用资源管理时为 None
        "resource": res_model,
        # PartDB 实时（partdb_sync.py 生成；缺失则 None，渲染时回退）
        "partdb": _load_partdb(),
        # 微信情报（wechat_intake.py 维护 data/微信事件.csv；缺失则 None）
        "wechat": wechat_model,
        # 物料行情（market.py 维护 监控清单+行情快照；缺失则 None）
        "market": market_model,
        # 原料行情（commodities.py 维护 上游原料 金/银/铜/锡/塑料；缺失则 None）
        "commodities": commodities_model,
        # 消息↔SeaTable 核对（wxmatch.py 维护 data/核对结果.csv；缺失则 None）
        "wxmatch": wxmatch_model,
        # 风险预测（foresee.py 维护 data/foresee.json；缺失则 None）
        "foresee": foresee_model,
        # 业务闭环控制平面（order_to_cash.py 维护 data/business_loop/*.csv；缺失则 None）
        "loop": loop_model,
        # 数据来源健康度（统一陈旧判据 + 最近一次运行的降级状态；见 SOURCE_SPEC）
        "source_health": source_health,
    }


# 内联 SVG 图标（构建时替换占位符，避免运行时修改 DOM 破坏工具栏）
ICONS = {
    "TITLE_ICON": '<svg class="svg-ic" width="24" height="24" viewBox="0 0 24 24"><rect x="3" y="3" width="7" height="9" rx="1.5"/><rect x="14" y="3" width="7" height="5" rx="1.5"/><rect x="14" y="12" width="7" height="9" rx="1.5"/><rect x="3" y="16" width="7" height="5" rx="1.5"/></svg>',
    "IC_GRID": '<svg class="svg-ic" width="20" height="20" viewBox="0 0 24 24"><rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/><rect x="3" y="14" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/></svg>',
    "IC_PROJ": '<svg class="svg-ic" width="20" height="20" viewBox="0 0 24 24"><path d="M3 7l9-4 9 4-9 4-9-4z"/><path d="M3 12l9 4 9-4"/><path d="M3 17l9 4 9-4"/></svg>',
    "IC_TIME": '<svg class="svg-ic" width="20" height="20" viewBox="0 0 24 24"><circle cx="12" cy="12" r="8.5"/><path d="M12 7.5V12l3 2"/></svg>',
    "IC_COST": '<svg class="svg-ic" width="20" height="20" viewBox="0 0 24 24"><path d="M12 3v18"/><path d="M16.5 7.5c0-1.7-2-3-4.5-3s-4.5 1.3-4.5 3 2 2.7 4.5 3 4.5 1.3 4.5 3-2 3-4.5 3-4.5-1.3-4.5-3"/></svg>',
    "IC_QUAL": '<svg class="svg-ic" width="20" height="20" viewBox="0 0 24 24"><path d="M12 3l2.5 5 5.5.8-4 3.9.9 5.5L12 21l-4.9 2.2.9-5.5-4-3.9 5.5-.8z"/></svg>',
    "IC_SUP": '<svg class="svg-ic" width="20" height="20" viewBox="0 0 24 24"><path d="M3 8l9-4 9 4-9 4-9-4z"/><path d="M3 8v8l9 4 9-4V8"/><path d="M12 12v8"/></svg>',
    "IC_DOWNLOAD": '<svg class="svg-ic" width="16" height="16" viewBox="0 0 24 24"><path d="M12 3v12"/><path d="M7 11l5 5 5-5"/><path d="M4 20h16"/></svg>',
    "IC_UPLOAD": '<svg class="svg-ic" width="16" height="16" viewBox="0 0 24 24"><path d="M12 21V9"/><path d="M7 13l5-5 5 5"/><path d="M4 4h16"/></svg>',
    "IC_REFRESH": '<svg class="svg-ic" width="16" height="16" viewBox="0 0 24 24"><path d="M20 11a8 8 0 1 0-2.3 5.7"/><path d="M20 5v6h-6"/></svg>',
    "IC_NEXT": '<svg class="svg-ic" width="20" height="20" viewBox="0 0 24 24"><path d="M5 7l2 2 4-4"/><path d="M5 13l2 2 4-4"/><path d="M14 9h5"/><path d="M14 15h5"/></svg>',
    "IC_EDIT": '<svg class="svg-ic" width="20" height="20" viewBox="0 0 24 24"><path d="M4 20h4L18.5 9.5a2.1 2.1 0 0 0-3-3L5 17v3z"/><path d="M13.5 6.5l3 3"/></svg>',
    "IC_COPY": '<svg class="svg-ic" width="16" height="16" viewBox="0 0 24 24"><rect x="9" y="9" width="11" height="11" rx="2"/><path d="M5 15V5a2 2 0 0 1 2-2h10"/></svg>',
    "IC_GANT": '<svg class="svg-ic" width="20" height="20" viewBox="0 0 24 24"><path d="M4 6h10"/><path d="M4 12h14"/><path d="M4 18h7"/></svg>',
    "IC_MIND": '<svg class="svg-ic" width="20" height="20" viewBox="0 0 24 24"><circle cx="5" cy="12" r="2.2"/><circle cx="19" cy="6" r="2.2"/><circle cx="19" cy="12" r="2.2"/><circle cx="19" cy="18" r="2.2"/><path d="M7.2 12h3.3c1.5 0 2.5-1 2.5-2.5S14 7 15.5 7h1.3"/><path d="M7.2 12h5"/><path d="M10.5 12c1.5 0 2.5 1 2.5 2.5S13 17 14.5 17h2.3"/></svg>',
    "IC_ANALYZE": '<svg class="svg-ic" width="16" height="16" viewBox="0 0 24 24"><circle cx="11" cy="11" r="6.5"/><path d="M16 16l4 4"/></svg>',
    "IC_SYNC": '<svg class="svg-ic" width="16" height="16" viewBox="0 0 24 24"><path d="M21 12a9 9 0 0 1-9 9 9 9 0 0 1-8-5"/><path d="M3 12a9 9 0 0 1 9-9 9 9 0 0 1 8 5"/><path d="M21 4v5h-5"/><path d="M3 20v-5h5"/></svg>',
    "IC_MKT": '<svg class="svg-ic" width="20" height="20" viewBox="0 0 24 24"><path d="M3 17l5-6 4 3 6-8"/><path d="M14 6h4v4"/><path d="M3 21h18"/></svg>',
    "IC_CHAT": '<svg class="svg-ic" width="20" height="20" viewBox="0 0 24 24"><path d="M4 5h16a1 1 0 0 1 1 1v10a1 1 0 0 1-1 1H9l-5 4V6a1 1 0 0 1 1-1z"/><path d="M8 10h8"/><path d="M8 13h5"/></svg>',
    "IC_BOX": '<svg class="svg-ic" width="16" height="16" viewBox="0 0 24 24"><path d="M3 7l9-4 9 4-9 4-9-4z"/><path d="M3 7v10l9 4 9-4V7"/><path d="M12 11v10"/></svg>',
    "IC_SHARE": '<svg class="svg-ic" width="16" height="16" viewBox="0 0 24 24"><circle cx="18" cy="5" r="3"/><circle cx="6" cy="12" r="3"/><circle cx="18" cy="19" r="3"/><path d="M8.6 13.5l7.8 4"/><path d="M15.4 6.5l-7.8 4"/></svg>',
    "IC_KEY": '<svg class="svg-ic" width="16" height="16" viewBox="0 0 24 24"><path d="M14 7a4 4 0 1 0-3.6 5.9L7 17v3H4v-3l5.4-5.4A4 4 0 0 0 14 7zm-1.6 2.4a2 2 0 1 1-2.8 2.8 2 2 0 0 1 2.8-2.8z"/></svg>',
    "IC_ADD": '<svg class="svg-ic" width="20" height="20" viewBox="0 0 24 24"><path d="M12 5v14"/><path d="M5 12h14"/></svg>',
    "IC_CHECK": '<svg class="svg-ic" width="20" height="20" viewBox="0 0 24 24"><rect x="3" y="4" width="18" height="16" rx="2"/><path d="M8 12l3 3 5-6"/></svg>',
    "IC_RADAR": '<svg class="svg-ic" width="20" height="20" viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="5"/><circle cx="12" cy="12" r="1.5"/><path d="M12 12L20 7"/></svg>',
    "IC_THEME": '<svg class="svg-ic" width="16" height="16" viewBox="0 0 24 24"><path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z"/></svg>',
    "IC_USER": '<svg class="svg-ic" width="20" height="20" viewBox="0 0 24 24"><circle cx="9" cy="8" r="3.5"/><path d="M3 20v-1.5C3 16 5.7 14.5 9 14.5s6 1.5 6 4V20"/><path d="M17 8.5h4"/><path d="M17 12h4"/><path d="M17 15.5h4"/></svg>',
    # v6 外壳（2026-10-02 UX 重构）新增
    "IC_DOTS": '<svg class="svg-ic" width="18" height="18" viewBox="0 0 24 24"><circle cx="12" cy="5" r="1.6"/><circle cx="12" cy="12" r="1.6"/><circle cx="12" cy="19" r="1.6"/></svg>',
    "IC_CHEV": '<svg class="svg-ic" width="12" height="12" viewBox="0 0 24 24"><path d="M6 9l6 6 6-6"/></svg>',
    "IC_MENU": '<svg class="svg-ic" width="18" height="18" viewBox="0 0 24 24"><path d="M4 7h16"/><path d="M4 12h16"/><path d="M4 17h16"/></svg>',
    "IC_INSIGHT": '<svg class="svg-ic" width="20" height="20" viewBox="0 0 24 24"><path d="M4 19V9"/><path d="M10 19V5"/><path d="M16 19v-7"/><path d="M22 19H2"/></svg>',
}

# ===== 访问口令（客户端校验，写进 HTML 源码；已 base64 混淆，开发者选项里不再一眼看到明文）=====
# 定位：防「文件误发到外部时被随手打开」的一道门，不是加密。
#      口令必然要写进 HTML 才能离线校验，所以拿到文件+懂开发者工具的人总能绕过。
#
# 🔑 口令从 config.yaml 读取（该文件已 gitignore，不会进版本库）：
#      cockpit:
#        admin_password: "xxxx"
#        role_passwords: {boss: "...", warehouse: "...", ...}
#   首次运行若 config.yaml 里没有口令，会自动生成一套随机口令并写回 config.yaml，
#   同时在终端打印出来——请自行分发给对应同事。
#
# ⚠️ 绝不要把口令写死在本文件里：本仓库是公开的，写死等于公开发布口令。
def _load_pw():
    """从 config.yaml 读口令；没有则随机生成并写回。返回 (admin, {role: pw})。"""
    import random as _rnd
    import string as _str
    from adapters.factory import load_config, _SKILL_DIR

    _ROLES = ("boss", "warehouse", "purchase", "production", "sales")
    cfg = load_config() or {}
    cp = cfg.get("cockpit") or {}
    admin = (cp.get("admin_password") or "").strip()
    roles = dict(cp.get("role_passwords") or {})

    # 强口令：大小写字母+数字，10 位，避免 "CK8888" 这种可猜格式
    alphabet = _str.ascii_letters + _str.digits
    def _gen():
        return "".join(_rnd.SystemRandom().choice(alphabet) for _ in range(10))

    missing = (not admin) or any(not (roles.get(r) or "").strip() for r in _ROLES)
    if not missing:
        return admin, {r: roles[r] for r in _ROLES}

    admin = admin or _gen()
    for r in _ROLES:
        if not (roles.get(r) or "").strip():
            roles[r] = _gen()

    # 写回 config.yaml（不存在则以 example 为基础创建）
    cfg_path = os.path.join(_SKILL_DIR, "config.yaml")
    try:
        block = ["", "# 驾驶舱访问口令（自动生成，可自行修改；本文件已 gitignore）",
                 "cockpit:", '  admin_password: "%s"' % admin, "  role_passwords:"]
        block += ['    %s: "%s"' % (r, roles[r]) for r in _ROLES]
        text = ""
        if os.path.exists(cfg_path):
            with open(cfg_path, "r", encoding="utf-8") as f:
                text = f.read()
        if "cockpit:" in text:
            # 已有 cockpit 段但字段不全 —— 不自动改写，避免破坏用户手写内容
            print("[warn] config.yaml 的 cockpit 段口令不完整，本次使用临时随机口令；"
                  "请手动补全后重跑。", file=sys.stderr)
            return admin, {r: roles[r] for r in _ROLES}
        with open(cfg_path, "a" if text else "w", encoding="utf-8") as f:
            if not text:
                f.write("backend: local\n")
            f.write("\n".join(block) + "\n")
        print("[口令] 已生成新的驾驶舱口令并写入 config.yaml：", file=sys.stderr)
        print("       管理员 %s" % admin, file=sys.stderr)
        for r in _ROLES:
            print("       %-10s %s" % (r, roles[r]), file=sys.stderr)
        print("       请分发给对应同事；改口令请编辑 config.yaml 后重跑本脚本。", file=sys.stderr)
    except Exception as e:
        print("[warn] 口令写回 config.yaml 失败（%s），本次使用临时随机口令。" % e, file=sys.stderr)
    return admin, {r: roles[r] for r in _ROLES}


ADMIN_PASSWORD, ROLE_PASSWORDS = _load_pw()

# 构建期：把口令表编码为 base64 再写入源码（开发者选项里不再是一眼明文）
import base64 as _b64
_PW_MAP = {"admin": ADMIN_PASSWORD, **ROLE_PASSWORDS}
PW_BLOB = _b64.b64encode(json.dumps(_PW_MAP, ensure_ascii=False).encode("utf-8")).decode("ascii")


HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, viewport-fit=cover">
<title>生产·项目管理驾驶舱</title>
<script>
/* 防闪烁：在 body 渲染前先应用已保存的手动主题（暗/亮），避免亮→暗闪一下 */
(function(){try{var t=localStorage.getItem('cockpit_theme');var r=document.documentElement;
if(t==='dark'){r.classList.add('theme-dark');}else if(t==='light'){r.classList.add('theme-light');}}catch(e){}})();
</script>
<style>
/* ════════════════════════════════════════════════════════════════
   生产·项目管理驾驶舱 — 重构样式 v2
   设计语言参考 Linear / Vercel / Stripe Dashboard：
   · 干净的卡片式顶栏（去渐变横幅），细边框 + 轻阴影
   · 无斑马纹表格，悬停高亮，小号大写表头
   · KPI：小色点 + 大数字 tabular-nums，去彩色顶边 hack
   · 状态药丸用 color-mix 半透明色调（亮暗自动适配，三处暗色覆盖收敛为 0）
   · 全部组件走 CSS 变量，暗色只重定义变量
   ════════════════════════════════════════════════════════════════ */

:root{
  /* 基础色板 */
  --bg:#f6f7f9; --card:#ffffff; --ink:#111827; --sub:#6b7280; --line:#e5e7eb;
  --primary:#4f46e5; --primary-hover:#4338ca; --primary-subtle:#eef2ff;
  --green:#059669; --red:#dc2626; --amber:#d97706; --purple:#7c3aed;
  --blue:#0284c7; --teal:#0d9488;
  --shadow:0 1px 2px rgba(16,24,40,.04);
  --shadow-md:0 2px 4px rgba(16,24,40,.04),0 12px 32px rgba(16,24,40,.07);
  /* 子背景（亮色） */
  --subbg:#f8fafc; --subbg2:#f1f5f9; --subbg3:#f3f4f6; --selbg:#eef2ff;
  --bd:#d1d5db; --txt:#111827; --inputbg:#ffffff;
  /* KPI / 标签强调色 */
  --ac-blue:#0284c7; --ac-green:#059669; --ac-red:#dc2626; --ac-amber:#d97706; --ac-purple:#7c3aed;
  /* 间距 / 字号阶梯 */
  --sp1:6px; --sp2:10px; --sp3:14px; --sp4:18px; --sp5:24px; --sp6:32px;
  --fs-xs:11.5px; --fs-sm:12.5px; --fs-md:14px; --fs-lg:16px; --fs-xl:18px; --fs-2xl:22px; --fs-3xl:28px;
  /* 按钮基础 */
  --btn-bg:#ffffff; --btn-ink:#374151; --btn-line:#d1d5db;
  --btn-bg-hover:#f9fafb;
  /* 焦点环 */
  --focus-ring:0 0 0 3px rgba(79,70,229,.18);
}

/* ══ 暗色主题：只重定义变量，不再有逐组件覆盖 ══
   三态：auto（跟随系统）/ theme-dark（强制暗）/ theme-light（强制亮） */
@media (prefers-color-scheme: dark){
  :root:not(.theme-light){
    --bg:#0b0e14; --card:#151a23; --ink:#f3f4f6; --sub:#8b95a5; --line:#252c39;
    --primary:#818cf8; --primary-hover:#a5b4fc; --primary-subtle:rgba(129,140,248,.12);
    --green:#34d399; --red:#f87171; --amber:#fbbf24; --purple:#a78bfa;
    --blue:#38bdf8; --teal:#2dd4bf;
    --shadow:0 1px 2px rgba(0,0,0,.4);
    --shadow-md:0 2px 4px rgba(0,0,0,.4),0 12px 32px rgba(0,0,0,.5);
    --subbg:#1a212c; --subbg2:#222a38; --subbg3:#1e2530; --selbg:rgba(129,140,248,.14);
    --bd:#33405280; --txt:#f3f4f6; --inputbg:#0e131b;
    --ac-blue:#38bdf8; --ac-green:#34d399; --ac-red:#f87171; --ac-amber:#fbbf24; --ac-purple:#a78bfa;
    --btn-bg:#1c2430; --btn-ink:#c7cfd9; --btn-line:#33405280;
    --btn-bg-hover:#242d3b;
    --focus-ring:0 0 0 3px rgba(129,140,248,.25);
  }
}
:root.theme-dark{
  --bg:#0b0e14; --card:#151a23; --ink:#f3f4f6; --sub:#8b95a5; --line:#252c39;
  --primary:#818cf8; --primary-hover:#a5b4fc; --primary-subtle:rgba(129,140,248,.12);
  --green:#34d399; --red:#f87171; --amber:#fbbf24; --purple:#a78bfa;
  --blue:#38bdf8; --teal:#2dd4bf;
  --shadow:0 1px 2px rgba(0,0,0,.4);
  --shadow-md:0 2px 4px rgba(0,0,0,.4),0 12px 32px rgba(0,0,0,.5);
  --subbg:#1a212c; --subbg2:#222a38; --subbg3:#1e2530; --selbg:rgba(129,140,248,.14);
  --bd:#33405280; --txt:#f3f4f6; --inputbg:#0e131b;
  --ac-blue:#38bdf8; --ac-green:#34d399; --ac-red:#f87171; --ac-amber:#fbbf24; --ac-purple:#a78bfa;
  --btn-bg:#1c2430; --btn-ink:#c7cfd9; --btn-line:#33405280;
  --btn-bg-hover:#242d3b;
  --focus-ring:0 0 0 3px rgba(129,140,248,.25);
}

*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
body{margin:0;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"PingFang SC","Microsoft YaHei",sans-serif;
  background:var(--bg);color:var(--ink);font-size:14px;line-height:1.6;-webkit-text-size-adjust:100%;
  font-weight:400;-webkit-font-smoothing:antialiased;text-rendering:optimizeLegibility}
.wrap{max-width:1640px;margin:0 auto;padding:24px 24px calc(40px + env(safe-area-inset-bottom))}

/* ══ 顶栏：干净卡片式，去渐变 ══ */
header.top{background:var(--card);color:var(--ink);border:1px solid var(--line);border-radius:14px;
  padding:18px 22px;box-shadow:var(--shadow-md);position:relative}
header.top h1{margin:0;font-size:19px;font-weight:700;letter-spacing:-.01em;display:flex;align-items:center;gap:10px;color:var(--ink)}
header.top h1 svg{opacity:.85}
header.top .meta{margin-top:5px;font-size:12.5px;color:var(--sub)}
.toolbar{display:flex;gap:8px;flex-wrap:wrap;margin-top:14px;align-items:center}
.banner{margin-top:12px;background:var(--subbg);border:1px solid var(--line);
  border-radius:10px;padding:9px 12px;font-size:12.5px;color:var(--sub)}
.demo-flag{position:absolute;top:16px;right:18px;background:color-mix(in srgb,var(--amber) 10%,transparent);color:var(--amber);font-size:11px;
  padding:3px 10px;border-radius:999px;font-weight:600;border:1px solid color-mix(in srgb,var(--amber) 25%,transparent)}
.real-flag{position:absolute;top:16px;right:18px;background:color-mix(in srgb,var(--green) 12%,transparent);color:var(--green);font-size:11px;
  padding:3px 10px;border-radius:999px;font-weight:600;border:1px solid color-mix(in srgb,var(--green) 25%,transparent)}

/* ══ 按钮体系（重点重构）══
   .btn          顶栏白底描边按钮（原蓝底横幅上的玻璃按钮 → 现在卡片上的次级按钮）
   .btn-sync     同步按钮（原白色叠加 → 现在只是 subtle 强调）
   .btn-primary  主操作（实底 indigo）
   .btn-ghost    次级操作（描边）
   .bf-btn       补录面板按钮（copy = 主操作 / ghost = 次级）
   .lock-btn     口令门主按钮
   统一规格：36px 高、10px 圆角、500 字重、SVG 图标 16px、hover 微亮、focus 焦点环 */
.btn,.bf-btn,.btn-primary,.btn-ghost,.lock-btn,.pw-cp,.role-share,.role-key,.pg button,.today-item .go,.more-tg{
  appearance:none;font-family:inherit;font-size:13px;font-weight:500;cursor:pointer;
  display:inline-flex;align-items:center;justify-content:center;gap:6px;
  min-height:36px;padding:7px 14px;border-radius:10px;line-height:1.2;
  transition:background .12s,border-color .12s,color .12s,box-shadow .12s;
  text-decoration:none;white-space:nowrap;user-select:none}
.btn svg,.bf-btn svg,.btn-primary svg,.lock-btn svg{width:15px;height:15px;flex:0 0 15px}
.btn{border:1px solid var(--btn-line);background:var(--btn-bg);color:var(--btn-ink)}
.btn:hover{background:var(--btn-bg-hover);border-color:color-mix(in srgb,var(--primary) 45%,var(--btn-line))}
.btn:active{transform:translateY(.5px)}
.btn:focus-visible,.bf-btn:focus-visible,.btn-primary:focus-visible,.btn-ghost:focus-visible,
.lock-btn:focus-visible,.pg button:focus-visible,.today-item .go:focus-visible,.pw-cp:focus-visible{
  outline:none;box-shadow:var(--focus-ring)}
.btn-sync{background:var(--primary-subtle);border-color:color-mix(in srgb,var(--primary) 30%,transparent);color:var(--primary)}
.btn-sync:hover{background:color-mix(in srgb,var(--primary) 16%,transparent)}
.btn-primary{border:1px solid transparent;background:var(--primary);color:#fff;font-weight:600}
.btn-primary:hover{background:var(--primary-hover)}
.btn-primary:active{transform:translateY(.5px)}
.btn-ghost{border:1px solid var(--btn-line);background:var(--btn-bg);color:var(--btn-ink)}
.btn-ghost:hover{background:var(--btn-bg-hover)}
.bf-btn{border:1px solid transparent}
.bf-btn.copy{background:var(--primary);color:#fff;font-weight:600}
.bf-btn.copy:hover{background:var(--primary-hover)}
.bf-btn.ghost{border-color:var(--btn-line);background:var(--btn-bg);color:var(--btn-ink)}
.bf-btn.ghost:hover{background:var(--btn-bg-hover)}
.lock-btn{width:100%;margin-top:14px;padding:12px;border:none;border-radius:11px;
  background:var(--primary);color:#fff;font-size:15px;font-weight:600}
.lock-btn:hover{background:var(--primary-hover)}
.lock-btn:active{background:var(--primary-hover);transform:translateY(.5px)}
.pw-cp{border:1px solid var(--btn-line);background:var(--btn-bg);color:var(--btn-ink);
  font-size:12px;padding:6px 12px;min-height:32px}
.pw-cp:hover{background:var(--btn-bg-hover)}
.role-share{border:1px solid transparent;background:var(--primary);color:#fff;font-weight:600}
.role-share:hover{background:var(--primary-hover)}
.role-key{border:1px solid var(--btn-line);background:var(--btn-bg);color:var(--btn-ink)}
.role-key:hover{background:var(--btn-bg-hover)}
.pg button{border:1px solid var(--btn-line);background:var(--btn-bg);color:var(--btn-ink);padding:6px 14px}
.pg button:hover:not(:disabled){background:var(--btn-bg-hover);border-color:color-mix(in srgb,var(--primary) 45%,var(--btn-line))}
.today-item .go{border:1px solid var(--btn-line);background:var(--btn-bg);color:var(--btn-ink);
  font-size:12.5px;font-weight:600;padding:8px 14px;min-height:36px}
.today-item .go:hover{background:var(--primary);border-color:var(--primary);color:#fff}
.more-tg{width:100%;justify-content:center;background:var(--card);border:1px dashed var(--bd);
  color:var(--sub);font-weight:600;padding:13px 18px;min-height:48px}
.more-tg:hover{background:var(--subbg);color:var(--ink);border-color:var(--primary)}
.more-tg .arrow{transition:transform .2s;font-size:12px}
.more-tg.open .arrow{transform:rotate(180deg)}
.lock-eye{position:absolute;right:8px;top:50%;transform:translateY(-50%);border:none;background:transparent;
  color:var(--sub);cursor:pointer;padding:6px;border-radius:8px;display:flex;align-items:center;justify-content:center;line-height:0}
.lock-eye:hover{color:var(--primary);background:var(--subbg)}
.lock-eye svg{display:block}

/* ══ 表单体系（重点重构）══
   统一规格：8px 圆角、1px 实边框、focus 变主色 + 焦点环、::placeholder 弱化、16px 字号（iOS 不缩放）
   覆盖：.lock-input / .bf-table input/select / .pw-note textarea */
.lock-input,.bf-table input,.bf-table select,.pw-note textarea.pw-out{
  font-family:inherit;font-size:15px;color:var(--ink);
  border:1px solid var(--bd);border-radius:10px;background:var(--inputbg);
  transition:border-color .12s,box-shadow .12s,background .12s}
.lock-input{width:100%;box-sizing:border-box;padding:12px 14px;font-size:16px;outline:none;
  -webkit-text-security:disc;text-security:disc}
.lock-input.pw-plain{-webkit-text-security:none;text-security:none}
.lock-input:focus,.bf-table input:focus,.bf-table select:focus,.pw-note textarea.pw-out:focus{
  outline:none;border-color:var(--primary);box-shadow:var(--focus-ring)}
.lock-input::placeholder,.bf-table input::placeholder,.pw-note textarea.pw-out::placeholder{
  color:color-mix(in srgb,var(--sub) 75%,transparent)}
.bf-table input,.bf-table select{width:100%;min-width:120px;padding:7px 10px;
  border-radius:8px;font-size:13px}
@media(max-width:680px){
  .bf-table input,.bf-table select{min-width:104px;font-size:15px}
  .lock-input{font-size:16px}
}
.bf-table select{appearance:none;-webkit-appearance:none;padding-right:26px;cursor:pointer;
  background-image:url("data:image/svg+xml;charset=utf-8,%3Csvg xmlns='http://www.w3.org/2000/svg' width='10' height='6' viewBox='0 0 10 6'%3E%3Cpath d='M1 1l4 4 4-4' fill='none' stroke='%236b7280' stroke-width='1.5' stroke-linecap='round' stroke-linejoin='round'/%3E%3C/svg%3E");
  background-repeat:no-repeat;background-position:right 9px center}
.pw-note textarea.pw-out{width:100%;margin-top:8px;height:92px;font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
  font-size:12px;padding:8px 10px;resize:vertical;line-height:1.55}

/* ══ 文字基础 ══ */
.sec-title{position:relative;display:flex;align-items:center;gap:9px;font-size:var(--fs-lg);font-weight:700;
  margin:0 0 16px 12px;letter-spacing:-.01em;color:var(--ink)}
.sec-title::before{content:"";position:absolute;left:-12px;top:3px;bottom:3px;width:3px;border-radius:3px;background:var(--primary)}
.sec-title svg{width:18px;height:18px;flex:0 0 18px;color:var(--primary)}
section{margin-top:30px}
.note{font-size:12px;color:var(--sub);margin-top:10px;line-height:1.65}
.empty{color:var(--sub);font-size:13px;padding:10px 0;text-align:center}
.sub{color:var(--sub);font-size:11px;font-weight:500;margin-left:2px}
.pos{color:var(--green)} .neg{color:var(--red)}
.num{font-variant-numeric:tabular-nums;font-feature-settings:"tnum"}
code{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:.92em;
  background:var(--subbg2);border:1px solid var(--line);border-radius:5px;padding:1px 5px;color:var(--ink)}
footer{margin-top:28px;text-align:center;font-size:12px;color:var(--sub)}

/* ══ 布局网格 ══ */
.grid{display:grid;gap:16px}
.g2{grid-template-columns:1fr 1fr}
.g3{grid-template-columns:repeat(3,1fr)}
.g4{grid-template-columns:repeat(4,1fr)}
@media(max-width:880px){.g2,.g3,.g4{grid-template-columns:1fr}}
@media(max-width:680px){.pd-stats{grid-template-columns:repeat(2,1fr)}}

/* ══ 卡片 ══ */
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:18px;box-shadow:var(--shadow)}
.card h3{margin:0 0 14px;font-size:13.5px;color:var(--ink);font-weight:600;padding-bottom:10px;
  border-bottom:1px solid var(--line);display:flex;align-items:center;gap:7px;letter-spacing:.01em}
.card h3 svg{width:15px;height:15px;color:var(--sub)}

/* ══ KPI 卡（去 inset 顶边 hack）══ */
.kpi{--ac:var(--primary);display:flex;flex-direction:column;gap:7px;padding:16px;
  background:var(--card);border:1px solid var(--line);border-radius:12px;box-shadow:var(--shadow);
  transition:transform .15s,box-shadow .15s,border-color .15s}
.kpi:hover{transform:translateY(-2px);box-shadow:var(--shadow-md);border-color:color-mix(in srgb,var(--ac) 35%,var(--line))}
.kpi .top{display:flex;align-items:center;gap:7px}
.kpi .ac-dot{width:7px;height:7px;border-radius:50%;background:var(--ac);flex:0 0 7px}
.kpi .lbl{font-size:var(--fs-sm);color:var(--sub);letter-spacing:.02em;font-weight:500}
.kpi .v{font-size:26px;font-weight:700;letter-spacing:-.02em;line-height:1.15;color:var(--ink);
  font-variant-numeric:tabular-nums;font-feature-settings:"tnum"}
.kpi .v.neg{color:var(--red)}
.kpi .sub{font-size:var(--fs-xs);color:var(--sub);line-height:1.5;margin-top:1px}
.kpi.ac-blue{--ac:var(--ac-blue)} .kpi.ac-green{--ac:var(--ac-green)} .kpi.ac-red{--ac:var(--ac-red)}
.kpi.ac-amber{--ac:var(--ac-amber)} .kpi.ac-purple{--ac:var(--ac-purple)}

/* ══ 表格：无斑马纹 + 悬停高亮 + 小表头 ══ */
table{width:100%;border-collapse:collapse;font-size:13px}
th,td{text-align:left;padding:10px 12px;border-bottom:1px solid var(--line)}
th{color:var(--sub);font-weight:600;font-size:11px;letter-spacing:.05em;text-transform:uppercase;
  background:transparent;border-bottom:1.5px solid var(--bd)}
tbody tr:last-child td{border-bottom:0}
tbody tr{transition:background .1s}
tbody tr:hover td{background:var(--subbg)}
td{color:var(--txt)}
.bf-table th{position:sticky;top:0;background:var(--card);z-index:1}
.bf-table{width:100%;border-collapse:collapse;font-size:13px;min-width:720px}
.bf-table th,.bf-table td{padding:8px 12px;border-bottom:1px solid var(--line);text-align:left;vertical-align:middle}
.bf-scroll{overflow-x:auto;border:1px solid var(--line);border-radius:10px}
.bf-table input,.bf-table select{border-radius:6px}

/* ══ 状态药丸：color-mix 半透明色调，亮暗自动适配 ══ */
.pill{display:inline-block;padding:2.5px 10px;border-radius:6px;font-size:11.5px;font-weight:600;
  white-space:nowrap;border:1px solid transparent;line-height:1.5}
.st-进行中{background:color-mix(in srgb,var(--blue) 12%,transparent);color:var(--blue);
  border-color:color-mix(in srgb,var(--blue) 25%,transparent)}
.st-计划中{background:color-mix(in srgb,var(--amber) 12%,transparent);color:var(--amber);
  border-color:color-mix(in srgb,var(--amber) 25%,transparent)}
.st-已完成{background:color-mix(in srgb,var(--green) 12%,transparent);color:var(--green);
  border-color:color-mix(in srgb,var(--green) 25%,transparent)}
.tag-red{background:color-mix(in srgb,var(--red) 10%,transparent);color:var(--red);
  border-color:color-mix(in srgb,var(--red) 22%,transparent)}
.tag-green{background:color-mix(in srgb,var(--green) 10%,transparent);color:var(--green);
  border-color:color-mix(in srgb,var(--green) 22%,transparent)}
.tag-amber{background:color-mix(in srgb,var(--amber) 12%,transparent);color:var(--amber);
  border-color:color-mix(in srgb,var(--amber) 25%,transparent)}
.pri-高{background:color-mix(in srgb,var(--red) 12%,transparent);color:var(--red);
  border-color:color-mix(in srgb,var(--red) 25%,transparent)}
.pri-中{background:color-mix(in srgb,var(--amber) 12%,transparent);color:var(--amber);
  border-color:color-mix(in srgb,var(--amber) 25%,transparent)}
.pri-提示{background:color-mix(in srgb,var(--blue) 10%,transparent);color:var(--blue);
  border-color:color-mix(in srgb,var(--blue) 22%,transparent)}

/* ══ 快速导航（吸顶）══ */
.sec-nav{position:sticky;top:0;z-index:30;display:flex;gap:8px;overflow-x:auto;padding:9px 2px;
  margin:0 0 16px;background:color-mix(in srgb,var(--bg) 92%,transparent);
  backdrop-filter:blur(8px);-webkit-backdrop-filter:blur(8px);
  border-bottom:1px solid var(--line);scrollbar-width:thin}
.sec-nav::-webkit-scrollbar{height:5px}
.sec-nav::-webkit-scrollbar-thumb{background:var(--bd);border-radius:3px}
.sec-nav-item{flex:0 0 auto;display:inline-flex;align-items:center;gap:5px;padding:6px 13px;
  border:1px solid transparent;background:transparent;border-radius:8px;font-size:13px;color:var(--sub);
  cursor:pointer;white-space:nowrap;transition:background .12s,color .12s}
.sec-nav-item:hover{background:var(--subbg);color:var(--ink)}
.sec-nav-item.active{border-color:color-mix(in srgb,var(--primary) 30%,transparent);color:var(--primary);
  background:var(--primary-subtle);font-weight:600}
.sec-nav-item.in-more{opacity:.65;font-size:12px}
.sec-nav-item svg{width:14px;height:14px;flex:0 0 14px}

/* ══ 横滑容器 / 图例 / 分页 ══ */
.hscroll{display:flex;gap:14px;overflow-x:auto;scroll-snap-type:x mandatory;padding:4px 2px 12px;scrollbar-width:thin}
.hscroll::-webkit-scrollbar{height:6px}
.hscroll::-webkit-scrollbar-thumb{background:var(--bd);border-radius:3px}
.hscroll>.card,.hscroll>.kpi{flex:0 0 auto;min-width:176px;max-width:250px;scroll-snap-align:start}
.pg{display:flex;align-items:center;justify-content:flex-end;gap:10px;margin-top:10px;font-size:13px;color:var(--sub)}
.pg button:disabled{opacity:.4;cursor:default}
.pg-info{font-variant-numeric:tabular-nums}
.donut-wrap{display:flex;align-items:center;gap:16px}
.legend{display:flex;flex-direction:column;gap:8px;font-size:13px;flex:1;min-width:0}
.legend .row{display:flex;align-items:center;gap:8px}
.legend .dot{width:9px;height:9px;border-radius:3px;flex:0 0 9px}
.legend .nm{flex:1;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.legend .vl{font-weight:600;font-variant-numeric:tabular-nums}

/* ══ 条形图 ══ */
.bar-row{display:flex;align-items:center;gap:10px;margin:10px 0;font-size:13px}
.bar-row .nm{width:92px;flex:0 0 92px;color:var(--sub);text-align:right}
.bar-track{flex:1;background:var(--subbg2);border-radius:5px;height:18px;overflow:hidden}
.bar-fill{height:100%;border-radius:5px}
.bar-row .vl{width:56px;flex:0 0 56px;font-weight:600;text-align:right;font-variant-numeric:tabular-nums}

/* ══ 现金流格子 ══ */
.cf{display:grid;grid-template-columns:repeat(4,1fr);gap:10px}
@media(max-width:680px){.cf{grid-template-columns:repeat(2,1fr)}}
.cf .cell{background:var(--subbg);border:1px solid var(--line);border-radius:10px;padding:13px;text-align:center;
  transition:transform .15s,box-shadow .15s,border-color .15s}
.cf .cell:hover{transform:translateY(-2px);box-shadow:var(--shadow);border-color:var(--bd)}
.cf .bk{font-size:11.5px;color:var(--sub);font-weight:500}
.cf .bi{font-size:15px;font-weight:700;color:var(--green);font-variant-numeric:tabular-nums}
.cf .bo{font-size:13px;color:var(--red);font-variant-numeric:tabular-nums}
.cf .bn{font-size:14px;font-weight:700;margin-top:2px;font-variant-numeric:tabular-nums}

/* ══ 行动建议 ══ */
.actions{display:flex;flex-direction:column;gap:8px}
.act{display:flex;align-items:flex-start;gap:12px;padding:12px 14px;border-radius:10px;
  background:var(--subbg);border:1px solid var(--line);transition:border-color .15s,background .15s}
.act:hover{border-color:var(--bd);background:var(--card)}
.act .pri{flex:0 0 auto;font-size:11px;font-weight:600;padding:2.5px 10px;border-radius:6px;margin-top:1px;white-space:nowrap}
.act .tx{flex:1;font-size:13.5px;line-height:1.55}

/* ══ 项目矩阵：思维导图 ══ */
.mm-tabs{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:10px}
.mm-tab{border:1px solid var(--btn-line);background:var(--btn-bg);color:var(--btn-ink);border-radius:8px;
  padding:5px 12px;font-size:13px;cursor:pointer;transition:background .15s,border-color .15s}
.mm-tab:hover{background:var(--btn-bg-hover)}
.mm-tab.on{background:var(--selbg);border-color:color-mix(in srgb,var(--primary) 40%,var(--btn-line));
  color:var(--primary);font-weight:600}
.mm-tab .ct{opacity:.6;font-size:11px;margin-left:3px;font-weight:400}
.mm-bar{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-bottom:10px}
.mm-bar .tf-input{max-width:210px;min-width:130px}
.mm-fb{border:1px solid var(--btn-line);background:var(--btn-bg);color:var(--btn-ink);border-radius:7px;
  padding:4px 10px;font-size:12px;cursor:pointer}
.mm-fb:hover{background:var(--btn-bg-hover)}
.mm-fb:focus-visible{outline:none;box-shadow:var(--focus-ring)}
.mm-stage{overflow:auto;max-height:600px;border:1px solid var(--line);border-radius:10px;background:var(--subbg)}
.mm-stage svg{display:block}
.mm-link{fill:none;stroke:var(--bd);stroke-width:1.4}
.mm-node{cursor:pointer}
.mm-node rect{transition:filter .15s}
.mm-node:hover rect{filter:brightness(1.07)}
.mm-node text{font-size:12px;pointer-events:none;dominant-baseline:middle}
.mm-node.mm-hit rect{stroke:var(--amber);stroke-width:2.2}
.mm-node.mm-hit text{font-weight:700}
.mm-tg{fill:var(--card);stroke:var(--bd);stroke-width:1}
.mm-tg-t{font-size:11px;fill:var(--sub);pointer-events:none;dominant-baseline:middle}

/* ══ 项目矩阵：应收口径差异说明（表内「待收」vs 看板「应收」）══ */
.recv-diff{margin:10px 0 2px;border:1px solid color-mix(in srgb,var(--amber) 30%,var(--line));
  border-radius:10px;background:color-mix(in srgb,var(--amber) 7%,transparent);overflow:hidden}
.recv-diff>summary{cursor:pointer;padding:9px 13px;font-size:12.5px;line-height:1.65;color:var(--ink);
  list-style:none;display:block}
.recv-diff>summary::-webkit-details-marker{display:none}
.recv-diff>summary::after{content:" ▾";color:var(--sub);font-size:11px}
.recv-diff[open]>summary::after{content:" ▴"}
.recv-diff>summary b{color:var(--amber);font-variant-numeric:tabular-nums}
.rd-body{padding:0 13px 11px}
.rd-p{margin:6px 0;font-size:12.5px;line-height:1.7;color:var(--sub)}
.rd-p b{color:var(--ink)}
.rd-tbl{width:100%;border-collapse:collapse;font-size:12.5px}
.rd-tbl th,.rd-tbl td{padding:6px 9px;border-bottom:1px solid var(--line);text-align:left;white-space:nowrap}
.rd-tbl th{font-size:11.5px;color:var(--sub);font-weight:600;background:var(--subbg2)}
.rd-tbl td.num{text-align:right;font-variant-numeric:tabular-nums}
.rd-tbl .rd-why{white-space:normal;color:var(--sub);min-width:220px}

/* ══ 项目矩阵：状态标签（项目/生产计划的真实取值）══ */
.pl{display:inline-block;padding:2.5px 10px;border-radius:6px;font-size:11.5px;font-weight:600;
  white-space:nowrap;border:1px solid var(--line);background:var(--subbg2);color:var(--sub);line-height:1.5}
.pl-已交付{background:color-mix(in srgb,var(--green) 12%,transparent);color:var(--green);
  border-color:color-mix(in srgb,var(--green) 25%,transparent)}
.pl-计划中{background:color-mix(in srgb,var(--amber) 12%,transparent);color:var(--amber);
  border-color:color-mix(in srgb,var(--amber) 25%,transparent)}
.pl-暂放,.pl-暂停{background:color-mix(in srgb,var(--red) 12%,transparent);color:var(--red);
  border-color:color-mix(in srgb,var(--red) 25%,transparent)}
.pl-备料中,.pl-改造中{background:color-mix(in srgb,var(--amber) 12%,transparent);color:var(--amber);
  border-color:color-mix(in srgb,var(--amber) 25%,transparent)}
.pl-组装{background:color-mix(in srgb,var(--blue) 12%,transparent);color:var(--blue);
  border-color:color-mix(in srgb,var(--blue) 25%,transparent)}
.pl-库存核对{background:color-mix(in srgb,var(--purple) 12%,transparent);color:var(--purple);
  border-color:color-mix(in srgb,var(--purple) 25%,transparent)}
.pl-进行中{background:color-mix(in srgb,var(--blue) 12%,transparent);color:var(--blue);
  border-color:color-mix(in srgb,var(--blue) 25%,transparent)}

/* ══ 甘特图 ══ */
.gantt-wrap{overflow-x:auto}
.gantt{position:relative;min-width:820px;padding-top:22px}
.gantt-tools{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-bottom:12px}
.gantt-tools .tf-input{max-width:220px;min-width:150px}
.gantt-fb{border:1px solid var(--btn-line);background:var(--btn-bg);color:var(--btn-ink);
  font-size:12.5px;font-weight:600;padding:5px 12px;border-radius:999px;cursor:pointer;
  transition:background .15s,border-color .15s,box-shadow .15s;font-family:inherit}
.gantt-fb:hover{background:var(--btn-bg-hover);border-color:color-mix(in srgb,var(--primary) 45%,var(--btn-line))}
.gantt-fb:focus-visible{outline:none;box-shadow:var(--focus-ring)}
.gantt-fb.on{background:var(--selbg);border-color:color-mix(in srgb,var(--primary) 40%,var(--btn-line));color:var(--primary)}
.gantt-fb .ct{font-weight:500;opacity:.65;margin-left:2px}
.gantt-row{display:flex;align-items:center;gap:12px;margin:7px 0;font-size:13px;transition:opacity .2s,background .15s;border-radius:6px;padding:0 4px}
.gantt-row:hover{background:var(--subbg)}
.gantt-row.hl{background:var(--selbg);opacity:1}
.gantt-row.hide{display:none}
.gantt-label{width:230px;flex:0 0 230px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;color:var(--ink);cursor:default}
.gantt-track{position:relative;flex:1;height:30px;background:var(--subbg3);border-radius:5px}
/* 交期菱形标记的泳道在轨道上部，进度条压在下部 —— 两种交期与进度各占一行，互不遮挡 */
.gantt-bar{position:absolute;top:11px;height:16px;border-radius:4px;overflow:hidden;display:flex;align-items:center;
  color:#fff;font-size:10.5px;font-weight:600;cursor:pointer;transition:filter .15s,transform .15s}
.gantt-bar:hover{filter:brightness(1.12);transform:translateY(-1px);box-shadow:var(--shadow-md);z-index:5}
.gantt-bar.done{background:var(--green)} .gantt-bar.overdue{background:var(--red)} .gantt-bar.run{background:var(--primary)}
/* 「交期为历史推算」（计划未填合同交期）：仍按真实时间轴画，用虚线边框 + 半透明底
   与真实交期区分 —— 是「据此排期」，不是「客户承诺」，两者不能混为一谈 */
.gantt-bar.est{background:color-mix(in srgb,var(--primary) 26%,transparent);
  border:1px dashed color-mix(in srgb,var(--primary) 70%,transparent)}
.gantt-bar.est.done{background:color-mix(in srgb,var(--green) 26%,transparent);
  border-color:color-mix(in srgb,var(--green) 70%,transparent)}
.gantt-bar.est.overdue{background:color-mix(in srgb,var(--red) 26%,transparent);
  border-color:color-mix(in srgb,var(--red) 70%,transparent)}
.gantt-bar.est .gantt-prog{background:color-mix(in srgb,var(--ink) 14%,transparent)}
.gantt-row[data-est="1"] .gantt-label::after{content:"≈";margin-left:4px;color:var(--sub);font-weight:600}
/* ── 交期菱形标记（业主 2026-09-15：甘特图要并列显示三种状态）──
   .due = ② 合同交期（实心，客户的硬承诺）
   .est = ③ 历史推算交期（空心虚线，我们自己算的对标线） */
.gantt-ms{position:absolute;top:2px;width:10px;height:10px;transform:translateX(-50%) rotate(45deg);
  border-radius:2px;z-index:3;transition:transform .15s;cursor:default}
.gantt-ms.due{background:var(--ink);border:1px solid var(--card);box-shadow:0 0 0 1px var(--ink)}
.gantt-ms.est{background:var(--card);border:1.5px dashed var(--primary)}
.gantt-ms:hover{transform:translateX(-50%) rotate(45deg) scale(1.4)}
/* 图例小样（section 图注里用） */
.lg{display:inline-flex;align-items:center;gap:4px;margin:0 5px;white-space:nowrap}
.lg-bar{display:inline-block;width:16px;height:9px;border-radius:2px;background:var(--subbg3);position:relative;vertical-align:middle;overflow:hidden}
.lg-bar::after{content:"";position:absolute;left:0;top:0;bottom:0;width:55%;background:rgba(255,255,255,.32)}
.lg-bar.run{background:var(--primary)}
.lg-bar.done{background:var(--green)}
.lg-bar.overdue{background:var(--red)}
.lg-ms{display:inline-block;width:9px;height:9px;transform:rotate(45deg);border-radius:2px;vertical-align:middle;margin:0 1px}
.lg-ms.due{background:var(--ink)}
.lg-ms.est{background:var(--card);border:1.5px dashed var(--primary)}
.gantt-fb.sep{margin-left:10px;border-left:1px solid var(--line);padding-left:12px;border-radius:0}
.gantt-prog{position:absolute;left:0;top:0;bottom:0;background:rgba(255,255,255,.32)}
.gantt-cap{position:relative;padding:0 6px;white-space:nowrap}
.gantt-today{position:absolute;top:0;bottom:0;width:2px;background:var(--red);z-index:2}
.gantt-today::after{content:"今日";position:absolute;top:-20px;left:-11px;font-size:10px;color:var(--red);font-weight:600}
.gantt-tip{position:fixed;z-index:90;pointer-events:none;background:var(--card);border:1px solid var(--line);
  border-radius:10px;box-shadow:var(--shadow-md);padding:10px 12px;font-size:12.5px;line-height:1.65;
  min-width:210px;max-width:320px;color:var(--ink);opacity:0;transition:opacity .12s}
.gantt-tip.show{opacity:1}
.gantt-tip b{font-size:13px}
.gantt-tip .tr{display:flex;justify-content:space-between;gap:18px}
.gantt-tip .tr span:first-child{color:var(--sub)}
.gantt-tip .tr span:last-child{font-weight:600;font-variant-numeric:tabular-nums}

/* ══ 补录面板 ══ */
.bf-card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px;box-shadow:var(--shadow)}
.bf-toolbar{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-bottom:12px}
.bf-count{font-size:12.5px;color:var(--sub)}
.bf-name{font-weight:600;white-space:nowrap}
.bf-sub{font-size:11px;color:var(--sub);font-weight:400;margin-top:2px}
.pd-head{display:flex;align-items:center;justify-content:space-between;gap:10px;flex-wrap:wrap;margin-bottom:12px}
.pd-head h3{margin:0;border:0;padding:0}
.pd-stats{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin-bottom:14px}
.pd-stats>div{background:var(--subbg);border:1px solid var(--line);border-radius:10px;padding:10px;text-align:center}
.pd-stats b{display:block;font-size:20px;font-weight:700;line-height:1.15;font-variant-numeric:tabular-nums}
.pd-stats span{font-size:11.5px;color:var(--sub)}
.pd-stats .c-red{color:var(--red)} .pd-stats .c-green{color:var(--green)} .pd-stats .c-amber{color:var(--amber)}

/* ══ 同步徽标 ══ */
.sync-badge{display:inline-flex;align-items:center;gap:6px;font-size:12px;color:var(--sub);
  margin-left:auto;padding:5px 12px;background:var(--subbg);border:1px solid var(--line);
  border-radius:999px;font-weight:500;white-space:nowrap}
.sync-badge .dot{width:7px;height:7px;border-radius:50%;display:inline-block}
@media(max-width:680px){.sync-badge{margin-left:0;width:100%;justify-content:center;margin-top:8px}}

/* ══ 弹窗（合并原双重定义，去掉蓝头）══ */
.modal{display:none;position:fixed;inset:0;z-index:60}
.modal.show{display:flex;align-items:center;justify-content:center}
.modal-mask{position:fixed;inset:0;background:rgba(10,15,25,.55);display:flex;align-items:center;
  justify-content:center;z-index:60;padding:16px}
.modal-card{position:relative;width:min(440px,92vw);max-height:86vh;overflow:auto;background:var(--card);
  border:1px solid var(--line);border-radius:14px;padding:18px 20px;box-shadow:0 24px 64px rgba(0,0,0,.25);
  animation:pop .16s ease}
@keyframes pop{from{transform:scale(.97);opacity:.5}to{transform:scale(1);opacity:1}}
.modal-h{display:flex;align-items:center;gap:8px;font-size:15px;font-weight:700;margin-bottom:6px;color:var(--ink)}
.modal-h .svg-ic{width:17px;height:17px;color:var(--primary)}
.modal-sub{font-size:11px;font-weight:400;color:var(--sub)}
.modal-x{margin-left:auto;border:none;background:var(--subbg2);width:28px;height:28px;border-radius:8px;
  font-size:17px;line-height:1;cursor:pointer;color:var(--sub);transition:background .12s,color .12s}
.modal-x:hover{background:var(--subbg);color:var(--ink)}
.sync-t{width:100%;border-collapse:collapse}
.sync-t td{padding:11px 18px;border-bottom:1px solid var(--line);font-size:13.5px;vertical-align:middle}
.sync-t .sk{color:var(--sub);width:38%;font-weight:500;white-space:nowrap}
.sync-t .sv{color:var(--ink)}
.sync-t .sv .dot{width:8px;height:8px;border-radius:50%;display:inline-block;margin-right:6px;vertical-align:middle}
.modal .note{font-size:12px;color:var(--sub);padding:12px 18px 0;line-height:1.65}
.modal-ft{display:flex;gap:10px;padding:14px 18px 18px;flex-wrap:wrap}
.modal-ft .btn{flex:1}
@media(max-width:680px){.modal-ft .btn{flex:1}}
/* 同步弹窗（动态创建的 .modal-mask > .modal 结构）*/
.modal-mask .modal{display:block;position:relative;background:var(--card);border:1px solid var(--line);
  border-radius:14px;max-width:540px;width:100%;box-shadow:0 24px 64px rgba(0,0,0,.25);
  overflow:hidden;animation:pop .16s ease}
.modal-mask .modal .modal-h{padding:15px 18px;border-bottom:1px solid var(--line);margin-bottom:0;color:var(--ink)}
.modal-mask .modal .modal-h .svg-ic{fill:none;stroke:currentColor;color:var(--primary)}
.modal-mask .modal .modal-ft{padding:14px 18px 18px}

/* ══ 口令管理弹窗内容 ══ */
.pw-list{margin:14px 0 6px;display:flex;flex-direction:column;gap:8px}
.pw-row{display:flex;align-items:center;gap:8px}
.pw-name{width:120px;font-size:13px;font-weight:600;color:var(--txt)}
.pw-val{flex:1;font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:12.5px;
  padding:7px 10px;border:1px solid var(--bd);border-radius:8px;background:var(--subbg);
  color:var(--txt);letter-spacing:.02em}
.modal-actions{display:flex;gap:10px;margin-top:14px}
.pw-note{margin-top:12px;font-size:12px;line-height:1.65;color:var(--amber);
  background:color-mix(in srgb,var(--amber) 8%,var(--card));border:1px solid color-mix(in srgb,var(--amber) 25%,transparent);
  border-radius:10px;padding:10px 12px}

/* ══ 口令门（锁定页）══ */
.lock-mask{position:fixed;inset:0;background:var(--bg);display:flex;align-items:center;justify-content:center;
  z-index:9999;padding:16px}
.lock-card{background:var(--card);border:1px solid var(--line);border-radius:16px;
  box-shadow:var(--shadow-md);padding:34px 36px;width:min(360px,88vw);text-align:center}
.lock-emoji{font-size:32px;margin-bottom:6px}
.lock-card h2{margin:0 0 6px;font-size:18px;color:var(--ink);font-weight:700;letter-spacing:-.01em}
.lock-card p{margin:0 0 20px;font-size:13px;color:var(--sub)}
.lock-role{font-size:13px;font-weight:600;color:var(--primary);margin:0 0 8px}
.lock-err{color:var(--red);font-size:13px;min-height:18px;margin-top:10px;font-weight:500}
.lock-hint{font-size:11px;color:var(--sub);margin-top:14px;line-height:1.6}

/* ══ 角色条 / Tab ══ */
.role-bar{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin:0 0 14px;padding:10px 12px;
  background:var(--card);border:1px solid var(--line);border-radius:12px}
.role-lbl{font-weight:600;font-size:13px;color:var(--sub)}
.role-tab{border:1px solid var(--btn-line);background:var(--btn-bg);color:var(--btn-ink);font-size:13px;
  font-weight:500;padding:7px 14px;border-radius:9px;cursor:pointer;transition:.12s;min-height:36px}
.role-tab:hover{border-color:var(--primary);color:var(--primary);background:var(--btn-bg-hover)}
.role-tab.active{background:var(--primary);border-color:var(--primary);color:#fff}
.role-hint{margin-left:auto;font-size:11px;color:var(--sub)}
.role-share .svg-ic{vertical-align:middle}

/* ══ Toast ══ */
.toast{position:fixed;left:50%;bottom:calc(24px + env(safe-area-inset-bottom));
  transform:translateX(-50%) translateY(20px);background:var(--ink);color:var(--bg);
  font-size:13.5px;line-height:1.55;padding:12px 18px;border-radius:10px;
  box-shadow:0 8px 24px rgba(0,0,0,.22);opacity:0;pointer-events:none;
  transition:opacity .2s,transform .2s;z-index:9999;max-width:88vw;text-align:center}
.toast.show{opacity:1;transform:translateX(-50%) translateY(0)}
@media(max-width:680px){.role-hint{display:none}.role-tab{flex:1;text-align:center}.role-share{flex:1;justify-content:center}}

/* ══ 今天要处理 ══ */
.today{background:var(--card);border:1px solid color-mix(in srgb,var(--red) 30%,var(--line));
  border-radius:14px;padding:18px 20px;margin:18px 0 6px;box-shadow:var(--shadow)}
.today-hd{display:flex;align-items:center;gap:10px;margin-bottom:14px;flex-wrap:wrap}
.today-hd .t{font-size:16px;font-weight:700;letter-spacing:-.01em;color:var(--ink)}
.today-hd .cnt{background:color-mix(in srgb,var(--red) 14%,transparent);color:var(--red);font-size:12px;font-weight:600;
  padding:2px 10px;border-radius:999px;border:1px solid color-mix(in srgb,var(--red) 25%,transparent)}
.today-hd .ok{background:color-mix(in srgb,var(--green) 12%,transparent);color:var(--green);font-size:12px;font-weight:600;
  padding:2px 10px;border-radius:999px;border:1px solid color-mix(in srgb,var(--green) 25%,transparent)}
.today-list{display:flex;flex-direction:column;gap:10px}
.today-item{display:flex;align-items:center;gap:12px;background:var(--subbg);border:1px solid var(--line);
  border-radius:10px;padding:12px 14px;min-height:52px}
.today-item.p-高{border-left:3px solid var(--red)}
.today-item.p-中{border-left:3px solid var(--amber)}
.today-item.p-提示{border-left:3px solid var(--primary)}
.today-item .tx{flex:1;font-size:14px;line-height:1.6}
.today-empty{color:var(--sub);font-size:14px;padding:6px 2px}
@media(max-width:680px){
  .today{padding:16px 14px}
  .today-item{flex-wrap:wrap}
  .today-item .go{width:100%;min-height:44px}
}

/* ══ 更多分析折叠区 ══ */
.more-wrap{margin-top:22px}
.more-body{display:none;margin-top:16px}
.more-body.open{display:block}

/* ══ 资源负载 ══ */
.rs-sum{display:flex;flex-wrap:wrap;gap:10px 22px;align-items:center;margin-bottom:14px;
  padding:12px 14px;background:var(--subbg);border:1px solid var(--line);border-radius:10px;
  font-size:13.5px;color:var(--sub)}
.rs-sum b{font-size:17px;font-weight:700;color:var(--ink);margin-left:4px;font-variant-numeric:tabular-nums}
.rs-sum b.neg{color:var(--red)}
.rs-bar{display:flex;align-items:center;gap:8px;min-width:150px}
.rs-track{position:relative;flex:1;height:18px;background:var(--subbg2);border-radius:5px;overflow:hidden}
.rs-fill{height:100%;border-radius:5px;transition:width .3s}
.rs-fill.over{background:var(--red)} .rs-fill.busy{background:var(--amber)}
.rs-fill.ok{background:var(--green)} .rs-fill.idle{background:var(--bd)}
.rs-num{flex:none;width:48px;text-align:right;font-size:12.5px;font-weight:600;color:var(--ink);
  font-variant-numeric:tabular-nums}
.rs-num.neg{color:var(--red)}
.rs-sub{font-size:11.5px;color:var(--sub);margin-top:2px;font-weight:400}
@media(max-width:680px){.rs-sum{gap:8px 14px;font-size:12.5px}.rs-sum b{font-size:15px}}

/* ══ 微信情报台表格内复选框 ══ */
.wx-sel{width:15px;height:15px;accent-color:var(--primary);cursor:pointer}
.wx-actions{margin-top:12px}
.wx-actions .rs-sub{line-height:1.6}

/* ══ 表格筛选/勾选工具条（通用：data-filter/data-select 表格）══ */
.tf-tools{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-bottom:10px}
.tf-tools .tf-input{flex:0 1 200px;min-width:140px;max-width:220px;font-size:12.5px;padding:6px 10px}
.tf-input,.tf-sel{height:32px;border-radius:8px;border:1px solid var(--bd);background:var(--inputbg);color:var(--txt);
  font-family:inherit;transition:border-color .15s,box-shadow .15s}
.tf-input:focus,.tf-sel:focus{outline:none;border-color:var(--primary);box-shadow:var(--focus-ring)}
.tf-input::placeholder{color:var(--sub);opacity:.75}
.tf-sel{font-size:12.5px;padding:0 26px 0 10px;cursor:pointer;
  appearance:none;-webkit-appearance:none;
  background-image:url("data:image/svg+xml;charset=utf-8,%3Csvg xmlns='http://www.w3.org/2000/svg' width='10' height='6' viewBox='0 0 10 6'%3E%3Cpath d='M1 1l4 4 4-4' fill='none' stroke='%236b7280' stroke-width='1.5' stroke-linecap='round' stroke-linejoin='round'/%3E%3C/svg%3E");
  background-repeat:no-repeat;background-position:right 9px center}
.tf-cnt{font-size:12px;color:var(--sub);white-space:nowrap}
.tf-row-sel{width:15px;height:15px;accent-color:var(--primary);cursor:pointer;vertical-align:middle}
table tr.hl td{background:var(--selbg)}
.tf-bar{display:none;align-items:center;gap:10px;flex-wrap:wrap;margin:10px 0 0;
  padding:9px 12px;background:var(--selbg);border:1px solid color-mix(in srgb,var(--primary) 30%,var(--line));
  border-radius:10px;font-size:12.5px;color:var(--ink)}
.tf-bar.show{display:flex}
.tf-bar b{font-variant-numeric:tabular-nums}
.tf-bar .btn,.tf-bar .btn-ghost{padding:5px 12px;font-size:12.5px;min-height:30px;border-radius:8px}
.badge-real{display:inline-block;background:color-mix(in srgb,var(--primary) 12%,transparent);color:var(--primary);
  font-size:11px;font-weight:600;padding:2.5px 10px;border-radius:999px;white-space:nowrap;
  border:1px solid color-mix(in srgb,var(--primary) 25%,transparent)}
.svg-ic{fill:none;stroke:currentColor;stroke-width:1.8;stroke-linecap:round;stroke-linejoin:round}

/* ══ 移动端收尾 ══ */
@media(max-width:680px){
  .wrap{padding:14px 14px calc(32px + env(safe-area-inset-bottom))}
  header.top{padding:15px 16px}
  header.top h1{font-size:17px}
  .btn,.bf-btn{min-height:40px}
  section{margin-top:22px}
}
/* ════════════════════════════════════════════════════════════════════════════
   v6 外壳（2026-10-02 UX 重构）
   背景：旧版把 22 个区块铺在一条 16 屏长的纸面上（展开后 14011px），
        导航 20 项但只有 4 个区块可见，首屏 80% 是外壳。
   本层只重写外壳与导航，区块渲染器一个不动 —— 靠 <section> 复用。
   ════════════════════════════════════════════════════════════════════════════ */
body{background:var(--bg)}
.shell{display:flex;flex-direction:column;min-height:100vh}

/* ── 顶栏：56px 单行 ───────────────────────────────────────────────── */
.topbar{position:sticky;top:0;z-index:60;display:flex;align-items:center;gap:10px;
  height:56px;padding:0 16px;background:color-mix(in srgb,var(--card) 86%,transparent);
  -webkit-backdrop-filter:blur(14px);backdrop-filter:blur(14px);
  border-bottom:1px solid var(--line)}
.topbar .brand{display:flex;align-items:center;gap:8px;font-size:15px;font-weight:600;
  color:var(--ink);white-space:nowrap;line-height:1}
.topbar .brand svg{width:20px;height:20px;color:var(--primary);opacity:1}
.topbar .spacer{flex:1 1 auto;min-width:8px}
.topbar .meta{font-size:12px;color:var(--sub);white-space:nowrap;overflow:hidden;
  text-overflow:ellipsis;max-width:360px;margin:0}
.chip{display:inline-flex;align-items:center;gap:6px;height:32px;padding:0 10px;
  background:var(--subbg);border:1px solid var(--line);border-radius:8px;
  font-size:12.5px;color:var(--sub);cursor:pointer;white-space:nowrap;line-height:1}
.chip:hover{color:var(--ink);border-color:var(--bd)}
.chip svg{width:14px;height:14px;color:currentColor}
.chip .dot{width:7px;height:7px;border-radius:50%;background:var(--green);flex:0 0 7px}
/* 数据状态 chip 里的文字很长（"真实数据 · 较新 · 同步于 … · 22 小时前"），
   不夹住的话它会 nowrap 把整个顶栏顶宽，进而在手机上把 layout viewport 撑到 570px。 */
#dataChip{max-width:min(360px,44vw);overflow:hidden}
#dataChip .sync-badge{min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.iconbtn{display:inline-flex;align-items:center;justify-content:center;width:32px;height:32px;
  padding:0;background:var(--subbg);border:1px solid var(--line);border-radius:8px;
  color:var(--sub);cursor:pointer}
.iconbtn:hover{color:var(--ink);border-color:var(--bd)}
.topbar .demo-flag,.topbar .real-flag{position:static;top:auto;right:auto;
  font-size:11px;padding:2.5px 8px;border-radius:6px;white-space:nowrap}
.dd{position:relative}
.dd-menu{position:absolute;top:calc(100% + 8px);right:0;min-width:216px;padding:6px;
  background:var(--card);border:1px solid var(--line);border-radius:12px;
  box-shadow:var(--shadow-md);display:none;z-index:70}
.dd-menu.open{display:block}
.dd-item{display:flex;align-items:center;gap:9px;width:100%;padding:8px 10px;border:0;
  background:none;border-radius:8px;font-size:13px;color:var(--ink);cursor:pointer;
  text-align:left;line-height:1.3;font-family:inherit}
.dd-item:hover{background:var(--subbg)}
.dd-item svg{width:15px;height:15px;flex:0 0 15px;color:var(--sub)}
.dd-item.active{color:var(--primary);background:var(--primary-subtle)}
.dd-item.active svg{color:var(--primary)}
.dd-sep{height:1px;margin:5px 4px;background:var(--line)}

/* ── 主体：左导航 + 内容 ───────────────────────────────────────────── */
.bodyrow{display:flex;flex:1;align-items:flex-start;min-width:0}
.sidenav{position:sticky;top:56px;flex:0 0 172px;width:172px;align-self:flex-start;
  max-height:calc(100vh - 56px);overflow-y:auto;padding:14px 10px 20px;
  display:flex;flex-direction:column;gap:2px}
.navitem{display:flex;align-items:center;gap:9px;width:100%;padding:9px 11px;border:0;
  background:none;border-radius:9px;font-size:13.5px;color:var(--sub);cursor:pointer;
  text-align:left;line-height:1.2;font-family:inherit}
.navitem:hover{background:var(--subbg);color:var(--ink)}
.navitem.active{background:var(--primary-subtle);color:var(--primary);font-weight:600}
.navitem svg{width:17px;height:17px;flex:0 0 17px;color:currentColor;opacity:.85}
.navitem .badge{margin-left:auto;min-width:19px;height:19px;padding:0 5px;border-radius:10px;
  background:var(--red);color:#fff;font-size:11px;line-height:19px;text-align:center;font-weight:600}
.nav-sep{height:1px;margin:8px 6px;background:var(--line)}
.nav-note{padding:6px 11px;font-size:11px;color:var(--sub);line-height:1.6}
.main{flex:1 1 auto;min-width:0;padding:0 22px 48px}

/* ── 标签条：吸顶，替代旧的滚动锚点导航 ──────────────────────────── */
.tabstrip{position:sticky;top:56px;z-index:50;display:flex;gap:4px;overflow-x:auto;
  padding:8px 0;background:color-mix(in srgb,var(--bg) 90%,transparent);
  -webkit-backdrop-filter:blur(10px);backdrop-filter:blur(10px);
  border-bottom:1px solid var(--line);margin-bottom:16px}
.tabstrip::-webkit-scrollbar{height:0}
.tab{flex:0 0 auto;padding:6px 13px;border-radius:8px;border:1px solid transparent;
  background:none;font-size:13px;color:var(--sub);cursor:pointer;white-space:nowrap;
  line-height:1.4;font-family:inherit}
.tab:hover{background:var(--subbg);color:var(--ink)}
.tab.active{background:var(--card);border-color:var(--line);color:var(--ink);font-weight:600}
.tab .tb-badge{display:inline-block;margin-left:6px;padding:0 5px;border-radius:8px;
  background:var(--red);color:#fff;font-size:10.5px;line-height:16px;font-weight:600}

/* ── 面板：一次只显示一个区块 ──────────────────────────────────────── */
.pane{display:none}
.pane.active{display:block;animation:panefade .16s ease-out}
@keyframes panefade{from{opacity:0;transform:translateY(3px)}to{opacity:1;transform:none}}
@media (prefers-reduced-motion:reduce){.pane.active{animation:none}}
.pane>section{margin-top:0}
.pane>section+section{margin-top:22px}
/* 页面内的锚点跳转不再需要滚动补偿（同页只显示一个区块） */
/* ── KPI 常驻条 ──────────────────────────────────────────────────────
   旧版是 4 张竖卡（116px 高，还只占左半边）。这里是 38px 一条横排，
   每个标签页顶部都在，但只吃掉首屏 1/20 而不是 1/6。 */
#kpiBar{display:flex;flex-wrap:wrap;gap:8px;margin:12px 0 2px}
#kpiBar .kpi{flex:1 1 196px;flex-direction:row;align-items:center;gap:7px;
  padding:9px 12px;border-radius:10px;min-width:0}
#kpiBar .kpi:hover{transform:none;box-shadow:var(--shadow);border-color:var(--line)}
#kpiBar .kpi .top{margin:0;flex:0 0 auto}
#kpiBar .kpi .lbl{font-size:11.5px;white-space:nowrap}
#kpiBar .kpi .v{font-size:18px;line-height:1.2;margin-left:auto}
#kpiBar .kpi .sub{margin:0;font-size:10.5px;white-space:nowrap;overflow:hidden;
  text-overflow:ellipsis;max-width:44%}

/* ── 抽屉：新建 / 补录 ─────────────────────────────────────────────── */
.drawer{position:fixed;inset:0;z-index:80;display:none}
.drawer.open{display:block}
.drawer-mask{position:absolute;inset:0;background:rgba(15,23,42,.42)}
.drawer-panel{position:absolute;top:0;right:0;bottom:0;width:min(720px,94vw);
  background:var(--bg);border-left:1px solid var(--line);overflow-y:auto;padding:20px 22px 40px}
.drawer-h{display:flex;align-items:center;gap:10px;margin-bottom:14px;font-size:15px;font-weight:600}
.drawer-h .x{margin-left:auto;width:30px;height:30px;border:1px solid var(--line);
  background:var(--card);border-radius:8px;cursor:pointer;color:var(--sub);font-size:17px;line-height:1}
.drawer-panel>section{margin-top:0}

/* ── 表格：短字段不换行（旧版 2026-07-28 会竖着断两行，看着像数据错乱） ──
   ⚠️ 这里**不做** sticky 表头：表格外层是 <div style="overflow-x:auto">，
   按 CSS 规范一轴非 visible 时另一轴会算成 auto —— 那个 div 因此成了纵向滚动容器，
   position:sticky 便以它为基准偏移，表头会钉在表格正中间压住第 5 行。
   现有表格都 data-paginate 到 8 行/页，也不需要 sticky。 */
.pane td.nowrap,.pane th.nowrap{white-space:nowrap}

/* ── 移动端：左导航换成底部标签栏 ─────────────────────────────────── */
.tabbar{display:none}
@media (max-width:1023px){
  .main{padding:0 14px calc(64px + env(safe-area-inset-bottom))}
  .sidenav{display:none}
  .tabstrip{top:56px}
  .tabbar{position:fixed;left:0;right:0;bottom:0;z-index:70;display:flex;
    background:color-mix(in srgb,var(--card) 94%,transparent);
    -webkit-backdrop-filter:blur(14px);backdrop-filter:blur(14px);
    border-top:1px solid var(--line);
    padding-bottom:env(safe-area-inset-bottom)}
  .tabbar button{flex:1 1 0;display:flex;flex-direction:column;align-items:center;gap:3px;
    padding:8px 2px 7px;border:0;background:none;color:var(--sub);font-size:10.5px;
    cursor:pointer;font-family:inherit;position:relative}
  .tabbar button.active{color:var(--primary)}
  .tabbar button svg{width:20px;height:20px}
  .tabbar .tb-badge{position:absolute;top:4px;left:50%;margin-left:4px;min-width:15px;
    height:15px;padding:0 4px;border-radius:8px;background:var(--red);color:#fff;
    font-size:10px;line-height:15px;font-weight:600}
  .topbar .meta{display:none}
}
@media (max-width:600px){
  .topbar{padding:0 10px;gap:7px}
  .topbar .brand b{display:none}
  .chip{padding:0 8px}
  .iconbtn{width:32px}
  /* KPI 改成 2×2：横排 minmax(196px) 在 390px 上会退化成 1 列 4 行，白吃 168px 首屏 */
  #kpiBar{display:grid;grid-template-columns:1fr 1fr;gap:8px}
  #kpiBar .kpi{flex-direction:column;align-items:flex-start;gap:2px;padding:8px 10px}
  #kpiBar .kpi .v{margin-left:0;font-size:17px}
  #kpiBar .kpi .sub{max-width:100%}
}

/* ════════════════════════════════════════════════════════════════════════════
   v7 图表工具箱（2026-10-02）
   起因：v6 把外壳修好了，但整站 **0 个图表** —— 数字堆在表格里，不是驾驶舱。
   参照（公开规范与开源实现）：
     · 制造业看板规范：核心 KPI 置顶；绿(达成≥90/良率≥98)/黄/红 三档语义色；
       图表 ≤4 种/屏；每屏 6~8 个部件封顶；"3 秒扫读"原则；实际值 + 目标值缺一不可
     · Tremor / shadcn dashboard-01：KPI 卡 = 标签 + 大数字 + 环比角标 + 目标进度条 + 迷你走势
     · 开源 OEE 看板（Dashboard-OEE）：主指标配色阈值 + 三分量拆解 + Top 损失 Pareto + 迷你柱图
   ════════════════════════════════════════════════════════════════════════════ */

/* ── 图表卡容器 ─────────────────────────────────────────────────────── */
.viz{background:var(--card);border:1px solid var(--line);border-radius:12px;
  box-shadow:var(--shadow);padding:15px 17px;min-width:0}
.viz+.viz{margin-top:14px}
.viz-hd{display:flex;align-items:baseline;gap:10px;margin-bottom:13px}
.viz-hd h3{margin:0;font-size:13.5px;font-weight:600;color:var(--ink);letter-spacing:-.01em}
.viz-hd .q{margin-left:auto;font-size:11px;color:var(--sub);text-align:right;line-height:1.4}
.viz-grid{display:grid;gap:14px;grid-template-columns:repeat(auto-fit,minmax(310px,1fr));align-items:start}
.viz-grid>.viz{margin-top:0}
.viz-note{font-size:11px;color:var(--sub);line-height:1.65;margin-top:11px;
  padding-top:10px;border-top:1px dashed var(--line)}
.viz-empty{font-size:12px;color:var(--sub);padding:14px 0;text-align:center}
/* 「更多指标」是横向滚动条（.hscroll），既有的弹性尺寸只写给 `> .card` 和 `> .kpi`。
   v7 的统计卡类名是 `.stat`，不补这条就会在 flex 行里被压扁/拉长。 */
.hscroll>.stat{flex:0 0 auto;min-width:210px;max-width:276px;scroll-snap-align:start}
/* ⚠️ 既有缺陷修复（v7 暴露出来的）：`.g2/.g3` 用的 `1fr` 其实等价于 `minmax(auto,1fr)`，
   于是列里一旦有**长不可断内容**（最典型是维修明细单元格里塞的 markdown 原文表格），
   该列的最小内容宽度就会把整行撑开、把邻列压成窄条 —— 质量页的良率卡就是这么被挤扁的。
   把 min 显式写成 0，列宽才真正均分、内容该溢出就溢出（表格本来就有 overflow-x:auto）。 */
.grid.g2{grid-template-columns:repeat(2,minmax(0,1fr))}
.grid.g3{grid-template-columns:repeat(3,minmax(0,1fr))}

/* ── 条形榜（Pareto）：标签 / 值 / 轨道 ─────────────────────────────── */
.bl{display:flex;flex-direction:column;gap:9px}
.bl-row{display:grid;grid-template-columns:1fr auto;gap:3px 10px;align-items:baseline}
.bl-lbl{font-size:12.5px;color:var(--ink);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.bl-val{font-size:12px;color:var(--sub);font-variant-numeric:tabular-nums;white-space:nowrap}
.bl-track{grid-column:1/-1;height:7px;background:var(--subbg3);border-radius:4px;overflow:hidden}
.bl-fill{display:block;height:100%;border-radius:4px;background:var(--primary);min-width:2px}
.bl-fill.s-ok{background:var(--green)} .bl-fill.s-warn{background:var(--amber)} .bl-fill.s-bad{background:var(--red)}

/* ── 子弹图（实际 vs 目标）：轨道 + 填充 + 目标刻线 ──────────────────── */
.bt+.bt{margin-top:11px}
.bt-head{display:flex;justify-content:space-between;gap:10px;font-size:12.5px;margin-bottom:5px}
.bt-head .bt-l{color:var(--ink);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.bt-head .bt-v{font-variant-numeric:tabular-nums;white-space:nowrap;color:var(--ink);font-weight:500}
.bt-head .bt-v em{font-style:normal;color:var(--sub);font-weight:400}
.bt-track{position:relative;height:15px;background:var(--subbg3);border-radius:4px;overflow:hidden}
.bt-fill{display:block;height:100%;border-radius:4px}
.bt-mark{position:absolute;top:-2px;bottom:-2px;width:2px;background:var(--ink);opacity:.6}
.bt-scale{display:flex;justify-content:space-between;font-size:10px;color:var(--sub);margin-top:3px}

/* ── 构成条（成本结构 / 阶段分布）─────────────────────────────────── */
.stk{display:flex;height:11px;border-radius:6px;overflow:hidden;background:var(--subbg3)}
.stk i{display:block;height:100%;min-width:2px}
.legend{display:flex;flex-wrap:wrap;gap:7px 15px;margin-top:12px}
.legend span{display:inline-flex;align-items:center;gap:6px;font-size:11.5px;color:var(--sub)}
.legend i{width:9px;height:9px;border-radius:2px;flex:0 0 9px}
.legend b{color:var(--ink);font-weight:600;font-variant-numeric:tabular-nums}

/* ── 环比角标 ──────────────────────────────────────────────────────── */
.tp{display:inline-flex;align-items:center;gap:2px;font-size:11px;font-weight:600;
  padding:1.5px 7px;border-radius:6px;font-variant-numeric:tabular-nums;white-space:nowrap}
.tp.good{background:color-mix(in srgb,var(--green) 13%,transparent);color:var(--green)}
.tp.bad{background:color-mix(in srgb,var(--red) 13%,transparent);color:var(--red)}
.tp.flat{background:var(--subbg3);color:var(--sub)}

/* ── 统计卡（重做 KPI）：标签 → 大数字 → 迷你走势 → 目标进度 → 备注 ─── */
#kpiBar{display:grid;grid-template-columns:repeat(auto-fit,minmax(212px,1fr));
  gap:12px;margin:12px 0 2px}
/* ⚠️ v6 曾在 @media(max-width:600px) 里把 #kpiBar 改成 2×2；但 v7 的上面这条
   网格规则写在**那个媒体查询之后**，同优先级下后者胜出，于是移动端又退回
   「1 列 4 行」，4 张卡把「今天要处理」整块挤到折叠线以下。这里重新声明一次。
   卡内元素同步收窄：不缩字号的话 181px 宽会把大数字和「目标」挤成两行。 */
@media (max-width:600px){
  #kpiBar{grid-template-columns:repeat(2,minmax(0,1fr));gap:8px}
  .stat{padding:10px 11px;gap:6px}
  .stat .sv b{font-size:20px}
  .stat .sl{font-size:11px}
  .stat .sf{font-size:10px}
  .stat .spark{height:18px}
}
.stat{background:var(--card);border:1px solid var(--line);border-radius:12px;
  box-shadow:var(--shadow);padding:13px 15px;display:flex;flex-direction:column;
  gap:8px;min-width:0}
.stat .sl{display:flex;align-items:center;gap:7px;font-size:11.5px;color:var(--sub);font-weight:500}
.stat .sl i{width:7px;height:7px;border-radius:50%;flex:0 0 7px;display:block}
.stat .sv{display:flex;align-items:baseline;gap:7px;flex-wrap:wrap}
.stat .sv b{font-size:25px;font-weight:600;letter-spacing:-.02em;color:var(--ink);
  font-variant-numeric:tabular-nums;line-height:1.1}
.stat .sv b.s-ok{color:var(--green)} .stat .sv b.s-warn{color:var(--amber)}
.stat .sv b.s-bad{color:var(--red)} .stat .sv b.s-neg{color:var(--red)}
.stat .sv em{font-style:normal;font-size:12px;color:var(--sub)}
.stat .spark{display:block;width:100%;height:22px;margin:-1px 0}
/* spark 的尺寸原本只定义在 .stat 命名空间里；图表卡里单独用会塌成 0 高。 */
.viz .spark{display:block;width:100%;height:42px;margin:6px 0 2px}
.stat .bar{height:5px;border-radius:3px;background:var(--subbg3);overflow:hidden}
.stat .bar i{display:block;height:100%;border-radius:3px;background:var(--primary)}
.stat .sf{display:flex;align-items:center;gap:8px;font-size:10.5px;color:var(--sub);
  line-height:1.5;min-height:15px}
.stat .sf .tgt{margin-left:auto;white-space:nowrap}

/* ── 行动行（今天要处理：左侧严重度色轨 + 影响面元信息）────────────── */
.acts{display:flex;flex-direction:column;gap:8px}
.act-row{display:grid;grid-template-columns:4px 1fr auto;border:1px solid var(--line);
  border-radius:10px;background:var(--card);overflow:hidden}
.act-rail{background:var(--red)}
.act-row.p-中 .act-rail{background:var(--amber)}
.act-row.p-低 .act-rail{background:var(--blue)}
.act-row.p-提示 .act-rail{background:var(--blue)}
.act-body{padding:11px 0 11px 13px;min-width:0}
.act-t{font-size:13px;color:var(--ink);line-height:1.55}
.act-m{display:flex;gap:6px;margin-top:7px;flex-wrap:wrap}
.act-m span{font-size:10.5px;color:var(--sub);background:var(--subbg);
  border:1px solid var(--line);border-radius:5px;padding:1.5px 7px;white-space:nowrap}
.act-m span b{color:var(--ink);font-weight:600;font-variant-numeric:tabular-nums}
.act-go{align-self:center;margin-right:13px}
/* ⚠️ 基础按钮样式（约 1508 行那组选择器）只覆盖 `.today-item .go`。
   v7 把「今天要处理」的列表换成了 .act-row，若不在这里自己给全，
   按钮会退化成浏览器默认样式（灰底凸起，跟整站完全不搭）。 */
.act-go{flex:0 0 auto;border:1px solid var(--btn-line);background:var(--btn-bg);
  color:var(--btn-ink);border-radius:8px;padding:7px 12px;font-size:12.5px;font-weight:500;
  cursor:pointer;font-family:inherit;white-space:nowrap;transition:background .12s,border-color .12s,color .12s}
.act-go:hover{background:var(--primary);border-color:var(--primary);color:#fff}
.act-go:focus-visible{outline:none;box-shadow:var(--focus-ring)}
@media (max-width:600px){
  /* 窄屏按钮不再挤在右侧，改为掉到正文下面一行（否则正文被压成窄条） */
  .act-row{grid-template-columns:4px 1fr}
  .act-body{padding:11px 13px 8px}
  .act-go{grid-column:2;margin:0 13px 12px;justify-self:start}
}

/* ── 良率环（质量页）───────────────────────────────────────────────── */
.rings{display:grid;grid-template-columns:repeat(auto-fit,minmax(126px,1fr));gap:12px}
.ringbox{display:flex;flex-direction:column;align-items:center;gap:7px;padding:12px 6px;
  border:1px solid var(--line);border-radius:10px;background:var(--subbg)}
.ringbox .rl{font-size:11.5px;color:var(--sub);text-align:center}
.ringbox .rs{font-size:10.5px;color:var(--sub);text-align:center}
</style>
</head>
<body>
<div class="lock-mask" id="lockMask">
  <div class="lock-card">
    <div class="lock-emoji">🔒</div>
    <h2>需要访问口令</h2>
    <p>生产 · 项目管理驾驶舱 · 内部业务数据</p>
    <div class="lock-role" id="lockRole"></div>
    <div style="position:relative"><input class="lock-input" id="lockInput" type="text" placeholder="请输入访问口令" autocomplete="off" style="padding-right:48px"><button class="lock-eye" id="lockEye" type="button" tabindex="-1" aria-label="显示或隐藏口令"><svg id="lockEyeIcon" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/></svg></button></div>
    <button class="lock-btn" id="lockBtn">解锁查看</button>
    <div class="lock-err" id="lockErr"></div>
    <div class="lock-hint">管理员口令可看全部角色；各角色有独立口令，仅能看自己那一份。口令已 base64 混淆存储，仍非真加密，请勿泄露。</div>
  </div>
</div>
<div class="modal" id="pwModal">
  <div class="modal-mask" id="pwModalMask"></div>
  <div class="modal-card">
    <div class="modal-h">口令管理 <span class="modal-sub">仅管理员可见 · 已 base64 混淆</span><button class="modal-x" id="pwClose" aria-label="关闭">×</button></div>
    <div id="pwList" class="pw-list"></div>
    <div class="modal-actions">
      <button class="btn-primary" id="pwRotate">轮换全部口令</button>
      <button class="btn-ghost" id="pwCopyAll">复制全部口令</button>
    </div>
    <div class="pw-note" id="pwNote"></div>
  </div>
</div>
<div class="shell">
  <!-- ── 顶栏：56px 单行。运维动作全部收进「⋯」菜单 ────────────────── -->
  <header class="topbar">
    <span class="brand">__TITLE_ICON__<b>生产驾驶舱</b></span>
    <span class="demo-flag" id="demoFlag" style="display:none"></span>
    <button class="chip" id="dataChip" title="查看数据同步详情"><span class="sync-badge" id="syncBadge"></span></button>
    <div class="spacer"></div>
    <div class="meta" id="metaLine"></div>
    <div class="dd">
      <button class="chip" id="roleBtn">__IC_USER__<span id="roleName">—</span>__IC_CHEV__</button>
      <div class="dd-menu" id="roleMenu"></div>
    </div>
    <div class="dd">
      <button class="iconbtn" id="btnMore" title="更多操作" aria-haspopup="true" aria-expanded="false">__IC_DOTS__</button>
      <div class="dd-menu" id="moreMenu">
        <button class="dd-item" id="btnNew">__IC_ADD__ 新建项目 / 生产计划</button>
        <button class="dd-item" id="btnBF">__IC_EDIT__ 补录缺失数据</button>
        <div class="dd-sep"></div>
        <button class="dd-item" id="btnSync">__IC_SYNC__ 同步状况</button>
        <button class="dd-item" id="btnRefresh">__IC_REFRESH__ 重新生成说明</button>
        <button class="dd-item" id="btnExport">__IC_DOWNLOAD__ 导出分析 JSON</button>
        <button class="dd-item" id="btnImport">__IC_UPLOAD__ 导入数据快照</button>
        <button class="dd-item" id="btnAnalyze">__IC_ANALYZE__ 分析数据（复制发我）</button>
        <div class="dd-sep"></div>
        <button class="dd-item" id="btnTheme">__IC_THEME__ 主题：自动</button>
        <button class="dd-item" id="btnShare">__IC_SHARE__ <span id="shareLabel">分享视图</span></button>
        <button class="dd-item" id="pwManageBtn" style="display:none">__IC_KEY__ 口令管理</button>
      </div>
    </div>
    <input type="file" id="fileInput" accept="application/json" style="display:none">
  </header>

  <div class="bodyrow">
    <nav class="sidenav" id="sideNav" aria-label="主导航"></nav>
    <main class="main">
      <div id="kpiBar"></div>
      <div class="tabstrip" id="tabStrip" role="tablist"></div>
      <div id="app"></div>
    </main>
  </div>

  <!-- ── 移动端底部标签栏 ──────────────────────────────────────────── -->
  <nav class="tabbar" id="tabBar" aria-label="主导航（移动）"></nav>

  <!-- ── 抽屉：新建 / 补录（复用原有区块，不另写一套） ──────────────── -->
  <div class="drawer" id="drawer">
    <div class="drawer-mask" id="drawerMask"></div>
    <div class="drawer-panel">
      <div class="drawer-h"><span id="drawerTitle">—</span><button class="x" id="drawerClose" aria-label="关闭">×</button></div>
      <div id="drawerBody"></div>
    </div>
  </div>

  <!-- 兼容保留：旧版横幅容器（内容改由顶栏 demoFlag + 数据详情弹窗承担） -->
  <div class="banner" id="banner" style="display:none"></div>

  <footer style="padding:0 22px 22px;text-align:center">驾驶舱由 seatable-production 技能数据快照生成 · 单文件离线可用 · 重跑 cockpit.py 可刷新</footer>
</div>

<script>
let MODEL = __MODEL__;
let UNLOCK = null;  // 解锁态：null | {level:"admin"} | {level:"role",role:"xxx"}
let LIVE = null;    // 在线模式：null=探测中/离线 | {origin:"http://127.0.0.1:8790"}

/* ── 在线模式（cockpit_server.py 伴生服务）──────────────────
   探测本机伴生服务器；在线时写操作按钮直连 API，无需复制粘贴。
   探测失败（没起服务器/离线打开）静默回退纯静态模式，行为与从前一致。 */
const LIVE_PROBE_PORTS = [8801, 8802, 8803, 8790];
function liveDetect(cb){
  if(window.__liveDetectTried){ cb(!!LIVE); return; }
  window.__liveDetectTried = true;
  let pending = LIVE_PROBE_PORTS.length;
  const finish = () => { if(--pending === 0) cb(!!LIVE); };
  LIVE_PROBE_PORTS.forEach(port=>{
    fetch("http://127.0.0.1:"+port+"/api/ping", {signal: AbortSignal.timeout ? AbortSignal.timeout(900) : undefined})
      .then(r=>r.ok ? r.json() : null)
      .then(j=>{
        if(j && j.server==="cockpit-local" && !LIVE){
          LIVE = {origin: "http://127.0.0.1:"+port};
          document.documentElement.classList.add("live-mode");
        }
        finish();
      })
      .catch(()=>finish());
  });
}
function liveAPI(path, body, cb){
  if(!LIVE){ cb({ok:false, error:"离线模式：请先启动 cockpit_server.py"}); return; }
  fetch(LIVE.origin + path, {
    method:"POST", headers:{"Content-Type":"application/json"},
    body: JSON.stringify(body || {})
  }).then(r=>r.json()).then(cb).catch(e=>cb({ok:false, error:String(e)}));
}


/* 口令以 base64 形式嵌在源码里，运行时解码；管理员本地轮换会写 localStorage 覆盖 */
function _b64dec(s){ try{ const bin=atob(s); const b=new Uint8Array(bin.length); for(let i=0;i<bin.length;i++) b[i]=bin.charCodeAt(i); return JSON.parse(new TextDecoder("utf-8").decode(b)); }catch(e){ return {}; } }
function _b64enc(o){ return btoa(unescape(encodeURIComponent(JSON.stringify(o)))); }
let PW = (function(){ const o=localStorage.getItem("cockpit_pw_override"); if(o){ try{ return _b64dec(o); }catch(e){} } return _b64dec(__PW_BLOB__); })();

/* ---------- 工具 ---------- */
const $ = (s,r=document)=>r.querySelector(s);
function fmt(n){ if(n===null||n===undefined||isNaN(n)) return "0";
  return Number(n).toLocaleString("zh-CN",{maximumFractionDigits:0}); }
function yuan(n){ return "¥"+fmt(n); }
function pct(n){
  /* ⚠️ 必须在这里取整到 1 位。旧写法是裸的 `n+"%"`，于是任何**没有预先 round**
     的比值都会把十几位小数原样打上屏幕 —— 实测出现过
     「物料齐套率 93.5064935064935%」这种明显没处理过的数字。
     保留 1 位是为了还能看出 0.1% 的差异，再多就只是噪声。
     这是全局兜底：调用方 round 过是双保险，漏了也不会漏到界面上。 */
  if(n==null||n==="") return "—";
  const v=Number(n);
  return isNaN(v)?"—":(Math.round(v*10)/10)+"%";
}
function el(html){ const t=document.createElement("template"); t.innerHTML=html.trim(); return t.content.firstChild; }
function esc(s){ return String(s==null?"":s).replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/"/g,"&quot;").replace(/'/g,"&#39;"); }

/* ---------- 主题切换：自动 / 暗色 / 亮色（三态，localStorage 持久化） ---------- */
const THEME_KEY = "cockpit_theme";
const THEME_LABEL = {auto:"主题：自动", dark:"主题：暗色", light:"主题：亮色"};
function _currentTheme(){
  const r=document.documentElement;
  if(r.classList.contains("theme-dark")) return "dark";
  if(r.classList.contains("theme-light")) return "light";
  return "auto";
}
function applyTheme(t){
  const r=document.documentElement;
  r.classList.toggle("theme-dark", t==="dark");
  r.classList.toggle("theme-light", t==="light");
  const b=document.getElementById("btnTheme");
  if(b){ const svg=(b.querySelector("svg")?b.innerHTML.split("</svg>")[0]+"</svg> ":"") ; b.innerHTML=svg+THEME_LABEL[t]; }
}
(function(){
  let saved;
  try{ saved=localStorage.getItem(THEME_KEY); }catch(e){ saved=null; }
  const cur = (saved==="dark"||saved==="light") ? saved : "auto";
  applyTheme(cur);
  const btn=document.getElementById("btnTheme");
  if(btn){ btn.onclick=function(){
    const next = _currentTheme()==="auto" ? "dark" : _currentTheme()==="dark" ? "light" : "auto";
    try{ if(next==="auto") localStorage.removeItem(THEME_KEY); else localStorage.setItem(THEME_KEY,next); }catch(e){}
    applyTheme(next);
  }; }
})();

/* ---------- SVG 图表 ---------- */
function donut(pctv, color, size=120){
  const r=size/2-12, c=2*Math.PI*r;
  const v=(pctv==null)?0:Math.min(100,Math.max(0,pctv));
  const off=c*(1-v/100);
  const disp=(pctv==null)?"—":pctv+"%";
  return `<svg width="${size}" height="${size}" viewBox="0 0 ${size} ${size}">
    <circle cx="${size/2}" cy="${size/2}" r="${r}" fill="none" stroke="#eef1f7" stroke-width="12"/>
    <circle cx="${size/2}" cy="${size/2}" r="${r}" fill="none" stroke="${color}" stroke-width="12"
      stroke-linecap="round" stroke-dasharray="${c.toFixed(1)}" stroke-dashoffset="${off.toFixed(1)}"
      transform="rotate(-90 ${size/2} ${size/2})"/>
    <text x="${size/2}" y="${size/2-2}" text-anchor="middle" font-size="22" font-weight="800" fill="#1f2733">${disp}</text>
    <text x="${size/2}" y="${size/2+18}" text-anchor="middle" font-size="11" fill="#6b7686">达成率</text>
  </svg>`;
}
function bars(items, opts={}){
  // items: [{name,value,color}]
  const max=Math.max(1,...items.map(i=>i.value));
  return items.map(i=>{
    const w=Math.round(i.value/max*100);
    const col=i.color||"#3b5bdb";
    return `<div class="bar-row"><div class="nm">${i.name}</div>
      <div class="bar-track"><div class="bar-fill" style="width:${w}%;background:${col}"></div></div>
      <div class="vl num">${opts.fmt?opts.fmt(i.value):fmt(i.value)}</div></div>`;
  }).join("");
}
function pie(items,size=150){
  const total=items.reduce((s,i)=>s+i.value,0)||1;
  let ang=-Math.PI/2, parts="";
  items.forEach(i=>{
    const a2=ang+i.value/total*2*Math.PI;
    const x1=size/2+size/2*0.42*Math.cos(ang), y1=size/2+size/2*0.42*Math.sin(ang);
    const x2=size/2+size/2*0.42*Math.cos(a2), y2=size/2+size/2*0.42*Math.sin(a2);
    const large=(a2-ang)>Math.PI?1:0;
    parts+=`<path d="M${size/2} ${size/2} L${x1.toFixed(1)} ${y1.toFixed(1)} A${size/2*0.42} ${size/2*0.42} 0 ${large} 1 ${x2.toFixed(1)} ${y2.toFixed(1)} Z" fill="${i.color}"/>`;
    ang=a2;
  });
  return `<svg width="${size}" height="${size}" viewBox="0 0 ${size} ${size}">${parts}</svg>`;
}

/* ---------- 补录（本地填写 → 复制发回） ---------- */
const BF_KEY = "cockpit_backfill_v1";
function loadOV(){ try{ return JSON.parse(localStorage.getItem(BF_KEY) || "{}"); }catch(e){ return {}; } }
function saveOV(o){ localStorage.setItem(BF_KEY, JSON.stringify(o)); }
function applyBackfillOverrides(m){
  const o = loadOV();
  const bp = o["生产计划"] || {}, ba = o["组装记录"] || {};
  (m.gantt || []).forEach(g => {
    const ov = bp[g.row_id]; if(!ov) return;
    if(ov["计划开始日期"]) g.start = ov["计划开始日期"];
    if(ov["计划完成日期"]) g.end = ov["计划完成日期"];
    if(ov["实际完成日期"]){ g.end = ov["实际完成日期"]; g.done = true; g.progress = 100; g.overdue = false; }
  });
  const vals = [];
  (m.backfill["组装记录"] || []).forEach(a => {
    const ov = ba[a.row_id];
    const v = ov && ov["组装良品率"] !== "" && ov["组装良品率"] !== undefined ? parseFloat(ov["组装良品率"]) : NaN;
    if(!isNaN(v)) vals.push(v);
  });
  if(vals.length) m.quality.asm_yield = Math.round(vals.reduce((s,x)=>s+x,0) / vals.length * 10) / 10;
}
function collectOV(){
  const o = {生产计划:{}, 组装记录:{}};
  document.querySelectorAll("#bfBody [data-rid]").forEach(n => {
    const tbl = n.getAttribute("data-tbl"), rid = n.getAttribute("data-rid"), field = n.getAttribute("data-field");
    const val = n.value;
    o[tbl][rid] = o[tbl][rid] || {};
    o[tbl][rid][field] = val;
  });
  for(const tbl of ["生产计划","组装记录"]){
    for(const rid in o[tbl]){
      const f = o[tbl][rid];
      for(const k in f) if(f[k] === "") delete f[k];
      if(!Object.keys(f).length) delete o[tbl][rid];
    }
  }
  return o;
}
function onBfChange(){
  const o = collectOV(); saveOV(o); render(MODEL);
}
function copyBackfill(){
  const o = loadOV();
  const submit = {}; let cnt = 0;
  for(const tbl of ["生产计划","组装记录"]){
    const t = o[tbl] || {};
    const cleaned = {};
    for(const rid in t){
      const f = {};
      for(const k in t[rid]){ const v = t[rid][k]; if(v !== "" && v !== null && v !== undefined){ f[k] = v; cnt++; } }
      if(Object.keys(f).length) cleaned[rid] = f;
    }
    if(Object.keys(cleaned).length) submit[tbl] = cleaned;
  }
  if(!cnt){ alert("还没填任何值哦～先在上方表格里补录缺失字段，再复制。"); return; }
  const txt = JSON.stringify(submit, null, 2);
  /* 在线模式：直接提交本机服务器存档（data/补录回传.csv），省去复制粘贴；
     离线保持原有复制→发 WorkBuddy 流程。 */
  if(LIVE){
    liveAPI("/api/backfill", {text: txt}, res=>{
      if(res.ok){
        alert("已提交 "+cnt+" 条补录数据 ✅\n\n已存到本机 data/补录回传.csv，跟我说「处理补录回传」即可确认写入 SeaTable。");
      }else{
        alert("提交失败：" + (res.error||"") + "\n已回退为复制模式。");
        const done = () => alert("已复制 " + cnt + " 条补录数据到剪贴板 ✅\n\n把下面这段直接粘贴到 WorkBuddy 对话发给我。");
        if(navigator.clipboard && navigator.clipboard.writeText){ navigator.clipboard.writeText(txt).then(done, () => fallbackCopy(txt, done)); }
        else { fallbackCopy(txt, done); }
      }
    });
    return;
  }
  const done = () => alert("已复制 " + cnt + " 条补录数据到剪贴板 ✅\n\n把下面这段直接粘贴到 WorkBuddy 对话发给我，我会跟你确认后再写入 SeaTable 云端真库。");
  if(navigator.clipboard && navigator.clipboard.writeText){
    navigator.clipboard.writeText(txt).then(done, () => fallbackCopy(txt, done));
  } else { fallbackCopy(txt, done); }
}
function fallbackCopy(txt, cb){
  const ta = document.createElement("textarea"); ta.value = txt; ta.style.position = "fixed"; ta.style.opacity = "0";
  document.body.appendChild(ta); ta.select();
  try { document.execCommand("copy"); cb(); } catch(e){ alert("复制失败，请手动选择下方文本复制：\n\n" + txt); }
  document.body.removeChild(ta);
}
function clearBackfill(){
  if(confirm("确定清空本机已填的补录数据？此操作仅清本地浏览器，不影响云端。")){
    localStorage.removeItem(BF_KEY); location.reload();
  }
}

/* ---------- 渲染 ---------- */
/* ---------- 流程思维导图（XMind → 可折叠 SVG 水平树） ---------- */
const MM={map:0,col:new Set(),q:"",zoom:1};
function mmText(n){ return String(n&&n.t!=null?n.t:""); }
function mmDisp(s){ s=String(s); return s.length>22 ? s.slice(0,22)+"…" : s; }
function mmW(n){ return Math.min(250, Math.max(62, mmText(n).length*12.4+26)); }
function mmIds(map){
  (function w(n,p){ n._id=p; (n.c||[]).forEach((k,i)=>w(k,p+"."+i)); })(map.root,"r");
}
/* 水平树布局：x 按深度，y 按叶子顺序后序回填，父节点取首末子节点中点 */
function mmLayout(root, collapsed){
  const NH=28, STEP=38, XGAP=205, PAD=18;
  let cur=0; const nodes=[];
  (function w(n,d){
    const kids=(n.c && !collapsed.has(n._id)) ? n.c : [];
    n._d=d;
    if(!kids.length){ n._y=cur; cur+=STEP; }
    else { kids.forEach(k=>w(k,d+1)); n._y=(kids[0]._y+kids[kids.length-1]._y)/2; }
    n._x=d*XGAP;
    nodes.push(n);
  })(root,0);
  const links=[];
  (function lk(n){
    const kids=(n.c && !collapsed.has(n._id)) ? n.c : [];
    kids.forEach(k=>{ links.push([n,k]); lk(k); });
  })(root);
  let W=0; nodes.forEach(n=>{ W=Math.max(W, n._x+mmW(n)); });
  return {nodes, links, W, H:Math.max(cur,STEP), NH, PAD};
}
function mmSVG(map, q){
  const L=mmLayout(map.root, MM.col), PAD=L.PAD, NH=L.NH;
  const W=L.W+PAD*2+16, H=L.H+PAD*2;
  const ql=(q||"").toLowerCase();
  let s=`<svg width="${Math.round(W)}" height="${Math.round(H)}" viewBox="0 0 ${Math.round(W)} ${Math.round(H)}">`;
  L.links.forEach(([p,c])=>{
    const px=p._x+mmW(p)+PAD+(p.c&&p.c.length?9:0);
    const y1=p._y+NH/2+PAD, x2=c._x+PAD, y2=c._y+NH/2+PAD, mx=(px+x2)/2;
    s+=`<path class="mm-link" d="M${px},${y1} C${mx},${y1} ${mx},${y2} ${x2},${y2}"/>`;
  });
  L.nodes.forEach(n=>{
    const w=mmW(n), x=n._x+PAD, y=n._y+PAD, d=n._d;
    let fill,stroke,tcol;
    if(d===0){ fill="var(--primary)"; stroke="var(--primary)"; tcol="#ffffff"; }
    else if(d===1){ fill="var(--primary-subtle)"; stroke="var(--primary)"; tcol="var(--primary)"; }
    else { fill="var(--card)"; stroke="var(--line)"; tcol="var(--ink)"; }
    const hit=ql&&mmText(n).toLowerCase().includes(ql);
    s+=`<g class="mm-node${hit?" mm-hit":""}" data-mm="${n._id}">`;
    s+=`<title>${esc(mmText(n))}${n.n?"\n"+esc(n.n):""}</title>`;
    s+=`<rect x="${x}" y="${y}" width="${w}" height="${NH}" rx="7" fill="${fill}" stroke="${stroke}" stroke-width="1"/>`;
    s+=`<text x="${x+11}" y="${y+NH/2+1}" fill="${tcol}">${esc(mmDisp(mmText(n)))}</text>`;
    if(n.c&&n.c.length){
      const cy=y+NH/2, cx=x+w+9, off=MM.col.has(n._id);
      s+=`<circle class="mm-tg" cx="${cx}" cy="${cy}" r="8"/>`;
      s+=`<text class="mm-tg-t" x="${cx}" y="${cy+1}" text-anchor="middle">${off?"+":"–"}</text>`;
    }
    s+=`</g>`;
  });
  return s+`</svg>`;
}
function mountMindmaps(host, model){
  const maps=(model&&model.maps)||[];
  if(!maps.length){
    host.innerHTML=`<div class="empty">未接入思维导图。把 XMind 文件放到 Claw/xmind-download/，运行
      <code>python tools/extract_mindmaps.py</code> 生成 <code>data/思维导图.json</code> 后重跑本页。</div>`;
    return;
  }
  maps.forEach(mmIds);
  if(MM.map>=maps.length) MM.map=0;
  host.innerHTML=
    `<div class="mm-tabs">`+maps.map((mp,i)=>
      `<button class="mm-tab${i===MM.map?" on":""}" data-mm-tab="${i}">${esc(mp.file)}<span class="ct">${mp.nodes}</span></button>`
    ).join("")+`</div>`+
    `<div class="mm-bar">
       <input class="tf-input" id="mmQ" type="text" placeholder="🔍 搜索节点…" value="${esc(MM.q)}">
       <button class="mm-fb" data-mm-act="all">展开全部</button>
       <button class="mm-fb" data-mm-act="l2">折叠到二级</button>
       <button class="mm-fb" data-mm-act="zin">放大 ＋</button>
       <button class="mm-fb" data-mm-act="zout">缩小 －</button>
       <button class="mm-fb" data-mm-act="z100">100%</button>
       <span class="tf-cnt" id="mmCnt"></span>
     </div>`+
    `<div class="mm-stage" id="mmStage"></div>`+
    `<div class="note">导图来自 XMind（业务方维护的流程规范，共 ${maps.length} 张 / ${maps.reduce((a,b)=>a+b.nodes,0)} 个节点）。
      点击节点右侧圆点折叠或展开；搜索可高亮命中节点。</div>`;

  const stage=host.querySelector("#mmStage");
  function draw(){
    const mp=maps[MM.map];
    stage.innerHTML=mmSVG(mp, MM.q);
    stage.querySelector("svg").style.zoom=MM.zoom;
    host.querySelector("#mmCnt").textContent=
      `${mp.file} · ${mp.nodes} 节点 · 深 ${mp.depth}`+(MM.q?` · 命中 ${stage.querySelectorAll(".mm-hit").length}`:"");
  }
  function collapseTo(level){
    MM.col=new Set();
    (function w(n,d){ if(d>=level&&n.c&&n.c.length) MM.col.add(n._id); (n.c||[]).forEach(k=>w(k,d+1)); })(maps[MM.map].root,0);
  }
  host.querySelectorAll("[data-mm-tab]").forEach(b=>b.onclick=()=>{
    MM.map=+b.dataset.mmTab; MM.q="";
    host.querySelectorAll("[data-mm-tab]").forEach(x=>x.classList.remove("on"));
    b.classList.add("on");
    host.querySelector("#mmQ").value="";
    draw();
  });
  host.querySelectorAll("[data-mm-act]").forEach(b=>b.onclick=()=>{
    const a=b.dataset.mmAct;
    if(a==="all") MM.col=new Set();
    else if(a==="l2") collapseTo(2);
    else if(a==="zin") MM.zoom=Math.min(2,+(MM.zoom+0.15).toFixed(2));
    else if(a==="zout") MM.zoom=Math.max(0.4,+(MM.zoom-0.15).toFixed(2));
    else if(a==="z100") MM.zoom=1;
    draw();
  });
  const qi=host.querySelector("#mmQ");
  let qt=null;
  qi.oninput=()=>{
    MM.q=qi.value.trim();
    if(MM.q) MM.col=new Set();               /* 搜索时自动展开，避免命中项被折叠藏起来 */
    clearTimeout(qt); qt=setTimeout(draw,140);
  };
  stage.addEventListener("click",ev=>{
    const g=ev.target.closest(".mm-node"); if(!g) return;
    const id=g.dataset.mm;
    if(!id) return;
    /* 只有带子节点的才响应折叠；据此还原节点引用 */
    const find=(n)=>{ if(n._id===id) return n; for(const k of (n.c||[])){ const r=find(k); if(r) return r; } return null; };
    const n=find(maps[MM.map].root);
    if(!n||!n.c||!n.c.length) return;
    MM.col.has(id)?MM.col.delete(id):MM.col.add(id);
    draw();
  });
  draw();
}
function renderGantt(g, todayStr){
  if(!g.length) return '<div class="empty">甘特图需「生产计划.立项日期」有值；当前 0 条可用</div>';
  const toD=s=>{const [y,m,d]=s.split('-').map(Number);return new Date(y,m-1,d);};
  /* 业主 2026-09-15 新口径：没填「合同交期」的计划**不再沉到底部**，
     改用**历史工期**推算一个交期后照常画在时间轴上（est=true）。
     虚线边框 + 名称后的 ≈ 号与真实交期区分 —— 一个是「我们据此排期」，一个是「客户承诺」，
     同轴可比，但绝不能让人误读成承诺交期。 */
  const isEst=x=>!!x.est;
  const td=toD(todayStr);
  /* 刻度范围把「今日」一并框进来，否则末条计划已交付时今日线会被推到画布之外。
     三状态改造后还要并入 due / due_est —— 合同交期可能**晚于**条尾、推算交期也可能**晚于**合同交期
     （合同比历史惯例更紧时就会这样），漏了任一都会把菱形标记甩出画布。 */
  const allD=g.map(x=>toD(x.start)).concat(g.map(x=>toD(x.end))).concat([td])
    .concat(g.filter(x=>x.due).map(x=>toD(x.due)))
    .concat(g.filter(x=>x.due_est).map(x=>toD(x.due_est)));
  const min=new Date(Math.min.apply(null,allD)), max=new Date(Math.max.apply(null,allD));
  const span=Math.max(1,(max-min)/86400000);
  const pct=d=>((d-min)/86400000)/span*100;
  /* 菱形标记定位用：夹到 0..100，防止极端日期把标记甩出轨道（视觉上宁可贴边也不要消失） */
  const clampPct=v=>Math.max(0,Math.min(100,v)).toFixed(2);
  const ticks=[];
  let cur=new Date(min.getFullYear(),min.getMonth(),1);
  while(cur<=max){
    if(cur>=min){
      const lbl=cur.getFullYear()+'-'+String(cur.getMonth()+1).padStart(2,'0');
      ticks.push(`<div style="position:absolute;left:${pct(cur).toFixed(2)}%;top:0;bottom:0;border-left:1px dashed var(--line)">
        <span style="position:absolute;top:-20px;left:4px;font-size:10.5px;color:var(--sub)">${lbl}</span></div>`);
    }
    cur=new Date(cur.getFullYear(),cur.getMonth()+1,1);
  }
  let tp=pct(td); tp=Math.max(0,Math.min(100,tp));
  const rows=g.map((x,i)=>{
    const s=toD(x.start), e=toD(x.end);
    const left=pct(s), width=Math.max(1.5,pct(e)-pct(s));
    const est=isEst(x);
    const k=x.done?'done':(x.overdue?'overdue':'run');
    /* 进度 = 生产计划表「阶段」列的**实测值**（每晚 19:00 同步后的快照），不再是日期推算；
       已交付恒 100%；阶段认不出时按 0 低报，绝不虚构。 */
    const prog=x.done?100:(x.progress||0);
    const cap=x.done?'已完成':(x.overdue?'逾期':prog+'%');
    const st=Math.round((e-s)/86400000);
    const leftDays=Math.max(0,Math.round((e-td)/86400000));
    const stg=x.stage||"";
    const sn=x.stages?Object.keys(x.stages).length:0;
    const stText=x.stages?Object.keys(x.stages).map(kk=>kk+'×'+x.stages[kk]).join(' · '):'';
    /* ── 三种状态同轴并列（业主 2026-09-15 口径）──
       ① 实际状态：进度条本体（prog 填充 + done/overdue/run 配色），不做额外标记
       ② 合同交期：实心菱形（= 对客户的**承诺**）
       ③ 历史推算交期：空心菱形（= 我们按自家历史工期算的**对标线**）
       两个菱形均按日期定位；合同交期缺失时该菱形不画（不是画在今天，是不画）。 */
    const dueD=x.due?pct(toD(x.due)):null;
    const estD=x.due_est?pct(toD(x.due_est)):null;
    const mkDue=dueD===null?'':`<i class="gantt-ms due" style="left:${clampPct(dueD)}%" title="合同交期 ${x.due}"></i>`;
    const mkEst=estD===null?'':`<i class="gantt-ms est" style="left:${clampPct(estD)}%" title="历史推算交期 ${x.due_est}"></i>`;
    // 合同 vs 推算 的松紧：负数=合同比历史惯例更紧（要盯），正数=更宽松
    const slack=(x.due&&x.due_est)?Math.round((toD(x.due)-toD(x.due_est))/86400000):null;
    return `<div class="gantt-row" data-gi="${i}" data-name="${esc(x.name)}" data-state="${k}" data-est="${est?1:0}">
      <div class="gantt-label" title="${esc(x.name)}">${esc(x.name)}</div>
      <div class="gantt-track">${mkEst}${mkDue}<div class="gantt-bar ${k}${est?' est':''}" style="left:${left.toFixed(2)}%;width:${width.toFixed(2)}%"
        data-i="${i}" tabindex="0" role="button" aria-label="${esc(x.name)} 计划详情">
        <div class="gantt-prog" style="width:${prog}%"></div>
        <span class="gantt-cap">${cap}</span></div></div>
      <span style="display:none" data-tip='{"name":"${esc(x.name)}","status":"${esc(x.status||"")}","start":"${x.start}","end":"${x.end}","est":${est?"true":"false"},"days":"${st}","left":"${x.done?"—":leftDays}","prog":"${prog}","stage":"${esc(stg)}","stages":"${esc(stText)}","sn":"${sn}","due":"${x.due||""}","dueEst":"${x.due_est||""}","slack":"${slack===null?"":slack}"}'></span>
    </div>`;
  }).join("");
  return `<div><div style="position:absolute;left:242px;right:0;top:22px;bottom:0;pointer-events:none">
      ${ticks.join('')}<div class="gantt-today" style="left:${tp.toFixed(2)}%"></div></div>
    ${rows}</div>`;
}
/* 甘特图交互：状态筛选 + 搜索 + 悬停详情卡 + 点击行高亮联动项目清单表 */
function initGanttTools(g, todayStr, toolsEl, ganttEl, estDays){
  if(!toolsEl || !ganttEl || !g.length) return;
  /* 业主 2026-09-15：**「计划中」优先** —— 筛选档也照这个顺序排，最该看的一档放最前。
     「推算交期」是**另一条轴**（交期来源），故用分隔线隔开放最后，计数会与状态档重叠，属预期。 */
  const isEst=x=>!!x.est;
  const stOf=x=>x.done?"done":(x.overdue?"overdue":(String(x.status||"")==="计划中"?"planned":"run"));
  const states=[
    {k:"all",    n:"全部"},
    {k:"planned",n:"计划中"},
    {k:"overdue",n:"逾期"},
    {k:"run",    n:"进行中"},
    {k:"done",   n:"已交付"},
    {k:"est",    n:"推算交期", sep:true},
  ];
  const cnt=k=>k==="all"?g.length:(k==="est"?g.filter(isEst).length:g.filter(x=>stOf(x)===k).length);
  toolsEl.innerHTML=
    `<input class="tf-input" type="text" placeholder="🔍 搜索产品/状态/阶段…">`+
    states.map(s=>`<button class="gantt-fb${s.k==="all"?" on":""}${s.sep?" sep":""}" data-st="${s.k}">${s.n}<span class="ct">${cnt(s.k)}</span></button>`).join("")+
    `<span class="rs-sub" style="margin-left:auto"></span>`;
  const inp=toolsEl.querySelector("input"), info=toolsEl.querySelector(".rs-sub");
  let st="all";
  function apply(){
    const q=inp.value.trim().toLowerCase();
    let shown=0;
    ganttEl.querySelectorAll(".gantt-row").forEach(r=>{
      const okS=st==="all"||(st==="est"?r.dataset.est==="1":r.dataset.state===st);
      let okQ=!q||r.dataset.name.toLowerCase().includes(q);
      if(q&&!okQ){
        const raw=r.querySelector("[data-tip]");
        if(raw){ try{ const t=JSON.parse(raw.getAttribute("data-tip"));
          okQ=((t.status||"")+" "+(t.stage||"")).toLowerCase().includes(q); }catch(e){}
        }
      }
      const show=okS&&okQ; r.classList.toggle("hide",!show); if(show)shown++;
    });
    info.textContent="显示 "+shown+" / "+g.length+" 条";
  }
  inp.oninput=apply;
  toolsEl.querySelectorAll(".gantt-fb").forEach(b=>b.onclick=()=>{
    toolsEl.querySelectorAll(".gantt-fb").forEach(x=>x.classList.remove("on"));
    b.classList.add("on"); st=b.dataset.st; apply();
  });
  apply();
  /* 悬停/键盘焦点 → 详情卡 */
  let tip=document.getElementById("ganttTip");
  if(!tip){ tip=el(`<div class="gantt-tip" id="ganttTip"></div>`); document.body.appendChild(tip); }
  function showTip(i,anchor){
    const raw=anchor.closest(".gantt-row").querySelector("[data-tip]");
    let t=null; try{ t=JSON.parse(raw.getAttribute("data-tip")); }catch(e){}
    if(!t) return;
    /* JSON.parse 出来的 est 是**布尔值**（不是字符串），两边都得认，否则推算条会被当成真实交期 */
    const isEst=(t.est===true||t.est==="true");
    const sn=parseInt(t.sn||"0",10);
    /* 三种状态并列呈现（业主 2026-09-15）：
       ① 实际状态（进度条）② 合同交期（承诺）③ 历史推算交期（对标） */
    const slack=(t.slack===""||t.slack===undefined||t.slack===null)?null:parseInt(t.slack,10);
    let slackTxt="—";
    if(slack!==null){
      if(slack<0) slackTxt=`比历史惯例紧 ${-slack} 天`;
      else if(slack>0) slackTxt=`比历史惯例松 ${slack} 天`;
      else slackTxt="与历史惯例持平";
    }
    tip.innerHTML=`<b>${t.name}</b>
      <div class="tr"><span>实际状态</span><span>${t.status||"—"} · 进度 ${t.prog}%${t.stage?" · "+t.stage:""}</span></div>
      <div class="tr"><span>立项日期</span><span>${t.start}</span></div>
      <div class="tr"><span>① 合同交期</span><span>${t.due||"未填（待收款后回填）"}</span></div>
      <div class="tr"><span>② 历史推算交期</span><span>${t.dueEst||"—"}${isEst?"（本行条长按此画）":""}</span></div>
      ${slack===null?"":`<div class="tr"><span>合同 vs 推算</span><span>${slackTxt}</span></div>`}
      <div class="tr"><span>计划工期</span><span>${t.days} 天</span></div>
      <div class="tr"><span>距交期</span><span>${t.left} 天</span></div>
      <div class="tr"><span>已登记环节</span><span>${sn>0?t.stages:"0 项（台账为空）"}</span></div>
      <div style="margin-top:6px;padding-top:6px;border-top:1px dashed var(--line);color:var(--sub);font-size:11.5px;max-width:240px;line-height:1.5">① 实际进度取生产计划表「阶段」实测值（每晚 19:00 同步）；② 合同交期为客户承诺，实心菱形；③ 推算交期 = 立项日 + 已交付计划中位工期（${estDays?estDays+" 天":"—"}），空心菱形，仅作对标、<b>不回写 SeaTable</b>。</div>`;
    tip.classList.add("show");
    const r=anchor.getBoundingClientRect();
    const tw=tip.offsetWidth||230, th=tip.offsetHeight||140;
    let x=r.left+r.width/2-tw/2, y=r.top-th-8;
    if(y<8) y=r.bottom+8;
    if(x<8) x=8; if(x+tw>window.innerWidth-8) x=window.innerWidth-8-tw;
    tip.style.left=x+"px"; tip.style.top=y+"px";
  }
  ganttEl.addEventListener("mouseover",ev=>{
    const b=ev.target.closest(".gantt-bar"); if(b) showTip(+b.dataset.i,b);
  });
  ganttEl.addEventListener("mouseout",ev=>{
    if(ev.target.closest(".gantt-bar")) tip.classList.remove("show");
  });
  ganttEl.addEventListener("focusin",ev=>{
    const b=ev.target.closest(".gantt-bar"); if(b) showTip(+b.dataset.i,b);
  });
  ganttEl.addEventListener("focusout",()=>tip.classList.remove("show"));
  /* 点击行 → 高亮 + 联动项目清单 */
  ganttEl.addEventListener("click",ev=>{
    const row=ev.target.closest(".gantt-row"); if(!row) return;
    const wasHl=row.classList.contains("hl");
    ganttEl.querySelectorAll(".gantt-row.hl").forEach(r=>r.classList.remove("hl"));
    document.querySelectorAll("#sec-PW .grid .card table tr.hl").forEach(r=>r.classList.remove("hl"));
    if(wasHl) return;
    row.classList.add("hl");
    const nm=row.dataset.name||"";
    document.querySelectorAll("#sec-PW .grid .card:first-child table tbody tr").forEach(r=>{
      if((r.textContent||"").indexOf(nm)>=0) r.classList.add("hl");
    });
  });
}
/* ---------- 角色视图（单文件 + 角色切换 + #role 书签）---------- */
const ROLES={
  // v6：sections 改成「能看哪些区块」，按新 IA 重排。
  // 旧版还带 core/more（首屏 vs 折叠区），那是「一条长滚动页」时代的产物，
  // 现在导航由 MODULES 决定，core/more 已废弃（保留字段不影响，但没有代码再读它）。
  // 老板：只看数据，不含任何写入口（新建/补录均为项目经理职责）
  boss:      {name:"老板",     actions:null,
              sections:["Today","A","WXC","WXM","PW","LC","PT","C","G","PL","P","T","Rs",
                        "Sup","Inv","Mkt","Raw","Q","KM","FC","SH","MM"]},
  // 仓库/采购：非项目经理，不开放「新建」写入口（仅看数据 + 各自作业动作）
  warehouse: {name:"仓库",     actions:["warehouse"],
              sections:["Today","A","P","Inv","WXM"]},
  // 原料行情对采购最有用：决定报价有效期与备货节奏。SH（来源健康）也开给采购：
  // 「行情/库存数据是几天前的」直接决定报价与备货判断。
  purchase:  {name:"采购",     actions:["purchase","market"],
              sections:["Today","A","Sup","Inv","Mkt","Raw","FC","SH","WXM"]},
  // 生产经理：新建生产计划（写「生产计划」表）+ 补录 + 资源排程 + 风险雷达 + 全套台账
  production:{name:"生产经理", actions:["production","warehouse","delivery","resource","wechat"],
              sections:["Today","A","WXC","WXM","WZ","BF","PW","LC","PT","C","G","PL","P","T","Rs",
                        "Sup","Inv","Mkt","Raw","FC","Q","KM","SH","MM"]},
  // 销售：立项职责 → 新建项目（写「项目」表，对应销售立项表单）
  sales:     {name:"销售",     actions:["sales","delivery"],
              sections:["Today","A","WXM","PW","LC","PT","G","WZ","BF","MM"]},
};
const ROLE_ORDER=["boss","production","purchase","warehouse","sales"];
/* hash 参数表：#role=xxx&route=mod/tab —— 两个参数互不干扰 */
function hashParams(){
  const o={};
  (location.hash||"").replace(/^#/,"").split("&").forEach(kv=>{
    if(!kv) return;
    const i=kv.indexOf("=");
    if(i>0) o[kv.slice(0,i)]=kv.slice(i+1);
  });
  return o;
}
function currentRole(){
  const p=hashParams();
  return ROLES[p.role]?p.role:"production";
}
function setHashParam(k,v){
  const p=hashParams(); p[k]=v;
  location.hash=Object.keys(p).filter(x=>p[x]!==""&&p[x]!=null)
    .map(x=>x+"="+p[x]).join("&");
}

/* ════════════════════════════════════════════════════════════════════════════
   v6 信息架构：22 个区块 → 6 个一级模块 + 标签页
   · 分组依据是「问题」不是「表」：成本归项目（同一笔钱），工时归在产（产能），
     供应链独立成模块（有自己的上游节奏）。
   · 区块渲染器一个都没改 —— 只是「住哪儿」变了。
   ════════════════════════════════════════════════════════════════════════════ */
/* 注意：图标占位符必须用反引号 —— Python 替换进来的是带双引号的 <svg class="...">，
   用 "..." 包会把 JS 字符串提前截断（v6 第一次生成就踩了这个坑）。 */
const MODULES=[
  {id:"today",name:"今天",icon:`__IC_NEXT__`,   tabs:[["Today","今天要处理"],["A","行动建议"],["WXC","微信情报台"],["WXM","消息核对台"]]},
  {id:"proj", name:"项目",icon:`__IC_PROJ__`,   tabs:[["PW","总览 & 在制"],["LC","全链案件"],["PT","项目全表"],["C","成本与毛利"]]},
  {id:"wip",  name:"在产",icon:`__IC_GANT__`,   tabs:[["G","甘特图"],["PL","生产计划表"],["P","产线流转"],["T","工时分析"],["Rs","资源负载"]]},
  {id:"sup",  name:"供应",icon:`__IC_SUP__`,    tabs:[["Sup","供应链（采购）"],["Inv","库存预警"],["Mkt","物料行情"],["Raw","原料行情"]]},
  {id:"qual", name:"质量",icon:`__IC_QUAL__`,   tabs:[["Q","质量分析"],["KM","更多指标"]]},
  {id:"ins",  name:"分析",icon:`__IC_INSIGHT__`,tabs:[["FC","风险雷达"],["SH","来源健康"],["MM","思维导图"]]},
];
/* 全局动作：不占标签页，从「⋯ → 新建 / 补录」抽屉打开（复用原区块） */
const DRAWER_TABS=[["WZ","新建项目 / 生产计划"],["BF","补录缺失数据"]];
const DRAWER_ROLES={WZ:["production","sales"],BF:["production","sales"]};
const ALL_TABS=[];
MODULES.forEach(x=>x.tabs.forEach(t=>ALL_TABS.push(t[0])));
DRAWER_TABS.forEach(t=>ALL_TABS.push(t[0]));
const MOD_OF={};
MODULES.forEach(x=>x.tabs.forEach(t=>MOD_OF[t[0]]=x.id));
DRAWER_TABS.forEach(t=>MOD_OF[t[0]]="_drawer");
/* 某个角色能不能看这个区块。Today 恒可见；KM 跟着 Q 走。 */
function tabVisible(ok,key){
  if(key==="Today") return true;
  if(key==="KM") return ok.has("Q");
  return ok.has(key);
}

/* 角标 = 「有几件事在等你」。只有真有事才亮，平日是干净的。 */
function moduleBadge(mod,m,role){
  try{
    if(mod==="today"){
      // 只算「今天要处理」的紧急项。微信 70 条是积压不是今日待办——
      // 之前把它们加在一起显示 "78"，是个会误导人的假指标；积压归到各自的标签页角标上。
      const acts=(m.next_actions||[]).filter(a=>role==="boss"||(ROLES[role].actions||[]).includes(a.cat));
      return acts.filter(a=>a.pri==="高"||a.pri==="中").length;
    }
    if(mod==="sup"){
      const s=m.supply||{}, pd=m.partdb||{};
      return (s.overdue_list||[]).length+(s.inventory_warn||[]).length+(pd.inventory_warn||[]).length;
    }
    if(mod==="wip") return (m.wip||[]).filter(w=>w.overdue).length;
    if(mod==="qual") return (m.quality||{}).repair_overdue||0;
    if(mod==="ins")  return (((m.foresee||{}).backward||{}).act_now||[]).length;
  }catch(e){}
  return 0;
}

/* 当前路由：#…&route=模块/标签 */
function currentRoute(){
  const p=hashParams();
  const seg=(p.route||"").split("/");
  const role=currentRole();
  const ok=new Set(ROLES[role].sections||[]);
  const vis=MODULES.filter(x=>x.tabs.some(t=>tabVisible(ok,t[0])));
  let mod=vis.some(x=>x.id===seg[0])?seg[0]:(vis[0]?vis[0].id:"today");
  const M=vis.find(x=>x.id===mod);
  let tab=(seg[1]&&MOD_OF[seg[1]]===mod&&tabVisible(ok,seg[1]))?seg[1]:null;
  if(!tab){ const f=M.tabs.find(t=>tabVisible(ok,t[0])); tab=f?f[0]:null; }
  return {mod:mod,tab:tab};
}
function setRoute(mod,tab){ setHashParam("route",mod+(tab?"/"+tab:"")); }

/* 外壳：左导航 + 标签条 + 移动端底部栏 */
function buildShell(role,m){
  const ok=new Set(ROLES[role].sections||[]);
  const mods=MODULES.filter(x=>x.tabs.some(t=>tabVisible(ok,t[0])));
  const r=currentRoute();
  const M=mods.find(x=>x.id===r.mod)||mods[0];
  const tabs=M.tabs.filter(t=>tabVisible(ok,t[0]));

  const nav=$("#sideNav");
  if(nav){
    nav.innerHTML=mods.map(x=>{
      const b=moduleBadge(x.id,m,role);
      return `<button class="navitem${x.id===M.id?" active":""}" data-mod="${x.id}">${x.icon}<span>${x.name}</span>${b?`<span class="badge">${b}</span>`:""}</button>`;
    }).join("")
    + `<div class="nav-sep"></div><div class="nav-note">快照 ${m.snapshot}`
    + (m.isDemo?" · <b style='color:var(--amber)'>演示</b>":" · 只读") + `</div>`;
    nav.querySelectorAll(".navitem").forEach(b=>b.onclick=()=>setRoute(b.dataset.mod,null));
  }
  const tb=$("#tabBar");
  if(tb){
    tb.innerHTML=mods.map(x=>{
      const b=moduleBadge(x.id,m,role);
      return `<button class="${x.id===M.id?"active":""}" data-mod="${x.id}">${x.icon}<span>${x.name}</span>${b?`<span class="tb-badge">${b}</span>`:""}</button>`;
    }).join("");
    tb.querySelectorAll("button").forEach(b=>b.onclick=()=>setRoute(b.dataset.mod,null));
  }
  const ts=$("#tabStrip");
  if(ts){
    ts.innerHTML=tabs.map(t=>{
      // 只在有「今天/供应」这类待办语义的标签上加角标，避免到处都是红点
      let badge=0;
      if(t[0]==="Today") badge=moduleBadge("today",m,role);
      else if(t[0]==="WXM") badge=(m.wxmatch||{}).pending_count||0;
      else if(t[0]==="WXC") badge=(m.wechat||{}).pending_count||0;
      else if(t[0]==="Inv") badge=((m.supply||{}).inventory_warn||[]).length;
      else if(t[0]==="Sup") badge=((m.supply||{}).overdue_list||[]).length;
      return `<button class="tab${t[0]===r.tab?" active":""}" role="tab" data-tab="${t[0]}">${t[1]}${badge?`<span class="tb-badge">${badge}</span>`:""}</button>`;
    }).join("");
    ts.querySelectorAll(".tab").forEach(b=>b.onclick=()=>setRoute(M.id,b.dataset.tab));
  }
  return {mod:M.id,tab:r.tab};
}

/* 抽屉：新建 / 补录（复用原区块，不另写一套表单） */
function openDrawer(key){
  const t=DRAWER_TABS.find(x=>x[0]===key);
  if(!t) return;
  const body=$("#drawerBody");
  if(!body) return;
  if(!body.children.length){
    toast("当前角色没有该写入口（仅生产经理 / 销售可用）");
    return;
  }
  $("#drawerTitle").textContent=t[1];
  $("#drawer").classList.add("open");
}
function closeDrawer(){ const d=$("#drawer"); if(d) d.classList.remove("open"); }
/* ── 角色视图：旧版是整行 role-bar（占一整行），v6 收进顶栏下拉 ────── */
function applyRoleChrome(role, unlock){
  const isAdmin = unlock && unlock.level==="admin";
  const keys = isAdmin ? ROLE_ORDER : [role];
  const menu=$("#roleMenu");
  if(menu){
    menu.innerHTML = keys.map(key=>
        `<button class="dd-item${key===role?" active":""}" data-role="${key}">__IC_USER__${ROLES[key].name}</button>`
      ).join("")
      + `<div class="dd-sep"></div><div class="nav-note">`
      + (isAdmin
          ? `管理员：可切换任意视图。每人开一个标签页钉住自己的视图即独立窗口；敏感财务仅老板可见。`
          : `本视图已用口令锁定在「${ROLES[role].name}」，如需切换其他视图，请用管理员口令重新打开。`)
      + `</div>`;
    menu.querySelectorAll(".dd-item[data-role]").forEach(b=>b.onclick=()=>{
      closeMenus();
      if(isAdmin && b.dataset.role!==role) location.hash="role="+b.dataset.role;
      else if(!isAdmin) toast("本视图已按口令锁定在「"+ROLES[role].name+"」");
    });
  }
  const rn=$("#roleName"); if(rn) rn.textContent=ROLES[role].name;
  const sh=$("#btnShare");
  if(sh) sh.onclick=()=>{ closeMenus(); shareRole(role); };
  const sl=$("#shareLabel");
  if(sl) sl.textContent=(role==="boss"||role==="sales")?("分享给"+ROLES[role].name):"分享本视图";
  const kb=$("#pwManageBtn");
  if(kb){ kb.style.display=isAdmin?"":"none"; kb.onclick=()=>{ closeMenus(); openPwModal(); }; }
  const rb=$("#roleBtn");
  if(rb) rb.onclick=e=>{ e.stopPropagation(); toggleMenu("#roleMenu"); };
}
/* 顶栏下拉开关 */
function toggleMenu(sel){
  const m=$(sel); if(!m) return;
  const willOpen=!m.classList.contains("open");
  closeMenus();
  if(willOpen){
    m.classList.add("open");
    const b=$("#btnMore"); if(sel==="#moreMenu"&&b) b.setAttribute("aria-expanded","true");
  }
}
function closeMenus(){
  document.querySelectorAll(".dd-menu.open").forEach(m=>m.classList.remove("open"));
  const b=$("#btnMore"); if(b) b.setAttribute("aria-expanded","false");
}
function supplierAvg(s){
  const rs=(s.supplier||[]).map(x=>x.rate).filter(x=>x!=null);
  return rs.length? rs.reduce((a,b)=>a+b,0)/rs.length : 0;
}
/* ════════════════════════════════════════════════════════════════════════════
   v7 图表工具箱
   为什么不用图表库：本驾驶舱是**单文件离线** HTML（可发微信、断网可开），
   引 CDN 会立刻破坏这个前提。所以用 CSS 布局 + 内联 SVG 自己画，
   够用的就那 4 种图（横条 / 构成条 / 迷你走势 / 环），正好卡在行业规范建议的
   「同一屏图表类型 ≤4 种」以内。
   ════════════════════════════════════════════════════════════════════════════ */

/* 语义阈值。**行业惯例 + 本项目历史基线**，不是拍脑袋：
   · 达成率 ≥90 绿 / 80~90 黄 / <80 红  —— MES 看板规范
   · 良率   ≥98 绿 / 95~98 黄 / <95 红  —— 同上
   · 维修率、逾期数、缺料数 → 越小越好（lowerBetter）
   业主想改口径，只动这一张表。 */
const THRESH={
  ontime_rate:[90,80], exec_rate:[80,50], smt_yield:[98,95], asm_yield:[98,95],
  margin:[25,10], kit_rate:[98,90],
  repair_rate:[2,5], purchase_overdue:[0,2], shortage:[0,2], res_over:[0,1], zero_stock:[0,3],
  sup_delay:[3,7], res_load:[85,100],
};
function sev(v,key,lowerBetter){
  if(v==null||isNaN(v)) return "";
  const t=THRESH[key]||[90,80];
  if(lowerBetter) return v<=t[0]?"ok":(v<=t[1]?"warn":"bad");
  return v>=t[0]?"ok":(v>=t[1]?"warn":"bad");
}
const SEV_HEX={ok:"var(--green)",warn:"var(--amber)",bad:"var(--red)"};
function sevColor(s){ return SEV_HEX[s]||"var(--primary)"; }

/* 构成条配色：固定顺序，同一含义**永远同一个颜色**（跨页一致才好记） */
const PALETTE=["#4f46e5","#0284c7","#0d9488","#059669","#d97706","#7c3aed","#dc2626","#64748b"];

/* 迷你走势（内联 SVG）。preserveAspectRatio=none 拉宽，靠 non-scaling-stroke 保住线宽。
   ⚠️ 末点标记用**竖线 path** 而不是 <circle>：preserveAspectRatio=none 会把圆
   沿 x 轴拉成椭圆，而 vector-effect 只管描边宽度、救不了圆的形变。 */
function spark(vals,opt){
  opt=opt||{};
  const a=(vals||[]).map(Number).filter(v=>isFinite(v));
  if(a.length<2) return "";
  const w=100,h=22,p=2;
  const mn=Math.min.apply(null,a), mx=Math.max.apply(null,a), rng=(mx-mn)||1;
  const pts=a.map((v,i)=>[p+i*(w-p*2)/(a.length-1), h-p-(v-mn)/rng*(h-p*2)]);
  const d=pts.map((q,i)=>(i?"L":"M")+q[0].toFixed(2)+" "+q[1].toFixed(2)).join(" ");
  const area=d+" L "+pts[a.length-1][0].toFixed(2)+" "+h+" L "+pts[0][0].toFixed(2)+" "+h+" Z";
  const c=opt.color||"var(--primary)";
  const last=pts[a.length-1];
  return `<svg class="spark" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none" aria-hidden="true">
    <path d="${area}" fill="${c}" opacity=".13"/>
    <path d="${d}" fill="none" stroke="${c}" stroke-width="1.6" vector-effect="non-scaling-stroke"
      stroke-linejoin="round" stroke-linecap="round"/>
    <path d="M ${last[0].toFixed(2)} ${(last[1]-3.5).toFixed(2)} L ${last[0].toFixed(2)} ${(last[1]+3.5).toFixed(2)}"
      fill="none" stroke="${c}" stroke-width="2" stroke-linecap="round" vector-effect="non-scaling-stroke"/>
  </svg>`;
}

/* 环比角标：good 方向可反转（维修率下降是好事） */
function trendPill(delta,lowerBetter,unit){
  if(delta==null||isNaN(delta)) return "";
  if(Math.abs(delta)<0.05) return `<span class="tp flat">持平</span>`;
  const up=delta>0, good=lowerBetter?!up:up;
  return `<span class="tp ${good?"good":"bad"}">${up?"↑":"↓"}${Math.abs(delta).toFixed(1)}${unit||"%"} 环比</span>`;
}

/* 条形榜（Pareto）：一眼看出"最大的那块是什么" */
function barList(items,opt){
  opt=opt||{};
  if(!items||!items.length) return `<div class="viz-empty">暂无数据</div>`;
  const max=Math.max.apply(null,items.map(x=>Math.abs(x.v)||0))||1;
  return `<div class="bl">`+items.map(x=>{
    const w=Math.max(2,Math.round(Math.abs(x.v)/max*100));
    const s=(x.sev!==undefined&&x.sev!==null)?x.sev:((opt.sev&&opt.sev(x.v))||"");
    return `<div class="bl-row">
      <span class="bl-lbl" title="${esc(x.l)}">${esc(x.l)}</span>
      <span class="bl-val">${x.txt!==undefined?x.txt:fmt(x.v)}</span>
      <span class="bl-track"><i class="bl-fill${s?" s-"+s:""}" style="width:${w}%"></i></span>
    </div>`;
  }).join("")+`</div>`;
}

/* 子弹图：实际值 vs 目标值。行业规范原话 ——
   "a count of 47 parts means nothing without knowing the target was 60"
   ⚠️ target==null 的行**不画轨道**：没有目标就没有「达成多少」可言，
   硬画一条满格灰带会被误读成一个进度。只留一行「标签 — 数值」。 */
function bullet(rows,opt){
  opt=opt||{};
  if(!rows||!rows.length) return `<div class="viz-empty">暂无数据</div>`;
  return `<div>`+rows.map(r=>{
    const txt=`${r.txt!==undefined?r.txt:fmt(r.v)}${r.unit||""}`;
    const c=sevColor(r.sev||"");
    if(r.target==null){
      return `<div class="bt bt-plain">
        <div class="bt-head"><span class="bt-l" title="${esc(r.l)}">${esc(r.l)}</span>
        <span class="bt-v" style="color:${r.sev?c:"var(--ink)"}">${txt}</span></div>
      </div>`;
    }
    const max=r.max||Math.max(r.v||0,r.target||0)*1.12||1;
    const pw=Math.max(0,Math.min(100,(r.v||0)/max*100));
    const mw=Math.max(0,Math.min(100,r.target/max*100));
    return `<div class="bt">
      <div class="bt-head">
        <span class="bt-l" title="${esc(r.l)}">${esc(r.l)}</span>
        <span class="bt-v" style="color:${c}">${txt}<em> / 目标 ${fmt(r.target)}${r.unit||""}</em></span>
      </div>
      <div class="bt-track">
        <i class="bt-fill" style="width:${pw}%;background:${c}"></i>
        <i class="bt-mark" style="left:${mw}%" title="目标 ${fmt(r.target)}"></i>
      </div>
      ${opt.scale?`<div class="bt-scale"><span>0</span><span>${fmt(max)}${opt.unit||""}</span></div>`:""}
    </div>`;
  }).join("")+`</div>`;
}

/* 构成条：成本结构 / 阶段分布 —— 用 donut 会浪费空间，横条更好读 */
function stackBar(items,opt){
  opt=opt||{};
  const it=(items||[]).filter(x=>(x.v||0)>0);
  if(!it.length) return `<div class="viz-empty">暂无数据</div>`;
  const tot=it.reduce((a,b)=>a+b.v,0)||1;
  const segs=it.map((x,i)=>({l:x.l,v:x.v,c:opt.palette?opt.palette[i%opt.palette.length]:PALETTE[i%PALETTE.length],p:x.v/tot*100}));
  return `<div class="stk">`+segs.map(s=>
      `<i style="width:${s.p}%;background:${s.c}" title="${esc(s.l)} ${fmt(s.v)}（${s.p.toFixed(1)}%）"></i>`
    ).join("")+`</div>
    <div class="legend">`+segs.map(s=>
      `<span><i style="background:${s.c}"></i>${esc(s.l)}<b>${opt.money?yuan(s.v):fmt(s.v)}</b>
        <span style="color:var(--sub)">${s.p.toFixed(0)}%</span></span>`
    ).join("")+`</div>`;
}

/* 良率环：单一百分比指标的最省空间画法 */
function ring(pct,opt){
  opt=opt||{};
  const v=Math.max(0,Math.min(100,Number(pct)||0));
  const r=26,c=2*Math.PI*r,on=v/100*c;
  const col=opt.color||sevColor(opt.sev||"");
  const size=opt.size||74;
  return `<svg viewBox="0 0 64 64" style="width:${size}px;height:${size}px" role="img"
      aria-label="${opt.label||""} ${v.toFixed(1)}%">
    <circle cx="32" cy="32" r="${r}" fill="none" stroke="var(--subbg3)" stroke-width="7"/>
    <circle cx="32" cy="32" r="${r}" fill="none" stroke="${col}" stroke-width="7" stroke-linecap="round"
      stroke-dasharray="${on.toFixed(2)} ${(c-on).toFixed(2)}" transform="rotate(-90 32 32)"/>
    <text x="32" y="33" text-anchor="middle" dominant-baseline="central" font-size="15" font-weight="600"
      fill="var(--ink)">${v.toFixed(0)}%</text>
  </svg>`;
}

/* 统计卡（Tremor / shadcn 的 KPI 卡解剖）：标签 → 大数字 → 走势 → 目标进度 → 备注
   · sev  = 这张卡"要不要看"（左侧圆点色 + 默认的进度条色）
   · nsev = 这个数字"本身好不好"（大数字的颜色）
   两者分开是必要的：有些卡是**复合**的 —— 例如「在制单数 9」，
   9 本身无所谓好坏，红点是「其中 5 单已逾期」。若共用一个 sev，
   就会把一个中性的 9 染成红色，等于谎报。 */
function statCard(o){
  const s=o.sev||"";
  const ns=(o.nsev!==undefined?o.nsev:s);
  /* ⚠️ 传进来的 cls 是老命名的裸类名（"neg"/"pos"），而本卡的 CSS 定义在
     `.stat .sv b.s-neg` 下。不补 s- 前缀的话 class 会落成 `neg`，
     规则匹配不上 —— 表现就是「在产缺料 5」该红不红，一直黑着。 */
  const rawCls=o.cls||"";
  const vcls=rawCls?(/^s-/.test(rawCls)?rawCls:"s-"+rawCls):(ns?"s-"+ns:"");
  return `<div class="stat">
    <div class="sl"><i style="background:${sevColor(s)}"></i>${esc(o.l)}</div>
    <div class="sv"><b class="${vcls}">${o.v}</b>${o.u?`<em>${o.u}</em>`:""}${o.tp||""}</div>
    ${o.spark||""}
    ${o.pct!=null?`<div class="bar"><i style="width:${Math.max(0,Math.min(100,o.pct))}%;background:${sevColor(s)}"></i></div>`:""}
    <div class="sf"><span>${o.sub||""}</span>${o.tgt?`<span class="tgt">${o.tgt}</span>`:""}</div>
  </div>`;
}

/* 图表卡外壳。
   ⚠️ 外层用 <section> 而不是 <div>：全站既有的区块间距规则写在
   `.pane>section{margin-top:0}` 和 `.pane>section+section{margin-top:22px}` 上，
   换成 div 会丢掉间距，图表卡会紧贴上一块。放进 .viz-grid 时靠
   `.viz-grid>.viz{margin-top:0}` 把间距归零（该规则在同优先级里排后，胜出）。 */
function vizCard(title,body,opt){
  opt=opt||{};
  return `<section class="viz">
    <div class="viz-hd"><h3>${title}</h3>${opt.q?`<span class="q">${opt.q}</span>`:""}</div>
    ${body}
    ${opt.note?`<div class="viz-note">${opt.note}</div>`:""}
  </section>`;
}

/* 按月聚合「日期列 + 数值列」（项目全表的 签订日期/合同总价 等）。
   ⚠️ 只输出**真有数据的月**；不足 2 个月时 spark() 自己返回空串 —— 宁可没有走势，
   也不画一条两点直线假装是趋势。 */
function monthlyAgg(rows,dateKey,valKey){
  const by={};
  (rows||[]).forEach(r=>{
    const k=String(r[dateKey]||"").slice(0,7);
    if(!/^\d{4}-\d{2}$/.test(k)) return;
    const v=parseFloat(String(r[valKey]==null?"":r[valKey]).replace(/[^0-9.\-]/g,""));
    if(isFinite(v)) by[k]=(by[k]||0)+v;
  });
  return Object.keys(by).sort().map(k=>by[k]);
}

function buildKPIs(m, role){
  const k=m.kpi, t=m.time, q=m.quality, s=m.supply, pd=m.partdb, res=m.resource;
  const wx=m.wechat, mk=m.market, cm=m.commodities, fs=m.foresee;
  const b=pd?pd.bom:null;
  const pf=m.projects_full||[];
  /* ── 三条**真实**序列，全部来自库里已有字段，没有一条是编的 ──────────────
     · gm   : 甘特 35 条按「结束月」聚合（2026-04~2026-10，7 个点）
     · sign : 项目全表按「签订日期」聚合的月度合同额（2025-07~2026-09，8 个点）
     · age  : 应收账龄 4 桶（逾期/30/60/90）—— 是分布不是时间序列，
              所以只喂给 barList，**不**当走势画（把桶当时间轴是骗人的） */
  const gm=ganttMonthly(m.gantt);
  const sign=monthlyAgg(pf,"签订日期","合同总价");
  const age=(m.cost&&m.cost.cashflow)||[];
  const shortageN=b?b.shortage.length:0;
  const kitRate=(b&&b.bom_count)?((b.bom_count-shortageN)/b.bom_count*100):null;
  const supAvg=supplierAvg(s);
  const rawAlerts=cm?cm.alerts.length:0;
  /* 供应商到货延迟（加权）：来自 foresee.supplier 的 106 个历史样本。
     原「供应商准时率」依赖 supply.supplier，而真实库里那张表是**空的** →
     旧写法会显示 0%，把「没有样本」误报成「准时率为零」。改成有样本才出数。 */
  let delayNum=0,delayDen=0;
  if(fs&&fs.supplier&&fs.supplier.cat_profile){
    Object.keys(fs.supplier.cat_profile).forEach(c=>{
      const p=fs.supplier.cat_profile[c];
      if(p&&p.n){ delayNum+=p.n*(p.mean||0); delayDen+=p.n; }
    });
  }
  const supDelay=delayDen?(delayNum/delayDen):null;
  const M={
    /* ── 经营 ─────────────────────────────────────────────────────── */
    projects:{l:"项目总数",v:fmt(k.projects),u:"个",
      sub:`进行中 ${k.active} · 计划 ${k.planned} · 完成 ${k.done}`,
      pct:k.projects?k.done/k.projects*100:null,tgt:`完成 ${k.done}/${k.projects}`},
    contract:{l:"总合同额",v:yuan(k.contract),
      sub:`已收 ${yuan(k.received)} · 执行率 ${pct(k.exec_rate)}`,
      sev:sev(k.exec_rate,"exec_rate"),pct:k.exec_rate,tgt:"目标执行率 80%",
      spark:spark(sign,{color:"var(--primary)"})},
    received:{l:"已收金额",v:yuan(k.received),
      sub:`${m.projects.filter(p=>p.received>0).length} 个项目有回款`,
      sev:sev(k.exec_rate,"exec_rate"),pct:k.exec_rate,tgt:`占合同 ${pct(k.exec_rate)}`},
    receivable:{l:"应收款",v:yuan(k.receivable),cls:"neg",
      sub:`表内待收 ${yuan((m.recv_check||{}).stored)}`,
      tgt:`逾期 ${yuan(age.length?age[0].in:0)}`,
      pct:k.contract?k.receivable/k.contract*100:null,
      spark:""},
    cost:{l:"生产总花销",v:yuan(k.cost),
      sub:`台账口径 ${yuan(k.ledger_cost)}`,
      sev:sev(k.margin,"margin"),pct:k.contract?k.cost/k.contract*100:null,
      tgt:`占合同 ${pct(k.contract?k.cost/k.contract*100:0)}`},
    margin:{l:"毛利率",v:pct(k.margin),
      sub:`利润 ${yuan(k.profit)}`,
      sev:sev(k.margin,"margin"),pct:k.margin,tgt:"目标 30%"},
    unit_cost:{l:"单片成本",v:yuan(k.unit_cost),sub:"台账均单价"},
    exec_rate:{l:"合同执行率",v:pct(k.exec_rate),sub:"已收 / 合同",
      sev:sev(k.exec_rate,"exec_rate"),pct:k.exec_rate,tgt:"目标 80%"},
    /* ── 交付 ─────────────────────────────────────────────────────── */
    ontime_rate:{l:"交期达成率",v:pct(k.ontime_rate),
      sub:t.dated?`实际耗时 ≤ 允许周期 ${t.ontime}/${t.dated}`:"无可判定样本",
      sev:sev(k.ontime_rate,"ontime_rate"),pct:k.ontime_rate||0,tgt:"目标 90%",
      spark:spark(gm.ontime,{color:"var(--green)"})},
    wip:{l:"在制单数",v:fmt(k.wip),u:"单",
      sub:`逾期 ${(m.gantt||[]).filter(x=>x.overdue).length} 单`,
      /** 圆点报警（有逾期→红），但 9 这个数字本身中性，不染色 */
      sev:sev((m.gantt||[]).filter(x=>x.overdue).length,"res_over",true),nsev:"",
      tgt:`共 ${t.total} 条计划`},
    cycle:{l:"平均生产周期",v:(t.avg_cycle||0)+"天",
      sub:`样本 ${(t.cycle_list||[]).length} 条 · 中位 ${(m.gantt_hist||{}).median||"—"} 天`,
      pct:150/(t.avg_cycle||1)*100, tgt:`历史中位 ${(m.gantt_hist||{}).median||"—"} 天`,
      spark:spark(gm.cycle,{color:"var(--blue,var(--primary))"})},
    /* ── 供应 ─────────────────────────────────────────────────────── */
    purchase_overdue:{l:"采购逾期",v:fmt(k.purchase_overdue),u:"单",
      sub:k.purchase_overdue?"需跟进":"全部在期内",
      sev:sev(k.purchase_overdue,"purchase_overdue",true),tgt:"目标 0 单"},
    shortage:{l:"在产缺料",v:fmt(shortageN),u:"种",
      sub:`零确认 ${pd?pd.zero_confirmed:0} 种 · 共 ${(b&&b.shortage.length)?b.shortage.reduce((a,x)=>a+(x.gap||0),0):0} 件`,
      sev:sev(shortageN,"shortage",true),cls:shortageN?"neg":"",
      tgt:b?`BOM ${b.bom_count} 行`:"PartDB 未配置"},
    kit_rate:{l:"物料齐套率",v:kitRate==null?"—":pct(kitRate),
      sub:b?`缺 ${shortageN} / 共 ${b.bom_count} 行`:"PartDB 未配置",
      sev:kitRate==null?"":sev(kitRate,"kit_rate"),pct:kitRate,tgt:"目标 98%"},
    part_count:{l:"在库料号",v:fmt(pd?pd.part_count:0),u:"种",sub:"零件总数"},
    zero_stock:{l:"零确认库存",v:fmt(pd?pd.zero_confirmed:0),u:"种",sub:"需盘点",
      sev:sev(pd?pd.zero_confirmed:0,"zero_stock",true),cls:(pd&&pd.zero_confirmed>0)?"neg":""},
    supplier_ontime:{l:"供应商准时率",v:s.supplier.length?pct(supAvg):"—",
      sub:s.supplier.length?`${s.supplier.length} 家供应商`:"采购记录暂无交期回填，无法计算",
      sev:supAvg?sev(supAvg,"ontime_rate"):"",pct:s.supplier.length?supAvg:null,
      tgt:s.supplier.length?"目标 90%":"待补录到货日期"},
    sup_delay:{l:"供应商到货延迟",v:supDelay==null?"—":(supDelay.toFixed(1)+"天"),
      sub:delayDen?`${delayDen} 个历史样本加权平均`:"无样本",
      sev:supDelay==null?"":sev(supDelay,"sup_delay",true),tgt:"目标 ≤3 天"},
    /* ── 质量 ─────────────────────────────────────────────────────── */
    smt_yield:{l:"贴片良品率",v:pct(q.smt_yield),
      sub:`${q.smt_batches||0}/${q.smt_rows||0} 批已录${q.smt_missing_n?` · ${q.smt_missing_n} 批待补录`:""}`,
      sev:sev(q.smt_yield,"smt_yield"),pct:q.smt_yield,tgt:"目标 98%"},
    asm_yield:{l:"组装良品率",v:q.asm_yield==null?"—":pct(q.asm_yield),
      sub:q.asm_yield==null?"组装记录未填良品率":"组装记录均值",
      sev:q.asm_yield==null?"":sev(q.asm_yield,"asm_yield"),
      pct:q.asm_yield,tgt:"目标 98%"},
    repair_rate:{l:"维修率",v:pct(q.repair_rate),
      sub:`维修 ${q.repair_total} / 发货 ${q.shipped} · 平均 ${q.repair_avg} 天`,
      sev:sev(q.repair_rate,"repair_rate",true),pct:q.repair_rate,
      cls:q.repair_rate>5?"neg":"",tgt:"目标 ≤2%"},
    repair_overdue:{l:"超期未完修",v:fmt(q.repair_overdue),u:"单",
      sub:"需跟进",sev:sev(q.repair_overdue,"res_over",true),cls:q.repair_overdue?"neg":""},
    /* ── 资源（未启用时整组隐藏） ─────────────────────────────────── */
    res_load:{l:"资源平均负载",v:res?pct(res.avg_load):"—",
      sub:res?`在岗 ${res.on_duty} 人/台`:"未启用资源管理",
      /* 负载不是单调指标：<70 闲置、70~100 健康、>100 超载。
         所以不走 sev() 的两档阈值，直接按三档写死。 */
      sev:res?(res.avg_load>100?"bad":(res.avg_load>85?"warn":"ok")):"",
      pct:res?res.avg_load:null,tgt:res?"健康 70~100%":""},
    res_over:{l:"超载资源",v:fmt(res?res.over.length:0),u:"项",
      sub:res?`共 ${res.total} 项资源`:"未启用",
      sev:res?sev(res.over.length,"res_over",true):"",cls:(res&&res.over.length)?"neg":""},
    res_conflict:{l:"排程冲突",v:fmt(res?res.conflicts.length:0),u:"处",
      sub:"同人同期多任务",sev:res?sev(res.conflicts.length,"res_over",true):"",
      cls:(res&&res.conflicts.length)?"neg":""},
    labor_cost:{l:"人工成本",v:res?yuan(res.labor_cost):"—",sub:"投入量 × 日费率"},
    /* ── 情报 ─────────────────────────────────────────────────────── */
    market_alert:{l:"物料行情预警",v:fmt(mk?mk.alerts.length:0),u:"条",
      sub:mk?`监控 ${mk.watch_count} 个料号 · 阈值 ±${mk.threshold}%`:"未启用行情监控",
      sev:mk?sev(mk.alerts.length,"purchase_overdue",true):"",
      cls:(mk&&mk.alerts.length)?"neg":""},
    raw_alert:{l:"原料波动告警",v:fmt(rawAlerts),u:"条",
      sub:cm?`监控 ${cm.rows.length} 个品种 · 阈值 ±${cm.threshold}%`:"未启用原料监控",
      sev:sev(rawAlerts,"shortage",true),
      cls:rawAlerts?"neg":"",
      spark:cm&&cm.rows.length?spark((cm.rows[0].hist||[]).map(Number),{color:"var(--amber)"}):""},
    wechat_pending:{l:"微信待确认",v:fmt(wx?wx.pending_count:0),u:"条",
      sub:wx?`今日新事件 ${wx.today_count} · 近 7 天 ${Object.values(wx.by_cat||{}).reduce((a,b)=>a+b,0)} 条`:"未接入微信情报",
      cls:(wx&&wx.pending_count)?"neg":"",tgt:"核对后写入业务表"},
    wxmatch_pending:{l:"消息核对待办",v:fmt(m.wxmatch?m.wxmatch.pending_count:0),u:"条",
      sub:m.wxmatch?`收款 ${m.wxmatch.by_type.收款||0} · 下单 ${m.wxmatch.by_type.下单||0}`:"未启用核对台",
      cls:(m.wxmatch&&m.wxmatch.pending_count)?"neg":""},
  };
  // 首屏只留最多 4 张「一眼定生死」的指标，其余下沉到「更多分析 → 更多指标」
  const sets={
    boss:      ["contract","receivable","margin","ontime_rate"],
    warehouse: ["shortage","kit_rate","zero_stock","part_count"],
    purchase:  ["purchase_overdue","sup_delay","shortage","ontime_rate"],
    production:["wip","ontime_rate","shortage",res?"res_over":"smt_yield"],
    sales:     ["contract","received","receivable","ontime_rate"],
  };
  const setsMore={
    boss:      ["projects","cost","ontime_rate","unit_cost","exec_rate","wip","smt_yield","repair_rate"]
                 .concat(res?["res_load","labor_cost"]:[])
                 .concat(mk?["market_alert"]:[]).concat(cm?["raw_alert"]:[])
                 .concat(wx?["wechat_pending"]:[]),
    warehouse: ["projects"],
    purchase:  ["projects","supplier_ontime"].concat(mk?["market_alert"]:[]).concat(cm?["raw_alert"]:[]),
    production:["smt_yield","asm_yield","repair_rate","cycle","kit_rate","sup_delay"]
                 .concat(res?["res_load","res_conflict","labor_cost"]:[])
                 .concat(wx?["wechat_pending"]:[]).concat(m.wxmatch?["wxmatch_pending"]:[]),
    sales:     ["projects","wip","exec_rate","wxmatch_pending"],
  };
  const pick=(arr)=>(arr||[]).map(key=>M[key]).filter(Boolean);
  return {core:pick(sets[role]||sets.boss), more:pick(setsMore[role])};
}
function kpiCard(x){
  return el(statCard(x));
}
/* 从甘特数据里按月聚合出三条**真实**序列（不编造环比）。
   旧版 KPI 只有裸数字，"9 单" 是什么水平完全看不出来。 */
function ganttMonthly(g){
  const by={};
  (g||[]).forEach(x=>{
    const d=String(x.end||x.start||"").slice(0,7);
    if(!/^\d{4}-\d{2}$/.test(d)) return;
    const b=by[d]||(by[d]={n:0,days:0,cnt:0,ontime:0});
    b.n++;
    if(x.days){ b.days+=x.days; b.cnt++; }
    if(x.done&&!x.overdue) b.ontime++;
  });
  const k=Object.keys(by).sort();
  return {
    months:k,
    count:k.map(m=>by[m].n),
    cycle:k.map(m=>by[m].cnt?+(by[m].days/by[m].cnt).toFixed(1):null),
    ontime:k.map(m=>by[m].n?+(by[m].ontime/by[m].n*100).toFixed(1):null),
  };
}

function render(m){
  applyBackfillOverrides(m);
  _navIO&&_navIO.disconnect();
  const role=currentRole();
  // 权限校验：非管理员且当前角色不是自己解锁的角色 → 越权，锁定回授权角色
  if(UNLOCK && UNLOCK.level!=="admin" && role!==UNLOCK.role){
    toast("无权查看「"+ROLES[role].name+"」视图，已锁定在「"+ROLES[UNLOCK.role].name+"」");
    location.hash="role="+UNLOCK.role;
    return;
  }
  const ok=new Set(ROLES[role].sections||[]);
  const app=$("#app"); app.innerHTML="";
  const drawerBody=$("#drawerBody"); if(drawerBody) drawerBody.innerHTML="";

  /* ── 挂载器 v6 ────────────────────────────────────────────────────────
     旧版：core → 首屏平铺；more → 塞进「更多分析」折叠区（于是变成一条 16 屏长纸）。
     新版：区块 → 它所属的那个「标签面板」。区块渲染代码一行没改。 */
  const PANES={};
  const put=(key,node)=>{
    const mod=MOD_OF[key];
    if(!mod) return;                                     // 未纳管的区块直接丢弃
    if(mod==="_drawer"){                                  // 新建 / 补录：进抽屉
      if(drawerBody && (DRAWER_ROLES[key]||[]).includes(role)) drawerBody.appendChild(node);
      return;
    }
    if(!tabVisible(ok,key)) return;                       // 该角色无权看
    let pane=PANES[key];
    if(!pane){
      pane=el(`<div class="pane" id="pane-${key}" role="tabpanel"></div>`);
      PANES[key]=pane; app.appendChild(pane);
    }
    pane.appendChild(node);
  };

  $("#metaLine").textContent=m.snapshot
    + (m.synced_at?" · 业务表同步 "+m.synced_at:"")
    + (m.partdb?" · PartDB 物料 "+m.partdb.generated_at:"");
  const flag=$("#demoFlag");
  if(m.isDemo){
    flag.textContent="演示数据"; flag.className="demo-flag"; flag.style.display="inline-block";
  }else{
    flag.style.display="none";
  }
  if(m.isDemo){
    const bn=$("#banner");
    if(bn){ bn.style.display="block"; bn.innerHTML="当前为<b>演示数据</b>（虚构示例）。清空本地 data/ 后录入真实数据，重跑 cockpit.py 即可生成你的真实驾驶舱。"; }
  }
  applyRoleChrome(role, UNLOCK);

  /* ── KPI 常驻条：所有标签页顶部都在，不再随页面滚走 ─────────────────── */
  const k=m.kpi;
  const kpiG=buildKPIs(m, role);
  const kpiBar=$("#kpiBar");
  if(kpiBar){ kpiBar.innerHTML=""; kpiG.core.forEach(x=>kpiBar.appendChild(kpiCard(x))); }

  /* 外壳（左导航 + 标签条 + 底部栏）；必须在区块之前建好，才能算出当前标签 */
  const shell=buildShell(role, m, kpiG.core.length===0);

  /* 今天要处理：首屏置顶，只留高/中优先级，一键跳到对应模块 */
  const actsAll=m.next_actions||[];
  const acts=(role==="boss")?actsAll:actsAll.filter(a=>(ROLES[role].actions||[]).includes(a.cat));
  // 每类事项按「候选模块」顺序找第一个当前角色可见的模块，保证「去处理」按钮总有落点
  const CAT_SEC={purchase:["Sup","Inv","P"],warehouse:["Inv","P","Sup"],
    delivery:["PW","P","G"],production:["P","G","T","PW"],sales:["PW","G"],boss:["PW","G"],
    resource:["Rs","G","T"],wechat:["WXC"],market:["Mkt"],risk:["FC"]};
  const visible=(kk)=>tabVisible(ok,kk);
  const urgent=acts.filter(a=>a.pri==="高"||a.pri==="中").slice(0,6);
  /* ── 影响面元信息（v7）────────────────────────────────────────────────
     行业规范里 "an open-actions table needs owner + impact" —— 光一行字
     「推进逾期未交付」没法判断轻重。这里给每条待办挂上**结构化**的影响面。
     ⚠️ 绝不能从 a.text 里正则抠数字（"剩-39天" 抠出来放进别的语境就会串味），
     一律回到 model 里的结构化字段取。字段缺失就少挂一个 chip，不硬凑。 */
  const fz=m.foresee||{}, sg=(fz.shortage||{}).plans||[], bwd=(fz.backward||{}).act_now||[];
  const impactOf=(a)=>{
    const o=[];
    const add=(lab,val)=>{ if(val!=null&&val!=="") o.push(`<span>${lab} <b>${val}</b></span>`); };
    if(a.cat==="risk"){
      const hit=bwd.find(x=>x.plan&&a.text.indexOf(x.plan)>=0);
      if(hit){
        add("计划",hit.plan);
        add(hit.days_left<0?"已逾期":"剩余",Math.abs(hit.days_left)+" 天");
        if(hit.do_now&&hit.do_now.length) add("未启动环节",hit.do_now.length+" 个");
      }
      if(sg.length&&/缺料|立刻下单/.test(a.text)){
        add("缺口",sg[0].gap_items+" 种 / "+sg[0].total_gap+" 件");
        add("零库存",sg[0].zero_stock_items+" 种");
      }
    }
    if(a.cat==="warehouse"){
      const bom=(m.partdb||{}).bom;
      if(bom) add("BOM 缺口",bom.shortage.length+" 种 / "+bom.bom_count+" 行");
    }
    if(a.cat==="wechat"&&m.wechat){
      add("待确认",m.wechat.pending_count+" 条");
      add("今日新增",m.wechat.today_count+" 条");
    }
    if(a.cat==="market"&&m.commodities){
      add("告警品种",m.commodities.alerts.length+" 个");
      add("阈值","±"+m.commodities.threshold+"%");
    }
    if(a.cat==="delivery"){
      const od=(m.wip||[]).filter(w=>w.overdue);
      if(od.length){
        add("逾期单",od.length+" 单");
        add("最长逾期",Math.abs(Math.min.apply(null,od.map(w=>w.remain)))+" 天");
      }
    }
    if(a.cat==="sales"){
      add("应收合计",yuan(k.receivable));
      const top=(m.cost&&m.cost.receivable_list||[])[0];
      if(top) add("最大一笔",yuan(top.receivable));
    }
    if(a.cat==="production"&&m.time){
      const fd=m.time.flow_dist||{}, ks=Object.keys(fd).sort((x,y)=>fd[y]-fd[x]);
      if(ks.length) add("最大积压",ks[0]+" "+fd[ks[0]]+" 单");
      if(m.time.ontime_rate!=null) add("交期达成",m.time.ontime_rate+"%");
    }
    if(a.cat==="purchase"&&m.supply) add("采购逾期",(m.supply.overdue_list||[]).length+" 单");
    return o.length?`<div class="act-m">${o.join("")}</div>`:"";
  };
  const todayBody=urgent.length
    ? urgent.map(a=>{
        const tgt=(CAT_SEC[a.cat]||[]).find(visible);
        const jump=tgt?`<button class="go act-go" data-jump="sec-${tgt}">去处理 →</button>`:"";
        return `<div class="act-row p-${a.pri}">
          <i class="act-rail"></i>
          <div class="act-body"><div class="act-t">${a.text}</div>${impactOf(a)}</div>
          ${jump}</div>`;
      }).join("")
    : `<div class="act-row p-低"><i class="act-rail"></i>
         <div class="act-body"><div class="act-t">今天没有逾期或临期事项，保持当前节奏即可 ✔</div></div></div>`;
  const secToday=el(`<div class="today" id="sec-Today">
    <div class="today-hd"><span class="t">今天要处理</span>
      ${urgent.length?`<span class="cnt">${urgent.length} 项</span>`:`<span class="ok">全部正常</span>`}
      <span class="note" style="margin:0">数据快照 ${m.snapshot} · 仅显示高/中优先级 · 右侧「去处理」直达对应标签页</span></div>
    <div class="today-list acts">${todayBody}</div></div>`);
  secToday.querySelectorAll("[data-jump]").forEach(b=>b.onclick=()=>{
    const key=b.dataset.jump.replace("sec-","");
    setRoute(MOD_OF[key]||shell.mod, key);       // v6：直接切到目标标签页，不再滚动
  });
  put("Today", secToday);

  /* 下一步行动建议（完整列表，按角色过滤） */
  const actHTML=acts.length?acts.map(a=>`<div class="act"><span class="pri pri-${a.pri}">${a.pri}</span>
    <span class="tx">${a.text}</span></div>`).join(""):`<div class="act"><span class="pri pri-提示">提示</span><span class="tx">当前角色暂无专属待办事项，保持节奏即可 ✔</span></div>`;
  const secA=el(`<section id="sec-A" class="sec"><div class="sec-title">__IC_NEXT__ 下一步行动建议 · ${ROLES[role].name}（按优先级）</div>
    <div class="card"><div class="actions">${actHTML}</div>
    <div class="note">基于当前真实数据自动推导，按角色筛选：红=高优、橙=中优、蓝=提示。老板视图含全部战略项。</div></div></section>`);
  put("A", secA);

  /* 补录缺失数据（行内填写 → 存本地 → 复制发回 → 写云端） */
  const bf = m.backfill || {生产计划: [], 组装记录: []};
  const ov = loadOV();
  const planRows = (bf["生产计划"] || []).map(p => {
    const o = (ov["生产计划"] || {})[p.row_id] || {};
    const v = (f) => (o[f] !== undefined ? o[f] : (p[f] || ""));
    return `<tr>
      <td class="bf-name">${p.name}<div class="bf-sub">${p.row_id || ""}</div></td>
      <td><input type="date" data-tbl="生产计划" data-rid="${p.row_id}" data-field="计划开始日期" value="${v("计划开始日期")}"></td>
      <td><input type="date" data-tbl="生产计划" data-rid="${p.row_id}" data-field="计划完成日期" value="${v("计划完成日期")}"></td>
      <td><input type="date" data-tbl="生产计划" data-rid="${p.row_id}" data-field="实际完成日期" value="${v("实际完成日期")}"></td>
      <td><select data-tbl="生产计划" data-rid="${p.row_id}" data-field="放行状态">
        <option value=""></option>
        <option value="待评审" ${v("放行状态") === "待评审" ? "selected" : ""}>待评审</option>
        <option value="允许进入下一阶段" ${v("放行状态") === "允许进入下一阶段" ? "selected" : ""}>允许进入下一阶段</option>
        <option value="禁止放行" ${v("放行状态") === "禁止放行" ? "selected" : ""}>禁止放行</option>
      </select></td></tr>`;
  }).join("");
  const asmRows = (bf["组装记录"] || []).map(a => {
    const o = (ov["组装记录"] || {})[a.row_id] || {};
    const val = o["组装良品率"] !== undefined ? o["组装良品率"] : (a["组装良品率"] || "");
    return `<tr>
      <td class="bf-name">${a.name}<div class="bf-sub">${a.row_id || ""}</div></td>
      <td colspan="3" style="color:var(--sub);font-size:12px">组装良品率（%，仅此列需填）</td>
      <td><input type="number" step="0.1" min="0" max="100" data-tbl="组装记录" data-rid="${a.row_id}" data-field="组装良品率" value="${val}" placeholder="如 98.5"></td></tr>`;
  }).join("");
  let bfCnt = 0;
  for (const tbl of ["生产计划", "组装记录"]) for (const rid in (ov[tbl] || {})) for (const k in ov[tbl][rid]) if (ov[tbl][rid][k] !== "") bfCnt++;
  const secBF = el(`<section id="sec-BF" class="sec"><div class="sec-title">__IC_EDIT__ 补录缺失数据（本地填写 → 复制发我 → 写云端）</div>
    <div class="bf-card">
      <div class="bf-toolbar">
        <button class="bf-btn copy" id="bfCopy">__IC_COPY__ 一键复制补录数据</button>
        <button class="bf-btn ghost" id="bfClear">清空本地补录</button>
        <span class="bf-count">已填 <b id="bfCount">${bfCnt}</b> 项 · 数据存本机浏览器，不会自动上传</span>
      </div>
      <div class="bf-scroll"><table class="bf-table"><thead><tr>
        <th>产品 / 编号</th><th>计划开始</th><th>计划完成</th><th>实际完成</th><th>放行状态</th>
      </tr></thead><tbody id="bfBody">
        ${planRows}
        ${asmRows.length ? `<tr><td colspan="5" style="background:var(--subbg);font-weight:600;color:var(--sub)">组装记录（${asmRows.length} 条）</td></tr>` : ""}
        ${asmRows}
      </tbody></table></div>
      <div class="note">说明：生产计划「计划开始 / 完成」已按 立项日期 / 合同交期 预填，可直接改；「实际完成」「放行状态」需你填写（放行状态三选一：待评审 / 允许进入下一阶段 / 禁止放行）。填完点「一键复制」，把内容粘贴到 WorkBuddy 发我，我确认后写入 SeaTable 云端真库。<b>写回不可逆</b>，我绝不替你编造数值。</div>
    </div></section>`);
  secBF.querySelector("#bfCopy").onclick = copyBackfill;
  secBF.querySelector("#bfClear").onclick = clearBackfill;
  put("BF", secBF);

  /* 项目总览 + 在制品看板 */
  const projRows=m.projects.map(p=>`<tr>
    <td>${p.name}</td>
    <td><span class="pill st-${p.status}">${p.status}</span></td>
    <td class="num">${yuan(p.contract)}</td>
    <td class="num">${yuan(p.received)}</td>
    <td class="num ${p.receivable>0?'neg':''}">${yuan(p.receivable)}</td>
    <td class="num">${p.due||"—"}</td></tr>`).join("");
  const wipRows = m.wip.length ? m.wip.map(w=>{
    const rm = w.remain==null?"—":(w.remain<0?("逾期"+(-w.remain)+"天"):(w.remain+"天"));
    const cls = w.overdue?"tag-red":(w.remain!=null&&w.remain<=7?"tag-amber":"tag-green");
    return `<tr><td>${w.product}</td><td><span class="pill st-${w.status}">${w.status}</span></td>
      <td>${w.stage}</td><td class="num">${fmt(w.qty)}</td>
      <td class="num">${w.due||"—"}</td><td><span class="pill ${cls}">${rm}</span></td></tr>`;
  }).join("") : `<tr><td colspan="6" class="empty">无在制品</td></tr>`;

  /* 项目状态 / 在制阶段构成条：两个「一眼看结构」的图，占位很矮。
     ⚠️ 用 stackBar 而不是饼：饼图靠角度估占比，构成条靠长度，后者可读性高一个量级。 */
  const stCount={};
  (m.projects||[]).forEach(p=>{ const s2=p.status||"未定义"; stCount[s2]=(stCount[s2]||0)+1; });
  const wipStage={};
  (m.wip||[]).forEach(w=>{ const s2=w.stage||"未定义"; wipStage[s2]=(wipStage[s2]||0)+1; });
  const pwViz=`<div class="viz-grid">${
    vizCard("项目状态构成",
      stackBar(Object.keys(stCount).sort((a,b2)=>stCount[b2]-stCount[a]).map(k2=>({l:k2,v:stCount[k2]}))),
      {q:`共 ${m.projects.length} 个项目`,
       note:"<b>存量视角</b>：已交付是历史沉淀；真正还要投入的是「计划中 / 已超期」那两块。"})}${
    vizCard("在制计划阶段分布",
      stackBar(Object.keys(wipStage).map(k2=>({l:k2,v:wipStage[k2]}))),
      {q:`共 ${(m.wip||[]).length} 个在制计划`,
       note:"在制 = 生产计划表里状态 ≠ 已交付。这一条直接回答「货卡在哪个工序」。"})}</div>`;
  const secPW=el(`<section id="sec-PW" class="sec"><div class="sec-title">__IC_PROJ__ 项目总览 & 在制品看板</div>
    ${pwViz}
    <div class="grid g2" style="margin-top:22px">
      <div class="card"><h3>项目清单（${m.projects.length}）</h3>
        <div style="overflow-x:auto"><table data-paginate="8" data-filter="1" data-select="1"><thead><tr><th>项目</th><th>状态</th><th>合同额</th><th>已收</th><th>应收</th><th>交期</th></tr></thead>
        <tbody>${projRows}</tbody></table></div></div>
      <div class="card"><h3>在制品看板（按剩余时间升序）</h3>
        <div style="overflow-x:auto"><table data-paginate="8" data-filter="1" data-select="1"><thead><tr><th>产品</th><th>状态</th><th>阶段</th><th>数量</th><th>交期</th><th>剩余</th></tr></thead>
        <tbody>${wipRows}</tbody></table></div>
        <div class="note">红色=已逾期，橙色=7天内到期，绿色=余量充足。昨天未完自动顺延至今日。</div></div>
    </div></section>`);
  put("PW", secPW);

  /* 甘特图 */
  const g=m.gantt||[];
  const gDone=g.filter(x=>x.done).length;
  const gOver=g.filter(x=>x.overdue).length;
  const gRate=gDone?((gDone-gOver)/gDone*100):null;
  const gmd2=ganttMonthly(g);
  const gViz=`<div class="viz-grid">${
    vizCard("甘特计划健康度",
      bullet([
        {l:"按期完成率（已交付中）",v:gRate==null?0:gRate,target:90,unit:"%",
          sev:sev(gRate,"ontime_rate")},
        {l:"逾期未交付",v:gOver,target:0,unit:" 单",sev:gOver?"bad":"ok"},
        {l:"未填合同交期",v:m.gantt_pending||0,target:null,txt:(m.gantt_pending||0)+" 单"},
      ],{}),
      {q:`共 ${g.length} 条计划 · 已交付 ${gDone} 条`,
       note:"按期 = 已交付且未逾期。<b>深色竖线是 90% 目标</b>。「未填交期」多为合同写「收款后 X 日内交货」、尚未收款故按规则留空，条长按历史工期推算。"})}${
    vizCard("月度计划量走势",
      gmd2.months.length>1?`${spark(gmd2.count,{color:"var(--primary)"})}
        <div class="bt-scale"><span>${gmd2.months[0]} · ${gmd2.count[0]} 条</span>
          <span>${gmd2.months[gmd2.months.length-1]} · ${gmd2.count[gmd2.count.length-1]} 条</span></div>`
        :`<div class="viz-empty">月份样本不足，暂不画走势</div>`,
      {q:`覆盖 ${gmd2.months.length} 个月`,
       note:"按计划的结束月归集。<b>只画真的有计划的月份</b> —— 给空月补零会平白造出一段「归零」的假趋势。"})}</div>`;
  const secG=el(`<section id="sec-G" class="sec"><div class="sec-title">__IC_GANT__ 生产计划甘特图（三种状态：实际进度 · 合同交期 · 历史推算交期）</div>
    ${gViz}
    <div class="card" style="margin-top:22px"><div class="gantt-tools" id="ganttTools"></div>
    <div class="gantt-wrap"><div class="gantt" id="gantt"></div></div>
    <div class="note"><b>图中三种状态</b>：
      <span class="lg"><i class="lg-bar run"></i>① 实际进度</span>＝条内浅色填充（读生产计划表「阶段」的<b>实测值</b>，每晚 19:00 同步后的快照，不按日期推算；紫=进行中／绿=已交付／红=逾期）；
      <span class="lg"><i class="lg-ms due"></i>② 合同交期</span>＝<b>实心菱形</b>，对客户的承诺日期（计划表未填时不画该菱形）；
      <span class="lg"><i class="lg-ms est"></i>③ 历史推算交期</span>＝<b>空心菱形</b>，按自家历史工期推算的对标线。
      三者在同一时间轴上并列，可直观判断「合同日期比历史惯例更紧还是更松」。竖红线为今日 ${m.snapshot}。悬停条形看详情与两种交期的松紧；点行可高亮联动项目表。默认按<b>「计划中」优先</b>排序。
      ${m.gantt_pending?`<br><b>其中 ${m.gantt_pending} 条未填合同交期</b>（多为合同写明「收款后 X 日内交货」、尚未收款故按规则留空）→ 条长与菱形均按推算值画，名称后带 ≈ 号。`:""}
      ${m.gantt_hist&&m.gantt_hist.n?`<br>推算依据＝<b>已交付计划的实际工期</b>：样本 ${m.gantt_hist.n} 条，中位 <b>${m.gantt_hist.median} 天</b>（p75 ${m.gantt_hist.p75} / p90 ${m.gantt_hist.p90}，区间 ${m.gantt_hist.min}~${m.gantt_hist.max} 天）→ 取「立项日 + ${m.gantt_est_days} 天」。<b>推算仅供排期对标，不回写 SeaTable、不等于客户承诺交期</b>；计划表补录交期后空心菱形会与实心菱形分开显示，便于复盘当初排得准不准。`:"历史工期样本不足，暂用兜底工期推算。"}</div></div></section>`);
  const gEl=secG.querySelector("#gantt");
  gEl.innerHTML=renderGantt(g,m.snapshot);
  initGanttTools(g, m.snapshot, secG.querySelector("#ganttTools"), gEl, m.gantt_est_days);
  put("G", secG);

  /* ══ 项目矩阵：项目全表 / 生产计划全表 / 流程思维导图（项目经理视角完整台账）══ */
  const pf=m.projects_full||[];
  const plRows=m.plans_full||[];
  const mmModel=m.mindmaps;
  const pillOf=s=>{ const v=String(s==null?"":s).trim();
    return `<span class="pl pl-${esc(v)}">${esc(v||"—")}</span>`; };
  const sumCol=(rows,k)=>rows.reduce((a,r)=>a+(parseFloat(String(r[k]).replace(/[^0-9.\-]/g,""))||0),0);
  const numOr=(v)=>{ const s=String(v==null?"":v).trim(); return /^[0-9.\-]+$/.test(s)?Number(s):null; };
  /* SeaTable 公式列会回传 "#VALUE!" 之类的错误串——一律显示为「—」，别把公式错误当数据 */
  const clean=(v)=>{ const s=String(v==null?"":v).trim(); return (!s||s.charAt(0)==="#")?"—":s; };
  const rc=m.recv_check||{kpi:0,stored:0,diff:0,rows:[],n:0};

  const secPT=el(`<section id="sec-PT" class="sec"><div class="sec-title">__IC_PROJ__ 项目全表（${pf.length} 个项目 · 全字段台账）</div>
    <div class="card">
      <div class="note">合同额合计 <b>${yuan(sumCol(pf,"合同总价"))}</b> · 已收 <b>${yuan(sumCol(pf,"实收"))}</b>
        · 待收（表内公式列）<b>${yuan(sumCol(pf,"待收"))}</b> · 应收（合同额−已收，看板现算）<b>${yuan(sumCol(pf,"合同总价")-sumCol(pf,"实收"))}</b>
        · 生产花销合计 <b>${yuan(sumCol(pf,"生产花销"))}</b>；末三列＝该项目关联的生产计划 / 发货单 / 维修记录条数。</div>
      <details class="recv-diff">
        <summary><span class="rd-ic">⚠️</span> 应收口径说明：表内「待收」<b>${yuan(rc.stored)}</b> ↔ 看板「应收」<b>${yuan(rc.kpi)}</b>，差 <b>${yuan(rc.diff)}</b>（逐项对不上的 <b>${rc.n}</b> 个项目见下）</summary>
        <div class="rd-body">
          <p class="rd-p"><b>两个数为什么不同？</b>「待收」直接取 SeaTable 表里的<b>公式列</b>汇总；「应收」是看板按 <b>合同总价 − 实收</b> 现算。二者本应逐项相等，
            实测差额 <b>${yuan(rc.diff)}</b>，<b>全部来自下面 ${rc.n} 个项目</b>（其余 ${pf.length - rc.n} 个完全一致）：</p>
          ${rc.rows.length? `<div style="overflow-x:auto"><table class="rd-tbl">
            <thead><tr><th>项目编号</th><th>项目</th><th>状态</th><th>合同总价</th><th>已收</th><th>表内待收</th><th>应收(算)</th><th>差</th><th>成因说明</th></tr></thead>
            <tbody>${rc.rows.map(r=>`<tr>
              <td>${esc(r.no)}</td><td>${esc(r.name)}</td><td>${pillOf(r.status)}</td>
              <td class="num">${yuan(r.contract)}</td><td class="num">${yuan(r.received)}</td>
              <td class="num">${yuan(r.stored)}</td><td class="num">${yuan(r.calc)}</td>
              <td class="num ${r.diff>0?'':'neg'}">${yuan(r.diff)}</td>
              <td class="rd-why">${esc(r.why)}</td></tr>`).join("")}</tbody></table></div>`
            : `<div class="note">✅ 逐项一致，无差异。</div>`}
          <p class="rd-p"><b>建议：</b>到 SeaTable「项目」表核对上表项目的「待收」公式列（多为公式未覆盖新行或未重算），补齐后两个口径即会自动吻合。</p>
        </div>
      </details>
      <div style="overflow-x:auto"><table data-paginate="12" data-filter="1">
        <thead><tr><th>项目编号</th><th>项目</th><th>状态</th><th>合同总价</th><th>已收</th><th>待收</th>
          <th>签订</th><th>合同交期</th><th>剩余(天)</th><th>花费天数</th><th>生产花销</th>
          <th title="关联生产计划数">计划</th><th title="发货单数">发货</th><th title="维修记录数">维修</th></tr></thead>
        <tbody>${pf.map(r=>`<tr>
          <td>${esc(r["项目编号"])}</td>
          <td>${esc(r["项目"])}</td>
          <td>${pillOf(r["状态"])}</td>
          <td class="num">${yuan(r["合同总价"])}</td>
          <td class="num">${yuan(r["实收"])}</td>
          <td class="num ${numOr(r["待收"])>0?"neg":""}">${yuan(r["待收"])}</td>
          <td>${esc(clean(r["签订日期"]))}</td>
          <td>${esc(clean(r["合同交期"]))}</td>
          <td class="num">${esc(clean(r["剩余（天）"]))}</td>
          <td class="num">${esc(clean(r["花费天数"]))}</td>
          <td class="num">${yuan(r["生产花销"])}</td>
          <td class="num">${r["_计划"]}</td>
          <td class="num">${r["_发货"]}</td>
          <td class="num">${r["_维修"]}</td>
        </tr>`).join("")}</tbody></table></div>
      <div class="note">共 ${pf.length} 行；表头下方工具条可搜索项目名/编号，按状态筛选，分页浏览。</div>
    </div></section>`);
  put("PT", secPT);

  const secPL=el(`<section id="sec-PL" class="sec"><div class="sec-title">__IC_TIME__ 生产计划全表（${plRows.length} 条 · 在产/交付台账）</div>
    <div class="card">
      <div class="note">生产花销合计 <b>${yuan(sumCol(plRows,"生产总花销"))}</b> · 已填单片成本
        <b>${plRows.filter(r=>numOr(r["此次单片成本"])>0).length}</b> 条，均值
        <b>${yuan(sumCol(plRows,"此次单片成本")/(plRows.filter(r=>numOr(r["此次单片成本"])>0).length||1))}</b></div>
      <div style="overflow-x:auto"><table data-paginate="12" data-filter="1">
        <thead><tr><th>计划编号</th><th>生产产品</th><th>数量</th><th>状态</th><th>阶段</th><th>关联项目</th>
          <th>立项</th><th>合同交期</th><th>花费天数</th><th>生产总花销</th><th>单片成本</th><th>批次号</th></tr></thead>
        <tbody>${plRows.map(r=>`<tr>
          <td>${esc(r["生产计划编号"])}</td>
          <td>${esc(r["生产产品"])}</td>
          <td class="num">${fmt(r["数量"])}</td>
          <td>${pillOf(r["状态"])}</td>
          <td>${pillOf(r["阶段"])}</td>
          <td>${esc(r["关联项目"])}</td>
          <td>${esc(clean(r["立项日期"]))}</td>
          <td>${r["_est"]
            ?`<span title="该计划未填「合同交期」（多为合同约定『收款后 X 日内交货』、尚未收款），此处按历史中位工期推算，非客户承诺">≈ ${esc(r["_due_est"])}</span>`
            :esc(clean(r["合同交期"]))}</td>
          <td class="num">${esc(clean(r["花费天数"]))}</td>
          <td class="num">${yuan(r["生产总花销"])}</td>
          <td class="num">${numOr(r["此次单片成本"])>0?yuan(r["此次单片成本"]):"—"}</td>
          <td>${esc(r["批次号"])}</td>
        </tr>`).join("")}</tbody></table></div>
    </div></section>`);
  put("PL", secPL);

  const secMM=el(`<section id="sec-MM" class="sec"><div class="sec-title">__IC_PROJ__ 流程思维导图（${mmModel?mmModel.count:0} 张 XMind · ${mmModel?mmModel.nodes_total:0} 个节点）</div>
    <div class="card"><div id="mmHost"></div></div></section>`);
  mountMindmaps(secMM.querySelector("#mmHost"), mmModel);
  put("MM", secMM);

  /* 工时 */
  const t=m.time;
  const stageItems=Object.entries(t.stage_dist).map(([k,v])=>({l:k,v:v}));
  /* 生产周期 Top：谁拖得最久一眼可见。必须带标签 —— 光有条形，看完还得回头
     去表格里对号才知道哪根条是哪个产品。 */
  const cycItems=(t.cycle_list||[]).slice().sort((a,b)=>b.days-a.days).slice(0,12)
    .map(x=>({l:x.product,v:x.days,txt:x.days+" 天",
      sev:x.days>45?"bad":(x.days>30?"warn":"")}));
  const gmd=ganttMonthly(m.gantt);
  const secT=el(`<section id="sec-T" class="sec"><div class="sec-title">__IC_TIME__ 工时分析（交付能力）</div>
    <div class="viz-grid">
      ${vizCard("交期达成率 vs 目标（90%）",
        bullet([{l:"内部周期达成",v:t.ontime_rate==null?0:t.ontime_rate,target:90,unit:"%",
                 sev:sev(t.ontime_rate,"ontime_rate")}],{scale:true,unit:"%"}),
        {q:`可判定 ${t.ontime}/${t.dated||0} 条`,
         note:"达成 = 实际花费天数 ≤ 允许周期（合同交期 − 立项日期）。<b>深色竖线是 90% 目标</b>。"})}
      ${vizCard("在产阶段分布", stackBar(stageItems),
        {q:`共 ${Object.values(t.stage_dist).reduce((a,b)=>a+b,0)} 条计划`,
         note:"推进中的环节看「备料中/贴片生产/组装/测试」，那才是瓶颈所在；「已交付」是历史沉淀。"})}
      ${vizCard("生产周期 Top（实际花费天数）", barList(cycItems),
        {q:"天数降序 · Top 12",
         note:"红＝超过 45 天，橙＝超过 30 天。数据源＝生产计划表「花费天数」（业务实填的工序耗时）。"})}
      ${vizCard("月度平均工期走势",
        gmd.months.length>1?`${spark(gmd.cycle,{color:"var(--blue)"})}
          <div class="bt-scale"><span>${gmd.months[0]}</span><span>${gmd.months[gmd.months.length-1]}</span></div>`
          :`<div class="viz-empty">月份样本不足，暂不画走势</div>`,
        {q:`覆盖 ${gmd.months.length} 个月`,
         note:"把每月计划的「花费天数」求平均。<b>只画真的有计划的月份</b>，不给空月补零 —— 补零会造出一条假的下探。"})}
    </div>
    <div class="card" style="margin-top:22px"><h3>口径说明</h3>
      <div class="note">交期达成率 = 内部周期达成：实际花费天数 ≤ 允许的(合同交期−立项)天数 的计划占比。<br>
      平均实际周期 = 各计划「花费天数」均值，当前 <b>${t.avg_cycle} 天</b>，历史中位 <b>${(m.gantt_hist||{}).median||"—"} 天</b>（p75 ${(m.gantt_hist||{}).p75||"—"} / p90 ${(m.gantt_hist||{}).p90||"—"}）。<br>
      阶段分布反映当前产能瓶颈所在工序。</div></div>
  </section>`);
  put("T", secT);

  /* 成本 */
  const c=m.cost;
  const palette=["#3b5bdb","#1c7ed6","#0ca678","#f08c00","#7048e8","#e8590c","#1098ad","#d6336c"];
  const catItems=c.category.map((x,i)=>({name:x.name,value:x.value,color:palette[i%palette.length]}));
  const recvRows=c.receivable_list.length?c.receivable_list.map(p=>`<tr>
    <td>${p.name}</td><td class="num neg">${yuan(p.receivable)}</td>
    <td class="num">${p.due||"—"}</td><td><span class="pill ${p.due&&p.due<m.snapshot?'tag-red':'tag-amber'}">${p.due&&p.due<m.snapshot?'逾期':'待收'}</span></td></tr>`).join("")
    :`<tr><td colspan="4" class="empty">无应收款</td></tr>`;
  const cf=c.cashflow.map(x=>`<div class="cell"><div class="bk">${x.bucket=="逾期"?"已逾期":x.bucket+"天内"}</div>
    <div class="bi num">+${fmt(x.in)}</div><div class="bo num">−${fmt(x.out)}</div>
    <div class="bn num ${x.net>=0?'pos':'neg'}">${x.net>=0?'+':''}${fmt(x.net)}</div></div>`).join("");
  /* 应收账龄分布条形图（逾期/30/60/90 天应收金额）*/
  const _d=s=>{try{const [y,mo,dd]=String(s).split("-").map(Number);return new Date(y,mo-1,dd).getTime();}catch(e){return NaN;}};
  const ageMap={"已逾期":0,"30":0,"60":0,"90":0};
  c.receivable_list.forEach(p=>{
    let k="90";
    if(p.due&&p.due<m.snapshot) k="已逾期";
    else if(p.due){
      const d=(_d(p.due)-_d(m.snapshot))/86400000;
      k=d<=30?"30":(d<=60?"60":"90");
    }
    ageMap[k]=(ageMap[k]||0)+p.receivable;
  });
  /* 应收账龄分布：账龄越长越难收 —— 逾期标红、60/90 天标红、30 天内标绿。 */
  const AGE_SEV={"已逾期":"bad","30":"ok","60":"bad","90":"bad"};
  const ageItems=[["已逾期"],["30"],["60"],["90"]]
    .map(([k2])=>{ const v=Math.round(ageMap[k2]||0);
      return {l:k2==="已逾期"?"已逾期":(k2+" 天内"),v:v,txt:yuan(v),sev:AGE_SEV[k2]||""}; })
    .filter(x=>x.v>0);
  const recvBarsHTML=c.receivable_list.length?`<div style="margin-top:16px">
    <h3 style="font-size:13px;color:var(--sub);font-weight:600;margin-bottom:8px">应收账龄分布（元）</h3>
    ${barList(ageItems)}
    <div class="viz-note">按合同交期距今分桶。<b>逾期的必须优先催收</b> —— 账龄越长，实际回收概率越低。</div></div>`:"";
  /* 应收 Top Pareto：催收要压在最大的几笔上，而不是平均用力。 */
  const recvTop=(c.receivable_list||[]).slice().sort((a,b)=>b.receivable-a.receivable).slice(0,12)
    .map(p=>({l:p.name,v:p.receivable,txt:yuan(p.receivable),
      sev:(p.due&&p.due<m.snapshot)?"bad":"warn"}));
  const recvTopHTML=recvTop.length?vizCard("应收款 Top（按金额）",barList(recvTop),
    {q:`共 ${c.receivable_list.length} 笔 · 合计 ${yuan(c.receivable_list.reduce((a,p)=>a+p.receivable,0))}`,
     note:"红＝已过合同交期。数据源＝项目表的 合同总价 − 实收（看板现算）。"}):"";
  const secC=el(`<section id="sec-C" class="sec"><div class="sec-title">__IC_COST__ 成本分析（盈利能力）</div>
    <div class="grid g2">
      <div class="card"><h3>成本结构（总花销 ${yuan(c.cost)}）</h3>
        ${stackBar(catItems.map(x=>({l:x.name,v:x.value})),{money:true})}
        <div class="viz-note">按采购明细汇总，共 ${catItems.length} 个科目。<b>用构成条代替饼图</b>：同一科目永远同一颜色，跨页可对照，且不用靠角度估算占比。</div></div>
      <div class="card"><h3>利润与预算</h3>
        <div class="kpi ac-green"><div class="top"><span class="ac-dot"></span><span class="lbl">总利润</span></div>
          <div class="v">${yuan(c.profit)}</div><div class="sub">合同 ${yuan(c.contract)} − 花销 ${yuan(c.cost)}</div></div>
        <div class="bar-row" style="margin-top:14px"><div class="nm">毛利率</div>
          <div class="bar-track"><div class="bar-fill" style="width:${Math.min(100,c.margin)}%;background:${c.margin>=c.budget_margin?'#2f9e44':'#e03131'}"></div></div>
          <div class="vl num">${pct(c.margin)}</div></div>
        <div style="display:flex;justify-content:space-between;margin-top:12px;font-size:13px">
          <span style="color:var(--sub)">单片成本（台账均单价）</span><span class="num" style="font-weight:700">${yuan(c.unit_cost)}</span></div>
        <div class="note">目标毛利率 ${pct(c.budget_margin)} · ${c.margin>=c.budget_margin?'盈利充足 ✔':'低于目标 ⚠'}</div>
        <div class="note">台账口径生产总花销 ${yuan(c.ledger_cost)}（采购明细汇总 ${yuan(c.cost)}），差异含人工/辅料。</div></div>
    </div>
    <div class="grid g2" style="margin-top:14px">
      <div class="card"><h3>应收款催收（${c.receivable_list.length}）</h3>
        <div style="overflow-x:auto"><table data-paginate="8" data-filter="1" data-select="1"><thead><tr><th>项目</th><th>应收</th><th>合同交期</th><th>状态</th></tr></thead>
        <tbody>${recvRows}</tbody></table></div>
        ${recvBarsHTML}</div>
      <div class="card"><h3>现金流预测（30/60/90天）</h3><div class="cf">${cf}</div>
        <div class="note">收入按合同交期、支出按采购预计到货归集；净额为收减付。</div></div>
    </div>
    ${recvTopHTML}</section>`);
  put("C", secC);

  /* 质量 */
  const q=m.quality;
  /* 良率/维修率改用「环 + 目标」：环最省横向空间，且必须配目标才有意义 ——
     一个孤零零的 "94.4%" 无法判断好坏，配上 "目标 98%" 才成立。 */
  const ringBox=(val,label,sub,sevKey,lowerBetter,bad)=>{
    const s=(bad||val==null)?"":sev(val,sevKey,lowerBetter);
    return `<div class="ringbox">
      ${val==null?`<div class="viz-empty" style="padding:24px 0">未录入</div>`:ring(val,{sev:s,label:label})}
      <div class="rl">${label}</div>
      <div class="rs">${sub}</div></div>`;
  };
  const smtMiss=(q.smt_missing_n||0);
  const secQ=el(`<section id="sec-Q" class="sec"><div class="sec-title">__IC_QUAL__ 质量分析（交付质量）</div>
    <div class="grid g3">
      <div class="card"><h3>良品率</h3>
        <div class="rings">
          ${ringBox(q.smt_yield,"贴片良品率","目标 98%","smt_yield",false)}
          ${ringBox(q.asm_yield,"组装良品率",q.asm_yield==null?"未录入":"目标 98%","asm_yield",false,q.asm_yield==null)}
        </div>
        <div class="viz-note"><b>样本完整度</b>：贴片 ${q.smt_batches||0}/${q.smt_rows||0} 批已录「良品数量」${smtMiss?`，<b style="color:var(--amber)">${smtMiss} 批待补录</b>（合计投入 ${fmt(q.smt_missing_qty)} 片，已从分母剔除）`:""}。
          ${smtMiss?`<br>⚠ 若把待补录批次的数量也算进分母，良率会被系统性稀释成一个偏低的假值 —— 那个数不能用来考核。`:""}
          ${q.asm_yield==null?`<br>组装良品率：${(m.backfill&&m.backfill["组装记录"]||[]).length} 条组装记录均未填良品率，暂无法计算（<b>并非 0%</b>）。可在「补录缺失数据」里填。`:""}</div></div>
      <div class="card"><h3>维修率</h3>
        <div class="rings">
          ${ringBox(q.repair_rate,"维修率","目标 ≤2%","repair_rate",true)}
        </div>
        <div style="margin-top:12px">
          ${bullet([
            {l:"维修件数 / 发货",v:q.repair_total,target:null,txt:q.repair_total+" / "+q.shipped,unit:" 件"},
            {l:"超期未完修",v:q.repair_overdue,target:0,unit:" 单",sev:q.repair_overdue>0?"bad":"ok"},
            {l:"平均返修周期",v:q.repair_avg,target:null,txt:q.repair_avg,unit:" 天"},
          ],{})}
        </div></div>
      <div class="card"><h3>维修明细</h3>
        ${q.repair_list.length?`<div style="overflow-x:auto"><table data-paginate="8" data-filter="1" data-select="1"><thead><tr><th>项目</th><th>问题</th><th>返修</th><th>状态</th></tr></thead><tbody>`
          +q.repair_list.map(r=>`<tr><td>${r.proj}</td><td>${r.item||"—"}</td><td class="num">${r.back}</td>
          <td><span class="pill ${r.done?'tag-green':(r.overdue?'tag-red':'tag-amber')}">${r.done?'已完成':(r.overdue?'超期':'处理中')}</span></td></tr>`).join("")
          +`</tbody></table></div>`:`<div class="empty">无维修记录</div>`}
        <div class="note">「问题」列原样取自 SeaTable 单元格，<b>部分行里塞的是 markdown 原文表格</b>（业务在单元格里直接粘了表格）—— 看板不做清洗，以免串改原始记录。</div></div>
    </div></section>`);
  put("Q", secQ);

  /* 生产进度 & 产线流转（基于生产计划 / 工序真实字段） */
  const tp=m.time;
  const flowEntries=Object.entries(tp.flow_dist).sort((a,b)=>a[0]<b[0]?-1:1);
  const FCOL=["#3b5bdb","#1c7ed6","#0ca678","#f08c00","#7048e8","#e8590c","#1098ad","#d6336c","#5c7cfa","#f783ac","#82c91e","#ff922b","#20c997","#845ef7","#fab005"];
  const flowItems=flowEntries.map(([k,v],i)=>({name:k.split(" ").slice(1).join(" ")||k,value:v,color:FCOL[i%FCOL.length]}));
  const flowMax=Math.max(1,...flowItems.map(x=>x.value));
  const flowHTML=flowItems.map(i=>`<div class="bar-row">
      <div class="nm" style="width:124px;flex:0 0 124px;text-align:left">${i.name}</div>
      <div class="bar-track"><div class="bar-fill" style="width:${Math.round(i.value/flowMax*100)}%;background:${i.color}"></div></div>
      <div class="vl num">${i.value}</div></div>`).join("");
  const stageItems2=Object.entries(tp.stage_dist).map(([k,v],i)=>({name:k,value:v,color:["#3b5bdb","#1c7ed6","#0ca678","#f08c00","#7048e8","#e8590c"][i%6]}));
  const secP=el(`<section id="sec-P" class="sec"><div class="sec-title">__IC_PROJ__ 生产进度 & 产线流转</div>
    <div class="grid g2">
      <div class="card"><h3>产线实时流转（在产计划当前工序）</h3>${flowHTML}
        <div class="note">基于生产计划表「阶段」（在产 = 状态≠已交付）：每个在产计划当前所处工序，用于定位产能瓶颈。共 ${flowEntries.length} 个工序环节在流转。</div></div>
      <div class="card"><h3>生产计划阶段分布</h3>${bars(stageItems2)}
        <div class="note">基于生产计划表「阶段」：整体进度分布（已交付/备料中/组装等）。</div></div>
    </div></section>`);
  put("P", secP);

  /* 供应链 + 物料库存预警 */
  const s=m.supply;
  const supRows=s.supplier.length?s.supplier.map(x=>{
    const rc=x.rate==null?'—':pct(x.rate);
    const cls=x.rate==null?'':(x.rate>=90?'tag-green':(x.rate>=70?'tag-amber':'tag-red'));
    return `<tr><td>${x.name}</td><td class="num">${x.orders}</td>
      <td class="num">${fmt(x.ontime)}/${fmt(x.late)}/${fmt(x.pending)}</td>
      <td><span class="pill ${cls}">${rc}</span></td><td class="num">${yuan(x.cost)}</td></tr>`;
  }).join(""):`<tr><td colspan="5" class="empty">无采购记录</td></tr>`;
  const odRows=s.overdue_list.length?s.overdue_list.map(o=>`<tr>
    <td>${o.supplier}</td><td>${o.material||"—"}</td><td>${o.plan}</td>
    <td class="num">${o.eta}</td><td><span class="pill tag-red">逾期${o.days}天</span></td></tr>`).join("")
    :`<tr><td colspan="5" class="empty">无采购逾期 ✔</td></tr>`;
  /* 供应商准时率条形图（只画有准时率的，按准时率升序 = 最差的排最前）*/
  const supBarsItems=s.supplier.filter(x=>x.rate!=null)
    .sort((a,b)=>a.rate-b.rate).slice(0,8)
    .map(x=>({name:x.name,value:x.rate,
      color:x.rate>=90?"var(--green)":(x.rate>=70?"var(--amber)":"var(--red)")}));
  const supBarsHTML=supBarsItems.length?bars(supBarsItems,{fmt:v=>v+"%"}):"";
  /* ── 供应商到货延迟（v7 新增）──────────────────────────────────────────
     下面那张「供应商交期准确率」依赖 supply.supplier，而**真实库里那张是空的**，
     于是准时率一直显示 0% —— 这等于把「没有样本」谎报成「准时率为零」。
     这里改用 foresee.supplier 的真实历史样本（当前 106 个）算到货延迟，
     有样本才出数，没样本就整块不渲染。 */
  const fsup=(m.foresee||{}).supplier||{};
  const catProf=fsup.cat_profile||{};
  const catDelay=Object.keys(catProf)
    .map(c=>({l:c,v:catProf[c].mean||0,
      txt:(catProf[c].mean||0).toFixed(1)+" 天 · "+catProf[c].n+" 单",
      sev:sev(catProf[c].mean,"sup_delay",true)}))
    .sort((a,b2)=>b2.v-a.v);
  const worstCat=catDelay.length?catDelay[0].l:null;
  const supDetailRows=(fsup.sup_detail||{})[worstCat]||[];
  const supDelayBars=supDetailRows.slice().sort((a,b2)=>b2.mean-a.mean).slice(0,10)
    .map(x=>({l:x.supplier,v:x.mean,
      txt:x.mean.toFixed(1)+" 天 · "+x.n+" 单"+(x.max?" · 最差 "+x.max+" 天":""),
      sev:sev(x.mean,"sup_delay",true)}));
  const supViz=catDelay.length?`<div class="viz-grid">${
    vizCard("各品类到货延迟（天）",barList(catDelay),
      {q:`${fsup.samples||0} 个历史样本 · 越低越好`,
       note:"延迟 = 实际到货 − 合同到货日，<b>负值＝提前到货</b>。样本只取采购记录里已回填到货日期的行。"})}${
    worstCat?vizCard(`延迟最重品类「${worstCat}」· 供应商排行`,barList(supDelayBars),
      {q:"按平均延迟降序 · Top 10",
       note:"同样天数下样本数 n 越多越可信；<b>n=1 的条只能当线索，不能当结论</b>。末位数字是最差单次延迟，用于看波动。"}):""}</div>`:"";
  const invRows=s.inventory_warn.length?s.inventory_warn.map(v=>`<tr>
    <td>${v.plan}</td><td>${v.result}</td><td class="num">${v.time||"—"}</td>
    <td><span class="pill tag-amber">待处理</span></td></tr>`).join("")
    :`<tr><td colspan="4" class="empty">库存核对正常 ✔</td></tr>`;
  const pd=m.partdb;
  let pdHTML;
  if(pd){
    const b=pd.bom;
    const shRows=b&&b.shortage.length?b.shortage.map(x=>{
      const rk=(x.risk&&x.risk.length)?x.risk.join('·'):'缺料';
      const cls=x.confirmed===0?'tag-red':(x.gap>0?'tag-amber':'tag-green');
      return `<tr><td>${x.name}${x.ipn?' <span class="sub">'+x.ipn+'</span>':''}</td>
        <td class="num">${x.qty_per}</td><td class="num">${x.need}</td>
        <td class="num">${x.confirmed}</td><td class="num">${x.gap}</td>
        <td class="num">${x.price!=null?yuan(x.price):'—'}</td>
        <td><span class="pill ${cls}">${rk}</span></td></tr>`;
    }).join(''):`<tr><td colspan="7" class="empty">✅ 当前库存可满足 ${b?b.qty:''} 套生产，无缺料</td></tr>`;
    const wRows=pd.inventory_warn.length?pd.inventory_warn.map(w=>{
      const cls=w.status==='紧急'?'tag-red':'tag-amber';
      return `<tr><td>${w.name}${w.ipn?' <span class="sub">'+w.ipn+'</span>':''}</td>
        <td class="num">${w.confirmed}</td><td class="num">${w.minamount}</td>
        <td class="num">${w.gap}</td><td><span class="pill ${cls}">${w.status}</span></td></tr>`;
    }).join(''):`<tr><td colspan="5" class="empty">✅ 暂无低于安全库存的零件（当前 ${pd.part_count} 个零件均未设置安全库存线 minamount）</td></tr>`;
    /* 缺料缺口 Top：改用 barList —— 每条自带料号与件数，不用再回头看表格对号。
       红＝零确认库存（必须新采购），橙＝部分缺口。 */
    const gapItems=(b&&b.shortage||[]).slice().sort((a,b2)=>b2.gap-a.gap).slice(0,10)
      .map(x=>({l:x.name,v:x.gap,
        txt:x.gap+" 件"+((x.ipn)?" · "+x.ipn:""),
        sev:x.confirmed===0?"bad":"warn"}));
    const gapHTML=gapItems.length?`<div style="margin-top:16px">
      <h3 style="font-size:13px;color:var(--sub);font-weight:600;margin-bottom:8px">缺口 Top（按缺口件数）</h3>
      ${barList(gapItems)}
      <div class="viz-note">缺口 = 需求 − 已确认库存，按 ${b.qty} 套 BOM 计算。红＝零确认库存（必须新采购），橙＝部分缺口。</div></div>`:"";
    pdHTML=`<div class="card" style="margin-top:14px"><div class="pd-head">
        <h3>在产缺料检查 · ${b?b.project_name:''}（${b?b.qty:''} 套）</h3>
        <span class="badge-real">PartDB 实时 · ${pd.generated_at}</span></div>
      <div class="pd-stats">
        <div><b>${pd.part_count}</b><span>零件总数</span></div>
        <div><b class="${b&&b.shortage.length?'c-red':'c-green'}">${b?b.shortage.length:0}</b><span>在产缺料</span></div>
        <div><b class="${pd.zero_confirmed?'c-amber':''}">${pd.zero_confirmed}</b><span>零确认库存</span></div>
        <div><b>${b?yuan(b.single_set_cost):'—'}</b><span>单套BOM成本</span></div>
      </div>
      <div style="overflow-x:auto"><table data-paginate="8" data-filter="1"><thead><tr><th>物料</th><th>单套</th><th>需求</th><th>确认库存</th><th>缺口</th><th>单价</th><th>风险</th></tr></thead>
        <tbody>${shRows}</tbody></table></div>
      ${gapHTML}
      <div class="note">缺料 = 需求 − 已确认库存（仅计 description 含盘点日期 MDD/MMDD 的批次）。单套成本按最低有效报价汇总，${b?b.unpriced_count:0} 种无价未计入。</div>
    </div>
      <div class="card" style="margin-top:14px"><h3>库存健康 · 安全库存预警（minamount）</h3>
      <div style="overflow-x:auto"><table data-paginate="8" data-filter="1"><thead><tr><th>物料</th><th>确认库存</th><th>安全库存</th><th>缺口</th><th>状态</th></tr></thead>
        <tbody>${wRows}</tbody></table></div>
    </div>`;
  }else{
    pdHTML=`<div class="card" style="margin-top:14px"><h3>物料库存预警（库存核对记录）</h3>
      <div style="overflow-x:auto"><table data-paginate="8" data-filter="1"><thead><tr><th>关联计划</th><th>核对结果</th><th>完成时间</th><th>状态</th></tr></thead>
      <tbody>${invRows}</tbody></table></div>
      <div class="note">⚠ PartDB 未配置，BOM 级缺料检查已跳过；以上仅基于「库存核对记录」的最终完成状态判定。</div></div>`;
  }
  const secSup=el(`<section id="sec-Sup" class="sec"><div class="sec-title">__IC_SUP__ 供应链（采购）</div>
    ${supViz}
    <div class="grid g2" style="margin-top:22px">
      <div class="card"><h3>采购逾期（${s.overdue_list.length}）</h3>
        <div style="overflow-x:auto"><table data-paginate="8" data-filter="1" data-select="1"><thead><tr><th>供应商</th><th>物料</th><th>关联计划</th><th>预计到货</th><th>状态</th></tr></thead>
        <tbody>${odRows}</tbody></table></div></div>
      <div class="card"><h3>供应商交期准确率</h3>
        <div style="overflow-x:auto"><table data-paginate="8" data-filter="1" data-select="1"><thead><tr><th>供应商</th><th>订单</th><th>准/迟/待</th><th>准时率</th><th>累计花销</th></tr></thead>
        <tbody>${supRows}</tbody></table></div>
        ${supBarsHTML?`<div style="margin-top:14px"><h3 style="font-size:13px;color:var(--sub);font-weight:600;margin-bottom:2px">准时率排行（最差优先）</h3>${supBarsHTML}</div>`:""}</div>
    </div></section>`);
  const secInv=el(`<section id="sec-Inv" class="sec"><div class="sec-title">__IC_BOX__ 物料库存预警（PartDB 实时）</div>
    ${pdHTML}</section>`);
  put("Sup", secSup);
  put("Inv", secInv);

  /* 资源负载（人/设备）：对标 MS Project 资源工作表 —— 谁超载、谁闲置、谁撞期 */
  const res=m.resource;
  if(res){
    const loadBar=(r)=>{
      const w=Math.min(r.load,150)/150*100;
      const cls=r.load>100?"over":(r.load>=70?"busy":(r.load>0?"ok":"idle"));
      return `<div class="rs-bar"><div class="rs-track"><div class="rs-fill ${cls}" style="width:${w.toFixed(1)}%"></div></div>
        <span class="rs-num ${r.load>100?'neg':''}">${r.load}%</span></div>`;
    };
    const resRows=res.rows.length?res.rows.map(r=>`<tr>
      <td><b>${r.name}</b><div class="rs-sub">${r.type}${r.stage?" · "+r.stage:""}</div></td>
      <td><span class="pill ${r.status==="在岗"?"tag-green":(r.status==="未登记"?"tag-amber":"tag-red")}">${r.status}</span></td>
      <td style="min-width:190px">${loadBar(r)}</td>
      <td class="num">${r.week_alloc} / ${r.capacity}<div class="rs-sub">${r.unit}</div></td>
      <td class="num">${r.rate?yuan(r.rate):"—"}</td>
      <td class="num">${yuan(r.cost)}</td>
      <td>${r.conflicts.length?`<span class="pill tag-red">${r.conflicts.length} 处冲突</span>`:
           (r.over?`<span class="pill tag-red">超载</span>`:
           (r.idle?`<span class="pill tag-amber">闲置</span>`:`<span class="pill tag-green">正常</span>`))}</td>
    </tr>`).join(""):`<tr><td colspan="7" class="empty">尚未录入资源。对我说「新增资源：张三，贴片工序，日产能 1，日费率 400」即可建档。</td></tr>`;

    const confHTML=res.conflicts.length?`<div class="card" style="margin-top:14px">
      <h3>排程冲突（同一资源同期被排了多个任务）</h3>
      <div style="overflow-x:auto"><table data-paginate="6"><thead><tr><th>资源</th><th>任务 A</th><th>任务 B</th><th>重叠天数</th></tr></thead>
      <tbody>${res.conflicts.map(c=>`<tr><td>${c.name}</td><td>${c.a}</td><td>${c.b}</td>
        <td class="num neg">${c.days} 天</td></tr>`).join("")}</tbody></table></div>
      <div class="note">重叠 = 两条「计划中/进行中」的分配在日期区间上相交。需改期、拆分投入量，或换人。</div></div>`:"";

    const planCostHTML=res.plan_cost.length?`<div class="card" style="margin-top:14px">
      <h3>各生产计划人工投入</h3>
      <div style="overflow-x:auto"><table data-paginate="8"><thead><tr><th>生产计划</th><th>投入人力</th><th>投入量</th><th>人工成本</th></tr></thead>
      <tbody>${res.plan_cost.map(p=>`<tr><td>${p.plan}</td><td class="num">${p.people} 人</td>
        <td class="num">${p.qty}</td><td class="num">${yuan(p.cost)}</td></tr>`).join("")}</tbody></table></div>
      <div class="note">人工成本 = Σ(投入量 × 该资源日费率)，可与「成本分析」里的物料花销合并看总成本。</div></div>`:"";
    /* 资源负载排行条形图（超载优先）*/
    const loadItems=res.rows.slice().sort((a,b)=>b.load-a.load).slice(0,10)
      .map(r=>({name:r.name,value:Math.round(r.load),
        color:r.load>100?"var(--red)":(r.load>=70?"var(--amber)":(r.load>0?"var(--green)":"var(--bd)"))}));
    const loadHTML=loadItems.length?`<div class="card" style="margin-top:14px">
      <h3>负载排行（超载优先）</h3>${bars(loadItems,{fmt:v=>v+"%"})}
      <div class="note">红=超载(&gt;100%) · 橙=繁忙(≥70%) · 绿=正常 · 灰=闲置。</div></div>`:"";

    const secRs=el(`<section id="sec-Rs" class="sec"><div class="sec-title">__IC_USER__ 资源负载与分配（${res.week.start} ~ ${res.week.end}）</div>
      <div class="card">
        <div class="rs-sum">
          <span>在岗 <b>${res.on_duty}</b>/${res.total}</span>
          <span>平均负载 <b class="${res.avg_load>100?'neg':''}">${res.avg_load}%</b></span>
          <span>超载 <b class="${res.over.length?'neg':''}">${res.over.length}</b></span>
          <span>冲突 <b class="${res.conflicts.length?'neg':''}">${res.conflicts.length}</b></span>
          <span>人工成本 <b>${yuan(res.labor_cost)}</b></span>
        </div>
        <div style="overflow-x:auto"><table data-paginate="10" data-filter="1"><thead><tr>
          <th>资源</th><th>状态</th><th>本周负载</th><th>已排/产能</th><th>日费率</th><th>累计成本</th><th>判定</th>
        </tr></thead><tbody>${resRows}</tbody></table></div>
        <div class="note">负载率 = 本周已排投入量 ÷（日产能 × 本周工作日 ${res.week.workdays} 天）。
          <b style="color:var(--red)">红=超载(&gt;100%)</b>、橙=繁忙(≥70%)、绿=正常、灰=闲置。
          「未登记」表示该人只出现在分配记录里、资源表中无档案，建议补建档。</div>
      </div>
      ${loadHTML}
      ${confHTML}
      ${planCostHTML}
    </section>`);
    put("Rs", secRs);
  }

  /* 物料行情（价格涨跌 + 停产/EOL）：market.py 维护的本地监控数据 */
  const mk=m.market;
  if(mk){
    const lcBadge=(lc)=>{
      const bad=(lc==="EOL停产"||lc==="停产");
      const cls=(lc==="在产")?"tag-green":(lc==="NRND"?"tag-amber":(bad?"tag-red":""));
      return cls?`<span class="pill ${cls}">${lc}</span>`:`<span class="pill">${lc||"未知"}</span>`;};
    const spark=(pts)=>{
      if(!pts||pts.length<2) return `<span class="rs-sub">待跟踪</span>`;
      const mn=Math.min(...pts), mx=Math.max(...pts), rg=(mx-mn)||1;
      const P=pts.map((v,i)=>`${(i/(pts.length-1)*86+2).toFixed(1)},${(23-(v-mn)/rg*20).toFixed(1)}`).join(" ");
      const up=pts[pts.length-1]>=pts[0];
      return `<svg width="90" height="26" viewBox="0 0 90 26"><polyline points="${P}" fill="none" stroke="${up?'#e03131':'#2f9e44'}" stroke-width="1.6"/></svg>`;};
    const mktRows=mk.rows.length?mk.rows.map(r=>{
      const vb=r.vs_buy;
      const vbTxt=(vb!=null)
        ?`<span style="color:${vb>=0?'var(--red)':'var(--green)'};font-weight:600">${vb>=0?"+":""}${vb.toFixed(1)}%</span>`
        :"—";
      return `<tr><td><b>${r.model}</b><div class="rs-sub">${r.category}</div></td>
        <td class="num">${r.buy?("¥"+r.buy):"—"}<div class="rs-sub">${r.buy_date||""}</div></td>
        <td class="num">${r.price?("¥"+r.price):"—"}<div class="rs-sub">${r.channel||""}</div></td>
        <td class="num">${vbTxt}<div class="rs-sub">${r.chg?("环比 "+r.chg+"%"):""}</div></td>
        <td>${lcBadge(r.lifecycle)}</td>
        <td>${spark(r.hist)}</td>
        <td class="rs-sub">${r.date||"待首检"}</td></tr>`;}).join("")
      :`<tr><td colspan="7" class="empty">监控清单为空。在技能目录运行 python domain/market.py watchlist 从采购记录生成；行情由每周一自动检查回填。</td></tr>`;
    const mktAlerts=mk.alerts.length?`<div class="card" style="margin-top:14px">
      <h3>行情告警（${mk.alerts.length}）</h3><div class="actions">${mk.alerts.map(a=>
        `<div class="act"><span class="pri ${a.type==='停产'?'pri-高':'pri-中'}">${a.type}</span>
         <span class="tx"><b>${a.model}</b>：${a.text}</span></div>`).join("")}</div>
      <div class="note">停产/NRND = 高优（确认替代料、锁定最后采购窗口）；涨跌超阈值 = 中优（评估提前备货或换源）。</div></div>`:"";
    const secMkt=el(`<section id="sec-Mkt" class="sec"><div class="sec-title">__IC_MKT__ 物料行情（价格涨跌 · 停产/EOL）</div>
      <div class="card"><div style="overflow-x:auto"><table data-paginate="12" data-filter="1"><thead><tr>
        <th>物料型号</th><th>上次采购价</th><th>最新行情</th><th>vs采购价</th><th>生命周期</th><th>趋势</th><th>行情日期</th>
      </tr></thead><tbody>${mktRows}</tbody></table></div>
      <div class="note">红=涨价、绿=降价（采购成本视角）。vs采购价偏差 ≥ ±${mk.threshold}% 或生命周期 NRND/EOL停产 触发告警。行情数据由每周一自动检查（立创/示例采购平台A等渠道现货价 + 原厂生命周期公告），也可随时对我说「查一下 XX 的行情」即时补录。</div></div>
      ${mktAlerts}</section>`);
    put("Mkt", secMkt);
  }

  /* 原料行情（上游成本）：commodities.py 维护的 data/原料行情记录.csv */
  const raw=m.commodities;
  if(raw){
    const rawSpark=(pts)=>{
      if(!pts||pts.length<2) return `<span class="rs-sub">待积累</span>`;
      const mn=Math.min(...pts), mx=Math.max(...pts), rg=(mx-mn)||1;
      const P=pts.map((v,i)=>`${(i/(pts.length-1)*86+2).toFixed(1)},${(23-(v-mn)/rg*20).toFixed(1)}`).join(" ");
      const up=pts[pts.length-1]>=pts[0];
      return `<svg width="90" height="26" viewBox="0 0 90 26"><polyline points="${P}" fill="none" stroke="${up?'#e03131':'#2f9e44'}" stroke-width="1.6"/></svg>`;};
    const pctTxt=(p)=>(p==null)?`<span class="rs-sub">—</span>`
      :`<span style="color:${p>=0?'var(--red)':'var(--green)'};font-weight:600">${p>=0?"+":""}${p.toFixed(1)}%</span>`;
    const basisPill=(b)=>{
      const isCont=b==="连续";
      return `<span class="pill ${isCont?'tag-green':''}">${b||"连续"}</span>`;};
    const rawRows=raw.rows.length?raw.rows.map(r=>`<tr>
        <td><b>${r.name}</b><div class="rs-sub">${r.category||""}</div></td>
        <td>${basisPill(r.basis)}</td>
        <td class="num">${r.price||"—"}<div class="rs-sub">${r.unit||""}</div></td>
        <td class="num">${pctTxt(r.pct)}<div class="rs-sub">近 ${raw.days} 天</div></td>
        <td>${rawSpark(r.hist)}</td>
        <td class="rs-sub">${r.date||""}</td>
        <td class="rs-sub">${r.source||""}</td></tr>`).join("")
      :`<tr><td colspan="7" class="empty">暂无原料行情。在技能目录运行 python domain/market.py raw fetch 拉取当日价，raw backfill AU --contract au2610 --days 40 补历史。</td></tr>`;
    const rawAlerts=raw.alerts.length?`<div class="card" style="margin-top:14px">
      <h3>原料波动告警（${raw.alerts.length}）</h3><div class="actions">${raw.alerts.map(a=>
        `<div class="act"><span class="pri ${a.type==='涨'?'pri-高':'pri-中'}">${a.type}</span>
         <span class="tx"><b>${a.name}</b>：${a.text}</span></div>`).join("")}</div>
      <div class="note">原料波动影响的是<b>整体报价基调</b>，不是单个料号——涨了复核供应商报价有效期与备货节奏，跌了可择机锁价。</div></div>`:"";
    /* 原料波动榜：按涨跌**绝对值**排序，一眼看出这 30 天哪几个品种在动。 */
    const rawBars=(raw.rows||[]).slice()
      .filter(r=>r.pct!=null&&isFinite(Number(r.pct)))
      .sort((a,b2)=>Math.abs(b2.pct)-Math.abs(a.pct)).slice(0,10)
      .map(r=>({l:r.name,v:Math.abs(Number(r.pct)),
        txt:(r.pct>=0?"+":"")+Number(r.pct).toFixed(1)+"% · "+(r.price||"—"),
        sev:Math.abs(r.pct)>=raw.threshold*1.5?"bad":(Math.abs(r.pct)>=raw.threshold?"warn":"ok")}));
    const rawViz=rawBars.length?vizCard(`原料波动榜（近 ${raw.days} 天）`,barList(rawBars),
      {q:`阈值 ±${raw.threshold}% · 共 ${raw.rows.length} 个品种`,
       note:"红＝波动 ≥ 阈值的 1.5 倍，橙＝已超阈值。<b>口径纪律</b>：期货「连续」与「具体合约」是两个口径，<b>不跨口径比价</b> —— 混比会算出假涨跌。"}):"";
    const secRaw=el(`<section id="sec-Raw" class="sec"><div class="sec-title">__IC_MKT__ 原料行情（上游成本 · 金属 / 塑料）</div>
      ${rawViz}
      <div class="card" style="margin-top:22px"><div style="overflow-x:auto"><table data-paginate="12" data-filter="1"><thead><tr>
        <th>原料</th><th>口径</th><th>最新价</th><th>区间涨跌</th><th>趋势</th><th>日期</th><th>来源</th>
      </tr></thead><tbody>${rawRows}</tbody></table></div>
      <div class="note">红=涨、绿=跌（成本视角）。近 ${raw.days} 天波动 ≥ ±${raw.threshold}% 触发告警（原料波动比单个料号频繁，阈值单独设，默认 5%）。
        <b>口径纪律</b>：期货「连续」与「具体合约」是两个口径，同原料出现多行属正常，<b>不跨口径比价</b>——混比会算出假涨跌。
        ABS/PC/PS 树脂现货无免费公开 API，走人工录入（<code>python domain/market.py raw add ABS --price 11800</code>）。</div></div>
      ${rawAlerts}</section>`);
    put("Raw", secRaw);
  }

  /* 微信情报台：wechat_intake.py 维护的 data/微信事件.csv */
  const wx=m.wechat;
  window.__WX_EVENTS = {};
  (wx&&wx.pending||[]).forEach(e=>{ if(e&&e["事件编号"]) window.__WX_EVENTS[e["事件编号"]]=e; });
  if(wx){
    const catPill=(c)=>`<span class="pill ${c==='停产通知'?'tag-red':((c==='价格变动'||c==='交期变更')?'tag-amber':'')}">${c||"其他"}</span>`;
    const pendRows=wx.pending.length?wx.pending.map(e=>`<tr>
        <td style="text-align:center;width:34px"><input type="checkbox" class="wx-sel" value="${e["事件编号"]||""}"></td>
        <td><b>${e["事件编号"]||""}</b><div class="rs-sub">${e["日期"]||""} ${e["时间"]||""}</div></td>
        <td>${(e["来源群"]||"").slice(0,16)}<div class="rs-sub">${(e["发送人"]||"").slice(0,12)}</div></td>
        <td>${catPill(e["分类"])}</td>
        <td style="max-width:340px">${(e["原文"]||"").slice(0,110)}</td>
        <td><span class="pill tag-red">待确认</span></td></tr>`).join("")
      :`<tr><td colspan="6" class="empty">暂无待确认事件。每日 9 点自动拉取微信监控群新消息并提取事件；「示例」开头的演示数据可运行 python wx/wechat_intake.py clear-demo 清除。</td></tr>`;
    const catStat=Object.entries(wx.by_cat||{}).map(([c,n])=>
      `<span class="pill" style="margin-right:6px">${c} ${n}</span>`).join("")
      ||`<span class="rs-sub">近 7 天无事件</span>`;
    const recRows=(wx.recent||[]).slice(0,10).map(e=>`<tr>
        <td class="rs-sub">${e["日期"]||""}</td><td>${(e["来源群"]||"").slice(0,14)}</td>
        <td>${catPill(e["分类"])}</td>
        <td style="max-width:300px">${(e["原文"]||"").slice(0,80)}</td>
        <td class="rs-sub">${e["状态"]||""}${e["写入结果"]?` · ${(e["写入结果"]||"").slice(0,28)}`:""}</td></tr>`).join("");
    const secWXC=el(`<section id="sec-WXC" class="sec"><div class="sec-title">__IC_CHAT__ 微信情报台（今日 ${wx.today_count} 条 · 待确认 ${wx.pending_count} 条）</div>
      <div class="card"><h3>待确认事件（勾选 → 确认写 SeaTable / 忽略）</h3>
        <div style="overflow-x:auto"><table data-paginate="8" data-filter="1"><thead><tr>
          <th style="width:34px;text-align:center">✓</th><th>编号/时间</th><th>来源</th><th>分类</th><th>原文</th><th>状态</th>
        </tr></thead><tbody>${pendRows}</tbody></table></div>
        <div class="wx-actions" style="display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-top:10px">
          <button class="btn-primary" onclick="wxApproveSelected()">✅ 确认选中（写 SeaTable）</button>
          <button class="btn-ghost" onclick="wxIgnoreSelected()">🚫 忽略选中</button>
          <span class="rs-sub">勾选事件后点「确认选中」：拉起 WorkBuddy 专家执行 <code>wechat_intake.py approve</code>，按意图(op=update/append)写对应业务表行（交期/价格/数量等自动落位），结果回填留痕；「忽略选中」执行 ignore。写入前专家会再向你确认一遍（技能铁律）。</span>
        </div>
        <div class="note">也可在对话里直接说「确认 WXxxx-001」/「忽略 WXxxx-001」。来源：win-wechat-summary 本地微信库（只读，零封号风险）。</div></div>
      <div class="card" style="margin-top:14px"><h3>近 7 天事件分布</h3>${catStat}
        <div class="note" style="margin-top:8px">分类：交期变更 / 价格变动 / 停产通知 / 催货 / 进度 / 库存 / 其他。</div></div>
      ${recRows?`<div class="card" style="margin-top:14px"><h3>最近事件（留痕）</h3>
        <div style="overflow-x:auto"><table data-paginate="10" data-filter="1"><thead><tr>
          <th>日期</th><th>群</th><th>分类</th><th>原文</th><th>状态/结果</th>
        </tr></thead><tbody>${recRows}</tbody></table></div></div>`:""}
    </section>`);
    put("WXC", secWXC);
  }

  /* 消息↔SeaTable 核对台：wxmatch.py 维护的 data/核对结果.csv
     （群消息收款/下单/合同PDF ↔ 项目表、供应商合同 ↔ 采购记录表，只读核对不写库） */
  const wmatch=m.wxmatch;
  if(wmatch){
    const typeBadge=(t)=>{
      const cls = (t==="收款") ? "tag-red" : ((t==="下单") ? "tag-amber" : "");
      return `<span class="pill ${cls}">${t||"其他"}</span>`;
    };
    const confBadge=(c)=>{
      const cls = (c==="高") ? "tag-red" : ((c==="中") ? "tag-amber" : "");
      return `<span class="pill ${cls}">${c||"低"}</span>`;
    };
    const wmRows=wmatch.pending.length?wmatch.pending.map(r=>`<tr>
        <td><b>${r["核对编号"]||""}</b><div class="rs-sub">${(r["日期"]||"").slice(0,10)}</div></td>
        <td>${typeBadge(r["类型"])}</td>
        <td style="max-width:300px" title="${(r["信号内容"]||"").replace(/"/g,"&quot;")}">${(r["信号内容"]||"").slice(0,44)}</td>
        <td style="max-width:200px">${r["匹配项目"]?`<b>${r["匹配项目"].slice(0,20)}</b>`:`<span class="rs-sub">${(r["匹配结果"]||"未匹配").slice(0,26)}</span>`}</td>
        <td>${(r["建议动作"]||"").slice(0,30)}${r["预填意图"]?`<div class="rs-sub">有预填意图</div>`:""}</td>
        <td>${confBadge(r["置信度"])}</td></tr>`).join("")
      :`<tr><td colspan="6" class="empty">暂无待核对项。运行 python wx/wxmatch.py scan 扫描监控群消息与合同 PDF。</td></tr>`;
    const typeStat=Object.entries(wmatch.by_type||{}).map(([t,n])=>
      `<span class="pill" style="margin-right:6px">${t} ${n}</span>`).join("")
      ||`<span class="rs-sub">暂无分类数据</span>`;
    const confStat=Object.entries(wmatch.by_conf||{}).map(([c,n])=>
      `<span class="pill ${c==="高"?"tag-red":(c==="中"?"tag-amber":"")}" style="margin-right:6px">${c}置信 ${n}</span>`).join("")
      ||`<span class="rs-sub">暂无置信度数据</span>`;
    const secWXM=el(`<section id="sec-WXM" class="sec"><div class="sec-title">__IC_CHECK__ 消息↔SeaTable 核对台（待核对 ${wmatch.pending_count} 条 · 已处置 ${wmatch.done_count} 条）</div>
      <div class="card"><h3>待核对项（收款 / 下单 / 客户合同 / 供应商合同 ↔ 业务表）</h3>
        <div style="overflow-x:auto"><table data-paginate="10" data-filter="1" data-select="1"><thead><tr>
          <th>编号/日期</th><th>类型</th><th>信号内容</th><th>匹配项目/结果</th><th>建议动作</th><th>置信度</th>
        </tr></thead><tbody>${wmRows}</tbody></table></div>
        <div class="note"><b>处置方式</b>：在 WorkBuddy 对话里说「核对 WX-M-xxxx 处理」或「忽略 WX-M-xxxx」，专家执行 wxmatch.py done 落留痕；高置信收款项确认后由 wechat_intake approve 写入项目「实收」列。扫描频率：每日 9 点自动化随微信拉取一起跑 <code>python wx/wxmatch.py scan</code>。核对引擎<b>只读</b>，绝不自动写 SeaTable。</div></div>
      <div class="card" style="margin-top:14px"><h3>分类 / 置信度分布</h3>
        <div>${typeStat}</div><div style="margin-top:8px">${confStat}</div>
        <div class="note" style="margin-top:8px">四类核对：① 收款（已打款/到账等信号 ↔ 项目待收金额 ±2% 容差）② 下单（新订单信号 ↔ 项目表客户名，匹配不到提示漏立项）③ 客户合同PDF（微信收到的合同文件 ↔ 项目表合同列）④ 供应商合同（采购合同 ↔ 5 张采购记录表供应商，含别名归一）。上次扫描：${wmatch.scan_date||"未知"}。</div></div>
    </section>`);
    put("WXM", secWXM);
  }

  /* 风险雷达：foresee.py 维护的 data/foresee.json
     （合同倒排 / 供应商交期画像 / 缺料预警 三路预测，只读展示） */
  const fc=m.foresee;
  if(fc){
    const fb=fc.backward||{}, fs=fc.supplier||{}, fsh=fc.shortage||{};
    const vBadge=(v)=>{
      const cls=(v==="已逾期"||v==="必须立刻下单"||v==="在途来不及")?"tag-red":
                ((v==="高风险")?"tag-amber":((v==="偏紧")?"tag-amber":""));
      return `<span class="pill ${cls}">${v}</span>`;
    };
    const bkRows=(fb.plans||[]).map(r=>{
      if(r.verdict==="缺数据")
        return `<tr><td><b>${r.plan}</b></td><td>${r.product}</td><td colspan="5" class="rs-sub">${r.note}</td></tr>`;
      const stageBadges=(r.stages||[]).filter(s=>s.urgent)
        .map(s=>`<span class="pill tag-red" style="margin:1px 2px">${s.stage} 已过最晚开始</span>`).join("");
      return `<tr>
        <td><b>${r.plan}</b><div class="rs-sub">${r.product}</div></td>
        <td>${r.due}<div class="rs-sub">${r.basis}</div></td>
        <td class="${r.days_left<0?"neg":""}"><b>${r.days_left}</b> 天</td>
        <td>${r.est_cycle!=null?r.est_cycle+" 天":"—"}</td>
        <td>${vBadge(r.verdict)}</td>
        <td style="max-width:340px">${r.note}${stageBadges?`<div style="margin-top:2px">${stageBadges}</div>`:""}</td></tr>`;
    }).join("")||`<tr><td colspan="6" class="empty">暂无在制计划。运行 <code>python domain/foresee.py</code> 生成风险预测。</td></tr>`;
    const supRows=(fs.cat_order||[]).map(c=>{
      const cp=fs.cat_profile[c]||{};
      const bad=(fs.sup_detail[c]||[]).filter(x=>x.mean>5).slice(0,4)
        .map(x=>`<span class="pill tag-amber" style="margin:1px 2px">${x.supplier.slice(0,12)} +${x.mean}天(最差+${x.max})</span>`).join("");
      return `<tr>
        <td><b>${c}</b></td><td>${cp.n||0}</td>
        <td class="${(cp.mean||0)>10?"neg":""}"><b>+${cp.mean!=null?cp.mean:"—"} 天</b></td>
        <td>+${cp.max!=null?cp.max:"—"} 天</td>
        <td><b>+${cp.buffer||0} 天</b></td>
        <td>${bad||'<span class="rs-sub">无显著风险供应商</span>'}</td></tr>`;
    }).join("")||`<tr><td colspan="6" class="empty">暂无供应商交期数据</td></tr>`;
    const shRows=(fsh.plans||[]).map(e=>{
      const gaps=(e.top_gaps||[]).slice(0,3)
        .map(g=>`<span class="pill ${g.confirmed===0?"tag-red":""}" style="margin:1px 2px">${g.name} ${g.confirmed}/${g.need}</span>`).join("");
      return `<tr>
        <td><b>${e.product}</b>${e.plan?`<div class="rs-sub">${e.plan}</div>`:""}${e.matched?"":'<div class="rs-sub">未关联生产计划</div>'}</td>
        <td>${e.due||"—"}</td>
        <td class="neg"><b>${e.gap_items}</b> 项</td>
        <td>${e.total_gap} 件 / 零库存 ${e.zero_stock_items}</td>
        <td>${e.intransit_n?`${e.intransit_n} 条<div class="rs-sub">ETA ${e.intransit_eta||"未知"}</div>`:"<b class='neg'>无在途</b>"}</td>
        <td>${vBadge(e.verdict)}</td>
        <td style="max-width:280px">${gaps||'<span class="rs-sub">—</span>'}</td></tr>`;
    }).join("")||`<tr><td colspan="7" class="empty">活动计划无缺料缺口（PartDB 快照 ${fsh.snapshot_at||"缺失"}）</td></tr>`;
    const secFC=el(`<section id="sec-FC" class="sec"><div class="sec-title">__IC_RADAR__ 风险雷达（合同倒排 · 供应商画像 · 缺料预警 · 生成于 ${fc.generated_at||"?"}）</div>
      <div class="card"><h3>【1】合同倒排 — 按历史工期预警（样本 n=${fb.hist_n||0}，中位 ${fb.hist_median||"—"} / p75 ${fb.hist_p75||"—"} / p90 ${fb.hist_p90||"—"} 天）</h3>
        <div style="overflow-x:auto"><table data-paginate="10" data-filter="1"><thead><tr>
          <th>计划/产品</th><th>目标交期</th><th>剩余</th><th>历史周期 p75</th><th>风险</th><th>环节预警</th>
        </tr></thead><tbody>${bkRows}</tbody></table></div>
        <div class="note">周期基准取<b>已交付计划的实际工期</b>（立项→交货）而非合同承诺；剩余天数 &lt; p75 即高风险。环节链：${(fb.plans&&fb.plans[0]&&fb.plans[0].stages?fb.plans[0].stages.map(s=>s.stage+"(提前"+s.lead+"天)").join(" → "):"BOM核对→IC/PCB采购→组装料采购→贴片组装→测试发货")}。生成命令：<code>python domain/foresee.py</code>。</div></div>
      <div class="card" style="margin-top:14px"><h3>【2】供应商交期画像 — 承诺 vs 实际（样本 ${fs.samples||0} 条）</h3>
        <div style="overflow-x:auto"><table><thead><tr>
          <th>类别</th><th>样本</th><th>平均偏差</th><th>最差</th><th>建议 buffer</th><th>风险供应商</th>
        </tr></thead><tbody>${supRows}</tbody></table></div>
        <div class="note">偏差 = 实际交期 − 承诺交期（正数=比承诺晚）。排程/缺料 ETA 计算时自动加该类别 buffer；对平均偏差 &gt;5 天的供应商下单时承诺交期需打折看待。</div></div>
      <div class="card" style="margin-top:14px"><h3>【3】缺料预警 — BOM 缺口 × 在途采购（PartDB 快照 ${fsh.snapshot_at||"缺失"}）</h3>
        <div style="overflow-x:auto"><table data-paginate="10" data-filter="1"><thead><tr>
          <th>产品/计划</th><th>交期</th><th>缺口</th><th>缺口量</th><th>在途</th><th>结论</th><th>主要缺口料</th>
        </tr></thead><tbody>${shRows}</tbody></table></div>
        <div class="note">在途 ETA = 下单日 + 承诺交期 + 类别 buffer。「必须立刻下单」= 有缺口且无任何在途；「在途来不及」= 在途 ETA 晚于合同交期，需催货或加急补单。BOM 缺口来自 <code>python sync/partdb_sync.py</code>，之后重跑 <code>python domain/foresee.py</code>。</div></div>
    </section>`);
    put("FC", secFC);
  }

  /* ── 全链案件（L2-0 接线）───────────────────────────────────────────────
     这条链一直存在（domain/order_to_cash.py，落 data/business_loop/*.csv），
     但驾驶舱只读 SeaTable，所以「微信来单 → CRM → 商机 → 需求/方案/报价版本 →
     合同 → 立项 → 采购 → 排产 → 生产 → 出货 → 验收回款 → 售后」一直看不见。
     这一块第一次把它显出来：16 态漏斗 + 案件卡 + 状态停留时长。

     ⚠️ 两条诚实标注**不能省**（少了界面就会谎报经营现状）：
        · 数据停更 —— 不写，就会把 19 天前的链路状态当成今天的；
        · 预演空壳 —— 对象构成完全对称、无一越过「立项回款」= 不是真实业务流。 */
  const _loop=m.loop;
  if(_loop && tabVisible(ok,"LC")){
    const lp=_loop;
    const ZH={}; lp.funnel.forEach(f=>ZH[f.state]=f.zh);
    const TYPE_ZH={customer:"客户",lead:"线索",opportunity:"商机",requirement:"需求",
      solution:"方案",quote:"报价",contract:"合同",project:"项目",
      production_order:"生产订单",purchase_order:"采购订单",shipment:"发货",after_sales:"售后"};

    /* 数据来源诚实标注 */
    const warns=[];
    if(lp.stale){
      warns.push(`<b>控制平面已停更 ${lp.stale_days} 天</b>（最后写入 ${lp.updated_at}）——
        下面显示的不是今天的链路状态。已查明两部分原因：① <code>partdb_sync</code> 返回 502 时
        <code>failure_policy="abort"</code> 把后续 9 步整链带走（23 次运行里 <code>loop_sync</code> 只跑通 6 次）；
        ② 唯一的增量入口 <code>workflows/loop_trigger.py</code>（微信来单 → 建案件）<b>从未接入 DAG</b>，
        所以即使跑通也只是把同一批数据反复镜像。`);
    }
    if(lp.scenario){
      warns.push(`<b>当前为预演数据，不是真实业务流</b>：${lp.case_count} 个案件中
        <b>${lp.seeded_cases} 个</b>恰好只含「客户 + 线索 + 商机」这同一套三件套
        （${(lp.seeded_cases/Math.max(1,lp.case_count)*100).toFixed(0)}%，人工逐单建立不会这么齐），
        且全库<b>无一对象越过「立项回款」</b>（合同 / 项目 / 生产订单 / 采购订单 / 发货 / 售后对象数均为 0）。
        请勿据此判断经营现状。`);
    }
    const warnHtml=warns.length?`<div class="card" style="border-left:3px solid var(--amber);margin-bottom:14px">
      <h3 style="color:var(--amber)">⚠️ 数据可信度提示</h3>
      <div class="note" style="margin-top:6px">${warns.map(w=>"· "+w).join("<br>")}</div></div>`:"";

    /* 四个常驻指标 */
    const advanced=lp.advanced_n||0;
    const stuckAll=lp.stuck.length+lp.overdue.length;
    const dw=Object.keys(lp.dwell_avg||{}).map(k=>lp.dwell_avg[k]);
    const dwMax=dw.length?Math.max.apply(null,dw):0;
    const kpis=[
      statCard({l:"在跑案件", v:lp.case_count, u:"单", sev:stuckAll?"warn":"ok",
        sub:`${lp.object_count} 个业务对象 · ${lp.transition_count} 条状态轨迹`}),
      statCard({l:"已越过立项回款", v:advanced, u:"个对象", sev:advanced?"ok":"bad",
        nsev:advanced?"ok":"bad",
        sub:advanced?"链路已进入交付段":"全部卡在立项之前"}),
      statCard({l:"停滞/逾期案件", v:stuckAll, u:"单", sev:stuckAll?"bad":"ok",
        nsev:(stuckAll&&!lp.stale)?"bad":"",
        sub:`停留 &gt; ${lp.stuck_days} 天算停滞 · 逾期 ${lp.overdue.length} 单${lp.stale?"（含停更期，非真实停滞）":""}`}),
      statCard({l:"最长状态停留", v:dwMax, u:"天", sev:dwMax>lp.stuck_days?"bad":"ok",
        nsev:dwMax>lp.stuck_days?"bad":"ok",
        sub:"由末次状态迁移时间推算"}),
    ].join("");

    /* 16 态漏斗：形状本身就是结论 —— 全挤在最前面几态 */
    let _mi=0;
    const funRows=lp.funnel.map(f=>{
      if(f.main) _mi++;
      const w=f.n?Math.max(3,Math.round(f.n/Math.max(1,lp.max_funnel)*100)):0;
      const col=f.n===0?"transparent":(f.main?"var(--primary)":"var(--amber)");
      // 主链编号 01~14；旁路两态不占主链序号，直接标「旁路 · 取消/售后」
      const label=f.main?`${String(_mi).padStart(2,"0")} ${esc(f.zh)}`:`旁路 · ${esc(f.zh)}`;
      return `<div class="bl-row">
        <span class="bl-lbl" title="${esc(f.state)}">${label}</span>
        <span class="bl-val">${f.n?`<b>${f.n}</b> 单`:'<span style="color:var(--sub)">0</span>'}</span>
        <span class="bl-track"><i class="bl-fill" style="width:${w}%;background:${col}"></i></span>
      </div>`;
    }).join("");

    const dwellItems=Object.keys(lp.dwell_avg||{}).map(k=>({
      l:ZH[k]||k, v:lp.dwell_avg[k], txt:lp.dwell_avg[k]+" 天",
      sev:lp.dwell_avg[k]>lp.stuck_days?"bad":(lp.dwell_avg[k]>3?"warn":"ok")}))
      .sort((a,b)=>b.v-a.v);

    const typeItems=Object.keys(lp.types).map(k=>({l:TYPE_ZH[k]||k, v:lp.types[k]}))
      .sort((a,b)=>b.v-a.v);

    const caseRows=lp.cases.map(c=>{
      const left=(c.days_left==null)
        ? '<span class="rs-sub">未设交期</span>'
        : (c.days_left<0?`<b class="neg">逾期 ${-c.days_left} 天</b>`:`剩 ${c.days_left} 天`);
      return `<tr>
        <td><b>${esc(c.customer)}</b><div class="rs-sub">${esc(c.root_id)}</div></td>
        <td style="max-width:200px">${esc(c.product)}</td>
        <td><span class="pill ${c.stuck?"tag-amber":(c.terminal?"tag-green":"")}">${esc(c.state_zh)}</span></td>
        <td>${esc(c.owner)}</td>
        <td class="${c.stuck?"neg":""}">${c.dwell!=null?c.dwell+" 天":"—"}</td>
        <td>${left}</td>
        <td>${c.objects_n} / ${c.evidence_n} / ${c.approvals_n}</td>
        <td style="max-width:330px">${esc(c.next_action)}<div class="rs-sub">更新 ${esc(c.updated_at||"—")}</div></td>
      </tr>`;
    }).join("")||`<tr><td colspan="8" class="empty">控制平面无案件。运行 <code>python workflows/loop_trigger.py --yes</code> 从来单线索建案。</td></tr>`;

    const secLC=el(`<section id="sec-LC" class="sec">
      <div class="sec-title">__IC_PROJ__ 全链案件 — 微信来单 → 立项 → 采购 → 生产 → 出货 → 验收回款 → 售后（16 态状态机）</div>
      ${warnHtml}
      <div class="hscroll" style="padding-bottom:14px">${kpis}</div>
      <div class="viz-grid">
        ${vizCard("16 态漏斗 — 每态有多少单",
          `<div class="bl">${funRows}</div>`,
          {q:`按<b>案件根对象</b>计（不是按全部 ${lp.object_count} 个对象）`,
           note:`主链 14 态（01→14）取 <code>application/contracts.py</code> 的 <code>PROJECT_STATES</code>，
                  与业主口径逐字对应；旁路两态（取消/售后）不计入主链。条越长 = 该态积压越多。`})}
        ${vizCard("各态平均停留",
          dwellItems.length?barList(dwellItems):`<div class="viz-empty">无状态迁移记录</div>`,
          {q:`> ${lp.stuck_days} 天判为停滞`,
           note:"停留时长 = 今天 − 末次状态迁移时间（无迁移则回退到建档时间）。这一列是找瓶颈最直接的入口。"
                +(lp.stale?`<b style="color:var(--amber)"> ⚠️ 本页数据已停更 ${lp.stale_days} 天，下面的「停留」实际等于停更天数，不是真实作业停滞。</b>`:"")})}
        ${vizCard("控制平面资产构成",
          stackBar(typeItems),
          {q:`${lp.object_count} 对象 / ${lp.evidence_count} 证据 / ${lp.approval_count} 审批`,
           note:`对象类型取自 <code>domain/order_to_cash.py</code> 的 <code>OBJECT_SPECS</code>（12 类，
                  每类有独立 ID 前缀）。证据链与审批记录是「每步有证据」的兑现载体。`})}
      </div>
      <div class="card" style="margin-top:16px"><h3>案件明细（按「逾期 &gt; 停滞 &gt; 停留时长」排序）</h3>
        <div style="overflow-x:auto"><table data-paginate="12" data-filter="1"><thead><tr>
          <th>客户 / 案件号</th><th>产品意向</th><th>当前状态</th><th>责任人</th><th>停留</th>
          <th>交期</th><th>对象/证据/审批</th><th>下一步（系统建议）</th>
        </tr></thead><tbody>${caseRows}</tbody></table></div>
        <div class="note">数据源 <code>data/business_loop/objects.csv</code>（本地控制平面，<b>不在 SeaTable</b>）·
          最后写入 <b>${lp.updated_at}</b>。状态推进命令：<code>python workflows/business_loop.py advance --root-id … --to … --reason … --yes</code>；
          本案展示与推进均为只读/需人工确认，驾驶舱不直接写控制平面。</div></div>
    </section>`);
    put("LC", secLC);
  }

  /* 新建向导：销售→「项目」表（销售立项表单）；其余角色→「生产计划」表（生产经理建计划）。
     老板/仓库/采购页不显示（已在 ROLES 裁剪：WZ 不在其 sections 中） */
  const _isSales = (role==="sales");
  const _wzTitle = _isSales ? "新建项目（向导）" : "新建生产项目（向导）";
  const _wzNameLbl = _isSales ? "项目名称 *" : "产品名称 *";
  const _wzNamePh = _isSales ? "如 客户A-4G小卡二代" : "如 4G小卡二代";
  const _wzTargetName = _isSales ? "项目" : "生产计划";
  const secWZ=el(`<section id="sec-WZ" class="sec"><div class="sec-title">__IC_ADD__ ${_wzTitle}</div>
    <div class="card">
      <p class="note">填完点「🚀 一键发起 → WorkBuddy 预填待确认」：会把信息整理成文字、<b>复制到剪贴板</b>，并<b>拉起本地 WorkBuddy、自动预填任务并开始执行</b>。注意：按技能铁律「任何增删改必须先向你确认」，专家会先列出完整数据<b>等你点头</b>，确认后才写库、算缺料、刷新驾驶舱——所以<b>不是全自动落库</b>，但仍省去手动复制粘贴。没装桌面端时点「🌐 网页版提交」手动发送。最终写入 SeaTable「${_wzTargetName}」表${_isSales?"（即销售立项表单）":""}。</p>
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px;max-width:680px">
        <label>${_wzNameLbl}<input id="wz-product" type="text" placeholder="${_wzNamePh}" style="width:100%"></label>
        <label>数量 *<input id="wz-qty" type="number" min="1" placeholder="100" style="width:100%"></label>
        <label>交期天数（天）<input id="wz-days" type="number" min="1" placeholder="30" style="width:100%"></label>
        <label>优先级<select id="wz-prio" style="width:100%"><option>高</option><option selected>中</option><option>低</option></select></label>
        <label>负责人<input id="wz-owner" type="text" placeholder="选填" style="width:100%"></label>
        <label>备注<input id="wz-note" type="text" placeholder="选填" style="width:100%"></label>
      </div>
      <div style="margin-top:14px;display:flex;gap:10px;align-items:center;flex-wrap:wrap">
        <button class="btn-primary" onclick="submitWizardCopy()">__IC_ADD__ 一键发起 → WorkBuddy 预填待确认</button>
        <button class="btn-ghost" onclick="window.open('https://www.workbuddy.cn/','_blank')">🌐 网页版提交（workbuddy.cn）</button>
        <button class="btn-ghost" onclick="submitWizardDownload()">__IC_DOWNLOAD__ 下载 JSON（备用）</button>
      </div>
      <textarea id="wz-text" readonly style="display:none;width:100%;max-width:680px;height:130px;margin-top:12px;font-size:13px;padding:8px;border:1px solid #d1d5db;border-radius:8px;font-family:inherit"></textarea>
      <p class="note" style="margin-top:10px">💡 「一键发起」需要 WorkBuddy 桌面端已安装，且<strong>在本机系统浏览器（Chrome / Edge）中打开本页</strong>再点按钮——浏览器会弹「是否用 WorkBuddy 打开」并拉起客户端、预填任务，专家会<b>请你确认后再写库</b>。注意：在 WorkBuddy 自带的预览面板里点可能不会触发系统协议，请改用外部浏览器打开 HTML 文件。没装桌面端则在系统浏览器打开「🌐 网页版提交（workbuddy.cn）」粘贴发送即可（网页版需先登录）。网页不持有任何账号/口令，数据由本机 Python 落库。</p>
    </div></section>`);
  put("WZ", secWZ);

  /* 次要指标 → 挂进「质量 › 更多指标」标签页（旧版塞在「更多分析」折叠区最底部） */
  if(kpiG.more.length && tabVisible(ok,"KM")){
    const secKM=el(`<section id="sec-KM" class="sec"><div class="sec-title">__IC_GRID__ 更多指标</div>
      <div class="hscroll" id="kpigm"></div></section>`);
    kpiG.more.forEach(x=>secKM.querySelector("#kpigm").appendChild(kpiCard(x)));
    put("KM", secKM);
  }

  /* ── 数据来源健康（全链降级 / Plan B 专项，2026-10-02）─────────────
     统一陈旧判据（Python 侧 SOURCE_SPEC）+ 最近一次运行的降级状态，
     一张表说清「每个数据源有多旧 / 本次是否降级 / 挂了会造成什么后果」。
     刻意如实标注是哪一个 run —— 驾驶舱运行时本次 final.json 还没落盘。 */
  const sh=m.source_health;
  if(sh && tabVisible(ok,"SH")){
    const LV_ZH={ok:"正常",warn:"偏旧",bad:"过旧"};
    const LV_COL={ok:"var(--green)",warn:"var(--amber)",bad:"var(--red)"};
    const srcs=sh.sources||[];
    const bad=srcs.filter(x=>x.level==="bad");
    const warn=srcs.filter(x=>x.level==="warn");
    const deg=srcs.filter(x=>x.degraded);
    const warns=[];
    if(bad.length){
      warns.push("<b>"+bad.length+" 个数据源已过旧</b>："+
        bad.map(x=>esc(x.name)).join("、")+" —— 看板上依赖它们的小结可能基于旧数据。");
    }
    if(deg.length){
      warns.push("<b>"+deg.length+" 个数据源本次为降级（沿用旧数据）</b>："+
        deg.map(x=>esc(x.name)+"（"+esc(x.step_id||"")+" 失败但未阻断）").join("、")+"。");
    }
    const warnHtml=warns.length?`<div class="card" style="border-left:3px solid var(--amber);margin-bottom:14px">
      <h3 style="color:var(--amber)">⚠️ 数据可信度提示</h3>
      <div class="note" style="margin-top:6px">${warns.map(w=>"· "+w).join("<br>")}</div></div>`:"";
    const kpis=[
      statCard({l:"数据源", v:srcs.length, u:"个", sev:"ok",
        sub:"统一口径 · 见 SOURCE_SPEC"}),
      statCard({l:"偏旧", v:warn.length, u:"个", sev:warn.length?"warn":"ok",
        nsev:warn.length?"warn":"",
        sub:"超过 ok 阈值但未超 bad"}),
      statCard({l:"过旧", v:bad.length, u:"个", sev:bad.length?"bad":"ok",
        nsev:bad.length?"bad":"",
        sub:"超过 bad 阈值或产物缺失"}),
      statCard({l:"本次降级", v:deg.length, u:"个", sev:deg.length?"warn":"ok",
        nsev:deg.length?"warn":"",
        sub:"步骤失败但声明为非阻断"}),
    ].join("");
    const rows=srcs.map(x=>{
      const col=LV_COL[x.level]||"var(--sub)";
      const age=(x.age_h==null)?"—":fmtAge(x.age_h);
      const st=!x.step_id?"—":(x.degraded?"降级":(x.level==="bad"?"异常":"正常"));
      const stCol=!x.step_id?"var(--sub)":(x.degraded?"var(--amber)":(x.level==="bad"?"var(--red)":"var(--green)"));
      return `<tr>
        <td><b>${esc(x.name)}</b></td>
        <td><code style="font-size:11px">${esc(x.artifact)}</code></td>
        <td>${esc(x.at||"—")}</td>
        <td>${esc(age)}</td>
        <td><span class="dot" style="background:${col};display:inline-block;margin-right:5px"></span>${LV_ZH[x.level]||esc(x.level)}</td>
        <td><span class="dot" style="background:${stCol};display:inline-block;margin-right:5px"></span>${esc(st)}</td>
        <td class="note" style="margin:0">${esc(x.note||"")}</td>
        <td class="note" style="margin:0">${esc(x.consequence||"")}</td>
      </tr>`;
    }).join("");
    const runLine = sh.run
      ? `最近一次已完成运行 <code>${esc(sh.run.run_id)}</code>（${esc(sh.run.workflow)} · 整次状态 ${esc(sh.run.status)} · ${esc(sh.run.finished_at||"—")}）`
      : "（未找到任何运行账本）";
    const secSH=el(`<section id="sec-SH" class="sec">
      <div class="sec-title">__IC_INSIGHT__ 数据来源健康 — 每个数据源有多旧、本次是否降级、挂了会怎样</div>
      ${warnHtml}
      <div class="hscroll" style="padding-bottom:14px">${kpis}</div>
      <div class="card" style="margin-top:16px"><h3>来源明细（按各自 ok / bad 阈值判级）</h3>
        <div style="overflow-x:auto"><table data-paginate="12" data-filter="1"><thead><tr>
          <th>数据源</th><th>产物文件</th><th>数据时间</th><th>距今</th>
          <th>新鲜度</th><th>最近一次运行</th><th>说明</th><th>不可用后果</th>
        </tr></thead><tbody>${rows}</tbody></table></div>
        <div class="note"><b>本面板反映的是：</b>${runLine}。<br>
          驾驶舱在 <code>cockpit</code> 步骤内生成，此时本次运行的账本尚未落盘，故这里显示的是
          <b>最近一次已完成运行</b>的步骤状态，<b>不是本次</b>。<br>
          <b>判据来源</b>：<code>cockpit.py</code> 的 <code>SOURCE_SPEC</code>（2026-10-02 起统一，
          替代原先 4 套各自为政的陈旧口径）；有内嵌时间戳的产物读其字段，无时间戳的 CSV 退回文件 mtime。<br>
          <b>阈值</b>：${srcs.map(x=>esc(x.name)+" "+x.ok_h+"h / "+x.bad_h+"h").join(" · ")}</div></div>
    </section>`);
    put("SH", secSH);
  }

  /* 激活当前标签页 —— 必须等所有区块挂载完，否则面板还是空的 */
  activatePane(shell.tab);
  if(kpiBar && !kpiG.core.length) kpiBar.style.display="none";
  polishTables();
  initTableTools();
  initPagination();
}
/* 表格微整形：表头一律不折行；纯日期/短标识单元格不折行。 */
function polishTables(){
  document.querySelectorAll("#app .pane table").forEach(t=>{
    t.querySelectorAll("th").forEach(th=>th.classList.add("nowrap"));
    t.querySelectorAll("tbody td").forEach(td=>{
      if(td.children.length) return;                     // 含按钮/药丸/输入的单元格跳过
      const s=(td.textContent||"").trim();
      if(!s) return;
      if(/^\d{4}-\d{2}-\d{2}$/.test(s) || (s.length<=12 && !/[，。；：、]/.test(s))){
        td.classList.add("nowrap");
      }
    });
  });
}
/* 切换可见面板 + 同步导航/标签的选中态 */
function activatePane(tab){
  document.querySelectorAll("#app .pane").forEach(p=>p.classList.remove("active"));
  const p=tab?document.getElementById("pane-"+tab):null;
  if(p) p.classList.add("active");
  const mod=tab?(MOD_OF[tab]||null):null;
  document.querySelectorAll("#sideNav .navitem,#tabBar button").forEach(b=>{
    b.classList.toggle("active", !!mod && b.dataset.mod===mod);
  });
  document.querySelectorAll("#tabStrip .tab").forEach(b=>{
    b.classList.toggle("active", !!tab && b.dataset.tab===tab);
  });
  if(_navIO){ _navIO.disconnect(); _navIO=null; }
}
/* 「更多分析」折叠区开关（render 内多处调用） */
function openMore(){
  const b=document.getElementById("moreBody"), t=document.getElementById("moreTg");
  if(!b) return;
  b.classList.add("open"); if(t){ t.classList.add("open"); t.setAttribute("aria-expanded","true"); }
}
function closeMore(){
  const b=document.getElementById("moreBody"), t=document.getElementById("moreTg");
  if(!b) return;
  b.classList.remove("open"); if(t){ t.classList.remove("open"); t.setAttribute("aria-expanded","false"); }
}

/* ---------- 交互 ---------- */
function toast(msg){
  let t=$("#toast");
  if(!t){ t=el(`<div class="toast" id="toast"></div>`); document.body.appendChild(t); }
  t.textContent=msg; t.classList.add("show");
  clearTimeout(t._timer);
  t._timer=setTimeout(()=>t.classList.remove("show"), 3200);
}
/* 新建项目向导：采集 → 复制成纯文本 → 唤起 WorkBuddy（用户粘贴发送，AI 落库） */
function currentTarget(){
  // 销售立项 → 写「项目」表（对应销售立项表单）；其余角色 → 写「生产计划」表
  return (currentRole()==="sales") ? "项目" : "生产计划";
}
function collectWizard(){
  const 产品=document.getElementById('wz-product').value.trim();
  const 数量=parseInt(document.getElementById('wz-qty').value)||0;
  const 天数=parseInt(document.getElementById('wz-days').value)||0;
  const 优先级=document.getElementById('wz-prio').value;
  const 负责人=document.getElementById('wz-owner').value.trim();
  const 备注=document.getElementById('wz-note').value.trim();
  if(!产品||!数量){ toast('请填写项目名称/产品名称和数量'); return null; }
  const 交期=new Date(Date.now()+天数*86400000).toISOString().slice(0,10);
  return {产品,数量,天数,交期,优先级,负责人,备注};
}
function wizardToText(d){
  const t=currentTarget();
  const head=(t==="项目")?"【新建项目】":"【新建生产计划】";
  const lines=[head,"产品："+d.产品,"数量："+d.数量,"交期天数："+d.天数,"预计完工："+d.交期,"优先级："+d.优先级];
  if(d.负责人) lines.push("负责人："+d.负责人);
  if(d.备注) lines.push("备注："+d.备注);
  lines.push("（由生产驾驶舱「新建项目」向导生成。请在已加载 seatable-production 技能的项目中运行 op.py apply-text 处理此指令：写入「"+t+"」表并刷新驾驶舱。）");
  return lines.join("\n");
}
function copyText(txt){
  if(navigator.clipboard && window.isSecureContext){
    return navigator.clipboard.writeText(txt).then(()=>true).catch(()=>null);
  }
  try{
    const ta=document.createElement('textarea');
    ta.value=txt; ta.style.position='fixed'; ta.style.top='-1000px'; ta.style.opacity='0';
    document.body.appendChild(ta); ta.focus(); ta.select();
    const ok=document.execCommand('copy'); document.body.removeChild(ta);
    return Promise.resolve(ok?true:null);
  }catch(e){ return Promise.resolve(null); }
}
function invokeWorkBuddyAuto(txt){
  // 云盘式一键发起：workbuddy://task?action=start 自动开始执行任务、skills= 自动加载 seatable-production 技能
  // 但技能铁律要求写入前必须经用户确认，故专家会先列出数据等待确认，不会直接落库
  // 用真实 <a> 在用户点击手势里触发顶层导航（隐藏 iframe 跳自定义协议会被 Chrome/Edge 静默拦截，导致毫无反应）
  const url='workbuddy://task?action=start&prompt='+encodeURIComponent(txt)+'&skills=seatable-production';
  try{
    const a=document.createElement('a');
    a.href=url; a.rel='noopener'; a.style.display='none';
    document.body.appendChild(a); a.click();
    setTimeout(()=>{ try{document.body.removeChild(a);}catch(e){} }, 2000);
  }catch(e){ /* 忽略：交给 toast / 网页版按钮兜底 */ }
}
/* 微信情报台：勾选确认/忽略 → 拉起专家执行 wechat_intake.py approve/ignore 写 SeaTable */
function wxCollectSelected(){
  return [...document.querySelectorAll('#sec-WXC input.wx-sel:checked')].map(c=>c.value).filter(Boolean);
}
function wxApproveSelected(){
  const ids=wxCollectSelected();
  if(!ids.length){ toast('请先勾选要确认的事件'); return; }
  const evs=ids.map(id=>(window.__WX_EVENTS||{})[id]).filter(Boolean);
  if(!evs.length){ toast('未找到事件数据，请刷新页面'); return; }
  /* 在线模式：直连本机 API，逐条 approve（服务端跑既有 CLI，留痕逻辑不变），
     完成后自动刷新快照 —— 零复制粘贴。 */
  if(LIVE){
    toast('正在确认 '+ids.length+' 条并写入 SeaTable …');
    liveAPI("/api/wx/approve", {ids}, res=>{
      if(res.ok){
        const okN=(res.results||[]).filter(x=>x.exit===0).length;
        toast('已确认 '+okN+'/'+ids.length+' 条 ✅ 正在刷新数据…');
        liveAPI("/api/refresh", {}, r2=>{
          if(r2.ok){ setTimeout(()=>location.reload(), 600); }
          else toast('写入成功，刷新失败：'+(r2.error||r2.exit));
        });
      }else{
        toast('确认失败：'+(res.error||'详见服务端日志'));
      }
    });
    return;
  }
  let blk='请核对以下微信事件，确认无误后写入 SeaTable 业务表（写入前请再向我确认一遍，可指定只写哪几条）。事件详情已附原文与出处，无需另行翻查：\n\n';
  evs.forEach((e,i)=>{
    const no=e["事件编号"]||"";
    const src=[e["来源群"],e["发送人"],e["日期"],e["时间"]].filter(Boolean).join("  ·  ");
    const cat=e["分类"]||"其他";
    const txt=(e["原文"]||"").trim();
    let intent=e["意图"]||"";
    try{ const j=JSON.parse(intent||"{}"); if(j&&typeof j==="object") intent=JSON.stringify(j,null,0); }catch(_){}
    blk+=`【${i+1}】${no}  [${cat}]\n`;
    blk+=`· 出处：${src}\n`;
    blk+=`· 原文：${txt}\n`;
    if(intent && intent!=="{}" && intent!=="[]") blk+=`· 意图：${intent}\n`;
    blk+=`\n`;
  });
  blk+='核对要点：① 原文与出处是否准确；② 意图 op=update/append/log 与目标表/行(row_id)是否正确。确认后运行 `wechat_intake.py approve <编号>` 逐条写入（同一编号也可只写指定几条）。';
  invokeWorkBuddyAuto(blk);
}
function wxIgnoreSelected(){
  const ids=wxCollectSelected();
  if(!ids.length){ toast('请先勾选要忽略的事件'); return; }
  const evs=ids.map(id=>(window.__WX_EVENTS||{})[id]).filter(Boolean);
  if(!evs.length){ toast('未找到事件数据，请刷新页面'); return; }
  if(LIVE){
    toast('正在忽略 '+ids.length+' 条 …');
    liveAPI("/api/wx/ignore", {ids}, res=>{
      if(res.ok){
        toast('已忽略 '+ids.length+' 条 ✅ 正在刷新数据…');
        liveAPI("/api/refresh", {}, r2=>{
          if(r2.ok){ setTimeout(()=>location.reload(), 600); }
          else toast('已忽略，刷新失败：'+(r2.error||r2.exit));
        });
      }else{
        toast('忽略失败：'+(res.error||'详见服务端日志'));
      }
    });
    return;
  }
  let blk='请核对以下微信事件，确认后标记为「忽略」（不写业务表，仅留痕）。事件详情已附原文与出处：\n\n';
  evs.forEach((e,i)=>{
    const no=e["事件编号"]||"";
    const src=[e["来源群"],e["发送人"],e["日期"],e["时间"]].filter(Boolean).join("  ·  ");
    const cat=e["分类"]||"其他";
    const txt=(e["原文"]||"").trim();
    blk+=`【${i+1}】${no}  [${cat}]\n· 出处：${src}\n· 原文：${txt}\n\n`;
  });
  blk+='确认忽略请运行 `wechat_intake.py ignore <编号>`。';
  invokeWorkBuddyAuto(blk);
}
function submitWizardCopy(){
  const d=collectWizard(); if(!d) return;
  const txt=wizardToText(d);
  const box=document.getElementById('wz-text'); box.value=txt; box.style.display='block';
  copyText(txt).then(ok=>{
    // 启发式检测：系统接管协议拉起本地客户端时，当前窗口通常会失焦(blur)。
    // 若 ~1.8s 后仍聚焦，说明协议未注册/未被接管，才提示兜底（手动步骤只在真失败时出现）。
    let launched=false;
    const onBlur=()=>{ launched=true; window.removeEventListener('blur', onBlur); };
    window.addEventListener('blur', onBlur);
    invokeWorkBuddyAuto(txt);
    setTimeout(()=>{
      window.removeEventListener('blur', onBlur);
      if(launched){
        toast('已拉起 WorkBuddy 并预填任务 ✅ 专家会列出数据，请你确认后再写库');
      }else{
        toast('未拉起客户端？请在系统浏览器(Chrome/Edge)中打开本页再点，或点「网页版提交」');
      }
    }, 1800);
  });
}
function submitWizardDownload(){
  const d=collectWizard(); if(!d) return;
  const t=currentTarget();
  const obj={_wizard:"new-project",_target:t,产品:d.产品,数量:d.数量,交期天数:d.天数,预计完工:d.交期,优先级:d.优先级,负责人:d.负责人,备注:d.备注,生成时间:new Date().toISOString()};
  const blob=new Blob([JSON.stringify(obj,null,2)],{type:"application/json"});
  const a=document.createElement('a'); a.href=URL.createObjectURL(blob); a.download=(t==="项目"?"新建项目.json":"新建生产项目.json"); a.click();
  URL.revokeObjectURL(a.href);
  toast('已下载 '+(t==="项目"?"新建项目.json":"新建生产项目.json")+'（备用）');
}
/* 分享角色视图：复制带 #role 锚点 + 口令的微信文案，直接发微信给对应人 */
function shareRole(role){
  const base=location.origin + location.pathname + (location.search||"");
  const url=base + "#role=" + role;
  const name=ROLES[role].name;
  const pw=PW[role];
  const txt="【生产·项目管理驾驶舱 · "+name+"视图】\n链接："+url+"\n打开后输入口令："+pw+"\n数据每日 9:00 自动更新，无需手动刷新。\n（内部查看，请勿外传）";
  const done=()=>toast("已复制「"+name+"视图」链接 ✅ 去微信粘给"+name+"即可");
  if(navigator.clipboard && navigator.clipboard.writeText){ navigator.clipboard.writeText(txt).then(done, ()=>fallbackCopy(txt,done)); }
  else fallbackCopy(txt, done);
}
/* 口令管理：仅管理员可见，查看 / 复制 / 轮换 */
function openPwModal(){
  populatePw();
  $("#pwNote").innerHTML="";
  $("#pwModal").classList.add("show");
}
function closePwModal(){ $("#pwModal").classList.remove("show"); }
function pwName(k){ return k==="admin" ? "管理员（全部角色）" : (ROLES[k]?ROLES[k].name:k); }
function populatePw(){
  const rows=Object.keys(PW).map(k=>
    `<div class="pw-row"><span class="pw-name">${pwName(k)}</span>
      <input class="pw-val" type="text" readonly value="${PW[k]}">
      <button class="pw-cp" data-pw="${PW[k]}">复制</button></div>`).join("");
  $("#pwList").innerHTML=rows;
  $("#pwList").querySelectorAll(".pw-cp").forEach(b=>b.onclick=()=>{
    const t=b.dataset.pw;
    const done=()=>toast("已复制口令 ✅");
    if(navigator.clipboard&&navigator.clipboard.writeText) navigator.clipboard.writeText(t).then(done,()=>fallbackCopy(t,done));
    else fallbackCopy(t,done);
  });
}
function genPw(prefix){ let s=prefix; for(let i=0;i<4;i++) s+=Math.floor(Math.random()*10); return s; }
function rotateAll(){
  const map={admin:genPw("ZHWL"), boss:genPw("ZHWL"), warehouse:genPw("CK"), purchase:genPw("CG"), production:genPw("SC"), sales:genPw("XS")};
  PW=map;
  localStorage.setItem("cockpit_pw_override", _b64enc(map));   // 本机立即生效
  populatePw();
  const lines=Object.keys(map).map(k=> pwName(k)+"："+map[k]).join("\n");
  $("#pwNote").innerHTML="✅ 已生成本机新口令并立即生效。<br>要让<b>所有分享链接（其他设备）也换成新口令、并作废旧口令</b>，请把下面新口令发我，或直接说「重新部署」——我会用这些新口令重建上线：<br><textarea class='pw-out' readonly>"+lines+"</textarea>";
  toast("已轮换本机口令 ✅");
}
function copyAllPw(){
  const lines=Object.keys(PW).map(k=> pwName(k)+"："+PW[k]).join("\n");
  const done=()=>toast("已复制全部口令 ✅");
  if(navigator.clipboard&&navigator.clipboard.writeText) navigator.clipboard.writeText(lines).then(done,()=>fallbackCopy(lines,done));
  else fallbackCopy(lines,done);
}
let _navIO=null;
/* ---------- 通用表格工具：搜索 + 状态筛选 + 勾选 + 批量导出 ----------
   约定：table 标 data-filter="1" 即启用搜索+状态筛选（状态列自动识别）；
   再加 data-select="1" 启用行勾选 + 批量操作栏（全选/导出选中 CSV/复制名称）。
   跳过占位 .empty 行。必须在 initPagination 之前调用（分页基于可见行重算）。*/
function initTableTools(){
  document.querySelectorAll("table[data-filter]").forEach(tbl=>{
    if(tbl._tfInit) return; tbl._tfInit=true;
    const tb=tbl.querySelector("tbody"); if(!tb) return;
    const container=tbl.parentElement;      /* 可能是 overflow-x:auto 滚动壳，也可能直接是卡片 */
    const scrolled=container&&container.tagName==="DIV"&&container.getAttribute("style")&&/overflow-x:\s*auto/.test(container.getAttribute("style"));
    const host=scrolled?(container.parentElement||container):container;  /* 工具条放滚动壳外，避免横向滚动带走搜索框 */
    const anchor=scrolled?container:tbl;
    const tools=el(`<div class="tf-tools">
      <input class="tf-input" type="text" placeholder="🔍 搜索…">
      <select class="tf-sel"></select>
      <span class="tf-cnt"></span></div>`);
    host.insertBefore(tools, anchor);
    const inp=tools.querySelector(".tf-input"), sel=tools.querySelector(".tf-sel"), cntEl=tools.querySelector(".tf-cnt");
    const allRows=Array.from(tb.querySelectorAll("tr")).filter(r=>!r.classList.contains("empty"));
    /* 状态列：优先找「状态」表头，其次含 pill 的列 */
    const ths=Array.from(tbl.querySelectorAll("thead th"));
    let stIdx=-1;
    ths.forEach((th,i)=>{ if(stIdx<0 && /状态/.test(th.textContent)) stIdx=i; });
    if(stIdx<0) ths.forEach((th,i)=>{ if(stIdx<0 && tbl.querySelectorAll(`tbody tr td:nth-child(${i+1}) .pill`).length>=3) stIdx=i; });
    /* 状态选项自动聚合 */
    const stMap=new Map();
    allRows.forEach(r=>{
      let v="";
      if(stIdx>=0){ const td=r.children[stIdx]; if(td){ const p=td.querySelector(".pill"); v=(p?p.textContent:td.textContent).trim(); } }
      stMap.set(v,(stMap.get(v)||0)+1);
    });
    const hasStatus=stMap.size>1||(stMap.size===1&&![...stMap.keys()][0]);
    sel.style.display=hasStatus?"":"none";
    if(hasStatus){
      sel.innerHTML=`<option value="">全部状态</option>`+
        [...stMap.entries()].sort((a,b)=>a[0]<b[0]?-1:1)
          .map(([v,n])=>`<option value="${esc(v)}">${esc(v||"无状态")}（${n}）</option>`).join("");
    }
    function visible(){ return allRows.filter(r=>r.style.display!=="none"&&!r.classList.contains("tf-hide")); }
    function apply(){
      const q=inp.value.trim().toLowerCase(), sv=hasStatus?sel.value:"";
      let shown=0;
      allRows.forEach(r=>{
        const okQ=!q||r.textContent.toLowerCase().includes(q);
        let okS=true;
        if(sv&&stIdx>=0){ const td=r.children[stIdx]; const p=td&&td.querySelector(".pill");
          okS=((p?p.textContent:(td?td.textContent:"")).trim()===sv); }
        const show=okQ&&okS;
        r.classList.toggle("tf-hide",!show);
        if(show){shown++;} else {r.style.display="none";}
      });
      if(shown) visible().forEach(r=>{r.style.display="";});
      cntEl.textContent=shown===allRows.length?("共 "+allRows.length+" 条"):("显示 "+shown+" / "+allRows.length+" 条");
      if(tbl._pgRedraw) tbl._pgRedraw();
      updateBar();
    }
    inp.oninput=apply; sel.onchange=apply;
    /* 勾选模式 */
    const selectable=tbl.hasAttribute("data-select");
    let bar=null, headChk=null;
    let updateBar=function(){};
    if(selectable){
      const head=tbl.querySelector("thead tr");
      headChk=el(`<th style="width:34px;text-align:center"><input type="checkbox" class="tf-row-sel tf-all"></th>`);
      head.insertBefore(headChk, head.firstChild);
      allRows.forEach(r=>{
        const td=document.createElement("td");
        td.style.textAlign="center"; td.style.width="34px";
        const c=el(`<input type="checkbox" class="tf-row-sel">`);
        td.appendChild(c); r.insertBefore(td, r.firstChild);
      });
      bar=el(`<div class="tf-bar"><span>已勾选 <b class="n">0</b> 条</span>
        <button class="btn-ghost tf-csv">⬇ 导出选中 CSV</button>
        <button class="btn-ghost tf-names">📋 复制名称列</button>
        <button class="btn tf-clear">取消选择</button></div>`);
      host.appendChild(bar);      const barN=bar.querySelector(".n");
      updateBar=function(){
        const n=tb.querySelectorAll("tr:not(.empty) .tf-row-sel:checked").length;
        barN.textContent=n; bar.classList.toggle("show",n>0);
        if(headChk){ const hc=headChk.querySelector("input");
          const vis=visible(); hc.checked=vis.length>0&&vis.every(r=>{const c=r.querySelector(".tf-row-sel");return c&&c.checked;}); }
      };
      tbl._tfUpdateBar=updateBar;
      tb.addEventListener("change",ev=>{
        if(ev.target.classList&&ev.target.classList.contains("tf-row-sel")) updateBar();
      });
      headChk.querySelector("input").onchange=function(){
        const on=this.checked;
        visible().forEach(r=>{ const c=r.querySelector(".tf-row-sel"); if(c) c.checked=on; });
        updateBar();
      };
      bar.querySelector(".tf-clear").onclick=()=>{
        tb.querySelectorAll(".tf-row-sel:checked").forEach(c=>c.checked=false); updateBar();
      };
      bar.querySelector(".tf-names").onclick=()=>{
        const names=[];
        tb.querySelectorAll("tr").forEach(r=>{
          if(r.classList.contains("empty")||r.style.display==="none") return;
          const c=r.querySelector(".tf-row-sel"); if(c&&c.checked){
            /* 跳过勾选列，取第一个文本单元格 */
            const td=Array.from(r.children).find((x,i)=>i>0&&x.textContent.trim());
            if(td) names.push(td.textContent.trim().split("\n")[0]);
          }
        });
        const txt=names.join("\n");
        const done=()=>toast("已复制 "+names.length+" 个名称 ✅");
        if(navigator.clipboard&&navigator.clipboard.writeText) navigator.clipboard.writeText(txt).then(done,()=>fallbackCopy(txt,done));
        else fallbackCopy(txt,done);
      };
      bar.querySelector(".tf-csv").onclick=()=>{
        const ths2=Array.from(tbl.querySelectorAll("thead th")).map(th=>th.textContent.trim());
        const lines=[ths2.slice(1).join(",")]; /* 去掉勾选列 */
        tb.querySelectorAll("tr").forEach(r=>{
          if(r.classList.contains("empty")||r.style.display==="none") return;
          const c=r.querySelector(".tf-row-sel"); if(!(c&&c.checked)) return;
          const cells=Array.from(r.children).slice(1).map(td=>{
            let t=td.textContent.trim().replace(/\s*\n\s*/g," ").replace(/"/g,'""');
            return /[",\n]/.test(t)?('"'+t+'"'):t;
          });
          lines.push(cells.join(","));
        });
        const blob=new Blob(["\ufeff"+lines.join("\n")],{type:"text/csv;charset=utf-8"});
        const a=document.createElement("a");
        a.href=URL.createObjectURL(blob);
        a.download="驾驶舱导出_"+new Date().toISOString().slice(0,10)+".csv";
        a.click(); URL.revokeObjectURL(a.href);
        toast("已导出 "+(lines.length-1)+" 行 CSV ✅");
      };
    }
    apply();
  });
}
function initPagination(){
  document.querySelectorAll('table[data-paginate]').forEach(tbl=>{
    if(tbl._pgInit) return; tbl._pgInit=true;
    const size=parseInt(tbl.dataset.paginate)||8;
    const tb=tbl.querySelector('tbody'); if(!tb) return;
    const allRows=Array.from(tb.querySelectorAll('tr')).filter(r=>!r.classList.contains('empty')&&!r.classList.contains('tf-hide'));
    const rows=()=>allRows.filter(r=>!r.classList.contains('tf-hide'));
    if(allRows.length<=size){ tbl._pgRedraw=null; return; }
    let cur=1;
    const nav=el(`<div class="pg"><button class="pg-prev">‹ 上一页</button><span class="pg-info"></span><button class="pg-next">下一页 ›</button></div>`);
    tbl.parentElement.after(nav);
    function draw(){
      const rs=rows();
      const pages=Math.max(1,Math.ceil(rs.length/size));
      if(cur>pages) cur=pages;
      const start=(cur-1)*size;
      rs.forEach((r,i)=>{ r.style.display=(i>=start&&i<start+size)?'':'none'; });
      /* 不在结果集里的行（被筛选隐藏）确保藏起来 */
      allRows.forEach(r=>{ if(!rs.includes(r)) r.style.display='none'; });
      nav.querySelector('.pg-info').textContent='第 '+cur+' / '+pages+' 页 · 共 '+rs.length+' 条';
      nav.querySelector('.pg-prev').disabled=cur<=1;
      nav.querySelector('.pg-next').disabled=cur>=pages;
    }
    nav.querySelector('.pg-prev').onclick=()=>{ if(cur>1){cur--;draw();} };
    nav.querySelector('.pg-next').onclick=()=>{ if(cur<pages()){cur++;draw();} };
    tbl._pgRedraw=draw;
    draw();
  });
}
function observeNav(nav){
  const items=Array.from(nav.querySelectorAll('.sec-nav-item'));
  const map={}; items.forEach(b=>map[b.dataset.target]=b);
  if(_navIO) _navIO.disconnect();
  _navIO=new IntersectionObserver(es=>{
    es.forEach(e=>{ if(e.isIntersecting){ items.forEach(i=>i.classList.remove('active')); const it=map[e.target.id]; if(it) it.classList.add('active'); } });
  },{rootMargin:'-45% 0px -50% 0px',threshold:0});
  document.querySelectorAll('section.sec').forEach(s=>_navIO.observe(s));
}
function exportJSON(){
  const blob=new Blob([JSON.stringify(MODEL,null,2)],{type:"application/json"});
  const a=document.createElement("a");
  a.href=URL.createObjectURL(blob);
  a.download="驾驶舱数据快照_"+MODEL.snapshot+".json";
  a.click(); URL.revokeObjectURL(a.href);
}
function importJSON(file){
  const r=new FileReader();
    r.onload=()=>{ try{ const d=JSON.parse(r.result); MODEL=d; render(MODEL);
      $("#banner").innerHTML="已导入本地快照（"+d.snapshot+"），点击「导出分析JSON」可保存。";
    }catch(e){ alert("JSON 解析失败："+e.message); } };
  r.readAsText(file);
}
/* 分析数据：自动拼好结构化分析请求 → 复制到剪贴板 → 用户粘贴到 WorkBuddy 发我 */
function analyzeData(){
  const m=MODEL, k=m.kpi, t=m.time, c=m.cost, q=m.quality, s=m.supply;
  const role=currentRole();
  const L=[];
  L.push("【请帮我分析这份生产·项目管理驾驶舱数据 — 当前角色视图："+ROLES[role].name+"】");
  L.push("数据快照："+m.snapshot + (m.isDemo ? "（演示数据）" : "（真实数据·SeaTable云"+(m.synced_at?"，同步 "+m.synced_at:"")+"）"));
  L.push("");
  L.push("【1 核心指标（"+ROLES[role].name+"视图）】");
  const _kp=buildKPIs(m, role);
  _kp.core.concat(_kp.more).forEach(x=>L.push("· "+x.l+"："+x.v+(x.s?"（"+x.s+"）":"")));
  L.push("");
  L.push("【2 下一步行动建议（"+ROLES[role].name+"专属）】");
  const actsAll=m.next_actions||[];
  const acts=(role==="boss")?actsAll:actsAll.filter(a=>(ROLES[role].actions||[]).includes(a.cat));
  if(acts.length) acts.forEach(a=>L.push("  ["+a.pri+"] "+a.text));
  else L.push("  （当前角色暂无专属待办）");
  L.push("");
  L.push("【3 关键明细】");
  L.push("· 采购逾期：" + (s.overdue_list.length ? s.overdue_list.map(o=>o.supplier+"·"+o.material+"（逾期"+o.days+"天）").join("；") : "无"));
  L.push("· 供应商准时率：" + (s.supplier.length ? s.supplier.map(x=>(x.name+": "+(x.rate==null?"—":x.rate+"%"))).join("，") : "无"));
  L.push("· 质量：贴片良品率 "+q.smt_yield+"%，组装良品率 "+(q.asm_yield==null?"未录入":q.asm_yield+"%")+"，维修率 "+q.repair_rate+"%（超期未完 "+q.repair_overdue+"）");
  L.push("· PartDB 缺料：" + (m.partdb && m.partdb.bom ? m.partdb.bom.shortage.length+" 种（零确认库存 "+m.partdb.zero_confirmed+" 种）" : "未连接"));
  L.push("· 产线瓶颈环节：" + (t.flow_dist ? (Object.entries(t.flow_dist).sort((a,b)=>b[1]-a[1])[0]||"无") : "无"));
  L.push("");
  L.push("【4 我在页面上已补录的字段（待确认后写回云端）】");
  const ov=loadOV(); let hasOv=false; const ovList=[];
  for(const tbl of ["生产计划","组装记录"]) for(const rid in (ov[tbl]||{})) for(const f in ov[tbl][rid]) if(ov[tbl][rid][f]!==""){ hasOv=true; ovList.push(tbl+" / "+rid+" / "+f+" = "+ov[tbl][rid][f]); }
  if(hasOv){ L.push(ovList.join("\n")); } else { L.push("（暂无，可忽略）"); }
  L.push("");
  L.push("请基于以上数据重点分析：①当前最大瓶颈与风险在哪；②未来 2 周最该优先推进的 3 件事；③成本 / 质量 / 交期三方面各有什么可优化点。给出具体、可执行的建议。");
  const txt=L.join("\n");
  const done=()=>alert("已复制「分析数据」请求到剪贴板 ✅\n\n你现在就在 WorkBuddy 里——直接 Ctrl+V 粘贴到下方对话框，点发送，我就会基于这份数据帮你做分析。");
  if(navigator.clipboard && navigator.clipboard.writeText){ navigator.clipboard.writeText(txt).then(done, ()=>fallbackCopy(txt,done)); }
  else fallbackCopy(txt, done);
}
function fmtAge(h){
  if(h==null) return "";
  if(h<1) return Math.max(1,Math.round(h*60))+" 分钟前";
  if(h<48) return Math.round(h)+" 小时前";
  return Math.round(h/24)+" 天前";
}
/* SeaTable 的 ok / bad 阈值改从 MODEL.source_health 取 —— Python 侧的 SOURCE_SPEC
   是唯一源。2026-10-02 之前这里硬编码 6h/24h，而 daily 9:00 与 evening 19:00 的
   间隔最长 14h，导致驾驶舱每天大部分时间常亮 amber「较新」、稀释了告警价值
   （业主决定放宽到 16h/24h）。 */
function satFreshThresholds(){
  const s=((MODEL.source_health||{}).sources||[]).filter(x=>x.key==="seatable")[0];
  return {ok:(s&&s.ok_h)||16, bad:(s&&s.bad_h)||24};
}
function syncFreshness(){
  const sat=MODEL.synced_at||"";
  if(!sat) return {h:null,lvl:"na",txt:"未知"};
  const d=new Date(sat.replace(/-/g,"/"));
  if(isNaN(d.getTime())) return {h:null,lvl:"na",txt:sat};
  const h=(Date.now()-d.getTime())/3600000;
  const th=satFreshThresholds();
  let lvl="green",txt="数据新鲜";
  if(h>=th.bad){lvl="red";txt="数据可能已过时";}
  else if(h>=th.ok){lvl="amber";txt="较新";}
  return {h:h,lvl:lvl,txt:txt};
}
function refreshSyncBadge(){
  const el=$("#syncBadge"); if(!el) return;
  const f=syncFreshness();
  const color=f.lvl==="green"?"var(--green)":f.lvl==="amber"?"var(--amber)":f.lvl==="red"?"var(--red)":"var(--sub)";
  const liveTag = LIVE ? '<b style="color:var(--primary)">⚡ 在线直连</b> · ' : "";
  // 窄屏只留「数据源 + 新鲜度」，时间戳进详情弹窗（点 chip 就能看，不必挤在顶栏）
  const narrow = window.innerWidth < 760;
  const head = MODEL.isDemo ? "演示数据" : ("真实数据 · "+f.txt);
  const tail = narrow ? "" : (f.h==null?"":(" · 同步于 "+MODEL.synced_at+" · "+fmtAge(f.h)));
  el.innerHTML='<span class="dot" style="background:'+color+'"></span>'+liveTag+head+tail;
  el.title = "数据快照 "+MODEL.snapshot+(MODEL.synced_at?(" · 同步于 "+MODEL.synced_at):"")
    + (f.h==null?"":"（"+fmtAge(f.h)+"）") + " · 点这里看详情";
}
function openSync(){
  const f=syncFreshness();
  const color=f.lvl==="green"?"var(--green)":f.lvl==="amber"?"var(--amber)":f.lvl==="red"?"var(--red)":"var(--sub)";
  const rows=[
    ["数据来源", MODEL.isDemo?"本地演示数据":("SeaTable 云「"+(MODEL.base_name||"生产")+"」+ PartDB 实时")],
    ["业务表同步于", MODEL.synced_at||"未知"],
    ["距今", f.h==null?"未知":fmtAge(f.h)],
    ["PartDB 物料快照", MODEL.partdb_at||"未知"],
    ["数据新鲜度", '<span class="dot" style="background:'+color+'"></span>'+f.txt],
    ["来源健康", (function(){
        const s=MODEL.source_health||{};
        const c=(s.worst==="bad")?"var(--red)":(s.worst==="warn")?"var(--amber)":"var(--green)";
        return '<span class="dot" style="background:'+c+'"></span>过旧 '+(s.n_bad||0)
          +' · 偏旧 '+(s.n_warn||0)+'（详见「分析 › 来源健康」）';
      })()],
    ["当前状态", MODEL.isDemo?"演示模式（非真实库）":"已接入真实库 · 只读快照"],
  ];
  let html='<div class="modal-mask" id="syncMask"><div class="modal"><div class="modal-h">__IC_SYNC__ 同步状况</div><table class="sync-t"><tbody>';
  for(const r of rows) html+='<tr><td class="sk">'+r[0]+'</td><td class="sv">'+r[1]+'</td></tr>';
  html+='</tbody></table>';
  if(f.lvl==="red") html+='<div class="note" style="color:var(--red)">⚠ 距上次同步已超过 '+satFreshThresholds().bad+' 小时，建议重新同步获取最新数据。</div>';
  html+='<div class="note">本驾驶舱是生成时的冻结快照，页面不会自联动物联网拉取。点下方按钮复制「重新同步」指令，粘贴到 WorkBuddy 发送，我就会重跑同步并重新生成 HTML。</div>';
  html+='<div class="modal-ft"><button class="btn" id="btnCopySync">一键复制重新同步指令</button><button class="btn btn-ghost" id="btnCloseSync">关闭</button></div></div></div>';
  const tmp=document.createElement("div"); tmp.innerHTML=html; document.body.appendChild(tmp.firstChild);
  const mask=$("#syncMask");
  $("#btnCloseSync").onclick=()=>mask.remove();
  mask.onclick=e=>{ if(e.target===mask) mask.remove(); };
  $("#btnCopySync").onclick=()=>{
    const txt="请重新同步数据并重生成「生产·项目管理驾驶舱」：运行 seatable_sync.py 拉取最新 SeaTable 云端 + PartDB 实时物料，再运行 cockpit.py 重新渲染。当前同步时间 "+(MODEL.synced_at||"未知")+"。";
    const done=()=>alert("已复制「重新同步」指令 ✅\n在下方对话框 Ctrl+V 粘贴并发送，我立刻重跑同步+渲染并交付新 HTML。");
    if(navigator.clipboard&&navigator.clipboard.writeText) navigator.clipboard.writeText(txt).then(done,()=>fallbackCopy(txt,done));
    else fallbackCopy(txt,done);
  };
}
function setupLock(){
  const mask=document.getElementById("lockMask");
  const saved=sessionStorage.getItem("cockpit_unlocked");
  if(saved){
    try{
      const u=JSON.parse(saved);
      if(u && (u.level==="admin" || (u.level==="role" && ROLES[u.role]))){ UNLOCK=u; mask.style.display="none"; return; }
    }catch(e){}
  }
  const role=currentRole();
  document.getElementById("lockRole").textContent="当前视图："+ROLES[role].name;
  const inp=document.getElementById("lockInput"), btn=document.getElementById("lockBtn"), err=document.getElementById("lockErr");
  const tryU=()=>{
    const v=inp.value.trim();
    if(v===PW.admin){ UNLOCK={level:"admin"}; }
    else if(PW[role] && v===PW[role]){ UNLOCK={level:"role",role:role}; }
    else { err.textContent="口令错误，请重试"; inp.value=""; inp.focus(); return; }
    sessionStorage.setItem("cockpit_unlocked", JSON.stringify(UNLOCK));
    mask.style.display="none";
    render(MODEL);
  };
  btn.onclick=tryU;
  inp.addEventListener("keydown",e=>{ if(e.key==="Enter" && !e.isComposing && e.keyCode!==229) tryU(); });
  const eye=document.getElementById("lockEye"), eyeIcon=document.getElementById("lockEyeIcon");
  if(eye){ eye.onclick=()=>{
    const p=inp.classList.toggle("pw-plain");
    if(eyeIcon){
      eyeIcon.innerHTML = p
        ? '<path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.45 18.45 0 0 1 5.06-5.94"/><path d="M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19"/><path d="M14.12 14.12a3 3 0 1 1-4.24-4.24"/><line x1="1" y1="1" x2="23" y2="23"/>'
        : '<path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/>';
    }
    inp.focus();
  }; }
  inp.focus();
}
window.addEventListener("DOMContentLoaded",()=>{
  setupLock();
  render(MODEL);
  /* ── v6 外壳交互：下拉菜单 / 抽屉 / 路由 ─────────────────────────── */
  const more=$("#btnMore");
  if(more) more.onclick=e=>{ e.stopPropagation(); toggleMenu("#moreMenu"); };
  document.addEventListener("click",e=>{
    if(!e.target.closest(".dd")) closeMenus();
  });
  document.addEventListener("keydown",e=>{ if(e.key==="Escape"){ closeMenus(); closeDrawer(); } });
  const bNew=$("#btnNew"); if(bNew) bNew.onclick=()=>{ closeMenus(); openDrawer("WZ"); };
  const bBF=$("#btnBF");   if(bBF)  bBF.onclick=()=>{ closeMenus(); openDrawer("BF"); };
  const dClose=$("#drawerClose"); if(dClose) dClose.onclick=closeDrawer;
  const dMask=$("#drawerMask");   if(dMask)  dMask.onclick=closeDrawer;
  const dChip=$("#dataChip");     if(dChip)  dChip.onclick=openSync;
  /* 旧的「导出/同步」等按钮仍在 ⋯ 菜单里，点完自动收起菜单 */
  ["btnSync","btnRefresh","btnExport","btnImport","btnAnalyze"].forEach(id=>{
    const b=document.getElementById(id); if(b) b.addEventListener("click",()=>closeMenus());
  });
  $("#btnExport").onclick=exportJSON;
  $("#btnSync").onclick=openSync;
  refreshSyncBadge();
  $("#btnAnalyze").onclick=analyzeData;
  $("#btnImport").onclick=()=>$("#fileInput").click();
  $("#fileInput").onchange=e=>{ if(e.target.files[0]) importJSON(e.target.files[0]); };
  $("#btnRefresh").onclick=()=>{
    if(LIVE){
      toast('正在重新生成快照 …');
      liveAPI("/api/refresh", {}, r=>{
        if(r.ok){ toast('已刷新 ✅ '+ (r.snapshot_time||'')); setTimeout(()=>location.reload(), 600); }
        else toast('刷新失败：'+(r.error||('exit '+r.exit)));
      });
    }else{
      alert("离线模式：在技能目录运行\npython cockpit/cockpit.py\n即可用最新本地数据重新生成此驾驶舱 HTML。\n\n想一键刷新？启动伴生服务器：python cockpit/cockpit_server.py");
    }
  };
  // 在线模式探测：启动即异步探测本机伴生服务器，成功后按钮自动切换为直连模式
  liveDetect(ok=>{ refreshSyncBadge(); });
  // 口令管理弹窗（仅管理员可见）
  $("#pwClose").onclick=closePwModal;
  $("#pwModalMask").onclick=closePwModal;
  $("#pwRotate").onclick=rotateAll;
  $("#pwCopyAll").onclick=copyAllPw;
  // 补录面板：事件委托（render 会重建 DOM，用委托保证每次都生效）
  // v6：补录区块搬进了抽屉，所以抽屉容器也要挂同一套委托
  const bfHandler=e=>{ if(e.target.matches("#bfBody [data-rid]")) onBfChange(); };
  $("#app").addEventListener("change", bfHandler);
  const dBody=$("#drawerBody"); if(dBody) dBody.addEventListener("change", bfHandler);
  // 角色切换 / 标签切换：URL hash 变化即重渲染对应视图
  window.addEventListener("hashchange", ()=>render(MODEL));
});
</script>
</body>
</html>
"""


def main():
    # Windows 控制台默认 GBK，末行 print 里的 ¥ 会抛 UnicodeEncodeError 让整个进程退出码=1
    # （HTML 其实已写好，但自动化会误判成失败）。统一把 stdout 切到 UTF-8。
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    # 支持 --config，使其能对指定的库生成驾驶舱。缺了它的话，
    # op.py 写入 A 库后刷新出来的却是默认库的驾驶舱（写 A 看 B）。
    argv = sys.argv[1:]
    cfg_path = None
    rest = []
    n = 0
    while n < len(argv):
        a = argv[n]
        if a == "--config" and n + 1 < len(argv):
            cfg_path = argv[n + 1]
            n += 2
            continue
        if a.startswith("--config="):
            cfg_path = a.split("=", 1)[1]
            n += 1
            continue
        rest.append(a)
        n += 1

    out = rest[0] if rest else DEFAULT_OUT
    adapter = get_adapter(load_config(cfg_path) if cfg_path else None)
    adapter.auth()
    today = datetime.now(_TZ).date()
    model = compute(_NormAdapter(adapter), today)
    html = HTML.replace("__MODEL__", json.dumps(model, ensure_ascii=False))
    for k, v in ICONS.items():
        html = html.replace("__" + k + "__", v)
    html = html.replace("__PW_BLOB__", json.dumps(PW_BLOB))
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"[ok] 驾驶舱已生成：{out}")
    print(f"     快照日期 {model['snapshot']} · 项目 {model['kpi']['projects']} · "
          f"合同额 ¥{model['kpi']['contract']:,.0f} · 采购逾期 {model['kpi']['purchase_overdue']}")


if __name__ == "__main__":
    main()
