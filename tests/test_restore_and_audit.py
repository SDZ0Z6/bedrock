"""第四轮反馈：

1. 记一笔里选账号，邮箱后面带上 ID；
2. 名下账号能「恢复使用」：风控、被换下（待结算）的账号回到使用中（AWS 解除了风控、换下来的还要接着用）。
   没用完的额度重新算进余额，对账单上那次「移出」不再算；时间线上之前的记录都留着，再记一条「恢复使用」。
   已经结算的、额度用完的不行。账号页的页头上也有这个按钮；
3. 操作日志里账号写成「邮箱（号码）」，客户写成「名字（编号）」，带日期的动作写上日期。
"""

from __future__ import annotations

import re
from datetime import date, timedelta

import pytest

from bedrock_cost import customers, excel_source, settings_pages

from .conftest import LEDGER_ROWS
from .test_accounts import _edit, admin, by_account, post  # noqa: F401  (admin 是 fixture)
from .test_account_timeline import tab
from .test_customer_pages import ALPHA, BETA, detail, make
from .test_web import page_env, scrape, usage_states  # noqa: F401  (autouse)

ALPHA_NO, BETA_NO = str(LEDGER_ROWS[0][1]), str(LEDGER_ROWS[1][1])
ONE = f"acct-one@example.com（{ALPHA_NO}）"
TWO = f"acct-two@example.com（{BETA_NO}）"
STAR = "星河智能（C001）"


def events(kind: str) -> list[excel_source.CustomerEvent]:
    return [e for e in excel_source.load_events(force=True) if e.type == kind]


def log(ledger) -> str:
    return (ledger.parent / excel_source.AUDIT_NAME).read_text(encoding="utf-8")


def holding(number: str) -> customers.Holding:
    accounts = excel_source.load_accounts(force=True, include_disabled=True)
    [customer] = excel_source.load_customers(force=True)
    view = customers.build_views([customer], accounts, excel_source.load_events(force=True), date.today(),
                                 with_alerts=False)[customer.id]
    return next(h for h in view.holdings if h.number == number)


def menu(html: str, number: str) -> str:
    """名下账号里这个账号那一行的「更多」菜单。"""
    row = re.search(rf'<tr data-stage="\w+" data-q="[^"]*{number}".*?</tr>', html, re.S).group(0)
    return re.search(r'<div class="row-more-pop"[^>]*>(.*?)</div>', row, re.S).group(1)


# ====================================================================== 1. 记一笔的账号带 ID
def test_记一笔里的账号带上ID(admin, fake_costs):
    make(admin)
    post(admin, "/customers/C001/assign", account=[ALPHA])
    dialog = re.search(r'<dialog class="modal modal-sm" id="dlg-note">(.*?)</dialog>', detail(admin), re.S).group(1)
    option = re.search(rf'<option value="{ALPHA_NO}"[^>]*>(.*?)</option>', dialog, re.S).group(1)
    assert scrape(option) == f"acct-one@example.com · {ALPHA_NO}"


# ====================================================================== 2. 恢复使用
def test_风控的账号能恢复使用(admin, fake_costs, ledger):
    make(admin)
    post(admin, "/customers/C001/assign", account=[ALPHA])
    post(admin, "/customers/C001/risk", key=ALPHA, action="mark")
    flagged = holding(ALPHA_NO)
    assert flagged.stage == "risk" and flagged.restorable
    assert "恢复使用" in menu(detail(admin), ALPHA_NO)
    when = date.today() - timedelta(days=1)
    response = post(admin, "/customers/C001/restore", key=ALPHA, date=when.isoformat(), note="AWS 解除了暂停")
    assert response.status_code == 302
    assert by_account(ALPHA_NO).lifecycle == ("正常",)
    back = holding(ALPHA_NO)
    assert back.stage == "use" and back.left is None
    [restore] = events("restore")
    assert (restore.customer, restore.account, restore.date, restore.note) == ("C001", ALPHA_NO, when, "AWS 解除了暂停")
    # 就当它没离开过：对账单上没有「移出」，余额又算上它
    [view] = customers.build_views(excel_source.load_customers(force=True),
                                   excel_source.load_accounts(force=True, include_disabled=True),
                                   excel_source.load_events(force=True), date.today(), with_alerts=False).values()
    assert sum(row.out for row in view.statement) == 0 and view.balance == pytest.approx(back.remaining)
    assert "restore" in [item.kind for item in view.timeline]
    assert f"客户 {STAR}：账号 {ONE} 恢复使用，日期 {when.isoformat()}" in log(ledger)


