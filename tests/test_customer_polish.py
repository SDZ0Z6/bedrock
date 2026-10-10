"""第三轮反馈：

1. 时间线两头的虚线也会动（往右走，从以前流向以后）；
2. 概览的筛选条能搜邮箱或账号 ID；
3. 客户页余额卡片里「不算」的那几行，小头像的字母居中；
4. 客户页时间线按账号筛：能搜邮箱、ID 的下拉，不再是一排签；
5. 名下账号：能按阶段筛、搜邮箱或 ID，表头能排序；默认使用中的在上面；分配日期写全（2026-10-01）；
6. 月度对账单「按月」：一个月一行只写数和几个账号，点月份展开每个账号；前面没有动静的月份不列；
7. 记一笔能选「标记风控」，和名下账号里的「标记风控」一样；
8. 账号页的时间线也能记一笔（记在账号现在归的客户名下），「记一笔」那几类在账号页就能改日期、删掉；
9. 换账号的下拉右边是生命周期（在 test_account_pages 里）。
"""

from __future__ import annotations

import re
from datetime import date, timedelta
from pathlib import Path

import pytest

from bedrock_cost import config, customers, excel_source, usage_explorer
from bedrock_cost.excel_source import Customer

from .conftest import LEDGER_ROWS
from .test_accounts import admin, by_account, post  # noqa: F401  (admin 是 fixture)
from .test_account_timeline import items, tab
from .test_customer_pages import ALPHA, BETA, detail, make, timeline
from .test_customers import TODAY, account, event, series_for
from .test_web import book, page_env, scrape, usage_states  # noqa: F401  (book 是 fixture；后两个 autouse)

ALPHA_NO, BETA_NO = str(LEDGER_ROWS[0][1]), str(LEDGER_ROWS[1][1])
CSS = (Path(config.__file__).parent / "static" / "style.css").read_text(encoding="utf-8")


def events(kind: str) -> list[excel_source.CustomerEvent]:
    return [e for e in excel_source.load_events(force=True) if e.type == kind]


def holdings_table(html: str) -> str:
    return re.search(r'<table class="holding-table" id="holding-table" data-sortable>(.*?)</table>', html, re.S).group(1)


# ====================================================================== 1. 虚线会动
def test_时间线两头的虚线会动():
    # 一段虚线是一块 12px 的图横着铺，动画里整块往右挪 12px：正好接上，看不出接缝
    assert "0 0 / 12px 100% repeat-x" in CSS
    assert "@keyframes tl-march { to { background-position: 12px 0; } }" in CSS
    assert re.search(r"\.tl-axis\.is-before \{ animation: [^}]*tl-march [^}]*infinite", CSS)
    assert re.search(r"\.tl-axis\.is-after \{ animation: [^}]*tl-march [^}]*infinite", CSS)


# ====================================================================== 2. 概览能搜
def test_概览能搜邮箱或ID(logged_in, book, fake_costs):
    html = logged_in.get("/").get_data(as_text=True)
    assert re.search(r'<input id="acct-q" type="search" placeholder="搜邮箱或账号 ID"', html)
    keys = re.findall(r'<article class="acct-card"[^>]*data-q="([^"]*)"', html)
    assert "alpha@example.com 111111111111" in keys and "beta@example.com 222222222222" in keys
    # 搜索框那一行不是一组筛选签：签只认带 data-group 的
    assert "#acct-filters .fchips[data-group]" in html


# ====================================================================== 3. 小头像
def test_余额卡片里不算的那几行_小头像的字母居中():
    """.bal-note span 会连小头像（也是 span）一起变成 flex，place-items 只剩竖着居中，字母歪到左边。"""
    assert ".bal-note span {" not in CSS
    assert ".bal-note > span {" in CSS


# ====================================================================== 4. 时间线按账号筛
def test_时间线按账号筛_能搜邮箱或ID(admin, fake_costs):
    make(admin)
    post(admin, "/customers/C001/assign", account=[ALPHA, BETA])
    html = detail(admin)
    box = re.search(r'<div class="tl-acct-filter" data-tl-acct>(.*?)</ul>', html, re.S).group(1)
    assert 'placeholder="搜邮箱或 ID"' in box and "data-picker" in box
    options = re.findall(r'<li class="acct-opt"[^>]*data-value="([^"]*)"[^>]*data-search="([^"]*)"', box)
    assert options[0] == ("", "全部账号")
    found = dict(options[1:])
    assert set(found) == {ALPHA_NO, BETA_NO}
    assert found[ALPHA_NO] == f"acct-one@example.com {ALPHA_NO}"
    assert 'data-tl-filter="acct"' not in html           # 不再是一排签


