"""调用 AWS Cost Explorer，把每个账号的消费拆成 TAG / UNTAG 两部分。

拆分口径由台账 TAG 列决定，按标签键做 GroupBy 后在本地归类：

  TAG 列填 `map-migrated=migXYT8EVQSVP`（键+值）
      标签值 == migXYT8EVQSVP  -> TAG 消费
      其余全部（空值、其他值）  -> UNTAG 消费
  TAG 列只填键，或留空（回落到 .env 的 TAG_KEY）
      标签有任意非空值        -> TAG 消费
      标签为空/缺失          -> UNTAG 消费

两种口径下 TAG + UNTAG 都等于账号总消费，不会有消费被漏掉。

一个账号一次 GetCostAndUsage 就能同时拿到两部分，CE 按请求计费
（约 0.01 USD/次），因此结果按「账号 + 日期区间 + 指标」缓存。
"""

from __future__ import annotations

import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date, timedelta

import boto3
from botocore.config import Config as BotoConfig
from botocore.exceptions import BotoCoreError, ClientError

import config
from excel_source import Account

_AK_PATTERN = re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{8,}\b")


@dataclass
class CostSplit:
    """单个账号在指定区间内的原始消费（未乘比率）。"""

    tag_raw: float = 0.0
    untag_raw: float = 0.0
    currency: str = "USD"
    error: str | None = None
    from_cache: bool = False
    fetched_at: float = 0.0
    # CE 在该区间实际出现过的标签值，用于提示台账里的标签值是否写错
    seen_values: tuple[str, ...] = ()
    note: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None

    @property
    def total_raw(self) -> float:
        return self.tag_raw + self.untag_raw


def _redact(message: str, account: Account) -> str:
    """错误信息可能带上凭证片段，落到页面前先擦掉。"""
    cleaned = _AK_PATTERN.sub("[已隐藏]", message)
    for secret in (account.ak, account.sk):
        if secret and len(secret) > 6:
            cleaned = cleaned.replace(secret, "[已隐藏]")
    return cleaned.strip()


def _friendly_error(exc: Exception, account: Account) -> str:
    if isinstance(exc, ClientError):
        code = exc.response.get("Error", {}).get("Code", "")
        detail = exc.response.get("Error", {}).get("Message", str(exc))
        hints = {
            "AccessDeniedException": "凭证缺少 ce:GetCostAndUsage 权限",
            "AccessDenied": "凭证缺少 ce:GetCostAndUsage 权限",
            "UnauthorizedOperation": "凭证缺少 ce:GetCostAndUsage 权限",
            "InvalidClientTokenId": "AK 无效或已删除",
            "SignatureDoesNotMatch": "SK 不匹配，请检查台账里的密钥",
            "DataUnavailableException": "该区间暂无成本数据",
            "LimitExceededException": "Cost Explorer 请求过于频繁，请稍后重试",
            "RequestChangedException": "分页请求参数发生变化，请重试",
        }
        hint = hints.get(code)
        label = f"{code}: {detail}" if code else detail
        if hint:
            label = f"{hint}（{code}）"
        return _redact(label, account)
    if isinstance(exc, BotoCoreError):
        return _redact(f"网络或凭证错误：{exc}", account)
    return _redact(f"{type(exc).__name__}: {exc}", account)


def _build_filter() -> dict | None:
    """只在配置了 SERVICE_FILTER 时才加服务过滤，默认统计账号全部服务。"""
    if not config.SERVICE_FILTER:
        return None
    return {
        "Dimensions": {
            "Key": "SERVICE",
            "Values": list(config.SERVICE_FILTER),
            "MatchOptions": ["EQUALS"],
        }
    }


def _split_group_key(raw_key: str) -> str:
    """CE 返回的分组键形如 'map-migrated$migXYT8EVQSVP'，取 $ 之后的标签值。"""
    _, separator, value = raw_key.partition("$")
    return value.strip() if separator else ""