def test_换下来的账号能恢复_换上来的不受影响(admin, fake_costs):
    make(admin)
    post(admin, "/customers/C001/assign", account=[ALPHA])
    post(admin, "/customers/C001/replace", old=ALPHA, new=BETA, reason="风控")
    assert set(by_account(ALPHA_NO).lifecycle) == {"风控", "结算"}
    html = detail(admin)
    assert '"replaced_by": "acct-two@example.com"' in html                 # 弹窗里说一声换上来的是谁
    post(admin, "/customers/C001/restore", key=ALPHA)
    assert (holding(ALPHA_NO).stage, holding(BETA_NO).stage) == ("use", "use")


def test_已结算的不能恢复(admin, fake_costs):
    make(admin)
    post(admin, "/customers/C001/assign", account=[ALPHA])
    post(admin, "/customers/C001/risk", key=ALPHA, action="mark")
    post(admin, "/customers/C001/settle", key=ALPHA)
    assert holding(ALPHA_NO).stage == "settled" and not holding(ALPHA_NO).restorable
    post(admin, "/customers/C001/restore", key=ALPHA)
    assert events("restore") == [] and "风控" in by_account(ALPHA_NO).lifecycle
    assert "恢复使用" not in menu(detail(admin), ALPHA_NO)


def test_额度用完的不能恢复_先调整额度(admin, fake_costs):
    make(admin)
    post(admin, "/customers/C001/assign", account=[ALPHA])
    post(admin, "/customers/C001/replace", old=ALPHA, new=BETA, reason="额度用完")
    excel_source.set_account_budget(ALPHA, 1.0, actor="tester")          # 早就用完了
    assert not holding(ALPHA_NO).restorable
    post(admin, "/customers/C001/restore", key=ALPHA)
    assert events("restore") == [] and "结算" in by_account(ALPHA_NO).lifecycle
    # 账号页的按钮也挡：去掉标签也还是待结算
    post(admin, f"/account/{ALPHA_NO}/restore")
    assert events("restore") == [] and "结算" in by_account(ALPHA_NO).lifecycle


def test_使用中的没有恢复使用(admin, fake_costs):
    make(admin)
    post(admin, "/customers/C001/assign", account=[ALPHA])
    assert "恢复使用" not in menu(detail(admin), ALPHA_NO)
    assert 'type="button" data-open-restore>' not in tab(admin, ALPHA_NO)      # 页头没有这个按钮


def test_恢复以后又风控_从这次风控算起(admin, fake_costs):
    today = date.today()
    make(admin)
    post(admin, "/customers/C001/assign", account=[ALPHA])
    post(admin, "/customers/C001/risk", key=ALPHA, action="mark", date=(today - timedelta(days=6)).isoformat())
    post(admin, "/customers/C001/restore", key=ALPHA, date=(today - timedelta(days=4)).isoformat())
    post(admin, "/customers/C001/risk", key=ALPHA, action="mark", date=(today - timedelta(days=2)).isoformat())
    again = holding(ALPHA_NO)
    assert (again.stage, again.left, again.left_why) == ("risk", today - timedelta(days=2), "风控")


