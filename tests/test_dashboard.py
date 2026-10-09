"""概览、账号页、运营看板共用的拼装（dashboard.py）：报错弹窗、概览卡片、筛选签上的计数、
按账号的颜色、消费构成、近 30 天成本。只拼装不碰 Flask，所以这里全用假数据直接调。"""

from __future__ import annotations

import re
from datetime import date, timedelta
from types import SimpleNamespace

import pytest

from bedrock_cost import activity, chart, config, dashboard, usage_explorer
from bedrock_cost.aws_errors import QueryError
from bedrock_cost.cost_explorer import CostSplit
from bedrock_cost.dashboard import Toast, _parts
from bedrock_cost.excel_source import Account, LifecycleTag
from bedrock_cost.report import build_row

TODAY = date(2026, 10, 9)


def make_account(number: str, partner: str, email: str = "", row: int = 2, **extra) -> Account:
    fields = dict(
        partner=partner, account=number, budget=1000.0, tag_ratio=1.25, untag_ratio=1.5,
        ak=f"AKIAFAKE{number}", sk="s" * 40, row=row, email=email,
    )
    fields.update(extra)
    return Account(**fields)


ALPHA = make_account("111111111111", "ALPHA", "alpha@example.com", start_date=date(2026, 9, 1),
                     lifecycle=("正常",))
BETA = make_account("222222222222", "BETA", "beta@example.com", row=3)
GAMMA = make_account("333333333333", "GAMMA", row=4)   # 台账没填邮箱：label 是号码


@pytest.fixture(autouse=True)
def _pinned_config(monkeypatch):
    """期望值按 $ 和 70 / 90 的阈值写，不跟着开发机的 .env 走。"""
    monkeypatch.setattr(config, "CURRENCY_SYMBOL", "$")
    monkeypatch.setattr(config, "WARN_PCT", 70)
    monkeypatch.setattr(config, "DANGER_PCT", 90)
    monkeypatch.setattr(config, "CACHE_TTL", 900)
    dashboard.clear_cache()
    usage_explorer.clear_cache()
    yield
    dashboard.clear_cache()
    usage_explorer.clear_cache()


# ---------------------------------------------------------------- 报错弹窗
class TestParts:
    def test_query_error(self):
        error = QueryError(account="111111111111", region="us-east-1", reason="凭证缺少 cloudwatch:ListMetrics 权限",
                           detail="An error occurred (AccessDenied) when calling the ListMetrics operation")
        assert _parts(error) == (
            "111111111111", "us-east-1", "凭证缺少 cloudwatch:ListMetrics 权限",
            "An error occurred (AccessDenied) when calling the ListMetrics operation",
        )

    def test_query_error_without_detail_shows_the_reason(self):
        assert _parts(QueryError(account="111111111111", reason="台账中缺少 AK 或 SK，无法查询")) == (
            "111111111111", "", "台账中缺少 AK 或 SK，无法查询", "台账中缺少 AK 或 SK，无法查询",
        )

    def test_query_error_of_a_single_account_result(self):
        """HourUsage 这类单账号的结果里 account 留空。"""
        assert _parts(QueryError(region="us-west-2", reason="AWS 迟迟没有响应")) == (
            "", "us-west-2", "AWS 迟迟没有响应", "AWS 迟迟没有响应",
        )

    def test_anything_with_a_reason_is_treated_the_same(self):
        assert _parts(SimpleNamespace(reason="x", account=None, region=None)) == ("", "", "x", "x")

    def test_legacy_string_with_account_and_region(self):
        text = "Jeff / 123456789012 @ us-east-1：原因"
        assert _parts(text) == ("123456789012", "us-east-1", "原因", text)

    def test_legacy_string_without_region(self):
        text = "BETA / 222222222222：凭证缺少 ce:GetCostAndUsage 权限（AccessDeniedException）"
        assert _parts(text) == ("222222222222", "", "凭证缺少 ce:GetCostAndUsage 权限（AccessDeniedException）", text)

    def test_legacy_string_without_a_colon_is_all_reason(self):
        text = "台账中缺少 AK 或 SK，无法查询"
        assert _parts(text) == ("", "", text, text)

    def test_only_twelve_digit_numbers_are_accounts(self):
        assert _parts("Jeff / 12345 @ us-east-1：原因")[:2] == ("", "us-east-1")

    def test_reason_is_trimmed(self):
        assert _parts("Jeff / 123456789012：  原因  ")[2] == "原因"

    def test_round_trips_through_str(self):
        """老路径上的字符串就是 str(QueryError)，按同一个格式拆得回来。"""
        error = QueryError(account="123456789012", region="us-east-2", partner="Jeff",
                           reason="被组织的 SCP（服务控制策略）显式拒绝 bedrock:ListInferenceProfiles",
                           code="AccessDeniedException")
        number, region, reason, detail = _parts(str(error))
        assert (number, region, reason) == ("123456789012", "us-east-2", error.message)
        assert detail == str(error)