# ====================================================================== 5. 名下账号
def test_名下账号_默认使用中的在上面_日期写全(admin, fake_costs):
    make(admin)
    post(admin, "/customers/C001/assign", account=[ALPHA, BETA])
    post(admin, "/customers/C001/risk", key=ALPHA, action="mark")        # ALPHA 先分的，标了风控
    table = holdings_table(detail(admin))
    assert re.findall(r'<tr data-stage="(\w+)"', table) == ["use", "risk"]
    joined = re.findall(r'<td class="joined-cell" data-sort="([^"]*)">([^<]*)', table)
    assert joined and all(re.fullmatch(r"\d{4}-\d{2}-\d{2}", text.strip()) and text.strip() == key
                          for key, text in joined)


def test_名下账号_能筛能排序(admin, fake_costs):
    make(admin)
    post(admin, "/customers/C001/assign", account=[ALPHA, BETA])
    post(admin, "/customers/C001/risk", key=ALPHA, action="mark")
    html = detail(admin)
    table = holdings_table(html)
    heads = re.findall(r'<th class="sortable" tabindex="0" aria-sort="none" data-sort-type="(\w+)">(.*?)</th>', table)
    assert heads == [("text", "账号"), ("number", "阶段"), ("number", "额度使用"), ("text", "分配")]
    # 阶段那一格排序用的值：使用中 0、风控 1
    assert re.findall(r'<td data-sort="(\d)">', table) == ["0", "1"]
    tools = re.search(r'<div class="holding-tools" id="holding-tools">(.*?)</label>', html, re.S).group(1)
    assert [scrape(b) for b in re.findall(r'<button class="seg-item[^"]*"[^>]*>(.*?)</button>', tools, re.S)] == [
        "全部 2", "使用中 1", "风控 1",
    ]
    assert 'id="holding-q"' in tools
    assert re.findall(r'<tr data-stage="\w+" data-q="([^"]*)"', table) == [
        f"acct-two@example.com {BETA_NO}", f"acct-one@example.com {ALPHA_NO}",
    ]
    assert "data-holding-none hidden" in table


def test_替换弹窗还是默认选中风控的那个(admin, fake_costs):
    """表格里使用中的排前面，替换弹窗「换下哪个」还是风控的在前、默认选中。"""
    make(admin)
    post(admin, "/customers/C001/assign", account=[ALPHA, BETA])
    post(admin, "/customers/C001/risk", key=BETA, action="mark")
    dialog = re.search(r'<dialog class="modal" id="dlg-replace">(.*?)</dialog>', detail(admin), re.S).group(1)
    assert re.search(rf'<input type="radio" name="old" value="{re.escape(BETA)}" checked>', dialog)


# ====================================================================== 6. 对账单「按月」
@pytest.fixture
def statement_view(monkeypatch):
    """6 月就开始合作，8-10 才分到第一个账号；9-18 它被风控；9 月分第二个，9-20 加了 20 万额度。"""
    spend = {
        "100000000001": {(date(2026, 8, 10) + timedelta(days=i)).isoformat(): 10_000.0 for i in range(40)},
        "100000000002": {(date(2026, 9, 1) + timedelta(days=i)).isoformat(): 5_000.0 for i in range(30)},
    }
    monkeypatch.setattr(usage_explorer, "account_series", series_for(spend))
    accounts = [
        account("100000000001", lifecycle=["风控"], start=date(2026, 8, 10), row=2),
        account("100000000002", budget=1_200_000.0, lifecycle=["正常"], start=date(2026, 9, 1), row=3),
    ]
    history = [
        event(1, date(2026, 8, 10), "assign", "100000000001", amount=1_000_000.0),
        event(2, date(2026, 9, 1), "assign", "100000000002", amount=1_000_000.0),
        event(3, date(2026, 9, 18), "risk", "100000000001"),
        event(4, date(2026, 9, 20), "budget", "100000000002", amount=1_200_000.0, before=1_000_000.0),
    ]
    customer = Customer(id="C001", name="星河智能", since=date(2026, 6, 3))
    return customers.build_views([customer], accounts, history, TODAY, with_alerts=False)["C001"]


def test_对账单从分到账号的那个月开始(statement_view):
    """6、7 月一行全是 0，只是占地方：从最早分到账号、有消费的那个月开始。"""
    assert [row.month for row in statement_view.statement] == ["2026-08", "2026-09", "2026-10"]
    assert statement_view.months == ["2026-08", "2026-09", "2026-10"]


