"""新增 / 修改弹窗分三页：基础信息 / 告警邮箱（可选）/ Telegram 告警（可选）。

三页在同一个 <form> 里，切页只是换一页显示（前端的事）。服务端要管的是：字段各在各的页、
打开时停在哪一页——校验没过就停在第一条错误所在的那页，刚测试过就停在测试的那页。
"""

from __future__ import annotations

import re

import pytest

from bedrock_cost import excel_source

from .test_accounts import NEW_FORM, _dialog, _edit, admin, by_account, post, token  # noqa: F401

TAB = re.compile(r'<button class="modal-tab"[^>]*data-tab="(\w+)"[^>]*aria-selected="(true|false)"[^>]*>(.*?)</button>', re.S)
PAGE = re.compile(r'<section class="tab-page( is-active)?"[^>]*data-page="(\w+)"[^>]*>(.*?)</section>', re.S)


def tabs(dialog: str) -> list[tuple[str, bool, str]]:
    """[(页, 是否选中, 页签上的字)]，按页签顺序。"""
    return [(key, selected == "true", re.sub(r"<[^>]+>", " ", label).split()) for key, selected, label in TAB.findall(dialog)]


def pages(dialog: str) -> dict[str, tuple[bool, str]]:
    """{页: (是否显示, 这一页的 HTML)}。"""
    return {key: (bool(active), body) for active, key, body in PAGE.findall(dialog)}


def active_page(dialog: str) -> str:
    shown = [key for key, (active, _) in pages(dialog).items() if active]
    selected = [key for key, on, _ in tabs(dialog) if on]
    assert shown == selected and len(shown) == 1, (shown, selected)
    return shown[0]


def error_tabs(dialog: str) -> set[str]:
    return {key for key, _, label in TAB.findall(dialog) if 'class="tab-error"' in label}


def page_html(admin) -> str:
    return admin.get("/accounts/").get_data(as_text=True)


class TestPages:
    @pytest.mark.parametrize("marker", ['id="dlg-create"', 'id="dlg-edit-1"'])
    def test_three_pages_in_order(self, admin, ledger, marker):
        dialog = _dialog(page_html(admin), marker)
        assert tabs(dialog) == [
            ("basic", True, ["基础信息"]),
            ("mail", False, ["告警邮箱", "可选"]),
            ("tg", False, ["Telegram", "告警", "可选"]),
        ]

    @pytest.mark.parametrize("marker", ['id="dlg-create"', 'id="dlg-edit-1"'])
    def test_every_field_is_on_its_own_page(self, admin, ledger, marker):
        found = pages(_dialog(page_html(admin), marker))
        wanted = {
            "basic": ("partner", "account", "budget", "start_date", "tag_ratio", "untag_ratio", "tag_spec"),
            "mail": ("mail_provider", "mail_address", "mail_password", "mail_server"),
            "tg": ("tg_chat_ids",),
        }
        for page, names in wanted.items():
            for key, (_, body) in found.items():
                for name in names:
                    assert (f'name="{name}"' in body) is (key == page), (name, key)

    def test_credentials_are_on_the_first_page_of_the_new_account(self, admin, ledger):
        basic = pages(_dialog(page_html(admin), 'id="dlg-create"'))["basic"][1]
        assert 'name="ak"' in basic and 'name="sk"' in basic

    def test_the_ak_note_is_on_the_first_page_of_the_edit_dialog(self, admin, ledger):
        found = pages(_dialog(page_html(admin), 'id="dlg-edit-1"'))
        assert "AKIAFAKE…0000" in found["basic"][1] and "不可修改" in found["basic"][1]

    def test_test_buttons_live_on_their_pages(self, admin, ledger):
        found = pages(_dialog(page_html(admin), 'id="dlg-edit-1"'))
        assert "测试连接" in found["mail"][1] and "发测试消息" in found["tg"][1]
        assert "测试连接" not in found["tg"][1] and "发测试消息" not in found["mail"][1]

    def test_each_page_says_where_its_switch_is(self, admin, ledger):
        found = pages(_dialog(page_html(admin), 'id="dlg-edit-1"'))
        assert "开关在表格的「邮件告警」列" in found["mail"][1]
        assert "开关在表格的「TG 告警」列" in found["tg"][1]

    def test_every_dialog_starts_on_the_first_page(self, admin, ledger):
        html = page_html(admin)
        for marker in ('id="dlg-create"', 'id="dlg-edit-1"', 'id="dlg-edit-2"'):
            assert active_page(_dialog(html, marker)) == "basic"
        assert 'class="tab-error"' not in html

    def test_ids_do_not_collide_between_dialogs(self, admin, ledger):
        html = page_html(admin)
        ids = re.findall(r'\sid="([^"]+)"', html)
        assert len(ids) == len(set(ids))


