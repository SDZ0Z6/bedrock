"""日期区间解析。

刻意不依赖 Flask 的 request：resolve_range 收一个普通的映射（视图里传
request.args），这样区间逻辑可以脱离请求上下文单独测。
"""

from __future__ import annotations

import calendar
from collections.abc import Mapping
from datetime import date, datetime, timedelta

# Cost Explorer 默认能查的月份数，**含当月**。所以最早可查的是当月往前数
# CE_HISTORY_MONTHS - 1 个月的 1 号，见 earliest_queryable。
#
# 这个「含当月」是拿真实账号量出来的，不是读文档猜的：2026-09-13 这天，
# 起点填 2025-08-01 能查（当月 + 前 13 个月 = 14 个月），填 2025-07-01、
# 07-13、07-14 全部报 ValidationException: You haven't enabled historical
# data beyond 14 months。早先写成 month_shift(today, -14) 实际要了 15 个月，
# 一律被拒。
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


def earliest_queryable(today: date) -> date:
    """Cost Explorer 还能查到的最早一天。

    当月也算在 CE_HISTORY_MONTHS 里，所以是往前数 CE_HISTORY_MONTHS - 1 个月。
    """
    return month_shift(today, -(CE_HISTORY_MONTHS - 1))


# cumulative_range 的第三个返回值，说明这个区间是不是打过折扣
CUMULATIVE_OK = ""            # 按台账里的启用日期，完整
CUMULATIVE_MISSING = "missing"  # 台账没填启用日期，按 CE 最早可查日兜底
CUMULATIVE_CLAMPED = "clamped"  # 启用日期早于 CE 保留期，前面那段查不到
CUMULATIVE_FUTURE = "future"    # 启用日期在今天之后，区间退化成今天一天


def cumulative_range(start_date: date | None, today: date) -> tuple[date, date, str]:
    """一个账号的累计统计区间：从启用日期到今天。

    概览页不再让用户选区间——消费和余额都是「从这个账号启用那天算到现在」，
    对着额度看才有意义。所以区间是每个账号各算各的，由这里统一决定。

    第三个返回值说明区间是否打了折扣，页面据此给对应的行加标记：光看数字
    分不出「这就是全部消费」和「更早的那段 CE 已经查不到了」。
    """
    earliest = earliest_queryable(today)
    if start_date is None:
        return earliest, today, CUMULATIVE_MISSING
    if start_date > today:
        return today, today, CUMULATIVE_FUTURE
    if start_date < earliest:
        return earliest, today, CUMULATIVE_CLAMPED
    return start_date, today, CUMULATIVE_OK


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

    earliest = earliest_queryable(today)
    if end < earliest:
        # 整段都在保留期之前：只抬开始日期的话区间就倒过来了，改成从最早可查日到今天
        notes.append(
            f"所选区间早于 Cost Explorer 的保留期（约 {CE_HISTORY_MONTHS} 个月），"
            f"已改成从 {earliest.isoformat()} 到今天。"
        )
        return earliest, today, notes
    if start < earliest:
        start = earliest
        notes.append(
            f"Cost Explorer 仅保留约 {CE_HISTORY_MONTHS} 个月历史数据，"
            f"开始日期已调整为 {earliest.isoformat()}。"
        )
    return start, end, notes
