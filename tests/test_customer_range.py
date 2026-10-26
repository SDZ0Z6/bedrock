"""客户页「调用次数」「预估」「每日消费」共用一个时间：图上面那一排（近 24 小时、近 7 天（默认）、近 30 天、近 90 天、全部），
后面「自定义」点开填起止。网址上是 ?range=24h / 30 / 90 / all，自定义是 ?start=&end=（两头都算）；老网址上各张图自己的
?calls= / ?estimate= / ?spend= 也认。

  · 近 24 小时：调用次数按小时；预估、每日消费按天，看昨天和今天（24 小时跨了这两个日期）；
  · 别的：三张都按天，调用次数、预估按 UTC 日期，每日消费按本地日期；
  · 自定义的一段过去的日子没有「今天」：预估写日均和最多的一天，调用次数旁边是这一段的预估；
  · 换时间只换那一排和三张卡片（?part=charts，app.js），不刷新整页；没开 JS 照样是普通链接、普通表单。
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone

from bedrock_cost import config, customer_pages
from bedrock_cost.dates import earliest_queryable

from .test_accounts import admin, post  # noqa: F401  (admin 是 fixture)
from .test_customer_calls import bars, card, estimate_card, estimates, lines, real_calls  # noqa: F401  (fixture)
from .test_customer_pages import ALPHA, BETA, detail, make
from .test_web import book, page_env, scrape, usage_states  # noqa: F401  (book 是 fixture；后两个 autouse)

STATIC = config.BASE_DIR / "src" / "bedrock_cost" / "static"


def bar_of(html: str) -> str:
    start = html.index('<form class="chart-range"')
    return html[start:html.index("</form>", start)]


def presets(bar: str) -> list[tuple[str, str, bool]]:
    """那一排现成的几档：[(名字, 网址, 是不是选中的)]。"""
    return [(scrape(label), href.replace("&amp;", "&"), bool(on)) for on, href, label in re.findall(
        r'<a class="seg-item( is-on)?" href="([^"]+)" data-loading="正在读这段时间的数"[^>]*>(.*?)</a>', bar, re.S)]


def spend_bars(html: str) -> list[str]:
    """「每日消费」每一根柱的日期（横轴上的字）。"""
    start = html.index('<h2 class="viz-title">每日消费</h2>')
    box = html[start:html.index("</article>", start)]
    axis = re.search(r'<g class="chart-xaxis">(.*?)</g>', box, re.S).group(1)
    return re.findall(r'<text [^>]*data-i="\d+"[^>]*>([^<]+)</text>', axis)


def spend_text(html: str) -> str:
    start = html.index('<h2 class="viz-title">每日消费</h2>')
    return scrape(html[start:html.index("</article>", start)])


# ====================================================================== 那一排
def test_图上面一排时间_三张图共用_默认近7天(admin, fake_costs):
    make(admin)
    post(admin, "/customers/C001/assign", account=[ALPHA, BETA])
    html = detail(admin)
    bar = bar_of(html)
    assert '<form class="chart-range" id="charts" method="get" action="/customers/C001#charts" data-part="range">' in html
    assert html.index(">余额<") < html.index('<form class="chart-range"') < html.index(">调用次数<")
    assert presets(bar) == [
        ("近 24 小时", "/customers/C001?range=24h#charts", False), ("近 7 天", "/customers/C001#charts", True),
        ("近 30 天", "/customers/C001?range=30#charts", False), ("近 90 天", "/customers/C001?range=90#charts", False),
        ("全部", "/customers/C001?range=all#charts", False)]
    # 「自定义」点开填起止：先填好现在看的那几天（本地日期），最早到 Cost Explorer 能查的那天、最晚到今天
    today = date.today()
    assert '<div class="chart-range-custom">' in bar and scrape(bar[bar.index("<summary>"):bar.index("</summary>")]) == "自定义"
    assert f'name="start" value="{(today - timedelta(days=6)).isoformat()}"' in bar
    assert f'name="end" value="{today.isoformat()}"' in bar
    assert f'min="{earliest_queryable(today).isoformat()}"' in bar and f'max="{today.isoformat()}"' in bar
    assert "调用次数、预估、每日消费都看这一段" in scrape(bar)
    # 三张卡片自己那一排没有了；换时间时按 data-part 换这一排和三张卡片
    for title in ("调用次数", "预估", "每日消费"):
        start = html.index(f'<h2 class="viz-title">{title}</h2>')
        box = html[html.rindex("<article", 0, start):html.index("</article>", start)]
        assert 'class="seg' not in box, title
    assert re.findall(r'data-part="(\w+)"', html) == ["range", "calls", "estimate", "spend"]


def test_选一档_三张图一起换(admin, fake_costs, real_calls, monkeypatch):
    asked = estimates(monkeypatch, {ALPHA.split("#")[0]: [1.0] * 400, BETA.split("#")[0]: [1.0] * 400})   # 每天 $1
    make(admin)
    post(admin, "/customers/C001/assign", account=[ALPHA, BETA])
    utc_today = datetime.now(timezone.utc).date()
    today = date.today()

    html = admin.get("/customers/C001?range=30").get_data(as_text=True)
    assert [on for *_, on in presets(bar_of(html))] == [False, False, True, False, False]
    assert len(lines(html)["labels"]) == 30 and "次 · 近 30 天" in scrape(card(html))            # 调用次数按天
    assert asked[-1][1:] == (utc_today - timedelta(days=29), utc_today)                           # 预估按 UTC 日期
    assert "近 30 天预估" in scrape(estimate_card(html)) and len(bars(html)) == 30
    assert len(spend_bars(html)) == 30 and "近 30 天" in spend_text(html)                          # 每日消费按本地日期

    # 近 24 小时：调用次数按小时；预估、每日消费按天看昨天和今天
    html = admin.get("/customers/C001?range=24h").get_data(as_text=True)
    assert len(lines(html)["labels"]) == 24 and "次 · 近 24 小时" in scrape(card(html))
    assert asked[-1][1:] == (utc_today - timedelta(days=1), utc_today)
    assert "昨天和今天预估" in scrape(estimate_card(html)) and [d["label"] for d in bars(html)][-1] == "今天"
    assert spend_bars(html) == [(today - timedelta(days=1)).strftime("%m-%d"), today.strftime("%m-%d")]
    assert "昨天和今天" in spend_text(html)

    # 近 90 天、全部也是三张一起
    html = admin.get("/customers/C001?range=90").get_data(as_text=True)
    assert len(lines(html)["labels"]) == 90 and len(bars(html)) == 90 and len(spend_bars(html)) == 90

    # 老网址：各张图自己的参数也认，取第一个认得的
    for old, on in (("spend=90", 3), ("calls=24h", 0), ("estimate=all", 4), ("calls=nonsense&spend=30", 2)):
        html = admin.get(f"/customers/C001?{old}").get_data(as_text=True)
        assert [i for i, (*_, picked) in enumerate(presets(bar_of(html))) if picked] == [on], old


def test_自定义起止_三张图都看这一段_过去的日子没有今天(admin, fake_costs, real_calls, monkeypatch):
    asked = estimates(monkeypatch, {ALPHA.split("#")[0]: [5.0] * 11})
    make(admin)
    post(admin, "/customers/C001/assign", account=[ALPHA, BETA])
    today = date.today()
    start, end = today - timedelta(days=20), today - timedelta(days=10)
    html = admin.get(f"/customers/C001?start={start.isoformat()}&end={end.isoformat()}").get_data(as_text=True)
    label = f"{start:%m-%d} ~ {end:%m-%d}" if start.year == end.year == today.year else f"{start} ~ {end}"
    bar = bar_of(html)
    assert not any(on for *_, on in presets(bar))                                      # 现成的那几档一个都没选
    assert '<div class="chart-range-custom is-on">' in bar
    assert scrape(bar[bar.index("<summary>"):bar.index("</summary>")]) == label         # 按钮上写着这一段
    assert f'name="start" value="{start.isoformat()}"' in bar and f'name="end" value="{end.isoformat()}"' in bar
    # 三张都看这一段：每天一根 / 一个点，两头都算
    assert spend_bars(html)[0] == start.strftime("%m-%d") and len(spend_bars(html)) == 11 and label in spend_text(html)
    assert asked[-1][1:] == (start, end)
    assert len(lines(html)["labels"]) == 11 and lines(html)["labels"][0] == start.strftime("%m-%d")
    assert f"次 · {label}" in scrape(card(html))
    # 过去的日子没有「今天」：预估写日均和最多的一天，调用次数旁边是这一段的预估
    box = scrape(estimate_card(html))
    assert f"$55 {label} 预估" in box and "今天到现在" not in box and "日均 $5.00" in box and "最多的一天 $5.00" in box
    assert [d["label"] for d in bars(html)][-1] == end.strftime("%m-%d")
    assert "$55.00 这段时间预估" in scrape(card(html)) and "今日预估" not in scrape(card(html))
    assert "这段时间没有调用" not in scrape(card(html))


def test_自定义的日子不对_改过来说一声():
    today = date(2026, 10, 26)
    pick = customer_pages._chart_range
    swapped = pick({"start": "2026-10-20", "end": "2026-10-10"}, today)
    assert (swapped.key, swapped.start, swapped.end, swapped.label) == ("custom", date(2026, 10, 10), date(2026, 10, 20),
                                                                       "10-10 ~ 10-20")
    assert swapped.notes == ["开始日期晚于结束日期，已自动调换。"]
    future = pick({"start": "2026-10-01", "end": "2026-11-30"}, today)
    assert future.end == today and future.notes == ["结束日期不能晚于今天，已调整为今天。"]
    early = pick({"start": "2020-01-01", "end": "2026-10-01"}, today)
    assert early.start == earliest_queryable(today) and len(early.notes) == 1
    assert pick({"start": "2026-10-01"}, today).end == today                            # 只填了开始：到今天
    assert pick({"start": "2026-10-20", "end": "2026-10-20"}, today).label == "10-20"   # 一天的只写那一天
    assert pick({"start": "2025-12-20", "end": "2026-01-05"}, today).label == "2025-12-20 ~ 2026-01-05"
    # 正好是近 7 / 30 / 90 天的，当那一档
    assert pick({"start": "2026-09-27", "end": "2026-10-26"}, today).key == "30"
    # 现成的几档、老网址上的参数、认不出的
    assert [pick(args, today).key for args in ({}, {"range": "24h"}, {"range": "nonsense"}, {"spend": "90"},
                                               {"calls": "all", "spend": "30"}, {"estimate": "x", "spend": "30"})] == [
        "7", "24h", "7", "90", "all", "30"]


def test_日子改过的那一排上写着(admin, fake_costs):
    make(admin)
    html = admin.get("/customers/C001?start=2026-10-20&end=2026-10-10").get_data(as_text=True)
    assert "开始日期晚于结束日期，已自动调换。" in scrape(bar_of(html))


# ====================================================================== 只换那一排和三张卡片
def test_换时间只取回那一排和三张卡片(admin, fake_costs):
    make(admin)
    post(admin, "/customers/C001/assign", account=[ALPHA, BETA])
    html = admin.get("/customers/C001?part=charts&range=30").get_data(as_text=True)
    assert "<html" not in html and "<nav" not in html
    assert html.strip().startswith('<form class="chart-range"')
    assert re.findall(r'data-part="(\w+)"', html) == ["range", "calls", "estimate", "spend"]
    assert [on for *_, on in presets(bar_of(html))] == [False, False, True, False, False]
    assert all("part=" not in href for _, href, _ in presets(bar_of(html)))           # 链接是整页的网址（没开 JS 照样能用）
    assert "<html" in admin.get("/customers/C001?part=timeline").get_data(as_text=True)     # 别的名字照常给整页


def test_换时间不刷新整页_脚本和样式():
    """app.js：点那一排的链接、提交自定义起止，都带上 ?part=charts 取回那一排和三张卡片，按 data-part 一张张换上，地址栏
    跟着改，新卡片的图重新挂上交互；比整页的「正在加载」先拦下；取不回来就整页跳过去。"""
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    for piece in ("function swapCharts(target) {", "part.searchParams.set('part', 'charts');",
                  """holder.content.querySelector('[data-part="' + old.dataset.part + '"]')""",
                  "history.replaceState(history.state, '', page.pathname + page.search + location.hash);",
                  "initCharts(pair[1]);", ".catch(function () { location.href = page.href; });",
                  """event.target.closest('[data-part="range"] a[href]')""",
                  """event.target.closest('form[data-part="range"]')""",
                  "for (const [name, value] of new FormData(form)) if (value) target.searchParams.set(name, value);"):
        assert piece in js, piece
    css = (STATIC / "style.css").read_text(encoding="utf-8")
    assert ".chart-range { grid-column: 1 / -1;" in css
    assert ".chart-range-custom.is-on .range-picker > summary {" in css