def test_对账单按月展开_每个账号的数加起来就是这个月的(statement_view):
    rows = {row.month: row for row in statement_view.statement}
    for row in rows.values():
        lines = list(row.lines.values())
        assert sum(line.added for line in lines) == pytest.approx(row.added)
        assert sum(line.delta for line in lines) == pytest.approx(row.delta)
        assert sum(line.used for line in lines) == pytest.approx(row.used)
        assert sum(line.out for line in lines) == pytest.approx(row.out)
    september = rows["2026-09"].lines
    # 表格的顺序：使用中的在前
    assert list(september) == ["100000000002", "100000000001"]
    fresh, flagged = september["100000000002"], september["100000000001"]
    assert (fresh.added, fresh.delta, fresh.used, fresh.out) == (1_000_000, 200_000, 5_000 * 30, 0)
    assert (flagged.added, flagged.used, flagged.why) == (0, 10_000 * 18, "风控")
    assert flagged.out == pytest.approx(1_000_000 - 10_000 * 40)
    assert rows["2026-08"].lines["100000000001"].used == 10_000 * 22
    assert rows[statement_view.months[-1]].end == pytest.approx(statement_view.balance)


def test_对账单按月_页面上一个月一行_点月份展开(admin, fake_costs):
    make(admin)
    post(admin, "/customers/C001/assign", account=[ALPHA, BETA])
    html = detail(admin)
    month = re.search(r'<div class="stmt-view" data-stmt-view="month">(.*?)</table>', html, re.S).group(1)
    current = date.today().isoformat()[:7]
    assert f'data-stmt-toggle="{current}"' in month and 'aria-expanded="false"' in month
    assert "2 个账号" in scrape(month)
    assert 'class="who"' not in month                      # 不再把邮箱一个个列在数下面
    lines = re.findall(rf'<tr class="stmt-line" data-stmt-of="{current}"[^>]*hidden>(.*?)</tr>', month, re.S)
    # 每行最前面是小头像 + 邮箱
    assert [scrape(re.search(r"<td>(.*?)</td>", line, re.S).group(1)).split()[-1] for line in lines] == [
        "acct-one@example.com", "acct-two@example.com",
    ]


# ====================================================================== 7. 记一笔：标记风控
def test_记一笔_标记风控_和名下账号里的一样(admin, fake_costs):
    make(admin)
    post(admin, "/customers/C001/assign", account=[ALPHA])
    when = date.today() - timedelta(days=2)
    response = post(admin, "/customers/C001/events", kind="risk", account=ALPHA_NO, date=when.isoformat(),
                    note="收到 AWS 暂停通知")
    assert response.status_code == 302
    flagged = by_account(ALPHA_NO).lifecycle
    assert "风控" in flagged and "正常" not in flagged
    [risk] = events("risk")
    assert (risk.customer, risk.account, risk.date, risk.note) == ("C001", ALPHA_NO, when, "收到 AWS 暂停通知")
    assert '<tr data-stage="risk"' in holdings_table(detail(admin))
    # 再标一次：本来就是风控，不再记
    post(admin, "/customers/C001/events", kind="risk", account=ALPHA_NO)
    assert len(events("risk")) == 1


def test_记一笔_标记风控_一定要选这个客户的账号(admin, fake_costs):
    make(admin)
    post(admin, "/customers/C001/assign", account=[ALPHA])
    post(admin, "/customers/C001/events", kind="risk", account="", note="没选账号")
    post(admin, "/customers/C001/events", kind="risk", account=BETA_NO)      # 库存里的，不是这个客户的
    assert "风控" not in by_account(ALPHA_NO).lifecycle and "风控" not in by_account(BETA_NO).lifecycle
    assert events("risk") == []


def test_记一笔弹窗里有标记风控(admin, fake_costs):
    make(admin)
    post(admin, "/customers/C001/assign", account=[ALPHA, BETA])
    post(admin, "/customers/C001/risk", key=BETA, action="mark")
    dialog = re.search(r'<dialog class="modal modal-sm" id="dlg-note">(.*?)</dialog>', detail(admin), re.S).group(1)
    assert '<option value="risk">标记风控</option>' in dialog
    # 已经是风控的不能再标（选「标记风控」时由 timeline.js 灰掉）
    assert re.search(rf'<option value="{ALPHA_NO}" data-markable="1">', dialog)
    assert re.search(rf'<option value="{BETA_NO}" data-markable="">', dialog)
    assert "data-note-risk hidden" in dialog


