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
        assert start == month_shift(TODAY, -CE_HISTORY_MONTHS)
        assert any("Cost Explorer" in n for n in notes)

    def test_works_without_a_request_context(self):
        """刻意不依赖 Flask 的 request，纯字典就能测。"""
        assert resolve_range({"start": "2026-08-10", "end": "2026-08-12"}, TODAY)[:2] == (
            date(2026, 8, 10),
            date(2026, 8, 12),
        )
