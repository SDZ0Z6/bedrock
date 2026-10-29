"""AK 失效以后账单照样能看：概览那一次查询顺手按服务拆开存下来，成本页签和导出 CSV 查不了时拿它顶上。

  · 概览的 Cost Explorer 查询 GroupBy [SERVICE, TAG]：还是一次请求，累计和以前一样，多出每个月、每个服务、
    每个标签值的原价（CostSplit.lines），跟着累计存进 last_known；
  · 成本页签查不了（AK 无效、被收权限……）：显示存下来的账单，按月、按服务（或按标签），折算后按台账现在的
    比率算，和所选时间有重叠的整月；右上角的报错照样弹，后面加一句「下面是存下来的账单」；
  · 老记录（这一项加上之前存的）只有累计：一格「启用 ~ 那天」，按服务一项「合计」，按标签 TAG / UNTAG；
  · 没存过：和以前一样，一片 0 加报错；
  · 导出 CSV 查不了时导出存下来的那份，第一列是月份。
"""

from __future__ import annotations

import csv
import io
import json
import re
from datetime import date, timedelta

from bedrock_cost import account_pages, config, cost_explorer, last_known
from bedrock_cost.cost_explorer import CostSplit
from bedrock_cost.dates import cumulative_range
from bedrock_cost.excel_source import Account

from .test_account_pages import fake_estimate, pages, selected  # noqa: F401  (fixture)
from .test_web import book, book_rows, fail_daily, notes, page_env, rewrite, scrape, toasts, usage_states  # noqa: F401

ALPHA = "111111111111"
OPUS, BEDROCK = "Claude Opus 5 (Amazon Bedrock Edition)", "Amazon Bedrock"
DEAD = "AK 无效或已删除"


def account(**overrides) -> Account:
    fields = dict(partner="P", account=ALPHA, budget=500_000.0, tag_ratio=1.0, untag_ratio=1.05,
                  ak="AKIAFAKEALPHA0000000", sk="x" * 40, row=2, tag_spec="map-migrated=migALPHA",
                  start_date=date(2026, 9, 1))
    fields.update(overrides)
    return Account(**fields)


class FakeClient:
    def __init__(self, response: dict):
        self.response = response
        self.requests: list[dict] = []

    def get_cost_and_usage(self, **request):
        self.requests.append(request)
        return self.response


def group(service: str, tag: str, amount: float) -> dict:
    return {"Keys": [service, f"map-migrated${tag}"],
            "Metrics": {config.COST_METRIC: {"Amount": str(amount), "Unit": "USD"}}}


# ====================================================================== 查询：顺手按服务拆开
def test_概览那一次查询顺手按服务拆开_累计和以前一样(monkeypatch):
    response = {"ResultsByTime": [
        {"TimePeriod": {"Start": "2026-09-01", "End": "2026-10-01"},
         "Groups": [group(OPUS, "migALPHA", 100), group(OPUS, "", 40), group(BEDROCK, "migALPHA", 10)]},
        # 没有 Groups 只给 Total 的月份（没打标签、没服务）照旧算 UNTAG，也记一行
        {"TimePeriod": {"Start": "2026-10-01", "End": "2026-10-29"}, "Groups": [],
         "Total": {config.COST_METRIC: {"Amount": "5", "Unit": "USD"}}},
    ]}
    fake = FakeClient(response)
    monkeypatch.setattr(cost_explorer.boto3, "client", lambda *a, **k: fake)
    split = cost_explorer._query(account(), date(2026, 9, 1), date(2026, 10, 28))
    assert len(fake.requests) == 1                                           # 还是一次请求
    assert fake.requests[0]["GroupBy"] == [{"Type": "DIMENSION", "Key": "SERVICE"},
                                           {"Type": "TAG", "Key": "map-migrated"}]
    assert (split.tag_raw, split.untag_raw) == (110.0, 45.0)
    assert split.monthly == {"2026-09": (110.0, 40.0), "2026-10": (0.0, 5.0)}
    assert split.lines == (("2026-09", BEDROCK, "migALPHA", 10.0), ("2026-09", OPUS, "", 40.0),
                           ("2026-09", OPUS, "migALPHA", 100.0), ("2026-10", "", "", 5.0))


def test_查成功的那一次连明细一起存下来(monkeypatch):
    response = {"ResultsByTime": [{"TimePeriod": {"Start": "2026-09-01", "End": "2026-10-01"},
                                   "Groups": [group(OPUS, "migALPHA", 100), group(OPUS, "", 40)]}]}
    monkeypatch.setattr(cost_explorer.boto3, "client", lambda *a, **k: FakeClient(response))
    cost_explorer.clear_cache()
    cost_explorer.fetch_split(account(), date(2026, 9, 1), date(2026, 9, 30))
    known = last_known.recall(account(), date(2026, 9, 1), date(2026, 10, 1))
    assert [list(line) for line in known.lines] == [["2026-09", OPUS, "", 40.0], ["2026-09", OPUS, "migALPHA", 100.0]]
    assert (known.tag_raw, known.untag_raw) == (100.0, 40.0)
    cost_explorer.clear_cache()


