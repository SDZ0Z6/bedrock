"""账号管理页：增 / 改 / 停用。

这一页是唯一会写 cred.xlsx 的地方，所以用例的重点不在「功能能用」，而在
「写坏了会怎样」：
    - AK/SK 只能进不能出，编辑路径上必须一个字节都不改；
    - 校验没过、或者行号对不上时，文件必须原样不动；
    - 软删只翻 ENABLED，行号不许移动（account.key 和各处缓存键都带行号）。
"""

from __future__ import annotations

import re

import openpyxl
from datetime import date, timedelta

import pytest

from bedrock_cost import excel_source
from bedrock_cost.excel_source import LedgerConflict, load_accounts

from .conftest import (
    LEDGER_HEADER,
    LEDGER_ROWS,
    RANGE_START,
    expected_marked,
    ledger_value,
    ledger_without,
    write_ledger,
)

CSRF_PATTERN = re.compile(r'name="csrf" value="([^"]+)"')
# <dialog id="…" … data-reopen> —— 属性形式，不会误命中脚本里的 dialog[data-reopen]
REOPEN_PATTERN = re.compile(r'id="(dlg-[\w-]+)"[^>]*\sdata-reopen>')

# 一份合法的新账号表单，用例按需覆盖其中几项
NEW_FORM = {
    "partner": "GAMMA",
    "account": "333333333333",
    "budget": "150000",
    "tag_ratio": "1",
    "untag_ratio": "1.2",
    "tag_spec": "map-migrated=migGAMMA",
    "ak": "AKIAFAKEGAMMA0000000",
    "sk": "z" * 40,
}


@pytest.fixture
def admin(logged_in, ledger):
    """登录后的 client，台账指向临时文件。"""
    return logged_in


def token(client) -> str:
    """从页面上取一个 CSRF 令牌。"""
    html = client.get("/accounts/").get_data(as_text=True)
    found = CSRF_PATTERN.search(html)
    assert found, "页面上没有 CSRF 令牌"
    return found.group(1)


def post(client, path: str, **fields):
    return client.post(path, data={"csrf": token(client), **fields})


def by_account(account_id: str, include_disabled: bool = True):
    rows = [
        a for a in load_accounts(force=True, include_disabled=include_disabled)
        if a.account == account_id
    ]
    return rows[0] if rows else None


def raw_rows(path):
    """绕过缓存和过滤，直接看文件里到底写了什么。"""
    workbook = openpyxl.load_workbook(path, data_only=True)
    try:
        return [list(row) for row in workbook.worksheets[0].iter_rows(values_only=True)]
    finally:
        workbook.close()


# ------------------------------------------------------------------ 访问控制
def test_未登录不能进账号管理页(client, ledger):
    response = client.get("/accounts/")
    assert response.status_code == 302
    assert "/login" in response.headers["Location"]


def test_未登录不能提交任何写操作(client, ledger):
    for path in ("/accounts/create", "/accounts/update", "/accounts/toggle"):
        response = client.post(path, data=NEW_FORM)
        assert response.status_code == 302, path
        assert "/login" in response.headers["Location"], path
    assert len(load_accounts(force=True, include_disabled=True)) == len(LEDGER_ROWS)


def test_没有CSRF令牌的提交不写文件(admin, ledger):
    before = ledger.stat().st_mtime_ns
    response = admin.post("/accounts/create", data=NEW_FORM)  # 故意不带 csrf
    assert response.status_code == 302
    assert by_account("333333333333") is None
    assert ledger.stat().st_mtime_ns == before


def test_错误的CSRF令牌同样被拒(admin, ledger):
    response = admin.post("/accounts/create", data={"csrf": "not-the-token", **NEW_FORM})
    assert response.status_code == 302
    assert by_account("333333333333") is None


# ------------------------------------------------------------------ 页面本身
def test_页面列出全部账号且不泄露凭证(admin):
    html = admin.get("/accounts/").get_data(as_text=True)
    for partner, account, *_ in LEDGER_ROWS:
        assert partner in html
        assert str(account) in html
    # SK 任何形式都不能出现；AK 只能是掩码
    for row in LEDGER_ROWS:
        assert ledger_value(row, "SK") not in html
        assert ledger_value(row, "AK") not in html
    assert "AKIAFAKE…" in html


def test_新增入口在页头而不是页面里(admin):
    html = admin.get("/accounts/").get_data(as_text=True)
    head, _, body = html.partition('<div class="page-actions">')
    assert 'data-open="dlg-create"' in body.partition("</div>")[0]


def test_每个账号各有一个修改弹窗(admin):
    html = admin.get("/accounts/").get_data(as_text=True)
    assert html.count('id="dlg-edit-') == len(LEDGER_ROWS)
    assert html.count('data-open="dlg-edit-') == len(LEDGER_ROWS)
    # 弹窗里是完整的一套可编辑字段，凭证不在其中
    for name in excel_source.EDITABLE:
        assert f'name="{name}"' in html
    assert 'name="ak"' in html  # 只在新增弹窗里
    assert html.count('name="ak"') == 1
    assert html.count('name="sk"') == 1


def test_启用中的账号点停用要先过确认弹窗(admin):
    html = admin.get("/accounts/").get_data(as_text=True)
    # 两个账号都启用中，各有一个确认弹窗；按钮只负责打开它，不直接提交
    assert html.count('id="dlg-off-') == len(LEDGER_ROWS)
    assert html.count('data-open="dlg-off-') == len(LEDGER_ROWS)
    assert "确定停用" in html
    # 确认弹窗里才是真正的 POST 表单
    assert html.count(f'action="/accounts/toggle"') == len(LEDGER_ROWS)


def test_停用的账号在页面上仍然可见并可一键恢复(admin, ledger):
    target = by_account("111111111111")
    post(admin, "/accounts/toggle", key=target.key, enabled="0")
    html = admin.get("/accounts/").get_data(as_text=True)
    assert "111111111111" in html
    assert "row-off" in html
    # 恢复是安全可逆的，不再拦一道确认：它是 <td> 里的一个直接提交表单
    assert "恢复" in html
    assert html.count('id="dlg-off-') == len(LEDGER_ROWS) - 1
    assert 'value="1"' in html


def reopened_dialogs(html: str) -> list[str]:
    """页面加载后会自动弹回来的窗口（校验没过时服务端打的标记）。"""
    return REOPEN_PATTERN.findall(html)


def test_新增校验失败会重新弹出新增窗口(admin, ledger):
    response = post(admin, "/accounts/create", **{**NEW_FORM, "budget": "abc"})
    html = response.get_data(as_text=True)
    assert reopened_dialogs(html) == ["dlg-create"]
    assert 'value="abc"' in html


