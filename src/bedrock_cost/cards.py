"""Telegram 告警卡片：把一条告警画成一张深色卡片图，配一段文字说明一起发。

Telegram 的文字消息只有粗体、等宽、引用这几种格式，做不出颜色、边框、徽章、进度条
和大号数字，所以每条告警画成一张 PNG，用 sendPhoto 发。图片下面的 caption 写账号 ID
和关键数字：通知预览、群内搜索、复制 UID 都靠它；卡片万一画不出来，也是退回发它。

分两层：
  · Card 和几种块（Uid / Tiles / Meter / Table / Notes / Info / Since / Paragraph /
    Status / Details / Quote / Divider）只描述「卡片上有什么」。alerts 和 mail_alerts 里
    拼出来，测试直接断言它们；
  · render() 把 Card 画成 PNG。量尺寸和真正画走同一套布局代码（_Pen 不带画布时只量
    不画），两边不会对不上。

字体随包带着（fonts/，都是 OFL 1.1）：Noto Sans SC 可变字体管中文、数字和英文，
JetBrains Mono 管 UID。不用系统字体，本地和服务器画出来一模一样；也固定用 Pillow 的
基础排版引擎，装没装 raqm 结果都一样。标题前的图标（柱状图、状态圆点、警告三角……）
是这里用几何图形画的，不依赖 emoji 字体。

先按 4 倍画、最后缩到 2 倍出图：Pillow 画圆、圆角矩形、多边形都没有抗锯齿，超采样
一次边缘才平滑。设计宽度 600 px，出图 1200 px——Telegram 普通画质最长边 1280，
不会被再压一遍。
"""

from __future__ import annotations

import io
import math
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFont

from . import avatars, config
from .chart import _nice_step, short_money
from .telegram import visible

FONT_DIR = Path(__file__).with_name("fonts")
SANS_FONT = FONT_DIR / "NotoSansSC-Variable.ttf"
MONO_FONT = FONT_DIR / "JetBrainsMono-Variable.ttf"

WIDTH = 600           # 设计宽度，下面所有尺寸都按这个量
OUTPUT_SCALE = 2      # 出图倍数：1200 px 宽
SUPERSAMPLE = 2       # 在出图尺寸上再放大一倍画，缩回去当抗锯齿
_K = OUTPUT_SCALE * SUPERSAMPLE

# 日报一张卡最多放几个账号，再多就分几张发（见 alerts.daily_cards）。15 行的卡片出图
# 约 1200 × 2600，caption 也还在 Telegram 的 1024 字以内
MAX_TABLE_ROWS = 15

MARGIN = 12    # 卡片外留一圈黑边：Telegram 会把图片切成圆角，贴边的彩色边框会被切掉
PAD = 24       # 卡片内边距
RADIUS = 18
GAP = 16       # 块与块之间

BLACK = (0, 0, 0)
WHITE = (255, 255, 255)
TEXT_2 = (190, 190, 190)   # 标签、说明
TEXT_3 = (135, 135, 135)   # 署名、占位
LINE = (46, 46, 46)
PANEL = (26, 26, 26)       # 数字格、说明框的底色
CHIP = (32, 32, 32)        # 小标签（「运行中」「已停用」）没有颜色时的底色
TRACK = (42, 42, 42)       # 进度条的底槽
QUOTE_TEXT = (214, 214, 214)  # 引用的邮件原文：比正文暗一点，看得出是贴过来的
BASELINE = (74, 74, 74)    # 折线图的零线：比网格线亮一档

# 账号头像的底色和字色：和 style.css 的 .av-0 … .av-7 一样，同一个账号在网页上、卡片上是一个颜色
AVATAR_COLORS = [
    ((243, 217, 203), (138, 63, 34)), ((214, 228, 242), (43, 90, 138)),
    ((211, 230, 223), (47, 107, 87)), ((224, 221, 241), (79, 74, 140)),
    ((239, 227, 200), (122, 90, 18)), ((242, 214, 220), (140, 51, 70)),
    ((223, 232, 208), (77, 107, 42)), ((230, 228, 220), (77, 76, 72)),
]


@dataclass(frozen=True)
class Tone:
    edge: tuple[int, int, int]    # 卡片边框、进度条
    text: tuple[int, int, int]    # 彩色的字和数字
    wash: tuple[int, int, int]    # 徽章底色


TONES = {
    "ok": Tone(edge=(34, 170, 90), text=(84, 200, 120), wash=(12, 44, 26)),
    "warn": Tone(edge=(228, 128, 22), text=(245, 150, 40), wash=(56, 32, 8)),
    "danger": Tone(edge=(226, 50, 58), text=(248, 92, 92), wash=(60, 14, 18)),
    "info": Tone(edge=(59, 130, 246), text=(100, 168, 250), wash=(14, 30, 58)),   # 测试消息
    "gray": Tone(edge=(104, 118, 140), text=(222, 222, 226), wash=(46, 46, 50)),  # 账号停用
}


def _color(tone: str) -> tuple[int, int, int]:
    """字的颜色：空 = 白，muted = 灰，其余按 TONES。"""
    if not tone:
        return WHITE
    if tone == "muted":
        return TEXT_3
    return TONES[tone].text


# ---------------------------------------------------------------- 卡片上有什么
@dataclass
class Uid:
    """「账号」标签 + 账号头像，主行账号邮箱、副行账号 ID（等宽字）——和日报表格里一个写法。号码一眼认不出
    是谁，邮箱能；没填邮箱的主行就是 ID。有没有邮箱一样高；邮箱太长就截断补省略号，不画出这一块的宽度。"""

    uid: str
    label: str = "账号"
    email: str = ""
    face: Face | None = None    # 没给就按邮箱、号码算（和网页上默认的头像一样）

    HEIGHT = 62
    R = 18           # 头像半径

    def strings(self) -> list[str]:
        return [self.label, self.email, self.uid] if self.email else [self.label, self.uid]

    def layout(self, pen: _Pen, x: float, y: float, w: float, tone: Tone) -> float:
        pen.text(x, y + 9, self.label, _font(13), TEXT_2)
        middle = y + 26 + self.R
        _draw_face(pen, self.face or account_face(None, self.email, self.uid), x + self.R, middle, self.R)
        left = x + self.R * 2 + 12
        if self.email:
            main = _font(16, 600)
            pen.text(left, middle - 9, _fit(self.email, main, x + w - left), main, WHITE)
            pen.text(left, middle + 11, self.uid, _font(12.5, mono=True), TEXT_2)
        else:
            pen.text(left, middle, self.uid, _font(16, 700, mono=True), WHITE)
        return self.HEIGHT


