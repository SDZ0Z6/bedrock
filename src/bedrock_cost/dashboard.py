"""概览、账号页、运营看板共用的拼装：把台账、Cost Explorer、CloudWatch 的结果整理成
模板要的样子。这里只有拼装和小计算，不碰 Flask，方便单独测。
"""

from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from types import SimpleNamespace

from . import chart, config, cost_history, usage_explorer
from .dates import earliest_queryable, month_shift
from .excel_source import Account, LifecycleTag
from .filters import money

# ---------------------------------------------------------------- 右上角的报错弹窗
@dataclass
class Toast:
    tone: str          # error / warn / ok / info
    title: str
    sub: str = ""
    text: str = ""
    detail: str = ""


def _parts(error) -> tuple[str, str, str, str]:
    """一条查询错误拆成 (账号号码, 区域, 一句话原因, 原始报错)。

    数据层的错误是 aws_errors.QueryError（有这几个字段）；老一点的路径上可能还是
    「Jeff / 123456789012 @ us-east-1：原因」这样的字符串，按同一个格式拆。
    """
    if hasattr(error, "reason"):
        return (getattr(error, "account", "") or "", getattr(error, "region", "") or "",
                error.reason, getattr(error, "detail", "") or error.reason)
    text = str(error)
    where, _, reason = text.partition("：")
    if not reason:
        return "", "", text, text
    account = ""
    for piece in where.replace("/", " ").replace("@", " ").split():
        if piece.isdigit() and len(piece) == 12:
            account = piece
    region = where.rpartition("@")[2].strip() if "@" in where else ""
    return account, region, reason.strip(), text


def who(account) -> str:
    """提示里说是哪个账号：邮箱在前、号码在后；没填邮箱就只写号码。"""
    return f"{account.email} · {account.account}" if account.email else account.account


def region_toasts(errors, account: Account | None, what: str = "查询失败") -> list[Toast]:
    """一个账号几个区的报错：原因相同的合成一条（「3 个区查询失败」），原始报错按区列在详情里。"""
    groups: dict[str, list[tuple[str, str]]] = {}
    for error in errors:
        _, region, reason, detail = _parts(error)
        groups.setdefault(reason, []).append((region, detail))
    sub = who(account) if account else ""
    toasts = []
    for reason, items in groups.items():
        regions = [region for region, _ in items if region]
        if len(regions) > 1:
            title = f"{len(regions)} 个区{what}"
        elif regions:
            title = f"{regions[0]} {what}"
        else:
            title = what
        detail = "\n".join(f"{region}：{text}" if region else text for region, text in items)
        toasts.append(Toast("error", title, sub, reason, detail))
    return toasts


def account_toasts(errors, accounts: list[Account], what: str) -> list[Toast]:
    """好几个账号的报错（概览、看板）：原因相同的账号合成一条，「2 个账号查不到 Cost Explorer」。"""
    by_id = {a.account: a for a in accounts}
    groups: dict[str, list[tuple[str, str]]] = {}
    for error in errors:
        number, _, reason, detail = _parts(error)
        groups.setdefault(reason, []).append((number, detail))
    toasts = []
    for reason, items in groups.items():
        numbers = list(dict.fromkeys(number for number, _ in items if number))
        if len(numbers) == 1 and numbers[0] in by_id:
            acct = by_id[numbers[0]]
            title, sub = what, who(acct)
        else:
            title, sub = (f"{len(numbers)} 个账号{what}" if numbers else what), ""
        lines = []
        for number, text in items:
            acct = by_id.get(number)
            lines.append(f"{who(acct)}：{text}" if acct else text)
        toasts.append(Toast("error", title, sub, reason, "\n".join(lines)))
    return toasts


