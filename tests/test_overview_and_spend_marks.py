"""第六轮反馈：

1. 概览「额度使用」改成和客户页「账号」一样的横条：按生命周期（风控、正常……）一档一根，后面是这一档的账号头像，
   鼠标放上去立刻弹出全名（app.js 的 data-tip；不用 title，浏览器自带的要停一会儿才出来）；
2. 概览加「Top 账号」：按累计消费排，默认前 5 个，点「全部」看剩下的；
3. 客户页「额度使用」拿掉，「余额」加上已使用多少；
4. 客户页「近 24 小时调用」和「每日消费」换位（在 test_customer_calls 里）；
5. 「每日消费」不写日均；每个账号的启用、风控、换下、用完、结算、恢复画成竖虚线，「停」的那条写着从启用起用了多少；
   右上角选看多久（近 30 天 / 近 90 天 / 全部），点图例只看一个账号时只留它的竖线；
6~9. 账号页：摘要的调用改成近 24 小时，成本、预估默认近 7 天，用量默认近 24 小时（test_account_pages 也改了）。

接着又改了两处：
- 账号页摘要、概览的「近 30 天成本」统一叫「每日消费」；账号页的也画竖虚线（启用、分给客户、风控……）；
- 点图例只看一个账号：别人的段子消失，它堆在上面的段子「跌」到底（app.js 算往下挪多少，style.css 管动画）。

再一轮：
- 所有进度条滚进屏幕才从左往右长出来（每根条上都有 data-reveal，app.js 加 .in 之前动画停在第一帧）；
- 概览、客户列表、客户页、账号页摘要的图都叫「每日消费」，右上角一排：近 7 天、近 30 天（默认）、近 90 天、全部。
  后来客户页的默认改成了近 7 天（别的页面还是近 30 天）。

再一轮：图例能挑几个账号（点一个加一个，再点放回去，挑了以后有「显示全部」），挑中的从底下重新叠起来。
账号页成本明细不并「其他」（在 test_account_pages、test_usage_explorer 里）。

再一轮：图例挑了几项时纵轴跟着它们重新算（去掉大的以后小的也看得清），app.js 和 chart.py 的算法要一样
（test_chart_js_parity 用 node 对一遍）。
"""

from __future__ import annotations

import json
import re
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from bedrock_cost import chart, config, customers, dashboard
from bedrock_cost.avatars import Avatar

from .conftest import RANGE_START
from .test_account_pages import ALPHA as ALPHA_NO_PAGES, fake_estimate, get, pages, range_label  # noqa: F401  (fixture)
from .test_accounts import admin, post  # noqa: F401  (admin 是 fixture)
from .test_customer_pages import ALPHA, BETA, detail, make
from .test_web import book, page_env, scrape, usage_states  # noqa: F401  (book 是 fixture；后两个 autouse)

STATIC = Path(config.__file__).parent / "static"
ALPHA_DAILY = 100 * 1 + 50 * 1.05            # fake_costs：alpha 每天折算后的消费


def overview(client) -> str:
    return client.get("/").get_data(as_text=True)


def card(html: str, title: str) -> str:
    start = html.rindex("<article", 0, html.index(f">{title}</h2>"))
    return html[start:html.index("</article>", start)]


# ====================================================================== 1. 额度使用：按生命周期的横条
def test_额度使用是按生命周期的横条(logged_in, book, fake_costs):
    box = card(overview(logged_in), "额度使用")
    assert "gauge" not in box and "快用完的账号" not in box
    rows = re.findall(r'<div class="stage-row( is-zero)?"[^>]*>(.*?)</i></div>', box, re.S)
    # alpha 正常、beta 风控；「结算」没人用也列出来（灰的），和下面的筛选签一样
    assert [(scrape(body).split()[0], bool(zero)) for zero, body in rows] == [
        ("正常", False), ("结算", True), ("风控", False)]
    bars = re.findall(r'<div class="stage-bar" data-reveal aria-hidden="true"><i style="width: ([\d.]+)%; background: ([^"]+)"', box)
    assert bars == [("50.0", "#2c7652"), ("0.0", "#87867f"), ("50.0", "#bc3b2e")]   # 颜色是标签的颜色
    # 头像上是全名，下面一行是号码和额度（不写使用率）
    faces = [re.findall(r'<span class="mini-av av-\d+" data-tip="([^"]+)"\s+data-tip-sub="([^"]+)">', body)
             for _, body in rows]
    assert [[(email, sub.split(" · ")[0]) for email, sub in row] for row in faces] == [
        [("alpha@example.com", "111111111111")], [], [("beta@example.com", "222222222222")]]
    assert re.fullmatch(r"额度 \$[\d,]+", faces[0][0][1].split(" · ")[1]) and "使用率" not in box[box.index("stage-bars"):]
    assert 'title="' not in rows[0][1]