SCP = "被组织的 SCP（服务控制策略）显式拒绝 cloudwatch:ListMetrics"
SLOW = "CloudWatch 请求过于频繁，请稍后重试"


def denied(region: str, account: str = ALPHA.account, reason: str = SCP) -> QueryError:
    return QueryError(account=account, region=region, reason=reason, detail=f"AccessDenied in {region}")


class TestRegionToasts:
    def test_same_reason_in_several_regions_is_one_toast(self):
        errors = [denied("us-east-1"), denied("us-east-2"), denied("us-west-1"),
                  denied("us-west-2", reason=SLOW)]
        assert dashboard.region_toasts(errors, ALPHA, "读不到 CloudWatch") == [
            Toast("error", "3 个区读不到 CloudWatch", "alpha@example.com · 111111111111", SCP,
                  "us-east-1：AccessDenied in us-east-1\nus-east-2：AccessDenied in us-east-2\n"
                  "us-west-1：AccessDenied in us-west-1"),
            Toast("error", "us-west-2 读不到 CloudWatch", "alpha@example.com · 111111111111", SLOW,
                  "us-west-2：AccessDenied in us-west-2"),
        ]

    def test_errors_without_a_region(self):
        """Cost Explorer 不分区：标题就是那句话，详情里不带区域前缀。"""
        error = QueryError(account=ALPHA.account, reason="该区间暂无成本数据", detail="DataUnavailableException")
        assert dashboard.region_toasts([error], ALPHA, "查不到每天的成本") == [
            Toast("error", "查不到每天的成本", "alpha@example.com · 111111111111", "该区间暂无成本数据", "DataUnavailableException"),
        ]

    def test_legacy_strings(self):
        toasts = dashboard.region_toasts(
            ["ALPHA / 111111111111 @ us-east-1：原因", "ALPHA / 111111111111 @ us-west-2：原因"], None
        )
        assert len(toasts) == 1
        assert toasts[0].title == "2 个区查询失败"
        assert toasts[0].sub == ""
        assert toasts[0].detail.splitlines() == [
            "us-east-1：ALPHA / 111111111111 @ us-east-1：原因",
            "us-west-2：ALPHA / 111111111111 @ us-west-2：原因",
        ]

    def test_label_falls_back_to_the_number(self):
        toast = dashboard.region_toasts([denied("us-east-1", GAMMA.account)], GAMMA)[0]
        assert toast.sub == "333333333333"          # 没填邮箱：只写号码

    def test_nothing_to_report(self):
        assert dashboard.region_toasts([], ALPHA) == []


class TestAccountToasts:
    def test_same_reason_across_accounts_is_one_toast(self):
        errors = [denied("us-east-1"), denied("us-east-2"), denied("us-east-1", BETA.account),
                  denied("us-west-2", BETA.account, reason=SLOW)]
        assert dashboard.account_toasts(errors, [ALPHA, BETA], "读不到 CloudWatch") == [
            Toast("error", "2 个账号读不到 CloudWatch", "", SCP,
                  "alpha@example.com · 111111111111：AccessDenied in us-east-1\n"
                  "alpha@example.com · 111111111111：AccessDenied in us-east-2\n"
                  "beta@example.com · 222222222222：AccessDenied in us-east-1"),
            Toast("error", "读不到 CloudWatch", "beta@example.com · 222222222222", SLOW,
                  "beta@example.com · 222222222222：AccessDenied in us-west-2"),
        ]

    def test_one_account_several_regions_names_the_account(self):
        toasts = dashboard.account_toasts([denied("us-east-1"), denied("us-west-1")], [ALPHA, BETA], "读不到 CloudWatch")
        assert [(t.title, t.sub) for t in toasts] == [("读不到 CloudWatch", "alpha@example.com · 111111111111")]
        assert len(toasts[0].detail.splitlines()) == 2

    def test_account_not_in_the_ledger(self):
        error = denied("us-east-1", "999999999999")
        toast = dashboard.account_toasts([error], [ALPHA], "读不到 CloudWatch")[0]
        assert (toast.title, toast.sub) == ("1 个账号读不到 CloudWatch", "")
        assert toast.detail == "AccessDenied in us-east-1"

    def test_legacy_strings(self):
        toasts = dashboard.account_toasts(
            ["BETA / 222222222222：凭证无效", "ALPHA / 111111111111：凭证无效"], [ALPHA, BETA], "查不到累计消费"
        )
        assert [(t.title, t.text) for t in toasts] == [("2 个账号查不到累计消费", "凭证无效")]
        assert toasts[0].detail.splitlines() == [
            "beta@example.com · 222222222222：BETA / 222222222222：凭证无效",
            "alpha@example.com · 111111111111：ALPHA / 111111111111：凭证无效",
        ]

    def test_errors_that_name_no_account(self):
        toast = dashboard.account_toasts(["连不上 AWS"], [ALPHA], "额度出不来")[0]
        assert (toast.title, toast.sub, toast.text, toast.detail) == ("额度出不来", "", "连不上 AWS", "连不上 AWS")


