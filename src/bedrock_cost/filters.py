"""Jinja 过滤器与全局。"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

from flask import Flask

from . import chart, config
from .auth import csrf_token

# 等待动效的素材。webm 优先——带 alpha 通道的版本只能是 webm（mp4 的 alpha
# 只有 Safari 认），有就用它；没有就退回 mp4。每次请求 stat 一下，换文件刷新
# 页面即生效，不用重启。
LOADING_MEDIA = ("loading.webm", "loading.mp4")


def money(value: float | None) -> str:
    """负数写成 -$12.30，不是 $-12.30（和告警卡片一个写法）。"""
    if value is None:
        return "—"
    sign = "-" if value < 0 else ""
    return f"{sign}{config.CURRENCY_SYMBOL}{abs(value):,.2f}"


def money0(value: float | None) -> str:
    """大号数字用的整数金额：$72,102。分位留给表格和卡片里的小字。"""
    if value is None:
        return "—"
    sign = "-" if value < 0 else ""
    return f"{sign}{config.CURRENCY_SYMBOL}{abs(value):,.0f}"


def pct(value: float | None) -> str:
    if value is None:
        return "—"
    return f"{value:,.2f}%"


def ratio(value: float) -> str:
    """比率去掉多余的零：1.0500 -> 1.05，1.0000 -> 1。"""
    text = f"{value:.4f}".rstrip("0").rstrip(".")
    return text or "0"


def compact(value: float | None) -> str:
    """非金额的紧凑数字：46.2K / 308.4M。用于调用次数和 token 量。"""
    if value is None:
        return "—"
    return chart.compact_number(value)


_MODEL = re.compile(r"(?i)claude[-\s]+(opus|sonnet|haiku|fable)[-\s]+(\d+)(?:[-.](\d)(?!\d))?")


def short_model(name: str) -> str:
    """窄地方（热力格的行名）用的模型短名：claude-sonnet-4-5-20250929-v1:0 → Sonnet 4.5。
    认不出来的原样返回。"""
    match = _MODEL.search(name or "")
    if not match:
        return name
    family, major, minor = match.groups()
    return f"{family.title()} {major}.{minor}" if minor else f"{family.title()} {major}"


def loading_media(static_folder: str | None) -> str:
    """挑一个存在的等待动效素材，都没有就返回空串（模板里会跳过 video）。"""
    if not static_folder:
        return ""
    folder = Path(static_folder)
    for name in LOADING_MEDIA:
        if (folder / name).is_file():
            return name
    return ""


def register_filters(app: Flask) -> None:
    app.jinja_env.filters.update(money=money, money0=money0, pct=pct, ratio=ratio, compact=compact,
                                 short_model=short_model)
    # 图表色板的唯一来源是 chart.py，模板里的图例和表格色块取同一套值，
    # 不在 CSS 里重复维护一遍
    app.jinja_env.globals["series_color"] = chart.color_for
    # 热力格的配色同理，色阶只在 chart.py 里定义一份
    app.jinja_env.globals["heat_style"] = chart.heat_style
    app.jinja_env.globals["heat_ramp"] = chart.HEAT_RAMP
    # 表单里的隐藏域直接调它，不用每个视图都往模板塞一遍
    app.jinja_env.globals["csrf_token"] = csrf_token

    # 等待动效在 shell.html 里，每个页面都要用，所以走 context_processor
    # 而不是让每个视图各传一遍。弹窗上的时间同理
    @app.context_processor
    def _shell_context():
        return {
            "loading_media": loading_media(app.static_folder),
            "toast_time": datetime.now().strftime("%Y-%m-%d %H:%M"),
        }
