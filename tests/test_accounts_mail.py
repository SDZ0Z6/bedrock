"""账号管理页上的告警邮箱：弹窗里填平台 / 地址 / 密码，表格里的开关，「测试连接」。

和 test_accounts 同一套写法（同样的临时台账、登录、CSRF 帮手）。重点还是「写坏了会怎样」：
密码只进不出——页面、回填的表单、审计日志、repr 里都不能出现。

要收的一般就是账号邮箱本身：告警邮箱的地址留空、只填密码，保存和「测试连接」都用账号邮箱。

弹窗里「告警邮箱」这一页的版式和提示文字会改，这里只认字段名、data-* 钩子和服务端给的文字。
"""

from __future__ import annotations

import re

import pytest

from bedrock_cost import excel_source, mail_inbox

from .test_accounts import (  # noqa: F401
    EMAILS,
    NEW_FORM,
    _dialog,
    _edit,
    admin,
    by_account,
    input_attrs,
    page,
    post,
    raw_rows,
    table_rows,
    text,
    token,
    warn_notes,
)

MAIL_PASSWORD = "S3cret-Client-Pass"
MAILBOX = {"mail_provider": "aliyun-sg", "mail_address": "root-alpha@example.com", "mail_password": MAIL_PASSWORD}


def mail_cells(admin) -> list[str]:
    """每一行的「邮件告警」格，按台账顺序（第一个是 111111111111）。"""
    return re.findall(r'<td class="col-mail">(.*?)</td>', page(admin), re.S)


def mail_result(fragment: str) -> tuple[bool, str]:
    """「测试连接」的结果框（data-mail-result）：(是否显示, 里面的字)。"""
    found = re.search(r"<(\w+)\b([^>]*\bdata-mail-result\b[^>]*)>(.*?)</\1>", fragment, re.S)
    assert found, "弹窗里没有测试结果框"
    return not re.search(r"\shidden\b", found.group(2)), text(found.group(3))


def stored_password(ledger, row: int = 0):
    header, *rows = raw_rows(ledger)
    return rows[row][header.index("MAIL_PASSWORD")] if "MAIL_PASSWORD" in header else None


def audit(ledger) -> str:
    return (ledger.parent / "ledger-audit.log").read_text(encoding="utf-8")


