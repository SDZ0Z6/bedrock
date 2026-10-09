"""账号的用量状态（activity.py）：活跃 / 已中断 / 无调用 / 异常，最近一次调用精确到分钟，
近 14 天的迷你柱图，以及缓存。

CloudWatch 两条路都换成假的：按小时的调用次数（cloudwatch_metrics.build_metrics）和一分钟
粒度的「这一小时最后一次调用」（activity 模块里引用的 hour_usage）。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from bedrock_cost import activity, cloudwatch_metrics
from bedrock_cost.activity import Activity
from bedrock_cost.aws_errors import QueryError
from bedrock_cost.cloudwatch_metrics import MetricSeries, UsageMetricsReport
from bedrock_cost.cost_estimate import HourUsage
from bedrock_cost.excel_source import Account

UTC = timezone.utc
NOW = datetime(2026, 10, 9, 10, 30, tzinfo=UTC)
HOUR = NOW.replace(minute=0)   # 当前这个还没走完的小时

ALPHA = Account(partner="ALPHA", account="111111111111", budget=1000, tag_ratio=1, untag_ratio=1,
                ak="AKIAFAKEALPHA0000000", sk="x" * 40, row=2, email="alpha@example.com")
BETA = Account(partner="BETA", account="222222222222", budget=1000, tag_ratio=1, untag_ratio=1,
               ak="AKIAFAKEBETA00000000", sk="y" * 40, row=3)
SCP = "被组织的 SCP（服务控制策略）显式拒绝 cloudwatch:ListMetrics"


class FakeCloudWatch:
    """hourly: 小时起点 -> 四区合计的调用次数；minutes: 小时起点 -> 这一小时最后一次调用；
    failed: 读不到的区。"""

    def __init__(self):
        self.hourly: dict[datetime, float] = {}
        self.minutes: dict[datetime, datetime] = {}
        self.failed: list[str] = []
        self.metric_calls: list[dict] = []
        self.hour_calls: list[datetime] = []

    def build_metrics(self, accounts, regions, window, metric_key="invocations", tag_filter="all", refresh=False):
        self.metric_calls.append(dict(accounts=list(accounts), regions=list(regions), window=window,
                                      metric_key=metric_key, refresh=refresh))
        stamps, labels = cloudwatch_metrics.build_grid(window)
        report = UsageMetricsReport(window=window, metric_key=metric_key, regions=list(regions),
                                    timestamps=stamps, labels=labels)
        report.series.append(
            MetricSeries(name="claude-opus-4-8", values=[self.hourly.get(s, 0.0) for s in stamps], slot=0)
        )
        report.errors = [QueryError(account=accounts[0].account, region=r, reason=SCP) for r in self.failed]
        return report

    def hour_usage(self, account, hour_start, regions=None):
        start = hour_start.astimezone(UTC)
        self.hour_calls.append(start)
        return HourUsage(start=start, last_call=self.minutes.get(start))


@pytest.fixture
def cw(monkeypatch):
    fake = FakeCloudWatch()
    monkeypatch.setattr(cloudwatch_metrics, "build_metrics", fake.build_metrics)
    monkeypatch.setattr(activity, "hour_usage", fake.hour_usage)
    activity.clear_cache()
    yield fake
    activity.clear_cache()


# ---------------------------------------------------------------- 文案
class TestTexts:
    @pytest.mark.parametrize(
        "kind, label", [("active", "活跃"), ("stopped", "已中断"), ("idle", "无调用"), ("unknown", "异常")]
    )
    def test_label(self, kind, label):
        assert Activity(kind=kind).label == label

    def test_unknown(self):
        assert Activity(kind="unknown", checked_at=NOW).sub == "读不到 CloudWatch"

    def test_idle(self):
        assert Activity(kind="idle", checked_at=NOW).sub == f"{activity.LOOKBACK_DAYS} 天内没有调用"

    @pytest.mark.parametrize(
        "ago, text",
        [(timedelta(seconds=30), "最近调用 刚刚"), (timedelta(minutes=5), "最近调用 5 分钟前"),
         (timedelta(minutes=59, seconds=59), "最近调用 59 分钟前")],
    )
    def test_active_counts_minutes_from_the_check(self, ago, text):
        assert Activity(kind="active", last_call=NOW - ago, checked_at=NOW).sub == text

    def test_active_without_check_time_uses_now(self):
        state = Activity(kind="active", last_call=datetime.now(UTC) - timedelta(minutes=2, seconds=5))
        assert state.sub == "最近调用 2 分钟前"

    def test_stopped_exact_is_local_time_to_the_minute(self):
        last = datetime(2026, 10, 8, 14, 7, tzinfo=UTC)
        state = Activity(kind="stopped", last_call=last, exact=True, checked_at=NOW)
        assert state.sub == f"最近调用 {last.astimezone():%m-%d %H:%M}"

    def test_stopped_inexact_only_names_the_hour(self):
        last = datetime(2026, 9, 1, 14, 0, tzinfo=UTC)
        state = Activity(kind="stopped", last_call=last, exact=False, checked_at=NOW)
        assert state.sub == f"最近调用 {last.astimezone():%m-%d %H} 点那一小时"

    @pytest.mark.parametrize("kind", ["active", "stopped"])
    def test_no_last_call_no_text(self, kind):
        assert Activity(kind=kind, checked_at=NOW).sub == ""


# ---------------------------------------------------------------- 判定
class TestAccountActivity:
    def test_no_credentials_is_unknown_without_asking(self, cw):
        keyless = Account(partner="GAMMA", account="333333333333", budget=0, tag_ratio=1, untag_ratio=1)
        state = activity.account_activity(keyless, now=NOW)
        assert state.kind == "unknown"
        assert state.errors and "AK" in state.errors[0]
        assert state.checked_at == NOW
        assert cw.metric_calls == [] and cw.hour_calls == []

    def test_asks_for_thirty_days_hourly_in_all_four_regions(self, cw):
        activity.account_activity(ALPHA, now=NOW)
        (call,) = cw.metric_calls
        assert call["accounts"] == [ALPHA]
        assert call["regions"] == cloudwatch_metrics.DEFAULT_REGIONS
        assert call["metric_key"] == "invocations"
        window = call["window"]
        assert (window.start, window.end, window.period_key) == (HOUR - timedelta(days=30), HOUR, "1h")

    def test_every_region_unreadable_is_unknown(self, cw):
        """说不清是没调用还是看不见，不能装作「无调用」。"""
        cw.failed = list(cloudwatch_metrics.DEFAULT_REGIONS)
        state = activity.account_activity(ALPHA, now=NOW)
        assert state.kind == "unknown"
        assert [e.region for e in state.errors] == cloudwatch_metrics.DEFAULT_REGIONS
        assert len(state.daily) == activity.SPARK_DAYS
        assert cw.hour_calls == []

    def test_some_regions_unreadable_is_still_judged(self, cw):
        cw.failed = ["us-west-1"]
        cw.minutes[HOUR] = NOW - timedelta(minutes=10)
        state = activity.account_activity(ALPHA, now=NOW)
        assert state.kind == "active"
        assert [e.region for e in state.errors] == ["us-west-1"]

    def test_call_in_the_current_hour(self, cw):
        cw.minutes[HOUR] = NOW - timedelta(minutes=10)
        state = activity.account_activity(ALPHA, now=NOW)
        assert (state.kind, state.last_call, state.exact) == ("active", NOW - timedelta(minutes=10), True)
        assert cw.hour_calls == [HOUR]

    def test_last_call_is_pinned_down_in_the_last_busy_hour(self, cw):
        busy = HOUR - timedelta(hours=3)
        cw.hourly[busy] = 50
        cw.hourly[HOUR - timedelta(hours=9)] = 80
        cw.minutes[busy] = busy + timedelta(minutes=42)
        state = activity.account_activity(ALPHA, now=NOW)
        assert (state.kind, state.last_call, state.exact) == ("stopped", busy + timedelta(minutes=42), True)
        assert cw.hour_calls == [HOUR, busy]

    def test_exactly_an_hour_ago_still_counts_as_active(self, cw):
        busy = HOUR - timedelta(hours=1)
        cw.hourly[busy] = 5
        cw.minutes[busy] = NOW - timedelta(minutes=activity.ACTIVE_MINUTES)
        assert activity.account_activity(ALPHA, now=NOW).kind == "active"

    def test_a_minute_more_is_stopped(self, cw):
        busy = HOUR - timedelta(hours=1)
        cw.hourly[busy] = 5
        cw.minutes[busy] = NOW - timedelta(minutes=activity.ACTIVE_MINUTES + 1)
        assert activity.account_activity(ALPHA, now=NOW).kind == "stopped"

    def test_minute_data_missing_falls_back_to_the_hour(self, cw):
        """读不到那一小时的一分钟数据：只知道是哪个小时，按小时末算（最晚可能就在那时）。"""
        busy = HOUR - timedelta(hours=2)
        cw.hourly[busy] = 5
        state = activity.account_activity(ALPHA, now=NOW)
        end_of_hour = busy + timedelta(hours=1) - timedelta(seconds=1)
        assert (state.kind, state.last_call, state.exact) == ("stopped", end_of_hour, False)
        assert cw.hour_calls == [HOUR, busy]

    def test_a_busy_previous_hour_without_minute_data_is_still_active(self, cw):
        """上一个小时（09 点）有调用、但读不到一分钟的数据：按小时末算，到 10:30 是半小时前，还算活跃；
        按小时开头算就成了「90 分钟前」，误判成已中断。"""
        busy = HOUR - timedelta(hours=1)
        cw.hourly[busy] = 5
        state = activity.account_activity(ALPHA, now=NOW)
        assert (state.kind, state.exact) == ("active", False)
        assert state.last_call == HOUR - timedelta(seconds=1)

    def test_beyond_minute_retention_only_the_hour_is_known(self, cw):
        """一分钟粒度的数据 CloudWatch 只留 15 天，更早的不去查，只知道是哪个小时。"""
        busy = HOUR - timedelta(days=activity.MINUTE_RETENTION_DAYS + 5)
        cw.hourly[busy] = 5
        cw.minutes[busy] = busy + timedelta(minutes=30)   # 就算有也不该去读
        state = activity.account_activity(ALPHA, now=NOW)
        end_of_hour = busy + timedelta(hours=1) - timedelta(seconds=1)
        assert (state.kind, state.last_call, state.exact) == ("stopped", end_of_hour, False)
        assert cw.hour_calls == [HOUR]

    def test_nothing_in_thirty_days_is_idle(self, cw):
        state = activity.account_activity(ALPHA, now=NOW)
        assert (state.kind, state.last_call) == ("idle", None)
        assert state.daily == [0.0] * activity.SPARK_DAYS
        assert state.checked_at == NOW

    def test_daily_numbers_for_the_spark(self, cw):
        """近 14 天每天一个数（本机时区的日期），最后一个是今天；更早的不进柱图。"""
        calls = {HOUR - timedelta(hours=1): 5.0, HOUR - timedelta(hours=25): 7.0,
                 HOUR - timedelta(days=12): 11.0, HOUR - timedelta(days=20): 13.0}
        cw.hourly.update(calls)
        state = activity.account_activity(ALPHA, now=NOW)
        today = NOW.astimezone().date()
        expected = [0.0] * activity.SPARK_DAYS
        for stamp, value in calls.items():
            back = (today - stamp.astimezone().date()).days
            if back < activity.SPARK_DAYS:
                expected[activity.SPARK_DAYS - 1 - back] += value
        assert state.daily == expected
        assert sum(state.daily) == 23.0


# ---------------------------------------------------------------- 缓存
class TestCache:
    def test_result_is_cached(self, cw):
        first = activity.account_activity(ALPHA)
        second = activity.account_activity(ALPHA)
        assert second is first
        assert len(cw.metric_calls) == 1

    def test_refresh_measures_again(self, cw):
        activity.account_activity(ALPHA)
        activity.account_activity(ALPHA, refresh=True)
        assert [call["refresh"] for call in cw.metric_calls] == [False, True]
        activity.account_activity(ALPHA)   # 刷新的结果照样进缓存
        assert len(cw.metric_calls) == 2

    def test_expired_entry_is_measured_again(self, cw):
        activity.account_activity(ALPHA)
        key = activity._cache_key(ALPHA)
        stamp, result = activity._cache[key]
        activity._cache[key] = (stamp - activity.CACHE_TTL - 1, result)
        activity.account_activity(ALPHA)
        assert len(cw.metric_calls) == 2

    def test_explicit_now_neither_reads_nor_writes_the_cache(self, cw):
        activity.account_activity(ALPHA)
        activity.account_activity(ALPHA, now=NOW)
        activity.account_activity(ALPHA, now=NOW)
        assert len(cw.metric_calls) == 3
        assert activity.account_activity(ALPHA).checked_at != NOW   # 缓存里还是不带 now 的那一份
        assert len(cw.metric_calls) == 3

    def test_key_changes_with_the_credentials(self, cw):
        """换了 AK（同一个号码、同一行）就是另一套凭证，结果不能串用。"""
        activity.account_activity(ALPHA)
        rotated = Account(**{**ALPHA.__dict__, "ak": "AKIAFAKEALPHA9999999"})
        activity.account_activity(rotated)
        assert len(cw.metric_calls) == 2

    def test_clear_cache(self, cw):
        activity.account_activity(ALPHA)
        activity.clear_cache()
        activity.account_activity(ALPHA)
        assert len(cw.metric_calls) == 2


class TestActivities:
    def test_no_accounts(self, cw):
        assert activity.activities([]) == {}

    def test_keyed_by_account_key(self, cw):
        cw.failed = list(cloudwatch_metrics.DEFAULT_REGIONS)
        states = activity.activities([ALPHA, BETA])
        assert set(states) == {ALPHA.key, BETA.key}
        assert {state.kind for state in states.values()} == {"unknown"}

    def test_uses_the_cache_and_passes_refresh(self, cw):
        activity.activities([ALPHA, BETA])
        activity.activities([ALPHA, BETA])
        assert len(cw.metric_calls) == 2
        activity.activities([ALPHA], refresh=True)
        assert len(cw.metric_calls) == 3
        assert cw.metric_calls[-1]["refresh"] is True
