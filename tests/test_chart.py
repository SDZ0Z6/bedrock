"""图表：色板约束、几何、无障碍属性，以及前端脚本要读的悬浮数据的形状。"""

from __future__ import annotations

import math
import re
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from bedrock_cost import chart
from bedrock_cost.cloudwatch_metrics import (
    REGIONS,
    MetricSeries,
    RegionPanel,
    UsageMetricsReport,
    build_grid,
)
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


def group(svg: str, name: str) -> str:
    """<g class="name"> 里面的内容（到第一个 </g> 为止，只用于不嵌套 <g> 的那几组）。"""
    start = svg.index(f'<g class="{name}"')
    return svg[start : svg.index("</g>", start)]


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

    @pytest.mark.parametrize("span", [0.3, 7, 99, 263602.68, 1e7])
    def test_step_is_a_round_number(self, span):
        """刻度落在 1 / 2 / 2.5 / 5 乘以 10 的整数次幂上。"""
        step = chart._nice_step(span, 4)
        mantissa = step / 10 ** math.floor(math.log10(step))
        assert round(mantissa, 9) in {1, 2, 2.5, 5}


class TestRender:
    def test_empty_report_renders_a_placeholder(self):
        rendered = chart.render_stacked_areas(make_report([]))
        assert rendered.empty is True
        assert "没有消费数据" in rendered.svg
        assert rendered.height >= chart.PLOT_H

    def test_all_zero_report_is_treated_as_empty(self):
        rendered = chart.render_stacked_areas(make_report([[0.0] * 17]))
        assert rendered.empty is True

    def test_basic_structure(self):
        rendered = chart.render_stacked_areas(make_report([[100.0] * 17, [50.0] * 17]))
        assert 'viewBox="0 0' in rendered.svg
        assert "aria-label=" in rendered.svg
        assert rendered.empty is False

    def test_one_area_band_per_series(self):
        """每条有数据的序列画一条面积带；全零的序列不画，免得在零线上留描边。"""
        report = make_report([[100.0] * 17, [50.0] * 17, [0.0] * 17])
        rendered = chart.render_stacked_areas(report)
        assert rendered.svg.count("<polygon") == 2

    def test_bands_are_stacked_not_overlaid(self):
        """第二条带的下沿必须压在第一条的上沿上，而不是各自从零线起画。

        两条等量序列：下面那条占 0~50%，上面那条占 50~100%。如果画错成各自
        从零开始，两条带会完全重合，图上只看得到一个颜色。
        """
        rendered = chart.render_stacked_areas(make_report([[100.0] * 17, [100.0] * 17]))
        polygons = re.findall(r'<polygon [^>]*points="([^"]+)"', rendered.svg)
        assert len(polygons) == 2
        # 每条带取第一个点的 y，上面那条应当更高（y 更小）
        first_y = [float(pts.split()[0].split(",")[1]) for pts in polygons]
        assert first_y[1] < first_y[0], "第二条带没有叠在第一条之上"

    def test_line_is_solid_and_fill_is_translucent(self):
        """线实色、面积半透明——分工不能反过来。

        识别信息由线承载，所以线必须用 SERIES_COLORS 里那个校验过 ≥3:1 的原值，
        不能带透明度；面积只负责体量感，淡一点更好读。反过来做的话，
        scripts/validate_palette.py 校验的就不是实际承载信息的那个元素了。
        线有多粗见 test_area_top_line_is_the_documented_1_8px。
        """
        rendered = chart.render_stacked_areas(make_report([[100.0] * 17, [50.0] * 17]))
        polygons = re.findall(r"<polygon [^>]*>", rendered.svg)
        polylines = re.findall(r"<polyline [^>]*>", rendered.svg)
        assert len(polygons) == 2 and len(polylines) == 2

        assert all(f'fill-opacity="{chart.AREA_FILL_OPACITY}"' in p for p in polygons)
        # 线上不许有任何透明度
        assert all("opacity" not in line for line in polylines)
        assert all(re.search(r'stroke-width="[\d.]+"', line) for line in polylines)

    def test_area_top_line_is_the_documented_1_8px(self):
        rendered = chart.render_stacked_areas(make_report([[100.0] * 17, [50.0] * 17]))
        widths = set(re.findall(r'<polyline [^>]*stroke-width="([^"]+)"', rendered.svg))
        assert widths == {"1.8"}

    def test_line_uses_the_validated_palette_colour(self):
        """线的颜色必须原样来自色板，不能自作主张调深调浅。"""
        rendered = chart.render_stacked_areas(make_report([[100.0] * 17, [50.0] * 17]))
        strokes = re.findall(r'<polyline [^>]*stroke="([^"]+)"', rendered.svg)
        assert strokes
        assert all(s in chart.SERIES_COLORS or s == chart.OTHER_COLOR for s in strokes)

    def test_single_bucket_draws_a_dot_not_a_line(self):
        """只有一个时间桶时没有「线」可言，得画个点，否则那一天什么都看不见。"""
        report = make_report([[100.0]], granularity="monthly")
        report.dates, report.labels = ["2026-08-01"], ["8月"]
        report.series[0].raw = report.series[0].marked = [100.0]
        rendered = chart.render_stacked_areas(report)
        assert "<circle" in rendered.svg
        assert "<polyline" not in rendered.svg

    def test_gridlines_are_solid_hairlines_with_a_darker_baseline(self):
        """虚线读起来像「预测」或「阈值」，不用：网格线一律是发丝实线，零线再深一档。"""
        rendered = chart.render_stacked_areas(make_report([[100.0] * 17]))
        lines = re.findall(r"<line [^>]*></line>", rendered.svg)
        assert len(lines) >= 2
        assert not any("dasharray" in line for line in lines)
        assert all('stroke-width="1"' in line for line in lines)
        # 零线最先画，用更深的那一档；其余都是 GRID
        assert f'stroke="{chart.BASELINE}"' in lines[0]
        assert all(f'stroke="{chart.GRID}"' in line for line in lines[1:])

    def test_height_includes_the_x_axis_band(self):
        """容器高度要含轴标签，否则卡片里会出现一条小小的纵向滚动条。"""
        rendered = chart.render_stacked_areas(make_report([[100.0] * 17]))
        assert rendered.height == chart.PAD_T + chart.PLOT_H + chart.PAD_B
        assert chart.PAD_B >= 40

    def test_one_hit_area_per_bucket_and_keyboard_reachable(self):
        report = make_report([[100.0] * 17])
        rendered = chart.render_stacked_areas(report)
        assert rendered.svg.count('class="chart-hit"') == len(report.dates)
        assert rendered.svg.count('tabindex="0"') == len(report.dates)

    def test_exactly_one_direct_label(self):
        """只在最高那根柱子上标一处，不给每根都写数字。"""
        rendered = chart.render_stacked_areas(make_report([[10.0] * 16 + [900.0]]))
        assert rendered.svg.count('font-weight="600"') == 1

    def test_tooltip_payload_carries_both_amounts(self):
        report = make_report([[100.0] * 17, [50.0] * 17])
        rendered = chart.render_stacked_areas(report)
        assert len(rendered.tooltip) == len(report.dates)
        rows = [row for bucket in rendered.tooltip for row in bucket["rows"]]
        assert rows
        assert all({"name", "color", "raw", "marked"} <= set(row) for row in rows)

    def test_tooltip_rows_sorted_by_amount(self):
        report = make_report([[10.0] * 17, [500.0] * 17])
        rendered = chart.render_stacked_areas(report)
        amounts = [row["marked"] for row in rendered.tooltip[0]["rows"]]
        assert amounts == sorted(amounts, reverse=True)

    def test_wide_range_grows_the_canvas_instead_of_squeezing(self):
        """一年按日有 365 根柱子，画布变宽由容器横向滚动，不能把柱子压没。"""
        long_report = make_report([[1.0] * 17])
        long_dates, long_labels = build_buckets(date(2025, 9, 1), date(2026, 8, 17), "daily")
        long_report.dates, long_report.labels = long_dates, long_labels
        long_report.series[0].raw = [1.0] * len(long_dates)
        long_report.series[0].marked = [1.0] * len(long_dates)
        rendered = chart.render_stacked_areas(long_report)
        assert rendered.width > chart.IDEAL_W
        assert len(long_dates) > 300

    def test_x_labels_are_thinned_to_avoid_collisions(self):
        long_report = make_report([[1.0] * 17])
        long_dates, long_labels = build_buckets(date(2026, 1, 1), date(2026, 8, 17), "daily")
        long_report.dates, long_report.labels = long_dates, long_labels
        long_report.series[0].raw = [1.0] * len(long_dates)
        long_report.series[0].marked = [1.0] * len(long_dates)
        rendered = chart.render_stacked_areas(long_report)
        shown = rendered.svg.count('text-anchor="middle"')
        assert shown < len(long_dates)

    def test_escapes_series_and_label_text(self):
        report = make_report([[100.0] * 17])
        report.labels[0] = '<script>"&'
        rendered = chart.render_stacked_areas(report)
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


