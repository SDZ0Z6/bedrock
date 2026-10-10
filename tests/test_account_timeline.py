"""账号页的「时间线」页签：一个账号从启用到今天的事，不管中间换过几个客户、解绑过没有。

库存里的账号调额度、标风控、停用 / 恢复也记（EVENTS 表里 CUSTOMER 空着）；账号管理里每一行都能点进来，
修改弹窗的额度框下面写着上一次是哪天、谁、从多少调到多少。
"""

from __future__ import annotations

import json
import re

import openpyxl

from bedrock_cost import customers, excel_source

from .conftest import LEDGER_ROWS, TEST_USER
from .test_accounts import _edit, admin, by_account, page, post  # noqa: F401  (admin 是 fixture)
from .test_customer_pages import ALPHA, BETA, make
from .test_web import page_env, scrape, usage_states  # noqa: F401  (autouse：缓存清空、用量状态不查 CloudWatch)

ALPHA_NO, BETA_NO = str(LEDGER_ROWS[0][1]), str(LEDGER_ROWS[1][1])


def tab(client, number: str) -> str:
    response = client.get(f"/account/{number}/timeline")
    assert response.status_code == 200, response.headers.get("Location")
    return response.get_data(as_text=True)


def items(html: str) -> list[dict]:
    raw = re.search(r'<script id="tl-data" type="application/json"[^>]*>(.*?)</script>', html, re.S).group(1)
    return json.loads(raw)["items"]


def table(html: str) -> list[list[str]]:
    """事件表：每行 [日期, 事件, 内容, 客户, 谁记的]，最近的在上面。"""
    body = re.search(r'<table class="ev-table">.*?<tbody>(.*?)</tbody>', html, re.S).group(1)
    return [[scrape(cell) for cell in re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)]
            for row in re.findall(r"<tr>(.*?)</tr>", body, re.S)]


def events(kind: str) -> list[excel_source.CustomerEvent]:
    return [event for event in excel_source.load_events(force=True) if event.type == kind]


def test_页签在预估后面_配额最后(admin, fake_costs):
    html = tab(admin, ALPHA_NO)
    nav = re.search(r'<nav class="acct-tabs"[^>]*>(.*?)</nav>', html, re.S).group(1)
    assert [scrape(name) for name in re.findall(r'<a class="acct-tab[^"]*"[^>]*>(.*?)</a>', nav, re.S)] == [
        "摘要", "成本", "用量", "预估", "时间线", "配额",
    ]
    assert "时间线" in scrape(re.search(r'<a class="acct-tab is-on"[^>]*>(.*?)</a>', nav, re.S).group(1))


def test_没分过客户的账号_从启用那天开始(admin, fake_costs):
    html = tab(admin, ALPHA_NO)
    found = items(html)
    assert found[0]["kind"] == "start" and found[0]["who"] is None and found[0]["none"] == "库存"
    # 「操作日志」按钮带着号码去搜：上线以前改过的额度在那里
    assert f'href="/settings/audit?q={ALPHA_NO}"' in html
    assert table(html)[-1][:2] == [found[0]["date"], "启用账号"]


def test_解绑以后_调过的额度还追得回来(admin, fake_costs):
    """第 8 条：客户页的时间线上看得到哪天加了多少额度；解绑以后，账号页的时间线上照样在。"""
    make(admin)
    post(admin, "/customers/C001/assign", account=[ALPHA])
    before = by_account(ALPHA_NO).budget
    post(admin, "/customers/C001/budget", key=ALPHA, add="300000", note="客户补了 30 万")
    post(admin, "/customers/C001/unassign", key=ALPHA, note="分错了")
    assert by_account(ALPHA_NO).customer == ""

    html = tab(admin, ALPHA_NO)
    kinds = [item["kind"] for item in items(html)]
    assert kinds.index("assign") < kinds.index("budget") < kinds.index("unassign")
    budget = next(item for item in items(html) if item["kind"] == "budget")
    assert budget["who"]["label"] == "星河智能" and "客户补了 30 万" in budget["text"]
    assert f"${before:,.0f} → ${before + 300000:,.0f}" in budget["text"]
    row = next(row for row in table(html) if row[1] == "调整额度")
    assert row[3] == "星河智能"
    assert row[4].startswith(f"{TEST_USER} ")    # 谁、几点几分记的
    assert re.search(r"\d{4}-\d\d-\d\d \d\d:\d\d:\d\d$", row[4])