def test_修改校验失败会重新弹出那一行的窗口(admin, ledger):
    target = by_account("111111111111")  # 台账里第一行
    response = post(
        admin, "/accounts/update",
        key=target.key, partner="ALPHA", account="111111111111",
        budget="abc", tag_ratio="1", untag_ratio="1", tag_spec="",
    )
    html = response.get_data(as_text=True)
    # 只弹这一个，而且必须是这个账号的窗口，不能是别人的
    assert reopened_dialogs(html) == ["dlg-edit-1"]
    assert 'value="abc"' in html


def test_没出错时不会自动弹窗(admin):
    assert reopened_dialogs(admin.get("/accounts/").get_data(as_text=True)) == []


# ------------------------------------------------------------------ 新增
def test_新增账号写进台账(admin, ledger):
    response = post(admin, "/accounts/create", **NEW_FORM)
    assert response.status_code == 302

    created = by_account("333333333333")
    assert created is not None
    assert created.partner == "GAMMA"
    assert created.budget == 150000
    assert created.untag_ratio == 1.2
    assert created.enabled is True
    assert created.has_credentials
    assert created.tag_value == "migGAMMA"


def test_新增追加在末尾不打乱已有行号(admin, ledger):
    before = {a.account: a.key for a in load_accounts(force=True)}
    post(admin, "/accounts/create", **NEW_FORM)
    after = {a.account: a.key for a in load_accounts(force=True)}
    for account_id, key in before.items():
        assert after[account_id] == key, "已有账号的 key 变了，缓存和书签都会失配"


def test_新增时预算接受千分位和货币符号(admin, ledger):
    post(admin, "/accounts/create", **{**NEW_FORM, "budget": "$1,200.50"})
    assert by_account("333333333333").budget == 1200.5


@pytest.mark.parametrize(
    "field, value, hint",
    [
        ("account", "", "账号不能为空"),
        ("account", "123", "12 位数字"),
        ("account", "12345678901x", "12 位数字"),
        ("partner", "", "上游不能为空"),
        ("budget", "abc", "额度要填数字"),
        ("budget", "-1", "额度不能是负数"),
        ("tag_ratio", "0", "必须大于 0"),
        ("untag_ratio", "-2", "必须大于 0"),
        ("tag_ratio", "1000", "看起来不对"),
        ("ak", "", "AK 不能为空"),
        ("ak", "not-an-ak", "AK 格式不对"),
        ("sk", "", "SK 不能为空"),
        ("sk", "tooshort", "SK 格式不对"),
    ],
)
def test_新增的字段校验(admin, ledger, field, value, hint):
    before = ledger.stat().st_mtime_ns
    response = post(admin, "/accounts/create", **{**NEW_FORM, field: value})
    assert response.status_code == 400
    assert hint in response.get_data(as_text=True)
    assert ledger.stat().st_mtime_ns == before, "校验没过却动了文件"


def test_新增时账号不能和已有的重复(admin, ledger):
    response = post(admin, "/accounts/create", **{**NEW_FORM, "account": "111111111111"})
    assert response.status_code == 400
    assert "已经在台账里了" in response.get_data(as_text=True)


def test_停用的账号ID也不能被重新占用(admin, ledger):
    target = by_account("111111111111")
    post(admin, "/accounts/toggle", key=target.key, enabled="0")

    response = post(admin, "/accounts/create", **{**NEW_FORM, "account": "111111111111"})
    assert response.status_code == 400
    assert "已经在台账里了" in response.get_data(as_text=True)


def test_校验失败时把填过的值回填(admin, ledger):
    response = post(admin, "/accounts/create", **{**NEW_FORM, "budget": "abc"})
    html = response.get_data(as_text=True)
    assert 'value="abc"' in html
    assert 'value="GAMMA"' in html
    # 但凭证不回填——不能让它出现在响应里
    assert NEW_FORM["sk"] not in html
    assert NEW_FORM["ak"] not in html


# ------------------------------------------------------------------ 编辑
def test_编辑改掉非凭证字段(admin, ledger):
    target = by_account("111111111111")
    response = post(
        admin, "/accounts/update",
        key=target.key, partner="ALPHA-NEW", account="111111111111",
        budget="777", tag_ratio="1.5", untag_ratio="2", tag_spec="cost-center=x",
    )
    assert response.status_code == 302

    changed = by_account("111111111111")
    assert changed.partner == "ALPHA-NEW"
    assert changed.budget == 777
    assert changed.tag_ratio == 1.5
    assert changed.untag_ratio == 2
    assert changed.tag_key == "cost-center"
    assert changed.tag_value == "x"


def test_编辑不碰凭证(admin, ledger):
    target = by_account("111111111111")
    ak_before, sk_before = target.ak, target.sk

    post(
        admin, "/accounts/update",
        key=target.key, partner="ALPHA", account="111111111111",
        budget="777", tag_ratio="1", untag_ratio="1.05", tag_spec="map-migrated=migALPHA",
    )
    after = by_account("111111111111")
    assert after.ak == ak_before
    assert after.sk == sk_before


def test_编辑表单里塞AK和SK也不会生效(admin, ledger):
    """凭证是单向的：更新凭证的唯一做法是停用旧账号再新建一条。"""
    target = by_account("111111111111")
    ak_before, sk_before = target.ak, target.sk

    post(
        admin, "/accounts/update",
        key=target.key, partner="ALPHA", account="111111111111",
        budget="777", tag_ratio="1", untag_ratio="1.05", tag_spec="map-migrated=migALPHA",
        ak="AKIAHACKED0000000000", sk="hacked" * 8,
    )
    after = by_account("111111111111")
    assert after.ak == ak_before
    assert after.sk == sk_before


def test_编辑没有实际改动时不写文件(admin, ledger):
    target = by_account("111111111111")
    before = ledger.stat().st_mtime_ns

    response = post(
        admin, "/accounts/update",
        key=target.key, partner=target.partner, account=target.account,
        budget="500000", tag_ratio="1", untag_ratio="1.05", tag_spec=target.tag_spec,
        # 弹窗里这个字段是带初值的，不原样带上就等于把它清空，那确实算一次改动
        start_date=target.start_date.isoformat(),
    )
    assert response.status_code == 302
    assert ledger.stat().st_mtime_ns == before
    assert not (ledger.parent / excel_source.BACKUP_DIR_NAME).exists()


