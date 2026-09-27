"""日期区间解析。"""

from __future__ import annotations

from datetime import date

import pytest

from bedrock_cost.dates import (
    CE_HISTORY_MONTHS,
    PRESET_KEYS,
    detect_preset,
    month_shift,
    parse_date,
    preset_range,
    CUMULATIVE_CLAMPED,
    CUMULATIVE_FUTURE,
    CUMULATIVE_MISSING,
    CUMULATIVE_OK,
    cumulative_range,
    earliest_queryable,
    resolve_range,
)

TODAY = date(2026, 8, 17)


@pytest.mark.parametrize(
    "key, expected",
    [
        ("mtd", (date(2026, 8, 1), date(2026, 8, 17))),
        ("last_month", (date(2026, 7, 1), date(2026, 7, 31))),
        ("last7", (date(2026, 8, 11), date(2026, 8, 17))),
        ("last30", (date(2026, 7, 19), date(2026, 8, 17))),
        ("ytd", (date(2026, 1, 1), date(2026, 8, 17))),
    ],
)
def test_preset_range(key, expected):
    assert preset_range(key, TODAY) == expected


def test_last_month_crosses_year():
    assert preset_range("last_month", date(2026, 1, 9)) == (
        date(2025, 12, 1),
        date(2025, 12, 31),
    )


def test_last_month_lands_on_february_end():
    assert preset_range("last_month", date(2028, 3, 5))[1] == date(2028, 2, 29)


def test_unknown_preset_is_none():
    assert preset_range("nonsense", TODAY) is None


def test_last7_is_inclusive_of_both_ends():
    start, end = preset_range("last7", TODAY)
    assert (end - start).days == 6


@pytest.mark.parametrize("key", PRESET_KEYS)
def test_detect_preset_round_trips(key):
    start, end = preset_range(key, TODAY)
    assert detect_preset(start, end, TODAY) == key


def test_detect_preset_returns_empty_for_custom_range():
    assert detect_preset(date(2026, 3, 3), date(2026, 3, 9), TODAY) == ""


def test_month_shift_wraps_year():
    assert month_shift(date(2026, 1, 15), -1) == date(2025, 12, 1)
    assert month_shift(date(2026, 12, 15), 1) == date(2027, 1, 1)


@pytest.mark.parametrize(
    "raw, expected",
    [("2026-08-17", date(2026, 8, 17)), ("", None), (None, None), ("abc", None), ("2026-13-01", None)],
)
def test_parse_date(raw, expected):
    assert parse_date(raw) == expected


class TestResolveRange:
    def test_defaults_to_month_to_date(self):
        start, end, notes = resolve_range({}, TODAY)
        assert (start, end) == (date(2026, 8, 1), TODAY)
        assert notes == []

    def test_preset_wins_over_explicit_dates(self):
        start, end, _ = resolve_range(
            {"preset": "last7", "start": "2026-01-01", "end": "2026-01-02"}, TODAY
        )
        assert (start, end) == preset_range("last7", TODAY)

    def test_swaps_reversed_range(self):
        start, end, notes = resolve_range({"start": "2026-08-20", "end": "2026-08-05"}, TODAY)
        assert start < end
        assert any("调换" in n for n in notes)

    def test_clamps_future_end_to_today(self):
        _, end, notes = resolve_range({"start": "2026-08-01", "end": "2099-01-01"}, TODAY)
        assert end == TODAY
        assert any("今天" in n for n in notes)

    def test_bad_dates_fall_back_and_warn(self):
        start, end, notes = resolve_range({"start": "notadate", "end": "也不是日期"}, TODAY)
        assert (start, end) == (date(2026, 8, 1), TODAY)
        assert len(notes) == 2

    def test_narrows_to_cost_explorer_retention(self):
        start, _, notes = resolve_range({"start": "2000-01-01"}, TODAY)
        assert start == earliest_queryable(TODAY)
        assert any("Cost Explorer" in n for n in notes)


class TestEarliestQueryable:
    def test_counts_the_current_month(self):
        """当月算在 14 个月之内，所以往前数的是 13 个月。

        实测：2026-09-13 这天起点填 2025-08-01 能查，填 2025-07-xx 一律被
        CE 拒掉（ValidationException）。早先按 -14 个月算，实际要了 15 个月。
        """
        assert earliest_queryable(date(2026, 9, 13)) == date(2025, 8, 1)

    def test_always_lands_on_a_month_start(self):
        for day in (1, 15, 28):
            assert earliest_queryable(date(2026, 9, day)).day == 1

    def test_spans_exactly_the_documented_month_count(self):
        start = earliest_queryable(TODAY)
        months = (TODAY.year - start.year) * 12 + TODAY.month - start.month
        assert months + 1 == CE_HISTORY_MONTHS   # +1 = 当月

    def test_works_without_a_request_context(self):
        """刻意不依赖 Flask 的 request，纯字典就能测。"""
        assert resolve_range({"start": "2026-08-10", "end": "2026-08-12"}, TODAY)[:2] == (
            date(2026, 8, 10),
            date(2026, 8, 12),
        )


class TestCumulativeRange:
    """概览页的区间：从账号的启用日期累计到今天，每个账号各算各的。"""

    def test_uses_the_start_date(self):
        assert cumulative_range(date(2026, 8, 1), TODAY) == (
            date(2026, 8, 1), TODAY, CUMULATIVE_OK,
        )

    def test_missing_start_date_falls_back_to_the_earliest_queryable(self):
        """没填就只能从 CE 最早可查日兜底，并且必须标出来——

        这时的「累计消费」不保证是这个账号的全部消费，余额会偏高。
        """
        start, end, status = cumulative_range(None, TODAY)
        assert (start, end) == (earliest_queryable(TODAY), TODAY)
        assert status == CUMULATIVE_MISSING

    def test_older_than_retention_is_clamped_and_flagged(self):
        start, end, status = cumulative_range(date(2010, 1, 1), TODAY)
        assert (start, end) == (earliest_queryable(TODAY), TODAY)
        assert status == CUMULATIVE_CLAMPED

    def test_a_future_start_date_degenerates_to_today(self):
        """台账被手改成未来日期时不能算出一个倒着的区间。"""
        assert cumulative_range(date(2099, 1, 1), TODAY) == (
            TODAY, TODAY, CUMULATIVE_FUTURE,
        )

    def test_exactly_at_the_boundary_is_not_clamped(self):
        edge = earliest_queryable(TODAY)
        assert cumulative_range(edge, TODAY) == (edge, TODAY, CUMULATIVE_OK)
