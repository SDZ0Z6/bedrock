"""日期区间解析。

刻意不依赖 Flask 的 request：resolve_range 收一个普通的映射（视图里传
request.args），这样区间逻辑可以脱离请求上下文单独测。
"""

from __future__ import annotations

import calendar
from collections.abc import Mapping
from datetime import date, datetime, timedelta

# Cost Explorer 大约保留 14 个月历史数据
CE_HISTORY_MONTHS = 14

# 快捷区间，顺序与页面上的排列一致
PRESET_KEYS = ("mtd", "last_month", "last7", "last30", "ytd")
PRESET_LABELS = {
    "mtd": "本月",
    "last_month": "上月",
    "last7": "近 7 天",
    "last30": "近 30 天",
    "ytd": "今年",
}


def parse_date(raw: str | None) -> date | None:
    if not raw:
        return None
    try:
        return datetime.strptime(raw.strip(), "%Y-%m-%d").date()
    except ValueError:
        return None


def month_shift(anchor: date, months: int) -> date:
    """按月偏移，落到该月 1 号。"""
    total = anchor.year * 12 + (anchor.month - 1) + months
    return date(total // 12, total % 12 + 1, 1)


def preset_range(name: str, today: date) -> tuple[date, date] | None:
    if name == "mtd":
        return today.replace(day=1), today
    if name == "last_month":
        first = month_shift(today, -1)
        last_day = calendar.monthrange(first.year, first.month)[1]
        return first, first.replace(day=last_day)
    if name == "last7":
        return today - timedelta(days=6), today
    if name == "last30":
        return today - timedelta(days=29), today
    if name == "ytd":
        return date(today.year, 1, 1), today
    return None


def detect_preset(start: date, end: date, today: date) -> str:
    """反查当前区间对应哪个快捷项。

    下钻页是「参数改动即提交」，表单只带 start/end 不带 preset，所以不能靠
    查询参数判断高亮，得拿解析出来的区间去比。
    """
    for key in PRESET_KEYS:
        if preset_range(key, today) == (start, end):
            return key
    return ""


def resolve_range(args: Mapping[str, str], today: date) -> tuple[date, date, list[str]]:
    """从查询参数解析日期区间，返回 (开始, 结束, 给用户看的提示)。"""
    notes: list[str] = []
    preset = (args.get("preset") or "").strip()
    chosen = preset_range(preset, today) if preset else None

    if chosen:
        start, end = chosen
    else:
        start = parse_date(args.get("start"))
        end = parse_date(args.get("end"))
        if args.get("start") and start is None:
            notes.append("开始日期格式无法识别，已使用本月 1 号。")
        if args.get("end") and end is None:
            notes.append("结束日期格式无法识别，已使用今天。")
        start = start or today.replace(day=1)
        end = end or today

    if start > end:
        start, end = end, start
        notes.append("开始日期晚于结束日期，已自动调换。")
    if end > today:
        end = today
        notes.append("结束日期不能晚于今天，已调整为今天。")

    earliest = month_shift(today, -CE_HISTORY_MONTHS)
    if start < earliest:
        start = earliest
        notes.append(
            f"Cost Explorer 仅保留约 {CE_HISTORY_MONTHS} 个月历史数据，"
            f"开始日期已调整为 {earliest.isoformat()}。"
        )
    return start, end, notes
