"""堆叠面积图（SVG，无外部依赖）。

配色是 dataviz 规范的分类色板，用 scripts/validate_palette.py 对本项目的卡片
底色跑过检查：对比度、亮度带、色度下限、色觉障碍分离度。**改色或换底色都要
重跑一遍**——对比度是跟着底色变的，2026-09 换浅色主题时黄和紫就是这么被揪出来
要压暗的。

色相沿用改主题之前那一套，只把黄和紫压暗到浅底上够 3:1。不换色相是有意的：
assign_slots 按名字哈希分配色槽，换了色相等于所有人记住的「哪个模型是什么颜色」
全部作废，而这次要解决的只是底色变浅带来的对比度问题。

绘制规范：
  · 每条序列是一条堆叠的面积带：上沿一条实色线，下方同色半透明填充
  · 线负责识别、面积只负责体量感。线用的就是 SERIES_COLORS 里那个值，也就是
    validate_palette 校验过 ≥3:1 的那个，所以校验结论对承载信息的元素依然成立
  · 堆叠的各条带几何上互不重叠，半透明只是各自对着纸底变淡，不会互相透叠
  · 零线是实线，其余网格线用虚线后退一步——面积图的色块本来就重
  · 只在最高的那个点上做一处直接标注，其余交给坐标轴、悬浮提示和明细表
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
    "#c48200",  # 4 yellow   浅底上 2.92:1 不够，压暗到 3.06
    "#d55181",  # 5 magenta
    "#008300",  # 6 green
    "#9384e0",  # 7 violet   同上。注意不能只压暗：单纯压暗会让它在红色盲下
                #           和蓝色更难分（ΔE 1.8 -> 0.7），所以色相同时往洋红
                #           挪了 3°，对比度 3.02、红色盲 ΔE 反而升到 2.0
    "#e66767",  # 8 red
]
OTHER_COLOR = "#7a8496"  # 「其他」用中性灰，不占分类色槽

# 这几个值要和 style.css 的令牌对上：SURFACE = --panel，GRID = --line-soft，
# BASELINE = --line，TICK_TEXT = --text-mute，LABEL_TEXT = --text。
# SVG 里用不了 CSS 变量（图是在服务端拼字符串生成的），只能各写一份，
# 所以改主题时两边都要动。
SURFACE = "#fbf9f6"  # 卡片底色，同时充当段间间隙的颜色
GRID = "#e4ded4"     # 比底色深一档的发丝网格线
BASELINE = "#d5cec2"
TICK_TEXT = "#6f6861"
LABEL_TEXT = "#1a1918"

PAD_L, PAD_R, PAD_T, PAD_B = 76, 20, 26, 48
PLOT_H = 260
# 面积图的每个桶只是折线上的一个点，不像柱子那样需要宽度，所以 MIN_BAND 比
# 柱状图时代小得多：一年按日是 365 个桶，柱状图要 5475px 画布，面积图 2190px
# 就够，横向滚动的距离少了一大半。下限 6px 是为了悬浮命中区还点得中。
MIN_BAND = 6.0
MAX_BAND = 56.0
IDEAL_W = 900
# 上沿实色线的粗细。1.8px 在浅底上够醒目，又不会粗到把窄峰糊成一块。
LINE_W = 1.8
# 面积填充的不透明度。线负责识别，面积只负责体量感，所以可以压得比较淡；
# 太浓的话 9 条叠起来整张图会闷，稀疏数据里那几个孤立的峰更是重得刺眼。
AREA_FILL_OPACITY = 0.28


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


def compact_number(value: float) -> str:
    """非金额的紧凑数字：46.2K 次 / 308.4M token。"""
    sign = "-" if value < 0 else ""
    v = abs(value)
    if v >= 1_000_000_000:
        text = f"{v / 1_000_000_000:.2f}B"
    elif v >= 1_000_000:
        text = f"{v / 1_000_000:.1f}M"
    elif v >= 1_000:
        text = f"{v / 1_000:.1f}K"
    elif v == 0:
        text = "0"
    elif v < 10:
        text = f"{v:.2f}".rstrip("0").rstrip(".")
    else:
        text = f"{v:,.0f}"
    return f"{sign}{text}"


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




@dataclass
class Chart:
    svg: str
    width: int
    height: int
    tooltip: list[dict] = field(default_factory=list)  # 每个时间桶的悬浮数据
    empty: bool = False


def render_stacked_areas(report, symbol: str = "$") -> Chart:
    """把 UsageReport 画成堆叠面积图。

    序列已按总额降序排好，所以金额大的堆在下面，视觉上更稳。

    report 只需要满足几个属性：dates / labels / series / column_totals / peak /
    granularity / dimension_label。usage_explorer.UsageReport 和
    cost_estimate.EstimateReport 都是照着这个形状来的。
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
        f'的{html.escape(report.dimension_label)}成本堆叠面积图，'
        f'共 {count} 个时间桶，{len(report.series)} 条序列">'
    )

    # 网格线与刻度。零线是实线（它是基准，要能站住），其余用虚线后退一步——
    # 面积图的色块本来就重，实线网格会和它抢。
    parts.append('<g class="chart-grid">')
    for tick in range(tick_count + 1):
        value = step * tick
        y = plot_bottom - value * scale
        dash = "" if tick == 0 else ' stroke-dasharray="3 4"'
        parts.append(
            f'<line x1="{PAD_L:.0f}" y1="{y:.2f}" x2="{width - PAD_R:.0f}" y2="{y:.2f}" '
            f'stroke="{BASELINE if tick == 0 else GRID}" stroke-width="1"{dash}></line>'
        )
        parts.append(
            f'<text x="{PAD_L - 10:.0f}" y="{y + 4:.2f}" text-anchor="end" '
            f'fill="{TICK_TEXT}" font-size="11" '
            f'style="font-variant-numeric:tabular-nums">'
            f"{html.escape(compact_money(value, symbol))}</text>"
        )
    parts.append("</g>")

    # ---------------------------------------------------------- 面积带
    # 每条序列画成一条堆叠的面积带：下沿是它下面所有序列的累计值，上沿是加上
    # 自己之后的累计值，首尾闭合成多边形。
    #
    # 上沿画一条**实色的线**，面积用同色**半透明**填充。分工是刻意的：
    #   · 线负责识别 —— 它用的就是 SERIES_COLORS 里那个颜色，也就是
    #     scripts/validate_palette.py 校验过 ≥3:1 的那个值，所以那份校验
    #     结论对「真正承载信息的元素」依然成立；
    #   · 面积只负责体量感 —— 它是装饰，淡一点反而更好读。
    #
    # 堆叠的各条带在几何上互不重叠（一条压在另一条之上，不是叠在一起），
    # 所以半透明只是各自对着纸底变淡，不会互相透叠成一团脏色。
    def point_x(index: int) -> float:
        return x0 + band * index + band / 2

    lower = [plot_bottom] * count           # 当前累计的上沿，逐层往上抬
    parts.append('<g class="chart-areas">')
    for series in report.series:
        upper = [
            lower[i] - series.marked[i] * scale for i in range(count)
        ]
        # 整条都是 0 的序列不画，免得在零线上留一条多余的线
        if all(abs(upper[i] - lower[i]) < 1e-9 for i in range(count)):
            continue

        color = color_for(series.slot)
        top_edge = " ".join(f"{point_x(i):.2f},{upper[i]:.2f}" for i in range(count))
        bottom_edge = " ".join(
            f"{point_x(i):.2f},{lower[i]:.2f}" for i in reversed(range(count))
        )
        # 面积：上沿正序 + 下沿倒序，闭合
        parts.append(
            f'<polygon fill="{color}" fill-opacity="{AREA_FILL_OPACITY}" '
            f'points="{top_edge} {bottom_edge}"></polygon>'
        )
        # 上沿的实色线。单桶时没有「线」可言，画个点代替，否则那一天什么都看不见。
        if count > 1:
            parts.append(
                f'<polyline fill="none" stroke="{color}" stroke-width="{LINE_W}" '
                f'stroke-linejoin="round" stroke-linecap="round" '
                f'points="{top_edge}"></polyline>'
            )
        else:
            parts.append(
                f'<circle fill="{color}" cx="{point_x(0):.2f}" '
                f'cy="{upper[0]:.2f}" r="{LINE_W}"></circle>'
            )
        lower = upper
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


