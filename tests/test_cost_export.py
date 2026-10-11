"""账号页「成本」页签的导出 CSV。

- 日期跟着页面上的筛选走（和成本页签同一段：不早于启用日期）；维度、粒度不管，导出的总是最细的一层：
  每天、每个服务、每个标签值一行；
- 每行写着算 TAG 还是 UNTAG、乘的比率、AWS 原价、折算后，加起来和页面上的两个合计对得上；
- 按钮由 app.js 用 fetch 取（data-export）：出了错服务端回一句纯文本，在右上角弹出来，不下载一个报错页面。
"""

from __future__ import annotations

import csv
import io
import re
from datetime import date, timedelta
from pathlib import Path

from bedrock_cost import config, usage_explorer
from bedrock_cost.dates import earliest_queryable
from bedrock_cost.excel_source import Account

from .conftest import RANGE_START
from .test_account_pages import ALPHA, fake_estimate, get, pages  # noqa: F401  (fixture)
from .test_aws_errors import stubbed  # noqa: F401  (fixture)
from .test_web import book, fail_daily, page_env, scrape, usage_states  # noqa: F401  (book 是 fixture；后两个 autouse)

OPUS = "Claude Opus 5 (Amazon Bedrock Edition)"
SONNET = "Claude Sonnet 5 (Amazon Bedrock Edition)"
STATIC = Path(config.__file__).parent / "static"


def csv_rows(response) -> list[list[str]]:
    body = response.get_data(as_text=True)
    assert body.startswith("\ufeff")                     # 带 BOM：Excel 认得出是 UTF-8，中文不乱
    return list(csv.reader(io.StringIO(body[1:])))


def filename(response) -> str:
    return re.fullmatch(r'attachment; filename="([^"]+)"', response.headers["Content-Disposition"]).group(1)


# ====================================================================== 内容
def test_按筛选的日期导出_每天每个服务每个标签值一行(pages):
    today = date.today()
    start, end = today - timedelta(days=9), today - timedelta(days=3)
    # 页面上选的是「按标签、按月」：导出不管，照样每天、每个服务、每个标签值一行
    response = pages.get(f"/account/{ALPHA}/cost.csv?start={start}&end={end}&dim=tag&granularity=monthly")
    assert response.status_code == 200 and response.mimetype == "text/csv"
    assert filename(response) == f"{ALPHA}-cost-{start}_{end}.csv"
    table = csv_rows(response)
    assert table[0] == ["日期", "服务", "标签值（map-migrated）", "算作", "比率", "AWS 原价（USD）", "折算后（USD）"]
    # alpha：打了 migALPHA 的每天 100 乘 TAG 比率 1，没打标签的 50 乘 UNTAG 比率 1.05
    days = [(start + timedelta(days=i)).isoformat() for i in range(7)]
    assert table[1:] == [row for day in days for row in (
        [day, OPUS, "migALPHA", "TAG", "1", "100", "100"],
        [day, SONNET, "", "UNTAG", "1.05", "50", "52.5"],
    )]


def test_加起来和页面上的两个合计对得上(pages):
    today = date.today()
    query = f"start={today - timedelta(days=29)}&end={today}"
    table = csv_rows(pages.get(f"/account/{ALPHA}/cost.csv?{query}"))
    page = get(pages, f"/account/{ALPHA}/cost?{query}")
    tiles = scrape(re.search(r'<section class="summary summary-3">(.*?)</section>', page, re.S).group(1))
    raw, marked = (sum(float(row[column]) for row in table[1:]) for column in (5, 6))
    assert f"折算后合计 ${marked:,.2f}" in tiles and f"AWS 原价 ${raw:,.2f}" in tiles


def test_没选就是页面默认的近7天_不早于启用日期(pages):
    today = date.today()
    first = max(RANGE_START, earliest_queryable(today))
    assert filename(pages.get(f"/account/{ALPHA}/cost.csv")) == \
        f"{ALPHA}-cost-{max(today - timedelta(days=6), first)}_{today}.csv"
    assert filename(pages.get(f"/account/{ALPHA}/cost.csv?preset=last30")) == \
        f"{ALPHA}-cost-{max(today - timedelta(days=29), first)}_{today}.csv"
    # 开始日期早于启用日期：和页面一样从启用日期起
    early = pages.get(f"/account/{ALPHA}/cost.csv?start={first - timedelta(days=20)}&end={today}")
    assert filename(early) == f"{ALPHA}-cost-{first}_{today}.csv"
    assert csv_rows(early)[1][0] == first.isoformat()