class TestMailSettings:
    """弹窗里填告警邮箱，开关只在表格里。密码只进不出。"""

    def test_dialogs_have_the_mailbox_but_no_switch(self, admin, ledger):
        html = page(admin)
        for marker in ('id="dlg-edit-1"', 'id="dlg-create"'):
            dialog = _dialog(html, marker)
            assert 'name="mail_provider"' in dialog and 'name="mail_address"' in dialog
            assert input_attrs(dialog, "mail_password")["type"] == "password"
            assert 'name="mail_enabled"' not in dialog
        assert "阿里邮箱 · 国际站（新加坡）" in html and "腾讯企业邮" in html and "其他平台" in html

    def test_off_by_default(self, admin, ledger):
        assert by_account("111111111111").mail_enabled is False
        assert ">邮件告警</th>" in page(admin)

    def test_new_account_with_a_mailbox_starts_switched_on(self, admin, ledger):
        post(admin, "/accounts/create", **NEW_FORM, **MAILBOX)
        created = by_account("333333333333")
        assert created.mail_active is True
        assert (created.mail_provider, created.mail_address) == ("aliyun-sg", "root-alpha@example.com")
        assert created.mail_box.host == "imap.sg.aliyun.com"
        assert "邮件告警已打开（root-alpha@example.com）" in audit(ledger)

    def test_new_account_without_a_mailbox_starts_switched_off(self, admin, ledger):
        post(admin, "/accounts/create", **NEW_FORM, mail_enabled="1")
        assert by_account("333333333333").mail_enabled is False

    def test_a_mailbox_needs_a_password(self, admin, ledger):
        response = post(admin, "/accounts/create", **NEW_FORM, mail_provider="aliyun-sg",
                        mail_address="root-alpha@example.com")
        assert response.status_code == 400
        assert "填了告警邮箱就要填密码（三方客户端安全密码）" in response.get_data(as_text=True)
        assert by_account("333333333333") is None

    def test_rejects_a_malformed_address(self, admin, ledger):
        response = _edit(admin, by_account("111111111111"), **{**MAILBOX, "mail_address": "not-an-email"})
        assert response.status_code == 400
        assert "告警邮箱的地址格式不对" in response.get_data(as_text=True)

    def test_blank_password_keeps_the_saved_one(self, admin, ledger):
        _edit(admin, by_account("111111111111"), **MAILBOX)
        _edit(admin, by_account("111111111111"), **{**MAILBOX, "mail_password": ""}, budget="777")
        after = by_account("111111111111")
        assert after.budget == 777 and after.mail_password == MAIL_PASSWORD

    def test_a_new_password_replaces_the_old_one(self, admin, ledger):
        _edit(admin, by_account("111111111111"), **MAILBOX)
        _edit(admin, by_account("111111111111"), **{**MAILBOX, "mail_password": "another-pass"})
        assert by_account("111111111111").mail_password == "another-pass"
        assert "MAIL_PASSWORD 已设置" in audit(ledger) and "MAIL_PASSWORD 已更新" in audit(ledger)

    def test_a_new_address_needs_its_password(self, admin, ledger):
        _edit(admin, by_account("111111111111"), **MAILBOX)
        response = _edit(admin, by_account("111111111111"),
                         **{**MAILBOX, "mail_address": "someone-else@example.com", "mail_password": ""})
        assert response.status_code == 400
        assert "换了邮箱地址，要重新填这个邮箱的密码" in response.get_data(as_text=True)
        assert by_account("111111111111").mail_address == "root-alpha@example.com"

    def test_clearing_the_address_clears_the_password_and_switches_off(self, admin, ledger):
        _edit(admin, by_account("111111111111"), **MAILBOX)
        post(admin, "/accounts/mail-toggle", key=by_account("111111111111").key, mail_enabled="1")
        _edit(admin, by_account("111111111111"), mail_address="", mail_password="")
        after = by_account("111111111111")
        assert (after.mail_address, after.mail_password, after.mail_enabled) == ("", "", False)
        assert stored_password(ledger) is None
        assert "MAIL_PASSWORD 已清除" in audit(ledger)
        assert "MAIL_ENABLED 开 → 关（告警邮箱删了）" in audit(ledger)

    def test_custom_provider_needs_a_server(self, admin, ledger):
        response = _edit(admin, by_account("111111111111"), **{**MAILBOX, "mail_provider": "custom"})
        assert response.status_code == 400
        assert "就要填 IMAP 服务器" in response.get_data(as_text=True)
        _edit(admin, by_account("111111111111"),
              **{**MAILBOX, "mail_provider": "custom", "mail_server": "IMAP.Example.com"})
        after = by_account("111111111111")
        assert after.mail_server == "imap.example.com:993"
        assert (after.mail_box.host, after.mail_box.port) == ("imap.example.com", 993)

    def test_preset_providers_do_not_store_a_server(self, admin, ledger):
        _edit(admin, by_account("111111111111"), **MAILBOX, mail_server="imap.evil.example")
        assert by_account("111111111111").mail_server == ""

    def test_the_password_is_never_rendered(self, admin, ledger):
        _edit(admin, by_account("111111111111"), **MAILBOX)
        html = page(admin)
        assert MAIL_PASSWORD not in html
        assert input_attrs(_dialog(html, 'id="dlg-edit-1"'), "mail_password").get("value", "") == ""
        # 校验没过、回填表单的时候也不回填密码
        failed = _edit(admin, by_account("111111111111"), **MAILBOX, budget="abc")
        assert failed.status_code == 400
        assert MAIL_PASSWORD not in failed.get_data(as_text=True)

    def test_the_password_never_reaches_the_audit_log(self, admin, ledger):
        post(admin, "/accounts/create", **NEW_FORM, **MAILBOX)
        _edit(admin, by_account("333333333333"), **{**MAILBOX, "mail_password": "second-pass-123"})
        log = audit(ledger)
        assert MAIL_PASSWORD not in log and "second-pass-123" not in log

    def test_a_password_starting_with_equals_is_not_a_formula(self, admin, ledger):
        """openpyxl 会把「=」开头的字符串存成公式。"""
        _edit(admin, by_account("111111111111"), **{**MAILBOX, "mail_password": "=SUM(A1)x"})
        assert by_account("111111111111").mail_password == "=SUM(A1)x"

    def test_the_account_repr_hides_the_password(self, admin, ledger):
        _edit(admin, by_account("111111111111"), **MAILBOX)
        assert MAIL_PASSWORD not in repr(by_account("111111111111"))


