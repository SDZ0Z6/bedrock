"""AWS 报错的解释：被拒的是哪个动作、被谁拒的、凭证擦干净、str() 还是原来那一行。

起因是配额页把 bedrock:ListInferenceProfiles 被 SCP 显式拒绝，说成了「凭证缺少
ce:GetCostAndUsage 权限」。所以这里的重点是：动作名必须来自这次调用本身，拒绝来源要分得清。

不联网：异常要么直接构造（botocore 抛的就是这个 ClientError，操作名、错误码、原话都在里面），
要么用 botocore.stub.Stubber 挂在真的 boto3 client 上，把各模块真实的取数路径走一遍。
"""

from __future__ import annotations

import time
from datetime import date, datetime, timedelta, timezone

import boto3
import pytest
from botocore.exceptions import (
    ClientError,
    ConnectTimeoutError,
    EndpointConnectionError,
    NoCredentialsError,
    ReadTimeoutError,
)
from botocore.stub import Stubber
from jinja2 import Environment

from bedrock_cost import aws_errors, cost_estimate, cost_explorer, pricing, quotas, usage_explorer
from bedrock_cost import cloudwatch_metrics as cwm
from bedrock_cost.aws_errors import QueryError, as_query_error, describe
from bedrock_cost.excel_source import Account
from bedrock_cost.windows import MetricWindow

# 账号自己的 AK 故意不长成 AKIA… 的样子：这样能单独验证「按账号擦」那一步，而不是被
# 「按形状擦」顺手擦掉。SK 是 AWS 文档里的示例值。
OWN_AK = "TESTKEYJEFF0001"
OWN_SK = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
OTHER_AK = "AKIAIOSFODNN7EXAMPLE"   # 别人的、长得像 AK 的串（也是 AWS 文档里的示例）

ACCOUNT = Account(
    partner="Jeff", account="111122223333", budget=0, tag_ratio=1.0, untag_ratio=1.0,
    ak=OWN_AK, sk=OWN_SK,
)

USER = "User: arn:aws:iam::111122223333:user/reader is not authorized to perform:"
SCP_ARN = "arn:aws:organizations::999988887777:policy/o-abc123/service_control_policy/p-def456"
ENDPOINT = "https://bedrock.us-west-1.amazonaws.com/"
# 生产上实际碰到的那条：调的是 ListServiceQuotas，被拒的却是 bedrock 的动作
SCP_ON_PROFILES = (
    f"{USER} bedrock:ListInferenceProfiles on resource: "
    "arn:aws:bedrock:us-east-1:111122223333:inference-profile/* "
    f"with an explicit deny in a service control policy: {SCP_ARN}"
)


def client_error(code: str, message: str, operation: str, status: int = 400) -> ClientError:
    return ClientError(
        {"Error": {"Code": code, "Message": message}, "ResponseMetadata": {"HTTPStatusCode": status}},
        operation,
    )


def denied(action: str, why: str, code: str = "AccessDeniedException", operation: str = "") -> ClientError:
    """IAM 风格的拒绝原话。operation 不给就用动作名的后半截。"""
    return client_error(code, f"{USER} {action} {why}".strip(), operation or action.split(":")[1])


