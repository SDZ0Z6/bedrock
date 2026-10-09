"""图表（SVG，无外部依赖）：成本页的堆叠面积图，模型用量页的折线图。

配色是 dataviz 规范的分类色板，用 scripts/validate_palette.py 对本项目的卡片
底色跑过检查：对比度、亮度带、色度下限、色觉障碍分离度。**改色或换底色都要
重跑一遍**——对比度是跟着底色变的，2026-09 换浅色主题时黄和紫就是这么被揪出来
要压暗的。2026-10 换象牙主题后卡片是纯白底，对 #ffffff 重跑一遍全部通过，
色板一个值都没动。

色相一直沿用最早那一套。不换色相是有意的：assign_slots 按名字哈希分配色槽，
换了色相等于所有人记住的「哪个模型是什么颜色」全部作废。

绘制规范：
  · 每条序列是一条堆叠的面积带：上沿一条实色线，下方同色的浅淡填充
  · 线负责识别、面积只负责体量感。线用的就是 SERIES_COLORS 里那个值，也就是
    validate_palette 校验过 ≥3:1 的那个，所以校验结论对承载信息的元素依然成立
  · 堆叠的各条带几何上互不重叠，半透明只是各自对着纸底变淡，不会互相透叠
  · 网格线一律是比底色深一档的发丝实线，零线再深一档。虚线读起来像「预测」
    或「阈值」，不用
  · 只在最高的那个点上做一处直接标注，其余交给坐标轴、悬浮提示和明细表
  · 文字一律用文本色，不用序列色
"""

from __future__ import annotations

import html
import math
from dataclasses import dataclass, field
from types import SimpleNamespace

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

# 这几个值要和 style.css 的令牌对上：SURFACE = --panel，TICK_TEXT = --text-mute，
# LABEL_TEXT = --text。GRID 比 --line-soft 再深一点点：纯白底上那一档几乎看不见。
# SVG 里用不了 CSS 变量（图是在服务端拼字符串生成的），只能各写一份，
# 所以改主题时两边都要动。
SURFACE = "#ffffff"  # 卡片底色，同时充当段间间隙的颜色
GRID = "#ebe9e1"     # 比底色深一档的发丝网格线
BASELINE = "#d1cfc5"
TICK_TEXT = "#6b6a64"
LABEL_TEXT = "#141413"

PAD_L, PAD_R, PAD_T, PAD_B = 76, 20, 26, 48
PLOT_H = 260
# 面积图的每个桶只是折线上的一个点，不像柱子那样需要宽度，所以 MIN_BAND 比
# 柱状图时代小得多：一年按日是 365 个桶，柱状图要 5475px 画布，面积图 2190px
# 就够，横向滚动的距离少了一大半。下限 6px 是为了悬浮命中区还点得中。
MIN_BAND = 6.0
# 每个桶最宽多少。面积图上一个桶只是折线上的一个点，桶少的时候（看九天）就该把整张图
# 撑满，而不是缩在中间两边留白——上限放到 200，只为了一两天时别拉得太散。
MAX_BAND = 200.0
IDEAL_W = 900
# 上沿实色线的粗细。1.8px 在浅底上够醒目，又不会粗到把窄峰糊成一块。
LINE_W = 1.8
# 面积填充的不透明度。线负责识别，面积只负责体量感，所以压得很淡（规范是
# 一层「水洗」，不是色块）；太浓的话 9 条叠起来整张图会闷，稀疏数据里那几个
# 孤立的峰更是重得刺眼。白底比原来的纸底更显色，所以从 0.28 降到 0.16。
AREA_FILL_OPACITY = 0.16


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
    legend: list[dict] = field(default_factory=list)   # 柱状图的图例：名字、颜色、合计


def render_stacked_areas(report, symbol: str = "$", ideal_width: int = IDEAL_W) -> Chart:
    """把 UsageReport 画成堆叠面积图。

    序列已按总额降序排好，所以金额大的堆在下面，视觉上更稳。

    report 只需要满足几个属性：dates / labels / series / column_totals / peak /
    granularity / dimension_label。usage_explorer.UsageReport 和
    cost_estimate.EstimateReport 都是照着这个形状来的。

    ideal_width 是不需要横向滚动时的画布宽：按图在页面上的大致宽度给，页面上就能
    按卡片宽度缩放而刻度字不变形；桶太多放不下时画布照样变宽、横向滚动。
    """
    dates = report.dates
    count = len(dates)
    totals = report.column_totals
    peak_index, peak_value = report.peak

    if not count or not report.series or max(totals, default=0.0) <= 0:
        height = PAD_T + PLOT_H + PAD_B
        svg = (
            f'<svg class="chart-svg" viewBox="0 0 {ideal_width} {height}" '
            f'width="{ideal_width}" height="{height}" role="img" '
            f'aria-label="所选区间内没有消费数据">'
            f'<text x="{ideal_width / 2:.0f}" y="{PAD_T + PLOT_H / 2:.0f}" '
            f'text-anchor="middle" fill="{TICK_TEXT}" font-size="13">'
            f"所选区间内没有消费数据</text></svg>"
        )
        return Chart(svg=svg, width=ideal_width, height=height, empty=True)

    # ---------------------------------------------------------- 尺寸
    available = ideal_width - PAD_L - PAD_R
    band = min(MAX_BAND, max(MIN_BAND, available / count))
    plot_w = band * count
    width = int(max(ideal_width, PAD_L + plot_w + PAD_R))
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
        f'<svg class="chart-svg area-chart" viewBox="0 0 {width} {height}" '
        f'width="{width}" height="{height}" role="img" '
        f'aria-label="按{"月" if report.granularity == "monthly" else "日"}'
        f'的{html.escape(report.dimension_label)}成本堆叠面积图，'
        f'共 {count} 个时间桶，{len(report.series)} 条序列">'
    )

    # 网格线与刻度：发丝实线，零线是基准要能站住，再深一档
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
            f'<text class="chart-peak" x="{peak_x:.2f}" y="{max(peak_y, PAD_T - 8):.2f}" '
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