def test_悬浮提示_鼠标放上去立刻弹出():
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    assert "closest('[data-tip]')" in js and "el.dataset.tipSub" in js and "el.dataset.tipFoot" in js


# ====================================================================== 2. 每月账号和消费（原来的 Top 账号）
def _acct(number: str, start: date | None, enabled: bool = True):
    return SimpleNamespace(account=number, start_date=start, enabled=enabled)


def _event(number: str, day: date, kind: str = "disable", deleted: bool = False):
    return SimpleNamespace(account=number, date=day, type=kind, deleted=deleted, source="manual")


def test_每月账号和消费_新加正常停用_消费含停用账号存下来的账(tmp_path, monkeypatch):
    """一个月一个账号只算一段：启用日期在这个月是新加；这个月停的是停用（EVENTS 里记的，没记的看操作日志）；
    之前就在用、这个月也一直开着的是正常。"""
    from bedrock_cost import cost_history, excel_source

    today = date(2026, 10, 15)
    accounts = [
        _acct("100000000001", date(2026, 3, 5)),               # 3 月新加，在用
        _acct("100000000002", date(2026, 8, 1)),               # 8 月新加，在用
        _acct("100000000003", date(2026, 8, 20), False),       # 8 月新加，9 月停用（EVENTS 里记了）
        _acct("100000000004", date(2025, 6, 1), False),        # 一年多以前加的，一直正常；操作日志里 7 月停用
        _acct("100000000005", None, False),                    # 没填启用日期，也不知道哪天停的
    ]
    events = [
        _event("100000000003", date(2026, 9, 2)),
        _event("100000000003", date(2026, 9, 20)),             # 一个月里停了两回：算一个
        _event("100000000002", date(2026, 9, 9), deleted=True),  # 删掉的不算
        _event("100000000001", date(2026, 9, 9), kind="note"),
        _event("999999999999", date(2026, 9, 9)),              # 台账里已经没有的号码不算
    ]
    # 2026-10-10 之前停的没进 EVENTS：去操作日志里找最近一回「停用账号」（老写法、新写法都认号码）
    monkeypatch.setattr(config, "EXCEL_PATH", tmp_path / "cred.xlsx")
    (tmp_path / excel_source.AUDIT_NAME).write_text(
        "2026-05-01 09:00:00\tadmin\t停用账号 100000000004（Jeff）\n"
        "2026-06-01 09:00:00\tadmin\t恢复账号 old@example.com（100000000004）\n"
        "2026-07-03 10:00:00\tadmin\t停用账号 old@example.com（100000000004）\t127.0.0.1\n", encoding="utf-8")
    # 停用的账号：客户页存下来的每天的账（8-20 起每天 10 块，到 9-10）
    days = [date(2026, 8, 20) + timedelta(days=i) for i in range(22)]
    cost_history.remember("100000000003", [d.isoformat() for d in days], [8.0] * 22, [10.0] * 22)
    rows = [SimpleNamespace(monthly={"2026-09": 100.0, "2026-10": 50.0, "2025-01": 999.0}, error=None),
            SimpleNamespace(monthly={}, error="查不到")]

    months = dashboard.account_months(rows, accounts, events, today)
    # 004 去年 6 月就在用，近 12 个月每个月都有账号：一个月都不省
    assert months.keys == [f"2025-{m:02d}" for m in (11, 12)] + [f"2026-{m:02d}" for m in range(1, 11)]
    assert months.labels == ["11月", "12月"] + [f"{m}月" for m in range(1, 11)]
    assert months.years == ["2025", "", "2026"] + [""] * 9
    assert months.tips[-1] == "2026 年 10 月（到今天）"
    #                        11 12  1  2  3  4  5  6  7  8  9 10
    assert months.added ==    [0, 0, 0, 0, 1, 0, 0, 0, 0, 2, 0, 0]
    assert months.normal ==   [1, 1, 1, 1, 1, 2, 2, 2, 1, 1, 2, 2]     # 004 停之前、001 新加之后、002 新加之后
    assert months.disabled == [0, 0, 0, 0, 0, 0, 0, 0, 1, 0, 1, 0]     # 004 七月（操作日志）、003 九月（停了两回算一个）
    assert months.spend == [0.0] * 9 + [120.0, 200.0, 50.0]          # 8 月 12 天 × 10；9 月 100 + 10 天 × 10
    assert (months.total_added, months.total_disabled, months.total_spend, months.in_use) == (3, 2, 370.0, 2)
    assert months.notes == ["1 个停用的账号不知道哪天停的，没算进去", "2 个停用的账号没存过每天的账，消费里没有它们",
                            "1 个账号这次没查到 Cost Explorer，消费里少了它们", "1 个账号没填启用日期，没算进新加、正常"]


