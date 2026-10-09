"""Bedrock 的模型配额，按账号下的应用推理配置（ARN）逐条列。

表格主键是**应用推理配置的 ARN**——`bedrock:ListInferenceProfiles` 里
`typeEquals=APPLICATION` 那批，也就是账号自己建的、CloudWatch 里以 12 位不透明
ID 出现的那些。一条 ARN 一行，配上它的底层模型和**该区域**的 TPM / TPD。

实测确认的几件事：

  · 「Global cross-region ... tokens per minute / per day」这两类配额
    **不再四区相同**。Claude Opus 4.6 V1 在 us-east-1 是 6M TPM / 8.64B TPD，
    另外三个区都是 3M / 4.32B——提额只落在提交申请的那个区。所以四个区必须
    各查一次。（本页原先的做法是挑 us-east-1 问了当成全局值，已被真实数据推翻。）
  · 配额口径是 **input + output token 合计**（配额描述里写明了）。
  · **可调性是分开的**：日配额 Adjustable=False（改不了），分钟配额
    Adjustable=True（可以在 Service Quotas 里申请提额）。所以两条要各自记，
    合成一个字段的话「有一条能调」会被显示成「都能调」。
  · 应用推理配置是**按区域创建**的：同一个模型在四个区各有一条独立 ARN，
    12 位 ID 互不相同。所以 ARN 天然带区域维度，不用额外拼。
  · 配置名（`claude46Oupsauto_wjc_0529`）是账号自己起的，和模型名不是一回事，
    两个账号的命名规则也完全不同。所以模型名只从配额名来，绝不从配置名猜。
  · `bedrock:ListInferenceProfiles` **可能被组织 SCP 显式拒绝**（实测账号
    139675293794 就是，四个区全拒），这时退回按「模型 × 区域」列配额，
    ARN 和模型 ID 两列留空——service-quotas 是另一套权限，通常还是通的。
  · `bedrock:ListFoundationModels` 实测两个账号都被拒，所以拿不到 AWS 的官方
    模型名。「模型名称」一列取自配额名（`Anthropic Claude Opus 4.6 V1`）。

四个区一起拉要 45 秒左右，这个数字改不动，原因实测过：

  · `ListServiceQuotas` 被**按账号跨区限流**到大约 1 请求/秒。四个区共 40 页，
    所以串行 43.9 秒、并发 46.3 秒——**并发一点没快**。代码仍然并发发，
    因为这样四次 ListInferenceProfiles 能顺带跑完，不额外花时间。
  · 想走 `GetServiceQuota` 点查（QuotaCode 实测跨区完全一致，35/35）也不行：
    凭证只有 `servicequotas:ListServiceQuotas`，`GetServiceQuota` 被拒。
  · 限流会吃掉重试次数，所以 service-quotas 这条通路的 max_attempts 单独放宽，
    用默认的 3 次会让某个区直接变成错误、整列数据消失。

配额属于配置信息、几乎不变，所以缓存期给得很长（首次加载慢，之后 6 小时内秒开）。
"""

from __future__ import annotations

import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

import boto3
from botocore.config import Config as BotoConfig

from . import config
from .aws_errors import QueryError, as_query_error, describe
from .cloudwatch_metrics import REGIONS
from .excel_source import Account

SERVICE_CODE = "bedrock"

# 四个美区各查一次。挑一个区问已经不成立了——见模块开头 Opus 4.6 那条。
QUOTA_REGIONS = list(REGIONS)

QUOTA_DAY = re.compile(r"^Global cross-region model inference tokens per day for (.+)$")
QUOTA_MINUTE = re.compile(r"^Global cross-region model inference tokens per minute for (.+)$")

# 只看 Claude。配额侧按显示名前缀过滤，ARN 侧按模型 ID 里的厂商段过滤。
VENDOR_PREFIX = "Anthropic Claude"
VENDOR_SLUG = "anthropic"

# 配额几乎不变，缓存久一点，免得每次翻页都拉四个区一千多条
QUOTA_CACHE_TTL = 6 * 3600

# ListServiceQuotas 每页条数。不设的话 boto3 默认每页 8 条，要发 146 次请求。
PAGE_SIZE = 100


