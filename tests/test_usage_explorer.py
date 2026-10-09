"""下钻页数据层：时间桶、颜色槽、三种维度。"""

from __future__ import annotations

from datetime import date

import pytest

from bedrock_cost import usage_explorer
from bedrock_cost.report import build_report
from bedrock_cost.usage_explorer import (
    MAX_SERIES,
    assign_slots,
    build_buckets,
    build_usage,
)

START, END = date(2026, 8, 1), date(2026, 8, 17)


class TestBuckets:
    def test_daily_covers_every_day_inclusive(self):
        dates, labels = build_buckets(START, END, "daily")
        assert len(dates) == 17
        assert dates[0] == "2026-08-01" and dates[-1] == "2026-08-17"
        assert labels[0] == "08-01"

    def test_monthly_lands_on_first_of_month(self):
        dates, labels = build_buckets(date(2025, 12, 5), date(2026, 2, 3), "monthly")
        assert dates == ["2025-12-01", "2026-01-01", "2026-02-01"]
        assert labels == ["2025-12", "2026-01", "2026-02"]

    def test_single_day(self):
        dates, _ = build_buckets(END, END, "daily")
        assert dates == ["2026-08-17"]


class TestAssignSlots:
    NAMES = [f"Claude Opus {v} (Amazon Bedrock Edition)" for v in ("4.5", "4.6", "4.7", "4.8", "5")]

    def test_colors_never_depend_on_display_order(self):
        """金额涨跌导致的排序变化绝不能换色。"""
        base = assign_slots(self.NAMES)
        assert assign_slots(list(reversed(self.NAMES))) == base
        assert assign_slots(sorted(self.NAMES)) == base

    def test_deterministic(self):
        assert assign_slots(self.NAMES) == assign_slots(self.NAMES)

    def test_no_duplicate_colors(self):
        slots = assign_slots(self.NAMES)
        assert len(set(slots.values())) == len(slots)

    def test_full_table_still_unique(self):
        names = [f"series-{i}" for i in range(MAX_SERIES)]
        slots = assign_slots(names)
        assert sorted(slots.values()) == list(range(MAX_SERIES))

    def test_similar_prefixes_still_spread_out(self):
        """这些名字前后缀高度雷同，crc32 低位几乎不散列，所以改用了 blake2b。"""
        slots = assign_slots(self.NAMES)
        assert len(set(slots.values())) == len(self.NAMES)

    def test_dropping_a_series_moves_at_most_one_other(self):
        base = assign_slots(self.NAMES)
        shrunk = assign_slots(self.NAMES[1:])
        moved = [n for n in self.NAMES[1:] if shrunk[n] != base[n]]
        assert len(moved) <= 1