@dataclass
class Tile:
    """一格数字：上面一行标签，下面一个值。"""

    label: str
    value: str
    tone: str = ""        # 值的颜色，见 _color
    big: bool = False     # 大号数字
    edge: bool = False    # 这一格描一圈和卡片同色的边
    note: str = ""        # 值下面再补一行
    note_tone: str = ""


@dataclass
class Tiles:
    """一排数字格，平分宽度、等高。"""

    tiles: list[Tile]

    def strings(self) -> list[str]:
        return [s for t in self.tiles for s in (t.label, t.value, t.note) if s]

    def layout(self, pen: _Pen, x: float, y: float, w: float, tone: Tone) -> float:
        gap = 12
        width = (w - gap * (len(self.tiles) - 1)) / len(self.tiles)
        height = max(self._height(t) for t in self.tiles)
        for index, tile in enumerate(self.tiles):
            left = x + index * (width + gap)
            pen.box(
                (left, y, left + width, y + height), 14, fill=PANEL,
                outline=tone.edge if tile.edge else None, width=1.2,
            )
            pen.text(left + 16, y + 16 + 9, tile.label, _font(13), TEXT_2)
            value_h = 42 if tile.big else 26
            font = _font(34, 600) if tile.big else _font(19, 700)
            pen.text(left + 16, y + 42 + value_h / 2, tile.value, font, _color(tile.tone))
            if tile.note:
                pen.text(left + 16, y + 42 + value_h + 2 + 11, tile.note, _font(15, 500), _color(tile.note_tone))
        return height

    @staticmethod
    def _height(tile: Tile) -> float:
        return 16 + 18 + 8 + (42 if tile.big else 26) + (24 if tile.note else 0) + 16


@dataclass
class Meter:
    """额度使用率：大号百分比 + 进度条 + 条下面左右两行小字。"""

    label: str
    value: str
    fraction: float
    left: str
    right: str

    def strings(self) -> list[str]:
        return [self.label, self.value, self.left, self.right]

    def layout(self, pen: _Pen, x: float, y: float, w: float, tone: Tone) -> float:
        # 标签和大号百分比坐在同一条基线上，不是各自居中
        baseline = y + 36
        pen.text(x, baseline, self.label, _font(14), TEXT_2, anchor="ls")
        pen.text(x + w, baseline, self.value, _font(34, 600), tone.text, anchor="rs")
        bar = y + 52
        pen.box((x, bar, x + w, bar + 12), 6, fill=TRACK)
        filled = max(0.0, min(1.0, self.fraction)) * w
        if filled > 0:
            # 太短画不出圆角：至少画一个圆点那么长
            pen.box((x, bar, x + max(filled, 12), bar + 12), 6, fill=tone.edge)
        pen.text(x, bar + 22 + 9, self.left, _font(13), TEXT_2)
        pen.text(x + w, bar + 22 + 9, self.right, _font(13), TEXT_2, anchor="rm")
        return 52 + 12 + 10 + 18


@dataclass(frozen=True)
class Face:
    """账号头像：圆圈里一个字（邮箱首字母之类），底色、字色见 AVATAR_COLORS。"""

    text: str
    bg: tuple[int, int, int]
    fg: tuple[int, int, int]


def face(text: str, tone: int) -> Face:
    """按 avatars.Avatar 的 text、tone 配好颜色。"""
    bg, fg = AVATAR_COLORS[tone % len(AVATAR_COLORS)]
    return Face(text, bg, fg)


def account_face(avatar: avatars.Avatar | None, email: str, number: str) -> Face:
    """一个账号在卡片上的头像，字和颜色和网页上一样（avatar 是 Account.avatar；没给就按邮箱、号码算）。
    卡片画不了彩色表情：台账里选了表情的，写邮箱（没有就号码）的首字母。"""
    avatar = avatar or avatars.avatar_for(email, number)
    text = avatar.text
    if avatar.emoji:
        text = next((char.upper() for char in (email or number) if char.isalnum()), "?")
    return face(text, avatar.tone)


@dataclass
class Cell:
    text: str
    tone: str = ""    # 字的颜色，见 _color
    mark: str = ""    # 只用在账号列：后面加一个彩色小圆点，表示这一行在下面有说明
    # 值下面再补一行灰色小字（「截至 09-28」「↑ $1,298」）；这一行会跟着变高。账号列里写的是账号 ID
    note: str = ""
    face: Face | None = None   # 只用在账号列：最前面的头像


