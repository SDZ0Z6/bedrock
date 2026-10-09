"""成本和使用情况页的数据层。

按日（或按月）拉取时间序列，并按「服务 / 标签 / 账号」三种维度拆分。

金额两个口径同时保留：
    raw    —— Cost Explorer 原样返回的金额
    marked —— 逐格乘上比率后的金额（匹配台账 TAG 的乘 TAG_RATIO，其余乘
              UNTAG_RATIO），因此各维度加总后能和主页的总消费对上。

为了拿到逐格的比率归属，「服务」维度用两级 GroupBy：[SERVICE, TAG]。
Cost Explorer 最多支持两级，刚好够用，一个账号仍然只发一次请求。
"""

from __future__ import annotations

import hashlib
import threading
import time
from dataclasses import dataclass, field
from datetime import date, timedelta

import boto3
from botocore.config import Config as BotoConfig

from . import config
from .aws_errors import QueryError, as_query_error, describe, missing_credentials
from .cost_explorer import build_filter, split_group_key
from .excel_source import Account

# 维度 -> (页面显示名, CE 的 GroupBy 维度键或 None)
DIMENSIONS: dict[str, str] = {
    "service": "服务",
    "tag": "标签",
    "account": "账号",
}

GRANULARITIES: dict[str, str] = {
    "daily": "按日",
    "monthly": "按月",
}

UNTAGGED_LABEL = "未打标签"
OTHER_LABEL = "其他"

# 图表最多用 8 个分类色槽，超出的折叠进「其他」（用中性灰，不占分类槽）
MAX_SERIES = 8


@dataclass
class Series:
    """一条堆叠序列。raw / marked 与 report.dates 一一对应。"""

    name: str
    raw: list[float]
    marked: list[float]
    slot: int = 0  # 颜色槽；-1 表示「其他」，用中性灰

    @property
    def total_raw(self) -> float:
        return sum(self.raw)

    @property
    def total_marked(self) -> float:
        return sum(self.marked)


@dataclass
class UsageReport:
    start: date
    end: date
    dimension: str
    granularity: str
    dates: list[str] = field(default_factory=list)  # 每个桶的起始日期(ISO)
    labels: list[str] = field(default_factory=list)  # 轴上显示的短标签
    series: list[Series] = field(default_factory=list)
    # 每个查不了的账号一条；str() 是「上游 / 账号：原因（错误码）」，和原来的文案一样
    errors: list[QueryError] = field(default_factory=list)
    currency: str = "USD"
    any_cached: bool = False
    folded_count: int = 0  # 被折叠进「其他」的序列数

    @property
    def dimension_label(self) -> str:
        return DIMENSIONS.get(self.dimension, self.dimension)

    @property
    def total_raw(self) -> float:
        return sum(s.total_raw for s in self.series)

    @property
    def total_marked(self) -> float:
        return sum(s.total_marked for s in self.series)

    @property
    def column_totals(self) -> list[float]:
        """每个时间桶的加价后总额，图表和表格的合计行共用。"""
        return [
            sum((s.marked[i] for s in self.series), 0.0) for i in range(len(self.dates))
        ]

    @property
    def peak(self) -> tuple[int, float]:
        """(最高桶的下标, 金额)，用于在图上做唯一一处直接标注。

        全是 0 时返回 (-1, 0.0)：没有消费就没有「最高点」，不能指着第 0 个桶
        说那是峰值。
        """
        totals = self.column_totals
        if not totals:
            return -1, 0.0
        top = max(range(len(totals)), key=lambda i: totals[i])
        if totals[top] <= 0:
            return -1, 0.0
        return top, totals[top]

    @property
    def has_data(self) -> bool:
        return bool(self.series) and self.total_marked > 0


