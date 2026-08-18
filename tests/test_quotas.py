"""模型配额：解析、分组、排序、缓存、异常。"""

from __future__ import annotations

import pytest

from bedrock_cost import config, quotas
from bedrock_cost.quotas import QuotaRow, build_quota_report, display_name, fetch_quotas


def quota(name: str, value: float, code: str = "L-X", adjustable: bool = False) -> dict:
    """实测：日配额 Adjustable=False，分钟配额 Adjustable=True。"""
    return {
        "QuotaName": name,
        "Value": value,
        "QuotaCode": code,
        "Adjustable": adjustable,
        "ServiceCode": "bedrock",
    }


DAY = "Global cross-region model inference tokens per day for "
MIN = "Global cross-region model inference tokens per minute for "

# 照抄实测返回的样子：Claude、Nova、Cohere 混在一起，还有大量无关配额
SAMPLE = [
    quota(DAY + "Anthropic Claude Opus 4.8", 43_200_000_000, "L-DAY48"),
    quota(MIN + "Anthropic Claude Opus 4.8", 30_000_000, "L-MIN48"),
    quota(DAY + "Anthropic Claude Opus 4.6 V1", 4_320_000_000),
    quota(MIN + "Anthropic Claude Opus 4.6 V1", 3_000_000),
    quota(DAY + "Anthropic Claude Sonnet 4.5 V1", 7_200_000_000),
    quota(MIN + "Anthropic Claude Sonnet 4.5 V1", 5_000_000),
    quota(DAY + "Anthropic Claude Sonnet 4.5 V1 1M Context Length", 1_440_000_000),
    quota(MIN + "Anthropic Claude Sonnet 4.5 V1 1M Context Length", 1_000_000),
    # 非 Claude，必须被排除
    quota(DAY + "Amazon Nova 2 Lite", 11_520_000_000),
    quota(MIN + "Amazon Nova 2 Lite", 8_000_000),
    quota(DAY + "Cohere Embed V4", 432_000_000),
    # 完全无关的 bedrock 配额，必须被排除
    quota("Batch inference input file size (in GB) for Claude Opus 5", 1000),
    quota("(Model customization) Sum of on demand custom model deployment tokens per day for Amazon Nova Lite", 1),
    quota("On-demand InvokeModel requests per minute for Anthropic Claude Opus 4.8", 250),
]


class FakePaginator:
    def __init__(self, pages):
        self._pages = pages
        self.seen_kwargs = None

    def paginate(self, **kwargs):
        self.seen_kwargs = kwargs
        return iter(self._pages)


class FakeClient:
    def __init__(self, pages):
        self.paginator = FakePaginator(pages)

    def get_paginator(self, name):
        assert name == "list_service_quotas"
        return self.paginator


@pytest.fixture
def fake_global_ids(monkeypatch):
    """假的 SYSTEM_DEFINED 跨区配置，照实测的样子。"""
    monkeypatch.setattr(
        quotas,
        "fetch_global_model_ids",
        lambda account: {
            quotas.match_key("global.anthropic.claude-opus-4-8"): "global.anthropic.claude-opus-4-8",
            quotas.match_key("global.anthropic.claude-opus-4-6-v1"): "global.anthropic.claude-opus-4-6-v1",
            quotas.match_key("global.anthropic.claude-sonnet-4-5-20250929-v1:0"):
                "global.anthropic.claude-sonnet-4-5-20250929-v1:0",
        },
    )


@pytest.fixture
def fake_quotas(monkeypatch):
    """把 boto3 换掉，返回实测样式的配额，分两页以验证分页处理。"""
    client = FakeClient([{"Quotas": SAMPLE[:7]}, {"Quotas": SAMPLE[7:]}])
    monkeypatch.setattr(quotas.boto3, "client", lambda *a, **k: client)
    quotas.clear_cache()
    yield client
    quotas.clear_cache()


class TestDisplayName:
    def test_strips_vendor(self):
        assert display_name("Anthropic Claude Opus 4.6 V1") == "Claude Opus 4.6 V1"

    def test_leaves_others_alone(self):
        assert display_name("Claude Opus 5") == "Claude Opus 5"


class TestQuotaRow:
    def test_ratio_and_balance(self):
        row = QuotaRow(model="x", day=43_200_000_000, minute=30_000_000)
        assert row.minutes_to_exhaust == pytest.approx(1440)
        assert row.balanced is True

    def test_unbalanced_is_flagged(self):
        row = QuotaRow(model="x", day=1_000_000, minute=1_000)
        assert row.minutes_to_exhaust == pytest.approx(1000)
        assert row.balanced is False

    def test_missing_side_has_no_ratio(self):
        assert QuotaRow(model="x", day=100).minutes_to_exhaust is None
        assert QuotaRow(model="x", minute=1).minutes_to_exhaust is None
        assert QuotaRow(model="x").balanced is False

    def test_zero_minute_does_not_divide_by_zero(self):
        assert QuotaRow(model="x", day=100, minute=0).minutes_to_exhaust is None

    def test_complete(self):
        assert QuotaRow(model="x", day=1, minute=1).complete is True
        assert QuotaRow(model="x", day=1).complete is False

    def test_long_context_detected(self):
        assert QuotaRow(model="Anthropic Claude Sonnet 4.5 V1 1M Context Length").is_long_context
        assert not QuotaRow(model="Anthropic Claude Sonnet 4.5 V1").is_long_context


