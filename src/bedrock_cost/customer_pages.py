"""客户页：客户列表、客户详情，以及详情页上的几个动作（新建 / 修改客户、分配、替换、标记风控、结算、
调整额度、解绑、记一笔、改 / 删时间线上的事）。

算钱、时间线、对账单都在 customers 里；写台账在 excel_source 里。这里只管表单进出和提示文案。
所有动作都是普通的表单提交（POST 之后跳回详情页），没开 JS 也能用。
"""

from __future__ import annotations

import csv
import io
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

from flask import Blueprint, Response, flash, redirect, render_template, request, session, url_for

from . import chart, cloudwatch_metrics, config, cost_estimate, customers, dashboard, excel_source, usage_explorer
from .auth import csrf_protect, login_required
from .dates import parse_date
from .excel_source import Account, ExcelSourceError
from .filters import money, money0
from .views import flash_result, page_meta, range_links, spend_links
from .windows import PERIODS, MetricWindow, resolve_window

bp = Blueprint("customers", __name__, url_prefix="/customers")

# 替换账号时能选的原因；前两个算风控，旧账号顺手打上「风控」
REPLACE_REASONS = ("风控", "封号", "额度用完", "上游回收", "其他")
RISKY_REASONS = ("风控", "封号")


def _actor() -> str:
    return session.get("user") or ""


def _who(account: Account | None, fallback: str = "") -> str:
    if account is None:
        return fallback
    return account.email or account.account


def _ledger():
    """(全部账号含停用的, 客户, 时间线事件, 读不了的原因)。"""
    try:
        accounts = excel_source.load_accounts(include_disabled=True)
        return accounts, excel_source.load_customers(), excel_source.load_events(), None
    except ExcelSourceError as exc:
        return [], [], [], str(exc)
    except Exception as exc:  # 兜底，避免整页 500
        return [], [], [], f"{type(exc).__name__}: {exc}"


def _when(raw: str | None, today: date) -> date:
    """表单里的日期：认不出、晚于今天的都按今天。"""
    picked = parse_date(raw)
    return picked if picked and picked <= today else today


def _toasts(views) -> list[dashboard.Toast]:
    """查不到消费的账号：原因相同的合成一条（和概览一样）。"""
    errors, accounts = [], []
    for view in views:
        for holding in view.holdings:
            if holding.series.error is not None:
                errors.append(holding.series.error)
                accounts.append(holding.account)
    return dashboard.account_toasts(errors, accounts, "查不到每天的消费") if errors else []


# ---------------------------------------------------------------- 卡片和图上用的小东西
avatar_info = customers.avatar_info


def _rank(view: customers.CustomerView) -> tuple:
    """卡片顺序：有问题的在前（有异常账号、快用完），暂停、流失的在后。"""
    status = {"on": 0, "pause": 1, "gone": 2}.get(view.customer.status, 0)
    left = view.days_left
    trouble = 0 if view.counts["bad"] else 1
    return status, trouble, left if left is not None else float("inf"), view.customer.id


def _slots(names: list[str]) -> dict[str, int]:
    return usage_explorer.assign_slots(names[: usage_explorer.MAX_SERIES])


def _stable_slots(ranked: list[str], everyone) -> dict[str, int]:
    """「每日消费」的颜色：近 30 天花得最多的几个各占一个色槽，换看多久也不变；只在别的时间、别的图上有的
    （30 天里没花钱）拿剩下的色槽。"""
    slots = _slots(ranked[: usage_explorer.MAX_SERIES])
    for name in sorted(set(everyone) - set(slots)):
        free = [slot for slot in range(usage_explorer.MAX_SERIES) if slot not in slots.values()]
        slots[name] = free[0] if free else -1
    return slots


def _per_customer(cards, start: date, end: date) -> dict[str, list[float]]:
    """客户列表：每个客户每天的消费（只算在用的那几天，见 Holding.active_daily）。"""
    width = (end - start).days + 1
    out = {}
    for view in cards:
        columns = [h.active_daily(start, end) for h in view.holdings]
        out[view.customer.id] = [sum(day) for day in zip(*columns)] if columns else [0.0] * width
    return out


def _stacked_rows(series: dict[str, list[float]], names: dict[str, str], width: int,
                  slots: dict[str, int] | None = None) -> list:
    """堆叠柱的序列：按总量排，8 个以外并进「其他」。slots 给了就照它配色（同一页上几张图，同一个账号同一个颜色）。"""
    ranked = sorted((key for key in series if sum(series[key]) > 0), key=lambda key: -sum(series[key]))
    keep = ranked[: usage_explorer.MAX_SERIES]
    slots = slots if slots is not None else _slots([names[key] for key in keep])
    rows = [SimpleNamespace(name=names[key], slot=slots.get(names[key], -1), values=series[key]) for key in keep]
    other = [sum(series[key][i] for key in ranked[usage_explorer.MAX_SERIES:]) for i in range(width)]
    if any(other):
        rows.append(SimpleNamespace(name=usage_explorer.OTHER_LABEL, slot=-1, values=other))
    return rows


def _spend_chart(series: dict[str, list[float]], stamps: list[date], names: dict[str, str], height: int = 230,
                 width: int = 560, slots: dict[str, int] | None = None, marks: list[chart.BarMark] | None = None):
    """每天的消费（stamps 那几天）：一根柱一天，按客户（或账号）堆叠。8 个以外并进「其他」。marks 是竖虚线。"""
    rows = _stacked_rows(series, names, len(stamps), slots)
    labels = [stamp.strftime("%m-%d") for stamp in stamps]
    bars = chart.render_bars(labels, rows, width=width, height=height, fmt=money, axis=customers.short_money,
                             marks=marks)
    total = sum(sum(values) for values in series.values())
    return SimpleNamespace(chart=bars, total=total, average=total / len(stamps) if stamps else 0.0,
                           start=labels[0] if labels else "", end=labels[-1] if labels else "")