@dataclass
class Table:
    """日报的表格。第一列是账号：头像，主行账号邮箱，副行账号 ID（等宽字）；没填邮箱的主行就是 ID。
    其余列右对齐，按列里最宽的字定宽，剩下的都给第一列——邮箱长的放不下就截断补省略号。"""

    columns: list[str]
    rows: list[list[Cell]]

    HEAD = 34
    ROW = 46
    NOTED_ROW = 60   # 有一格带小字时的行高
    GAP = 14         # 列与列之间
    FACE = 26        # 头像直径
    FACE_GAP = 10    # 头像和邮箱之间
    CELL = 14.5      # 数字的字号：比别的卡片小半号，给第一列的邮箱多留点地方

    def strings(self) -> list[str]:
        return [*self.columns, *(s for row in self.rows for cell in row for s in (cell.text, cell.note) if s)]

    def _height(self, row: list[Cell]) -> float:
        return self.NOTED_ROW if any(cell.note for cell in row) else self.ROW

    def edges(self, w: float) -> tuple[float, list[float]]:
        """(第一列能用多宽, 其余每一列字的右边)，都从左边 0 算起。"""
        head, cell_font, note_font = _font(14, 700), _font(self.CELL), _font(11.5)
        rights, right = [], w
        for index in range(len(self.columns) - 1, 0, -1):
            widest = max([_width(self.columns[index], head)] + [
                max(_width(row[index].text, cell_font), _width(row[index].note, note_font)) for row in self.rows
            ])
            rights.insert(0, right)
            right -= widest + self.GAP
        return right, rights

    def layout(self, pen: _Pen, x: float, y: float, w: float, tone: Tone) -> float:
        first, rights = self.edges(w)
        head = _font(14, 700)
        pen.text(x, y + self.HEAD / 2, self.columns[0], head, WHITE)
        for name, right in zip(self.columns[1:], rights):
            pen.text(x + right, y + self.HEAD / 2, name, head, WHITE, anchor="rm")

        cell_font, note_font = _font(self.CELL), _font(11.5)
        top = y + self.HEAD
        for row in self.rows:
            height = self._height(row)
            pen.line(x, top, x + w, top, LINE)
            middle = top + height / 2
            self._account(pen, row[0], x, middle, first)
            for cell, right in zip(row[1:], rights):
                # 带小字的格子：值往上挪，小字放在值下面
                value_y = middle - 8 if cell.note else middle
                pen.text(x + right, value_y, cell.text, cell_font, _color(cell.tone), anchor="rm")
                if cell.note:
                    pen.text(x + right, middle + 12, cell.note, note_font, TEXT_2, anchor="rm")
            top += height
        return self.HEAD + sum(self._height(row) for row in self.rows)

    def _account(self, pen: _Pen, cell: Cell, x: float, middle: float, room: float) -> None:
        left = x
        if cell.face:
            _draw_face(pen, cell.face, x + self.FACE / 2, middle, self.FACE / 2)
            left += self.FACE + self.FACE_GAP
        # 没填邮箱：主行就是账号 ID，等宽字
        main = _font(13.5, 700, mono=True) if cell.text.isdigit() else _font(13.5, 600)
        center = middle - 8 if cell.note else middle
        text = _fit(cell.text, main, x + room - left - (16 if cell.mark else 0))
        pen.text(left, center, text, main, WHITE)
        if cell.mark:
            dot = left + _width(text, main) + 8
            pen.ellipse((dot, center - 4, dot + 8, center + 4), fill=_color(cell.mark))
        if cell.note:
            small = _font(11.5, mono=True)
            pen.text(left, middle + 12, _fit(cell.note, small, x + room - left), small, TEXT_2)


@dataclass
class TrendLine:
    name: str                       # 账号邮箱（没填就是 ID）；「其他」是并进去的那几个
    values: list[float]             # 每天一个数，和 Trend.labels 一样长
    color: tuple[int, int, int]
    face: Face | None = None        # 线尾的头像；「其他」没有，画个灰点


@dataclass
class Trend:
    """日报表格上面的折线图：一个账号一条线，每天一个点。线是平滑的、不冲过头（不会画出负数），线下一层往下
    渐隐的填充；线尾是账号的头像（圈是线的颜色）和最后一天的数，挨在一起的上下错开、拉一根细线连回线尾。"""

    title: str
    labels: list[str]               # 横轴：每天一个（MM-DD）
    lines: list[TrendLine]
    hint: str = ""                  # 标题右边的灰色小字
    notes: list[str] = field(default_factory=list)   # 图下面的灰色小字（没画线的账号）

    PLOT_H = 150
    AXIS_W = 46     # 左边纵轴的字
    END_W = 84      # 右边线尾的头像和数
    R = 9           # 线尾头像的半径
    SPACING = 21    # 线尾头像之间至少隔多远

    def strings(self) -> list[str]:
        return [s for s in (self.title, self.hint, *self.labels, *(line.name for line in self.lines), *self.notes) if s]

    def layout(self, pen: _Pen, x: float, y: float, w: float, tone: Tone) -> float:
        pen.text(x, y + 9, self.title, _font(13, 700), TEXT_2)
        if self.hint:
            pen.text(x + w, y + 9, self.hint, _font(12), TEXT_3, anchor="rm")
        top = y + 18 + 16
        bottom = top + self.PLOT_H
        left, right = x + self.AXIS_W, x + w - self.END_W
        count = len(self.labels)
        most = max([value for line in self.lines for value in line.values] + [0.0])
        step = _nice_step(most or 1.0, 4)
        ceiling = step * max(1, math.ceil((most or 1.0) / step - 1e-9))

        def at_x(index: int) -> float:
            return left + (right - left) * index / max(1, count - 1)

        def at_y(value: float) -> float:
            return bottom - (bottom - top) * max(0.0, value) / ceiling

        small = _font(11)
        tick = 0.0
        while tick <= ceiling + 1e-9:
            pen.line(left, at_y(tick), right, at_y(tick), BASELINE if not tick else LINE)
            pen.text(left - 8, at_y(tick), short_money(tick, config.CURRENCY_SYMBOL), small, TEXT_3, anchor="rm")
            tick += step
        for index, label in enumerate(self.labels):
            pen.text(at_x(index), bottom + 14, label, small, TEXT_3, anchor="mm")

        drawn = []
        for line in self.lines:
            points = [(at_x(index), at_y(value)) for index, value in enumerate(line.values)]
            curve = _smooth(points)
            drawn.append((line, points, curve))
            # 填充先画，压在所有线下面；从图顶往下淡出
            pen.area([*curve, (curve[-1][0], bottom), (curve[0][0], bottom)], line.color, top, bottom)
        for line, _, curve in drawn:
            pen.lines(curve, line.color, width=2)
        for line, points, _ in drawn:
            for px, py in points:
                pen.ellipse((px - 3.2, py - 3.2, px + 3.2, py + 3.2), fill=line.color, outline=BLACK, width=1.2)

        # 线尾：从上到下排好，挤在一起的错开
        ends = sorted(((points[-1][1], line) for line, points, _ in drawn), key=lambda pair: pair[0])
        spots = _spread([end for end, _ in ends], top + self.R, bottom - self.R, self.SPACING)
        cx = right + 10 + self.R
        for (end, line), spot in zip(ends, spots):
            if abs(spot - end) > 1:
                pen.line(right + 4, end, cx - self.R, spot, _mix(line.color, BLACK, 0.45))
            if line.face:
                _draw_face(pen, line.face, cx, spot, self.R, ring=line.color)
            else:
                pen.ellipse((cx - 5, spot - 5, cx + 5, spot + 5), fill=line.color)
            value = short_money(line.values[-1], config.CURRENCY_SYMBOL) if line.values else ""
            pen.text(cx + self.R + 6, spot, value, _font(11.5, 700), WHITE)

        height = bottom + 28 - y
        note_font = _font(12)
        for note in self.notes:
            for text in _wrap(note, note_font, w):
                pen.text(x, y + height + 9, text, note_font, TEXT_3)
                height += 19
        return height