def make_timed(count: int, period: int, start: datetime | None = None) -> SimpleNamespace:
    """时间戳落在本机时区整点上的报表：刻度挑哪些点按本机时区算，这样测出来的结果
    不随跑测试那台机器的时区变。"""
    start = start or datetime(2026, 8, 17).astimezone()   # 本机时区的 08-17 零点
    stamps = [start + timedelta(seconds=period * i) for i in range(count)]
    return SimpleNamespace(
        labels=[s.strftime("%m-%d %H:%M") for s in stamps], timestamps=stamps,
        series=[MetricSeries(name="model-0", values=[5.0] * count, slot=0)],
        metric_label="调用次数", unit="次", window=SimpleNamespace(period=period),
    )


def x_ticks(svg: str) -> list[tuple[float, str]]:
    """横轴刻度：(刻度线的 x, 刻度字)。"""
    block = group(svg, "chart-xaxis")
    xs = [float(x) for x in re.findall(r'<line x1="([\d.]+)"', block)]
    texts = re.findall(r">([^<]*)</text>", block)
    assert len(xs) == len(texts)
    return list(zip(xs, texts))


def line_path(svg: str, index: int = 0) -> str:
    return re.findall(r'<path class="chart-line"[^>]* d="([^"]+)"', svg)[index]


