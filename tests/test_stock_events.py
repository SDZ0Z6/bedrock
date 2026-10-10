"""账号在库存里时记的事，在账号页的时间线上能改日期、删掉。

库存里标风控、调额度、停用 / 恢复、恢复使用都记在 EVENTS 表里（CUSTOMER 空着），只在账号页的时间线上。那时候
账号没有客户，不连着谁的钱和阶段，也没有客户页可改——以前点开只有「关闭」，标错了、日期不对都没处改。
现在和「记一笔」那几类一样点开就能改日期、删掉；删掉只是不在时间线上显示，生命周期、额度照旧。
分给了客户以后记的（「记一笔」那几类除外）还是去客户页改。
"""

from __future__ import annotations

from datetime import date, timedelta

import openpyxl

from bedrock_cost import excel_source

from .conftest import LEDGER_ROWS
from .test_account_timeline import items, tab
from .test_accounts import admin, by_account, post  # noqa: F401  (admin 是 fixture)
from .test_customer_pages import ALPHA, make
from .test_web import page_env, usage_states  # noqa: F401  (autouse)

ALPHA_NO, BETA_NO = str(LEDGER_ROWS[0][1]), str(LEDGER_ROWS[1][1])


def events(kind: str) -> list[excel_source.CustomerEvent]:
    return [e for e in excel_source.load_events(force=True) if e.type == kind]


def cards(client, number: str) -> dict[str, dict]:
    """时间线上的卡片（交给 timeline.js 的那份），按类别；每类只有一张。"""
    return {item["kind"]: item for item in items(tab(client, number))}


def audit(ledger) -> str:
    return (ledger.parent / excel_source.AUDIT_NAME).read_text(encoding="utf-8")


def test_库存里标的风控_账号页能改日期能删(admin, fake_costs, ledger):
    post(admin, f"/account/{BETA_NO}/events", kind="risk", note="AWS 暂停")
    [risk] = events("risk")
    assert risk.customer == ""
    card = cards(admin, BETA_NO)["risk"]
    assert card["editable"] and card["none"] == "库存"
    earlier = date.today() - timedelta(days=2)
    post(admin, f"/account/{BETA_NO}/events/change", id=str(risk.id), action="date", date=earlier.isoformat())
    assert events("risk")[0].date == earlier
    post(admin, f"/account/{BETA_NO}/events/change", id=str(risk.id), action="delete")
    assert events("risk")[0].deleted
    assert "risk" not in cards(admin, BETA_NO)
    assert "风控" in by_account(BETA_NO).lifecycle                # 删掉只是不显示，标签还在
    log = audit(ledger)
    assert f"#{risk.id}「标记风控」 的日期 {date.today().isoformat()} → {earlier.isoformat()}" in log
    assert f"删掉 #{risk.id}「标记风控」（日期 {earlier.isoformat()}）" in log


def test_账号管理里打的风控_没写内容的那条也能删(admin, fake_costs):
    # 截图里那条：账号管理里把库存账号的生命周期改成风控，时间线上跟着记的，没有内容
    post(admin, "/accounts/lifecycle", key=by_account(BETA_NO).key, lifecycle=["风控"])
    [risk] = events("risk")
    assert (risk.customer, risk.note, risk.amount) == ("", "", None)
    assert cards(admin, BETA_NO)["risk"]["editable"]
    post(admin, f"/account/{BETA_NO}/events/change", id=str(risk.id), action="delete")
    assert events("risk")[0].deleted


def test_库存里打着风控_弹窗提示怎么去掉(admin, fake_costs):
    tip = 'data-risk-tip="要去掉「风控」，点页头的「恢复使用」。"'
    assert tip not in tab(admin, BETA_NO)
    post(admin, f"/account/{BETA_NO}/events", kind="risk")
    assert tip in tab(admin, BETA_NO)
    # 分给了客户的不提示：那边恢复使用连着客户的余额，去客户页看清楚再点
    make(admin)
    post(admin, "/customers/C001/assign", account=[ALPHA])
    post(admin, f"/account/{ALPHA_NO}/events", kind="risk")
    assert tip not in tab(admin, ALPHA_NO)


def test_分给客户以后标的风控_还是去客户页改(admin, fake_costs):
    make(admin)
    post(admin, "/customers/C001/assign", account=[ALPHA])
    post(admin, f"/account/{ALPHA_NO}/events", kind="risk")
    [risk] = events("risk")
    assert risk.customer == "C001"
    assert cards(admin, ALPHA_NO)["risk"]["editable"] is False
    post(admin, f"/account/{ALPHA_NO}/events/change", id=str(risk.id), action="delete")
    assert not events("risk")[0].deleted


def test_后来分给了客户_库存时记的还在账号页改(admin, fake_costs):
    # 库存里标了风控、又恢复使用，后来分给客户：看的是记下来那时候在不在库存，不是现在
    post(admin, f"/account/{ALPHA_NO}/events", kind="risk", note="AWS 暂停")
    post(admin, f"/account/{ALPHA_NO}/restore", note="解除了")
    make(admin)
    post(admin, "/customers/C001/assign", account=[ALPHA])
    mine = cards(admin, ALPHA_NO)
    assert mine["risk"]["editable"] and mine["restore"]["editable"]
    assert not mine["assign"]["editable"]
    [restore] = events("restore")
    post(admin, f"/account/{ALPHA_NO}/events/change", id=str(restore.id), action="delete")
    assert events("restore")[0].deleted


def test_台账里手写的类别_库存里的照样删得掉(admin, fake_costs, ledger):
    # 有人直接在 Excel 里记了一行，类别不在表里：以前碰不到它，现在能删，操作日志照原样写类别，不报错
    workbook = openpyxl.load_workbook(ledger)
    sheet = workbook.create_sheet(excel_source.EVENTS_SHEET)
    sheet.append(list(excel_source.EVENT_HEADER))
    sheet.append([1, "2026-09-01", None, BETA_NO, "对账", None, None, None, "对过一次", "manual", None, None, None,
                  "admin"])
    workbook.save(ledger)
    excel_source.load_events(force=True)
    [card] = [item for item in items(tab(admin, BETA_NO)) if item["id"] == "1"]
    assert card["editable"]
    response = post(admin, f"/account/{BETA_NO}/events/change", id="1", action="delete")
    assert response.status_code == 302
    assert [event.deleted for event in excel_source.load_events(force=True)] == [True]
    assert "删掉 #1「对账」（日期 2026-09-01）" in audit(ledger)
