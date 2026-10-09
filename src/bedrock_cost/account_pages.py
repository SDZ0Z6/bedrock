"""账号页：一个账号一个页面，五个页签（摘要 / 成本 / 用量 / 配额 / 预估）。

看的是哪个账号写在网址里（/account/<号码>/usage），能直接收藏、发给别人；最近看的那个
记在会话里，侧边栏的「账号」和老网址（/model-usage 之类）都跳到它。

原来的四个查询页（成本和使用情况、模型用量、模型配额、预估成本）就是这里的四个页签，
页面上的筛选只剩时间、维度这些，账号由网址决定。
"""

from __future__ import annotations

from datetime import date, datetime, time, timezone

from flask import Blueprint, flash, redirect, render_template, request, session, url_for

from . import activity, chart, cloudwatch_metrics, config, cost_estimate, cost_explorer, dashboard, quotas, usage_explorer
from .auth import login_required
from .dates import cumulative_range, detect_preset, earliest_queryable, resolve_range
from .excel_source import Account, ExcelSourceError, lifecycle_colors, load_accounts
from .report import build_row
from .views import page_meta, tz_name
from .windows import PERIODS, WINDOWS, MetricWindow, detect_window, fit_period, floor_to_period, resolve_window

bp = Blueprint("account", __name__, url_prefix="/account")

TABS = {
    "summary": ("摘要", "account.summary"),
    "cost": ("成本", "account.cost"),
    "usage": ("用量", "account.usage"),
    "quota": ("配额", "account.quota"),
    "estimate": ("预估", "account.estimate"),
}
SESSION_KEY = "account"
# 成本、预估页签的快捷时间
DATE_PRESETS = (("mtd", "本月"), ("last_month", "上月"), ("last7", "近 7 天"), ("last30", "近 30 天"), ("ytd", "今年"))


# ---------------------------------------------------------------- 找账号
def _all_accounts() -> list[Account]:
    return load_accounts(include_disabled=True)


def _find(accounts: list[Account], number: str) -> Account | None:
    """按号码找；同一个号码理论上只有一行（台账校验保证），启用的优先。"""
    matches = [a for a in accounts if a.account == number]
    return next((a for a in matches if a.enabled), matches[0] if matches else None)


def remembered(accounts: list[Account]) -> Account | None:
    """最近看的那个账号；没看过、或者已经不在台账里，就是台账里第一个启用的。"""
    number = session.get(SESSION_KEY)
    found = _find(accounts, number) if number else None
    return found or next((a for a in accounts if a.enabled), accounts[0] if accounts else None)


def _open(number: str, tab: str):
    """每个页签的开头：读台账、找账号、记住它。找不到就回概览说一声。"""
    try:
        accounts = _all_accounts()
    except ExcelSourceError as exc:
        flash(str(exc), "error")
        return None, None, redirect(url_for("main.index"))
    account = _find(accounts, number)
    if account is None:
        flash(f"台账里没有账号 {number}。", "warn")
        return None, None, redirect(url_for("main.index"))
    session[SESSION_KEY] = account.account
    return account, accounts, None


def _frame(account: Account, accounts: list[Account], tab: str, refresh: bool = False, **extra) -> dict:
    """五个页签共用的页头数据。"""
    today = date.today()
    state = activity.account_activity(account, refresh=refresh)
    return dict(
        account=account,
        accounts=accounts,
        tab=tab,
        tab_label=TABS[tab][0],
        activity=state,
        query_failed=False,
        days_active=dashboard.days_between(account.start_date, today),
        lifecycle_colors=lifecycle_colors(),
        active_page="account",
        tz_name=tz_name(),
        **page_meta(),
        **extra,
    )


# ---------------------------------------------------------------- 入口
@bp.route("/")
@login_required
def home():
    try:
        account = remembered(_all_accounts())
    except ExcelSourceError as exc:
        flash(str(exc), "error")
        return redirect(url_for("main.index"))
    if account is None:
        flash("台账里还没有账号，先去账号管理加一个。", "info")
        return redirect(url_for("accounts.index"))
    return redirect(url_for("account.summary", number=account.account))


@bp.route("/switch")
@login_required
def switch():
    """页头「切换账号」不开 JS 时的落脚点：下拉框交上来的是 account.key。"""
    key = (request.args.get("account") or "").strip()
    tab = request.args.get("tab") if request.args.get("tab") in TABS else "summary"
    number = key.rpartition("#")[0] or key
    return redirect(url_for(TABS[tab][1], number=number))


