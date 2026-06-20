#!/usr/bin/env python3
"""统一报告标题生成。"""

from __future__ import annotations

from datetime import datetime


def _parse_date(date: str | datetime) -> datetime:
    if isinstance(date, datetime):
        return date
    return datetime.strptime(date, "%Y-%m-%d")


def _month_week_number(date: str | datetime) -> int:
    dt = _parse_date(date)
    month_start = dt.replace(day=1)
    return ((dt.day + month_start.weekday() - 1) // 7) + 1


def build_daily_title(date: str | datetime) -> str:
    dt = _parse_date(date)
    return f"{dt.year}年{dt.month}月{dt.day}日 日报"


def build_weekly_title(date: str | datetime) -> str:
    dt = _parse_date(date)
    week_num = _month_week_number(dt)
    return f"{dt.year}年{dt.month}月第{week_num}周 周报"


def build_monthly_title(date: str | datetime) -> str:
    dt = _parse_date(date)
    return f"{dt.year}年{dt.month}月 月报"


def build_report_title(report_type: str, date: str | datetime) -> str:
    if report_type == "weekly":
        return build_weekly_title(date)
    if report_type == "monthly":
        return build_monthly_title(date)
    return build_daily_title(date)