# ---------------------------------------------------------------- 概览的卡片
def ok_row(account: Account = ALPHA, tag_raw: float = 400.0, untag_raw: float = 100.0):
    return build_row(account, CostSplit(tag_raw=tag_raw, untag_raw=untag_raw))


def failed_row(account: Account = ALPHA, problem: QueryError | None = None, error: str = "凭证无效"):
    return build_row(account, CostSplit(error=error, problem=problem))


def state(kind: str = "active", daily=(1.0, 2.0, 3.0)) -> activity.Activity:
    return activity.Activity(kind=kind, daily=list(daily))


def card(row=None, account: Account = ALPHA, kind: str = "active", daily=(1.0, 2.0, 3.0), order: int = 0):
    return dashboard.CardRow(row or ok_row(account), account, state(kind, daily), order, TODAY)


class TestCardRow:
    def test_ledger_fields(self):
        item = card(order=4)
        assert (item.email, item.label, item.key, item.order) == ("alpha@example.com", "alpha", ALPHA.key, 4)
        assert item.avatar == ALPHA.avatar
        assert item.lifecycle == ("正常",)

    def test_days_active_counts_both_ends(self):
        assert card().days_active == (TODAY - date(2026, 9, 1)).days + 1 == 39
        assert card(account=BETA).days_active == 0   # 台账没填启用日期

    def test_everything_else_comes_from_the_report_row(self):
        row = ok_row()
        item = card(row)
        assert item.total_cost == row.total_cost == 400 * 1.25 + 100 * 1.5
        assert item.usage_pct == row.usage_pct
        assert item.has_numbers is True and item.level == row.level

    @pytest.mark.parametrize(
        "kind, tone",
        [("active", "ok"), ("stopped", "warn"), ("idle", "none"), ("unknown", "none")],
    )
    def test_spark_accent_follows_the_usage_state(self, kind, tone):
        item = card(kind=kind)
        assert item.spark.startswith('<svg class="spark-svg"')
        assert f'fill="{chart.TONE_COLORS[tone]}"' in item.spark

    @pytest.mark.parametrize(
        "kind, failed, state, label",
        [("active", False, "active", "活跃"), ("stopped", False, "stopped", "已中断"), ("idle", False, "idle", "无调用"),
         ("unknown", False, "error", "异常"), ("active", True, "error", "异常")],
    )
    def test_state(self, kind, failed, state, label):
        """卡片上那行状态：用量读不到、消费查不到都是异常，其余照用量状态。"""
        item = card(failed_row() if failed else ok_row(), kind=kind)
        assert (item.state, item.state_label) == (state, label)

    def test_no_spark_without_daily_numbers(self):
        assert card(daily=()).spark == ""

    def test_no_issue_when_the_query_worked(self):
        item = card()
        assert item.issue == "" and item.error_detail == ""

    def test_issue_uses_the_structured_problem(self):
        problem = QueryError(account=ALPHA.account, reason="凭证缺少 ce:GetCostAndUsage 权限",
                             detail="An error occurred (AccessDeniedException) when calling GetCostAndUsage",
                             code="AccessDeniedException")
        item = card(failed_row(problem=problem, error=problem.message))
        assert item.issue == "凭证缺少 ce:GetCostAndUsage 权限"
        assert item.error_detail == "An error occurred (AccessDeniedException) when calling GetCostAndUsage"

    def test_issue_from_a_plain_error(self):
        item = card(failed_row(error="台账中缺少 AK 或 SK，无法查询"))
        assert item.issue == item.error_detail == "台账中缺少 AK 或 SK，无法查询"