def test_每月账号和消费_同一个月新加又停了算停用():
    accounts = [_acct("100000000001", date(2026, 9, 3), False), _acct("100000000001", date(2026, 9, 1), False)]
    months = dashboard.account_months([], accounts, [_event("100000000001", date(2026, 9, 25))], date(2026, 10, 15))
    assert (months.added[-2], months.normal[-2], months.disabled[-2]) == (0, 0, 1)   # 同一个号码两行：按一个算
    assert (months.added[-1], months.normal[-1], months.disabled[-1]) == (0, 0, 0)


def test_每月账号和消费_至少六个月_跨年写年份():
    months = dashboard.account_months([], [_acct("100000000001", date(2026, 12, 3))], [], date(2027, 2, 10))
    assert months.labels == ["9月", "10月", "11月", "12月", "1月", "2月"]
    assert months.years == ["2026", "", "", "", "2027", ""]
    assert months.added == [0, 0, 0, 1, 0, 0] and months.normal == [0, 0, 0, 0, 1, 1] and months.notes == []


def test_每月账号和消费的图_柱子看左边_线看右边():
    months = SimpleNamespace(labels=["8月", "9月", "10月"], years=["2026", "", ""],
                             tips=["2026 年 8 月", "2026 年 9 月", "2026 年 10 月（到今天）"],
                             added=[2, 0, 1], normal=[3, 0, 5], disabled=[1, 0, 0], spend=[1200.0, 900.0, 300.0])
    drawn = chart.render_month_combo(months)
    svg = drawn.svg
    assert 'class="chart-svg bar-chart month-combo"' in svg and ">账号（个）<" in svg and ">消费<" in svg
    assert svg.count('<g class="bar-col"') == 3                                  # 没有柱子的那个月也占一格（悬浮对得上）
    # 8 月三段（正常在底下、新加、停用在最上面，顶上那段是圆头的 path），10 月两段
    col = svg[svg.index('<g class="bar-col" data-idx="0"'):svg.index('<g class="bar-col" data-idx="1"')]
    assert re.findall(r'<(rect|path) data-s="(\d)"', col) == [("rect", "0"), ("rect", "1"), ("path", "2")]
    assert chart.MONTH_STACK[0][1] == "正常" and chart.MONTH_COLORS["normal"] == chart.TONE_COLORS["ok"]   # 和额度使用里的「正常」一个绿
    # 左右两根纵轴，刻度落在同一排网格线上：左边是整数个数，右边是金额
    left = re.findall(r'<text x="32" y="([\d.]+)" text-anchor="end"[^>]*>(\d+)</text>', svg)
    right = re.findall(r'<text x="458" y="([\d.]+)" fill=[^>]*>([^<]+)</text>', svg)
    assert [y for y, _ in left] == [y for y, _ in right] and len(left) >= 3
    assert [int(n) for _, n in left] == [0, 2, 4, 6] and right[0][1] == "$0" and right[-1][1].startswith("$")
    # 线压在柱子上：底下垫一道 5px 底色的边；没过完的这个月空心
    assert f'stroke="{chart.SURFACE}" stroke-width="5"' in svg
    circles = re.findall(r'<circle [^>]*fill="([^"]+)" stroke="([^"]+)"', svg)
    assert [fill for fill, _ in circles] == [chart.MONTH_COLORS["spend"]] * 2 + [chart.SURFACE]
    assert drawn.tooltip[0] == {"label": "2026 年 8 月", "head": "", "rows": [
        {"name": "消费", "color": chart.MONTH_COLORS["spend"], "value": "$1,200"},
        {"name": "新加", "color": chart.MONTH_COLORS["added"], "value": "2 个"},
        {"name": "正常", "color": chart.MONTH_COLORS["normal"], "value": "3 个"},
        {"name": "停用", "color": chart.MONTH_COLORS["disabled"], "value": "1 个"}]}
    assert [item["name"] for item in drawn.legend] == ["新加", "正常", "停用", "消费"]
    assert chart.render_month_combo(SimpleNamespace(labels=[], years=[], tips=[], added=[], normal=[], disabled=[],
                                                    spend=[])).empty


