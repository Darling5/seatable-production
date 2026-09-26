# -*- coding: utf-8 -*-
"""application/decision_support/backtest.py — 预测复盘（含防未来信息泄漏）。

复盘这件事最容易自欺的地方，是**用今天才知道的结果，去评价当时的判断**。
那样算出来的准确率能到 100%，也毫无意义 —— 因为在预测的那一天，
那个信息根本还不存在。

所以本模块把「不许用未来信息」做成**可断言的性质**，而不是一句口号：

  · 每条预测记录必须带 `predicted_at`（预测时点）。
  · 所有历史样本必须带 `captured_at`（采集时间）。
  · 任何进入某次回测的样本，都要求 `captured_at <= predicted_at`。
  · `history_median_duration()` 这类函数只接受 `as_of` 参数，
    **测试直接验证**：往样本里塞一条 as_of 之后采集的极端值，结果必须不变。
    如果变了，就说明未来信息漏进来了 —— 这是可以在 CI 里跑死的判定，
    不需要靠人记得小心。

复盘还要有对照，否则「准」是自说自话：
  · 与**历史中位工期基线**比（一个不做任何推理的笨办法）；
  · 与既有 `foresee.py` 的裁判口径对齐（预警正确 / 误报 / 漏报），
    这样两期跑出来的准确率是同一把尺子量的。
  · 样本不足时返回 `insufficient_sample`，**不给数字**。给小样本一个百分比，
    比不给更糟：它看起来像结论，但其实是噪声。

纯计算、无 I/O、无副作用。
"""
from __future__ import annotations

import datetime as _dt
import statistics

from . import schema as S
from .calendar import WorkCalendar

# 与 foresee.py 的复盘口径对齐（同一把尺子）。取不到就显式降级并标明。
try:                                     # pragma: no cover - 依赖仓库根目录在 sys.path
    from foresee import VERDICT_REVIEW as _FORESEE_VERDICT_REVIEW
    VERDICT_SOURCE = "foresee.VERDICT_REVIEW"
except Exception:                        # pragma: no cover
    _FORESEE_VERDICT_REVIEW = {
        ("高风险", "晚了"): "预警正确", ("偏紧", "晚了"): "预警正确",
        ("高风险", "没晚"): "误报", ("偏紧", "没晚"): "误报",
        ("正常", "晚了"): "漏报", ("正常", "没晚"): "正确",
    }
    VERDICT_SOURCE = "fallback（未取到 foresee，口径为等价副本）"

VERDICT_REVIEW = dict(_FORESEE_VERDICT_REVIEW)

# 少于这个样本数就不给准确率结论
MIN_SAMPLE = 5
# 命中容差（天）：预测与实际相差在 ±N 天内算命中
HIT_TOLERANCE_DAYS = 1


# ────────────────────────── 时间工具 ──────────────────────────
def _parse_dt(value):
    if not value:
        return None
    if isinstance(value, _dt.datetime):
        return value
    if isinstance(value, _dt.date):
        return _dt.datetime.combine(value, _dt.time(0, 0))
    text = str(value).strip()
    try:
        return _dt.datetime.fromisoformat(text)
    except ValueError:
        try:
            return _dt.datetime.combine(_dt.date.fromisoformat(text[:10]), _dt.time(0, 0))
        except ValueError:
            return None


def _norm_date(value):
    dt = _parse_dt(value)
    return dt.date().isoformat() if dt else ""


# ────────────────────────── 防未来信息 ──────────────────────────
def filter_as_of(records: list[dict], as_of, time_key: str = "captured_at") -> list[dict]:
    """只保留 as_of（含）之前采集/发生的记录。**这是所有回测的唯一入口。**

    缺时间字段的记录会被**丢弃**，而不是被当成「反正应该没问题」放进来 ——
    无法证明它在预测之前就存在，就不能用它。
    """
    cutoff = _parse_dt(as_of)
    if cutoff is None:
        return []
    out = []
    for r in records or []:
        t = _parse_dt(r.get(time_key))
        if t is None:
            continue
        if t <= cutoff:
            out.append(r)
    return out


