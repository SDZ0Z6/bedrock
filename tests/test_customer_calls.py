"""客户页「调用次数」（原来叫「近 24 小时调用」）：账单晚 1~2 天，这张看现在——CloudWatch 的调用次数（四区合计）。
折线图，一个账号一条线，线尾是这个账号的头像；颜色和「每日消费」一样。下面一条线一个签，点了挑几个只看它们
（纵轴跟着它们重新算，app.js）。右上角选看多久：近 24 小时（默认）每小时一个点、只查还在用的账号；近 7 天、
近 30 天、全部每天一个点（UTC 日期），查这段时间在这个客户名下用过的账号，每个账号只算它在用的那几天。客户页的「账号」卡片是按阶段的横条（长短按占全部账号的比例，后面是这一档的账号头像）。

账号页摘要页签的「近 24 小时调用」（按模型的折线图，没改）下面补一行「今天预估花费」（用量 × 牌价，和「预估」页签一个算法）。

客户页的「预估」（原来是「还能用几天」）：近 7 天每天的预估花费（CloudWatch 的 token × 牌价，UTC 日期，今天算到现在），
按账号叠起来，颜色和「每日消费」一样，每个账号只算它在用的那几天；和「调用次数」上的今日预估一起查一次。
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
    """换回真的按账号汇总（conftest 默认换成了全 0），CloudWatch 用 fake_cloudwatch：每个区每格 12 + 7 = 19 次。"""
    monkeypatch.setattr(customer_pages, "_account_calls", cloudwatch_metrics.account_totals)


def card(html: str) -> str:
    start = html.index(">调用次数<")
    return html[start:html.index("</article>", start)]


def legend(html: str, holder: str) -> dict[str, str]:
    """柱状图的图例：{名字: 颜色}。"""
    block = re.search(rf'<div class="legend-toggles" data-legend-for="{holder}"[^>]*>(.*?)</div>', html, re.S).group(1)
    return {scrape(name): color for color, name in
            re.findall(r'<i class="swatch" style="background: ([^"]+)"></i>(.*?)</button>', block, re.S)}


def lines(html: str) -> dict:
    """折线图交给悬浮脚本的数据：{unit, labels, x, series: [{name, color, v, y}]}。"""
    return json.loads(re.search(r'<script id="cust-calls-data" type="application/json">(.*?)</script>', html, re.S).group(1))


def per_account(html: str) -> dict[str, list[float]]:
    """{名字: 每个点的调用次数}。"""
    return {s["name"]: s["v"] for s in lines(html)["series"]}


def quiet_except(monkeypatch, busy: dict[str, float]):
    """按号码给每格的调用次数，没写的账号一直是 0。"""
    def fake(accounts, window, metric_key="invocations", regions=None, refresh=False):
        stamps, labels = cloudwatch_metrics.build_grid(window)
        return cloudwatch_metrics.AccountHours(
            timestamps=stamps, labels=labels,
            values={a.key: [busy.get(a.account, 0.0)] * len(stamps) for a in accounts})

    monkeypatch.setattr(customer_pages, "_account_calls", fake)


# ====================================================================== 客户页
def test_默认近24小时_一个账号一条线_线尾是头像_下面的签能挑(admin, fake_costs, real_calls):
    make(admin)
    post(admin, "/customers/C001/assign", account=[ALPHA, BETA])
    html = detail(admin)
    text = scrape(card(html))
    assert "3.6K 次 · 近 24 小时" in text                     # 2 个账号 × 4 个区 × 19 次 × 24 小时
    assert "最近一小时 152" in text and "2 / 2 个在用的账号有调用" in text
    data = lines(html)
    assert data["unit"] == "次" and len(data["labels"]) == 24
    assert per_account(html) == {"acct-one": [76.0] * 24, "acct-two": [76.0] * 24}
    # 线尾：两个头像（字母和 .av-N 的颜色），末值一样也上下错开，拉一根细线连回线尾（没错开的那根看不见）
    ends = re.search(r'<g class="chart-endavatars">(.*?)</g></g>', html, re.S).group(1)
    assert len(re.findall(r'<g class="chart-endavatar av-\d+" data-s="\d"', ends)) == 2
    assert ends.count('style="fill: var(--av-bg)"') == 2
    assert re.findall(r'class="end-link"[^>]*stroke-opacity="([^"]+)"', ends) == ["0", ".55"]
    # 下面一条线一个签，和线同一个顺序（data-s），悬浮看次数和今日预估
    box = card(html)
    assert 'id="cust-calls" data-line-src="cust-calls-data"' in box
    assert list(legend(box, "cust-calls")) == [s["name"] for s in data["series"]]
    tips = re.findall(r'<button class="legend-toggle"[^>]*title="([^"]+)"', box)
    assert "acct-one@example.com · 近 24 小时 1,824 次 · 今日 $0.00" in tips


def test_和每日消费换了位置_在预估左边(admin, fake_costs):
    """调用次数和「预估」（原来是「还能用几天」）一排（占三分之二宽）；每日消费挪到图的最后，整行宽。"""
    make(admin)
    html = detail(admin)
    assert (html.index(">调用次数<") < html.index('<h2 class="viz-title">预估</h2>') < html.index(">每日消费<")
            < html.index('id="tl-title"'))
    assert ">还能用几天<" not in html
    calls = html.rindex("<article", 0, html.index(">调用次数<"))
    spend = html.rindex("<article", 0, html.index(">每日消费<"))
    assert html.startswith('<article class="viz-card col-4" id="calls"', calls)
    assert html.startswith('<article class="viz-card col-6" id="spend"', spend)


def test_和每日消费一个颜色(admin, fake_costs, real_calls, monkeypatch):
    from bedrock_cost import usage_explorer

    from .test_customers import series_for

    today = datetime.now().date()
    spend = {ALPHA_NO: {(today - timedelta(days=i)).isoformat(): 100.0 for i in range(1, 10)},
             BETA_NO: {(today - timedelta(days=i)).isoformat(): 300.0 for i in range(1, 10)}}
    monkeypatch.setattr(usage_explorer, "account_series", series_for(spend))
    make(admin)
    post(admin, "/customers/C001/assign", account=[ALPHA, BETA])
    html = detail(admin)
    assert legend(html, "cust-calls") == legend(html, "cust-spend")


def test_只查还在用的账号(admin, fake_costs, monkeypatch):
    asked = []

    def spy(accounts, window, metric_key="invocations", regions=None, refresh=False):
        asked.append(sorted(a.account for a in accounts))
        stamps, labels = cloudwatch_metrics.build_grid(window)
        return cloudwatch_metrics.AccountHours(timestamps=stamps, labels=labels,
                                               values={a.key: [0.0] * len(stamps) for a in accounts})

    monkeypatch.setattr(customer_pages, "_account_calls", spy)
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

    monkeypatch.setattr(customer_pages, "_account_calls", broken)
    make(admin)
    post(admin, "/customers/C001/assign", account=[ALPHA])
    html = detail(admin)
    assert "读不到 CloudWatch" in html and "凭证缺少 cloudwatch 权限" in html


def test_一直没调用的不画线_在图下面点名(admin, fake_costs, monkeypatch):
    quiet_except(monkeypatch, {ALPHA_NO: 30.0})
    make(admin)
    post(admin, "/customers/C001/assign", account=[ALPHA, BETA])
    html = detail(admin)
    assert [s["name"] for s in lines(html)["series"]] == ["acct-one"] == list(legend(html, "cust-calls"))
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
    calls = scrape(html[html.index(">近 24 小时调用<"):html.index(">配额</h2>")])
    assert "今天预估花费 $20.00" in calls and "以账单为准" in calls
    assert f'href="/account/{ALPHA_NO_PAGES}/estimate"' in html


def test_近24小时没有调用_今天就不用估了(pages, monkeypatch):
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
    ys = [float(y) for y in re.findall(r'<circle class="end-face" cx="[\d.]+" cy="([\d.]+)" r="10"', svg)]
    assert len(ys) == 3 and all(b - a >= chart.AVATAR_GAP - 0.01 for a, b in zip(sorted(ys), sorted(ys)[1:]))
    # 错开的那两个拉了线；没错开的那根也画上、看不见（纵轴缩放以后可能要用）
    assert re.findall(r'class="end-link"[^>]*stroke-opacity="([^"]+)"', svg) == ["0", ".55", ".55"]
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
    widths = re.findall(r'<div class="stage-bar" data-reveal aria-hidden="true"><i style="width: ([\d.]+)%', box)
    assert widths == ["50.0", "50.0", "0.0", "0.0"]
    assert re.search(r'<span class="mini-av av-\d+" data-tip="acct-two@example.com"', rows[1][1])   # 风控那一档是 B


# ====================================================================== 今日预估
def estimates(monkeypatch, daily: dict[str, list[float]], partial: bool = False) -> list:
    """「预估」那一次查询换成假的：按号码给近 7 天每天的钱（最后一个是今天），没写的账号每天 0。返回问过的
    [(号码们, 起, 止)]。"""
    asked = []

    def fake(accounts, start, end, refresh=False):
        asked.append((sorted(a.account for a in accounts), start, end))
        width = (end - start).days + 1
        return {a.key: list(daily.get(a.account, [0.0] * width)) for a in accounts}, partial

    monkeypatch.setattr(customer_pages, "_estimate_days", fake)
    return asked


def test_标题下面只写时间_旁边是今日预估(admin, fake_costs, real_calls, monkeypatch):
    asked = estimates(monkeypatch, {ALPHA_NO: [0.0] * 6 + [12.5], BETA_NO: [0.0] * 6 + [7.5]})
    make(admin)
    post(admin, "/customers/C001/assign", account=[ALPHA, BETA])
    box = card(detail(admin))
    sub = scrape(re.search(r'<p class="viz-sub">(.*?)</p>', box, re.S).group(1))
    assert re.fullmatch(r"\d\d-\d\d \d\d:00 ~ \d\d-\d\d \d\d:00", sub)       # 那句说明拿掉了
    assert "$20.00 今日预估" in scrape(box) and "有的没算上" not in box
    tips = re.findall(r'<button class="legend-toggle"[^>]*title="([^"]+)"', box)
    assert "acct-one@example.com · 近 24 小时 1,824 次 · 今日 $12.50" in tips
    # 和「预估」那张一起查一次：近 7 天，按 UTC 日期
    utc_today = datetime.now(timezone.utc).date()
    assert asked == [(sorted([ALPHA_NO, BETA_NO]), utc_today - timedelta(days=6), utc_today)]


def test_今日预估就是预估那张的今天(admin, fake_costs, monkeypatch):
    """同一份数：BETA 近 24 小时没画线（这一小时才开始用的也是这样），今天的预估照样算进去，两张对得上。"""
    quiet_except(monkeypatch, {ALPHA_NO: 30.0})
    estimates(monkeypatch, {ALPHA_NO: [1.0] * 7, BETA_NO: [1.0] * 7}, partial=True)
    make(admin)
    post(admin, "/customers/C001/assign", account=[ALPHA, BETA])
    html = detail(admin)
    assert "$2.00 今日预估（有的没算上）" in scrape(card(html))
    assert "今天到现在 $2.00" in scrape(estimate_card(html))


def test_每个账号各估各的_刷新只清一次缓存(ledger, fake_estimate, monkeypatch):
    """fake_estimate：每个账号每个区每天 100 万输入 token 的 Opus 5（$5/M），四个区一天 $20。"""
    from datetime import date

    from bedrock_cost import excel_source

    accounts = excel_source.load_accounts(force=True)
    cleared = []
    real_clear = cost_estimate.clear_cache
    monkeypatch.setattr(cost_estimate, "clear_cache", lambda: (cleared.append(1), real_clear()))
    end = date.today()
    costs, partial = customer_pages.daily_estimates(accounts, end - timedelta(days=6), end, refresh=True)
    assert costs == {a.key: [pytest.approx(20.0)] * 7 for a in accounts} and partial is False
    assert len(cleared) == 1


# ====================================================================== 预估（原来是「还能用几天」）
def estimate_card(html: str) -> str:
    start = html.index('<h2 class="viz-title">预估</h2>')
    return html[html.rindex("<article", 0, start):html.index("</article>", start)]


def bars(html: str) -> list[dict]:
    """「预估」柱状图交给悬浮脚本的数据：一天一项 {label, total, rows: [{name, color, v}]}。"""
    return json.loads(re.search(r'<script id="cust-estimate-data" type="application/json">(.*?)</script>',
                                html, re.S).group(1))


def test_预估_近7天每天一根_按账号叠_今天算到现在(admin, fake_costs, monkeypatch):
    from bedrock_cost import usage_explorer

    from .test_customers import series_for

    today = datetime.now().date()
    spend = {number: {(today - timedelta(days=i)).isoformat(): 100.0 for i in range(1, 10)} for number in (ALPHA_NO, BETA_NO)}
    monkeypatch.setattr(usage_explorer, "account_series", series_for(spend))     # 两个都在「每日消费」的图例里
    estimates(monkeypatch, {ALPHA_NO: [10.0, 20.0, 30.0, 40.0, 50.0, 60.0, 70.0], BETA_NO: [1.0] * 7})
    make(admin)
    post(admin, "/customers/C001/assign", account=[ALPHA, BETA])
    html = detail(admin)
    box = estimate_card(html)
    assert box.startswith('<article class="viz-card col-2" id="estimate"')
    text = scrape(box)
    assert "$287 近 7 天预估" in text and "有的没算上" not in text
    assert "今天到现在 $71.00 · 昨天 $61.00" in text
    utc_today = datetime.now(timezone.utc).date()
    first = (utc_today - timedelta(days=6)).strftime("%m-%d")
    assert f"{first} ~ {utc_today.strftime('%m-%d')} · CloudWatch 用量 × 牌价，按 UTC 日期" in text
    days = bars(html)
    assert len(days) == 7 and days[0]["label"] == first and days[-1]["label"] == "今天"
    assert [row["v"] for row in days[-1]["rows"]] == [70.0, 1.0]
    # 颜色和「每日消费」一样：同一个账号同一个颜色
    spend = legend(html, "cust-spend")
    assert all(spend[row["name"]] == row["color"] for day in days for row in day["rows"])
    # 底下一个账号一个签（花得多的在前），点了挑几个只看它们——和每日消费、调用次数一样（app.js）
    picks = legend(box, "cust-estimate")
    assert list(picks) == ["acct-one", "acct-two"] and all(spend[name] == color for name, color in picks.items())
    tips = re.findall(r'<button class="legend-toggle"[^>]*title="([^"]+)"', box)
    assert tips == ["acct-one 近 7 天预估 $280.00", "acct-two 近 7 天预估 $7.00"]


def test_预估_只算在这个客户名下的那几天(admin, fake_costs, monkeypatch):
    """前天把 ALPHA 换成 BETA：ALPHA 从前天起不算（和每日消费一个规矩）；BETA 的启用日期更早，按启用日期算。"""
    estimates(monkeypatch, {ALPHA_NO: [100.0] * 7, BETA_NO: [1.0] * 7})
    make(admin)
    post(admin, "/customers/C001/assign", account=[ALPHA])
    swapped = datetime.now(timezone.utc).date() - timedelta(days=2)
    post(admin, "/customers/C001/replace", old=ALPHA, new=BETA, reason="风控", date=swapped.isoformat())
    html = detail(admin)
    assert "$407 近 7 天预估" in scrape(estimate_card(html))           # ALPHA 4 天 × $100 + BETA 7 天 × $1
    days = bars(html)
    assert [row["v"] for row in days[3]["rows"]] == [100.0, 1.0] and [row["v"] for row in days[4]["rows"]] == [1.0]


def test_预估_停用的账号不查(admin, fake_costs, monkeypatch):
    from bedrock_cost import excel_source

    asked = estimates(monkeypatch, {})
    make(admin)
    post(admin, "/customers/C001/assign", account=[ALPHA, BETA])
    excel_source.set_enabled(BETA, False, actor="tester")
    detail(admin)
    assert asked[-1][0] == [ALPHA_NO]


def test_预估_有的没算上_两张都说一声(admin, fake_costs, real_calls, monkeypatch):
    estimates(monkeypatch, {ALPHA_NO: [2.0] * 7}, partial=True)
    make(admin)
    post(admin, "/customers/C001/assign", account=[ALPHA])
    html = detail(admin)
    assert "$14 近 7 天预估（有的没算上）" in scrape(estimate_card(html))
    assert "$2.00 今日预估（有的没算上）" in scrape(card(html))


def test_预估_没有账号的客户(admin, fake_costs):
    make(admin)
    html = detail(admin)
    text = scrape(estimate_card(html))
    assert "$0 近 7 天预估" in text and "今天到现在 $0.00 · 昨天 $0.00" in text and "这段时间没有数据" in text
    assert 'data-legend-for="cust-estimate"' not in html                 # 没有账号就没有签
