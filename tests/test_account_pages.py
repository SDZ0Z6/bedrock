"""账号页：一个账号一个页面，五个页签（摘要 / 成本 / 用量 / 配额 / 预估）。

还有跟账号页连在一起的几件事：页头切换账号、记住最近看的账号、老网址（/cost-usage 之类）
跳到对应页签、起止日期不早于账号的启用日期，以及各页签查询失败时右上角的弹窗。

CE、CloudWatch、Service Quotas、价目表和用量状态都是 fake。台账用 test_web 的 book：
alpha（111111111111，正常）、beta（222222222222，风控）启用中，gamma（333333333333）已停用，
三个都从 2026-08-01 启用。页脚和仪表盘 SVG 的写法还在改，这里不拿它们断言。
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime, time, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from bedrock_cost import cloudwatch_metrics, config, cost_estimate, pricing, quotas, usage_explorer
from bedrock_cost.aws_errors import QueryError
from bedrock_cost.dates import earliest_queryable

from .conftest import RANGE_START
from .test_web import (  # noqa: F401  page_env / usage_states 是 autouse，import 进来才生效
    BOOK_HEADER,
    book,
    book_rows,
    ce_failure,
    fail_cumulative,
    fail_daily,
    main_part,
    notes,
    page_env,
    rewrite,
    scrape,
    toasts,
    usage_state,
    usage_states,
)

ALPHA, BETA, GAMMA = "111111111111", "222222222222", "333333333333"
TABS = [("", "摘要"), ("cost", "成本"), ("usage", "用量"), ("quota", "配额"), ("estimate", "预估")]
STATIC = Path(config.__file__).parent / "static"


@pytest.fixture
def fake_estimate(monkeypatch):
    """预估页签：每个账号每个区每天 100 万输入 token 的 Opus 5（跨区价 $5/M），一天 $20。"""
    price = pricing.ModelPrice(
        service_name="Claude Opus 5 (Amazon Bedrock Edition)", tier=pricing.GLOBAL,
        input=5.0 / 1e6, output=25.0 / 1e6, cache_read=0.5 / 1e6, cache_write=6.25 / 1e6,
    )
    table = pricing.PriceTable(prices={(price.service_name, price.tier): price}, fetched_at=1.0)
    monkeypatch.setattr(cost_estimate, "load_prices", lambda force=False: table)

    def fetch(account, region, start, end, stamps):
        return {("global.anthropic.claude-opus-5", "InputTokenCount"): [1_000_000.0] * len(stamps)}, False, None

    monkeypatch.setattr(cost_estimate, "_fetch_region", fetch)
    monkeypatch.setattr(cost_estimate, "resolve_profiles", lambda account, region: ({}, True))
    cost_estimate.clear_cache()
    yield
    cost_estimate.clear_cache()


@pytest.fixture
def pages(logged_in, book, fake_costs, fake_cloudwatch, fake_service_quotas, fake_estimate):
    """五个页签都能打开的客户端。"""
    return logged_in


def get(client, path: str) -> str:
    response = client.get(path)
    assert response.status_code == 200, (path, response.status_code, response.headers.get("Location"))
    return response.get_data(as_text=True)


def spy(monkeypatch, module, name) -> list:
    """把 module.name 换成一个记账的壳：照常调原来的，顺手记下每次的参数。"""
    calls = []
    original = getattr(module, name)

    def wrapper(*args, **kwargs):
        calls.append(args)
        return original(*args, **kwargs)

    monkeypatch.setattr(module, name, wrapper)
    return calls


def hero(html: str) -> str:
    """页头左边：头像、号码、状态、邮箱、上游、启用日期、生命周期（不含右边切换账号的下拉）。"""
    start = html.index('<header class="acct-hero">')
    return html[start : html.index('<div class="acct-hero-actions">', start)]


def date_inputs(html: str) -> dict[str, str]:
    """时间下拉里自定义起止的两个框。"""
    return dict(re.findall(r'<input type="(?:date|datetime-local)" name="(start|end)" value="([^"]*)"', html))


def selected(html: str, name: str) -> str:
    block = re.search(rf'<select id="{name}" name="{name}">(.*?)</select>', html, re.S).group(1)
    return re.search(r'<option value="([^"]*)" selected>', block).group(1)


def range_label(html: str) -> str:
    return scrape(re.search(r"<summary>(.*?)</summary>", html, re.S).group(1))


# ====================================================================== 页头和页签
class TestHeader:
    @pytest.mark.parametrize("tab, label", TABS)
    def test_header_says_whose_page_this_is(self, pages, tab, label):
        html = get(pages, f"/account/{BETA}/{tab}")
        text = scrape(hero(html))
        days = (date.today() - RANGE_START).days + 1
        assert re.search(r'class="avatar[^"]*avatar-lg[^"]*"[^>]*><span>B</span>', hero(html))
        for words in (BETA, "beta@example.com", "BETA", RANGE_START.isoformat(), "风控", "活跃"):
            assert words in text
        assert re.search(rf"(?<!\d){days}(?!\d)", text)
        assert f"<title>beta · {label} · Bedrock 成本监控</title>" in html
        # 面包屑回概览
        assert re.search(r'<nav class="crumbs"[^>]*>\s*<a href="/">概览</a>', html)

    @pytest.mark.parametrize("tab, label", TABS)
    def test_five_tabs_and_the_current_one(self, pages, tab, label):
        html = get(pages, f"/account/{ALPHA}/{tab}")
        nav = re.search(r'<nav class="acct-tabs"[^>]*>(.*?)</nav>', html, re.S).group(1)
        links = re.findall(r'<a class="acct-tab[^"]*" href="([^"]+)"\s*(aria-current="page")?[^>]*>(.*?)</a>', nav, re.S)
        assert [(href, name) for href, _, name in links] == [
            (f"/account/{ALPHA}/{key}", name) for key, name in TABS
        ]
        assert [name for _, current, name in links if current] == [label]

    def test_the_account_picker_keeps_the_tab(self, pages):
        """切换账号：开了 JS 直接跳到另一个账号的同一个页签；不开 JS 是下拉 + 提交到 /account/switch。"""
        html = get(pages, f"/account/{ALPHA}/usage")
        form = re.search(r'<form method="get" action="/account/switch">(.*?)</form>', html, re.S).group(1)
        assert '<input type="hidden" name="tab" value="usage">' in form
        assert 'data-href="/account/__ID__/usage"' in form
        assert re.findall(r'<option value="([^"]+)"\s*(selected)?>', form) == [
            (f"{ALPHA}#2", "selected"), (f"{BETA}#3", ""), (f"{GAMMA}#4", ""),
        ]
        assert form.index('<optgroup label="已停用">') < form.index(f'value="{GAMMA}#4"')   # 停用的放最后一组

    def test_the_email_is_the_title_and_the_number_comes_next(self, pages):
        """主行是邮箱（号码一眼认不出是谁），号码在下面那行的最前面。"""
        html = hero(get(pages, f"/account/{BETA}/"))
        assert scrape(re.search(r"<h1[^>]*>(.*?)</h1>", html, re.S).group(1)) == "beta@example.com"
        sub = scrape(re.search(r'<p class="acct-hero-sub">(.*?)</p>', html, re.S).group(1))
        assert sub.startswith(f"{BETA} · BETA ·")

    def test_without_an_email_the_number_is_the_title(self, pages, book):
        rewrite(book, book_rows(EMAIL={BETA: ""}))
        html = hero(get(pages, f"/account/{BETA}/"))
        assert scrape(re.search(r"<h1[^>]*>(.*?)</h1>", html, re.S).group(1)) == BETA
        assert scrape(re.search(r'<p class="acct-hero-sub">(.*?)</p>', html, re.S).group(1)).startswith("未填账号邮箱")

    def test_the_picker_puts_the_email_first(self, pages):
        html = get(pages, f"/account/{ALPHA}/")
        button = re.search(r'<button class="acct-picker-btn".*?</button>', html, re.S).group(0)
        assert [scrape(text) for text in re.findall(r'<span class="acct-picker-(?:name|sub)"[^>]*>(.*?)</span>', button)] == [
            "alpha@example.com", ALPHA,
        ]
        options = re.findall(r'<span class="acct-opt-name[^"]*">(.*?)</span>\s*<span class="acct-opt-sub[^"]*">(.*?)</span>', html)
        assert options == [("alpha@example.com", ALPHA), ("beta@example.com", BETA), ("gamma@example.com", GAMMA)]
        # 不开 JS 时的原生下拉也是邮箱在前
        assert re.search(rf'<option value="{ALPHA}#2"\s*selected>alpha@example.com · {ALPHA} · ALPHA</option>', html)

    @pytest.mark.parametrize("tab", [key for key, _ in TABS])
    def test_unreadable_usage_is_an_error(self, pages, usage_states, tab):
        """用量读不到（四个区都被拒）就是异常：红色的签，和概览卡片上一个说法。"""
        usage_states[BETA] = usage_state("unknown", errors=["us-east-1：被拒绝"])
        chip = re.search(r'<span class="acct-chip k-(\w+)">(.*?)</span>', hero(get(pages, f"/account/{BETA}/{tab}")), re.S)
        assert (chip.group(1), scrape(chip.group(2))) == ("error", "异常")

    def test_a_cost_explorer_failure_is_an_error_too(self, pages, monkeypatch):
        """摘要页签查不到累计消费：用量明明在跑，状态也算异常（账号本身在报错）。"""
        fail_cumulative(monkeypatch, {ALPHA: ce_failure(ALPHA)})
        chip = re.search(r'<span class="acct-chip k-(\w+)">(.*?)</span>', hero(get(pages, f"/account/{ALPHA}/")), re.S)
        assert (chip.group(1), scrape(chip.group(2))) == ("error", "异常")

    def test_edit_link_opens_this_row_in_account_management(self, pages):
        assert f'href="/accounts/?edit={BETA}%233"' in get(pages, f"/account/{BETA}/")

    def test_disabled_accounts_are_still_viewable(self, pages):
        html = get(pages, f"/account/{GAMMA}/cost")
        assert "已停用" in scrape(hero(html))
        assert "gamma@example.com" in scrape(hero(html))

    def test_unknown_account_goes_back_to_the_overview(self, pages):
        response = pages.get("/account/999999999999/cost")
        assert response.status_code == 302 and response.headers["Location"] == "/"
        html = get(pages, "/")
        assert [(t.tone, t.title) for t in toasts(html)] == [("warn", "台账里没有账号 999999999999。")]

    def test_missing_ledger_goes_back_to_the_overview(self, logged_in, tmp_path, monkeypatch):
        monkeypatch.setattr(config, "EXCEL_PATH", tmp_path / "gone.xlsx")
        response = logged_in.get(f"/account/{ALPHA}/cost", follow_redirects=True)
        assert response.request.path == "/"
        html = response.get_data(as_text=True)
        [toast] = toasts(html)
        assert toast.tone == "error" and toast.title.startswith("找不到账号台账文件")
        assert '<div class="alert alert-error">' in html


# ====================================================================== 记住最近看的账号
class TestRemembered:
    def test_first_visit_opens_the_first_enabled_account(self, pages):
        assert pages.get("/account/").headers["Location"] == f"/account/{ALPHA}/"

    @pytest.mark.parametrize("tab", [key for key, _ in TABS])
    def test_every_tab_remembers_the_account(self, pages, tab):
        get(pages, f"/account/{BETA}/{tab}")
        with pages.session_transaction() as session:
            assert session["account"] == BETA
        assert pages.get("/account/").headers["Location"] == f"/account/{BETA}/"

    def test_an_account_gone_from_the_ledger_falls_back_to_the_first_enabled(self, pages):
        with pages.session_transaction() as session:
            session["account"] = "999999999999"
        assert pages.get("/account/").headers["Location"] == f"/account/{ALPHA}/"

    def test_an_unknown_number_does_not_overwrite_the_memory(self, pages):
        get(pages, f"/account/{BETA}/")
        pages.get("/account/999999999999/")
        with pages.session_transaction() as session:
            assert session["account"] == BETA

    def test_an_empty_ledger_sends_you_to_account_management(self, logged_in, ledger):
        rewrite(ledger, [], header=BOOK_HEADER)
        assert logged_in.get("/account/").headers["Location"] == "/accounts/"
        with logged_in.session_transaction() as session:
            assert session["_flashes"] == [("info", "台账里还没有账号，先去账号管理加一个。")]

    def test_switch_goes_to_the_same_tab_of_the_other_account(self, pages):
        """页头下拉不开 JS 时提交过来的是 account.key（号码#行号）。"""
        assert pages.get(f"/account/switch?account={BETA}%233&tab=usage").headers["Location"] == f"/account/{BETA}/usage"
        # 光给号码也行；认不出的页签就去摘要
        assert pages.get(f"/account/switch?account={BETA}&tab=bogus").headers["Location"] == f"/account/{BETA}/"