def audit_no_lookahead(record: dict, samples: list[dict]) -> dict:
    """审计一条预测记录：它引用的样本里有没有「预测时点之后才知道」的东西。"""
    predicted_at = _parse_dt(record.get("predicted_at"))
    violations = []
    if predicted_at is None:
        violations.append({"type": "missing_predicted_at",
                           "message": "记录缺 predicted_at，无法审计是否用了未来信息"})
        return {"ok": False, "violations": violations}
    used_ids = set(record.get("sample_ids") or [])
    pool = [s for s in (samples or []) if not used_ids or str(s.get("sample_id")) in used_ids]
    latest = None
    for s in pool:
        t = _parse_dt(s.get("captured_at"))
        if t is None:
            continue
        if latest is None or t > latest:
            latest = t
        if t > predicted_at:
            violations.append({
                "type": "future_information",
                "sample_id": s.get("sample_id"),
                "captured_at": t.isoformat(),
                "predicted_at": predicted_at.isoformat(),
                "message": "样本 %s 采集于 %s，晚于预测时点 %s —— 未来信息泄漏"
                           % (s.get("sample_id"), t.isoformat(), predicted_at.isoformat())})
    return {
        "ok": not violations,
        "predicted_at": predicted_at.isoformat(),
        "latest_sample_captured_at": latest.isoformat() if latest else None,
        "sample_count": len(pool),
        "violations": violations,
    }


# ────────────────────────── 基线：历史中位工期 ──────────────────────────
def history_median_duration(samples: list[dict], as_of,
                            value_key: str = "duration_hours") -> dict:
    """历史中位工期基线 —— 只用 as_of 之前采集的样本。

    这是「笨办法对照」：如果我们的排程还跑不赢「历史中位」，那它没有价值。
    """
    kept = filter_as_of(samples, as_of)
    vals = []
    for s in kept:
        v = s.get(value_key)
        try:
            vals.append(float(v))
        except (TypeError, ValueError):
            continue
    if not vals:
        return {"value_hours": None, "n_samples": 0, "as_of": _norm_date(as_of) or "",
                "status": S.ACCURACY_UNDEFINED,
                "message": "as_of 之前没有可用历史样本，不给中位工期"}
    return {
        "value_hours": round(statistics.median(vals), 2),
        "n_samples": len(vals),
        "as_of": _parse_dt(as_of).isoformat() if _parse_dt(as_of) else "",
        "status": "ok" if len(vals) >= MIN_SAMPLE else S.ACCURACY_UNDEFINED,
        "message": ("样本 %d 条" % len(vals)) if len(vals) >= MIN_SAMPLE
                   else "样本仅 %d 条（< %d），中位数仅供参考，不作为准确率对照"
                        % (len(vals), MIN_SAMPLE),
    }


# ────────────────────────── 复盘 ──────────────────────────
def review_prediction(record: dict, samples: list[dict] = None) -> dict:
    """复盘单条预测：预测 vs 实际，并做未来信息审计。"""
    predicted = _norm_date(record.get("predicted_date"))
    actual = _norm_date((record.get("actual") or {}).get("date"))
    audit = audit_no_lookahead(record, samples or [])
    if not predicted:
        return {"prediction_id": record.get("prediction_id"),
                "status": "undetermined",
                "message": "该次预测本身没有产出确定日期（工期未知或条件未确认），不予复盘",
                "audit": audit}
    if not actual:
        return {"prediction_id": record.get("prediction_id"),
                "status": "pending", "predicted_date": predicted,
                "message": S.GAP_CN[S.GAP_NO_ACTUAL], "audit": audit}
    err = (_dt.date.fromisoformat(actual) - _dt.date.fromisoformat(predicted)).days
    return {
        "prediction_id": record.get("prediction_id"),
        "plan_id": record.get("plan_id"),
        # 来源必须一路带着走：复盘报表按它分组，人的判断与算法的输出不混算
        "prediction_source": (record.get("prediction_source")
                              or S.PREDICTION_SOURCE_MODEL),
        "source_ref": record.get("source_ref") or "",
        "status": "reviewed",
        "predicted_at": record.get("predicted_at"),
        "predicted_date": predicted,
        "actual_date": actual,
        "error_days": err,              # 正 = 实际比预测更晚（预测偏乐观）
        "abs_error_days": abs(err),
        "hit": abs(err) <= HIT_TOLERANCE_DAYS,
        "rules_version": record.get("rules_version") or "",
        "method": record.get("method") or "",
        "sample_scope": record.get("sample_scope") or {},
        "assumptions": list(record.get("assumptions") or []),
        "input_snapshot_id": record.get("input_snapshot_id")
                             or record.get("snapshot_id") or "",
        "foresee_verdict": _foresee_conclusion(record, actual),
        "audit": audit,
    }