# 折线图的线宽。和面积图上沿的 LINE_W（1.8）分开起名：同名的话后定义的会把前面那个盖掉
LINES_W = 2.0
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


# 时间轴的刻度间隔。从小到大挑第一个让刻度数不超过上限的，刻度只落在对齐这个
# 间隔的整点、整天上
TICK_STEPS = [
    60, 300, 600, 900, 1800, 3600, 2 * 3600, 3 * 3600, 6 * 3600, 12 * 3600,
    86400, 2 * 86400, 3 * 86400, 5 * 86400, 7 * 86400, 14 * 86400,
]
# 再长（按天看几个月到一年多）就按月标，刻度落在每月 1 号：每 1 / 2 / 3 / 6 / 12 个月一个
TICK_MONTHS = [1, 2, 3, 6, 12]
MONTH_SECONDS = 30.44 * 86400
TICK_MIN_GAP = 88.0       # 两个刻度之间至少留这么宽，刻度字之间要有明显的空白
SMOOTH_MAX_POINTS = 400   # 点再多就画折线：那个密度下曲线看不出区别，HTML 却要大好几倍
AREA_TOP_OPACITY = 0.16   # 线下渐变填充最浓处（贴着线）的不透明度，往下渐隐到 0


def _month_ticks(local, xs: list[float], span: float, most: int) -> list[tuple[float, str]]:
    """按月的刻度：每个月第一个点（按天的数据就是 1 号）上标，隔 1 / 2 / 3 / 6 / 12 个月一个，
    挑第一个能让刻度数不超过 most 的。隔半年以上的写「2026-03」，否则写「03-01」。"""
    months = next((m for m in TICK_MONTHS if span / (m * MONTH_SECONDS) <= most), TICK_MONTHS[-1])
    fmt = "%Y-%m" if months >= 6 else "%m-%d"
    ticks = []
    for i, t in enumerate(local):
        if i == 0 or (t.year, t.month) == (local[i - 1].year, local[i - 1].month):
            continue
        if (t.year * 12 + t.month - 1) % months == 0:
            ticks.append((xs[i], t.strftime(fmt)))
    return ticks


