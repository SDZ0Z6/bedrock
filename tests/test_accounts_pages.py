"""新增 / 修改弹窗分三页：基础信息 / 告警邮箱（可选）/ Telegram 告警（可选）。

三页在同一个 <form> 里，切页只是换一页显示（前端的事）。服务端要管的是：字段各在各的页、
打开时停在哪一页——校验没过就停在第一条错误所在的那页，刚测试过就停在测试的那页。

「告警邮箱」「Telegram 告警」两页的版式和提示文字会改：这里只认字段名、formaction、
data-* 钩子，不认外面怎么包、提示怎么写。
"""

from __future__ import annotations

import re

import pytest

from bedrock_cost import excel_source

from .test_accounts import (  # noqa: F401
    NEW_FORM,
    _dialog,
    _edit,
    admin,
    by_account,
    input_attrs,
    page,
    post,
    reopened_dialogs,
    token,
)

TAB = re.compile(r'<button class="modal-tab"[^>]*data-tab="(\w+)"[^>]*aria-selected="(true|false)"[^>]*>(.*?)</button>', re.S)
# 每一页的开头。一页的内容从这里一直到下一页的开头（最后一页到弹窗末尾）——不靠 </section>
# 去截，页里面再套 <section> 也不会截错
PAGE_START = re.compile(r'<section\b([^>]*\bdata-page="(\w+)"[^>]*)>')


def tabs(dialog: str) -> list[tuple[str, bool, str]]:
    """[(页, 是否选中, 页签上的字)]，按页签顺序。"""
    return [(key, selected == "true", " ".join(re.sub(r"<[^>]+>", " ", label).split()))
            for key, selected, label in TAB.findall(dialog)]


def pages(dialog: str) -> dict[str, tuple[bool, str]]:
    """{页: (是否显示, 这一页的 HTML)}。"""
    starts = list(PAGE_START.finditer(dialog))
    found = {}
    for index, match in enumerate(starts):
        end = starts[index + 1].start() if index + 1 < len(starts) else len(dialog)
        classes = re.search(r'class="([^"]*)"', match.group(1))
        active = bool(classes) and "is-active" in classes.group(1).split()
        found[match.group(2)] = (active, dialog[match.end() : end])
    return found


def active_page(dialog: str) -> str:
    shown = [key for key, (active, _) in pages(dialog).items() if active]
    selected = [key for key, on, _ in tabs(dialog) if on]
    assert shown == selected and len(shown) == 1, (shown, selected)
    return shown[0]


def error_tabs(dialog: str) -> set[str]:
    return {key for key, _, label in TAB.findall(dialog) if 'class="tab-error"' in label}


