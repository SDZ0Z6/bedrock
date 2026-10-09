"""台账读取：TAG 列解析、数字容错、列缺失、凭证不外泄，以及改版加的几列可选列。

生命周期标签（LIFECYCLE 列和第二个工作表）细测在 test_lifecycle，头像（AVATAR、AVATAR_COLOR）在 test_avatars。
"""

from __future__ import annotations

from datetime import date, datetime

import pytest

from bedrock_cost import config, excel_source
from bedrock_cost.excel_source import (
    Account,
    ExcelSourceError,
    _to_date,
    load_accounts,
    parse_tag_spec,
)

from .conftest import LEDGER_HEADER, LEDGER_ROWS, ledger_without, write_ledger


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("map-migrated=migXYT8EVQSVP", ("map-migrated", "migXYT8EVQSVP")),
        ("map-migrated:migXYT8EVQSVP", ("map-migrated", "migXYT8EVQSVP")),
        ("map-migrated$migXYT8EVQSVP", ("map-migrated", "migXYT8EVQSVP")),
        ("  map-migrated = migXYT8EVQSVP  ", ("map-migrated", "migXYT8EVQSVP")),
        ("Project=Team A=B", ("Project", "Team A=B")),  # 只在第一个分隔符处切
        ("map-migrated", ("map-migrated", None)),  # 只给键 = 任意非空值算 TAG
        ("map-migrated=", ("map-migrated", None)),
    ],
)
def test_parse_tag_spec(raw, expected):
    assert parse_tag_spec(raw) == expected


@pytest.mark.parametrize("raw", ["", "   ", None])
def test_blank_tag_spec_falls_back_to_config(raw):
    assert parse_tag_spec(raw) == (config.TAG_KEY, None)


def test_loads_both_accounts(accounts):
    assert [a.partner for a in accounts] == ["ALPHA", "BETA"]
    assert [a.account for a in accounts] == ["111111111111", "222222222222"]
    assert [a.budget for a in accounts] == [500000.0, 100000.0]


def test_tag_column_drives_per_account_split(accounts):
    alpha, beta = accounts
    assert (alpha.tag_key, alpha.tag_value) == ("map-migrated", "migALPHA")
    assert (beta.tag_key, beta.tag_value) == ("map-migrated", "migBETA")
    assert alpha.tag_label == "map-migrated=migALPHA"


def test_account_key_is_stable_and_unique(accounts):
    keys = [a.key for a in accounts]
    assert len(set(keys)) == len(keys)
    assert all(a.account in a.key for a in accounts)


def test_repr_hides_credentials(accounts):
    """凭证不能出现在 repr 里，否则一次误打印就泄露。"""
    text = repr(accounts)
    assert "AKIAFAKEALPHA0000000" not in text
    assert "x" * 40 not in text
    assert "ALPHA" in text  # 其他字段照常可见


class TestNumberParsing:
    """预算和比率允许写成千分位、百分号、带货币符号。"""

    @pytest.mark.parametrize(
        "budget, tag_ratio, untag_ratio, expected",
        [
            ("500,000", "1", "1.05", (500000.0, 1.0, 1.05)),
            ("$1,234.56", "100%", "105%", (1234.56, 1.0, 1.05)),
            ("", "", "", (0.0, 1.0, 1.0)),  # 空值取默认
            ("abc", "xyz", "???", (0.0, 1.0, 1.0)),  # 垃圾值取默认
        ],
    )
    def test_tolerant_numbers(self, tmp_path, monkeypatch, budget, tag_ratio, untag_ratio, expected):
        path = tmp_path / "n.xlsx"
        write_ledger(path, rows=[["P", 1, budget, tag_ratio, untag_ratio, "ak", "sk", "k=v"]])
        monkeypatch.setattr(config, "EXCEL_PATH", path)
        excel_source.clear_cache()
        account = load_accounts(force=True)[0]
        assert (account.budget, account.tag_ratio, account.untag_ratio) == expected

    def test_account_id_loses_float_tail(self, tmp_path, monkeypatch):
        path = tmp_path / "f.xlsx"
        write_ledger(path, rows=[["P", 139675293794.0, 1, 1, 1, "ak", "sk", "k=v"]])
        monkeypatch.setattr(config, "EXCEL_PATH", path)
        excel_source.clear_cache()
        assert load_accounts(force=True)[0].account == "139675293794"