# --------------------------------------------------------------- 时间桶
def _month_shift(anchor: date, months: int) -> date:
    total = anchor.year * 12 + (anchor.month - 1) + months
    return date(total // 12, total % 12 + 1, 1)


def build_buckets(start: date, end: date, granularity: str) -> tuple[list[str], list[str]]:
    """生成时间桶的 ISO 日期和轴标签。"""
    dates: list[str] = []
    labels: list[str] = []
    if granularity == "monthly":
        cursor = start.replace(day=1)
        while cursor <= end:
            dates.append(cursor.isoformat())
            labels.append(cursor.strftime("%Y-%m"))
            cursor = _month_shift(cursor, 1)
    else:
        cursor = start
        while cursor <= end:
            dates.append(cursor.isoformat())
            labels.append(cursor.strftime("%m-%d"))
            cursor += timedelta(days=1)
    return dates, labels


def _bucket_index(dates: list[str], granularity: str) -> dict:
    """CE 返回的桶起始日期 -> 下标。

    按月查询且区间从月中开始时，CE 返回的 Start 是区间起点而不是 1 号，
    所以按月匹配 (年, 月) 而不是精确日期。
    """
    if granularity == "monthly":
        index = {}
        for position, iso in enumerate(dates):
            parsed = date.fromisoformat(iso)
            index[(parsed.year, parsed.month)] = position
        return index
    return {iso: position for position, iso in enumerate(dates)}


def _lookup(index: dict, granularity: str, bucket_start: str) -> int | None:
    if granularity == "monthly":
        try:
            parsed = date.fromisoformat(bucket_start)
        except ValueError:
            return None
        return index.get((parsed.year, parsed.month))
    return index.get(bucket_start)


# --------------------------------------------------------------- 颜色槽
def assign_slots(names: list[str]) -> dict[str, int]:
    """按名字哈希分配颜色槽，而不是按金额排名。

    保证：分配只取决于名字的集合，与展示顺序（金额高低）无关。所以改日期
    范围后只要出现的序列还是那几个，颜色就完全不变；金额涨跌导致的排序变化
    永远不会换色。

    做不到的部分：8 个色槽装 8 条序列必然有哈希冲突，冲突要靠「谁先占到」
    来解决，因此某条序列彻底消失时，原先和它撞槽的那一两条会挪位。要在
    「颜色绝对稳定」和「同一张图里不出现重复颜色」之间二选一时，这里选了
    后者——图上颜色撞车比偶尔挪色更影响判读。身份识别始终有图例、明细表
    和悬浮提示三条不依赖颜色的通道兜底。

    用 blake2b 摘要而不是 crc32 取模：本项目的序列名前后缀高度雷同
    （"Claude Opus 4.6 (Amazon Bedrock Edition)" 之类），crc32 的低 3 位在
    这种输入上几乎不散列——实测 8 个服务名有 6 个挤进同一个槽，只用到 3 种
    颜色。换成充分混淆的摘要后能用到 6 个槽，撞槽和挪位都大幅减少。
    """
    slots: dict[str, int] = {}
    used: set[int] = set()
    for name in sorted(names):  # 排序保证与展示顺序（按金额）解耦
        digest = hashlib.blake2b(name.encode("utf-8"), digest_size=8).digest()
        slot = int.from_bytes(digest, "big") % MAX_SERIES
        while slot in used and len(used) < MAX_SERIES:
            slot = (slot + 1) % MAX_SERIES
        used.add(slot)
        slots[name] = slot
    return slots


# --------------------------------------------------------------- CE 查询
_cache_lock = threading.Lock()
_cache: dict[tuple, tuple[float, dict, str]] = {}


def clear_cache() -> None:
    with _cache_lock:
        _cache.clear()


def _cache_key(account: Account, start, end, dimension: str, granularity: str) -> tuple:
    return (
        account.ak[-6:],
        account.account,
        account.row,
        start.isoformat(),
        end.isoformat(),
        dimension,
        granularity,
        account.tag_key,
        account.tag_value,
        # 折算后的金额是查询时按比率乘好再缓存的：台账里改了比率，缓存要跟着失效
        account.tag_ratio,
        account.untag_ratio,
        config.COST_METRIC,
        tuple(config.SERVICE_FILTER),
    )


def _query_account(
    account: Account, start: date, end: date, dimension: str, granularity: str,
    dates: list[str],
) -> tuple[dict[str, list[list[float]]], str]:
    """返回 {序列名: [[raw, marked], ...]}，长度与 dates 一致。"""
    tag_key = account.tag_key
    expected = account.tag_value

    group_by: list[dict] = []
    if dimension == "service":
        group_by.append({"Type": "DIMENSION", "Key": "SERVICE"})
    group_by.append({"Type": "TAG", "Key": tag_key})

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
        "TimePeriod": {
            "Start": start.isoformat(),
            "End": (end + timedelta(days=1)).isoformat(),  # CE 的 End 是开区间
        },
        "Granularity": "MONTHLY" if granularity == "monthly" else "DAILY",
        "Metrics": [config.COST_METRIC],
        "GroupBy": group_by,
    }
    cost_filter = build_filter()
    if cost_filter:
        request["Filter"] = cost_filter

    index = _bucket_index(dates, granularity)
    width = len(dates)
    data: dict[str, list[list[float]]] = {}
    currency = "USD"
    account_label = f"{account.partner} / {account.account}"

    next_token: str | None = None
    while True:
        if next_token:
            request["NextPageToken"] = next_token
        response = client.get_cost_and_usage(**request)

        for bucket in response.get("ResultsByTime", []):
            position = _lookup(index, granularity, bucket.get("TimePeriod", {}).get("Start", ""))
            if position is None:
                continue
            for group in bucket.get("Groups", []):
                keys = group.get("Keys") or []
                metric = group.get("Metrics", {}).get(config.COST_METRIC, {})
                amount = float(metric.get("Amount") or 0.0)
                currency = metric.get("Unit") or currency

                # GroupBy 顺序：service 维度是 [SERVICE, TAG]，其余是 [TAG]
                tag_value = split_group_key(keys[-1] if keys else "")
                if expected is None:
                    is_tag = bool(tag_value)
                else:
                    is_tag = tag_value == expected
                marked = amount * (account.tag_ratio if is_tag else account.untag_ratio)

                if dimension == "service":
                    name = keys[0] if keys else "(未知服务)"
                elif dimension == "tag":
                    name = tag_value or UNTAGGED_LABEL
                else:
                    name = account_label

                slot = data.setdefault(name, [[0.0, 0.0] for _ in range(width)])
                slot[position][0] += amount
                slot[position][1] += marked

        next_token = response.get("NextPageToken")
        if not next_token:
            break

    return data, currency