# ---------------------------------------------------------------- 摘要
@bp.route("/<number>/")
@login_required
def summary(number: str):
    account, accounts, bounce = _open(number, "summary")
    if bounce:
        return bounce
    refresh = request.args.get("refresh") == "1"
    today = date.today()
    toasts: list[dashboard.Toast] = []

    period = cumulative_range(account.start_date, today)
    splits = cost_explorer.fetch_all([account], {account.key: period[:2]}, refresh=refresh)
    split = splits.get(account.key) or cost_explorer.CostSplit(error="未取到数据")
    row = build_row(account, split, period)
    row.end_label = today.strftime("%m-%d")
    _, _, row.issue, detail = dashboard._parts(row.problem or row.error) if row.error else ("", "", "", "")
    if row.error:
        toasts.append(dashboard.Toast(
            "error", "查不到 Cost Explorer", dashboard.who(account),
            row.issue + ("，下面显示的是上一次查到的数" if row.stale_as_of else ""), detail))

    trend = dashboard.single_cost_trend(account, today, refresh=refresh)
    if trend.errors and not row.error:
        toasts += dashboard.region_toasts(trend.errors, account, "查不到每天的成本")

    window, _ = resolve_window({"win": "7d", "period": "1h"})
    calls_report = cloudwatch_metrics.build_metrics([account], list(cloudwatch_metrics.DEFAULT_REGIONS),
                                                     window, "invocations", refresh=refresh)
    view = chart.render_views(calls_report, width=820)[0]
    calls = dict(start=calls_report.labels[0] if calls_report.labels else "",
                 end=calls_report.labels[-1] if calls_report.labels else "",
                 total=view.total, average=view.average, peak_value=view.peak_value,
                 peak_label=view.peak_label, series=view.series, chart=view.chart)
    toasts += dashboard.region_toasts(calls_report.errors, account, "读不到 CloudWatch")

    context = _frame(account, accounts, "summary", refresh=refresh, row=row,
                     gauge=chart.render_gauge(None if row.usage_pct is None else row.usage_pct / 100, row.level),
                     trend=trend, calls=calls, quota_top=_quota_top(account), toasts=toasts, notes=[])
    context["query_failed"] = bool(row.error)
    return render_template("account_summary.html", **context)


def _quota_top(account: Account) -> list:
    """摘要页签的「配额」卡：只用已经缓存的配额（第一次查要 45 秒左右，不能拖慢摘要）。"""
    report = quotas.peek_quota_report([account])
    if not report:
        return []
    best: dict[str, dict] = {}
    for row in report.all_rows:
        if row.tpm is None:
            continue
        item = best.setdefault(row.display, {"name": row.display, "tpm": 0, "tpd": 0, "lagging": False})
        item["tpm"] = max(item["tpm"], row.tpm or 0)
        item["tpd"] = max(item["tpd"], row.tpd or 0)
        item["lagging"] = item["lagging"] or bool(row.below_best)
    return sorted(best.values(), key=lambda item: -item["tpm"])[:5]


# ---------------------------------------------------------------- 起止日期不早于启用日期
def _clamp_dates(account: Account, start: date, end: date, today: date, notes: list[str]) -> tuple[date, date]:
    first = max(account.start_date, earliest_queryable(today)) if account.start_date else None
    if first and first > today:
        # 启用日期填在了今天之后：没法从它起算（区间会倒过来），先按所选区间看，说一声
        notes.append(f"启用日期 {first.isoformat()} 还没到，先按所选区间显示。")
        return start, end
    if first and start < first:
        if end < first:
            # 整段都在启用之前：改成从启用日期到今天
            notes.append(f"所选区间在启用日期 {first.isoformat()} 之前，已改成从启用日期到今天。")
            return first, today
        notes.append(f"开始日期早于启用日期，已从 {first.isoformat()} 起算。")
        return first, end
    return start, end


def _date_presets(endpoint: str, account: Account, today: date, active: str, **params) -> list[tuple[str, str, bool]]:
    return [
        (url_for(endpoint, number=account.account, preset=key, **params), name, active == key)
        for key, name in DATE_PRESETS
    ]


def _date_label(start: date, end: date, active: str) -> str:
    named = dict(DATE_PRESETS)
    return named.get(active) or f"{start.isoformat()} ~ {end.isoformat()}"


def _date_min(account: Account, today: date) -> str:
    return (max(account.start_date, earliest_queryable(today)) if account.start_date else earliest_queryable(today)).isoformat()


