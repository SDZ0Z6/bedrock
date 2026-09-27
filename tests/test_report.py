"""概览页八列口径。"""

from __future__ import annotations

from datetime import date

from bedrock_cost.cost_explorer import CostSplit
from bedrock_cost.excel_source import Account
from bedrock_cost.report import build_report, build_row

from .conftest import expected_marked

START, END = date(2026, 8, 1), date(2026, 8, 17)


def make_account(budget=1000.0, tag_ratio=1.0, untag_ratio=1.05, **kwargs) -> Account:
    return Account(
        partner=kwargs.get("partner", "P"),
        account=kwargs.get("account", "999"),
        budget=budget,
        tag_ratio=tag_ratio,
        untag_ratio=untag_ratio,
        ak="ak",
        sk="sk",
        row=kwargs.get("row", 2),
        tag_spec=kwargs.get("tag_spec", "map-migrated=migX"),
    )


class TestBuildRow:
    def test_applies_each_ratio_to_its_own_half(self):
        row = build_row(make_account(), CostSplit(tag_raw=200.0, untag_raw=100.0))
        assert row.tag_cost == 200.0  # 200 × 1
        assert row.untag_cost == 105.0  # 100 × 1.05
        assert row.total_cost == 305.0
        assert row.balance == 695.0
        assert row.usage_pct == 30.5

    def test_zero_budget_has_no_usage_pct(self):
        row = build_row(make_account(budget=0.0), CostSplit(tag_raw=100.0, untag_raw=100.0))
        assert row.usage_pct is None
        assert row.level == "none"

    def test_overspend_goes_negative_and_red(self):
        row = build_row(
            make_account(budget=100.0, untag_ratio=1.0), CostSplit(tag_raw=150.0, untag_raw=0.0)
        )
        assert row.balance == -50.0
        assert row.usage_pct == 150.0
        assert row.level == "danger"
        assert row.overspent is True
        assert row.bar_width == 100.0  # 进度条封顶，不溢出格子

    def test_thresholds(self):
        def level_at(pct):
            return build_row(
                make_account(budget=100.0, untag_ratio=1.0),
                CostSplit(tag_raw=pct, untag_raw=0.0),
            ).level

        assert level_at(50.0) == "ok"
        assert level_at(75.0) == "warn"
        assert level_at(95.0) == "danger"

    def test_failed_split_produces_an_error_row(self):
        row = build_row(make_account(), CostSplit(error="凭证无效"))
        assert row.error == "凭证无效"
        assert row.total_cost == 0.0
        assert row.usage_pct is None

    def test_carries_tag_label_and_note(self):
        split = CostSplit(tag_raw=0.0, untag_raw=100.0, note="标签值没匹配上")
        row = build_row(make_account(), split)
        assert row.tag_label == "map-migrated=migX"
        assert row.note == "标签值没匹配上"


class TestBuildReport:
    def test_totals_match_the_rows(self, ledger, fake_costs, accounts):
        report = build_report(END)
        assert len(report.rows) == 2
        assert report.total_cost == sum(r.total_cost for r in report.rows)
        assert report.total_budget == 600000.0
        for account, row in zip(accounts, report.rows):
            assert row.total_cost == expected_marked(account, START, END)

    def test_usage_and_balance_are_consistent(self, ledger, fake_costs):
        report = build_report(END)
        assert report.total_balance == report.total_budget - report.total_cost
        assert report.total_usage_pct == report.total_cost / report.total_budget * 100

    def test_tag_plus_untag_equals_total(self, ledger, fake_costs):
        report = build_report(END)
        assert round(report.total_tag + report.total_untag, 6) == round(report.total_cost, 6)

    def test_failed_rows_are_excluded_from_totals(self, ledger, fake_costs, monkeypatch):
        from bedrock_cost import cost_explorer

        def half_broken(account_list, ranges, refresh=False):
            splits = {}
            for index, account in enumerate(account_list):
                splits[account.key] = (
                    CostSplit(error="查询失败") if index == 0 else CostSplit(tag_raw=10.0, untag_raw=0.0)
                )
            return splits

        monkeypatch.setattr(cost_explorer, "fetch_all", half_broken)
        report = build_report(END)
        assert report.failed_count == 1
        assert report.total_cost == 10.0  # 只算成功的那一行
        assert report.total_budget == 100000.0  # 额度也只算成功的行
        assert len(report.errors) == 1