# ---------------------------------------------------------------- 客户列表
@bp.route("/")
@login_required
def index(**extra):
    today = date.today()
    refresh = request.args.get("refresh") == "1"
    accounts, all_customers, events, fatal = _ledger()
    views = {}
    if not fatal and all_customers:
        try:
            views = customers.build_views(all_customers, accounts, events, today, refresh=refresh)
        except ExcelSourceError as exc:
            fatal = str(exc)
    cards = sorted(views.values(), key=_rank)

    context: dict = {}
    if cards:
        # 近 30 天：「按区域」和每张客户卡片用这一份
        per_customer = _per_customer(cards, today - timedelta(days=29), today)
        names = {view.customer.id: view.customer.name for view in cards}
        # 「每日消费」右上角选看多久，「全部」从最早分过来的那个账号算起；颜色按近 30 天花得最多的几个客户分，换时间不变
        spend = dashboard.spend_range(request.args.get("spend"), today,
                                      min((h.joined for view in cards for h in view.holdings if h.joined), default=None))
        series = per_customer if spend.days == 30 else _per_customer(cards, spend.start, today)
        stamps = [spend.start + timedelta(days=i) for i in range(spend.days)]
        ranked = sorted((cid for cid in per_customer if sum(per_customer[cid]) > 0), key=lambda cid: -sum(per_customer[cid]))
        trend = _spend_chart(series, stamps, names, slots=_stable_slots([names[cid] for cid in ranked], names.values()))
        trend.label = spend.label
        soon = sorted((view for view in cards if view.days_left is not None and view.balance > 0
                       and view.customer.status != "gone"), key=lambda view: view.days_left)[:4]
        regions: dict[str, float] = {}
        for view in cards:
            code = view.customer.region or "OTHER"
            regions[code] = regions.get(code, 0.0) + sum(per_customer[view.customer.id])
        by_region = sorted(((code, value) for code, value in regions.items() if value > 0), key=lambda kv: -kv[1])
        context = dict(trend=trend, spend=spend, spend_links=spend_links("customers.index", spend.key), soon=soon,
                       by_region=by_region,
                       region_top=by_region[0][1] if by_region else 1.0, per_customer=per_customer)

    region_counts: dict[str, int] = {}
    for view in cards:
        code = view.customer.region or "OTHER"
        region_counts[code] = region_counts.get(code, 0) + 1
    used_regions = [(code, name, region_counts[code]) for code, name in customers.REGIONS if code in region_counts]
    status_counts = {key: sum(1 for view in cards if view.customer.status == key)
                     for key in excel_source.CUSTOMER_STATUSES}
    return render_template(
        "customers.html",
        active_page="customers",
        cards=cards,
        fatal=fatal,
        toasts=_toasts(cards),
        today=today,
        stock_count=len(customers.stock(accounts)),
        used_regions=used_regions,
        status_counts=status_counts,
        new_id=_next_id(all_customers),
        form=extra.pop("form", {}),
        errors=extra.pop("errors", []),
        open_create=extra.pop("open_create", False),
        **_shared(),
        **context,
        **extra,
        **page_meta(),
    )


def _next_id(all_customers) -> str:
    numbers = [int(c.id[1:]) for c in all_customers if c.id[1:].isdigit()]
    return f"C{max(numbers, default=0) + 1:03d}"


def _shared() -> dict:
    """列表和详情都要的：地区、状态、头像个数（画头像、国旗的几个函数是模板全局，见 filters）。"""
    return dict(
        regions=customers.REGIONS,
        statuses=excel_source.CUSTOMER_STATUSES,
        avatar_count=excel_source.CUSTOMER_AVATARS,
        stages=customers.STAGES,
    )


@bp.route("/create", methods=["POST"])
@login_required
@csrf_protect
def create():
    _, all_customers, _, fatal = _ledger()
    if fatal:
        flash(fatal, "error")
        return redirect(url_for("customers.index"))
    data, errors = customers.validate_customer(request.form, all_customers)
    if errors:
        return index(form=request.form.to_dict(), errors=errors, open_create=True), 400
    try:
        cid = excel_source.create_customer(data, actor=_actor())
    except ExcelSourceError as exc:
        return index(form=request.form.to_dict(), errors=[str(exc)], open_create=True), 409
    flash_result("已新建客户", data["name"], "下一步：给它分配账号。")
    return redirect(url_for("customers.detail", cid=cid))


# 客户页「调用次数」的取数（每个账号四区合计）。测试里默认换成不查 CloudWatch 的（见 tests/conftest.py）
_account_calls = cloudwatch_metrics.account_totals


def daily_estimates(accounts: list[Account], start: date, end: date,
                    refresh: bool = False) -> tuple[dict[str, list[float]], bool]:
    """start 到 end（UTC 日期）每天每个账号的预估花费，今天算到现在：CloudWatch 的 token × 牌价，和账号页「预估」
    页签一个算法——AWS 原价，不套台账的比率，以账单为准。

    返回 ({account.key: 每天的钱}, 有没有没算上的——读不到的区、价目表里没有的模型)。几个账号并发着查；刷新时只清一次
    缓存，不让每个账号都去重拉一遍价目表。
    """
    if not accounts:
        return {}, False
    if refresh:
        cost_estimate.clear_cache()

    def one(account: Account):
        return account.key, cost_estimate.build_estimate([account], start, end)

    with ThreadPoolExecutor(max_workers=max(1, min(config.MAX_WORKERS, len(accounts)))) as pool:
        reports = dict(pool.map(one, accounts))
    partial = any(report.errors or report.unpriced or report.price_stale for report in reports.values())
    return {key: report.column_totals for key, report in reports.items()}, partial


# 客户页的预估（「预估」那张、「调用次数」上的今日预估）走这个名字：测试里默认换成不查的（见 tests/conftest.py）
_estimate_days = daily_estimates

# 「预估」右上角选看多久，网址上是 ?estimate=（近 7 天是默认，不带）。一天一根，没有按小时的
ESTIMATE_RANGES = (("7", "近 7 天"), ("30", "近 30 天"), ("all", "全部"))