def test_概览_Top账号换成每月账号和消费(logged_in, book, fake_costs):
    html = overview(logged_in)
    assert "Top 账号" not in html and 'id="top-card"' not in html
    assert html.index(">消费构成<") < html.index(">每月账号和消费<") < html.index(">每日消费<")
    box = card(html, "每月账号和消费")
    assert 'class="viz-card span-2" id="months"' in box and 'data-tip-src="months-data"' in box
    tips = json.loads(re.search(r'<script id="months-data" type="application/json">(.*?)</script>', box, re.S).group(1))
    assert len(tips) >= 6 and all(item["head"] == "" for item in tips)
    assert [row["name"] for row in tips[-1]["rows"]] == ["消费", "新加", "正常", "停用"]
    # 这个月到今天：alpha（每天 100 + 50 × 1.05）、beta（100 + 50 × 1.1）在用；gamma 停用了、没存过账，不算
    today = date.today()
    assert tips[-1]["label"] == f"{today.year} 年 {today.month} 月（到今天）"
    assert tips[-1]["rows"][0]["value"] == f"${today.day * 307.5:,.0f}"
    # gamma 停用了，不知道哪天停的（EVENTS、操作日志里都没有），也没存过每天的账：写在图下面
    note = scrape(box[box.index('<p class="combo-note">'):])
    assert "1 个停用的账号不知道哪天停的" in note and "1 个停用的账号没存过每天的账" in note
    assert [scrape(key) for key in re.findall(r'<span class="combo-key is-\w+">(.*?)</span>', box)] == ["新加", "正常", "停用", "消费"]
    # 这个月在用：alpha、beta 正常（gamma 停用了）
    assert "这个月在用 2 个" in scrape(box)


# ====================================================================== 3. 余额加上已使用
def test_余额卡片写着已使用多少_额度使用拿掉了(admin, fake_costs):
    make(admin)
    post(admin, "/customers/C001/assign", account=[ALPHA, BETA])
    html = detail(admin)
    bento = html[html.index('<section class="detail-bento"'):html.index('id="tl-title"')]
    assert [scrape(t) for t in re.findall(r'<h2 class="viz-title">(.*?)</h2>', bento)] == [
        "账号", "余额", "调用次数", "预估", "每日消费"]
    box = card(html, "余额")
    assert 'class="viz-card col-4"' in box and 'class="bal-bar bal-bar-total' in box
    budget, used, left = (int(n.replace(",", "")) for n in re.search(
        r"预算 <strong>\$([\d,]+)</strong> = 已使用 <strong>\$([\d,]+)</strong>\s*\+ 余额 <strong[^>]*>\$([\d,]+)</strong>",
        box).groups())
    assert budget == 600_000 and abs(used + left - budget) <= 1
    assert f"${used:,} 已使用" in scrape(box) and f"${left:,} 余额" in scrape(box)
    assert scrape(box).count("已使用 $") == 3                                    # 合计一行 + 两个账号各一行


def test_余额里的账号点了进它的账号页(admin, fake_costs):
    """使用中的每一行整行是一个链接（a.bal-row）；下面不算进余额的（风控、待结算）邮箱也能点。"""
    make(admin)
    post(admin, "/customers/C001/assign", account=[ALPHA, BETA])
    post(admin, "/customers/C001/risk", key=BETA, action="mark", date=date.today().isoformat())
    box = card(detail(admin), "余额")
    rows = re.findall(r'<a class="bal-row" style="--i: \d+" href="([^"]+)"\s+data-loading="正在读这个账号"', box)
    assert rows == [f"/account/{ALPHA.split('#')[0]}/"]
    note = box[box.index('<p class="bal-note">'):]
    assert f'<a href="/account/{BETA.split("#")[0]}/" data-loading="正在读这个账号"' in note
    assert "风控 · 待替换，没用完的" in scrape(note)


# ====================================================================== 5. 每日消费
def spend_card(html: str) -> str:
    return card(html, "每日消费")


def bars(html: str) -> int:
    return len(re.findall(r'<g class="bar-col"', spend_card(html)))


def test_每日消费_不写日均_看多久跟着上面那一排(admin, fake_costs):
    """客户页的每日消费没有自己的那一排了，跟着三张图共用的时间走（默认近 7 天，见 test_customer_range）。"""
    make(admin)
    post(admin, "/customers/C001/assign", account=[ALPHA])
    html = detail(admin)
    box = spend_card(html)
    assert "日均" not in box and 'id="spend"' in box and 'class="seg' not in box
    assert bars(html) == 7 and "近 7 天" in scrape(box)
    for key, count in (("30", 30), ("90", 90), ("nonsense", 7), ("24h", 2)):
        assert bars(admin.get(f"/customers/C001?range={key}").get_data(as_text=True)) == count
    # 全部：从最早分过来的那个账号算起（ALPHA 的启用日期 08-01）
    everything = admin.get("/customers/C001?range=all").get_data(as_text=True)
    assert bars(everything) == (date.today() - RANGE_START).days + 1
    assert f"{RANGE_START.strftime('%m-%d')} 起全部" in scrape(spend_card(everything))


def switch(box: str) -> list[tuple[str, str, bool]]:
    """右上角那一排：[(名字, 网址, 是不是选中的)]"""
    return [(scrape(label), href.replace("&amp;", "&"), bool(on)) for on, href, label in re.findall(
        r'<a class="seg-item( is-on)?" href="([^"]+)#spend" data-loading="正在读每日消费"[^>]*>(.*?)</a>', box, re.S)]


