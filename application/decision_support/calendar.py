# -*- coding: utf-8 -*-
"""application/decision_support/calendar.py — 工作日历。

为什么要有它：说「3 天」的时候，到底含不含周末？跨国庆算几天？
不算清楚，A 说周二、B 说下周三，谁都对，账对不上。
所以工期一律用**工作小时**表示，落到哪一天由日历裁决。

本期默认日历（首个验收样例明确规定）：
  周一至周五 09:00—17:00，每天 8 工作小时，**无额外节假日**。
但节假日必须可配 —— 现实中十月一日是放假的，只是样例特意关掉了它，
用来验证「日期边界」确实由日历驱动，而不是被硬编码的周末逻辑蒙对。

纯标准库、无副作用、可单测。
"""
from __future__ import annotations

import datetime as _dt

from . import schema as S

_MIN_PER_DAY = int(S.HOURS_PER_DAY * 60)


class CalendarError(ValueError):
    """日历定义本身有问题（例如工作时段起点晚于终点）。"""


class WorkCalendar:
    """工作日历。工作日 = 周一至周五，减去 holidays，加上 extra_workdays（调休）。

    start_hour / end_hour 用小数小时表示（9.5 = 09:30）。
    """

    def __init__(self, workdays=(0, 1, 2, 3, 4), start_hour: float = 9.0,
                 end_hour: float = 17.0, holidays=(), extra_workdays=(),
                 name: str = "默认日历（周一至周五 09:00—17:00，无节假日）"):
        self.workdays = tuple(sorted({int(d) % 7 for d in workdays}))
        self.start_hour = float(start_hour)
        self.end_hour = float(end_hour)
        self.holidays = {_as_date(d) for d in holidays if d}
        self.extra_workdays = {_as_date(d) for d in extra_workdays if d}
        self.name = name
        if self.end_hour <= self.start_hour:
            raise CalendarError("工作时段终点 %s 不晚于起点 %s"
                                % (self.end_hour, self.start_hour))

    # ── 基本量 ──────────────────────────────────────
    @property
    def minutes_per_day(self) -> int:
        return int(round((self.end_hour - self.start_hour) * 60))

    def is_workday(self, d) -> bool:
        d = _as_date(d)
        if d in self.extra_workdays:
            return True
        if d in self.holidays:
            return False
        return d.weekday() in self.workdays

    def day_start(self, d) -> _dt.datetime:
        return _dt.datetime.combine(_as_date(d),
                                    _dt.time(int(self.start_hour),
                                             int(round((self.start_hour % 1) * 60))))

    def day_end(self, d) -> _dt.datetime:
        return _dt.datetime.combine(_as_date(d),
                                    _dt.time(int(self.end_hour),
                                             int(round((self.end_hour % 1) * 60))))

    def add_days(self, d, n: int):
        """自然日推进（保留原语义：不是工作日推进）。"""
        return _as_date(d) + _dt.timedelta(days=int(n))

    def next_workday(self, d):
        """返回 d（含）之后第一个工作日。"""
        cur = _as_date(d)
        for _ in range(4000):
            if self.is_workday(cur):
                return cur
            cur += _dt.timedelta(days=1)
        raise CalendarError("4000 天内找不到工作日，日历定义可能有问题")

    def workdays_between(self, a, b) -> int:
        """[a, b] 闭区间内的工作日天数（a > b 返回 0）。"""
        a, b = _as_date(a), _as_date(b)
        if b < a:
            return 0
        n = 0
        cur = a
        while cur <= b:
            if self.is_workday(cur):
                n += 1
            cur += _dt.timedelta(days=1)
        return n

    # ── 时间对齐 ──────────────────────────────────────
    def align_forward(self, dt) -> _dt.datetime:
        """把时刻向前对齐到「可工作」状态。

        · 工作日 09:00 <= t < 17:00 → 原样返回
        · 工作日 00:00 <= t < 09:00 → 当天 09:00
        · 其余（下班后 / 非工作日）→ 下一个工作日 09:00
        """
        dt = _as_dt(dt)
        d = dt.date()
        if self.is_workday(d):
            if self.day_start(d) <= dt < self.day_end(d):
                return dt
            if dt < self.day_start(d):
                return self.day_start(d)
        nd = self.next_workday(d + _dt.timedelta(days=1))
        return self.day_start(nd)

    def align_backward(self, dt) -> _dt.datetime:
        """把时刻向后对齐到「可工作」状态的右端点（用于反推最晚开始/结束）。"""
        dt = _as_dt(dt)
        d = dt.date()
        if self.is_workday(d):
            if self.day_start(d) < dt <= self.day_end(d):
                return dt
            if dt > self.day_end(d):
                return self.day_end(d)
        # 早于当天上班时间 → 退到上一个工作日的下班时刻
        pd = d - _dt.timedelta(days=1)
        for _ in range(4000):
            if self.is_workday(pd):
                return self.day_end(pd)
            pd -= _dt.timedelta(days=1)
        raise CalendarError("4000 天内找不到工作日，日历定义可能有问题")

    # ── 工期推进（核心）──────────────────────────────
    def add_working_hours(self, start, hours: float) -> _dt.datetime:
        """从 start 起推进 hours 工作小时，返回完成时刻。

        规则：当天已下班/非工作日 → 从下一个工作日 09:00 起算。
        hours=0 → 返回 forward 对齐后的时刻（零工期工序即「与前置同时完成」）。
        """
        cur = self.align_forward(start)
        remain = int(round(float(hours) * 60))
        if remain <= 0:
            return cur
        guard = 0
        while remain > 0:
            guard += 1
            if guard > 20000:
                raise CalendarError("推进工作日超过 20000 次，检查工期或日历")
            d = cur.date()
            avail = int((self.day_end(d) - cur).total_seconds() // 60)
            if avail <= 0:
                cur = self.day_start(self.next_workday(d + _dt.timedelta(days=1)))
                continue
            if remain <= avail:
                return cur + _dt.timedelta(minutes=remain)
            remain -= avail
            cur = self.day_start(self.next_workday(d + _dt.timedelta(days=1)))
        return cur

    def sub_working_hours(self, end, hours: float) -> _dt.datetime:
        """从 end 起倒推 hours 工作小时，返回最晚开始时刻（关键路径反推用）。"""
        cur = self.align_backward(end)
        remain = int(round(float(hours) * 60))
        if remain <= 0:
            return cur
        guard = 0
        while remain > 0:
            guard += 1
            if guard > 20000:
                raise CalendarError("倒推工作日超过 20000 次，检查工期或日历")
            d = cur.date()
            if not self.is_workday(d):
                cur = self.align_backward(d)
                d = cur.date()
            avail = int((cur - self.day_start(d)).total_seconds() // 60)
            if avail <= 0:
                cur = self.day_end(_prev_workday(self, d))
                continue
            if remain <= avail:
                return cur - _dt.timedelta(minutes=remain)
            remain -= avail
            cur = self.day_end(_prev_workday(self, d))
        return cur

    # ── 输出辅助 ──────────────────────────────────────
    def finish_date(self, finish_dt):
        """完成时刻 → 完成日（17:00 整点算当天完成，不推到次日）。"""
        return _as_dt(finish_dt).date()

    def describe(self) -> dict:
        return {
            "name": self.name,
            "workdays": list(self.workdays),
            "start_hour": self.start_hour,
            "end_hour": self.end_hour,
            "hours_per_day": round(self.minutes_per_day / 60, 2),
            "holidays": sorted(d.isoformat() for d in self.holidays),
            "extra_workdays": sorted(d.isoformat() for d in self.extra_workdays),
            "note": "工作日 = 周一至周五（减去节假日、加上调休）",
        }


def _prev_workday(cal: WorkCalendar, d):
    cur = _as_date(d) - _dt.timedelta(days=1)
    for _ in range(4000):
        if cal.is_workday(cur):
            return cur
        cur -= _dt.timedelta(days=1)
    raise CalendarError("4000 天内找不到工作日，日历定义可能有问题")


def _as_date(value):
    if isinstance(value, _dt.datetime):
        return value.date()
    if isinstance(value, _dt.date):
        return value
    return _dt.date.fromisoformat(str(value).strip()[:10])


def _as_dt(value):
    if isinstance(value, _dt.datetime):
        return value
    if isinstance(value, _dt.date):
        return _dt.datetime.combine(value, _dt.time(0, 0))
    text = str(value).strip()
    try:
        return _dt.datetime.fromisoformat(text)
    except ValueError:
        return _dt.datetime.combine(_dt.date.fromisoformat(text[:10]), _dt.time(0, 0))


def default_calendar(**kw) -> WorkCalendar:
    """首个验收样例指定的日历：周一至周五 09:00—17:00，无额外节假日。"""
    return WorkCalendar(**kw)


def from_dict(data) -> WorkCalendar:
    """从快照里的日历定义构造。缺字段就用样例默认值。"""
    data = dict(data or {})
    return WorkCalendar(
        workdays=data.get("workdays") or (0, 1, 2, 3, 4),
        start_hour=data.get("start_hour", 9.0),
        end_hour=data.get("end_hour", 17.0),
        holidays=data.get("holidays") or (),
        extra_workdays=data.get("extra_workdays") or (),
        name=data.get("name") or "快照日历",
    )


__all__ = ["WorkCalendar", "CalendarError", "default_calendar", "from_dict"]
