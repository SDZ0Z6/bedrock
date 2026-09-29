"""主页表格的业务口径。

每一行：
    上游      = Excel PARTNER
    账号      = Excel ACCOUNT
    额度      = Excel BUDGET
    TAG 消费   = Cost Explorer 中匹配 Excel TAG 的消费 × TAG_RATIO
    UNTAG 消费 = Cost Explorer 中其余消费 × UNTAG_RATIO
    总消费     = TAG 消费 + UNTAG 消费
    使用率     = 总消费 / 额度 × 100
    余额       = 额度 - 总消费

**区间是每个账号各算各的**：从 Excel START_DATE 那天累计到今天。概览页因此
没有日期筛选——额度是一次性发的，拿「本月消费」去比它没有意义，要看的是
「发出去的额度用掉了多少」。区间怎么定见 dates.cumulative_range，台账没填
启用日期、或启用日期早于 CE 保留期时会打折扣，对应的行上会标出来。

TAG 的匹配规则见 cost_explorer 模块文档。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from . import config, cost_explorer
from .dates import (
    CUMULATIVE_CLAMPED,
    CUMULATIVE_FUTURE,
    CUMULATIVE_MISSING,
    CUMULATIVE_OK,
    cumulative_range,
)
from .excel_source import Account, load_accounts


@dataclass
class ReportRow:
    partner: str
    account: str
    budget: float
    tag_ratio: float
    untag_ratio: float
    tag_raw: float = 0.0
    untag_raw: float = 0.0
    tag_cost: float = 0.0
    untag_cost: float = 0.0
    total_cost: float = 0.0
    usage_pct: float | None = None
    balance: float = 0.0
    currency: str = "USD"
    error: str | None = None
    note: str | None = None
    tag_label: str = ""
    from_cache: bool = False
    row_number: int = 0
    # 这一行的统计区间。每个账号各不相同，所以挂在行上而不是报表上
    start_date: date | None = None        # 台账里填的启用日期，None = 没填
    range_start: date | None = None       # 实际查了哪一天起（可能被兜底或钳过）
    range_end: date | None = None
    range_status: str = ""                # dates.CUMULATIVE_* 之一
    # 这次查询失败（error 照留），行上的金额是上一次成功查到这一天为止的结果；
    # None = 金额是这次新查的。见 last_known
    stale_as_of: date | None = None

    @property
    def has_numbers(self) -> bool:
        """这一行有金额可显示：查询成功，或者失败了但有上一次成功的数顶着。"""
        return self.error is None or self.stale_as_of is not None

    @property
    def level(self) -> str:
        """使用率档位，模板据此上色。"""
        if self.usage_pct is None:
            return "none"
        if self.usage_pct >= config.DANGER_PCT:
            return "danger"
        if self.usage_pct >= config.WARN_PCT:
            return "warn"
        return "ok"

    @property
    def bar_width(self) -> float:
        if self.usage_pct is None:
            return 0.0
        return max(0.0, min(100.0, self.usage_pct))

    @property
    def overspent(self) -> bool:
        return self.budget > 0 and self.balance < 0

    @property
    def range_incomplete(self) -> bool:
        """这一行的累计消费不是「全部消费」，页面要标出来。

        两种情况都会让余额显示得比真实值高，光看数字分辨不出来：
        没填启用日期（只能从 CE 最早可查日兜底），或启用日期早于 CE 保留期。
        """
        return self.range_status in (CUMULATIVE_MISSING, CUMULATIVE_CLAMPED)

    @property
    def range_hint(self) -> str:
        """区间打了折扣时给出的一句说明，正常时是空串。"""
        if self.range_status == CUMULATIVE_MISSING:
            return "台账未填启用日期，按 Cost Explorer 最早可查日起算"
        if self.range_status == CUMULATIVE_CLAMPED:
            return (
                f"启用日期 {self.start_date.isoformat()} 早于 Cost Explorer 的保留期，"
                "更早的消费查不到，余额偏高"
            )
        if self.range_status == CUMULATIVE_FUTURE:
            return "启用日期晚于今天，暂无可统计区间"
        return ""


@dataclass
class Report:
    # 区间按账号各算各的，所以报表上只有共同的终点（今天），起点在每一行上
    end: date
    rows: list[ReportRow] = field(default_factory=list)
    currency: str = "USD"
    errors: list[str] = field(default_factory=list)
    any_cached: bool = False

    @property
    def incomplete_rows(self) -> list[ReportRow]:
        """累计区间打了折扣的行——它们的余额偏高。"""
        return [r for r in self.rows if r.range_incomplete]

    # -------------------------------------------------- 合计（只统计有金额的行）
    @property
    def _good(self) -> list[ReportRow]:
        # 查询失败但有上一次成功的数的行也算进来：表上显示着它的数，合计不含它的话，
        # 加起来就对不上表上的数了。页脚会说明含了几个旧数
        return [r for r in self.rows if r.has_numbers]

    @property
    def stale_rows(self) -> list[ReportRow]:
        """查询失败、显示的是上一次成功的数的行。"""
        return [r for r in self.rows if r.stale_as_of is not None]

    @property
    def missing_count(self) -> int:
        """查询失败、也没有上一次的数可顶的行——合计里没有它们。"""
        return sum(1 for r in self.rows if not r.has_numbers)

    @property
    def total_budget(self) -> float:
        return sum(r.budget for r in self._good)

    @property
    def total_tag(self) -> float:
        return sum(r.tag_cost for r in self._good)

    @property
    def total_untag(self) -> float:
        return sum(r.untag_cost for r in self._good)

    @property
    def total_cost(self) -> float:
        return sum(r.total_cost for r in self._good)

    @property
    def total_balance(self) -> float:
        return self.total_budget - self.total_cost

    @property
    def total_usage_pct(self) -> float | None:
        if self.total_budget <= 0:
            return None
        return self.total_cost / self.total_budget * 100

    @property
    def total_level(self) -> str:
        pct = self.total_usage_pct
        if pct is None:
            return "none"
        if pct >= config.DANGER_PCT:
            return "danger"
        if pct >= config.WARN_PCT:
            return "warn"
        return "ok"

    @property
    def failed_count(self) -> int:
        return sum(1 for r in self.rows if r.error is not None)


def _usage_pct(total_cost: float, budget: float) -> float | None:
    """额度为 0 或未填时使用率无意义，返回 None 让页面显示 “—”。"""
    if budget <= 0:
        return None
    return total_cost / budget * 100


def build_row(
    account: Account,
    split: cost_explorer.CostSplit,
    period: tuple[date, date, str] | None = None,
) -> ReportRow:
    start, end, status = period or (None, None, CUMULATIVE_OK)
    row = ReportRow(
        partner=account.partner,
        account=account.account,
        budget=account.budget,
        tag_ratio=account.tag_ratio,
        untag_ratio=account.untag_ratio,
        currency=split.currency,
        error=split.error,
        note=split.note,
        tag_label=account.tag_label,
        from_cache=split.from_cache,
        row_number=account.row,
        start_date=account.start_date,
        range_start=start,
        range_end=end,
        range_status=status,
    )
    if split.error and split.stale_as_of is None:
        return row

    row.stale_as_of = split.stale_as_of
    row.tag_raw = split.tag_raw
    row.untag_raw = split.untag_raw
    row.tag_cost = split.tag_raw * account.tag_ratio
    row.untag_cost = split.untag_raw * account.untag_ratio
    row.total_cost = row.tag_cost + row.untag_cost
    row.usage_pct = _usage_pct(row.total_cost, row.budget)
    row.balance = row.budget - row.total_cost
    return row


def build_report(today: date, refresh: bool = False) -> Report:
    """读台账 -> 按各账号的启用日期并发查 CE -> 算出主页需要的每一列。

    没有区间参数：概览页的口径就是「从启用那天累计到今天」，区间由台账决定，
    不由用户选。
    """
    accounts = load_accounts(force=refresh)
    periods = {a.key: cumulative_range(a.start_date, today) for a in accounts}
    ranges = {key: (start, end) for key, (start, end, _) in periods.items()}
    splits = cost_explorer.fetch_all(accounts, ranges, refresh=refresh)

    report = Report(end=today)
    for account in accounts:
        split = splits.get(account.key) or cost_explorer.CostSplit(error="未取到数据")
        row = build_row(account, split, periods[account.key])
        report.rows.append(row)
        if row.error:
            report.errors.append(f"{account.partner} / {account.account}：{row.error}")
        if row.from_cache:
            report.any_cached = True

    currencies = {r.currency for r in report._good if r.currency}
    if len(currencies) == 1:
        report.currency = currencies.pop()
    return report