# ====================================================================== 老网址
OLD = [("/cost-usage", "cost"), ("/model-usage", "usage"), ("/model-quota", "quota"), ("/cost-estimate", "estimate")]


class TestLegacyRedirects:
    """原来的四个查询页现在是账号页的四个页签，收藏夹里的老网址照样能用。"""

    @pytest.mark.parametrize("old, tab", OLD)
    def test_old_pages_open_the_tab_of_the_remembered_account(self, pages, old, tab):
        assert pages.get(old).headers["Location"] == f"/account/{ALPHA}/{tab}"      # 没看过：第一个启用的
        get(pages, f"/account/{BETA}/")
        assert pages.get(old).headers["Location"] == f"/account/{BETA}/{tab}"

    def test_query_parameters_come_along(self, pages):
        location = pages.get("/cost-usage?start=2026-09-01&end=2026-09-30&dim=tag&granularity=monthly").headers["Location"]
        url = urlsplit(location)
        assert url.path == f"/account/{ALPHA}/cost"
        assert parse_qs(url.query) == {
            "start": ["2026-09-01"], "end": ["2026-09-30"], "dim": ["tag"], "granularity": ["monthly"],
        }

    def test_the_old_account_parameter_picks_that_account(self, pages):
        """老网址上的 account=号码#行号：去那个账号，参数本身不再往下带；之后侧边栏「账号」也是它。"""
        location = pages.get(f"/model-quota?account={BETA}%233&region=us-east-2").headers["Location"]
        assert location == f"/account/{BETA}/quota?region=us-east-2"
        with pages.session_transaction() as session:
            assert session["account"] == BETA

    @pytest.mark.parametrize("value", ["all", "nope", "999999999999%232", ""])
    def test_an_old_account_value_not_in_the_ledger_is_ignored(self, pages, value):
        location = pages.get(f"/cost-estimate?account={value}&preset=last7").headers["Location"]
        assert location == f"/account/{ALPHA}/estimate?preset=last7"

    def test_the_redirect_lands_on_a_working_page(self, pages):
        response = pages.get("/model-usage?metric=output_tokens&win=24h", follow_redirects=True)
        assert response.status_code == 200
        html = response.get_data(as_text=True)
        assert "输出 Token · 四区合计" in scrape(html)
        assert range_label(html) == "近 24 小时"

    def test_missing_ledger_goes_back_to_the_overview(self, logged_in, tmp_path, monkeypatch):
        monkeypatch.setattr(config, "EXCEL_PATH", tmp_path / "gone.xlsx")
        assert logged_in.get("/model-usage").headers["Location"] == "/"

    def test_a_parameter_named_like_the_route_argument_does_not_break_it(self, pages):
        response = pages.get("/cost-usage?number=5&dim=tag")
        assert response.status_code == 302
        assert response.headers["Location"].startswith(f"/account/{ALPHA}/cost")


