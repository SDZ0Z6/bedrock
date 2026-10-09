"""查询失败时顶上「上一次查到的数」：存取、fetch_split 的回退、概览页和合计。

日报卡片上怎么显示在 test_alerts 的 TestStaleNumbers；额度告警不拿旧数判阈值也在那里。
"""

from __future__ import annotations

import json
from datetime import date

import pytest

from bedrock_cost import config, cost_explorer, last_known
from bedrock_cost.cost_explorer import CostSplit
from bedrock_cost.excel_source import Account
from bedrock_cost.report import Report, build_row

START = date(2026, 8, 1)
MON, TUE = date(2026, 9, 28), date(2026, 9, 29)


def account(**overrides) -> Account:
    fields = dict(
        partner="P", account="626052499798", budget=1_000_000.0, tag_ratio=1.0, untag_ratio=1.05,
        ak="AKIAFAKE0000000000000", sk="s" * 40, row=2, tag_spec="map-migrated=migX", start_date=START,
    )
    fields.update(overrides)
    return Account(**fields)


def good(tag=100.0, untag=200.0) -> CostSplit:
    return CostSplit(tag_raw=tag, untag_raw=untag, currency="USD")


class TestStore:
    def test_round_trip(self):
        last_known.remember(account(), START, MON, good())
        known = last_known.recall(account(), START, TUE)
        assert (known.tag_raw, known.untag_raw, known.as_of) == (100.0, 200.0, MON)

    def test_nothing_remembered_yet(self):
        assert last_known.recall(account(), START, TUE) is None

    def test_a_different_start_date_does_not_count(self):
        """启用日期改过：旧的数是另一个起算点算的，拿来顶上反而误导。"""
        last_known.remember(account(), START, MON, good())
        assert last_known.recall(account(), date(2026, 9, 1), TUE) is None

    def test_a_different_tag_does_not_count(self):
        last_known.remember(account(), START, MON, good())
        assert last_known.recall(account(tag_spec="map-migrated=migY"), START, TUE) is None

    def test_a_different_cost_metric_does_not_count(self, monkeypatch):
        last_known.remember(account(), START, MON, good())
        monkeypatch.setattr(config, "COST_METRIC", "AmortizedCost")
        assert last_known.recall(account(), START, TUE) is None

    def test_an_older_result_does_not_replace_a_newer_one(self):
        """额度告警查的是「截至前天」，不能盖掉概览页刚存的「截至今天」。"""
        last_known.remember(account(), START, TUE, good(tag=300.0))
        last_known.remember(account(), START, date(2026, 9, 27), good(tag=1.0))
        assert last_known.recall(account(), START, TUE).tag_raw == 300.0

    def test_a_newer_result_replaces_the_old_one(self):
        last_known.remember(account(), START, MON, good(tag=1.0))
        last_known.remember(account(), START, TUE, good(tag=2.0))
        known = last_known.recall(account(), START, TUE)
        assert (known.tag_raw, known.as_of) == (2.0, TUE)

    def test_a_result_newer_than_asked_is_not_used(self):
        last_known.remember(account(), START, TUE, good())
        assert last_known.recall(account(), START, MON) is None

    def test_accounts_are_kept_apart(self):
        last_known.remember(account(), START, MON, good(tag=1.0))
        last_known.remember(account(account="052005814650"), START, MON, good(tag=2.0))
        assert last_known.recall(account(), START, TUE).tag_raw == 1.0

    def test_a_corrupt_file_counts_as_empty(self):
        config.LAST_KNOWN_COSTS_PATH.write_text("{ not json", encoding="utf-8")
        assert last_known.recall(account(), START, TUE) is None
        last_known.remember(account(), START, MON, good())       # 写得回去
        assert last_known.recall(account(), START, TUE) is not None

    def test_no_temp_file_left_behind(self):
        last_known.remember(account(), START, MON, good())
        assert [p.name for p in config.LAST_KNOWN_COSTS_PATH.parent.iterdir()] == ["last-known-costs.json"]

    def test_stores_raw_amounts_not_ratios(self):
        """比率在显示时现乘：改了比率，顶上的数跟着变。"""
        last_known.remember(account(), START, MON, good())
        saved = json.loads(config.LAST_KNOWN_COSTS_PATH.read_text(encoding="utf-8"))
        assert saved["accounts"]["626052499798"]["untag_raw"] == 200.0