class TestMailAddressDefault:
    """告警邮箱的地址留空、却填了密码：要收的就是账号邮箱本身，地址照它填上。"""

    def test_a_new_account_with_only_a_password(self, admin, ledger):
        post(admin, "/accounts/create", **NEW_FORM, mail_password=MAIL_PASSWORD)
        created = by_account("333333333333")
        assert (created.mail_address, created.mail_provider) == (NEW_FORM["email"], mail_inbox.DEFAULT_PROVIDER)
        assert created.mail_password == MAIL_PASSWORD
        assert created.mail_active is True                       # 新增时填了邮箱就打开
        assert f"邮件告警已打开（{NEW_FORM['email']}）" in audit(ledger)

    def test_an_edit_with_only_a_password(self, admin, ledger):
        _edit(admin, by_account("111111111111"), mail_password=MAIL_PASSWORD)
        after = by_account("111111111111")
        assert (after.mail_address, after.mail_password) == (EMAILS["111111111111"], MAIL_PASSWORD)
        assert after.mail_enabled is False                       # 开关还是只在表格里点
        assert f"MAIL_ADDRESS 空 → {EMAILS['111111111111']}" in audit(ledger)

    def test_it_takes_the_email_from_the_same_form(self, admin, ledger):
        """用的是这次一起提交的账号邮箱，不是台账里原来那个。"""
        _edit(admin, by_account("111111111111"), email="acct-one-new@example.com", mail_password=MAIL_PASSWORD)
        assert by_account("111111111111").mail_address == "acct-one-new@example.com"

    def test_a_typed_address_wins(self, admin, ledger):
        post(admin, "/accounts/create", **NEW_FORM, **MAILBOX)
        assert by_account("333333333333").mail_address == MAILBOX["mail_address"]

    def test_no_password_means_no_mailbox(self, admin, ledger):
        """只是没填地址（平台下拉框总有个默认值）：不收信，平台也不存。"""
        post(admin, "/accounts/create", **NEW_FORM, mail_provider="aliyun-sg")
        created = by_account("333333333333")
        assert (created.mail_address, created.mail_provider, created.mail_enabled) == ("", "", False)

    def test_a_malformed_account_email_is_not_borrowed(self):
        """账号邮箱自己格式就不对：只报它自己的错，不拿去当告警邮箱。"""
        data, errors = excel_source.validate(
            {**NEW_FORM, "email": "not-an-email", "mail_password": MAIL_PASSWORD}, [], creating=True
        )
        assert errors == ["账号邮箱格式不对，应该形如 name@example.com。"]
        assert data["mail_address"] == ""

    def test_the_next_edit_needs_no_retyping(self, admin, ledger):
        """之后再打开弹窗，地址已经是账号邮箱了；密码留空 = 不改。"""
        _edit(admin, by_account("111111111111"), mail_password=MAIL_PASSWORD)
        dialog = _dialog(page(admin), 'id="dlg-edit-1"')
        assert input_attrs(dialog, "mail_address")["value"] == EMAILS["111111111111"]
        _edit(admin, by_account("111111111111"), budget="777")
        after = by_account("111111111111")
        assert after.budget == 777 and after.mail_password == MAIL_PASSWORD