def test_账号页的恢复使用(admin, fake_costs, ledger):
    make(admin)
    post(admin, "/customers/C001/assign", account=[ALPHA])
    post(admin, "/customers/C001/risk", key=ALPHA, action="mark")
    html = tab(admin, ALPHA_NO)
    head = re.search(r'<header class="page-head acct-page-head">(.*?)</header>', html, re.S).group(1)
    assert "data-open-restore" in head and scrape(head).index("恢复使用") < scrape(head).index("调整额度")
    assert f'action="/account/{ALPHA_NO}/restore"' in html and "回到星河智能的使用中" in scrape(html)
    response = post(admin, f"/account/{ALPHA_NO}/restore", next=f"/account/{ALPHA_NO}/timeline", note="解除了")
    assert response.headers["Location"].endswith(f"/account/{ALPHA_NO}/timeline")     # 回到原来那个页签
    assert holding(ALPHA_NO).stage == "use"
    assert [(e.customer, e.note) for e in events("restore")] == [("C001", "解除了")]


def test_账号页_库存里的风控账号也能恢复(admin, fake_costs, ledger):
    excel_source.set_lifecycle(BETA, ["风控"], actor="tester")
    assert 'type="button" data-open-restore>' in tab(admin, BETA_NO)
    post(admin, f"/account/{BETA_NO}/restore")
    assert by_account(BETA_NO).lifecycle == ("正常",)
    [restore] = events("restore")
    assert (restore.customer, restore.account) == ("", BETA_NO)
    assert f"账号 {TWO}（库存）恢复使用，日期 {date.today().isoformat()}" in log(ledger)


def test_账号页_不能拿恢复跳到别的网站(admin, fake_costs):
    excel_source.set_lifecycle(BETA, ["风控"], actor="tester")
    response = post(admin, f"/account/{BETA_NO}/restore", next="//evil.example.com/")
    assert response.headers["Location"].endswith(f"/account/{BETA_NO}/")


# ====================================================================== 3. 操作日志
def test_日志里事件的说法和时间线一样():
    for kind, title in excel_source.EVENT_TITLES.items():
        assert customers.EVENT_TYPES[kind][0] == title
    assert set(excel_source.EVENT_TITLES) == set(customers.EVENT_TYPES)


def test_日志写邮箱_客户名_日期(admin, fake_costs, ledger):
    make(admin)
    when = (date.today() - timedelta(days=3)).isoformat()
    post(admin, "/customers/C001/assign", account=[ALPHA, BETA], date=when)
    post(admin, "/customers/C001/risk", key=ALPHA, action="mark", date=when)
    post(admin, "/customers/C001/events", kind="note", account=BETA_NO, note="客户说要加量", date=when)
    [note] = events("note")
    post(admin, "/customers/C001/events/change", id=str(note.id), action="delete")
    text = log(ledger)
    assert "新建客户 星河智能（C001）" in text
    assert f"客户 {STAR}：分配账号 {ONE}、{TWO}，日期 {when}" in text
    assert f"客户 {STAR}：账号 {ONE} 标记风控，日期 {when}" in text
    assert f"客户 {STAR} 的时间线记一笔：#{note.id}「备注」，账号 {TWO}，日期 {when}" in text
    assert f"客户 {STAR} 的时间线：删掉 #{note.id}「备注」（账号 {TWO}），原本的日期 {when}" in text


def test_账号管理里改客户_写客户名(admin, fake_costs, ledger):
    make(admin)
    _edit(admin, by_account(ALPHA_NO), customer="C001")
    assert f"修改账号 {ONE}：" in log(ledger) and f"客户 库存 → {STAR}" in log(ledger)


def test_库存账号在账号页记一笔_日志归到账号(admin, fake_costs, ledger):
    post(admin, f"/account/{ALPHA_NO}/events", kind="rampup", note="压测")
    [rampup] = events("rampup")
    line = f"账号 {ONE}（库存）的时间线记一笔：#{rampup.id}「开始上量」，日期 {date.today().isoformat()}"
    assert line in log(ledger)
    assert settings_pages._kind(line) == "账号"
    # 老格式的日志照样分得对
    assert settings_pages._kind("账号 111111111111（客户 C001）标记风控") == "客户"
    assert settings_pages._kind("停用账号 111111111111") == "账号"