def display_name(quota_model: str) -> str:
    """去掉厂商前缀：Anthropic Claude Opus 4.6 V1 -> Claude Opus 4.6 V1。"""
    return quota_model.replace("Anthropic ", "").strip()


def base_model_id(model_arns: list[str]) -> str:
    """从应用配置的 modelArn 里取底层基础模型 ID。

    一条应用配置会带两条 modelArn：一条不带区域（跨区标记），一条带本区。
    两条指向的是同一个基础模型，取尾段结果一样，所以取第一条非空的即可。

        arn:aws:bedrock:::foundation-model/anthropic.claude-opus-4-6-v1
                                        -> anthropic.claude-opus-4-6-v1
    """
    for arn in model_arns:
        tail = arn.rsplit("/", 1)[-1].strip()
        if tail:
            return tail
    return ""


# --------------------------------------------------------------- 数据结构
@dataclass
class QuotaPair:
    """一个模型在一个区域的两条 Global cross-region 配额。"""

    tpm: float | None = None
    tpd: float | None = None
    tpm_code: str = ""
    tpd_code: str = ""
    tpm_adjustable: bool = False
    tpd_adjustable: bool = False


@dataclass
class AppProfile:
    """一条应用推理配置。region 取自查询它的那个区（等于 ARN 的第 4 段）。"""

    profile_id: str
    arn: str
    name: str
    region: str
    model_id: str


@dataclass
class QuotaRow:
    """表格的一行：某个账号下一条 ARN（降级时是一个模型）在某区域的 TPM / TPD。"""

    region: str
    account: str = ""          # 12 位账号号码，表格里显示的就是它
    account_key: str = ""      # Account.key，分组和取最高值用（号码理论上会重复）
    partner: str = ""          # 上游名，只用作账号列的悬浮提示
    quota_model: str = ""      # 配额里的原始显示名，join 上了才有
    model_id: str = ""         # anthropic.claude-opus-4-6-v1，从 ARN 拆出
    profile_id: str = ""       # obc6dxcudai0
    profile_arn: str = ""
    profile_name: str = ""     # 账号自己起的配置名
    tpm: float | None = None
    tpd: float | None = None
    tpm_code: str = ""
    tpd_code: str = ""
    tpm_adjustable: bool = False
    tpd_adjustable: bool = False
    # 该模型在四个区里的最高 TPM / TPD，以及取到最高 TPM 的那个区。
    # 用来标出「这个区没跟上提额」——提额是按区批的，不会自动铺开。
    best_tpm: float | None = None
    best_tpd: float | None = None
    best_region: str = ""

    @property
    def display(self) -> str:
        """模型名称。配额名最可读，join 不上就退回模型 ID、再退回配置名。"""
        if self.quota_model:
            return display_name(self.quota_model)
        return self.model_id or self.profile_name

    @property
    def has_arn(self) -> bool:
        return bool(self.profile_id)

    @property
    def matched(self) -> bool:
        """在该区的配额清单里找到了对应条目。"""
        return bool(self.quota_model)

    @property
    def complete(self) -> bool:
        """TPM 和 TPD 都读到了。缺一条说明 AWS 那边没列出来，值得标出来。"""
        return self.tpm is not None and self.tpd is not None

    @property
    def is_long_context(self) -> bool:
        """1M 上下文是独立配额，标一下免得被当成同名模型的重复行。"""
        return "1m context" in self.quota_model.lower()

    @property
    def below_best(self) -> bool:
        """本区配额低于该模型在其他区的最高值。"""
        return (
            self.tpm is not None
            and self.best_tpm is not None
            and self.tpm < self.best_tpm
        )


@dataclass
class OrphanModel:
    """配额清单里有、但账号下没有对应应用推理配置的模型。

    走这些模型的调用只能直连，不经过带标签的配置，
    在「模型用量」页会算成无标签流量。
    """

    name: str
    account: str = ""
    tpm: float | None = None
    tpd: float | None = None