def test_每日消费看多久():
    today = date(2026, 10, 11)
    picks = {key: dashboard.spend_range(key, today, date(2026, 6, 1)) for key in ("7", "30", "90", "all", "", "x")}
    assert [(r.key, r.days, r.start, r.label) for r in picks.values()] == [
        ("7", 7, date(2026, 10, 5), "近 7 天"), ("30", 30, date(2026, 9, 12), "近 30 天"),
        ("90", 90, date(2026, 7, 14), "近 90 天"), ("all", 133, date(2026, 6, 1), "06-01 起全部"),
        ("30", 30, date(2026, 9, 12), "近 30 天"), ("30", 30, date(2026, 9, 12), "近 30 天"),   # 认不出：默认近 30 天
    ]
    # 客户页认不出的是近 7 天
    assert dashboard.spend_range("x", today, None, default="7").key == "7"
    # 全部：不早于 Cost Explorer 能查的最早一天；不知道从哪天起就和近 30 天一样长
    from bedrock_cost.dates import earliest_queryable
    assert dashboard.spend_range("all", today, date(2020, 1, 1)).start == earliest_queryable(today)
    assert dashboard.spend_range("all", today, None).days == 30


def test_概览_客户列表_账号页的每日消费_右上角都能选看多久(admin, fake_costs):
    make(admin)
    post(admin, "/customers/C001/assign", account=[ALPHA])
    pages = {"/": "/", "/customers/": "/customers/", "/account/111111111111/": "/account/111111111111/"}
    for path, base in pages.items():
        box = card(admin.get(path).get_data(as_text=True), "每日消费")
        assert 'id="spend"' in box, path
        assert switch(box) == [("近 7 天", base + "?spend=7", False), ("近 30 天", base, True),
                               ("近 90 天", base + "?spend=90", False), ("全部", base + "?spend=all", False)], path
        assert "近 30 天" in scrape(box)
        picked = card(admin.get(base + "?spend=7").get_data(as_text=True), "每日消费")
        assert len(re.findall(r'<g class="bar-col"', picked)) == 7 and "近 7 天" in scrape(picked), path
    # 客户列表原来叫「近 30 天消费」；「按区域」照旧是近 30 天
    listing = admin.get("/customers/?spend=all").get_data(as_text=True)
    assert "近 30 天消费</h2>" not in listing and ">每日消费</h2>" in listing
    assert f"{RANGE_START.strftime('%m-%d')} 起全部" in scrape(card(listing, "每日消费"))


def test_进度条滚进屏幕才长出来(admin, fake_costs):
    """每根进度条上都有 data-reveal：app.js 看它滚进屏幕才加 .in，之前从左往右长的动画停在第一帧。"""
    make(admin)
    post(admin, "/customers/C001/assign", account=[ALPHA, BETA])
    for path in ("/", "/customers/", "/customers/C001"):
        html = admin.get(path).get_data(as_text=True)
        found = re.findall(r'<(?:div|span) class="(meter|stage-bar|top-bar|bal-bar)\b[^>]*>', html)
        tags = re.findall(r'<(?:div|span) class="(?:meter|stage-bar|top-bar|bal-bar)\b[^>]*>', html)
        assert found and all(re.search(r"\sdata-reveal\s", tag) for tag in tags), path
    css = (STATIC / "style.css").read_text(encoding="utf-8")
    assert "[data-reveal]:not(.in) *, [data-reveal]:not(.in) *::before" in css
    assert ".meter > i { animation: meter-fill" in css and "@keyframes meter-fill { from { clip-path: inset(0 100% 0 0" in css


def marks_of(html: str) -> list[tuple]:
    """[(data-s, 谁, 什么事, 用了多少, 线头的字)]"""
    box = spend_card(html)
    return [(int(s), who, sub, foot, scrape(label)) for s, who, sub, foot, label in re.findall(
        r'<g class="chart-mark[^"]*" data-s="(-?\d+)" data-tip="([^"]*)" data-tip-sub="([^"]*)" '
        r'data-tip-foot="([^"]*)".*?<text x="[\d.]+" dx="-?\d+"[^>]*>(.*?)</text></g>', box, re.S)]


def legend_index(html: str) -> dict[str, int]:
    box = spend_card(html)
    return {scrape(name): int(s) for s, name in re.findall(
        r'<button class="legend-toggle" type="button" data-s="(\d+)".*?</i>(.*?)</button>', box, re.S)}