# ---------------------------------------------------------------- 概览的卡片
class CardRow:
    """概览一张卡片要的东西：ReportRow 的金额和状态，加上台账里的邮箱、头像、生命周期，
    再加上用量状态和近 14 天的迷你柱图。没定义的属性都转给 ReportRow。"""

    def __init__(self, row, account: Account, activity, order: int, today: date):
        self.row = row
        self.email = account.email
        self.avatar = account.avatar
        self.lifecycle = account.lifecycle
        self.customer = account.customer
        self.label = account.label
        self.key = account.key
        self.order = order
        self.activity = activity
        self.days_active = (today - account.start_date).days + 1 if account.start_date else 0
        accent = {"active": chart.TONE_COLORS["ok"], "stopped": chart.TONE_COLORS["warn"]}.get(
            activity.kind, chart.TONE_COLORS["none"])
        self.spark = chart.render_spark(activity.daily, accent=accent) if activity.daily else ""

    @property
    def state(self) -> str:
        return card_state(self)

    @property
    def state_label(self) -> str:
        return STATES[self.state]

    @property
    def issue(self) -> str:
        """卡片上那行红字：消费查询为什么失败，一句话。"""
        if not self.row.error:
            return ""
        return _parts(self.row.problem or self.row.error)[2]

    @property
    def error_detail(self) -> str:
        """「查看详情」里的原始报错：有结构化原因就用 AWS 原话，没有就是那一行字。"""
        if not self.row.error:
            return ""
        return _parts(self.row.problem or self.row.error)[3]

    def __getattr__(self, name):
        return getattr(self.row, name)


# 卡片上的状态，也是概览「状态」那排筛选签。CloudWatch 读不到（activity 是 unknown）和
# Cost Explorer 查询失败都算异常：两种都是账号本身在报错，不是「不知道有没有在用」
STATES = {"active": "活跃", "stopped": "已中断", "idle": "无调用", "error": "异常"}


def card_state(row) -> str:
    if row.error or row.activity.kind == "unknown":
        return "error"
    return row.activity.kind


def risk_rank(card: CardRow) -> int:
    """卡片的默认顺序，有问题的排前面：异常 → 用量中断 → 其余。"""
    state = card_state(card)
    if state == "error":
        return 0
    return 1 if state == "stopped" else 2


def lifecycle_counts(rows, tags: list[LifecycleTag]) -> list[SimpleNamespace]:
    """筛选签上每个生命周期有几个账号。清单外的老标签也列出来，按灰色画。"""
    known = {tag.name: tag.hex for tag in tags}
    seen = list(known)
    for row in rows:
        for name in row.lifecycle:
            if name not in known and name not in seen:
                seen.append(name)
    out = []
    for name in seen:
        count = sum(1 for row in rows if name in row.lifecycle)
        if count or name in known:
            out.append(SimpleNamespace(name=name, color=known.get(name, chart.TONE_COLORS["none"]), count=count))
    return out


def state_counts(rows) -> list[SimpleNamespace]:
    """筛选签上每个状态有几个账号。一个账号只落在一个状态里，几个签的数加起来就是全部。"""
    out = []
    for key, label in STATES.items():
        count = sum(1 for row in rows if card_state(row) == key)
        if count:
            out.append(SimpleNamespace(key=key, label=label, count=count))
    return out


def _by_usage(row) -> float:
    """档里的先后：使用率高的在前，查不到消费的放最后。"""
    return -(row.usage_pct or 0.0) if row.has_numbers else 1.0


def lifecycle_rows(rows, tags: list[LifecycleTag]) -> list[SimpleNamespace]:
    """概览「额度使用」：按生命周期一档一根横条（风控、正常……，和客户页「账号」那张一个样子），后面是这一档的账号头像。
    和下面「生命周期」那排筛选签一个算法：打了几个标签就在几档里（风控又结算的两档都有），清单里没人用的也列出来
    （灰的），没打标签的有才列「未标记」。档里使用率高的在前。"""
    out = [SimpleNamespace(name=item.name, color=item.color, count=item.count,
                           rows=sorted((row for row in rows if item.name in row.lifecycle), key=_by_usage))
           for item in lifecycle_counts(rows, tags)]
    bare = sorted((row for row in rows if not row.lifecycle), key=_by_usage)
    if bare:
        out.append(SimpleNamespace(name="未标记", color=chart.TONE_COLORS["none"], count=len(bare), rows=bare))
    return out


# ---------------------------------------------------------------- 每月账号和消费
MONTHS_BACK = 12       # 概览「每月账号和消费」看近几个月（含这个月，到今天）
MONTHS_MIN = 6         # 前面几个月什么都没有就不画，但至少留这么多个月
_NUMBER = re.compile(r"(?<!\d)\d{12}(?!\d)")