class TestRenderLines:
    def test_empty_placeholder(self):
        rendered = chart.render_lines(make_lines([]))
        assert rendered.empty is True
        assert "没有调用数据" in rendered.svg

    def test_all_zero_is_empty(self):
        rendered = chart.render_lines(make_lines([[0.0] * 12]))
        assert rendered.empty is True

    def test_one_line_and_one_fill_per_series(self):
        rendered = chart.render_lines(make_lines([[5.0] * 12, [3.0] * 12, [1.0] * 12]))
        assert rendered.svg.count('class="chart-line"') == 3
        assert rendered.svg.count('class="chart-area"') == 3

    def test_lines_are_2px_round_and_unfilled(self):
        rendered = chart.render_lines(make_lines([[5.0] * 12]))
        line = re.search(r'<path class="chart-line"[^>]*>', rendered.svg).group(0)
        assert 'stroke-width="2.0"' in line
        assert 'stroke-linejoin="round"' in line
        assert 'stroke-linecap="round"' in line
        assert 'fill="none"' in line

    def test_lines_animate_by_path_length(self):
        """pathLength="1"：进场「画出来」的动画按比例走，不用知道线有多长。"""
        rendered = chart.render_lines(make_lines([[5.0] * 12, [3.0] * 12]))
        lines = re.findall(r'<path class="chart-line"[^>]*>', rendered.svg)
        assert len(lines) == 2
        assert all('pathLength="1"' in line for line in lines)

    def test_smooth_curve_passes_through_every_point(self):
        report = make_lines([[5.0, 9.0, 2.0, 7.0, 7.0, 0.0, 3.0, 8.0, 1.0, 4.0, 6.0, 5.0]])
        rendered = chart.render_lines(report)
        path = line_path(rendered.svg)
        assert path.startswith("M")
        segments = re.findall(r"C([\d.]+),([\d.]+) ([\d.]+),([\d.]+) ([\d.]+),([\d.]+)", path)
        assert len(segments) == len(report.timestamps) - 1
        tip = rendered.tooltip
        ends = [(float(s[4]), float(s[5])) for s in segments]
        assert ends == list(zip(tip["x"][1:], tip["series"][0]["y"][1:]))

    def test_smoothing_never_overshoots_between_points(self):
        """单调样条：控制点不越过两端点的上下界——普通平滑会在 0 附近冲成负数，
        画出「负的调用次数」。"""
        report = make_lines([[0.0, 0.0, 10.0, 0.0, 0.0, 6.0, 7.0, 1.0, 9.0, 0.0, 2.0, 0.0]])
        rendered = chart.render_lines(report)
        path = line_path(rendered.svg)
        start = re.match(r"M([\d.]+),([\d.]+)", path)
        y_prev = float(start.group(2))
        for c1, c2, end in re.findall(r"C[\d.]+,([\d.]+) [\d.]+,([\d.]+) [\d.]+,([\d.]+)", path):
            low, high = sorted((y_prev, float(end)))
            assert low - 0.1 <= float(c1) <= high + 0.1
            assert low - 0.1 <= float(c2) <= high + 0.1
            y_prev = float(end)
        plot_bottom = chart.PAD_T + chart.PLOT_H
        assert max(float(y) for y in re.findall(r",([\d.]+)", path)) <= plot_bottom + 0.05

    def test_too_many_points_fall_back_to_straight_segments(self):
        """点多到那个密度时曲线看不出区别，HTML 却要大好几倍。"""
        count = chart.SMOOTH_MAX_POINTS + 1
        rendered = chart.render_lines(make_lines([[5.0] * count], hours=count))
        path = line_path(rendered.svg)
        assert "C" not in path
        assert path.count(" L") == count - 1

    def test_each_line_gets_a_fading_gradient_fill(self):
        """线下的面积用渐变而不是平涂：几条线的填充叠在一起时，重叠发生在各自已经很淡的地方。"""
        report = make_lines([[5.0] * 12, [3.0] * 12])
        rendered = chart.render_lines(report, uid="cost")
        ids = re.findall(r'<linearGradient id="([^"]+)"', rendered.svg)
        assert ids == [f"cost-fade{s.slot}" for s in report.series]
        fills = re.findall(r'<path class="chart-area"[^>]*fill="url\(#([^)]+)\)"', rendered.svg)
        assert fills == ids
        # 贴着线最浓，往下收到全透明——收不到 0 就等于换了个写法的平涂
        stops = re.findall(r'stop-opacity="([^"]+)"', rendered.svg)
        assert stops == [str(chart.AREA_TOP_OPACITY), "0"] * len(report.series)

    def test_fills_are_drawn_before_any_line(self):
        """后一条的填充不能盖在前一条的线上。"""
        svg = chart.render_lines(make_lines([[5.0] * 12, [3.0] * 12])).svg
        assert svg.rindex('class="chart-area"') < svg.index('class="chart-line"')

    def test_gradient_ids_differ_between_charts_on_one_page(self):
        """SVG 的 id 是全文档共享的：同一页上几张图只用色槽编号做 id 会互相覆盖。"""
        report = make_lines([[5.0] * 12])
        first = re.findall(r'<linearGradient id="([^"]+)"', chart.render_lines(report, uid="a").svg)
        second = re.findall(r'<linearGradient id="([^"]+)"', chart.render_lines(report, uid="b").svg)
        assert first and second
        assert set(first).isdisjoint(second)

    def test_uid_is_escaped(self):
        rendered = chart.render_lines(make_lines([[5.0] * 12]), uid='x"><script>')
        assert "<script>" not in rendered.svg

    def test_never_stacks_the_values(self):
        """折线是叠放不是堆叠：Y 轴上限取单条最大值，不是各条之和。"""
        rendered = chart.render_lines(make_lines([[100.0] * 12, [100.0] * 12]))
        ticks = re.findall(r">([^<]+)</text>", group(rendered.svg, "chart-grid"))
        assert ticks[-1] == "100"   # 堆叠的话会到 200

    def test_markers_only_when_sparse(self):
        sparse = chart.render_lines(make_lines([[5.0] * 12], hours=12))
        dense = chart.render_lines(make_lines([[5.0] * 200], hours=200))
        assert sparse.svg.count('class="chart-marker"') == 12
        assert dense.svg.count('class="chart-marker"') == 0

    def test_end_labels_only_for_four_or_fewer_series(self):
        few = chart.render_lines(make_lines([[5.0] * 12, [9.0] * 12]))
        many = chart.render_lines(make_lines([[float(i + 1)] * 12 for i in range(6)]))
        assert 'class="chart-endlabels"' in few.svg
        assert 'class="chart-endlabels"' not in many.svg

    def test_colliding_end_labels_are_dropped_not_stacked(self):
        """三条线末值一样时，硬挤开会让标签和线脱钩，宁可只留一个。"""
        rendered = chart.render_lines(make_lines([[10.0] * 12, [10.0] * 12, [10.0] * 12]))
        assert group(rendered.svg, "chart-endlabels").count("<text") == 1

    def test_separated_end_labels_are_all_kept(self):
        rendered = chart.render_lines(make_lines([[10.0] * 12, [500.0] * 12, [1000.0] * 12]))
        assert group(rendered.svg, "chart-endlabels").count("<text") == 3

    def test_single_y_axis_only(self):
        """绝不做双 Y 轴：两个刻度的对齐是任意的，会凭空造出相关性。"""
        rendered = chart.render_lines(make_lines([[5.0] * 12, [500000.0] * 12]))
        assert rendered.svg.count('text-anchor="end"') >= 1
        # 右侧不应出现第二组刻度文字
        assert rendered.svg.count('class="chart-grid"') == 1

    def test_gridlines_are_solid_only_the_crosshair_is_dashed(self):
        """网格是发丝实线（零线深一档）；唯一的虚线是悬浮时那条准线。"""
        svg = chart.render_lines(make_lines([[5.0] * 12])).svg
        grid = re.findall(r"<line [^>]*>", group(svg, "chart-grid"))
        assert len(grid) >= 2
        assert not any("dasharray" in line for line in grid)
        assert f'stroke="{chart.BASELINE}"' in grid[0]
        assert all(f'stroke="{chart.GRID}"' in line for line in grid[1:])
        assert svg.count("stroke-dasharray") == 1
        assert 'stroke-dasharray="4 4"' in group(svg, "chart-cross")

    def test_crosshair_focus_dots_and_overlay(self):
        report = make_lines([[5.0] * 12, [3.0] * 12])
        svg = chart.render_lines(report).svg
        assert svg.count('class="chart-cross"') == 1
        assert svg.count('class="chart-focus"') == len(report.series)
        assert '<g class="chart-cursor" aria-hidden="true">' in svg
        overlay = re.search(r'<rect class="chart-overlay"[^>]*>', svg).group(0)
        assert 'tabindex="0"' in overlay   # 键盘可达
        # 一整块盖住绘图区，最后画，压在所有东西上面接鼠标
        assert f'x="{chart.PAD_L}"' in overlay and f'y="{chart.PAD_T}"' in overlay
        assert f'height="{chart.PLOT_H}"' in overlay
        assert svg.index('class="chart-overlay"') > svg.index('class="chart-cursor"')

    def test_crosshair_points_at_the_axis(self):
        """准线竖贯绘图区，底下带个小三角指着横轴。"""
        svg = chart.render_lines(make_lines([[5.0] * 12])).svg
        cross = group(svg, "chart-cross")
        bottom = chart.PAD_T + chart.PLOT_H
        assert f'y1="{chart.PAD_T}" x2="0" y2="{bottom}"' in cross
        assert "<path" in cross

    def test_focus_dots_pair_up_with_the_tooltip_series(self):
        """app.js 按下标把第 i 个点对到 tooltip.series[i]，两边顺序必须一致。"""
        report = make_lines([[5.0] * 12, [3.0] * 12, [1.0] * 12])
        rendered = chart.render_lines(report)
        colours = re.findall(r'<circle class="chart-focus"[^>]*fill="([^"]+)"', rendered.svg)
        assert colours == [entry["color"] for entry in rendered.tooltip["series"]]

    def test_tooltip_payload_is_compact(self):
        """名字和颜色每条序列只写一次，按时间点只放值和纵坐标（一周按小时 168 个点、
        五个视图，每个点都重复写名字和颜色的话光这份 JSON 就有几百 KB）。"""
        report = make_lines([[5.0] * 12, [3.0] * 12])
        tip = chart.render_lines(report, unit="次").tooltip
        assert set(tip) == {"unit", "labels", "x", "series"}
        assert tip["unit"] == "次"
        assert tip["labels"] == report.labels
        assert len(tip["x"]) == len(report.timestamps)
        assert tip["x"] == sorted(tip["x"])
        assert len(tip["series"]) == len(report.series)
        for entry, series in zip(tip["series"], report.series):
            assert set(entry) == {"name", "color", "v", "y"}
            assert entry["name"] == series.name
            assert entry["color"] == chart.color_for(series.slot)
            assert entry["v"] == series.values
            assert len(entry["y"]) == len(series.values)

    def test_tooltip_unit_defaults_to_the_report(self):
        report = make_lines([[5.0] * 12])
        assert chart.render_lines(report).tooltip["unit"] == report.unit

    def test_tooltip_y_follows_the_value(self):
        report = make_lines([[0.0, 2.0, 4.0] + [1.0] * 9])
        tip = chart.render_lines(report).tooltip
        y = tip["series"][0]["y"]
        assert y[0] == chart.PAD_T + chart.PLOT_H   # 0 落在零线上
        assert y[2] < y[1] < y[0]                     # 数越大越靠上

    def test_tooltip_values_are_rounded(self):
        report = make_lines([[1.23456] * 12])
        assert chart.render_lines(report).tooltip["series"][0]["v"][0] == 1.23

    def test_height_includes_x_axis_band(self):
        rendered = chart.render_lines(make_lines([[5.0] * 12]))
        assert rendered.height == chart.PAD_T + chart.PLOT_H + chart.PAD_B

    def test_no_horizontal_scroll_needed(self):
        """折线不像柱子，密了也不用加宽画布。"""
        rendered = chart.render_lines(make_lines([[5.0] * 500], hours=500))
        assert rendered.width == chart.IDEAL_W

    def test_width_sets_the_viewbox(self):
        rendered = chart.render_lines(make_lines([[5.0] * 12]), width=640)
        assert rendered.width == 640
        assert 'viewBox="0 0 640 ' in rendered.svg

    def test_x_labels_stay_inside_and_do_not_shift(self):
        rendered = chart.render_lines(make_lines([[5.0] * 168], hours=168))
        # 贴边的标签改锚点而不是挪 x，挪位会挤到邻居
        assert 'text-anchor="end"' in rendered.svg or 'text-anchor="start"' in rendered.svg

    def test_single_point_renders_a_dot(self):
        rendered = chart.render_lines(make_lines([[5.0]], hours=1, period="1h"))
        assert "<circle" in rendered.svg
        assert 'class="chart-line"' not in rendered.svg
        assert "<linearGradient" not in rendered.svg
        assert len(rendered.tooltip["x"]) == 1

    def test_escapes_labels(self):
        report = make_lines([[5.0] * 12])
        report.labels[0] = "<script>"
        rendered = chart.render_lines(report)
        assert "<script>" not in rendered.svg