# =================================================================== 折线图
# 时间序列 + 多个模型对比用折线而不是堆叠：折线能直接读出每条序列自己的绝对值
# 和走势，堆叠图里上层序列得靠目测减下层。
#
# 规范要点：线宽 2px、圆角端点；网格发丝实线；序列 ≥2 条必有图例；≤4 条时额外
# 在线尾直标末值（多了就只靠图例和悬浮，绝不给每个点都写数字）；一律单 Y 轴——
# 调用次数和 token 量级完全不同，双轴的刻度对齐是任意的，会凭空造出相关性，
# 所以指标做成单选。

LINE_W = 2.0
MARKER_R = 4.0          # 直径 8px，规范下限
MARKER_MAX_POINTS = 30  # 点太多就不画标记，否则连成一片
END_LABEL_MAX_SERIES = 4
END_LABEL_MIN_GAP = 14.0
PAD_R_LABELED = 64


def _line_path(points: list[tuple[float, float]]) -> str:
    head, *rest = points
    return f"M{head[0]:.2f},{head[1]:.2f}" + "".join(
        f" L{x:.2f},{y:.2f}" for x, y in rest
    )


# --------------------------------------------------------------- 小倍数图
# 四个区域各一张小折线图，2×2 排列。两条硬规则：
#   1. 四张图共用一个 Y 轴刻度。各自缩放的话，一个每小时几十次的小区会画得和
#      每小时几万次的大区一样高，横向对比直接失去意义。
#   2. 同一个模型在四张图里必须是同一个颜色（序列集合和颜色槽在数据层就统一好
#      了），并且只用一份共享图例。
PANEL_W = 468
PANEL_PLOT_H = 148
PANEL_PAD_L = 62
PANEL_PAD_R = 14
PANEL_PAD_T = 12
PANEL_PAD_B = 32
PANEL_MARKER_MAX_POINTS = 20
# 小图线下渐变填充的**顶端**不透明度，往零线方向渐隐到 0。
# 比堆叠面积图的 0.28 低：那里各层互不重叠，这里的线是叠在一起的，
# 每条的填充都铺到零线，浓度会累加。
PANEL_FILL_OPACITY = 0.22


