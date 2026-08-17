"""堆叠柱状图：色板约束、几何、无障碍属性。"""

from __future__ import annotations

import re
from datetime import date

import pytest

from bedrock_cost import chart
from bedrock_cost.usage_explorer import Series, UsageReport, build_buckets

START, END = date(2026, 8, 1), date(2026, 8, 17)


def make_report(series_values: list[list[float]], granularity: str = "daily") -> UsageReport:
    dates, labels = build_buckets(START, END, granularity)
    report = UsageReport(
        start=START, end=END, dimension="service", granularity=granularity,
        dates=dates, labels=labels,
    )
    for index, values in enumerate(series_values):
        padded = (values + [0.0] * len(dates))[: len(dates)]
        report.series.append(
            Series(name=f"svc-{index}", raw=list(padded), marked=list(padded), slot=index)
        )
    return report


class TestPalette:
    def test_eight_categorical_slots(self):
        assert len(chart.SERIES_COLORS) == 8
        assert len(set(chart.SERIES_COLORS)) == 8

    def test_other_is_not_a_categorical_slot(self):
        """「其他」必须用中性灰，不能占用第 9 个分类色。"""
        assert chart.OTHER_COLOR not in chart.SERIES_COLORS
        assert chart.color_for(-1) == chart.OTHER_COLOR

    def test_slots_are_assigned_in_fixed_order_never_generated(self):
        for index, expected in enumerate(chart.SERIES_COLORS):
            assert chart.color_for(index) == expected

    def test_all_colors_are_valid_hex(self):
        for color in [*chart.SERIES_COLORS, chart.OTHER_COLOR, chart.SURFACE, chart.GRID]:
            assert re.fullmatch(r"#[0-9a-f]{6}", color), color


class TestCompactMoney:
    @pytest.mark.parametrize(
        "value, expected",
        [
            (0, "$0"),
            (12.5, "$12.50"),
            (1500, "$1.5K"),
            (265576.82, "$265.6K"),
            (1_050_000, "$1.05M"),
            (2_500_000_000, "$2.50B"),
            (-1500, "-$1.5K"),
        ],
    )
    def test_formats(self, value, expected):
        assert chart.compact_money(value) == expected

    def test_honours_currency_symbol(self):
        assert chart.compact_money(1500, "￥") == "￥1.5K"


class TestNiceStep:
    @pytest.mark.parametrize("span", [1, 7, 99, 1000, 263602.68, 1e7])
    def test_step_covers_the_span_in_at_most_six_ticks(self, span):
        step = chart._nice_step(span, 4)
        assert step > 0
        assert span / step <= 6

    def test_step_is_a_round_number(self):
        step = chart._nice_step(263602.68, 4)
        mantissa = step / 10 ** len(str(int(step)).rstrip("0").replace(".", ""))
        assert str(step).rstrip("0").rstrip(".").lstrip("0").lstrip(".") != ""


