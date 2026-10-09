"""账号头像：默认是账号邮箱的首字母，台账里填了表情（AVATAR 列）就用表情；底色（AVATAR_COLOR，
0~7 选一组）没选就按邮箱算——同一个账号在概览、下拉框、账号页里永远是同一个颜色。

头像那一格里多打了字时的规矩正在改（只留第一个字形，不再报错），所以这里只用「一个表情」，
不去碰多打了字的情况。
"""

from __future__ import annotations

import re
import zlib

import pytest

from bedrock_cost import avatars, excel_source
from bedrock_cost.avatars import Avatar, avatar_for, tone_for

from .test_accounts import (  # noqa: F401
    EMAILS,
    NEW_FORM,
    _dialog,
    _edit,
    admin,
    by_account,
    input_attrs,
    inputs,
    page,
    post,
    raw_rows,
    reopened_dialogs,
    rewrite_ledger,
    table_rows,
    text,
)

ONE = EMAILS["111111111111"]
TWO = EMAILS["222222222222"]


def cells(ledger, column: str) -> list:
    """台账里某一列的原值，按行。"""
    header, *rows = raw_rows(ledger)
    return [row[header.index(column)] for row in rows]


def preview(dialog: str) -> tuple[set[str], str]:
    """弹窗里头像预览的初始样子：(类名, 字)。之后跟着输入变是页面脚本的事。"""
    found = re.search(r"<span\b([^>]*\bdata-avatar-preview\b[^>]*)>(.*?)</span>\s*</span>", dialog, re.S)
    assert found, "弹窗里没有头像预览"
    classes = re.search(r'class="([^"]*)"', found.group(1)).group(1)
    return set(classes.split()), text(found.group(2))


def table_avatar(html: str, row: int = 0) -> tuple[set[str], str]:
    """表格里那一行账号格里的小头像：(类名, 字)。"""
    _, row_cells = table_rows(html)[row]
    found = re.search(r'<span class="([^"]*\bavatar\b[^"]*)"[^>]*><span>(.*?)</span></span>', row_cells[0], re.S)
    assert found, "账号格里没有头像"
    return set(found.group(1).split()), found.group(2)


def chosen_color(dialog: str) -> list[str]:
    return [field["value"] for field in inputs(dialog, "avatar_color") if field.get("checked")]


# ------------------------------------------------------------------ avatars.py
class TestAvatarFor:
    def test_the_letter_comes_from_the_email(self):
        assert avatar_for(ONE, "111111111111", "ALPHA") == Avatar("A", tone_for(ONE), False)

    def test_the_letter_is_upper_case(self):
        assert avatar_for("zed@example.com", "111111111111").text == "Z"

    def test_without_an_email_the_partner_gives_the_letter_and_the_number_the_color(self):
        """号码的首位是个数字，认不出是谁，所以字取上游的首字；颜色仍按号码（每个账号各不相同）。"""
        assert avatar_for("", "111111111111", "beta") == Avatar("B", tone_for("111111111111"), False)

    def test_only_a_number(self):
        assert avatar_for("", "222222222222") == Avatar("2", tone_for("222222222222"), False)

    def test_nothing_at_all(self):
        assert avatar_for("", "") == Avatar("?", tone_for("?"), False)

    @pytest.mark.parametrize("emoji", ["🦊", "👍🏽"])
    def test_an_emoji_wins_over_the_letter(self, emoji):
        assert avatar_for(ONE, "111111111111", "ALPHA", emoji) == Avatar(emoji, tone_for(ONE), True)

    def test_a_chosen_color_wins(self):
        assert avatar_for(ONE, "111111111111", color=5).tone == 5
        assert avatar_for(ONE, "111111111111", emoji="🐳", color=3) == Avatar("🐳", 3, True)

    def test_color_zero_is_a_choice_too(self):
        assert avatar_for(ONE, "111111111111", color=0).tone == 0

    @pytest.mark.parametrize("color", [8, -1])
    def test_an_impossible_color_is_ignored(self, color):
        assert avatar_for(ONE, "111111111111", color=color).tone == tone_for(ONE)

    def test_css(self):
        assert Avatar("A", 3, False).css == "av-3"
        assert Avatar("🦊", 0, True).css == "av-0 avatar-emoji"


