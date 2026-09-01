"""模型配额：解析、join、按区差异、降级、排序、缓存、异常。

页面主键是应用推理配置的 ARN，所以这里的 fake 要同时提供两套数据：
Service Quotas 的配额清单，和 Bedrock 的 APPLICATION 推理配置。
配额值按区不同（照实测：Opus 4.6 只有 us-east-1 提到了 6M），
不然「区域」这一维就没东西可测。
"""

from __future__ import annotations

import pytest

from bedrock_cost import quotas
from bedrock_cost.quotas import (
    QuotaRow,
    base_model_id,
    build_quota_report,
    display_name,
    fetch_app_profiles,
    fetch_region_quotas,
)

DAY = "Global cross-region model inference tokens per day for "
MIN = "Global cross-region model inference tokens per minute for "

REGIONS = quotas.QUOTA_REGIONS


def quota(name: str, value: float, code: str = "L-X", adjustable: bool = False) -> dict:
    """实测：日配额 Adjustable=False，分钟配额 Adjustable=True。"""
    return {
        "QuotaName": name,
        "Value": value,
        "QuotaCode": code,
        "Adjustable": adjustable,
        "ServiceCode": "bedrock",
    }


# 实测形态：提额是按区批的，Opus 4.6 只有 us-east-1 提到了 6M / 8.64B
OPUS46_RAISED = {"us-east-1": (6_000_000, 8_640_000_000)}
OPUS46_DEFAULT = (3_000_000, 4_320_000_000)


# AK -> 账号号码。多账号时 ARN 必须互不相同、配额也要不同，
# 不然「一行一条 ARN」和「最高值不跨账号」都测不出来。
ACCOUNT_BY_AK = {
    "AKIAFAKEALPHA0000000": "111111111111",
    "AKIAFAKEBETA00000000": "222222222222",
}
RAISED_ACCOUNT = "111111111111"


def quotas_for(region: str, acct: str = RAISED_ACCOUNT) -> list[dict]:
    """照实测返回的样子：Claude、Nova 混在一起，还有大量无关配额。

    只有 RAISED_ACCOUNT 那个账号提了 Opus 4.6 的额；另一个账号四个区都是 3M。
    """
    if acct == RAISED_ACCOUNT:
        tpm46, tpd46 = OPUS46_RAISED.get(region, OPUS46_DEFAULT)
    else:
        tpm46, tpd46 = OPUS46_DEFAULT
    rows = [
        quota(DAY + "Anthropic Claude Opus 4.8", 43_200_000_000, "L-DAY48"),
        quota(MIN + "Anthropic Claude Opus 4.8", 30_000_000, "L-MIN48", adjustable=True),
        quota(DAY + "Anthropic Claude Opus 4.6 V1", tpd46, "L-DAY46"),
        quota(MIN + "Anthropic Claude Opus 4.6 V1", tpm46, "L-MIN46", adjustable=True),
        quota(DAY + "Anthropic Claude Sonnet 4.5 V1 1M Context Length", 1_440_000_000),
        quota(MIN + "Anthropic Claude Sonnet 4.5 V1 1M Context Length", 1_000_000),
        # 非 Claude，必须被排除
        quota(DAY + "Amazon Nova 2 Lite", 11_520_000_000),
        quota(MIN + "Amazon Nova 2 Lite", 8_000_000),
        # 完全无关的 bedrock 配额，必须被排除
        quota("On-demand InvokeModel requests per minute for Anthropic Claude Opus 4.8", 250),
        quota("Batch inference input file size (in GB) for Claude Opus 5", 1000),
    ]
    if region != "us-west-1":
        # 实测 us-west-1 列出的模型最少，某些模型那里根本没有
        rows += [
            quota(DAY + "Anthropic Claude Sonnet 4 V1", 288_000_000),
            quota(MIN + "Anthropic Claude Sonnet 4 V1", 200_000),
        ]
    return rows