# ================================================================== 动作名
class TestActionNaming:
    def test_the_message_wins_over_the_operation(self):
        """生产上那一条：调的是 ListServiceQuotas，AWS 说被拒的是 bedrock:ListInferenceProfiles。"""
        error = describe(
            client_error("AccessDeniedException", SCP_ON_PROFILES, "ListServiceQuotas"),
            ACCOUNT, "us-east-1",
        )
        assert error.action == "bedrock:ListInferenceProfiles"
        assert error.reason == "被组织的 SCP（服务控制策略）显式拒绝 bedrock:ListInferenceProfiles"
        assert "ce:GetCostAndUsage" not in str(error)
        assert "ListServiceQuotas" in error.detail      # 原始报错里还看得出是哪个调用

    @pytest.mark.parametrize(
        "operation, action",
        [
            ("GetCostAndUsage", "ce:GetCostAndUsage"),
            ("GetMetricData", "cloudwatch:GetMetricData"),
            ("ListMetrics", "cloudwatch:ListMetrics"),
            ("ListServiceQuotas", "servicequotas:ListServiceQuotas"),
            ("ListInferenceProfiles", "bedrock:ListInferenceProfiles"),
            ("ListTagsForResource", "bedrock:ListTagsForResource"),
        ],
    )
    def test_falls_back_to_the_operation_name(self, operation, action):
        """原话里没写动作（老式的 UnauthorizedOperation 就不写），就按这次调用的操作名认。"""
        exc = client_error("UnauthorizedOperation", "You are not authorized to perform this operation.", operation)
        error = describe(exc, ACCOUNT)
        assert error.action == action
        assert error.reason == f"凭证缺少 {action} 权限"

    def test_unknown_operation_is_not_pinned_on_a_wrong_service(self):
        """认不出就说「这个操作」——以前在这里一律说成 ce:GetCostAndUsage。"""
        plain = describe(client_error("AccessDeniedException", "", "DescribeSomethingNew"), ACCOUNT)
        assert plain.action == ""
        assert plain.reason == "凭证缺少这个操作的权限"

        scp = describe(
            client_error("AccessDenied", "Denied with an explicit deny in a service control policy", "DescribeSomethingNew"),
            ACCOUNT,
        )
        assert scp.reason == "被组织的 SCP（服务控制策略）显式拒绝这个操作"
        assert "ce:" not in str(plain) + str(scp)


# ================================================================== 被谁拒的
class TestWhoDenied:
    @pytest.mark.parametrize(
        "why, reason, denied_by",
        [
            (
                f"with an explicit deny in a service control policy: {SCP_ARN}",
                "被组织的 SCP（服务控制策略）显式拒绝 cloudwatch:GetMetricData", "scp",
            ),
            (
                "because no service control policy allows the cloudwatch:GetMetricData action",
                "组织的 SCP（服务控制策略）没有放行 cloudwatch:GetMetricData", "scp",
            ),
            (
                "with an explicit deny in a resource control policy",
                "被组织的 RCP（资源控制策略）显式拒绝 cloudwatch:GetMetricData", "rcp",
            ),
            (
                "with an explicit deny in an identity-based policy",
                "被凭证所属用户 / 角色的 IAM 策略显式拒绝 cloudwatch:GetMetricData", "identity",
            ),
            (
                "because no identity-based policy allows the cloudwatch:GetMetricData action",
                "凭证缺少 cloudwatch:GetMetricData 权限", "identity",
            ),
            (
                "with an explicit deny in a permissions boundary",
                "被权限边界（permissions boundary）显式拒绝 cloudwatch:GetMetricData", "boundary",
            ),
            (
                "because no permissions boundary allows the cloudwatch:GetMetricData action",
                "权限边界（permissions boundary）没有放行 cloudwatch:GetMetricData", "boundary",
            ),
            (
                "with an explicit deny in a session policy",
                "被会话策略（session policy）显式拒绝 cloudwatch:GetMetricData", "session",
            ),
            (
                "with an explicit deny in a resource-based policy",
                "被资源上的策略（resource-based policy）显式拒绝 cloudwatch:GetMetricData", "resource",
            ),
            # 说了显式拒绝、没说是哪种策略
            ("with an explicit deny", "被一条策略显式拒绝 cloudwatch:GetMetricData", ""),
            # 老式的原话：只说没授权，没说为什么——就是缺权限
            ("on resource: arn:aws:cloudwatch:us-east-1:111122223333:*", "凭证缺少 cloudwatch:GetMetricData 权限", ""),
        ],
    )
    def test_names_the_policy_type(self, why, reason, denied_by):
        error = describe(denied("cloudwatch:GetMetricData", why, code="AccessDenied"), ACCOUNT, "us-east-1")
        assert error.kind == aws_errors.DENIED
        assert error.reason == reason
        assert error.denied_by == denied_by
        assert error.scp is (denied_by == "scp")
        assert error.code == "AccessDenied"

    def test_the_scp_policy_arn_is_kept(self):
        """SCP 的策略 ARN 是能直接拿去改的线索：拆出来，原话里也还在。"""
        why = f"with an explicit deny in a service control policy: {SCP_ARN}."
        error = describe(denied("bedrock:ListInferenceProfiles", why), ACCOUNT)
        assert error.policy == SCP_ARN
        assert SCP_ARN in error.detail

    def test_cost_explorer_not_enabled_is_not_called_a_permission_problem(self):
        exc = client_error("AccessDeniedException", "User not enabled for cost explorer access", "GetCostAndUsage")
        error = describe(exc, ACCOUNT)
        assert "还没有启用 Cost Explorer" in error.reason
        assert "缺少" not in error.reason

    def test_a_non_iam_denial_is_quoted_not_guessed(self):
        """不是 IAM 那套说法的拒绝，照抄原话，不往「缺权限」上猜。"""
        message = "You don't have access to the model with the specified model ID."
        error = describe(client_error("AccessDeniedException", message, "ListInferenceProfiles"), ACCOUNT)
        assert error.reason == f"AWS 拒绝了 bedrock:ListInferenceProfiles，原话是“{message}”"
        assert error.kind == aws_errors.DENIED
        # 原因里不能有全角冒号：str() 用它隔开「谁：原因」，页面上有按它拆老字符串的地方
        assert "：" not in error.reason


