"""真实调用 Cost Explorer 的用例。

默认跳过（pyproject 的 addopts 里排掉了 integration 标记）。要跑：

    pytest -m integration

前提：项目根有真实的 cred.xlsx，且里面的 AK/SK 有 ce:GetCostAndUsage 权限。
注意 CE 按请求计费（约 0.01 USD/次），跑一遍是账号数 × 用例数 次请求。
"""

from __future__ import annotations

from datetime import date

import pytest

from bedrock_cost import config, cost_explorer, excel_source, usage_explorer
from bedrock_cost.report import build_report

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def real_accounts():
    if not config.EXCEL_PATH.is_file():
        pytest.skip(f"没有真实台账：{config.EXCEL_PATH}")
    accounts = [a for a in excel_source.load_accounts(force=True) if a.has_credentials]
    if not accounts:
        pytest.skip("台账里没有带凭证的账号")
    return accounts


@pytest.fixture(scope="module")
def month_to_date():
    today = date.today()
    return today.replace(day=1), today


def test_tag_split_adds_up_to_the_account_total(real_accounts, month_to_date):
    """TAG + UNTAG 的原始金额必须精确等于账号总消费，一分钱都不能漏。"""
    start, end = month_to_date
    account = real_accounts[0]
    split = cost_explorer.fetch_split(account, start, end, refresh=True)
    if split.error:
        pytest.skip(f"CE 查询失败：{split.error}")

    total = cost_explorer._query(  # 不分组的总额，用来对账
        account, start, end
    )
    assert round(split.tag_raw + split.untag_raw, 4) == round(
        total.tag_raw + total.untag_raw, 4
    )


@pytest.mark.parametrize("dimension", ["service", "tag", "account"])
def test_drilldown_reconciles_with_overview(real_accounts, month_to_date, dimension):
    """任一维度加总都必须等于概览页的总消费。"""
    start, end = month_to_date
    overview = build_report(start, end, refresh=True)
    if overview.failed_count:
        pytest.skip(f"有账号查询失败：{overview.errors}")

    usage = usage_explorer.build_usage(real_accounts, start, end, dimension, "daily", refresh=True)
    assert not usage.errors, usage.errors
    assert round(usage.total_marked, 2) == round(overview.total_cost, 2)


def test_daily_and_monthly_agree(real_accounts, month_to_date):
    start, end = month_to_date
    daily = usage_explorer.build_usage(real_accounts, start, end, "service", "daily")
    monthly = usage_explorer.build_usage(real_accounts, start, end, "service", "monthly")
    assert round(daily.total_marked, 2) == round(monthly.total_marked, 2)


def test_mid_month_start_does_not_drop_buckets(real_accounts):
    """按月查询且区间从月中开始时，CE 返回的 Start 不是 1 号，别对错桶。"""
    today = date.today()
    start = usage_explorer._month_shift(today, -2).replace(day=15)
    usage = usage_explorer.build_usage(real_accounts, start, today, "service", "monthly")
    assert round(sum(usage.column_totals), 4) == round(usage.total_marked, 4)


def test_cache_saves_a_request(real_accounts, month_to_date):
    start, end = month_to_date
    account = real_accounts[0]
    cost_explorer.clear_cache()
    first = cost_explorer.fetch_split(account, start, end)
    second = cost_explorer.fetch_split(account, start, end)
    assert first.from_cache is False
    assert second.from_cache is True
    assert second.tag_raw == first.tag_raw


def test_invalid_credentials_do_not_leak_the_key(real_accounts, month_to_date):
    start, end = month_to_date
    broken = excel_source.Account(
        partner="BROKEN",
        account="000000000000",
        budget=1.0,
        tag_ratio=1.0,
        untag_ratio=1.0,
        ak="AKIAZZZZZZZZZZZZZZZZ",
        sk="wrongsecretkey1234567890abcdefghij",
        row=99,
        tag_spec="map-migrated=migX",
    )
    split = cost_explorer.fetch_split(broken, start, end, refresh=True)
    assert split.error
    assert "AKIAZZZZZZZZZZZZZZZZ" not in split.error
    assert "wrongsecretkey1234567890abcdefghij" not in split.error