def _estimate_window(view: customers.CustomerView, key: str) -> SimpleNamespace:
    """「预估」看哪几天：按 UTC 日期（CloudWatch 的日桶就是在 UTC 零点切的），今天算到现在。全部从最早分过来的那个
    账号算起，不早于 CloudWatch 按天能查的最早一天（455 天），和「调用次数」的全部一样。"""
    key = key if key in dict(ESTIMATE_RANGES) else "7"
    today = datetime.now(timezone.utc).date()
    if key == "all":
        first = min((h.joined for h in view.holdings if h.joined), default=today - timedelta(days=6))
        start = max(min(first, today), today - timedelta(days=PERIODS["1d"][2] - 1))
        what = f"{start:%m-%d} 起累计预估"
    else:
        start = today - timedelta(days=int(key) - 1)
        what = f"{dict(ESTIMATE_RANGES)[key]}预估"
    return SimpleNamespace(key=key, start=start, end=today, what=what)


def _estimate_card(view: customers.CustomerView, names: dict[str, str], slots: dict[str, int],
                   costs: dict[str, list[float]], partial: bool, window: SimpleNamespace):
    """「预估」（原来是「还能用几天」）：每天的预估花费（右上角选近 7 天、近 30 天、全部），按账号叠起来，颜色和
    「每日消费」一样。账单晚 1~2 天、被 credits 抵成 0 的时候，这张照样看得到实际用了多少。和「调用次数」按天看的一个
    规矩：每个账号只算它算这个客户的那几天（分过来那天起，到风控、换下、用完、结算的前一天）。costs 是
    daily_estimates 查好的 {account.key: 每天的钱}，window 是 _estimate_window。
    """
    start, end = window.start, window.end
    stamps = [start + timedelta(days=i) for i in range((end - start).days + 1)]
    width = len(stamps)
    per_account: dict[str, list[float]] = {}
    today_by_key: dict[str, float] = {}       # 今天还算这个客户的账号今天的预估：「调用次数」上的今日预估、签上的「今日」
    for h in view.holdings:
        values = costs.get(h.key)
        if not values:
            continue
        begin, stop = h.joined or date.min, h.left or date.max
        mine = [value if begin <= day < stop else 0.0 for value, day in zip(values, stamps)]
        per_account[h.number] = [a + b for a, b in zip(per_account.get(h.number, [0.0] * width), mine)]
        if begin <= end < stop:
            today_by_key[h.key] = mine[-1]
    totals = [sum(column) for column in zip(*per_account.values())] if per_account else [0.0] * width
    labels = [day.strftime("%m-%d") for day in stamps[:-1]] + ["今天"]
    # 和左边「调用次数」一排、各占一半宽：两张图一样宽、一样高（600 × 334，和调用次数的折线图一个比例）
    bars = chart.render_bars(labels, _stacked_rows(per_account, names, width, slots), width=600, height=334,
                             fmt=money, axis=customers.short_money)
    return SimpleNamespace(chart=bars, total=sum(totals), today=totals[-1], yesterday=totals[-2] if width > 1 else 0.0,
                           partial=partial, start=stamps[0].strftime("%m-%d"), end=stamps[-1].strftime("%m-%d"),
                           today_by_key=today_by_key, key=window.key, what=window.what)


# 「调用次数」右上角选看多久，网址上是 ?calls=（近 24 小时是默认，不带）
CALLS_RANGES = (("24h", "近 24 小时"), ("7", "近 7 天"), ("30", "近 30 天"), ("all", "全部"))


def _calls_window(view: customers.CustomerView, key: str) -> MetricWindow:
    """近 24 小时每小时一根，最后一根是上一个整点那一小时（没过完的这一小时不画）。近 7 天、近 30 天、全部每天一根，
    按 UTC 日期——和 Cost Explorer 的账、「每日消费」一个分法——今天算到现在。全部从最早分过来的那个账号算起，
    不早于 CloudWatch 按天能查的最早一天（455 天）。"""
    if key not in dict(CALLS_RANGES) or key == "24h":
        return resolve_window({"win": "24h", "period": "1h"})[0]
    today = datetime.now(timezone.utc).date()
    if key == "all":
        first = min((h.joined for h in view.holdings if h.joined), default=today - timedelta(days=29))
        start = max(min(first, today), today - timedelta(days=PERIODS["1d"][2] - 1))
    else:
        start = today - timedelta(days=int(key) - 1)

    def midnight(day: date) -> datetime:
        return datetime(day.year, day.month, day.day, tzinfo=timezone.utc)

    return MetricWindow(start=midnight(start), end=midnight(today + timedelta(days=1)), period_key="1d", window_key=key)