class TestFetchSplitFallsBack:
    @pytest.fixture
    def ce(self, monkeypatch):
        """替换真正发请求的 _query：knobs["error"] 有值就抛，没有就返回 knobs["split"]。"""
        knobs = {"error": None, "split": good()}

        def fake_query(acct, start, end):
            if knobs["error"]:
                raise knobs["error"]
            return knobs["split"]

        monkeypatch.setattr(cost_explorer, "_query", fake_query)
        cost_explorer.clear_cache()
        yield knobs
        cost_explorer.clear_cache()

    def test_a_success_is_remembered(self, ce):
        cost_explorer.fetch_split(account(), START, MON)
        assert last_known.recall(account(), START, TUE) is not None

    def test_a_failure_brings_back_the_last_numbers(self, ce):
        cost_explorer.fetch_split(account(), START, MON)
        ce["error"] = RuntimeError("AccessDeniedException")
        split = cost_explorer.fetch_split(account(), START, TUE, refresh=True)
        assert split.error and "AccessDeniedException" in split.error      # 失败照样说出来
        assert (split.tag_raw, split.untag_raw, split.stale_as_of) == (100.0, 200.0, MON)
        assert not split.ok                                                 # 其余地方照旧当失败

    def test_a_failure_with_nothing_remembered_is_a_plain_failure(self, ce):
        ce["error"] = RuntimeError("AccessDeniedException")
        split = cost_explorer.fetch_split(account(), START, TUE)
        assert split.error and split.stale_as_of is None
        assert split.total_raw == 0

    def test_a_changed_tag_is_a_plain_failure(self, ce):
        cost_explorer.fetch_split(account(), START, MON)
        ce["error"] = RuntimeError("boom")
        split = cost_explorer.fetch_split(account(tag_spec="map-migrated=migY"), START, TUE)
        assert split.stale_as_of is None

    def test_an_unwritable_file_does_not_break_the_query(self, ce, monkeypatch):
        def boom(*args, **kwargs):
            raise OSError("read-only file system")

        monkeypatch.setattr(last_known, "remember", boom)
        split = cost_explorer.fetch_split(account(), START, MON)
        assert split.ok and split.tag_raw == 100.0


class TestReport:
    def stale(self) -> CostSplit:
        return CostSplit(tag_raw=100.0, untag_raw=200.0, error="凭证缺少权限", stale_as_of=MON)

    def test_a_stale_row_has_numbers_and_keeps_the_error(self):
        row = build_row(account(), self.stale())
        assert row.has_numbers and row.stale_as_of == MON
        assert row.error == "凭证缺少权限"
        assert row.total_cost == 100.0 + 200.0 * 1.05          # 比率照当前台账现乘
        assert row.balance == 1_000_000.0 - row.total_cost

    def test_a_plain_failure_has_no_numbers(self):
        row = build_row(account(), CostSplit(error="x"))
        assert not row.has_numbers and row.total_cost == 0

    def test_totals_include_stale_rows(self):
        """表上显示着旧数，合计不含它的话，加起来就对不上表了。"""
        report = Report(end=TUE, rows=[
            build_row(account(), self.stale()),
            build_row(account(account="052005814650"), good(tag=10.0, untag=0.0)),
            build_row(account(account="111111111111"), CostSplit(error="x")),
        ])
        assert report.total_cost == pytest.approx(100.0 + 200.0 * 1.05 + 10.0)
        assert report.failed_count == 2                      # 查询确实失败了两个
        assert report.missing_count == 1                     # 其中一个没有旧数可顶
        assert [r.account for r in report.stale_rows] == ["626052499798"]


class TestOverviewPage:
    def test_shows_the_last_numbers_with_their_date(self, logged_in, ledger, fake_costs, monkeypatch):
        """概览是一张张卡片：查询失败但有上一次的数，卡片照常显示那个数（标黄、写明截至哪天），
        合计照算；原因从右上角的弹窗说，不在页面中间插红条。"""
        import html as htmllib
        import re

        from bedrock_cost import activity, dashboard

        def one_stale(accounts, ranges, refresh=False):
            first, second = accounts
            return {
                first.key: CostSplit(tag_raw=0.0, untag_raw=1487.37, error="凭证缺少 ce:GetCostAndUsage 权限（AccessDeniedException）", stale_as_of=MON),
                second.key: CostSplit(tag_raw=50.0, untag_raw=0.0),
            }

        monkeypatch.setattr(cost_explorer, "fetch_all", one_stale)
        # 概览别的几块不查 AWS：用量状态当没调用，近 30 天的成本走 fake_costs；页面层缓存换成空的
        monkeypatch.setattr(activity, "account_activity", lambda account, now=None, refresh=False: activity.Activity(kind="idle"))
        monkeypatch.setattr(activity, "_cache", {})
        monkeypatch.setattr(dashboard, "_trend_cache", {})
        monkeypatch.setattr(config, "CURRENCY_SYMBOL", "$")
        html = logged_in.get("/").get_data(as_text=True)

        def text(fragment: str) -> str:
            return " ".join(htmllib.unescape(re.sub(r"<[^>]+>", " ", fragment)).split())

        card = next(c for c in re.findall(r'<article class="acct-card".*?</article>', html, re.S)
                    if 'href="/account/111111111111/"' in c)
        assert "消费查询失败：凭证缺少 ce:GetCostAndUsage 权限（AccessDeniedException）。下面是截至 09-28 的数" in text(card)
        assert "$1,561.74" in text(card) and "tone-warn" in card          # 1487.37 × 1.05，标黄
        assert "使用率" in text(card)                                    # 有数就照常算使用率和余额

        stack = html[html.index('id="toasts"') : html.index("<template data-toast-template")]
        assert "查不到 Cost Explorer" in text(stack)
        assert "有上一次数据的照常显示" in text(stack)

        # 合计含这个旧数：表上显示着它，合计不含它的话就对不上了
        assert f"余额 ${600_000 - (1487.37 * 1.05 + 50.0):,.2f}" in text(html)
        assert 'class="alert' not in html                                  # 不再整行只写「查询失败」