# 账号自建的应用推理配置。配置名和模型名毫无关系，照实测的命名抄。
PROFILE_MODELS = [
    ("anthropic.claude-opus-4-8", "claude48oupsauto_0706"),
    ("anthropic.claude-opus-4-6-v1", "claude46Oupsauto_wjc_0529"),
    ("amazon.nova-2-lite", "nova2liteauto_0706"),  # 非 Claude，必须被排除
]


def profile_id_for(region: str, index: int, acct: str = RAISED_ACCOUNT) -> str:
    """不透明 ID。每个账号、每个区各建各的，互不相同。"""
    return f"{acct[:2]}{region.replace('-', '')}{index}"


def profiles_for(region: str, acct: str = RAISED_ACCOUNT) -> list[dict]:
    summaries = []
    for index, (model, name) in enumerate(PROFILE_MODELS):
        pid = profile_id_for(region, index, acct)
        summaries.append(
            {
                "inferenceProfileId": pid,
                "inferenceProfileName": name,
                "inferenceProfileArn": (
                    f"arn:aws:bedrock:{region}:{acct}"
                    f":application-inference-profile/{pid}"
                ),
                # 跨区配置会多带一条不带区域的 modelArn，两条指向同一个模型
                "models": [
                    {"modelArn": f"arn:aws:bedrock:::foundation-model/{model}"},
                    {"modelArn": f"arn:aws:bedrock:{region}::foundation-model/{model}"},
                ],
                "status": "ACTIVE",
                "type": "APPLICATION",
            }
        )
    return summaries


class FakePaginator:
    def __init__(self, pages):
        self._pages = pages
        self.seen_kwargs = None

    def paginate(self, **kwargs):
        self.seen_kwargs = kwargs
        return iter(self._pages)


class FakeClient:
    """一个 client 同时扮演 service-quotas 和 bedrock，按 service 名分派。"""

    def __init__(self, service, region, acct, profiles_denied=False, profiles=None):
        self.service = service
        self.region = region
        self.acct = acct
        self.profiles_denied = profiles_denied
        self.profiles = profiles or profiles_for
        payload = quotas_for(region, acct)
        # 拆两页，顺便验证分页处理
        self.paginator = FakePaginator([{"Quotas": payload[:5]}, {"Quotas": payload[5:]}])

    def get_paginator(self, name):
        assert name == "list_service_quotas"
        return self.paginator

    def list_inference_profiles(self, **kwargs):
        assert kwargs["typeEquals"] == "APPLICATION"
        if self.profiles_denied:
            raise RuntimeError(
                "AccessDeniedException: bedrock:ListInferenceProfiles "
                "with an explicit deny in a service control policy"
            )
        return {"inferenceProfileSummaries": self.profiles(self.region, self.acct)}


def install(monkeypatch, *, profiles_denied=False, profiles=None) -> dict:
    """把 boto3.client 换掉，返回 (service, region) -> FakeClient 便于断言。"""
    made: dict[tuple[str, str], FakeClient] = {}

    def factory(service, **kwargs):
        region = kwargs.get("region_name", "")
        # boto3.client 收到的 AK 就是账号身份，据此分派
        acct = ACCOUNT_BY_AK.get(kwargs.get("aws_access_key_id", ""), "999999999999")
        client = made.get((service, region, acct))
        if client is None:
            client = FakeClient(
                service, region, acct,
                profiles_denied=profiles_denied, profiles=profiles,
            )
            made[(service, region, acct)] = client
        return client

    monkeypatch.setattr(quotas.boto3, "client", factory)
    quotas.clear_cache()
    return made


@pytest.fixture
def fake_aws(monkeypatch):
    """配额和应用推理配置都读得到——正常路径。"""
    made = install(monkeypatch)
    yield made
    quotas.clear_cache()