# ====================================================================== 起止日期不早于启用日期
class TestStartDateClamp:
    """账号启用之前的消费和用量不算：时间下拉最早只能选到启用日期，网址上手填得更早也会被改过来。
    启用日期定在三天前，和今天是哪天无关。"""

    @pytest.fixture
    def first(self, book):
        day = date.today() - timedelta(days=3)
        rewrite(book, book_rows(START_DATE={ALPHA: day}))
        return day

    @pytest.mark.parametrize("tab, module, name", [("cost", usage_explorer, "build_usage"),
                                                   ("estimate", cost_estimate, "build_estimate")])
    def test_a_start_before_the_start_date_is_moved_up(self, pages, first, monkeypatch, tab, module, name):
        calls = spy(monkeypatch, module, name)
        html = get(pages, f"/account/{ALPHA}/{tab}?preset=last7")
        today = date.today()
        assert f"开始日期早于启用日期，已从 {first.isoformat()} 起算。" in notes(html)
        assert date_inputs(html) == {"start": first.isoformat(), "end": today.isoformat()}
        assert [args[1:3] for args in calls] == [(first, today)]          # 查的也是这一段

    @pytest.mark.parametrize("tab", ["cost", "estimate"])
    def test_a_range_wholly_before_the_start_date_becomes_start_to_today(self, pages, first, tab):
        today = date.today()
        html = get(pages, f"/account/{ALPHA}/{tab}?start={today - timedelta(days=30)}&end={today - timedelta(days=20)}")
        assert f"所选区间在启用日期 {first.isoformat()} 之前，已改成从启用日期到今天。" in notes(html)
        assert date_inputs(html) == {"start": first.isoformat(), "end": today.isoformat()}

    def test_usage_starts_at_local_midnight_of_the_start_date(self, pages, first, monkeypatch):
        calls = spy(monkeypatch, cloudwatch_metrics, "build_metrics")
        html = get(pages, f"/account/{ALPHA}/usage?win=7d")
        assert f"开始时间早于启用日期，已从 {first.isoformat()} 起算。" in notes(html)
        window = calls[0][2]                                              # 主图那一次
        midnight = datetime.combine(first, time.min).astimezone()
        assert window.start <= midnight < window.start + timedelta(seconds=window.period)

    def test_usage_range_wholly_before_the_start_date_becomes_start_to_now(self, pages, first):
        before = first - timedelta(days=10)
        html = get(pages, f"/account/{ALPHA}/usage?start={before}T00:00&end={before + timedelta(days=5)}T00:00")
        assert f"所选时间在启用日期 {first.isoformat()} 之前，已改成从启用日期到现在。" in notes(html)

    @pytest.mark.parametrize("tab, kind", [("cost", "date"), ("estimate", "date"), ("usage", "datetime-local")])
    def test_the_pickers_cannot_go_before_the_start_date(self, pages, first, tab, kind):
        html = get(pages, f"/account/{ALPHA}/{tab}")
        least = first.isoformat() + ("T00:00" if kind == "datetime-local" else "")
        assert re.findall(rf'<input type="{kind}" name="(?:start|end)"[^>]*\smin="([^"]+)"', html) == [least, least]
        assert "最早只能选到启用日期" in html

    def test_without_a_start_date_the_limit_is_what_cost_explorer_keeps(self, pages, book):
        rewrite(book, book_rows(START_DATE={BETA: None}))
        html = get(pages, f"/account/{BETA}/cost")
        least = earliest_queryable(date.today()).isoformat()
        assert re.findall(r'<input type="date" name="(?:start|end)"[^>]*\smin="([^"]+)"', html) == [least, least]
        assert "最早只能选到启用日期" not in html

    @pytest.mark.parametrize("tab", ["cost", "estimate", "usage"])
    def test_a_future_start_date_does_not_turn_the_range_around(self, pages, book, tab):
        rewrite(book, book_rows(START_DATE={ALPHA: date.today() + timedelta(days=30)}))
        fields = date_inputs(get(pages, f"/account/{ALPHA}/{tab}"))
        assert fields["start"] <= fields["end"]

    @pytest.mark.parametrize("tab", ["cost", "estimate"])
    def test_a_range_wholly_before_ce_retention_does_not_turn_around(self, pages, book, tab):
        rewrite(book, book_rows(START_DATE={BETA: None}))
        fields = date_inputs(get(pages, f"/account/{BETA}/{tab}?start=2020-01-01&end=2020-02-01"))
        assert fields["start"] <= fields["end"]


