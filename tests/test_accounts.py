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
import pytest

from bedrock_cost import excel_source
from bedrock_cost.excel_source import LedgerConflict, load_accounts

from .conftest import LEDGER_HEADER, LEDGER_ROWS, write_ledger

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
    for _, _, _, _, _, ak, sk, _ in LEDGER_ROWS:
        assert sk not in html
        assert ak not in html
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
        ("budget", "abc", "预算要填数字"),
        ("budget", "-1", "预算不能是负数"),
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
    write_ledger(
        ledger,
        header=[name for name in LEDGER_HEADER if name != "TAG"],
        rows=[row[:-1] for row in LEDGER_ROWS],
    )
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
