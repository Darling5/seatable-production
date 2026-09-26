# -*- coding: utf-8 -*-
"""application/project_brain/timeparse.py — 中文相对日期的**消息时间**解析。

━━ 这条规则为什么必须存在 ━━
微信里说的「下周一给你」是相对**那条消息发出的时候**说的，不是相对我们
今天跑采集脚本的时候。如果按采集时间去解析，一条 8 月 3 日的「明天到货」
会被解析成 9 月 27 日 —— 期限整体漂移，且漂移量随采集延迟变化，
这种错误在账面上完全看不出来。

所以本模块的入口一律要求传 ``base_dt``（消息时间），函数内部**不调用
``datetime.now()``**。取不到消息时间时由调用方显式决定降级策略（本模块
不偷偷用当前时间兜底）。

支持：今天/明天/后天/大后天/昨天/前天、N天后/前、N个工作日后、
本周X/下周X/这周X、下周、本周内、本月内、月底(前)、下月初、
X月Y日/号、YYYY-MM-DD / YYYY/MM/DD。
"""
from __future__ import annotations

import datetime as _dt
import re
from typing import Optional

CN_DIGITS = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
             "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}

WEEKDAY_CN = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5,
              "日": 6, "天": 6, "七": 6}

# 相对日：词语 → 相对于消息日的偏移天数
REL_DAY = {
    "大后天": 3, "后天": 2, "明天": 1, "明日": 1, "今天": 0, "今日": 0,
    "昨天": -1, "昨日": -1, "前天": -2, "大前天": -3,
}


def _cn_num(text: str) -> Optional[int]:
    """解析阿拉伯数字或 1~99 的简单中文数字。"""
    text = text.strip()
    if not text:
        return None
    if text.isdigit():
        return int(text)
    if text == "十":
        return 10
    if "十" in text:
        head, _, tail = text.partition("十")
        tens = CN_DIGITS.get(head, 1) if head else 1
        ones = CN_DIGITS.get(tail, 0) if tail else 0
        return tens * 10 + ones
    if len(text) == 1 and text in CN_DIGITS:
        return CN_DIGITS[text]
    return None


def _month_end(d: _dt.date) -> _dt.date:
    nxt = d.replace(day=1) + _dt.timedelta(days=32)
    return nxt.replace(day=1) - _dt.timedelta(days=1)


def _add_workdays(d: _dt.date, n: int) -> _dt.date:
    """按工作日推进（跳过周六周日）；n 可为负。"""
    step = 1 if n >= 0 else -1
    left = abs(int(n))
    cur = d
    while left > 0:
        cur += _dt.timedelta(days=step)
        if cur.weekday() < 5:
            left -= 1
    return cur


def _as_date(base) -> _dt.date:
    if isinstance(base, _dt.datetime):
        return base.date()
    if isinstance(base, _dt.date):
        return base
    if isinstance(base, str):
        dt = _parse_iso(base)
        if dt is not None:
            return dt.date()
    raise TypeError("base_dt 必须是 datetime/date/ISO 字符串，实际：%r" % (base,))


def _parse_iso(text: str) -> Optional[_dt.datetime]:
    text = str(text).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    for cand in (text, text.replace(" ", "T"), text.replace("/", "-")):
        try:
            return _dt.datetime.fromisoformat(cand)
        except ValueError:
            continue
    return None


def _week_start_monday(d: _dt.date) -> _dt.date:
    return d - _dt.timedelta(days=d.weekday())