class TestFetchQuotas:
    def test_pairs_day_and_minute_into_one_row(self, ledger, accounts, fake_quotas):
        rows, error = fetch_quotas(accounts[0])
        assert error is None
        opus = next(r for r in rows if r.display == "Claude Opus 4.8")
        assert opus.day == 43_200_000_000
        assert opus.minute == 30_000_000
        assert opus.day_code == "L-DAY48"
        assert opus.minute_code == "L-MIN48"

    def test_only_claude(self, ledger, accounts, fake_quotas):
        rows, _ = fetch_quotas(accounts[0])
        names = {r.display for r in rows}
        assert not any("Nova" in n or "Cohere" in n for n in names)

    def test_ignores_other_bedrock_quotas(self, ledger, accounts, fake_quotas):
        """只要这两类 token 配额，批处理大小、RPM 之类的一律不要。"""
        rows, _ = fetch_quotas(accounts[0])
        assert len(rows) == 4
        for row in rows:
            assert row.day is not None or row.minute is not None

    def test_long_context_is_its_own_row(self, ledger, accounts, fake_quotas):
        """1M 上下文是独立配额，绝不能并进同名模型。"""
        rows, _ = fetch_quotas(accounts[0])
        names = {r.display for r in rows}
        assert "Claude Sonnet 4.5 V1" in names
        assert "Claude Sonnet 4.5 V1 1M Context Length" in names
        plain = next(r for r in rows if r.display == "Claude Sonnet 4.5 V1")
        assert plain.day == 7_200_000_000  # 没有被 1M 那条覆盖

    def test_sorted_by_day_quota_desc(self, ledger, accounts, fake_quotas):
        rows, _ = fetch_quotas(accounts[0])
        assert [r.day for r in rows] == sorted((r.day for r in rows), reverse=True)

    def test_asks_for_a_big_page(self, ledger, accounts, fake_quotas):
        """默认分页每页只有 8 条，1162 条要发 146 次请求，必须显式设 PageSize。"""
        fetch_quotas(accounts[0])
        kwargs = fake_quotas.paginator.seen_kwargs
        assert kwargs["PaginationConfig"]["PageSize"] == quotas.PAGE_SIZE
        assert quotas.PAGE_SIZE >= 100

    def test_result_is_cached(self, ledger, accounts, fake_quotas, monkeypatch):
        fetch_quotas(accounts[0])

        def explode(*a, **k):
            raise AssertionError("命中缓存时不该再调 AWS")

        monkeypatch.setattr(quotas.boto3, "client", explode)
        rows, error = fetch_quotas(accounts[0])
        assert error is None and rows

    def test_cache_is_per_account(self, ledger, accounts, fake_quotas):
        fetch_quotas(accounts[0])
        keys = {
            (a.ak[-6:], a.account, "quotas") for a in accounts
        }
        assert len(keys) == len(accounts)

    def test_api_failure_is_returned_not_raised(self, ledger, accounts, monkeypatch):
        def broken(*a, **k):
            raise RuntimeError("AccessDeniedException: no servicequotas permission")

        monkeypatch.setattr(quotas.boto3, "client", broken)
        quotas.clear_cache()
        rows, error = fetch_quotas(accounts[0])
        assert rows == []
        assert error and "servicequotas" in error

    def test_error_does_not_leak_the_key(self, ledger, accounts, monkeypatch):
        def broken(*a, **k):
            raise RuntimeError(f"bad key {accounts[0].ak}")

        monkeypatch.setattr(quotas.boto3, "client", broken)
        quotas.clear_cache()
        _rows, error = fetch_quotas(accounts[0])
        assert accounts[0].ak not in error


