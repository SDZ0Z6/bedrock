"""账号的用量状态：活跃 / 已中断 / 无调用 / 用量未知。

概览的卡片和账号页的页头都要回答「这个账号现在有没有在用、上次用是什么时候」。
TG 的每小时巡检也在判断这件事，但它只看开了告警的账号、只记到小时，所以这里
单独算一遍，全部账号一视同仁：

  · 往回查 LOOKBACK_DAYS 天、按小时的调用次数（四个区、全部模型合计），找到最后一个
    有调用的整点小时；
  · 再用一分钟粒度看「当前这个还没走完的小时」和「最后那个有调用的小时」，把最近一次
    调用精确到分钟（CloudWatch 一分钟的数据只留 15 天，更早的就只能精确到小时）；
  · 最近一次调用在 ACTIVE_MINUTES 分钟以内算活跃，否则是已中断；整段时间一次都没有
    是无调用；四个区全读不到（被 SCP 拒绝之类）是用量未知。

顺带给出近 SPARK_DAYS 天每天的调用次数，卡片上那条迷你柱图用它。
CloudWatch 的 GetMetricData 按请求的指标数收费（每千个 0.01 美元），这点量可以忽略；
慢的是往返，所以几个账号并发查，结果缓存 CACHE_TTL 秒。
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from . import cloudwatch_metrics
from .cost_estimate import hour_usage
from .excel_source import Account
from .windows import MetricWindow

ACTIVE_MINUTES = 60
LOOKBACK_DAYS = 30
SPARK_DAYS = 14
MINUTE_RETENTION_DAYS = 15   # 一分钟粒度的数据 CloudWatch 只留这么久
CACHE_TTL = 300
MAX_WORKERS = 6

LABELS = {"active": "活跃", "stopped": "已中断", "idle": "无调用", "unknown": "用量未知"}


@dataclass
class Activity:
    kind: str                            # active / stopped / idle / unknown
    last_call: datetime | None = None    # UTC
    exact: bool = True                   # last_call 精确到分钟；False 时只知道是哪个小时
    daily: list[float] = field(default_factory=list)   # 近 SPARK_DAYS 天每天，最后一个是今天
    errors: list = field(default_factory=list)          # 读不到的区（QueryError 或字符串）
    checked_at: datetime | None = None

    @property
    def label(self) -> str:
        return LABELS[self.kind]

    @property
    def sub(self) -> str:
        """状态后面那半句：最近一次调用是什么时候。"""
        if self.kind == "unknown":
            return "读不到 CloudWatch"
        if self.kind == "idle":
            return f"{LOOKBACK_DAYS} 天内没有调用"
        if not self.last_call:
            return ""
        now = self.checked_at or datetime.now(timezone.utc)
        minutes = int((now - self.last_call).total_seconds() // 60)
        if self.kind == "active":
            return "最近调用 刚刚" if minutes < 1 else f"最近调用 {minutes} 分钟前"
        local = self.last_call.astimezone()
        return f"最近调用 {local:%m-%d %H:%M}" if self.exact else f"最近调用 {local:%m-%d %H} 点那一小时"


_cache_lock = threading.Lock()
_cache: dict[str, tuple[float, Activity]] = {}


def clear_cache() -> None:
    with _cache_lock:
        _cache.clear()


def _cache_key(account: Account) -> str:
    return f"{account.account}#{account.row}#{account.ak[-6:]}"


def account_activity(account: Account, now: datetime | None = None, refresh: bool = False) -> Activity:
    """一个账号的用量状态。now 只给测试用。"""
    key = _cache_key(account)
    if not refresh and now is None:
        with _cache_lock:
            hit = _cache.get(key)
        if hit and time.time() - hit[0] < CACHE_TTL:
            return hit[1]

    result = _measure(account, now or datetime.now(timezone.utc), refresh)
    if now is None:
        with _cache_lock:
            _cache[key] = (time.time(), result)
    return result


def activities(accounts: list[Account], refresh: bool = False) -> dict[str, Activity]:
    """一批账号的用量状态，按 account.key。几个账号并发查。"""
    if not accounts:
        return {}
    with ThreadPoolExecutor(max_workers=min(MAX_WORKERS, len(accounts))) as pool:
        results = list(pool.map(lambda a: account_activity(a, refresh=refresh), accounts))
    return {account.key: result for account, result in zip(accounts, results)}


def _measure(account: Account, now: datetime, refresh: bool) -> Activity:
    if not account.has_credentials:
        return Activity(kind="unknown", errors=["台账中缺少 AK 或 SK"], checked_at=now)

    hour_now = now.replace(minute=0, second=0, microsecond=0)
    window = MetricWindow(start=hour_now - timedelta(days=LOOKBACK_DAYS), end=hour_now, period_key="1h")
    regions = list(cloudwatch_metrics.DEFAULT_REGIONS)
    report = cloudwatch_metrics.build_metrics([account], regions, window, "invocations", refresh=refresh)
    hourly = report.column_totals
    stamps = report.timestamps
    daily = _daily(stamps, hourly, now)

    # 每个区都读不到：说不清是没调用还是看不见，不能装作「无调用」
    if report.errors and len(report.errors) >= len(regions):
        return Activity(kind="unknown", daily=daily, errors=list(report.errors), checked_at=now)

    # 当前这一小时还没走完，按小时的序列里没有它，单独用一分钟粒度看
    current = hour_usage(account, hour_now)
    last_call = current.last_call
    exact = True
    if last_call is None:
        last_hour = next((stamps[i] for i in range(len(hourly) - 1, -1, -1) if hourly[i] > 0), None)
        if last_hour is not None:
            last_hour = last_hour.astimezone(timezone.utc)
            if now - last_hour <= timedelta(days=MINUTE_RETENTION_DAYS):
                detail = hour_usage(account, last_hour)
                last_call = detail.last_call
            if last_call is None:
                # 一分钟的数据已经过期（或者恰好没读到）：只知道是哪个小时。按这个小时的最后一刻
                # 算——那一小时里有调用，最晚可能就在小时末。按小时开头算的话，09:59 的调用到
                # 10:05 就成了「65 分钟前」，账号被误判成已中断
                last_call, exact = last_hour + timedelta(hours=1) - timedelta(seconds=1), False

    if last_call is None:
        kind = "idle"
    elif now - last_call <= timedelta(minutes=ACTIVE_MINUTES):
        kind = "active"
    else:
        kind = "stopped"
    return Activity(
        kind=kind, last_call=last_call, exact=exact, daily=daily,
        errors=list(report.errors), checked_at=now,
    )


def _daily(stamps: list[datetime], hourly: list[float], now: datetime) -> list[float]:
    """按小时的调用次数加成近 SPARK_DAYS 天、每天（本机时区的日期）一个数。"""
    today = now.astimezone().date()
    days = [today - timedelta(days=SPARK_DAYS - 1 - i) for i in range(SPARK_DAYS)]
    position = {day: i for i, day in enumerate(days)}
    totals = [0.0] * SPARK_DAYS
    for stamp, value in zip(stamps, hourly):
        slot = position.get(stamp.astimezone().date())
        if slot is not None:
            totals[slot] += value
    return totals
