"""Bedrock 的模型配额。

配额来自 Service Quotas（ServiceCode=bedrock），只取这两类：

    Global cross-region model inference tokens per day for <模型>
    Global cross-region model inference tokens per minute for <模型>

实测确认的几件事：

  · 这两类配额是**账号级的，不分区域**。四个美区 ListServiceQuotas 返回的值
    完全相同（32 条里 30 条一致，另 2 条只是 us-west-1 根本没列出那个模型）。
    名字里的 Global cross-region 就是字面意思——一个全局共享池。所以这一页
    没有区域维度，随便挑一个区问就行。
  · 配额口径是 **input + output token 合计**（配额描述里写明了）。
  · **可调性是分开的**：日配额 Adjustable=False（改不了），分钟配额
    Adjustable=True（可以在 Service Quotas 里申请提额）。12 个模型全部如此。
    所以两条配额要各自记可调性，不能合并成一个字段。
  · 1M 上下文长度是**独立的一条配额**，不能和同名模型的普通配额合并。

配额属于配置信息、几乎不变，所以缓存期给得很长。
"""

from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass, field

import boto3
from botocore.config import Config as BotoConfig

from . import config
from .cost_explorer import friendly_error
from .excel_source import Account

SERVICE_CODE = "bedrock"

# Service Quotas 是分区域的接口，但这两类配额本身是账号级的，随便挑一个区问就行。
# 选 us-east-1 是因为它列出的模型最全（实测 1162 条，us-west-1 只有 222 条）。
QUOTA_REGION = "us-east-1"

QUOTA_DAY = re.compile(r"^Global cross-region model inference tokens per day for (.+)$")
QUOTA_MINUTE = re.compile(r"^Global cross-region model inference tokens per minute for (.+)$")

# 只看 Claude
VENDOR_PREFIX = "Anthropic Claude"

# 配额几乎不变，缓存久一点，免得每次翻页都拉一千多条
QUOTA_CACHE_TTL = 6 * 3600

# ListServiceQuotas 每页条数。不设的话 boto3 默认每页 8 条，要发 146 次请求。
PAGE_SIZE = 100


def display_name(quota_model: str) -> str:
    """去掉厂商前缀：Anthropic Claude Opus 4.6 V1 -> Claude Opus 4.6 V1。"""
    return quota_model.replace("Anthropic ", "").strip()


def sort_key(row: QuotaRow) -> tuple:
    """按日配额从大到小排，同额度的按名字。缺配额值的沉底。"""
    return (-(row.day or 0), -(row.minute or 0), row.display)


@dataclass
class QuotaRow:
    """一个模型的两条 token 配额。"""

    model: str                      # 配额里的原始显示名
    day: float | None = None        # 每日 token 配额
    minute: float | None = None     # 每分钟 token 配额
    day_code: str = ""
    minute_code: str = ""
    model_id: str = ""   # global.* 跨区推理配置 ID，就是调用时要传的那个
    # 可调性按条记：日配额改不了，分钟配额可以申请提额。合成一个字段的话，
    # 「有一条能调」会被显示成「都能调」，等于给了错误的操作指引。
    day_adjustable: bool = False
    minute_adjustable: bool = False

    @property
    def display(self) -> str:
        return display_name(self.model)

    @property
    def is_long_context(self) -> bool:
        """1M 上下文是独立配额，页面上标一下，免得被当成同名模型的重复行。"""
        return "1m context" in self.model.lower()

    @property
    def complete(self) -> bool:
        """两条配额都读到了。缺一条说明 AWS 那边没列出来，值得标出来。"""
        return self.day is not None and self.minute is not None

    @property
    def minutes_to_exhaust(self) -> float | None:
        """按分钟配额满速跑，多久用完当天的额度。

        这是纯粹由两条配额算出来的比值，不涉及任何实际用量：日配额 ÷ 分钟配额。
        实测大多是 1440（正好一天），偏离 1440 的说明两条配额不是按整天配的。
        """
        if not self.day or not self.minute:
            return None
        return self.day / self.minute

    @property
    def balanced(self) -> bool:
        """日配额是否正好等于分钟配额跑满 24 小时（1440 分钟）。"""
        ratio = self.minutes_to_exhaust
        return ratio is not None and abs(ratio - 1440) < 1