@dataclass
class QuotaReport:
    account_label: str = ""
    account_count: int = 0
    # rows 是**筛选后**的，模板直接用；all_rows 未筛选，筛选下拉的选项从它来，
    # 「本区没跟上提额」也必须按未筛选的数据算——只筛出一个区的话，那个区
    # 自己就是最高值，标黄会全部消失。
    rows: list[QuotaRow] = field(default_factory=list)
    all_rows: list[QuotaRow] = field(default_factory=list)
    regions: list[str] = field(default_factory=list)
    orphan_models: list[OrphanModel] = field(default_factory=list)
    model_filter: str = ""
    region_filter: str = ""
    # True = 至少有一个账号读到了 ARN。多账号时可能是混合的：一个账号读得到、
    # 另一个被 SCP 拒，那就一张表里两种行并存。
    arn_driven: bool = False
    # 下面两句是给页面直接显示的一行字（最多举两个账号）；逐条的原因在 arn_errors / errors 里
    arn_error: str | None = None      # 读不到应用推理配置的原因（带账号）
    error: str | None = None          # 读不到 Service Quotas 的原因（带账号）
    # 每个失败的 (账号, 区域) 一条，str() 是「账号 @ 区域：原因（错误码）」，detail 里是 AWS
    # 原话——SCP 拒绝时原话带着策略 ARN（也拆到了 policy 里），是能直接拿去改的线索
    errors: list[QueryError] = field(default_factory=list)       # Service Quotas
    arn_errors: list[QueryError] = field(default_factory=list)   # 应用推理配置

    def apply_filters(self, model: str = '', region: str = '') -> None:
        """按模型 / 区域裁 rows。

        all_rows 一动不动：筛选下拉的选项要列全部模型，「本区没跟上提额」也必须
        按四个区的全量算——只留一个区的话，那个区自己就是最高值，标黄会消失。
        """
        self.model_filter = model
        self.region_filter = region
        keep = self.all_rows
        if model:
            keep = [r for r in keep if r.display == model]
        if region:
            keep = [r for r in keep if r.region == region]
        self.rows = keep

    @property
    def is_filtered(self) -> bool:
        return bool(self.model_filter or self.region_filter)

    @property
    def model_options(self) -> list[str]:
        """模型筛选的选项。按表格自己的顺序（配额从大到小），不按字母。"""
        seen: dict[str, None] = {}
        for row in self.all_rows:
            if row.display:
                seen.setdefault(row.display, None)
        return list(seen)

    @property
    def region_options(self) -> list[str]:
        return list(self.regions)

    @property
    def profile_count(self) -> int:
        return len({r.profile_arn for r in self.rows if r.profile_arn})

    @property
    def model_count(self) -> int:
        return len({r.display for r in self.rows if r.display})

    @property
    def incomplete_rows(self) -> list[QuotaRow]:
        return [r for r in self.rows if not r.complete]

    @property
    def unmatched_rows(self) -> list[QuotaRow]:
        """有 ARN 但配额清单里查不到对应条目的行。"""
        return [r for r in self.rows if r.has_arn and not r.matched]

    @property
    def lagging_rows(self) -> list[QuotaRow]:
        """配额低于同模型其他区最高值的行——提额没铺开的那些。"""
        return [r for r in self.rows if r.below_best]

    @property
    def lagging_models(self) -> list[str]:
        seen: dict[str, None] = {}
        for row in self.lagging_rows:
            seen.setdefault(row.display, None)
        return list(seen)


# --------------------------------------------------------------- 缓存
_lock = threading.Lock()
_cache: dict[tuple, tuple[float, object]] = {}


def clear_cache() -> None:
    with _lock:
        _cache.clear()


def _cached(key: tuple):
    """命中且未过期就返回缓存值，否则 None。CACHE_TTL=0 表示整体关掉缓存。"""
    if config.CACHE_TTL <= 0:
        return None
    with _lock:
        hit = _cache.get(key)
    if hit and time.time() - hit[0] < QUOTA_CACHE_TTL:
        return hit[1]
    return None


def _store(key: tuple, value) -> None:
    if config.CACHE_TTL > 0:
        with _lock:
            _cache[key] = (time.time(), value)


# ListServiceQuotas 被按账号跨区限流，默认 3 次重试不够用：某一页被限流打穿
# 之后整个区就变成错误，页面上会少掉一整列数据。放宽到 8 次，多等几秒也认了。
QUOTA_RETRIES = 8