def test_库存里调额度也记_只在账号页(admin, fake_costs):
    target = by_account(ALPHA_NO)
    response = _edit(admin, target, budget="250000")
    assert response.status_code == 302
    [change] = events("budget")
    assert (change.customer, change.account, change.before, change.amount) == ("", ALPHA_NO, target.budget, 250000)
    assert change.actor and change.created_at is not None
    item = next(item for item in items(tab(admin, ALPHA_NO)) if item["kind"] == "budget")
    assert item["who"] is None and item["none"] == "库存"
    # 之后分给客户：客户页上只从分过去那天算，库存时调的额度不算这个客户的
    make(admin)
    post(admin, "/customers/C001/assign", account=[ALPHA])
    view = customers.build_views(excel_source.load_customers(force=True), excel_source.load_accounts(force=True),
                                 excel_source.load_events(force=True), change.date)["C001"]
    assert all(item.kind != "budget" for item in view.timeline)


def test_库存里标风控也记(admin, fake_costs):
    target = by_account(ALPHA_NO)
    _edit(admin, target, lifecycle=[*target.lifecycle, excel_source.TAG_RISK])
    [risk] = events("risk")
    assert (risk.customer, risk.account) == ("", ALPHA_NO)


def test_停用和恢复都记(admin, fake_costs):
    target = by_account(ALPHA_NO)
    post(admin, "/accounts/toggle", key=target.key, enabled="0")
    post(admin, "/accounts/toggle", key=target.key, enabled="1")
    assert [(e.type, e.customer) for e in excel_source.load_events(force=True)] == [("disable", ""), ("enable", "")]
    kinds = [item["kind"] for item in items(tab(admin, ALPHA_NO))]
    assert kinds[-2:] == ["disable", "enable"]


def test_替换账号_两个账号的时间线上都有_写着另一个是谁(admin, fake_costs):
    make(admin)
    post(admin, "/customers/C001/assign", account=[ALPHA])
    post(admin, "/customers/C001/replace", old=ALPHA, new=BETA, reason="风控")
    old = next(item for item in items(tab(admin, ALPHA_NO)) if item["kind"] == "replace")
    new = next(item for item in items(tab(admin, BETA_NO)) if item["kind"] == "replace")
    assert old["text"].startswith(f"换成 {by_account(BETA_NO).email}")
    assert new["text"].startswith(f"换下 {by_account(ALPHA_NO).email}")
    # 替换上来的账号：从那天起归这个客户
    assert new["who"]["label"] == "星河智能"


def test_台账里删掉的客户只写编号(admin, fake_costs, monkeypatch):
    make(admin)
    post(admin, "/customers/C001/assign", account=[ALPHA])
    monkeypatch.setattr(excel_source, "load_customers", lambda force=False: [])
    monkeypatch.setattr("bedrock_cost.account_pages.load_customers", lambda force=False: [])
    item = next(item for item in items(tab(admin, ALPHA_NO)) if item["kind"] == "assign")
    assert item["who"]["label"] == "C001"


def test_没有客户的事件也读得回来(ledger):
    """EVENTS 表里 CUSTOMER 空着、ACCOUNT 有号码的行：库存时记的，不丢；两样都空的才跳过。"""
    workbook = openpyxl.load_workbook(ledger)
    sheet = workbook.create_sheet(excel_source.EVENTS_SHEET)
    sheet.append(list(excel_source.EVENT_HEADER))
    sheet.append([1, "2026-09-01", None, ALPHA_NO, "budget", 2000, 1000, None, None, "manual", None, None, None, "admin"])
    sheet.append([2, "2026-09-02", None, None, "note", None, None, None, "没人认领", "manual", None, None, None, "admin"])
    workbook.save(ledger)
    [event] = excel_source.load_events(force=True)
    assert (event.id, event.customer, event.account, event.amount) == (1, "", ALPHA_NO, 2000)


def test_账号管理_每一行都能进时间线(admin):
    html = page(admin)
    for number in (ALPHA_NO, BETA_NO):
        assert f'href="/account/{number}/timeline"' in html


def test_账号管理_修改弹窗写着上一次调额度(admin, fake_costs):
    target = by_account(ALPHA_NO)
    _edit(admin, target, budget="250000")
    html = page(admin)
    hint = re.search(r'<span class="form-hint budget-hint">(.*?)</span>', html, re.S).group(1)
    text = scrape(hint)
    assert f"从 ${target.budget:,.0f} 调到 $250,000" in text and TEST_USER in text
    assert "调过 1 次，看时间线" in text and f'href="/account/{ALPHA_NO}/timeline"' in hint


def test_审计日志里的大数不写成科学计数(admin, fake_costs, ledger):
    make(admin)
    post(admin, "/customers/C001/assign", account=[ALPHA])
    post(admin, "/customers/C001/budget", key=ALPHA, add="1000000")
    log = (ledger.parent / excel_source.AUDIT_NAME).read_text(encoding="utf-8")
    assert "e+0" not in log
    before = by_account(ALPHA_NO).budget - 1000000
    assert f"的额度 {before:g} → {before + 1000000:.0f}" in log