def test_老记录没有明细也读得出来():
    """这一项加上之前存的记录里根本没有 lines 这一栏：照样认，lines 是空的。"""
    old = {"start": "2026-09-01", "end": "2026-09-30", "basis": last_known._basis(account()), "tag_raw": 1.0,
           "untag_raw": 2.0, "currency": "USD", "saved_at": "2026-09-30T08:00:00+00:00"}
    config.LAST_KNOWN_COSTS_PATH.write_text(json.dumps({"version": 1, "accounts": {ALPHA: old}}), encoding="utf-8")
    known = last_known.recall(account(), date(2026, 9, 1), date(2026, 10, 1))
    assert not known.lines and (known.tag_raw, known.untag_raw) == (1.0, 2.0)


# ====================================================================== 成本页签：查不了时显示存下来的账单
TODAY = date.today()


def alpha() -> Account:
    return next(a for a in account_pages._all_accounts() if a.account == ALPHA)


def month_starts(begin: date, last: date) -> list[date]:
    months, cursor = [], begin.replace(day=1)
    while cursor <= last:
        months.append(cursor)
        cursor = (cursor + timedelta(days=32)).replace(day=1)
    return months


def remember_bill(as_of: date, lines: tuple | None = None) -> tuple[date, list[tuple]]:
    """给 alpha 存一份账单（概览从启用算起的那一份），默认最近三个月每月两个服务、TAG 和没打标签各一笔。"""
    begin = cumulative_range(alpha().start_date, TODAY)[0]
    if lines is None:
        lines = []
        for k, month in enumerate(month_starts(begin, as_of)[-3:]):
            key = f"{month:%Y-%m}"
            lines += [(key, OPUS, "migALPHA", 100.0 + k), (key, OPUS, "", 40.0), (key, BEDROCK, "migALPHA", 10.0)]
    lines = tuple(lines)
    tag = sum(line[3] for line in lines if line[2] == "migALPHA")
    untag = sum(line[3] for line in lines if line[2] != "migALPHA")
    last_known.remember(alpha(), begin, as_of, CostSplit(tag_raw=tag or 300.0, untag_raw=untag or 120.0, lines=lines))
    return begin, list(lines)


def tiles(html: str) -> str:
    return scrape(re.search(r'<section class="summary summary-3">(.*?)</section>', html, re.S).group(1))


def sub(html: str) -> str:
    return scrape(re.search(r'<p class="viz-sub">(.*?)</p>', html, re.S).group(1))


def get(client, path: str) -> str:
    response = client.get(path)
    assert response.status_code == 200
    return response.get_data(as_text=True)


def test_AK失效_成本页签显示存下来的账单(pages, monkeypatch):
    as_of = TODAY - timedelta(days=1)
    begin, lines = remember_bill(as_of)
    fail_daily(monkeypatch, {ALPHA}, reason=DEAD)
    html = get(pages, f"/account/{ALPHA}/cost?start={begin}&end={TODAY}")
    raw = sum(line[3] for line in lines)
    marked = sum(line[3] * (1.0 if line[2] == "migALPHA" else 1.05) for line in lines)    # 台账现在的比率
    assert f"折算后合计 ${marked:,.2f}" in tiles(html) and f"AWS 原价 ${raw:,.2f}" in tiles(html)
    assert f"存到哪天 {as_of.isoformat()}" in tiles(html)
    assert sub(html) == f"存下来的账单 · 截至 {as_of.isoformat()} · 按月 · 折算后"
    assert any(text.startswith(f"Cost Explorer 这次查不了，下面是 {as_of.isoformat()} 查成功时存下来的账单")
               for text in notes(html))
    assert selected(html, "granularity") == "monthly"                         # 按日的明细没存
    toast = next(t for t in toasts(html) if t.title == "查不到 Cost Explorer")
    assert toast.text == f"{DEAD}，下面是存下来的账单"
    # 明细表：按服务、每个月一列
    months = sorted({line[0] for line in lines})
    table = re.search(r'<table class="wide-table">(.*?)</table>', html, re.S).group(1)
    assert [scrape(h) for h in re.findall(r'<th class="num date-col">(.*?)</th>', table, re.S)] == months
    assert OPUS in scrape(table) and BEDROCK in scrape(table)