# ================================================================== 别的错误
class TestOtherErrors:
    @pytest.mark.parametrize(
        "code, reason",
        [
            ("InvalidClientTokenId", "AK 无效或已删除"),
            ("UnrecognizedClientException", "AK 无效或已删除"),       # JSON 协议的服务这么叫
            ("SignatureDoesNotMatch", "SK 不匹配，请检查台账里的密钥"),
            ("InvalidSignatureException", "SK 不匹配，请检查台账里的密钥"),
        ],
    )
    def test_credential_problems(self, code, reason):
        message = "The security token included in the request is invalid."
        error = describe(client_error(code, message, "GetCostAndUsage"), ACCOUNT)
        assert (error.reason, error.kind, error.code) == (reason, aws_errors.CREDENTIALS, code)

    def test_clock_skew_is_not_called_a_wrong_secret(self):
        message = "Signature expired: 20260101T000000Z is now earlier than ..."
        error = describe(client_error("InvalidSignatureException", message, "ListServiceQuotas"), ACCOUNT)
        assert "时钟" in error.reason
        assert "SK 不匹配" not in error.reason

    @pytest.mark.parametrize(
        "code, operation, reason",
        [
            # 原来 friendly_error 的说法，一字不改
            ("LimitExceededException", "GetCostAndUsage", "Cost Explorer 请求过于频繁，请稍后重试"),
            ("TooManyRequestsException", "ListServiceQuotas", "Service Quotas 请求过于频繁，请稍后重试"),
            ("Throttling", "GetMetricData", "CloudWatch 请求过于频繁，请稍后重试"),
            ("ThrottlingException", "SomethingNew", "AWS 请求过于频繁，请稍后重试"),
        ],
    )
    def test_throttling_names_the_service(self, code, operation, reason):
        error = describe(client_error(code, "Rate exceeded", operation), ACCOUNT)
        assert (error.reason, error.kind) == (reason, aws_errors.THROTTLED)

    @pytest.mark.parametrize(
        "code, reason",
        [
            ("DataUnavailableException", "该区间暂无成本数据"),
            ("RequestChangedException", "分页请求参数发生变化，请重试"),
        ],
    )
    def test_cost_explorer_hints_are_kept(self, code, reason):
        assert describe(client_error(code, "x", "GetCostAndUsage"), ACCOUNT).reason == reason

    def test_server_side_failures(self):
        known = describe(client_error("InternalFailure", "oops", "GetMetricData", status=500), ACCOUNT)
        by_status = describe(client_error("SomethingOdd", "oops", "GetMetricData", status=503), ACCOUNT)
        for error in (known, by_status):
            assert error.reason == "CloudWatch 服务端暂时出错，请稍后重试"
            assert error.kind == aws_errors.SERVICE

    def test_unknown_code_keeps_the_aws_message(self):
        error = describe(client_error("ValidationException", "1 validation error detected", "GetMetricData"), ACCOUNT)
        assert error.message == "1 validation error detected（ValidationException）"
        assert error.kind == aws_errors.OTHER

    @pytest.mark.parametrize(
        "exc, words, kind",
        [
            (EndpointConnectionError(endpoint_url=ENDPOINT), "连不上", aws_errors.NETWORK),
            (ConnectTimeoutError(endpoint_url=ENDPOINT, error="x"), "超时", aws_errors.NETWORK),
            (ReadTimeoutError(endpoint_url=ENDPOINT, error="x"), "读超时", aws_errors.NETWORK),
            (NoCredentialsError(), "凭证", aws_errors.CREDENTIALS),
        ],
    )
    def test_botocore_errors_get_a_network_sentence(self, exc, words, kind):
        error = describe(exc, ACCOUNT, "us-west-1")
        assert words in error.reason
        assert error.kind == kind
        assert error.code == ""
        assert error.detail.startswith(type(exc).__name__)   # 原始报错（含端点地址）在详情里

    def test_anything_else_is_type_and_message(self):
        error = describe(RuntimeError("boom"), ACCOUNT)
        assert (error.reason, error.detail, error.code) == ("RuntimeError: boom", "RuntimeError: boom", "")
        assert error.message == "RuntimeError: boom"


