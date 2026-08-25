"""页面路由：概览 + 成本和使用情况。"""

from __future__ import annotations

from datetime import date, datetime

from flask import Blueprint, flash, redirect, render_template, request, url_for

from . import (
    chart,
    cloudwatch_metrics,
    config,
    cost_estimate,
    cost_explorer,
    quotas,
    usage_explorer,
)
from .auth import login_required
from .dates import detect_preset, resolve_range
from .excel_source import Account, ExcelSourceError, load_accounts
from .report import build_report
from .windows import PERIODS, WINDOWS, detect_window, resolve_window

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


# --------------------------------------------------------------- 模型用量（CloudWatch）
@bp.route("/model-usage")
@login_required
def model_usage():
    window, notes = resolve_window(request.args)
    refresh = request.args.get("refresh") == "1"

    metric_key = (request.args.get("metric") or cloudwatch_metrics.DEFAULT_METRIC).strip()
    if metric_key not in cloudwatch_metrics.METRICS:
        metric_key = cloudwatch_metrics.DEFAULT_METRIC
    tag_filter = (request.args.get("tags") or cloudwatch_metrics.DEFAULT_TAG_FILTER).strip()
    if tag_filter not in cloudwatch_metrics.TAG_FILTERS:
        tag_filter = cloudwatch_metrics.DEFAULT_TAG_FILTER

    # 区域不再是筛选项：四个美国区各画一张小图（2×2），一次全查
    picked_regions = list(cloudwatch_metrics.DEFAULT_REGIONS)

    accounts: list[Account] = []
    fatal = None
    try:
        accounts = load_accounts(force=refresh)
    except ExcelSourceError as exc:
        fatal = str(exc)
    except Exception as exc:
        fatal = f"{type(exc).__name__}: {exc}"

    selected, chosen = _select_accounts(accounts, request.args.get("account", ""), notes)

    report = cloudwatch_metrics.UsageMetricsReport(
        window=window, metric_key=metric_key, tag_filter=tag_filter, regions=picked_regions
    )
    if not fatal:
        try:
            report = cloudwatch_metrics.build_metrics(
                chosen, picked_regions, window, metric_key, tag_filter, refresh=refresh
            )
        except Exception as exc:
            fatal = f"{type(exc).__name__}: {exc}"

    if not fatal and not report.tags_resolved:
        notes.append(
            "读不到推理配置上的标签（缺 bedrock:ListTagsForResource 权限？），"
            "所有流量都会被当成「无标签」，标签筛选此时不可信。"
        )

    panels = chart.render_small_multiples(report)

    # Windows 上 tzname() 会给出「Malay Peninsula Standard Time」这种长名字，
    # 放在标签里太占地方也不够明确，改用 UTC 偏移。
    offset = datetime.now().astimezone().strftime("%z")
    tz_name = f"UTC{offset[:3]}:{offset[3:]}" if len(offset) == 5 else "本机时区"

    return render_template(
        "model_usage.html",
        active_page="model_usage",
        report=report,
        panels=panels,
        fatal=fatal,
        notes=notes,
        window=window,
        accounts=accounts,
        selected_account=selected,
        regions=cloudwatch_metrics.REGIONS,
        metrics=cloudwatch_metrics.METRICS,
        metric_key=metric_key,
        tag_filters=cloudwatch_metrics.TAG_FILTERS,
        tag_filter=tag_filter,
        periods=PERIODS,
        windows=WINDOWS,
        active_window=detect_window(window),
        local_start=window.start.astimezone().strftime("%Y-%m-%dT%H:%M"),
        local_end=window.end.astimezone().strftime("%Y-%m-%dT%H:%M"),
        tz_name=tz_name,
        **page_meta(),
    )


# --------------------------------------------------------------- 预估成本
@bp.route("/cost-estimate")
@login_required
def estimate():
    """CloudWatch token 量 × AWS 牌价。用来补 Cost Explorer 那一两天的延迟。"""
    today = date.today()
    start, end, notes = resolve_range(request.args, today)
    refresh = request.args.get("refresh") == "1"

    accounts: list[Account] = []
    fatal = None
    try:
        accounts = load_accounts(force=refresh)
    except ExcelSourceError as exc:
        fatal = str(exc)
    except Exception as exc:
        fatal = f"{type(exc).__name__}: {exc}"

    selected, chosen = _select_accounts(accounts, request.args.get("account", ""), notes)

    report = cost_estimate.EstimateReport(start=start, end=end)
    if not fatal:
        try:
            report = cost_estimate.build_estimate(chosen, start, end, refresh=refresh)
        except Exception as exc:
            fatal = f"{type(exc).__name__}: {exc}"

    if report.price_stale:
        notes.append(
            "拉不到最新的 AWS 价目表，用的是本地缓存副本"
            f"（{report.price_error}）。单价可能已经过时。"
        )
    if report.unpriced:
        notes.append(
            "这些模型在 AWS 价目表里没有对应条目，**没有计入**估算总额："
            + "、".join(report.unpriced)
            + "。多半是刚发布的新模型，等 AWS 更新价目表即可。"
        )

    return render_template(
        "cost_estimate.html",
        active_page="cost_estimate",
        report=report,
        chart=chart.render_stacked_bars(report, config.CURRENCY_SYMBOL),
        fatal=fatal,
        notes=notes,
        start=start,
        end=end,
        today=today,
        accounts=accounts,
        selected_account=selected,
        kinds=cost_estimate.KIND_ORDER,
        kind_labels=cost_estimate.KIND_LABELS,
        active_preset=detect_preset(start, end, today),
        **page_meta(),
    )


@bp.route("/cache/clear")
@login_required
def clear_cache():
    cost_explorer.clear_cache()
    usage_explorer.clear_cache()
    cost_estimate.clear_cache()
    cloudwatch_metrics.clear_cache()
    quotas.clear_cache()
    flash("已清空缓存，下一次查询会重新调用 Cost Explorer 和 CloudWatch。", "ok")
    return redirect(request.referrer or url_for("main.index"))


# --------------------------------------------------------------- 模型配额
@bp.route("/model-quota")
@login_required
def model_quota():
    refresh = request.args.get("refresh") == "1"
    notes: list[str] = []

    accounts: list[Account] = []
    fatal = None
    try:
        accounts = load_accounts(force=refresh)
    except ExcelSourceError as exc:
        fatal = str(exc)
    except Exception as exc:
        fatal = f"{type(exc).__name__}: {exc}"

    # 配额和限流都是按账号算的，没有「全部账号」这个选项：把多个账号合起来会
    # 把一个快满的账号藏在平均值里，真限流时页面上还显示一切正常
    chosen = None
    selected = (request.args.get("account") or "").strip()
    if accounts:
        by_key = {a.key: a for a in accounts}
        if selected and selected not in by_key:
            notes.append("所选账号已不在台账中，已切回第一个账号。")
            selected = ""
        chosen = by_key.get(selected) or accounts[0]
        selected = chosen.key

    report = quotas.QuotaReport()
    if not fatal:
        try:
            report = quotas.build_quota_report(chosen, refresh=refresh)
        except Exception as exc:
            fatal = f"{type(exc).__name__}: {exc}"

    if report.error:
        notes.append(f"读不到 Service Quotas：{report.error}")

    return render_template(
        "model_quota.html",
        active_page="model_quota",
        report=report,
        fatal=fatal,
        notes=notes,
        accounts=accounts,
        selected_account=selected,
        quota_region=quotas.QUOTA_REGION,
        quota_service=quotas.SERVICE_CODE,
        quota_cache_hours=round(quotas.QUOTA_CACHE_TTL / 3600),
        **page_meta(),
    )