@dataclass
class Panel:
    region: str
    label: str
    total: float
    svg: str
    tooltip: list[dict] = field(default_factory=list)
    empty: bool = False


def render_small_multiples(report) -> list[Panel]:
    """把 UsageMetricsReport 的四个区域面板画成 2×2 小折线图。"""
    labels = report.labels
    count = len(labels)
    shared_peak = report.shared_peak

    step_value = _nice_step(shared_peak, 3) if shared_peak > 0 else 1.0
    tick_count = max(1, math.ceil(shared_peak / step_value - 1e-9)) if shared_peak > 0 else 1
    y_max = step_value * tick_count

    plot_w = PANEL_W - PANEL_PAD_L - PANEL_PAD_R
    plot_bottom = PANEL_PAD_T + PANEL_PLOT_H
    height = PANEL_PAD_T + PANEL_PLOT_H + PANEL_PAD_B
    step = plot_w / (count - 1) if count > 1 else 0.0
    scale = PANEL_PLOT_H / y_max if y_max else 0.0

    def x_at(index: int) -> float:
        return PANEL_PAD_L + (step * index if count > 1 else plot_w / 2)

    def y_at(value: float) -> float:
        return plot_bottom - value * scale

    panels: list[Panel] = []
    for panel_data in report.panels:
        parts: list[str] = [
            f'<svg class="chart-svg panel-svg" viewBox="0 0 {PANEL_W} {height}" '
            f'width="{PANEL_W}" height="{height}" role="img" '
            f'aria-label="{html.escape(panel_data.label)} 的'
            f'{html.escape(report.metric_label)}折线图，'
            f'合计 {html.escape(compact_number(panel_data.total))} '
            f'{html.escape(report.unit)}">'
        ]

        # 网格与刻度（四张图完全相同，因为共用刻度）。零线实线，其余虚线，
        # 和堆叠面积图一个规矩。
        parts.append('<g class="chart-grid">')
        for tick in range(tick_count + 1):
            value = step_value * tick
            y = y_at(value)
            dash = "" if tick == 0 else ' stroke-dasharray="3 4"'
            parts.append(
                f'<line x1="{PANEL_PAD_L:.0f}" y1="{y:.2f}" '
                f'x2="{PANEL_W - PANEL_PAD_R:.0f}" y2="{y:.2f}" '
                f'stroke="{BASELINE if tick == 0 else GRID}" stroke-width="1"{dash}></line>'
            )
            parts.append(
                f'<text x="{PANEL_PAD_L - 8:.0f}" y="{y + 4:.2f}" text-anchor="end" '
                f'fill="{TICK_TEXT}" font-size="10" '
                f'style="font-variant-numeric:tabular-nums">'
                f"{html.escape(compact_number(value))}</text>"
            )
        parts.append("</g>")

        if not count or panel_data.total <= 0:
            parts.append(
                f'<text x="{PANEL_PAD_L + plot_w / 2:.0f}" '
                f'y="{PANEL_PAD_T + PANEL_PLOT_H / 2:.0f}" text-anchor="middle" '
                f'fill="{TICK_TEXT}" font-size="12">该区无调用</text>'
            )
            parts.append("</svg>")
            panels.append(
                Panel(
                    region=panel_data.region, label=panel_data.label,
                    total=panel_data.total, svg="".join(parts), empty=True,
                )
            )
            continue

        # 线下的渐变填充。折线图和堆叠面积图不一样：这里的线是**互相重叠**的，
        # 每条的填充都从自己那条线一直铺到零线，靠近底部会层层叠加——两层 28%
        # 叠出 48%，三层 63%，五六条模型下来底部就糊成一团脏色了。
        #
        # 所以用渐变而不是平涂：紧贴线的地方最浓，往下渐隐到全透明。重叠因此
        # 发生在各自已经很淡的区域，既有「线下有面积」的观感，又不会互相糊掉。
        drawable = [s for s in panel_data.series if s.peak > 0]
        if drawable and count > 1:
            parts.append("<defs>")
            for series in drawable:
                # id 必须带上区域：四张小图在同一个页面上，SVG 的 id 是全文档
                # 共享的，只用 slot 会让四张图抢同一个渐变
                parts.append(
                    f'<linearGradient id="fade-{html.escape(panel_data.region)}-{series.slot}" '
                    f'x1="0" y1="{PANEL_PAD_T:.0f}" x2="0" y2="{plot_bottom:.0f}" '
                    f'gradientUnits="userSpaceOnUse">'
                    f'<stop offset="0" stop-color="{color_for(series.slot)}" '
                    f'stop-opacity="{PANEL_FILL_OPACITY}"></stop>'
                    f'<stop offset="1" stop-color="{color_for(series.slot)}" '
                    f'stop-opacity="0"></stop>'
                    f"</linearGradient>"
                )
            parts.append("</defs>")

        # 先把所有填充画完，再画所有线。混在一起画的话，后一条序列的填充会
        # 盖在前一条的线上——填充最浓的那一段正好紧贴线，压上去很明显。
        geometry = {
            series.slot: [(x_at(i), y_at(series.values[i])) for i in range(count)]
            for series in drawable
        }

        if count > 1:
            parts.append('<g class="chart-fills">')
            for series in drawable:
                points = geometry[series.slot]
                # 面积：沿线走一遍，再从末端落到零线、沿零线回到起点，闭合
                area = (
                    f"{_line_path(points)} L {points[-1][0]:.2f} {plot_bottom:.2f} "
                    f"L {points[0][0]:.2f} {plot_bottom:.2f} Z"
                )
                parts.append(
                    f'<path d="{area}" fill="url(#fade-'
                    f'{html.escape(panel_data.region)}-{series.slot})" stroke="none"></path>'
                )
            parts.append("</g>")

        parts.append('<g class="chart-lines">')
        for series in drawable:
            colour = color_for(series.slot)
            points = geometry[series.slot]
            if count == 1:
                parts.append(
                    f'<circle cx="{points[0][0]:.2f}" cy="{points[0][1]:.2f}" '
                    f'r="{MARKER_R:.1f}" fill="{colour}"></circle>'
                )
                continue
            parts.append(
                f'<path d="{_line_path(points)}" fill="none" stroke="{colour}" '
                f'stroke-width="{LINE_W}" stroke-linejoin="round" '
                f'stroke-linecap="round"></path>'
            )
            if count <= PANEL_MARKER_MAX_POINTS:
                for x, y in points:
                    parts.append(
                        f'<circle cx="{x:.2f}" cy="{y:.2f}" r="{MARKER_R - 1:.1f}" '
                        f'fill="{colour}" stroke="{SURFACE}" stroke-width="1.5"></circle>'
                    )
        parts.append("</g>")

        # X 轴标签：面板窄，标签更稀疏；小图不做末值直标，交给共享图例和悬浮
        label_width = 60.0 if report.window.period < 86400 else 44.0
        stride = max(1, math.ceil(label_width / step)) if step else 1
        shown = [i for i in range(count) if i % stride == 0]
        parts.append('<g class="chart-xaxis">')
        for index in shown:
            x = x_at(index)
            half = label_width / 2
            if x - half < 2:
                anchor, text_x = "start", 2.0
            elif x + half > PANEL_W - 2:
                anchor, text_x = "end", float(PANEL_W - 2)
            else:
                anchor, text_x = "middle", x
            parts.append(
                f'<text x="{text_x:.2f}" y="{plot_bottom + 18:.0f}" '
                f'text-anchor="{anchor}" fill="{TICK_TEXT}" font-size="10">'
                f"{html.escape(labels[index])}</text>"
            )
        parts.append("</g>")

        parts.append('<g class="chart-cursor" aria-hidden="true">')
        parts.append(
            f'<line class="chart-crosshair" x1="0" y1="{PANEL_PAD_T}" x2="0" '
            f'y2="{plot_bottom}" stroke="{TICK_TEXT}" stroke-width="1"></line>'
        )
        for series in panel_data.series:
            parts.append(
                f'<circle class="chart-focus" cx="0" cy="0" r="{MARKER_R:.1f}" '
                f'fill="{color_for(series.slot)}" stroke="{SURFACE}" '
                f'stroke-width="1.5"></circle>'
            )
        parts.append("</g>")
        parts.append(
            f'<rect class="chart-overlay" x="{PANEL_PAD_L}" y="{PANEL_PAD_T}" '
            f'width="{plot_w:.2f}" height="{PANEL_PLOT_H}" tabindex="0" '
            f'role="application" aria-label="{html.escape(panel_data.label)}：'
            f"按左右方向键逐个时间点查看各模型的"
            f'{html.escape(report.metric_label)}"></rect>'
        )
        parts.append("</svg>")

        tooltip = []
        for index in range(count):
            rows = [
                {
                    "name": series.name,
                    "color": color_for(series.slot),
                    "value": series.values[index],
                    "y": round(y_at(series.values[index]), 2),
                }
                for series in panel_data.series
            ]
            rows.sort(key=lambda row: -row["value"])
            tooltip.append(
                {
                    "label": labels[index],
                    "total": sum(row["value"] for row in rows),
                    "x": round(x_at(index), 2),
                    "rows": rows,
                }
            )

        panels.append(
            Panel(
                region=panel_data.region, label=panel_data.label,
                total=panel_data.total, svg="".join(parts), tooltip=tooltip,
            )
        )

    return panels