# ====================================================================== 摘要
class TestSummaryTab:
    def test_credit_counts_from_the_start_date(self, pages):
        html = get(pages, f"/account/{ALPHA}/")
        card = scrape(html[html.index(">额度使用<") : html.index(">近 30 天成本<")])
        spent = 152.5 * ((date.today() - RANGE_START).days + 1)       # 100 × 1 + 50 × 1.05 每天
        assert f"{RANGE_START.isoformat()} 起累计" in card
        assert "额度 $500,000" in card
        assert f"使用率 {spent / 500_000 * 100:.2f}%" in card
        assert f"余额 ${500_000 - spent:,.2f}" in card

    def test_last_30_days_of_cost(self, pages):
        html = get(pages, f"/account/{ALPHA}/")
        card = html[html.index(">近 30 天成本<") : html.index(">告警<")]
        assert "$4,575" in scrape(card)                                  # 30 × 152.5
        assert f'href="/account/{ALPHA}/cost"' in card
        buckets = json.loads(re.search(r'<script id="trend-data" type="application/json">(.*?)</script>', html, re.S).group(1))
        assert len(buckets) == 30 and {b["total"] for b in buckets} == {"$152.50"}

    def test_last_7_days_of_calls(self, pages):
        """fake_cloudwatch：每小时每个区 Opus 5 直连 12 次、走推理配置的 Opus 4.8 7 次，7 × 24 个小时。"""
        html = get(pages, f"/account/{ALPHA}/")
        card = scrape(html[html.index(">近 7 天调用<") : html.index(">配额</h2>")])
        assert "12.8K 次" in card                                        # 168 × 4 × 19
        assert "claude-opus-5 8.1K" in card and "claude-opus-4-8 4.7K" in card

    def test_alert_switches_and_ledger_facts(self, pages):
        html = get(pages, f"/account/{ALPHA}/")
        alerts = scrape(html[html.index(">告警<") : html.index("</dl>", html.index(">告警<"))])
        assert "TG 告警 没开 0 个群" in alerts and "邮件告警 没开" in alerts and "用量 活跃" in alerts
        facts = scrape(html[html.index(">账号资料<") :])
        for words in ("上游 ALPHA", "账号邮箱 alpha@example.com", f"启用日期 {RANGE_START.isoformat()}",
                      "额度 $500,000.00", "TAG 比率 1", "UNTAG 比率 1.05", "TAG 判定 map-migrated=migALPHA",
                      "AK AKIAFAKE…0000"):
            assert words in facts

    def test_quota_card_only_uses_what_the_quota_tab_already_fetched(self, pages):
        """第一次查配额要 45 秒左右，摘要不等它：配额页签查过之后才有。"""
        def quota_card():
            html = get(pages, f"/account/{ALPHA}/")
            return html[html.index(">配额</h2>") : html.index("</article>", html.index(">配额</h2>"))]

        assert "还没有缓存" in scrape(quota_card())
        get(pages, f"/account/{ALPHA}/quota")
        rows = [scrape(row) for row in re.findall(r'<div class="quota-top-row">(.*?)</div>', quota_card(), re.S)]
        assert rows == [
            "Claude Opus 4.8 30.0M TPD 43.20B",
            "Claude Opus 4.6 V1 6.0M TPD 8.64B · 有的区没跟上",          # 只有 us-east-1 提了额
        ]

    def test_cost_explorer_failure_is_a_toast(self, pages, monkeypatch):
        fail_cumulative(monkeypatch, {ALPHA: ce_failure(ALPHA)})
        html = get(pages, f"/account/{ALPHA}/")
        [toast] = toasts(html)
        assert (toast.tone, toast.title, toast.sub, toast.text) == (
            "error", "查不到 Cost Explorer", f"alpha@example.com · {ALPHA}", "凭证缺少 ce:GetCostAndUsage 权限",
        )
        assert "is not authorized to perform: ce:GetCostAndUsage" in toast.detail
        assert "没有查到消费数据 · 额度 $500,000.00" in scrape(html)
        assert "消费查询 失败 凭证缺少 ce:GetCostAndUsage 权限" in scrape(html)
        assert 'class="alert' not in html

    def test_stale_numbers_are_shown_with_their_date(self, pages, monkeypatch):
        stale = ce_failure(ALPHA, tag_raw=1000.0, untag_raw=0.0, stale_as_of=date(2026, 9, 28))
        fail_cumulative(monkeypatch, {ALPHA: stale})
        html = get(pages, f"/account/{ALPHA}/")
        [toast] = toasts(html)
        assert toast.text == "凭证缺少 ce:GetCostAndUsage 权限，下面显示的是上一次查到的数"
        assert "今天查询失败，显示的是截至 09-28 的累计消费" in notes(html)
        assert "余额 $499,000.00" in scrape(html)                      # 上一次的 1000 照常算

    def test_cloudwatch_failure_is_one_toast_for_four_regions(self, pages, monkeypatch):
        def fetch(account, region, win, metric_key):
            return {}, False, QueryError(region=region, reason="凭证缺少 cloudwatch:GetMetricData 权限", kind="denied",
                                         detail=f"AccessDenied ({region})")

        monkeypatch.setattr(cloudwatch_metrics, "_fetch_region", fetch)
        [toast] = toasts(get(pages, f"/account/{ALPHA}/"))
        assert (toast.title, toast.sub, toast.text) == (
            "4 个区读不到 CloudWatch", f"alpha@example.com · {ALPHA}", "凭证缺少 cloudwatch:GetMetricData 权限",
        )
        assert toast.detail.splitlines() == [f"{r}：AccessDenied ({r})" for r in cloudwatch_metrics.DEFAULT_REGIONS]

    def test_daily_cost_failure_is_its_own_toast(self, pages, monkeypatch):
        fail_daily(monkeypatch, {ALPHA})
        [toast] = toasts(get(pages, f"/account/{ALPHA}/"))
        assert (toast.title, toast.sub) == ("查不到每天的成本", f"alpha@example.com · {ALPHA}")


