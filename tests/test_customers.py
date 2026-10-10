"""客户：台账里的客户 / 时间线读写、账号的阶段、客户的钱、自动事件、月度对账单。"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import openpyxl
import pytest

from bedrock_cost import config, cost_history, customers, events as alert_events, excel_source, usage_explorer
from bedrock_cost.excel_source import Account, Customer, CustomerEvent, LedgerConflict

from .conftest import LEDGER_ROWS

TODAY = date(2026, 10, 9)
ALPHA = f"{LEDGER_ROWS[0][1]}#2"
BETA = f"{LEDGER_ROWS[1][1]}#3"


def make_customer(name="星河智能", **extra) -> str:
    data = {"name": name, "region": "CN", "avatar": 6, "status": "on", "since": date(2026, 6, 3), "note": ""}
    data.update(extra)
    return excel_source.create_customer(data, actor="tester")


def account(number: str, *, budget=1_000_000.0, customer="C001", lifecycle=(), settled=None, enabled=True,
            start=date(2026, 6, 1), row=2) -> Account:
    return Account(partner="Jeff", account=number, budget=budget, tag_ratio=1.0, untag_ratio=1.0,
                   ak="AKIAFAKE00000000", sk="x" * 40, row=row, start_date=start, enabled=enabled,
                   email=f"{number[-4:]}@example.com", lifecycle=tuple(lifecycle), customer=customer,
                   settled=settled)


def event(ident, when, kind, account="", **extra) -> CustomerEvent:
    return CustomerEvent(id=ident, date=when, customer="C001", account=account, type=kind, **extra)


def series_for(spend: dict[str, dict[str, float]], start: date = date(2026, 6, 1)):
    """假的 usage_explorer.account_series：spend 是 {号码: {日期: 折算后}}，没写的天是 0。"""

    def fake(account, begin, end, granularity="daily", refresh=False):
        days = (end - begin).days + 1
        dates = [(begin + timedelta(days=i)).isoformat() for i in range(days)]
        values = spend.get(account.account, {})
        marked = [values.get(day, 0.0) for day in dates]
        return dates, [value * 0.9 for value in marked], marked, False, None

    return fake


# ==================================================================== 台账
def test_新建客户按顺序编号(ledger):
    assert make_customer() == "C001"
    assert make_customer("Northwind Labs", region="US", avatar=0) == "C002"
    found = {c.id: c for c in excel_source.load_customers(force=True)}
    assert found["C001"].name == "星河智能"
    assert found["C001"].avatar == 6
    assert found["C001"].since == date(2026, 6, 3)
    assert found["C002"].avatar == 0
    assert found["C002"].status == "on"
    assert found["C002"].status_label == "合作中"


def test_改客户资料_没变就不写(ledger):
    make_customer()
    note = excel_source.update_customer("C001", {
        "name": "星河智能（新）", "region": "HK", "avatar": 0, "status": "pause", "since": date(2026, 6, 3), "note": "x",
    })
    assert "NAME" in note and "REGION" in note
    customer = excel_source.load_customers(force=True)[0]
    assert (customer.name, customer.region, customer.avatar, customer.status) == ("星河智能（新）", "HK", 0, "pause")
    again = excel_source.update_customer("C001", {
        "name": "星河智能（新）", "region": "HK", "avatar": 0, "status": "pause", "since": date(2026, 6, 3), "note": "x",
    })
    assert again == ""


def test_分配账号_写上客户_标正常_记一条(ledger):
    make_customer()
    excel_source.assign_accounts("C001", [ALPHA], date(2026, 6, 5), actor="tester")
    alpha = next(a for a in excel_source.load_accounts(force=True) if a.key == ALPHA)
    assert alpha.customer == "C001"
    assert excel_source.TAG_NORMAL in alpha.lifecycle
    [assigned] = [e for e in excel_source.load_events() if e.type == "assign"]
    assert (assigned.account, assigned.amount, assigned.date) == (alpha.account, alpha.budget, date(2026, 6, 5))
    assert assigned.actor == "tester"


def test_分配已经有客户的账号会被拒(ledger):
    make_customer()
    make_customer("别的客户")
    excel_source.assign_accounts("C001", [ALPHA], TODAY)
    with pytest.raises(LedgerConflict):
        excel_source.assign_accounts("C002", [ALPHA], TODAY)


def test_标记风控和取消风控(ledger):
    make_customer()
    excel_source.assign_accounts("C001", [ALPHA], TODAY)
    excel_source.mark_risk("C001", ALPHA, TODAY, spent=312_400)
    alpha = next(a for a in excel_source.load_accounts(force=True) if a.key == ALPHA)
    assert excel_source.TAG_RISK in alpha.lifecycle and excel_source.TAG_NORMAL not in alpha.lifecycle
    assert excel_source.mark_risk("C001", ALPHA, TODAY) == ""      # 已经是风控
    excel_source.clear_risk("C001", ALPHA, TODAY)
    alpha = next(a for a in excel_source.load_accounts(force=True) if a.key == ALPHA)
    assert excel_source.TAG_RISK not in alpha.lifecycle and excel_source.TAG_NORMAL in alpha.lifecycle
    kinds = [e.type for e in excel_source.load_events()]
    assert kinds == ["assign", "risk", "unrisk"]


def test_替换_旧的待结算_新的分过来(ledger):
    make_customer()
    excel_source.assign_accounts("C001", [ALPHA], TODAY)
    excel_source.replace_account("C001", ALPHA, BETA, "风控", TODAY, spent=100.0, risky=True)
    accounts = {a.key: a for a in excel_source.load_accounts(force=True)}
    old, new = accounts[ALPHA], accounts[BETA]
    assert {excel_source.TAG_RISK, excel_source.TAG_SETTLE} <= set(old.lifecycle)
    assert new.customer == "C001" and excel_source.TAG_NORMAL in new.lifecycle
    kinds = [(e.type, e.account, e.peer) for e in excel_source.load_events()]
    assert ("risk", old.account, "") in kinds
    assert ("replace", old.account, new.account) in kinds


def test_替换的新账号必须在库存(ledger):
    make_customer()
    excel_source.assign_accounts("C001", [ALPHA, BETA], TODAY)
    with pytest.raises(LedgerConflict):
        excel_source.replace_account("C001", ALPHA, BETA, "风控", TODAY)


def test_结算_写日期_可选停用(ledger):
    make_customer()
    excel_source.assign_accounts("C001", [ALPHA, BETA], TODAY)
    excel_source.settle_account("C001", ALPHA, TODAY, spent=1234.5)
    excel_source.settle_account("C001", BETA, TODAY, disable=True)
    accounts = {a.key: a for a in excel_source.load_accounts(force=True, include_disabled=True)}
    assert accounts[ALPHA].settled == TODAY and accounts[ALPHA].enabled
    assert accounts[BETA].settled == TODAY and not accounts[BETA].enabled
    assert excel_source.TAG_SETTLE in accounts[ALPHA].lifecycle
    kinds = [e.type for e in excel_source.load_events()]
    assert kinds.count("settle") == 2 and kinds.count("disable") == 1
    with pytest.raises(LedgerConflict):    # 结算过的不能再结
        excel_source.settle_account("C001", ALPHA, TODAY)


def test_加额度_记改前改后_额度用完的可以回到使用中(ledger):
    make_customer()
    excel_source.assign_accounts("C001", [ALPHA], TODAY)
    excel_source.replace_account("C001", ALPHA, BETA, "额度用完", TODAY)   # ALPHA 打上「结算」
    excel_source.set_budget("C001", ALPHA, 800_000, TODAY, actor="tester", revive=True)
    alpha = next(a for a in excel_source.load_accounts(force=True) if a.key == ALPHA)
    assert alpha.budget == 800_000
    assert excel_source.TAG_SETTLE not in alpha.lifecycle and excel_source.TAG_NORMAL in alpha.lifecycle
    [change] = [e for e in excel_source.load_events() if e.type == "budget"]
    assert (change.before, change.amount, change.actor) == (LEDGER_ROWS[0][2], 800_000, "tester")
    assert change.created_at is not None


def test_解绑_回到库存(ledger):
    make_customer()
    excel_source.assign_accounts("C001", [ALPHA], TODAY)
    excel_source.settle_account("C001", ALPHA, TODAY)
    excel_source.unassign_account("C001", ALPHA, TODAY, note="分错了")
    alpha = next(a for a in excel_source.load_accounts(force=True) if a.key == ALPHA)
    assert alpha.customer == "" and alpha.settled is None
    assert excel_source.load_events()[-1].type == "unassign"


def test_时间线_改日期_删掉(ledger):
    make_customer()
    excel_source.add_customer_event("C001", "note", TODAY, note="客户说下周起量")
    [note] = excel_source.load_events(force=True)
    excel_source.change_customer_event("C001", str(note.id), when=date(2026, 10, 1))
    assert excel_source.load_events(force=True)[0].date == date(2026, 10, 1)
    excel_source.change_customer_event("C001", str(note.id), delete=True)
    assert excel_source.load_events(force=True)[0].deleted


def test_自动事件的改动只留一行(ledger):
    make_customer()
    key = "rampup:111111111111:2026-06-09"
    excel_source.change_customer_event("C001", f"auto:{key}", when=date(2026, 6, 10), auto_kind="rampup",
                                       auto_account="111111111111")
    excel_source.change_customer_event("C001", f"auto:{key}", delete=True)
    rows = [e for e in excel_source.load_events(force=True) if e.key == key]
    assert len(rows) == 1 and rows[0].deleted and rows[0].source == "auto"


def test_账号管理里改客户和额度_时间线跟着记(ledger):
    make_customer()
    make_customer("Northwind Labs")
    accounts = excel_source.load_accounts(force=True)
    alpha = next(a for a in accounts if a.key == ALPHA)
    form = {
        "partner": alpha.partner, "account": alpha.account, "budget": "600000", "tag_ratio": "1",
        "untag_ratio": "1.05", "tag_spec": alpha.tag_spec, "start_date": alpha.start_date.isoformat(),
        "email": "alpha@example.com", "customer": "C001",
    }
    data, errors = excel_source.validate(form, [a for a in accounts if a.key != ALPHA], creating=False,
                                         current=alpha, customers=excel_source.load_customers())
    assert not errors
    excel_source.update_account(ALPHA, data)
    form.update(budget="700000", customer="C002")
    alpha = next(a for a in excel_source.load_accounts(force=True) if a.key == ALPHA)
    data, errors = excel_source.validate(form, [], creating=False, current=alpha,
                                         customers=excel_source.load_customers())
    excel_source.update_account(ALPHA, data)
    kinds = [(e.customer, e.type) for e in excel_source.load_events(force=True)]
    # 第一次：分给 C001（额度改动和分配在同一次里，只记分配）；第二次：C001 解绑、分给 C002
    assert kinds == [("C001", "assign"), ("C001", "unassign"), ("C002", "assign")]


def test_表单里没有客户这一项就当没改(ledger):
    make_customer()
    excel_source.assign_accounts("C001", [ALPHA], TODAY)
    alpha = next(a for a in excel_source.load_accounts(force=True) if a.key == ALPHA)
    data, _ = excel_source.validate({"partner": "ALPHA"}, [], creating=False, current=alpha)
    assert data["customer"] == "C001"


def test_选了不存在的客户报错(ledger):
    accounts = excel_source.load_accounts(force=True)
    alpha = accounts[0]
    _, errors = excel_source.validate({"customer": "C009"}, [], creating=False, current=alpha, customers=[])
    assert any("客户" in error for error in errors)


def test_账号管理里打上风控_时间线记一条(ledger):
    make_customer()
    excel_source.assign_accounts("C001", [ALPHA], TODAY)
    excel_source.set_lifecycle(ALPHA, ["风控"])
    assert [e.type for e in excel_source.load_events(force=True)] == ["assign", "risk"]


def test_客户流程用到的标签被删了会加回清单(ledger):
    make_customer()
    excel_source.remove_lifecycle("结算")
    excel_source.assign_accounts("C001", [ALPHA], TODAY)
    excel_source.settle_account("C001", ALPHA, TODAY)
    names = [tag.name for tag in excel_source.load_lifecycle(force=True)]
    assert "结算" in names


def test_老台账没有客户这几张表也能读(ledger):
    assert excel_source.load_customers(force=True) == []
    assert excel_source.load_events() == []
    assert all(a.customer == "" and a.settled is None for a in excel_source.load_accounts())


def test_手改过的客户表也认(ledger):
    book = openpyxl.load_workbook(ledger)
    sheet = book.create_sheet("CUSTOMERS")
    sheet.append(["name", "id", "status", "avatar"])   # 表头顺序、大小写随便
    sheet.append(["手填的客户", "c007", "暂停", "3.0"])
    book.save(ledger)
    [customer] = excel_source.load_customers(force=True)
    assert (customer.id, customer.name, customer.status, customer.avatar) == ("C007", "手填的客户", "pause", 3)


# ==================================================================== 阶段
@pytest.mark.parametrize("extra, spent, stage", [
    ({}, None, "use"),
    ({"lifecycle": ["风控"]}, None, "risk"),
    ({"lifecycle": ["风控", "结算"]}, None, "pending"),
    ({}, 1_000_000.0, "pending"),                       # 额度用完
    ({"settled": TODAY, "lifecycle": ["结算"]}, None, "settled"),
])
def test_阶段(extra, spent, stage):
    assert customers.stage_of(account("111111111111", **extra), spent) == stage


# ==================================================================== 钱
@pytest.fixture
def scenario(monkeypatch):
    """星河智能：lumen 用完已结算、orbit 风控换成 nova 已结算、kite 风控待替换、nova 在用。"""
    spend = {
        # lumen：6 月起每天 1 万，100 天用满 100 万（实际用到 9 月初）
        "100000000001": {(date(2026, 6, 5) + timedelta(days=i)).isoformat(): 10_000.0 for i in range(100)},
        # orbit：7 月起每天 4 千，到 9-14
        "100000000002": {(date(2026, 7, 3) + timedelta(days=i)).isoformat(): 4_000.0 for i in range(74)},
        # kite：8-21 起每天 1 万，10-02 以后几乎没量
        "100000000003": {(date(2026, 8, 21) + timedelta(days=i)).isoformat(): 10_000.0 for i in range(42)},
        # nova：9-16 起每天 3 万
        "100000000004": {(date(2026, 9, 16) + timedelta(days=i)).isoformat(): 30_000.0 for i in range(24)},
    }
    monkeypatch.setattr(usage_explorer, "account_series", series_for(spend))
    accounts = [
        account("100000000001", lifecycle=["结算"], settled=date(2026, 9, 20), start=date(2026, 6, 5), row=2),
        account("100000000002", lifecycle=["风控", "结算"], settled=date(2026, 9, 24), start=date(2026, 7, 3), row=3),
        account("100000000003", lifecycle=["风控"], start=date(2026, 8, 21), row=4),
        account("100000000004", lifecycle=["正常"], start=date(2026, 9, 15), row=5),
    ]
    events = [
        event(1, date(2026, 6, 5), "assign", "100000000001", amount=1_000_000.0),
        event(2, date(2026, 7, 3), "assign", "100000000002", amount=1_000_000.0),
        event(3, date(2026, 8, 21), "assign", "100000000003", amount=1_000_000.0),
        event(4, date(2026, 9, 15), "risk", "100000000002"),
        event(5, date(2026, 9, 15), "replace", "100000000002", peer="100000000004", amount=1_000_000.0, note="风控"),
        event(6, date(2026, 9, 20), "settle", "100000000001"),
        event(7, date(2026, 9, 24), "settle", "100000000002"),
        event(8, date(2026, 10, 8), "risk", "100000000003"),
    ]
    customer = Customer(id="C001", name="星河智能", since=date(2026, 6, 3))
    views = customers.build_views([customer], accounts, events, TODAY, with_alerts=False)
    return views["C001"]


def test_钱只算使用中的账号(scenario):
    view = scenario
    assert [h.number for h in view.in_use] == ["100000000004"]
    nova = view.in_use[0]
    assert nova.spent == 30_000 * 24
    assert view.budget == 1_000_000
    assert view.spent == nova.spent
    assert view.balance == view.budget - view.spent
    # 历史消费是所有账号
    assert view.lifetime == 1_000_000 + 4_000 * 74 + 10_000 * 42 + 30_000 * 24
    counts = view.counts
    assert (counts["total"], counts["use"], counts["risk"], counts["settled"], counts["bad"]) == (4, 1, 1, 2, 1)


def test_还能用几天(scenario):
    view = scenario
    # 近 7 天（不含今天）只有 nova 在用：每天 3 万
    assert view.avg7 == pytest.approx(30_000)
    assert view.days_left == pytest.approx(view.balance / 30_000)
    assert view.runs_out_on == TODAY + timedelta(days=int(view.balance // 30_000))


def test_余额走势的最后一天就是余额(scenario):
    history = scenario.balance_history()
    assert history[-1] == (TODAY, scenario.balance)
    # 10-08 kite 标了风控：那天起它剩下的不算了，余额掉一截
    by_day = dict(history)
    assert by_day[date(2026, 10, 7)] - by_day[date(2026, 10, 8)] > 500_000


def test_换下来的账号从哪天起不算(scenario):
    holdings = {h.number: h for h in scenario.holdings}
    assert (holdings["100000000002"].left, holdings["100000000002"].left_why) == (date(2026, 9, 15), "风控")
    assert holdings["100000000003"].left == date(2026, 10, 8)
    assert holdings["100000000001"].left_why == "用完"
    assert holdings["100000000004"].via == "100000000002"
    assert holdings["100000000004"].joined == date(2026, 9, 15)


def test_对账单对得上余额(scenario):
    rows = scenario.statement
    assert rows[0].month == "2026-06" and rows[-1].month == "2026-10" and rows[-1].current
    assert rows[-1].end == pytest.approx(scenario.balance)
    added = sum(row.added for row in rows)
    used = sum(row.used for row in rows)
    out = sum(row.out for row in rows)
    assert added - used - out == pytest.approx(scenario.balance)
    # orbit 9 月被风控：剩下的在 9 月移出
    september = next(row for row in rows if row.month == "2026-09")
    assert ("100000000002", 1_000_000 - 4_000 * 74, "风控") in september.out_accounts


def test_对账单算上加的额度(monkeypatch):
    spend = {"100000000004": {(date(2026, 9, 16) + timedelta(days=i)).isoformat(): 30_000.0 for i in range(24)}}
    monkeypatch.setattr(usage_explorer, "account_series", series_for(spend))
    accounts = [account("100000000004", budget=1_500_000.0, start=date(2026, 9, 15))]
    events = [
        event(1, date(2026, 9, 15), "assign", "100000000004", amount=1_000_000.0),
        event(2, date(2026, 10, 2), "budget", "100000000004", amount=1_500_000.0, before=1_000_000.0),
    ]
    view = customers.build_views([Customer(id="C001", name="x", since=date(2026, 9, 15))], accounts, events, TODAY,
                                 with_alerts=False)["C001"]
    rows = view.statement
    assert rows[0].added == 1_000_000 and rows[-1].delta == 500_000
    assert rows[-1].end == pytest.approx(view.balance)
    # 加额度之前那天的余额按当时的额度算
    assert view.balance_on(date(2026, 10, 1)) == pytest.approx(1_000_000 - 30_000 * 16)


def test_自动事件_上量_终止_恢复_额度(monkeypatch):
    days = {}
    start = date(2026, 9, 1)
    for i in range(30):
        day = start + timedelta(days=i)
        # 9-01~9-09 每天 1 千；9-10~9-12 每天 10 块（终止）；9-13 起每天 4 万（恢复）
        days[day.isoformat()] = 1_000.0 if i < 9 else 10.0 if i < 12 else 40_000.0
    monkeypatch.setattr(usage_explorer, "account_series", series_for({"100000000004": days}))
    accounts = [account("100000000004", budget=500_000.0, start=start)]
    view = customers.build_views([Customer(id="C001", name="x", since=start)], accounts, [], TODAY,
                                 with_alerts=False)["C001"]
    autos = [(item.kind, item.date.isoformat(), item.title) for item in view.timeline if item.auto]
    assert ("rampup", "2026-09-01", "开始上量") in autos
    assert ("stop", "2026-09-10", "上量终止") in autos
    assert ("resume", "2026-09-13", "恢复上量") in autos
    titles = [title for kind, _, title in autos if kind == "quota"]
    assert titles == ["额度 70%", "额度 90%", "额度用完"]


def test_最近两天没量不算上量终止(monkeypatch):
    days = {(TODAY - timedelta(days=i)).isoformat(): (0.0 if i < 2 else 1_000.0) for i in range(10)}
    monkeypatch.setattr(usage_explorer, "account_series", series_for({"100000000004": days}))
    accounts = [account("100000000004", start=TODAY - timedelta(days=9))]
    view = customers.build_views([Customer(id="C001", name="x")], accounts, [], TODAY, with_alerts=False)["C001"]
    assert not [item for item in view.timeline if item.kind == "stop"]


def test_自动事件可以删掉和改日期(scenario, monkeypatch):
    rampups = [item for item in scenario.timeline if item.kind == "rampup"]
    first, second = rampups[0], rampups[1]
    key1, key2 = first.ident[len("auto:"):], second.ident[len("auto:"):]
    scenario.events.append(CustomerEvent(id=99, date=first.date, customer="C001", type="rampup", source="auto",
                                         key=key1, deleted=True))
    scenario.events.append(CustomerEvent(id=100, date=date(2026, 7, 20), customer="C001", type="rampup",
                                         source="auto", key=key2))
    del scenario.__dict__["timeline"]   # 清掉缓存，按改动重算
    idents = {item.ident: item for item in scenario.timeline}
    assert first.ident not in idents
    assert idents[second.ident].date == date(2026, 7, 20)


def test_风控邮件进时间线(monkeypatch):
    monkeypatch.setattr(usage_explorer, "account_series", series_for({}))
    alert_events.record("mail-suspended", "账号被暂停", "Your AWS account has been suspended", tone="error",
                        account="100000000004", when=datetime(2026, 10, 7, 1, 12, tzinfo=timezone.utc))
    alert_events.record("stopped", "用量中断", "连续 6 小时没有调用", tone="warn", account="100000000004")
    accounts = [account("100000000004", start=date(2026, 9, 15))]
    view = customers.build_views([Customer(id="C001", name="x")], accounts, [], TODAY)["C001"]
    mails = [item for item in view.timeline if item.kind == "mail"]
    assert len(mails) == 1 and "账号被暂停" in mails[0].text
    assert [item.kind for item in view.feed] == ["stopped", "mail-suspended"]


def test_停用的账号用存下来的历史(monkeypatch):
    def boom(*args, **kwargs):
        raise AssertionError("停用的账号不该查 CE")

    cost_history.remember("100000000001", ["2026-09-01", "2026-09-02"], [90.0, 90.0], [100.0, 100.0])
    monkeypatch.setattr(usage_explorer, "account_series", boom)
    accounts = [account("100000000001", settled=date(2026, 9, 3), enabled=False, lifecycle=["结算"])]
    view = customers.build_views([Customer(id="C001", name="x")], accounts, [], TODAY, with_alerts=False)["C001"]
    assert view.lifetime == 200.0
    assert view.holdings[0].raw_spent == 180.0


def test_查不到的用存下来的顶着(monkeypatch):
    cost_history.remember("100000000004", ["2026-10-01"], [9.0], [10.0])

    def failing(account, start, end, granularity="daily", refresh=False):
        return [], [], [], False, "Jeff / 100000000004：没有权限"

    monkeypatch.setattr(usage_explorer, "account_series", failing)
    view = customers.build_views([Customer(id="C001", name="x")], [account("100000000004")], [], TODAY,
                                 with_alerts=False)["C001"]
    holding = view.holdings[0]
    assert holding.spent == 10.0 and holding.series.stale_as_of == date(2026, 10, 1)
    assert holding.series.error


def test_查成功的存下来(monkeypatch):
    monkeypatch.setattr(usage_explorer, "account_series",
                        series_for({"100000000004": {"2026-10-08": 5.0}}))
    customers.build_views([Customer(id="C001", name="x")], [account("100000000004", start=date(2026, 10, 1))],
                          [], TODAY, with_alerts=False)
    saved = cost_history.recall("100000000004")
    assert saved is not None and sum(saved.marked) == 5.0 and saved.as_of == TODAY


# ==================================================================== 客户资料表单
def test_客户表单校验():
    others = [Customer(id="C001", name="星河智能")]
    data, errors = customers.validate_customer(
        {"name": "  云帆  科技 ", "region": "sg", "avatar": "0", "status": "on", "since": "2026-10-01"}, others,
        today=TODAY)
    assert not errors
    assert data == {"name": "云帆 科技", "region": "SG", "avatar": 0, "status": "on", "since": date(2026, 10, 1),
                    "note": ""}
    _, errors = customers.validate_customer({"name": "星河智能", "region": "XX", "avatar": "11",
                                             "since": "2027-01-01"}, others, today=TODAY)
    assert len(errors) == 4


def test_首字母头像():
    assert customers.letter_of("星河智能") == "星"
    assert customers.letter_of("  kumo studio") == "K"
    assert customers.letter_of("") == "?"
    assert customers.tone_of("C004") == customers.tone_of("C004")
    assert 0 <= customers.tone_of("C001") < customers.LETTER_TONES


def test_国旗():
    assert 'class="flag"' in customers.flag_svg("CN")
    assert customers.flag_svg("XX") != customers.flag_svg("CN")   # 认不出的画地球
    assert all(code in customers.FLAGS or code == "OTHER" for code, _ in customers.REGIONS)


# ==================================================================== 图
def test_还能用几天的图():
    from bedrock_cost import chart
    history = [(TODAY - timedelta(days=20 - i), 1_000_000 - i * 30_000) for i in range(21)]
    svg = chart.render_runway(history, 30_000)
    assert 'class="chart-line"' in svg and "今天" in svg
    assert "stroke-dasharray=\"5 4\"" in svg                # 往后推的那段是虚线
    flat = chart.render_runway(history, 0)                  # 最近没量：不画预测
    assert "stroke-dasharray=\"5 4\"" not in flat
    assert "没有数据" in chart.render_runway([], 10)