class TestTimeTicks:
    """横轴按时间挑刻度：落在整点 / 整天上，零点那一格写日期，其余写时刻。"""

    def test_hourly_ticks_land_on_round_hours(self):
        rendered = chart.render_lines(make_timed(48, 3600))
        labels = [text for _, text in x_ticks(rendered.svg)]
        assert labels == ["08-17", "06:00", "12:00", "18:00", "08-18", "06:00", "12:00", "18:00"]

    def test_a_week_hourly_is_one_tick_per_midnight(self):
        rendered = chart.render_lines(make_timed(168, 3600))
        labels = [text for _, text in x_ticks(rendered.svg)]
        assert labels == ["08-17", "08-18", "08-19", "08-20", "08-21", "08-22", "08-23"]

    @pytest.mark.parametrize(
        "hours, period",
        [(12, "1h"), (24, "1h"), (48, "1h"), (168, "1h"), (720, "1h"), (24, "5m"), (72, "1m"),
         (24 * 60, "1d"), (24 * 120, "1d")],
    )
    def test_ticks_are_at_least_the_minimum_gap_apart(self, hours, period):
        rendered = chart.render_lines(make_lines([[5.0] * 5000], hours=hours, period=period))
        xs = [x for x, _ in x_ticks(rendered.svg)]
        assert len(xs) >= 2
        assert min(b - a for a, b in zip(xs, xs[1:])) >= chart.TICK_MIN_GAP - 0.01

    def test_a_year_daily_still_keeps_the_minimum_gap(self):
        rendered = chart.render_lines(make_lines([[5.0] * 400], hours=24 * 365, period="1d"))
        xs = [x for x, _ in x_ticks(rendered.svg)]
        assert min(b - a for a, b in zip(xs, xs[1:])) >= chart.TICK_MIN_GAP - 0.01

    def test_daily_points_get_dates(self):
        """按天的点不一定在本地零点（AWS 按 UTC 零点切天），每天那一个点都认。"""
        report = make_timed(10, 86400, start=datetime(2026, 8, 17, 8, 0).astimezone())
        ticks = x_ticks(chart.render_lines(report).svg)
        assert len(ticks) >= 2
        days = [datetime.strptime(f"2026-{text}", "%Y-%m-%d").date() for _, text in ticks]
        steps = {(b - a).days for a, b in zip(days, days[1:])}
        assert len(steps) == 1 and steps.pop() >= 1

    def test_tick_marks_hang_below_the_baseline(self):
        rendered = chart.render_lines(make_timed(48, 3600))
        bottom = chart.PAD_T + chart.PLOT_H
        marks = re.findall(r"<line [^>]*>", group(rendered.svg, "chart-xaxis"))
        assert marks
        assert all(f'y1="{bottom}"' in m and f'y2="{bottom + 4}"' in m for m in marks)

    def test_without_timestamps_falls_back_to_a_stride(self):
        report = make_timed(48, 3600)
        report.timestamps = None
        rendered = chart.render_lines(report)
        ticks = x_ticks(rendered.svg)
        assert ticks[0][1] == report.labels[0]
        xs = [x for x, _ in ticks]
        assert min(b - a for a, b in zip(xs, xs[1:])) >= chart.TICK_MIN_GAP - 0.01

    def test_single_point_gets_one_full_label(self):
        report = make_timed(1, 3600)
        ticks = x_ticks(chart.render_lines(report).svg)
        assert [text for _, text in ticks] == ["08-17 00:00"]


# ------------------------------------------------------------ 模型用量的主图
def make_regions(per_region: dict[str, list[list[float]]], hours: int = 12) -> UsageMetricsReport:
    """per_region: {区域: [每条序列的值]}。四个区都有面板（没数据的补零），序列名和色槽
    在各区之间一致——和 build_metrics 出来的一样。"""
    report = make_lines([], hours=hours)
    width = len(report.timestamps)
    names = [f"model-{i}" for i in range(max((len(v) for v in per_region.values()), default=0))]
    merged = {name: [0.0] * width for name in names}
    for region in REGIONS:
        panel = RegionPanel(region=region)
        series_values = per_region.get(region, [])
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