@dataclass
class Notes:
    """「需要注意」框：每条前面一个彩色圆点。"""

    title: str
    items: list[tuple[str, str]]    # (颜色, 一句话)

    def strings(self) -> list[str]:
        return [self.title, *(text for _, text in self.items)]

    def layout(self, pen: _Pen, x: float, y: float, w: float, tone: Tone) -> float:
        pad, line = 14, 21
        body = _font(14)
        items = [(color, _wrap(text, body, w - pad * 2 - 16)) for color, text in self.items]
        height = pad + 18 + 8 + sum(len(lines) * line for _, lines in items) + 6 * (len(items) - 1) + pad
        pen.box((x, y, x + w, y + height), 12, fill=PANEL)
        pen.text(x + pad, y + pad + 9, self.title, _font(13, 700), TEXT_2)
        top = y + pad + 26
        for color, lines in items:
            dot = top + line / 2
            pen.ellipse((x + pad, dot - 4, x + pad + 8, dot + 4), fill=_color(color))
            for text in lines:
                pen.text(x + pad + 16, top + line / 2, text, body, WHITE)
                top += line
            top += 6
        return height


@dataclass
class Info:
    """说明框：一个小标题（可带图标）+ 几行字，比如「统计口径」。"""

    title: str
    lines: list[str]
    icon: str = ""

    def strings(self) -> list[str]:
        return [self.title, *self.lines]

    def layout(self, pen: _Pen, x: float, y: float, w: float, tone: Tone) -> float:
        pad, line = 14, 22
        body = _font(14)
        lines = [piece for text in self.lines for piece in _wrap(text, body, w - pad * 2)]
        height = pad + 20 + 6 + len(lines) * line + pad
        pen.box((x, y, x + w, y + height), 12, fill=PANEL)
        left = x + pad
        if self.icon:
            _icon(pen, self.icon, left, y + pad + 2, 16)
            left += 24
        pen.text(left, y + pad + 10, self.title, _font(14, 700), TEXT_2)
        top = y + pad + 26
        for text in lines:
            pen.text(x + pad, top + line / 2, text, body, WHITE)
            top += line
        return height


@dataclass
class Since:
    """钟表图标 + 「上一次有调用」+ 时间。"""

    label: str
    value: str

    def strings(self) -> list[str]:
        return [self.label, self.value]

    def layout(self, pen: _Pen, x: float, y: float, w: float, tone: Tone) -> float:
        _icon(pen, "clock", x, y + 12, 18)
        pen.text(x + 28, y + 9, self.label, _font(13), TEXT_2)
        pen.text(x + 28, y + 34, self.value, _font(17, 700), WHITE)
        return 46


@dataclass
class Paragraph:
    """一段字，可以在上面带一行灰色小标题（测试消息的「测试内容」）。"""

    text: str
    tone: str = ""
    size: float = 14
    label: str = ""

    def strings(self) -> list[str]:
        return [self.label, self.text] if self.label else [self.text]

    def layout(self, pen: _Pen, x: float, y: float, w: float, tone: Tone) -> float:
        top = y
        if self.label:
            pen.text(x, top + 9, self.label, _font(13), TEXT_2)
            top += 18 + 8
        font = _font(self.size)
        line = round(self.size * 1.5)
        lines = _wrap(self.text, font, w)
        for index, text in enumerate(lines):
            pen.text(x, top + index * line + line / 2, text, font, _color(self.tone))
        return top - y + len(lines) * line


@dataclass
class Status:
    """一行状态：圆形图标 + 彩色的一句结论 + 下面一行灰字（「账号已成功启用」）。"""

    icon: str
    title: str
    detail: str = ""
    tone: str = ""

    def strings(self) -> list[str]:
        return [self.title, self.detail]

    def layout(self, pen: _Pen, x: float, y: float, w: float, tone: Tone) -> float:
        _icon(pen, self.icon, x, y + 9, 26)
        pen.text(x + 38, y + 12, self.title, _font(17, 700), _color(self.tone))
        if self.detail:
            pen.text(x + 38, y + 36, self.detail, _font(13), TEXT_2)
        return 48


@dataclass
class Row:
    """详情面板里的一行：左边标签，右边值。"""

    label: str
    value: str
    tone: str = ""       # 值的颜色，见 _color
    pill: bool = False   # 值画成一个圆角小标签（「运行中」「已停用」）
    bold: bool = True


@dataclass
class Details:
    """一块深色面板：顶上可选一行灰色小标题、账号（头像、主行邮箱、副行 ID，见 Uid），
    下面一行行标签和值。"""

    rows: list[Row]
    uid: str = ""
    ruled: bool = False   # 行与行之间画分隔线（测试消息那种）
    title: str = ""       # 面板顶上的灰色小标题（「上一次有调用的那一小时」）
    email: str = ""       # 账号邮箱：主行邮箱、副行 ID（见 Uid）；没有 uid 时不画
    face: Face | None = None   # 账号头像，跟着 uid

    TITLE_H = 30

    def strings(self) -> list[str]:
        title = [self.title] if self.title else []
        head = Uid(self.uid, email=self.email, face=self.face).strings() if self.uid else []
        return [*title, *head, *(s for row in self.rows for s in (row.label, row.value))]

    def layout(self, pen: _Pen, x: float, y: float, w: float, tone: Tone) -> float:
        pad = 18
        row_h = 50 if self.ruled else 38
        head = (Uid.HEIGHT + 16) if self.uid else 0
        title_h = self.TITLE_H if self.title else 0
        height = title_h + (14 if self.uid else 0) + head + row_h * len(self.rows) + (0 if self.ruled else 8)
        pen.box((x, y, x + w, y + height), 12, fill=PANEL)
        top = y
        if self.title:
            pen.text(x + pad, y + 14 + 9, self.title, _font(13, 700), TEXT_2)
            top += title_h
        if self.uid:
            top += 14
            Uid(self.uid, email=self.email, face=self.face).layout(pen, x + pad, top, w - pad * 2, tone)
            top += Uid.HEIGHT + 8
            pen.line(x + pad, top, x + w - pad, top, LINE)
            top += 8
        label, value = _font(15), _font(16, 700)
        for index, row in enumerate(self.rows):
            if self.ruled and index:
                pen.line(x + pad, top, x + w - pad, top, LINE)
            middle = top + row_h / 2
            pen.text(x + pad, middle, row.label, label, TEXT_2)
            right = x + w - pad
            if row.pill:
                font = _font(13, 500)
                width = _width(row.value, font) + 20
                wash = TONES[row.tone].wash if row.tone in TONES else CHIP
                pen.box((right - width, middle - 12, right, middle + 12), 12, fill=wash)
                pen.text(right - width / 2, middle, row.value, font, _color(row.tone), anchor="mm")
            else:
                font = value if row.bold else _font(16)
                # 值太长会压到左边的标签上：放不下就截断，补一个省略号
                room = right - (x + pad + _width(row.label, label) + 16)
                pen.text(right, middle, _fit(row.value, font, room), font, _color(row.tone), anchor="rm")
            top += row_h
        return height