def _client(account: Account, service: str, region: str):
    attempts = QUOTA_RETRIES if service == "service-quotas" else config.CE_RETRIES
    return boto3.client(
        service,
        region_name=region,
        aws_access_key_id=account.ak,
        aws_secret_access_key=account.sk,
        config=BotoConfig(
            read_timeout=config.CE_TIMEOUT,
            connect_timeout=config.CE_TIMEOUT,
            retries={"max_attempts": max(attempts, config.CE_RETRIES), "mode": "standard"},
        ),
    )


# --------------------------------------------------------------- 取配额
def fetch_region_quotas(
    account: Account, region: str
) -> tuple[dict[str, QuotaPair], QueryError | None]:
    """某个区域的 Claude token 配额：配额显示名 -> QuotaPair。"""
    key = (account.ak[-6:], account.account, region, "quotas")
    hit = _cached(key)
    if hit is not None:
        return dict(hit), None

    found: dict[str, QuotaPair] = {}
    try:
        client = _client(account, "service-quotas", region)
        # 必须显式设 PageSize：boto3 的默认分页每页只有 8 条，1162 条配额要发
        # 146 次请求（实测 54 秒）；设成 100 之后是 30 页 12 秒，快 4 倍多
        for page in client.get_paginator("list_service_quotas").paginate(
            ServiceCode=SERVICE_CODE,
            PaginationConfig={"PageSize": PAGE_SIZE},
        ):
            for quota in page.get("Quotas", []):
                name = quota.get("QuotaName", "")
                day = QUOTA_DAY.match(name)
                minute = QUOTA_MINUTE.match(name)
                if not (day or minute):
                    continue
                model = (day or minute).group(1)
                if not model.startswith(VENDOR_PREFIX):
                    continue
                # 同一个模型的日配额和分钟配额是两条独立记录，按显示名归并
                pair = found.setdefault(model, QuotaPair())
                value = float(quota.get("Value") or 0)
                adjustable = bool(quota.get("Adjustable"))
                if day:
                    pair.tpd = value
                    pair.tpd_code = quota.get("QuotaCode", "")
                    pair.tpd_adjustable = adjustable
                else:
                    pair.tpm = value
                    pair.tpm_code = quota.get("QuotaCode", "")
                    pair.tpm_adjustable = adjustable
    except Exception as exc:
        return {}, describe(exc, account, region)

    _store(key, found)
    return dict(found), None


def fetch_app_profiles(
    account: Account, region: str
) -> tuple[list[AppProfile], QueryError | None]:
    """某个区域的应用推理配置。读不到就把原因带回去，让上层决定降级。

    错误原文要留着：SCP 拒绝的消息里带策略 ID，是能直接拿去改的线索。它在 QueryError 的
    detail（策略 ARN 另外拆在 policy）里，页面上那一句只说被谁拒了哪个动作。
    """
    key = (account.ak[-6:], account.account, region, "app-profiles")
    hit = _cached(key)
    if hit is not None:
        return list(hit), None

    profiles: list[AppProfile] = []
    try:
        client = _client(account, "bedrock", region)
        token = None
        while True:
            kwargs = {"maxResults": 100, "typeEquals": "APPLICATION"}
            if token:
                kwargs["nextToken"] = token
            page = client.list_inference_profiles(**kwargs)
            for summary in page.get("inferenceProfileSummaries", []):
                arn = summary.get("inferenceProfileArn", "")
                models = [
                    m.get("modelArn", "")
                    for m in summary.get("models", [])
                    if m.get("modelArn")
                ]
                profiles.append(
                    AppProfile(
                        profile_id=summary.get("inferenceProfileId", "")
                        or arn.rsplit("/", 1)[-1],
                        arn=arn,
                        name=summary.get("inferenceProfileName", ""),
                        region=region,
                        model_id=base_model_id(models),
                    )
                )
            token = page.get("nextToken")
            if not token:
                break
    except Exception as exc:
        return [], describe(exc, account, region)

    _store(key, profiles)
    return list(profiles), None


# --------------------------------------------------------------- join
# 配额名里只有显示名（Anthropic Claude Opus 4.6 V1），应用配置里只有基础模型 ID
# （anthropic.claude-opus-4-6-v1）。两边归到同一个 key 上做 join，不靠字符串拼。
FAMILY = re.compile(r"(opus|sonnet|haiku|fable)", re.I)


