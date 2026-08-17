"""Jinja 过滤器与全局。"""

from __future__ import annotations

from flask import Flask

from . import chart, config


def money(value: float | None) -> str:
    if value is None:
        return "—"
    return f"{config.CURRENCY_SYMBOL}{value:,.2f}"


def pct(value: float | None) -> str:
    if value is None:
        return "—"
    return f"{value:,.2f}%"


def ratio(value: float) -> str:
    """比率去掉多余的零：1.0500 -> 1.05，1.0000 -> 1。"""
    text = f"{value:.4f}".rstrip("0").rstrip(".")
    return text or "0"


def register_filters(app: Flask) -> None:
    app.jinja_env.filters.update(money=money, pct=pct, ratio=ratio)
    # 图表色板的唯一来源是 chart.py，模板里的图例和表格色块取同一套值，
    # 不在 CSS 里重复维护一遍
    app.jinja_env.globals["series_color"] = chart.color_for
