"""CloudWatch 时间窗口与粒度。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from bedrock_cost.windows import (
    DEFAULT_PERIOD,
    DEFAULT_WINDOW,
    MAX_POINTS,
    PERIODS,
    WINDOWS,
    MetricWindow,
    coarser,
    detect_window,
    fit_period,
    resolve_window,
)

NOW = datetime(2026, 8, 17, 13, 37, 42, tzinfo=timezone.utc)


class TestPeriods:
    def test_order_is_coarsening(self):
        seconds = [PERIODS[k][0] for k in ("1m", "5m", "1h", "1d")]
        assert seconds == sorted(seconds)

    def test_coarser_walks_up_then_stops(self):
        assert coarser("1m") == "5m"
        assert coarser("1h") == "1d"
        assert coarser("1d") is None

    def test_retention_grows_with_period(self):
        retentions = [PERIODS[k][2] for k in ("1m", "5m", "1h", "1d")]
        assert retentions == sorted(retentions)


class TestFitPeriod:
    def test_short_window_keeps_fine_granularity(self):
        start = NOW - timedelta(hours=2)
        assert fit_period(start, NOW, "1m") == ("1m", [])

    def test_beyond_one_minute_retention_bumps_up(self):
        """1 分钟数据只留 15 天，超了必须换档而不是查出空。"""
        start = NOW - timedelta(days=20)
        period, notes = fit_period(start, NOW, "1m")
        assert period != "1m"
        assert any("保留" in n for n in notes)

    def test_too_many_points_bumps_up(self):
        start = NOW - timedelta(days=10)  # 1 分钟 = 14400 点，远超上限
        period, notes = fit_period(start, NOW, "1m")
        assert (NOW - start).total_seconds() / PERIODS[period][0] <= MAX_POINTS
        assert notes

    def test_never_exceeds_max_points_whatever_the_ask(self):
        for days in (1, 7, 30, 90, 400):
            start = NOW - timedelta(days=days)
            period, _ = fit_period(start, NOW, "1m")
            assert (NOW - start).total_seconds() / PERIODS[period][0] <= MAX_POINTS + 1

    def test_unknown_period_falls_back(self):
        period, _ = fit_period(NOW - timedelta(hours=1), NOW, "nonsense")
        assert period in PERIODS

    def test_coarsest_period_reports_rather_than_loops(self):
        start = NOW - timedelta(days=900)  # 超过 1 天粒度的保留期
        period, notes = fit_period(start, NOW, "1d")
        assert period == "1d"
        assert any("查不到" in n for n in notes)


class TestResolveWindow:
    def test_defaults(self):
        window, notes = resolve_window({}, NOW)
        assert window.window_key == DEFAULT_WINDOW
        assert window.period_key in PERIODS
        assert notes == []

    @pytest.mark.parametrize("key", list(WINDOWS))
    def test_each_preset(self, key):
        window, _ = resolve_window({"win": key, "period": "1h"}, NOW)
        expected = WINDOWS[key][1]
        # 起止都对齐到 Period 边界，所以允许一个 Period 的误差
        assert abs((window.span - expected).total_seconds()) <= window.period

    def test_boundaries_are_period_aligned(self):
        window, _ = resolve_window({"win": "24h", "period": "1h"}, NOW)
        assert int(window.start.timestamp()) % window.period == 0
        assert int(window.end.timestamp()) % window.period == 0

    def test_end_alignment_drops_the_partial_bucket(self):
        """末桶必须是完整的，否则最后一个点会凭空塌下去。"""
        window, _ = resolve_window({"win": "24h", "period": "1h"}, NOW)
        assert window.end <= NOW
        assert window.end.minute == 0 and window.end.second == 0

    def test_absolute_range(self):
        window, _ = resolve_window(
            {"start": "2026-08-10T00:00", "end": "2026-08-11T00:00", "period": "1h"}, NOW
        )
        assert window.span == timedelta(days=1)
        assert window.window_key == ""

    def test_reversed_range_is_swapped(self):
        window, notes = resolve_window(
            {"start": "2026-08-12T00:00", "end": "2026-08-10T00:00", "period": "1h"}, NOW
        )
        assert window.start < window.end
        assert any("调换" in n for n in notes)

    def test_future_end_is_clamped(self):
        window, notes = resolve_window(
            {"start": "2026-08-16T00:00", "end": "2099-01-01T00:00", "period": "1h"}, NOW
        )
        assert window.end <= NOW
        assert any("现在" in n for n in notes)

    def test_garbage_falls_back_with_a_note(self):
        window, notes = resolve_window({"start": "abc", "end": "def"}, NOW)
        assert window.window_key == DEFAULT_WINDOW
        assert any("无法识别" in n for n in notes)

    def test_identical_start_and_end(self):
        window, notes = resolve_window(
            {"start": "2026-08-10T05:00", "end": "2026-08-10T05:00"}, NOW
        )
        assert window.span > timedelta(0)
        assert notes

    def test_point_count_matches_span(self):
        window, _ = resolve_window({"win": "24h", "period": "1h"}, NOW)
        assert window.point_count == 24

    def test_works_without_flask(self):
        assert resolve_window({"win": "6h"}, NOW)[0].span <= timedelta(hours=6, minutes=5)


class TestDetectWindow:
    @pytest.mark.parametrize("key", list(WINDOWS))
    def test_round_trips_from_the_preset_link(self, key):
        window, _ = resolve_window({"win": key, "period": "1h"}, NOW)
        assert detect_window(window, NOW) == key

    def test_absolute_range_matching_a_preset_is_still_detected(self):
        """参数改动即提交时表单只带 start/end，高亮得靠区间反查。"""
        start = (NOW - timedelta(hours=24)).replace(minute=0, second=0, microsecond=0)
        end = NOW.replace(minute=0, second=0, microsecond=0)
        window = MetricWindow(start=start, end=end, period_key="1h")
        assert detect_window(window, NOW) == "24h"

    def test_custom_range_detects_nothing(self):
        window = MetricWindow(
            start=datetime(2026, 3, 1, tzinfo=timezone.utc),
            end=datetime(2026, 3, 4, tzinfo=timezone.utc),
            period_key="1h",
        )
        assert detect_window(window, NOW) == ""