def _time_ticks(stamps, xs: list[float], plot_w: float, period: int) -> list[tuple[float, str]]:
    """按时间挑刻度：落在整点 / 整天上，零点那一格写日期，其余写时刻。

    以前是每隔 N 个点标一次，标出来的是「10-02 20:00」「10-03 08:00」这种不整的
    时间，一周按小时就是十几个挤在一起。现在先按跨度挑一个整的间隔，再只在对齐
    这个间隔的点上标：一周按小时就是每天零点一个「10-03」。
    """
    if not stamps:
        return []
    local = [s.astimezone() for s in stamps]
    if len(local) == 1:
        return [(xs[0], local[0].strftime("%m-%d %H:%M"))]
    span = (local[-1] - local[0]).total_seconds()
    most = max(2, int(plot_w // TICK_MIN_GAP))
    step = next((s for s in TICK_STEPS if span / s <= most), None)
    if step is None:
        # 几个月到一年多：按月标
        ticks = _month_ticks(local, xs, span, most)
        if len(ticks) >= 2:
            return ticks
        step = TICK_STEPS[-1]
    ticks: list[tuple[float, str]] = []
    for i, t in enumerate(local):
        seconds = t.hour * 3600 + t.minute * 60 + t.second
        if step < 86400:
            if seconds % step:
                continue
            ticks.append((xs[i], t.strftime("%m-%d") if seconds == 0 else t.strftime("%H:%M")))
            continue
        # 按天的间隔。小时以下的粒度只认零点那个点；按天的粒度一天只有一个点，
        # 不一定在零点（AWS 按 UTC 零点切天），取每天的那一个
        if period < 86400 and seconds:
            continue
        if period >= 86400 and i > 0 and t.date() == local[i - 1].date():
            continue
        if t.date().toordinal() % (step // 86400):
            continue
        ticks.append((xs[i], t.strftime("%m-%d")))
    if len(ticks) < 2:
        # 点没对齐到任何整的时刻（比如时区差半小时）：退回按间隔取点
        stride = max(1, math.ceil(len(local) / most))
        fmt = "%m-%d" if period >= 86400 else "%m-%d %H:%M"
        ticks = [(xs[i], local[i].strftime(fmt)) for i in range(0, len(local), stride)]
    return ticks


def _smooth_path(points: list[tuple[float, float]]) -> str:
    """单调三次样条（Fritsch–Carlson）：曲线经过每个点，两点之间不会冲过头——
    普通的平滑会在 0 附近冲到负数，画出「负的调用次数」。"""
    n = len(points)
    if n < 3:
        return _line_path(points)
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    dx = [xs[i + 1] - xs[i] for i in range(n - 1)]
    slope = [(ys[i + 1] - ys[i]) / dx[i] if dx[i] else 0.0 for i in range(n - 1)]
    tangent = [slope[0]] + [0.0] * (n - 2) + [slope[-1]]
    for i in range(1, n - 1):
        if slope[i - 1] * slope[i] <= 0:
            continue          # 拐点处切线放平，保证不冲过头
        w1, w2 = 2 * dx[i] + dx[i - 1], dx[i] + 2 * dx[i - 1]
        tangent[i] = (w1 + w2) / (w1 / slope[i - 1] + w2 / slope[i])
    out = [f"M{xs[0]:.1f},{ys[0]:.1f}"]
    for i in range(n - 1):
        h = dx[i] / 3
        out.append(
            f"C{xs[i] + h:.1f},{ys[i] + tangent[i] * h:.1f} "
            f"{xs[i + 1] - h:.1f},{ys[i + 1] - tangent[i + 1] * h:.1f} {xs[i + 1]:.1f},{ys[i + 1]:.1f}"
        )
    return " ".join(out)


def render_lines(report, unit: str = "", width: int = IDEAL_W, uid: str = "ln") -> Chart:
    """把 UsageMetricsReport（或形状一样的东西）画成折线图。

    样式：平滑曲线、线下一层往下渐隐的浅色填充、悬浮时一条虚线准线 + 每条线上
    一个带白圈的点。width 是 viewBox 的宽，页面上 SVG 会跟着卡片缩放，按图在页面上
    的大致宽度给，刻度字才是原本的 11px。uid 用来区分同一页上几张图的渐变 id。

    report 要有 labels / series / metric_label / unit / window；有 timestamps 时
    横轴按时间挑整点刻度，没有就按间隔取。
    """
    labels = report.labels
    count = len(labels)
    series_list = report.series
    peak_all = max((s.peak for s in series_list), default=0.0)

    if not count or not series_list or peak_all <= 0:
        height = PAD_T + PLOT_H + PAD_B
        svg = (
            f'<svg class="chart-svg" viewBox="0 0 {width} {height}" '
            f'width="{width}" height="{height}" role="img" '
            f'aria-label="所选区间内没有调用数据">'
            f'<text x="{width / 2:.0f}" y="{PAD_T + PLOT_H / 2:.0f}" '
            f'text-anchor="middle" fill="{TICK_TEXT}" font-size="13">'
            f"所选区间内没有调用数据</text></svg>"
        )
        return Chart(svg=svg, width=width, height=height, empty=True)

    label_ends = len(series_list) <= END_LABEL_MAX_SERIES
    pad_r = PAD_R_LABELED if label_ends else PAD_R
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

    safe_uid = html.escape(uid)
    parts: list[str] = [
        f'<svg class="chart-svg line-chart" viewBox="0 0 {width} {height}" '
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

    smooth = count <= SMOOTH_MAX_POINTS
    geometry = {
        series.slot: [(x_at(i), y_at(series.values[i])) for i in range(count)]
        for series in series_list
    }
    paths = {
        slot: (_smooth_path(points) if smooth else _line_path(points))
        for slot, points in geometry.items()
    }

    # 线下的渐变：贴着线最浓，往下渐隐到全透明。几条线的填充互相叠在一起时，
    # 重叠发生在各自已经很淡的地方，不会糊成一团
    if count > 1:
        parts.append("<defs>")
        for series in series_list:
            colour = color_for(series.slot)
            parts.append(
                f'<linearGradient id="{safe_uid}-fade{series.slot}" x1="0" y1="{PAD_T:.0f}" '
                f'x2="0" y2="{plot_bottom:.0f}" gradientUnits="userSpaceOnUse">'
                f'<stop offset="0" stop-color="{colour}" stop-opacity="{AREA_TOP_OPACITY}"></stop>'
                f'<stop offset="1" stop-color="{colour}" stop-opacity="0"></stop></linearGradient>'
            )
        parts.append('</defs><g class="chart-areas">')
        # 先画完所有填充再画线，否则后一条的填充会盖在前一条的线上
        for series in series_list:
            points = geometry[series.slot]
            parts.append(
                f'<path class="chart-area" d="{paths[series.slot]} L{points[-1][0]:.1f},{plot_bottom:.1f} '
                f'L{points[0][0]:.1f},{plot_bottom:.1f} Z" fill="url(#{safe_uid}-fade{series.slot})"></path>'
            )
        parts.append("</g>")

    parts.append('<g class="chart-lines">')
    for series in series_list:
        colour = color_for(series.slot)
        points = geometry[series.slot]
        if count == 1:
            parts.append(
                f'<circle cx="{points[0][0]:.2f}" cy="{points[0][1]:.2f}" '
                f'r="{MARKER_R:.1f}" fill="{colour}"></circle>'
            )
            continue
        # pathLength="1"：进场时「画出来」的动画按比例走，不用知道线有多长
        parts.append(
            f'<path class="chart-line" pathLength="1" d="{paths[series.slot]}" fill="none" '
            f'stroke="{colour}" stroke-width="{LINES_W}" stroke-linejoin="round" '
            f'stroke-linecap="round"></path>'
        )
        if count <= MARKER_MAX_POINTS:
            for x, y in points:
                parts.append(
                    f'<circle class="chart-marker" cx="{x:.2f}" cy="{y:.2f}" r="{MARKER_R:.1f}" '
                    f'fill="{colour}" stroke="{SURFACE}" stroke-width="2"></circle>'
                )
    parts.append("</g>")

    # 线尾直标末值。相互压字时宁可不标，交给图例和悬浮——把标签硬挤开会让它
    # 和自己那条线脱钩，比不标更难读
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

    stamps = getattr(report, "timestamps", None)
    if stamps and len(stamps) == count:
        ticks = _time_ticks(stamps, [x_at(i) for i in range(count)], plot_w, report.window.period)
    else:
        stride = max(1, math.ceil(TICK_MIN_GAP / step)) if step else 1
        ticks = [(x_at(i), labels[i]) for i in range(0, count, stride)]
    parts.append('<g class="chart-xaxis">')
    for x, text in ticks:
        # 贴边时改锚点而不是挪位置：挪了标签就和它对应的刻度脱钩
        half = len(text) * 3.4
        if x - half < 2:
            anchor = "start"
        elif x + half > width - 2:
            anchor = "end"
        else:
            anchor = "middle"
        parts.append(
            f'<line x1="{x:.2f}" y1="{plot_bottom:.0f}" x2="{x:.2f}" y2="{plot_bottom + 4:.0f}" '
            f'stroke="{BASELINE}" stroke-width="1"></line>'
            f'<text x="{x:.2f}" y="{plot_bottom + 20:.0f}" text-anchor="{anchor}" '
            f'fill="{TICK_TEXT}" font-size="11">{html.escape(text)}</text>'
        )
    parts.append("</g>")

    # 悬浮：一条虚线准线（底下带个小三角指着横轴）+ 每条线上一个带白圈的点，
    # 位置由 JS 按最近的时间点移动。用一个盖住绘图区的透明层接鼠标，而不是逐列
    # 命中区：点可以多到 1500 个，逐列的命中区窄到点不中
    parts.append('<g class="chart-cursor" aria-hidden="true">')
    parts.append(
        f'<g class="chart-cross"><line x1="0" y1="{PAD_T}" x2="0" y2="{plot_bottom}" '
        f'stroke="{TICK_TEXT}" stroke-width="1" stroke-dasharray="4 4"></line>'
        f'<path d="M-4.5,{plot_bottom + 9:.0f} L4.5,{plot_bottom + 9:.0f} L0,{plot_bottom + 2:.0f} Z" '
        f'fill="{LABEL_TEXT}"></path></g>'
    )
    for series in series_list:
        parts.append(
            f'<circle class="chart-focus" cx="0" cy="0" r="{MARKER_R + 1:.1f}" '
            f'fill="{color_for(series.slot)}" stroke="{SURFACE}" stroke-width="2.5"></circle>'
        )
    parts.append("</g>")
    parts.append(
        f'<rect class="chart-overlay" x="{PAD_L}" y="{PAD_T}" '
        f'width="{plot_w:.2f}" height="{PLOT_H}" tabindex="0" role="application" '
        f'aria-label="按左右方向键逐个时间点查看各模型的'
        f'{html.escape(report.metric_label)}"></rect>'
    )
    parts.append("</svg>")

    # 悬浮数据：名字和颜色每条序列只写一次，按时间点只放值和纵坐标——一周按小时是 168 个点，
    # 五个视图，每个点都重复写一遍模型名和颜色的话，光这份 JSON 就有好几百 KB
    tooltip = {
        "unit": unit or report.unit,
        "labels": list(labels),
        "x": [round(x_at(i), 1) for i in range(count)],
        "series": [
            {
                "name": series.name,
                "color": color_for(series.slot),
                "v": [round(v, 2) for v in series.values],
                "y": [round(y_at(v), 1) for v in series.values],
            }
            for series in series_list
        ],
    }

    return Chart(svg="".join(parts), width=width, height=height, tooltip=tooltip)


# --------------------------------------------------------------- 模型用量的主图
# 一张大折线图，「四区合计」和每个区各是它的一个视图，页面上用页签切换。
# 视图之间不并排出现，所以各用各的纵轴——共用刻度只会把小区压成一条贴地的线；
# 要横向比各区就看页签上的占比和明细表的区域列。
VIEW_W = 1120


@dataclass
class ChartView:
    key: str            # "all" 或区域代码，页签和视图靠它对上
    label: str          # 卡片标题里的名字：「四区合计」「弗吉尼亚 us-east-1」
    tab_label: str      # 页签上的短名
    region: str         # 空串 = 四区合计
    total: float
    share: float        # 占四区合计的百分比
    series: list = field(default_factory=list)  # 这个视图里有量的序列，图例用
    chart: Chart | None = None
    average: float = 0.0       # 每个时间点的平均
    peak_value: float = 0.0    # 单个时间点的最高（各模型相加）
    peak_label: str = ""       # 最高的那个时间点

    @property
    def empty(self) -> bool:
        return self.total <= 0


def render_views(report, width: int = VIEW_W) -> list[ChartView]:
    """四区合计排第一，各区按用量从大到小，这段时间没调用的区排最后（页签上置灰）。

    颜色跟着模型走而不是跟着排名走：各区的序列和合计用的是同一套色槽，
    某个区里没有的模型只是不画，剩下的不会换色。
    """
    grand = report.total

    def view(key: str, label: str, tab: str, region: str, series) -> ChartView:
        live = [s for s in series if s.total > 0]
        shape = SimpleNamespace(
            labels=report.labels, timestamps=report.timestamps, series=live,
            metric_label=report.metric_label, unit=report.unit, window=report.window,
        )
        amount = sum(s.total for s in live)
        columns = [sum(s.values[i] for s in live) for i in range(len(report.labels))]
        top = max(range(len(columns)), key=columns.__getitem__) if columns else -1
        return ChartView(
            key=key, label=label, tab_label=tab, region=region, total=amount,
            share=amount / grand * 100 if grand > 0 else 0.0, series=live,
            chart=render_lines(shape, report.unit, width=width, uid=f"v-{key}"),
            average=amount / len(columns) if columns else 0.0,
            peak_value=columns[top] if top >= 0 else 0.0,
            peak_label=report.labels[top] if top >= 0 and columns[top] > 0 else "",
        )

    views = [view("all", "四区合计", "四区合计", "", report.series)]
    for panel in sorted(report.panels, key=lambda p: -p.total):
        views.append(view(panel.region, panel.label, panel.region, panel.region, panel.series))
    return views


# --------------------------------------------------------------- 仪表盘上的小图
# 概览和模型用量页卡片拼版里的几种小图。都是纯 SVG 字符串，尺寸用 viewBox，
# 页面上跟着卡片缩放；数字和文字尽量放在 HTML 里（字体、换行都更可控），SVG 只画图形。

# 状态色（和 style.css 的 --ok / --warn / --danger 一致）。只表示严重程度，
# 绝不拿来当第几条序列的颜色
TONE_COLORS = {"ok": "#2c7652", "warn": "#8c610f", "danger": "#bc3b2e", "none": "#87867f"}


def _arc(cx: float, cy: float, r: float, start: float, end: float) -> str:
    """顺时针从 start 画到 end（度；0 = 正右方，SVG 的 y 朝下，所以顺时针为正）。"""
    a0, a1 = math.radians(start), math.radians(end)
    x0, y0 = cx + r * math.cos(a0), cy + r * math.sin(a0)
    x1, y1 = cx + r * math.cos(a1), cy + r * math.sin(a1)
    large = 1 if end - start > 180 else 0
    return f"M{x0:.2f},{y0:.2f} A{r:.2f},{r:.2f} 0 {large} 1 {x1:.2f},{y1:.2f}"


GAUGE_START = 160.0   # 从左下方起笔
GAUGE_SWEEP = 220.0   # 扫过顶部到右下方，开口朝下


def render_gauge(fraction: float | None, tone: str = "ok", width: int = 280, height: int = 172) -> str:
    """额度使用的半圆仪表。fraction 是 0~1，超过 1 按满格画（超了多少由文字说）。

    规范里的 meter：填充按严重程度上色，底槽是同一色相的浅一档，整条弧说的是同一件事。
    中间的数字由模板盖在上面，这里只画弧。

    进场动画（CSS，系统开了「减弱动画」就不动）：进度弧从起点画到终点（pathLength=1 再动
    dashoffset），端点的小圆点同步沿着弧转过去——它画在起点上，再绕圆心转 --sweep 度到终点。
    圆的弧长和转角成正比，两者用同一条缓动曲线，所以点始终压在弧的头上。不动画时直接就是
    最终的样子。
    """
    color = TONE_COLORS.get(tone, TONE_COLORS["ok"])
    stroke = 16.0
    # 220° 的弧，最低点在圆心下方 r·sin20° ≈ 0.342r 处
    r = min((width - stroke) / 2 - 4, (height - stroke - 6) / 1.342)
    cx, cy = width / 2, stroke / 2 + 4 + r
    parts = [
        f'<svg class="gauge-svg" viewBox="0 0 {width} {height}" width="{width}" height="{height}" '
        f'aria-hidden="true">',
        f'<path d="{_arc(cx, cy, r, GAUGE_START, GAUGE_START + GAUGE_SWEEP)}" fill="none" '
        f'stroke="{color}" stroke-opacity="0.14" stroke-width="{stroke}" stroke-linecap="round"></path>',
    ]
    share = 0.0 if fraction is None else max(0.0, min(1.0, fraction))
    if share > 0:
        sweep = max(GAUGE_SWEEP * share, 0.5)
        parts.append(
            f'<path class="gauge-arc" pathLength="1" d="{_arc(cx, cy, r, GAUGE_START, GAUGE_START + sweep)}" '
            f'fill="none" stroke="{color}" stroke-width="{stroke}" stroke-linecap="round"></path>'
        )
        start = math.radians(GAUGE_START)
        # 进度端点：同色实心点 + 一圈白，压在弧上也看得清。画在起点，靠旋转放到终点
        parts.append(
            f'<g class="gauge-knob" style="--sweep: {sweep:.2f}deg; transform-origin: {cx:.2f}px {cy:.2f}px">'
            f'<circle cx="{cx + r * math.cos(start):.2f}" cy="{cy + r * math.sin(start):.2f}" r="6" '
            f'fill="{color}" stroke="{SURFACE}" stroke-width="3"></circle></g>'
        )
    parts.append("</svg>")
    return "".join(parts)


def _touching(a, b, r: float, gap: float) -> list[tuple[float, float]]:
    """和圆 a、b 都外切（中间隔 gap）的、半径为 r 的圆心位置，最多两个。"""
    (ax, ay, ar), (bx, by, br) = a, b
    ra, rb = ar + r + gap, br + r + gap
    dx, dy = bx - ax, by - ay
    d = math.hypot(dx, dy)
    if d == 0 or d > ra + rb or d < abs(ra - rb):
        return []
    along = (ra * ra - rb * rb + d * d) / (2 * d)
    h = math.sqrt(max(0.0, ra * ra - along * along))
    mx, my = ax + dx * along / d, ay + dy * along / d
    return [(mx + h * dy / d, my - h * dx / d), (mx - h * dy / d, my + h * dx / d)]


def _pack(radii: list[float], gap: float) -> list[tuple[float, float, float]]:
    """把圆一个个贴上去：每个新圆都和已有的某两个圆相切，在不重叠的位置里挑离
    （按面积加权的）重心最近的那个。圆最多七八个，穷举切点就够了。"""
    placed: list[tuple[float, float, float]] = []
    for r in radii:
        if not placed:
            placed.append((0.0, 0.0, r))
            continue
        if len(placed) == 1:
            x0, y0, r0 = placed[0]
            angle = -math.pi / 6            # 第二个放在第一个的右上方
            d = r0 + r + gap
            placed.append((x0 + d * math.cos(angle), y0 + d * math.sin(angle), r))
            continue
        weight = sum(p[2] ** 2 for p in placed)
        cx = sum(p[0] * p[2] ** 2 for p in placed) / weight
        cy = sum(p[1] * p[2] ** 2 for p in placed) / weight
        best: tuple[float, float, float] | None = None
        for i in range(len(placed)):
            for j in range(i + 1, len(placed)):
                for px, py in _touching(placed[i], placed[j], r, gap):
                    if all(math.hypot(px - q[0], py - q[1]) >= q[2] + r + gap - 1e-6 for q in placed):
                        dist = math.hypot(px - cx, py - cy)
                        if best is None or dist < best[0]:
                            best = (dist, px, py)
        if best is None:   # 理论上走不到：兜底贴在最右边
            best = (0.0, max(p[0] + p[2] for p in placed) + gap + r, 0.0)
        placed.append((best[1], best[2], r))
    return placed


def render_bubbles(items: list[tuple[str, float, str]], width: int = 300, height: int = 210, fmt=None) -> str:
    """面积按数值比例的气泡：一眼看出谁占大头。占比写在泡里，准确的数在图例和悬浮里。

    items 是 (名字, 数值, 颜色)。颜色跟着实体走（同一个东西在别的图里也是这个色），
    填充是同色的浅底，字一律用墨色——浅底上的彩色字不够清楚。fmt 把数值写成悬浮
    提示里的样子（金额、次数），默认是紧凑数字。

    动效（都在 CSS 里，系统开了「减弱动画」就不动）：进场时一个个弹出来，之后各自
    慢慢上下漂；悬浮的那个放大一点，其余变淡。泡可以用鼠标拖着走，挤到别的泡会把
    它们推开，松手后都弹回原位（app.js）。
    """
    fmt = fmt or compact_number
    live = sorted(((n, v, c) for n, v, c in items if v > 0), key=lambda item: -item[1])
    if not live:
        return (
            f'<svg class="bubble-svg" viewBox="0 0 {width} {height}" width="{width}" height="{height}" '
            f'role="img" aria-label="没有数据"><text x="{width / 2:.0f}" y="{height / 2:.0f}" '
            f'text-anchor="middle" fill="{TICK_TEXT}" font-size="13">没有数据</text></svg>'
        )
    total = sum(v for _, v, _ in live)
    top = live[0][1]
    placed = _pack([math.sqrt(v / top) * 100 for _, v, _ in live], gap=5.0)
    # 留出上下漂动的余量，泡漂起来不会被裁掉
    pad = 8.0
    left = min(x - r for x, _, r in placed)
    right = max(x + r for x, _, r in placed)
    upper = min(y - r for _, y, r in placed)
    lower = max(y + r for _, y, r in placed)
    scale = min((width - 2 * pad) / (right - left), (height - 2 * pad) / (lower - upper))
    ox = (width - (right - left) * scale) / 2 - left * scale
    oy = (height - (lower - upper) * scale) / 2 - upper * scale

    parts = [
        f'<svg class="bubble-svg" viewBox="0 0 {width} {height}" width="{width}" height="{height}" '
        f'role="group" aria-label="占比">'
    ]
    for k, ((x, y, r), (name, value, color)) in enumerate(zip(placed, live)):
        bx, by, br = ox + x * scale, oy + y * scale, r * scale
        share = value / total * 100
        text = "<1%" if share < 1 else f"{share:.0f}%"
        # 每个泡漂的节奏不一样（周期 4.6~6.4 秒、相位错开），看着才像各自漂着，
        # 而不是整齐地一起跳
        duration = 4.6 + (k * 0.7) % 1.8
        delay = -((k * 1.3) % duration)
        parts.append(
            f'<g class="bubble" tabindex="0" role="img" '
            f'aria-label="{html.escape(name)} {html.escape(fmt(value))}，占 {text}" '
            f'data-name="{html.escape(name)}" data-value="{html.escape(fmt(value))}" data-share="{text}" '
            f'style="--i:{k};--dur:{duration:.1f}s;--delay:{delay:.1f}s">'
            # 三层：最外层 CSS 在漂，中间这层是拖动的位移（JS 写 transform 属性，
            # 不和 CSS 的漂动抢同一个 transform），最里层是弹出和悬浮放大
            f'<g class="bubble-drag"><g class="bubble-pop">'
            f'<circle cx="{bx:.2f}" cy="{by:.2f}" r="{br:.2f}" fill="{color}" fill-opacity="0.2"></circle>'
        )
        # 泡太小放不下字就不写，图例里有
        if br >= 15:
            size = max(11.0, min(26.0, br * 0.42))
            parts.append(
                f'<text x="{bx:.2f}" y="{by + size * 0.36:.2f}" text-anchor="middle" fill="{LABEL_TEXT}" '
                f'font-size="{size:.1f}" font-weight="600">{text}</text>'
            )
        parts.append("</g></g></g>")
    parts.append("</svg>")
    return "".join(parts)


def _round_top(x: float, top: float, w: float, bottom: float, radius: float) -> str:
    """顶端两个圆角、底端方角的柱子：数据那头圆，贴基线那头方。"""
    r = max(0.0, min(radius, w / 2, bottom - top))
    return (
        f"M{x:.2f},{bottom:.2f} L{x:.2f},{top + r:.2f} Q{x:.2f},{top:.2f} {x + r:.2f},{top:.2f} "
        f"L{x + w - r:.2f},{top:.2f} Q{x + w:.2f},{top:.2f} {x + w:.2f},{top + r:.2f} "
        f"L{x + w:.2f},{bottom:.2f} Z"
    )


BAR_MAX_W = 24.0   # 柱子最粗 24px，不把一格填满，剩下的是留白
BAR_GAP = 2.0      # 堆叠的段与段之间留 2px 底色的缝，靠缝分开，不靠描边


def _colour(series) -> str:
    """序列的颜色：一般按色槽取；只有一条序列、想用别的颜色时（比如账号页的成本柱子
    用陶土色），序列上带一个 color 就用它。"""
    return getattr(series, "color", None) or color_for(series.slot)


def render_bars(
    labels: list[str], series, width: int = 560, height: int = 196, fmt=None, axis=None, sort: bool = True,
) -> Chart:
    """堆叠柱状图，每格一根柱。series 的元素要有 name / slot / values。

    大的序列堆在底下；sort=False 时按给的顺序堆（第一个在最底下），用在顺序本身有意思的
    时候——比如「AWS 原价在下、毛利在上，整根是收入」，不能因为毛利比原价多就颠倒过来。fmt 把数值写成悬浮提示里的样子（金额或次数），axis 是纵轴刻度
    的写法（要短，金额用 compact_money），两个默认都是紧凑数字。
    交互和动效：进场时一根根长出来；鼠标划到哪一格，那一格亮、其余变淡，提示里列出
    这一格的全部序列；点图例里的某一项，只亮这一项（再点一次恢复）。脚本在 app.js，
    动画在 CSS 里，系统开了「减弱动画」就不动。
    """
    fmt = fmt or compact_number
    axis = axis or compact_number
    count = len(labels)
    live = [s for s in series if sum(s.values) > 0]
    if sort:
        live.sort(key=lambda s: -sum(s.values))
    totals = [sum(s.values[i] for s in live) for i in range(count)]
    pad_l, pad_r, pad_t, pad_b = 48.0, 6.0, 12.0, 26.0
    plot_h = height - pad_t - pad_b
    plot_bottom = pad_t + plot_h
    legend = [{"name": s.name, "color": _colour(s), "total": sum(s.values)} for s in live]
    if not count or not live:
        svg = (
            f'<svg class="chart-svg" viewBox="0 0 {width} {height}" width="{width}" height="{height}" '
            f'role="img" aria-label="没有数据"><text x="{width / 2:.0f}" y="{pad_t + plot_h / 2:.0f}" '
            f'text-anchor="middle" fill="{TICK_TEXT}" font-size="13">这段时间没有数据</text></svg>'
        )
        return Chart(svg=svg, width=width, height=height, empty=True)

    band = (width - pad_l - pad_r) / count
    bar_w = min(BAR_MAX_W, band * 0.6)
    step = _nice_step(max(totals) or 1.0, 3)
    ticks = max(1, math.ceil((max(totals) or 1.0) / step - 1e-9))
    scale = plot_h / (step * ticks)

    parts = [
        f'<svg class="chart-svg bar-chart" viewBox="0 0 {width} {height}" width="{width}" height="{height}" '
        f'role="img" aria-label="堆叠柱状图，{count} 格，{len(live)} 条序列">',
        '<g class="chart-grid">',
    ]
    for tick in range(ticks + 1):
        y = plot_bottom - step * tick * scale
        parts.append(
            f'<line x1="{pad_l:.0f}" y1="{y:.2f}" x2="{width - pad_r:.0f}" y2="{y:.2f}" '
            f'stroke="{BASELINE if tick == 0 else GRID}" stroke-width="1"></line>'
            f'<text x="{pad_l - 8:.0f}" y="{y + 4:.2f}" text-anchor="end" fill="{TICK_TEXT}" '
            f'font-size="10.5" style="font-variant-numeric:tabular-nums">'
            f"{html.escape(axis(step * tick))}</text>"
        )
    parts.append('</g><g class="chart-bars">')
    for i in range(count):
        x = pad_l + band * i + (band - bar_w) / 2
        stack = [(k, s, s.values[i]) for k, s in enumerate(live) if s.values[i] > 0]
        bottom = plot_bottom
        parts.append(f'<g class="bar-col" data-idx="{i}" style="--i:{i}">')
        for n, (k, s, value) in enumerate(stack):
            top = bottom - value * scale
            last = n == len(stack) - 1
            # 上面还有段的，顶端让出一道缝
            seg_top = top if last else min(bottom, top + BAR_GAP)
            if bottom - seg_top >= 0.4:
                colour = _colour(s)
                if last:
                    parts.append(
                        f'<path data-s="{k}" d="{_round_top(x, seg_top, bar_w, bottom, 4.0)}" '
                        f'fill="{colour}"></path>'
                    )
                else:
                    parts.append(
                        f'<rect data-s="{k}" x="{x:.2f}" y="{seg_top:.2f}" width="{bar_w:.2f}" '
                        f'height="{bottom - seg_top:.2f}" fill="{colour}"></rect>'
                    )
            bottom = top
        parts.append("</g>")
    parts.append('</g><g class="chart-xaxis">')
    stride = max(1, math.ceil(44.0 / band))
    for i in range(0, count, stride):
        parts.append(
            f'<text x="{pad_l + band * i + band / 2:.2f}" y="{plot_bottom + 17:.0f}" text-anchor="middle" '
            f'fill="{TICK_TEXT}" font-size="10.5">{html.escape(labels[i])}</text>'
        )
    parts.append('</g><g class="chart-hits">')
    for i in range(count):
        parts.append(
            f'<rect class="chart-hit" data-idx="{i}" x="{pad_l + band * i:.2f}" y="{pad_t:.0f}" '
            f'width="{band:.2f}" height="{plot_h:.0f}" tabindex="0" role="button" '
            f'aria-label="{html.escape(labels[i])} 合计 {html.escape(fmt(totals[i]))}"></rect>'
        )
    parts.append("</g></svg>")

    tooltip = [
        {
            "label": labels[i],
            "total": fmt(totals[i]),
            "rows": [
                {"name": s.name, "color": _colour(s), "value": fmt(s.values[i]), "s": k}
                for k, s in sorted(enumerate(live), key=lambda pair: -pair[1].values[i])
                if s.values[i] > 0
            ],
        }
        for i in range(count)
    ]
    return Chart(svg="".join(parts), width=width, height=height, tooltip=tooltip, legend=legend)


def render_spark(values: list[float], accent: str = "#d97757", width: int = 112, height: int = 28) -> str:
    """卡片里的迷你柱图：前面几天用退后一步的灰，最后一天（今天）用强调色。进场时一根根长出来（CSS）。"""
    count = len(values)
    if not count:
        return ""
    top = max(values) or 1.0
    band = width / count
    w = max(1.5, band - 2.0)
    parts = [
        f'<svg class="spark-svg" viewBox="0 0 {width} {height}" width="{width}" height="{height}" '
        f'aria-hidden="true">'
    ]
    for i, value in enumerate(values):
        x = band * i + (band - w) / 2
        if value <= 0:
            # 没调用的那天画一道贴底的短线，区分「0」和「没画出来」
            parts.append(
                f'<rect class="spark-bar" style="--i:{i}" x="{x:.2f}" y="{height - 1.5:.2f}" width="{w:.2f}" '
                f'height="1.5" fill="{BASELINE}"></rect>'
            )
            continue
        h = max(2.0, value / top * (height - 2))
        fill = accent if i == count - 1 else "#c2c0b6"
        parts.append(
            f'<path class="spark-bar" style="--i:{i}" d="{_round_top(x, height - h, w, height, 1.5)}" '
            f'fill="{fill}"></path>'
        )
    parts.append("</svg>")
    return "".join(parts)


def downsample(values: list[float], buckets: int = 28) -> list[float]:
    """把一长串按时间的数合并成至多 buckets 段（每段求和），给迷你柱图用。"""
    if len(values) <= buckets:
        return list(values)
    size = len(values) / buckets
    return [sum(values[int(i * size):int((i + 1) * size)]) for i in range(buckets)]


# --------------------------------------------------------------- 热力格
# 多少用一个色相的深浅表示（规范里的 sequential：单一色相、亮度单调），陶土色从浅到深。
# 格子里同时写着数，颜色只是帮着扫一眼。字色按底色深浅取墨色或白色，都过了 4.5:1。
HEAT_RAMP = ["#f6e7de", "#efcdbb", "#e5ad92", "#d98a69", "#c46849", "#9d4a2e"]
HEAT_TEXT = [LABEL_TEXT, LABEL_TEXT, LABEL_TEXT, LABEL_TEXT, LABEL_TEXT, "#ffffff"]


def heat_style(value: float, top: float) -> str:
    """一格的 inline style。0 或没有数据返回空串，模板给它 .heat-zero（底色，不上色）。"""
    if value <= 0 or top <= 0:
        return ""
    step = min(len(HEAT_RAMP) - 1, int(value / top * len(HEAT_RAMP)))
    return f"background:{HEAT_RAMP[step]};color:{HEAT_TEXT[step]}"


WEEKDAYS = "一二三四五六日"


def week_grid(stamps, values: list[float], period: int) -> list[list[float | None]] | None:
    """「一周作息」：7 行（周一到周日）× 24 列（0~23 点），每格是这个星期几、这个钟点
    平均每小时多少。粒度比一小时细的先按小时加总；按天的数据看不出钟点，返回 None。
    窗口里没覆盖到的格子是 None（页面上留空），和「有数据但是 0」区分开。
    """
    if period >= 86400 or not stamps:
        return None
    hourly: dict[tuple, float] = {}
    for stamp, value in zip(stamps, values):
        local = stamp.astimezone()
        key = (local.date(), local.hour)
        hourly[key] = hourly.get(key, 0.0) + value
    cells: dict[tuple[int, int], list[float]] = {}
    for (day, hour), total in hourly.items():
        cells.setdefault((day.weekday(), hour), []).append(total)
    return [
        [
            (sum(cells[(weekday, hour)]) / len(cells[(weekday, hour)])) if (weekday, hour) in cells else None
            for hour in range(24)
        ]
        for weekday in range(7)
    ]