class TestToneFor:
    def test_stable_and_ignores_case_and_spaces(self):
        """同一个邮箱在哪个页面上都是同一个颜色：只看内容，不看大小写和前后空格。"""
        expected = zlib.crc32(ONE.encode("utf-8")) % avatars.TONES
        assert tone_for(ONE) == tone_for(" ACCT-One@Example.com ") == expected

    def test_spreads_over_every_tone(self):
        assert {tone_for(f"user{n}@example.com") for n in range(200)} == set(range(avatars.TONES))


# ------------------------------------------------------------------ 台账里的两列
class TestLedgerColumns:
    def test_an_old_ledger_uses_letters_and_computed_colors(self, accounts):
        first, second = accounts
        assert (first.avatar_emoji, first.avatar_color) == ("", None)
        assert first.avatar == Avatar("A", tone_for("111111111111"), False)        # 上游 ALPHA 的首字
        assert second.avatar == Avatar("B", tone_for("222222222222"), False)

    def test_the_columns_are_read(self, ledger):
        rewrite_ledger(ledger, EMAIL=[ONE, TWO], AVATAR=[" 🦊 ", None], AVATAR_COLOR=[3, None])
        first, second = excel_source.load_accounts(force=True)
        assert (first.avatar_emoji, first.avatar_color) == ("🦊", 3)
        assert first.avatar == Avatar("🦊", 3, True)
        assert second.avatar == Avatar("A", tone_for(TWO), False)                  # 邮箱首字母，按邮箱配色

    @pytest.mark.parametrize(
        "raw, expected",
        [
            (None, None),
            ("", None),
            ("0", 0),
            ("7", 7),
            (7, 7),
            (3.0, 3),            # Excel 把数字存成浮点
            ("3.0", 3),
            (" 5 ", 5),
            ("8", None),         # 只有八组
            ("-1", None),
            ("1.5", None),
            ("abc", None),
        ],
    )
    def test_color_parsing(self, raw, expected):
        """空着、写错都当没选（按邮箱自动配色）。"""
        assert excel_source._to_avatar_color(raw) == expected

    def test_an_odd_digit_in_the_ledger_counts_as_unset(self, ledger):
        rewrite_ledger(ledger, AVATAR_COLOR=["²", None])
        first, _ = excel_source.load_accounts(force=True)
        assert first.avatar_color is None


# ------------------------------------------------------------------ 表单校验
class TestValidation:
    def run(self, **fields):
        return excel_source.validate({**NEW_FORM, **fields}, [], creating=True)

    @pytest.mark.parametrize("emoji", ["🦊", "👍🏽", " 🐳 "])
    def test_one_emoji_is_stored(self, emoji):
        data, errors = self.run(avatar_emoji=emoji)
        assert errors == [] and data["avatar_emoji"] == emoji.strip()

    def test_no_emoji_is_fine(self):
        data, errors = self.run(avatar_emoji="")
        assert errors == [] and data["avatar_emoji"] == ""

    @pytest.mark.parametrize("raw, expected", [("", None), ("0", 0), ("7", 7), (" 3 ", 3)])
    def test_colors(self, raw, expected):
        data, errors = self.run(avatar_color=raw)
        assert errors == [] and data["avatar_color"] == expected

    @pytest.mark.parametrize("raw", ["8", "-1", "abc", "1.5"])
    def test_a_color_off_the_swatches_is_refused(self, raw):
        _, errors = self.run(avatar_color=raw)
        assert errors == ["头像底色不对，请从色块里选。"]

    def test_an_odd_digit_is_refused_not_a_crash(self):
        _, errors = self.run(avatar_color="²")
        assert errors == ["头像底色不对，请从色块里选。"]