@pytest.fixture
def fake_aws_no_profiles(monkeypatch):
    """配额读得到、推理配置被 SCP 拒——降级路径（实测账号 139675293794）。"""
    made = install(monkeypatch, profiles_denied=True)
    yield made
    quotas.clear_cache()


class TestDisplayName:
    def test_strips_vendor(self):
        assert display_name("Anthropic Claude Opus 4.6 V1") == "Claude Opus 4.6 V1"

    def test_leaves_others_alone(self):
        assert display_name("Claude Opus 5") == "Claude Opus 5"


class TestBaseModelId:
    def test_takes_the_tail(self):
        assert (
            base_model_id(["arn:aws:bedrock:::foundation-model/anthropic.claude-opus-4-6-v1"])
            == "anthropic.claude-opus-4-6-v1"
        )

    def test_region_less_and_regional_agree(self):
        """跨区配置带两条 ARN，指向同一个模型，取哪条尾段都一样。"""
        both = [
            "arn:aws:bedrock:::foundation-model/anthropic.claude-opus-4-7",
            "arn:aws:bedrock:us-east-1::foundation-model/anthropic.claude-opus-4-7",
        ]
        assert base_model_id(both) == base_model_id(both[::-1]) == "anthropic.claude-opus-4-7"

    def test_empty(self):
        assert base_model_id([]) == ""


class TestMatchKey:
    """配额显示名和基础模型 ID 要归到同一个 key 上，join 才成立。"""

    @pytest.mark.parametrize(
        "text, expected",
        [
            ("Anthropic Claude Opus 4.8", ("opus", "4.8", False)),
            ("anthropic.claude-opus-4-8", ("opus", "4.8", False)),
            ("Anthropic Claude Opus 4.6 V1", ("opus", "4.6", False)),
            ("anthropic.claude-opus-4-6-v1", ("opus", "4.6", False)),
            # 日期戳必须先剥掉，否则版本号会被读成 4.20250514
            ("anthropic.claude-sonnet-4-20250514-v1:0", ("sonnet", "4", False)),
            ("Anthropic Claude Sonnet 4 V1", ("sonnet", "4", False)),
            ("anthropic.claude-haiku-4-5-20251001-v1:0", ("haiku", "4.5", False)),
            ("Anthropic Claude Haiku 4.5", ("haiku", "4.5", False)),
            ("anthropic.claude-fable-5", ("fable", "5", False)),
            ("Anthropic Claude Sonnet 4.5 V1 1M Context Length", ("sonnet", "4.5", True)),
        ],
    )
    def test_match_key(self, text, expected):
        assert quotas.match_key(text) == expected

    def test_non_claude_has_no_key(self):
        assert quotas.match_key("Amazon Nova 2 Lite") is None
        assert quotas.match_key("amazon.nova-2-lite") is None

    def test_long_context_is_a_different_key(self):
        plain = quotas.match_key("Anthropic Claude Sonnet 4.5 V1")
        long_ctx = quotas.match_key("Anthropic Claude Sonnet 4.5 V1 1M Context Length")
        assert plain != long_ctx


class TestQuotaRow:
    def test_complete_needs_both(self):
        assert QuotaRow(region="us-east-1", tpm=1, tpd=1).complete is True
        assert QuotaRow(region="us-east-1", tpd=1).complete is False
        assert QuotaRow(region="us-east-1").complete is False

    def test_display_falls_back_to_model_id(self):
        """join 不上配额时不能显示成空白。"""
        row = QuotaRow(region="us-east-1", model_id="anthropic.claude-opus-9")
        assert row.display == "anthropic.claude-opus-9"

    def test_below_best_only_when_lower(self):
        assert QuotaRow(region="us-west-2", tpm=3, best_tpm=6).below_best is True
        assert QuotaRow(region="us-east-1", tpm=6, best_tpm=6).below_best is False
        assert QuotaRow(region="us-east-1", best_tpm=6).below_best is False

    def test_long_context_detected(self):
        assert QuotaRow(
            region="us-east-1", quota_model="Anthropic Claude Sonnet 4.5 V1 1M Context Length"
        ).is_long_context
        assert not QuotaRow(region="us-east-1", quota_model="Anthropic Claude Sonnet 4.5 V1").is_long_context