def _calls_chart(view: customers.CustomerView, names: dict[str, str], slots: dict[str, int], key: str = "24h",
                 refresh: bool = False, today_costs: dict[str, float] | None = None, partial: bool = False):
    """调用次数：CloudWatch 的调用次数（四区合计），晚几分钟出数，比账单快得多。折线图，一个账号一条线，线尾是这个
    账号的头像；颜色和「每日消费」一样。点下面的账号挑几个只看它们，纵轴跟着它们重新算（app.js）。

    近 24 小时只查还在用的账号——结算了、停用了的不会再有调用，少查几次。按天看的查这段时间里在这个客户名下用过的
    账号（停用的不查），每个账号只算它算这个客户的那几天（Holding.active_daily 一样的规矩：分过来那天起，到风控、
    换下、用完、结算的前一天）。一直没调用的不画线，在图下面点名；账号多过 8 个时，调用少的并成「其他」一条。
    today_costs 是「预估」那张算好的今天还算这个客户的账号今天的预估（{account.key: 金额}，UTC 日期），加起来就是
    今日预估，和「预估」那张的「今天到现在」是同一个数；partial 是有没有没算上的。
    """
    window = _calls_window(view, key)
    hourly = window.period_key != "1d"
    if hourly:
        watched = [h for h in view.holdings if h.stage != "settled" and h.account.enabled]
    else:
        first, last = window.start.date(), (window.end - timedelta(days=1)).date()
        watched = [h for h in view.holdings if h.account.enabled
                   and (h.joined or date.min) <= last and (h.left or date.max) > first]
    counts = _account_calls([h.account for h in watched], window, "invocations", refresh=refresh)
    width = len(counts.timestamps)
    by_number = {h.number: h for h in watched}
    series = {}
    for h in watched:
        values = counts.values.get(h.key) or [0.0] * width
        if not hourly:
            begin, stop = h.joined or date.min, h.left or date.max
            values = [value if begin <= stamp.date() < stop else 0.0 for value, stamp in zip(values, counts.timestamps)]
        series[h.number] = values
    # 悬浮提示里的时间：按小时的写 10-12 13:00（本机时间），按天的写 10-12（UTC 日期）
    labels = counts.labels if hourly else [stamp.strftime("%m-%d") for stamp in counts.timestamps]
    ranked = sorted((number for number in series if any(series[number])), key=lambda number: -sum(series[number]))
    keep, rest = ranked[: usage_explorer.MAX_SERIES], ranked[usage_explorer.MAX_SERIES:]
    lines = [cloudwatch_metrics.MetricSeries(name=names[number], values=series[number],
                                             slot=slots.get(names[number], -1)) for number in keep]
    if rest:
        lines.append(cloudwatch_metrics.MetricSeries(
            name=usage_explorer.OTHER_LABEL, values=[sum(series[number][i] for number in rest) for i in range(width)],
            slot=-1))
    report = SimpleNamespace(labels=labels, timestamps=counts.timestamps, window=window, series=lines,
                             metric_label="调用次数", unit="次", dimension_label="账号")
    drawn = chart.render_lines(report, unit="次", width=600, uid="calls",
                               ends={names[number]: by_number[number].account.avatar for number in keep})
    totals = [sum(values[i] for values in series.values()) for i in range(width)]
    peak = max(range(width), key=lambda i: totals[i]) if any(totals) else None
    costs = today_costs or {}
    label = dict(CALLS_RANGES).get(key, "近 24 小时")
    # 图下面的签：一条线一个，和线同一个顺序（签上的 data-s 就是线的序号），悬浮看这个账号这段时间多少次、今天预估花了多少
    legend = []
    for number in keep:
        tip = f"{by_number[number].account.email or number} · {label} {sum(series[number]):,.0f} 次"
        if by_number[number].key in costs:
            tip += f" · 今日 {money(costs[by_number[number].key])}"
        legend.append(SimpleNamespace(name=names[number], color=chart.color_for(slots.get(names[number], -1)), tip=tip))
    if rest:
        legend.append(SimpleNamespace(name=usage_explorer.OTHER_LABEL, color=chart.color_for(-1),
                                      tip=f"调用少的 {len(rest)} 个账号并在一起 · {label}"))
    if hourly:
        # 写到窗口的末尾（最后一个点是 17:00 那一小时，数到 18:00），不写最后一个点的起点
        start = counts.labels[0] if width else ""
        end = window.end.astimezone().strftime("%m-%d %H:00") if width else ""
    else:
        start, end = (labels[0], labels[-1]) if width else ("", "")
    return SimpleNamespace(
        key=key if key in dict(CALLS_RANGES) else "24h", label=label, hourly=hourly,
        chart=drawn, legend=legend, total=sum(totals), last=totals[-1] if totals else 0.0,
        average=sum(totals) / width if width else 0.0,
        today_cost=sum(costs.values()), partial=partial,
        silent=[h for h in watched if not any(series[h.number])],
        peak_label=labels[peak] if peak is not None else "", peak_value=totals[peak] if peak is not None else 0.0,
        active=sum(1 for values in series.values() if any(values)), watched=len(watched),
        start=start, end=end, errors=counts.errors, accounts=[h.account for h in watched],
    )


# ---------------------------------------------------------------- 每日消费：竖虚线
# 竖虚线：手动记的事里这几类（启用另算，是分过来那天；用完另算，是累计消费到额度那天）。
# 「停」的那条写上从启用（或恢复）到前一天用了多少；同一个账号同一天的几件事并成一条
_MARK_EVENTS = {"risk": ("stop", "风控"), "replace": ("stop", "换下"), "settle": ("stop", "结算"),
                "restore": ("start", "恢复"), "unrisk": ("start", "取消风控")}


def _spend_marks(view: customers.CustomerView, stamps: list[date], names: dict[str, str],
                 slots: dict[str, int]) -> list[chart.BarMark]:
    """「每日消费」上每个账号的竖虚线：哪天启用、哪天风控 / 换下 / 用完 / 结算、哪天恢复。只画落在图里的那几天，
    「用了多少」照样从图外的启用日算起。"""
    where = {day: i for i, day in enumerate(stamps)}
    events = [e for e in view.events if not e.deleted and e.source == "manual"]
    marks = []
    for h in view.holdings:
        moments = [(h.joined, 0, "start", "启用")] if h.joined else []
        moments += [(e.date, e.id) + _MARK_EVENTS[e.type] for e in events
                    if e.account == h.number and e.type in _MARK_EVENTS]
        if h.left_why == "用完" and h.used_up_on:
            moments.append((h.used_up_on, 10**9, "stop", "用完"))
        name = names[h.number]
        # 同一天的几件事并成一条线（风控换下的那天是「风控、换下」）
        for day, labels, start, used in customers.mark_days(moments, h.daily):
            if day not in where:
                continue
            what = "、".join(labels)
            sub = f"{what} · {day.isoformat()}"
            if "启用" in labels and h.via:
                peer = view.accounts_by_number.get(h.via)
                sub += f" · 换下 {peer.label if peer else h.via}"
            marks.append(chart.BarMark(
                index=where[day], label=what if used is None else f"{what} {customers.short_money(used)}",
                series=name, color=chart.color_for(slots.get(name, -1)), face=h.account.avatar,
                tip=h.account.email or h.number, tip_sub=sub,
                tip_foot="" if used is None else f"{start.strftime('%m-%d')} 起用了 {money(used)}",
            ))
    return marks