# ------------------------------------------------------------------ 页面和保存
class TestOnThePage:
    def test_a_new_account_with_an_emoji_and_a_color(self, admin, ledger):
        response = post(admin, "/accounts/create", **NEW_FORM, avatar_emoji="🦊", avatar_color="3")
        assert response.status_code == 302
        created = by_account("333333333333")
        assert created.avatar == Avatar("🦊", 3, True)
        assert (cells(ledger, "AVATAR")[-1], cells(ledger, "AVATAR_COLOR")[-1]) == ("🦊", 3)
        classes, shown = table_avatar(page(admin), row=2)
        assert {"avatar", "av-3", "avatar-emoji", "avatar-sm"} <= classes and shown == "🦊"

    def test_a_new_account_without_either(self, admin, ledger):
        post(admin, "/accounts/create", **NEW_FORM)
        created = by_account("333333333333")
        assert created.avatar == Avatar("A", tone_for(NEW_FORM["email"]), False)   # acct-three 的 A
        assert created.avatar_color is None

    def test_the_table_uses_the_email_letter_by_default(self, admin, ledger):
        classes, shown = table_avatar(page(admin))
        assert shown == "A" and f"av-{tone_for(ONE)}" in classes and "avatar-emoji" not in classes

    def test_editing_and_clearing_it(self, admin, ledger):
        _edit(admin, by_account("111111111111"), avatar_emoji="🐳", avatar_color="0")
        assert by_account("111111111111").avatar == Avatar("🐳", 0, True)
        _edit(admin, by_account("111111111111"), avatar_emoji="", avatar_color="")
        after = by_account("111111111111")
        assert (after.avatar_emoji, after.avatar_color) == ("", None)
        assert after.avatar == Avatar("A", tone_for(ONE), False)

    def test_changes_are_audited(self, admin, ledger):
        _edit(admin, by_account("111111111111"), avatar_emoji="🐳", avatar_color="4")
        log = (ledger.parent / excel_source.AUDIT_NAME).read_text(encoding="utf-8")
        assert "AVATAR 空 → 🐳" in log and "AVATAR_COLOR 空 → 4" in log

    def test_saving_it_unchanged_writes_nothing(self, admin, ledger):
        """Excel 里存的是数字 3，表单交上来的是文本 "3"：比较时要按同一套规则归一，不能算一次改动。"""
        rewrite_ledger(ledger, EMAIL=[ONE, TWO], AVATAR=["🦊", None], AVATAR_COLOR=[3, None])
        before = ledger.stat().st_mtime_ns
        assert _edit(admin, by_account("111111111111")).status_code == 302
        assert ledger.stat().st_mtime_ns == before

    def test_a_bad_color_is_refused_and_the_dialog_comes_back(self, admin, ledger):
        before = ledger.stat().st_mtime_ns
        response = _edit(admin, by_account("111111111111"), avatar_emoji="🐳", avatar_color="9")
        assert response.status_code == 400
        html = response.get_data(as_text=True)
        assert reopened_dialogs(html) == ["dlg-edit-1"]
        dialog = _dialog(html, 'id="dlg-edit-1"')
        assert "头像底色不对，请从色块里选。" in dialog
        assert input_attrs(dialog, "avatar_emoji")["value"] == "🐳"            # 填过的表情还在
        assert ledger.stat().st_mtime_ns == before

    def test_the_edit_dialog_shows_the_saved_choice(self, admin, ledger):
        rewrite_ledger(ledger, EMAIL=[ONE, TWO], AVATAR=["🦊", None], AVATAR_COLOR=[3, None])
        html = page(admin)
        dialog = _dialog(html, 'id="dlg-edit-1"')
        assert input_attrs(dialog, "avatar_emoji")["value"] == "🦊"
        assert chosen_color(dialog) == ["3"]
        classes, shown = preview(dialog)
        assert {"av-3", "avatar-emoji"} <= classes and shown == "🦊"
        # 没选过的账号：「自动」勾着，预览是邮箱首字母、按邮箱配的色
        other = _dialog(html, 'id="dlg-edit-2"')
        assert chosen_color(other) == [""]
        classes, shown = preview(other)
        assert f"av-{tone_for(TWO)}" in classes and "avatar-emoji" not in classes and shown == "A"

    def test_the_new_account_dialog_starts_on_auto(self, admin, ledger):
        dialog = _dialog(page(admin), 'id="dlg-create"')
        assert [field["value"] for field in inputs(dialog, "avatar_color")] == ["", *map(str, range(avatars.TONES))]
        assert chosen_color(dialog) == [""]
        assert input_attrs(dialog, "avatar_emoji")["value"] == ""
        assert preview(dialog)[1] == "?"                                    # 还什么都没填

    def test_quick_emoji_buttons(self, admin, ledger):
        from bedrock_cost.accounts import AVATAR_EMOJIS

        dialog = _dialog(page(admin), 'id="dlg-create"')
        assert re.findall(r'data-emoji="([^"]+)"', dialog) == list(AVATAR_EMOJIS)