class TestFetchRegionQuotas:
    def test_pairs_tpm_and_tpd(self, ledger, accounts, fake_aws):
        pairs, error = fetch_region_quotas(accounts[0], "us-east-1")
        assert error is None
        opus = pairs["Anthropic Claude Opus 4.8"]
        assert opus.tpd == 43_200_000_000
        assert opus.tpm == 30_000_000
        assert opus.tpd_code == "L-DAY48"
        assert opus.tpm_code == "L-MIN48"

    def test_only_claude(self, ledger, accounts, fake_aws):
        pairs, _ = fetch_region_quotas(accounts[0], "us-east-1")
        assert not any("Nova" in name for name in pairs)

    def test_ignores_other_bedrock_quotas(self, ledger, accounts, fake_aws):
        """只要这两类 token 配额，批处理大小、RPM 之类的一律不要。"""
        pairs, _ = fetch_region_quotas(accounts[0], "us-east-1")
        assert set(pairs) == {
            "Anthropic Claude Opus 4.8",
            "Anthropic Claude Opus 4.6 V1",
            "Anthropic Claude Sonnet 4.5 V1 1M Context Length",
            "Anthropic Claude Sonnet 4 V1",
        }

    def test_adjustability_is_tracked_per_quota_not_merged(self, ledger, accounts, fake_aws):
        """实测 TPD 改不了、TPM 能申请提额。合成一个字段会把 TPD 也说成能调。"""
        pairs, _ = fetch_region_quotas(accounts[0], "us-east-1")
        opus = pairs["Anthropic Claude Opus 4.8"]
        assert opus.tpd_adjustable is False
        assert opus.tpm_adjustable is True

    def test_regions_can_differ(self, ledger, accounts, fake_aws):
        """提额按区批，us-east-1 提了、别的区没提——这一页存在的理由。"""
        east, _ = fetch_region_quotas(accounts[0], "us-east-1")
        west, _ = fetch_region_quotas(accounts[0], "us-west-2")
        assert east["Anthropic Claude Opus 4.6 V1"].tpm == 6_000_000
        assert west["Anthropic Claude Opus 4.6 V1"].tpm == 3_000_000

    def test_asks_for_a_big_page(self, ledger, accounts, fake_aws):
        """默认分页每页只有 8 条，1162 条要发 146 次请求，必须显式设 PageSize。"""
        fetch_region_quotas(accounts[0], "us-east-1")
        key = ("service-quotas", "us-east-1", "111111111111")
        kwargs = fake_aws[key].paginator.seen_kwargs
        assert kwargs["PaginationConfig"]["PageSize"] == quotas.PAGE_SIZE
        assert quotas.PAGE_SIZE >= 100

    def test_cache_is_per_region(self, ledger, accounts, fake_aws, monkeypatch):
        """缓存键必须带区域，否则四个区会互相顶掉、全变成第一个区的值。"""
        fetch_region_quotas(accounts[0], "us-east-1")
        fetch_region_quotas(accounts[0], "us-west-2")

        def explode(*a, **k):
            raise AssertionError("命中缓存时不该再调 AWS")

        monkeypatch.setattr(quotas.boto3, "client", explode)
        east, _ = fetch_region_quotas(accounts[0], "us-east-1")
        west, _ = fetch_region_quotas(accounts[0], "us-west-2")
        assert east["Anthropic Claude Opus 4.6 V1"].tpm == 6_000_000
        assert west["Anthropic Claude Opus 4.6 V1"].tpm == 3_000_000

    def test_api_failure_is_returned_not_raised(self, ledger, accounts, monkeypatch):
        def broken(*a, **k):
            raise RuntimeError("AccessDeniedException: no servicequotas permission")

        monkeypatch.setattr(quotas.boto3, "client", broken)
        quotas.clear_cache()
        pairs, error = fetch_region_quotas(accounts[0], "us-east-1")
        assert pairs == {}
        assert error and "servicequotas" in error

    def test_error_does_not_leak_the_key(self, ledger, accounts, monkeypatch):
        def broken(*a, **k):
            raise RuntimeError(f"bad key {accounts[0].ak}")

        monkeypatch.setattr(quotas.boto3, "client", broken)
        quotas.clear_cache()
        _pairs, error = fetch_region_quotas(accounts[0], "us-east-1")
        assert accounts[0].ak not in error