class TestSchemaTolerance:
    def test_column_order_and_case_do_not_matter(self, tmp_path, monkeypatch):
        path = tmp_path / "shuffled.xlsx"
        write_ledger(
            path,
            header=["tag", "sk", "ak", "untag_ratio", "tag_ratio", "budget", "account", "partner", "备注"],
            rows=[["map-migrated=migZ", "sk", "ak", 1.05, 1, 9000, 333, "GAMMA", "多余列"]],
        )
        monkeypatch.setattr(config, "EXCEL_PATH", path)
        excel_source.clear_cache()
        account = load_accounts(force=True)[0]
        assert (account.partner, account.account, account.budget) == ("GAMMA", "333", 9000.0)
        assert account.tag_value == "migZ"

    def test_blank_rows_are_skipped(self, tmp_path, monkeypatch):
        path = tmp_path / "gaps.xlsx"
        write_ledger(
            path,
            rows=[
                LEDGER_ROWS[0],
                [None] * len(LEDGER_HEADER),
                LEDGER_ROWS[1],
            ],
        )
        monkeypatch.setattr(config, "EXCEL_PATH", path)
        excel_source.clear_cache()
        assert len(load_accounts(force=True)) == 2

    def test_tag_column_is_optional(self, tmp_path, monkeypatch):
        """老台账没有 TAG 列也要能跑，回落到 .env 的 TAG_KEY。"""
        path = tmp_path / "notag.xlsx"
        header, rows = ledger_without("TAG")
        write_ledger(path, header=header, rows=rows[:1])
        monkeypatch.setattr(config, "EXCEL_PATH", path)
        excel_source.clear_cache()
        account = load_accounts(force=True)[0]
        assert (account.tag_key, account.tag_value) == (config.TAG_KEY, None)

    def test_missing_required_column_names_it(self, tmp_path, monkeypatch):
        path = tmp_path / "broken.xlsx"
        write_ledger(path, header=["PARTNER", "ACCOUNT", "TAG"], rows=[["P", 1, "k=v"]])
        monkeypatch.setattr(config, "EXCEL_PATH", path)
        excel_source.clear_cache()
        with pytest.raises(ExcelSourceError, match="BUDGET"):
            load_accounts(force=True)

    def test_missing_file_is_reported(self, tmp_path, monkeypatch):
        monkeypatch.setattr(config, "EXCEL_PATH", tmp_path / "nope.xlsx")
        excel_source.clear_cache()
        with pytest.raises(ExcelSourceError, match="找不到"):
            load_accounts(force=True)


def test_edits_are_picked_up_without_restart(ledger, monkeypatch):
    """改完台账刷新页面就生效，靠 mtime+size 判断，不用重启服务。"""
    assert len(load_accounts()) == 2
    write_ledger(ledger, rows=[*LEDGER_ROWS, ["GAMMA", 333, 1000, 1, 1, "ak", "sk", "k=v"]])
    assert len(load_accounts()) == 3


class TestStartDate:
    """启用日期：概览页从这一天累计消费，所以宁可当没填也不要猜。"""

    @pytest.mark.parametrize(
        "raw, expected",
        [
            ("2026-09-01", date(2026, 9, 1)),
            ("2026/09/01", date(2026, 9, 1)),
            ("2026.09.01", date(2026, 9, 1)),
            ("20260901", date(2026, 9, 1)),
            (" 2026-09-01 ", date(2026, 9, 1)),
            (datetime(2026, 9, 1, 13, 45), date(2026, 9, 1)),  # Excel 日期格式
            (date(2026, 9, 1), date(2026, 9, 1)),
            ("", None),
            (None, None),
            ("下周一", None),      # 认不出就当没填，不猜
            (12345, None),
        ],
    )
    def test_parsing(self, raw, expected):
        assert _to_date(raw) == expected

    def test_column_is_optional(self, tmp_path, monkeypatch):
        """老台账没有这一列也要能读，start_date 留空。"""
        path = tmp_path / "nostart.xlsx"
        header, rows = ledger_without("START_DATE")
        write_ledger(path, header=header, rows=rows)
        monkeypatch.setattr(config, "EXCEL_PATH", path)
        excel_source.clear_cache()
        assert all(a.start_date is None for a in load_accounts(force=True))

    def test_is_read_from_the_ledger(self, ledger):
        excel_source.clear_cache()
        assert load_accounts(force=True)[0].start_date == date(2026, 8, 1)