def _foresee_conclusion(record, actual_date):
    """按 foresee 的裁判表给出结论（预警正确 / 误报 / 漏报 / 正确）。

    只在记录带 verdict（如「高风险」「正常」）时给出。
    合同交期和目标交期是两件事：这里用记录里的 target_date 做裁判基准。
    """
    verdict = str(record.get("verdict") or "").strip()
    target = _norm_date(record.get("target_date"))
    if not verdict or not target or not actual_date:
        return None
    late = actual_date > target
    outcome = "晚了" if late else "没晚"
    if verdict == "已逾期":
        return {"verdict": verdict, "outcome": outcome,
                "conclusion": "已逾期（判定时已过交期）"}
    conclusion = VERDICT_REVIEW.get((verdict, outcome))
    if conclusion is None:
        return {"verdict": verdict, "outcome": outcome, "conclusion": "未定义"}
    return {"verdict": verdict, "outcome": outcome, "conclusion": conclusion,
            "late_days": (_dt.date.fromisoformat(actual_date)
                          - _dt.date.fromisoformat(target)).days}


def backtest(records: list[dict], samples: list[dict] = None,
             baseline_samples: list[dict] = None,
             as_of=None) -> dict:
    """对一批预测记录做复盘 + 基线对照。

    samples          —— 历史样本（带 captured_at，用于未来信息审计）
    baseline_samples —— 用于「历史中位工期」对照的样本
    as_of            —— 本次复盘的截止时点；不传则用当前时间
    """
    as_of = as_of or _dt.datetime.now()
    samples = samples or []
    baseline_samples = baseline_samples if baseline_samples is not None else samples

    reviews = [review_prediction(r, samples) for r in (records or [])]
    done = [r for r in reviews if r["status"] == "reviewed"]
    pending = [r for r in reviews if r["status"] == "pending"]
    undet = [r for r in reviews if r["status"] == "undetermined"]

    errs = [r["error_days"] for r in done]
    abs_errs = [r["abs_error_days"] for r in done]
    n = len(done)
    enough = n >= MIN_SAMPLE

    by_method: dict[str, dict] = {}
    for r in done:
        m = r["method"] or "unknown"
        b = by_method.setdefault(m, {"n": 0, "errors": [], "hits": 0})
        b["n"] += 1
        b["errors"].append(r["error_days"])
        b["hits"] += 1 if r["hit"] else 0

    baseline = history_median_duration(baseline_samples, as_of)
    foresee_stats: dict[str, int] = {}
    for r in done:
        f = r.get("foresee_verdict")
        if f:
            foresee_stats[f["conclusion"]] = foresee_stats.get(f["conclusion"], 0) + 1

    violations = [v for r in reviews for v in (r.get("audit") or {}).get("violations", [])]

    summary = {
        "n_records": len(records or []),
        "n_reviewed": n,
        "n_pending": len(pending),
        "n_undetermined": len(undet),
        "accuracy_status": "ok" if enough else S.ACCURACY_UNDEFINED,
        "min_sample_required": MIN_SAMPLE,
        "message": ("样本 %d 条，可给准确率" % n) if enough else
                   ("可复盘的预测仅 %d 条（< %d），**不给准确率数字**——"
                    "小样本百分比看起来像结论，实际是噪声" % (n, MIN_SAMPLE)),
    }
    if enough:
        summary.update({
            "mae_days": round(statistics.mean(abs_errs), 2),
            "bias_days": round(statistics.mean(errs), 2),   # 正 = 系统性偏乐观
            "hit_rate": round(sum(1 for r in done if r["hit"]) / n, 3),
            "hit_tolerance_days": HIT_TOLERANCE_DAYS,
            "max_error_days": max(abs_errs),
            "by_method": {m: {"n": b["n"],
                              "mae_days": round(statistics.mean([abs(e) for e in b["errors"]]), 2),
                              "bias_days": round(statistics.mean(b["errors"]), 2),
                              "hit_rate": round(b["hits"] / b["n"], 3)}
                          for m, b in by_method.items()},
        })
    else:
        summary["by_method"] = {m: {"n": b["n"]} for m, b in by_method.items()}

    return {
        "contract_version": S.DS_CONTRACT_VERSION,
        "rules_version": S.RULES_VERSION,
        "no_lookahead_guarantee": S.BACKTEST_NO_LOOKAHEAD,
        "no_lookahead_violations": violations,
        "no_lookahead_ok": not violations,
        "verdict_source": VERDICT_SOURCE,
        "as_of": _parse_dt(as_of).isoformat(),
        "baseline": baseline,
        "foresee_alignment": foresee_stats,
        # 按来源分开计数：人的预测与算法的预测**各自**统计。
        # 看这一块就能知道「这次的样本里有多少是算法自己的」——
        # 若 model 为 0，这份报告说明的只是人的判断准不准，不是算法的。
        "by_source": _by_source(records or [], reviews),
        "summary": summary,
        "reviews": reviews,
    }