class TestMailToggle:
    def with_mailbox(self, admin):
        _edit(admin, by_account("111111111111"), **MAILBOX)
        return by_account("111111111111").key

    def test_switches_on_and_off(self, admin, ledger):
        key = self.with_mailbox(admin)
        post(admin, "/accounts/mail-toggle", key=key, mail_enabled="1")
        assert by_account("111111111111").mail_active is True
        post(admin, "/accounts/mail-toggle", key=key, mail_enabled="0")
        assert by_account("111111111111").mail_enabled is False
        assert "开启账号 111111111111 的邮件告警" in audit(ledger)
        assert "关闭账号 111111111111 的邮件告警" in audit(ledger)

    def test_cannot_switch_on_without_a_mailbox(self, admin, ledger):
        post(admin, "/accounts/mail-toggle", key=by_account("111111111111").key, mail_enabled="1")
        assert by_account("111111111111").mail_enabled is False
        assert "先点「修改」填上再开" in page(admin)

    def test_saving_the_dialog_leaves_the_switch_alone(self, admin, ledger):
        """和 TG 开关同一条要紧的规矩：弹窗里没有开关，保存不能把它当成关。"""
        post(admin, "/accounts/mail-toggle", key=self.with_mailbox(admin), mail_enabled="1")
        _edit(admin, by_account("111111111111"), budget="777")
        assert by_account("111111111111").mail_enabled is True

    def test_switch_is_disabled_until_the_mailbox_is_filled(self, admin, ledger):
        cell = mail_cells(admin)[0]
        assert "disabled" in cell and 'title="还没有填告警邮箱，先点「修改」填上"' in cell
        self.with_mailbox(admin)
        cell = mail_cells(admin)[0]
        assert "disabled" not in cell
        assert "点一下开启（root-alpha@example.com · 阿里邮箱 · 国际站（新加坡））" in cell

    def test_no_text_beside_the_switch(self, admin, ledger):
        for cell in mail_cells(admin):
            assert re.sub(r"<[^>]+>", "", cell).strip() == ""

    def test_title_says_where_the_alerts_go(self, admin, ledger, monkeypatch):
        from bedrock_cost import config

        monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "123:FAKE")
        post(admin, "/accounts/mail-toggle", key=self.with_mailbox(admin), mail_enabled="1")
        assert "也没配固定群，收到了也发不出去" in mail_cells(admin)[0]
        monkeypatch.setattr(config, "MAIL_ALERT_CHAT_IDS", ("-1009999999999",))
        assert "邮件告警会发到固定群" in mail_cells(admin)[0]
        monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "")
        assert "服务器没配 TELEGRAM_BOT_TOKEN，收到了也发不出去" in mail_cells(admin)[0]

    def test_page_warns_once_when_the_token_is_missing(self, admin, ledger):
        post(admin, "/accounts/mail-toggle", key=self.with_mailbox(admin), mail_enabled="1")
        html = page(admin)
        assert html.count("开着的邮件告警收到了也发不出去") == 1
        assert any("开着的邮件告警收到了也发不出去" in note for note in warn_notes(html))

    def test_disabling_the_account_mentions_the_mailbox(self, admin, ledger):
        post(admin, "/accounts/mail-toggle", key=self.with_mailbox(admin), mail_enabled="1")
        dialog = _dialog(page(admin), 'id="dlg-off-1"')
        assert "不再收它的告警邮箱（root-alpha@example.com）" in dialog

    def test_rows_say_the_switch_is_on_for_the_filters(self, admin, ledger):
        """表格的「告警」筛选靠每一行的 data-alert。"""
        post(admin, "/accounts/mail-toggle", key=self.with_mailbox(admin), mail_enabled="1")
        (first, _), (second, _) = table_rows(page(admin))
        assert (first["data-alert"], second["data-alert"]) == ("mail", "")

    def test_needs_csrf(self, admin, ledger):
        key = self.with_mailbox(admin)
        admin.post("/accounts/mail-toggle", data={"key": key, "mail_enabled": "1"})
        assert by_account("111111111111").mail_enabled is False

    def test_needs_login(self, client, ledger):
        response = client.post("/accounts/mail-toggle", data={"key": "x", "mail_enabled": "1"})
        assert response.status_code == 302 and "/login" in response.headers["Location"]