# ================================================================== 擦凭证
class TestRedaction:
    def test_keys_never_reach_the_page(self):
        # 没见过的错误码：原因就是 AWS 的原话，原话里的凭证也得擦
        message = f"Something odd with key {OTHER_AK} / {OWN_AK} / {OWN_SK}"
        error = describe(client_error("SomethingOdd", message, "GetCostAndUsage"), ACCOUNT)
        for text in (error.reason, error.detail, str(error)):
            for secret in (OTHER_AK, OWN_AK, OWN_SK):
                assert secret not in text
        assert error.reason == "Something odd with key [已隐藏] / [已隐藏] / [已隐藏]"
        assert aws_errors.HIDDEN in error.detail

    def test_non_aws_errors_are_scrubbed_too(self):
        error = describe(ValueError(f"bad secret {OWN_SK} for {OWN_AK}"), ACCOUNT)
        assert OWN_SK not in error.detail and OWN_AK not in error.detail
        assert OWN_SK not in error.reason

    def test_without_an_account_only_the_key_shape_is_scrubbed(self):
        assert aws_errors.redact(f"leaked {OTHER_AK} here") == "leaked [已隐藏] here"

    def test_cost_explorer_still_exports_redact(self):
        """redact 搬去了 aws_errors，老代码 from .cost_explorer import redact 还要能用。"""
        assert cost_explorer.redact(f"{OWN_SK} {OTHER_AK}", ACCOUNT) == "[已隐藏] [已隐藏]"


# ================================================================== str() 的样子
class TestStrShape:
    REASON = "凭证缺少 cloudwatch:GetMetricData 权限"

    @pytest.mark.parametrize(
        "where, text",
        [
            # 模型用量 / 预估成本
            ({"partner": "Jeff", "account": "111122223333", "region": "us-east-1"},
             "Jeff / 111122223333 @ us-east-1：凭证缺少 cloudwatch:GetMetricData 权限（AccessDenied）"),
            # 成本和使用情况（CE 不分区）
            ({"partner": "Jeff", "account": "111122223333"},
             "Jeff / 111122223333：凭证缺少 cloudwatch:GetMetricData 权限（AccessDenied）"),
            # 配额页（原来就不带上游）
            ({"account": "111122223333", "region": "us-east-1"},
             "111122223333 @ us-east-1：凭证缺少 cloudwatch:GetMetricData 权限（AccessDenied）"),
            # 单个账号自己的结果（HourUsage）只写区
            ({"region": "us-west-2"}, "us-west-2：凭证缺少 cloudwatch:GetMetricData 权限（AccessDenied）"),
            ({}, "凭证缺少 cloudwatch:GetMetricData 权限（AccessDenied）"),
        ],
    )
    def test_one_line(self, where, text):
        error = QueryError(reason=self.REASON, code="AccessDenied", **where)
        assert str(error) == f"{error}" == text

    def test_no_code_no_brackets(self):
        assert str(QueryError(account="111122223333", reason="台账中缺少 AK 或 SK，无法查询")) == (
            "111122223333：台账中缺少 AK 或 SK，无法查询"
        )

    @pytest.mark.parametrize(
        "exc",
        [
            denied("cloudwatch:GetMetricData", f"with an explicit deny in a service control policy: {SCP_ARN}"),
            denied("ce:GetCostAndUsage", "because no identity-based policy allows the ce:GetCostAndUsage action"),
            client_error("AccessDeniedException", "User not enabled for cost explorer access", "GetCostAndUsage"),
            client_error("AccessDeniedException", "Linked account doesn't have access to cost explorer.", "GetCostAndUsage"),
            client_error("UnrecognizedClientException", "invalid", "GetCostAndUsage"),
            client_error("TooManyRequestsException", "Rate exceeded", "ListServiceQuotas"),
            client_error("InternalFailure", "oops", "GetMetricData", status=500),
            client_error("RequestExpired", "Request has expired.", "GetMetricData"),
            EndpointConnectionError(endpoint_url=ENDPOINT),
        ],
    )
    def test_reasons_have_no_full_width_colon(self, exc):
        """str() 用全角冒号隔开「谁：原因」，页面上按第一个全角冒号拆老字符串（row.error）。"""
        assert "：" not in describe(exc, ACCOUNT, "us-east-1").reason

    def test_where_is_ready_for_a_toast(self):
        error = QueryError(partner="Jeff", account="111122223333", region="us-east-1", reason="x")
        assert error.where == "Jeff / 111122223333 @ us-east-1"

    def test_templates_print_the_same_line_escaped(self):
        """老模板写的是 {{ message }}，不改模板也得照样显示（并且照样转义）。"""
        error = QueryError(account="111122223333", region="us-east-1", reason="缺 <x> 权限", code="AccessDenied")
        html = Environment(autoescape=True).from_string("{{ e }}").render(e=error)
        assert html == "111122223333 @ us-east-1：缺 &lt;x&gt; 权限（AccessDenied）"