def test_编辑不能把账号改成别人的ID(admin, ledger):
    target = by_account("111111111111")
    response = post(
        admin, "/accounts/update",
        key=target.key, partner="ALPHA", account="222222222222",
        budget="1", tag_ratio="1", untag_ratio="1", tag_spec="",
    )
    assert response.status_code == 400
    assert "已经在台账里了" in response.get_data(as_text=True)
    assert by_account("111111111111") is not None


def test_编辑已经不存在的账号会被挡下(admin, ledger):
    response = post(
        admin, "/accounts/update",
        key="999999999999#9", partner="X", account="999999999999",
        budget="1", tag_ratio="1", untag_ratio="1", tag_spec="",
    )
    assert response.status_code == 302  # 重定向回列表并提示


def test_台账被换过之后旧的行号不会改错行(ledger):
    """页面拿到 key 之后，有人 scp 覆盖了台账——此时宁可报错也不能写错行。"""
    target = [a for a in load_accounts(force=True) if a.account == "111111111111"][0]

    # 两行对调，target.key 里的行号现在指向另一个账号
    write_ledger(ledger, rows=list(reversed(LEDGER_ROWS)))
    excel_source.clear_cache()

    data, errors = excel_source.validate(
        {"partner": "X", "account": "111111111111", "budget": "1",
         "tag_ratio": "1", "untag_ratio": "1", "tag_spec": ""},
        [], creating=False,
    )
    assert not errors
    with pytest.raises(LedgerConflict):
        excel_source.update_account(target.key, data, actor="tester")

    # 文件没被动过
    assert [row[0] for row in raw_rows(ledger)[1:]] == ["BETA", "ALPHA"]


# ------------------------------------------------------------------ 软删
def test_停用后账号从查询口径里消失(admin, ledger):
    target = by_account("111111111111")
    response = post(admin, "/accounts/toggle", key=target.key, enabled="0")
    assert response.status_code == 302

    assert by_account("111111111111", include_disabled=False) is None
    still_there = by_account("111111111111")
    assert still_there is not None and still_there.enabled is False


def test_停用只翻ENABLED不删行(admin, ledger):
    target = by_account("222222222222")
    post(admin, "/accounts/toggle", key=target.key, enabled="0")

    rows = raw_rows(ledger)
    assert len(rows) == len(LEDGER_ROWS) + 1  # 表头 + 两行，一行都没少
    assert by_account("222222222222").key == target.key, "行号变了"
    # 凭证也还在
    assert by_account("222222222222").has_credentials


def test_停用不影响其他账号的key(admin, ledger):
    before = {a.account: a.key for a in load_accounts(force=True, include_disabled=True)}
    post(admin, "/accounts/toggle", key=before["111111111111"], enabled="0")
    after = {a.account: a.key for a in load_accounts(force=True, include_disabled=True)}
    assert after == before


def test_恢复停用的账号(admin, ledger):
    target = by_account("111111111111")
    post(admin, "/accounts/toggle", key=target.key, enabled="0")
    post(admin, "/accounts/toggle", key=target.key, enabled="1")

    back = by_account("111111111111", include_disabled=False)
    assert back is not None
    assert back.budget == 500000
    assert back.has_credentials


def test_重复停用不会反复写文件(admin, ledger):
    target = by_account("111111111111")
    post(admin, "/accounts/toggle", key=target.key, enabled="0")
    mtime = ledger.stat().st_mtime_ns
    post(admin, "/accounts/toggle", key=target.key, enabled="0")
    assert ledger.stat().st_mtime_ns == mtime


def test_停用的账号不进概览和下钻页(admin, ledger, fake_costs):
    target = by_account("111111111111")
    # 跟着跳转走一遍，把「已停用」的 flash 消费掉，免得它出现在下面的页面里
    post(admin, "/accounts/toggle", key=target.key, enabled="0")
    admin.get("/accounts/")

    overview = admin.get("/").get_data(as_text=True)
    assert "222222222222" in overview
    assert "111111111111" not in overview

    drilldown = admin.get("/cost-usage").get_data(as_text=True)
    assert "111111111111" not in drilldown


# ------------------------------------------------------------------ 老台账兼容
def test_没有ENABLED列的老台账全部算启用(ledger):
    header = [name for name in LEDGER_HEADER]
    assert "ENABLED" not in header
    assert all(a.enabled for a in load_accounts(force=True))


def test_写入时自动补上缺失的ENABLED列(admin, ledger):
    target = by_account("111111111111")
    post(admin, "/accounts/toggle", key=target.key, enabled="0")

    header = [str(cell).upper() for cell in raw_rows(ledger)[0] if cell]
    assert "ENABLED" in header


def test_没有TAG列的台账也能新增(admin, ledger):
    header, rows = ledger_without("TAG")
    write_ledger(ledger, header=header, rows=rows)
    excel_source.clear_cache()

    response = post(admin, "/accounts/create", **NEW_FORM)
    assert response.status_code == 302
    assert by_account("333333333333").tag_value == "migGAMMA"


# ------------------------------------------------------------------ 备份与审计
def test_每次写入前都留一份备份(admin, ledger):
    post(admin, "/accounts/create", **NEW_FORM)
    backups = list((ledger.parent / excel_source.BACKUP_DIR_NAME).glob("*.xlsx"))
    assert len(backups) == 1

    # 备份里是改动之前的样子
    assert len(raw_rows(backups[0])) == len(LEDGER_ROWS) + 1


def test_备份只保留最近若干份(admin, ledger, monkeypatch):
    monkeypatch.setattr(excel_source, "BACKUP_KEEP", 3)
    target = by_account("111111111111")
    for index in range(6):
        post(
            admin, "/accounts/update",
            key=target.key, partner=f"ALPHA-{index}", account="111111111111",
            budget="500000", tag_ratio="1", untag_ratio="1.05",
            tag_spec="map-migrated=migALPHA",
        )
    backups = list((ledger.parent / excel_source.BACKUP_DIR_NAME).glob("*.xlsx"))
    assert len(backups) == 3


def test_审计日志记录操作但不含凭证(admin, ledger):
    post(admin, "/accounts/create", **NEW_FORM)
    target = by_account("333333333333")
    post(admin, "/accounts/toggle", key=target.key, enabled="0")

    log = (ledger.parent / excel_source.AUDIT_NAME).read_text(encoding="utf-8")
    assert "新增账号 333333333333" in log
    assert "停用账号 333333333333" in log
    assert "tester" in log  # 操作人
    assert NEW_FORM["ak"] not in log
    assert NEW_FORM["sk"] not in log


