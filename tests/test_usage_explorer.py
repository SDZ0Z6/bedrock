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
        """最关键的不变量：任一维度加总都等于概览页的总消费。"""
        overview = build_report(START, END)
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