def _by_source(records, reviews) -> dict:
    out = {s: {"n_records": 0, "n_reviewed": 0, "n_pending": 0, "n_undetermined": 0}
           for s in S.PREDICTION_SOURCES}
    for r in records:
        src = str(r.get("prediction_source") or S.PREDICTION_SOURCE_MODEL)
        src = src if src in S.PREDICTION_SOURCES else S.PREDICTION_SOURCE_MODEL
        out[src]["n_records"] += 1
    for rev in reviews:
        src = str(rev.get("prediction_source") or S.PREDICTION_SOURCE_MODEL)
        src = src if src in S.PREDICTION_SOURCES else S.PREDICTION_SOURCE_MODEL
        status = rev.get("status")
        if status == "reviewed":
            out[src]["n_reviewed"] += 1
        elif status == "pending":
            out[src]["n_pending"] += 1
        else:
            out[src]["n_undetermined"] += 1
    out["note"] = ("human = 人的判断（二期 predictions[]）；model = 算法产出。"
                   "两者分开统计，不合并成一个准确率。")
    return out


# ────────────────────────── 生成预测记录 ──────────────────────────
def make_record_from_schedule(schedule: dict, plan_id: str, *,
                              predicted_at, snapshot_id: str = "",
                              method: str = "", verdict: str = "",
                              target_date: str = "", assumptions=(),
                              sample_scope=None, sample_ids=(),
                              prediction_source: str = "model",
                              source_ref: str = "") -> dict:
    """从一次排程结果里抽出某个计划的预测，落成一条可复盘的预测记录。

    关键：`predicted_at` 必须显式给出。**没有预测时点，就没有复盘可言** ——
    事后补的时点等于事后编的准确率。

    `prediction_source` 区分这条预测是**算法算的**（model，默认）还是
    **人说的**（human，来自二期 predictions[]）。两者都保留、分开统计 ——
    合并成一个「准确率」是没有意义的数字。
    """
    plan = next((p for p in (schedule.get("plans") or [])
                 if p.get("plan_id") == plan_id), None)
    if plan is None:
        return {"prediction_id": "", "plan_id": plan_id,
                "status": "undetermined", "message": "排程里没有该计划"}
    fd = plan.get("forecast_date")
    return {
        "prediction_id": "PDC-%s-%s" % (_parse_dt(predicted_at).strftime("%Y%m%d"), plan_id),
        "plan_id": plan_id,
        "prediction_source": (prediction_source
                              if prediction_source in S.PREDICTION_SOURCES
                              else S.PREDICTION_SOURCE_MODEL),
        "source_ref": source_ref,
        "predicted_at": _parse_dt(predicted_at).isoformat(),
        "predicted_date": fd,
        "predicted_status": plan.get("forecast_status"),
        "rules_version": schedule.get("rules_version") or S.RULES_VERSION,
        "method": method or schedule.get("method") or "",
        "input_snapshot_id": snapshot_id or schedule.get("snapshot_id") or "",
        "assumptions": list(assumptions),
        "sample_scope": dict(sample_scope or {}),
        "sample_ids": list(sample_ids),
        "verdict": verdict,
        "target_date": _norm_date(target_date),
        "contract_date": plan.get("contract_date") or "",
        "actual": {},
        "undetermined_reasons": plan.get("undetermined_reasons") or [],
    }


def naive_baseline_finish(start_date, history_median_hours: float,
                          calendar: WorkCalendar):
    """笨办法对照：拿历史中位工期直接往后推。给「我们的排程是否值得」当标尺。"""
    if history_median_hours is None:
        return None
    d = _parse_dt(start_date)
    if d is None:
        return None
    fin = calendar.add_working_hours(calendar.align_forward(d), history_median_hours)
    return fin.date().isoformat()


__all__ = ["backtest", "review_prediction", "audit_no_lookahead", "filter_as_of",
           "history_median_duration", "make_record_from_schedule",
           "naive_baseline_finish", "VERDICT_REVIEW", "VERDICT_SOURCE",
           "MIN_SAMPLE", "HIT_TOLERANCE_DAYS"]