class TestAsQueryError:
    def test_wraps_plain_strings(self):
        """测试里替换取数函数时给的假错误、自己写的句子，都是字符串。"""
        error = as_query_error("该区不可用", account="111122223333", region="us-west-1", partner="Jeff")
        assert str(error) == "Jeff / 111122223333 @ us-west-1：该区不可用"

    def test_only_fills_what_is_given(self):
        base = describe(denied("ce:GetCostAndUsage", ""), ACCOUNT)
        error = as_query_error(base, partner="Jeff")
        assert (error.partner, error.account, error.region) == ("Jeff", "111122223333", "")
        assert error.reason == base.reason and error.detail == base.detail


class TestFriendlyError:
    """老接口：还是返回一行「原因（错误码）」，只是不再张冠李戴。"""

    def test_a_real_cost_explorer_denial_reads_as_before(self):
        exc = denied("ce:GetCostAndUsage", "because no identity-based policy allows the ce:GetCostAndUsage action")
        assert cost_explorer.friendly_error(exc, ACCOUNT) == "凭证缺少 ce:GetCostAndUsage 权限（AccessDeniedException）"

    def test_a_cloudwatch_denial_is_no_longer_blamed_on_cost_explorer(self):
        exc = denied("cloudwatch:GetMetricData", "with an explicit deny in a service control policy", code="AccessDenied")
        assert cost_explorer.friendly_error(exc, ACCOUNT) == (
            "被组织的 SCP（服务控制策略）显式拒绝 cloudwatch:GetMetricData（AccessDenied）"
        )

    def test_non_aws_errors(self):
        assert cost_explorer.friendly_error(RuntimeError("boom"), ACCOUNT) == "RuntimeError: boom"


# ================================================================== 各模块真实的取数路径
@pytest.fixture
def stubbed(monkeypatch, tmp_path):
    """真的 boto3 client 挂上 Stubber：请求在发出去之前就被截住，换成排好的响应或错误。

    开发机上的 AWS 配置一概不读（配置文件指到不存在的地方，环境变量里的凭证、profile 清掉），
    凭证是假的；Stubber 没排到的调用会直接报错，不会有请求真的发出去。
    """
    for name in (
        "AWS_PROFILE", "AWS_DEFAULT_PROFILE", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN", "AWS_DEFAULT_REGION", "AWS_REGION", "AWS_ENDPOINT_URL",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AWS_CONFIG_FILE", str(tmp_path / "no-aws-config"))
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(tmp_path / "no-aws-credentials"))
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    session = boto3.session.Session(
        aws_access_key_id="AKIAFAKESTUB00000000",
        aws_secret_access_key="stub-secret-not-real-0000000000000000000",
        region_name="us-east-1",
    )
    stubbers: list[Stubber] = []

    def make(service: str, region: str = "us-east-1"):
        client = session.client(service, region_name=region)
        stubber = Stubber(client)
        stubber.activate()
        stubbers.append(stubber)
        return client, stubber

    for module in (quotas, cwm, cost_estimate, cost_explorer, usage_explorer):
        module.clear_cache()
    yield make
    for stubber in stubbers:
        stubber.deactivate()