@dataclass
class Quote:
    """邮件原文节选：深色面板上一个灰色小标题，下面左边一道和卡片同色的竖条，
    竖条右边是一行粗体（原邮件主题）和节选的原文。"""

    label: str
    text: str = ""        # 一行一段
    heading: str = ""

    HEAD_LINE = 21
    LINE = 20
    PARAGRAPH_GAP = 6

    def strings(self) -> list[str]:
        return [s for s in (self.label, self.heading, self.text) if s]

    def layout(self, pen: _Pen, x: float, y: float, w: float, tone: Tone) -> float:
        pad, indent = 14, 14   # 竖条 3 px + 间距
        room = w - pad * 2 - indent
        head_font, body = _font(14, 700), _font(13.5)
        heads = _wrap(self.heading, head_font, room) if self.heading else []
        paragraphs = [_wrap(part, body, room) for part in self.text.split("\n") if part.strip()]
        quoted = (
            len(heads) * self.HEAD_LINE
            + (6 if heads and paragraphs else 0)
            + sum(len(lines) for lines in paragraphs) * self.LINE
            + self.PARAGRAPH_GAP * max(0, len(paragraphs) - 1)
        )
        height = pad + 18 + 8 + quoted + pad
        pen.box((x, y, x + w, y + height), 12, fill=PANEL)
        pen.text(x + pad, y + pad + 9, self.label, _font(13, 700), TEXT_2)
        top = y + pad + 26
        if quoted:
            pen.box((x + pad, top, x + pad + 3, top + quoted), 1.5, fill=tone.edge)
        for text in heads:
            pen.text(x + pad + indent, top + self.HEAD_LINE / 2, text, head_font, WHITE)
            top += self.HEAD_LINE
        if heads and paragraphs:
            top += 6
        for index, lines in enumerate(paragraphs):
            if index:
                top += self.PARAGRAPH_GAP
            for text in lines:
                pen.text(x + pad + indent, top + self.LINE / 2, text, body, QUOTE_TEXT)
                top += self.LINE
        return height


@dataclass
class Divider:
    """一道分隔线。本身不占高度，靠前后的块间距撑开。"""

    def strings(self) -> list[str]:
        return []

    def layout(self, pen: _Pen, x: float, y: float, w: float, tone: Tone) -> float:
        pen.line(x, y, x + w, y, LINE)
        return 0


@dataclass
class Card:
    """一张卡片 + 图片下面的那段文字。"""

    kind: str            # daily / started / stopped / quota / test / mail-*……，dry-run 存图时当文件名
    tone: str            # 边框、徽章的颜色：ok / warn / danger
    icon: str            # 标题前的图标，见 _icon
    title: str
    badge: str
    subtitle: str
    caption: str         # 图片下面的文字，Telegram HTML
    blocks: list = field(default_factory=list)
    subtitle_icon: str = ""
    footer: str = ""         # 底部左侧：一句说明，或者日报的「账号数量」
    footer_right: str = ""   # 底部右侧的小字
    footer_icon: str = ""    # 底部那句说明前的小图标
    footer_rule: bool = True  # 底部上面画不画分隔线
    signature: str = field(default_factory=lambda: config.TELEGRAM_CARD_SIGNATURE)
    _png: bytes | None = field(default=None, init=False, repr=False, compare=False)

    def png(self) -> bytes:
        """画成 PNG。同一张卡片发给几个群时只画一次。"""
        if self._png is None:
            self._png = render(self)
        return self._png

    def text(self) -> str:
        """caption 和卡片上的全部文字，纯文本。dry-run 打印、测试断言都用它。"""
        parts = [visible(self.caption), self.title, self.badge, self.subtitle]
        for block in self.blocks:
            parts.extend(block.strings())
        parts += [self.footer, self.footer_right, self.signature]
        return "\n".join(part for part in parts if part)


# ---------------------------------------------------------------- 画
def render(card: Card) -> bytes:
    height = _layout(card, _Pen())                     # 先量出高度
    image = Image.new("RGB", (_px(WIDTH), _px(height)), BLACK)
    _layout(card, _Pen(image))                         # 再真的画
    image = image.resize(
        (WIDTH * OUTPUT_SCALE, round(height * OUTPUT_SCALE)), Image.Resampling.LANCZOS
    )
    out = io.BytesIO()
    image.save(out, format="PNG")
    return out.getvalue()


def _layout(card: Card, pen: _Pen) -> float:
    """按顺序排版，返回整张图的高度（设计尺寸）。"""
    tone = TONES[card.tone]
    x, w = MARGIN + PAD, WIDTH - 2 * (MARGIN + PAD)
    y = MARGIN + 22

    # 标题行：图标 + 标题，徽章靠右
    _icon(pen, card.icon, x, y + 2, 26)
    pen.text(x + 36, y + 15, card.title, _font(22, 700), WHITE)
    if card.badge:
        font = _font(11.5, 700)
        width = _width(card.badge, font) + 22
        pen.box((x + w - width, y + 3, x + w, y + 27), 12, fill=tone.wash)
        pen.text(x + w - width / 2, y + 15, card.badge, font, tone.text, anchor="mm")
    y += 38
    left = x
    if card.subtitle_icon:
        _icon(pen, card.subtitle_icon, x, y + 1, 16)
        left += 24
    pen.text(left, y + 9, card.subtitle, _font(12.5), TEXT_2)
    y += 18 + GAP
    pen.line(x, y, x + w, y, LINE)
    y += GAP

    for index, block in enumerate(card.blocks):
        if index:
            y += GAP
        y += block.layout(pen, x, y, w, tone)

    if card.footer or card.footer_right or card.signature:
        if card.footer_rule:
            y += GAP + 4
            pen.line(x, y, x + w, y, LINE)
            y += 14
        else:
            y += GAP - 2
        font = _font(13)
        if card.footer or card.footer_right:
            left = x
            if card.footer_icon:
                _icon(pen, card.footer_icon, x, y + 2, 16)
                left += 24
            room = x + w - left - (_width(card.footer_right, font) + 16 if card.footer_right else 0)
            lines = _wrap(card.footer, font, room) if card.footer else [""]
            for index, text in enumerate(lines):
                pen.text(left, y + 10 + index * 20, text, font, TEXT_2)
            if card.footer_right:
                pen.text(x + w, y + 10, card.footer_right, font, TEXT_2, anchor="rm")
            y += len(lines) * 20 + 6
        if card.signature:
            pen.oblique(x, y + 10, card.signature, _font(12.5), TEXT_3)
            y += 20
    y += 18

    # 边框最后画：画到这里才知道卡片有多高
    pen.box((MARGIN, MARGIN, WIDTH - MARGIN, y), RADIUS, outline=tone.edge, width=1.5)
    return y + MARGIN


