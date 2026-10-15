"""这一轮：

1. 账号页摘要的「告警」卡片里加告警流：这个账号发出去的 TG 告警、收到的 AWS 邮件（和客户页的告警流一个样子）；
2. 客户页「近 24 小时调用」改成「调用次数」：右上角选看多久（近 24 小时按小时，近 7 天、近 30 天、全部按天），
   还是折线图、线尾是头像，下面一条线一个签，点了挑几个只看它们、纵轴跟着它们重新算（基本的在 test_customer_calls 里）；
3. 账号页的时间线也有告警：一条一张卡、写几点几分，「用量开始 / 中断」同一天的并成一张，筛选多一个「告警」；
4. 客户页时间线「按账号筛选」的下拉右边写着生命周期（在 test_customer_polish 里）。
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

from bedrock_cost import cloudwatch_metrics, config, customer_pages
from bedrock_cost import events as alert_events

from .conftest import RANGE_START
from .test_account_pages import ALPHA as ALPHA_NO, BETA as BETA_NO, fake_estimate, get, pages  # noqa: F401  (fixture)
from .test_account_timeline import items, table
from .test_accounts import admin, post  # noqa: F401  (admin 是 fixture)
from .test_customer_calls import card as calls_card, legend, lines, per_account, real_calls  # noqa: F401  (fixture)
from .test_customer_pages import ALPHA, BETA, detail, make
from .test_web import book, page_env, scrape, usage_states  # noqa: F401  (book 是 fixture；后两个 autouse)

STATIC = Path(config.__file__).parent / "static"


def article(html: str, title: str) -> str:
    start = html.rindex("<article", 0, html.index(f'<h2 class="viz-title">{title}</h2>'))
    return html[start:html.index("</article>", start)]


# ====================================================================== 1. 摘要的告警流
def test_摘要的告警卡片里有告警流_新的在前_先露四条(pages):
    base = datetime(2026, 10, 10, 1, 0, tzinfo=timezone.utc)
    for n in range(5):
        alert_events.record("started" if n % 2 else "stopped", "用量开始" if n % 2 else "用量中断", f"第 {n} 条",
                            tone="ok" if n % 2 else "warn", account=ALPHA_NO, when=base + timedelta(hours=n))
    alert_events.record("mail-case", "AWS 工单", "Case 123", tone="info", account=ALPHA_NO, when=base + timedelta(hours=7))
    alert_events.record("daily", "Bedrock 日报", "日报", tone="ok", when=base + timedelta(hours=8))           # 不是哪个账号的
    alert_events.record("stopped", "用量中断", "别人的", tone="warn", account=BETA_NO, when=base + timedelta(hours=9))
    box = article(get(pages, f"/account/{ALPHA_NO}/"), "告警")
    assert "TG 告警" in scrape(box) and "告警流 6 条" in scrape(box)                 # 开关还在，下面是告警流
    rows = re.findall(r'<li class="t-(\w+)"\s*(hidden data-more)?>.*?<b>(.*?)</b><small>(.*?)</small>', box, re.S)
    assert [(tone, bool(more), title) for tone, more, title, _ in rows] == [
        ("info", False, "AWS 工单"), ("warn", False, "用量中断"), ("ok", False, "用量开始"), ("warn", False, "用量中断"),
        ("ok", True, "用量开始"), ("warn", True, "用量中断")]
    stamp = (base + timedelta(hours=7)).astimezone().strftime("%m-%d %H:%M")
    assert rows[0][3] == f"{stamp} · 邮件" and rows[1][3].endswith(" · TG")
    assert "更早的 2 条" in scrape(box) and "data-feed-more" in box
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    assert "closest('[data-feed-more]')" in js and "box.querySelectorAll('.feed [data-more]')" in js


def test_没发过告警的账号(pages):
    box = article(get(pages, f"/account/{ALPHA_NO}/"), "告警")
    assert "还没有发过告警" in scrape(box) and '<ul class="feed"' not in box


# ====================================================================== 2. 调用次数：选看多久
def range_links(box: str) -> list[tuple[str, str, bool]]:
    return [(scrape(label), href.replace("&amp;", "&"), bool(on)) for on, href, label in re.findall(
        r'<a class="seg-item( is-on)?" href="([^"]+)#calls" data-loading="正在读调用次数"[^>]*>(.*?)</a>', box, re.S)]


def test_调用次数右上角选看多久_和每日消费那排互相带着(admin, fake_costs):
    make(admin)
    post(admin, "/customers/C001/assign", account=[ALPHA, BETA])
    html = admin.get("/customers/C001?spend=90").get_data(as_text=True)
    assert range_links(calls_card(html)) == [
        ("近 24 小时", "/customers/C001?spend=90", True), ("近 7 天", "/customers/C001?spend=90&calls=7", False),
        ("近 30 天", "/customers/C001?spend=90&calls=30", False), ("全部", "/customers/C001?spend=90&calls=all", False)]
    html = admin.get("/customers/C001?calls=7").get_data(as_text=True)
    assert [on for *_, on in range_links(calls_card(html))] == [False, True, False, False]
    spend = re.findall(r'<a class="seg-item( is-on)?" href="([^"]+)#spend"', html)
    assert [href.replace("&amp;", "&") for _, href in spend] == [                # 客户页的每日消费默认近 7 天：那一档不带
        "/customers/C001?calls=7", "/customers/C001?calls=7&spend=30", "/customers/C001?calls=7&spend=90",
        "/customers/C001?calls=7&spend=all"]
    assert 'id="calls"' in html


def test_按天看_UTC日期_每个账号只算它在用的那几天(admin, fake_costs, real_calls):
    """风控了的账号，风控那天起的调用不算这个客户的（和「每日消费」一样）；没风控的一直算。每天 4 区 × 19 = 76 次。"""
    make(admin)
    post(admin, "/customers/C001/assign", account=[ALPHA, BETA])
    risk_day = date.today() - timedelta(days=2)
    post(admin, "/customers/C001/risk", key=ALPHA, action="mark", date=risk_day.isoformat())
    html = admin.get("/customers/C001?calls=7").get_data(as_text=True)
    utc_today = datetime.now(timezone.utc).date()
    days = [utc_today - timedelta(days=6 - i) for i in range(7)]
    assert lines(html)["labels"] == [day.strftime("%m-%d") for day in days]
    assert per_account(html) == {"acct-one": [76.0 if day < risk_day else 0.0 for day in days], "acct-two": [76.0] * 7}
    text = scrape(calls_card(html))
    assert "按 UTC 日期" in text and "次 · 近 7 天" in text and "日均" in text and "最多的一天" in text
    assert "2 / 2 个账号有调用" in text                       # 风控了的也查：这段时间里它在用过


def test_全部_从最早分过来的那个账号算起(admin, fake_costs, real_calls):
    make(admin)
    post(admin, "/customers/C001/assign", account=[ALPHA])
    html = admin.get("/customers/C001?calls=all").get_data(as_text=True)
    utc_today = datetime.now(timezone.utc).date()
    assert len(lines(html)["labels"]) == (utc_today - RANGE_START).days + 1    # 分过来的账号按启用日期（08-01）算起
    assert lines(html)["labels"][0] == RANGE_START.strftime("%m-%d")
    assert "最后一天" in html                                  # 线尾头像的悬浮：按天看的写「最后一天」


def test_按天看_查这段时间里用过的_停用的不查(admin, fake_costs, monkeypatch):
    asked = []

    def spy(accounts, window, metric_key="invocations", regions=None, refresh=False):
        asked.append((window.period_key, sorted(a.account for a in accounts)))
        stamps, labels = cloudwatch_metrics.build_grid(window)
        return cloudwatch_metrics.AccountHours(timestamps=stamps, labels=labels,
                                               values={a.key: [0.0] * len(stamps) for a in accounts})

    monkeypatch.setattr(customer_pages, "_account_calls", spy)
    make(admin)
    post(admin, "/customers/C001/assign", account=[ALPHA, BETA])
    post(admin, "/customers/C001/settle", key=ALPHA)          # 结算了：近 24 小时不查，按天看的还在这段时间里
    admin.get("/customers/C001?calls=30")
    admin.get("/customers/C001")
    alpha, beta = ALPHA.split("#")[0], BETA.split("#")[0]
    assert asked == [("1d", [alpha, beta]), ("1h", [beta])]


def test_折线图挑了几条_别的淡出_纵轴跟着它们_线按比例拉高():
    """线、面积、点、线尾头像、悬浮的点上都写着 data-s（第几条线），svg 上写着绘图区上下边和纵轴最高的刻度：
    app.js 按挑中的重新算纵轴（render_lines 一样分 4 格取整），线和面积按基线拉高（transform，线宽照旧），
    点和线尾头像挪到新的高度（头像照 chart.end_spots 重新错开）。"""
    from types import SimpleNamespace

    from bedrock_cost import chart
    from bedrock_cost.avatars import Avatar
    from bedrock_cost.windows import MetricWindow

    end = datetime(2026, 10, 10, 12, tzinfo=timezone.utc)
    window = MetricWindow(start=end - timedelta(hours=3), end=end, period_key="1h")
    stamps, labels = cloudwatch_metrics.build_grid(window)
    report = SimpleNamespace(labels=labels, timestamps=stamps, window=window, metric_label="调用次数", unit="次",
                             dimension_label="账号", series=[
                                 cloudwatch_metrics.MetricSeries(name="kite", values=[500.0, 800.0, 300.0], slot=0),
                                 cloudwatch_metrics.MetricSeries(name="kumo", values=[5.0, 8.0, 3.0], slot=1)])
    svg = chart.render_lines(report, unit="次", width=900, uid="t",
                             ends={"kite": Avatar("K", 1, False), "kumo": Avatar("K", 2, False)}).svg
    assert re.search(r'<svg class="chart-svg line-chart"[^>]* data-base="([\d.]+)" data-top="([\d.]+)" data-ymax="([\d.]+)"',
                     svg).groups() == (f"{chart.PAD_T + chart.PLOT_H:.2f}", f"{chart.PAD_T:.2f}", "800")
    for cls in ("chart-area", "chart-line", "chart-marker", "chart-endavatar[^\"]*", "chart-focus"):
        assert sorted(set(re.findall(rf'class="{cls}" data-s="(\d)"', svg))) == ["0", "1"], cls
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    assert "const step = niceStep(most || 1, 4), ticks = Math.max(1, Math.ceil((most || 1) / step - 1e-9));" in js
    assert "target = ymax / (step * ticks);" in js and "redrawAxis(grid, gridOriginal, plan, base, compactNumber);" in js
    assert "'translate(0 ' + (base * (1 - lift)).toFixed(2) + ') scale(1 ' + lift.toFixed(5) + ')'" in js
    assert "const spots = endSpots(shown.map(function (one) { return one.y; }), plotTop + AVATAR_R, base - AVATAR_R);" in js
    # 悬浮：只列挑中的，挑了一条就不写合计
    assert "!picked.size || live.length > 1 ? '合计 ' + number(total) + ' ' + data.unit : ''" in js
    css = (STATIC / "style.css").read_text(encoding="utf-8")
    assert ".line-chart .chart-line { vector-effect: non-scaling-stroke; }" in css
    assert ".line-chart[data-focus] [data-s]:not(.is-focus) { opacity: 0 !important; pointer-events: none; }" in css
    # 进场画线只在动画里用虚线那一招，画完是整条实线（拉高以后虚线会对不上）
    assert ("@keyframes line-draw { from { stroke-dasharray: 1; stroke-dashoffset: 1; } "
            "to { stroke-dasharray: 1; stroke-dashoffset: 0; } }") in css


# ====================================================================== 3. 账号页时间线的告警
def local(day: date, hour: int, minute: int = 0) -> datetime:
    return datetime.combine(day, time(hour, minute)).astimezone()


def test_账号时间线有告警_一条一张卡_用量开始中断同一天并成一张(pages):
    busy, calm = date.today() - timedelta(days=3), date.today() - timedelta(days=1)
    alert_events.record("stopped", "用量中断", "连续 1 小时没有调用", tone="warn", account=ALPHA_NO, when=local(busy, 9))
    alert_events.record("started", "用量开始", "又有调用了", tone="ok", account=ALPHA_NO, when=local(busy, 11, 5))
    alert_events.record("stopped", "用量中断", "连续 1 小时没有调用", tone="warn", account=ALPHA_NO, when=local(busy, 14, 30))
    alert_events.record("started", "用量开始", "又有调用了", tone="ok", account=ALPHA_NO, when=local(calm, 8, 15))
    alert_events.record("quota", "额度使用已达 80%", "已用 $400,000", tone="warn", account=ALPHA_NO, when=local(calm, 9))
    alert_events.record("mail-case", "AWS 工单回复", "Case 123", tone="info", account=ALPHA_NO, when=local(calm, 10))
    alert_events.record("mail-suspended", "账号暂停", "suspended", tone="error", account=ALPHA_NO, when=local(calm, 11))
    alert_events.record("daily", "Bedrock 日报", "日报", tone="ok", when=local(calm, 12))
    alert_events.record("stopped", "用量中断", "别人的", tone="warn", account=BETA_NO, when=local(calm, 13))
    html = get(pages, f"/account/{ALPHA_NO}/timeline")
    found = [item for item in items(html) if item["cat"] == "alert"]
    assert [(item["date"], item["title"], item["text"], item["color"], item["icon"]) for item in found] == [
        (busy.isoformat(), "用量中断、开始", "中断 2 次、开始 1 次 · 最后一次 14:30 中断", "warn", "send"),
        (calm.isoformat(), "用量开始", "08:15 · 又有调用了", "up", "send"),
        (calm.isoformat(), "额度使用已达 80%", "09:00 · 已用 $400,000", "warn", "send"),
        (calm.isoformat(), "AWS 工单回复", "10:00 · Case 123", "misc", "mail"),
    ]
    # 风控邮件照旧是风控那一类的「AWS 邮件」，不重复
    mails = [item for item in items(html) if item["kind"] == "mail"]
    assert len(mails) == 1 and mails[0]["cat"] == "risk"
    chips = re.findall(r'<button class="fchip" type="button" data-value="(\w*)"', html)
    assert chips == ["", "acct", "use", "risk", "money", "misc", "alert"]
    who = {row[1]: row[4] for row in table(html)}
    assert who["额度使用已达 80%"] == "TG 告警" and who["AWS 工单回复"] == "AWS 邮件"
    assert '<template data-icon="send">' in html
    assert "告警是发出去（收到）的时候记下来的，改不了日期，也删不掉。" in html


def test_客户页的时间线没有告警这一类(admin, fake_costs):
    make(admin)
    html = detail(admin)
    assert 'data-value="alert"' not in html