# ====================================================================== 成本
def month_days() -> int:
    today = date.today()
    return (today - max(today.replace(day=1), RANGE_START)).days + 1


class TestCostTab:
    def test_filters_submit_on_change(self, pages):
        """维度、粒度改了就查；时间下拉里的自定义起止点「应用」才查。不开 JS 时还有一个查询按钮。"""
        html = get(pages, f"/account/{ALPHA}/cost")
        form = re.search(r'<form class="filters filters-auto" id="usage-filters"[^>]*>(.*?)</form>', html, re.S).group(1)
        buttons = [scrape(b) for b in re.findall(r"<button[^>]*>(.*?)</button>", form, re.S)]
        assert buttons == ["应用", "查询"]
        assert "<noscript><button" in form
        assert [scrape(o) for o in re.findall(r"<option[^>]*>(.*?)</option>",
                                              re.search(r'<select id="dim".*?</select>', form, re.S).group(0))] \
            == ["按服务", "按标签"]                                         # 一个账号的页面，没有「按账号」

    @pytest.mark.parametrize(
        "query, title",
        [("", "按服务的成本"), ("?dim=tag", "按标签的成本"), ("?dim=nonsense", "按服务的成本"),
         ("?dim=account", "按服务的成本")],
    )
    def test_dimension(self, pages, query, title):
        assert f">{title}</h2>" in get(pages, f"/account/{ALPHA}/cost{query}")

    @pytest.mark.parametrize("query, granularity", [("", "daily"), ("?granularity=monthly", "monthly"),
                                                    ("?granularity=hourly", "daily")])
    def test_granularity(self, pages, query, granularity):
        assert selected(get(pages, f"/account/{ALPHA}/cost{query}"), "granularity") == granularity

    @pytest.mark.parametrize(
        "query, note",
        [
            ("?start=abc&end=def", "开始日期格式无法识别，已使用本月 1 号。"),
            ("?start=2026-09-17&end=2026-09-01", "开始日期晚于结束日期，已自动调换。"),
            ("?start=2000-01-01", "Cost Explorer 仅保留约 14 个月历史数据"),
        ],
    )
    def test_bad_dates_are_fixed_with_a_note(self, pages, query, note):
        assert any(text.startswith(note) for text in notes(get(pages, f"/account/{ALPHA}/cost{query}")))

    def test_long_daily_range_suggests_monthly(self, pages, book):
        today = date.today()
        rewrite(book, book_rows(START_DATE={ALPHA: today - timedelta(days=200)}))
        html = get(pages, f"/account/{ALPHA}/cost?start={today - timedelta(days=200)}&end={today}&granularity=daily")
        assert "当前区间有 201 天，按日的点会很密，可以把粒度切成「按月」。" in notes(html)

    def test_shows_both_amount_bases(self, pages):
        """折算后（按台账比率逐格乘）和 AWS 原价都给：alpha 每天 152.5 / 150。"""
        days = month_days()
        tiles = scrape(re.search(r'<section class="summary summary-3">(.*?)</section>', get(pages, f"/account/{ALPHA}/cost"), re.S).group(1))
        assert f"折算后合计 ${152.5 * days:,.2f}" in tiles
        assert f"AWS 原价 ${150 * days:,.2f}" in tiles
        assert f"日均（折算后） $152.50" in tiles and f"{days} 天" in tiles

    def test_preset_highlight_survives_a_dimension_change(self, pages):
        """表单不带 preset，高亮靠区间反查，所以换维度后不能掉。"""
        today = date.today()
        html = get(pages, f"/account/{ALPHA}/cost?start={today - timedelta(days=29)}&end={today}&dim=tag")
        assert range_label(html) == "近 30 天"
        assert re.search(r'<a class="range-opt" href="[^"]*preset=last30[^"]*" aria-current="true">', html)

    def test_legend_and_detail_table(self, pages):
        html = get(pages, f"/account/{ALPHA}/cost")
        legend = [scrape(n) for n in re.findall(r'<span class="legend-name">(.*?)</span>', html)]
        assert legend == ["Claude Opus 5 (Amazon Bedrock Edition)", "Claude Sonnet 5 (Amazon Bedrock Edition)"]
        table = html[html.index(">成本明细<") :]
        assert table.count('<th class="num date-col">') == month_days()   # 每天一列
        assert "chart-hit" in html and 'id="chart-data"' in html          # 面积图的悬浮

    def test_cost_explorer_failure_is_a_toast(self, pages, monkeypatch):
        fail_daily(monkeypatch, {ALPHA})
        html = get(pages, f"/account/{ALPHA}/cost")
        [toast] = toasts(html)
        assert (toast.title, toast.sub, toast.text) == (
            "查不到 Cost Explorer", f"alpha@example.com · {ALPHA}", "Cost Explorer 请求过于频繁，请稍后重试",
        )
        assert "所选区间没有消费数据。" in scrape(html)

    def test_a_crash_in_the_data_layer_is_shown_not_a_500(self, pages, monkeypatch):
        def boom(*args, **kwargs):
            raise RuntimeError("usage exploded")

        monkeypatch.setattr(usage_explorer, "build_usage", boom)
        html = get(pages, f"/account/{ALPHA}/cost")
        assert "<strong>无法生成报表：</strong>RuntimeError: usage exploded" in html


