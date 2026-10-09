"""运营看板的数据：一段时间里全部启用账号的收入、AWS 原价和毛利，额度，需要关注的账号，
最近发出去的告警，按模型、按区域的用量。只做拼装和小计算，不碰 Flask，方便单独测。

口径
  · 收入 = 按台账比率折算后的消费：有标签的乘 TAG 比率、没标签的乘 UNTAG 比率。比率就是
    加价，所以毛利 = 收入 − AWS 原价，毛利率 = 毛利 / 收入。
  · 每个账号都从它的启用日期算起（和概览一致），启用之前的消费不算。
  · 只算启用中的账号。停用的不再查 Cost Explorer（省钱），所以「今年」这种长区间里，已经
    停用的账号停用之前的收入也不在里面——页面页脚写明了。
  · 日子都是 UTC 日期：Cost Explorer 按 UTC 出账，CloudWatch 的按天数据也对齐到 UTC 零点。

一个账号只查一次 Cost Explorer：按天拉最近 12 个月（含本月），本月、上月、近 30 天、今年
这几段，各自的上一段，近 12 个月的走势，全从这一份里切。CE 每次请求收 0.01 美元，按天最多
往回查 14 个月；结果缓存 2 小时，和概览的「近 30 天成本」一样。
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

from . import chart, cloudwatch_metrics, config, events, usage_explorer
from .dashboard import _parts, money_axis
from .dates import earliest_queryable, month_shift
from .excel_source import Account, LifecycleTag
from .filters import money, money0, pct
from .windows import MetricWindow

PERIODS = {
    "mtd": "本月",
    "last_month": "上月",
    "30d": "近 30 天",
    "ytd": "今年",
}
DEFAULT_PERIOD = "mtd"
TREND_MONTHS = 12

# 近 12 个月那张图：底下是 AWS 原价（暖灰），上面一截是毛利（陶土色），整根柱就是收入。
# 两个都过了对白底 3:1（灰 3.7、陶土 3.1）
RAW_COLOR = "#87867f"
MARGIN_COLOR = "#d97757"


# ---------------------------------------------------------------- 时间段
@dataclass(frozen=True)
class Span:
    start: date
    end: date

    @property
    def days(self) -> int:
        return (self.end - self.start).days + 1

    @property
    def label(self) -> str:
        fmt = "%m-%d" if self.start.year == self.end.year else "%Y-%m-%d"
        if self.start == self.end:
            return self.start.strftime(fmt)
        return f"{self.start.strftime(fmt)} ~ {self.end.strftime(fmt)}"

    def __contains__(self, day: date) -> bool:
        return self.start <= day <= self.end


@dataclass(frozen=True)
class Period:
    key: str
    label: str
    span: Span
    previous: Span | None   # 拿来比的上一段；今年没有（CE 按天查不到去年年初）
    compare: str            # 「比上月同期」；没有上一段时是空串


def _month_end(first: date) -> date:
    return month_shift(first, 1) - timedelta(days=1)


def period_for(key: str, today: date) -> Period:
    """时间段快捷项。认不出的 key 当本月。"""
    if key not in PERIODS:
        key = DEFAULT_PERIOD
    label = PERIODS[key]
    if key == "last_month":
        first = month_shift(today, -1)
        before = month_shift(today, -2)
        return Period(key, label, Span(first, _month_end(first)), Span(before, _month_end(before)), "比前一个月")
    if key == "30d":
        start = today - timedelta(days=29)
        previous = Span(start - timedelta(days=30), start - timedelta(days=1))
        return Period(key, label, Span(start, today), previous, "比前 30 天")
    if key == "ytd":
        return Period(key, label, Span(date(today.year, 1, 1), today), None, "")
    # 本月：和上个月的同一段比（上个月没有这么多天就比到月底）
    first = month_shift(today, -1)
    end = min(first + timedelta(days=today.day - 1), _month_end(first))
    return Period(key, label, Span(today.replace(day=1), today), Span(first, end), "比上月同期")


def change(now: float | None, before: float | None) -> float | None:
    """变化百分比。上一段是 0、负数或者没有，就不比（比出来的百分比没有意义）。"""
    if now is None or before is None or before <= 0:
        return None
    return (now - before) / before * 100


# ---------------------------------------------------------------- 每个账号每天的钱
LEDGER_TTL = 2 * 3600
_lock = threading.Lock()
_cache: dict[tuple, tuple[float, tuple[list[float], list[float]]]] = {}


def clear_cache() -> None:
    with _lock:
        _cache.clear()


@dataclass
class Ledger:
    """每个账号每天的原价和折算后，已经按启用日期裁过（启用之前的记 0）。

    查不了的账号不在 raw / marked 里，原因在 errors。"""

    days: list[date]
    raw: dict[str, list[float]] = field(default_factory=dict)      # account.key -> 每天
    marked: dict[str, list[float]] = field(default_factory=dict)
    errors: list = field(default_factory=list)                     # aws_errors.QueryError
    any_cached: bool = False

    def indices(self, span: Span) -> range:
        if not self.days:
            return range(0)
        first = (span.start - self.days[0]).days
        last = (span.end - self.days[0]).days
        return range(max(0, first), min(len(self.days), last + 1))

    def sums(self, key: str, span: Span) -> tuple[float, float]:
        """这个账号在这一段里的 (原价, 折算后)。"""
        raw, marked = self.raw.get(key), self.marked.get(key)
        if raw is None or marked is None:
            return 0.0, 0.0
        picked = self.indices(span)
        return sum(raw[i] for i in picked), sum(marked[i] for i in picked)

    def daily(self, span: Span) -> tuple[list[float], list[float]]:
        """这一段里全部账号逐日加总的 (原价, 折算后)，给指标卡的迷你柱用。"""
        picked = self.indices(span)
        raw = [sum(values[i] for values in self.raw.values()) for i in picked]
        marked = [sum(values[i] for values in self.marked.values()) for i in picked]
        return raw, marked


def ledger_window(today: date) -> Span:
    """看板要用到的全部日子：近 12 个月的走势从 11 个月前的 1 号起，别的时间段和它们的
    上一段都落在这里面。再早就超出 Cost Explorer 的保留期了。"""
    return Span(max(month_shift(today, -(TREND_MONTHS - 1)), earliest_queryable(today)), today)


def _fetch(account: Account, window: Span, refresh: bool):
    """一个账号整个窗口每天的 (原价, 折算后, 是否缓存, 错误)。比率、标签、启用日期都进缓存键：
    台账里改了比率，下一次打开就按新比率算，不用等缓存过期。"""
    key = (
        account.key, account.ak[-6:], window.start, window.end, account.tag_key, account.tag_value,
        account.tag_ratio, account.untag_ratio, config.COST_METRIC, tuple(config.SERVICE_FILTER),
    )
    if not refresh:
        with _lock:
            hit = _cache.get(key)
        if hit and time.time() - hit[0] < LEDGER_TTL:
            raw, marked = hit[1]
            return list(raw), list(marked), True, None
    _, raw, marked, cached, error = usage_explorer.account_series(
        account, window.start, window.end, "daily", refresh=refresh
    )
    if error:
        return [], [], False, error
    with _lock:
        _cache[key] = (time.time(), (list(raw), list(marked)))
    return raw, marked, cached, None


def build_ledger(accounts: list[Account], today: date, refresh: bool = False) -> Ledger:
    window = ledger_window(today)
    ledger = Ledger(days=[window.start + timedelta(days=i) for i in range(window.days)])
    if not accounts:
        return ledger

    def run(account: Account):
        return (account, *_fetch(account, window, refresh))

    workers = max(1, min(config.MAX_WORKERS, len(accounts)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(run, accounts))

    for account, raw, marked, cached, error in results:
        if error:
            ledger.errors.append(error)
            continue
        if len(raw) != len(ledger.days) or len(marked) != len(ledger.days):
            continue  # 不该发生：同一个窗口切出来的桶数对不上，宁可不算也别错位
        ledger.any_cached = ledger.any_cached or cached
        # 启用之前的消费不算（和概览一个口径）
        if account.start_date and account.start_date > window.start:
            cut = min(len(raw), (account.start_date - window.start).days)
            raw = [0.0] * cut + list(raw[cut:])
            marked = [0.0] * cut + list(marked[cut:])
        ledger.raw[account.key] = list(raw)
        ledger.marked[account.key] = list(marked)
    return ledger


# ---------------------------------------------------------------- 经营汇总
@dataclass
class Money:
    """一个账号、一个上游或者全部加起来，在这段时间里的钱。"""

    name: str
    raw: float = 0.0
    marked: float = 0.0
    prev_raw: float | None = None      # 没有上一段（今年）时是 None
    prev_marked: float | None = None

    @property
    def margin(self) -> float:
        return self.marked - self.raw

    @property
    def rate(self) -> float | None:
        """毛利率：毛利占收入的百分比。没有收入就没有毛利率。"""
        return self.margin / self.marked * 100 if self.marked > 0 else None

    @property
    def prev_margin(self) -> float | None:
        if self.prev_marked is None or self.prev_raw is None:
            return None
        return self.prev_marked - self.prev_raw

    @property
    def prev_rate(self) -> float | None:
        if self.prev_marked is None or self.prev_marked <= 0 or self.prev_margin is None:
            return None
        return self.prev_margin / self.prev_marked * 100

    @property
    def growth(self) -> float | None:
        """收入比上一段的变化（%）。"""
        return change(self.marked, self.prev_marked)

    def add(self, other: Money) -> None:
        self.raw += other.raw
        self.marked += other.marked
        if other.prev_marked is not None:
            self.prev_raw = (self.prev_raw or 0.0) + (other.prev_raw or 0.0)
            self.prev_marked = (self.prev_marked or 0.0) + other.prev_marked


@dataclass
class AccountMoney(Money):
    account: Account | None = None
    error: object | None = None    # 查不到 Cost Explorer 时的原因（不算进合计）
    credit: object | None = None   # 概览报表里这个账号的那一行：额度、累计、余额

    @property
    def has_numbers(self) -> bool:
        return self.error is None


@dataclass
class PartnerMoney(Money):
    accounts: int = 0
    failed: int = 0               # 其中查不到的账号数
    budget: float = 0.0
    used: float = 0.0
    balance: float = 0.0


def _spark(values: list[float]) -> str:
    if not values or not any(values):
        return ""
    return chart.render_spark(chart.downsample(values, 28), accent=MARGIN_COLOR, width=96, height=30)


def _card(label: str, value: str, delta: float | None, unit: str = "%", spark: str = "", hint: str = "") -> dict:
    return dict(label=label, value=value, delta=delta, unit=unit, spark=spark, hint=hint)


def build_business(accounts: list[Account], ledger: Ledger, period: Period, credit_rows: dict) -> SimpleNamespace:
    """经营汇总：四张指标卡、按账号和按上游的明细、合计。credit_rows 是 account.key ->
    概览报表的那一行（拿额度、已用、余额），没有就空着。"""
    reasons: dict[str, object] = {}
    for error in ledger.errors:
        number = _parts(error)[0]
        if number:
            reasons.setdefault(number, error)

    start_prev = 0.0 if period.previous else None
    total = Money("合计", prev_raw=start_prev, prev_marked=start_prev)
    rows: list[AccountMoney] = []
    for account in accounts:
        row = AccountMoney(account.label, account=account, credit=credit_rows.get(account.key))
        if account.key not in ledger.raw:
            row.error = reasons.get(account.account) or "查不到 Cost Explorer"
            rows.append(row)
            continue
        row.raw, row.marked = ledger.sums(account.key, period.span)
        if period.previous:
            row.prev_raw, row.prev_marked = ledger.sums(account.key, period.previous)
        total.add(row)
        rows.append(row)
    # 收入多的在前；查不到的放最后
    rows.sort(key=lambda r: (r.error is not None, -r.marked))

    start_prev = 0.0 if period.previous else None
    partners: dict[str, PartnerMoney] = {}
    for row in rows:
        group = partners.setdefault(
            row.account.partner,
            PartnerMoney(row.account.partner, prev_raw=start_prev, prev_marked=start_prev),
        )
        group.accounts += 1
        if row.error is not None:
            group.failed += 1
        else:
            group.add(row)
        credit = row.credit
        if credit is not None and credit.has_numbers:
            group.budget += credit.budget
            group.used += credit.total_cost
            group.balance += credit.balance
    partner_rows = sorted(partners.values(), key=lambda p: -p.marked)

    raw_days, marked_days = ledger.daily(period.span)
    margin_days = [m - r for r, m in zip(raw_days, marked_days)]
    rate_delta = (
        total.rate - total.prev_rate if total.rate is not None and total.prev_rate is not None else None
    )
    failed = sum(1 for row in rows if row.error is not None)
    cards = [
        _card("收入", money0(total.marked), total.growth, spark=_spark(marked_days), hint="折算后"),
        _card("AWS 原价", money0(total.raw), change(total.raw, total.prev_raw), spark=_spark(raw_days)),
        _card("毛利", money0(total.margin), change(total.margin, total.prev_margin), spark=_spark(margin_days)),
        _card("毛利率", pct(total.rate), rate_delta, unit=" 个百分点", hint="毛利 / 收入"),
    ]
    return SimpleNamespace(
        period=period, total=total, accounts=rows, partners=partner_rows, cards=cards,
        failed=failed, counted=len(rows) - failed, any_cached=ledger.any_cached,
    )


def monthly_trend(ledger: Ledger, today: date) -> SimpleNamespace:
    """近 12 个月：每月一根柱，底下 AWS 原价、上面毛利，整根就是收入。本月是到今天为止的。"""
    months = [month_shift(today, -k) for k in range(TREND_MONTHS - 1, -1, -1)]
    raw: list[float] = []
    marked: list[float] = []
    for first in months:
        span = Span(first, min(_month_end(first), today))
        month_raw = month_marked = 0.0
        for key in ledger.raw:
            a, b = ledger.sums(key, span)
            month_raw += a
            month_marked += b
        raw.append(month_raw)
        marked.append(month_marked)
    margin = [m - r for r, m in zip(raw, marked)]
    labels = [first.strftime("%Y-%m") for first in months]
    # 比率是加价，毛利不会是负的；万一有人把比率填到 1 以下，那一截按 0 画（数字还是对的）
    series = [
        SimpleNamespace(name="AWS 原价", slot=0, color=RAW_COLOR, values=raw),
        SimpleNamespace(name="毛利", slot=1, color=MARGIN_COLOR, values=[max(0.0, g) for g in margin]),
    ]
    # sort=False：原价永远在下、毛利在上，哪怕加价超过一倍、毛利比原价还多
    bars = chart.render_bars(labels, series, width=760, height=232, fmt=money, axis=money_axis, sort=False)
    total_raw, total_marked = sum(raw), sum(marked)
    return SimpleNamespace(
        chart=bars, start=labels[0], end=labels[-1], months=len(months),
        total_raw=total_raw, total_marked=total_marked, total_margin=total_marked - total_raw,
        rate=(total_marked - total_raw) / total_marked * 100 if total_marked > 0 else None,
    )


def credit_summary(report) -> SimpleNamespace:
    """额度：概览报表（各账号从启用日期累计到今天）的合计，外加累计毛利。"""
    rows = [row for row in report.rows if row.has_numbers]
    pct_used = report.total_usage_pct
    return SimpleNamespace(
        budget=report.total_budget,
        used=report.total_cost,
        balance=report.total_balance,
        usage_pct=pct_used,
        level=report.total_level,
        gauge=chart.render_gauge(None if pct_used is None else pct_used / 100, report.total_level),
        overspent=sum(1 for row in rows if row.overspent),
        danger=sum(1 for row in rows if row.level == "danger" and not row.overspent),
        warn=sum(1 for row in rows if row.level == "warn"),
        failed=report.missing_count,
        margin=sum(row.total_cost - row.tag_raw - row.untag_raw for row in rows),
        accounts=len(report.rows),
    )


# ---------------------------------------------------------------- 风险与告警
TONE_RANK = {"danger": 0, "warn": 1, "info": 2}


@dataclass
class Issue:
    tone: str      # danger / warn / info
    label: str     # 一个短词：「超出额度」「用量中断」
    detail: str = ""


@dataclass
class Watch:
    account: Account
    issues: list[Issue]

    @property
    def rank(self) -> int:
        return min(TONE_RANK.get(issue.tone, 3) for issue in self.issues)


# 生命周期标签的颜色说明它有多要紧：红的（默认是「风控」）算出事，琥珀的算要留意，其余只是标记
LIFECYCLE_TONES = {"red": "danger", "amber": "warn"}


def watch_list(accounts: list[Account], credit_rows: dict, states: dict, tags: list[LifecycleTag]) -> list[Watch]:
    """需要关注的账号：额度快用完 / 超了、Cost Explorer 查不到、用量中断、CloudWatch 读不到、
    打了红色（风控）或琥珀色的生命周期标签。一个账号一行，问题按轻重排，最要紧的账号在最前。"""
    tag_tone = {tag.name: LIFECYCLE_TONES.get(tag.color) for tag in tags}
    out: list[Watch] = []
    for account in accounts:
        issues: list[Issue] = []
        row = credit_rows.get(account.key)
        if row is not None:
            if row.error:
                reason = _parts(row.problem or row.error)[2]
                if row.stale_as_of:
                    issues.append(Issue("warn", "Cost Explorer 查询失败",
                                        f"显示的是截至 {row.stale_as_of:%m-%d} 的数 · {reason}"))
                else:
                    issues.append(Issue("danger", "查不到 Cost Explorer", reason))
            if row.has_numbers and row.overspent:
                issues.append(Issue("danger", "超出额度", f"超了 {money(-row.balance)}"))
            elif row.has_numbers and row.level in ("danger", "warn"):
                issues.append(Issue(row.level, f"额度用了 {row.usage_pct:.0f}%", f"余额 {money(row.balance)}"))
        state = states.get(account.key)
        if state is not None:
            if state.kind == "stopped":
                issues.append(Issue("warn", "用量中断", state.sub))
            elif state.kind == "unknown":
                issues.append(Issue("info", "读不到 CloudWatch", "不知道还有没有调用"))
        for name in account.lifecycle:
            tone = tag_tone.get(name)
            if tone:
                issues.append(Issue(tone, name, "生命周期"))
        if issues:
            issues.sort(key=lambda issue: TONE_RANK.get(issue.tone, 3))
            out.append(Watch(account, issues))
    out.sort(key=lambda watch: (watch.rank, -len(watch.issues)))
    return out


def recent_events(accounts: list[Account], limit: int = 12) -> list[SimpleNamespace]:
    """最近发出去的告警（events.py 记的流水），按时间倒序。账号用邮箱前缀写，认不出就空着。"""
    by_id = {a.account: a for a in accounts}
    out = []
    for event in events.recent(limit):
        account = by_id.get(event.account)
        if account is not None:
            who = account.label
        elif event.email:
            who = event.email.split("@", 1)[0]
        else:
            who = event.account or ("全部账号" if event.kind == "daily" else "")
        local = event.when.astimezone()
        out.append(SimpleNamespace(
            when=local.strftime("%m-%d %H:%M"), iso=local.isoformat(timespec="minutes"),
            tone=event.tone, title=event.title, text=event.text, who=who,
            number=event.account if account is not None else "", groups=event.groups,
        ))
    return out


# ---------------------------------------------------------------- 模型与用量
USAGE_METRICS = ("invocations", "input_tokens", "output_tokens")


def _utc_midnight(day: date) -> datetime:
    return datetime(day.year, day.month, day.day, tzinfo=timezone.utc)


def usage_window(period: Period) -> MetricWindow:
    """一次把这一段和上一段都查了：从上一段的第一天到这一段最后一天的 24 点（UTC）。
    结束时间取整天，一天里重复打开都命中缓存；中间隔着的那几天（本月比上月同期时）查了不用。"""
    first = period.previous.start if period.previous else period.span.start
    return MetricWindow(start=_utc_midnight(first), end=_utc_midnight(period.span.end + timedelta(days=1)),
                        period_key="1d")


def build_usage_block(accounts: list[Account], period: Period, refresh: bool = False) -> SimpleNamespace:
    """全部启用账号 × 四个区的调用次数和 token：四张指标卡（比上一段）、按模型、按区域。"""
    window = usage_window(period)
    regions = list(cloudwatch_metrics.DEFAULT_REGIONS)
    if refresh:
        cloudwatch_metrics.clear_cache()
    reports: dict[str, object] = {}
    if accounts:
        with ThreadPoolExecutor(max_workers=len(USAGE_METRICS)) as pool:
            futures = {
                key: pool.submit(cloudwatch_metrics.build_metrics, accounts, regions, window, key, "all")
                for key in USAGE_METRICS
            }
            reports = {key: future.result() for key, future in futures.items()}

    # 同一个账号同一个区，三个指标会各报一遍同样的错，只留一条
    errors, seen = [], set()
    for report in reports.values():
        for error in report.errors:
            number, region, reason, _ = _parts(error)
            if (number, region) not in seen:
                seen.add((number, region))
                errors.append(error)
    jobs = len(accounts) * len(regions)
    unreadable = bool(accounts) and len(seen) >= jobs

    def days(report) -> list[date]:
        return [stamp.astimezone(timezone.utc).date() for stamp in report.timestamps]

    def pick(report, span: Span | None) -> list[int]:
        if span is None:
            return []
        return [i for i, day in enumerate(days(report)) if day in span]

    def total(report, span: Span | None) -> float:
        columns = report.column_totals
        return sum(columns[i] for i in pick(report, span))

    current = {key: total(report, period.span) for key, report in reports.items()}
    before = {key: total(report, period.previous) for key, report in reports.items()} if period.previous else {}
    for bucket in (current, before):
        if bucket:
            bucket["total_tokens"] = bucket.get("input_tokens", 0.0) + bucket.get("output_tokens", 0.0)

    def daily(key: str) -> list[float]:
        if key == "total_tokens":
            ins, outs = daily("input_tokens"), daily("output_tokens")
            return [a + b for a, b in zip(ins, outs)]
        report = reports.get(key)
        if report is None:
            return []
        columns = report.column_totals
        return [columns[i] for i in pick(report, period.span)]

    cards = []
    for key, (label, _, unit) in cloudwatch_metrics.METRICS.items():
        value = current.get(key) if reports and not unreadable else None
        cards.append(dict(
            key=key, label=label, unit=unit, total=value,
            delta=change(value, before.get(key)) if before and value is not None else None,
            spark=_spark(daily(key)) if value else "",
        ))

    # 按模型：本段的调用次数排名，token 按同一个名字对上（输入 + 输出）
    models: list[SimpleNamespace] = []
    invocations = reports.get("invocations")
    if invocations is not None and not unreadable:
        picked = pick(invocations, period.span)
        tokens: dict[str, float] = {}
        for key in ("input_tokens", "output_tokens"):
            report = reports.get(key)
            if report is None:
                continue
            idx = pick(report, period.span)
            for series in report.series:
                tokens[series.name] = tokens.get(series.name, 0.0) + sum(series.values[i] for i in idx)
        grand = current.get("invocations", 0.0)
        for series in invocations.series:
            value = sum(series.values[i] for i in picked)
            if value <= 0:
                continue
            models.append(SimpleNamespace(
                name=series.name, color=chart.color_for(series.slot), value=value,
                tokens=tokens.get(series.name), share=value / grand * 100 if grand else 0.0,
                other=series.slot < 0,
            ))
        models.sort(key=lambda m: (m.other, -m.value))

    # 按区域：四个区都列出来，没有调用的也列（灰着），一眼看出流量在哪
    regions_out: list[SimpleNamespace] = []
    if invocations is not None and not unreadable:
        grand = current.get("invocations", 0.0)
        token_by_region: dict[str, float] = {}
        for key in ("input_tokens", "output_tokens"):
            report = reports.get(key)
            if report is None:
                continue
            idx = pick(report, period.span)
            for panel in report.panels:
                token_by_region[panel.region] = token_by_region.get(panel.region, 0.0) + sum(
                    sum(series.values[i] for i in idx) for series in panel.series)
        picked = pick(invocations, period.span)
        for panel in invocations.panels:
            value = sum(sum(series.values[i] for i in picked) for series in panel.series)
            regions_out.append(SimpleNamespace(
                region=panel.region, name=cloudwatch_metrics.REGIONS.get(panel.region, panel.region),
                value=value, tokens=token_by_region.get(panel.region, 0.0),
                share=value / grand * 100 if grand else 0.0,
            ))
        regions_out.sort(key=lambda r: -r.value)

    return SimpleNamespace(
        cards=cards, models=models, regions=regions_out, errors=errors, unreadable=unreadable,
        failed_jobs=len(seen), jobs=jobs,
        any_cached=any(getattr(r, "any_cached", False) for r in reports.values()),
        tags_resolved=all(getattr(r, "tags_resolved", True) for r in reports.values()),
    )