def test_竖虚线_启用到风控用了多少(admin, fake_costs):
    make(admin)
    post(admin, "/customers/C001/assign", account=[ALPHA, BETA])
    risk_day = date.today() - timedelta(days=5)
    post(admin, "/customers/C001/risk", key=ALPHA, action="mark", date=risk_day.isoformat())
    used = ALPHA_DAILY * (risk_day - RANGE_START).days                       # 08-01 启用，到风控前一天
    html = admin.get("/customers/C001?range=all").get_data(as_text=True)
    index = legend_index(html)
    marks = marks_of(html)
    assert [(who, sub, foot, label) for _, who, sub, foot, label in marks] == [
        ("acct-one@example.com", f"启用 · {RANGE_START.isoformat()}", "", "启用"),
        ("acct-two@example.com", f"启用 · {RANGE_START.isoformat()}", "", "启用"),
        ("acct-one@example.com", f"风控 · {risk_day.isoformat()}", f"{RANGE_START.strftime('%m-%d')} 起用了 ${used:,.2f}",
         f"风控 {customers.short_money(used)}"),
    ]
    # 每条线跟着图例里的那个账号走：点 acct-one 只留它的两条
    assert [s for s, *_ in marks] == [index["acct-one"], index["acct-two"], index["acct-one"]]
    # 默认近 7 天：启用那天不在图里，只有风控那条，「用了多少」照样从启用日算
    recent = marks_of(detail(admin))
    assert [(label, foot) for *_, foot, label in recent] == [
        (f"风控 {customers.short_money(used)}", f"{RANGE_START.strftime('%m-%d')} 起用了 ${used:,.2f}")]


def test_竖虚线_同一个账号同一天的并成一条(admin, fake_costs):
    """风控换下：旧账号那天又是风控又是换下，并成一条「风控、换下」。换上来的新账号从它的启用日期算（台账里是
    08-01，和它的柱子从哪天起一致），在「全部」里，写着换下了谁。"""
    make(admin)
    post(admin, "/customers/C001/assign", account=[ALPHA])
    swap_day = date.today() - timedelta(days=3)
    post(admin, "/customers/C001/replace", old=ALPHA, new=BETA, reason="风控", date=swap_day.isoformat())
    used = ALPHA_DAILY * (swap_day - RANGE_START).days
    swapped = ("acct-one@example.com", f"风控、换下 · {swap_day.isoformat()}", f"风控、换下 {customers.short_money(used)}")
    assert [(who, sub, label) for _, who, sub, _, label in marks_of(detail(admin))] == [swapped]
    everything = admin.get("/customers/C001?range=all").get_data(as_text=True)
    assert [(who, sub, label) for _, who, sub, _, label in marks_of(everything)] == [   # 按日子从左到右
        ("acct-one@example.com", f"启用 · {RANGE_START.isoformat()}", "启用"),
        ("acct-two@example.com", f"启用 · {RANGE_START.isoformat()} · 换下 acct-one", "启用"),
        swapped,
    ]


def test_竖虚线_点图例只留它的线():
    css = (STATIC / "style.css").read_text(encoding="utf-8")
    assert ".bar-chart[data-focus] .chart-marks [data-s]:not(.is-focus) { opacity: 0; pointer-events: none; }" in css


def test_竖虚线的线头挤了往下排_靠右的字写在左边():
    labels = [f"10-{day:02d}" for day in range(1, 11)]
    series = [SimpleNamespace(name="kite", slot=0, values=[1.0] * 10)]
    face = Avatar("K", 1, False)
    marks = [chart.BarMark(index=2, label="启用", series="kite", face=face, tip="kite@example.com"),
             chart.BarMark(index=3, label="风控 $1.2K", series="kite", face=face, tip="kite@example.com"),
             chart.BarMark(index=9, label="结算 $12.3K", series="别人"),
             chart.BarMark(index=10, label="出了图", series="kite")]                # 不在图里的不画
    plain = chart.render_bars(labels, series, width=400, height=200)
    drawn = chart.render_bars(labels, series, width=400, height=200, marks=marks)
    assert plain.height == 200 and drawn.height == 200 + 2 * chart.MARK_LANE  # 挨着的两条分两排
    groups = re.findall(r'<g class="(chart-mark[^"]*)" data-s="(-?\d+)"', drawn.svg)
    assert groups == [("chart-mark av-1", "0"), ("chart-mark av-1", "0"), ("chart-mark", "-1")]
    band = (400 - 48 - 6) / 10
    xs = re.findall(r'<line x1="([\d.]+)" y1="[\d.]+" x2="\1" y2="[\d.]+" stroke="[^"]+" stroke-width="1.3" '
                    r'stroke-dasharray="4 3">', drawn.svg)
    assert xs == [f"{48 + band * i:.2f}" for i in (2, 3, 9)]                   # 画在那一格的左边：从这天起
    anchors = re.findall(r'dx="(-?\d+)" y="[\d.]+" text-anchor="(start|end)"', drawn.svg)
    assert anchors == [("11", "start"), ("11", "start"), ("-11", "end")]      # 最右边那条字写在左边
    assert f'fill="{chart.OTHER_COLOR}"' in drawn.svg                         # 对不上序列的画灰点
    assert "出了图" not in drawn.svg and plain.svg.count("chart-mark") == 0