class TestBuildUsage:
    @pytest.mark.parametrize("dimension", ["service", "tag", "account"])
    def test_reconciles_with_the_overview_page(self, ledger, fake_costs, dimension):
        """最关键的不变量：任一维度加总都等于概览页的总消费。

        概览页不再收区间，它按每个账号的启用日期累计到「今天」。台账里两个账号的
        启用日期都是 START，所以拿 END 当今天，两边查的就是同一个区间。
        """
        overview = build_report(END)
        usage = build_usage(
            __import__("bedrock_cost").excel_source.load_accounts(), START, END, dimension, "daily"
        )
        assert round(usage.total_marked, 6) == round(overview.total_cost, 6)

    @pytest.mark.parametrize("dimension", ["service", "tag", "account"])
    def test_rows_and_columns_agree(self, ledger, fake_costs, accounts, dimension):
        usage = build_usage(accounts, START, END, dimension, "daily")
        by_row = sum(s.total_marked for s in usage.series)
        by_column = sum(usage.column_totals)
        assert round(by_row, 6) == round(by_column, 6) == round(usage.total_marked, 6)

    def test_series_lengths_match_bucket_count(self, ledger, fake_costs, accounts):
        usage = build_usage(accounts, START, END, "service", "daily")
        width = len(usage.dates)
        assert width == 17
        assert all(len(s.raw) == width and len(s.marked) == width for s in usage.series)

    def test_marked_is_at_least_raw_when_ratios_exceed_one(self, ledger, fake_costs, accounts):
        usage = build_usage(accounts, START, END, "service", "daily")
        assert usage.total_marked >= usage.total_raw

    def test_account_dimension_lists_every_account(self, ledger, fake_costs, accounts):
        usage = build_usage(accounts, START, END, "account", "daily")
        assert len(usage.series) == len(accounts)
        assert {s.name for s in usage.series} == {
            f"{a.partner} / {a.account}" for a in accounts
        }

    def test_daily_and_monthly_totals_agree(self, ledger, fake_costs, accounts):
        daily = build_usage(accounts, START, END, "service", "daily")
        monthly = build_usage(accounts, START, END, "service", "monthly")
        assert round(daily.total_marked, 4) == round(monthly.total_marked, 4)

    def test_folds_the_tail_into_other(self, ledger, accounts, monkeypatch):
        """超过 8 条要折叠，且「其他」用中性灰而不是第 9 个分类色。"""

        def many_series(account, start, end, dimension, granularity, dates, refresh):
            width = len(dates)
            rows = {
                f"svc-{i:02d}": [[float(20 - i), float(20 - i)] for _ in range(width)]
                for i in range(14)
            }
            return rows, "USD", False, None

        monkeypatch.setattr(usage_explorer, "_fetch_account", many_series)
        usage_explorer.clear_cache()
        usage = build_usage(accounts[:1], START, END, "service", "daily")

        assert len(usage.series) == MAX_SERIES + 1
        assert usage.series[-1].name == usage_explorer.OTHER_LABEL
        assert usage.series[-1].slot == -1
        assert usage.folded_count == 14 - MAX_SERIES
        # 折叠不能丢钱
        assert round(sum(s.total_marked for s in usage.series), 4) == round(
            sum(20 - i for i in range(14)) * len(usage.dates), 4
        )

    def test_series_sorted_by_amount_descending(self, ledger, fake_costs, accounts):
        usage = build_usage(accounts, START, END, "service", "daily")
        totals = [s.total_marked for s in usage.series]
        assert totals == sorted(totals, reverse=True)

    def test_unknown_dimension_falls_back(self, ledger, fake_costs, accounts):
        usage = build_usage(accounts, START, END, "nonsense", "daily")
        assert usage.dimension == "service"

    def test_unknown_granularity_falls_back(self, ledger, fake_costs, accounts):
        usage = build_usage(accounts, START, END, "service", "hourly")
        assert usage.granularity == "daily"

    def test_account_errors_are_collected_not_raised(self, ledger, accounts, monkeypatch):
        def broken(account, start, end, dimension, granularity, dates, refresh):
            return {}, "USD", False, "凭证无效"

        monkeypatch.setattr(usage_explorer, "_fetch_account", broken)
        usage_explorer.clear_cache()
        usage = build_usage(accounts, START, END, "service", "daily")
        assert len(usage.errors) == len(accounts)
        assert usage.series == []
        assert usage.has_data is False

    def test_empty_account_list(self):
        usage = build_usage([], START, END, "service", "daily")
        assert usage.series == []
        assert usage.total_marked == 0.0
        assert usage.peak == (-1, 0.0)

    def test_peak_points_at_the_tallest_bucket(self, ledger, accounts, monkeypatch):
        def spiky(account, start, end, dimension, granularity, dates, refresh):
            cells = [[1.0, 1.0] for _ in dates]
            cells[3] = [99.0, 99.0]
            return {"svc": cells}, "USD", False, None

        monkeypatch.setattr(usage_explorer, "_fetch_account", spiky)
        usage_explorer.clear_cache()
        usage = build_usage(accounts[:1], START, END, "service", "daily")
        assert usage.peak == (3, 99.0)


# ------------------------------------------------------------ 运营看板：一个账号的逐日序列
from bedrock_cost import config  # noqa: E402
from bedrock_cost.aws_errors import CREDENTIALS, MISSING_CREDENTIALS, QueryError  # noqa: E402
from bedrock_cost.excel_source import Account  # noqa: E402