SCP_ON_METRICS = (
    f"{USER} cloudwatch:ListMetrics with an explicit deny in a service control policy: {SCP_ARN}"
)
DENIED_PROFILES = "被组织的 SCP（服务控制策略）显式拒绝 bedrock:ListInferenceProfiles（AccessDeniedException）"
DENIED_METRICS = "被组织的 SCP（服务控制策略）显式拒绝 cloudwatch:ListMetrics（AccessDenied）"
HOUR = datetime(2026, 9, 27, 5, tzinfo=timezone.utc)


class TestThroughTheDataLayers:
    def test_quota_page_names_the_real_action_and_the_scp(self, stubbed, monkeypatch):
        """生产上那一条的回归：以前这里写的是「凭证缺少 ce:GetCostAndUsage 权限」。"""
        monkeypatch.setattr(quotas, "QUOTA_REGIONS", ["us-east-1"])
        service_quotas, sq_stub = stubbed("service-quotas")
        sq_stub.add_client_error("list_service_quotas", "AccessDeniedException", SCP_ON_PROFILES)
        bedrock, br_stub = stubbed("bedrock")
        br_stub.add_client_error("list_inference_profiles", "AccessDeniedException", SCP_ON_PROFILES)
        clients = {"service-quotas": service_quotas, "bedrock": bedrock}
        monkeypatch.setattr(quotas, "_client", lambda account, service, region: clients[service])

        report = quotas.build_quota_report([ACCOUNT])

        sq_stub.assert_no_pending_responses()
        br_stub.assert_no_pending_responses()
        assert report.error == f"111122223333 @ us-east-1：{DENIED_PROFILES}"
        (problem,) = report.errors
        assert str(problem) == report.error
        assert problem.scp and problem.policy == SCP_ARN
        assert "ListServiceQuotas" in problem.detail
        # 推理配置那一路：页面上一句话，原话（带策略 ARN）在 arn_errors 里
        (arn_problem,) = report.arn_errors
        assert report.arn_error.endswith(f"111122223333：{DENIED_PROFILES}")
        assert "service control policy" in arn_problem.detail
        assert SCP_ARN not in report.arn_error

    def test_model_usage_report(self, stubbed, monkeypatch):
        cloudwatch, stub = stubbed("cloudwatch")
        stub.add_client_error("list_metrics", "AccessDenied", SCP_ON_METRICS, http_status_code=403)
        monkeypatch.setattr(cwm, "_client", lambda account, service, region: cloudwatch)
        now = datetime(2026, 9, 27, 12, tzinfo=timezone.utc)
        window = MetricWindow(start=now - timedelta(hours=6), end=now, period_key="1h")

        report = cwm.build_metrics([ACCOUNT], ["us-east-1"], window)

        (problem,) = report.errors
        assert str(problem) == f"Jeff / 111122223333 @ us-east-1：{DENIED_METRICS}"
        assert (problem.partner, problem.account, problem.region) == ("Jeff", "111122223333", "us-east-1")

    def test_hourly_invocations_stay_joinable_strings(self, stubbed, monkeypatch):
        """告警那边用「；」.join 拼这些，它们必须还是 str。"""
        cloudwatch, stub = stubbed("cloudwatch")
        stub.add_client_error("list_metrics", "AccessDenied", SCP_ON_METRICS, http_status_code=403)
        monkeypatch.setattr(cwm, "_client", lambda account, service, region: cloudwatch)

        counts, failed = cwm.hourly_invocations(ACCOUNT, [HOUR], regions=["us-east-1"])

        assert counts is None
        assert "；".join(failed) == f"us-east-1：{DENIED_METRICS}"

    def test_hour_usage_and_estimate_split(self, stubbed, monkeypatch):
        """HourUsage 给 QueryError（只写区，和原来一样）；SplitEstimate 仍是字符串，告警要拼。"""
        cloudwatch, stub = stubbed("cloudwatch")
        for _ in range(2):
            stub.add_client_error("list_metrics", "AccessDenied", SCP_ON_METRICS, http_status_code=403)
        monkeypatch.setattr(cwm, "_client", lambda account, service, region: cloudwatch)
        no_prices = pricing.PriceTable(prices={}, fetched_at=1.0)
        monkeypatch.setattr(cost_estimate, "load_prices", lambda force=False: no_prices)
        day = HOUR.date()

        usage = cost_estimate.hour_usage(ACCOUNT, HOUR, regions=["us-east-1"])
        (problem,) = usage.errors
        assert str(problem) == f"us-east-1：{DENIED_METRICS}"
        assert problem.account == "" and problem.scp

        split = cost_estimate.estimate_split(ACCOUNT, day, day, regions=["us-east-1"])
        assert split.errors == [f"us-east-1：{DENIED_METRICS}"]

    def test_cost_explorer_split_keeps_a_string_and_a_structure(self, stubbed, monkeypatch):
        ce, stub = stubbed("ce")
        stub.add_client_error(
            "get_cost_and_usage", "AccessDeniedException",
            f"{USER} ce:GetCostAndUsage on resource: arn:aws:ce:us-east-1:111122223333:/GetCostAndUsage "
            "because no identity-based policy allows the ce:GetCostAndUsage action",
        )
        monkeypatch.setattr(cost_explorer.boto3, "client", lambda *a, **k: ce)

        split = cost_explorer.fetch_split(ACCOUNT, date(2026, 9, 1), date(2026, 9, 30), refresh=True)

        assert split.error == "凭证缺少 ce:GetCostAndUsage 权限（AccessDeniedException）"
        assert split.problem.message == split.error
        assert (split.problem.account, split.problem.region) == ("111122223333", "")

    def test_cost_usage_report(self, stubbed, monkeypatch):
        ce, stub = stubbed("ce")
        stub.add_client_error("get_cost_and_usage", "LimitExceededException", "Rate exceeded")
        monkeypatch.setattr(usage_explorer.boto3, "client", lambda *a, **k: ce)

        report = usage_explorer.build_usage(
            [ACCOUNT], date(2026, 9, 1), date(2026, 9, 3), "service", "daily"
        )

        (problem,) = report.errors
        assert str(problem) == (
            "Jeff / 111122223333：Cost Explorer 请求过于频繁，请稍后重试（LimitExceededException）"
        )
        assert problem.kind == aws_errors.THROTTLED


