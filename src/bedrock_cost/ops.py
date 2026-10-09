"""运营看板：电脑上每天巡检用的一页。

三块：经营汇总（收入、AWS 原价、毛利，按上游 / 按账号）、风险与告警（需要关注的账号、
最近发出去的告警）、模型与用量（全部账号的调用和 token，按模型、按区域）。数据怎么来、
口径是什么见 ops_report.py。

四份数据互不依赖、各自都要查 AWS，所以并发着拿：每个账号近 12 个月的逐日成本（CE）、
概览那份累计报表（CE，和概览共用缓存）、用量状态（CloudWatch，和概览共用缓存）、三个指标
的用量（CloudWatch）。哪一份出错只影响它那一块，页面照常出来，原因从右上角说。
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import date

from flask import Blueprint, render_template, request

from . import activity, config, dashboard, ops_report
from .auth import login_required
from .excel_source import ExcelSourceError, load_accounts, load_lifecycle
from .report import build_report
from .views import page_meta

bp = Blueprint("ops", __name__, url_prefix="/ops")


def _safely(job, *args):
    """跑一份数据；出错返回 (None, 一句话原因)，别让一块的错把整页带走。
    AWS 的错数据层自己会收成 QueryError，走到这里的是意料之外的。"""
    try:
        return job(*args), None
    except Exception as exc:  # 兜底
        return None, f"{type(exc).__name__}: {exc}"


@bp.route("/")
@login_required
def index():
    today = date.today()
    refresh = request.args.get("refresh") == "1"
    period = ops_report.period_for(request.args.get("period") or "", today)
    context = dict(active_page="ops", period=period, periods=ops_report.PERIODS, today=today,
                   warn_pct=config.WARN_PCT, danger_pct=config.DANGER_PCT, **page_meta())

    try:
        accounts = load_accounts(force=refresh)
        everyone = load_accounts(include_disabled=True)
        tags = load_lifecycle()
    except ExcelSourceError as exc:
        return render_template("ops.html", fatal=str(exc), **context)

    if refresh:
        ops_report.clear_cache()
    with ThreadPoolExecutor(max_workers=4) as pool:
        jobs = [
            pool.submit(_safely, ops_report.build_ledger, accounts, today, refresh),
            pool.submit(_safely, build_report, today, refresh),
            pool.submit(_safely, activity.activities, accounts, refresh),
            pool.submit(_safely, ops_report.build_usage_block, accounts, period, refresh),
        ]
        (ledger, ledger_fail), (report, report_fail), (states, states_fail), (usage, usage_fail) = (
            job.result() for job in jobs
        )

    # 概览报表的行按 (号码, 行号) 对回台账里的账号
    credit_rows: dict = {}
    if report is not None:
        by_row = {(a.account, a.row): a for a in accounts}
        for row in report.rows:
            account = by_row.get((row.account, row.row_number))
            if account is not None:
                credit_rows[account.key] = row

    toasts: list[dashboard.Toast] = []
    for what, failure in (("经营汇总出不来", ledger_fail), ("额度出不来", report_fail),
                          ("用量状态出不来", states_fail), ("模型与用量出不来", usage_fail)):
        if failure:
            toasts.append(dashboard.Toast("error", what, text="页面其余部分照常显示", detail=failure))

    business = trend = None
    known: set[str] = set()
    if ledger is not None:
        business = ops_report.build_business(accounts, ledger, period, credit_rows)
        trend = ops_report.monthly_trend(ledger, today)
        toasts += dashboard.account_toasts(ledger.errors, accounts, "查不到 Cost Explorer")
        known = {dashboard._parts(error)[0] for error in ledger.errors}
    if report is not None:
        # 累计那份是另一次查询；逐日那份已经报过的账号不再报一遍
        extra = [row.problem or f"{row.partner} / {row.account}：{row.error}" for row in report.rows
                 if row.error and row.account not in known]
        toasts += dashboard.account_toasts(extra, accounts, "查不到累计消费")
    if usage is not None:
        toasts += dashboard.account_toasts(usage.errors, accounts, "读不到 CloudWatch")

    return render_template(
        "ops.html",
        accounts=accounts,
        business=business,
        trend=trend,
        credit=ops_report.credit_summary(report) if report is not None else None,
        watch=ops_report.watch_list(accounts, credit_rows, states or {}, tags),
        events=ops_report.recent_events(everyone),
        life_counts=dashboard.lifecycle_counts(accounts, tags),
        unlabeled=sum(1 for a in accounts if not a.lifecycle),
        lifecycle_colors={tag.name: tag.hex for tag in tags},
        usage=usage,
        toasts=toasts,
        fatal=None,
        **context,
    )