def _fetch_account(
    account: Account, start: date, end: date, dimension: str, granularity: str,
    dates: list[str], refresh: bool,
) -> tuple[dict[str, list[list[float]]], str, bool, QueryError | None]:
    """返回 (数据, 货币, 是否命中缓存, 错误)。"""
    if not account.has_credentials:
        return {}, "USD", False, missing_credentials(account)

    key = _cache_key(account, start, end, dimension, granularity)
    now = time.time()
    if not refresh and config.CACHE_TTL > 0:
        with _cache_lock:
            hit = _cache.get(key)
        if hit and now - hit[0] < config.CACHE_TTL:
            return hit[1], hit[2], True, None

    try:
        data, currency = _query_account(account, start, end, dimension, granularity, dates)
    except Exception as exc:
        return {}, "USD", False, describe(exc, account)

    if config.CACHE_TTL > 0:
        with _cache_lock:
            _cache[key] = (now, data, currency)
    return data, currency, False, None


def account_series(
    account: Account, start: date, end: date, granularity: str = "daily", refresh: bool = False,
) -> tuple[list[str], list[float], list[float], bool, QueryError | None]:
    """一个账号每个时间桶的 (原价, 折算后)，不再往下拆。

    返回 (桶的起始日期, 原价, 折算后, 是否命中缓存, 错误)。运营看板用它：看板要每个账号
    各自的数，build_usage 会把第 8 个以后的账号并进「其他」。查不了时两个列表是空的。
    """
    dates, _ = build_buckets(start, end, granularity)
    data, _, cached, error = _fetch_account(account, start, end, "account", granularity, dates, refresh)
    if error:
        return dates, [], [], False, as_query_error(error, account=account.account, partner=account.partner)
    raw = [0.0] * len(dates)
    marked = [0.0] * len(dates)
    for cells in data.values():
        for position, (amount, priced) in enumerate(cells):
            raw[position] += amount
            marked[position] += priced
    return dates, raw, marked, cached, None


# --------------------------------------------------------------- 组装
def build_usage(
    accounts: list[Account], start: date, end: date, dimension: str,
    granularity: str, refresh: bool = False,
) -> UsageReport:
    """把若干账号的查询结果合并成一份可以直接喂给图表和表格的报表。"""
    if dimension not in DIMENSIONS:
        dimension = "service"
    if granularity not in GRANULARITIES:
        granularity = "daily"

    dates, labels = build_buckets(start, end, granularity)
    report = UsageReport(
        start=start, end=end, dimension=dimension, granularity=granularity,
        dates=dates, labels=labels,
    )

    combined: dict[str, list[list[float]]] = {}
    width = len(dates)

    # 账号之间可以并发，但账号数通常很少，顺序执行已经够快且更好排错
    for account in accounts:
        data, currency, cached, error = _fetch_account(
            account, start, end, dimension, granularity, dates, refresh
        )
        if error:
            report.errors.append(
                as_query_error(error, account=account.account, partner=account.partner)
            )
            continue
        if cached:
            report.any_cached = True
        report.currency = currency
        for name, cells in data.items():
            target = combined.setdefault(name, [[0.0, 0.0] for _ in range(width)])
            for position, (raw, marked) in enumerate(cells):
                target[position][0] += raw
                target[position][1] += marked

    # 按加价后总额排序，超出 8 条折叠进「其他」
    ranked = sorted(
        combined.items(), key=lambda kv: -sum(cell[1] for cell in kv[1])
    )
    kept = ranked[:MAX_SERIES]
    folded = ranked[MAX_SERIES:]
    report.folded_count = len(folded)

    slots = assign_slots([name for name, _ in kept])
    for name, cells in kept:
        report.series.append(
            Series(
                name=name,
                raw=[cell[0] for cell in cells],
                marked=[cell[1] for cell in cells],
                slot=slots[name],
            )
        )

    if folded:
        raw_total = [0.0] * width
        marked_total = [0.0] * width
        for _, cells in folded:
            for position, (raw, marked) in enumerate(cells):
                raw_total[position] += raw
                marked_total[position] += marked
        report.series.append(
            Series(name=OTHER_LABEL, raw=raw_total, marked=marked_total, slot=-1)
        )

    return report