class TestBuildQuotaReport:
    def test_labels_the_account(self, ledger, accounts, fake_quotas):
        report = build_quota_report(accounts[0])
        assert accounts[0].partner in report.account_label
        assert accounts[0].account in report.account_label

    def test_no_account_gives_an_empty_report(self):
        report = build_quota_report(None)
        assert report.rows == []
        assert report.account_label == ""

    def test_highest_and_lowest(self, ledger, accounts, fake_quotas):
        report = build_quota_report(accounts[0])
        assert report.highest.display == "Claude Opus 4.8"
        assert report.lowest.display == "Claude Sonnet 4.5 V1 1M Context Length"

    def test_all_balanced_in_the_sample(self, ledger, accounts, fake_quotas):
        """实测这些配额都是 1440 倍关系，样例也照抄了真实值。"""
        report = build_quota_report(accounts[0])
        assert report.unbalanced_rows == []
        assert len(report.complete_rows) == 4

    def test_incomplete_row_is_separated(self, ledger, accounts, monkeypatch):
        """只有日配额没有分钟配额时，要能被单独识别出来而不是当成 0。"""
        client = FakeClient([{"Quotas": [quota(DAY + "Anthropic Claude Opus 9", 100)]}])
        monkeypatch.setattr(quotas.boto3, "client", lambda *a, **k: client)
        quotas.clear_cache()
        report = build_quota_report(accounts[0])
        assert len(report.incomplete_rows) == 1
        assert report.incomplete_rows[0].minute is None
        assert report.incomplete_rows[0].minutes_to_exhaust is None

    def test_refresh_bypasses_cache(self, ledger, accounts, fake_quotas, monkeypatch):
        build_quota_report(accounts[0])
        calls = []
        original = quotas.boto3.client
        monkeypatch.setattr(
            quotas.boto3, "client", lambda *a, **k: (calls.append(1), original(*a, **k))[1]
        )
        build_quota_report(accounts[0], refresh=True)
        assert calls, "refresh=1 应该跳过缓存重新拉"

    def test_adjustability_is_tracked_per_quota_not_merged(self, ledger, accounts, monkeypatch):
        """实测日配额改不了、分钟配额能申请提额。合成一个字段会把日配额也说成能调。"""
        client = FakeClient(
            [{"Quotas": [
                quota(DAY + "Anthropic Claude Opus 9", 100, adjustable=False),
                quota(MIN + "Anthropic Claude Opus 9", 1, adjustable=True),
            ]}]
        )
        monkeypatch.setattr(quotas.boto3, "client", lambda *a, **k: client)
        quotas.clear_cache()
        report = build_quota_report(accounts[0])
        row = report.rows[0]
        assert row.day_adjustable is False
        assert row.minute_adjustable is True
        assert report.adjustable_minute_rows == [row]


class TestModelId:
    """Model ID 取自真实的 SYSTEM_DEFINED 跨区配置，不是从配额名拼出来的。"""

    @pytest.mark.parametrize(
        "text, expected",
        [
            ("Anthropic Claude Opus 4.8", ("opus", "4.8", False)),
            ("global.anthropic.claude-opus-4-8", ("opus", "4.8", False)),
            ("Anthropic Claude Opus 4.6 V1", ("opus", "4.6", False)),
            ("global.anthropic.claude-opus-4-6-v1", ("opus", "4.6", False)),
            # 日期戳必须先剥掉，否则版本号会被读成 4.20250514
            ("global.anthropic.claude-sonnet-4-20250514-v1:0", ("sonnet", "4", False)),
            ("Anthropic Claude Sonnet 4 V1", ("sonnet", "4", False)),
            ("Anthropic Claude Sonnet 4.5 V1 1M Context Length", ("sonnet", "4.5", True)),
        ],
    )
    def test_match_key(self, text, expected):
        assert quotas.match_key(text) == expected

    def test_non_claude_has_no_key(self):
        assert quotas.match_key("Amazon Nova 2 Lite") is None

    def test_long_context_is_a_different_key(self):
        plain = quotas.match_key("Anthropic Claude Sonnet 4.5 V1")
        long_ctx = quotas.match_key("Anthropic Claude Sonnet 4.5 V1 1M Context Length")
        assert plain != long_ctx

    def test_model_id_is_filled_in(self, ledger, accounts, fake_quotas, fake_global_ids):
        report = build_quota_report(accounts[0])
        opus = next(r for r in report.rows if r.display == "Claude Opus 4.8")
        assert opus.model_id == "global.anthropic.claude-opus-4-8"

    def test_long_context_has_no_model_id(self, ledger, accounts, fake_quotas, fake_global_ids):
        """1M 那条没有独立的 global 配置，留空是正确结果而不是 join 失败。"""
        report = build_quota_report(accounts[0])
        row = next(r for r in report.rows if r.is_long_context)
        assert row.model_id == ""
        assert row in report.without_model_id

    def test_missing_permission_degrades_gracefully(self, ledger, accounts, fake_quotas, monkeypatch):
        """没有 bedrock 权限时配额表照样出，只是没有 ID 列。"""
        monkeypatch.setattr(quotas, "fetch_global_model_ids", lambda a: {})
        report = build_quota_report(accounts[0])
        assert report.rows
        assert all(r.model_id == "" for r in report.rows)

    def test_ignores_non_global_profiles(self, ledger, accounts, monkeypatch):
        """只认 global.anthropic.*，us.* / 应用配置都不算。"""
        profiles = [
            {"inferenceProfileId": "us.anthropic.claude-opus-4-8"},
            {"inferenceProfileId": "global.anthropic.claude-opus-4-8"},
            {"inferenceProfileId": "2kbsta0lwebx"},
        ]

        class _Client:
            def list_inference_profiles(self, **kw):
                assert kw["typeEquals"] == "SYSTEM_DEFINED"
                return {"inferenceProfileSummaries": profiles}

        monkeypatch.setattr(quotas.boto3, "client", lambda *a, **k: _Client())
        quotas.clear_cache()
        ids = quotas.fetch_global_model_ids(accounts[0])
        assert list(ids.values()) == ["global.anthropic.claude-opus-4-8"]