# ---------------------------------------------------------------- 客户详情
# 客户页三排「看多久」各自的默认值：网址上不带。「每日消费」默认近 7 天（概览、客户列表、账号页是近 30 天）
_RANGE_DEFAULTS = {"spend": "7", "calls": "24h", "estimate": "7"}
# 切「看多久」时能只取回来的那几张卡片（?part=…，见 customer_part.html）
CHART_PARTS = ("calls", "estimate", "spend")


def _ranges(**picked: str) -> dict[str, str | None]:
    """别的几排选了什么，写进这一排的链接：选的是默认值就不带（None）。"""
    return {name: None if key == _RANGE_DEFAULTS[name] else key for name, key in picked.items()}


def _detail_context(cid: str, refresh: bool = False):
    """(客户, CustomerView, 全部账号, 读不了的原因)。没有这个客户时客户是 None。"""
    accounts, all_customers, events, fatal = _ledger()
    if fatal:
        return None, None, [], fatal
    customer = next((c for c in all_customers if c.id == cid.upper()), None)
    if customer is None:
        return None, None, accounts, None
    view = customers.build_views([customer], accounts, events, date.today(), refresh=refresh)[customer.id]
    return customer, view, accounts, None


@bp.route("/<cid>")
@login_required
def detail(cid: str, **extra):
    refresh = request.args.get("refresh") == "1"
    customer, view, accounts, fatal = _detail_context(cid, refresh)
    if fatal:
        flash(fatal, "error")
        return redirect(url_for("customers.index"))
    if customer is None:
        flash_result("没有这个客户", cid, "可能已经被删了，或者网址写错了。", tone="warn")
        return redirect(url_for("customers.index"))
    today = view.today
    counts = view.counts

    # 「账号」：按阶段一根横条，长短按占全部账号的比例，名字后面是这一档里的账号头像
    stage_rows = [
        SimpleNamespace(key=key, label=label, color=color, count=counts[key],
                        holdings=[h for h in view.listed if h.stage == key])
        for key, label, color in (("use", "使用中", chart.TONE_COLORS["ok"]),
                                  ("risk", "风控 · 待替换", chart.TONE_COLORS["danger"]),
                                  ("pending", "待结算", "#de9a92"), ("settled", "已结算", "#c2c0b6"))
    ]
    stamps, per_account = view.daily(30)
    names = {h.number: h.account.label for h in view.holdings}
    # 「每日消费」的颜色照旧（按近 30 天有消费的账号分色槽，换成近 90 天、全部也不变）；「近 24 小时调用」用同一套，
    # 一个账号一个颜色。只在别的时间、别的图上有的账号（30 天没消费）拿剩下的色槽
    spenders = sorted((number for number in per_account if sum(per_account[number]) > 0),
                      key=lambda number: -sum(per_account[number]))
    slots = _stable_slots([names[number] for number in spenders], names.values())
    # 右上角选看多久（默认近 7 天），「全部」从最早分过来的那个账号算起
    spend = dashboard.spend_range(request.args.get("spend"), today,
                                  min((h.joined for h in view.holdings if h.joined), default=None),
                                  default=_RANGE_DEFAULTS["spend"])
    if spend.days != 30:
        stamps, per_account = view.daily(spend.days)
    trend = _spend_chart(per_account, stamps, names, height=230, width=1100, slots=slots,
                         marks=_spend_marks(view, stamps, names, slots))
    trend.label = spend.label
    # 「预估」和「调用次数」上的今日预估一起查一次（右上角选的那几天，UTC 日期，今天算到现在）：这几天在这个客户
    # 名下用过的账号，停用的不查
    window = _estimate_window(view, request.args.get("estimate") or "7")
    estimated = [h.account for h in view.holdings if h.account.enabled
                 and (h.joined or date.min) <= window.end and (h.left or date.max) > window.start]
    costs, partial = _estimate_days(estimated, window.start, window.end, refresh=refresh)
    estimate = _estimate_card(view, names, slots, costs, partial, window)
    # 「调用次数」右上角也选看多久：近 24 小时（默认）按小时，其余按天
    calls = _calls_chart(view, names, slots, request.args.get("calls") or "24h", refresh=refresh,
                         today_costs=estimate.today_by_key, partial=partial)
    # 三排「看多久」互相带着别的两排选的，切一排另外两排不变
    links = {
        "spend_links": spend_links("customers.detail", spend.key, _RANGE_DEFAULTS["spend"], cid=customer.id,
                                   **_ranges(calls=calls.key, estimate=estimate.key)),
        "calls_links": range_links("customers.detail", "calls", CALLS_RANGES, calls.key, "24h", cid=customer.id,
                                   **_ranges(spend=spend.key, estimate=estimate.key)),
        "estimate_links": range_links("customers.detail", "estimate", ESTIMATE_RANGES, estimate.key, "7",
                                      cid=customer.id, **_ranges(spend=spend.key, calls=calls.key)),
    }
    # 切「看多久」时 app.js 只要那一张卡片（?part=calls / estimate / spend）：同一套算法、同一份宏，不画整页
    part = request.args.get("part")
    if part in CHART_PARTS:
        return render_template("customer_part.html", part=part, trend=trend, calls=calls, estimate=estimate, **links)

    by_number = {a.account: a for a in accounts}
    timeline = _timeline_payload(view, by_number)
    stock = customers.stock(accounts)
    # 被换下的账号换成了谁（恢复使用的弹窗里说一声：换上来的那个不受影响）
    swapped = {e.account: e.peer for e in view.events if e.type == "replace" and not e.deleted and e.source == "manual"}
    # 表格里每个账号的动作（结算、调整额度、标记风控、解绑）打开弹窗时要填的数，交给页面脚本
    holdings_data = {
        "balance": view.balance,
        "name": customer.name,
        "holdings": {
            h.key: {
                "email": h.account.email or h.number, "number": h.number, "partner": h.account.partner,
                "budget": h.account.budget, "spent": h.spent, "raw": h.raw_spent, "remaining": h.remaining,
                "stage": h.stage, "stage_label": h.stage_label, "left": h.left.strftime("%m-%d") if h.left else "",
                "left_why": h.left_why, "has_numbers": h.has_numbers, "restorable": h.restorable,
                "replaced_by": _who(by_number.get(swapped.get(h.number, "")), swapped.get(h.number, "")),
                # 最近两天还有量：Cost Explorer 的账还没出完，结算时提醒一声
                "recent": sum(h.daily(today - timedelta(days=1), today)) > config.STOP_DAILY,
            }
            for h in view.holdings
        },
    }
    return render_template(
        "customer.html",
        active_page="customers",
        customer=customer,
        view=view,
        hero_av=avatar_info(customer),
        stage_rows=stage_rows,
        trend=trend,
        spend=spend,
        calls=calls,
        estimate=estimate,
        **links,
        timeline=timeline,
        timeline_fallback=_timeline_fallback(view, by_number),
        categories=customers.CATEGORIES,
        lifecycle_colors=excel_source.lifecycle_colors(),
        stock=stock,
        by_number=by_number,
        holdings_data=holdings_data,
        currency=config.CURRENCY_SYMBOL,
        reasons=REPLACE_REASONS,
        manual_kinds=[(kind, customers.EVENT_TYPES[kind][0]) for kind in customers.NOTE_KINDS],
        stage_names=customers.STAGE_SHORT,
        stage_rank=customers.TABLE_ORDER,
        today=today,
        toasts=_toasts([view]) + (dashboard.account_toasts(calls.errors, calls.accounts, "读不到 CloudWatch")
                                  if calls.errors else []),
        form=extra.pop("form", None),
        errors=extra.pop("errors", []),
        open_edit=extra.pop("open_edit", False),
        **_shared(),
        **extra,
        **page_meta(),
    )