@dataclass
class QuotaReport:
    account_label: str = ""
    rows: list[QuotaRow] = field(default_factory=list)
    error: str | None = None

    @property
    def complete_rows(self) -> list[QuotaRow]:
        return [r for r in self.rows if r.complete]

    @property
    def incomplete_rows(self) -> list[QuotaRow]:
        return [r for r in self.rows if not r.complete]

    @property
    def unbalanced_rows(self) -> list[QuotaRow]:
        """日配额和分钟配额不是 1440 倍关系的。"""
        return [r for r in self.complete_rows if not r.balanced]

    @property
    def without_model_id(self) -> list[QuotaRow]:
        """没查到 global 配置 ID 的行。1M 上下文那条本来就没有，属正常。"""
        return [r for r in self.rows if not r.model_id]

    @property
    def adjustable_minute_rows(self) -> list[QuotaRow]:
        """分钟配额可以申请提额的模型。"""
        return [r for r in self.rows if r.minute_adjustable]

    @property
    def highest(self) -> QuotaRow | None:
        candidates = [r for r in self.rows if r.day is not None]
        return max(candidates, key=lambda r: r.day) if candidates else None

    @property
    def lowest(self) -> QuotaRow | None:
        candidates = [r for r in self.rows if r.day is not None]
        return min(candidates, key=lambda r: r.day) if candidates else None


# --------------------------------------------------------------- 取配额
_lock = threading.Lock()
_cache: dict[tuple, tuple[float, list[QuotaRow]]] = {}
_model_id_cache: dict[tuple, tuple[float, dict]] = {}


def clear_cache() -> None:
    with _lock:
        _cache.clear()
        _model_id_cache.clear()


def fetch_quotas(account: Account) -> tuple[list[QuotaRow], str | None]:
    """拉该账号的 Claude token 配额，返回 (配额行, 错误)。"""
    cache_key = (account.ak[-6:], account.account, "quotas")
    if config.CACHE_TTL > 0:
        with _lock:
            hit = _cache.get(cache_key)
        if hit and time.time() - hit[0] < QUOTA_CACHE_TTL:
            return list(hit[1]), None

    by_model: dict[str, QuotaRow] = {}
    try:
        client = boto3.client(
            "service-quotas",
            region_name=QUOTA_REGION,
            aws_access_key_id=account.ak,
            aws_secret_access_key=account.sk,
            config=BotoConfig(
                read_timeout=config.CE_TIMEOUT,
                connect_timeout=config.CE_TIMEOUT,
                retries={"max_attempts": config.CE_RETRIES, "mode": "standard"},
            ),
        )
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
                # 按配额里的模型显示名分组：同一个模型的 day 和 minute 是两条记录
                row = by_model.setdefault(model, QuotaRow(model=model))
                value = float(quota.get("Value") or 0)
                adjustable = bool(quota.get("Adjustable"))
                if day:
                    row.day = value
                    row.day_code = quota.get("QuotaCode", "")
                    row.day_adjustable = adjustable
                else:
                    row.minute = value
                    row.minute_code = quota.get("QuotaCode", "")
                    row.minute_adjustable = adjustable
    except Exception as exc:
        return [], friendly_error(exc, account)

    rows = sorted(by_model.values(), key=sort_key)
    if config.CACHE_TTL > 0:
        with _lock:
            _cache[cache_key] = (time.time(), rows)
    return list(rows), None


def build_quota_report(account: Account | None, refresh: bool = False) -> QuotaReport:
    """一个账号的 Claude token 配额清单。

    只接受单个账号：配额是**按账号**发的，两个账号的额度可能不同，合在一起看
    没有意义（也不能相加——它们是各自独立的池子）。
    """
    report = QuotaReport()
    if account is None:
        return report
    report.account_label = f"{account.partner} / {account.account}"
    if refresh:
        clear_cache()
    report.rows, report.error = fetch_quotas(account)

    # 配额名里没有可调用的 model ID，从系统推理配置里查真实 ID 补上。
    # 查不到（没权限，或该模型没有 global 配置）就留空，页面显示 —。
    ids = fetch_global_model_ids(account)
    for row in report.rows:
        row.model_id = ids.get(match_key(row.model), "")
    return report


