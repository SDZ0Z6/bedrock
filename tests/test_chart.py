"""堆叠柱状图：色板约束、几何、无障碍属性。"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone

import pytest

from bedrock_cost import chart
from bedrock_cost.cloudwatch_metrics import MetricSeries, UsageMetricsReport, build_grid
from bedrock_cost.usage_explorer import Series, UsageReport, build_buckets
from bedrock_cost.windows import MetricWindow

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


# ------------------------------------------------------------------ 折线图
NOW = datetime(2026, 8, 17, 12, 0, tzinfo=timezone.utc)


def make_lines(series_values: list[list[float]], hours: int = 12, period: str = "1h"):
    window = MetricWindow(start=NOW - timedelta(hours=hours), end=NOW, period_key=period)
    stamps, labels = build_grid(window)
    report = UsageMetricsReport(
        window=window, metric_key="invocations",
        regions=["us-east-1"], timestamps=stamps, labels=labels,
    )
    for index, values in enumerate(series_values):
        padded = (list(values) + [0.0] * len(stamps))[: len(stamps)]
        report.series.append(MetricSeries(name=f"model-{index}", values=padded, slot=index))
    return report


class TestRenderLines:
    def test_empty_placeholder(self):
        rendered = chart.render_lines(make_lines([]))
        assert rendered.empty is True
        assert "没有调用数据" in rendered.svg

    def test_all_zero_is_empty(self):
        rendered = chart.render_lines(make_lines([[0.0] * 12]))
        assert rendered.empty is True

    def test_one_path_per_series(self):
        rendered = chart.render_lines(make_lines([[5.0] * 12, [3.0] * 12, [1.0] * 12]))
        assert rendered.svg.count('<path d="M') == 3

    def test_lines_are_2px_round_and_unfilled(self):
        rendered = chart.render_lines(make_lines([[5.0] * 12]))
        assert 'stroke-width="2.0"' in rendered.svg
        assert 'stroke-linejoin="round"' in rendered.svg
        assert 'stroke-linecap="round"' in rendered.svg
        assert 'fill="none"' in rendered.svg

    def test_never_stacks_the_values(self):
        """折线是叠放不是堆叠：Y 轴上限取单条最大值，不是各条之和。"""
        rendered = chart.render_lines(make_lines([[100.0] * 12, [100.0] * 12]))
        # 两条都是 100，若被堆叠，刻度会到 200
        assert "200" not in rendered.svg or "100" in rendered.svg

    def test_markers_only_when_sparse(self):
        sparse = chart.render_lines(make_lines([[5.0] * 12], hours=12))
        dense = chart.render_lines(make_lines([[5.0] * 200], hours=200))
        assert sparse.svg.count("<circle") > dense.svg.count("<circle")

    def test_end_labels_only_for_four_or_fewer_series(self):
        few = chart.render_lines(make_lines([[5.0] * 12, [9.0] * 12]))
        many = chart.render_lines(make_lines([[float(i + 1)] * 12 for i in range(6)]))
        assert 'class="chart-endlabels"' in few.svg
        assert 'class="chart-endlabels"' not in many.svg

    @staticmethod
    def _endlabel_block(svg: str) -> str:
        start = svg.index('<g class="chart-endlabels">')
        return svg[start : svg.index("</g>", start)]

    def test_colliding_end_labels_are_dropped_not_stacked(self):
        """三条线末值一样时，硬挤开会让标签和线脱钩，宁可只留一个。"""
        rendered = chart.render_lines(make_lines([[10.0] * 12, [10.0] * 12, [10.0] * 12]))
        assert self._endlabel_block(rendered.svg).count("<text") == 1

    def test_separated_end_labels_are_all_kept(self):
        rendered = chart.render_lines(make_lines([[10.0] * 12, [500.0] * 12, [1000.0] * 12]))
        assert self._endlabel_block(rendered.svg).count("<text") == 3

    def test_single_y_axis_only(self):
        """绝不做双 Y 轴：两个刻度的对齐是任意的，会凭空造出相关性。"""
        rendered = chart.render_lines(make_lines([[5.0] * 12, [500000.0] * 12]))
        assert rendered.svg.count('text-anchor="end"') >= 1
        # 右侧不应出现第二组刻度文字
        assert rendered.svg.count('class="chart-grid"') == 1

    def test_gridlines_solid(self):
        assert "dasharray" not in chart.render_lines(make_lines([[5.0] * 12])).svg

    def test_crosshair_focus_dots_and_overlay(self):
        report = make_lines([[5.0] * 12, [3.0] * 12])
        rendered = chart.render_lines(report)
        assert rendered.svg.count('class="chart-crosshair"') == 1
        assert rendered.svg.count('class="chart-focus"') == len(report.series)
        assert 'class="chart-overlay"' in rendered.svg
        assert 'tabindex="0"' in rendered.svg  # 键盘可达

    def test_height_includes_x_axis_band(self):
        rendered = chart.render_lines(make_lines([[5.0] * 12]))
        assert rendered.height == chart.PAD_T + chart.PLOT_H + chart.PAD_B

    def test_no_horizontal_scroll_needed(self):
        """折线不像柱子，密了也不用加宽画布。"""
        rendered = chart.render_lines(make_lines([[5.0] * 500], hours=500))
        assert rendered.width == chart.IDEAL_W

    def test_x_labels_stay_inside_and_do_not_shift(self):
        rendered = chart.render_lines(make_lines([[5.0] * 168], hours=168))
        # 贴边的标签改锚点而不是挪 x，挪位会挤到邻居
        assert 'text-anchor="end"' in rendered.svg or 'text-anchor="start"' in rendered.svg

    def test_tooltip_payload_has_position_and_value(self):
        report = make_lines([[5.0] * 12, [3.0] * 12])
        rendered = chart.render_lines(report)
        assert len(rendered.tooltip) == len(report.timestamps)
        rows = rendered.tooltip[0]["rows"]
        assert all({"name", "color", "value", "y"} <= set(r) for r in rows)
        assert "x" in rendered.tooltip[0]

    def test_tooltip_rows_sorted_descending(self):
        rendered = chart.render_lines(make_lines([[1.0] * 12, [50.0] * 12]))
        values = [r["value"] for r in rendered.tooltip[0]["rows"]]
        assert values == sorted(values, reverse=True)

    def test_single_point_renders_a_dot(self):
        rendered = chart.render_lines(make_lines([[5.0]], hours=1, period="1h"))
        assert "<circle" in rendered.svg

    def test_escapes_labels(self):
        report = make_lines([[5.0] * 12])
        report.labels[0] = "<script>"
        rendered = chart.render_lines(report)
        assert "<script>" not in rendered.svg


# ------------------------------------------------------------ 2×2 小倍数图
from bedrock_cost.cloudwatch_metrics import REGIONS, RegionPanel  # noqa: E402


def make_panels(per_region: dict[str, list[list[float]]], hours: int = 12):
    """per_region: {区域: [每条序列的值]}，序列名/槽位在所有区域间保持一致。"""
    report = make_lines([], hours=hours)
    width = len(report.timestamps)
    names = [f"model-{i}" for i in range(max((len(v) for v in per_region.values()), default=0))]
    merged = {name: [0.0] * width for name in names}
    for region, series_values in per_region.items():
        panel = RegionPanel(region=region)
        for index, name in enumerate(names):
            values = list(series_values[index]) if index < len(series_values) else [0.0] * width
            values = (values + [0.0] * width)[:width]
            panel.series.append(MetricSeries(name=name, values=values, slot=index))
            for position, value in enumerate(values):
                merged[name][position] += value
        report.panels.append(panel)
    for index, name in enumerate(names):
        report.series.append(MetricSeries(name=name, values=merged[name], slot=index))
    return report


class TestSmallMultiples:
    def test_one_panel_per_region(self):
        report = make_panels({r: [[5.0] * 12] for r in REGIONS})
        panels = chart.render_small_multiples(report)
        assert [p.region for p in panels] == list(REGIONS)

    def test_all_panels_share_one_y_scale(self):
        """一个区大一个区小时，四张图的刻度文字必须完全相同。"""
        report = make_panels(
            {
                "us-east-1": [[1000.0] * 12],
                "us-east-2": [[10.0] * 12],
                "us-west-1": [[5.0] * 12],
                "us-west-2": [[1.0] * 12],
            }
        )
        panels = chart.render_small_multiples(report)
        ticks = [re.findall(r'font-size="10"[^>]*>([^<]+)</text>', p.svg)[:4] for p in panels]
        assert len({tuple(t) for t in ticks}) == 1

    def test_small_region_is_visibly_smaller(self):
        """共用刻度的意义：小区的线要真的更矮，而不是各自撑满。"""
        report = make_panels(
            {"us-east-1": [[1000.0] * 12], "us-east-2": [[10.0] * 12],
             "us-west-1": [[10.0] * 12], "us-west-2": [[10.0] * 12]}
        )
        panels = chart.render_small_multiples(report)
        def first_y(svg):
            return float(re.search(r'<path d="M[\d.]+,([\d.]+)', svg).group(1))
        # y 越小越靠上，大区的线必须明显更高
        assert first_y(panels[0].svg) < first_y(panels[1].svg) - 40

    def test_panel_carries_its_own_total_and_label(self):
        report = make_panels({r: [[5.0] * 12] for r in REGIONS})
        panels = chart.render_small_multiples(report)
        assert all(p.total > 0 for p in panels)
        assert all(p.region in p.label for p in panels)

    def test_region_without_traffic_says_so(self):
        report = make_panels(
            {"us-east-1": [[5.0] * 12], "us-east-2": [[0.0] * 12],
             "us-west-1": [[0.0] * 12], "us-west-2": [[0.0] * 12]}
        )
        panels = chart.render_small_multiples(report)
        assert panels[0].empty is False
        assert panels[1].empty is True
        assert "该区无调用" in panels[1].svg

    def test_flat_zero_series_is_not_drawn(self):
        """某个区没跑过某个模型时不画一条贴地的直线，否则底部一片糊。"""
        report = make_panels(
            {"us-east-1": [[5.0] * 12, [3.0] * 12], "us-east-2": [[5.0] * 12, [0.0] * 12],
             "us-west-1": [[5.0] * 12, [0.0] * 12], "us-west-2": [[5.0] * 12, [0.0] * 12]}
        )
        panels = chart.render_small_multiples(report)
        assert panels[0].svg.count('<path d="M') == 2
        assert panels[1].svg.count('<path d="M') == 1

    def test_each_panel_has_its_own_cursor_and_overlay(self):
        report = make_panels({r: [[5.0] * 12] for r in REGIONS})
        panels = chart.render_small_multiples(report)
        for panel in panels:
            assert panel.svg.count('class="chart-crosshair"') == 1
            assert panel.svg.count('class="chart-overlay"') == 1
            assert 'tabindex="0"' in panel.svg

    def test_no_end_labels_on_small_panels(self):
        report = make_panels({r: [[5.0] * 12] for r in REGIONS})
        panels = chart.render_small_multiples(report)
        assert all("chart-endlabels" not in p.svg for p in panels)

    def test_tooltip_payload_per_panel(self):
        report = make_panels({r: [[5.0] * 12] for r in REGIONS})
        panels = chart.render_small_multiples(report)
        for panel in panels:
            assert len(panel.tooltip) == len(report.timestamps)
            assert all({"label", "total", "x", "rows"} <= set(b) for b in panel.tooltip)

    def test_x_positions_identical_across_panels(self):
        """准线联动的前提：同一个时间点在四张图里 x 坐标相同。"""
        report = make_panels({r: [[5.0] * 12] for r in REGIONS})
        panels = chart.render_small_multiples(report)
        xs = {tuple(b["x"] for b in p.tooltip) for p in panels}
        assert len(xs) == 1

    def test_gridlines_solid(self):
        report = make_panels({r: [[5.0] * 12] for r in REGIONS})
        assert all("dasharray" not in p.svg for p in chart.render_small_multiples(report))

    def test_svg_scales_by_viewbox(self):
        report = make_panels({r: [[5.0] * 12] for r in REGIONS})
        panels = chart.render_small_multiples(report)
        assert all('viewBox="0 0' in p.svg and "panel-svg" in p.svg for p in panels)

    def test_no_panels_gives_no_output(self):
        assert chart.render_small_multiples(make_lines([])) == []
