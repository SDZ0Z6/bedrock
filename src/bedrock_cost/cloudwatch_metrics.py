"""CloudWatch 里的 Bedrock 调用量与 token 量。

命名空间 AWS/Bedrock，维度只用 [ModelId]。已实测确认：
  · 把所有 ModelId 的 Invocations 相加，正好等于不带维度的账号级总量（差 0），
    所以逐模型求和不会重复计数，也不会漏。
  · [ModelId, ContextWindow] 是 [ModelId] 的子集（1M 上下文约占 7%），
    属于细分而不是独立的流，绝不能和 [ModelId] 相加。
  · 原厂模型流（global.anthropic.*）和应用推理配置流（12 位不透明 ID）互不相交：
    走配置的流量只记在配置上。所以把配置归并到底层模型是无损的。

ModelId 有两种形态：
    global.anthropic.claude-opus-4-8   直连原厂/跨区模型
    2kbsta0lwebx                       应用推理配置，用 bedrock:ListInferenceProfiles
                                       解析成 map-global-claude-opus-4-8-use1
                                       及其底层模型 anthropic.claude-opus-4-8

**有标签 / 无标签**就落在这个区别上。CloudWatch 的指标本身不带成本分配标签，
但实测确认：应用推理配置上带着 map-migrated 标签，且值与台账 TAG 列一致
（两个账号各 11 / 9 个配置全部带），而直连调用没有任何标签。所以

    走推理配置的调用   -> 有标签
    直连模型的调用     -> 无标签

判定用的是 bedrock:ListTagsForResource 读到的真实标签，不靠配置名猜——不同账号
的命名规则完全不同（map-global-claude-opus-4-8-use1 vs claude48oupsauto_0706）。
匹配规则和 cost_explorer 保持一致：台账写死了值就要精确相等，只给键就是任意非空。

需要的 IAM 权限：cloudwatch:ListMetrics、cloudwatch:GetMetricData、
bedrock:ListInferenceProfiles、bedrock:ListTagsForResource。缺后两个不会让页面
崩掉，但标签判定会失效（全部落入无标签），届时页面上会明确提示。
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import boto3
from botocore.config import Config as BotoConfig

from . import config
from .cost_explorer import friendly_error
from .excel_source import Account
from .usage_explorer import MAX_SERIES, OTHER_LABEL, assign_slots
from .windows import MetricWindow

NAMESPACE = "AWS/Bedrock"

# 美国四区
REGIONS: dict[str, str] = {
    "us-east-1": "弗吉尼亚",
    "us-east-2": "俄亥俄",
    "us-west-1": "北加州",
    "us-west-2": "俄勒冈",
}
DEFAULT_REGIONS = list(REGIONS)

# 推理配置的名字里通常带区域后缀（map-global-claude-opus-4-6-v1-use1）。
# 既然区域是合并的，就得把它去掉，否则同一个配置会被拆成四条线，
# 反而把真正的大头挤进「其他」。
REGION_SHORT = {
    "us-east-1": "use1",
    "us-east-2": "use2",
    "us-west-1": "usw1",
    "us-west-2": "usw2",
}

# 指标 -> (显示名, 需要相加的 CloudWatch 指标, 单位)
# 调用次数和 token 不能画在同一个 Y 轴上（两个刻度的对齐是任意的，会凭空
# 造出相关性），所以这里是单选，一次只画一种。
METRICS: dict[str, tuple[str, tuple[str, ...], str]] = {
    "invocations": ("调用次数", ("Invocations",), "次"),
    "input_tokens": ("输入 Token", ("InputTokenCount",), "token"),
    "output_tokens": ("输出 Token", ("OutputTokenCount",), "token"),
    "total_tokens": ("总 Token", ("InputTokenCount", "OutputTokenCount"), "token"),
}
DEFAULT_METRIC = "invocations"

# 标签筛选。图例始终按模型分线，这里只决定算哪部分流量。
TAG_FILTERS: dict[str, str] = {
    "all": "全部",
    "tagged": "仅有标签",
    "untagged": "仅无标签",
}
DEFAULT_TAG_FILTER = "all"

# 配置和标签属于配置信息，变动远比指标少，单独给一个更长的缓存期，
# 免得每次换时间窗口都重新拉一遍 ListTagsForResource
PROFILE_CACHE_TTL = 3600

# 跨区推理（CRIS）前缀，归并原厂模型时剥掉
CRIS_PREFIXES = ("global.", "us-gov.", "us.", "eu.", "apac.", "jp.", "au.")

# GetMetricData 单次最多 500 个查询
MAX_QUERIES_PER_CALL = 500


# --------------------------------------------------------------- 模型名归一
def strip_cris_prefix(model_id: str) -> str:
    for prefix in CRIS_PREFIXES:
        if model_id.startswith(prefix):
            return model_id[len(prefix) :]
    return model_id


def short_model_name(model_id: str) -> str:
    """anthropic.claude-opus-4-8 -> claude-opus-4-8，图例里短一点。"""
    text = strip_cris_prefix(model_id)
    provider, _, rest = text.partition(".")
    return rest or provider


def looks_like_profile_id(model_id: str) -> bool:
    """应用推理配置在 CloudWatch 里是 12 位小写字母数字，没有点也没有冒号。"""
    return (
        len(model_id) == 12
        and model_id.isalnum()
        and model_id.islower()
        and "." not in model_id
    )


# --------------------------------------------------------------- 数据结构
@dataclass(frozen=True)
class ProfileInfo:
    """一个应用推理配置。"""

    name: str
    model: str       # 底层原厂模型，已剥掉跨区前缀
    tag_value: str   # 台账那个标签键的值；空串表示没打这个标签
    # 原始的 modelArn 列表，未做任何加工。计价要靠它判断跨区还是本区
    # （见 cost_estimate.tier_of_profile），所以这里刻意保留全貌。
    model_arns: tuple[str, ...] = ()


@dataclass
class MetricSeries:
    name: str
    values: list[float]
    slot: int = 0

    @property
    def total(self) -> float:
        return sum(self.values)

    @property
    def peak(self) -> float:
        return max(self.values, default=0.0)


@dataclass
class RegionPanel:
    """一个区域一张小图。

    四个面板的 series 顺序、名字、颜色槽完全一致（某个区没有某个模型时补零），
    这样同一个模型在四张图里是同一个颜色——小倍数图的前提。
    """

    region: str
    series: list[MetricSeries] = field(default_factory=list)

    @property
    def label(self) -> str:
        return f"{REGIONS.get(self.region, '')} {self.region}".strip()

    @property
    def total(self) -> float:
        return sum(s.total for s in self.series)

    @property
    def width(self) -> int:
        return len(self.series[0].values) if self.series else 0

    @property
    def column_totals(self) -> list[float]:
        return [
            sum((s.values[i] for s in self.series), 0.0) for i in range(self.width)
        ]

    @property
    def peak(self) -> tuple[int, float]:
        totals = self.column_totals
        if not totals:
            return -1, 0.0
        top = max(range(len(totals)), key=lambda i: totals[i])
        if totals[top] <= 0:
            return -1, 0.0
        return top, totals[top]

    @property
    def has_data(self) -> bool:
        return self.total > 0


@dataclass
class UsageMetricsReport:
    window: MetricWindow
    metric_key: str
    tag_filter: str = DEFAULT_TAG_FILTER
    # 有标签 / 无标签的全量拆分，不受 tag_filter 影响——汇总处始终展示
    tagged_total: float = 0.0
    untagged_total: float = 0.0
    tags_resolved: bool = True
    regions: list[str] = field(default_factory=list)
    timestamps: list[datetime] = field(default_factory=list)
    labels: list[str] = field(default_factory=list)
    # 四个区域合并后的序列，用于图例和明细表
    series: list[MetricSeries] = field(default_factory=list)
    # 逐区域的面板，用于 2×2 小倍数图
    panels: list[RegionPanel] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    any_cached: bool = False
    folded_count: int = 0

    @property
    def region_totals(self) -> dict[str, float]:
        return {panel.region: panel.total for panel in self.panels}

    @property
    def shared_peak(self) -> float:
        """四个面板共用的 Y 轴上限来源：所有面板里单条序列的最大单点值。

        折线是叠放不是堆叠，所以取单点最大值而不是各条之和；四张图共用一个刻度，
        否则小区看起来和大区一样高，横向对比就失去意义。
        """
        return max(
            (s.peak for panel in self.panels for s in panel.series),
            default=0.0,
        )

    @property
    def metric_label(self) -> str:
        return METRICS[self.metric_key][0]

    @property
    def unit(self) -> str:
        return METRICS[self.metric_key][2]

    @property
    def tag_filter_label(self) -> str:
        return TAG_FILTERS.get(self.tag_filter, self.tag_filter)

    @property
    def split_total(self) -> float:
        """有标签 + 无标签，即不加筛选时的四区总量。"""
        return self.tagged_total + self.untagged_total

    @property
    def tagged_share(self) -> float | None:
        total = self.split_total
        return self.tagged_total / total * 100 if total > 0 else None

    @property
    def untagged_share(self) -> float | None:
        total = self.split_total
        return self.untagged_total / total * 100 if total > 0 else None

    @property
    def is_filtered(self) -> bool:
        return self.tag_filter != DEFAULT_TAG_FILTER

    @property
    def total(self) -> float:
        return sum(s.total for s in self.series)

    @property
    def column_totals(self) -> list[float]:
        return [
            sum((s.values[i] for s in self.series), 0.0)
            for i in range(len(self.timestamps))
        ]

    @property
    def peak(self) -> tuple[int, float]:
        totals = self.column_totals
        if not totals:
            return -1, 0.0
        top = max(range(len(totals)), key=lambda i: totals[i])
        if totals[top] <= 0:
            return -1, 0.0
        return top, totals[top]

    @property
    def has_data(self) -> bool:
        return bool(self.series) and self.total > 0


# --------------------------------------------------------------- 时间栅格
def build_grid(window: MetricWindow) -> tuple[list[datetime], list[str]]:
    """生成本地时区的时间桶。

    end 已经对齐到 Period 边界，所以最后一个桶是完整的——不会出现「当前这
    一小时才过了几分钟」导致最后一个点凭空塌下去的假象。
    """
    stamps: list[datetime] = []
    cursor = window.start
    step = timedelta(seconds=window.period)
    while cursor < window.end:
        stamps.append(cursor)
        cursor += step

    local = datetime.now().astimezone().tzinfo
    if window.period >= 86400:
        fmt = "%m-%d"
    elif window.period >= 3600:
        fmt = "%m-%d %H:00"
    else:
        fmt = "%m-%d %H:%M"
    labels = [s.astimezone(local).strftime(fmt) for s in stamps]
    return stamps, labels


# --------------------------------------------------------------- 缓存
_lock = threading.Lock()
_models_cache: dict[tuple, tuple[float, list[str]]] = {}
_profiles_cache: dict[tuple, tuple[float, dict[str, tuple[str, str]]]] = {}
_data_cache: dict[tuple, tuple[float, dict[str, list[float]]]] = {}


def clear_cache() -> None:
    with _lock:
        _models_cache.clear()
        _profiles_cache.clear()
        _data_cache.clear()


def _cached(store: dict, key: tuple, ttl: int | None = None):
    limit = config.CACHE_TTL if ttl is None else ttl
    if config.CACHE_TTL <= 0:
        return None  # 关掉缓存时连配置信息也一起关，行为可预期
    with _lock:
        hit = store.get(key)
    if hit and time.time() - hit[0] < limit:
        return hit[1]
    return None


def _store(store: dict, key: tuple, value) -> None:
    if config.CACHE_TTL > 0:
        with _lock:
            store[key] = (time.time(), value)


def _client(account: Account, service: str, region: str):
    return boto3.client(
        service,
        region_name=region,
        aws_access_key_id=account.ak,
        aws_secret_access_key=account.sk,
        config=BotoConfig(
            read_timeout=config.CE_TIMEOUT,
            connect_timeout=config.CE_TIMEOUT,
            retries={"max_attempts": config.CE_RETRIES, "mode": "standard"},
        ),
    )


# --------------------------------------------------------------- 发现与解析
def list_model_ids(account: Account, region: str) -> list[str]:
    """列出该账号该区近两周有过数据的 ModelId（只取 [ModelId] 这一种维度组合）。"""
    key = (account.ak[-6:], account.account, region, "models")
    hit = _cached(_models_cache, key)
    if hit is not None:
        return hit

    client = _client(account, "cloudwatch", region)
    found: set[str] = set()
    for page in client.get_paginator("list_metrics").paginate(Namespace=NAMESPACE):
        for metric in page.get("Metrics", []):
            dims = {d["Name"]: d["Value"] for d in metric.get("Dimensions", [])}
            # 只要恰好只有 ModelId 的那一组；带 ContextWindow 的是子集，会重复计数
            if set(dims) == {"ModelId"}:
                found.add(dims["ModelId"])
    models = sorted(found)
    _store(_models_cache, key, models)
    return models


def resolve_profiles(account: Account, region: str) -> tuple[dict[str, ProfileInfo], bool]:
    """推理配置 ID -> ProfileInfo，外加「标签有没有真的读到」。

    标签得逐个配置调 ListTagsForResource（list_inference_profiles 不返回标签），
    所以并发拉取，并用更长的 TTL 缓存——配置和标签变动远比指标少。
    """
    key = (account.ak[-6:], account.account, region, account.tag_key, "profiles")
    hit = _cached(_profiles_cache, key, PROFILE_CACHE_TTL)
    if hit is not None:
        return hit

    mapping: dict[str, ProfileInfo] = {}
    tags_available = False
    try:
        client = _client(account, "bedrock", region)
        summaries: list[dict] = []
        token = None
        while True:
            kwargs = {"maxResults": 100, "typeEquals": "APPLICATION"}
            if token:
                kwargs["nextToken"] = token
            page = client.list_inference_profiles(**kwargs)
            summaries.extend(page.get("inferenceProfileSummaries", []))
            token = page.get("nextToken")
            if not token:
                break

        def fetch_tags(summary: dict):
            arn = summary.get("inferenceProfileArn", "")
            if not arn:
                return summary, None
            try:
                tags = client.list_tags_for_resource(resourceARN=arn).get("tags", [])
            except Exception:
                return summary, None  # 没权限读标签
            return summary, {t["key"]: t["value"] for t in tags}

        if summaries:
            workers = max(1, min(config.MAX_WORKERS, len(summaries)))
            with ThreadPoolExecutor(max_workers=workers) as pool:
                for summary, tags in pool.map(fetch_tags, summaries):
                    if tags is not None:
                        tags_available = True
                    models = [
                        m.get("modelArn", "").rsplit("/", 1)[-1]
                        for m in summary.get("models", [])
                        if m.get("modelArn")
                    ]
                    arns = tuple(
                        m.get("modelArn", "")
                        for m in summary.get("models", [])
                        if m.get("modelArn")
                    )
                    mapping[summary["inferenceProfileId"]] = ProfileInfo(
                        name=(
                            summary.get("inferenceProfileName")
                            or summary["inferenceProfileId"]
                        ),
                        model=strip_cris_prefix(models[0]) if models else "",
                        tag_value=(tags or {}).get(account.tag_key, ""),
                        model_arns=arns,
                    )
    except Exception:
        # 没有 bedrock 权限也要能出图，只是标签判定会失效（页面上会明确提示）
        mapping, tags_available = {}, False

    result = (mapping, tags_available)
    _store(_profiles_cache, key, result)
    return result


def strip_region_suffix(name: str, region: str) -> str:
    """去掉配置名末尾的区域后缀。

    只剥掉「这一行数据实际所属区域」的短码，不做模式猜测：拿 us-east-1 的数据
    就只尝试去掉 -use1，名字对不上就原样保留。
    """
    code = REGION_SHORT.get(region)
    if not code:
        return name
    for separator in ("-", "_", "."):
        suffix = separator + code
        if name.lower().endswith(suffix):
            return name[: -len(suffix)]
    return name


def group_key(model_id: str, profiles: dict[str, ProfileInfo]) -> str:
    """把一个 ModelId 归到图例上的某条序列。

    图例一律按模型：走配置的流量归并进它的底层模型，所以同一个模型的有标签和
    无标签流量落在同一条线上（要分开看就用标签筛选）。
    """
    info = profiles.get(model_id)
    if info is not None and info.model:
        return short_model_name(info.model)
    if looks_like_profile_id(model_id):
        return f"未知配置 {model_id}"
    return short_model_name(model_id)


def is_tagged(model_id: str, profiles: dict[str, ProfileInfo], account: Account) -> bool:
    """这条流量算不算「有标签」。

    规则和 cost_explorer 完全一致：台账把值写死了就要精确相等，只给键就是任意
    非空值。直连模型（不是推理配置）永远算无标签——它确实没打标签。
    """
    info = profiles.get(model_id)
    if info is None or not info.tag_value:
        return False
    expected = account.tag_value
    return info.tag_value == expected if expected else True


# --------------------------------------------------------------- 取数
def _fetch_region(
    account: Account, region: str, window: MetricWindow, metric_key: str
) -> tuple[dict[str, list[float]], bool, str | None]:
    """返回 {ModelId: 按桶对齐的值}。"""
    cache_key = (
        account.ak[-6:], account.account, region, metric_key,
        window.start.isoformat(), window.end.isoformat(), window.period_key,
    )
    hit = _cached(_data_cache, cache_key)
    if hit is not None:
        return hit, True, None

    stamps, _ = build_grid(window)
    width = len(stamps)
    index = {int(s.timestamp()): i for i, s in enumerate(stamps)}
    metric_names = METRICS[metric_key][1]

    try:
        models = list_model_ids(account, region)
        if not models:
            return {}, False, None

        client = _client(account, "cloudwatch", region)
        queries = []
        lookup: dict[str, str] = {}
        for m_index, model in enumerate(models):
            for n_index, metric_name in enumerate(metric_names):
                query_id = f"q{m_index}_{n_index}"
                lookup[query_id] = model
                queries.append(
                    {
                        "Id": query_id,
                        "MetricStat": {
                            "Metric": {
                                "Namespace": NAMESPACE,
                                "MetricName": metric_name,
                                "Dimensions": [{"Name": "ModelId", "Value": model}],
                            },
                            "Period": window.period,
                            "Stat": "Sum",
                        },
                        "ReturnData": True,
                    }
                )

        # 这些都是计数类指标，CloudWatch 在没有流量时不发点，所以缺失就是 0，
        # 补 0 是正确的（延迟类指标就不能这么补，那种缺失应该断线）。
        rows: dict[str, list[float]] = {m: [0.0] * width for m in models}
        for batch_start in range(0, len(queries), MAX_QUERIES_PER_CALL):
            batch = queries[batch_start : batch_start + MAX_QUERIES_PER_CALL]
            token = None
            while True:
                kwargs = {
                    "MetricDataQueries": batch,
                    "StartTime": window.start,
                    "EndTime": window.end,
                    "ScanBy": "TimestampAscending",
                }
                if token:
                    kwargs["NextToken"] = token
                response = client.get_metric_data(**kwargs)
                for result in response.get("MetricDataResults", []):
                    model = lookup.get(result["Id"])
                    if model is None:
                        continue
                    target = rows[model]
                    for stamp, value in zip(
                        result.get("Timestamps", []), result.get("Values", [])
                    ):
                        position = index.get(int(stamp.timestamp()))
                        if position is not None:
                            target[position] += float(value)
                token = response.get("NextToken")
                if not token:
                    break
    except Exception as exc:
        return {}, False, friendly_error(exc, account)

    rows = {model: values for model, values in rows.items() if any(values)}
    _store(_data_cache, cache_key, rows)
    return rows, False, None


def build_metrics(
    accounts: list[Account],
    regions: list[str],
    window: MetricWindow,
    metric_key: str = DEFAULT_METRIC,
    tag_filter: str = DEFAULT_TAG_FILTER,
    refresh: bool = False,
) -> UsageMetricsReport:
    """把多个账号 × 多个区域的数据合并成一份折线图报表。

    tag_filter 只影响进入图表和明细表的序列；有标签/无标签的全量拆分照样统计，
    这样切到「仅有标签」时汇总处仍然能看到被排除掉的那部分有多少。
    """
    if metric_key not in METRICS:
        metric_key = DEFAULT_METRIC
    if tag_filter not in TAG_FILTERS:
        tag_filter = DEFAULT_TAG_FILTER
    picked = [r for r in regions if r in REGIONS] or DEFAULT_REGIONS

    stamps, labels = build_grid(window)
    report = UsageMetricsReport(
        window=window, metric_key=metric_key, tag_filter=tag_filter, regions=picked,
        timestamps=stamps, labels=labels,
    )
    if not accounts:
        return report

    if refresh:
        clear_cache()

    jobs = [(account, region) for account in accounts for region in picked]
    width = len(stamps)
    # 合并口径（图例、明细表）和逐区域口径（四张小图）同时累积
    combined: dict[str, list[float]] = {}
    by_region: dict[str, dict[str, list[float]]] = {r: {} for r in picked}
    tags_resolved = False
    saw_profiles = False

    def run(job):
        account, region = job
        rows, cached, error = _fetch_region(account, region, window, metric_key)
        return account, region, rows, cached, error

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
            profiles, tags_ok = resolve_profiles(account, region)
            tags_resolved = tags_resolved or tags_ok
            for model_id, values in rows.items():
                tagged = is_tagged(model_id, profiles, account)
                if looks_like_profile_id(model_id):
                    saw_profiles = True
                amount = sum(values)
                if tagged:
                    report.tagged_total += amount
                else:
                    report.untagged_total += amount

                # 筛选只挡序列，不挡上面的拆分统计
                if tag_filter == "tagged" and not tagged:
                    continue
                if tag_filter == "untagged" and tagged:
                    continue

                name = group_key(model_id, profiles)
                total_target = combined.setdefault(name, [0.0] * width)
                region_target = by_region[region].setdefault(name, [0.0] * width)
                for position, value in enumerate(values):
                    total_target[position] += value
                    region_target[position] += value

    # 读不到标签时全部会落进「无标签」，这个必须说清楚，否则页面在撒谎
    report.tags_resolved = tags_resolved or not saw_profiles

    # 序列集合按「四区合计」排名后决定，四张图共用同一套名字和颜色槽——
    # 否则同一个模型在不同面板里会是不同颜色，小倍数图就没法读了
    ranked = sorted(combined.items(), key=lambda kv: -sum(kv[1]))
    kept, folded = ranked[:MAX_SERIES], ranked[MAX_SERIES:]
    report.folded_count = len(folded)
    folded_names = {name for name, _ in folded}

    slots = assign_slots([name for name, _ in kept])
    order = [(name, slots[name]) for name, _ in kept]

    for name, values in kept:
        report.series.append(MetricSeries(name=name, values=values, slot=slots[name]))
    if folded:
        merged = [0.0] * width
        for _, values in folded:
            for position, value in enumerate(values):
                merged[position] += value
        report.series.append(MetricSeries(name=OTHER_LABEL, values=merged, slot=-1))

    for region in picked:
        panel = RegionPanel(region=region)
        rows = by_region[region]
        for name, slot in order:
            panel.series.append(
                MetricSeries(
                    name=name,
                    values=list(rows.get(name, [0.0] * width)),
                    slot=slot,
                )
            )
        if folded:
            merged = [0.0] * width
            for name in folded_names:
                for position, value in enumerate(rows.get(name, [])):
                    merged[position] += value
            panel.series.append(MetricSeries(name=OTHER_LABEL, values=merged, slot=-1))
        report.panels.append(panel)

    return report


# --------------------------------------------------------------- 告警用：每小时调用次数
def hourly_invocations(
    account: Account,
    hour_starts: list[datetime],
    regions: list[str] | None = None,
) -> tuple[list[float] | None, list[str]]:
    """几个整点小时里、四个区合计的调用次数，给 Telegram「用量切换」告警用。

    返回 (与 hour_starts 对齐的次数, 出错的区)。**只要有一个区出错就返回 None**：
    缺一个区的零不能当成真的零，否则那个区的调用会被误报成「用量中断」。

    刻意逐 ModelId 相加，而不是查不带维度的账号级 Invocations：后者只在写这个模块时
    拿来对过一次账（差 0），生产代码从没依赖过它，而那几个账号的凭证现在都失效了，
    没法再验证它一定存在——GetMetricData 查一个不存在的指标也是返回空 + Complete，
    分不出「没用量」和「指标不存在」。逐 ModelId 这条路模型用量页天天在跑。

    ListMetrics 只列近两周有数据的指标，但这里只看最近几个小时：上一小时有调用的
    模型必然在两周内有数据、必然被列出来，所以对这个窗口没有缺口。
    """
    picked = regions or list(DEFAULT_REGIONS)
    if not hour_starts:
        return [], []
    starts = [h.astimezone(timezone.utc) for h in hour_starts]
    index = {stamp: i for i, stamp in enumerate(starts)}
    totals = [0.0] * len(starts)
    failed: list[str] = []

    for region in picked:
        try:
            model_ids = list_model_ids(account, region)
            if not model_ids:
                continue
            client = _client(account, "cloudwatch", region)
            queries = [
                {
                    "Id": f"h{i}",
                    "MetricStat": {
                        "Metric": {
                            "Namespace": NAMESPACE,
                            "MetricName": "Invocations",
                            "Dimensions": [{"Name": "ModelId", "Value": model_id}],
                        },
                        "Period": 3600,
                        "Stat": "Sum",
                    },
                    "ReturnData": True,
                }
                for i, model_id in enumerate(sorted(model_ids))
            ]
            for offset in range(0, len(queries), MAX_QUERIES_PER_CALL):
                chunk = queries[offset : offset + MAX_QUERIES_PER_CALL]
                token = None
                while True:
                    kwargs = {
                        "MetricDataQueries": chunk,
                        "StartTime": starts[0],
                        "EndTime": starts[-1] + timedelta(hours=1),
                    }
                    if token:
                        kwargs["NextToken"] = token
                    response = client.get_metric_data(**kwargs)
                    for result in response.get("MetricDataResults", []):
                        for stamp, value in zip(
                            result.get("Timestamps", []), result.get("Values", [])
                        ):
                            position = index.get(stamp.astimezone(timezone.utc))
                            if position is not None:
                                totals[position] += value
                    token = response.get("NextToken")
                    if not token:
                        break
        except Exception as exc:
            failed.append(f"{region}：{friendly_error(exc, account)}")

    if failed:
        return None, failed
    return totals, []