class _Pen:
    """画笔，坐标都是设计尺寸。不带画布时只量不画。"""

    def __init__(self, image: Image.Image | None = None):
        self.image = image
        self.draw = ImageDraw.Draw(image) if image is not None else None

    def text(self, x, y, text, font, fill, anchor="lm"):
        """默认 y 是这一行的垂直中线；anchor 的含义同 Pillow（ls = 左侧基线……）。"""
        if self.draw is not None and text:
            self.draw.text((_px(x), _px(y)), text, font=font, fill=fill, anchor=anchor)

    def oblique(self, x, y, text, font, fill, slant=0.2):
        """仿斜体：Noto Sans SC 没有斜体，把字画到一层蒙版上再往右错切。"""
        if self.image is None or not text:
            return
        height = int(font.size * 1.6)
        shift = int(height * slant) + 1
        mask = Image.new("L", (int(font.getlength(text)) + shift + 2, height), 0)
        ImageDraw.Draw(mask).text((0, height / 2), text, font=font, fill=255, anchor="lm")
        # AFFINE 的系数是「输出像素 → 从哪取」：越靠上取得越靠左，字就往右倒
        mask = mask.transform(
            mask.size, Image.Transform.AFFINE, (1, slant, -shift, 0, 1, 0),
            resample=Image.Resampling.BICUBIC,
        )
        left, top = _px(x), _px(y) - height // 2
        self.image.paste(fill, (left, top, left + mask.width, top + mask.height), mask)

    def box(self, box, radius, fill=None, outline=None, width=0.0):
        """圆角矩形。圆角半径不会超过短边的一半。"""
        if self.draw is None:
            return
        x0, y0, x1, y1 = (_px(v) for v in box)
        radius = min(_px(radius), (x1 - x0) // 2, (y1 - y0) // 2)
        self.draw.rounded_rectangle(
            (x0, y0, x1, y1), radius=radius, fill=fill, outline=outline,
            width=max(1, _px(width)) if outline else 0,
        )

    def ellipse(self, box, fill=None, outline=None, width=0.0):
        if self.draw is not None:
            self.draw.ellipse(
                tuple(_px(v) for v in box), fill=fill, outline=outline,
                width=max(1, _px(width)) if outline else 0,
            )

    def line(self, x0, y0, x1, y1, fill, width=1.0):
        self.lines([(x0, y0), (x1, y1)], fill, width)

    def lines(self, points, fill, width=1.0):
        if self.draw is not None:
            self.draw.line(
                [(_px(px), _px(py)) for px, py in points], fill=fill,
                width=max(1, _px(width)), joint="curve",
            )

    def polygon(self, points, fill):
        if self.draw is not None:
            self.draw.polygon([(_px(px), _px(py)) for px, py in points], fill=fill)

    def area(self, points, fill, top, bottom, alpha=0.2):
        """半透明的填充，从 top 往下淡到 bottom 全透明（折线图线下面那一层）。Pillow 的几何图形没有透明度：
        画到一张蒙版上，乘一道从上到下的渐变，再按蒙版把颜色贴上来。"""
        if self.image is None:
            return
        x0 = min(_px(px) for px, _ in points)
        x1 = max(_px(px) for px, _ in points)
        y0, y1 = _px(top), _px(bottom)
        size = (max(1, x1 - x0 + 1), max(1, y1 - y0 + 1))
        mask = Image.new("L", size, 0)
        ImageDraw.Draw(mask).polygon([(_px(px) - x0, _px(py) - y0) for px, py in points], fill=255)
        fade = Image.linear_gradient("L").rotate(180).resize(size).point(lambda v: round(v * alpha))
        self.image.paste(fill, (x0, y0, x0 + size[0], y0 + size[1]), ImageChops.multiply(mask, fade))

    def layer(self, x, y, s, draw, angle=0.0):
        """在一层透明小画布上画（坐标是画布像素，边长 _px(s)），转 angle 度后贴上来。

        用来画斜着的图标（试管、卫星天线）：Pillow 的几何图形不能直接斜着画。
        """
        if self.image is None:
            return
        size = _px(s)
        layer = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        draw(ImageDraw.Draw(layer), size)
        if angle:
            layer = layer.rotate(angle, resample=Image.Resampling.BICUBIC)
        self.image.paste(layer, (_px(x), _px(y)), layer)


def _px(value: float) -> int:
    return round(value * _K)


@lru_cache(maxsize=None)
def _font(size: float, weight: int = 400, mono: bool = False) -> ImageFont.FreeTypeFont:
    # 按路径打开，不要读成 bytes 再传：每个字号、字重都是一个对象，
    # 17 MB 的字体读进内存一份份复制就太多了
    font = ImageFont.truetype(
        str(MONO_FONT if mono else SANS_FONT), _px(size), layout_engine=ImageFont.Layout.BASIC
    )
    font.set_variation_by_axes([weight])
    return font


def _width(text: str, font: ImageFont.FreeTypeFont) -> float:
    return font.getlength(text) / _K


def _fit(text: str, font: ImageFont.FreeTypeFont, width: float) -> str:
    """一行放不下就从后面截掉、补一个省略号。"""
    if _width(text, font) <= width:
        return text
    while text and _width(text + "…", font) > width:
        text = text[:-1]
    return text.rstrip() + "…"


def _draw_face(pen: _Pen, who: Face, cx: float, cy: float, r: float, ring=None) -> None:
    """账号头像：圆圈 + 一个字；ring 给了就描一圈那个颜色（折线图线尾的头像，圈是线的颜色）。"""
    pen.ellipse((cx - r, cy - r, cx + r, cy + r), fill=who.bg, outline=ring, width=1.6 if ring else 0)
    pen.text(cx, cy, who.text, _font(r * 1.05, 700), who.fg, anchor="mm")


def _mix(color, other, t: float) -> tuple[int, int, int]:
    return tuple(round(a + (b - a) * t) for a, b in zip(color, other))


def _smooth(points: list[tuple[float, float]], steps: int = 14) -> list[tuple[float, float]]:
    """经过每个点的平滑曲线，切成一段段折线给 Pillow 画。单调三次样条（Fritsch–Carlson，和网页上的
    chart._smooth_path 一样）：两点之间不会冲过头，挨着 0 的那段不会画成负数。"""
    n = len(points)
    if n < 3:
        return list(points)
    xs, ys = [p[0] for p in points], [p[1] for p in points]
    dx = [xs[i + 1] - xs[i] for i in range(n - 1)]
    slope = [(ys[i + 1] - ys[i]) / dx[i] if dx[i] else 0.0 for i in range(n - 1)]
    tangent = [slope[0]] + [0.0] * (n - 2) + [slope[-1]]
    for i in range(1, n - 1):
        if slope[i - 1] * slope[i] <= 0:
            continue          # 拐点处切线放平，保证不冲过头
        w1, w2 = 2 * dx[i] + dx[i - 1], dx[i] + 2 * dx[i - 1]
        tangent[i] = (w1 + w2) / (w1 / slope[i - 1] + w2 / slope[i])
    out = [points[0]]
    for i in range(n - 1):
        h = dx[i]
        for step in range(1, steps + 1):
            t = step / steps
            h00, h10 = 2 * t**3 - 3 * t**2 + 1, t**3 - 2 * t**2 + t
            h01, h11 = -2 * t**3 + 3 * t**2, t**3 - t**2
            out.append((xs[i] + h * t, h00 * ys[i] + h10 * h * tangent[i] + h01 * ys[i + 1] + h11 * h * tangent[i + 1]))
    return out


def _spread(ys: list[float], top: float, bottom: float, gap: float) -> list[float]:
    """线尾头像的纵坐标：ys 从上到下排好，挤在一起的往下错开 gap，挤出底了再从下往上推（和网页上的
    chart.end_spots 一个排法）。"""
    spots: list[float] = []
    for y in ys:
        spots.append(max(y, top) if not spots else max(y, spots[-1] + gap))
    if spots and spots[-1] > bottom:
        spots[-1] = bottom
        for i in range(len(spots) - 2, -1, -1):
            spots[i] = min(spots[i], spots[i + 1] - gap)
    return spots


# 能连在一起不断开的一串：英文单词、数字、金额、模型 ID 之类
_TOKEN = re.compile(r"[A-Za-z0-9_.,:/$%@#\-+]+|\s+|.")


def _wrap(text: str, font: ImageFont.FreeTypeFont, width: float) -> list[str]:
    """按宽度折行。中文逐字可断；英文单词、数字、金额尽量不从中间劈开。"""
    limit = width * _K
    lines: list[str] = []
    for paragraph in text.split("\n"):
        first = len(lines)
        line = ""
        for token in _TOKEN.findall(paragraph):
            # 一个词本身就比整行还宽（很长的 ARN、URL）：只能逐字硬切
            pieces = list(token) if font.getlength(token) > limit else [token]
            for piece in pieces:
                if not line or font.getlength(line + piece) <= limit:
                    line += piece
                    continue
                broken, carry = line.rstrip(), ""
                # 行尾不留左括号（「权限（」换行、括号里的字到下一行），跟着内容挪下去
                if len(broken) > 1 and broken[-1] in _OPENERS and font.getlength(broken[-1] + piece) <= limit:
                    broken, carry = broken[:-1], broken[-1]
                lines.append(broken)
                line = carry + piece.lstrip()
        lines.append(line.rstrip())
        _no_orphan(lines, first)
    return lines


_OPENERS = "（([「『《【“‘"


def _no_orphan(lines: list[str], first: int) -> None:
    """一段的最后一行只剩一两个字（「能。」）很难看：从上一行挪两个汉字下来。

    只挪汉字和中文标点，不会把英文单词劈开；上一行变短、最后一行也还很短，都放得下。
    """
    if len(lines) - first < 2:
        return
    previous, last = lines[-2], lines[-1]
    moved = previous[-2:]
    if 0 < len(last) <= 2 and len(previous) > 4 and not any(ch.isascii() for ch in moved):
        lines[-2], lines[-1] = previous[:-2], moved + last


# ---------------------------------------------------------------- 图标
def _icon(pen: _Pen, kind: str, x: float, y: float, s: float) -> None:
    """标题、日期前的小图标。(x, y) 是左上角，s 是边长。"""
    if pen.draw is None or not kind:
        return
    if kind == "dot-green":
        _ball(pen, x, y, s, light=(170, 250, 200), dark=(22, 150, 80))
    elif kind == "dot-red":
        _ball(pen, x, y, s, light=(255, 180, 185), dark=(200, 30, 55))
    elif kind == "dot-gray":
        _ball(pen, x, y, s, light=(160, 158, 170), dark=(34, 32, 40))
    elif kind in ("check-circle", "pause-circle"):
        color = TONES["ok"].text if kind == "check-circle" else (214, 214, 218)
        pen.ellipse((x + s * 0.06, y + s * 0.06, x + s * 0.94, y + s * 0.94), outline=color, width=s * 0.085)
        if kind == "check-circle":
            pen.lines([(x + s * 0.3, y + s * 0.52), (x + s * 0.45, y + s * 0.66), (x + s * 0.71, y + s * 0.38)],
                      color, width=s * 0.085)
        else:
            for left in (0.37, 0.56):
                pen.box((x + s * left, y + s * 0.33, x + s * (left + 0.075), y + s * 0.67), s * 0.03, fill=color)
    elif kind == "test-tube":
        pen.layer(x, y, s, _draw_test_tube, angle=-38)
    elif kind == "satellite":
        pen.layer(x, y, s, _draw_dish_stand)
        pen.layer(x, y, s, _draw_dish, angle=32)
    elif kind in ("warning", "warning-red"):
        body = (245, 166, 35) if kind == "warning" else (239, 68, 68)
        mark = (60, 40, 0) if kind == "warning" else WHITE
        corners = [(x + s * 0.5, y + s * 0.12), (x + s * 0.93, y + s * 0.86), (x + s * 0.07, y + s * 0.86)]
        pen.polygon(corners, body)
        pen.lines([*corners, corners[0]], body, width=s * 0.12)    # 把三个角磨圆
        pen.box((x + s * 0.445, y + s * 0.36, x + s * 0.555, y + s * 0.62), s * 0.05, fill=mark)
        pen.ellipse((x + s * 0.44, y + s * 0.68, x + s * 0.56, y + s * 0.8), fill=mark)
    elif kind == "chart":
        pen.box((x + s * 0.06, y + s * 0.06, x + s * 0.94, y + s * 0.94), s * 0.2, fill=(228, 233, 242))
        bars = (((236, 72, 153), 0.36), ((59, 130, 246), 0.58), ((20, 184, 166), 0.46))
        for index, (color, height) in enumerate(bars):
            left = x + s * (0.22 + index * 0.21)
            pen.box((left, y + s * (0.8 - height), left + s * 0.15, y + s * 0.8), s * 0.04, fill=color)
    elif kind == "check":
        pen.box((x + s * 0.06, y + s * 0.06, x + s * 0.94, y + s * 0.94), s * 0.22, fill=(34, 197, 94))
        pen.lines([(x + s * 0.28, y + s * 0.52), (x + s * 0.44, y + s * 0.68), (x + s * 0.74, y + s * 0.34)],
                  WHITE, width=s * 0.11)
    elif kind == "calendar":
        pen.box((x + s * 0.08, y + s * 0.16, x + s * 0.92, y + s * 0.94), s * 0.16, fill=(236, 232, 248))
        pen.box((x + s * 0.08, y + s * 0.16, x + s * 0.92, y + s * 0.42), s * 0.16, fill=(139, 92, 246))
        pen.box((x + s * 0.08, y + s * 0.3, x + s * 0.92, y + s * 0.42), 0, fill=(139, 92, 246))
        for ring in (0.32, 0.68):
            pen.box((x + s * (ring - 0.045), y + s * 0.06, x + s * (ring + 0.045), y + s * 0.26), s * 0.04,
                    fill=(96, 70, 170))
        for row in range(2):
            for column in range(3):
                cx, cy = x + s * (0.3 + column * 0.2), y + s * (0.58 + row * 0.2)
                pen.ellipse((cx - s * 0.05, cy - s * 0.05, cx + s * 0.05, cy + s * 0.05), fill=(150, 138, 196))
    elif kind == "pin":
        pen.line(x + s * 0.52, y + s * 0.5, x + s * 0.3, y + s * 0.95, (175, 175, 175), width=s * 0.09)
        pen.ellipse((x + s * 0.28, y + s * 0.06, x + s * 0.82, y + s * 0.6), fill=(239, 68, 68))
        pen.ellipse((x + s * 0.4, y + s * 0.15, x + s * 0.56, y + s * 0.31), fill=(252, 170, 170))
    elif kind == "clock":
        pen.ellipse((x + s * 0.06, y + s * 0.06, x + s * 0.94, y + s * 0.94), outline=TEXT_2, width=s * 0.09)
        pen.lines([(x + s * 0.5, y + s * 0.27), (x + s * 0.5, y + s * 0.52), (x + s * 0.68, y + s * 0.62)],
                  TEXT_2, width=s * 0.09)
    else:
        raise ValueError(f"没有这个图标：{kind}")


def _draw_test_tube(d: ImageDraw.ImageDraw, n: int) -> None:
    """竖着的试管（画好后整层转过去）：玻璃管、绿色液体、管口、一道高光。"""
    glass, liquid = (214, 226, 240, 255), (96, 214, 132, 255)
    d.rounded_rectangle((0.36 * n, 0.14 * n, 0.64 * n, 0.92 * n), radius=0.14 * n, fill=glass)
    d.rounded_rectangle((0.36 * n, 0.5 * n, 0.64 * n, 0.92 * n), radius=0.14 * n, fill=liquid)
    d.rectangle((0.36 * n, 0.5 * n, 0.64 * n, 0.64 * n), fill=liquid)
    d.rounded_rectangle((0.3 * n, 0.08 * n, 0.7 * n, 0.19 * n), radius=0.05 * n, fill=(168, 186, 208, 255))
    d.rounded_rectangle((0.41 * n, 0.23 * n, 0.46 * n, 0.46 * n), radius=0.025 * n, fill=(255, 255, 255, 200))


def _draw_dish_stand(d: ImageDraw.ImageDraw, n: int) -> None:
    d.polygon([(0.44 * n, 0.6 * n), (0.56 * n, 0.6 * n), (0.7 * n, 0.95 * n), (0.3 * n, 0.95 * n)],
              fill=(128, 128, 138, 255))


def _draw_dish(d: ImageDraw.ImageDraw, n: int) -> None:
    """卫星天线的锅（画好后斜过去）：朝上的半个椭圆 + 馈源杆 + 红色小头。"""
    d.pieslice((0.1 * n, 0.14 * n, 0.9 * n, 0.74 * n), start=0, end=180, fill=(224, 228, 236, 255))
    d.line([(0.5 * n, 0.44 * n), (0.5 * n, 0.17 * n)], fill=(150, 150, 160, 255), width=max(1, round(0.07 * n)))
    d.ellipse((0.43 * n, 0.08 * n, 0.57 * n, 0.22 * n), fill=(239, 68, 68, 255))


def _ball(pen: _Pen, x: float, y: float, s: float, light, dark) -> None:
    """有高光的圆球：从外圈的深色一层层叠到左上角的浅色。"""
    steps = 18
    for index in range(steps):
        t = index / (steps - 1)
        radius = s / 2 * (1 - 0.62 * t)
        cx, cy = x + s / 2 - s * 0.12 * t, y + s / 2 - s * 0.15 * t
        color = tuple(round(d + (l - d) * t) for d, l in zip(dark, light))
        pen.ellipse((cx - radius, cy - radius, cx + radius, cy + radius), fill=color)