class TestRiskRank:
    def test_errors_then_stopped_then_the_rest(self):
        assert dashboard.risk_rank(card(failed_row(), kind="stopped")) == 0
        assert dashboard.risk_rank(card(kind="unknown")) == 0          # 用量读不到也是异常
        assert dashboard.risk_rank(card(kind="stopped")) == 1
        for kind in ("active", "idle"):
            assert dashboard.risk_rank(card(kind=kind)) == 2

    def test_sorting_like_the_overview(self):
        cards = [card(ok_row(BETA), BETA, "active"), card(failed_row(GAMMA), GAMMA, "active"),
                 card(ok_row(ALPHA), ALPHA, "stopped")]
        cards.sort(key=lambda item: (dashboard.risk_rank(item), -(item.usage_pct or -1)))
        assert [item.account for item in cards] == [GAMMA.account, ALPHA.account, BETA.account]


TAGS = [LifecycleTag("正常", "green"), LifecycleTag("结算", "gray"), LifecycleTag("风控", "red")]


class TestFilterCounts:
    def test_lifecycle_counts(self):
        rows = [SimpleNamespace(lifecycle=("正常",)), SimpleNamespace(lifecycle=("正常", "老标签")),
                SimpleNamespace(lifecycle=())]
        counts = [(c.name, c.color, c.count) for c in dashboard.lifecycle_counts(rows, TAGS)]
        assert counts == [
            ("正常", "#2c7652", 2),
            ("结算", "#87867f", 0),             # 清单里的标签没人用也列出来
            ("风控", "#bc3b2e", 0),
            ("老标签", chart.TONE_COLORS["none"], 1),   # 清单外的老标签按灰色画
        ]

    def test_state_counts_skip_empty_states(self):
        rows = [
            SimpleNamespace(error=None, activity=SimpleNamespace(kind="active")),
            SimpleNamespace(error=None, activity=SimpleNamespace(kind="active")),
            SimpleNamespace(error=None, activity=SimpleNamespace(kind="stopped")),
            SimpleNamespace(error="凭证无效", activity=SimpleNamespace(kind="stopped")),   # 消费查不到：异常
            SimpleNamespace(error=None, activity=SimpleNamespace(kind="unknown")),         # 用量读不到：也是异常
        ]
        counts = [(c.key, c.label, c.count) for c in dashboard.state_counts(rows)]
        assert counts == [("active", "活跃", 2), ("stopped", "已中断", 1), ("error", "异常", 2)]   # 无调用没人，不列
        assert sum(count for *_, count in counts) == len(rows)          # 一个账号只落在一个状态里

    def test_near_limit(self):
        rows = [SimpleNamespace(label=name, has_numbers=ok, usage_pct=used)
                for name, ok, used in [("a", True, 50.0), ("b", True, 99.0), ("c", False, 120.0),
                                       ("d", True, None), ("e", True, 70.0), ("f", True, 10.0)]]
        assert [row.label for row in dashboard.near_limit(rows)] == ["b", "e", "a"]
        assert [row.label for row in dashboard.near_limit(rows, limit=1)] == ["b"]


def spender(label: str, cost: float, ok: bool = True) -> SimpleNamespace:
    return SimpleNamespace(label=label, total_cost=cost, has_numbers=ok)


class TestAccountColours:
    def test_top_eight_by_spend_get_a_slot(self):
        rows = [spender(f"acct{i}", float(100 - i)) for i in range(10)]
        rows += [spender("zero", 0.0), spender("failed", 5000.0, ok=False)]
        slots = dashboard.account_slots(rows)
        expected = [f"acct{i}" for i in range(8)]
        assert slots == usage_explorer.assign_slots(expected)
        assert sorted(slots.values()) == list(range(8))

    def test_spend_share_folds_the_rest_into_other(self):
        rows = [spender("small", 100.0), spender("big", 900.0), spender("tail", 30.0), spender("tail2", 20.0),
                spender("nothing", 0.0), spender("failed", 400.0, ok=False)]
        slots = usage_explorer.assign_slots(["big", "small"])
        share = dashboard.spend_share(rows, slots)
        assert [(item.name, item.value, item.color) for item in share.legend] == [
            ("big", 900.0, chart.color_for(slots["big"])),
            ("small", 100.0, chart.color_for(slots["small"])),
            ("其他", 50.0, chart.OTHER_COLOR),
        ]
        assert re.findall(r'data-name="([^"]+)"', share.svg) == ["big", "small", "其他"]
        assert 'data-value="$900.00"' in share.svg   # 悬浮里写金额

    def test_spend_share_with_nothing_spent(self):
        share = dashboard.spend_share([spender("a", 0.0), spender("b", 10.0, ok=False)], {})
        assert share.legend == []
        assert "没有数据" in share.svg