class TestRenderViews:
    def test_combined_view_first_then_regions_by_volume(self):
        report = make_regions(
            {"us-east-1": [[1.0] * 12], "us-east-2": [[100.0] * 12], "us-west-2": [[10.0] * 12]}
        )
        views = chart.render_views(report)
        assert [v.key for v in views] == ["all", "us-east-2", "us-west-2", "us-east-1", "us-west-1"]
        combined = views[0]
        assert (combined.label, combined.tab_label, combined.region) == ("四区合计", "四区合计", "")
        east2 = views[1]
        assert (east2.label, east2.tab_label, east2.region) == ("俄亥俄 us-east-2", "us-east-2", "us-east-2")

    def test_quiet_region_goes_last_and_is_empty(self):
        report = make_regions({"us-east-1": [[5.0] * 12]})
        views = chart.render_views(report)
        assert views[1].key == "us-east-1"
        for quiet in views[2:]:
            assert quiet.empty is True and quiet.chart.empty is True
            assert quiet.series == []
            assert quiet.share == 0.0 and quiet.average == 0.0
            assert quiet.peak_value == 0.0 and quiet.peak_label == ""

    def test_totals_shares_and_averages(self):
        report = make_regions({"us-east-1": [[1.0] * 12], "us-east-2": [[3.0] * 12]})
        views = {v.key: v for v in chart.render_views(report)}
        assert views["all"].total == 48.0 and views["all"].share == 100.0
        assert views["us-east-1"].share == 25.0 and views["us-east-2"].share == 75.0
        assert views["all"].average == 4.0 and views["us-east-2"].average == 3.0
        assert views["all"].empty is False

    def test_peak_is_the_busiest_point_across_models(self):
        first, second = [1.0] * 12, [1.0] * 12
        first[3], second[5] = 9.0, 9.5
        report = make_regions({"us-east-1": [first, second]})
        view = chart.render_views(report)[0]
        assert view.peak_value == 10.5
        assert view.peak_label == report.labels[5]

    def test_colours_follow_the_model_not_the_rank(self):
        """某个区里没有的模型只是不画，剩下的不换色。"""
        report = make_regions(
            {"us-east-1": [[5.0] * 12, [3.0] * 12], "us-east-2": [[0.0] * 12, [3.0] * 12]}
        )
        east2 = {v.key: v for v in chart.render_views(report)}["us-east-2"]
        assert [(s.name, s.slot) for s in east2.series] == [("model-1", 1)]
        assert re.findall(r'<linearGradient id="([^"]+)"', east2.chart.svg) == ["v-us-east-2-fade1"]
        assert east2.chart.tooltip["series"][0]["color"] == chart.color_for(1)

    def test_each_view_has_its_own_gradient_ids(self):
        report = make_regions({region: [[5.0] * 12] for region in REGIONS})
        views = chart.render_views(report)
        ids = [i for v in views for i in re.findall(r'<linearGradient id="([^"]+)"', v.chart.svg)]
        assert len(ids) == len(views) == 5
        assert len(set(ids)) == 5

    def test_each_view_scales_to_its_own_data(self):
        """视图之间不并排出现，各用各的纵轴：共用刻度只会把小区压成一条贴地的线。"""
        report = make_regions({"us-east-1": [[1000.0] * 12], "us-east-2": [[10.0] * 12]})
        views = {v.key: v for v in chart.render_views(report)}
        big = views["us-east-1"].chart.tooltip["series"][0]["y"][0]
        small = views["us-east-2"].chart.tooltip["series"][0]["y"][0]
        assert small == big

    def test_width_passes_through(self):
        report = make_regions({"us-east-1": [[5.0] * 12]})
        assert all(v.chart.width == chart.VIEW_W for v in chart.render_views(report))
        assert all(v.chart.width == 820 for v in chart.render_views(report, width=820))

    def test_report_without_panels_has_only_the_combined_view(self):
        views = chart.render_views(make_lines([[5.0] * 12]))
        assert [v.key for v in views] == ["all"]
        assert views[0].total == 60.0

    def test_empty_report(self):
        views = chart.render_views(make_lines([]))
        assert len(views) == 1
        assert views[0].empty and views[0].chart.empty
        assert views[0].peak_label == "" and views[0].share == 0.0


# ------------------------------------------------------------ 仪表盘上的小图
_ARC = re.compile(r"M(-?[\d.]+),(-?[\d.]+) A([\d.]+),[\d.]+ 0 ([01]) 1 (-?[\d.]+),(-?[\d.]+)")


def arcs(svg: str) -> list[SimpleNamespace]:
    """仪表里画弧的 path，按画的顺序：第一条是底槽，后面是用量那一段。只认弧的几何
    （起点、半径、大弧标记、终点），不管动效之类的其余标记。"""
    found = []
    for tag in re.findall(r"<path\b[^>]*>", svg):
        d = re.search(r'\bd="([^"]+)"', tag)
        match = _ARC.search(d.group(1)) if d else None
        if match:
            x0, y0, r, large, x1, y1 = match.groups()
            found.append(SimpleNamespace(tag=tag, start=(float(x0), float(y0)), r=float(r),
                                         large=large, end=(float(x1), float(y1))))
    return found


def point_at(track: SimpleNamespace, degrees: float) -> tuple[float, float]:
    """底槽那个圆上某个角度（0 = 正右方，顺时针为正）的点。圆心从底槽的起点反推。"""
    start = math.radians(chart.GAUGE_START)
    cx, cy = track.start[0] - track.r * math.cos(start), track.start[1] - track.r * math.sin(start)
    return cx + track.r * math.cos(math.radians(degrees)), cy + track.r * math.sin(math.radians(degrees))