def test_按标签看_和所选时间有重叠的整月(pages, monkeypatch):
    as_of = TODAY - timedelta(days=1)
    begin, lines = remember_bill(as_of)
    fail_daily(monkeypatch, {ALPHA}, reason=DEAD)
    last_month = as_of.replace(day=1)
    html = get(pages, f"/account/{ALPHA}/cost?start={max(begin, last_month + timedelta(days=3))}&end={TODAY}&dim=tag")
    shown = [line for line in lines if line[0] == f"{last_month:%Y-%m}"]      # 选的是这个月里的几天：整月都在
    assert f"AWS 原价 ${sum(line[3] for line in shown):,.2f}" in tiles(html)
    table = scrape(re.search(r'<table class="wide-table">(.*?)</table>', html, re.S).group(1))
    assert "migALPHA" in table and "未打标签" in table


def test_比率改了_折算后跟着台账走(pages, monkeypatch, book):
    as_of = TODAY - timedelta(days=1)
    begin, lines = remember_bill(as_of)
    rewrite(book, book_rows(UNTAG_RATIO={ALPHA: 1.2}))
    fail_daily(monkeypatch, {ALPHA}, reason=DEAD)
    html = get(pages, f"/account/{ALPHA}/cost?start={begin}&end={TODAY}")
    marked = sum(line[3] * (1.0 if line[2] == "migALPHA" else 1.2) for line in lines)
    assert f"折算后合计 ${marked:,.2f}" in tiles(html)


def test_老记录只有累计(pages, monkeypatch):
    as_of = TODAY - timedelta(days=2)
    begin = cumulative_range(alpha().start_date, TODAY)[0]
    last_known.remember(alpha(), begin, as_of, CostSplit(tag_raw=300.0, untag_raw=100.0))
    fail_daily(monkeypatch, {ALPHA}, reason=DEAD)
    html = get(pages, f"/account/{ALPHA}/cost")
    assert f"折算后合计 ${300 + 100 * 1.05:,.2f}" in tiles(html) and "AWS 原价 $400.00" in tiles(html)
    assert sub(html) == f"存下来的累计 · 截至 {as_of.isoformat()} · 折算后"
    assert any(text.startswith(f"Cost Explorer 这次查不了，下面是 {as_of.isoformat()} 查成功时存下来的累计")
               for text in notes(html))
    table = scrape(re.search(r'<table class="wide-table">(.*?)</table>', html, re.S).group(1))
    assert "合计" in table and f"{begin:%m-%d} ~ {as_of:%m-%d}" in table
    by_tag = scrape(re.search(r'<table class="wide-table">(.*?)</table>',
                              get(pages, f"/account/{ALPHA}/cost?dim=tag"), re.S).group(1))
    assert "TAG" in by_tag and "UNTAG" in by_tag


def test_没存过_和以前一样(pages, monkeypatch):
    fail_daily(monkeypatch, {ALPHA}, reason=DEAD)
    html = get(pages, f"/account/{ALPHA}/cost")
    assert "折算后合计 $0.00" in tiles(html)
    assert next(t for t in toasts(html) if t.title == "查不到 Cost Explorer").text == DEAD
    assert not any("存下来" in text for text in notes(html))


def test_查得到的时候不碰存下来的(pages):
    remember_bill(TODAY - timedelta(days=1))
    html = get(pages, f"/account/{ALPHA}/cost")
    assert "日均（折算后）" in tiles(html) and "存到哪天" not in tiles(html)
    assert not any("存下来" in text for text in notes(html))


# ====================================================================== 导出 CSV
def rows_of(response) -> list[list[str]]:
    return list(csv.reader(io.StringIO(response.get_data(as_text=True).lstrip("﻿"))))


def test_导出CSV_查不了时导出存下来的账单(pages, monkeypatch):
    as_of = TODAY - timedelta(days=1)
    begin, lines = remember_bill(as_of)
    fail_daily(monkeypatch, {ALPHA}, reason=DEAD)
    response = pages.get(f"/account/{ALPHA}/cost.csv?start={begin}&end={TODAY}")
    assert response.status_code == 200
    assert f'filename="{ALPHA}-cost-saved-{as_of.isoformat()}.csv"' in response.headers["Content-Disposition"]
    rows = rows_of(response)
    assert rows[0][0] == f"月份（存下来的账单，截至 {as_of.isoformat()}）"
    body = rows[1:]
    assert len(body) == len(lines)
    first = next(row for row in body if row[1] == OPUS and row[2] == "")
    assert first[3:] == ["UNTAG", "1.05", "40", "42"]
    assert sorted({row[0] for row in body}) == sorted({line[0] for line in lines})


def test_导出CSV_没存过还是报错(pages, monkeypatch):
    fail_daily(monkeypatch, {ALPHA}, reason=DEAD)
    response = pages.get(f"/account/{ALPHA}/cost.csv")
    assert response.status_code == 502
    assert response.get_data(as_text=True).startswith("查不到 Cost Explorer")