# ====================================================================== 出错
def test_查不到_回一句纯文本(pages, monkeypatch):
    """app.js 拿到纯文本就在右上角弹「导不出来」，不跳页面、不下载一个报错页面。"""
    fail_daily(monkeypatch, {ALPHA})
    response = pages.get(f"/account/{ALPHA}/cost.csv")
    assert (response.status_code, response.mimetype) == (502, "text/plain")
    assert response.get_data(as_text=True) == \
        "查不到 Cost Explorer：Cost Explorer 请求过于频繁，请稍后重试（ThrottlingException）"
    missing = pages.get("/account/999999999999/cost.csv")
    assert (missing.status_code, missing.mimetype) == (404, "text/plain")
    assert missing.get_data(as_text=True) == "台账里没有账号 999999999999。"


def test_要先登录(client, book):
    response = client.get(f"/account/{ALPHA}/cost.csv")
    assert response.status_code == 302 and "/login" in response.headers["Location"]


# ====================================================================== 按钮
def test_成本明细上有导出按钮_带着页面上选的日期(pages):
    today = date.today()
    start = today - timedelta(days=13)
    html = get(pages, f"/account/{ALPHA}/cost?start={start}&end={today}&dim=tag")
    box = html[html.index(">成本明细<"):]
    link = re.search(r'<a class="btn btn-sm" href="([^"]+)"\s+download data-export title="([^"]+)">(.*?)</a>', box, re.S)
    assert link.group(1).replace("&amp;", "&") == f"/account/{ALPHA}/cost.csv?start={start}&end={today}"
    assert scrape(link.group(3)) == "导出 CSV" and '<span data-export-label>' in link.group(3)
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    assert "closest('a[data-export]')" in js and "label.textContent = '正在导出…';" in js
    assert "window.appToast('error', '导不出来', reason || '登录可能过期了，刷新页面再试。');" in js


# ====================================================================== 数据层：真走一遍 Cost Explorer 的请求
JEFF = Account(partner="Jeff", account="111122223333", budget=0, tag_ratio=1.0, untag_ratio=1.2,
               ak="TESTKEYEXPORT01", sk="not-a-real-secret-0000000000000000000", tag_spec="map-migrated=migJEFF")


def ce_day(start: str, end: str, *groups) -> dict:
    return {"TimePeriod": {"Start": start, "End": end}, "Total": {}, "Estimated": False, "Groups": [
        {"Keys": [service, f"map-migrated${tag}"], "Metrics": {"UnblendedCost": {"Amount": amount, "Unit": "USD"}}}
        for service, tag, amount in groups]}


def test_一个请求按服务和标签分组_标签值不并掉_结果多了翻页(stubbed, monkeypatch):
    """和「按服务」是同一个请求（[SERVICE, TAG] 两级分组），只是标签值留着：每一行算 TAG 还是 UNTAG、乘哪个比率
    都看得到。台账写了标签值（migJEFF）的，别的值（other）和没打的一样算 UNTAG。"""
    monkeypatch.setattr(config, "SERVICE_FILTER", [])
    monkeypatch.setattr(config, "COST_METRIC", "UnblendedCost")
    ce, stub = stubbed("ce")
    request = {
        "TimePeriod": {"Start": "2026-09-01", "End": "2026-09-03"},         # CE 的 End 是开区间
        "Granularity": "DAILY",
        "Metrics": ["UnblendedCost"],
        "GroupBy": [{"Type": "DIMENSION", "Key": "SERVICE"}, {"Type": "TAG", "Key": "map-migrated"}],
    }
    stub.add_response("get_cost_and_usage", {
        "ResultsByTime": [ce_day("2026-09-01", "2026-09-02",
                                 (OPUS, "migJEFF", "10.5"), (OPUS, "", "2"), (SONNET, "other", "0.0000001"))],
        "NextPageToken": "page-2",
    }, request)
    stub.add_response("get_cost_and_usage", {
        "ResultsByTime": [ce_day("2026-09-02", "2026-09-03", (SONNET, "other", "4"), (OPUS, "migJEFF", "1"))],
    }, {**request, "NextPageToken": "page-2"})
    monkeypatch.setattr(usage_explorer.boto3, "client", lambda *a, **k: ce)

    lines, currency, error = usage_explorer.export_lines(JEFF, date(2026, 9, 1), date(2026, 9, 2), refresh=True)
    stub.assert_no_pending_responses()
    assert error is None and currency == "USD"
    assert [(x.day, x.service, x.tag_value, x.tagged, x.ratio, x.raw, x.marked) for x in lines] == [
        ("2026-09-01", OPUS, "", False, 1.2, 2.0, 2.4),
        ("2026-09-01", OPUS, "migJEFF", True, 1.0, 10.5, 10.5),
        # Sonnet 那一笔 0.0000001：到小数点后 6 位都是 0，不出行
        ("2026-09-02", OPUS, "migJEFF", True, 1.0, 1.0, 1.0),
        ("2026-09-02", SONNET, "other", False, 1.2, 4.0, 4.8),
    ]