class TestGauge:
    """只测「用了多少 → 弧画到哪」，动效、端点小圆这些标记怎么写不管。"""

    @pytest.mark.parametrize("fraction", [None, 0, -0.3])
    def test_nothing_used_draws_only_the_track(self, fraction):
        assert len(arcs(chart.render_gauge(fraction))) == 1

    def test_track_spans_the_whole_sweep(self):
        (track,) = arcs(chart.render_gauge(None))
        assert track.start == pytest.approx(point_at(track, chart.GAUGE_START), abs=0.02)
        assert track.end == pytest.approx(point_at(track, chart.GAUGE_START + chart.GAUGE_SWEEP), abs=0.02)
        assert track.large == "1"

    @pytest.mark.parametrize("fraction", [0.1, 0.25, 0.5, 0.73, 0.9, 1.0])
    def test_value_arc_ends_at_the_fraction(self, fraction):
        track, value = arcs(chart.render_gauge(fraction))
        assert value.start == track.start and value.r == track.r
        expected = point_at(track, chart.GAUGE_START + chart.GAUGE_SWEEP * fraction)
        assert value.end == pytest.approx(expected, abs=0.02)

    def test_over_the_limit_is_drawn_full(self):
        """超过 1 按满格画，超了多少由文字说。"""
        track, value = arcs(chart.render_gauge(1.7, "danger"))
        assert value.end == track.end

    @pytest.mark.parametrize("fraction, large", [(0.5, "0"), (0.9, "1")])
    def test_large_arc_flag(self, fraction, large):
        """弧扫过 180° 以上时要用大弧，否则 SVG 会画成另一侧的小弧。"""
        assert arcs(chart.render_gauge(fraction))[1].large == large

    def test_tiny_usage_is_still_visible(self):
        assert len(arcs(chart.render_gauge(0.0001))) == 2

    @pytest.mark.parametrize("tone", ["ok", "warn", "danger"])
    def test_track_and_value_share_the_tone_colour(self, tone):
        """规范里的 meter：填充按严重程度上色，底槽是同一色相的浅一档。"""
        track, value = arcs(chart.render_gauge(0.5, tone))
        colour = chart.TONE_COLORS[tone]
        assert f'stroke="{colour}"' in track.tag and f'stroke="{colour}"' in value.tag
        others = [c for name, c in chart.TONE_COLORS.items() if name != tone]
        assert not any(c in arc.tag for arc in (track, value) for c in others)

    def test_unknown_tone_falls_back_to_ok(self):
        assert f'stroke="{chart.TONE_COLORS["ok"]}"' in arcs(chart.render_gauge(0.5, "nonsense"))[1].tag

    def test_fits_the_viewbox(self):
        svg = chart.render_gauge(0.75, width=280, height=172)
        assert 'viewBox="0 0 280 172"' in svg
        for arc in arcs(svg):
            for x, y in (arc.start, arc.end):
                assert 0 <= x <= 280 and 0 <= y <= 172


def bubbles(svg: str) -> list[tuple[float, float, float]]:
    return [
        (float(x), float(y), float(r))
        for x, y, r in re.findall(r'<circle cx="([\d.]+)" cy="([\d.]+)" r="([\d.]+)"', svg)
    ]


ITEMS = [
    ("opus", 500.0, "#3987e5"), ("sonnet", 320.0, "#d95926"), ("haiku", 120.0, "#199e70"),
    ("fable", 60.0, "#c48200"), ("a", 40.0, "#d55181"), ("b", 25.0, "#008300"),
    ("c", 9.0, "#9384e0"), ("d", 3.0, "#e66767"), ("其他", 1.0, chart.OTHER_COLOR),
]


class TestBubbles:
    def test_empty(self):
        for items in ([], [("a", 0.0, "#3987e5"), ("b", -5.0, "#d95926")]):
            svg = chart.render_bubbles(items)
            assert "没有数据" in svg
            assert 'class="bubble"' not in svg

    def test_one_bubble_per_positive_item_biggest_first(self):
        svg = chart.render_bubbles([("small", 1.0, "#111111"), ("zero", 0.0, "#222222"), ("big", 3.0, "#333333")])
        assert re.findall(r'data-name="([^"]+)"', svg) == ["big", "small"]

    def test_data_attributes(self):
        svg = chart.render_bubbles(
            [("a<b>", 990.0, "#111111"), ("tiny", 5.0, "#222222"), ("mid", 5.0, "#333333")],
            fmt=lambda v: f"${v:,.0f}",
        )
        names = re.findall(r'data-name="([^"]+)"', svg)
        assert names[0] == "a&lt;b&gt;"
        assert re.findall(r'data-value="([^"]+)"', svg) == ["$990", "$5", "$5"]
        assert re.findall(r'data-share="([^"]+)"', svg) == ["99%", "<1%", "<1%"]
        assert "<b>" not in svg

    def test_default_format_is_compact(self):
        svg = chart.render_bubbles([("a", 46_200.0, "#111111")])
        assert 'data-value="46.2K"' in svg and 'data-share="100%"' in svg

    @pytest.mark.parametrize("count", [2, 3, 5, 9])
    def test_packing_has_no_overlaps(self, count):
        circles = bubbles(chart.render_bubbles(ITEMS[:count]))
        assert len(circles) == count
        for i in range(count):
            for j in range(i + 1, count):
                (x1, y1, r1), (x2, y2, r2) = circles[i], circles[j]
                assert math.hypot(x1 - x2, y1 - y2) >= r1 + r2 - 0.02, (i, j)

    def test_equal_values_do_not_overlap_either(self):
        circles = bubbles(chart.render_bubbles([(f"m{i}", 10.0, "#3987e5") for i in range(7)]))
        for i, (x1, y1, r1) in enumerate(circles):
            for x2, y2, r2 in circles[i + 1 :]:
                assert math.hypot(x1 - x2, y1 - y2) >= r1 + r2 - 0.02

    def test_area_is_proportional_to_the_value(self):
        circles = bubbles(chart.render_bubbles(ITEMS))
        biggest = circles[0][2] ** 2
        for (_, value, _), (_, _, r) in zip(ITEMS, circles):
            assert r * r / biggest == pytest.approx(value / ITEMS[0][1], rel=0.01)

    def test_stays_inside_the_canvas(self):
        circles = bubbles(chart.render_bubbles(ITEMS, width=250, height=210))
        for x, y, r in circles:
            assert x - r >= -0.01 and x + r <= 250.01
            assert y - r >= -0.01 and y + r <= 210.01

    def test_only_bubbles_big_enough_get_a_label(self):
        svg = chart.render_bubbles(ITEMS)
        labelled = sum(1 for _, _, r in bubbles(svg) if r >= 15)
        assert 0 < labelled < len(ITEMS)
        assert svg.count("<text") == labelled

    def test_ink_text_on_a_tinted_fill(self):
        """浅底上的彩色字不够清楚，字一律用墨色；填充是同色的浅底。"""
        svg = chart.render_bubbles(ITEMS[:3])
        assert set(re.findall(r'<text [^>]*fill="([^"]+)"', svg)) == {chart.LABEL_TEXT}
        fills = re.findall(r'<circle [^>]*fill="([^"]+)" fill-opacity="0.2"', svg)
        assert fills == [colour for _, _, colour in ITEMS[:3]]

    def test_each_bubble_has_its_own_rhythm_and_drag_layers(self):
        svg = chart.render_bubbles(ITEMS[:4])
        styles = re.findall(r'style="--i:(\d+);--dur:([\d.]+)s;--delay:(-?[\d.]+)s"', svg)
        assert [int(i) for i, _, _ in styles] == [0, 1, 2, 3]
        assert len({dur for _, dur, _ in styles}) > 1
        assert svg.count('<g class="bubble-drag"><g class="bubble-pop">') == 4

    def test_keyboard_and_screen_reader(self):
        svg = chart.render_bubbles([("opus", 3.0, "#111111"), ("sonnet", 1.0, "#222222")])
        assert svg.count('tabindex="0"') == 2
        assert 'aria-label="opus 3，占 75%"' in svg