def render_lines(report, unit: str = "") -> Chart:
    """把 UsageMetricsReport 画成折线图。"""
    labels = report.labels
    count = len(labels)
    series_list = report.series
    peak_all = max((s.peak for s in series_list), default=0.0)

    if not count or not series_list or peak_all <= 0:
        height = PAD_T + PLOT_H + PAD_B
        svg = (
            f'<svg class="chart-svg" viewBox="0 0 {IDEAL_W} {height}" '
            f'width="{IDEAL_W}" height="{height}" role="img" '
            f'aria-label="所选区间内没有调用数据">'
            f'<text x="{IDEAL_W / 2:.0f}" y="{PAD_T + PLOT_H / 2:.0f}" '
            f'text-anchor="middle" fill="{TICK_TEXT}" font-size="13">'
            f"所选区间内没有调用数据</text></svg>"
        )
        return Chart(svg=svg, width=IDEAL_W, height=height, empty=True)

    label_ends = len(series_list) <= END_LABEL_MAX_SERIES
    pad_r = PAD_R_LABELED if label_ends else PAD_R
    width = IDEAL_W
    height = PAD_T + PLOT_H + PAD_B
    plot_w = width - PAD_L - pad_r
    plot_bottom = PAD_T + PLOT_H
    step = plot_w / (count - 1) if count > 1 else 0.0

    def x_at(index: int) -> float:
        return PAD_L + (step * index if count > 1 else plot_w / 2)

    step_value = _nice_step(peak_all, 4)
    tick_count = max(1, math.ceil(peak_all / step_value - 1e-9))
    y_max = step_value * tick_count
    scale = PLOT_H / y_max if y_max else 0.0

    def y_at(value: float) -> float:
        return plot_bottom - value * scale

    parts: list[str] = [
        f'<svg class="chart-svg" viewBox="0 0 {width} {height}" '
        f'width="{width}" height="{height}" role="img" '
        f'aria-label="{html.escape(report.metric_label)}随时间变化的折线图，'
        f"{count} 个时间点，{len(series_list)} 条序列，"
        f'纵轴单位 {html.escape(unit or report.unit)}">'
    ]

    parts.append('<g class="chart-grid">')
    for tick in range(tick_count + 1):
        value = step_value * tick
        y = y_at(value)
        parts.append(
            f'<line x1="{PAD_L:.0f}" y1="{y:.2f}" x2="{width - pad_r:.0f}" y2="{y:.2f}" '
            f'stroke="{BASELINE if tick == 0 else GRID}" stroke-width="1"></line>'
        )
        parts.append(
            f'<text x="{PAD_L - 10:.0f}" y="{y + 4:.2f}" text-anchor="end" '
            f'fill="{TICK_TEXT}" font-size="11" '
            f'style="font-variant-numeric:tabular-nums">'
            f"{html.escape(compact_number(value))}</text>"
        )
    parts.append("</g>")

    parts.append('<g class="chart-lines">')
    for series in series_list:
        colour = color_for(series.slot)
        points = [(x_at(i), y_at(series.values[i])) for i in range(count)]
        if count == 1:
            parts.append(
                f'<circle cx="{points[0][0]:.2f}" cy="{points[0][1]:.2f}" '
                f'r="{MARKER_R:.1f}" fill="{colour}"></circle>'
            )
            continue
        parts.append(
            f'<path d="{_line_path(points)}" fill="none" stroke="{colour}" '
            f'stroke-width="{LINE_W}" stroke-linejoin="round" '
            f'stroke-linecap="round"></path>'
        )
        if count <= MARKER_MAX_POINTS:
            for x, y in points:
                parts.append(
                    f'<circle cx="{x:.2f}" cy="{y:.2f}" r="{MARKER_R:.1f}" '
                    f'fill="{colour}" stroke="{SURFACE}" stroke-width="2"></circle>'
                )
    parts.append("</g>")

    # 线尾直标末值。相互压字时宁可不标，交给图例和悬浮——把标签硬挤开会让它
    # 和自己那条线脱钩，比不标更难读。
    if label_ends:
        candidates = sorted(
            ((y_at(s.values[-1]), s) for s in series_list), key=lambda item: item[0]
        )
        placed: list[float] = []
        parts.append('<g class="chart-endlabels">')
        for y, series in candidates:
            if placed and abs(y - placed[-1]) < END_LABEL_MIN_GAP:
                continue
            placed.append(y)
            parts.append(
                f'<text x="{x_at(count - 1) + 8:.2f}" y="{y + 4:.2f}" '
                f'text-anchor="start" fill="{LABEL_TEXT}" font-size="11" '
                f'font-weight="600" style="font-variant-numeric:tabular-nums">'
                f"{html.escape(compact_number(series.values[-1]))}</text>"
            )
        parts.append("</g>")

    # "08-10 21:00" 实测约 57px 宽，留点余量
    label_width = 68.0 if report.window.period < 86400 else 46.0
    stride = max(1, math.ceil(label_width / step)) if step else 1
    shown = [i for i in range(count) if i % stride == 0]
    if shown and shown[-1] != count - 1 and (count - 1 - shown[-1]) * step >= label_width:
        shown.append(count - 1)
    parts.append('<g class="chart-xaxis">')
    for index in shown:
        x = x_at(index)
        # 贴边时改锚点而不是挪位置：挪了标签就和它对应的刻度脱钩，
        # 还会挤到邻居身上（挪 18px 正好让首尾两个标签压字）
        half = label_width / 2
        if x - half < 2:
            anchor, text_x = "start", 2.0
        elif x + half > width - 2:
            anchor, text_x = "end", float(width - 2)
        else:
            anchor, text_x = "middle", x
        parts.append(
            f'<text x="{text_x:.2f}" y="{plot_bottom + 20:.0f}" text-anchor="{anchor}" '
            f'fill="{TICK_TEXT}" font-size="11">{html.escape(labels[index])}</text>'
        )
    parts.append("</g>")

    # 十字准线 + 每条序列的焦点圆点，位置由 JS 按最近点移动。
    # 用一个覆盖绘图区的透明层，而不是逐列命中区：点可以多到 1500 个，逐列命中
    # 区会窄到点不中；覆盖层配合「取最近点」在任何密度下都好用，也能用左右
    # 方向键逐点浏览。
    parts.append('<g class="chart-cursor" aria-hidden="true">')
    parts.append(
        f'<line class="chart-crosshair" x1="0" y1="{PAD_T}" x2="0" y2="{plot_bottom}" '
        f'stroke="{TICK_TEXT}" stroke-width="1"></line>'
    )
    for series in series_list:
        parts.append(
            f'<circle class="chart-focus" cx="0" cy="0" r="{MARKER_R + 0.5:.1f}" '
            f'fill="{color_for(series.slot)}" stroke="{SURFACE}" stroke-width="2"></circle>'
        )
    parts.append("</g>")
    parts.append(
        f'<rect class="chart-overlay" x="{PAD_L}" y="{PAD_T}" '
        f'width="{plot_w:.2f}" height="{PLOT_H}" tabindex="0" role="application" '
        f'aria-label="按左右方向键逐个时间点查看各模型的'
        f'{html.escape(report.metric_label)}"></rect>'
    )
    parts.append("</svg>")

    tooltip = []
    for index in range(count):
        rows = [
            {
                "name": series.name,
                "color": color_for(series.slot),
                "value": series.values[index],
                "y": round(y_at(series.values[index]), 2),
            }
            for series in series_list
        ]
        rows.sort(key=lambda row: -row["value"])
        tooltip.append(
            {
                "label": labels[index],
                "total": sum(row["value"] for row in rows),
                "x": round(x_at(index), 2),
                "rows": rows,
            }
        )

    return Chart(svg="".join(parts), width=width, height=height, tooltip=tooltip)