# ---------------------------------------------------------------- 成本
@bp.route("/<number>/cost")
@login_required
def cost(number: str):
    account, accounts, bounce = _open(number, "cost")
    if bounce:
        return bounce
    today = date.today()
    start, end, notes = resolve_range(request.args, today)
    start, end = _clamp_dates(account, start, end, today, notes)
    refresh = request.args.get("refresh") == "1"

    # 只看一个账号，「按账号」这个维度没有意义了
    dimensions = {k: v for k, v in usage_explorer.DIMENSIONS.items() if k != "account"}
    dimension = (request.args.get("dim") or "service").strip()
    if dimension not in dimensions:
        dimension = "service"
    granularity = (request.args.get("granularity") or "daily").strip()
    if granularity not in usage_explorer.GRANULARITIES:
        granularity = "daily"
    span = (end - start).days + 1
    if granularity == "daily" and span > 120:
        notes.append(f"当前区间有 {span} 天，按日的点会很密，可以把粒度切成「按月」。")

    fatal = None
    report = usage_explorer.UsageReport(start=start, end=end, dimension=dimension, granularity=granularity)
    try:
        report = usage_explorer.build_usage([account], start, end, dimension, granularity, refresh=refresh)
    except Exception as exc:  # 兜底，避免整页 500
        fatal = f"{type(exc).__name__}: {exc}"
    toasts = dashboard.region_toasts(report.errors, account, "查不到 Cost Explorer")

    active = detect_preset(start, end, today)
    context = _frame(
        account, accounts, "cost", refresh=refresh, report=report, fatal=fatal, notes=notes, toasts=toasts,
        chart=chart.render_stacked_areas(report, config.CURRENCY_SYMBOL, ideal_width=1080),
        start=start, end=end, today=today, dimension=dimension, granularity=granularity,
        dimensions=dimensions, granularities=usage_explorer.GRANULARITIES,
        bucket_average=report.total_marked / (len(report.dates) or 1),
        range_label=_date_label(start, end, active),
        range_presets=_date_presets("account.cost", account, today, active, dim=dimension, granularity=granularity),
        date_min=_date_min(account, today),
    )
    return render_template("account_cost.html", **context)


# ---------------------------------------------------------------- 用量
def _clamp_window(account: Account, window: MetricWindow, notes: list[str]) -> MetricWindow:
    """开始时间不早于启用日期（本机时区那天的零点）。"""
    if not account.start_date:
        return window
    first = datetime.combine(account.start_date, time.min).astimezone().astimezone(timezone.utc)
    if window.start >= first:
        return window
    if first > datetime.now(timezone.utc):
        # 启用日期填在了今天之后：从它起算的话开始比现在还晚，先按所选时间看
        notes.append(f"启用日期 {account.start_date.isoformat()} 还没到，先按所选时间显示。")
        return window
    if window.end <= first:
        notes.append(f"所选时间在启用日期 {account.start_date.isoformat()} 之前，已改成从启用日期到现在。")
        end = datetime.now(timezone.utc)
    else:
        notes.append(f"开始时间早于启用日期，已从 {account.start_date.isoformat()} 起算。")
        end = window.end
    period_key, more = fit_period(first, end, window.period_key)
    notes.extend(more)
    period = PERIODS[period_key][0]
    return MetricWindow(start=floor_to_period(first, period), end=floor_to_period(end, period),
                        period_key=period_key, window_key="")


def _local(stamp: datetime) -> str:
    return stamp.astimezone().strftime("%Y-%m-%dT%H:%M")