def bar(name: str, values: list[float], slot: int = 0, **extra) -> SimpleNamespace:
    return SimpleNamespace(name=name, slot=slot, values=values, **extra)


def segments(svg: str, column: int) -> list[tuple[str, str]]:
    """某一格里画出来的段：(data-s, 标签名)，从下往上。"""
    start = svg.index(f'<g class="bar-col" data-idx="{column}"')
    block = svg[start : svg.index("</g>", start)]
    return re.findall(r'<(rect|path) data-s="(\d+)"', block)


class TestBars:
    def test_bigger_series_is_stacked_at_the_bottom(self):
        rendered = chart.render_bars(
            ["a", "b"], [bar("small", [1.0, 1.0], slot=0), bar("big", [5.0, 5.0], slot=1)]
        )
        assert [item["name"] for item in rendered.legend] == ["big", "small"]
        # 每格从下往上：big（序号 0）是方角的 rect，最上面的 small 是圆顶的 path
        assert segments(rendered.svg, 0) == [("rect", "0"), ("path", "1")]

    def test_bottom_segment_sits_on_the_baseline_and_leaves_a_gap(self):
        rendered = chart.render_bars(["a"], [bar("big", [5.0]), bar("small", [1.0], slot=1)], height=196)
        plot_bottom = 196 - 26
        rect = re.search(r'<rect data-s="0" x="[\d.]+" y="([\d.]+)" width="[\d.]+" height="([\d.]+)"', rendered.svg)
        top, height = float(rect.group(1)), float(rect.group(2))
        assert top + height == pytest.approx(plot_bottom, abs=0.02)
        upper = re.search(r'<path data-s="1" d="M[\d.]+,([\d.]+)', rendered.svg)
        # 上面那段从下面那段的顶上起画，中间让出 BAR_GAP 的缝
        assert top - float(upper.group(1)) == pytest.approx(chart.BAR_GAP, abs=0.02)

    def test_zero_cells_draw_nothing(self):
        rendered = chart.render_bars(["a", "b"], [bar("x", [3.0, 0.0])])
        assert segments(rendered.svg, 0) == [("path", "0")]
        assert segments(rendered.svg, 1) == []

    def test_bars_are_never_wider_than_the_cap(self):
        rendered = chart.render_bars(["a", "b"], [bar("x", [3.0, 1.0])], width=900)
        widths = [float(w) for w in re.findall(r'<rect data-s="\d+" [^>]*width="([\d.]+)"', rendered.svg)]
        heads = re.findall(r'<path data-s="\d+" d="M([\d.]+),[\d.]+ .*? L([\d.]+),[\d.]+ Z"', rendered.svg)
        widths += [float(right) - float(left) for left, right in heads]
        assert widths and all(w <= chart.BAR_MAX_W + 0.01 for w in widths)

    def test_tooltip_shape(self):
        """app.js 读的形状：每格一项，提示里列出这一格的全部序列，大的在上。"""
        rendered = chart.render_bars(
            ["08-01", "08-02"],
            [bar("small", [1.0, 2.0], slot=0), bar("big", [5.0, 0.0], slot=1)],
            fmt=lambda v: f"${v:.2f}",
        )
        assert rendered.tooltip == [
            {"label": "08-01", "total": "$6.00", "rows": [
                {"name": "big", "color": chart.color_for(1), "value": "$5.00", "s": 0},
                {"name": "small", "color": chart.color_for(0), "value": "$1.00", "s": 1},
            ]},
            {"label": "08-02", "total": "$2.00", "rows": [
                {"name": "small", "color": chart.color_for(0), "value": "$2.00", "s": 1},
            ]},
        ]

    def test_row_index_matches_the_legend_and_the_segments(self):
        """点图例只亮这一项：图例的序号、段上的 data-s、提示里的 s 是同一个东西。"""
        rendered = chart.render_bars(
            ["a", "b", "c"],
            [bar("x", [1.0, 2.0, 3.0], slot=0), bar("y", [9.0, 0.0, 1.0], slot=1), bar("z", [2.0, 2.0, 2.0], slot=2)],
        )
        index = {item["name"]: k for k, item in enumerate(rendered.legend)}
        for column in rendered.tooltip:
            for row in column["rows"]:
                assert row["s"] == index[row["name"]]
        assert {int(s) for s in re.findall(r'data-s="(\d+)"', rendered.svg)} == set(index.values())

    def test_legend_shape(self):
        rendered = chart.render_bars(
            ["a", "b"],
            [bar("small", [1.0, 1.0], slot=0), bar("big", [5.0, 5.0], slot=1), bar("none", [0.0, 0.0], slot=2)],
        )
        assert rendered.legend == [
            {"name": "big", "color": chart.color_for(1), "total": 10.0},
            {"name": "small", "color": chart.color_for(0), "total": 2.0},
        ]

    @pytest.mark.parametrize(
        "labels, series",
        [([], [bar("x", [])]), (["a", "b"], []), (["a", "b"], [bar("x", [0.0, 0.0])])],
    )
    def test_empty_state(self, labels, series):
        rendered = chart.render_bars(labels, series)
        assert rendered.empty is True
        assert "这段时间没有数据" in rendered.svg
        assert rendered.tooltip == [] and rendered.legend == []

    def test_colour_override(self):
        """只有一条序列、想用别的颜色时（账号页的成本柱子用陶土色），序列上带 color 就用它。"""
        rendered = chart.render_bars(["a"], [bar("成本", [5.0], slot=0, color="#d97757")])
        assert rendered.legend[0]["color"] == "#d97757"
        assert rendered.tooltip[0]["rows"][0]["color"] == "#d97757"
        assert 'fill="#d97757"' in rendered.svg
        assert chart.color_for(0) not in rendered.svg

    def test_colour_helper(self):
        assert chart._colour(bar("x", [], slot=2)) == chart.color_for(2)
        assert chart._colour(bar("x", [], slot=-1)) == chart.OTHER_COLOR
        assert chart._colour(bar("x", [], slot=2, color=None)) == chart.color_for(2)
        assert chart._colour(bar("x", [], slot=2, color="#123456")) == "#123456"

    def test_one_hit_area_per_column(self):
        rendered = chart.render_bars(["a", "b", "c"], [bar("x", [1.0, 0.0, 3.0])], fmt=lambda v: f"${v:.0f}")
        hits = re.findall(r'<rect class="chart-hit" data-idx="(\d+)"[^>]*aria-label="([^"]+)"', rendered.svg)
        assert hits == [("0", "a 合计 $1"), ("1", "b 合计 $0"), ("2", "c 合计 $3")]

    def test_axis_formatter_writes_the_ticks(self):
        rendered = chart.render_bars(["a"], [bar("x", [6.0])], axis=lambda v: f"T{v:g}")
        ticks = re.findall(r">([^<]+)</text>", group(rendered.svg, "chart-grid"))
        assert ticks == ["T0", "T2", "T4", "T6"]

    def test_gridlines_are_solid(self):
        rendered = chart.render_bars(["a"], [bar("x", [6.0])])
        lines = re.findall(r"<line [^>]*>", group(rendered.svg, "chart-grid"))
        assert not any("dasharray" in line for line in lines)
        assert f'stroke="{chart.BASELINE}"' in lines[0]

    def test_x_labels_are_thinned(self):
        labels = [f"{i:02d}" for i in range(60)]
        rendered = chart.render_bars(labels, [bar("x", [1.0] * 60)])
        shown = re.findall(r">(\d\d)</text>", group(rendered.svg, "chart-xaxis"))
        assert shown[0] == "00"
        assert 1 < len(shown) < 60

    def test_escapes_text(self):
        rendered = chart.render_bars(["<b>"], [bar("<i>", [1.0])])
        assert "<b>" not in rendered.svg and "&lt;b&gt;" in rendered.svg


