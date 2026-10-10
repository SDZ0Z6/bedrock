"""客户页：列表、详情和详情页上的动作（新建 / 修改客户、分配、替换、标记风控、结算、调整额度、解绑、
记一笔、改 / 删时间线上的事、导出对账单）。"""

from __future__ import annotations

import json
import re
from datetime import date, timedelta

import pytest

from bedrock_cost import config, excel_source

from .conftest import LEDGER_ROWS
from .test_accounts import EMAILS, admin, post, token  # noqa: F401  (admin 是 fixture)

ALPHA = f"{LEDGER_ROWS[0][1]}#2"
BETA = f"{LEDGER_ROWS[1][1]}#3"
CUSTOMER = {"name": "星河智能", "region": "CN", "avatar": "6", "status": "on", "since": "2026-06-03",
            "note": "按折算价算"}


def make(client, **extra) -> str:
    response = post(client, "/customers/create", **{**CUSTOMER, **extra})
    assert response.status_code == 302, response.get_data(as_text=True)[:400]
    return response.headers["Location"].rsplit("/", 1)[-1]


def detail(client, cid="C001") -> str:
    response = client.get(f"/customers/{cid}")
    assert response.status_code == 200
    return response.get_data(as_text=True)


def timeline(html: str) -> list[dict]:
    raw = re.search(r'<script id="tl-data" type="application/json"[^>]*>(.*?)</script>', html, re.S).group(1)
    return json.loads(raw)["items"]


def account(key: str) -> excel_source.Account:
    return next(a for a in excel_source.load_accounts(force=True, include_disabled=True) if a.key == key)


def test_侧边栏有客户入口(admin):
    html = admin.get("/accounts/").get_data(as_text=True)
    assert 'href="/customers/"' in html and "客户" in html


def test_还没有客户(admin, fake_costs):
    html = admin.get("/customers/").get_data(as_text=True)
    assert "还没有客户" in html
    assert "库存里有 2 个账号" in html


def test_新建客户_跳到详情(admin, fake_costs):
    assert make(admin) == "C001"
    html = detail(admin)
    assert "星河智能" in html and "按折算价算" in html
    assert "avatars/c06-deco.webp" in html            # 选了插画头像，banner 用带装饰的那张
    assert "还没有账号" in html
    assert [item["kind"] for item in timeline(html)] == ["signup"]


def test_没选头像用名字的第一个字(admin, fake_costs):
    make(admin, avatar="0", name="Kumo Studio")
    html = detail(admin)
    assert 'class="hero-letter cletter' in html and ">K</span>" in html


def test_新建客户校验不过_弹窗重新打开(admin, fake_costs):
    response = post(admin, "/customers/create", **{**CUSTOMER, "name": "", "since": "2099-01-01"})
    assert response.status_code == 400
    html = response.get_data(as_text=True)
    assert "客户名字不能为空" in html and "不能晚于今天" in html
    assert re.search(r'id="dlg-create"[^>]*data-reopen', html)
    assert excel_source.load_customers(force=True) == []


def test_重名的客户不行(admin, fake_costs):
    make(admin)
    response = post(admin, "/customers/create", **CUSTOMER)
    assert response.status_code == 400
    assert "已经有叫「星河智能」的客户了" in response.get_data(as_text=True)


def test_没带令牌不行(admin, fake_costs):
    response = admin.post("/customers/create", data=CUSTOMER)
    assert response.status_code == 302
    assert excel_source.load_customers(force=True) == []


def test_分配账号(admin, fake_costs):
    make(admin)
    response = post(admin, "/customers/C001/assign", account=[ALPHA], date=date.today().isoformat())
    assert response.status_code == 302
    assert account(ALPHA).customer == "C001"
    html = detail(admin)
    assert EMAILS["111111111111"] in html
    assert "使用中" in html
    kinds = [item["kind"] for item in timeline(html)]
    assert "assign" in kinds