class TestFetchAppProfiles:
    def test_parses_the_summary(self, ledger, accounts, fake_aws):
        profiles, error = fetch_app_profiles(accounts[0], "us-east-1")
        assert error is None
        first = profiles[0]
        assert first.profile_id == profile_id_for("us-east-1", 0)
        assert first.arn.startswith("arn:aws:bedrock:us-east-1:")
        assert first.arn.endswith(first.profile_id)
        assert first.name == "claude48oupsauto_0706"
        assert first.model_id == "anthropic.claude-opus-4-8"
        assert first.region == "us-east-1"

    def test_each_region_has_its_own_ids(self, ledger, accounts, fake_aws):
        """应用配置按区创建，四个区的 12 位 ID 互不相同。"""
        east, _ = fetch_app_profiles(accounts[0], "us-east-1")
        west, _ = fetch_app_profiles(accounts[0], "us-west-2")
        assert {p.profile_id for p in east}.isdisjoint({p.profile_id for p in west})

    def test_denied_is_returned_not_raised(self, ledger, accounts, fake_aws_no_profiles):
        profiles, error = fetch_app_profiles(accounts[0], "us-east-1")
        assert profiles == []
        assert error and "service control policy" in error

    def test_denied_error_does_not_leak_the_key(self, ledger, accounts, monkeypatch):
        def broken(*a, **k):
            raise RuntimeError(f"denied for {accounts[0].ak}")

        monkeypatch.setattr(quotas.boto3, "client", broken)
        quotas.clear_cache()
        _profiles, error = fetch_app_profiles(accounts[0], "us-east-1")
        assert accounts[0].ak not in error