class TestSpark:
    def test_empty(self):
        assert chart.render_spark([]) == ""

    def test_last_bar_uses_the_accent(self):
        svg = chart.render_spark([1.0, 2.0, 3.0], accent="#123456")
        fills = re.findall(r'<path [^>]*fill="([^"]+)"', svg)
        assert fills == ["#c2c0b6", "#c2c0b6", "#123456"]

    def test_zero_days_get_a_flat_mark(self):
        """没调用的那天画一道贴底的短线，区分「0」和「没画出来」。"""
        svg = chart.render_spark([0.0, 2.0, 0.0])
        marks = re.findall(r'<rect [^>]*height="1.5" fill="([^"]+)"', svg)
        assert marks == [chart.BASELINE, chart.BASELINE]
        assert svg.count("<path") == 1
        assert "#d97757" not in svg   # 今天是 0，没有强调色

    def test_all_zero(self):
        svg = chart.render_spark([0.0] * 5)
        assert svg.count("<rect") == 5 and "<path" not in svg

    def test_heights_are_relative_to_the_busiest_day(self):
        svg = chart.render_spark([1.0, 4.0], width=40, height=30)
        tops = [float(y) for y in re.findall(r'<path class="spark-bar"[^>]*\sd="M[\d.]+,[\d.]+ L[\d.]+,([\d.]+)', svg)]
        # 圆角前那一点：最高的那根到顶（留 2px），矮的至少 2px
        heights = [30 - (top - 1.5) for top in tops]
        assert heights[1] == pytest.approx(28.0, abs=0.02)
        assert heights[0] == pytest.approx(7.0, abs=0.02)

    def test_size_and_accessibility(self):
        svg = chart.render_spark([1.0], width=96, height=30)
        assert 'viewBox="0 0 96 30"' in svg and 'aria-hidden="true"' in svg

    def test_each_bar_carries_its_index_for_the_entrance_animation(self):
        """进场时一根根长出来：每根柱（包括没调用那天贴底的短线）带着 --i，CSS 按它错开。"""
        svg = chart.render_spark([0.0, 3.0, 5.0])
        assert re.findall(r'class="spark-bar" style="--i:(\d+)"', svg) == ["0", "1", "2"]


class TestDownsample:
    def test_short_series_is_a_copy(self):
        values = [1.0, 2.0, 3.0]
        result = chart.downsample(values, 28)
        assert result == values and result is not values

    def test_pairs_are_summed(self):
        result = chart.downsample([float(i) for i in range(56)], 28)
        assert result == [float(2 * i + 2 * i + 1) for i in range(28)]

    @pytest.mark.parametrize("length", [29, 30, 100, 1000])
    def test_nothing_is_lost(self, length):
        values = [float(i % 7) for i in range(length)]
        result = chart.downsample(values, 28)
        assert len(result) == 28
        assert sum(result) == pytest.approx(sum(values))

    def test_custom_bucket_count(self):
        assert chart.downsample([1.0] * 10, 5) == [2.0] * 5


class TestHeatStyle:
    @pytest.mark.parametrize("value, top", [(0, 10), (-1, 10), (5, 0), (5, -1)])
    def test_nothing_to_colour(self, value, top):
        assert chart.heat_style(value, top) == ""

    def test_busiest_cell_is_the_darkest_with_white_text(self):
        assert chart.heat_style(10, 10) == f"background:{chart.HEAT_RAMP[-1]};color:#ffffff"

    def test_light_cells_use_ink_text(self):
        assert chart.heat_style(0.1, 10) == f"background:{chart.HEAT_RAMP[0]};color:{chart.LABEL_TEXT}"

    def test_darker_as_the_value_grows(self):
        steps = [chart.HEAT_RAMP.index(chart.heat_style(v, 100).split(";")[0].split(":")[1]) for v in range(1, 101)]
        assert steps == sorted(steps)
        assert set(steps) == set(range(len(chart.HEAT_RAMP)))


def local(day: int, hour: int, minute: int = 0) -> datetime:
    """本机时区里 2026-08 某一天的某个钟点（08-17 是周一）。"""
    return datetime(2026, 8, day, hour, minute).astimezone()


class TestWeekGrid:
    def test_daily_data_has_no_hours(self):
        assert chart.week_grid([local(17, 0)], [1.0], 86400) is None

    def test_no_data(self):
        assert chart.week_grid([], [], 3600) is None

    def test_seven_rows_of_twenty_four(self):
        grid = chart.week_grid([local(17, 9)], [5.0], 3600)
        assert len(grid) == 7 and all(len(row) == 24 for row in grid)
        assert grid[0][9] == 5.0   # 周一 9 点
        assert sum(cell is not None for row in grid for cell in row) == 1

    def test_same_slot_in_different_weeks_is_averaged(self):
        grid = chart.week_grid([local(10, 9), local(17, 9)], [10.0, 30.0], 3600)
        assert grid[0][9] == 20.0

    def test_finer_data_is_summed_per_hour_first(self):
        stamps = [local(18, 14, 5 * i) for i in range(12)]   # 周二 14 点的 12 个五分钟
        grid = chart.week_grid(stamps, [1.0] * 12, 300)
        assert grid[1][14] == 12.0

    def test_zero_is_not_missing(self):
        """窗口覆盖到但没调用是 0，没覆盖到是 None，页面上一个画成浅色、一个留空。"""
        grid = chart.week_grid([local(23, 3)], [0.0], 3600)
        assert grid[6][3] == 0.0
        assert grid[6][4] is None

    def test_utc_stamps_are_read_in_local_time(self):
        stamp = datetime(2026, 8, 17, 9, 0, tzinfo=timezone.utc)
        moment = stamp.astimezone()
        grid = chart.week_grid([stamp], [7.0], 3600)
        assert grid[moment.weekday()][moment.hour] == 7.0
