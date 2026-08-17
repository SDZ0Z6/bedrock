"""堆叠柱状图（SVG，无外部依赖）。

配色是 dataviz 规范的深色分类色板，已用 scripts/validate_palette.py 对本项目
的卡片底色 #1a1e2a 跑过全部检查：亮度带、色度下限、CVD 分离度（最差相邻
ΔE 8.4）、常视觉下限（19.3）、对比度全部 PASS。改色请重新跑一遍验证。

绘制规范：
  · 柱子最粗 24px，band 里剩下的留白不填满
  · 堆叠段之间留 2px 底色间隙做分隔，不画描边
  · 最顶端的数据端 4px 圆角，贴基线的一端是方角
  · 网格线是 1px 实线，比底色深一档，尽量后退
  · 只在最高的那根柱子上做一处直接标注，其余交给坐标轴、悬浮提示和明细表
  · 文字一律用文本色，不用序列色
"""

from __future__ import annotations

import html
import math
from dataclasses import dataclass, field

# 8 个分类色槽（深色底），顺序即分配顺序，不循环、不生成第 9 个色
SERIES_COLORS = [
    "#3987e5",  # 1 blue
    "#d95926",  # 2 orange
    "#199e70",  # 3 aqua
    "#c98500",  # 4 yellow
    "#d55181",  # 5 magenta
    "#008300",  # 6 green
    "#9085e9",  # 7 violet
    "#e66767",  # 8 red
]
OTHER_COLOR = "#7a8496"  # 「其他」用中性灰，不占分类色槽（对比度 4.41:1）

SURFACE = "#1a1e2a"  # 卡片底色，同时充当段间间隙的颜色
GRID = "#2a3040"     # 比底色深一档的发丝网格线
BASELINE = "#383f52"
TICK_TEXT = "#9aa3b5"
LABEL_TEXT = "#e6e9f0"

PAD_L, PAD_R, PAD_T, PAD_B = 76, 20, 26, 48
PLOT_H = 260
MAX_BAR_W = 24.0
MIN_BAND = 15.0
MAX_BAND = 56.0
IDEAL_W = 900
SEG_GAP = 2.0
CORNER = 4.0


def color_for(slot: int) -> str:
    """slot 为 -1 表示「其他」。"""
    if slot < 0:
        return OTHER_COLOR
    return SERIES_COLORS[slot % len(SERIES_COLORS)]


def compact_money(value: float, symbol: str = "$") -> str:
    """坐标轴用的紧凑金额：$1.2K / $34.5K / $1.05M。"""
    sign = "-" if value < 0 else ""
    v = abs(value)
    if v >= 1_000_000_000:
        text = f"{v / 1_000_000_000:.2f}B"
    elif v >= 1_000_000:
        text = f"{v / 1_000_000:.2f}M"
    elif v >= 1_000:
        text = f"{v / 1_000:.1f}K"
    elif v == 0:
        text = "0"
    else:
        text = f"{v:.2f}"
    return f"{sign}{symbol}{text}"


def _nice_step(span: float, intervals: int = 4) -> float:
    """把轴刻度落到 1 / 2 / 2.5 / 5 / 10 这类整数上。"""
    if span <= 0:
        return 1.0
    rough = span / intervals
    exponent = math.floor(math.log10(rough))
    base = 10.0**exponent
    for multiple in (1, 2, 2.5, 5, 10):
        if rough <= base * multiple:
            return base * multiple
    return base * 10


def _top_rounded_path(x: float, y: float, w: float, h: float, r: float) -> str:
    """顶端圆角、底端方角的矩形路径。"""
    r = max(0.0, min(r, w / 2, h))
    if r <= 0.2:
        return (
            f'<rect x="{x:.2f}" y="{y:.2f}" width="{w:.2f}" height="{h:.2f}"></rect>'
        )
    bottom = y + h
    d = (
        f"M{x:.2f},{bottom:.2f} L{x:.2f},{y + r:.2f} "
        f"A{r:.2f},{r:.2f} 0 0 1 {x + r:.2f},{y:.2f} "
        f"L{x + w - r:.2f},{y:.2f} "
        f"A{r:.2f},{r:.2f} 0 0 1 {x + w:.2f},{y + r:.2f} "
        f"L{x + w:.2f},{bottom:.2f} Z"
    )
    return f'<path d="{d}"></path>'