# --------------------------------------------------------------- Model ID
# 配额名里只有显示名（Anthropic Claude Opus 4.8），没有可以直接调用的 model ID。
# 「Global cross-region」这两类配额对应的就是 global.* 那组系统推理配置，所以
# 从 bedrock:ListInferenceProfiles(SYSTEM_DEFINED) 里取真实 ID，不靠字符串拼。
FAMILY = re.compile(r"(opus|sonnet|haiku|fable)", re.I)

GLOBAL_PREFIX = "global.anthropic."


def match_key(text: str) -> tuple[str, str, bool] | None:
    """把配额显示名和 model ID 归到同一个 join key。

        Anthropic Claude Opus 4.8            -> ("opus", "4.8", False)
        global.anthropic.claude-opus-4-8     -> ("opus", "4.8", False)
        global.anthropic.claude-sonnet-4-20250514-v1:0 -> ("sonnet", "4", False)
        ...Sonnet 4.5 V1 1M Context Length   -> ("sonnet", "4.5", True)

    先剥掉 8 位日期戳再抽版本号——不剥的话 claude-sonnet-4-20250514 会被读成
    版本 4.20250514。V1 后缀两边有时有有时没有，所以不参与 key。1M 上下文用
    第三个字段区分，它没有对应的 global 配置，join 不上是正确结果。

    不用名字直接对：配置名有的带 Anthropic 有的不带（Global Claude Sonnet 4）、
    有的带 V1 有的不带，还有一个是 GLOBAL 大写，靠名字迟早对错。
    """
    lowered = re.sub(r"[-_]?\d{8}", "", text.lower())
    family = FAMILY.search(lowered)
    if not family:
        return None
    version = re.search(r"(\d+(?:[.\-]\d+)?)", lowered[family.end():])
    if not version:
        return None
    return (family.group(1), version.group(1).replace("-", "."), "1m" in lowered)


def fetch_global_model_ids(account: Account) -> dict[tuple, str]:
    """join key -> global.* 跨区推理配置 ID。读不到就返回空，页面显示 — 即可。"""
    cache_key = (account.ak[-6:], account.account, "global-ids")
    if config.CACHE_TTL > 0:
        with _lock:
            hit = _model_id_cache.get(cache_key)
        if hit and time.time() - hit[0] < QUOTA_CACHE_TTL:
            return dict(hit[1])

    mapping: dict[tuple, str] = {}
    try:
        client = boto3.client(
            "bedrock",
            region_name=QUOTA_REGION,
            aws_access_key_id=account.ak,
            aws_secret_access_key=account.sk,
            config=BotoConfig(
                read_timeout=config.CE_TIMEOUT,
                connect_timeout=config.CE_TIMEOUT,
                retries={"max_attempts": config.CE_RETRIES, "mode": "standard"},
            ),
        )
        token = None
        while True:
            kwargs = {"maxResults": 100, "typeEquals": "SYSTEM_DEFINED"}
            if token:
                kwargs["nextToken"] = token
            page = client.list_inference_profiles(**kwargs)
            for summary in page.get("inferenceProfileSummaries", []):
                profile_id = summary.get("inferenceProfileId", "")
                if not profile_id.startswith(GLOBAL_PREFIX):
                    continue
                key = match_key(profile_id)
                if key is not None:
                    mapping.setdefault(key, profile_id)
            token = page.get("nextToken")
            if not token:
                break
    except Exception:
        # 没有 bedrock:ListInferenceProfiles 权限也要能出配额表，只是没有 ID 列
        mapping = {}

    if config.CACHE_TTL > 0:
        with _lock:
            _model_id_cache[cache_key] = (time.time(), mapping)
    return dict(mapping)