# ------------------------------------------------------------------ 文件完整性
def test_写入后台账仍然是可读的xlsx(admin, ledger):
    post(admin, "/accounts/create", **NEW_FORM)
    target = by_account("111111111111")
    post(
        admin, "/accounts/update",
        key=target.key, partner="ALPHA-X", account="111111111111",
        budget="9", tag_ratio="1", untag_ratio="1", tag_spec="",
    )
    post(admin, "/accounts/toggle", key=target.key, enabled="0")

    rows = raw_rows(ledger)
    assert rows[0][0] == "PARTNER"
    assert len(rows) == len(LEDGER_ROWS) + 2  # 表头 + 原两行 + 新增一行
    assert len(load_accounts(force=True, include_disabled=True)) == len(LEDGER_ROWS) + 1


def test_写入不会留下临时文件(admin, ledger):
    post(admin, "/accounts/create", **NEW_FORM)
    leftovers = [p.name for p in ledger.parent.glob(".*.tmp")]
    assert leftovers == []


class TestStartDate:
    """启用日期是概览页累计消费的起点，所以它既要能改，也要改不坏。"""

    def test_column_and_form_field_exist(self, admin, ledger):
        html = admin.get("/accounts/").get_data(as_text=True)
        assert ">启用日期</th>" in html
        assert 'name="start_date"' in html
        assert "<code>2026-08-01</code>" in html

    def test_unset_is_called_out(self, admin, ledger):
        """没填不是「空着好看」——概览页会拿 CE 最早可查日兜底，要让人知道去填。"""
        header, rows = ledger_without("START_DATE")
        write_ledger(ledger, header=header, rows=rows)
        excel_source.clear_cache()
        html = admin.get("/accounts/").get_data(as_text=True)
        assert "未设置" in html

    def test_can_be_changed(self, admin, ledger):
        target = by_account("111111111111")
        response = post(
            admin, "/accounts/update",
            key=target.key, partner=target.partner, account=target.account,
            budget="500000", tag_ratio="1", untag_ratio="1.05", tag_spec=target.tag_spec,
            start_date="2026-05-06",
        )
        assert response.status_code == 302
        assert by_account("111111111111").start_date == date(2026, 5, 6)

    def test_can_be_cleared(self, admin, ledger):
        target = by_account("111111111111")
        post(
            admin, "/accounts/update",
            key=target.key, partner=target.partner, account=target.account,
            budget="500000", tag_ratio="1", untag_ratio="1.05", tag_spec=target.tag_spec,
            start_date="",
        )
        assert by_account("111111111111").start_date is None

    def test_rejects_an_unparseable_date(self, admin, ledger):
        target = by_account("111111111111")
        response = post(
            admin, "/accounts/update",
            key=target.key, partner=target.partner, account=target.account,
            budget="500000", tag_ratio="1", untag_ratio="1.05", tag_spec=target.tag_spec,
            start_date="下周一",
        )
        assert "认不出来" in response.get_data(as_text=True)
        assert by_account("111111111111").start_date == date(2026, 8, 1)  # 没被改坏

    def test_rejects_a_future_date(self, admin, ledger):
        """未来日期会让累计区间退化成今天一天，拦在写入之前。"""
        target = by_account("111111111111")
        later = (date.today() + timedelta(days=1)).isoformat()
        response = post(
            admin, "/accounts/update",
            key=target.key, partner=target.partner, account=target.account,
            budget="500000", tag_ratio="1", untag_ratio="1.05", tag_spec=target.tag_spec,
            start_date=later,
        )
        assert "不能晚于今天" in response.get_data(as_text=True)
        assert by_account("111111111111").start_date == date(2026, 8, 1)

    def test_new_account_can_set_it(self, admin, ledger):
        post(admin, "/accounts/create", **NEW_FORM, start_date="2026-07-15")
        assert by_account("333333333333").start_date == date(2026, 7, 15)

    def test_new_account_may_leave_it_empty(self, admin, ledger):
        """启用日期不是必填——老台账迁过来时还没人填。"""
        response = post(admin, "/accounts/create", **NEW_FORM)
        assert response.status_code == 302
        assert by_account("333333333333").start_date is None

    def test_added_to_a_ledger_that_lacks_the_column(self, admin, ledger):
        """老台账没有这一列，写入时自动补上。"""
        header, rows = ledger_without("START_DATE")
        write_ledger(ledger, header=header, rows=rows)
        excel_source.clear_cache()

        post(admin, "/accounts/create", **NEW_FORM, start_date="2026-07-15")
        assert by_account("333333333333").start_date == date(2026, 7, 15)