def _disabled_in_log(numbers: set[str]) -> dict[str, date]:
    """操作日志里最近一回「停用账号 …（号码）」是哪天。EVENTS 表 2026-10-10 起才记停用，之前停的只有日志里这一行；
    日志里也没有的（手改 Excel 停的、日志翻过去了）不在里面。"""
    from .settings_pages import audit_lines      # 设置页读操作日志的那一段；在这里 import，免得两个模块互相引用

    found: dict[str, date] = {}
    for line in audit_lines():                     # 新的在前
        if not line.note.startswith("停用账号"):
            continue
        try:
            day = date.fromisoformat(line.when[:10])
        except ValueError:
            continue
        for number in _NUMBER.findall(line.note):
            if number in numbers and number not in found:
                found[number] = day
    return found


def account_months(rows, accounts: list[Account], events, today: date, months: int = MONTHS_BACK) -> SimpleNamespace:
    """概览「每月账号和消费」：近 months 个月（含这个月，到今天），每个月的账号分三段——
      · 新加：启用日期在这个月。台账里没有「哪天加进来」，启用日期就是各页面说的账号的开始；
      · 停用：这个月停的。EVENTS 表里记的「停用」（账号管理里停用、结算时一起停用，2026-10-10 起都记）；之前停的没记，
        去操作日志里找最近一回「停用账号」；都找不到的（手改 Excel 停的）不知道哪天，不算。停了又恢复的，停的那个月算停用；
      · 正常：这个月之前就在用、这个月也一直开着的（没停，或者是后来才停的）。
    一个账号一个月只算一段：同一个月里新加又停了的算停用，一个月里停了两回算一个。同一个号码在台账里有几行的按一个算
    （启用日期取最早的）。再加上每个月的消费，折算后（和概览一个口径）：在用的账号用累计消费那次查询按月留下的
    （rows 是概览的卡片，ReportRow.monthly），停用的账号用客户页存下来的每天的账（cost_history）加起来——都不多查 AWS；
    停用的账号没存过账的不算。
    accounts 是台账里全部的账号（含停用的），events 是 EVENTS 表（load_events）。前面几个月什么都没有就不画
    （至少留 MONTHS_MIN 个月）；没算进去的写成几句 notes，放在图下面。"""
    first = month_shift(today, -(months - 1))
    spans = [(month_shift(first, i), min(month_shift(first, i + 1) - timedelta(days=1), today)) for i in range(months)]
    keys = [begin.strftime("%Y-%m") for begin, _ in spans]
    index = {key: i for i, key in enumerate(keys)}

    # 一个号码一份：启用日期取最早的，有一行开着就算开着
    starts: dict[str, date | None] = {}
    on: set[str] = set()
    for account in accounts:
        known = starts.get(account.account)
        if account.account not in starts or (account.start_date and (known is None or account.start_date < known)):
            starts[account.account] = account.start_date
        if account.enabled:
            on.add(account.account)
    off = set(starts) - on

    # 哪天停的：EVENTS 里的每一回；现在停着、EVENTS 里又没有的，看操作日志
    stops: dict[str, list[date]] = {}
    for event in events:
        if event.deleted or event.type != "disable" or event.source != "manual" or event.account not in starts:
            continue
        stops.setdefault(event.account, []).append(event.date)
    silent = off - set(stops)
    from_log = _disabled_in_log(silent) if silent else {}
    for number, day in from_log.items():
        stops[number] = [day]
    ended = {number: max(stops[number]) for number in off if number in stops}   # 现在停着的：最后一回停是哪天

    added, normal, stopped = [0] * months, [0] * months, [0] * months
    for i, (begin, end) in enumerate(spans):
        for number, start in starts.items():
            if any(begin <= day <= end for day in stops.get(number, ())):
                stopped[i] += 1
            elif start is None or start > end:
                continue
            elif start >= begin:
                added[i] += 1
            elif number in on or ended.get(number, date.min) > end:
                normal[i] += 1

    spend = [0.0] * months
    for row in rows:
        for key, value in (row.monthly or {}).items():
            if key in index:
                spend[index[key]] += value
    saved = cost_history.recall_many(sorted(off))
    for history in saved.values():
        for offset, value in enumerate(history.marked):
            key = (history.start + timedelta(days=offset)).strftime("%Y-%m")
            if key in index:
                spend[index[key]] += value

    filled = [i for i in range(months) if added[i] or normal[i] or stopped[i] or spend[i] > 0]
    begin_at = max(0, min(filled[0] if filled else months, months - MONTHS_MIN))
    shown = range(begin_at, months)
    labels, years, tips = [], [], []
    for i in shown:
        month = spans[i][0]
        labels.append(f"{month.month}月")
        years.append(str(month.year) if i == begin_at or month.month == 1 else "")
        tips.append(f"{month.year} 年 {month.month} 月" + ("（到今天）" if i == months - 1 else ""))
    notes = []
    unknown = len(silent - set(from_log))
    if unknown:
        notes.append(f"{unknown} 个停用的账号不知道哪天停的，没算进去")
    if off - set(saved):
        notes.append(f"{len(off - set(saved))} 个停用的账号没存过每天的账，消费里没有它们")
    failed = sum(1 for row in rows if row.error)
    if failed:
        notes.append(f"{failed} 个账号这次没查到 Cost Explorer，消费里少了它们")
    no_start = sum(1 for start in starts.values() if start is None)
    if no_start:
        notes.append(f"{no_start} 个账号没填启用日期，没算进新加、正常")
    return SimpleNamespace(
        keys=[keys[i] for i in shown], labels=labels, years=years, tips=tips,
        added=[added[i] for i in shown], normal=[normal[i] for i in shown], disabled=[stopped[i] for i in shown],
        spend=[spend[i] for i in shown],
        total_added=sum(added[i] for i in shown), total_disabled=sum(stopped[i] for i in shown),
        total_spend=sum(spend[i] for i in shown), in_use=normal[-1] + added[-1], notes=notes,
    )


