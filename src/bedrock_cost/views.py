"""页面路由：概览 + 成本和使用情况。"""

from __future__ import annotations

from datetime import date, datetime

from flask import Blueprint, flash, redirect, render_template, request, url_for

from . import chart, config, cost_explorer, usage_explorer
from .auth import login_required
from .dates import detect_preset, resolve_range
from .excel_source import Account, ExcelSourceError, load_accounts
from .report import build_report

bp = Blueprint("main", __name__)

# 超过这个天数还按日画，柱子会挤成一团，提示用户切按月
DENSE_DAY_LIMIT = 120


def page_meta() -> dict:
    """两个页面页脚共用的说明信息。"""
    return {
        "cost_metric": config.COST_METRIC,
        "service_scope": (
            "、".join(config.SERVICE_FILTER) if config.SERVICE_FILTER else "账号全部服务"
        ),
        "cache_ttl_minutes": round(config.CACHE_TTL / 60, 1),
        "excel_name": config.EXCEL_PATH.name,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


@bp.route("/")
@login_required
def index():
    today = date.today()
    start, end, notes = resolve_range(request.args, today)
    refresh = request.args.get("refresh") == "1"

    report = None
    fatal = None
    try:
        report = build_report(start, end, refresh=refresh)
    except ExcelSourceError as exc:
        fatal = str(exc)
    except Exception as exc:  # 兜底，避免整页 500
        fatal = f"{type(exc).__name__}: {exc}"

    return render_template(
        "index.html",
        active_page="overview",
        report=report,
        fatal=fatal,
        notes=notes,
        start=start,
        end=end,
        today=today,
        active_preset=detect_preset(start, end, today),
        tag_key=config.TAG_KEY,
        **page_meta(),
    )


def _select_accounts(
    accounts: list[Account], requested: str, notes: list[str]
) -> tuple[str, list[Account]]:
    """校验账号参数，返回 (最终选中值, 参与查询的账号)。"""
    selected = (requested or "all").strip()
    known = {account.key for account in accounts}
    if selected != "all" and selected not in known:
        if selected:
            notes.append("所选账号已不在台账中，已切回「全部账号」。")
        selected = "all"
    if selected == "all":
        return selected, accounts
    return selected, [a for a in accounts if a.key == selected]


@bp.route("/cost-usage")
@login_required
def cost_usage():
    today = date.today()
    start, end, notes = resolve_range(request.args, today)
    refresh = request.args.get("refresh") == "1"

    dimension = (request.args.get("dim") or "service").strip()
    if dimension not in usage_explorer.DIMENSIONS:
        dimension = "service"
    granularity = (request.args.get("granularity") or "daily").strip()
    if granularity not in usage_explorer.GRANULARITIES:
        granularity = "daily"

    accounts: list[Account] = []
    fatal = None
    try:
        accounts = load_accounts(force=refresh)
    except ExcelSourceError as exc:
        fatal = str(exc)
    except Exception as exc:
        fatal = f"{type(exc).__name__}: {exc}"

    selected, chosen = _select_accounts(accounts, request.args.get("account", ""), notes)

    span_days = (end - start).days + 1
    if granularity == "daily" and span_days > DENSE_DAY_LIMIT:
        notes.append(
            f"当前区间有 {span_days} 天，按日的柱子会很密，可以把粒度切成「按月」。"
        )

    report = usage_explorer.UsageReport(
        start=start, end=end, dimension=dimension, granularity=granularity
    )
    if not fatal:
        try:
            report = usage_explorer.build_usage(
                chosen, start, end, dimension, granularity, refresh=refresh
            )
        except Exception as exc:
            fatal = f"{type(exc).__name__}: {exc}"

    rendered = chart.render_stacked_bars(report, config.CURRENCY_SYMBOL)
    bucket_count = len(report.dates) or 1

    return render_template(
        "cost_usage.html",
        active_page="cost_usage",
        report=report,
        chart=rendered,
        fatal=fatal,
        notes=notes,
        start=start,
        end=end,
        today=today,
        accounts=accounts,
        selected_account=selected,
        dimension=dimension,
        granularity=granularity,
        dimensions=usage_explorer.DIMENSIONS,
        granularities=usage_explorer.GRANULARITIES,
        bucket_average=report.total_marked / bucket_count,
        active_preset=detect_preset(start, end, today),
        **page_meta(),
    )


@bp.route("/cache/clear")
@login_required
def clear_cache():
    cost_explorer.clear_cache()
    usage_explorer.clear_cache()
    flash("已清空成本缓存，下一次查询会重新调用 Cost Explorer。", "ok")
    return redirect(request.referrer or url_for("main.index"))
