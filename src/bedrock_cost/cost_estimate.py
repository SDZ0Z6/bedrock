"""用 CloudWatch 的 token 量 × AWS 牌价，估算当天的 Bedrock 花费。

**为什么要有这一页**：Cost Explorer 的数据有一到两天延迟，今天花了多少要等到后天
才看得见。CloudWatch 的指标是分钟级的，用它乘单价就能立刻知道大概花了多少。

    估算成本 = 输入×输入价 + 输出×输出价 + 缓存读×缓存读价 + 缓存写×缓存写价

四个计数器**互相独立，不重复计算**——这一点是实测的：某天输入 32 亿、缓存读 765 亿，
缓存读比输入大 24 倍，`InputTokenCount` 不可能包含它。所以输入那一项不用减缓存。

单价来自 AWS 官方价目表（见 pricing 模块），按 ModelId 走哪一档自动选跨区/本区。
2026-08 拿一天 10 万美金的真实账单逐模型核对过，**整体误差 0.06%**。

**这是 AWS 公开牌价**，不含 EDP 或私有折扣；也**不套台账的 TAG_RATIO /
UNTAG_RATIO**，出来的是 AWS 原始成本，不是加价后的对外金额。

已知的低估来源（这一版有意不做）：
  · 1M 上下文档位单价更高，但 AWS 价目表里没有对应 SKU，无从取值；
  · 缓存写的 1 小时档比 5 分钟档贵 60%，CloudWatch 只有一个计数器，区分不了。
    实测按 5 分钟档能对上账单，说明这部分流量占比很小。
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

from . import config
from .cloudwatch_metrics import (
    DEFAULT_REGIONS,
    MAX_QUERIES_PER_CALL,
    NAMESPACE,
    ProfileInfo,
    _cached,
    _client,
    _store,
    list_model_ids,
    looks_like_profile_id,
    resolve_profiles,
    short_model_name,
    strip_cris_prefix,
)
from .cost_explorer import friendly_error
from .excel_source import Account
from .usage_explorer import MAX_SERIES, OTHER_LABEL, Series, assign_slots
from .pricing import (
    GLOBAL,
    KIND_LABELS as PRICE_KIND_LABELS,
    KINDS,
    STANDARD,
    ModelPrice,
    PriceTable,
    PricingError,
    load_prices,
    to_service_name,
)

# CloudWatch 指标名 -> 我们的四种口径
METRIC_TO_KIND = {
    "InputTokenCount": "input",
    "OutputTokenCount": "output",
    "CacheReadInputTokenCount": "cache_read",
    "CacheWriteInputTokenCount": "cache_write",
}
TOKEN_METRICS = tuple(METRIC_TO_KIND)

# 表格里四种 token 的展示顺序与中文名（单价列同序）
KIND_ORDER = KINDS
KIND_LABELS = PRICE_KIND_LABELS

# 一天一个桶
DAY_SECONDS = 86400

_data_cache: dict[tuple, object] = {}


def clear_cache() -> None:
    _data_cache.clear()


# --------------------------------------------------------------- 计价档判定
def tier_of_profile(info: ProfileInfo | None) -> str:
    """应用推理配置走的是跨区还是本区。

    判据是 modelArn 里有没有**不带区域**的那一条
    （`arn:aws:bedrock:::foundation-model/...`）——跨区配置会带上它，本区配置
    只有 `arn:aws:bedrock:us-east-1::foundation-model/...`。

    刻意不看配置名：不同账号的命名规则完全不同，猜名字迟早出错（模型用量页
    判定标签时也是同样的理由）。名字里的 "global" 只能当旁证。

    实测：该账号 11 个配置全部带无区域 ARN，描述里也写着 Global，按跨区价算出来
    和账单误差 0.06%。反向的例子（本区配置）暂时没有样本，所以留了
    PRICE_TIER 这个开关可以强制覆盖。
    """
    if info is None:
        return config.PRICE_TIER if config.PRICE_TIER in (GLOBAL, STANDARD) else GLOBAL
    for arn in info.model_arns:
        parts = arn.split(":")
        # arn:aws:bedrock:<region>:<account>:resource —— 第 4 段是区域
        if len(parts) > 3 and not parts[3]:
            return GLOBAL
    return STANDARD


def tier_of(model_id: str, profiles: dict[str, ProfileInfo]) -> str:
    """一条 CloudWatch 流量该按哪一档计价。

    直连模型的 ModelId 自带前缀：`global.` 是全球跨区（便宜约 10%），
    `us.` / `eu.` 之类是地理级跨区，走的是本区价。
    """
    if config.PRICE_TIER in (GLOBAL, STANDARD):
        return config.PRICE_TIER  # 手工覆盖，排查用
    if looks_like_profile_id(model_id):
        return tier_of_profile(profiles.get(model_id))
    return GLOBAL if model_id.startswith("global.") else STANDARD


def underlying_model(model_id: str, profiles: dict[str, ProfileInfo]) -> str:
    """把一条流量归到某个原厂模型上，用作明细表的分组键。

    两件事一起做：推理配置归并到它的底层模型，以及**剥掉跨区前缀**。后者容易漏——
    直连的 `global.anthropic.claude-opus-5` 和走配置归并出来的
    `anthropic.claude-opus-5` 其实是同一个模型，不剥的话表格里会出现两行同名的
    claude-opus-5（short_model_name 会把前缀显示掉，看上去一模一样）。

    计价档不在这里判断——它由 tier_of 单独给出，和模型一起构成分组键。
    """
    info = profiles.get(model_id)
    if info is not None and info.model:
        return info.model  # resolve_profiles 已经剥过前缀
    return strip_cris_prefix(model_id)


# --------------------------------------------------------------- 数据结构
@dataclass
class EstimateRow:
    """明细表的一行：一个模型在一个计价档下的用量与花费。"""

    model_id: str                       # 归并后的底层模型，如 anthropic.claude-opus-4-8
    tier: str
    tokens: dict[str, float] = field(default_factory=dict)
    price: ModelPrice | None = None

    @property
    def label(self) -> str:
        return short_model_name(self.model_id)

    @property
    def tier_label(self) -> str:
        return "跨区" if self.tier == GLOBAL else "本区"

    @property
    def priced(self) -> bool:
        return self.price is not None and self.price.complete

    @property
    def cost(self) -> float:
        return self.price.cost(self.tokens) if self.price else 0.0

    @property
    def total_tokens(self) -> float:
        return sum(self.tokens.values())

    def count(self, kind: str) -> float:
        return self.tokens.get(kind, 0.0)

    def rate_per_million(self, kind: str) -> float | None:
        return self.price.per_million(kind) if self.price else None


@dataclass
class EstimateReport:
    start: date
    end: date
    rows: list[EstimateRow] = field(default_factory=list)
    # 下面三个字段的形状是照着 usage_explorer.UsageReport 来的，这样
    # chart.render_stacked_bars 能直接吃，配色也和成本页共用同一套色槽
    dates: list[str] = field(default_factory=list)           # 每个桶的 ISO 日期
    labels: list[str] = field(default_factory=list)          # 轴上的短标签
    series: list[Series] = field(default_factory=list)       # 每个模型每天的钱
    errors: list[str] = field(default_factory=list)
    unpriced: list[str] = field(default_factory=list)        # 没在价目表里找到的模型
    any_cached: bool = False
    price_stale: bool = False
    price_error: str | None = None

    # chart.render_stacked_bars 会读这两个属性来写 SVG 的 aria-label。
    # 这一页永远按日、永远按模型分线，所以是常量。
    granularity: str = "daily"
    dimension_label: str = "模型"

    @property
    def total_cost(self) -> float:
        return sum(row.cost for row in self.rows)

    @property
    def has_data(self) -> bool:
        return any(row.total_tokens for row in self.rows)

    def kind_total(self, kind: str) -> float:
        """某一种 token 的总量。"""
        return sum(row.count(kind) for row in self.rows)

    def kind_cost(self, kind: str) -> float:
        """某一种 token 花了多少钱——用来看钱主要花在哪一类上。"""
        return sum(
            row.count(kind) * row.price.rate(kind) for row in self.rows if row.price
        )

    @property
    def column_totals(self) -> list[float]:
        """每天的总花费。图表和表格的合计行共用。"""
        return [
            sum((s.marked[i] for s in self.series), 0.0) for i in range(len(self.dates))
        ]

    @property
    def peak(self) -> tuple[int, float]:
        """(最高的那天的下标, 金额)。图上只在这一处做直接标注。"""
        totals = self.column_totals
        if not totals:
            return -1, 0.0
        index = max(range(len(totals)), key=totals.__getitem__)
        # 全是 0 的时候没有「最高的那天」可言，返回 -1 让模板显示占位符
        return (index, totals[index]) if totals[index] > 0 else (-1, 0.0)

    @property
    def daily_average(self) -> float:
        totals = self.column_totals
        return sum(totals) / len(totals) if totals else 0.0


# --------------------------------------------------------------- 取数
def build_days(start: date, end: date) -> tuple[list[datetime], list[str], list[str]]:
    """按 UTC 自然日切桶，返回 (查询用的时间戳, ISO 日期, 轴上的短标签)。

    CloudWatch 的 Period=86400 就是对齐到 UTC 零点的，所以桶边界天然就是 UTC 自然日。
    """
    stamps, dates, labels = [], [], []
    day = start
    while day <= end:
        stamps.append(datetime(day.year, day.month, day.day, tzinfo=timezone.utc))
        dates.append(day.isoformat())
        labels.append(day.isoformat()[5:])  # MM-DD
        day += timedelta(days=1)
    return stamps, dates, labels


def _fetch_region(
    account: Account, region: str, start: date, end: date, stamps: list[datetime]
) -> tuple[dict[tuple[str, str], list[float]], bool, str | None]:
    """返回 {(ModelId, 指标): 按天对齐的 token 数}。"""
    cache_key = (
        account.ak[-6:], account.account, region,
        start.isoformat(), end.isoformat(), "estimate",
    )
    hit = _cached(_data_cache, cache_key)
    if hit is not None:
        return hit, True, None  # type: ignore[return-value]

    index = {stamp: i for i, stamp in enumerate(stamps)}
    width = len(stamps)
    rows: dict[tuple[str, str], list[float]] = {}

    try:
        model_ids = list_model_ids(account, region)
        if not model_ids:
            _store(_data_cache, cache_key, rows)
            return rows, False, None

        client = _client(account, "cloudwatch", region)
        queries, labels = [], {}
        for i, model_id in enumerate(sorted(model_ids)):
            for j, metric in enumerate(TOKEN_METRICS):
                query_id = f"e{i}x{j}"
                labels[query_id] = (model_id, metric)
                queries.append({
                    "Id": query_id,
                    "MetricStat": {
                        "Metric": {
                            "Namespace": NAMESPACE,
                            "MetricName": metric,
                            "Dimensions": [{"Name": "ModelId", "Value": model_id}],
                        },
                        "Period": DAY_SECONDS,
                        "Stat": "Sum",
                    },
                    "ReturnData": True,
                })

        window_end = datetime(end.year, end.month, end.day, tzinfo=timezone.utc) + timedelta(days=1)
        for offset in range(0, len(queries), MAX_QUERIES_PER_CALL):
            chunk = queries[offset : offset + MAX_QUERIES_PER_CALL]
            token = None
            while True:
                kwargs = {
                    "MetricDataQueries": chunk,
                    "StartTime": stamps[0],
                    "EndTime": window_end,
                }
                if token:
                    kwargs["NextToken"] = token
                response = client.get_metric_data(**kwargs)
                for result in response.get("MetricDataResults", []):
                    key = labels.get(result["Id"])
                    if not key:
                        continue
                    bucket = rows.setdefault(key, [0.0] * width)
                    for stamp, value in zip(result.get("Timestamps", []), result.get("Values", [])):
                        position = index.get(stamp.astimezone(timezone.utc))
                        if position is not None:
                            bucket[position] += value
                token = response.get("NextToken")
                if not token:
                    break
    except Exception as exc:
        return {}, False, friendly_error(exc, account)

    rows = {key: values for key, values in rows.items() if any(values)}
    _store(_data_cache, cache_key, rows)
    return rows, False, None


def build_estimate(
    accounts: list[Account],
    start: date,
    end: date,
    regions: list[str] | None = None,
    refresh: bool = False,
) -> EstimateReport:
    """把若干账号 × 四个美区的 token 量换算成预估花费。

    四个美区单价一致（已核对），所以区域只是取数维度，不影响计价。
    """
    picked = regions or list(DEFAULT_REGIONS)
    stamps, dates, labels = build_days(start, end)
    report = EstimateReport(start=start, end=end, dates=dates, labels=labels)

    try:
        table = load_prices(force=refresh)
    except PricingError as exc:
        report.errors.append(str(exc))
        return report
    report.price_stale = table.stale
    report.price_error = table.error

    if not accounts:
        return report
    if refresh:
        clear_cache()

    width = len(stamps)
    # (底层模型, 计价档) -> {口径: 按天的 token 数}
    buckets: dict[tuple[str, str], dict[str, list[float]]] = {}
    unpriced: set[str] = set()

    def run(job):
        account, region = job
        rows, cached, error = _fetch_region(account, region, start, end, stamps)
        return account, region, rows, cached, error

    jobs = [(a, r) for a in accounts for r in picked]
    workers = max(1, min(config.MAX_WORKERS, len(jobs)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for account, region, rows, cached, error in pool.map(run, jobs):
            if error:
                report.errors.append(
                    f"{account.partner} / {account.account} @ {region}：{error}"
                )
                continue
            if cached:
                report.any_cached = True
            if not rows:
                continue

            profiles, _ = resolve_profiles(account, region)
            for (model_id, metric), values in rows.items():
                kind = METRIC_TO_KIND.get(metric)
                if kind is None:
                    continue
                key = (underlying_model(model_id, profiles), tier_of(model_id, profiles))
                slot = buckets.setdefault(key, {})
                daily = slot.setdefault(kind, [0.0] * width)
                for i, value in enumerate(values):
                    daily[i] += value

    daily_cost: dict[str, list[float]] = {}
    for (model_id, tier), kinds in sorted(buckets.items()):
        service_name = to_service_name(model_id)
        price = table.get(service_name, tier) if service_name else None
        if price is None or not price.complete:
            unpriced.add(model_id)
        row = EstimateRow(
            model_id=model_id,
            tier=tier,
            tokens={kind: sum(daily) for kind, daily in kinds.items()},
            price=price if (price and price.complete) else None,
        )
        report.rows.append(row)

        if row.price is None:
            continue  # 没单价的不进图，否则等于悄悄按 0 画进去
        # 每天的钱：四种 token 各自按天乘单价再相加
        line = daily_cost.setdefault(row.label, [0.0] * width)
        for kind, daily in kinds.items():
            rate = row.price.rate(kind)
            for i, value in enumerate(daily):
                line[i] += value * rate

    report.rows.sort(key=lambda r: (-r.cost, -r.total_tokens, r.label))
    report.unpriced = sorted(unpriced)
    report.series = _to_series(daily_cost, width)
    return report


def _to_series(daily_cost: dict[str, list[float]], width: int) -> list[Series]:
    """把 {模型: 每天的钱} 转成图表用的序列。

    超过 MAX_SERIES 条就把尾巴折进「其他」——和成本页同样的处理，否则图例会
    长得没法看，色槽也不够分。颜色槽用 assign_slots 按名字分配，和其他页共用
    同一套规则，这样同一个模型在不同页面上是同一个颜色。
    """
    ranked = sorted(daily_cost.items(), key=lambda kv: -sum(kv[1]))
    keep, folded = ranked[:MAX_SERIES], ranked[MAX_SERIES:]
    slots = assign_slots([name for name, _ in keep])

    series = [
        Series(name=name, raw=list(values), marked=list(values), slot=slots.get(name, 0))
        for name, values in keep
    ]
    if folded:
        merged = [0.0] * width
        for _, values in folded:
            for i, value in enumerate(values):
                merged[i] += value
        series.append(Series(name=OTHER_LABEL, raw=merged, marked=list(merged), slot=-1))
    return series