# ====================================================================== 用量
class TestUsageTab:
    """fake_cloudwatch：每个点每个区 Opus 5 直连 12、走推理配置的 Opus 4.8 7。默认近 7 天、每 1 小时。"""

    def test_four_metric_cards_drive_the_main_chart(self, pages):
        html = get(pages, f"/account/{ALPHA}/usage")
        nav = re.search(r'<nav class="metric-cards" aria-label="指标">(.*?)</nav>', html, re.S).group(1)
        cards = re.findall(r'<a class="metric-card" href="([^"]+)"\s*(aria-current="true")?[^>]*>(.*?)</a>', nav, re.S)
        assert [parse_qs(urlsplit(href.replace("&amp;", "&")).query)["metric"][0] for href, _, _ in cards] == [
            "invocations", "input_tokens", "output_tokens", "total_tokens",
        ]
        assert [scrape(body) for _, _, body in cards] == [
            "调用次数 12.8K 次 0% 比前 7 天", "输入 Token 12.8K token 0% 比前 7 天",
            "输出 Token 12.8K token 0% 比前 7 天", "总 Token 25.5K token 0% 比前 7 天",
        ]
        assert [current for _, current, _ in cards] == ["aria-current=\"true\"", "", "", ""]

    @pytest.mark.parametrize("metric, title", [("input_tokens", "输入 Token"), ("total_tokens", "总 Token"),
                                               ("nonsense", "调用次数")])
    def test_metric(self, pages, metric, title):
        html = get(pages, f"/account/{ALPHA}/usage?metric={metric}")
        assert f"{title} · 四区合计" in scrape(re.search(r'<h2 class="viz-title">(.*?)</h2>', html, re.S).group(1))

    @pytest.mark.parametrize("query, name, value", [
        ("?tags=tagged", "tags", "tagged"), ("?tags=untagged", "tags", "untagged"), ("?tags=nonsense", "tags", "all"),
        ("?period=1d", "period", "1d"), ("?period=nonsense", "period", "1h"),
    ])
    def test_filters(self, pages, query, name, value):
        assert selected(get(pages, f"/account/{ALPHA}/usage{query}"), name) == value

    @pytest.mark.parametrize("query, label", [("", "近 7 天"), ("?win=6h", "近 6 小时"), ("?win=30d", "近 30 天")])
    def test_time_window(self, pages, query, label):
        assert range_label(get(pages, f"/account/{ALPHA}/usage{query}")) == label

    def test_unreadable_times_fall_back_to_the_default_window(self, pages):
        html = get(pages, f"/account/{ALPHA}/usage?start=abc&end=def")
        assert "时间格式无法识别，已改用默认窗口。" in notes(html)
        assert range_label(html) == "近 7 天"

    def test_long_window_at_fine_granularity_is_coarsened(self, pages):
        html = get(pages, f"/account/{ALPHA}/usage?win=30d&period=1m")
        assert any("粒度已自动调整" in text for text in notes(html))
        assert selected(html, "period") != "1m"

    def test_filters_submit_on_change(self, pages):
        html = get(pages, f"/account/{ALPHA}/usage")
        form = re.search(r'<form class="filters filters-auto" id="metric-filters"[^>]*>(.*?)</form>', html, re.S).group(1)
        assert '<input type="hidden" name="metric" value="invocations">' in form
        assert [scrape(b) for b in re.findall(r"<button[^>]*>(.*?)</button>", form, re.S)] == ["应用", "查询"]

    def test_one_line_chart_with_five_views(self, pages):
        """四区合计和四个区是同一张图的五个视图，页签切换，不重新加载。"""
        html = get(pages, f"/account/{ALPHA}/usage")
        tabs = re.findall(r'<button class="seg-item" type="button" role="tab" id="tab-([\w-]+)"', html)
        assert tabs == ["all", *cloudwatch_metrics.DEFAULT_REGIONS]
        panels = re.findall(r'<div class="chart-view" id="view-([\w-]+)" role="tabpanel"[^>]*?(hidden)?>', html)
        assert panels == [("all", ""), *[(region, "hidden") for region in cloudwatch_metrics.DEFAULT_REGIONS]]
        main = html[html.index('<div class="chart-views"') : html.index("<!-- ----", html.index('<div class="chart-views"'))]
        assert main.count('class="chart-svg line-chart"') == 5
        assert "chart-overlay" in main and "bar-chart" not in main

    def test_models_by_region_heat_table(self, pages):
        html = get(pages, f"/account/{ALPHA}/usage")
        heat = re.search(r'<table class="heat-table">(.*?)</table>', html, re.S).group(1)
        assert [scrape(th) for th in re.findall(r'<th scope="row"[^>]*>(.*?)</th>', heat, re.S)] == ["Opus 5", "Opus 4.8"]
        assert [scrape(th) for th in re.findall(r'<th[^>]*scope="col"[^>]*>(.*?)</th>', heat, re.S)] == [
            "模型", "east-1", "east-2", "west-1", "west-2",
        ]

    def test_nothing_readable_is_not_zero_calls(self, pages, monkeypatch):
        """四个区都读不到（被 SCP 拒了之类）：别把「看不到」画成「0 次调用」。"""
        def fetch(account, region, win, metric_key):
            return {}, False, QueryError(region=region, kind="denied", denied_by="scp",
                                         reason="被组织的 SCP（服务控制策略）显式拒绝 cloudwatch:ListMetrics")

        monkeypatch.setattr(cloudwatch_metrics, "_fetch_region", fetch)
        html = get(pages, f"/account/{ALPHA}/usage")
        assert "四个区都读不到 CloudWatch（原因见右上角），这段时间的调用量看不到——不是没有调用。" in scrape(html)
        cards = re.findall(r'<a class="metric-card"[^>]*>(.*?)</a>', html, re.S)
        assert all(scrape(card).endswith("— 读不到 CloudWatch") for card in cards)
        assert [(t.title, t.sub) for t in toasts(html)] == [("4 个区读不到 CloudWatch", f"alpha@example.com · {ALPHA}")]

    def test_unreadable_tags_are_flagged(self, pages, monkeypatch):
        """读不到推理配置上的标签时全部流量都会算成无标签，标签筛选不可信，得说出来。"""
        original = cloudwatch_metrics.resolve_profiles
        monkeypatch.setattr(cloudwatch_metrics, "resolve_profiles", lambda a, r: (original(a, r)[0], False))
        html = get(pages, f"/account/{ALPHA}/usage")
        assert any(text.startswith("读不到推理配置上的标签") for text in notes(html))