class TestCostSplitProblem:
    START, MON, TUE = date(2026, 9, 1), date(2026, 9, 28), date(2026, 9, 29)

    def test_missing_credentials(self):
        keyless = Account(partner="Jeff", account="111122223333", budget=0, tag_ratio=1, untag_ratio=1)
        split = cost_explorer.fetch_split(keyless, self.START, self.MON)
        assert split.error == "台账中缺少 AK 或 SK，无法查询"
        assert split.problem.kind == aws_errors.CREDENTIALS

    def test_the_stale_fallback_keeps_the_problem(self, monkeypatch):
        """失败时顶上上一次的数，结构化的原因也要跟着带过去，页面才弹得出原因。"""
        calls = []

        def query(account, start, end):
            calls.append(end)
            if len(calls) == 1:
                return cost_explorer.CostSplit(tag_raw=1.0, untag_raw=2.0, fetched_at=time.time())
            raise client_error("ThrottlingException", "Rate exceeded", "GetCostAndUsage")

        monkeypatch.setattr(cost_explorer, "_query", query)
        cost_explorer.clear_cache()
        cost_explorer.fetch_split(ACCOUNT, self.START, self.MON)
        split = cost_explorer.fetch_split(ACCOUNT, self.START, self.TUE, refresh=True)

        assert split.stale_as_of == self.MON
        assert split.error == split.problem.message
        assert split.error == "Cost Explorer 请求过于频繁，请稍后重试（ThrottlingException）"