# ---------------------------------------------------------------- 近 30 天成本
class FakeCE:
    """usage_explorer._fetch_account 的替身：每个账号每天的 (原价, 折算后)，按「上游 / 账号」出一条。"""

    def __init__(self, daily: dict[str, float], errors: dict[str, str] | None = None, spike: int | None = None):
        self.daily = daily          # 账号号码 -> 每天原价
        self.errors = errors or {}
        self.spike = spike          # 这一天（下标）所有账号翻 10 倍
        self.calls = 0

    def __call__(self, account, start, end, dimension, granularity, dates, refresh):
        self.calls += 1
        if account.account in self.errors:
            return {}, "USD", False, self.errors[account.account]
        cells = []
        for index in range(len(dates)):
            raw = self.daily.get(account.account, 0.0) * (10 if index == self.spike else 1)
            cells.append([raw, raw * account.tag_ratio])
        return {f"{account.partner} / {account.account}": cells}, "USD", False, None


@pytest.fixture
def fake_ce(monkeypatch):
    def install(daily, errors=None, spike=None) -> FakeCE:
        fake = FakeCE(daily, errors, spike)
        monkeypatch.setattr(usage_explorer, "_fetch_account", fake)
        return fake

    return install


class TestCostTrend:
    def test_series_are_relabelled_by_account(self, fake_ce):
        fake_ce({ALPHA.account: 8.0, BETA.account: 4.0})
        slots = usage_explorer.assign_slots(["alpha", "beta"])
        trend = dashboard.cost_trend([ALPHA, BETA], TODAY, slots)
        legend = trend.chart.legend
        assert [item["name"] for item in legend] == ["alpha", "beta"]
        assert [item["color"] for item in legend] == [chart.color_for(slots["alpha"]), chart.color_for(slots["beta"])]

    def test_accounts_without_a_slot_fold_into_other(self, fake_ce):
        fake_ce({ALPHA.account: 8.0, BETA.account: 4.0, GAMMA.account: 2.0})
        trend = dashboard.cost_trend([ALPHA, BETA, GAMMA], TODAY, usage_explorer.assign_slots(["alpha"]))
        assert [(item["name"], item["color"]) for item in trend.chart.legend] == [
            ("alpha", chart.color_for(usage_explorer.assign_slots(["alpha"])["alpha"])),
            ("其他", chart.OTHER_COLOR),
        ]
        # 其他 = BETA + GAMMA 的折算后
        assert trend.chart.legend[1]["total"] == pytest.approx((4.0 + 2.0) * 1.25 * 30)

    def test_totals_average_and_peak(self, fake_ce):
        fake_ce({ALPHA.account: 8.0, BETA.account: 4.0}, spike=20)
        trend = dashboard.cost_trend([ALPHA, BETA], TODAY, usage_explorer.assign_slots(["alpha", "beta"]))
        day = (8.0 + 4.0) * 1.25
        assert trend.total == pytest.approx(day * 29 + day * 10)
        assert trend.average == pytest.approx(trend.total / 30)
        assert (trend.days, trend.start, trend.end) == (30, "09-10", "10-09")
        assert trend.peak_label == "09-30"
        assert trend.peak_value == pytest.approx(day * 10)
        assert trend.chart.tooltip[20]["total"] == "$150.00"

    def test_custom_length(self, fake_ce):
        fake_ce({ALPHA.account: 1.0})
        trend = dashboard.cost_trend([ALPHA], TODAY, {}, days=7)
        assert (trend.start, trend.end, len(trend.chart.tooltip)) == ("10-03", "10-09", 7)

    def test_errors_come_back_with_the_trend(self, fake_ce):
        fake_ce({ALPHA.account: 8.0}, errors={BETA.account: "凭证无效"})
        trend = dashboard.cost_trend([ALPHA, BETA], TODAY, usage_explorer.assign_slots(["alpha"]))
        assert [(e.account, e.partner, e.reason) for e in trend.errors] == [(BETA.account, "BETA", "凭证无效")]
        assert [item["name"] for item in trend.chart.legend] == ["alpha"]

    def test_nothing_spent(self, fake_ce):
        fake_ce({})
        trend = dashboard.cost_trend([ALPHA], TODAY, {})
        assert trend.chart.empty is True
        assert trend.total == 0.0 and trend.peak_label == ""

    def test_axis_and_tooltip_are_money(self, fake_ce):
        fake_ce({ALPHA.account: 800.0})
        trend = dashboard.cost_trend([ALPHA], TODAY, usage_explorer.assign_slots(["alpha"]))
        assert trend.chart.tooltip[0]["total"] == "$1,000.00"
        ticks = re.findall(r">([^<]+)</text>", trend.chart.svg[trend.chart.svg.index("chart-grid"):])
        assert "$0" in ticks and "$1.0K" in ticks