def _timeline_payload(view: customers.CustomerView, by_number: dict[str, Account]) -> dict:
    """时间线交给 static/timeline.js 画（横向、可拖、可筛）。卡片上那一行是账号，替换的再带上换上来的那个。

    accounts 是「按账号筛选」下拉里的账号：名下现在的全列上，和「名下账号」表格一个顺序——一件事都没有的也在
    （替换上来的只在「替换账号」里当换上的那个，台账里直接填的客户连「分配账号」都没有）；时间线上出现过、
    已经不在名下的（解绑了的）排在后面，group 是 gone。
    """
    items = []
    for item in view.timeline:
        entry = customers.item_payload(item)
        entry.update(
            who=customers.account_face(item.account, by_number),
            peer=customers.account_face(item.peer, by_number),
            none="整个客户" if not item.account and item.kind != "signup" else "",
            keys=[number for number in (item.account, item.peer) if number],
        )
        items.append(entry)
    current = list(dict.fromkeys(holding.number for holding in view.listed))
    mentioned = dict.fromkeys(number for item in view.timeline for number in (item.account, item.peer) if number)
    here = set(current)
    gone = [number for number in mentioned if number not in here]
    accounts = ([{**customers.account_face(number, by_number), "group": "on"} for number in current]
                + [{**customers.account_face(number, by_number), "group": "gone"} for number in gone])
    return {"items": items, "today": view.today.isoformat(), "accounts": accounts}


def _timeline_fallback(view: customers.CustomerView, by_number: dict[str, Account]) -> list[tuple]:
    """没开 JS 时的清单：最近的在上面，[(日期, 标题, 账号 · 一句话)]。"""
    out = []
    for item in reversed(view.timeline):
        face = customers.account_face(item.account, by_number)
        out.append((item.date, item.title, " · ".join(bit for bit in (face["label"] if face else "", item.text) if bit)))
    return out


# ---------------------------------------------------------------- 动作
def _back(cid: str):
    return redirect(url_for("customers.detail", cid=cid))


def _holding(cid: str, key: str):
    """(CustomerView, 这个账号在客户名下的 Holding)。用来取「当时用了多少」和阶段。"""
    customer, view, _, fatal = _detail_context(cid)
    if fatal or view is None:
        return None, None
    return view, next((h for h in view.holdings if h.key == key), None)


@bp.route("/<cid>/update", methods=["POST"])
@login_required
@csrf_protect
def update(cid: str):
    _, all_customers, _, fatal = _ledger()
    customer = next((c for c in all_customers if c.id == cid), None)
    if fatal or customer is None:
        flash(fatal or "没有这个客户，可能已经被删了。", "error")
        return redirect(url_for("customers.index"))
    others = [c for c in all_customers if c.id != cid]
    data, errors = customers.validate_customer(request.form, others)
    if errors:
        return detail(cid, form=request.form.to_dict(), errors=errors, open_edit=True), 400
    try:
        note = excel_source.update_customer(cid, data, actor=_actor())
    except ExcelSourceError as exc:
        return detail(cid, form=request.form.to_dict(), errors=[str(exc)], open_edit=True), 409
    if not note:
        flash_result("没有改动", customer.name, "填的和原来一样。", tone="info")
    else:
        flash_result("已保存", data["name"])
    return _back(cid)


@bp.route("/<cid>/assign", methods=["POST"])
@login_required
@csrf_protect
def assign(cid: str):
    keys = [key for key in request.form.getlist("account") if key]
    today = date.today()
    accounts, _, _, _ = _ledger()
    picked = [a for a in accounts if a.key in keys]
    try:
        excel_source.assign_accounts(cid, keys, _when(request.form.get("date"), today), actor=_actor())
    except ExcelSourceError as exc:
        flash_result("账号没有分配", text=str(exc), tone="error")
        return _back(cid)
    total = sum(a.budget for a in picked)
    who = _who(picked[0]) if len(picked) == 1 else ""
    flash_result(f"已分配 {len(picked)} 个账号" if len(picked) > 1 else "已分配账号", who,
                 f"额度一共 {money0(total)}，算进预算和余额。")
    return _back(cid)