class TestPages:
    @pytest.mark.parametrize("marker", ['id="dlg-create"', 'id="dlg-edit-1"'])
    def test_three_pages_in_order(self, admin, ledger, marker):
        dialog = _dialog(page(admin), marker)
        found = tabs(dialog)
        assert [(key, on) for key, on, _ in found] == [("basic", True), ("mail", False), ("tg", False)]
        labels = [label for _, _, label in found]
        assert labels[0].startswith("基础信息") and labels[1].startswith("告警邮箱") and labels[2].startswith("Telegram")
        assert list(pages(dialog)) == ["basic", "mail", "tg"]

    @pytest.mark.parametrize("marker", ['id="dlg-create"', 'id="dlg-edit-1"'])
    def test_every_tab_starts_with_an_icon(self, admin, ledger, marker):
        """页签名字前面一个线条小图标：基础信息是证件，告警邮箱是信封，Telegram 是纸飞机。"""
        dialog = _dialog(page(admin), marker)
        labels = [label for _, _, label in TAB.findall(dialog)]
        assert len(labels) == 3
        assert all(label.lstrip().startswith('<svg class="icon"') for label in labels)

    @pytest.mark.parametrize("marker", ['id="dlg-create"', 'id="dlg-edit-1"'])
    def test_the_alert_pages_open_on_their_fields(self, admin, ledger, marker):
        """告警邮箱、Telegram 告警两页没有顶部那段说明，一打开就是要填的东西。"""
        found = pages(_dialog(page(admin), marker))
        for key, first in (("mail", 'name="mail_provider"'), ("tg", 'name="tg_chat_ids"')):
            body = found[key][1]
            assert "setting-intro" not in body
            assert "<p" not in body[: body.index(first)]

    @pytest.mark.parametrize("marker", ['id="dlg-create"', 'id="dlg-edit-1"'])
    def test_the_email_comes_before_the_number(self, admin, ledger, marker):
        """和各页面一样，邮箱在前、号码在后；打开弹窗时光标落在邮箱上。"""
        basic = pages(_dialog(page(admin), marker))["basic"][1]
        assert basic.index('name="email"') < basic.index('name="account"')
        assert "querySelector('.tab-page.is-active [name=\"email\"]')" in page(admin)

    def test_enter_saves_instead_of_testing_the_mailbox(self, admin, ledger):
        """表单里第一个提交按钮是「告警邮箱」页里的「测试连接」：浏览器默认的回车会去点它。
        页面脚本接管回车，改成点底部的主按钮（保存 / 添加账号）。"""
        html = page(admin)
        assert "event.isComposing" in html                      # 输入法选字时的回车不算
        assert "requestSubmit(main)" in html and "'.modal-foot [type=\"submit\"]'" in html

    @pytest.mark.parametrize("marker", ['id="dlg-create"', 'id="dlg-edit-1"'])
    def test_every_field_is_on_its_own_page(self, admin, ledger, marker):
        found = pages(_dialog(page(admin), marker))
        wanted = {
            "basic": ("avatar_emoji", "avatar_color", "account", "email", "partner", "budget", "start_date",
                      "tag_ratio", "untag_ratio", "tag_spec", "lifecycle"),
            "mail": ("mail_provider", "mail_address", "mail_password", "mail_server"),
            "tg": ("tg_chat_ids",),
        }
        for page_key, names in wanted.items():
            for key, (_, body) in found.items():
                for name in names:
                    assert (f'name="{name}"' in body) is (key == page_key), (name, key)

    def test_credentials_are_on_the_first_page_of_the_new_account(self, admin, ledger):
        basic = pages(_dialog(page(admin), 'id="dlg-create"'))["basic"][1]
        assert 'name="ak"' in basic and 'name="sk"' in basic
        assert input_attrs(basic, "sk")["type"] == "password"

    def test_the_ak_note_is_on_the_first_page_of_the_edit_dialog(self, admin, ledger):
        found = pages(_dialog(page(admin), 'id="dlg-edit-1"'))
        assert "AKIAFAKE…0000" in found["basic"][1] and "不可修改" in found["basic"][1]

    def test_the_first_page_has_the_avatar_picker_and_the_lifecycle_ticks(self, admin, ledger):
        from bedrock_cost.accounts import AVATAR_EMOJIS

        basic = pages(_dialog(page(admin), 'id="dlg-edit-1"'))["basic"][1]
        for hook in ("data-avatar-pick", "data-avatar-preview", "data-avatar-emoji"):
            assert hook in basic
        assert re.findall(r'data-emoji="([^"]+)"', basic) == list(AVATAR_EMOJIS)   # 点一下就填进去的几个表情
        assert len(re.findall(r'name="avatar_color"', basic)) == 1 + 8      # 「自动」+ 八组底色
        assert re.findall(r'name="lifecycle" value="([^"]+)"', basic) == ["正常", "结算", "风控"]

    def test_test_buttons_live_on_their_pages(self, admin, ledger):
        found = pages(_dialog(page(admin), 'id="dlg-edit-1"'))
        assert 'formaction="/accounts/mail-test"' in found["mail"][1]
        assert 'formaction="/accounts/tg-test"' in found["tg"][1]
        assert 'formaction="/accounts/mail-test"' not in found["tg"][1] + found["basic"][1]
        assert 'formaction="/accounts/tg-test"' not in found["mail"][1] + found["basic"][1]

    def test_the_pages_keep_their_script_hooks(self, admin, ledger):
        """弹窗里的脚本靠这些 data-* 找东西：换平台显示服务器框、加 / 删群组行、测试结果贴在哪。"""
        found = pages(_dialog(page(admin), 'id="dlg-edit-1"'))
        for hook in ("data-mail-block", "data-mail-provider", "data-mail-hint", "data-mail-server",
                     "data-mail-test", "data-mail-result"):
            assert hook in found["mail"][1], hook
        for hook in ("data-chat-rows", "data-add-chat", "data-remove-chat"):
            assert hook in found["tg"][1], hook
        assert re.search(r'class="chat-row\b', found["tg"][1])

    def test_every_dialog_starts_on_the_first_page(self, admin, ledger):
        html = page(admin)
        for marker in ('id="dlg-create"', 'id="dlg-edit-1"', 'id="dlg-edit-2"'):
            assert active_page(_dialog(html, marker)) == "basic"
        assert 'class="tab-error"' not in html

    def test_ids_do_not_collide_between_dialogs(self, admin, ledger):
        html = page(admin)
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

    def test_a_missing_email_opens_the_first_page(self, admin, ledger):
        """账号邮箱在第一页：它和告警邮箱一起错时，先停在第一页。"""
        response = _edit(admin, by_account("111111111111"), email="", mail_address="bad-address")
        dialog = _dialog(response.get_data(as_text=True), 'id="dlg-edit-1"')
        assert active_page(dialog) == "basic"
        assert error_tabs(dialog) == {"basic", "mail"}

    def test_the_first_error_wins_and_every_page_with_one_is_marked(self, admin, ledger):
        response = post(admin, "/accounts/create", **{**NEW_FORM, "budget": "abc"},
                        mail_address="bad-address", tg_chat_ids="abc")
        dialog = _dialog(response.get_data(as_text=True), 'id="dlg-create"')
        assert active_page(dialog) == "basic"
        assert error_tabs(dialog) == {"basic", "mail", "tg"}

    def test_other_dialogs_are_left_alone(self, admin, ledger):
        html = _edit(admin, by_account("111111111111"), tg_chat_ids="abc").get_data(as_text=True)
        assert reopened_dialogs(html) == ["dlg-edit-1"]
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
        _, errors = excel_source.validate(form, [], creating=True, lifecycle=excel_source.load_lifecycle())
        return {str(error): getattr(error, "page", "basic") for error in errors}

    def test_basic_errors_are_plain_strings(self, ledger):
        errors = self.run(budget="abc")
        assert errors == {"额度要填数字。": "basic"}

    def test_mail_and_tg_errors_know_their_page(self, ledger):
        errors = self.run(mail_address="bad-address", tg_chat_ids="abc")
        assert set(errors.values()) == {"mail", "tg"}
        assert all(page_key == "mail" for text_, page_key in errors.items() if "邮箱" in text_)

    def test_email_lifecycle_and_avatar_errors_are_on_the_first_page(self, ledger):
        errors = self.run(email="not-an-email", lifecycle="外星人", avatar_color="9")
        assert errors == {
            "账号邮箱格式不对，应该形如 name@example.com。": "basic",
            "生命周期里没有「外星人」，先在标签清单里加上。": "basic",
            "头像底色不对，请从色块里选。": "basic",
        }

    def test_form_errors_still_compare_as_text(self):
        error = excel_source.FormError("换了邮箱地址，要重新填这个邮箱的密码。", "mail")
        assert error == "换了邮箱地址，要重新填这个邮箱的密码。" and error.page == "mail"