# ---------------------------------------------------------------- 按账号的颜色
def account_slots(rows) -> dict[str, int]:
    """消费最多的 8 个账号各占一个色槽，其余并进「其他」（灰）。概览的「每日消费」和
    「消费构成」用同一份，同一个账号在两张图里是同一个颜色。"""
    ranked = sorted((row for row in rows if row.has_numbers and row.total_cost > 0), key=lambda r: -r.total_cost)
    top = [row.label for row in ranked[: usage_explorer.MAX_SERIES]]
    return usage_explorer.assign_slots(top)


def spend_share(rows, slots: dict[str, int]) -> SimpleNamespace:
    """消费构成：各账号的累计消费（折算后），8 个以外并进「其他」。"""
    items: list[tuple[str, float, str]] = []
    other = 0.0
    for row in sorted(rows, key=lambda r: -r.total_cost if r.has_numbers else 0):
        if not row.has_numbers or row.total_cost <= 0:
            continue
        if row.label in slots:
            items.append((row.label, row.total_cost, chart.color_for(slots[row.label])))
        else:
            other += row.total_cost
    if other > 0:
        items.append((usage_explorer.OTHER_LABEL, other, chart.color_for(-1)))
    return SimpleNamespace(
        svg=chart.render_bubbles(items, width=250, height=210, fmt=money),
        legend=[SimpleNamespace(name=n, value=v, color=c) for n, v, c in items],
    )


# ---------------------------------------------------------------- 每日消费看多久
# 概览、客户列表、客户页、账号页摘要的「每日消费」右上角都是这一排，网址上是 ?spend=
SPEND_RANGES = (("7", "近 7 天"), ("30", "近 30 天"), ("90", "近 90 天"), ("all", "全部"))


def spend_range(picked: str | None, today: date, first: date | None, default: str = "30") -> SimpleNamespace:
    """「每日消费」看多久：key（7 / 30 / 90 / all，认不出是 default：客户页是 7，别的页面是 30）、days、start、
    label（写在大数字旁边）。「全部」从 first（最早启用、分过来的那天）起，不早于 Cost Explorer 能查的最早一天；
    没有 first 就和近 30 天一样长。"""
    key = picked if picked in dict(SPEND_RANGES) else default
    if key == "all":
        start = min(max(first or today - timedelta(days=29), earliest_queryable(today)), today)
        return SimpleNamespace(key=key, days=(today - start).days + 1, start=start,
                               label=start.strftime("%m-%d") + " 起全部")
    days = int(key)
    return SimpleNamespace(key=key, days=days, start=today - timedelta(days=days - 1), label=dict(SPEND_RANGES)[key])


# ---------------------------------------------------------------- 每日消费（近 N 天，按账号）
# Cost Explorer 每次请求收 0.01 美元，一天也只更新几次，所以这张图单独缓存 2 小时
TREND_TTL = 2 * 3600
_trend_lock = threading.Lock()
_trend_cache: dict[tuple, tuple[float, usage_explorer.UsageReport]] = {}


def clear_cache() -> None:
    with _trend_lock:
        _trend_cache.clear()


