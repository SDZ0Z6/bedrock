"""换下、风控的账号离开以后还有零星的账：客户页「每天消费」、近 7 天日均（「还能用几天」）、客户列表的近 30 天
消费都不算它离开以后的（只算它算这个客户的那几天）；历史消费、对账单、结算照样按这个账号全部的账算。
"""

from __future__ import annotations

import json
import re
from datetime import date, timedelta

import pytest

from bedrock_cost import customers, usage_explorer
from bedrock_cost.excel_source import Customer

from .conftest import LEDGER_ROWS
from .test_accounts import admin, post  # noqa: F401  (admin 是 fixture)
from .test_customer_pages import ALPHA, BETA, detail, make
from .test_customers import TODAY, account, event, series_for
from .test_web import page_env, usage_states  # noqa: F401  (autouse)

OLD, NEW = "100000000001", "100000000002"
REPLACED = date(2026, 10, 1)


@pytest.fixture
def swapped(monkeypatch):
    """OLD 9-01 分过来、每天 1000；10-01 风控换成 NEW（每天 500）。OLD 换下以后每天还有 300 的零星账。"""
    spend = {
        OLD: {(date(2026, 9, 1) + timedelta(days=i)).isoformat(): 1000.0 if date(2026, 9, 1) + timedelta(days=i) < REPLACED
              else 300.0 for i in range(38)},                         # 9-01 ~ 10-08
        NEW: {(REPLACED + timedelta(days=i)).isoformat(): 500.0 for i in range(8)},   # 10-01 ~ 10-08
    }
    monkeypatch.setattr(usage_explorer, "account_series", series_for(spend))
    accounts = [account(OLD, lifecycle=["风控", "结算"], start=date(2026, 9, 1), row=2),
                account(NEW, lifecycle=["正常"], start=REPLACED, row=3)]
    history = [
        event(1, date(2026, 9, 1), "assign", OLD, amount=1_000_000.0),
        event(2, REPLACED, "risk", OLD),
        event(3, REPLACED, "replace", OLD, peer=NEW, amount=1_000_000.0, note="风控"),
    ]
    view = customers.build_views([Customer(id="C001", name="星河智能", since=date(2026, 9, 1))], accounts, history,
                                 TODAY, with_alerts=False)["C001"]
    return view


def test_离开以后的账不画进每天消费(swapped):
    stamps, per_account = swapped.daily(30)
    by_day = dict(zip(stamps, per_account[OLD]))
    assert by_day[REPLACED - timedelta(days=1)] == 1000.0         # 换下前一天照算
    assert all(by_day[day] == 0.0 for day in stamps if day >= REPLACED)
    assert sum(per_account[NEW]) == 500.0 * 8


def test_近7天日均只算在用的(swapped):
    """10-02 ~ 10-08：OLD 每天还有 300 的账，不算；NEW 每天 500。"""
    assert swapped.avg7 == pytest.approx(500.0)
    assert swapped.days_left == pytest.approx(swapped.balance / 500.0)


def test_历史消费和对账单照样按全部的账(swapped):
    old = next(h for h in swapped.holdings if h.number == OLD)
    assert old.left == REPLACED and old.stage == "pending"
    assert old.spent == 1000.0 * 30 + 300.0 * 8                  # 结算要和上游对的是整个账号的账
    assert swapped.lifetime == pytest.approx(old.spent + 500.0 * 8)
    assert sum(row.used for row in swapped.statement) == pytest.approx(swapped.lifetime)


def test_在用的账号_从分过来那天起都算():
    holding = customers.Holding(account=account(NEW, start=REPLACED), series=customers.Series(
        dates=[(REPLACED + timedelta(days=i)).isoformat() for i in range(3)], raw=[1.0] * 3, marked=[5.0] * 3),
        joined=REPLACED)
    assert holding.active_daily(REPLACED - timedelta(days=1), REPLACED + timedelta(days=2)) == [0.0, 5.0, 5.0, 5.0]


def test_客户页每天消费的图(admin, fake_costs, monkeypatch):
    today = date.today()
    left = today - timedelta(days=5)
    numbers = {ALPHA: str(LEDGER_ROWS[0][1]), BETA: str(LEDGER_ROWS[1][1])}
    spend = {numbers[ALPHA]: {(today - timedelta(days=i)).isoformat(): 100.0 for i in range(1, 20)},
             numbers[BETA]: {(today - timedelta(days=i)).isoformat(): 50.0 for i in range(1, 20)}}
    monkeypatch.setattr(usage_explorer, "account_series", series_for(spend, start=today - timedelta(days=60)))
    make(admin)
    post(admin, "/customers/C001/assign", account=[ALPHA, BETA], date=(today - timedelta(days=20)).isoformat())
    post(admin, "/customers/C001/risk", key=ALPHA, action="mark", date=left.isoformat())
    html = detail(admin)
    buckets = json.loads(re.search(r'<script id="cust-spend-data" type="application/json">(.*?)</script>', html, re.S).group(1))
    by_label = {bucket["label"]: bucket for bucket in buckets}
    gone = left.strftime("%m-%d")
    before = (left - timedelta(days=1)).strftime("%m-%d")
    assert {row["name"] for row in by_label[before]["rows"]} == {"acct-one", "acct-two"}
    assert {row["name"] for row in by_label[gone]["rows"]} == {"acct-two"}     # 风控那天起 acct-one 不画了
    assert "按账号叠起来，只算在用的那几天" in html
