"""AWS 调用失败的原因：结构化的 QueryError，以及把异常翻成人话的 describe。

以前各模块共用 cost_explorer.friendly_error，它把所有 AccessDenied 都说成「凭证缺少
ce:GetCostAndUsage 权限」——CloudWatch、Service Quotas、Bedrock 被拒也这么说。实际碰到过：
配额页写着缺 ce:GetCostAndUsage，AWS 的原话却是 bedrock:ListInferenceProfiles 被组织的 SCP
显式拒绝。换了个 API，原因也根本不是缺权限。所以这里分两步认：

  · 被拒的是哪个 IAM 动作：先看原话里的「not authorized to perform: <动作>」——最准，有的
    API 背后还会以调用者的身份去调别的服务，被拒的是那一个；原话里没有，再按
    ClientError.operation_name 查本项目发出的那几个调用；还认不出就说「这个操作」，
    宁可含糊，也不能点错服务。
  · 被谁拒的：SCP / RCP 是组织层面的，这个账号自己改不了；身份策略、权限边界、会话策略、
    资源策略是账号里的；原话只说没有放行、又没说是哪种策略，才算「缺权限」。

页面右上角的弹窗要分开显示「哪个账号哪个区」「一句原因」和能展开的原始报错，所以结果是
结构化的 QueryError。它的 str() 拼回以前那一行字，老模板和告警里的 f-string 不用改。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from botocore import exceptions as boto_errors
from botocore.exceptions import BotoCoreError, ClientError

if TYPE_CHECKING:
    from .excel_source import Account

# QueryError.kind 的取值。弹窗按它挑语气：权限和凭证要有人去改，限流、网络、服务端出错过一会儿
# 可能自己就好了
DENIED = "denied"            # 权限不够：IAM、SCP、权限边界……被谁拒的看 denied_by
CREDENTIALS = "credentials"  # AK / SK 本身不对、过期，或者台账里没填
THROTTLED = "throttled"      # 请求太频繁被限流
NETWORK = "network"          # 网络不通、超时、端点连不上
SERVICE = "service"          # AWS 那边出错（5xx）
OTHER = "other"

HIDDEN = "[已隐藏]"
MISSING_CREDENTIALS = "台账中缺少 AK 或 SK，无法查询"

_AK_PATTERN = re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{8,}\b")


@dataclass(frozen=True)
class QueryError:
    """一次查询失败：哪个账号、哪个区、一句原因、AWS 的原话。

    str() 是以前各模块拼的那一行字：「上游 / 账号 @ 区域：原因（错误码）」，没有的部分不写——
    Cost Explorer 不分区，就没有「@ 区域」；配额页原来的文案不带上游，就不填 partner。
    """

    account: str = ""     # 12 位账号 ID。单个账号自己的结果（HourUsage）里留空，和原来的文案一致
    region: str = ""      # Cost Explorer 这类全局服务为空
    reason: str = ""      # 一句人话，弹窗的正文。自己写的句子里不放全角冒号，str() 拿它隔开「谁：原因」
    detail: str = ""      # AWS 原始报错（错误码、操作名、原话），已擦掉凭证；「查看详情」里展开
    code: str = ""        # AWS 错误码，如 AccessDeniedException；不是 AWS 返回的错误时为空
    action: str = ""      # 出错的 IAM 动作，如 bedrock:ListInferenceProfiles；认不出时为空
    kind: str = OTHER     # 大类，见模块开头 DENIED / CREDENTIALS / THROTTLED / NETWORK / SERVICE / OTHER
    # 权限被哪种策略拒的：scp / rcp / identity / resource / boundary / session / endpoint，
    # AWS 原话没说清时为空
    denied_by: str = ""
    policy: str = ""      # 拒绝它的那条策略的 ARN，AWS 原话里带了才有——改策略的直接线索
    partner: str = ""     # 上游名，只在原来的文案带上游的地方填

    @property
    def scp(self) -> bool:
        """被组织的 SCP 拒了：这个账号里怎么改都没用，得找组织的管理员。"""
        return self.denied_by == "scp"

    @property
    def message(self) -> str:
        """「原因（错误码）」，也就是以前 friendly_error 返回的那一句。"""
        return f"{self.reason}（{self.code}）" if self.code else self.reason

    @property
    def where(self) -> str:
        """「上游 / 账号 @ 区域」，没有的部分不写。弹窗标题后面那半句可以直接用。"""
        who = " / ".join(part for part in (self.partner, self.account) if part)
        if not self.region:
            return who
        return f"{who} @ {self.region}" if who else self.region

    def __str__(self) -> str:
        where = self.where
        return f"{where}：{self.message}" if where else self.message


def redact(message: str, account: Account | None = None) -> str:
    """错误信息可能带上凭证片段，落到页面前先擦掉。

    先擦这个账号自己的 AK / SK，再按形状擦别的 AK：反过来的话，SK 里恰好有一段长得像 AK
    时会先被擦掉一截，整串 SK 就对不上、擦不掉了。
    """
    cleaned = message
    for secret in (getattr(account, "ak", ""), getattr(account, "sk", "")):
        if secret and len(secret) > 6:
            cleaned = cleaned.replace(secret, HIDDEN)
    return _AK_PATTERN.sub(HIDDEN, cleaned).strip()


def describe(exc: BaseException, account: Account | None = None, region: str = "") -> QueryError:
    """把一次 AWS 调用抛出的异常翻成 QueryError。

    account 有两个用处：标出是哪个账号，以及擦掉它自己的 AK / SK。region 是这次调用的区域，
    Cost Explorer 这类全局服务不传。
    """
    where = {"account": getattr(account, "account", "") or "", "region": region}
    if isinstance(exc, ClientError):
        return _client_error(exc, account, where)
    raw = redact(f"{type(exc).__name__}: {exc}", account)
    if isinstance(exc, BotoCoreError):
        reason, kind = _botocore_reason(exc)
        return QueryError(**where, reason=reason, detail=raw, kind=kind)
    # 不是 AWS 的错（多半是程序自己的问题）：没什么可解释的，类型和原话照写
    return QueryError(**where, reason=raw, detail=raw)


def as_query_error(
    error: QueryError | str, *, account: str = "", region: str = "", partner: str = ""
) -> QueryError:
    """取数函数报回来的错误统一成 QueryError，顺手补上是哪个账号、哪个区、哪个上游。

    给了的才覆盖，没给的保持原样。也收字符串：自己写的句子（台账缺凭证之类），以及测试里
    替换取数函数时给的假错误，整句当原因。
    """
    if not isinstance(error, QueryError):
        return QueryError(account=account, region=region, partner=partner, reason=str(error))
    changes = {
        name: value
        for name, value in (("account", account), ("region", region), ("partner", partner))
        if value
    }
    return replace(error, **changes) if changes else error


def missing_credentials(account: Account, region: str = "") -> QueryError:
    """台账里这个账号没填 AK 或 SK：不用发请求就知道查不了。"""
    return QueryError(
        account=account.account, region=region, reason=MISSING_CREDENTIALS, kind=CREDENTIALS
    )


# ----------------------------------------------------------------- ClientError
# 本项目实际发出的调用：boto 的操作名 -> IAM 动作。ClientError 只带操作名、不带服务名，所以
# 只收这几个。ListTagsForResource 好多服务都有，这里只有 Bedrock 的会被调到。没标模块的几个
# 现在没调用，是 quotas / pricing 模块开头讨论过的替代做法（单条配额点查、Price List API），
# 先登记上，哪天换过去也不会报成「这个操作」。
_ACTIONS = {
    "GetCostAndUsage": "ce:GetCostAndUsage",                 # cost_explorer、usage_explorer
    "ListMetrics": "cloudwatch:ListMetrics",                  # cloudwatch_metrics、cost_estimate
    "GetMetricData": "cloudwatch:GetMetricData",
    "ListServiceQuotas": "servicequotas:ListServiceQuotas",   # quotas
    "ListAWSDefaultServiceQuotas": "servicequotas:ListAWSDefaultServiceQuotas",
    "GetServiceQuota": "servicequotas:GetServiceQuota",
    "GetAWSDefaultServiceQuota": "servicequotas:GetAWSDefaultServiceQuota",
    "ListInferenceProfiles": "bedrock:ListInferenceProfiles",  # cloudwatch_metrics、quotas
    "ListTagsForResource": "bedrock:ListTagsForResource",      # cloudwatch_metrics
    "GetProducts": "pricing:GetProducts",
}

_SERVICE_NAMES = {
    "ce": "Cost Explorer",
    "cloudwatch": "CloudWatch",
    "servicequotas": "Service Quotas",
    "bedrock": "Bedrock",
    "pricing": "Price List",
}

_ACTION_IN_MESSAGE = re.compile(r"\bperform:\s*([a-z0-9-]+:[A-Za-z0-9*]+)")
# 「...with an explicit deny in a service control policy: arn:aws:organizations::…」
_POLICY_ARN = re.compile(r"(?:policy|boundary):\s*(arn:[^\s,;]+)", re.IGNORECASE)

_DENIED_CODES = frozenset({"AccessDenied", "AccessDeniedException", "UnauthorizedOperation"})

# AWS 原话里各种策略的叫法 -> (denied_by, 中文叫法)。显式拒绝写作
# 「with an explicit deny in a(n) <叫法>」，没放行写作「because no <叫法> allows the … action」
_POLICY_TYPES = (
    ("service control policy", "scp", "组织的 SCP（服务控制策略）"),
    ("resource control policy", "rcp", "组织的 RCP（资源控制策略）"),
    ("permissions boundary", "boundary", "权限边界（permissions boundary）"),
    ("session policy", "session", "会话策略（session policy）"),
    ("identity-based policy", "identity", "凭证所属用户 / 角色的 IAM 策略"),
    ("resource-based policy", "resource", "资源上的策略（resource-based policy）"),
    ("vpc endpoint policy", "endpoint", "VPC 终端节点策略"),
)

# Cost Explorer 自己的拒绝，不是 IAM 权限的事：照「缺权限」说会把人带去改错地方
_CE_DENIALS = (
    (("not enabled for cost explorer",), "这个账号还没有启用 Cost Explorer，要先在 Billing 控制台里开通"),
    (("linked account", "cost explorer"), "组织的管理账号没有给这个成员账号开放 Cost Explorer"),
)

# 凭证本身的问题。JSON 协议的服务（ce、servicequotas、bedrock）和 Query 协议的（cloudwatch）
# 对同一件事用的错误码不一样，两套都认
_CREDENTIAL_HINTS = {
    "InvalidClientTokenId": "AK 无效或已删除",
    "UnrecognizedClientException": "AK 无效或已删除",
    "SignatureDoesNotMatch": "SK 不匹配，请检查台账里的密钥",
    "InvalidSignatureException": "SK 不匹配，请检查台账里的密钥",
    "ExpiredToken": "临时凭证已过期",
    "ExpiredTokenException": "临时凭证已过期",
}
# 时钟差太多时 AWS 也报签名错误，但这时 SK 是对的，说成「SK 不匹配」会把人带偏
_CLOCK_CODES = frozenset({"RequestExpired", "RequestTimeTooSkewed"})

_THROTTLE_CODES = frozenset({
    "Throttling", "ThrottlingException", "ThrottledException", "TooManyRequestsException",
    "RequestLimitExceeded", "LimitExceededException", "RequestThrottled",
    "RequestThrottledException", "SlowDown",
})

_SERVER_CODES = frozenset({
    "InternalFailure", "InternalError", "InternalServerError", "InternalServerException",
    "ServiceException", "ServiceUnavailable", "ServiceUnavailableException", "Unavailable",
})

# Cost Explorer 的另外两种，原来 friendly_error 里就有的说法
_CE_HINTS = {
    "DataUnavailableException": "该区间暂无成本数据",
    "RequestChangedException": "分页请求参数发生变化，请重试",
}


def _client_error(exc: ClientError, account: Account | None, where: dict) -> QueryError:
    error = exc.response.get("Error") or {}
    code = str(error.get("Code") or "")
    message = redact(str(error.get("Message") or ""), account)
    lowered = message.lower()
    action = _action_in(message) or _ACTIONS.get(getattr(exc, "operation_name", "") or "", "")
    detail = redact(str(exc), account)

    def make(reason: str, kind: str, **extra) -> QueryError:
        return QueryError(
            **where, reason=reason, detail=detail, code=code, action=action, kind=kind, **extra
        )

    if code in _DENIED_CODES or "not authorized" in lowered or "explicit deny" in lowered:
        reason, denied_by = _denial(message, lowered, action)
        return make(reason, DENIED, denied_by=denied_by, policy=_policy_in(message))
    if code in _CLOCK_CODES or "signature expired" in lowered:
        return make("服务器时钟和 AWS 相差太多，请求签名过期了，请校准系统时间", OTHER)
    if code in _CREDENTIAL_HINTS:
        return make(_CREDENTIAL_HINTS[code], CREDENTIALS)
    if code in _THROTTLE_CODES:
        return make(f"{_service(action)} 请求过于频繁，请稍后重试", THROTTLED)
    if code in _CE_HINTS:
        return make(_CE_HINTS[code], OTHER)
    status = (exc.response.get("ResponseMetadata") or {}).get("HTTPStatusCode")
    if code in _SERVER_CODES or (isinstance(status, int) and status >= 500):
        return make(f"{_service(action)} 服务端暂时出错，请稍后重试", SERVICE)
    # 没见过的错误码：AWS 的原话比任何猜测都准
    return make(message or f"AWS 返回了 {code or '一个没有说明的错误'}", OTHER)


def _denial(message: str, lowered: str, action: str) -> tuple[str, str]:
    """权限被拒时的那句话，以及被哪种策略拒的。"""
    for needle, denied_by, label in _POLICY_TYPES:
        if re.search(rf"explicit deny in an? {re.escape(needle)}", lowered):
            return _naming(f"被{label}显式拒绝", action), denied_by
        if f"no {needle} allows" in lowered:
            # 身份策略里没写放行 = 最常见的「缺权限」
            if denied_by == "identity":
                return _missing(action), denied_by
            return _naming(f"{label}没有放行", action), denied_by
    if "explicit deny" in lowered:
        return _naming("被一条策略显式拒绝", action), ""
    if "not authorized" in lowered or not message:
        return _missing(action), ""
    for needles, reason in _CE_DENIALS:
        if all(needle in lowered for needle in needles):
            return reason, ""
    # 不是 IAM 那套说法：照抄原话，不往「缺权限」上猜。原因里不用全角冒号——str() 拿它分隔
    # 「谁：原因」，页面上还有按第一个全角冒号拆老字符串的地方
    target = f" {action}" if action else "这次请求"
    return f"AWS 拒绝了{target}，原话是“{message}”", ""


def _naming(text: str, action: str) -> str:
    """「……拒绝」后面接上动作名：英文动作名前空一格，认不出动作就说「这个操作」。"""
    return f"{text} {action}" if action else f"{text}这个操作"


def _missing(action: str) -> str:
    return f"凭证缺少 {action} 权限" if action else "凭证缺少这个操作的权限"


def _action_in(message: str) -> str:
    found = _ACTION_IN_MESSAGE.search(message)
    return found.group(1) if found else ""


def _policy_in(message: str) -> str:
    found = _POLICY_ARN.search(message)
    return found.group(1).rstrip(".)") if found else ""


def _service(action: str) -> str:
    return _SERVICE_NAMES.get(action.partition(":")[0], "AWS") if action else "AWS"


# ----------------------------------------------------------------- BotoCoreError
def _botocore_reason(exc: BotoCoreError) -> tuple[str, str]:
    """请求没拿到 AWS 的回复：多半是网络，也可能是本机的凭证或 botocore 本身。

    子类要排在父类前面：ConnectTimeoutError、SSLError 都是 botocore 的 ConnectionError。
    """
    if isinstance(
        exc,
        (
            boto_errors.NoCredentialsError,
            boto_errors.PartialCredentialsError,
            boto_errors.CredentialRetrievalError,
        ),
    ):
        return "没拿到完整的凭证，台账里的 AK / SK 可能没填全", CREDENTIALS
    if isinstance(exc, boto_errors.ConnectTimeoutError):
        return "连接 AWS 超时（网络不通，或者被防火墙拦了）", NETWORK
    if isinstance(exc, boto_errors.ReadTimeoutError):
        return "AWS 迟迟没有响应（读超时），请稍后重试", NETWORK
    if isinstance(exc, boto_errors.ProxyConnectionError):
        return "连不上配置的代理服务器", NETWORK
    if isinstance(exc, boto_errors.SSLError):
        return "和 AWS 建立 TLS 连接失败（证书或代理的问题）", NETWORK
    if isinstance(exc, boto_errors.EndpointConnectionError):
        return "连不上 AWS 的服务端点（网络不通，或者这个区域没有这项服务）", NETWORK
    if isinstance(exc, (boto_errors.ConnectionError, boto_errors.HTTPClientError)):
        return "和 AWS 的连接出错，请稍后重试", NETWORK
    if isinstance(exc, boto_errors.DataNotFoundError):
        return "本机的 boto3 / botocore 太旧，不认识这项服务，需要升级", OTHER
    if isinstance(exc, boto_errors.ParamValidationError):
        return "请求参数没通过 botocore 的校验（程序的问题）", OTHER
    return "网络或凭证错误，没拿到 AWS 的响应", NETWORK