class TestReopenedPage:
    """校验没过、或者刚测过：重新打开的弹窗停在对的那一页。"""

    def test_a_mail_error_opens_the_mail_page(self, admin, ledger):
        response = _edit(admin, by_account("111111111111"), mail_provider="aliyun-sg", mail_address="bad-address")
        dialog = _dialog(response.get_data(as_text=True), 'id="dlg-edit-1"')
        assert active_page(dialog) == "mail"
        assert error_tabs(dialog) == {"mail"}
        assert "告警邮箱的地址格式不对" in dialog                 # 错误清单在页签上面，哪一页都看得到

    def test_a_tg_error_opens_the_tg_page(self, admin, ledger):
        dialog = _dialog(_edit(admin, by_account("111111111111"), tg_chat_ids="abc").get_data(as_text=True),
                         'id="dlg-edit-1"')
        assert active_page(dialog) == "tg" and error_tabs(dialog) == {"tg"}

    def test_the_first_error_wins_and_every_page_with_one_is_marked(self, admin, ledger):
        response = post(admin, "/accounts/create", **{**NEW_FORM, "budget": "abc"},
                        mail_address="bad-address", tg_chat_ids="abc")
        dialog = _dialog(response.get_data(as_text=True), 'id="dlg-create"')
        assert active_page(dialog) == "basic"
        assert error_tabs(dialog) == {"basic", "mail", "tg"}

    def test_other_dialogs_are_left_alone(self, admin, ledger):
        html = _edit(admin, by_account("111111111111"), tg_chat_ids="abc").get_data(as_text=True)
        assert active_page(_dialog(html, 'id="dlg-edit-2"')) == "basic"
        assert active_page(_dialog(html, 'id="dlg-create"')) == "basic"
        assert error_tabs(_dialog(html, 'id="dlg-create"')) == set()

    def test_the_tg_test_comes_back_to_the_tg_page(self, admin, ledger):
        target = by_account("111111111111")
        html = post(admin, "/accounts/tg-test", key=target.key, tg_chat_ids="-1001234567890").get_data(as_text=True)
        dialog = _dialog(html, 'id="dlg-edit-1"')
        assert "data-reopen" in dialog and active_page(dialog) == "tg"

    def test_the_mail_test_without_javascript_comes_back_to_the_mail_page(self, admin, ledger, monkeypatch):
        from bedrock_cost import mail_inbox

        from .test_mail import FakeIMAP

        monkeypatch.setattr(mail_inbox, "_open", FakeIMAP(password="pw-123456"))
        html = admin.post("/accounts/mail-test", data={
            "csrf": token(admin), **NEW_FORM,
            "mail_provider": "aliyun-sg", "mail_address": "root@example.com", "mail_password": "pw-123456",
        }).get_data(as_text=True)
        dialog = _dialog(html, 'id="dlg-create"')
        assert active_page(dialog) == "mail" and "连上了" in dialog


class TestErrorPages:
    """validate() 给每条错误标上它属于哪一页。"""

    def run(self, **fields):
        form = {**NEW_FORM, **fields}
        _, errors = excel_source.validate(form, [], creating=True)
        return {str(error): getattr(error, "page", "basic") for error in errors}

    def test_basic_errors_are_plain_strings(self):
        errors = self.run(budget="abc")
        assert errors == {"额度要填数字。": "basic"}

    def test_mail_and_tg_errors_know_their_page(self):
        errors = self.run(mail_address="bad-address", tg_chat_ids="abc")
        assert set(errors.values()) == {"mail", "tg"}
        assert all(page == "mail" for text, page in errors.items() if "邮箱" in text)

    def test_form_errors_still_compare_as_text(self):
        error = excel_source.FormError("换了邮箱地址，要重新填这个邮箱的密码。", "mail")
        assert error == "换了邮箱地址，要重新填这个邮箱的密码。" and error.page == "mail"