def test_钱只算使用中的账号(admin, fake_costs):
    make(admin)
    post(admin, "/customers/C001/assign", account=[ALPHA, BETA])
    post(admin, "/customers/C001/risk", key=BETA, action="mark")
    html = detail(admin)
    budget = LEDGER_ROWS[0][2]
    assert f"预算 <strong>${budget:,.0f}</strong>" in html      # 只有 ALPHA 在用
    assert "风控 · 待替换" in html


def test_替换账号(admin, fake_costs):
    make(admin)
    post(admin, "/customers/C001/assign", account=[ALPHA])
    response = post(admin, "/customers/C001/replace", old=ALPHA, new=BETA, reason="风控")
    assert response.status_code == 302
    old, new = account(ALPHA), account(BETA)
    assert {excel_source.TAG_RISK, excel_source.TAG_SETTLE} <= set(old.lifecycle)
    assert new.customer == "C001"
    items = timeline(detail(admin))
    replace = next(item for item in items if item["kind"] == "replace")
    assert replace["who"]["number"] == old.account and replace["peer"]["number"] == new.account
    assert not replace["auto"]                                  # 手动记的，卡片是虚线框


def test_更多操作是浮层_不会被表格裁掉(admin, fake_costs):
    """「…」点开的菜单用原生 popover（顶层图层），不再是表格里的 <details>——表格的横向滚动框会把它裁掉。"""
    make(admin)
    post(admin, "/customers/C001/assign", account=[ALPHA])
    html = detail(admin)
    assert 'popovertarget="more-1"' in html and re.search(r'<div class="row-more-pop" id="more-1" popover>', html)
    assert "<details class=\"row-more\"" not in html


def test_结算_默认不停用(admin, fake_costs):
    make(admin)
    post(admin, "/customers/C001/assign", account=[ALPHA])
    post(admin, "/customers/C001/replace", old=ALPHA, new=BETA, reason="风控")
    response = post(admin, "/customers/C001/settle", key=ALPHA, note="都对完了")
    assert response.status_code == 302
    alpha = account(ALPHA)
    assert alpha.settled == date.today() and alpha.enabled
    html = detail(admin)
    assert "已结算 · 只留在历史里" in html
    settle = next(item for item in timeline(html) if item["kind"] == "settle")
    assert "都对完了" in settle["text"]


def test_结算时勾了停用(admin, fake_costs):
    make(admin)
    post(admin, "/customers/C001/assign", account=[ALPHA])
    post(admin, "/customers/C001/settle", key=ALPHA, disable="1")
    assert not account(ALPHA).enabled


def test_加额度_记时间戳(admin, fake_costs):
    make(admin)
    post(admin, "/customers/C001/assign", account=[ALPHA])
    before = account(ALPHA).budget
    response = post(admin, "/customers/C001/budget", key=ALPHA, add="300000", note="客户补了 30 万")
    assert response.status_code == 302
    assert account(ALPHA).budget == before + 300000
    [change] = [e for e in excel_source.load_events(force=True) if e.type == "budget"]
    assert (change.before, change.amount) == (before, before + 300000)
    assert change.created_at is not None and change.actor
    item = next(item for item in timeline(detail(admin)) if item["kind"] == "budget")
    assert "客户补了 30 万" in item["text"] and item["created"]


def test_加额度要填数字(admin, fake_costs):
    make(admin)
    post(admin, "/customers/C001/assign", account=[ALPHA])
    before = account(ALPHA).budget
    post(admin, "/customers/C001/budget", key=ALPHA, add="abc")
    post(admin, "/customers/C001/budget", key=ALPHA, add=str(-before - 1))
    assert account(ALPHA).budget == before


def test_解绑(admin, fake_costs):
    make(admin)
    post(admin, "/customers/C001/assign", account=[ALPHA])
    response = post(admin, "/customers/C001/unassign", key=ALPHA, note="分错了")
    assert response.status_code == 302
    assert account(ALPHA).customer == ""
    html = detail(admin)
    assert "还没有账号" in html
    assert any(item["kind"] == "unassign" for item in timeline(html))