# ====================================================================== 6~9. 账号页的默认时间
def test_账号页的默认时间(pages):
    assert range_label(get(pages, f"/account/{ALPHA_NO_PAGES}/cost")) == "近 7 天"
    assert range_label(get(pages, f"/account/{ALPHA_NO_PAGES}/estimate")) == "近 7 天"
    assert range_label(get(pages, f"/account/{ALPHA_NO_PAGES}/usage")) == "近 24 小时"
    assert ">近 24 小时调用<" in get(pages, f"/account/{ALPHA_NO_PAGES}/")


# ====================================================================== 账号页的每日消费：也有竖虚线
def summary_marks(html: str) -> list[tuple]:
    """摘要「每日消费」上的竖虚线：[(颜色, 什么事, 用了多少, 线头的字)]"""
    box = card(html, "每日消费")
    found = re.findall(
        r'<g class="chart-mark" data-s="-1" data-tip="[^"]*" data-tip-sub="([^"]*)" data-tip-foot="([^"]*)"'
        r'.*?stroke="([^"]+)" stroke-width="1.3".*?<text x="[\d.]+" dx="-?\d+"[^>]*>(.*?)</text></g>', box, re.S)
    return [(colour, sub, foot, scrape(label)) for sub, foot, colour, label in found]


def test_账号页每日消费_分给客户到风控用了多少(admin, fake_costs):
    make(admin)
    today = date.today()
    given, risk_day = today - timedelta(days=10), today - timedelta(days=5)
    post(admin, "/customers/C001/assign", account=[ALPHA], date=given.isoformat())
    post(admin, "/customers/C001/risk", key=ALPHA, action="mark", date=risk_day.isoformat())
    html = admin.get("/account/111111111111/").get_data(as_text=True)
    assert ">每日消费</h2>" in html and "近 30 天成本" not in html
    used = ALPHA_DAILY * 5                                                       # 分过去那天起，到风控前一天
    assert summary_marks(html) == [
        (chart.TONE_COLORS["ok"], f"分给 星河智能 · {given.isoformat()}", "", "分给 星河智能"),
        (chart.TONE_COLORS["danger"], f"风控 · {risk_day.isoformat()}", f"{given.strftime('%m-%d')} 起用了 ${used:,.2f}",
         f"风控 {customers.short_money(used)}"),
    ]                                                                            # 启用（08-01）在 30 天以外，不画


def test_账号页每日消费_图外的事不用查每天的消费(admin, fake_costs, monkeypatch):
    """竖虚线只画近 30 天里的；「用了多少」要查这个账号全部的每日消费，图里没有「停」的就不查。"""
    make(admin)
    post(admin, "/customers/C001/assign", account=[ALPHA], date=(date.today() - timedelta(days=60)).isoformat())
    post(admin, "/customers/C001/risk", key=ALPHA, action="mark", date=(date.today() - timedelta(days=40)).isoformat())
    monkeypatch.setattr(customers, "fetch_series", lambda *a, **k: pytest.fail("不该查"))
    html = admin.get("/account/111111111111/").get_data(as_text=True)
    assert summary_marks(html) == []