# ====================================================================== 配额
def quota_body(html: str) -> str:
    table = re.search(r'<table class="wide-table quota-table" data-sortable>(.*?)</table>', html, re.S).group(1)
    return re.search(r"<tbody>(.*?)</tbody>", table, re.S).group(1)


def quota_rows(html: str) -> list[str]:
    return [row for row in quota_body(html).split("<tr")[1:]]


class TestQuotaTab:
    """配额页签：这个账号每个应用推理配置在四个区的 TPM / TPD，不查 CloudWatch。"""

    def test_one_row_per_inference_profile_of_this_account(self, pages):
        """2 个模型 × 4 个区 = 8 条 ARN，每条一行；别的账号的不在这里。"""
        html = get(pages, f"/account/{ALPHA}/quota")
        thead = re.search(r"<thead>(.*?)</thead>", html, re.S).group(1)
        assert [scrape(th) for th in re.findall(r"<th[^>]*>(.*?)</th>", thead, re.S)] == [
            "ARN ID", "模型", "模型 ID", "区域", "TPM", "TPD",
        ]
        body = quota_body(html)
        ids = re.findall(r"<code>(\d\d[a-z0-9]+)</code>", body)
        assert sorted(ids) == sorted(f"11{region.replace('-', '')}{i}" for region in quotas.QUOTA_REGIONS for i in (0, 1))
        assert "22useast1" not in html
        # 完整 ARN 每行都是同一套前缀，不显示；账号自己起的配置名也不显示
        assert "application-inference-profile/" not in html
        assert "claude46Oupsauto_wjc_0529" not in html

    def test_exact_quotas_with_model_name_and_id(self, pages):
        html = get(pages, f"/account/{ALPHA}/quota")
        body = quota_body(html)
        for words in ("Claude Opus 4.8", "Claude Opus 4.6 V1", "anthropic.claude-opus-4-6-v1", "<code>us-east-1</code>",
                      "30,000,000", "43,200,000,000"):
            assert words in body
        assert "30.0M" not in body                                        # 不是压缩写法
        for chinese in ("弗吉尼亚", "俄亥俄", "北加州", "俄勒冈"):
            assert chinese not in body
        assert "Nova" not in html and "日 ÷ 分" not in html

    def test_flags_regions_that_missed_a_quota_increase(self, pages):
        """提额按区批：alpha 的 Opus 4.6 只有 us-east-1 提到了 6M，另外三个区标黄。"""
        html = get(pages, f"/account/{ALPHA}/quota")
        lagging = [row for row in quota_rows(html) if "tone-warn" in row]
        assert len(lagging) == 3
        assert all("Claude Opus 4.6 V1" in row and 'data-sort="3000000"' in row for row in lagging)
        assert not any('data-sort="us-east-1"' in row for row in lagging)
        assert "1 个模型的配额四区不一致" in scrape(html)

    def test_another_account_is_judged_on_its_own(self, pages):
        """beta 四个区都是 3M：不能拿 alpha 的 6M 把它整片标黄。"""
        html = get(pages, f"/account/{BETA}/quota")
        assert not [row for row in quota_rows(html) if "tone-warn" in row]
        assert "22useast10" in html and "11useast10" not in html

    def test_region_and_model_filters(self, pages):
        html = get(pages, f"/account/{ALPHA}/quota?region=us-east-2")
        body = quota_body(html)
        assert "us-east-2" in body and "us-west-2" not in body
        assert "已筛选，共 2 / 8 行" in scrape(html)

        html = get(pages, f"/account/{ALPHA}/quota?model=Claude+Opus+4.8")
        assert "Claude Opus 4.6 V1" not in quota_body(html)
        # 下拉里别的模型还在，不然筛完就换不回去了
        assert '<option value="Claude Opus 4.6 V1" >' in html
        assert selected(html, "model") == "Claude Opus 4.8"
        assert selected(get(pages, f"/account/{ALPHA}/quota"), "region") == ""          # 默认全部区域一起列

    @pytest.mark.parametrize("query, note", [("?region=eu-west-9", "区域参数无效，已取消区域筛选。"),
                                             ("?model=Claude+Nonexistent+9", "「Claude Nonexistent 9」不在这个账号的配额里，已取消模型筛选。")])
    def test_bad_filters_are_dropped_with_a_note(self, pages, query, note):
        assert note in notes(get(pages, f"/account/{ALPHA}/quota{query}"))

    def test_long_context_quota_has_no_profile(self, pages):
        """1M 上下文是独立配额、没有对应的应用配置，落在下面那张表里。"""
        html = get(pages, f"/account/{ALPHA}/quota")
        orphans = html[html.index(">有配额，但没有应用推理配置<") :]
        assert "Claude Sonnet 4.5 V1 1M Context Length" in orphans
        assert "独立配额" in orphans

    def test_table_sorts_in_the_browser(self, pages):
        """表头点一次升、再一次降、第三次回到服务端排好的分组。显示的字带千分位、还有「未列出」
        「无权限」，所以每格带一个规范值 data-sort，排序脚本在 app.js 里。"""
        html = get(pages, f"/account/{ALPHA}/quota")
        thead = re.search(r"<thead>(.*?)</thead>", html, re.S).group(1)
        assert len(re.findall(r'<th class="[^"]*\bsortable\b', thead)) == 6
        assert thead.count('aria-sort="none"') == 6
        assert thead.count('data-sort-type="number"') == 2 and thead.count('data-sort-type="text"') == 4
        body = quota_body(html)
        for value in ("30000000", "43200000000", "us-east-1", "Claude Opus 4.8", "11useast10"):
            assert f'data-sort="{value}"' in body
        assert "group-start" in body and "account-start" not in html     # 只留模型分组的细线
        script = (STATIC / "app.js").read_text(encoding="utf-8")
        for needle in ("table[data-sortable]", "thead th.sortable", "aria-sort", "head.cellIndex"):
            assert needle in script
        css = (STATIC / "style.css").read_text(encoding="utf-8")
        assert ".quota-table.is-sorted tr.group-start td { border-top: none; }" in css
        assert "tr.account-start" not in css

    def test_service_quotas_failure_is_a_toast_not_a_crash(self, pages, monkeypatch):
        def broken(*args, **kwargs):
            raise RuntimeError("AccessDeniedException: servicequotas denied")

        monkeypatch.setattr(quotas.boto3, "client", broken)
        quotas.clear_cache()
        html = get(pages, f"/account/{ALPHA}/quota")
        assert {(t.title, t.text) for t in toasts(html)} == {
            ("4 个区读不到 Service Quotas", "RuntimeError: AccessDeniedException: servicequotas denied"),
            ("4 个区读不到应用推理配置", "RuntimeError: AccessDeniedException: servicequotas denied"),
        }
        assert "读不到 Service Quotas，原因见右上角。" in scrape(quota_body(html))

    def test_denied_profiles_fall_back_to_model_rows(self, pages, monkeypatch):
        """SCP 拒绝 ListInferenceProfiles：配额表照出（按模型 × 区域），ARN 两列写「无权限」。"""
        monkeypatch.setattr(quotas, "fetch_app_profiles",
                            lambda account, region: ([], "AccessDeniedException: service control policy"))
        quotas.clear_cache()
        html = get(pages, f"/account/{ALPHA}/quota")
        assert "Claude Opus 4.8" in quota_body(html) and "无权限" in quota_body(html)
        assert any(text.startswith("读不到这个账号的应用推理配置（原因见右上角）") for text in notes(html))
        [toast] = toasts(html)
        assert (toast.title, toast.text) == ("4 个区读不到应用推理配置", "AccessDeniedException: service control policy")


