"""运营看板（/ops/）：经营汇总（收入、AWS 原价、毛利）、风险与告警、模型与用量。

看板默认关着（OPS_DASHBOARD），这里的用例先把它打开再建 app；关着时的样子在 test_web。
CE、CloudWatch、用量状态都是 fake，「今天」钉在 2026-10-09，所以每个数都能手算：

    fake_costs 每个账号每天：打了标签的原价 100、没打的 50
    alpha   111111111111  ALPHA  TAG 1.0 / UNTAG 1.1  每天折算后 155  启用 08-01  正常
    alpha2  444444444444  ALPHA  TAG 1.2 / UNTAG 1.2  每天折算后 180  启用 10-05（本月才启用）
    beta    222222222222  BETA   TAG 1.1 / UNTAG 1.1  每天折算后 165  启用 08-01  风控
    gamma   333333333333  GAMMA  已停用——哪儿都不算

本月（10-01 ~ 10-09）：alpha 9 天 1395 / 1350，alpha2 只算 10-05 起的 5 天 900 / 750，
beta 9 天 1485 / 1350；合计收入 3780、原价 3450、毛利 330、毛利率 8.73%。
上月同期（09-01 ~ 09-09）：alpha 1395、beta 1485、alpha2 还没启用 → 收入 2880、原价 2700。
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta, timezone

import pytest

from bedrock_cost import cloudwatch_metrics, config, events, excel_source, ops, ops_report, usage_explorer
from bedrock_cost.aws_errors import QueryError

from .conftest import LEDGER_HEADER, TEST_PASSWORD, write_ledger
from .test_web import (  # noqa: F401  page_env / usage_states 是 autouse，import 进来才生效
    ce_failure,
    fail_cumulative,
    fail_daily,
    nav_items,
    notes,
    page_env,
    scrape,
    toasts,
    usage_state,
    usage_states,
)

TODAY = date(2026, 10, 9)

OPS_HEADER = [*LEDGER_HEADER, "EMAIL", "LIFECYCLE", "ENABLED"]
OPS_ROWS = [
    ["ALPHA", 111111111111, 500000, 1.0, 1.1, "AKIAFAKEALPHA0000000", "x" * 40, "map-migrated=migALPHA",
     date(2026, 8, 1), "alpha@example.com", "正常", True],
    ["ALPHA", 444444444444, 50000, 1.2, 1.2, "AKIAFAKEALPHB0000000", "w" * 40, "map-migrated=migALPHB",
     date(2026, 10, 5), "alpha2@example.com", "", True],
    ["BETA", 222222222222, 100000, 1.1, 1.1, "AKIAFAKEBETA00000000", "y" * 40, "map-migrated=migBETA",
     date(2026, 8, 1), "beta@example.com", "风控", True],
    ["GAMMA", 333333333333, 1000, 1.0, 1.0, "AKIAFAKEGAMMA0000000", "z" * 40, "map-migrated=migGAMMA",
     date(2026, 8, 1), "gamma@example.com", "", False],
]
ENABLED = ("111111111111", "444444444444", "222222222222")


@pytest.fixture(autouse=True)
def ops_dashboard_on(monkeypatch):
    """看板默认下线：先打开开关（得在 conftest 的 app 建出来之前，autouse 的先跑）。"""
    monkeypatch.setattr(config, "OPS_DASHBOARD", True, raising=False)


class _Today(date):
    """看板里 date.today() 拿到的「今天」。"""

    @classmethod
    def today(cls):
        return TODAY


def rows_with(**changes) -> list[list]:
    """OPS_ROWS 的副本，改几格：rows_with(BUDGET={"222222222222": 10000})。"""
    rows = [list(row) for row in OPS_ROWS]
    for column, by_number in changes.items():
        position = OPS_HEADER.index(column)
        for row in rows:
            if str(row[1]) in by_number:
                row[position] = by_number[str(row[1])]
    return rows


def rewrite(path, rows) -> None:
    write_ledger(path, header=OPS_HEADER, rows=rows)
    excel_source.clear_cache()


@pytest.fixture
def ops_book(ledger):
    rewrite(ledger, OPS_ROWS)
    return ledger


@pytest.fixture
def board(ops_book, fake_costs, fake_cloudwatch, monkeypatch, logged_in):
    """打开看板用的客户端：台账、CE、CloudWatch 都是假的，今天是 2026-10-09。"""
    monkeypatch.setattr(ops, "date", _Today)
    return logged_in


def page(client, query: str = "") -> str:
    response = client.get(f"/ops/{query}")
    assert response.status_code == 200
    return response.get_data(as_text=True)


# ---------------------------------------------------------------- 读页面的小工具
def section(html: str, key: str) -> str:
    """三块之一：biz（经营汇总）/ risk（风险与告警）/ usage（模型与用量）。"""
    start = html.index(f'aria-labelledby="ops-{key}"')
    end = html.find('<section class="ops-section"', start)
    return html[start : end if end > 0 else html.index("</main>")]


def metric_cards(html: str) -> list[str]:
    """指标卡，每张一行字：名字、（小字说明）、大数字、和上一段比。"""
    return [scrape(card) for card in re.findall(r'<div class="metric-card is-static">(.*?)</div>', html, re.S)]


def table(html: str, panel: str) -> tuple[list[str], list[list[str]], list[str]]:
    """明细表（partner / account）：(表头, 每一行每一格的字, 合计行)。"""
    block = re.search(rf'id="detail-{panel}".*?</table>', html, re.S).group(0)

    def cells(row: str) -> list[str]:
        return [scrape(cell) for cell in re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)]

    head = [scrape(th) for th in re.findall(r"<th[^>]*>(.*?)</th>", block, re.S)]
    body = re.search(r"<tbody>(.*?)</tbody>", block, re.S).group(1)
    foot = re.search(r"<tfoot>(.*?)</tfoot>", block, re.S)
    return head, [cells(row) for row in re.findall(r"<tr>(.*?)</tr>", body, re.S)], cells(foot.group(1)) if foot else []


def bar_items(html: str, title: str) -> list[str]:
    """「按模型」「按区域」里的每一条，一行字。"""
    start = html.index(f">{title}</h3>")
    block = html[start : html.index("</article>", start)]
    return [scrape(item.split(">", 1)[1]) for item in block.split('<div class="bar-item')[1:]]


def watch(html: str) -> list[tuple[str, list[tuple[str, str, str]]]]:
    """需要关注：[(账号页链接, [(轻重, 一个短词, 细节)])]，按页面上的先后。"""
    found = []
    for item in re.findall(r'<li class="watch-item">(.*?)</ul>\s*</li>', section(html, "risk"), re.S):
        href = re.search(r'href="([^"]+)"', item).group(1)
        issues = [
            (tone, scrape(label), scrape(detail))
            for tone, label, detail in re.findall(
                r'<li class="issue issue-(\w+)"[^>]*>\s*<span class="issue-label">(.*?)</span>'
                r'(?:<span class="issue-detail">(.*?)</span>)?', item, re.S)
        ]
        found.append((href, issues))
    return found


# ====================================================================== 开关与入口
class TestEntry:
    def test_anonymous_is_redirected_to_login(self, client):
        for path in ("/ops/", "/ops/?period=ytd"):
            response = client.get(path)
            assert response.status_code == 302 and "/login" in response.headers["Location"]

    def test_switched_on_it_is_in_the_sidebar_and_marked_current(self, board):
        items = nav_items(page(board))
        assert [(href, title) for href, title, _ in items] == [
            ("/", "概览"), ("/ops/", "运营看板"), ("/account/", "账号"), ("/accounts/", "账号管理"),
        ]
        assert [title for _, title, on in items if on] == ["运营看板"]

    def test_nav_link_says_what_the_dashboard_waits_for(self, board):
        html = board.get("/").get_data(as_text=True)
        item = re.search(r'href="/ops/" title="运营看板"\s+data-loading="([^"]*)"\s+data-loading-note="([^"]*)"', html)
        assert item and item.groups() == ("正在汇总各账号的数", "第一次要查 Cost Explorer 和 CloudWatch，十几秒")

    def test_overview_links_to_it_next_to_the_cost_trend(self, board):
        html = board.get("/").get_data(as_text=True)
        trend = html[html.index(">近 30 天成本<") : html.index(">消费构成<")]
        assert re.search(r'<a class="viz-link" href="/ops/"[^>]*>收入和毛利</a>', trend)

    def test_missing_ledger_shows_a_message_not_a_500(self, board, tmp_path, monkeypatch):
        monkeypatch.setattr(config, "EXCEL_PATH", tmp_path / "gone.xlsx")
        html = page(board)
        assert '<div class="alert alert-error"><div><strong>读不到台账：</strong>找不到账号台账文件' in html

    @pytest.mark.parametrize("query", ["", "?period=last_month", "?period=30d", "?period=ytd", "?refresh=1"])
    def test_never_leaks_credentials_or_password(self, board, query):
        html = page(board, query)
        for secret in ("AKIAFAKEALPHA0000000", "AKIAFAKEALPHB0000000", "AKIAFAKEBETA00000000",
                       "x" * 40, "y" * 40, "w" * 40, TEST_PASSWORD):
            assert secret not in html


# ====================================================================== 时间段
PAGE_SUB = re.compile(r'<p class="page-sub">(.*?)</p>', re.S)


class TestPeriods:
    @pytest.mark.parametrize(
        "query, sub",
        [
            ("", "本月 10-01 ~ 10-09 · 比上月同期（09-01 ~ 09-09） · 3 个启用中的账号"),
            ("?period=nonsense", "本月 10-01 ~ 10-09 · 比上月同期（09-01 ~ 09-09） · 3 个启用中的账号"),
            ("?period=last_month", "上月 09-01 ~ 09-30 · 比前一个月（08-01 ~ 08-31） · 3 个启用中的账号"),
            ("?period=30d", "近 30 天 09-10 ~ 10-09 · 比前 30 天（08-11 ~ 09-09） · 3 个启用中的账号"),
            ("?period=ytd", "今年 01-01 ~ 10-09 · 3 个启用中的账号"),
        ],
    )
    def test_the_page_says_which_period_and_what_it_compares_with(self, board, query, sub):
        """认不出的时间段当本月；停用的 gamma 不在「启用中」里。"""
        assert scrape(PAGE_SUB.search(page(board, query)).group(1)) == sub

    @pytest.mark.parametrize("query, current", [("", "mtd"), ("?period=30d", "30d"), ("?period=bogus", "mtd")])
    def test_period_switcher_marks_the_current_one(self, board, query, current):
        html = page(board, query)
        bar = re.search(r'<nav class="seg" aria-label="时间段">(.*?)</nav>', html, re.S).group(1)
        links = re.findall(r'<a class="seg-item( is-on)?" href="/ops/\?period=(\w+)"', bar)
        assert [key for _, key in links] == ["mtd", "last_month", "30d", "ytd"]
        assert [key for on, key in links if on] == [current]
        assert f'href="/ops/?period={current}&amp;refresh=1"' in html          # 强制刷新留在这个时间段

    @pytest.mark.parametrize(
        "query, total",
        [
            # 收入、AWS 原价、毛利、毛利率、比上一段（今年没有）、额度、已用、余额（后三格是累计到今天的）
            ("", ["合计", "3", "$3,780.00", "$3,450.00", "$330.00", "8.73%", "↑31%",
                  "$650,000", "$23,300", "$626,700"]),
            # 上月：alpha 30 × 155 + beta 30 × 165 = 9600；八月是 9920
            ("?period=last_month", ["合计", "3", "$9,600.00", "$9,000.00", "$600.00", "6.25%", "↓3%",
                                    "$650,000", "$23,300", "$626,700"]),
            # 近 30 天：alpha 4650 + beta 4950 + alpha2 5 天 900；前 30 天 alpha2 还没启用
            ("?period=30d", ["合计", "3", "$10,500.00", "$9,750.00", "$750.00", "7.14%", "↑9%",
                             "$650,000", "$23,300", "$626,700"]),
            # 今年：各自从启用日期算起，所以和额度那边的「已用」一样
            ("?period=ytd", ["合计", "3", "$23,300.00", "$21,750.00", "$1,550.00", "6.65%",
                             "$650,000", "$23,300", "$626,700"]),
        ],
    )
    def test_totals_for_each_period(self, board, query, total):
        assert table(page(board, query), "partner")[2] == total

    def test_this_year_has_nothing_to_compare_with(self, board):
        """Cost Explorer 按天只查得到 14 个月，「今年」没有去年同期，就不比、也不出那一列。"""
        html = page(board, "?period=ytd")
        assert all(card.endswith("没有可比的上一段") for card in metric_cards(section(html, "biz")))
        head, _, _ = table(html, "partner")
        assert not any(name.startswith("收入比") for name in head)


# ====================================================================== 经营汇总
class TestBusiness:
    def test_four_metric_cards_compare_with_last_month_to_date(self, board):
        """收入 = 折算后的消费，毛利 = 收入 − AWS 原价，毛利率 = 毛利 / 收入；都和上月同期比。"""
        assert metric_cards(section(page(board), "biz")) == [
            "收入 折算后 $3,780 ↑31% 比上月同期",            # 2880 → 3780
            "AWS 原价 $3,450 ↑28% 比上月同期",              # 2700 → 3450
            "毛利 $330 ↑83% 比上月同期",                    # 180 → 330
            "毛利率 毛利 / 收入 8.73% ↑2.5 个百分点 比上月同期",  # 6.25% → 8.73%
        ]

    def test_partner_table_groups_accounts_by_upstream(self, board):
        head, rows, _ = table(page(board), "partner")
        assert head == ["上游", "账号", "收入", "AWS 原价", "毛利", "毛利率", "收入比上月同期", "额度", "已用", "余额"]
        assert rows == [
            # alpha 1395 + alpha2 900；上月同期只有 alpha 的 1395
            ["ALPHA", "2", "$2,295.00", "$2,100.00", "$195.00", "8.50%", "↑65%", "$550,000", "$11,750", "$538,250"],
            ["BETA", "1", "$1,485.00", "$1,350.00", "$135.00", "9.09%", "持平", "$100,000", "$11,550", "$88,450"],
        ]

    def test_account_table_lists_each_enabled_account(self, board):
        """收入多的在前；本月才启用的 alpha2 只算 10-05 起的 5 天，上月同期没有数可比。"""
        head, rows, _ = table(page(board), "account")
        assert head == ["账号", "上游", "生命周期", "收入", "AWS 原价", "毛利", "毛利率", "收入比上月同期", "额度使用"]
        assert rows == [
            ["B 222222222222 beta@example.com", "BETA", "风控", "$1,485.00", "$1,350.00", "$135.00", "9.09%", "持平", "11.55%"],
            ["A 111111111111 alpha@example.com", "ALPHA", "正常", "$1,395.00", "$1,350.00", "$45.00", "3.23%", "持平", "2.17%"],
            ["A 444444444444 alpha2@example.com", "ALPHA", "", "$900.00", "$750.00", "$150.00", "16.67%", "—", "1.80%"],
        ]

    def test_accounts_link_to_their_cost_tab(self, board):
        block = re.search(r'id="detail-account".*?</table>', page(board), re.S).group(0)
        assert re.findall(r'href="([^"]+)"', block) == [f"/account/{n}/cost" for n in
                                                       ("222222222222", "111111111111", "444444444444")]

    def test_disabled_accounts_are_left_out(self, board):
        html = page(board, "?period=ytd")
        assert "333333333333" not in section(html, "biz")
        assert "gamma" not in scrape(section(html, "biz"))

    def test_both_tables_sort_in_the_browser(self, board):
        """点表头排序（app.js 的 table[data-sortable]）：格子上的 data-sort 是没格式化的数。"""
        html = page(board)
        for panel, sortable in (("partner", 10), ("account", 8)):
            block = re.search(rf'id="detail-{panel}".*?</table>', html, re.S).group(0)
            assert '<table class="wide-table ops-table" data-sortable>' in block
            assert len(re.findall(r'<th class="[^"]*\bsortable\b', block)) == sortable
        assert 'data-sort="2295.0"' in html and 'data-sort="1350.0"' in html
        # 默认看「按上游」；「按账号」那一页先藏着，选过哪个记在浏览器里
        assert 'id="detail-account" role="tabpanel" aria-labelledby="tab-account" hidden>' in html
        assert "localStorage.setItem('ops-detail'" in html

    def test_twelve_month_trend_is_raw_cost_plus_margin(self, board):
        """近 12 个月：每月一根柱，底下 AWS 原价、上面毛利，整根是收入；本月到今天为止。"""
        html = page(board)
        card = html[html.index(">近 12 个月<") : html.index(">额度<")]
        assert "2025-11 ~ 2026-10" in scrape(card)
        assert "$1,550 毛利" in scrape(card)
        assert "收入 $23,300 · AWS 原价 $21,750 · 毛利率 6.65%" in scrape(card)
        raw = re.search(r'<script id="ops-trend-data" type="application/json">(.*?)</script>', html, re.S)
        months = json.loads(raw.group(1))
        assert [m["label"] for m in months][:1] + [m["label"] for m in months][-1:] == ["2025-11", "2026-10"]
        assert len(months) == 12
        october = months[-1]
        assert october["total"] == "$3,780.00"
        assert {row["name"]: row["value"] for row in october["rows"]} == {"AWS 原价": "$3,450.00", "毛利": "$330.00"}
        assert months[0] == {"label": "2025-11", "total": "$0.00", "rows": []}        # 还没有账号启用

    def test_three_places_agree(self, board):
        """各账号都是窗口里启用的：今年的收入 = 近 12 个月的收入 = 额度那边累计的已用。"""
        html = page(board, "?period=ytd")
        revenue = table(html, "partner")[2][2]
        assert revenue == "$23,300.00"
        assert "收入 $23,300 ·" in scrape(html[html.index(">近 12 个月<") : html.index(">额度<")])
        assert table(html, "partner")[2][-2] == "$23,300"

    def test_credit_card_adds_up_from_each_start_date(self, board):
        html = page(board)
        card = scrape(html[html.index(">额度</h3>") : html.index("</article>", html.index(">额度</h3>"))])
        for words in ("3 个账号 · 各自从启用日期累计到 10-09", "额度 $650,000", "使用率 3.58%", "余额 $626,700",
                      "累计毛利 $1,550", "超出额度 0 个", "快用完（≥ 90%） 0 个", "过半（≥ 70%） 0 个"):
            assert words in card

    def test_overspent_and_nearly_spent_accounts(self, board, ops_book):
        """beta 额度只有 1 万却用了 11550：超了；alpha2 额度 1000 用了 900：快用完。"""
        rewrite(ops_book, rows_with(BUDGET={"222222222222": 10000, "444444444444": 1000}))
        html = page(board)
        card = scrape(html[html.index(">额度</h3>") : html.index("</article>", html.index(">额度</h3>"))])
        assert "超出额度 1 个" in card and "快用完（≥ 90%） 1 个" in card
        assert table(html, "partner")[1][1][-1] == "-$1,550"                    # beta 那一行的余额
        assert watch(html) == [
            ("/account/222222222222/", [("danger", "超出额度", "超了 $1,550.00"), ("danger", "风控", "生命周期")]),
            ("/account/444444444444/", [("danger", "额度用了 90%", "余额 $100.00")]),
        ]

    def test_cached_for_two_hours_unless_refreshed(self, board, monkeypatch):
        """逐日账每个账号查一次、缓存 2 小时（CE 按请求收钱）；强制刷新跳过缓存。"""
        assert table(page(board), "partner")[2][3] == "$3,450.00"
        original = usage_explorer._fetch_account

        def doubled(account, start, end, dimension, granularity, dates, refresh):
            rows, currency, cached, error = original(account, start, end, dimension, granularity, dates, refresh)
            return {k: [[raw * 2, marked * 2] for raw, marked in cells] for k, cells in rows.items()}, currency, cached, error

        monkeypatch.setattr(usage_explorer, "_fetch_account", doubled)
        cached = page(board)
        assert table(cached, "partner")[2][3] == "$3,450.00"
        assert "部分数据来自缓存" in scrape(PAGE_SUB.search(cached).group(1))
        assert table(page(board, "?refresh=1"), "partner")[2][3] == "$6,900.00"

    def test_clear_cache_drops_the_daily_ledger_too(self, board, monkeypatch):
        assert table(page(board), "partner")[2][3] == "$3,450.00"
        fail_daily(monkeypatch, set(ENABLED))
        board.get("/cache/clear")
        assert table(page(board), "partner")[2][3] == "$0.00"


# ====================================================================== 出错
class TestProblems:
    """哪一份数据出错只影响它那一块，页面照常出来，原因从右上角说。"""

    def test_an_account_missing_from_cost_explorer(self, board, monkeypatch):
        fail_daily(monkeypatch, {"222222222222"})
        html = page(board)
        [toast] = toasts(html)
        assert (toast.tone, toast.title, toast.sub) == ("error", "查不到 Cost Explorer", "beta · 222222222222")
        assert toast.text == "Cost Explorer 请求过于频繁，请稍后重试"
        assert "ThrottlingException: Rate exceeded (222222222222)" in toast.detail

        assert "1 个账号查不到 Cost Explorer，没算进上面的数（原因见右上角）。" in notes(html)
        _, partners, total = table(html, "partner")
        assert total[2:5] == ["$2,295.00", "$2,100.00", "$195.00"]              # 只剩 alpha 和 alpha2
        assert partners[1][:3] == ["BETA", "1 1 个查不到", "$0.00"]
        _, accounts, _ = table(html, "account")
        assert accounts[-1] == ["B 222222222222 beta@example.com", "BETA", "风控", "查不到", "—", "—", "—", "—", "11.55%"]
        # 查不到的格子 data-sort 留空，点表头排序时沉底
        block = re.search(r'id="detail-account".*?</table>', html, re.S).group(0)
        assert '<td class="num err-cell" data-sort=""' in block

    def test_cumulative_failure_is_its_own_toast(self, board, monkeypatch):
        """累计那份（额度、已用）是另一次查询：逐日查得到、累计查不到的账号单独说。"""
        fail_cumulative(monkeypatch, {"222222222222": ce_failure("222222222222")})
        html = page(board)
        [toast] = toasts(html)
        assert (toast.title, toast.sub, toast.text) == (
            "查不到累计消费", "beta · 222222222222", "凭证缺少 ce:GetCostAndUsage 权限",
        )
        assert ("danger", "查不到 Cost Explorer", "凭证缺少 ce:GetCostAndUsage 权限") in watch(html)[0][1]
        card = scrape(html[html.index(">额度</h3>") : html.index("</article>", html.index(">额度</h3>"))])
        assert "查不到 1 个" in card

    def test_a_block_that_blows_up_does_not_take_the_page_down(self, board, monkeypatch):
        def boom(*args, **kwargs):
            raise RuntimeError("ledger exploded")

        monkeypatch.setattr(ops_report, "build_ledger", boom)
        html = page(board)
        [toast] = toasts(html)
        assert (toast.title, toast.text, toast.detail) == ("经营汇总出不来", "页面其余部分照常显示", "RuntimeError: ledger exploded")
        assert "经营汇总出不来（原因见右上角）。" in notes(html)
        assert ">近 12 个月<" not in html and 'id="detail-partner"' not in html
        # 其余几块照常
        assert "额度 $650,000" in scrape(section(html, "biz"))
        assert watch(html) and bar_items(html, "按区域")

    def test_some_regions_unreadable(self, board, monkeypatch):
        """us-west-1 被 SCP 拒了：三个账号各少一个区，数照出，说清楚少了哪些。"""
        original = cloudwatch_metrics._fetch_region

        def fetch(account, region, win, metric_key):
            if region == "us-west-1":
                return {}, False, QueryError(region=region, kind="denied", denied_by="scp",
                                             reason="被组织的 SCP（服务控制策略）显式拒绝 cloudwatch:GetMetricData",
                                             detail="explicit deny in a service control policy")
            return original(account, region, win, metric_key)

        monkeypatch.setattr(cloudwatch_metrics, "_fetch_region", fetch)
        html = page(board)
        [toast] = toasts(html)
        assert (toast.title, toast.sub) == ("3 个账号读不到 CloudWatch", "")
        assert len(toast.detail.splitlines()) == 3                                # 每个「账号 × 区」一行，三个指标不重复报
        assert "3 / 12 个「账号 × 区」读不到 CloudWatch，下面的数少了这些（原因见右上角）。" in notes(html)
        assert "北加州 us-west-1 0 次 · 0.00% 0 token" in bar_items(html, "按区域")

    def test_cloudwatch_unreadable_everywhere(self, board, monkeypatch):
        """全读不到：指标卡写「读不到」，不画成 0 次调用；按模型、按区域不出。"""
        def fetch(account, region, win, metric_key):
            return {}, False, QueryError(region=region, reason="凭证缺少 cloudwatch:GetMetricData 权限", kind="denied")

        monkeypatch.setattr(cloudwatch_metrics, "_fetch_region", fetch)
        html = page(board)
        usage = section(html, "usage")
        assert metric_cards(usage) == [f"{name} — 读不到 CloudWatch" for name in ("调用次数", "输入 Token", "输出 Token", "总 Token")]
        assert ">按模型</h3>" not in usage and ">按区域</h3>" not in usage
        assert [t.title for t in toasts(html)] == ["3 个账号读不到 CloudWatch"]


# ====================================================================== 风险与告警
class TestWatchList:
    def test_only_accounts_with_something_wrong(self, board):
        """默认都在正常用：只有打着红色（风控）标签的 beta 要关注。"""
        assert watch(page(board)) == [("/account/222222222222/", [("danger", "风控", "生命周期")])]

    def test_most_urgent_first(self, board, usage_states):
        usage_states["111111111111"] = usage_state("stopped")
        usage_states["444444444444"] = usage_state("unknown", errors=["us-east-1：读不到"])
        found = watch(page(board))
        assert [href for href, _ in found] == ["/account/222222222222/", "/account/111111111111/", "/account/444444444444/"]
        stopped = found[1][1]
        assert [(tone, label) for tone, label, _ in stopped] == [("warn", "用量中断")]
        assert stopped[0][2].startswith("最近调用 ")
        assert found[2][1] == [("info", "读不到 CloudWatch", "不知道还有没有调用")]

    def test_stale_numbers_are_a_warning_not_a_failure(self, board, monkeypatch):
        """查询失败但有上一次的数顶着：降一级，写明是截至哪天的数。"""
        stale = ce_failure("111111111111", tag_raw=100.0, untag_raw=50.0, stale_as_of=date(2026, 9, 28))
        fail_cumulative(monkeypatch, {"111111111111": stale})
        alpha = dict(watch(page(board)))["/account/111111111111/"]
        assert alpha == [("warn", "Cost Explorer 查询失败", "显示的是截至 09-28 的数 · 凭证缺少 ce:GetCostAndUsage 权限")]

    def test_lifecycle_distribution(self, board):
        risk = section(page(board), "risk")
        dist = scrape(re.search(r'<div class="life-dist">(.*?)</div>', risk, re.S).group(1))
        assert dist == "生命周期 正常 1 结算 0 风控 1 未标记 1 去账号管理改"
        assert 'href="/accounts/"' in risk


def event_rows(html: str) -> list[dict]:
    """最近告警，按页面上的先后。"""
    found = []
    for tone, body in re.findall(r'<li class="event event-(\w+)">(.*?)</li>', section(html, "risk"), re.S):
        who = re.search(r'<(a|span) class="event-who"(?: href="([^"]+)")?[^>]*>(.*?)</\1>', body, re.S)
        text = re.search(r'<p class="event-text">(.*?)</p>', body, re.S)
        found.append(dict(
            tone=tone,
            title=scrape(re.search(r'<span class="event-title">(.*?)</span>', body, re.S).group(1)),
            who=scrape(who.group(3)) if who else "",
            link=(who.group(2) or "") if who else "",
            text=scrape(text.group(1)) if text else "",
            time=scrape(re.search(r'<time class="event-time"[^>]*>(.*?)</time>', body, re.S).group(1)),
        ))
    return found


class TestRecentAlerts:
    """最近告警读 events 记的流水（conftest 已经把它指到临时目录）。"""

    def test_nothing_recorded_yet(self, board):
        risk = scrape(section(page(board), "risk"))
        assert "最近告警 还没有记录" in risk
        assert "告警发出去之后会出现在这里" in risk

    def test_newest_first_with_who_it_was_about(self, board):
        base = datetime(2026, 10, 9, 1, 0, tzinfo=timezone.utc)
        events.record("daily", "日报", "3 个账号 · 昨天合计 $1,234.00", tone="info", when=base)
        events.record("stopped", "用量中断", "最近调用 10-09 08:12", tone="warn",
                      account="222222222222", email="beta@example.com", when=base + timedelta(hours=1))
        events.record("disabled", "账号停用", "gamma 已停用", tone="warn",
                      account="333333333333", email="gamma@example.com", when=base + timedelta(hours=2))
        events.record("mail-abuse", "AWS 邮件：滥用通知", "来自 AWS Trust & Safety", tone="error",
                      email="stranger@example.com", when=base + timedelta(hours=3))

        def at(hours):
            return (base + timedelta(hours=hours)).astimezone().strftime("%m-%d %H:%M")

        html = page(board)
        assert "发到 TG 群的最近 4 条" in scrape(section(html, "risk"))
        assert event_rows(html) == [
            # 认不出是哪个账号的邮件：用邮箱 @ 前面那段，不给链接
            dict(tone="error", title="AWS 邮件：滥用通知", who="stranger", link="", text="来自 AWS Trust & Safety", time=at(3)),
            # 停用的账号也认得出，能点进它的页面
            dict(tone="warn", title="账号停用", who="gamma", link="/account/333333333333/", text="gamma 已停用", time=at(2)),
            dict(tone="warn", title="用量中断", who="beta", link="/account/222222222222/", text="最近调用 10-09 08:12", time=at(1)),
            dict(tone="info", title="日报", who="全部账号", link="", text="3 个账号 · 昨天合计 $1,234.00", time=at(0)),
        ]

    def test_only_the_latest_twelve(self, board):
        base = datetime(2026, 10, 8, tzinfo=timezone.utc)
        for i in range(15):
            events.record("test", f"测试消息 {i}", tone="info", when=base + timedelta(minutes=i))
        html = page(board)
        assert [row["title"] for row in event_rows(html)] == [f"测试消息 {i}" for i in range(14, 2, -1)]
        assert "发到 TG 群的最近 12 条" in scrape(section(html, "risk"))


# ====================================================================== 模型与用量
class TestUsage:
    """fake_cloudwatch：每个账号每个区每个点，Opus 5 直连 12、走推理配置的 Opus 4.8 是 7。
    本月 9 天 × 3 个启用的账号 × 4 个区。"""

    def test_metric_cards_compare_with_last_month_to_date(self, board):
        assert metric_cards(section(page(board), "usage")) == [
            "调用次数 2.1K 次 持平 比上月同期",          # (12 + 7) × 9 × 12 = 2052
            "输入 Token 2.1K token 持平 比上月同期",
            "输出 Token 2.1K token 持平 比上月同期",
            "总 Token 4.1K token 持平 比上月同期",      # 输入 + 输出
        ]

    def test_by_model(self, board):
        assert bar_items(page(board), "按模型") == [
            "Opus 5 1.3K 次 · 63.16% 2.6K token",    # 12 × 9 × 12 = 1296
            "Opus 4.8 756 次 · 36.84% 1.5K token",   # 7 × 9 × 12
        ]

    def test_by_region_lists_all_four(self, board):
        assert bar_items(page(board), "按区域") == [
            f"{name} {code} 513 次 · 25.00% 1.0K token"
            for name, code in (("弗吉尼亚", "us-east-1"), ("俄亥俄", "us-east-2"), ("北加州", "us-west-1"), ("俄勒冈", "us-west-2"))
        ]

    def test_disabled_accounts_are_not_counted(self, board, monkeypatch):
        """CloudWatch 只查启用中的账号。"""
        seen = set()
        original = cloudwatch_metrics._fetch_region

        def fetch(account, region, win, metric_key):
            seen.add(account.account)
            return original(account, region, win, metric_key)

        monkeypatch.setattr(cloudwatch_metrics, "_fetch_region", fetch)
        page(board)
        assert seen == set(ENABLED)
