"""台账读取：TAG 列解析、数字容错、列缺失、凭证不外泄。"""

from __future__ import annotations

import pytest

from bedrock_cost import config, excel_source
from bedrock_cost.excel_source import ExcelSourceError, load_accounts, parse_tag_spec

from .conftest import LEDGER_HEADER, LEDGER_ROWS, write_ledger


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
        write_ledger(
            path,
            header=LEDGER_HEADER[:-1],
            rows=[LEDGER_ROWS[0][:-1]],
        )
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