# ====================================================================== 预估
class TestEstimateTab:
    """CloudWatch 的 token × AWS 公开牌价；更细的（四种 token、计价档、悬浮数据）在 test_cost_estimate。"""

    def test_estimate_is_for_this_account_only(self, pages):
        html = get(pages, f"/account/{ALPHA}/estimate?preset=last7")
        tiles = scrape(re.search(r'<section class="summary summary-4">(.*?)</section>', html, re.S).group(1))
        assert "预估总花费 $140.00" in tiles                              # 7 天 × 4 个区 × $5
        assert "日均 $20.00 共 7 天" in tiles
        row = re.search(r"<tbody>(.*?)</tbody>", html, re.S).group(1)
        assert "claude-opus-5" in row and "跨区" in row and "28.0M" in row
        assert "公开牌价" in scrape(main_part(html))                      # 不是账单

    def test_a_stale_price_table_is_a_note(self, pages, monkeypatch):
        stale = pricing.PriceTable(prices={}, fetched_at=1.0, stale=True, error="URLError: 网络不通")
        monkeypatch.setattr(cost_estimate, "load_prices", lambda force=False: stale)
        html = get(pages, f"/account/{ALPHA}/estimate")
        assert "拉不到最新的 AWS 价目表，用的是本地缓存副本（URLError: 网络不通）。单价可能已经过时。" in notes(html)

    def test_cloudwatch_failure_is_a_toast(self, pages, monkeypatch):
        error = QueryError(reason="凭证缺少 cloudwatch:ListMetrics 权限", kind="denied")
        monkeypatch.setattr(cost_estimate, "_fetch_region", lambda account, region, start, end, stamps: ({}, False, error))
        html = get(pages, f"/account/{ALPHA}/estimate")
        assert [(t.title, t.sub, t.text) for t in toasts(html)] == [
            ("4 个区读不到 CloudWatch", f"alpha@example.com · {ALPHA}", "凭证缺少 cloudwatch:ListMetrics 权限"),
        ]

    def test_a_crash_in_the_data_layer_is_shown_not_a_500(self, pages, monkeypatch):
        def boom(*args, **kwargs):
            raise RuntimeError("estimate exploded")

        monkeypatch.setattr(cost_estimate, "build_estimate", boom)
        assert "<strong>无法生成报表：</strong>RuntimeError: estimate exploded" in get(pages, f"/account/{ALPHA}/estimate")
