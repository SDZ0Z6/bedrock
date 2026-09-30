"""账号管理页：删除账号（要输登录密码）和「只有新增、修改才备份」。

删除是把整行清空、不删行：行号不动，其他账号的 key 和缓存键都不受影响——和停用的软删
是同一条底线（见 excel_source 模块说明）。
"""

from __future__ import annotations

import re

import pytest

from bedrock_cost import auth, config, excel_source

from .conftest import LEDGER_ROWS, TEST_PASSWORD
from .test_accounts import NEW_FORM, _dialog, _edit, admin, by_account, post, raw_rows  # noqa: F401


def backups(ledger) -> list:
    return sorted((ledger.parent / excel_source.BACKUP_DIR_NAME).glob("*.xlsx"))


def audit(ledger) -> str:
    path = ledger.parent / excel_source.AUDIT_NAME
    return path.read_text(encoding="utf-8") if path.exists() else ""


def delete(admin, account_id: str, password: str = TEST_PASSWORD):
    return post(admin, "/accounts/delete", key=by_account(account_id).key, password=password)


class TestDelete:
    def test_every_row_has_a_delete_icon(self, admin, ledger):
        html = admin.get("/accounts/").get_data(as_text=True)
        assert html.count('data-open="dlg-del-') == len(LEDGER_ROWS)
        assert 'aria-label="删除 111111111111"' in html
        assert ">删除</button>" not in html                       # 和别的操作一样只有图标

    def test_the_dialog_asks_for_the_login_password(self, admin, ledger):
        dialog = _dialog(admin.get("/accounts/").get_data(as_text=True), 'id="dlg-del-1"')
        assert "删除后不能恢复" in dialog
        assert re.search(r'name="password" type="password" required', dialog)
        assert "删除不会自动备份" in dialog
        assert "「停用」就够了" in dialog                          # 启用着的账号先提醒一句停用

    def test_the_right_password_clears_the_whole_row(self, admin, ledger):
        response = delete(admin, "111111111111")
        assert response.status_code == 302
        assert by_account("111111111111") is None
        header, *rows = raw_rows(ledger)
        assert rows[0] == [None] * len(header)                   # 整行清空，凭证一起没了
        assert rows[1][header.index("ACCOUNT")] == 222222222222   # 下面那行原地不动
        assert "删除账号 111111111111（ALPHA）" in admin.get("/accounts/").get_data(as_text=True)

    def test_other_rows_keep_their_keys(self, admin, ledger):
        """不删行：删行会让下面所有账号的行号上移，key 和各处的缓存键跟着错位。"""
        before = by_account("222222222222").key
        delete(admin, "111111111111")
        assert by_account("222222222222").key == before

    def test_a_wrong_password_deletes_nothing(self, admin, ledger):
        before = ledger.stat().st_mtime_ns
        response = delete(admin, "111111111111", password="wrong-password")
        assert response.status_code == 403
        assert ledger.stat().st_mtime_ns == before
        html = response.get_data(as_text=True)
        dialog = _dialog(html, 'id="dlg-del-1"')
        assert "data-reopen" in html[html.index('id="dlg-del-1"') - 40 : html.index('id="dlg-del-1"') + 80]
        assert "登录密码不对，账号没有删除" in dialog
        assert "wrong-password" not in html

    def test_wrong_passwords_share_the_login_lockout(self, admin, ledger, monkeypatch):
        """拿到一个没退出的会话，也不能靠这里一遍遍试出密码。"""
        monkeypatch.setattr(config, "MAX_LOGIN_ATTEMPTS", 2)
        delete(admin, "111111111111", password="wrong-1")
        delete(admin, "111111111111", password="wrong-2")
        response = delete(admin, "111111111111")                  # 这次密码对了也不行
        assert response.status_code == 429
        assert "密码输错太多次了" in response.get_data(as_text=True)
        assert by_account("111111111111") is not None
        auth.clear_failures()

    def test_the_audit_log_never_sees_the_password(self, admin, ledger):
        delete(admin, "111111111111", password="wrong-password")
        delete(admin, "111111111111")
        log = audit(ledger)
        assert "删除账号 111111111111（ALPHA）" in log
        assert TEST_PASSWORD not in log and "wrong-password" not in log

    def test_a_deleted_id_can_be_added_again(self, admin, ledger):
        """和停用不同：删掉就是真的没了，同一个账号 ID 可以重新新增。"""
        delete(admin, "111111111111")
        post(admin, "/accounts/create", **{**NEW_FORM, "account": "111111111111"})
        assert by_account("111111111111").partner == "GAMMA"

    def test_disabled_accounts_can_be_deleted_too(self, admin, ledger):
        post(admin, "/accounts/toggle", key=by_account("111111111111").key, enabled="0")
        delete(admin, "111111111111")
        assert by_account("111111111111") is None

    def test_a_stale_page_is_caught(self, admin, ledger):
        response = post(admin, "/accounts/delete", key="999999999999#9", password=TEST_PASSWORD)
        assert response.status_code == 302
        assert len(excel_source.load_accounts(force=True, include_disabled=True)) == len(LEDGER_ROWS)

    def test_needs_csrf(self, admin, ledger):
        admin.post("/accounts/delete", data={"key": by_account("111111111111").key, "password": TEST_PASSWORD})
        assert by_account("111111111111") is not None

    def test_needs_login(self, client, ledger):
        response = client.post("/accounts/delete", data={"key": "x", "password": TEST_PASSWORD})
        assert response.status_code == 302 and "/login" in response.headers["Location"]


class TestBackups:
    """只有新增和修改账号才备份：开关、停用 / 恢复、删除都不备份。"""

    def test_creating_and_editing_back_up(self, admin, ledger):
        post(admin, "/accounts/create", **NEW_FORM)
        _edit(admin, by_account("111111111111"), budget="777")
        assert len(backups(ledger)) == 2

    def test_switches_disabling_and_deleting_do_not(self, admin, ledger):
        from .test_accounts_mail import MAILBOX

        _edit(admin, by_account("111111111111"), tg_chat_ids="-1001234567890", **MAILBOX)
        assert len(backups(ledger)) == 1
        key = by_account("111111111111").key
        post(admin, "/accounts/tg-toggle", key=key, tg_enabled="1")
        post(admin, "/accounts/mail-toggle", key=key, mail_enabled="1")
        post(admin, "/accounts/toggle", key=key, enabled="0")
        post(admin, "/accounts/toggle", key=key, enabled="1")
        delete(admin, "222222222222")
        assert len(backups(ledger)) == 1                          # 还是改字段时的那一份
        log = audit(ledger)
        for line in ("开启账号 111111111111 的 TG 告警", "开启账号 111111111111 的邮件告警",
                     "停用账号 111111111111", "恢复账号 111111111111", "删除账号 222222222222"):
            assert line in log                                   # 不备份，但照样记审计

    def test_the_page_says_when_it_backs_up(self, admin, ledger):
        html = admin.get("/accounts/").get_data(as_text=True)
        assert "新增和修改账号前自动备份" in html and "开关、停用和删除不备份" in html