@bp.route("/<cid>/replace", methods=["POST"])
@login_required
@csrf_protect
def replace(cid: str):
    old_key = request.form.get("old") or ""
    new_key = request.form.get("new") or ""
    reason = request.form.get("reason") or "其他"
    if reason not in REPLACE_REASONS:
        reason = "其他"
    today = date.today()
    view, old = _holding(cid, old_key)
    accounts, _, _, _ = _ledger()
    new = next((a for a in accounts if a.key == new_key), None)
    if old is None or new is None:
        flash_result("账号没有替换", text="要换下的账号或者新账号不在了，刷新页面再试。", tone="error")
        return _back(cid)
    try:
        excel_source.replace_account(cid, old_key, new_key, reason, _when(request.form.get("date"), today),
                                     actor=_actor(), spent=old.spent if old.has_numbers else None,
                                     risky=reason in RISKY_REASONS)
    except ExcelSourceError as exc:
        flash_result("账号没有替换", text=str(exc), tone="error")
        return _back(cid)
    flash_result("已替换账号", f"{_who(old.account)} → {_who(new)}",
                 f"{_who(old.account)} 进入待结算，核对完账单再点「结算」。")
    return _back(cid)


@bp.route("/<cid>/risk", methods=["POST"])
@login_required
@csrf_protect
def risk(cid: str):
    key = request.form.get("key") or ""
    clear = request.form.get("action") == "clear"
    today = date.today()
    view, holding = _holding(cid, key)
    if holding is None:
        flash_result("没有改动", text="这个账号已经不是这个客户的了，刷新页面再试。", tone="error")
        return _back(cid)
    if not clear:
        return _mark_risk(cid, holding, _when(request.form.get("date"), today), request.form.get("note"))
    try:
        note = excel_source.clear_risk(cid, key, today, actor=_actor())
    except ExcelSourceError as exc:
        flash_result("没有改动", _who(holding.account), str(exc), tone="error")
        return _back(cid)
    if not note:
        flash_result("没有改动", _who(holding.account), "本来就是这样。", tone="info")
    else:
        flash_result("已取消风控", _who(holding.account), "回到使用中，额度重新算进余额。")
    return _back(cid)


def _mark_risk(cid: str, holding, when: date, note: str | None):
    """标记风控（名下账号的「更多」、记一笔选「标记风控」都走这里）：生命周期换成风控，从这天起不算进余额。"""
    try:
        changed = excel_source.mark_risk(cid, holding.key, when, actor=_actor(),
                                         spent=holding.spent if holding.has_numbers else None,
                                         note=(note or "").strip()[:200])
    except ExcelSourceError as exc:
        flash_result("没有改动", _who(holding.account), str(exc), tone="error")
        return _back(cid)
    if not changed:
        flash_result("没有改动", _who(holding.account), "这个账号本来就是风控。", tone="info")
    else:
        since = "今天" if when == date.today() else when.isoformat()
        flash_result("已标记风控", _who(holding.account), f"从{since}起不算进余额，记得替换账号。")
    return _back(cid)


@bp.route("/<cid>/restore", methods=["POST"])
@login_required
@csrf_protect
def restore(cid: str):
    """恢复使用：风控、被换下的账号回到使用中（AWS 解除了风控、还要接着用）。"""
    key = request.form.get("key") or ""
    today = date.today()
    view, holding = _holding(cid, key)
    if holding is None:
        flash_result("没有恢复", text="这个账号已经不是这个客户的了，刷新页面再试。", tone="error")
        return _back(cid)
    if not holding.restorable:
        flash_result("没有恢复", _who(holding.account),
                     "只有风控、被换下（待结算）的账号能恢复；已经结算的不行，额度用完的先调整额度。", tone="error")
        return _back(cid)
    try:
        changed = excel_source.restore_account(key, _when(request.form.get("date"), today), actor=_actor(),
                                               note=(request.form.get("note") or "").strip()[:200], cid=cid)
    except ExcelSourceError as exc:
        flash_result("没有恢复", _who(holding.account), str(exc), tone="error")
        return _back(cid)
    if not changed:
        flash_result("没有改动", _who(holding.account), "本来就是使用中。", tone="info")
    else:
        flash_result("已恢复使用", _who(holding.account),
                     f"回到使用中，没用完的 {money0(max(0.0, holding.remaining))} 重新算进预算和余额。")
    return _back(cid)


@bp.route("/<cid>/settle", methods=["POST"])
@login_required
@csrf_protect
def settle(cid: str):
    key = request.form.get("key") or ""
    today = date.today()
    view, holding = _holding(cid, key)
    if holding is None:
        flash_result("没有结算", text="这个账号已经不是这个客户的了，刷新页面再试。", tone="error")
        return _back(cid)
    disable = request.form.get("disable") == "1"
    was_using = holding.stage == "use"
    try:
        excel_source.settle_account(cid, key, _when(request.form.get("date"), today), actor=_actor(),
                                    spent=holding.spent if holding.has_numbers else None, disable=disable,
                                    note=(request.form.get("note") or "").strip()[:200])
    except ExcelSourceError as exc:
        flash_result("没有结算", _who(holding.account), str(exc), tone="error")
        return _back(cid)
    if was_using:
        text = "它的额度和消费从今天起不算进预算和余额。"
    else:
        text = f"预算和余额不变：它从 {holding.left.strftime('%m-%d') if holding.left else '之前'} 起就不算了。"
    flash_result("已结算", _who(holding.account), text + ("也停用了。" if disable else ""))
    return _back(cid)