def _query(account: Account, start: date, end: date) -> CostSplit:
    tag_key = account.tag_key
    expected = account.tag_value  # None = 任意非空值都算 TAG

    client = boto3.client(
        "ce",
        region_name=config.CE_REGION,
        aws_access_key_id=account.ak,
        aws_secret_access_key=account.sk,
        config=BotoConfig(
            read_timeout=config.CE_TIMEOUT,
            connect_timeout=config.CE_TIMEOUT,
            retries={"max_attempts": config.CE_RETRIES, "mode": "standard"},
        ),
    )

    request: dict = {
        # CE 的 End 是开区间，所以要在用户选择的结束日期上加一天。
        "TimePeriod": {
            "Start": start.isoformat(),
            "End": (end + timedelta(days=1)).isoformat(),
        },
        "Granularity": "MONTHLY",
        "Metrics": [config.COST_METRIC],
        "GroupBy": [{"Type": "TAG", "Key": tag_key}],
    }
    cost_filter = _build_filter()
    if cost_filter:
        request["Filter"] = cost_filter

    split = CostSplit(fetched_at=time.time())
    seen: dict[str, float] = {}
    next_token: str | None = None
    while True:
        if next_token:
            request["NextPageToken"] = next_token
        response = client.get_cost_and_usage(**request)

        for bucket in response.get("ResultsByTime", []):
            for group in bucket.get("Groups", []):
                metric = group.get("Metrics", {}).get(config.COST_METRIC, {})
                amount = float(metric.get("Amount") or 0.0)
                split.currency = metric.get("Unit") or split.currency
                tag_value = _split_group_key((group.get("Keys") or [""])[0])
                if tag_value:
                    seen[tag_value] = seen.get(tag_value, 0.0) + amount

                if expected is None:
                    is_tag = bool(tag_value)  # 任意非空值
                else:
                    is_tag = tag_value == expected  # 精确匹配台账里的值

                if is_tag:
                    split.tag_raw += amount
                else:
                    split.untag_raw += amount
            # 某些区间 CE 不返回 Groups，只给 Total（例如完全没有消费）
            if not bucket.get("Groups"):
                total = bucket.get("Total", {}).get(config.COST_METRIC, {})
                if total.get("Unit"):
                    split.currency = total["Unit"]
                split.untag_raw += float(total.get("Amount") or 0.0)

        next_token = response.get("NextPageToken")
        if not next_token:
            break

    split.seen_values = tuple(sorted(seen, key=lambda v: -seen[v]))

    # 台账里写死了标签值，但该区间内一分钱都没匹配上 —— 大概率是值写错了，
    # 这种情况下全部消费会被按 UNTAG_RATIO 加价，必须让用户看见。
    if expected is not None and split.tag_raw == 0 and split.untag_raw > 0:
        if split.seen_values:
            others = "、".join(split.seen_values[:3])
            split.note = (
                f"台账中的标签值 {expected} 未匹配到任何消费；"
                f"该区间实际出现的值是：{others}"
            )
        else:
            split.note = (
                f"该区间内 {tag_key} 标签没有任何带值的消费，"
                f"台账中的 {expected} 未匹配到，全部计入 UNTAG"
            )

    return split


# ----------------------------------------------------------------- 缓存
_cache_lock = threading.Lock()
_cache: dict[tuple, CostSplit] = {}


def _cache_key(account: Account, start: date, end: date) -> tuple:
    return (
        account.ak[-6:],  # 只留尾部片段做区分，不在缓存键里存完整凭证
        account.account,
        account.row,
        start.isoformat(),
        end.isoformat(),
        account.tag_key,
        account.tag_value,
        config.COST_METRIC,
        tuple(config.SERVICE_FILTER),
    )


def clear_cache() -> None:
    with _cache_lock:
        _cache.clear()


def fetch_split(account: Account, start: date, end: date, refresh: bool = False) -> CostSplit:
    """取单个账号的 TAG / UNTAG 原始消费，命中缓存则不发请求。"""
    if not account.has_credentials:
        return CostSplit(error="台账中缺少 AK 或 SK，无法查询", fetched_at=time.time())

    key = _cache_key(account, start, end)
    now = time.time()
    if not refresh and config.CACHE_TTL > 0:
        with _cache_lock:
            cached = _cache.get(key)
        if cached and cached.ok and now - cached.fetched_at < config.CACHE_TTL:
            hit = CostSplit(**{**cached.__dict__})
            hit.from_cache = True
            return hit

    try:
        split = _query(account, start, end)
    except Exception as exc:  # 单个账号失败不能影响整页
        return CostSplit(error=_friendly_error(exc, account), fetched_at=now)

    if config.CACHE_TTL > 0:
        with _cache_lock:
            _cache[key] = split
    return split


def fetch_all(
    accounts: list[Account], start: date, end: date, refresh: bool = False
) -> dict[str, CostSplit]:
    """并发查询多个账号，返回 {account.key: CostSplit}。"""
    if not accounts:
        return {}
    workers = max(1, min(config.MAX_WORKERS, len(accounts)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = pool.map(lambda a: fetch_split(a, start, end, refresh), accounts)
        return {account.key: split for account, split in zip(accounts, results)}