# ------------------------------------------------------------------ Telegram 告警
class TestTelegramSettings:
    """弹窗里只管群组 ID；开关只在表格里（/accounts/tg-toggle），弹窗保存不碰它。"""

    CHAT = "-1001234567890"
    NO_TOKEN_NOTICE = "表格里开着的 TG 告警在配置好之前一条都不会发"

    def edit(self, admin, target, **fields):
        base = dict(
            key=target.key, partner=target.partner, account=target.account,
            budget=f"{target.budget:g}", tag_ratio="1", untag_ratio=f"{target.untag_ratio:g}",
            tag_spec=target.tag_spec,
            start_date=target.start_date.isoformat() if target.start_date else "",
        )
        base.update(fields)
        return post(admin, "/accounts/update", **base)

    def switch_on(self, admin):
        """页面上开告警的唯一路径：弹窗里填好群，再在表格里点开关。"""
        self.edit(admin, by_account("111111111111"), tg_chat_ids=self.CHAT)
        post(admin, "/accounts/tg-toggle", key=by_account("111111111111").key, tg_enabled="1")
        assert by_account("111111111111").tg_enabled is True

    def test_off_by_default(self, admin, ledger):
        """告警是往外发消息，必须主动开：老台账没这两列，一律算关。"""
        assert by_account("111111111111").tg_enabled is False
        html = admin.get("/accounts/").get_data(as_text=True)
        assert ">TG 告警</th>" in html

    def test_dialogs_have_chat_ids_but_no_switch(self, admin, ledger):
        """开关只在表格里放一处，修改和新增弹窗里都不再放一次。"""
        html = admin.get("/accounts/").get_data(as_text=True)
        for marker in ('id="dlg-edit-1"', 'id="dlg-create"'):
            dialog = _dialog(html, marker)
            assert 'name="tg_chat_ids"' in dialog
            assert 'name="tg_enabled"' not in dialog and 'role="switch"' not in dialog

    def test_saving_chat_ids_does_not_switch_it_on(self, admin, ledger):
        self.edit(admin, by_account("111111111111"), tg_chat_ids=self.CHAT)
        after = by_account("111111111111")
        assert after.tg_chat_ids == (self.CHAT,)
        assert after.tg_enabled is False and after.tg_active is False

    def test_saving_the_dialog_leaves_the_switch_alone(self, admin, ledger):
        """最要紧的一条：弹窗里没有开关，表单里「没有这个字段」不能被当成关——
        否则改一下额度，告警就被悄悄关掉了。"""
        self.switch_on(admin)
        self.edit(admin, by_account("111111111111"), budget="777", tg_chat_ids=self.CHAT)
        after = by_account("111111111111")
        assert after.budget == 777
        assert after.tg_enabled is True

    def test_a_stale_form_cannot_flip_the_switch(self, admin, ledger):
        """上线前打开的旧页面（弹窗里还有开关）或者手搓的请求，带着 tg_enabled 也不算数。"""
        self.edit(admin, by_account("111111111111"), tg_enabled="1", tg_chat_ids=self.CHAT)
        assert by_account("111111111111").tg_enabled is False

    def test_clearing_every_chat_switches_it_off(self, admin, ledger):
        """群全删了开关还开着 = 开着却没处发。顺手关掉，并且在审计里说出来。"""
        self.switch_on(admin)
        self.edit(admin, by_account("111111111111"), tg_chat_ids="")
        after = by_account("111111111111")
        assert after.tg_chat_ids == ()
        assert after.tg_enabled is False
        log = (ledger.parent / "ledger-audit.log").read_text(encoding="utf-8")
        assert "TG_ENABLED 开 → 关（群组 ID 全删了）" in log

    def test_rejects_a_malformed_chat_id(self, admin, ledger):
        response = self.edit(admin, by_account("111111111111"), tg_chat_ids="abc")
        assert response.status_code == 400
        assert "「abc」格式不对" in response.get_data(as_text=True)   # 点名是哪一个

    def test_audit_log_speaks_plainly(self, admin, ledger):
        self.edit(admin, by_account("111111111111"), tg_chat_ids=self.CHAT)
        log = (ledger.parent / "ledger-audit.log").read_text(encoding="utf-8")
        assert f"TG_CHAT_IDS 空 → {self.CHAT}" in log

    def test_chat_id_stays_text_in_excel(self, admin, ledger):
        """存成数字的话，一个长负数可能被 Excel 显示成科学计数法。"""
        self.edit(admin, by_account("111111111111"), tg_chat_ids=self.CHAT)
        header, *rows = raw_rows(ledger)
        column = header.index("TG_CHAT_IDS")
        assert rows[0][column] == self.CHAT

    def test_new_account_with_chats_starts_switched_on(self, admin, ledger):
        """新增时填了群就直接打开：群里马上收到「新账号启用」，之后日报和告警也照常发。"""
        post(admin, "/accounts/create", **NEW_FORM, tg_chat_ids=self.CHAT)
        created = by_account("333333333333")
        assert created.tg_chat_ids == (self.CHAT,)
        assert created.tg_active is True
        log = (ledger.parent / "ledger-audit.log").read_text(encoding="utf-8")
        assert "TG 告警已打开（1 个群）" in log

    def test_new_account_without_chats_starts_switched_off(self, admin, ledger):
        """没填群就是关。表单里硬塞 tg_enabled 也不算数。"""
        post(admin, "/accounts/create", **NEW_FORM, tg_enabled="1")
        assert by_account("333333333333").tg_enabled is False

    def test_new_row_does_not_inherit_a_leftover_switch(self, admin, ledger):
        """追加的那一行可能是手工清空过内容、却留着旧开关值的行：新建时显式写成关。"""
        header = [*LEDGER_HEADER, "TG_ENABLED", "TG_CHAT_IDS"]
        leftover = [None] * len(LEDGER_HEADER) + [True, None]
        write_ledger(ledger, header=header, rows=[*(row + [None, None] for row in LEDGER_ROWS), leftover])
        excel_source.clear_cache()

        post(admin, "/accounts/create", **NEW_FORM)
        assert by_account("333333333333").tg_enabled is False

    def test_disabled_account_never_sends(self, admin, ledger):
        self.switch_on(admin)
        post(admin, "/accounts/toggle", key=by_account("111111111111").key, enabled="0")
        assert by_account("111111111111").tg_active is False

    def test_page_warns_once_when_the_token_is_missing(self, admin, ledger, monkeypatch):
        """开关旁边不放字了：没配 Token 就在页面上说一次，不是每行挂一个「未生效」。"""
        from bedrock_cost import config

        self.switch_on(admin)
        monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "")
        html = admin.get("/accounts/").get_data(as_text=True)
        assert html.count(self.NO_TOKEN_NOTICE) == 1
        assert "未生效" not in html

    def test_no_token_notice_when_nothing_would_be_sent(self, admin, ledger, monkeypatch):
        from bedrock_cost import config

        monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "")
        assert self.NO_TOKEN_NOTICE not in admin.get("/accounts/").get_data(as_text=True)