class TestRender:
    def test_empty_report_renders_a_placeholder(self):
        rendered = chart.render_stacked_bars(make_report([]))
        assert rendered.empty is True
        assert "没有消费数据" in rendered.svg
        assert rendered.height >= chart.PLOT_H

    def test_all_zero_report_is_treated_as_empty(self):
        rendered = chart.render_stacked_bars(make_report([[0.0] * 17]))
        assert rendered.empty is True

    def test_basic_structure(self):
        rendered = chart.render_stacked_bars(make_report([[100.0] * 17, [50.0] * 17]))
        assert 'viewBox="0 0' in rendered.svg
        assert "aria-label=" in rendered.svg
        assert rendered.empty is False

    def test_rounded_data_end_uses_a_clip_per_column(self):
        """圆角必须落在整根柱子的轮廓上，否则顶端是细条时等于没加。"""
        report = make_report([[100.0] * 17, [0.01] * 17])
        rendered = chart.render_stacked_bars(report)
        columns_with_cost = sum(1 for v in report.column_totals if v > 0)
        assert rendered.svg.count("<clipPath") == columns_with_cost
        assert "<path d=" in rendered.svg

    def test_gridlines_are_solid_never_dashed(self):
        rendered = chart.render_stacked_bars(make_report([[100.0] * 17]))
        assert "dasharray" not in rendered.svg

    def test_bar_width_is_capped(self):
        """柱子不许填满整个 band，留白是设计的一部分。"""
        rendered = chart.render_stacked_bars(make_report([[100.0, 100.0, 100.0]]))
        widths = [float(w) for w in re.findall(r'<rect fill="[^"]+" x="[^"]+" y="[^"]+" width="([\d.]+)"', rendered.svg)]
        assert widths
        assert max(widths) <= chart.MAX_BAR_W

    def test_height_includes_the_x_axis_band(self):
        """容器高度要含轴标签，否则卡片里会出现一条小小的纵向滚动条。"""
        rendered = chart.render_stacked_bars(make_report([[100.0] * 17]))
        assert rendered.height == chart.PAD_T + chart.PLOT_H + chart.PAD_B
        assert chart.PAD_B >= 40

    def test_one_hit_area_per_bucket_and_keyboard_reachable(self):
        report = make_report([[100.0] * 17])
        rendered = chart.render_stacked_bars(report)
        assert rendered.svg.count('class="chart-hit"') == len(report.dates)
        assert rendered.svg.count('tabindex="0"') == len(report.dates)

    def test_exactly_one_direct_label(self):
        """只在最高那根柱子上标一处，不给每根都写数字。"""
        rendered = chart.render_stacked_bars(make_report([[10.0] * 16 + [900.0]]))
        assert rendered.svg.count('font-weight="600"') == 1

    def test_tooltip_payload_carries_both_amounts(self):
        report = make_report([[100.0] * 17, [50.0] * 17])
        rendered = chart.render_stacked_bars(report)
        assert len(rendered.tooltip) == len(report.dates)
        rows = [row for bucket in rendered.tooltip for row in bucket["rows"]]
        assert rows
        assert all({"name", "color", "raw", "marked"} <= set(row) for row in rows)

    def test_tooltip_rows_sorted_by_amount(self):
        report = make_report([[10.0] * 17, [500.0] * 17])
        rendered = chart.render_stacked_bars(report)
        amounts = [row["marked"] for row in rendered.tooltip[0]["rows"]]
        assert amounts == sorted(amounts, reverse=True)

    def test_wide_range_grows_the_canvas_instead_of_squeezing(self):
        """一年按日有 365 根柱子，画布变宽由容器横向滚动，不能把柱子压没。"""
        long_report = make_report([[1.0] * 17])
        long_dates, long_labels = build_buckets(date(2025, 9, 1), date(2026, 8, 17), "daily")
        long_report.dates, long_report.labels = long_dates, long_labels
        long_report.series[0].raw = [1.0] * len(long_dates)
        long_report.series[0].marked = [1.0] * len(long_dates)
        rendered = chart.render_stacked_bars(long_report)
        assert rendered.width > chart.IDEAL_W
        assert len(long_dates) > 300

    def test_x_labels_are_thinned_to_avoid_collisions(self):
        long_report = make_report([[1.0] * 17])
        long_dates, long_labels = build_buckets(date(2026, 1, 1), date(2026, 8, 17), "daily")
        long_report.dates, long_report.labels = long_dates, long_labels
        long_report.series[0].raw = [1.0] * len(long_dates)
        long_report.series[0].marked = [1.0] * len(long_dates)
        rendered = chart.render_stacked_bars(long_report)
        shown = rendered.svg.count('text-anchor="middle"')
        assert shown < len(long_dates)

    def test_escapes_series_and_label_text(self):
        report = make_report([[100.0] * 17])
        report.labels[0] = '<script>"&'
        rendered = chart.render_stacked_bars(report)
        assert "<script>" not in rendered.svg
        assert "&lt;script&gt;" in rendered.svg