@dataclass
class Chart:
    svg: str
    width: int
    height: int
    tooltip: list[dict] = field(default_factory=list)  # 每个时间桶的悬浮数据
    empty: bool = False


def render_stacked_bars(report, symbol: str = "$") -> Chart:
    """把 UsageReport 画成堆叠柱状图。

    序列已按加价后总额降序排好，所以金额大的堆在下面，视觉上更稳。
    """
    dates = report.dates
    count = len(dates)
    totals = report.column_totals
    peak_index, peak_value = report.peak

    if not count or not report.series or max(totals, default=0.0) <= 0:
        height = PAD_T + PLOT_H + PAD_B
        svg = (
            f'<svg class="chart-svg" viewBox="0 0 {IDEAL_W} {height}" '
            f'width="{IDEAL_W}" height="{height}" role="img" '
            f'aria-label="所选区间内没有消费数据">'
            f'<text x="{IDEAL_W / 2:.0f}" y="{PAD_T + PLOT_H / 2:.0f}" '
            f'text-anchor="middle" fill="{TICK_TEXT}" font-size="13">'
            f"所选区间内没有消费数据</text></svg>"
        )
        return Chart(svg=svg, width=IDEAL_W, height=height, empty=True)

    # ---------------------------------------------------------- 尺寸
    available = IDEAL_W - PAD_L - PAD_R
    band = min(MAX_BAND, max(MIN_BAND, available / count))
    plot_w = band * count
    width = int(max(IDEAL_W, PAD_L + plot_w + PAD_R))
    height = PAD_T + PLOT_H + PAD_B
    # 桶少的时候把绘图区居中，避免整张图挤在左边
    x0 = PAD_L + max(0.0, (width - PAD_L - PAD_R - plot_w) / 2)
    bar_w = min(MAX_BAR_W, max(4.0, band - 6))
    plot_bottom = PAD_T + PLOT_H

    # ---------------------------------------------------------- Y 轴
    data_max = max(totals)
    step = _nice_step(data_max, 4)
    tick_count = max(1, math.ceil(data_max / step - 1e-9))
    y_max = step * tick_count
    scale = PLOT_H / y_max if y_max > 0 else 0.0

    parts: list[str] = []
    parts.append(
        f'<svg class="chart-svg" viewBox="0 0 {width} {height}" '
        f'width="{width}" height="{height}" role="img" '
        f'aria-label="按{"月" if report.granularity == "monthly" else "日"}'
        f'的{html.escape(report.dimension_label)}成本堆叠柱状图，'
        f'共 {count} 个时间桶，{len(report.series)} 条序列">'
    )

    # 网格线与刻度（发丝实线，从不用虚线）
    parts.append('<g class="chart-grid">')
    for tick in range(tick_count + 1):
        value = step * tick
        y = plot_bottom - value * scale
        parts.append(
            f'<line x1="{PAD_L:.0f}" y1="{y:.2f}" x2="{width - PAD_R:.0f}" y2="{y:.2f}" '
            f'stroke="{BASELINE if tick == 0 else GRID}" stroke-width="1"></line>'
        )
        parts.append(
            f'<text x="{PAD_L - 10:.0f}" y="{y + 4:.2f}" text-anchor="end" '
            f'fill="{TICK_TEXT}" font-size="11" '
            f'style="font-variant-numeric:tabular-nums">'
            f"{html.escape(compact_money(value, symbol))}</text>"
        )
    parts.append("</g>")

    # ---------------------------------------------------------- 柱体
    # 圆角要落在整根柱子的轮廓上，不能只给最上面那一段：堆叠图里顶端常常是
    # 一条亚像素的细条（比如「其他」只有几分钱），给它加圆角等于没加。
    # 所以按列生成一个「顶端圆角」的裁剪路径，再把各段裁进去。
    parts.append("<defs>")
    for index in range(count):
        if totals[index] <= 0:
            continue
        band_x = x0 + band * index
        bar_x = band_x + (band - bar_w) / 2
        h = totals[index] * scale
        parts.append(
            f'<clipPath id="cubar{index}">'
            f"{_top_rounded_path(bar_x, plot_bottom - h, bar_w, h, CORNER)}"
            f"</clipPath>"
        )
    parts.append("</defs>")

    parts.append('<g class="chart-bars">')
    for index in range(count):
        if totals[index] <= 0:
            continue
        band_x = x0 + band * index
        bar_x = band_x + (band - bar_w) / 2
        # 该列最上面一个非零段：它上面没有东西，所以不留间隙
        top_series = -1
        for position, series in enumerate(report.series):
            if series.marked[index] > 0:
                top_series = position

        parts.append(f'<g clip-path="url(#cubar{index})">')
        y_base = plot_bottom
        for position, series in enumerate(report.series):
            value = series.marked[index]
            if value <= 0:
                continue
            h = value * scale
            gap = 0.0 if position == top_series else SEG_GAP
            draw_h = h - gap
            if draw_h < 1.0:
                draw_h = min(h, 1.0)
            # 间隙留在段的上沿，堆叠边界仍落在真实的累计位置上
            parts.append(
                f'<rect fill="{color_for(series.slot)}" x="{bar_x:.2f}" '
                f'y="{y_base - draw_h:.2f}" width="{bar_w:.2f}" '
                f'height="{draw_h:.2f}"></rect>'
            )
            y_base -= h
        parts.append("</g>")
    parts.append("</g>")

    # ---------------------------------------------------------- 唯一一处直接标注：最高的柱子
    if peak_index >= 0 and peak_value > 0:
        peak_x = x0 + band * peak_index + band / 2
        peak_y = plot_bottom - peak_value * scale - 9
        anchor = "middle"
        if peak_x < PAD_L + 30:
            anchor = "start"
        elif peak_x > width - PAD_R - 30:
            anchor = "end"
        parts.append(
            f'<text x="{peak_x:.2f}" y="{max(peak_y, PAD_T - 8):.2f}" '
            f'text-anchor="{anchor}" fill="{LABEL_TEXT}" font-size="11" '
            f'font-weight="600" style="font-variant-numeric:tabular-nums">'
            f"{html.escape(compact_money(peak_value, symbol))}</text>"
        )

    # ---------------------------------------------------------- X 轴标签
    # 按标签宽度决定间隔，避免相互压字
    stride = max(1, math.ceil(46.0 / band))
    parts.append('<g class="chart-xaxis">')
    shown: list[int] = []
    for index in range(count):
        if index % stride == 0:
            shown.append(index)
    if shown and shown[-1] != count - 1 and (count - 1 - shown[-1]) * band >= 46:
        shown.append(count - 1)
    for index in shown:
        x = x0 + band * index + band / 2
        parts.append(
            f'<text x="{x:.2f}" y="{plot_bottom + 20:.0f}" text-anchor="middle" '
            f'fill="{TICK_TEXT}" font-size="11">'
            f"{html.escape(report.labels[index])}</text>"
        )
    parts.append("</g>")

    # ---------------------------------------------------------- 悬浮命中区
    # 一列一个命中区，覆盖整个 band 和绘图高度，比单个色块好点得多，
    # 也支持键盘 Tab 逐列查看。精确到每个序列的数值由下方明细表承载。
    parts.append('<g class="chart-hits">')
    for index in range(count):
        band_x = x0 + band * index
        parts.append(
            f'<rect class="chart-hit" data-idx="{index}" x="{band_x:.2f}" '
            f'y="{PAD_T:.0f}" width="{band:.2f}" height="{PLOT_H:.0f}" '
            f'tabindex="0" role="button" '
            f'aria-label="{html.escape(dates[index])} 合计 '
            f'{html.escape(compact_money(totals[index], symbol))}"></rect>'
        )
    parts.append("</g>")
    parts.append("</svg>")

    # ---------------------------------------------------------- 悬浮数据
    tooltip = []
    for index in range(count):
        rows = [
            {
                "name": series.name,
                "color": color_for(series.slot),
                "marked": series.marked[index],
                "raw": series.raw[index],
            }
            for series in report.series
            if series.marked[index] > 0
        ]
        rows.sort(key=lambda row: -row["marked"])
        tooltip.append(
            {
                "date": dates[index],
                "label": report.labels[index],
                "total": totals[index],
                "rows": rows,
            }
        )

    return Chart(svg="".join(parts), width=width, height=height, tooltip=tooltip)