class TestTelegramTestButton:
    """弹窗里的「发测试消息」：测的是此刻填着的值，不写台账，测完原样回填。"""

    CHAT = "-1001234567890"

    @pytest.fixture
    def outbox(self, monkeypatch):
        """Telegram 那一头：[(群组 ID, 图片下面的文字)]。卡片是真的画出来的。"""
        from bedrock_cost import config, telegram

        box = []
        monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "123:FAKE")
        monkeypatch.setattr(telegram, "send_photo", lambda chat, png, caption: box.append((chat, caption)))
        monkeypatch.setattr(telegram, "send_message", lambda chat, text: box.append((chat, text)))
        return box

    def test_sends_to_the_typed_chat_without_saving(self, admin, ledger, outbox):
        target = by_account("111111111111")
        before = ledger.stat().st_mtime_ns
        response = post(
            admin, "/accounts/tg-test",
            key=target.key, partner=target.partner, account=target.account,
            budget="500000", tg_chat_ids=self.CHAT,
        )
        assert response.status_code == 200
        assert outbox[0][0] == self.CHAT
        assert "测试消息" in outbox[0][1]
        assert ledger.stat().st_mtime_ns == before       # 一个字节都没写

    def test_reopens_the_same_dialog_with_the_input_kept(self, admin, ledger, outbox):
        target = by_account("111111111111")
        html = post(
            admin, "/accounts/tg-test",
            key=target.key, partner=target.partner, account=target.account,
            budget="777", tg_chat_ids=self.CHAT,
        ).get_data(as_text=True)
        assert "全部发送成功" in html
        assert f'value="{self.CHAT}"' in html
        assert 'value="777"' in html                     # 没保存的其他字段也还在
        dialog = html[html.index('data-reopen'):]
        assert "全部发送成功" in dialog[: dialog.index("</dialog>")]

    def test_explains_a_failure(self, admin, ledger, monkeypatch):
        from bedrock_cost import config, telegram
        from bedrock_cost.telegram import TelegramError

        monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "123:FAKE")

        def boom(chat, png, caption):
            raise TelegramError("群组 ID 不对，或者 bot 还没有被拉进这个群")

        monkeypatch.setattr(telegram, "send_photo", boom)
        target = by_account("111111111111")
        html = post(admin, "/accounts/tg-test", key=target.key, tg_chat_ids=self.CHAT).get_data(as_text=True)
        assert "bot 还没有被拉进这个群" in html

    def test_names_the_account_but_not_the_partner(self, admin, ledger, outbox, monkeypatch):
        """和正式消息一样：群里只看得到账号 ID，看不到上游——图片下面的字和卡片上的字都是。"""
        from bedrock_cost import alerts

        seen = []
        monkeypatch.setattr(alerts, "_send", lambda chat, card: seen.append(card) or "")
        target = by_account("111111111111")
        post(
            admin, "/accounts/tg-test",
            key=target.key, partner=target.partner, account=target.account, tg_chat_ids=self.CHAT,
        )
        text = seen[0].text()                            # caption + 卡片上画的每一个字
        assert target.account in text
        assert target.partner not in text

    def test_says_so_when_the_card_had_to_fall_back_to_text(self, admin, ledger, outbox, monkeypatch):
        """卡片画不出来（比如字体文件丢了）会改发文字：发是发出去了，但页面上要说清楚。"""
        from bedrock_cost import cards

        def boom(card):
            raise OSError("cannot open resource")

        monkeypatch.setattr(cards, "render", boom)
        target = by_account("111111111111")
        html = post(admin, "/accounts/tg-test", key=target.key, tg_chat_ids=self.CHAT).get_data(as_text=True)
        assert outbox and "测试消息" in outbox[0][1]      # 文字照样发了
        assert "✓ 已发送（卡片画不出来，改发了文字" in html

    def test_asks_for_a_chat_id_first(self, admin, ledger, outbox):
        target = by_account("111111111111")
        html = post(admin, "/accounts/tg-test", key=target.key, tg_chat_ids="").get_data(as_text=True)
        assert "先填群组 ID" in html
        assert outbox == []

    def test_works_from_the_new_account_dialog(self, admin, ledger, outbox):
        html = post(admin, "/accounts/tg-test", account="333333333333", tg_chat_ids=self.CHAT).get_data(as_text=True)
        assert outbox and "全部发送成功" in html
        assert 'id="dlg-create" data-reopen' in html

    def test_needs_csrf(self, admin, ledger, outbox):
        """和其他 POST 一样：令牌不对就提示后退回，什么都不发。"""
        response = admin.post("/accounts/tg-test", data={"tg_chat_ids": self.CHAT})
        assert response.status_code == 302
        assert outbox == []

    def test_needs_login(self, client, ledger, outbox):
        response = client.post("/accounts/tg-test", data={"tg_chat_ids": self.CHAT})
        assert response.status_code == 302
        assert outbox == []


# ------------------------------------------------------------------ 多个群 / 表格里的开关 / 图标按钮
def _edit(admin, target, **fields):
    """提交修改弹窗：不传的字段按账号现值填。"""
    base = dict(
        key=target.key, partner=target.partner, account=target.account,
        budget=f"{target.budget:g}", tag_ratio="1", untag_ratio=f"{target.untag_ratio:g}",
        tag_spec=target.tag_spec,
        start_date=target.start_date.isoformat() if target.start_date else "",
    )
    base.update(fields)
    return post(admin, "/accounts/update", **base)


def _dialog(html: str, marker: str) -> str:
    """截出某个弹窗的 HTML。"""
    part = html[html.index(marker) :]
    return part[: part.index("</dialog>")]


class TestMultipleChats:
    A, B, C = "-1001111111111", "-1002222222222", "-1003333333333"

    def test_saves_every_row(self, admin, ledger):
        """一行一个框、同名字段有多个——全部要存下，不能只剩第一个。"""
        _edit(admin, by_account("111111111111"), tg_chat_ids=[self.A, self.B])
        assert by_account("111111111111").tg_chat_ids == (self.A, self.B)
        header, *rows = raw_rows(ledger)
        assert rows[0][header.index("TG_CHAT_IDS")] == f"{self.A},{self.B}"

    def test_blank_rows_and_duplicates_are_dropped(self, admin, ledger):
        _edit(admin, by_account("111111111111"), tg_chat_ids=[self.A, "", self.B, self.A, "  "])
        assert by_account("111111111111").tg_chat_ids == (self.A, self.B)

    def test_a_pasted_list_in_one_box_is_split(self, admin, ledger):
        """有人会把一串 ID 一次粘进第一个框。"""
        _edit(admin, by_account("111111111111"), tg_chat_ids=[f"{self.A}, {self.B}"])
        assert by_account("111111111111").tg_chat_ids == (self.A, self.B)

    def test_the_bad_one_is_named(self, admin, ledger):
        response = _edit(admin, by_account("111111111111"), tg_chat_ids=[self.A, "oops", self.B])
        assert response.status_code == 400
        assert "「oops」格式不对" in response.get_data(as_text=True)
        assert by_account("111111111111").tg_chat_ids == ()          # 一个都没存

    def test_too_many_chats(self, admin, ledger):
        many = [f"-100{n:010d}" for n in range(excel_source.MAX_TG_CHATS + 1)]
        response = _edit(admin, by_account("111111111111"), tg_chat_ids=many)
        assert response.status_code == 400
        assert f"最多 {excel_source.MAX_TG_CHATS} 个群" in response.get_data(as_text=True)

    def test_a_failed_save_keeps_every_row_in_the_form(self, admin, ledger):
        """校验失败回填时，三行都得回来——只回填第一行等于让人重打。"""
        html = _edit(
            admin, by_account("111111111111"), budget="abc", tg_chat_ids=[self.A, self.B, self.C]
        ).get_data(as_text=True)
        dialog = _dialog(html, "data-reopen")
        for chat in (self.A, self.B, self.C):
            assert f'value="{chat}"' in dialog

    def test_same_set_again_is_not_a_change(self, admin, ledger):
        _edit(admin, by_account("111111111111"), tg_chat_ids=[self.A, self.B])
        before = ledger.stat().st_mtime_ns
        _edit(admin, by_account("111111111111"), tg_chat_ids=[f"{self.A} ,{self.B}"])
        assert ledger.stat().st_mtime_ns == before

    def test_edit_dialog_shows_one_row_per_chat(self, admin, ledger):
        _edit(admin, by_account("111111111111"), tg_chat_ids=[self.A, self.B])
        dialog = _dialog(admin.get("/accounts/").get_data(as_text=True), 'id="dlg-edit-1"')
        assert dialog.count('name="tg_chat_ids"') == 2
        assert "data-add-chat" in dialog and "data-remove-chat" in dialog

    def test_empty_account_still_gets_one_box(self, admin, ledger):
        dialog = _dialog(admin.get("/accounts/").get_data(as_text=True), 'id="dlg-edit-1"')
        assert dialog.count('name="tg_chat_ids"') == 1

    def test_test_button_reports_each_chat(self, admin, ledger, monkeypatch):
        """挨个发，结果贴在各自那一行旁边：哪个 ID 错了一眼看到。"""
        from bedrock_cost import config, telegram
        from bedrock_cost.telegram import TelegramError

        monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "123:FAKE")
        sent = []

        def send(chat, png, caption):
            if chat == self.B:
                raise TelegramError("bot 不在这个群里，先把它拉进来")
            sent.append(chat)

        monkeypatch.setattr(telegram, "send_photo", send)
        target = by_account("111111111111")
        html = post(
            admin, "/accounts/tg-test", key=target.key,
            tg_chat_ids=[self.A, self.B, "nope"],
        ).get_data(as_text=True)
        assert sent == [self.A]                                  # 格式不对的根本没发
        assert "3 个群里 1 个成功、2 个失败" in html
        assert "✓ 已发送" in html
        assert "✗ bot 不在这个群里" in html
        assert "✗ 格式不对" in html