def daily_cost(accounts: list[Account], start: date, end: date, refresh: bool = False) -> usage_explorer.UsageReport:
    """每个账号每天的成本（按台账比率折算后）。"""
    key = (tuple(a.key for a in accounts), start, end)
    if not refresh:
        with _trend_lock:
            hit = _trend_cache.get(key)
        if hit and time.time() - hit[0] < TREND_TTL:
            return hit[1]
    report = usage_explorer.build_usage(accounts, start, end, "account", "daily", refresh=refresh)
    if not report.errors:
        with _trend_lock:
            _trend_cache[key] = (time.time(), report)
    return report


def money_axis(value: float) -> str:
    """柱状图纵轴上的金额：紧凑写法，小数尾巴上的 0 不写（$750、$1.5K、$100K、$2.5M），和客户页的一样。
    「$100.0K」七个字在纵轴那一栏里放不下，头一个字会被切掉。图例只挑小的几项时纵轴会缩到几块钱以下，
    所以一块钱以内照写小数（$0.25），不取整成 $0。"""
    return chart.short_money(value, config.CURRENCY_SYMBOL)


def cost_trend(accounts: list[Account], today: date, slots: dict[str, int], days: int = 30,
               refresh: bool = False, width: int = 560) -> SimpleNamespace:
    """概览的「每日消费」（近 30 天）：每天一根柱，按账号堆叠，账号用邮箱前缀显示。width 是图的宽（整行宽的卡片画宽一点）。"""
    start = today - timedelta(days=days - 1)
    report = daily_cost(accounts, start, today, refresh)
    label_of = {f"{a.partner} / {a.account}": a.label for a in accounts}
    by_label: dict[str, list[float]] = {}
    for series in report.series:
        name = label_of.get(series.name, series.name)
        by_label.setdefault(name, [0.0] * len(report.dates))
        by_label[name] = [x + y for x, y in zip(by_label[name], series.marked)]
    series_list = []
    other = [0.0] * len(report.dates)
    for name, values in by_label.items():
        if name in slots:
            series_list.append(SimpleNamespace(name=name, slot=slots[name], values=values))
        else:
            other = [x + y for x, y in zip(other, values)]
    if any(other):
        series_list.append(SimpleNamespace(name=usage_explorer.OTHER_LABEL, slot=-1, values=other))
    bars = chart.render_bars(report.labels, series_list, width=width, height=230, fmt=money, axis=money_axis)
    totals = [sum(s.values[i] for s in series_list) for i in range(len(report.labels))]
    peak = max(range(len(totals)), key=totals.__getitem__) if totals else -1
    return SimpleNamespace(
        days=days, start=report.labels[0] if report.labels else "", end=report.labels[-1] if report.labels else "",
        total=sum(totals), average=sum(totals) / days if days else 0.0,
        peak_label=report.labels[peak] if peak >= 0 and totals[peak] > 0 else "",
        peak_value=totals[peak] if peak >= 0 else 0.0,
        chart=bars, errors=report.errors, any_cached=report.any_cached,
    )


def single_cost_trend(account: Account, today: date, days: int = 30, refresh: bool = False,
                      marks: list[chart.BarMark] | None = None) -> SimpleNamespace:
    """账号页摘要的「每日消费」（近 30 天）：一个账号，单色柱子（陶土色）；marks 是竖虚线（启用、风控……）。"""
    start = today - timedelta(days=days - 1)
    report = daily_cost([account], start, today, refresh)
    values = report.column_totals
    series = [SimpleNamespace(name="成本", slot=0, color="#d97757", values=values)]
    bars = chart.render_bars(report.labels, series, height=210, fmt=money, axis=money_axis, marks=marks)
    peak = max(range(len(values)), key=values.__getitem__) if values else -1
    return SimpleNamespace(
        days=days, start=report.labels[0] if report.labels else "", end=report.labels[-1] if report.labels else "",
        total=sum(values), average=sum(values) / days if days else 0.0,
        peak_label=report.labels[peak] if peak >= 0 and values[peak] > 0 else "",
        peak_value=values[peak] if peak >= 0 else 0.0,
        chart=bars, errors=report.errors,
    )


# ---------------------------------------------------------------- 杂项
def days_between(start: date | None, today: date) -> int:
    return (today - start).days + 1 if start else 0


def local_now() -> datetime:
    return datetime.now().astimezone()