class TestMailTestButton:
    """「测试连接」：用此刻填着的邮箱登录一下，只读，不写台账。页面上走 fetch，回 JSON。"""

    @pytest.fixture
    def server(self, monkeypatch):
        from bedrock_cost import mail_inbox

        from .test_mail import FakeIMAP

        fake = FakeIMAP(password=MAIL_PASSWORD)
        monkeypatch.setattr(mail_inbox, "_open", fake)
        return fake

    def probe(self, admin, **fields):
        return admin.post(
            "/accounts/mail-test", data={"csrf": token(admin), **fields},
            headers={"X-Requested-With": "fetch"},
        )

    @staticmethod
    def logins(server) -> list[str]:
        return [command[1] for command in server.commands if command[0] == "LOGIN"]

    def test_logs_in_with_the_typed_values_without_saving(self, admin, ledger, server):
        from . import mail_samples

        server.add(1, mail_samples.abuse())
        before = ledger.stat().st_mtime_ns
        result = self.probe(admin, key=by_account("111111111111").key, **MAILBOX).get_json()
        assert result["ok"] is True and "收件箱里有 1 封邮件" in result["message"]
        assert "有 1 封符合告警规则" in result["message"]
        assert (server.host, server.port) == ("imap.sg.aliyun.com", 993)
        assert self.logins(server) == ["root-alpha@example.com"]
        assert ledger.stat().st_mtime_ns == before          # 一个字节都没写

    def test_a_blank_password_uses_the_saved_one(self, admin, ledger, server):
        _edit(admin, by_account("111111111111"), **MAILBOX)
        result = self.probe(admin, key=by_account("111111111111").key, **{**MAILBOX, "mail_password": ""})
        assert result.get_json()["ok"] is True

    def test_the_saved_password_is_not_reused_for_another_address(self, admin, ledger, server):
        _edit(admin, by_account("111111111111"), **MAILBOX)
        result = self.probe(admin, key=by_account("111111111111").key,
                            **{**MAILBOX, "mail_address": "other@example.com", "mail_password": ""}).get_json()
        assert result == {"ok": False, "message": "先填密码再测。"}
        assert server.connections == 0

    def test_explains_a_failure(self, admin, ledger, server):
        result = self.probe(admin, **{**MAILBOX, "mail_password": "wrong-one"}).get_json()
        assert result["ok"] is False
        assert "登录被拒" in result["message"] and "wrong-one" not in result["message"]

    def test_asks_for_an_address_or_the_account_email_first(self, admin, ledger, server):
        result = self.probe(admin, mail_provider="aliyun-sg", mail_password=MAIL_PASSWORD).get_json()
        assert result == {"ok": False, "message": "先填邮箱地址（或账号邮箱）再测。"}
        assert server.connections == 0

    def test_a_blank_address_falls_back_to_the_account_email(self, admin, ledger, server):
        """和保存时同一条规矩：地址留空就是收账号邮箱本身，测的也是弹窗里填着的账号邮箱。"""
        result = self.probe(admin, email="root-alpha@example.com", mail_provider="aliyun-sg",
                            mail_address="", mail_password=MAIL_PASSWORD).get_json()
        assert result["ok"] is True
        assert self.logins(server) == ["root-alpha@example.com"]

    def test_a_typed_address_wins_over_the_account_email(self, admin, ledger, server):
        self.probe(admin, email="acct-three@example.com", **MAILBOX)
        assert self.logins(server) == ["root-alpha@example.com"]

    def test_the_saved_password_goes_with_the_account_email(self, admin, ledger, server):
        """保存时地址留空（于是收账号邮箱），测试时地址、密码都留空：用存着的密码登账号邮箱。"""
        _edit(admin, by_account("111111111111"), mail_password=MAIL_PASSWORD)
        target = by_account("111111111111")
        result = self.probe(admin, key=target.key, email=target.email, mail_provider="aliyun-sg",
                            mail_address="", mail_password="").get_json()
        assert result["ok"] is True
        assert self.logins(server) == [EMAILS["111111111111"]]

    def test_a_stale_key_is_reported(self, admin, ledger, server):
        result = self.probe(admin, key="999999999999#9", **MAILBOX).get_json()
        assert result["ok"] is False and "已经不在台账里了" in result["message"]
        assert server.connections == 0

    def test_works_without_javascript_too(self, admin, ledger, server):
        """没开 JS：普通提交，整页刷新、重新打开那个弹窗，结果在里面；密码不回填。"""
        response = admin.post("/accounts/mail-test", data={"csrf": token(admin), **NEW_FORM, **MAILBOX})
        html = response.get_data(as_text=True)
        assert response.status_code == 200
        assert 'id="dlg-create" data-reopen' in html
        dialog = _dialog(html, 'id="dlg-create"')
        shown, message = mail_result(dialog)
        assert shown and "连上了" in message
        assert input_attrs(dialog, "mail_address")["value"] == "root-alpha@example.com"
        assert input_attrs(dialog, "email")["value"] == NEW_FORM["email"]
        assert MAIL_PASSWORD not in html

    def test_the_result_box_is_hidden_until_there_is_a_result(self, admin, ledger):
        shown, message = mail_result(_dialog(page(admin), 'id="dlg-edit-1"'))
        assert not shown and message == ""

    def test_needs_csrf(self, admin, ledger, server):
        response = admin.post("/accounts/mail-test", data=MAILBOX, headers={"X-Requested-With": "fetch"})
        assert response.status_code == 302
        assert server.connections == 0