class TestSingleCostTrend:
    def test_one_clay_series(self, fake_ce):
        fake_ce({ALPHA.account: 8.0}, spike=29)
        trend = dashboard.single_cost_trend(ALPHA, TODAY)
        assert trend.chart.legend == [{"name": "成本", "color": "#d97757", "total": pytest.approx(10.0 * 39)}]
        assert trend.total == pytest.approx(10.0 * 39)
        assert trend.average == pytest.approx(10.0 * 39 / 30)
        assert (trend.peak_label, trend.peak_value) == ("10-09", pytest.approx(100.0))
        assert chart.color_for(0) not in trend.chart.svg

    def test_failed_query(self, fake_ce):
        fake_ce({}, errors={ALPHA.account: "凭证无效"})
        trend = dashboard.single_cost_trend(ALPHA, TODAY)
        assert trend.chart.empty is True
        assert trend.total == 0.0 and trend.peak_label == ""
        assert [e.reason for e in trend.errors] == ["凭证无效"]


class TestDailyCostCache:
    """CE 每次请求收 0.01 美元，一天也只更新几次：这张图单独缓存 TREND_TTL。"""

    def test_second_call_is_cached(self, fake_ce):
        fake = fake_ce({ALPHA.account: 1.0})
        first = dashboard.daily_cost([ALPHA], TODAY - timedelta(days=29), TODAY)
        second = dashboard.daily_cost([ALPHA], TODAY - timedelta(days=29), TODAY)
        assert second is first and fake.calls == 1

    def test_refresh_queries_again(self, fake_ce):
        fake = fake_ce({ALPHA.account: 1.0})
        dashboard.daily_cost([ALPHA], TODAY, TODAY)
        dashboard.daily_cost([ALPHA], TODAY, TODAY, refresh=True)
        assert fake.calls == 2

    def test_failures_are_not_cached(self, fake_ce):
        fake = fake_ce({}, errors={ALPHA.account: "凭证无效"})
        dashboard.daily_cost([ALPHA], TODAY, TODAY)
        dashboard.daily_cost([ALPHA], TODAY, TODAY)
        assert fake.calls == 2

    def test_expired_entry_is_queried_again(self, fake_ce):
        fake = fake_ce({ALPHA.account: 1.0})
        dashboard.daily_cost([ALPHA], TODAY, TODAY)
        key = next(iter(dashboard._trend_cache))
        stamp, report = dashboard._trend_cache[key]
        dashboard._trend_cache[key] = (stamp - dashboard.TREND_TTL - 1, report)
        dashboard.daily_cost([ALPHA], TODAY, TODAY)
        assert fake.calls == 2

    def test_key_has_the_accounts_and_the_range(self, fake_ce):
        fake = fake_ce({ALPHA.account: 1.0, BETA.account: 1.0})
        dashboard.daily_cost([ALPHA], TODAY, TODAY)
        dashboard.daily_cost([ALPHA, BETA], TODAY, TODAY)
        dashboard.daily_cost([ALPHA], TODAY - timedelta(days=1), TODAY)
        assert fake.calls == 4   # 1 + 2（账号组合不同就不是同一个键）+ 1


class TestMisc:
    @pytest.mark.parametrize(
        "value, expected",
        [(0, "$0"), (750, "$750"), (999, "$999"), (1000, "$1.0K"), (1500, "$1.5K"), (2_000_000, "$2.00M")],
    )
    def test_money_axis(self, value, expected):
        assert dashboard.money_axis(value) == expected

    def test_days_between(self):
        assert dashboard.days_between(date(2026, 10, 1), TODAY) == 9
        assert dashboard.days_between(TODAY, TODAY) == 1
        assert dashboard.days_between(None, TODAY) == 0

    def test_local_now_is_aware(self):
        assert dashboard.local_now().tzinfo is not None
