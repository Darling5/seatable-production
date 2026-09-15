#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
cockpit.py — 生产·项目管理驾驶舱 生成器。

读取 seatable-production 技能本地库（14 张表），计算「工时 / 成本 / 质量 / 供应链」
四维指标 + 项目总览 + 在制品看板 + 物料库存预警，渲染为单文件、内联 SVG、响应式
HTML 驾驶舱。

用法：
    python cockpit.py                      # 输出到默认工作区
    python cockpit.py 路径/驾驶舱.html      # 自定义输出路径

数据来源：与 op.py 同源的适配器，local / seatable 自动切换。
生成的是「数据快照」：HTML 内嵌当前计算结果；也可用页面内「导入数据」按钮载入
导出的 JSON 快照刷新，无需重跑本脚本。
"""
import os
import sys
import json
import re
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
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
    snap = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "partdb_snapshot.json")
    if not os.path.exists(snap):
        return None
    try:
        with open(snap, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _load_sync_meta():
    """读取 seatable_sync.py 写入的同步标记；存在说明业务表已是云端真实数据。"""
    meta = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "_sync_meta.json")
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
    p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", name)
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
    p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "foresee.json")
    if not os.path.exists(p):
        return None
    try:
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


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
    p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "思维导图.json")
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
    smt_yield = 0.0
    if smt_rows:
        tot = sum(_num(r.get("贴片数量")) for r in smt_rows)
        good = sum(_num(r.get("良品数量")) for r in smt_rows)
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
<div class="wrap">
  <header class="top">
    <span class="demo-flag" id="demoFlag"></span>
    <h1>__TITLE_ICON__ 生产 · 项目管理驾驶舱</h1>
    <div class="meta" id="metaLine"></div>
    <div class="toolbar">
      <button class="btn" id="btnTheme" title="切换主题：自动 / 暗色 / 亮色">__IC_THEME__ 主题：自动</button>
      <button class="btn" id="btnExport">__IC_DOWNLOAD__ 导出分析JSON</button>
      <button class="btn" id="btnImport">__IC_UPLOAD__ 导入数据快照</button>
      <button class="btn btn-sync" id="btnAnalyze">__IC_ANALYZE__ 分析数据（复制发我）</button>
      <button class="btn" id="btnRefresh">__IC_REFRESH__ 重新生成说明</button>
      <button class="btn btn-sync" id="btnSync">__IC_SYNC__ 同步状况</button>
      <input type="file" id="fileInput" accept="application/json" style="display:none">
      <span class="sync-badge" id="syncBadge"></span>
    </div>
    <div class="banner" id="banner"></div>
  </header>

  <div id="app"></div>

  <footer>驾驶舱由 seatable-production 技能数据快照生成 · 单文件离线可用 · 重跑 cockpit.py 可刷新</footer>
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
function pct(n){ return (n==null?"—":n+"%"); }
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
      <code>python extract_mindmaps.py</code> 生成 <code>data/思维导图.json</code> 后重跑本页。</div>`;
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
  // 老板：只看数据，不含任何写入口（新建/补录均为项目经理职责，不出现在老板页）
  // core = 首屏核心模块（≤4）；more = 折叠进「更多分析」的二级模块
  boss:      {name:"老板",     sections:["K","A","PW","G","T","C","Q","P","Sup","Inv","Rs","WXC","WXM","FC","Mkt","Raw","PT","PL","MM"],
              core:["K","A","PW"],                more:["WXC","WXM","FC","Mkt","Raw","Rs","G","T","C","Q","P","Sup","Inv","PT","PL","MM"], actions:null},
  // 仓库/采购：非项目经理，不开放「新建」写入口（仅看数据 + 各自作业动作）
  warehouse: {name:"仓库",     sections:["K","A","Inv","P"],
              core:["K","A","Inv"],               more:["P"],                     actions:["warehouse"]},
  // 原料行情对采购最有用：决定报价有效期与备货节奏，故给采购页也开
  purchase:  {name:"采购",     sections:["K","A","Sup","FC","Mkt","Raw"],
              core:["K","A","Sup","FC"],          more:["Mkt","Raw"],             actions:["purchase","market"]},
  // 生产经理：项目经理职责 → 新建生产计划（写「生产计划」表）+ 资源排程 + 风险雷达
  // 项目经理视角的完整台账（项目全表 / 生产计划全表 / 流程思维导图）默认进首屏
  production:{name:"生产经理", sections:["K","A","WZ","Rs","FC","G","T","Q","P","Inv","WXC","WXM","PT","PL","MM"],
              core:["K","A","PT","MM"],           more:["WZ","Rs","FC","G","T","Q","P","Inv","WXC","WXM","PL"],   actions:["production","warehouse","delivery","resource","wechat"]},
  // 销售：立项职责 → 新建项目（写「项目」表，对应销售立项表单）
  sales:     {name:"销售",     sections:["K","A","PW","WZ","G","WXM","PT","MM"],
              core:["K","A","PW","WZ"],           more:["G","WXM","PT","MM"],                     actions:["sales","delivery"]},
};
const ROLE_ORDER=["boss","production","purchase","warehouse","sales"];
function currentRole(){
  const m=/role=([a-z]+)/.exec(location.hash||"");
  const r=m?m[1]:"";
  return ROLES[r]?r:"production";
}
function buildRoleBar(role, unlock){
  const isAdmin = unlock && unlock.level==="admin";
  const keys = isAdmin ? ROLE_ORDER : [role];
  const tabs=keys.map(key=>`<button class="role-tab ${key===role?"active":""}" data-role="${key}">${ROLES[key].name}</button>`).join("");
  const shareBtn=(role==="boss"||role==="sales")
    ? `<button class="role-share" id="roleShareBtn">__IC_SHARE__ 分享给${ROLES[role].name}</button>` : "";
  const keyBtn = isAdmin
    ? `<button class="role-key" id="pwManageBtn">__IC_KEY__ 口令管理</button>` : "";
  const hint = isAdmin
    ? `每人开一个标签页钉住自己的 #role 即独立窗口；视图按角色裁剪，敏感财务仅老板可见`
    : `本视图已按口令锁定（${ROLES[role].name}），如需切换其他视图请用管理员口令重新打开`;
  const bar=el(`<div class="role-bar"><span class="role-lbl">角色视图</span>${tabs}${shareBtn}${keyBtn}
    <span class="role-hint">${hint}</span></div>`);
  bar.querySelectorAll(".role-tab").forEach(b=>b.onclick=()=>{ if(isAdmin) location.hash="role="+b.dataset.role; });
  const sb=bar.querySelector("#roleShareBtn");
  if(sb) sb.onclick=()=>shareRole(role);
  const kb=bar.querySelector("#pwManageBtn");
  if(kb) kb.onclick=openPwModal;
  return bar;
}
function supplierAvg(s){
  const rs=(s.supplier||[]).map(x=>x.rate).filter(x=>x!=null);
  return rs.length? rs.reduce((a,b)=>a+b,0)/rs.length : 0;
}
function buildKPIs(m, role){
  const k=m.kpi, t=m.time, q=m.quality, s=m.supply, pd=m.partdb, res=m.resource;
  const wx=m.wechat, mk=m.market;
  const b=pd?pd.bom:null;
  const M={
    projects:{l:"项目总数",v:fmt(k.projects),s:`进行中 ${k.active} · 计划 ${k.planned} · 完成 ${k.done}`,c:"",ac:"blue"},
    contract:{l:"总合同额",v:yuan(k.contract),s:"已收 "+yuan(k.received),c:"",ac:"blue"},
    received:{l:"已收金额",v:yuan(k.received),s:"合同执行率 "+pct(k.exec_rate),c:"",ac:"green"},
    receivable:{l:"应收款",v:yuan(k.receivable),s:"待回收 · 表内待收 "+yuan((m.recv_check||{}).stored),c:"neg",ac:"red"},
    cost:{l:"生产总花销",v:yuan(k.cost),s:"毛利率 "+pct(k.margin),c:"",ac:"amber"},
    margin:{l:"毛利率",v:pct(k.margin),s:"目标 30%",c:"",ac:"green"},
    unit_cost:{l:"单片成本",v:yuan(k.unit_cost),s:"台账均单价",c:"",ac:"amber"},
    exec_rate:{l:"合同执行率",v:pct(k.exec_rate),s:"已收 / 合同",c:"",ac:"blue"},
    ontime_rate:{l:"交期达成率",v:pct(k.ontime_rate),s:"在制 "+k.wip+" 单",c:"",ac:"blue"},
    purchase_overdue:{l:"采购逾期",v:fmt(k.purchase_overdue),s:"需跟进",c:k.purchase_overdue>0?"neg":"",ac:"red"},
    wip:{l:"在制单数",v:fmt(k.wip),s:"进行中生产",c:"",ac:"blue"},
    shortage:{l:"在产缺料",v:fmt(b?b.shortage.length:0),s:`零确认 ${pd?pd.zero_confirmed:0} 种`,c:(b&&b.shortage.length)?"neg":"",ac:"red"},
    kit_rate:{l:"物料齐套率",v:pct(b?((b.bom_count-b.shortage.length)/b.bom_count*100):100),s:`BOM ${b?b.bom_count:0} 行`,c:"",ac:"green"},
    part_count:{l:"在库料号",v:fmt(pd?pd.part_count:0),s:"零件总数",c:"",ac:"blue"},
    zero_stock:{l:"零确认库存",v:fmt(pd?pd.zero_confirmed:0),s:"需盘点",c:(pd&&pd.zero_confirmed>0)?"neg":"",ac:"red"},
    supplier_ontime:{l:"供应商准时率",v:pct(supplierAvg(s)),s:`${s.supplier.length} 家`,c:supplierAvg(s)<70?"neg":"",ac:"amber"},
    smt_yield:{l:"贴片良品率",v:pct(q.smt_yield),s:"良品 / 投入",c:"",ac:"green"},
    repair_rate:{l:"维修率",v:pct(q.repair_rate),s:`${q.repair_total}/${q.shipped}`,c:"",ac:"red"},
    cycle:{l:"平均生产周期",v:t.avg_cycle+"天",s:"实际花费天数",c:"",ac:"blue"},
    res_load:{l:"资源平均负载",v:res?pct(res.avg_load):"—",s:res?`在岗 ${res.on_duty} 人/台`:"未启用资源管理",
      c:(res&&res.avg_load>100)?"neg":"",ac:"purple"},
    res_over:{l:"超载资源",v:fmt(res?res.over.length:0),s:res?`共 ${res.total} 项资源`:"未启用",
      c:(res&&res.over.length)?"neg":"",ac:"red"},
    res_conflict:{l:"排程冲突",v:fmt(res?res.conflicts.length:0),s:"同人同期多任务",
      c:(res&&res.conflicts.length)?"neg":"",ac:"red"},
    labor_cost:{l:"人工成本",v:res?yuan(res.labor_cost):"—",s:"投入量 × 日费率",c:"",ac:"amber"},
    market_alert:{l:"物料行情预警",v:fmt(mk?mk.alerts.length:0),
      s:mk?`停产物料 ${mk.alerts.filter(a=>a.type==="停产").length} · 阈值±${mk.threshold}%`:"未启用行情监控",
      c:(mk&&mk.alerts.length)?"neg":"",ac:"red"},
    wechat_pending:{l:"微信待确认",v:fmt(wx?wx.pending_count:0),
      s:wx?`今日新事件 ${wx.today_count}`:"未接入微信情报",
      c:(wx&&wx.pending_count)?"neg":"",ac:"amber"},
  };
  // 首屏只留最多 4 张「一眼定生死」的指标，其余下沉到「更多分析 → 更多指标」
  const sets={
    boss:      ["contract","receivable","margin","purchase_overdue"],
    warehouse: ["shortage","kit_rate","zero_stock","part_count"],
    purchase:  ["purchase_overdue","supplier_ontime","shortage","ontime_rate"],
    production:["wip","ontime_rate","shortage",res?"res_over":"cycle"],
    sales:     ["contract","received","receivable","ontime_rate"],
  };
  const setsMore={
    boss:      ["projects","cost","ontime_rate","unit_cost","exec_rate","wip"]
                 .concat(res?["res_load","labor_cost"]:[])
                 .concat(mk?["market_alert"]:[]).concat(wx?["wechat_pending"]:[]),
    warehouse: [],
    purchase:  [].concat(mk?["market_alert"]:[]),
    production:["smt_yield","repair_rate","cycle"]
                 .concat(res?["res_load","res_conflict","labor_cost"]:[])
                 .concat(wx?["wechat_pending"]:[]),
    sales:     ["projects","wip","exec_rate"],
  };
  const pick=(arr)=>(arr||[]).map(key=>M[key]).filter(Boolean);
  return {core:pick(sets[role]||sets.boss), more:pick(setsMore[role])};
}
function kpiCard(x){
  return el(`<div class="card kpi ac-${x.ac}"><div class="top"><span class="ac-dot"></span><span class="lbl">${x.l}</span></div>
     <div class="v ${x.c}">${x.v}</div><div class="sub">${x.s}</div></div>`);
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
  const app=$("#app"); app.innerHTML="";
  /* 分流器：core → 首屏直接挂载；more → 收进「更多分析」折叠区；都不在 → 不渲染 */
  const CORE=ROLES[role].core||ROLES[role].sections, MORE=ROLES[role].more||[];
  const moreBody=el(`<div class="more-body" id="moreBody"></div>`);
  const put=(key,node)=>{
    if(CORE.includes(key)) app.appendChild(node);
    else if(MORE.includes(key)) moreBody.appendChild(node);
  };
  $("#metaLine").textContent="数据快照："+m.snapshot
    + (m.partdb?" · 物料/缺料接入 PartDB 实时（"+m.partdb.generated_at+"）":"")
    + (m.synced_at?" · 业务表接入 SeaTable 云「"+(m.base_name||"生产")+"」（同步 "+m.synced_at+"）":"")
    + " · 四维分析（工时/成本/质量/供应链）";
  const flag=$("#demoFlag");
  if(m.isDemo){
    flag.textContent="演示数据"; flag.className="demo-flag"; flag.style.display="inline-block";
    $("#banner").innerHTML="当前为<b>演示数据</b>（虚构示例）。清空本地 data/ 后录入真实数据，重跑 cockpit.py 即可生成你的真实驾驶舱。";
  }else{
    flag.textContent="真实数据 · SeaTable云"; flag.className="real-flag"; flag.style.display="inline-block";
    $("#banner").innerHTML="业务表已接入 SeaTable 云端「"+(m.base_name||"生产")+"」真实数据（同步于 "+m.synced_at+"）。物料/缺料来自 PartDB 实时。"
      + (m.partdb?"":"<span style='color:var(--red)'> ⚠ PartDB 未连接。</span>");
  }
  app.appendChild(buildRoleBar(role, UNLOCK));

  /* KPI 概览（按角色裁剪，首屏最多 4 张） */
  const k=m.kpi;
  const kpiG=buildKPIs(m, role);
  const secK=el(`<section id="sec-K" class="sec"><div class="sec-title">__IC_GRID__ 核心指标概览 · ${ROLES[role].name}</div>
    <div class="hscroll" id="kpig"></div></section>`);
  kpiG.core.forEach(x=>secK.querySelector("#kpig").appendChild(kpiCard(x)));
  put("K", secK);

  /* 今天要处理：首屏置顶，只留高/中优先级，一键跳到对应模块 */
  const actsAll=m.next_actions||[];
  const acts=(role==="boss")?actsAll:actsAll.filter(a=>(ROLES[role].actions||[]).includes(a.cat));
  // 每类事项按「候选模块」顺序找第一个当前角色可见的模块，保证「去处理」按钮总有落点
  const CAT_SEC={purchase:["Sup","Inv","P"],warehouse:["Inv","P","Sup"],
    delivery:["PW","P","G"],production:["P","G","T","PW"],sales:["PW","G"],boss:["PW","G"],
    resource:["Rs","G","T"],wechat:["WXC"],market:["Mkt"],risk:["FC"]};
  const visible=(kk)=>CORE.includes(kk)||MORE.includes(kk);
  const urgent=acts.filter(a=>a.pri==="高"||a.pri==="中").slice(0,6);
  const todayBody=urgent.length
    ? urgent.map(a=>{
        const tgt=(CAT_SEC[a.cat]||[]).find(visible);
        const jump=tgt?`<button class="go" data-jump="sec-${tgt}">去处理 →</button>`:"";
        return `<div class="today-item p-${a.pri}"><span class="tx">${a.text}</span>${jump}</div>`;
      }).join("")
    : `<div class="today-item p-提示"><span class="tx">今天没有逾期或临期事项，保持当前节奏即可 ✔</span></div>`;
  const secToday=el(`<div class="today" id="sec-Today">
    <div class="today-hd"><span class="t">今天要处理</span>
      ${urgent.length?`<span class="cnt">${urgent.length} 项</span>`:`<span class="ok">全部正常</span>`}
      <span class="note" style="margin:0">数据快照 ${m.snapshot} · 仅显示高/中优先级</span></div>
    <div class="today-list">${todayBody}</div></div>`);
  secToday.querySelectorAll("[data-jump]").forEach(b=>b.onclick=()=>{
    const key=b.dataset.jump.replace("sec-","");
    if(MORE.includes(key)) openMore();          // 目标在折叠区 → 先展开
    const t=document.getElementById(b.dataset.jump);
    if(t) t.scrollIntoView({behavior:"smooth",block:"start"});
  });
  app.appendChild(secToday);
  if(CORE.includes("K")) app.insertBefore(secToday, secK);

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

  const secPW=el(`<section id="sec-PW" class="sec"><div class="sec-title">__IC_PROJ__ 项目总览 & 在制品看板</div>
    <div class="grid g2">
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
  const secG=el(`<section id="sec-G" class="sec"><div class="sec-title">__IC_GANT__ 生产计划甘特图（三种状态：实际进度 · 合同交期 · 历史推算交期）</div>
    <div class="card"><div class="gantt-tools" id="ganttTools"></div>
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
  const stageItems=Object.entries(t.stage_dist).map(([k,v],i)=>({name:k,value:v,
    color:["#3b5bdb","#1c7ed6","#0ca678","#f08c00","#7048e8","#e8590c"][i%6]}));
  const cycleTxt=t.cycle_list.length?t.cycle_list.map(c=>c.product+":"+c.days+"天").join(" · "):"暂无工序耗时记录";
  const secT=el(`<section id="sec-T" class="sec"><div class="sec-title">__IC_TIME__ 工时分析（交付能力）</div>
    <div class="grid g3">
      <div class="card"><h3>交期达成率（内部周期）</h3><div class="donut-wrap">
        <div>${donut(t.ontime_rate,"#3b5bdb")}</div>
        <div class="legend"><div class="row"><span class="dot" style="background:#3b5bdb"></span>
          <span class="nm">周期达成</span><span class="vl">${t.ontime}/${t.dated||0}</span></div>
          <div class="row"><span class="nm" style="color:var(--sub)">平均实际周期</span>
          <span class="vl">${t.avg_cycle}天</span></div></div></div>
        <div class="note">${cycleTxt}</div></div>
      <div class="card"><h3>在制阶段分布</h3>${bars(stageItems)}</div>
      <div class="card"><h3>说明</h3>
        <div class="note">交期达成率 = 内部周期达成：实际花费天数 ≤ 允许的(合同交期−立项)天数 的计划占比。<br>
        平均实际周期 = 各计划「花费天数」均值（真实工序耗时）。<br>
        阶段分布反映当前产能瓶颈所在工序。</div></div>
    </div></section>`);
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
  const ageItems=[["已逾期","var(--red)"],["30","var(--amber)"],["60","var(--blue)"],["90","var(--primary)"]]
    .map(([k,col])=>({name:k,value:Math.round(ageMap[k]||0),color:col}));
  const recvBarsHTML=c.receivable_list.length?`<div style="margin-top:14px">
    <h3 style="font-size:13px;color:var(--sub);font-weight:600;margin-bottom:2px">应收账龄分布（元）</h3>
    ${bars(ageItems,{fmt:v=>yuan(v)})}</div>`:"";
  const secC=el(`<section id="sec-C" class="sec"><div class="sec-title">__IC_COST__ 成本分析（盈利能力）</div>
    <div class="grid g2">
      <div class="card"><h3>成本结构（总花销 ${yuan(c.cost)}）</h3>
        <div class="donut-wrap"><div>${pie(catItems,150)}</div>
        <div class="legend">${catItems.map((x,i)=>`<div class="row"><span class="dot" style="background:${x.color}"></span>
          <span class="nm">${x.name}</span><span class="vl num">${yuan(x.value)}</span></div>`).join("")}</div></div></div>
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
    </div></section>`);
  put("C", secC);

  /* 质量 */
  const q=m.quality;
  const secQ=el(`<section id="sec-Q" class="sec"><div class="sec-title">__IC_QUAL__ 质量分析（交付质量）</div>
    <div class="grid g3">
      <div class="card"><h3>贴片良品率</h3><div class="donut-wrap">
        <div>${donut(q.smt_yield,"#0ca678")}</div>
        <div class="legend"><div class="row"><span class="dot" style="background:#0ca678"></span>
          <span class="nm">良品 / 投入</span><span class="vl">${pct(q.smt_yield)}</span></div>
          <div class="row"><span class="nm" style="color:var(--sub)">组装良品率</span><span class="vl">${q.asm_yield==null?'未录入':pct(q.asm_yield)}</span></div></div></div>
        <div class="note">组装良品率：2 条组装记录均未填良品率，暂无法计算（并非 0%）。</div></div>
      <div class="card"><h3>维修率</h3><div class="donut-wrap">
        <div>${donut(q.repair_rate,"#e8590c")}</div>
        <div class="legend"><div class="row"><span class="dot" style="background:#e8590c"></span>
          <span class="nm">维修 ${q.repair_total} / 发货 ${q.shipped}</span><span class="vl">${pct(q.repair_rate)}</span></div>
          <div class="row"><span class="nm" style="color:var(--sub)">超期未完修</span><span class="vl ${q.repair_overdue>0?'neg':''}">${q.repair_overdue}</span></div></div></div>
        <div class="note">平均返修周期 ${q.repair_avg} 天</div></div>
      <div class="card"><h3>维修明细</h3>
        ${q.repair_list.length?`<table data-paginate="8" data-filter="1" data-select="1"><thead><tr><th>项目</th><th>问题</th><th>返修</th><th>状态</th></tr></thead><tbody>`
          +q.repair_list.map(r=>`<tr><td>${r.proj}</td><td>${r.item||"—"}</td><td class="num">${r.back}</td>
          <td><span class="pill ${r.done?'tag-green':(r.overdue?'tag-red':'tag-amber')}">${r.done?'已完成':(r.overdue?'超期':'处理中')}</span></td></tr>`).join("")
          +`</tbody></table>`:`<div class="empty">无维修记录</div>`}</div>
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
    /* 缺料缺口 Top 条形图（红=零确认库存，橙=部分缺口）*/
    const gapItems=(b&&b.shortage||[]).slice().sort((a,b2)=>b2.gap-a.gap).slice(0,8)
      .map(x=>({name:x.name,value:x.gap,color:x.confirmed===0?"var(--red)":"var(--amber)"}));
    const gapHTML=gapItems.length?`<div style="margin-top:14px">
      <h3 style="font-size:13px;color:var(--sub);font-weight:600;margin-bottom:2px">缺口 Top（按缺口件数）</h3>
      ${bars(gapItems)}</div>`:"";
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
    <div class="grid g2">
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
      :`<tr><td colspan="7" class="empty">监控清单为空。在技能目录运行 python market.py watchlist 从采购记录生成；行情由每周一自动检查回填。</td></tr>`;
    const mktAlerts=mk.alerts.length?`<div class="card" style="margin-top:14px">
      <h3>行情告警（${mk.alerts.length}）</h3><div class="actions">${mk.alerts.map(a=>
        `<div class="act"><span class="pri ${a.type==='停产'?'pri-高':'pri-中'}">${a.type}</span>
         <span class="tx"><b>${a.model}</b>：${a.text}</span></div>`).join("")}</div>
      <div class="note">停产/NRND = 高优（确认替代料、锁定最后采购窗口）；涨跌超阈值 = 中优（评估提前备货或换源）。</div></div>`:"";
    const secMkt=el(`<section id="sec-Mkt" class="sec"><div class="sec-title">__IC_MKT__ 物料行情（价格涨跌 · 停产/EOL）</div>
      <div class="card"><div style="overflow-x:auto"><table data-paginate="12" data-filter="1"><thead><tr>
        <th>物料型号</th><th>上次采购价</th><th>最新行情</th><th>vs采购价</th><th>生命周期</th><th>趋势</th><th>行情日期</th>
      </tr></thead><tbody>${mktRows}</tbody></table></div>
      <div class="note">红=涨价、绿=降价（采购成本视角）。vs采购价偏差 ≥ ±${mk.threshold}% 或生命周期 NRND/EOL停产 触发告警。行情数据由每周一自动检查（立创/华秋等渠道现货价 + 原厂生命周期公告），也可随时对我说「查一下 XX 的行情」即时补录。</div></div>
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
      :`<tr><td colspan="7" class="empty">暂无原料行情。在技能目录运行 python market.py raw fetch 拉取当日价，raw backfill AU --contract au2610 --days 40 补历史。</td></tr>`;
    const rawAlerts=raw.alerts.length?`<div class="card" style="margin-top:14px">
      <h3>原料波动告警（${raw.alerts.length}）</h3><div class="actions">${raw.alerts.map(a=>
        `<div class="act"><span class="pri ${a.type==='涨'?'pri-高':'pri-中'}">${a.type}</span>
         <span class="tx"><b>${a.name}</b>：${a.text}</span></div>`).join("")}</div>
      <div class="note">原料波动影响的是<b>整体报价基调</b>，不是单个料号——涨了复核供应商报价有效期与备货节奏，跌了可择机锁价。</div></div>`:"";
    const secRaw=el(`<section id="sec-Raw" class="sec"><div class="sec-title">__IC_MKT__ 原料行情（上游成本 · 金属 / 塑料）</div>
      <div class="card"><div style="overflow-x:auto"><table data-paginate="12" data-filter="1"><thead><tr>
        <th>原料</th><th>口径</th><th>最新价</th><th>区间涨跌</th><th>趋势</th><th>日期</th><th>来源</th>
      </tr></thead><tbody>${rawRows}</tbody></table></div>
      <div class="note">红=涨、绿=跌（成本视角）。近 ${raw.days} 天波动 ≥ ±${raw.threshold}% 触发告警（原料波动比单个料号频繁，阈值单独设，默认 5%）。
        <b>口径纪律</b>：期货「连续」与「具体合约」是两个口径，同原料出现多行属正常，<b>不跨口径比价</b>——混比会算出假涨跌。
        ABS/PC/PS 树脂现货无免费公开 API，走人工录入（<code>python market.py raw add ABS --price 11800</code>）。</div></div>
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
      :`<tr><td colspan="6" class="empty">暂无待确认事件。每日 9 点自动拉取微信监控群新消息并提取事件；「示例」开头的演示数据可运行 python wechat_intake.py clear-demo 清除。</td></tr>`;
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
      :`<tr><td colspan="6" class="empty">暂无待核对项。运行 python wxmatch.py scan 扫描监控群消息与合同 PDF。</td></tr>`;
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
        <div class="note"><b>处置方式</b>：在 WorkBuddy 对话里说「核对 WX-M-xxxx 处理」或「忽略 WX-M-xxxx」，专家执行 wxmatch.py done 落留痕；高置信收款项确认后由 wechat_intake approve 写入项目「实收」列。扫描频率：每日 9 点自动化随微信拉取一起跑 <code>python wxmatch.py scan</code>。核对引擎<b>只读</b>，绝不自动写 SeaTable。</div></div>
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
    }).join("")||`<tr><td colspan="6" class="empty">暂无在制计划。运行 <code>python foresee.py</code> 生成风险预测。</td></tr>`;
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
        <div class="note">周期基准取<b>已交付计划的实际工期</b>（立项→交货）而非合同承诺；剩余天数 &lt; p75 即高风险。环节链：${(fb.plans&&fb.plans[0]&&fb.plans[0].stages?fb.plans[0].stages.map(s=>s.stage+"(提前"+s.lead+"天)").join(" → "):"BOM核对→IC/PCB采购→组装料采购→贴片组装→测试发货")}。生成命令：<code>python foresee.py</code>。</div></div>
      <div class="card" style="margin-top:14px"><h3>【2】供应商交期画像 — 承诺 vs 实际（样本 ${fs.samples||0} 条）</h3>
        <div style="overflow-x:auto"><table><thead><tr>
          <th>类别</th><th>样本</th><th>平均偏差</th><th>最差</th><th>建议 buffer</th><th>风险供应商</th>
        </tr></thead><tbody>${supRows}</tbody></table></div>
        <div class="note">偏差 = 实际交期 − 承诺交期（正数=比承诺晚）。排程/缺料 ETA 计算时自动加该类别 buffer；对平均偏差 &gt;5 天的供应商下单时承诺交期需打折看待。</div></div>
      <div class="card" style="margin-top:14px"><h3>【3】缺料预警 — BOM 缺口 × 在途采购（PartDB 快照 ${fsh.snapshot_at||"缺失"}）</h3>
        <div style="overflow-x:auto"><table data-paginate="10" data-filter="1"><thead><tr>
          <th>产品/计划</th><th>交期</th><th>缺口</th><th>缺口量</th><th>在途</th><th>结论</th><th>主要缺口料</th>
        </tr></thead><tbody>${shRows}</tbody></table></div>
        <div class="note">在途 ETA = 下单日 + 承诺交期 + 类别 buffer。「必须立刻下单」= 有缺口且无任何在途；「在途来不及」= 在途 ETA 晚于合同交期，需催货或加急补单。BOM 缺口来自 <code>python partdb_sync.py</code>，之后重跑 <code>python foresee.py</code>。</div></div>
    </section>`);
    put("FC", secFC);
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

  /* 更多分析（二级折叠区）：把非核心模块 + 次要指标收进来，首屏只保留 3-4 块 */
  if(kpiG.more.length){
    const secKM=el(`<section id="sec-KM" class="sec"><div class="sec-title">__IC_GRID__ 更多指标</div>
      <div class="hscroll" id="kpigm"></div></section>`);
    kpiG.more.forEach(x=>secKM.querySelector("#kpigm").appendChild(kpiCard(x)));
    moreBody.insertBefore(secKM, moreBody.firstChild);
  }
  const moreCnt=moreBody.children.length;
  if(moreCnt){
    const tg=el(`<button class="more-tg" id="moreTg" aria-expanded="false">
      <span>更多分析（${moreCnt} 项）</span><span class="arrow">▼</span></button>`);
    const wrap=el(`<div class="more-wrap"></div>`);
    wrap.appendChild(tg); wrap.appendChild(moreBody);
    tg.onclick=()=>{ moreBody.classList.contains("open")?closeMore():openMore(); };
    app.appendChild(wrap);
  }

  /* 快速导航条：首屏模块直接跳；折叠区模块点击时自动展开再跳 */
  const SEC_NAV={K:['核心指标','__IC_GRID__'],KM:['更多指标','__IC_GRID__'],A:['行动建议','__IC_NEXT__'],
    WZ:['新建项目','__IC_ADD__'],BF:['补录数据','__IC_EDIT__'],Rs:['资源负载','__IC_USER__'],
    WXC:['微信情报','__IC_CHAT__'],Mkt:['物料行情','__IC_MKT__'],Raw:['原料行情','__IC_MKT__'],
    WXM:['消息核对','__IC_CHECK__'],FC:['风险雷达','__IC_RADAR__'],
    PW:['项目&在制','__IC_PROJ__'],G:['甘特图','__IC_GANT__'],T:['工时','__IC_TIME__'],
    C:['成本','__IC_COST__'],Q:['质量','__IC_QUAL__'],P:['产线流转','__IC_PROJ__'],
    Sup:['供应链','__IC_SUP__'],Inv:['库存预警','__IC_BOX__'],
    PT:['项目全表','__IC_PROJ__'],PL:['生产计划','__IC_TIME__'],MM:['思维导图','__IC_MIND__']};
  const navKeys=[["Today",null]].concat(
    CORE.filter(k=>SEC_NAV[k]).map(k=>[k,false]),
    MORE.filter(k=>SEC_NAV[k]).map(k=>[k,true]));
  const navHTML=navKeys.map(([k,inMore])=>{
    if(k==="Today") return `<button class="sec-nav-item" data-target="sec-Today">__IC_NEXT__今天要处理</button>`;
    return `<button class="sec-nav-item${inMore?' in-more':''}" data-target="sec-${k}" data-more="${inMore?1:0}">${SEC_NAV[k][1]}${SEC_NAV[k][0]}</button>`;
  }).join("");
  const navBar=el(`<nav class="sec-nav" id="secNav">${navHTML}</nav>`);
  navBar.querySelectorAll(".sec-nav-item").forEach(b=>b.onclick=()=>{
    if(b.dataset.more==="1") openMore();
    const t=document.getElementById(b.dataset.target);
    if(t) t.scrollIntoView({behavior:"smooth",block:"start"});
  });
  app.insertBefore(navBar, secToday);
  initTableTools();
  initPagination();
  observeNav(navBar);
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
function syncFreshness(){
  const sat=MODEL.synced_at||"";
  if(!sat) return {h:null,lvl:"na",txt:"未知"};
  const d=new Date(sat.replace(/-/g,"/"));
  if(isNaN(d.getTime())) return {h:null,lvl:"na",txt:sat};
  const h=(Date.now()-d.getTime())/3600000;
  let lvl="green",txt="数据新鲜";
  if(h>=24){lvl="red";txt="数据可能已过时";}
  else if(h>=6){lvl="amber";txt="较新";}
  return {h:h,lvl:lvl,txt:txt};
}
function refreshSyncBadge(){
  const el=$("#syncBadge"); if(!el) return;
  const f=syncFreshness();
  const color=f.lvl==="green"?"var(--green)":f.lvl==="amber"?"var(--amber)":f.lvl==="red"?"var(--red)":"var(--sub)";
  const age=f.h==null?"":(" · 同步于 "+MODEL.synced_at+" · "+fmtAge(f.h));
  const liveTag = LIVE ? '<b style="color:var(--primary)">⚡ 在线直连</b> · ' : "";
  el.innerHTML='<span class="dot" style="background:'+color+'"></span>'+liveTag+(MODEL.isDemo?"演示数据":("真实数据 · "+f.txt))+age;
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
    ["当前状态", MODEL.isDemo?"演示模式（非真实库）":"已接入真实库 · 只读快照"],
  ];
  let html='<div class="modal-mask" id="syncMask"><div class="modal"><div class="modal-h">__IC_SYNC__ 同步状况</div><table class="sync-t"><tbody>';
  for(const r of rows) html+='<tr><td class="sk">'+r[0]+'</td><td class="sv">'+r[1]+'</td></tr>';
  html+='</tbody></table>';
  if(f.lvl==="red") html+='<div class="note" style="color:var(--red)">⚠ 距上次同步已超过 24 小时，建议重新同步获取最新数据。</div>';
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
      alert("离线模式：在技能目录运行\npython cockpit.py\n即可用最新本地数据重新生成此驾驶舱 HTML。\n\n想一键刷新？启动伴生服务器：python cockpit_server.py");
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
  $("#app").addEventListener("change", e=>{ if(e.target.matches("#bfBody [data-rid]")) onBfChange(); });
  // 角色切换：URL hash 变化即重渲染对应视图
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