class TestArnDrivenReport:
    def test_one_row_per_arn(self, ledger, accounts, fake_aws):
        report = build_quota_report([accounts[0]])
        assert report.arn_driven is True
        # 每个区 2 条 Claude 配置（第三条是 Nova，被排除）
        assert len(report.rows) == 2 * len(REGIONS)
        assert report.profile_count == len(report.rows)
        assert len({r.profile_id for r in report.rows}) == len(report.rows)

    def test_only_claude_profiles(self, ledger, accounts, fake_aws):
        report = build_quota_report([accounts[0]])
        assert not any("nova" in r.model_id.lower() for r in report.rows)

    def test_model_id_comes_from_the_arn(self, ledger, accounts, fake_aws):
        """模型 ID 是配置自己声明的 modelArn，不是从配额名拼的。"""
        report = build_quota_report([accounts[0]])
        row = next(r for r in report.rows if r.display == "Claude Opus 4.6 V1")
        assert row.model_id == "anthropic.claude-opus-4-6-v1"

    def test_model_name_comes_from_the_quota_not_the_profile_name(
        self, ledger, accounts, fake_aws
    ):
        """配置名是账号自己起的，和模型名毫无关系。"""
        report = build_quota_report([accounts[0]])
        row = next(r for r in report.rows if r.model_id == "anthropic.claude-opus-4-6-v1")
        assert row.display == "Claude Opus 4.6 V1"
        assert row.profile_name == "claude46Oupsauto_wjc_0529"

    def test_quota_follows_the_row_region(self, ledger, accounts, fake_aws):
        """每行的 TPM/TPD 必须取它自己那个区的值，不能拿一个区的顶四个区。"""
        report = build_quota_report([accounts[0]])
        by_region = {
            r.region: r
            for r in report.rows
            if r.model_id == "anthropic.claude-opus-4-6-v1"
        }
        assert by_region["us-east-1"].tpm == 6_000_000
        assert by_region["us-east-2"].tpm == 3_000_000
        assert by_region["us-west-2"].tpm == 3_000_000

    def test_lagging_regions_are_flagged(self, ledger, accounts, fake_aws):
        report = build_quota_report([accounts[0]])
        lagging = {r.region for r in report.lagging_rows}
        assert lagging == {"us-east-2", "us-west-1", "us-west-2"}
        assert report.lagging_models == ["Claude Opus 4.6 V1"]
        for row in report.lagging_rows:
            assert row.best_region == "us-east-1"
            assert row.best_tpm == 6_000_000

    def test_uniform_model_is_not_flagged(self, ledger, accounts, fake_aws):
        report = build_quota_report([accounts[0]])
        opus48 = [r for r in report.rows if r.display == "Claude Opus 4.8"]
        assert len(opus48) == len(REGIONS)
        assert not any(r.below_best for r in opus48)

    def test_regions_of_a_model_stay_adjacent(self, ledger, accounts, fake_aws):
        """同一个模型的四个区必须挨着，区域差异才看得出来。"""
        report = build_quota_report([accounts[0]])
        names = [r.display for r in report.rows]
        for name in set(names):
            first, last = names.index(name), len(names) - 1 - names[::-1].index(name)
            assert last - first + 1 == names.count(name)

    def test_regions_in_fixed_order_within_a_model(self, ledger, accounts, fake_aws):
        report = build_quota_report([accounts[0]])
        group = [r.region for r in report.rows if r.display == "Claude Opus 4.8"]
        assert group == REGIONS

    def test_groups_sorted_by_quota_desc(self, ledger, accounts, fake_aws):
        report = build_quota_report([accounts[0]])
        assert report.rows[0].display == "Claude Opus 4.8"  # 43.2B，最大

    def test_orphan_models_are_listed(self, ledger, accounts, fake_aws):
        """有配额但没建配置的模型不能悄悄消失——它们的流量不带标签。"""
        report = build_quota_report([accounts[0]])
        names = {o.name for o in report.orphan_models}
        assert names == {"Claude Sonnet 4.5 V1 1M Context Length", "Claude Sonnet 4 V1"}

    def test_orphans_carry_their_quota(self, ledger, accounts, fake_aws):
        report = build_quota_report([accounts[0]])
        item = next(o for o in report.orphan_models if o.name == "Claude Sonnet 4 V1")
        assert item.tpm == 200_000
        assert item.tpd == 288_000_000

    def test_unmatched_profile_still_shows(self, ledger, accounts, monkeypatch):
        """配额清单里没有对应条目时，行要留着并标出来，不能整行丢掉。"""

        def brand_new(region, acct):
            return [
                {
                    "inferenceProfileId": "brandnew1234",
                    "inferenceProfileName": "claude9auto",
                    "inferenceProfileArn": (
                        f"arn:aws:bedrock:{region}:222222222222"
                        ":application-inference-profile/brandnew1234"
                    ),
                    "models": [
                        {
                            "modelArn": "arn:aws:bedrock:::foundation-model"
                            "/anthropic.claude-opus-9"
                        }
                    ],
                }
            ]

        monkeypatch.setattr(quotas, "QUOTA_REGIONS", ["us-east-1"])
        install(monkeypatch, profiles=brand_new)
        report = build_quota_report([accounts[0]])
        row = next(r for r in report.rows if r.profile_id == "brandnew1234")
        assert row.matched is False
        assert row.tpm is None
        assert row in report.unmatched_rows
        # join 不上也要有个能看的名字，不能空白
        assert row.display == "anthropic.claude-opus-9"