def test_标记风控和取消(admin, fake_costs):
    make(admin)
    post(admin, "/customers/C001/assign", account=[ALPHA])
    post(admin, "/customers/C001/risk", key=ALPHA, action="mark", note="收到暂停通知")
    assert excel_source.TAG_RISK in account(ALPHA).lifecycle
    post(admin, "/customers/C001/risk", key=ALPHA, action="clear")
    assert excel_source.TAG_RISK not in account(ALPHA).lifecycle


def test_记一笔_改日期_删掉(admin, fake_costs):
    make(admin)
    post(admin, "/customers/C001/events", kind="note", note="客户说下周起量会翻倍")
    note = next(item for item in timeline(detail(admin)) if item["kind"] == "note")
    earlier = (date.today() - timedelta(days=3)).isoformat()
    post(admin, "/customers/C001/events/change", id=note["id"], action="date", date=earlier)
    note = next(item for item in timeline(detail(admin)) if item["kind"] == "note")
    assert note["date"] == earlier
    post(admin, "/customers/C001/events/change", id=note["id"], action="delete")
    assert not [item for item in timeline(detail(admin)) if item["kind"] == "note"]


def test_空的备注不记(admin, fake_costs):
    make(admin)
    post(admin, "/customers/C001/events", kind="note", note="  ")
    assert not [e for e in excel_source.load_events(force=True) if e.type == "note"]


def test_自动事件可以删(admin, fake_costs, monkeypatch):
    monkeypatch.setattr(config, "RAMPUP_DAILY", 100)        # 假数据每天 150 来块，让它算「开始上量」
    make(admin)
    post(admin, "/customers/C001/assign", account=[ALPHA], date=(date.today() - timedelta(days=20)).isoformat())
    rampup = next(item for item in timeline(detail(admin)) if item["kind"] == "rampup")
    assert rampup["auto"]
    post(admin, "/customers/C001/events/change", id=rampup["id"], action="delete")
    assert not [item for item in timeline(detail(admin)) if item["kind"] == "rampup"]
    [override] = [e for e in excel_source.load_events(force=True) if e.source == "auto"]
    assert override.deleted and override.type == "rampup"


def test_编辑客户(admin, fake_costs):
    make(admin)
    response = post(admin, "/customers/C001/update", **{**CUSTOMER, "name": "星河智能科技", "avatar": "0"})
    assert response.status_code == 302
    customer = excel_source.load_customers(force=True)[0]
    assert (customer.name, customer.avatar) == ("星河智能科技", 0)


def test_编辑客户校验不过(admin, fake_costs):
    make(admin)
    response = post(admin, "/customers/C001/update", **{**CUSTOMER, "name": ""})
    assert response.status_code == 400
    assert re.search(r'id="dlg-edit"[^>]*data-reopen', response.get_data(as_text=True))


def test_没有这个客户(admin, fake_costs):
    response = admin.get("/customers/C404")
    assert response.status_code == 302


def test_导出对账单(admin, fake_costs):
    make(admin)
    post(admin, "/customers/C001/assign", account=[ALPHA])
    response = admin.get("/customers/C001/statement.csv")
    assert response.status_code == 200
    assert response.mimetype == "text/csv"
    body = response.get_data(as_text=True)
    assert body.startswith("﻿客户,月份,账号邮箱")
    assert EMAILS["111111111111"] in body


def test_列表页有卡片和图(admin, fake_costs):
    make(admin)
    make(admin, name="Northwind Labs", region="US", avatar="0")
    post(admin, "/customers/C001/assign", account=[ALPHA])
    html = admin.get("/customers/").get_data(as_text=True)
    assert html.count('class="acct-card cust-card"') == 2
    assert "近 30 天消费" in html and "快用完的客户" in html and "按区域" in html
    assert 'data-region="US"' in html
    assert "库存里还有 1 个账号" in html


def test_详情页对账单对得上余额(admin, fake_costs):
    make(admin)
    post(admin, "/customers/C001/assign", account=[ALPHA, BETA])
    html = detail(admin)
    found = re.search(r"= <b>(-?\$[\d,]+)</b>，和现在的余额一样", html)
    assert found
    balance = re.search(r'<span data-countup>(-?\$[\d,]+)</span> <small>\d+ 个使用中的账号', html).group(1)
    assert found.group(1) == balance
