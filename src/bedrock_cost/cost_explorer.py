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

import threading
import time
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date, timedelta

import boto3
from botocore.config import Config as BotoConfig

from . import config, last_known
from .aws_errors import QueryError, describe, missing_credentials
# redact 搬去了 aws_errors；留个名字在这里，老代码 from .cost_explorer import redact 照样能用
from .aws_errors import redact  # noqa: F401
from .excel_source import Account


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
    # 这次查询失败（error 照留），金额是上一次成功查到这一天为止的结果，见 last_known。
    # 只有概览页和日报的 build_row 会拿它来显示；其余地方看到 error 就当失败，比如额度
    # 告警——拿旧的数去判阈值只会晚报
    stale_as_of: date | None = None
    # 失败原因的结构化版本（哪个账号、一句话原因、AWS 原话），给页面的报错弹窗用。
    # error 是它的一行字（problem.message），report / alerts / last_known 照旧只读 error
    problem: QueryError | None = None

    @property
    def ok(self) -> bool:
        return self.error is None

    @property
    def total_raw(self) -> float:
        return self.tag_raw + self.untag_raw


def friendly_error(exc: Exception, account: Account) -> str:
    """一行「原因（错误码）」，给只要一句话的老调用方。

    要把账号、原因、原始报错分开显示的（页面弹窗），用 aws_errors.describe 拿 QueryError。
    """
    return describe(exc, account).message


def build_filter() -> dict | None:
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


def split_group_key(raw_key: str) -> str:
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
    cost_filter = build_filter()
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
                tag_value = split_group_key((group.get("Keys") or [""])[0])
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
        problem = missing_credentials(account)
        return CostSplit(error=problem.message, problem=problem, fetched_at=time.time())

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
        problem = describe(exc, account)
        failed = CostSplit(error=problem.message, problem=problem, fetched_at=now)
        return _fallback(account, start, end, failed)

    try:
        last_known.remember(account, start, end, split)
    except OSError:
        pass  # 记不下来不影响这次的结果，最多是下次失败时没有旧数可顶
    if config.CACHE_TTL > 0:
        with _cache_lock:
            _cache[key] = split
    return split


def _fallback(account: Account, start: date, end: date, failed: CostSplit) -> CostSplit:
    """查询失败：有同口径的上一次成功结果就带上它的金额（错误照留），没有就原样返回。"""
    try:
        known = last_known.recall(account, start, end)
    except OSError:
        known = None
    if known is None:
        return failed
    return CostSplit(
        tag_raw=known.tag_raw,
        untag_raw=known.untag_raw,
        currency=known.currency,
        error=failed.error,
        problem=failed.problem,
        fetched_at=failed.fetched_at,
        stale_as_of=known.as_of,
    )


def fetch_all(
    accounts: list[Account],
    ranges: Mapping[str, tuple[date, date]],
    refresh: bool = False,
) -> dict[str, CostSplit]:
    """并发查询多个账号，返回 {account.key: CostSplit}。

    区间**按账号给**，不是所有账号共用一个：概览页的口径是「从各自的启用日期
    累计到今天」，两个账号启用时间不同，区间就不同。缓存键本来就含起止日期，
    所以这么改不影响缓存命中。
    """
    if not accounts:
        return {}
    workers = max(1, min(config.MAX_WORKERS, len(accounts)))

    def one(account: Account) -> CostSplit:
        start, end = ranges[account.key]
        return fetch_split(account, start, end, refresh)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = pool.map(one, accounts)
        return {account.key: split for account, split in zip(accounts, results)}
