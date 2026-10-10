"""客户页：客户列表、客户详情，以及详情页上的几个动作（新建 / 修改客户、分配、替换、标记风控、结算、
调整额度、解绑、记一笔、改 / 删时间线上的事）。

算钱、时间线、对账单都在 customers 里；写台账在 excel_source 里。这里只管表单进出和提示文案。
所有动作都是普通的表单提交（POST 之后跳回详情页），没开 JS 也能用。
"""

from __future__ import annotations

import csv
import io
from datetime import date, timedelta
from types import SimpleNamespace

from flask import Blueprint, Response, flash, redirect, render_template, request, session, url_for

from . import chart, config, customers, dashboard, excel_source, usage_explorer
from .auth import csrf_protect, login_required
from .dates import parse_date
from .excel_source import Account, ExcelSourceError
from .filters import money, money0
from .views import flash_result, page_meta

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


def _spend_chart(series: dict[str, list[float]], stamps: list[date], names: dict[str, str], height: int = 230,
                 width: int = 560):
    """近 30 天每天的消费：一根柱一天，按客户（或账号）堆叠。8 个以外并进「其他」。"""
    ranked = sorted((key for key in series if sum(series[key]) > 0), key=lambda key: -sum(series[key]))
    keep = ranked[: usage_explorer.MAX_SERIES]
    slots = _slots([names[key] for key in keep])
    rows = [SimpleNamespace(name=names[key], slot=slots[names[key]], values=series[key]) for key in keep]
    other = [sum(series[key][i] for key in ranked[usage_explorer.MAX_SERIES:]) for i in range(len(stamps))]
    if any(other):
        rows.append(SimpleNamespace(name=usage_explorer.OTHER_LABEL, slot=-1, values=other))
    labels = [stamp.strftime("%m-%d") for stamp in stamps]
    bars = chart.render_bars(labels, rows, width=width, height=height, fmt=money, axis=customers.short_money)
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
        stamps = [today - timedelta(days=29 - i) for i in range(30)]
        per_customer = {}
        for view in cards:
            columns = [h.daily(stamps[0], today) for h in view.holdings]
            per_customer[view.customer.id] = [sum(day) for day in zip(*columns)] if columns else [0.0] * 30
        names = {view.customer.id: view.customer.name for view in cards}
        trend = _spend_chart(per_customer, stamps, names)
        soon = sorted((view for view in cards if view.days_left is not None and view.balance > 0
                       and view.customer.status != "gone"), key=lambda view: view.days_left)[:4]
        regions: dict[str, float] = {}
        for view in cards:
            code = view.customer.region or "OTHER"
            regions[code] = regions.get(code, 0.0) + sum(per_customer[view.customer.id])
        by_region = sorted(((code, value) for code, value in regions.items() if value > 0), key=lambda kv: -kv[1])
        context = dict(trend=trend, soon=soon, by_region=by_region,
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


# ---------------------------------------------------------------- 客户详情
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

    # 「账号」环形图：正常、异常（风控 + 待结算，两段都是红色系）、已结算
    donut = chart.render_donut([
        ("正常", counts["use"], chart.TONE_COLORS["ok"]),
        ("风控 · 待替换", counts["risk"], chart.TONE_COLORS["danger"]),
        ("待结算", counts["pending"], "#de9a92"),
        ("已结算", counts["settled"], "#c2c0b6"),
    ])
    pct = view.usage_pct
    gauge = chart.render_gauge(None if pct is None else pct / 100, view.level)
    stamps, per_account = view.daily(30)
    names = {h.number: h.account.label for h in view.holdings}
    trend = _spend_chart(per_account, stamps, names, height=230, width=760)
    runway = chart.render_runway(view.balance_history(21), view.burn, fmt=money, axis=customers.short_money)

    by_number = {a.account: a for a in accounts}
    timeline = _timeline_payload(view, by_number)
    stock = customers.stock(accounts)
    # 表格里每个账号的动作（结算、调整额度、标记风控、解绑）打开弹窗时要填的数，交给页面脚本
    holdings_data = {
        "balance": view.balance,
        "name": customer.name,
        "holdings": {
            h.key: {
                "email": h.account.email or h.number, "number": h.number, "partner": h.account.partner,
                "budget": h.account.budget, "spent": h.spent, "raw": h.raw_spent, "remaining": h.remaining,
                "stage": h.stage, "stage_label": h.stage_label, "left": h.left.strftime("%m-%d") if h.left else "",
                "left_why": h.left_why, "has_numbers": h.has_numbers,
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
        donut=donut,
        gauge=gauge,
        trend=trend,
        runway=runway,
        timeline=timeline,
        timeline_fallback=_timeline_fallback(view, by_number),
        categories=customers.CATEGORIES,
        stock=stock,
        by_number=by_number,
        holdings_data=holdings_data,
        currency=config.CURRENCY_SYMBOL,
        reasons=REPLACE_REASONS,
        manual_kinds=[(kind, customers.EVENT_TYPES[kind][0]) for kind in customers.NOTE_KINDS],
        stage_names=customers.STAGE_SHORT,
        stage_rank=customers.TABLE_ORDER,
        today=today,
        toasts=_toasts([view]),
        form=extra.pop("form", None),
        errors=extra.pop("errors", []),
        open_edit=extra.pop("open_edit", False),
        **_shared(),
        **extra,
        **page_meta(),
    )


def _timeline_payload(view: customers.CustomerView, by_number: dict[str, Account]) -> dict:
    """时间线交给 static/timeline.js 画（横向、可拖、可筛）。卡片上那一行是账号，替换的再带上换上来的那个。"""
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
    numbers = list(dict.fromkeys(item.account for item in view.timeline if item.account))
    return {"items": items, "today": view.today.isoformat(),
            "accounts": [customers.account_face(number, by_number) for number in numbers]}


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
        extra = {"auto_kind": item.kind, "auto_account": item.account, "auto_date": item.date}
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
