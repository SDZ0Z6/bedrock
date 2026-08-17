"""CloudWatch 的时间窗口与粒度。

和 dates.py 分开：Cost Explorer 按自然日取整，CloudWatch 要的是精确到分钟的
UTC 时间戳加一个 Period，两者的约束完全不同。

CloudWatch 对不同 Period 有不同的保留期，超期的数据直接查不到（不是返回 0，
是根本没有），所以这里会自动往粗调并告诉用户调了什么。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

# 粒度 -> (Period 秒数, 显示名, CloudWatch 对该 Period 的保留天数)
PERIODS: dict[str, tuple[int, str, int]] = {
    "1m": (60, "1 分钟", 15),
    "5m": (300, "5 分钟", 63),
    "1h": (3600, "1 小时", 455),
    "1d": (86400, "1 天", 455),
}
PERIOD_ORDER = ["1m", "5m", "1h", "1d"]
DEFAULT_PERIOD = "1h"

# 相对时间窗口。CloudWatch 看的是「最近多久」，比自然日区间更贴合。
WINDOWS: dict[str, tuple[str, timedelta]] = {
    "1h": ("近 1 小时", timedelta(hours=1)),
    "6h": ("近 6 小时", timedelta(hours=6)),
    "24h": ("近 24 小时", timedelta(hours=24)),
    "7d": ("近 7 天", timedelta(days=7)),
    "30d": ("近 30 天", timedelta(days=30)),
}
# 默认 7 天而不是 24 小时：这类 MAP 分发流量是突发的，中间可能空置一两天，
# 默认 24 小时经常一开页就是空图，看着像坏了。
DEFAULT_WINDOW = "7d"

# 每条序列最多取多少个点。折线图上再多也看不出东西，
# 而且 GetMetricData 单次请求有 100,800 个数据点的硬上限。
MAX_POINTS = 1500


@dataclass
class MetricWindow:
    """一次查询的时间窗口，start/end 都是带 tzinfo 的 UTC 时间。"""

    start: datetime
    end: datetime
    period_key: str
    window_key: str = ""

    @property
    def period(self) -> int:
        return PERIODS[self.period_key][0]

    @property
    def period_label(self) -> str:
        return PERIODS[self.period_key][1]

    @property
    def span(self) -> timedelta:
        return self.end - self.start

    @property
    def point_count(self) -> int:
        return max(1, int(self.span.total_seconds() // self.period))


def _parse_local(raw: str | None) -> datetime | None:
    """解析 <input type="datetime-local"> 的值，按本机时区理解，转成 UTC。"""
    if not raw:
        return None
    text = raw.strip().replace(" ", "T")
    for fmt in ("%Y-%m-%dT%H:%M", "%Y-%m-%dT%H:%M:%S"):
        try:
            naive = datetime.strptime(text, fmt)
        except ValueError:
            continue
        return naive.astimezone(timezone.utc) if naive.tzinfo else naive.replace(
            tzinfo=datetime.now().astimezone().tzinfo
        ).astimezone(timezone.utc)
    return None


def _floor(moment: datetime, period: int) -> datetime:
    """把时间对齐到 Period 边界，否则首尾会出现半个桶。"""
    epoch = int(moment.timestamp())
    return datetime.fromtimestamp(epoch - epoch % period, tz=timezone.utc)


def coarser(period_key: str) -> str | None:
    index = PERIOD_ORDER.index(period_key)
    return PERIOD_ORDER[index + 1] if index + 1 < len(PERIOD_ORDER) else None


def fit_period(start: datetime, end: datetime, period_key: str) -> tuple[str, list[str]]:
    """把粒度调到既在保留期内、点数又不爆的档位。返回 (最终粒度, 提示)。"""
    notes: list[str] = []
    if period_key not in PERIODS:
        period_key = DEFAULT_PERIOD

    now = datetime.now(timezone.utc)
    while True:
        seconds, label, retention_days = PERIODS[period_key]
        age_days = (now - start).total_seconds() / 86400
        too_old = age_days > retention_days
        too_many = (end - start).total_seconds() / seconds > MAX_POINTS
        if not (too_old or too_many):
            return period_key, notes

        nxt = coarser(period_key)
        if nxt is None:
            if too_old:
                notes.append(
                    f"CloudWatch 对 {label} 粒度只保留 {retention_days} 天，"
                    f"更早的数据查不到。"
                )
            return period_key, notes

        reason = (
            f"CloudWatch 的 {label} 数据只保留 {retention_days} 天"
            if too_old
            else f"{label} 粒度下这个区间会有 "
            f"{int((end - start).total_seconds() / seconds):,} 个点，太密"
        )
        notes.append(f"{reason}，粒度已自动调整为 {PERIODS[nxt][1]}。")
        period_key = nxt


def resolve_window(args: Mapping[str, str], now: datetime | None = None) -> tuple[MetricWindow, list[str]]:
    """从查询参数解析时间窗口。

    优先用相对窗口（win=24h）；也支持 start/end 两个 datetime-local 的绝对区间。
    """
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    notes: list[str] = []

    window_key = (args.get("win") or "").strip()
    start = _parse_local(args.get("start"))
    end = _parse_local(args.get("end"))

    if window_key in WINDOWS:
        end = now
        start = now - WINDOWS[window_key][1]
    elif start and end:
        window_key = ""
    else:
        if args.get("start") or args.get("end"):
            notes.append("时间格式无法识别，已改用默认窗口。")
        window_key = DEFAULT_WINDOW
        end = now
        start = now - WINDOWS[DEFAULT_WINDOW][1]

    if start > end:
        start, end = end, start
        notes.append("开始时间晚于结束时间，已自动调换。")
    if end > now:
        end = now
        notes.append("结束时间不能晚于现在，已调整为当前时刻。")
    if start == end:
        start = end - WINDOWS[DEFAULT_WINDOW][1]
        notes.append("开始和结束时间相同，已改用默认窗口。")

    period_key = (args.get("period") or DEFAULT_PERIOD).strip()
    period_key, period_notes = fit_period(start, end, period_key)
    notes.extend(period_notes)

    period = PERIODS[period_key][0]
    return (
        MetricWindow(
            start=_floor(start, period),
            end=_floor(end, period),
            period_key=period_key,
            window_key=window_key,
        ),
        notes,
    )


def detect_window(window: MetricWindow, now: datetime | None = None) -> str:
    """反查当前窗口对应哪个快捷项，用于高亮（容忍一个 Period 的对齐误差）。"""
    if window.window_key:
        return window.window_key
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    for key, (_, span) in WINDOWS.items():
        if abs((now - window.end).total_seconds()) <= window.period and abs(
            (window.span - span).total_seconds()
        ) <= window.period:
            return key
    return ""