class TestTgChatIds:
    """一个账号可以发到多个群：台账里一格存多个，逗号隔开。读的时候宽松，写回统一。"""

    @pytest.mark.parametrize(
        "raw, expected",
        [
            (None, ()),
            ("", ()),
            ("-1001111111111", ("-1001111111111",)),
            (-1001111111111, ("-1001111111111",)),          # Excel 当成了数字
            (-1001111111111.0, ("-1001111111111",)),
            ("-1001111111111,-1002222222222", ("-1001111111111", "-1002222222222")),
            ("-1001111111111，-1002222222222", ("-1001111111111", "-1002222222222")),  # 全角逗号
            ("-1001111111111; @alerts_ch", ("-1001111111111", "@alerts_ch")),
            ("-1001111111111\n-1002222222222", ("-1001111111111", "-1002222222222")),
            ("-1002222222222, -1001111111111, -1002222222222", ("-1002222222222", "-1001111111111")),
        ],
    )
    def test_splitting(self, raw, expected):
        from bedrock_cost.excel_source import _split_chat_ids

        assert _split_chat_ids(raw) == expected

    def test_stored_as_one_comma_joined_cell(self):
        from bedrock_cost.excel_source import _canon_chat_ids

        assert _canon_chat_ids(" -1001111111111 ，-1002222222222 ") == "-1001111111111,-1002222222222"


class TestNewColumns:
    """改版加的 EMAIL、LIFECYCLE、AVATAR、AVATAR_COLOR 都是可选列：老台账没有它们照样能读。"""

    def test_an_old_ledger_reads_with_blanks(self, accounts):
        for account in accounts:
            assert (account.email, account.lifecycle, account.avatar_emoji, account.avatar_color) == ("", (), "", None)

    def test_they_are_read(self, tmp_path, monkeypatch):
        path = tmp_path / "new.xlsx"
        # 表头和别的列一样不分大小写、不管前后空格
        header = [*LEDGER_HEADER, "email", " Lifecycle ", "AVATAR", "avatar_color"]
        rows = [
            [*LEDGER_ROWS[0], " Ops.Team@Example.com ", "正常，风控", "🦊", 2],
            [*LEDGER_ROWS[1], None, None, None, None],
        ]
        write_ledger(path, header=header, rows=rows)
        monkeypatch.setattr(config, "EXCEL_PATH", path)
        excel_source.clear_cache()
        first, second = load_accounts(force=True)
        assert first.email == "Ops.Team@Example.com"          # 去掉前后空格，大小写照存
        assert first.lifecycle == ("正常", "风控")
        assert (first.avatar_emoji, first.avatar_color) == ("🦊", 2)
        assert (second.email, second.lifecycle, second.avatar_emoji, second.avatar_color) == ("", (), "", None)

    @pytest.mark.parametrize(
        "email, label",
        [
            ("ops.team@example.com", "ops.team"),     # 图表、面包屑、下拉框里的短名：@ 前面那段
            ("", "111111111111"),                     # 没填邮箱就用号码
        ],
    )
    def test_label(self, email, label):
        account = Account(partner="P", account="111111111111", budget=0, tag_ratio=1, untag_ratio=1, email=email)
        assert account.label == label

    def test_the_missing_column_message_lists_the_optional_ones_too(self, tmp_path, monkeypatch):
        path = tmp_path / "broken.xlsx"
        write_ledger(path, header=["PARTNER", "ACCOUNT"], rows=[["P", 1]])
        monkeypatch.setattr(config, "EXCEL_PATH", path)
        excel_source.clear_cache()
        with pytest.raises(ExcelSourceError) as caught:
            load_accounts(force=True)
        optional = str(caught.value).partition("可选")[2]
        for column in ("EMAIL", "LIFECYCLE", "AVATAR", "AVATAR_COLOR"):
            assert column in optional

    def test_writing_keeps_them_editable_but_not_the_credentials(self):
        """账号邮箱、生命周期、头像都能在修改弹窗里改；凭证不行（要换就停用重建）。"""
        for name in ("email", "lifecycle", "avatar_emoji", "avatar_color"):
            assert name in excel_source.EDITABLE
        assert "ak" not in excel_source.EDITABLE and "sk" not in excel_source.EDITABLE