class TestAccountSeries:
    """运营看板要每个账号各自的数：不拆维度、不折叠进「其他」，按桶把各条序列加起来。"""

    @pytest.fixture(autouse=True)
    def _clean_cache(self):
        usage_explorer.clear_cache()
        yield
        usage_explorer.clear_cache()

    @staticmethod
    def install(monkeypatch, answer) -> list[dict]:
        """把 _fetch_account 换成假的；answer(dates) 给出它的返回值。"""
        calls: list[dict] = []

        def fake(account, start, end, dimension, granularity, dates, refresh):
            calls.append(dict(account=account, start=start, end=end, dimension=dimension,
                              granularity=granularity, dates=list(dates), refresh=refresh))
            return answer(dates)

        monkeypatch.setattr(usage_explorer, "_fetch_account", fake)
        return calls

    def test_sums_every_series_per_bucket(self, ledger, accounts, monkeypatch):
        def answer(dates):
            rows = {
                "ALPHA / 111111111111": [[1.0, 1.25] for _ in dates],
                "其他": [[float(i), float(i) * 2] for i in range(len(dates))],
            }
            return rows, "USD", False, None

        self.install(monkeypatch, answer)
        dates, raw, marked, cached, error = usage_explorer.account_series(accounts[0], START, END)
        assert error is None and cached is False
        assert dates == build_buckets(START, END, "daily")[0]
        assert raw == [1.0 + i for i in range(17)]
        assert marked == [1.25 + 2 * i for i in range(17)]

    def test_asks_for_the_account_dimension(self, ledger, accounts, monkeypatch):
        calls = self.install(monkeypatch, lambda dates: ({}, "USD", False, None))
        usage_explorer.account_series(accounts[0], START, END)
        usage_explorer.account_series(accounts[0], START, END, "monthly", refresh=True)
        assert [(c["dimension"], c["granularity"], c["refresh"]) for c in calls] == [
            ("account", "daily", False), ("account", "monthly", True),
        ]
        assert calls[1]["dates"] == build_buckets(START, END, "monthly")[0]
        assert (calls[0]["account"], calls[0]["start"], calls[0]["end"]) == (accounts[0], START, END)

    def test_monthly_buckets(self, ledger, accounts, monkeypatch):
        self.install(monkeypatch, lambda dates: ({"x": [[5.0, 6.0] for _ in dates]}, "USD", False, None))
        dates, raw, marked, _, _ = usage_explorer.account_series(accounts[0], date(2026, 6, 15), END, "monthly")
        assert dates == ["2026-06-01", "2026-07-01", "2026-08-01"]
        assert (raw, marked) == ([5.0] * 3, [6.0] * 3)

    def test_cached_flag_passes_through(self, ledger, accounts, monkeypatch):
        self.install(monkeypatch, lambda dates: ({}, "USD", True, None))
        assert usage_explorer.account_series(accounts[0], START, END)[3] is True

    def test_no_spend_is_zeros_not_empty(self, ledger, accounts, monkeypatch):
        """查成功但没消费：每个桶都是 0，和「查不了」的空列表分得开。"""
        self.install(monkeypatch, lambda dates: ({}, "USD", False, None))
        _, raw, marked, _, error = usage_explorer.account_series(accounts[0], START, END)
        assert raw == marked == [0.0] * 17
        assert error is None

    def test_failure_gives_empty_lists_and_says_whose(self, ledger, accounts, monkeypatch):
        self.install(monkeypatch, lambda dates: ({}, "USD", True, "凭证无效"))
        dates, raw, marked, cached, error = usage_explorer.account_series(accounts[0], START, END)
        assert dates == build_buckets(START, END, "daily")[0]
        assert (raw, marked, cached) == ([], [], False)
        assert isinstance(error, QueryError)
        assert (error.account, error.partner, error.reason) == ("111111111111", "ALPHA", "凭证无效")
        assert str(error) == "ALPHA / 111111111111：凭证无效"

    def test_structured_failure_keeps_its_details(self, ledger, accounts, monkeypatch):
        problem = QueryError(reason="凭证缺少 ce:GetCostAndUsage 权限", detail="AccessDeniedException: denied",
                             code="AccessDeniedException", action="ce:GetCostAndUsage", kind="denied")
        self.install(monkeypatch, lambda dates: ({}, "USD", False, problem))
        error = usage_explorer.account_series(accounts[1], START, END)[4]
        assert (error.account, error.partner) == ("222222222222", "BETA")
        assert (error.reason, error.detail, error.code, error.action, error.kind) == (
            problem.reason, problem.detail, problem.code, problem.action, problem.kind,
        )

    def test_missing_credentials_never_reach_aws(self, ledger, accounts):
        keyless = Account(partner="GAMMA", account="333333333333", budget=0, tag_ratio=1, untag_ratio=1)
        _, raw, marked, cached, error = usage_explorer.account_series(keyless, START, END)
        assert (raw, marked, cached) == ([], [], False)
        assert (error.account, error.partner, error.reason, error.kind) == (
            "333333333333", "GAMMA", MISSING_CREDENTIALS, CREDENTIALS,
        )

    def test_second_call_comes_from_the_ce_cache(self, ledger, accounts, monkeypatch):
        """真走一遍 _fetch_account 的缓存，只把发请求的 _query_account 换成假的。"""
        dimensions: list[str] = []

        def query(account, start, end, dimension, granularity, dates):
            dimensions.append(dimension)
            return {"ALPHA / 111111111111": [[1.0, 2.0] for _ in dates]}, "USD"

        monkeypatch.setattr(usage_explorer, "_query_account", query)
        monkeypatch.setattr(config, "CACHE_TTL", 900)
        first = usage_explorer.account_series(accounts[0], START, END)
        second = usage_explorer.account_series(accounts[0], START, END)
        assert dimensions == ["account"]
        assert (first[3], second[3]) == (False, True)
        assert second[1:3] == first[1:3] == ([1.0] * 17, [2.0] * 17)
