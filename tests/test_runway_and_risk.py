"""线上出过的两件事：

1. 「还能用几天」：账号刚风控、近 7 天只剩零星几分钱的消费时，余额 ÷ 日均是天文数字，加到今天上超出了
   日期的范围（OverflowError），客户详情页 500。现在：日均不到半分钱当没有消费；一年以上写「一年以上」，
   不算日期。（客户详情页上那张后来换成了「预估」，客户列表的「快用完的客户」还按它排。）
2. 把打着「风控」的库存账号分给客户（或者替换上来），生命周期被改成了「正常」——风控是 AWS 那边的事，
   分给谁都还在。现在：「风控」留着，只去掉上一段合作的「结算」，没有风控的才标「正常」。
"""

from __future__ import annotations

import re
from datetime import date, timedelta

from bedrock_cost import customers, excel_source, usage_explorer
from bedrock_cost.excel_source import Customer

from .conftest import LEDGER_ROWS
from .test_accounts import admin, by_account, post  # noqa: F401  (admin 是 fixture)
from .test_customers import ALPHA, BETA, account, event, make_customer, series_for
from .test_web import page_env, usage_states  # noqa: F401  (autouse)

TODAY = date(2026, 10, 9)



# ====================================================================== 还能用几天
def view_with(daily: float) -> customers.CustomerView:
    """一个客户、一个在用的账号（额度 100 万），近 30 天每日消费 daily。"""
    spend = {"100000000009": {(TODAY - timedelta(days=i)).isoformat(): daily for i in range(1, 31)}}
    usage_explorer_fake = series_for(spend)
    accounts = [account("100000000009", lifecycle=["正常"], start=date(2026, 6, 1))]
    events = [event(1, date(2026, 6, 1), "assign", "100000000009", amount=1_000_000.0)]
    original = usage_explorer.account_series
    usage_explorer.account_series = usage_explorer_fake
    try:
        return customers.build_views([Customer(id="C001", name="星河智能", since=date(2026, 6, 1))], accounts,
                                     events, TODAY, with_alerts=False)["C001"]
    finally:
        usage_explorer.account_series = original


def test_日均不到半分钱_当最近没有消费():
    view = view_with(0.001)
    assert view.days_left is None and not view.long_runway and view.runs_out_on is None


def test_一年以上_不算日期():
    view = view_with(1.0)                     # 余额快 100 万、每天 1 块：两千多年
    assert view.days_left > customers.LONG_RUNWAY_DAYS
    assert view.long_runway and view.runs_out_on is None


def test_一年以内_照常算日期():
    view = view_with(10_000.0)
    assert not view.long_runway
    assert view.runs_out_on == TODAY + timedelta(days=int(view.days_left))


def test_日均很低_客户页和客户列表都打得开(admin, fake_costs, monkeypatch):
    make_customer()
    post(admin, "/customers/C001/assign", account=[ALPHA])
    number = by_account(str(LEDGER_ROWS[0][1])).account
    spend = {number: {(date.today() - timedelta(days=i)).isoformat(): 1e-6 for i in range(1, 60)}}
    monkeypatch.setattr(usage_explorer, "account_series", series_for(spend))
    assert admin.get("/customers/C001").status_code == 200
    assert admin.get("/customers/").status_code == 200
    # 每天 5 毛：一年以上才用完，列表的「快用完的客户」写「一年以上」，不算日期
    spend[number] = {(date.today() - timedelta(days=i)).isoformat(): 0.5 for i in range(1, 60)}
    html = admin.get("/customers/?refresh=1").get_data(as_text=True)
    assert "一年以上" in html


# ====================================================================== 风控的账号分出去还是风控
def tags(key: str) -> tuple[str, ...]:
    return next(a for a in excel_source.load_accounts(force=True) if a.key == key).lifecycle


def test_分配风控的账号_风控留着_不标正常(ledger):
    make_customer()
    excel_source.set_lifecycle(ALPHA, ["风控", "结算"], actor="tester")
    excel_source.assign_accounts("C001", [ALPHA], date(2026, 6, 5), actor="tester")
    assert tags(ALPHA) == ("风控",)                 # 上一段合作的「结算」去掉，「风控」留着
    assert next(a for a in excel_source.load_accounts(force=True) if a.key == ALPHA).customer == "C001"


def test_分配没风控的账号_标正常_去掉结算(ledger):
    make_customer()
    excel_source.set_lifecycle(ALPHA, ["结算"], actor="tester")
    excel_source.assign_accounts("C001", [ALPHA], date(2026, 6, 5), actor="tester")
    assert tags(ALPHA) == ("正常",)


def test_替换上来的是风控的_风控留着(ledger):
    make_customer()
    excel_source.assign_accounts("C001", [ALPHA], date(2026, 6, 5))
    excel_source.set_lifecycle(BETA, ["风控"], actor="tester")
    excel_source.replace_account("C001", ALPHA, BETA, "风控", date(2026, 6, 9), risky=True)
    assert tags(BETA) == ("风控",)


def test_库存里风控的排在最后():
    accounts = [account("1", customer="", lifecycle=["风控"]), account("2", customer=""), account("3", customer="")]
    assert [a.account for a in customers.stock(accounts)] == ["2", "3", "1"]


def test_分配弹窗里风控的账号标出来(admin, fake_costs):
    make_customer()
    excel_source.set_lifecycle(BETA, ["风控"], actor="tester")
    html = admin.get("/customers/C001").get_data(as_text=True)
    dialog = re.search(r'<dialog class="modal" id="dlg-assign">(.*?)</dialog>', html, re.S).group(1)
    assert dialog.count('class="pick-flag"') == 1
    # 替换的默认选项不是风控的那个（排在最后）
    assert dialog.index(str(LEDGER_ROWS[0][1])) < dialog.index(str(LEDGER_ROWS[1][1]))
