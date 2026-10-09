"""页面路由：概览，以及老网址的跳转。

一个账号的成本、用量、配额、预估在账号页里（account_pages.py），运营看板在 ops.py。
"""

from __future__ import annotations

from datetime import date, datetime

from flask import Blueprint, flash, redirect, render_template, request, session, url_for

from . import (
    activity,
    chart,
    cloudwatch_metrics,
    config,
    cost_estimate,
    cost_explorer,
    dashboard,
    ops_report,
    quotas,
    usage_explorer,
)
from .auth import login_required
from .dates import earliest_queryable
from .excel_source import ExcelSourceError, load_accounts, load_lifecycle
from .report import build_report

bp = Blueprint("main", __name__)


def page_meta() -> dict:
    """各页面页脚共用的说明信息。"""
    return {
        "cost_metric": config.COST_METRIC,
        "service_scope": (
            "、".join(config.SERVICE_FILTER) if config.SERVICE_FILTER else "账号全部服务"
        ),
        "cache_ttl_minutes": round(config.CACHE_TTL / 60, 1),
        # 页脚上的「缓存 15 分钟」：整分钟就写分钟，否则写秒，不写成 15.0
        "cache_label": (f"{config.CACHE_TTL // 60} 分钟" if config.CACHE_TTL % 60 == 0
                        else f"{config.CACHE_TTL} 秒"),
        "excel_name": config.EXCEL_PATH.name,
        "generated_at": datetime.now().strftime("%m-%d %H:%M"),
    }


def tz_name() -> str:
    """本机时区写成 UTC+08:00。Windows 上 tzname() 会给出「Malay Peninsula Standard
    Time」这种长名字，放在标签里太占地方也不够明确。"""
    offset = datetime.now().astimezone().strftime("%z")
    return f"UTC{offset[:3]}:{offset[3:]}" if len(offset) == 5 else "本机时区"


# --------------------------------------------------------------- 概览
@bp.route("/")
@login_required
def index():
    # 没有日期筛选：消费和余额都是「从各账号的启用日期累计到今天」。额度是一次性
    # 发的，拿一个可选区间的消费去比它没有意义，要看的是额度用掉了多少。
    today = date.today()
    refresh = request.args.get("refresh") == "1"
    notes: list[str] = []
    toasts: list[dashboard.Toast] = []

    report = None
    fatal = None
    try:
        report = build_report(today, refresh=refresh)
    except ExcelSourceError as exc:
        fatal = str(exc)
    except Exception as exc:  # 兜底，避免整页 500
        fatal = f"{type(exc).__name__}: {exc}"

    context: dict = {}
    if report:
        accounts = load_accounts()
        tags = load_lifecycle()
        by_row = {(a.account, a.row): a for a in accounts}
        states = activity.activities(accounts, refresh=refresh)
        cards = []
        for order, row in enumerate(report.rows):
            account = by_row.get((row.account, row.row_number))
            if account is not None:
                cards.append(dashboard.CardRow(row, account, states[account.key], order, today))
        cards.sort(key=lambda card: (dashboard.risk_rank(card), -(card.usage_pct or -1)))
        report.rows = cards

        slots = dashboard.account_slots(cards)
        trend = dashboard.cost_trend(accounts, today, slots, refresh=refresh)
        pct = report.total_usage_pct
        context = dict(
            lifecycle_counts=dashboard.lifecycle_counts(cards, tags),
            state_counts=dashboard.state_counts(cards),
            lifecycle_colors={tag.name: tag.hex for tag in tags},
            gauge=chart.render_gauge(None if pct is None else pct / 100, report.total_level),
            trend=trend,
            share=dashboard.spend_share(cards, slots),
            near_limit=dashboard.near_limit(cards),
            activity_days=activity.LOOKBACK_DAYS,
        )
        failed = [card for card in cards if card.error]
        toasts += _ce_toasts(failed)
        # 每天成本那张图另外查一遍 CE；累计已经失败的账号不重复报
        known = {card.account for card in failed}
        extra = [e for e in trend.errors if dashboard._parts(e)[0] not in known]
        toasts += dashboard.account_toasts(extra, accounts, "查不到每天的成本")
        cw_errors = [e for state in states.values() if state.kind == "unknown" for e in state.errors]
        toasts += dashboard.account_toasts(cw_errors, accounts, "读不到 CloudWatch")
        if report.incomplete_rows:
            notes.append(
                f"{len(report.incomplete_rows)} 个账号的累计区间不完整（未填启用日期，"
                "或启用日期早于 Cost Explorer 的保留期），这些卡片的余额偏高，卡片上已标出。"
            )

    return render_template(
        "index.html",
        active_page="overview",
        report=report,
        fatal=fatal,
        notes=notes,
        toasts=toasts,
        today=today,
        earliest=earliest_queryable(today),
        tag_key=config.TAG_KEY,
        **context,
        **page_meta(),
    )