@bp.route("/<number>/usage")
@login_required
def usage(number: str):
    account, accounts, bounce = _open(number, "usage")
    if bounce:
        return bounce
    refresh = request.args.get("refresh") == "1"
    window, notes = resolve_window(request.args)
    window = _clamp_window(account, window, notes)

    metric_key = (request.args.get("metric") or cloudwatch_metrics.DEFAULT_METRIC).strip()
    if metric_key not in cloudwatch_metrics.METRICS:
        metric_key = cloudwatch_metrics.DEFAULT_METRIC
    tag_filter = (request.args.get("tags") or cloudwatch_metrics.DEFAULT_TAG_FILTER).strip()
    if tag_filter not in cloudwatch_metrics.TAG_FILTERS:
        tag_filter = cloudwatch_metrics.DEFAULT_TAG_FILTER
    regions = list(cloudwatch_metrics.DEFAULT_REGIONS)

    fatal = None
    report = cloudwatch_metrics.UsageMetricsReport(window=window, metric_key=metric_key, tag_filter=tag_filter,
                                                   regions=regions)
    cards: list = []
    all_failed = False
    try:
        report = cloudwatch_metrics.build_metrics([account], regions, window, metric_key, tag_filter, refresh=refresh)
        # 四个区全读不到（被 SCP 拒绝之类）：别把「看不到」画成「0 次调用」
        all_failed = len(report.errors) >= len(regions)
        cards = _metric_cards(account, regions, window, metric_key, tag_filter, report, all_failed)
    except Exception as exc:
        fatal = f"{type(exc).__name__}: {exc}"
    if not fatal and not report.tags_resolved:
        notes.append("读不到推理配置上的标签（缺 bedrock:ListTagsForResource 权限？），"
                     "所有流量都会被当成「无标签」，标签筛选此时不可信。")
    toasts = dashboard.region_toasts(report.errors, account, "读不到 CloudWatch")

    views = chart.render_views(report)
    week = chart.week_grid(report.timestamps, report.column_totals, window.period)
    days = (window.end - window.start).total_seconds() / 86400
    week_note = "" if days >= 13 else f"只看了 {days:.0f} 天，有的格子只平均了一天，换成「近 30 天」更准。" if days >= 1 else ""
    active_window = detect_window(window)
    presets = [
        (url_for("account.usage", number=account.account, metric=metric_key, tags=tag_filter,
                 period=window.period_key, win=key), spec[0], active_window == key)
        for key, spec in WINDOWS.items()
    ]
    context = _frame(
        account, accounts, "usage", refresh=refresh, report=report, views=views, fatal=fatal, notes=notes,
        toasts=toasts, window=window,
        model_share=chart.render_bubbles([(s.name, s.total, chart.color_for(s.slot)) for s in report.series],
                                         height=180, fmt=lambda v: f"{v:,.0f} {report.unit}"),
        regions=cloudwatch_metrics.REGIONS, metrics=cloudwatch_metrics.METRICS, metric_key=metric_key,
        tag_filters=cloudwatch_metrics.TAG_FILTERS, tag_filter=tag_filter, periods=PERIODS,
        range_label=WINDOWS[active_window][0] if active_window in WINDOWS else
        f"{window.start.astimezone():%m-%d %H:%M} ~ {window.end.astimezone():%m-%d %H:%M}",
        range_presets=presets, local_start=_local(window.start), local_end=_local(window.end),
        local_now=_local(datetime.now(timezone.utc)),
        start_min=f"{account.start_date.isoformat()}T00:00" if account.start_date else "",
        metric_cards=cards, compare_label=f"比前 {_span_label(window)}", all_failed=all_failed,
        heat_panels=report.panels, heat_top=max((s.total for p in report.panels for s in p.series), default=0),
        week=week, week_top=max((v for r in week or [] for v in r if v is not None), default=0),
        week_note=week_note,
    )
    return render_template("account_usage.html", **context)


def _span_label(window: MetricWindow) -> str:
    hours = (window.end - window.start).total_seconds() / 3600
    if hours < 48:
        return f"{hours:.0f} 小时"
    return f"{hours / 24:.0f} 天"


def _metric_cards(account, regions, window, current_key, tag_filter, current_report, unreadable=False) -> list:
    """四张指标卡：这段时间的总数、和前一段同样长的时间比的变化、小走势。

    「总 Token」就是输入 + 输出，不单独查；前一段只要总数。一共多查 4~5 次
    CloudWatch（每次四个区），都有缓存。
    """
    def url(key):
        return url_for("account.usage", number=account.account, metric=key, tags=tag_filter,
                       period=window.period_key, win=window.window_key or None,
                       start=None if window.window_key else _local(window.start),
                       end=None if window.window_key else _local(window.end))

    if unreadable:
        # 读都读不到，别的指标也不用再查一遍了
        return [dict(key=key, label=label, unit=unit, total=None, delta=None, current=key == current_key,
                     url=url(key), spark="")
                for key, (label, _, unit) in cloudwatch_metrics.METRICS.items()]

    span = window.end - window.start
    before = MetricWindow(start=window.start - span, end=window.start, period_key=window.period_key)
    now_reports, old_totals = {}, {}
    for key in ("invocations", "input_tokens", "output_tokens"):
        now_reports[key] = current_report if key == current_key else cloudwatch_metrics.build_metrics(
            [account], regions, window, key, tag_filter)
        old_totals[key] = cloudwatch_metrics.build_metrics([account], regions, before, key, tag_filter).total

    def columns(key):
        return now_reports[key].column_totals

    totals = {key: now_reports[key].total for key in now_reports}
    totals["total_tokens"] = totals["input_tokens"] + totals["output_tokens"]
    old_totals["total_tokens"] = old_totals["input_tokens"] + old_totals["output_tokens"]
    series = {key: columns(key) for key in now_reports}
    series["total_tokens"] = [a + b for a, b in zip(columns("input_tokens"), columns("output_tokens"))]

    cards = []
    for key, (label, _, unit) in cloudwatch_metrics.METRICS.items():
        old = old_totals.get(key, 0.0)
        delta = (totals[key] - old) / old * 100 if old > 0 else None
        cards.append(dict(
            key=key, label=label, unit=unit, total=totals[key], delta=delta, current=key == current_key,
            url=url(key),
            spark=chart.render_spark(chart.downsample(series[key], 28), accent="#d97757", width=96, height=30),
        ))
    return cards