def match_key(text: str) -> tuple[str, str, bool] | None:
    """把配额显示名和模型 ID 归到同一个 join key。

        Anthropic Claude Opus 4.6 V1                   -> ("opus", "4.6", False)
        anthropic.claude-opus-4-6-v1                   -> ("opus", "4.6", False)
        anthropic.claude-sonnet-4-5-20250929-v1:0      -> ("sonnet", "4.5", False)
        ...Sonnet 4.5 V1 1M Context Length             -> ("sonnet", "4.5", True)

    先剥掉 8 位日期戳再抽版本号——不剥的话 claude-sonnet-4-20250514 会被读成
    版本 4.20250514。V1 后缀两边有时有有时没有，所以不参与 key。1M 上下文用
    第三个字段区分，它没有对应的应用配置，join 不上是正确结果。

    不用名字直接对：配置名是账号自己起的（claude46Oupsauto_wjc_0529），
    和模型名毫无关系，靠名字必错。
    """
    lowered = re.sub(r"[-_]?\d{8}", "", text.lower())
    family = FAMILY.search(lowered)
    if not family:
        return None
    version = re.search(r"(\d+(?:[.\-]\d+)?)", lowered[family.end():])
    if not version:
        return None
    return (family.group(1), version.group(1).replace("-", "."), "1m" in lowered)