class TestDegradedReport:
    """读不到应用推理配置时，退回按「模型 × 区域」列配额。"""

    def test_falls_back_to_models(self, ledger, accounts, fake_aws_no_profiles):
        report = build_quota_report([accounts[0]])
        assert report.arn_driven is False
        assert report.rows
        assert all(not r.has_arn for r in report.rows)
        assert all(r.model_id == "" for r in report.rows)

    def test_keeps_the_region_dimension(self, ledger, accounts, fake_aws_no_profiles):
        report = build_quota_report([accounts[0]])
        opus = [r for r in report.rows if r.display == "Claude Opus 4.6 V1"]
        assert {r.region for r in opus} == set(REGIONS)
        assert next(r for r in opus if r.region == "us-east-1").tpm == 6_000_000

    def test_quota_numbers_are_unaffected(self, ledger, accounts, fake_aws_no_profiles):
        """service-quotas 是另一套权限，配额本身照常读得到。"""
        report = build_quota_report([accounts[0]])
        assert report.error is None
        assert any(r.tpm == 30_000_000 for r in report.rows)

    def test_reason_is_kept(self, ledger, accounts, fake_aws_no_profiles):
        """SCP 拒绝的原文里有策略线索，要留给用户。"""
        report = build_quota_report([accounts[0]])
        assert report.arn_error
        assert "service control policy" in report.arn_error

    def test_no_orphan_section(self, ledger, accounts, fake_aws_no_profiles):
        """降级时表里就是全部模型，「有配额没配置」这个概念不成立。"""
        report = build_quota_report([accounts[0]])
        assert report.orphan_models == []

    def test_models_missing_in_one_region_are_not_invented(
        self, ledger, accounts, fake_aws_no_profiles
    ):
        """us-west-1 没列出 Sonnet 4 V1，就不该给它编一行出来。"""
        report = build_quota_report([accounts[0]])
        sonnet4 = {r.region for r in report.rows if r.display == "Claude Sonnet 4 V1"}
        assert sonnet4 == {"us-east-1", "us-east-2", "us-west-2"}


class TestBuildQuotaReport:
    def test_labels_a_single_account(self, ledger, accounts, fake_aws):
        report = build_quota_report([accounts[0]])
        assert accounts[0].partner in report.account_label
        assert accounts[0].account in report.account_label
        assert report.account_count == 1

    def test_labels_all_accounts(self, ledger, accounts, fake_aws):
        report = build_quota_report(accounts)
        assert report.account_label == f"全部账号（{len(accounts)} 个）"
        assert report.account_count == len(accounts)

    @pytest.mark.parametrize("empty", [None, []])
    def test_no_account_gives_an_empty_report(self, empty):
        report = build_quota_report(empty)
        assert report.rows == []
        assert report.all_rows == []
        assert report.account_label == ""
        assert report.regions == quotas.QUOTA_REGIONS

    def test_refresh_bypasses_cache(self, ledger, accounts, fake_aws, monkeypatch):
        build_quota_report([accounts[0]])
        calls = []
        original = quotas.boto3.client
        monkeypatch.setattr(
            quotas.boto3, "client", lambda *a, **k: (calls.append(1), original(*a, **k))[1]
        )
        build_quota_report([accounts[0]], refresh=True)
        assert calls, "refresh=1 应该跳过缓存重新拉"

    def test_quota_failure_is_reported_not_raised(self, ledger, accounts, monkeypatch):
        def broken(*a, **k):
            raise RuntimeError("AccessDeniedException: servicequotas denied")

        monkeypatch.setattr(quotas.boto3, "client", broken)
        quotas.clear_cache()
        report = build_quota_report([accounts[0]])
        assert report.rows == []
        assert report.error and "servicequotas" in report.error
