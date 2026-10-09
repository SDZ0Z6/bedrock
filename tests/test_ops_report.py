"""运营看板的数据层（ops_report.py）：时间段、逐日台账和它的缓存、经营汇总、近 12 个月走势、
额度、需要关注的账号、最近的告警、模型与用量。

AWS 一律是假的：Cost Explorer 那条路换掉 usage_explorer.account_series（或者更底下的
_query_account），CloudWatch 那条路换掉 cloudwatch_metrics._fetch_region。日期全部钉死。
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, time, timedelta, timezone
from types import SimpleNamespace

import pytest

from bedrock_cost import activity, aws_errors, chart, cloudwatch_metrics, config, events, ops_report, usage_explorer
from bedrock_cost.aws_errors import QueryError
from bedrock_cost.cost_explorer import CostSplit
from bedrock_cost.dates import earliest_queryable, month_shift
from bedrock_cost.excel_source import Account, LifecycleTag
from bedrock_cost.ops_report import Issue, Ledger, Money, Span
from bedrock_cost.report import Report, build_row

UTC = timezone.utc
TODAY = date(2026, 10, 9)
SCP = "被组织的 SCP（服务控制策略）显式拒绝 cloudwatch:ListMetrics"
CE_DENIED = "凭证缺少 ce:GetCostAndUsage 权限"


def make_account(number: str, partner: str, email: str = "", row: int = 2, **extra) -> Account:
    fields = dict(
        partner=partner, account=number, budget=1000.0, tag_ratio=1.25, untag_ratio=1.5,
        ak=f"AKIAFAKE{number}", sk="s" * 40, row=row, email=email, tag_spec="map-migrated=migTEST",
    )
    fields.update(extra)
    return Account(**fields)


ALPHA = make_account("111111111111", "ALPHA", "alpha@example.com")
BETA = make_account("222222222222", "BETA", "beta@example.com", row=3)


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    """期望值按 $、70 / 90 的阈值写；各层缓存每个用例都从空的开始。"""
    monkeypatch.setattr(config, "CURRENCY_SYMBOL", "$")
    monkeypatch.setattr(config, "WARN_PCT", 70)
    monkeypatch.setattr(config, "DANGER_PCT", 90)
    monkeypatch.setattr(config, "CACHE_TTL", 900)
    for module in (ops_report, usage_explorer, cloudwatch_metrics):
        module.clear_cache()
    yield
    for module in (ops_report, usage_explorer, cloudwatch_metrics):
        module.clear_cache()


# ---------------------------------------------------------------- 时间段
class TestPeriodFor:
    def test_offered_periods(self):
        assert list(ops_report.PERIODS) == ["mtd", "last_month", "30d", "ytd"]
        assert ops_report.DEFAULT_PERIOD == "mtd"

    @pytest.mark.parametrize(
        "today, previous",
        [
            (date(2026, 10, 9), (date(2026, 9, 1), date(2026, 9, 9))),
            (date(2026, 3, 31), (date(2026, 2, 1), date(2026, 2, 28))),   # 上月没有 31 号：比到月底
            (date(2028, 3, 31), (date(2028, 2, 1), date(2028, 2, 29))),   # 闰年
            (date(2026, 3, 29), (date(2026, 2, 1), date(2026, 2, 28))),
            (date(2026, 5, 31), (date(2026, 4, 1), date(2026, 4, 30))),
            (date(2026, 1, 15), (date(2025, 12, 1), date(2025, 12, 15))),   # 跨年
            (date(2026, 10, 1), (date(2026, 9, 1), date(2026, 9, 1))),
        ],
    )
    def test_month_to_date(self, today, previous):
        period = ops_report.period_for("mtd", today)
        assert (period.key, period.label, period.compare) == ("mtd", "本月", "比上月同期")
        assert period.span == Span(today.replace(day=1), today)
        assert period.previous == Span(*previous)

    @pytest.mark.parametrize(
        "today, span, previous",
        [
            (date(2026, 10, 9), (date(2026, 9, 1), date(2026, 9, 30)), (date(2026, 8, 1), date(2026, 8, 31))),
            (date(2026, 3, 31), (date(2026, 2, 1), date(2026, 2, 28)), (date(2026, 1, 1), date(2026, 1, 31))),
            (date(2028, 3, 15), (date(2028, 2, 1), date(2028, 2, 29)), (date(2028, 1, 1), date(2028, 1, 31))),
            (date(2026, 1, 5), (date(2025, 12, 1), date(2025, 12, 31)), (date(2025, 11, 1), date(2025, 11, 30))),
            (date(2026, 2, 28), (date(2026, 1, 1), date(2026, 1, 31)), (date(2025, 12, 1), date(2025, 12, 31))),
        ],
    )
    def test_last_month(self, today, span, previous):
        period = ops_report.period_for("last_month", today)
        assert (period.key, period.label, period.compare) == ("last_month", "上月", "比前一个月")
        assert period.span == Span(*span)
        assert period.previous == Span(*previous)

    @pytest.mark.parametrize("today", [date(2026, 10, 9), date(2026, 3, 1), date(2026, 1, 1)])
    def test_last_thirty_days(self, today):
        period = ops_report.period_for("30d", today)
        assert (period.label, period.compare) == ("近 30 天", "比前 30 天")
        assert period.span == Span(today - timedelta(days=29), today)
        assert period.previous == Span(today - timedelta(days=59), today - timedelta(days=30))
        assert period.span.days == period.previous.days == 30

    @pytest.mark.parametrize("today", [TODAY, date(2026, 1, 1)])
    def test_year_to_date_has_nothing_to_compare(self, today):
        """CE 按天查不到去年年初，所以今年不比。"""
        period = ops_report.period_for("ytd", today)
        assert (period.label, period.compare) == ("今年", "")
        assert period.span == Span(date(today.year, 1, 1), today)
        assert period.previous is None

    @pytest.mark.parametrize("key", ["", "nonsense", "MTD", "last7"])
    def test_unknown_key_means_this_month(self, key):
        assert ops_report.period_for(key, TODAY) == ops_report.period_for("mtd", TODAY)


class TestSpan:
    def test_label_within_a_year(self):
        assert Span(date(2026, 9, 1), date(2026, 9, 30)).label == "09-01 ~ 09-30"

    def test_label_across_years_shows_the_year(self):
        assert Span(date(2025, 12, 15), date(2026, 1, 10)).label == "2025-12-15 ~ 2026-01-10"

    def test_label_of_a_single_day(self):
        assert Span(TODAY, TODAY).label == "10-09"

    def test_days_and_membership(self):
        span = Span(date(2026, 9, 1), date(2026, 9, 30))
        assert span.days == 30
        assert date(2026, 9, 1) in span and date(2026, 9, 30) in span
        assert date(2026, 8, 31) not in span and date(2026, 10, 1) not in span


class TestChange:
    @pytest.mark.parametrize(
        "now, before, expected",
        [(110, 100, 10.0), (90, 100, -10.0), (0, 100, -100.0), (100, 0, None), (100, -5, None),
         (None, 100, None), (100, None, None)],
    )
    def test_percentage_change(self, now, before, expected):
        """上一段是 0、负数或者没有就不比：比出来的百分比没有意义。"""
        assert ops_report.change(now, before) == expected


# ---------------------------------------------------------------- 逐日台账
SEPTEMBER = [date(2026, 9, 1) + timedelta(days=i) for i in range(30)]


def september_ledger() -> Ledger:
    return Ledger(
        days=list(SEPTEMBER),
        raw={"a#2": [1.0] * 30, "b#3": [float(i) for i in range(30)]},
        marked={"a#2": [2.0] * 30, "b#3": [float(2 * i) for i in range(30)]},
    )


class TestLedger:
    @pytest.mark.parametrize(
        "span, expected",
        [
            ((date(2026, 9, 10), date(2026, 9, 12)), range(9, 12)),
            ((date(2026, 8, 25), date(2026, 9, 2)), range(0, 2)),     # 前面超出窗口：裁掉
            ((date(2026, 9, 29), date(2026, 10, 5)), range(28, 30)),  # 后面超出窗口：裁掉
            ((date(2026, 8, 1), date(2026, 10, 31)), range(0, 30)),
        ],
    )
    def test_indices(self, span, expected):
        assert september_ledger().indices(Span(*span)) == expected

    @pytest.mark.parametrize("span", [(date(2026, 10, 1), date(2026, 10, 5)), (date(2026, 8, 1), date(2026, 8, 5))])
    def test_indices_outside_the_window(self, span):
        assert len(september_ledger().indices(Span(*span))) == 0

    def test_indices_of_an_empty_ledger(self):
        assert Ledger(days=[]).indices(Span(TODAY, TODAY)) == range(0)

    def test_sums(self):
        ledger = september_ledger()
        span = Span(date(2026, 9, 10), date(2026, 9, 12))
        assert ledger.sums("a#2", span) == (3.0, 6.0)
        assert ledger.sums("b#3", span) == (30.0, 60.0)

    def test_sums_of_an_unknown_account(self):
        assert september_ledger().sums("x#9", Span(date(2026, 9, 1), date(2026, 9, 30))) == (0.0, 0.0)

    def test_daily_adds_up_every_account(self):
        raw, marked = september_ledger().daily(Span(date(2026, 9, 1), date(2026, 9, 3)))
        assert raw == [1.0, 2.0, 3.0]
        assert marked == [2.0, 4.0, 6.0]

    def test_daily_outside_the_window(self):
        assert september_ledger().daily(Span(date(2026, 10, 1), date(2026, 10, 2))) == ([], [])


class TestLedgerWindow:
    def test_eleven_months_back_to_today(self):
        assert ops_report.ledger_window(TODAY) == Span(date(2025, 11, 1), TODAY)

    @pytest.mark.parametrize("today", [TODAY, date(2026, 1, 5), date(2026, 3, 31), date(2028, 2, 29)])
    def test_every_period_fits_inside(self, today):
        """本月、上月、近 30 天、今年，以及它们的上一段，都从这一份里切。"""
        window = ops_report.ledger_window(today)
        assert window.start >= earliest_queryable(today)
        assert window.start == month_shift(today, -(ops_report.TREND_MONTHS - 1))
        for key in ops_report.PERIODS:
            period = ops_report.period_for(key, today)
            for span in (period.span, period.previous):
                if span is not None:
                    assert span.start in window and span.end in window


class FakeSeries:
    """usage_explorer.account_series 的替身：每个账号每天固定的原价，折算后按这次传进来的
    账号的 TAG 比率算（这样看得出比率有没有用上新的）。"""

    def __init__(self):
        self.raw: dict[str, float] = {}       # 账号号码 -> 每天原价，没写的按 10
        self.errors: dict[str, object] = {}   # 账号号码 -> 错误
        self.short: set[str] = set()          # 这些账号故意少返回一个桶
        self.cached = False
        self.calls: list[SimpleNamespace] = []

    def __call__(self, account, start, end, granularity="daily", refresh=False):
        self.calls.append(SimpleNamespace(account=account.account, start=start, end=end,
                                          granularity=granularity, refresh=refresh))
        dates, _ = usage_explorer.build_buckets(start, end, granularity)
        if account.account in self.errors:
            error = aws_errors.as_query_error(self.errors[account.account], account=account.account,
                                              partner=account.partner)
            return dates, [], [], False, error
        width = len(dates) - (1 if account.account in self.short else 0)
        raw = [self.raw.get(account.account, 10.0)] * width
        return dates, raw, [value * account.tag_ratio for value in raw], self.cached, None


@pytest.fixture
def series(monkeypatch):
    fake = FakeSeries()
    monkeypatch.setattr(usage_explorer, "account_series", fake)
    return fake


WINDOW = ops_report.ledger_window(TODAY)


class TestBuildLedger:
    def test_days_cover_the_window(self, series):
        ledger = ops_report.build_ledger([ALPHA], TODAY)
        assert (ledger.days[0], ledger.days[-1]) == (date(2025, 11, 1), TODAY)
        assert len(ledger.days) == WINDOW.days == 343

    def test_one_daily_query_per_account_over_the_whole_window(self, series):
        ops_report.build_ledger([ALPHA, BETA], TODAY)
        assert sorted((c.account, c.start, c.end, c.granularity, c.refresh) for c in series.calls) == [
            (ALPHA.account, WINDOW.start, TODAY, "daily", False),
            (BETA.account, WINDOW.start, TODAY, "daily", False),
        ]

    def test_keeps_each_account_separately(self, series):
        series.raw = {ALPHA.account: 10.0, BETA.account: 4.0}
        ledger = ops_report.build_ledger([ALPHA, BETA], TODAY)
        assert ledger.raw[ALPHA.key] == [10.0] * WINDOW.days
        assert ledger.marked[BETA.key] == [5.0] * WINDOW.days
        assert ledger.errors == [] and ledger.any_cached is False

    def test_spend_before_the_start_date_is_dropped(self, series):
        """和概览一个口径：启用之前的消费不算。"""
        late = replace(ALPHA, start_date=date(2026, 9, 15))
        ledger = ops_report.build_ledger([late], TODAY)
        cut = (date(2026, 9, 15) - WINDOW.start).days
        assert ledger.raw[late.key][:cut] == [0.0] * cut
        assert ledger.raw[late.key][cut:] == [10.0] * (WINDOW.days - cut)
        assert ledger.sums(late.key, Span(date(2026, 9, 1), date(2026, 9, 30))) == (160.0, 200.0)

    @pytest.mark.parametrize("start", [None, date(2024, 1, 1), date(2025, 11, 1)])
    def test_start_date_on_or_before_the_window_keeps_everything(self, series, start):
        account = replace(ALPHA, start_date=start)
        assert ops_report.build_ledger([account], TODAY).raw[account.key] == [10.0] * WINDOW.days

    def test_start_date_in_the_future_counts_nothing(self, series):
        account = replace(ALPHA, start_date=date(2026, 12, 1))
        assert ops_report.build_ledger([account], TODAY).raw[account.key] == [0.0] * WINDOW.days

    def test_errors_are_collected_not_raised(self, series):
        series.errors = {BETA.account: "凭证无效"}
        ledger = ops_report.build_ledger([ALPHA, BETA], TODAY)
        assert list(ledger.raw) == [ALPHA.key]
        assert [(e.account, e.partner, e.reason) for e in ledger.errors] == [(BETA.account, "BETA", "凭证无效")]

    def test_wrong_bucket_count_is_left_out_rather_than_misaligned(self, series):
        series.short = {BETA.account}
        ledger = ops_report.build_ledger([ALPHA, BETA], TODAY)
        assert BETA.key not in ledger.raw and BETA.key not in ledger.marked
        assert ALPHA.key in ledger.raw

    def test_any_cached(self, series):
        series.cached = True
        assert ops_report.build_ledger([ALPHA], TODAY).any_cached is True

    def test_no_accounts(self, series):
        ledger = ops_report.build_ledger([], TODAY)
        assert len(ledger.days) == WINDOW.days
        assert ledger.raw == {} and series.calls == []


def age_cache(seconds: float) -> None:
    """把台账缓存里每一条都往前拨。"""
    for key, (stamp, value) in list(ops_report._cache.items()):
        ops_report._cache[key] = (stamp - seconds, value)


class TestLedgerCache:
    """CE 每次请求收 0.01 美元：一个账号的整窗数据缓存 LEDGER_TTL（2 小时）。"""

    def test_ttl_is_two_hours(self):
        assert ops_report.LEDGER_TTL == 2 * 3600

    def test_second_build_is_served_from_the_cache(self, series):
        first = ops_report.build_ledger([ALPHA], TODAY)
        second = ops_report.build_ledger([ALPHA], TODAY)
        assert len(series.calls) == 1
        assert second.raw == first.raw and second.marked == first.marked
        assert (first.any_cached, second.any_cached) == (False, True)

    def test_cached_lists_are_not_shared(self, series):
        first = ops_report.build_ledger([ALPHA], TODAY)
        first.raw[ALPHA.key][-1] = 999.0
        assert ops_report.build_ledger([ALPHA], TODAY).raw[ALPHA.key][-1] == 10.0

    def test_still_fresh_just_before_the_ttl(self, series):
        ops_report.build_ledger([ALPHA], TODAY)
        age_cache(ops_report.LEDGER_TTL - 60)
        ops_report.build_ledger([ALPHA], TODAY)
        assert len(series.calls) == 1

    def test_expires_after_the_ttl(self, series):
        ops_report.build_ledger([ALPHA], TODAY)
        age_cache(ops_report.LEDGER_TTL + 1)
        ops_report.build_ledger([ALPHA], TODAY)
        assert len(series.calls) == 2

    @pytest.mark.parametrize(
        "change", [dict(tag_ratio=1.3), dict(untag_ratio=1.6), dict(tag_spec="map-migrated=migOTHER")]
    )
    def test_ratio_or_tag_change_is_a_new_key(self, series, change):
        """台账里改了比率，下一次打开就按新比率算，不用等缓存过期。"""
        ops_report.build_ledger([ALPHA], TODAY)
        edited = replace(ALPHA, **change)
        ledger = ops_report.build_ledger([edited], TODAY)
        assert len(series.calls) == 2
        assert ledger.marked[ALPHA.key][-1] == 10.0 * edited.tag_ratio

    def test_start_date_change_applies_without_a_new_query(self, series):
        """启用日期是在缓存之后才裁的，改了不用重查。"""
        ops_report.build_ledger([ALPHA], TODAY)
        later = replace(ALPHA, start_date=date(2026, 10, 1))
        ledger = ops_report.build_ledger([later], TODAY)
        assert len(series.calls) == 1
        assert sum(ledger.raw[ALPHA.key]) == 90.0

    def test_a_new_day_is_a_new_window(self, series):
        ops_report.build_ledger([ALPHA], TODAY)
        ops_report.build_ledger([ALPHA], TODAY + timedelta(days=1))
        assert len(series.calls) == 2

    def test_refresh_bypasses_and_refills_the_cache(self, series):
        ops_report.build_ledger([ALPHA], TODAY)
        ops_report.build_ledger([ALPHA], TODAY, refresh=True)
        assert [c.refresh for c in series.calls] == [False, True]
        ops_report.build_ledger([ALPHA], TODAY)
        assert len(series.calls) == 2

    def test_failures_are_not_cached(self, series):
        series.errors = {ALPHA.account: "凭证无效"}
        ops_report.build_ledger([ALPHA], TODAY)
        series.errors = {}
        ledger = ops_report.build_ledger([ALPHA], TODAY)
        assert len(series.calls) == 2 and ALPHA.key in ledger.raw

    def test_clear_cache(self, series):
        ops_report.build_ledger([ALPHA], TODAY)
        ops_report.clear_cache()
        ops_report.build_ledger([ALPHA], TODAY)
        assert len(series.calls) == 2

    def test_ratio_change_reaches_the_ledger_through_the_ce_cache(self, monkeypatch):
        def query(account, start, end, dimension, granularity, dates):
            label = f"{account.partner} / {account.account}"
            return {label: [[10.0, 10.0 * account.tag_ratio] for _ in dates]}, "USD"

        monkeypatch.setattr(usage_explorer, "_query_account", query)
        ops_report.build_ledger([ALPHA], TODAY)
        ledger = ops_report.build_ledger([replace(ALPHA, tag_ratio=2.0)], TODAY)
        assert ledger.marked[ALPHA.key][-1] == 20.0


# ---------------------------------------------------------------- 经营汇总
class TestMoney:
    def test_margin_and_rate(self):
        money = Money("x", raw=100.0, marked=125.0)
        assert money.margin == 25.0 and money.rate == 20.0

    def test_no_revenue_no_rate(self):
        assert Money("x").rate is None
        assert Money("x", raw=5.0, marked=0.0).rate is None

    def test_nothing_to_compare_with(self):
        money = Money("x", raw=1.0, marked=2.0)
        assert (money.prev_margin, money.prev_rate, money.growth) == (None, None, None)

    def test_previous_span(self):
        money = Money("x", raw=100.0, marked=125.0, prev_raw=80.0, prev_marked=100.0)
        assert (money.prev_margin, money.prev_rate, money.growth) == (20.0, 20.0, 25.0)

    def test_previous_span_without_revenue(self):
        money = Money("x", raw=1.0, marked=2.0, prev_raw=0.0, prev_marked=0.0)
        assert (money.prev_rate, money.growth) == (None, None)

    def test_add_carries_the_previous_span_only_when_there_is_one(self):
        total = Money("合计", prev_raw=0.0, prev_marked=0.0)
        total.add(Money("a", raw=1.0, marked=2.0, prev_raw=3.0, prev_marked=4.0))
        total.add(Money("b", raw=10.0, marked=20.0))
        assert (total.raw, total.marked, total.prev_raw, total.prev_marked) == (11.0, 22.0, 3.0, 4.0)

        fresh = Money("合计")
        fresh.add(Money("a", raw=1.0, marked=2.0))
        assert fresh.prev_marked is None
        fresh.add(Money("b", raw=1.0, marked=2.0, prev_raw=1.0, prev_marked=3.0))
        assert (fresh.prev_raw, fresh.prev_marked) == (1.0, 3.0)


A1 = make_account("111111111111", "ALPHA", "a1@example.com", row=2)
A2 = make_account("333333333333", "ALPHA", "a2@example.com", row=4)
B1 = make_account("222222222222", "BETA", "b1@example.com", row=3)
LEDGER_DAYS = [date(2026, 9, 1) + timedelta(days=i) for i in range(39)]   # 09-01 … 10-09
MTD = ops_report.period_for("mtd", TODAY)   # 10-01 … 10-09，比 09-01 … 09-09


def business_ledger(report_error: bool = True) -> Ledger:
    """A1 九月每天原价 10、十月 20，比率 1.25；A2 每天 6，比率 1.5；B1 查不到。"""
    a1 = [10.0 if day.month == 9 else 20.0 for day in LEDGER_DAYS]
    a2 = [6.0] * len(LEDGER_DAYS)
    ledger = Ledger(days=list(LEDGER_DAYS), raw={A1.key: a1, A2.key: a2},
                    marked={A1.key: [v * 1.25 for v in a1], A2.key: [v * 1.5 for v in a2]})
    if report_error:
        ledger.errors.append(QueryError(account=B1.account, partner="BETA", reason=CE_DENIED,
                                        code="AccessDeniedException"))
    return ledger


def business(period=MTD, credit_rows=None, ledger=None):
    return ops_report.build_business([A1, A2, B1], ledger or business_ledger(), period, credit_rows or {})


class TestBuildBusiness:
    def test_totals(self):
        total = business().total
        # 本段 A1 原价 180 / 折算 225，A2 54 / 81
        assert (total.raw, total.marked, total.margin) == (234.0, 306.0, 72.0)
        assert total.rate == pytest.approx(72 / 306 * 100)

    def test_compared_with_the_previous_span(self):
        total = business().total
        # 上一段 A1 90 / 112.5，A2 54 / 81
        assert (total.prev_raw, total.prev_marked) == (144.0, 193.5)
        assert total.growth == pytest.approx((306 - 193.5) / 193.5 * 100)

    def test_accounts_by_revenue_with_failures_last(self):
        rows = business().accounts
        assert [row.name for row in rows] == ["a1", "a2", "b1"]
        assert [(row.raw, row.marked) for row in rows[:2]] == [(180.0, 225.0), (54.0, 81.0)]
        assert rows[0].growth == pytest.approx(100.0)
        assert [row.account for row in rows] == [A1, A2, B1]

    def test_failed_account_carries_its_reason_and_is_not_counted(self):
        ledger = business_ledger()
        failed = ops_report.build_business([A1, A2, B1], ledger, MTD, {}).accounts[-1]
        assert failed.error is ledger.errors[0]
        assert failed.has_numbers is False
        assert (failed.raw, failed.marked) == (0.0, 0.0)

    def test_failed_account_without_a_reported_error(self):
        failed = business(ledger=business_ledger(report_error=False)).accounts[-1]
        assert failed.error == "查不到 Cost Explorer"

    def test_partners(self):
        partners = business().partners
        assert [(p.name, p.accounts, p.failed, p.raw, p.marked) for p in partners] == [
            ("ALPHA", 2, 0, 234.0, 306.0),
            ("BETA", 1, 1, 0.0, 0.0),
        ]
        assert partners[0].growth == pytest.approx((306 - 193.5) / 193.5 * 100)
        assert partners[1].growth is None

    def test_partner_credit_comes_from_the_overview_rows(self):
        credit_rows = {
            A1.key: build_row(A1, CostSplit(tag_raw=240.0)),                                   # 用了 300
            A2.key: build_row(replace(A2, budget=500.0),                                       # 旧数顶着
                              CostSplit(tag_raw=80.0, error="凭证无效", stale_as_of=date(2026, 10, 7))),
            B1.key: build_row(replace(B1, budget=2000.0), CostSplit(error="凭证无效")),         # 没有数
        }
        result = business(credit_rows=credit_rows)
        alpha, beta = result.partners
        assert (alpha.budget, alpha.used, alpha.balance) == (1500.0, 400.0, 1100.0)
        assert (beta.budget, beta.used, beta.balance) == (0.0, 0.0, 0.0)
        assert [row.credit for row in result.accounts] == [credit_rows[A1.key], credit_rows[A2.key], credit_rows[B1.key]]

    def test_cards(self):
        cards = business().cards
        assert [c["label"] for c in cards] == ["收入", "AWS 原价", "毛利", "毛利率"]
        assert [c["value"] for c in cards] == ["$306", "$234", "$72", "23.53%"]
        assert [c["unit"] for c in cards] == ["%", "%", "%", " 个百分点"]
        assert [c["hint"] for c in cards] == ["折算后", "", "", "毛利 / 收入"]
        assert [c["delta"] for c in cards] == pytest.approx([
            (306 - 193.5) / 193.5 * 100,          # 收入
            (234 - 144) / 144 * 100,              # 原价
            (72 - 49.5) / 49.5 * 100,             # 毛利
            72 / 306 * 100 - 49.5 / 193.5 * 100,  # 毛利率差几个百分点
        ])

    def test_sparks(self):
        cards = business().cards
        for card in cards[:3]:
            assert card["spark"].startswith('<svg class="spark-svg"')
            assert ops_report.MARGIN_COLOR in card["spark"]
            assert card["spark"].count("<path") == MTD.span.days
        assert cards[3]["spark"] == ""

    def test_year_to_date_has_no_deltas(self):
        result = business(period=ops_report.period_for("ytd", TODAY))
        assert (result.total.prev_raw, result.total.prev_marked, result.total.growth) == (None, None, None)
        assert [card["delta"] for card in result.cards] == [None, None, None, None]
        assert all(partner.prev_marked is None for partner in result.partners)

    def test_previous_span_without_revenue_gives_no_deltas(self):
        ledger = business_ledger()
        for key in ledger.raw:
            ledger.raw[key] = [0.0 if day.month == 9 else v for day, v in zip(LEDGER_DAYS, ledger.raw[key])]
            ledger.marked[key] = [0.0 if day.month == 9 else v for day, v in zip(LEDGER_DAYS, ledger.marked[key])]
        result = business(ledger=ledger)
        assert result.total.growth is None
        assert [card["delta"] for card in result.cards] == [None, None, None, None]

    def test_counts_and_flags(self):
        ledger = business_ledger()
        ledger.any_cached = True
        result = ops_report.build_business([A1, A2, B1], ledger, MTD, {})
        assert (result.failed, result.counted, result.any_cached) == (1, 2, True)
        assert result.period is MTD


def full_ledger(ratio: float = 1.25, keys=("a#2",), raw_per_day: float = 10.0, days=None) -> Ledger:
    days = days or [WINDOW.start + timedelta(days=i) for i in range(WINDOW.days)]
    return Ledger(days=list(days), raw={k: [raw_per_day] * len(days) for k in keys},
                  marked={k: [raw_per_day * ratio] * len(days) for k in keys})


MONTHS = ["2025-11", "2025-12", "2026-01", "2026-02", "2026-03", "2026-04", "2026-05", "2026-06",
          "2026-07", "2026-08", "2026-09", "2026-10"]


class TestMonthlyTrend:
    def test_twelve_months_ending_this_month(self):
        trend = ops_report.monthly_trend(full_ledger(), TODAY)
        assert (trend.months, trend.start, trend.end) == (12, "2025-11", "2026-10")
        assert [column["label"] for column in trend.chart.tooltip] == MONTHS

    def test_each_bar_is_the_revenue_of_its_month(self):
        tooltip = ops_report.monthly_trend(full_ledger(), TODAY).chart.tooltip
        assert tooltip[0]["total"] == "$375.00"     # 2025-11：30 天 × 12.5
        assert tooltip[3]["total"] == "$350.00"     # 2026-02：28 天
        assert tooltip[-1]["total"] == "$112.50"    # 本月只到今天：9 天
        assert [row["name"] for row in tooltip[0]["rows"]] == ["AWS 原价", "毛利"]

    def test_totals(self):
        trend = ops_report.monthly_trend(full_ledger(), TODAY)
        assert (trend.total_raw, trend.total_marked, trend.total_margin) == (3430.0, 4287.5, 857.5)
        assert trend.rate == pytest.approx(20.0)

    def test_raw_at_the_bottom_margin_on_top(self):
        trend = ops_report.monthly_trend(full_ledger(), TODAY)
        assert trend.chart.legend == [
            {"name": "AWS 原价", "color": ops_report.RAW_COLOR, "total": 3430.0},
            {"name": "毛利", "color": ops_report.MARGIN_COLOR, "total": 857.5},
        ]

    def test_raw_stays_at_the_bottom_even_with_a_big_markup(self):
        trend = ops_report.monthly_trend(full_ledger(ratio=3.0), TODAY)
        assert [item["name"] for item in trend.chart.legend] == ["AWS 原价", "毛利"]

    def test_all_accounts_are_added_up(self):
        trend = ops_report.monthly_trend(full_ledger(keys=("a#2", "b#3")), TODAY)
        assert (trend.total_raw, trend.total_marked) == (6860.0, 8575.0)

    def test_negative_margin_is_drawn_as_zero_but_counted(self):
        """有人把比率填到 1 以下时那一截按 0 画，合计里的数还是对的。"""
        ledger = full_ledger()
        for i, day in enumerate(ledger.days):
            if (day.year, day.month) == (2025, 11):
                ledger.marked["a#2"][i] = 5.0
        trend = ops_report.monthly_trend(ledger, TODAY)
        november = trend.chart.tooltip[0]
        assert [row["name"] for row in november["rows"]] == ["AWS 原价"]
        assert november["total"] == "$300.00"
        drawn = {item["name"]: item["total"] for item in trend.chart.legend}["毛利"]
        assert drawn == 857.5 - 75.0
        assert trend.total_margin == 857.5 - 75.0 - 150.0
        assert trend.total_raw == 3430.0

    def test_months_before_the_ledger_are_empty(self):
        trend = ops_report.monthly_trend(full_ledger(days=LEDGER_DAYS), TODAY)
        assert [column["total"] for column in trend.chart.tooltip[:10]] == ["$0.00"] * 10
        assert trend.chart.tooltip[10]["total"] == "$375.00"

    def test_no_accounts(self):
        trend = ops_report.monthly_trend(Ledger(days=[]), TODAY)
        assert trend.chart.empty is True
        assert (trend.total_raw, trend.total_marked, trend.rate) == (0.0, 0.0, None)


# ---------------------------------------------------------------- 额度
def spent(total: float, budget: float = 1000.0, **split):
    """概览报表的一行：用了 total（比率 1.25，全是有标签的消费）。"""
    return build_row(replace(ALPHA, budget=budget), CostSplit(tag_raw=total / 1.25, **split))


def overview(*rows) -> Report:
    report = Report(end=TODAY)
    report.rows = list(rows)
    return report


class TestCreditSummary:
    def test_totals_and_counts(self):
        report = overview(
            spent(600.0), spent(800.0), spent(950.0), spent(1200.0),
            build_row(ALPHA, CostSplit(error="凭证无效")),                                  # 没有数
            spent(100.0, error="凭证无效", stale_as_of=date(2026, 10, 7)),                  # 旧数顶着
        )
        credit = ops_report.credit_summary(report)
        assert (credit.budget, credit.used, credit.balance) == (5000.0, 3650.0, 1350.0)
        assert credit.usage_pct == pytest.approx(73.0)
        assert credit.level == "warn"
        assert (credit.overspent, credit.danger, credit.warn, credit.failed, credit.accounts) == (1, 1, 1, 1, 6)
        assert credit.margin == pytest.approx(0.25 * (600 + 800 + 950 + 1200 + 100) / 1.25)
        assert credit.gauge == chart.render_gauge(0.73, "warn")

    def test_no_budget_means_no_gauge_reading(self):
        credit = ops_report.credit_summary(overview(spent(100.0, budget=0.0)))
        assert (credit.usage_pct, credit.level) == (None, "none")
        assert credit.gauge == chart.render_gauge(None, "none")

    def test_empty_overview(self):
        credit = ops_report.credit_summary(overview())
        assert (credit.budget, credit.used, credit.accounts, credit.margin) == (0.0, 0.0, 0, 0.0)


# ---------------------------------------------------------------- 风险与告警
NOW = datetime(2026, 10, 9, 10, 30, tzinfo=UTC)
TAGS = [LifecycleTag("正常", "green"), LifecycleTag("结算", "gray"), LifecycleTag("风控", "red"),
        LifecycleTag("观察", "amber")]
STOPPED = activity.Activity(kind="stopped", last_call=datetime(2026, 10, 8, 14, 7, tzinfo=UTC), checked_at=NOW)
UNKNOWN = activity.Activity(kind="unknown", checked_at=NOW)
ACTIVE = activity.Activity(kind="active", last_call=NOW - timedelta(minutes=3), checked_at=NOW)
CE_PROBLEM = QueryError(account="", reason=CE_DENIED, code="AccessDeniedException")


def numbered(index: int, **extra) -> Account:
    return make_account(f"10000000{index:04d}", "ALPHA", f"u{index}@example.com", row=index + 1, **extra)


def credit_for(account: Account, total: float, **split):
    return build_row(account, CostSplit(tag_raw=total / 1.25, **split))


class TestWatchList:
    def test_every_kind_of_problem(self):
        over, hot, warm, blind, stale, paused, dark, risky, watched, fine, quiet = (
            numbered(1), numbered(2), numbered(3), numbered(4), numbered(5), numbered(6), numbered(7),
            numbered(8, lifecycle=("风控",)), numbered(9, lifecycle=("观察",)),
            numbered(10, lifecycle=("正常", "老标签")), numbered(11),
        )
        credit_rows = {
            over.key: credit_for(over, 1200.0),
            hot.key: credit_for(hot, 950.0),
            warm.key: credit_for(warm, 800.0),
            blind.key: build_row(blind, CostSplit(error=CE_PROBLEM.message, problem=CE_PROBLEM)),
            stale.key: credit_for(stale, 300.0, error=CE_PROBLEM.message, problem=CE_PROBLEM,
                                  stale_as_of=date(2026, 10, 7)),
            fine.key: credit_for(fine, 100.0),
        }
        states = {paused.key: STOPPED, dark.key: UNKNOWN, fine.key: ACTIVE,
                  quiet.key: activity.Activity(kind="idle", checked_at=NOW)}
        accounts = [over, hot, warm, blind, stale, paused, dark, risky, watched, fine, quiet]
        watch = ops_report.watch_list(accounts, credit_rows, states, TAGS)
        assert [(w.account.label, w.issues) for w in watch] == [
            ("u1", [Issue("danger", "超出额度", "超了 $200.00")]),
            ("u2", [Issue("danger", "额度用了 95%", "余额 $50.00")]),
            ("u4", [Issue("danger", "查不到 Cost Explorer", CE_DENIED)]),
            ("u8", [Issue("danger", "风控", "生命周期")]),
            ("u3", [Issue("warn", "额度用了 80%", "余额 $200.00")]),
            ("u5", [Issue("warn", "Cost Explorer 查询失败", f"显示的是截至 10-07 的数 · {CE_DENIED}")]),
            ("u6", [Issue("warn", "用量中断", STOPPED.sub)]),
            ("u9", [Issue("warn", "观察", "生命周期")]),
            ("u7", [Issue("info", "读不到 CloudWatch", "不知道还有没有调用")]),
        ]

    def test_issues_sorted_and_busiest_account_first(self):
        messy = numbered(1, lifecycle=("观察",))
        single = numbered(2)
        credit_rows = {messy.key: build_row(messy, CostSplit(error="凭证无效")),
                       single.key: credit_for(single, 1200.0)}
        watch = ops_report.watch_list([single, messy], credit_rows, {messy.key: STOPPED}, TAGS)
        assert [w.account.label for w in watch] == ["u1", "u2"]
        assert [(i.tone, i.label) for i in watch[0].issues] == [
            ("danger", "查不到 Cost Explorer"), ("warn", "用量中断"), ("warn", "观察"),
        ]
        assert [w.rank for w in watch] == [0, 0]

    def test_stale_numbers_are_still_checked_against_the_budget(self):
        account = numbered(1)
        row = credit_for(account, 1100.0, error="凭证无效", stale_as_of=date(2026, 10, 7))
        (watch,) = ops_report.watch_list([account], {account.key: row}, {}, TAGS)
        assert [(i.tone, i.label) for i in watch.issues] == [("danger", "超出额度"), ("warn", "Cost Explorer 查询失败")]

    def test_plain_error_text_is_the_reason(self):
        account = numbered(1)
        row = build_row(account, CostSplit(error="台账中缺少 AK 或 SK，无法查询"))
        (watch,) = ops_report.watch_list([account], {account.key: row}, {}, TAGS)
        assert watch.issues == [Issue("danger", "查不到 Cost Explorer", "台账中缺少 AK 或 SK，无法查询")]

    def test_quiet_accounts_are_left_out(self):
        green = numbered(1, lifecycle=("正常", "结算", "不在清单里的标签"))
        assert ops_report.watch_list([green], {green.key: credit_for(green, 10.0)}, {green.key: ACTIVE}, TAGS) == []
        assert ops_report.watch_list([], {}, {}, TAGS) == []

    def test_rank_is_the_most_serious_issue(self):
        watch = ops_report.Watch(ALPHA, [Issue("info", "a"), Issue("warn", "b")])
        assert watch.rank == 1


T0 = datetime(2026, 10, 9, 3, 0, tzinfo=UTC)


class TestRecentEvents:
    """读 events.record 记下的流水（conftest 把 ALERT_EVENTS_PATH 指到了每个用例自己的临时目录）。"""

    def test_who(self):
        alpha = make_account("111111111111", "ALPHA", "alpha@example.com")
        gamma = make_account("333333333333", "GAMMA", row=4)   # 台账没填邮箱
        events.record("stopped", "用量中断", "这一小时没有调用", tone="error", account=alpha.account,
                      email="old@example.com", groups=2, when=T0)
        events.record("quota", "额度预警", "用了 92%", tone="warn", account=gamma.account, when=T0 + timedelta(hours=1))
        events.record("mail-abuse", "AWS 邮件", "滥用通知", tone="warn", account="999999999999",
                      email="stranger@example.com", when=T0 + timedelta(hours=2))
        events.record("disabled", "账号停用", account="888888888888", when=T0 + timedelta(hours=3))
        events.record("daily", "Bedrock 日报", "发到 1 个群", when=T0 + timedelta(hours=4))
        events.record("test", "测试消息", when=T0 + timedelta(hours=5))
        out = ops_report.recent_events([alpha, gamma])
        assert [(e.title, e.who, e.number) for e in out] == [
            ("测试消息", "", ""),
            ("Bedrock 日报", "全部账号", ""),
            ("账号停用", "888888888888", ""),
            ("AWS 邮件", "stranger", ""),            # 台账里没有：用邮箱前缀
            ("额度预警", "333333333333", "333333333333"),
            ("用量中断", "alpha", "111111111111"),    # 台账里有：用台账的名字，不用当时记的邮箱
        ]

    def test_local_time_and_passthrough(self):
        events.record("stopped", "用量中断", "这一小时没有调用", tone="error", groups=3, when=T0)
        (event,) = ops_report.recent_events([])
        local = T0.astimezone()
        assert (event.when, event.iso) == (local.strftime("%m-%d %H:%M"), local.isoformat(timespec="minutes"))
        assert (event.tone, event.title, event.text, event.groups) == ("error", "用量中断", "这一小时没有调用", 3)

    def test_newest_first_and_limited(self):
        for minute in range(15):
            events.record("test", f"#{minute}", when=T0 + timedelta(minutes=minute))
        assert [e.title for e in ops_report.recent_events([])] == [f"#{m}" for m in range(14, 2, -1)]
        assert [e.title for e in ops_report.recent_events([], limit=3)] == ["#14", "#13", "#12"]

    def test_nothing_sent_yet(self):
        assert ops_report.recent_events([ALPHA]) == []


# ---------------------------------------------------------------- 模型与用量
class TestUsageWindow:
    @pytest.mark.parametrize(
        "key, start, end",
        [
            ("mtd", date(2026, 9, 1), date(2026, 10, 10)),
            ("last_month", date(2026, 8, 1), date(2026, 10, 1)),
            ("30d", date(2026, 8, 11), date(2026, 10, 10)),
            ("ytd", date(2026, 1, 1), date(2026, 10, 10)),
        ],
    )
    def test_from_the_previous_span_to_the_end_of_the_last_day(self, key, start, end):
        window = ops_report.usage_window(ops_report.period_for(key, TODAY))
        assert window.start == datetime.combine(start, time(0), tzinfo=UTC)
        assert window.end == datetime.combine(end, time(0), tzinfo=UTC)
        assert window.period_key == "1d"

    def test_one_bucket_per_utc_day(self):
        window = ops_report.usage_window(MTD)
        stamps, _ = cloudwatch_metrics.build_grid(window)
        assert len(stamps) == 39
        assert all(stamp.astimezone(UTC).time() == time(0) for stamp in stamps)
        assert [stamps[0].astimezone(UTC).date(), stamps[-1].astimezone(UTC).date()] == [date(2026, 9, 1), TODAY]


OPUS, SONNET = "global.anthropic.claude-opus-4-8", "global.anthropic.claude-sonnet-4-5"
MIX = {"us-east-1": {OPUS: 3, SONNET: 1}, "us-west-2": {OPUS: 1}}


class FakeRegions:
    """cloudwatch_metrics._fetch_region 的替身。每天的量 = 段里的底数 × (区, 模型) 的倍数 × 指标的倍数。
    本段每天 10、上一段每天 4、中间隔着的那几天每天 1000——切错了一眼就看得出来。"""

    PER_METRIC = {"invocations": 1, "input_tokens": 100, "output_tokens": 10}

    def __init__(self, period, mix, failed=(), cached=False):
        self.period, self.mix, self.failed, self.cached = period, mix, set(failed), cached
        self.calls: list[tuple] = []

    def base(self, day: date) -> float:
        if day in self.period.span:
            return 10.0
        if self.period.previous is not None and day in self.period.previous:
            return 4.0
        return 1000.0

    def __call__(self, account, region, window, metric_key):
        self.calls.append((account.account, region, metric_key, window))
        if (account.account, region) in self.failed:
            return {}, False, QueryError(region=region, reason=SCP, detail=f"AccessDenied in {region}", kind="denied")
        stamps, _ = cloudwatch_metrics.build_grid(window)
        rows = {
            model: [self.base(s.astimezone(UTC).date()) * times * self.PER_METRIC[metric_key] for s in stamps]
            for model, times in self.mix.get(region, {}).items()
        }
        return rows, self.cached, None


@pytest.fixture
def regions(monkeypatch):
    def install(period=MTD, mix=MIX, failed=(), cached=False, tags_ok=True) -> FakeRegions:
        fake = FakeRegions(period, mix, failed, cached)
        monkeypatch.setattr(cloudwatch_metrics, "_fetch_region", fake)
        monkeypatch.setattr(cloudwatch_metrics, "resolve_profiles", lambda account, region: ({}, tags_ok))
        return fake

    return install


def cards_of(block) -> dict:
    return {card["key"]: card for card in block.cards}


class TestBuildUsageBlock:
    def test_three_metrics_for_every_account_and_region_over_one_window(self, regions):
        fake = regions()
        ops_report.build_usage_block([ALPHA], MTD)
        asked = {(account, region, metric) for account, region, metric, _ in fake.calls}
        assert asked == {(ALPHA.account, r, m) for r in cloudwatch_metrics.DEFAULT_REGIONS
                         for m in ops_report.USAGE_METRICS}
        assert {window.start for *_, window in fake.calls} == {ops_report.usage_window(MTD).start}

    def test_cards_compare_this_span_with_the_previous_one(self, regions):
        regions()
        block = ops_report.build_usage_block([ALPHA], MTD)
        assert [card["key"] for card in block.cards] == list(cloudwatch_metrics.METRICS)
        assert [(c["label"], c["unit"]) for c in block.cards] == [
            ("调用次数", "次"), ("输入 Token", "token"), ("输出 Token", "token"), ("总 Token", "token"),
        ]
        # 本段 9 天 × 10 × (3 + 1 + 1)；上一段 9 天 × 4 × 5；中间那几天的 1000 一个都不能算进来
        assert [c["total"] for c in block.cards] == [450.0, 45000.0, 4500.0, 49500.0]
        assert [c["delta"] for c in block.cards] == pytest.approx([150.0] * 4)
        assert all(c["spark"].count("<path") == MTD.span.days for c in block.cards)

    def test_models_by_calls_with_tokens_and_share(self, regions):
        regions()
        block = ops_report.build_usage_block([ALPHA], MTD)
        slots = usage_explorer.assign_slots(["claude-opus-4-8", "claude-sonnet-4-5"])
        assert [(m.name, m.value, m.tokens, m.share, m.other) for m in block.models] == [
            ("claude-opus-4-8", 360.0, 39600.0, 80.0, False),
            ("claude-sonnet-4-5", 90.0, 9900.0, 20.0, False),
        ]
        assert [m.color for m in block.models] == [chart.color_for(slots["claude-opus-4-8"]),
                                                    chart.color_for(slots["claude-sonnet-4-5"])]

    def test_other_goes_last_even_when_it_is_bigger(self, regions):
        mix = {"us-east-1": {**{f"global.anthropic.claude-m{i}": 10 + i for i in range(8)},
                             "global.anthropic.claude-tiny-a": 6, "global.anthropic.claude-tiny-b": 6}}
        regions(mix=mix)
        models = ops_report.build_usage_block([ALPHA], MTD).models
        assert [m.name for m in models] == [f"claude-m{i}" for i in range(7, -1, -1)] + ["其他"]
        other = models[-1]
        assert other.other is True and other.color == chart.OTHER_COLOR
        assert other.value > models[-2].value   # 比最小的那个模型还大，照样排最后

    def test_every_region_is_listed_even_without_calls(self, regions):
        regions()
        block = ops_report.build_usage_block([ALPHA], MTD)
        assert [(r.region, r.name, r.value, r.tokens, r.share) for r in block.regions] == [
            ("us-east-1", "弗吉尼亚", 360.0, 39600.0, 80.0),
            ("us-west-2", "俄勒冈", 90.0, 9900.0, 20.0),
            ("us-east-2", "俄亥俄", 0.0, 0.0, 0.0),
            ("us-west-1", "北加州", 0.0, 0.0, 0.0),
        ]

    def test_year_to_date_has_no_deltas(self, regions):
        ytd = ops_report.period_for("ytd", TODAY)
        regions(period=ytd)
        block = ops_report.build_usage_block([ALPHA], ytd)
        assert cards_of(block)["invocations"]["total"] == 282 * 10 * 5   # 01-01 … 10-09 共 282 天
        assert [c["delta"] for c in block.cards] == [None] * 4

    def test_same_failure_from_three_metrics_is_reported_once(self, regions):
        regions(failed={(ALPHA.account, "us-west-1")})
        block = ops_report.build_usage_block([ALPHA], MTD)
        assert [(e.account, e.region, e.partner, e.reason) for e in block.errors] == [
            (ALPHA.account, "us-west-1", "ALPHA", SCP),
        ]
        assert (block.failed_jobs, block.jobs, block.unreadable) == (1, 4, False)
        assert cards_of(block)["invocations"]["total"] == 450.0

    def test_every_job_failing_is_unreadable(self, regions):
        regions(failed={(ALPHA.account, r) for r in cloudwatch_metrics.DEFAULT_REGIONS})
        block = ops_report.build_usage_block([ALPHA], MTD)
        assert block.unreadable is True
        assert (block.failed_jobs, block.jobs, len(block.errors)) == (4, 4, 4)
        assert [(c["total"], c["delta"], c["spark"]) for c in block.cards] == [(None, None, "")] * 4
        assert block.models == [] and block.regions == []

    def test_one_unreadable_account_out_of_two_is_still_readable(self, regions):
        regions(failed={(BETA.account, r) for r in cloudwatch_metrics.DEFAULT_REGIONS})
        block = ops_report.build_usage_block([ALPHA, BETA], MTD)
        assert (block.unreadable, block.failed_jobs, block.jobs) == (False, 4, 8)
        assert {e.account for e in block.errors} == {BETA.account}
        assert cards_of(block)["invocations"]["total"] == 450.0

    def test_errors_are_kept_per_account(self, regions):
        regions(failed={(ALPHA.account, "us-west-1"), (BETA.account, "us-west-1")})
        block = ops_report.build_usage_block([ALPHA, BETA], MTD)
        assert sorted((e.account, e.region) for e in block.errors) == [
            (ALPHA.account, "us-west-1"), (BETA.account, "us-west-1"),
        ]
        assert cards_of(block)["invocations"]["total"] == 900.0

    def test_no_accounts(self, regions):
        fake = regions()
        block = ops_report.build_usage_block([], MTD)
        assert fake.calls == []
        assert [c["total"] for c in block.cards] == [None] * 4
        assert (block.models, block.regions, block.errors) == ([], [], [])
        assert (block.unreadable, block.jobs, block.failed_jobs, block.any_cached) == (False, 0, 0, False)

    def test_refresh_clears_the_cloudwatch_cache_once(self, regions, monkeypatch):
        regions()
        cleared = []
        monkeypatch.setattr(cloudwatch_metrics, "clear_cache", lambda: cleared.append(1))
        ops_report.build_usage_block([ALPHA], MTD)
        assert cleared == []
        ops_report.build_usage_block([ALPHA], MTD, refresh=True)
        assert cleared == [1]

    def test_cache_and_tag_flags_pass_through(self, regions):
        regions(mix={"us-east-1": {"2kbsta0lwebx": 1}}, cached=True, tags_ok=False)
        block = ops_report.build_usage_block([ALPHA], MTD)
        assert block.any_cached is True
        assert block.tags_resolved is False
        regions()
        assert ops_report.build_usage_block([ALPHA], MTD).tags_resolved is True