# --------------------------------------------------------------- 组装报表
def _fetch_all(accounts: list[Account]) -> dict:
    """并发拉每个账号 × 每个区的配额和应用配置。

    限流是**按账号**的，所以跨账号并发是真的有效：两个账号一起拉，墙上时间
    和一个账号差不多。同一账号内的四个区并发则没有收益（见模块开头），但也
    不亏，而且能让四次 ListInferenceProfiles 顺带跑完。

    配额任务排在前面：worker 有限时，先让慢的那批占住线程，快的排后面填空隙。
    """
    tasks = [("quotas", a, r) for a in accounts for r in QUOTA_REGIONS]
    tasks += [("profiles", a, r) for a in accounts for r in QUOTA_REGIONS]

    def run(task):
        kind, account, region = task
        if kind == "quotas":
            return kind, account.key, region, fetch_region_quotas(account, region)
        return kind, account.key, region, fetch_app_profiles(account, region)

    out = {
        a.key: {"quotas": {}, "profiles": {}, "quota_errors": [], "arn_errors": []}
        for a in accounts
    }
    if not tasks:
        return out

    # 决定墙上时间的是「有多少个配额列举能同时跑」，所以 worker 数至少要够
    # 覆盖 账号数 × 区域数，不能被 MAX_WORKERS（那是给 CE 调的）卡住
    workers = min(len(tasks), max(config.MAX_WORKERS, len(accounts) * len(QUOTA_REGIONS)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for kind, key, region, (payload, error) in pool.map(run, tasks):
            bucket = out[key]
            if kind == "quotas":
                bucket["quotas"][region] = payload
                if error:
                    bucket["quota_errors"].append((region, error))
            else:
                bucket["profiles"][region] = payload
                if error:
                    bucket["arn_errors"].append((region, error))
    return out


def _arn_rows(
    account: Account,
    quota_by_region: dict[str, dict[str, QuotaPair]],
    profiles_by_region: dict[str, list[AppProfile]],
) -> list[QuotaRow]:
    """一条应用推理配置一行，TPM / TPD 取它自己那个区的值。"""
    rows: list[QuotaRow] = []
    for region in QUOTA_REGIONS:
        pairs = quota_by_region.get(region, {})
        by_key = {match_key(m): m for m in pairs if match_key(m)}
        for profile in profiles_by_region.get(region, []):
            if VENDOR_SLUG not in profile.model_id.lower():
                continue  # 只看 Claude
            quota_model = by_key.get(match_key(profile.model_id), "")
            pair = pairs.get(quota_model) or QuotaPair()
            rows.append(
                QuotaRow(
                    region=region,
                    account=account.account,
                    account_key=account.key,
                    partner=account.partner,
                    quota_model=quota_model,
                    model_id=profile.model_id,
                    profile_id=profile.profile_id,
                    profile_arn=profile.arn,
                    profile_name=profile.name,
                    tpm=pair.tpm,
                    tpd=pair.tpd,
                    tpm_code=pair.tpm_code,
                    tpd_code=pair.tpd_code,
                    tpm_adjustable=pair.tpm_adjustable,
                    tpd_adjustable=pair.tpd_adjustable,
                )
            )
    return rows


def _model_rows(
    account: Account, quota_by_region: dict[str, dict[str, QuotaPair]]
) -> list[QuotaRow]:
    """降级行：这个账号读不到 ARN 时，按「模型 × 区域」列配额，ARN 两列留空。"""
    rows: list[QuotaRow] = []
    for region in QUOTA_REGIONS:
        for model, pair in quota_by_region.get(region, {}).items():
            rows.append(
                QuotaRow(
                    region=region,
                    account=account.account,
                    account_key=account.key,
                    partner=account.partner,
                    quota_model=model,
                    tpm=pair.tpm,
                    tpd=pair.tpd,
                    tpm_code=pair.tpm_code,
                    tpd_code=pair.tpd_code,
                    tpm_adjustable=pair.tpm_adjustable,
                    tpd_adjustable=pair.tpd_adjustable,
                )
            )
    return rows


def _mark_best(rows: list[QuotaRow]) -> None:
    """给每行记上**同账号同模型**在四个区里的最高配额，以及取到最高 TPM 的区。

    提额是按区批的：Opus 4.6 在某账号的 us-east-1 提到了 6M，另外三个区还是
    3M。不标出来的话，看 us-west-2 那行会以为这个模型只有 3M 可用。

    分组键必须带账号。配额池是按账号独立发的，实测两个账号同一个模型的额度就
    不一样（一个 6M 一个 3M）；只按模型分组会拿另一个账号的额度去判这个账号
    「没跟上提额」，凭空标出一片黄。
    """
    best_tpm: dict[tuple, float] = {}
    best_tpd: dict[tuple, float] = {}
    best_region: dict[tuple, str] = {}
    for row in rows:
        key = (row.account_key, row.display)
        if row.tpm is not None and row.tpm > best_tpm.get(key, -1):
            best_tpm[key] = row.tpm
            best_region[key] = row.region
        if row.tpd is not None and row.tpd > best_tpd.get(key, -1):
            best_tpd[key] = row.tpd
    for row in rows:
        key = (row.account_key, row.display)
        row.best_tpm = best_tpm.get(key)
        row.best_tpd = best_tpd.get(key)
        row.best_region = best_region.get(key, "")


def _sort_rows(rows: list[QuotaRow], account_order: dict[str, int]) -> list[QuotaRow]:
    """先按账号（台账顺序）分组，组内按模型（最高 TPD 从大到小），再按固定区域顺序。

    账号在最外层：配额池是按账号独立的，一个账号的数据连在一起看才成立。
    同一个模型的四个区必须相邻——区域间的配额差异就是靠这个看出来的。
    """
    region_order = {region: i for i, region in enumerate(QUOTA_REGIONS)}
    return sorted(
        rows,
        key=lambda r: (
            account_order.get(r.account_key, len(account_order)),
            -(r.best_tpd or 0),
            r.display,
            region_order.get(r.region, len(region_order)),
            r.profile_id,
        ),
    )


def _orphans(
    account: Account,
    quota_by_region: dict[str, dict[str, QuotaPair]],
    profiles_by_region: dict[str, list[AppProfile]],
) -> list[OrphanModel]:
    """该账号配额里有、但账号下没有对应应用推理配置的 Claude 模型。

    这些模型只能直连调用，流量不会带上台账里那个标签。
    """
    covered = {
        match_key(p.model_id)
        for profiles in profiles_by_region.values()
        for p in profiles
    }
    seen: dict[str, QuotaPair] = {}
    for region in QUOTA_REGIONS:
        for model, pair in quota_by_region.get(region, {}).items():
            # 取第一个列出该模型的区（QUOTA_REGIONS 顺序固定，结果稳定）
            seen.setdefault(model, pair)
    return [
        OrphanModel(
            name=display_name(model),
            account=account.account,
            tpm=pair.tpm,
            tpd=pair.tpd,
        )
        for model, pair in sorted(seen.items())
        if match_key(model) not in covered
    ]


def _label(accounts: list[Account]) -> str:
    if len(accounts) == 1:
        return f"{accounts[0].partner} / {accounts[0].account}"
    return f"全部账号（{len(accounts)} 个）"


def build_quota_report(
    accounts: list[Account] | None,
    refresh: bool = False,
    model: str = "",
    region: str = "",
) -> QuotaReport:
    """账号下的 Claude 配额清单，按应用推理配置 ARN × 区域 逐条列。

    接受多个账号：表格里有账号列，一行对应一个账号下的一条 ARN，不做任何跨账号
    汇总——配额池是**按账号**独立发的，相加没有意义。所以「全部账号」在这一页是
    安全的（早先禁掉它是为了防止求和/求平均，那个顾虑对逐行清单不成立）。

    model / region 是展示层的筛选，只裁 rows，不影响 all_rows 和「没跟上提额」
    的判定。
    """
    report = QuotaReport(
        regions=list(QUOTA_REGIONS), model_filter=model, region_filter=region
    )
    if not accounts:
        return report
    report.account_label = _label(accounts)
    report.account_count = len(accounts)
    if refresh:
        clear_cache()

    fetched = _fetch_all(accounts)

    rows: list[QuotaRow] = []
    quota_errors: list[str] = []
    arn_errors: list[str] = []
    denied: list[str] = []
    for account in accounts:
        bucket = fetched[account.key]
        # 逐条留给页面的报错弹窗分组；下面的一行字只举每个账号的第一个区——四个区通常是同一个原因
        quota_problems = [
            as_query_error(error, account=account.account, region=region_name)
            for region_name, error in bucket["quota_errors"]
        ]
        arn_problems = [
            as_query_error(error, account=account.account, region=region_name)
            for region_name, error in bucket["arn_errors"]
        ]
        report.errors += quota_problems
        report.arn_errors += arn_problems
        if quota_problems:
            quota_errors.append(str(quota_problems[0]))
        # 每个账号各自判断能不能按 ARN 列。多账号时可能是混合的，一张表里
        # 两种行并存——QuotaRow 两种都支持，不用拆成两张表。
        if any(bucket["profiles"].values()):
            rows += _arn_rows(account, bucket["quotas"], bucket["profiles"])
            report.orphan_models += _orphans(
                account, bucket["quotas"], bucket["profiles"]
            )
            report.arn_driven = True
            if arn_problems:
                failed = "、".join(problem.region for problem in arn_problems)
                arn_errors.append(f"{account.account} 的 {failed} 读不到应用推理配置")
        else:
            rows += _model_rows(account, bucket["quotas"])
            if arn_problems:
                denied.append(account.account)
                # 只放一句原因，不再贴 AWS 原话：原话（连同 SCP 的策略 ARN）在 arn_errors 里
                arn_errors.append(f"{account.account}：{arn_problems[0].message}")

    if quota_errors:
        report.error = "；".join(quota_errors[:2])
    if arn_errors:
        prefix = ""
        if denied:
            prefix = (
                f"账号 {'、'.join(denied)} 的行已退回按「模型 × 区域」列，"
                "ARN 和模型 ID 两列显示「无权限」。 "
            )
        report.arn_error = prefix + "；".join(arn_errors[:2])

    _mark_best(rows)
    account_order = {a.key: i for i, a in enumerate(accounts)}
    report.all_rows = _sort_rows(rows, account_order)

    report.apply_filters(model=model, region=region)
    return report


def peek_quota_report(accounts: list[Account]) -> QuotaReport | None:
    """只用缓存里已经有的配额拼一份报表；哪个账号哪个区还没查过（或者已经过期）就返回 None。

    账号页摘要的「配额」卡用它：第一次查配额要 40~50 秒（Service Quotas 按账号限流），
    不能拖慢摘要。在「配额」页签查过一次之后，摘要上就有了。查失败的区不进缓存，
    所以上一次有区失败时这里也是 None，摘要上就不显示（配额页签会说清楚为什么）。
    """
    if not accounts:
        return None
    for account in accounts:
        for region in QUOTA_REGIONS:
            for kind in ("quotas", "app-profiles"):
                if _cached((account.ak[-6:], account.account, region, kind)) is None:
                    return None
    return build_quota_report(accounts)
