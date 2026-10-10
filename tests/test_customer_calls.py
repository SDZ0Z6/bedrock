"""客户页「近 24 小时调用」：账单晚 1~2 天，这张看现在——CloudWatch 的调用次数，折线图，一个账号一条线
（四区合计、每小时一个点），线尾是这个账号的头像。只查还在用的账号；颜色和「每天消费」一样，一个账号一个颜色。
客户页的「账号」卡片是按阶段的横条（长短按占全部账号的比例，后面是这一档的账号头像）。

账号页摘要页签的「近 7 天调用」下面补一行「今天预估花费」（用量 × 牌价，和「预估」页签一个算法）。
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone

import pytest

from bedrock_cost import chart, cloudwatch_metrics, cost_estimate, customer_pages
from bedrock_cost.avatars import Avatar
from bedrock_cost.aws_errors import QueryError
from bedrock_cost.windows import MetricWindow

from .conftest import LEDGER_ROWS
from .test_account_pages import ALPHA as ALPHA_NO_PAGES, fake_estimate, get, pages  # noqa: F401  (fixture)
from .test_accounts import admin, post  # noqa: F401  (admin 是 fixture)
from .test_customer_pages import ALPHA, BETA, detail, make
from .test_web import book, page_env, scrape, usage_states  # noqa: F401  (book 是 fixture；后两个 autouse)

ALPHA_NO, BETA_NO = str(LEDGER_ROWS[0][1]), str(LEDGER_ROWS[1][1])


@pytest.fixture
def real_calls(monkeypatch, fake_cloudwatch):
    """换回真的按账号汇总（conftest 默认换成了全 0），CloudWatch 用 fake_cloudwatch：每个区每小时 12 + 7 = 19 次。"""
    monkeypatch.setattr(customer_pages, "_hourly_calls", cloudwatch_metrics.account_totals)


def card(html: str) -> str:
    start = html.index(">近 24 小时调用<")
    return html[start:html.index("</article>", start)]


def legend(html: str, holder: str) -> dict[str, str]:
    """柱状图的图例：{名字: 颜色}。"""
    block = re.search(rf'<div class="legend-toggles" data-legend-for="{holder}"[^>]*>(.*?)</div>', html, re.S).group(1)
    return {scrape(name): color for color, name in
            re.findall(r'<i class="swatch" style="background: ([^"]+)"></i>(.*?)</button>', block, re.S)}


def lines(html: str) -> dict:
    """折线图交给悬浮脚本的数据：{unit, labels, x, series: [{name, color, v, y}]}。"""
    return json.loads(re.search(r'<script id="cust-calls-data" type="application/json">(.*?)</script>', html, re.S).group(1))


def quiet_except(monkeypatch, busy: dict[str, float]):
    """按号码给每小时的调用次数，没写的账号一直是 0。"""
    def fake(accounts, window, metric_key="invocations", regions=None, refresh=False):
        stamps, labels = cloudwatch_metrics.build_grid(window)
        return cloudwatch_metrics.AccountHours(
            timestamps=stamps, labels=labels,
            values={a.key: [busy.get(a.account, 0.0)] * len(stamps) for a in accounts})

    monkeypatch.setattr(customer_pages, "_hourly_calls", fake)


# ====================================================================== 客户页
def test_近24小时调用_一个账号一条线_线尾是头像(admin, fake_costs, real_calls):
    make(admin)
    post(admin, "/customers/C001/assign", account=[ALPHA, BETA])
    html = detail(admin)
    text = scrape(card(html))
    assert "3.6K 次" in text                                  # 2 个账号 × 4 个区 × 19 次 × 24 小时
    assert "最近一小时 152" in text and "2 / 2 个在用的账号有调用" in text
    data = lines(html)
    assert data["unit"] == "次" and len(data["labels"]) == 24
    assert {s["name"]: s["v"] for s in data["series"]} == {"acct-one": [76.0] * 24, "acct-two": [76.0] * 24}
    # 线尾：两个头像（字母和 .av-N 的颜色），末值一样也上下错开，拉一根细线连回线尾
    ends = re.search(r'<g class="chart-endavatars">(.*?)</g></g>', html, re.S).group(1)
    assert len(re.findall(r'<g class="chart-endavatar av-\d', ends)) == 2
    assert ends.count('style="fill: var(--av-bg)"') == 2 and ends.count("<line ") == 1
    # 图下面：头像对邮箱，外圈是线的颜色
    who = scrape(re.search(r'<div class="calls-legend">(.*?)</div>', html, re.S).group(1))
    assert "acct-one@example.com 1.8K" in who and "acct-two@example.com 1.8K" in who
    assert "data-line-src=\"cust-calls-data\"" in html


def test_放在每天消费和还能用几天下面_时间线上面(admin, fake_costs):
    make(admin)
    html = detail(admin)
    assert html.index(">还能用几天<") < html.index(">近 24 小时调用<") < html.index('id="tl-title"')
    assert 'class="viz-card col-6"' in html                   # 整行宽


def test_和每天消费一个颜色(admin, fake_costs, real_calls, monkeypatch):
    from bedrock_cost import usage_explorer

    from .test_customers import series_for

    today = datetime.now().date()
    spend = {ALPHA_NO: {(today - timedelta(days=i)).isoformat(): 100.0 for i in range(1, 10)},
             BETA_NO: {(today - timedelta(days=i)).isoformat(): 300.0 for i in range(1, 10)}}
    monkeypatch.setattr(usage_explorer, "account_series", series_for(spend))
    make(admin)
    post(admin, "/customers/C001/assign", account=[ALPHA, BETA])
    html = detail(admin)
    assert {s["name"]: s["color"] for s in lines(html)["series"]} == legend(html, "cust-spend")


def test_只查还在用的账号(admin, fake_costs, monkeypatch):
    asked = []

    def spy(accounts, window, metric_key="invocations", regions=None, refresh=False):
        asked.append(sorted(a.account for a in accounts))
        stamps, labels = cloudwatch_metrics.build_grid(window)
        return cloudwatch_metrics.AccountHours(timestamps=stamps, labels=labels,
                                               values={a.key: [0.0] * len(stamps) for a in accounts})

    monkeypatch.setattr(customer_pages, "_hourly_calls", spy)
    make(admin)
    post(admin, "/customers/C001/assign", account=[ALPHA, BETA])
    post(admin, "/customers/C001/settle", key=ALPHA)          # 结算了的不会再有调用
    html = detail(admin)
    assert asked[-1] == [BETA_NO]
    assert "0 / 1 个在用的账号有调用" in scrape(card(html))


def test_读不到CloudWatch_说一声(admin, fake_costs, monkeypatch):
    def broken(accounts, window, metric_key="invocations", regions=None, refresh=False):
        stamps, labels = cloudwatch_metrics.build_grid(window)
        errors = [QueryError(account=a.account, region="us-east-1", reason="凭证缺少 cloudwatch 权限") for a in accounts]
        return cloudwatch_metrics.AccountHours(timestamps=stamps, labels=labels,
                                               values={a.key: [0.0] * len(stamps) for a in accounts}, errors=errors)

    monkeypatch.setattr(customer_pages, "_hourly_calls", broken)
    make(admin)
    post(admin, "/customers/C001/assign", account=[ALPHA])
    html = detail(admin)
    assert "读不到 CloudWatch" in html and "凭证缺少 cloudwatch 权限" in html


def test_一直没调用的不画线_在图下面点名(admin, fake_costs, monkeypatch):
    quiet_except(monkeypatch, {ALPHA_NO: 30.0})
    make(admin)
    post(admin, "/customers/C001/assign", account=[ALPHA, BETA])
    html = detail(admin)
    assert [s["name"] for s in lines(html)["series"]] == ["acct-one"]
    assert "近 24 小时没有调用： A acct-two@example.com" in scrape(card(html))     # A 是邮箱的头一个字母
    assert "1 / 2 个在用的账号有调用" in scrape(card(html))


def test_没有账号的客户(admin, fake_costs):
    make(admin)
    assert "没有在用的账号" in scrape(card(detail(admin)))


# ====================================================================== 按账号汇总
def test_每个账号四区合计_不分模型(ledger, monkeypatch):
    from bedrock_cost import excel_source

    accounts = excel_source.load_accounts(force=True)
    end = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    window = MetricWindow(start=end - timedelta(hours=3), end=end, period_key="1h")

    def fetch(account, region, win, metric_key):
        if account.account == BETA_NO and region == "us-west-2":
            return {}, False, "该区不可用"
        base = 1.0 if account.account == ALPHA_NO else 10.0
        return {"model-a": [base] * 3, "model-b": [base] * 3}, False, None

    monkeypatch.setattr(cloudwatch_metrics, "_fetch_region", fetch)
    hours = cloudwatch_metrics.account_totals(accounts, window, regions=["us-east-1", "us-east-2", "us-west-2"])
    by_number = {a.account: hours.values[a.key] for a in accounts}
    assert by_number[ALPHA_NO] == [6.0, 6.0, 6.0]            # 3 个区 × 2 个模型 × 1
    assert by_number[BETA_NO] == [40.0, 40.0, 40.0]          # 一个区读不到，另外两个照算
    assert len(hours.labels) == 3 and [str(e.region) for e in hours.errors] == ["us-west-2"]


# ====================================================================== 账号页：今天预估花费
def test_摘要页签_今天预估花费(pages):
    """fake_estimate：每个区每天 100 万输入 token 的 Opus 5（$5/M），四个区一天 $20。"""
    html = get(pages, f"/account/{ALPHA_NO_PAGES}/")
    calls = scrape(html[html.index(">近 7 天调用<"):html.index(">配额</h2>")])
    assert "今天预估花费 $20.00" in calls and "以账单为准" in calls
    assert f'href="/account/{ALPHA_NO_PAGES}/estimate"' in html


def test_近7天没有调用_今天就不用估了(pages, monkeypatch):
    monkeypatch.setattr(cloudwatch_metrics, "_fetch_region", lambda account, region, win, key: ({}, False, None))
    asked = []
    monkeypatch.setattr(cost_estimate, "build_estimate", lambda *a, **k: asked.append(a))
    html = get(pages, f"/account/{ALPHA_NO_PAGES}/")
    assert "今天预估花费 $0.00" in scrape(html) and asked == []


# ====================================================================== 线尾的头像
def calls_report(*series):
    from datetime import datetime as dt
    end = dt(2026, 10, 10, 12, tzinfo=timezone.utc)
    window = MetricWindow(start=end - timedelta(hours=3), end=end, period_key="1h")
    stamps, labels = cloudwatch_metrics.build_grid(window)
    from types import SimpleNamespace
    return SimpleNamespace(labels=labels, timestamps=stamps, window=window, series=list(series),
                           metric_label="调用次数", unit="次", dimension_label="账号")


def test_末值一样的头像上下错开_一个都不省():
    faces = {name: Avatar(name[0].upper(), i, False) for i, name in enumerate(("kite", "kumo", "nova"))}
    report = calls_report(*(cloudwatch_metrics.MetricSeries(name=name, values=[5.0, 8.0, 3.0], slot=i)
                            for i, name in enumerate(faces)))
    svg = chart.render_lines(report, unit="次", width=900, uid="t", ends=faces).svg
    ys = [float(y) for y in re.findall(r'<circle cx="[\d.]+" cy="([\d.]+)" r="10"', svg)]
    assert len(ys) == 3 and all(b - a >= chart.AVATAR_GAP - 0.01 for a, b in zip(sorted(ys), sorted(ys)[1:]))
    assert svg.count('<line x1=') >= 2                       # 错开的那两个拉了线
    assert "按左右方向键逐个时间点查看各账号的调用次数" in svg
    assert svg.count('class="chart-endlabels"') == 0         # 有头像就不另标末值的那一组


def test_其他_没有头像_画个灰点():
    report = calls_report(cloudwatch_metrics.MetricSeries(name="kite", values=[1.0, 2.0, 3.0], slot=0),
                          cloudwatch_metrics.MetricSeries(name="其他", values=[9.0, 9.0, 9.0], slot=-1))
    svg = chart.render_lines(report, unit="次", width=900, uid="t", ends={"kite": Avatar("K", 2, False)}).svg
    ends = svg[svg.index('<g class="chart-endavatars">'):]
    assert f'r="6" fill="{chart.OTHER_COLOR}"' in ends and 'class="chart-endavatar av-2"' in ends


# ====================================================================== 「账号」横条
def test_账号卡片是按阶段的横条(admin, fake_costs):
    make(admin)
    post(admin, "/customers/C001/assign", account=[ALPHA, BETA])
    post(admin, "/customers/C001/risk", key=BETA, action="mark")
    html = detail(admin)
    start = html.index('<h2 class="viz-title">账号</h2>')
    box = html[start:html.index("</article>", start)]
    assert 'class="donut"' not in box and "2 个账号" in scrape(box)
    rows = re.findall(r'<div class="stage-row( is-zero)?"[^>]*>(.*?)</i></div>', box, re.S)
    assert [(scrape(body).split()[0], bool(zero)) for zero, body in rows] == [
        ("使用中", False), ("风控", False), ("待结算", True), ("已结算", True)]
    widths = re.findall(r'<div class="stage-bar" aria-hidden="true"><i style="width: ([\d.]+)%', box)
    assert widths == ["50.0", "50.0", "0.0", "0.0"]
    assert re.search(r'<span class="mini-av av-\d+" title="acct-two@example.com"', rows[1][1])   # 风控那一档是 B