def resolve_date(text: str, base_dt, *, default_year_forward: bool = True
                 ) -> Optional[str]:
    """把一句中文/数字日期解析成 ``YYYY-MM-DD``。

    ``base_dt`` 是**消息时间**（发生时间），不是采集时间。
    解析不出来返回 ``None``，绝不猜测。
    """
    if not text:
        return None
    s = str(text).strip()
    base = _as_date(base_dt)

    # 0) 绝对 ISO 日期优先（2026-09-30 / 2026/09/30）
    m = re.search(r"(20\d{2})[-/年](\d{1,2})[-/月](\d{1,2})", s)
    if m:
        try:
            return _dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3))).isoformat()
        except ValueError:
            return None

    # 1) X月Y日 / X月Y号（年份按消息日推断）
    m = re.search(r"(\d{1,2})\s*月\s*(\d{1,2})\s*[日号]", s)
    if m:
        mon, day = int(m.group(1)), int(m.group(2))
        try:
            cand = _dt.date(base.year, mon, day)
        except ValueError:
            return None
        if default_year_forward and cand < base:
            cand = _dt.date(base.year + 1, mon, day)
        return cand.isoformat()

    # 2) 本周/下周/这周 + X（星期）
    m = re.search(r"(本周|这周|下周|下星期|本星期|周|星期)\s*([一二三四五六日天七])", s)
    if m:
        marker, wd_cn = m.group(1), m.group(2)
        wd = WEEKDAY_CN[wd_cn]
        start = _week_start_monday(base)
        if marker == "下周":
            start += _dt.timedelta(days=7)
        return (start + _dt.timedelta(days=wd)).isoformat()

    # 3) 明天/后天/…（长的先匹配，避免「大后天」被「后天」截断）
    for word in sorted(REL_DAY, key=len, reverse=True):
        if word in s:
            return (base + _dt.timedelta(days=REL_DAY[word])).isoformat()

    # 4) N 个工作日（后/内）
    m = re.search(r"([0-9一二两三四五六七八九十]+)\s*个?\s*工作日\s*(后|以后|内|之内)", s)
    if m:
        n = _cn_num(m.group(1))
        if n is None:
            return None
        return _add_workdays(base, n).isoformat()

    # 5) N 天（后/以后/内/之内/前/以前）
    m = re.search(r"([0-9一二两三四五六七八九十]+)\s*天\s*(后|以后|内|之内|前|以前|之前)", s)
    if m:
        n = _cn_num(m.group(1))
        if n is None:
            return None
        if m.group(2) in ("前", "以前", "之前"):
            n = -n
        return (base + _dt.timedelta(days=n)).isoformat()

    # 6) 周（下周一整周 / 一周后）
    m = re.search(r"([0-9一二两三四五六七八九十]+)\s*(周|星期|礼拜)\s*(后|以后|内|之内)", s)
    if m:
        n = _cn_num(m.group(1))
        if n is None:
            return None
        if m.group(3) in ("内", "之内"):
            start = _week_start_monday(base) + _dt.timedelta(days=7 * n)
            return (start + _dt.timedelta(days=6)).isoformat()
        return (base + _dt.timedelta(days=7 * n)).isoformat()

    # 7) 下个月 / 下月 / 下月初
    if re.search(r"下个?月\s*初", s):
        nxt = (base.replace(day=1) + _dt.timedelta(days=32)).replace(day=1)
        return nxt.isoformat()
    if re.search(r"下个?月", s):
        nxt = (base.replace(day=1) + _dt.timedelta(days=32)).replace(day=1)
        return _month_end(nxt).isoformat()

    # 8) 月底 / 本月底 / 月末（含「月底前」）
    if re.search(r"(本月|这个月|这个月|当月)?\s*月底", s) or "月末" in s:
        return _month_end(base).isoformat()

    # 9) 本月内 / 这个月内
    if re.search(r"(本月|这个月)\s*内", s):
        return _month_end(base).isoformat()

    # 10) 本周内 / 这周内
    if re.search(r"(本周|这周)\s*内", s):
        return (_week_start_monday(base) + _dt.timedelta(days=6)).isoformat()

    # 11) 周末
    if "周末" in s:
        return (_week_start_monday(base) + _dt.timedelta(days=5)).isoformat()

    return None


def parse_message_time(value) -> Optional[_dt.datetime]:
    """解析来源消息时间。支持 ISO / epoch 秒 / epoch 毫秒 / ``YYYY-MM-DD HH:MM``。"""
    if value in (None, ""):
        return None
    if isinstance(value, _dt.datetime):
        return value
    if isinstance(value, _dt.date):
        return _dt.datetime.combine(value, _dt.time())
    if isinstance(value, (int, float)):
        ts = float(value)
        if ts > 1e11:      # 毫秒
            ts /= 1000.0
        try:
            return _dt.datetime.fromtimestamp(ts)
        except (ValueError, OverflowError, OSError):
            return None
    return _parse_iso(value)


def age_hours(captured_at, now=None) -> Optional[float]:
    """采集时间距今小时数（来源时效判定用）。"""
    dt = parse_message_time(captured_at)
    if dt is None:
        return None
    now = parse_message_time(now) or _dt.datetime.now()
    return round((now - dt).total_seconds() / 3600.0, 2)


__all__ = ["resolve_date", "parse_message_time", "age_hours", "REL_DAY",
           "WEEKDAY_CN"]