def _ce_toasts(failed) -> list[dashboard.Toast]:
    """概览上 Cost Explorer 查询失败的账号：原因相同的合成一条。"""
    groups: dict[str, list] = {}
    for card in failed:
        groups.setdefault(card.issue, []).append(card)
    toasts = []
    for reason, cards in groups.items():
        stale = sum(1 for card in cards if card.stale_as_of)
        text = reason + ("。卡片上已标出，有上一次数据的照常显示" if stale else "。卡片上已标出")
        if len(cards) == 1:
            title, sub = "查不到 Cost Explorer", f"{cards[0].label} · {cards[0].account}"
        else:
            title, sub = f"{len(cards)} 个账号查不到 Cost Explorer", ""
        detail = "\n".join(f"{card.label} · {card.account}：{card.error_detail}" for card in cards)
        toasts.append(dashboard.Toast("error", title, sub, text, detail))
    return toasts


# --------------------------------------------------------------- 老网址
# 原来的四个查询页现在是账号页的四个页签。收藏夹里的老网址照样能用：跳到「最近看的
# 那个账号」的对应页签，查询参数原样带过去（老网址上的 account=号码#行号 也认）。
def _legacy(tab_endpoint: str):
    from .account_pages import SESSION_KEY, remembered

    try:
        accounts = load_accounts(include_disabled=True)
    except ExcelSourceError as exc:
        flash(str(exc), "error")
        return redirect(url_for("main.index"))
    params = request.args.to_dict()
    key = params.pop("account", "")
    # 其余参数原样带过去，但不能撞上 url_for 自己的参数：账号页路由的 number，
    # 还有 _anchor / _external 这类下划线开头的（不然手搓一个参数就能让这一跳 500）
    params = {name: value for name, value in params.items() if name != "number" and not name.startswith("_")}
    number = key.rpartition("#")[0] or key
    account = next((a for a in accounts if a.account == number), None) or remembered(accounts)
    if account is None:
        return redirect(url_for("main.index"))
    session[SESSION_KEY] = account.account
    return redirect(url_for(tab_endpoint, number=account.account, **params))


@bp.route("/cost-usage")
@login_required
def cost_usage():
    return _legacy("account.cost")


@bp.route("/model-usage")
@login_required
def model_usage():
    return _legacy("account.usage")


@bp.route("/model-quota")
@login_required
def model_quota():
    return _legacy("account.quota")


@bp.route("/cost-estimate")
@login_required
def estimate():
    return _legacy("account.estimate")


@bp.route("/cache/clear")
@login_required
def clear_cache():
    cost_explorer.clear_cache()
    usage_explorer.clear_cache()
    cost_estimate.clear_cache()
    cloudwatch_metrics.clear_cache()
    quotas.clear_cache()
    activity.clear_cache()
    dashboard.clear_cache()
    ops_report.clear_cache()
    flash("已清空缓存，下一次查询会重新调用 Cost Explorer 和 CloudWatch。", "ok")
    return redirect(request.referrer or url_for("main.index"))