@bp.route("/<cid>/budget", methods=["POST"])
@login_required
@csrf_protect
def budget(cid: str):
    key = request.form.get("key") or ""
    today = date.today()
    view, holding = _holding(cid, key)
    if holding is None:
        flash_result("额度没有改", text="这个账号已经不是这个客户的了，刷新页面再试。", tone="error")
        return _back(cid)
    raw = (request.form.get("add") or "").replace(",", "").replace(config.CURRENCY_SYMBOL, "").strip()
    try:
        add = float(raw)
    except ValueError:
        flash_result("额度没有改", _who(holding.account), "调整多少要填数字，比如 300000，减额度写负数。", tone="error")
        return _back(cid)
    if add != add or add in (float("inf"), float("-inf")) or add == 0:
        flash_result("额度没有改", _who(holding.account), "调整多少要填一个不是 0 的数。", tone="error")
        return _back(cid)
    before = holding.account.budget
    after = before + add
    if after < 0:
        flash_result("额度没有改", _who(holding.account), "减完额度变成负数了。", tone="error")
        return _back(cid)
    # 因为额度用完才进的待结算：加完又够用了，回到使用中
    revive = (holding.stage == "pending" and holding.left_why == "用完" and excel_source.TAG_RISK not in
              holding.account.lifecycle and not any(e.type == "replace" and e.account == holding.number
                                                     and not e.deleted for e in view.events)
              and holding.spent < after)
    try:
        excel_source.set_budget(cid, key, after, _when(request.form.get("date"), today), actor=_actor(),
                                note=(request.form.get("note") or "").strip()[:200], revive=revive)
    except ExcelSourceError as exc:
        flash_result("额度没有改", _who(holding.account), str(exc), tone="error")
        return _back(cid)
    text = f"{money0(before)} → {money0(after)}。" + ("够用了，回到使用中。" if revive else "")
    flash_result("已调整额度", _who(holding.account), text)
    return _back(cid)


@bp.route("/<cid>/unassign", methods=["POST"])
@login_required
@csrf_protect
def unassign(cid: str):
    key = request.form.get("key") or ""
    view, holding = _holding(cid, key)
    if holding is None:
        flash_result("没有解绑", text="这个账号已经不是这个客户的了，刷新页面再试。", tone="error")
        return _back(cid)
    try:
        excel_source.unassign_account(cid, key, date.today(), actor=_actor(),
                                      note=(request.form.get("note") or "").strip()[:200])
    except ExcelSourceError as exc:
        flash_result("没有解绑", _who(holding.account), str(exc), tone="error")
        return _back(cid)
    flash_result("已解绑", _who(holding.account), "回到库存了，它的消费不再算进这个客户。")
    return _back(cid)


@bp.route("/<cid>/events", methods=["POST"])
@login_required
@csrf_protect
def add_event(cid: str):
    kind = request.form.get("kind") or "note"
    if kind not in customers.NOTE_KINDS:
        kind = "note"
    note = (request.form.get("note") or "").strip()
    number = (request.form.get("account") or "").strip()
    today = date.today()
    if kind == "note" and not note:
        flash_result("没有记下来", text="备注要写点什么。", tone="error")
        return _back(cid)
    if len(note) > 200:
        flash_result("没有记下来", text="说明最多 200 个字。", tone="error")
        return _back(cid)
    if kind == "risk":
        # 和名下账号里的「标记风控」一样：要选账号，生命周期跟着改
        if not number:
            flash_result("没有记下来", text="标记风控要选是哪个账号。", tone="error")
            return _back(cid)
        _, view, _, fatal = _detail_context(cid)
        holding = next((h for h in view.holdings if h.number == number), None) if view else None
        if holding is None:
            flash_result("没有记下来", text="这个账号不在这个客户名下，刷新页面再选。", tone="error")
            return _back(cid)
        return _mark_risk(cid, holding, _when(request.form.get("date"), today), note)
    accounts, _, _, _ = _ledger()
    if number and not any(a.account == number and a.customer == cid for a in accounts):
        flash_result("没有记下来", text="这个账号不在这个客户名下，刷新页面再选。", tone="error")
        return _back(cid)
    try:
        excel_source.add_customer_event(cid, kind, _when(request.form.get("date"), today), actor=_actor(),
                                        account=number, note=note)
    except ExcelSourceError as exc:
        flash_result("没有记下来", text=str(exc), tone="error")
        return _back(cid)
    flash_result("已记下来", customers.EVENT_TYPES[kind][0], note)
    return _back(cid)


@bp.route("/<cid>/events/change", methods=["POST"])
@login_required
@csrf_protect
def change_event(cid: str):
    ident = (request.form.get("id") or "").strip()
    delete = request.form.get("action") == "delete"
    today = date.today()
    when = None if delete else parse_date(request.form.get("date"))
    if not delete and (when is None or when > today):
        flash_result("日期没有改", text="日期认不出来，或者晚于今天。", tone="error")
        return _back(cid)
    extra = {}
    if ident.startswith("auto:"):
        customer, view, _, fatal = _detail_context(cid)
        item = next((item for item in view.timeline if item.ident == ident), None) if view else None
        if item is None:
            flash_result("没有改动", text="时间线上已经没有这条了，刷新页面再试。", tone="error")
            return _back(cid)
        extra = {"auto_kind": item.kind, "auto_account": item.account, "auto_date": item.date, "auto_title": item.title}
    try:
        note = excel_source.change_customer_event(cid, ident, actor=_actor(), when=when, delete=delete, **extra)
    except ExcelSourceError as exc:
        flash_result("没有改动", text=str(exc), tone="error")
        return _back(cid)
    if not note:
        flash_result("没有改动", text="本来就是这样。", tone="info")
    elif delete:
        flash_result("已删掉", text="自动记的删掉以后不会再自动生成。" if ident.startswith("auto:") else "")
    else:
        flash_result("已改日期", text=f"改到 {when.isoformat()}。")
    return _back(cid)


@bp.route("/<cid>/statement.csv")
@login_required
def statement_csv(cid: str):
    """月度对账单：每个账号每个月一行，折算后和 AWS 原价都有。Excel 直接打开（带 BOM）。"""
    customer, view, _, fatal = _detail_context(cid)
    if fatal or customer is None:
        flash(fatal or "没有这个客户。", "error")
        return redirect(url_for("customers.index"))
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(["客户", "月份", "账号邮箱", "账号 ID", "上游", "阶段", "折算后消费", "AWS 原价"])
    for holding, months in view.statement_matrix():
        for month in view.months:
            marked, raw = months.get(month, (0.0, 0.0))
            if not marked and not raw:
                continue
            writer.writerow([customer.name, month, holding.account.email, holding.number, holding.account.partner,
                             holding.stage_label, f"{marked:.2f}", f"{raw:.2f}"])
    body = "﻿" + out.getvalue()
    filename = f"{customer.id}-statement-{view.today.isoformat()}.csv"
    return Response(body, mimetype="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})