# ====================================================================== 8. 账号页的时间线记一笔
def test_账号页有记一笔(admin, fake_costs):
    html = tab(admin, ALPHA_NO)
    assert '<button class="btn btn-sm btn-primary" type="button" data-open="dlg-note">记一笔</button>' in html
    dialog = re.search(r'<dialog class="modal modal-sm" id="dlg-note">(.*?)</dialog>', html, re.S).group(1)
    assert f'action="/account/{ALPHA_NO}/events"' in dialog
    assert [scrape(o) for o in re.findall(r"<option[^>]*>(.*?)</option>", dialog, re.S)] == [
        "备注", "开始上量", "上量终止", "恢复上量", "标记风控",
    ]
    assert "在库存里，只记这个账号" in scrape(dialog)


def test_账号页记一笔_库存里的只记账号(admin, fake_costs):
    response = post(admin, f"/account/{ALPHA_NO}/events", kind="rampup", date=date.today().isoformat(),
                    note="客户开始压测")
    assert response.status_code == 302 and response.headers["Location"].endswith(f"/account/{ALPHA_NO}/timeline")
    [found] = events("rampup")
    assert (found.customer, found.account, found.note) == ("", ALPHA_NO, "客户开始压测")
    assert "rampup" in [item["kind"] for item in items(tab(admin, ALPHA_NO))]


def test_账号页记一笔_分给了客户的记在客户名下(admin, fake_costs):
    make(admin)
    post(admin, "/customers/C001/assign", account=[ALPHA])
    assert "记在星河智能名下" in scrape(tab(admin, ALPHA_NO))
    post(admin, f"/account/{ALPHA_NO}/events", kind="note", note="客户说下周起量")
    [note] = events("note")
    assert (note.customer, note.account) == ("C001", ALPHA_NO)
    assert "note" in [item["kind"] for item in timeline(detail(admin))]       # 客户页的时间线上也有


def test_账号页记一笔_备注要写点什么(admin, fake_costs):
    post(admin, f"/account/{ALPHA_NO}/events", kind="note", note="  ")
    assert events("note") == []


def test_账号页记一笔_标记风控(admin, fake_costs):
    # 库存里的：只改生命周期、只记账号
    post(admin, f"/account/{BETA_NO}/events", kind="risk", note="AWS 暂停")
    assert "风控" in by_account(BETA_NO).lifecycle
    [risk] = events("risk")
    assert (risk.customer, risk.account, risk.note) == ("", BETA_NO, "AWS 暂停")
    # 已经是风控了：记一笔里这一项灰掉
    assert re.search(r'<option value="risk" disabled>\s*标记风控（已经是风控）</option>', tab(admin, BETA_NO))
    # 分给了客户的：记在客户名下，客户页上它变成「风控 · 待替换」
    make(admin)
    post(admin, "/customers/C001/assign", account=[ALPHA])
    post(admin, f"/account/{ALPHA_NO}/events", kind="risk")
    mine = [e for e in events("risk") if e.account == ALPHA_NO]
    assert len(mine) == 1 and mine[0].customer == "C001"
    assert '<tr data-stage="risk"' in holdings_table(detail(admin))


def test_账号页_记一笔那几类能改日期能删(admin, fake_costs):
    post(admin, f"/account/{ALPHA_NO}/events", kind="note", note="先记一笔")
    [note] = events("note")
    earlier = date.today() - timedelta(days=3)
    post(admin, f"/account/{ALPHA_NO}/events/change", id=str(note.id), action="date", date=earlier.isoformat())
    assert events("note")[0].date == earlier
    post(admin, f"/account/{ALPHA_NO}/events/change", id=str(note.id), action="delete")
    assert events("note")[0].deleted
    assert "note" not in [item["kind"] for item in items(tab(admin, ALPHA_NO))]


def test_账号页_连着钱和阶段的不在这里改(admin, fake_costs):
    make(admin)
    post(admin, "/customers/C001/assign", account=[ALPHA])
    [assign] = events("assign")
    post(admin, f"/account/{ALPHA_NO}/events/change", id=str(assign.id), action="delete")
    assert not events("assign")[0].deleted
    assert 'data-editable-kinds="note rampup stop resume"' in tab(admin, ALPHA_NO)


def test_账号页_别的账号的那一条不让改(admin, fake_costs):
    post(admin, f"/account/{ALPHA_NO}/events", kind="note", note="ALPHA 的")
    [note] = events("note")
    post(admin, f"/account/{BETA_NO}/events/change", id=str(note.id), action="delete")
    assert not events("note")[0].deleted