class TestTableToggle:
    """表格里的 TG 开关：点一下立刻写台账，只翻 TG_ENABLED 这一格。"""

    CHAT = "-1001234567890"

    def with_chat(self, admin):
        _edit(admin, by_account("111111111111"), tg_chat_ids=self.CHAT)
        return by_account("111111111111").key

    @staticmethod
    def cells(admin) -> list[str]:
        """每一行的 TG 告警格，按台账顺序（第一个是 111111111111）。"""
        html = admin.get("/accounts/").get_data(as_text=True)
        return re.findall(r'<td class="col-tg">(.*?)</td>', html, re.S)

    def test_switches_on_and_off(self, admin, ledger):
        key = self.with_chat(admin)
        post(admin, "/accounts/tg-toggle", key=key, tg_enabled="1")
        assert by_account("111111111111").tg_enabled is True
        post(admin, "/accounts/tg-toggle", key=key, tg_enabled="0")
        assert by_account("111111111111").tg_enabled is False

    def test_leaves_the_chat_ids_alone(self, admin, ledger):
        post(admin, "/accounts/tg-toggle", key=self.with_chat(admin), tg_enabled="1")
        assert by_account("111111111111").tg_chat_ids == (self.CHAT,)

    def test_cannot_switch_on_without_a_chat(self, admin, ledger):
        """页面上是灰的，但页面可能是几分钟前开的——写入时在锁里再判一次。"""
        response = post(admin, "/accounts/tg-toggle", key=by_account("111111111111").key, tg_enabled="1")
        assert response.status_code == 302
        assert by_account("111111111111").tg_enabled is False
        assert "先点「修改」填上再开" in admin.get("/accounts/").get_data(as_text=True)  # 提示条

    def test_renders_as_a_switch(self, admin, ledger, monkeypatch):
        from bedrock_cost import config

        monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "123:FAKE")   # 配好了 Token 的服务器
        post(admin, "/accounts/tg-toggle", key=self.with_chat(admin), tg_enabled="1")
        cell = self.cells(admin)[0]
        assert 'role="switch"' in cell and 'aria-checked="true"' in cell
        assert f'title="点一下关闭（1 个群：{self.CHAT}）"' in cell

    def test_switch_is_disabled_without_a_chat(self, admin, ledger):
        cell = self.cells(admin)[0]
        assert "disabled" in cell
        assert 'title="还没有填群组 ID，先点「修改」填上"' in cell

    def test_no_text_beside_the_switch(self, admin, ledger):
        """开关旁边不放字：群数、账号停用、没配 Token 都收进 title，悬停才看。"""
        post(admin, "/accounts/tg-toggle", key=self.with_chat(admin), tg_enabled="1")
        cells = self.cells(admin)
        assert len(cells) == len(LEDGER_ROWS)                   # 开着有群的、没填群的都在
        for cell in cells:
            assert re.sub(r"<[^>]+>", "", cell).strip() == ""

    def test_title_says_when_nothing_will_be_sent(self, admin, ledger, monkeypatch, fake_costs):
        from bedrock_cost import alerts, config

        # 停用会给群发一张「账号停用」通知（TestAccountNotices 管它），这里不关心，换掉
        monkeypatch.setattr(alerts, "_send", lambda chat, card: "")
        key = self.with_chat(admin)
        post(admin, "/accounts/tg-toggle", key=key, tg_enabled="1")
        monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "")
        assert "。服务器没配 TELEGRAM_BOT_TOKEN，不会发" in self.cells(admin)[0]

        monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "123:FAKE")
        assert "不会发" not in self.cells(admin)[0]
        post(admin, "/accounts/toggle", key=key, enabled="0")
        assert "。账号已停用，不会发" in self.cells(admin)[0]

    def test_is_audited(self, admin, ledger):
        post(admin, "/accounts/tg-toggle", key=self.with_chat(admin), tg_enabled="1")
        log = (ledger.parent / "ledger-audit.log").read_text(encoding="utf-8")
        assert "开启账号 111111111111 的 TG 告警" in log

    def test_needs_csrf(self, admin, ledger):
        key = self.with_chat(admin)
        admin.post("/accounts/tg-toggle", data={"key": key, "tg_enabled": "1"})
        assert by_account("111111111111").tg_enabled is False

    def test_needs_login(self, client, ledger):
        response = client.post("/accounts/tg-toggle", data={"key": "x", "tg_enabled": "1"})
        assert response.status_code == 302 and "/login" in response.headers["Location"]