# ---------------------------------------------------------------- 配额
@bp.route("/<number>/quota")
@login_required
def quota(number: str):
    account, accounts, bounce = _open(number, "quota")
    if bounce:
        return bounce
    refresh = request.args.get("refresh") == "1"
    notes: list[str] = []
    model = (request.args.get("model") or "").strip()
    region = (request.args.get("region") or "").strip()
    if region and region not in quotas.QUOTA_REGIONS:
        notes.append("区域参数无效，已取消区域筛选。")
        region = ""

    fatal = None
    report = quotas.QuotaReport()
    try:
        report = quotas.build_quota_report([account], refresh=refresh)
    except Exception as exc:
        fatal = f"{type(exc).__name__}: {exc}"
    if model and model not in report.model_options:
        notes.append(f"「{model}」不在这个账号的配额里，已取消模型筛选。")
        model = ""
    report.apply_filters(model=model, region=region)

    toasts = []
    # 逐区的结构化原因（带 AWS 原话和 SCP 的策略 ARN）；只有一行字的时候退回去用它
    if report.errors or report.error:
        toasts += dashboard.region_toasts(report.errors or _as_list(report.error), account, "读不到 Service Quotas")
    if report.arn_errors or report.arn_error:
        toasts += dashboard.region_toasts(report.arn_errors or _as_list(report.arn_error), account,
                                          "读不到应用推理配置")

    context = _frame(account, accounts, "quota", refresh=refresh, report=report, fatal=fatal, notes=notes,
                     toasts=toasts, selected_model=model, selected_region=region,
                     quota_regions=quotas.QUOTA_REGIONS, quota_service=quotas.SERVICE_CODE,
                     quota_cache_hours=round(quotas.QUOTA_CACHE_TTL / 3600))
    return render_template("account_quota.html", **context)


def _as_list(value) -> list:
    if value is None:
        return []
    return list(value) if isinstance(value, (list, tuple)) else [value]


# ---------------------------------------------------------------- 预估
@bp.route("/<number>/estimate")
@login_required
def estimate(number: str):
    account, accounts, bounce = _open(number, "estimate")
    if bounce:
        return bounce
    today = date.today()
    start, end, notes = resolve_range(request.args, today)
    start, end = _clamp_dates(account, start, end, today, notes)
    refresh = request.args.get("refresh") == "1"

    fatal = None
    report = cost_estimate.EstimateReport(start=start, end=end)
    try:
        report = cost_estimate.build_estimate([account], start, end, refresh=refresh)
    except Exception as exc:
        fatal = f"{type(exc).__name__}: {exc}"
    if report.price_stale:
        notes.append(f"拉不到最新的 AWS 价目表，用的是本地缓存副本（{report.price_error}）。单价可能已经过时。")
    if report.unpriced:
        notes.append("这些模型在 AWS 价目表里没有对应条目，没有计入估算总额：" + "、".join(report.unpriced)
                     + "。多半是刚发布的新模型，等 AWS 更新价目表即可。")
    toasts = dashboard.region_toasts(report.errors, account, "读不到 CloudWatch")

    active = detect_preset(start, end, today)
    context = _frame(
        account, accounts, "estimate", refresh=refresh, report=report, fatal=fatal, notes=notes, toasts=toasts,
        chart=chart.render_stacked_areas(report, config.CURRENCY_SYMBOL, ideal_width=1080), start=start, end=end, today=today,
        kinds=cost_estimate.KIND_ORDER, kind_labels=cost_estimate.KIND_LABELS,
        range_label=_date_label(start, end, active),
        range_presets=_date_presets("account.estimate", account, today, active),
        date_min=_date_min(account, today),
    )
    return render_template("account_estimate.html", **context)