# ====================================================================== 点图例：挑几项只看这几项，纵轴跟着它们
def test_柱状图写着绘图区上下边_图例能挑几项_纵轴跟着重新算():
    """点一项加一项，再点一下放回去，挑了以后后面有「显示全部」。别人的段子淡出；挑中的从基线（data-base）起按原来的
    次序重新叠，纵轴按它们每天加起来最多的那天重新算（顶到 data-top），刻度重画——挑的都是小的，柱子也撑得起来。
    全放回来就是服务端画的原样。提示里只列挑中的，两项以上写它们的合计（每行带着 v）。"""
    series = [SimpleNamespace(name="kite", slot=0, values=[1.0, 2.0]), SimpleNamespace(name="kumo", slot=1, values=[3.0, 1.0])]
    svg = chart.render_bars(["10-01", "10-02"], series, width=300, height=150).svg
    head = re.search(r'<svg class="chart-svg bar-chart"[^>]* data-base="([\d.]+)" data-top="([\d.]+)"', svg)
    assert head.groups() == (f"{150 - 26.0:.2f}", "12.00")
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    assert "const picked = new Set();" in js and "if (picked.has(index)) picked.delete(index); else picked.add(index);" in js
    assert "reset.textContent = '显示全部';" in js and "reset.hidden = picked.size === 0;" in js
    # 纵轴：挑中的每天加起来最多的那天，照 chart.py 取整；段子从基线起叠，上面还有段的让出一道缝
    assert "const step = niceStep(most || 1, 3);" in js and "const scale = (base - plotTop) / (step * ticks);" in js
    assert "goal.set(seg, [n === mine.length - 1 ? high : Math.min(bottom, high + BAR_GAP), bottom]);" in js
    assert "text.textContent = label(plan.step * tick);" in js
    assert "redrawAxis(grid, gridOriginal, plan, base, function (value) { return axisMoney(value, symbol); });" in js
    # 放大着也对：柱顶圆头那段交给放大的脚本重画
    assert "if (svg.zoomReshape) svg.zoomReshape(seg.el, d); else seg.el.setAttribute('d', d);" in js
    assert "svg.zoomReshape = function (el, d) {" in js
    # 没挑：全部的合计；挑了一项：不写合计；两项以上：只加挑中的
    assert ("const head = typeof item.head === 'string' ? item.head\n"
            "          : !picked.size ? '合计 ' + item.total : sum ? '合计 ' + sum : rows.length ? '' : '这天没有';") in js
    # 数据里自带 head 的（每月账号和消费）照它写；这一格没有柱子就贴着这一格摆提示
    assert "placeTip(column && column.width ? column : hit.getBoundingClientRect());" in js
    css = (STATIC / "style.css").read_text(encoding="utf-8")
    assert ".bar-chart[data-focus] .chart-bars [data-s]:not(.is-focus) { opacity: 0; }" in css
    assert "--drop" not in css + js and "is-rising" not in css + js           # 以前靠平移往下落，现在改的是段子的上下边
    assert ".legend-reset[hidden] { display: none; }" in css


def test_薄得看不见的段子也画上():
    """小的那项在全部一起看时不到半个像素，以前不画；现在照画——图例只挑它的时候纵轴放大，它就看得见了。"""
    series = [SimpleNamespace(name="big", slot=0, values=[1000.0, 1000.0]), SimpleNamespace(name="tiny", slot=1, values=[0.01, 0.0])]
    svg = chart.render_bars(["10-01", "10-02"], series, width=300, height=150).svg
    assert len(re.findall(r'<(?:rect|path) data-s="1"', svg)) == 1
    assert len(re.findall(r'<(?:rect|path) data-s="0"', svg)) == 2


def test_图例能挑几项_四张图都是():
    """概览、客户页（按账号）、客户列表（按客户）、收入和毛利（按项）的图例是同一套：签上没有写死「只看一个」。"""
    for name, label in (("index.html", "挑几个账号只看它们"), ("_customer_charts.html", "挑几个账号只看它们"),
                        ("customers.html", "挑几个客户只看它们"), ("ops.html", "挑几项只看它们")):
        html = (STATIC.parent / "templates" / name).read_text(encoding="utf-8")
        assert re.search(rf'<div class="legend-toggles" data-legend-for="[^"]+" role="group" aria-label="{label}">', html), name


def test_进度条是长出来的_不是到一半才蹦出来():
    """剪裁的动画两头都写成 inset()：只写起点的话终点是 clip-path: none，形状和 none 之间插不了值，
    整根条会憋到动画一半才一下子出现——看起来是「一条一条蹦出来」，不是从零长到位。"""
    css = (STATIC / "style.css").read_text(encoding="utf-8")
    assert ("@keyframes meter-fill { from { clip-path: inset(0 100% 0 0 round 999px); } "
            "to { clip-path: inset(0 0 0 0 round 999px); } }") in css
    assert "@keyframes area-reveal { from { clip-path: inset(0 100% 0 0); } to { clip-path: inset(0 0 0 0); } }" in css
    for frames in re.findall(r"@keyframes [\w-]+ \{(.*?)\}\s*\}", css, re.S):
        if "clip-path" in frames:
            assert " to {" in frames or "100%" in frames, frames


def test_近7天的柱子粗一些_不会中间空一大片():
    """柱子占一格的六成：7 根铺满整行宽的时候，两根中间的空白比柱子本身还窄（以前最粗 24，中间空一大片）。"""
    labels = [f"10-{day:02d}" for day in range(5, 12)]
    series = [SimpleNamespace(name="kite", slot=0, values=[3.0] * 7)]
    svg = chart.render_bars(labels, series, width=1100, height=230).svg
    # 顶上那段是圆头的 path：M 左边,底 … L 右边,底 Z
    edges = [(float(left), float(right)) for left, right in
             re.findall(r'<path data-s="0" d="M([\d.]+),[\d.]+ .*? L([\d.]+),[\d.]+ Z"', svg)]
    band = (1100 - 48 - 6) / 7
    assert len(edges) == 7 and all(abs((right - left) - band * 0.6) < 0.05 for left, right in edges)
    gap = edges[1][0] - edges[0][1]
    assert gap < edges[0][1] - edges[0][0]
