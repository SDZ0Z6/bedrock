"""主页表格的业务口径。

每一行：
    上游      = Excel PARTNER
    账号      = Excel ACCOUNT
    预算      = Excel BUDGET
    TAG 消费   = Cost Explorer 中匹配 Excel TAG 的消费 × TAG_RATIO
    UNTAG 消费 = Cost Explorer 中其余消费 × UNTAG_RATIO
    总消费     = TAG 消费 + UNTAG 消费
    使用率     = 总消费 / 预算 × 100
    余额       = 预算 - 总消费

TAG 的匹配规则见 cost_explorer 模块文档。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

import config
import cost_explorer
from excel_source import Account, load_accounts


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


@dataclass
class Report:
    start: date
    end: date
    rows: list[ReportRow] = field(default_factory=list)
    currency: str = "USD"
    errors: list[str] = field(default_factory=list)
    any_cached: bool = False

    # -------------------------------------------------- 合计（只统计查询成功的行）
    @property
    def _good(self) -> list[ReportRow]:
        return [r for r in self.rows if r.error is None]

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
    """预算为 0 或未填时使用率无意义，返回 None 让页面显示 “—”。"""
    if budget <= 0:
        return None
    return total_cost / budget * 100


def build_row(account: Account, split: cost_explorer.CostSplit) -> ReportRow:
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
    )
    if split.error:
        return row

    row.tag_raw = split.tag_raw
    row.untag_raw = split.untag_raw
    row.tag_cost = split.tag_raw * account.tag_ratio
    row.untag_cost = split.untag_raw * account.untag_ratio
    row.total_cost = row.tag_cost + row.untag_cost
    row.usage_pct = _usage_pct(row.total_cost, row.budget)
    row.balance = row.budget - row.total_cost
    return row


def build_report(start: date, end: date, refresh: bool = False) -> Report:
    """读台账 -> 并发查 CE -> 算出主页需要的每一列。"""
    accounts = load_accounts(force=refresh)
    splits = cost_explorer.fetch_all(accounts, start, end, refresh=refresh)

    report = Report(start=start, end=end)
    for account in accounts:
        split = splits.get(account.key) or cost_explorer.CostSplit(error="未取到数据")
        row = build_row(account, split)
        report.rows.append(row)
        if row.error:
            report.errors.append(f"{account.partner} / {account.account}：{row.error}")
        if row.from_cache:
            report.any_cached = True

    currencies = {r.currency for r in report._good if r.currency}
    if len(currencies) == 1:
        report.currency = currencies.pop()
    return report