class TestIconButtons:
    """修改 / 停用 / 恢复换成了图标按钮：没有文字，靠 title 和 aria-label 说清是什么。"""

    def test_edit_and_disable_are_icons(self, admin, ledger):
        html = admin.get("/accounts/").get_data(as_text=True)
        assert ">修改</button>" not in html and ">停用</button>" not in html
        assert 'aria-label="修改 ALPHA / 111111111111"' in html
        assert 'aria-label="停用 111111111111"' in html

    def test_restore_is_an_icon(self, admin, ledger):
        post(admin, "/accounts/toggle", key=by_account("111111111111").key, enabled="0")
        html = admin.get("/accounts/").get_data(as_text=True)
        assert ">恢复</button>" not in html
        assert 'aria-label="恢复 111111111111"' in html

    def test_icons_still_open_their_dialogs(self, admin, ledger):
        """换的只是外观：弹窗照旧由 data-open 打开，停用照旧先确认。"""
        html = admin.get("/accounts/").get_data(as_text=True)
        assert html.count('data-open="dlg-edit-') == len(LEDGER_ROWS)
        assert html.count('data-open="dlg-off-') == len(LEDGER_ROWS)


class TestAccountNotices:
    """新增 / 停用 / 恢复账号时，按 TG 开关给账号的群发一张通知卡片。"""

    CHAT = "-1001234567890"

    @pytest.fixture
    def cards_sent(self, monkeypatch):
        """[(群组 ID, 卡片)]。Token 给一个假的，发送口换掉。"""
        from bedrock_cost import alerts, config

        box = []
        monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "123:FAKE")
        monkeypatch.setattr(alerts, "_send", lambda chat, card: box.append((chat, card)) or "")
        return box

    def switch_on(self, admin):
        _edit(admin, by_account("111111111111"), tg_chat_ids=self.CHAT)
        post(admin, "/accounts/tg-toggle", key=by_account("111111111111").key, tg_enabled="1")

    def page(self, admin) -> str:
        return admin.get("/accounts/").get_data(as_text=True)

    def test_a_new_account_with_a_chat_is_announced(self, admin, ledger, cards_sent):
        post(admin, "/accounts/create", **NEW_FORM, tg_chat_ids=self.CHAT)
        assert [(chat, card.kind) for chat, card in cards_sent] == [(self.CHAT, "created")]
        text = cards_sent[0][1].text()
        assert "新账号启用" in text and "333333333333" in text and "$150,000.00" in text
        assert "已通知这个账号的 1 个 TG 群" in self.page(admin)

    def test_a_new_account_without_a_chat_sends_nothing(self, admin, ledger, cards_sent):
        post(admin, "/accounts/create", **NEW_FORM)
        assert cards_sent == []

    def test_disabling_sends_a_last_notice_with_the_spend(self, admin, ledger, cards_sent, fake_costs):
        """停用时账号已经不算 tg_active 了，但这条「账号停用」正是它的最后一条消息。"""
        from bedrock_cost.dates import cumulative_range

        self.switch_on(admin)
        post(admin, "/accounts/toggle", key=by_account("111111111111").key, enabled="0")
        (chat, card), = cards_sent
        assert (chat, card.kind, card.title) == (self.CHAT, "disabled", "账号停用")
        rows = {row.label: row.value for row in card.blocks[1].rows}
        assert rows["账号状态"] == "已停用"
        start, end, _ = cumulative_range(RANGE_START, date.today())   # 和概览页同一个口径
        spent = expected_marked(by_account("111111111111"), start, end)
        assert rows["停用前累计消费"] == f"${spent:,.2f}"
        assert by_account("111111111111").enabled is False

    def test_restoring_is_announced_too(self, admin, ledger, cards_sent, fake_costs):
        self.switch_on(admin)
        key = by_account("111111111111").key
        post(admin, "/accounts/toggle", key=key, enabled="0")
        post(admin, "/accounts/toggle", key=key, enabled="1")
        assert [card.kind for _, card in cards_sent] == ["disabled", "restored"]

    def test_follows_the_tg_switch(self, admin, ledger, cards_sent, fake_costs):
        """TG 告警没开的账号，停用、恢复都不发。"""
        _edit(admin, by_account("111111111111"), tg_chat_ids=self.CHAT)      # 填了群、开关没开
        key = by_account("111111111111").key
        post(admin, "/accounts/toggle", key=key, enabled="0")
        post(admin, "/accounts/toggle", key=key, enabled="1")
        assert cards_sent == []

    def test_nothing_is_sent_when_nothing_changed(self, admin, ledger, cards_sent):
        """点了恢复、但账号本来就是启用的：台账没变，也不发。"""
        self.switch_on(admin)
        post(admin, "/accounts/toggle", key=by_account("111111111111").key, enabled="1")
        assert cards_sent == []

    def test_a_failed_notice_does_not_undo_the_change(self, admin, ledger, fake_costs, monkeypatch):
        from bedrock_cost import alerts, config
        from bedrock_cost.telegram import TelegramError

        monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "123:FAKE")
        self.switch_on(admin)

        def down(chat, card):
            raise TelegramError("连不上 Telegram")

        monkeypatch.setattr(alerts, "_send", down)
        post(admin, "/accounts/toggle", key=by_account("111111111111").key, enabled="0")
        assert by_account("111111111111").enabled is False            # 照样停用了
        page = self.page(admin)
        assert "停用账号 111111111111" in page
        assert "TG 通知没有全部发出去" in page and "连不上 Telegram" in page

    def test_missing_token_is_reported(self, admin, ledger):
        """测试里默认没有 Token：通知发不了，要说出来，而不是悄悄不发。"""
        post(admin, "/accounts/create", **NEW_FORM, tg_chat_ids=self.CHAT)
        assert "TG 通知没有全部发出去：服务器没有配置 TELEGRAM_BOT_TOKEN" in self.page(admin)
        assert by_account("333333333333") is not None                 # 账号照样建好了

    def test_the_page_says_what_will_be_sent(self, admin, ledger):
        """新增弹窗说清楚「填了群就开告警、会发卡片」；停用确认窗在账号开着 TG 时多一条。"""
        page = self.page(admin)
        assert "账号建好就会打开 TG 告警" in _dialog(page, 'id="dlg-create"')
        assert "「账号停用」卡片" not in _dialog(page, 'id="dlg-off-1"')
        self.switch_on(admin)
        assert "它的 1 个 TG 群会收到一张「账号停用」卡片" in _dialog(self.page(admin), 'id="dlg-off-1"')

    def test_notices_do_not_name_the_partner(self, admin, ledger, cards_sent, fake_costs):
        self.switch_on(admin)
        key = by_account("111111111111").key
        post(admin, "/accounts/toggle", key=key, enabled="0")
        post(admin, "/accounts/toggle", key=key, enabled="1")
        post(admin, "/accounts/create", **NEW_FORM, tg_chat_ids=self.CHAT)
        assert len(cards_sent) == 3
        for _, card in cards_sent:
            assert "ALPHA" not in card.text() and "GAMMA" not in card.text()