class TestMailNotices:
    """开 / 关邮件告警都给账号的群发一张卡片，跟 TG 开关走。

    告警邮箱写全、不打码：号码认不出是谁，群里靠邮箱认是哪个账号在收信（见 alerts.mail_on_card）。"""

    CHAT = "-1001234567890"

    @pytest.fixture
    def cards_sent(self, monkeypatch):
        from bedrock_cost import alerts, config

        box = []
        monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "123:FAKE")
        monkeypatch.setattr(alerts, "_send", lambda chat, card: box.append((chat, card)) or "")
        return box

    def ready(self, admin, tg=True):
        """填好群和邮箱；tg=True 再把 TG 开关打开。"""
        _edit(admin, by_account("111111111111"), tg_chat_ids=self.CHAT, **MAILBOX)
        key = by_account("111111111111").key
        if tg:
            post(admin, "/accounts/tg-toggle", key=key, tg_enabled="1")
        return key

    def test_switching_on_is_announced(self, admin, ledger, cards_sent):
        post(admin, "/accounts/mail-toggle", key=self.ready(admin), mail_enabled="1")
        (chat, card), = cards_sent
        assert (chat, card.kind, card.title) == (self.CHAT, "mail-on", "邮件告警已开启")
        text_ = card.text()
        assert "root-alpha@example.com" in text_ and "r***@example.com" not in text_   # 写全，不打码
        assert "阿里邮箱 · 国际站（新加坡）" in text_ and "111111111111" in text_
        assert MAIL_PASSWORD not in text_
        assert "已通知这个账号的 1 个 TG 群" in page(admin)

    def test_switching_off_is_announced(self, admin, ledger, cards_sent):
        key = self.ready(admin)
        post(admin, "/accounts/mail-toggle", key=key, mail_enabled="1")
        post(admin, "/accounts/mail-toggle", key=key, mail_enabled="0")
        assert [card.kind for _, card in cards_sent] == ["mail-on", "mail-off"]
        assert cards_sent[1][1].title == "邮件告警已关闭"

    def test_clearing_the_mailbox_announces_it_with_the_old_address(self, admin, ledger, cards_sent):
        key = self.ready(admin)
        post(admin, "/accounts/mail-toggle", key=key, mail_enabled="1")
        _edit(admin, by_account("111111111111"), mail_address="", mail_password="")
        assert [card.kind for _, card in cards_sent] == ["mail-on", "mail-off"]
        # 台账里已经清空了，卡片上要写的是被关掉的那个邮箱
        assert by_account("111111111111").mail_address == ""
        assert "root-alpha@example.com" in cards_sent[1][1].text()

    def test_other_edits_send_nothing(self, admin, ledger, cards_sent):
        key = self.ready(admin)
        post(admin, "/accounts/mail-toggle", key=key, mail_enabled="1")
        _edit(admin, by_account("111111111111"), budget="777")
        assert [card.kind for _, card in cards_sent] == ["mail-on"]

    def test_follows_the_tg_switch(self, admin, ledger, cards_sent):
        post(admin, "/accounts/mail-toggle", key=self.ready(admin, tg=False), mail_enabled="1")
        assert cards_sent == []

    def test_nothing_changed_nothing_sent(self, admin, ledger, cards_sent):
        key = self.ready(admin)
        post(admin, "/accounts/mail-toggle", key=key, mail_enabled="0")    # 本来就是关的
        assert cards_sent == []

    def test_a_new_account_card_says_mail_alerts_are_on(self, admin, ledger, cards_sent):
        """新增时填了邮箱就开了邮件告警：不另发一张，写在「新账号启用」那张上。"""
        post(admin, "/accounts/create", **NEW_FORM, tg_chat_ids=self.CHAT, **MAILBOX)
        (chat, card), = cards_sent
        rows = {row.label: row.value for row in card.blocks[1].rows}
        assert card.kind == "created" and rows["邮件告警"] == "已开启"

    def test_disabling_says_mail_alerts_stop(self, admin, ledger, cards_sent, fake_costs):
        key = self.ready(admin)
        post(admin, "/accounts/mail-toggle", key=key, mail_enabled="1")
        post(admin, "/accounts/toggle", key=key, enabled="0")
        rows = {row.label: row.value for row in cards_sent[-1][1].blocks[1].rows}
        assert cards_sent[-1][1].kind == "disabled" and rows["邮件告警"] == "已停止"

    def test_no_mail_row_without_mail_alerts(self, admin, ledger, cards_sent):
        post(admin, "/accounts/create", **NEW_FORM, tg_chat_ids=self.CHAT)
        rows = {row.label for row in cards_sent[0][1].blocks[1].rows}
        assert "邮件告警" not in rows
