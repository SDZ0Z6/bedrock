"""AWS 官方价目表：Bedrock 上 Claude 模型的 token 单价。

数据来自 AWS Price List 的**公开批量端点**，不需要任何凭证，也不需要给台账里的
账号加 `pricing:GetProducts` 权限：

    https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/
        AmazonBedrockFoundationModels/current/us-east-1/index.json

以下几条都是 2026-08 对着真实账单逐模型核对过的（一天 10 万美金的量，误差 0.06%）：

  · Claude **不在** `AmazonBedrock` 这个 service code 里。那里面只有 5 条
    Claude 2.x/3 时代的老 SKU，现役模型一条都没有。现役的在
    `AmazonBedrockFoundationModels` 里，`servicename` 恰好就是 Cost Explorer
    里的计费条目名（"Claude Opus 5 (Amazon Bedrock Edition)"）。
  · **四个美区单价完全一致**，所以只拉 us-east-1 一份就够，四区合并计价是安全的。
  · `usagetype` 有**两代命名**：老的是 `InputTokenCount`，新的是
    `input_tokens_standard`，不同世代的模型混着用，两套都得认。
  · **Global（跨区推理）比标准价便宜约 10%**，必须分开。走哪一档看 CloudWatch
    的 ModelId 有没有 `global.` 前缀。
  · 缓存写有 5 分钟和 1 小时两档（后者贵 60%），但 CloudWatch 只有一个
    `CacheWriteInputTokenCount` 指标，区分不了。实测按 5 分钟档算能对上账单。

**这是 AWS 公开牌价**，不含 EDP 或任何私有折扣。如果你的账号有协议价，这里估出来
的数会偏高。
"""

from __future__ import annotations

import json
import re
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from . import config

OFFER_CODE = "AmazonBedrockFoundationModels"
# 四个美区同价，拉一份即可。真要支持定价不同的区，把这里改成按区拉。
PRICE_REGION = "us-east-1"
BASE_URL = "https://pricing.us-east-1.amazonaws.com"

# 只认 Claude。别的厂商的模型这一版不估。
MODEL_PREFIX = "anthropic.claude-"
SERVICE_SUFFIX = " (Amazon Bedrock Edition)"

# 我们要的四种 token 口径
KINDS = ("input", "output", "cache_read", "cache_write")
KIND_LABELS = {
    "input": "输入",
    "output": "输出",
    "cache_read": "缓存读",
    "cache_write": "缓存写",
}

# usagetype 主体 -> (口径, 是否 Global)。
# 只收标准按需档：batch（五折）、1h 缓存写、Reserved TPM 都不在这一版口径里。
USAGE_TO_KIND: dict[str, tuple[str, bool]] = {
    # 老一代命名
    "inputtokencount": ("input", False),
    "inputtokencount_global": ("input", True),
    "outputtokencount": ("output", False),
    "outputtokencount_global": ("output", True),
    "cachereadinputtokencount": ("cache_read", False),
    "cachereadinputtokencount_global": ("cache_read", True),
    "cachewriteinputtokencount": ("cache_write", False),
    "cachewriteinputtokencount_global": ("cache_write", True),
    # 新一代命名
    "input_tokens_standard": ("input", False),
    "input_tokens_global_standard": ("input", True),
    "output_tokens_standard": ("output", False),
    "output_tokens_global_standard": ("output", True),
    "cache_read_tokens_standard": ("cache_read", False),
    "cache_read_tokens_global_standard": ("cache_read", True),
    "cache_write_tokens_standard": ("cache_write", False),
    "cache_write_tokens_global_standard": ("cache_write", True),
}

STANDARD = "standard"
GLOBAL = "global"
TIER_LABELS = {STANDARD: "标准", GLOBAL: "跨区"}

# 价目表一天拉一次足够——AWS 改价是以周/月计的
CACHE_TTL = 24 * 3600
FETCH_TIMEOUT = 30

# 落盘的解析结果。存解析后的表而不是 0.4 MB 的原始 JSON：小得多，重启后
# 不用重拉，AWS 端点临时不可达时也还有一份能用。
CACHE_NAME = "bedrock-prices.json"

# 8 位日期段（20251001）和末尾的 v1 都不是模型版本号的一部分
_DATE_PART = re.compile(r"^\d{8}$")
_VERSION_PART = re.compile(r"^v\d+$")


class PricingError(RuntimeError):
    """价目表拉不到、也没有可用的本地副本。"""


@dataclass(frozen=True)
class ModelPrice:
    """一个模型在一个计价档下的四个单价，单位是 USD / token。"""

    service_name: str
    tier: str
    input: float = 0.0
    output: float = 0.0
    cache_read: float = 0.0
    cache_write: float = 0.0

    @property
    def tier_label(self) -> str:
        return TIER_LABELS.get(self.tier, self.tier)

    @property
    def model_label(self) -> str:
        """去掉 "(Amazon Bedrock Edition)" 后缀，页面上短一点。"""
        return self.service_name.removesuffix(SERVICE_SUFFIX)

    def rate(self, kind: str) -> float:
        return float(getattr(self, kind, 0.0))

    def per_million(self, kind: str) -> float:
        """页面上展示的 $/1M tokens。"""
        return self.rate(kind) * 1_000_000

    @property
    def complete(self) -> bool:
        """四个单价齐不齐。缺了说明这个档 AWS 没有对应 SKU。"""
        return all(self.rate(kind) > 0 for kind in KINDS)

    def cost(self, counts: dict[str, float]) -> float:
        """按四种 token 数算钱。counts 的键是 KINDS。"""
        return sum(counts.get(kind, 0.0) * self.rate(kind) for kind in KINDS)


@dataclass
class PriceTable:
    prices: dict[tuple[str, str], ModelPrice]
    fetched_at: float = 0.0
    stale: bool = False          # 用的是本地旧副本，这次没拉到新的
    error: str | None = None     # 拉取失败的原因（stale 时才有意义）

    def get(self, service_name: str, tier: str) -> ModelPrice | None:
        return self.prices.get((service_name, tier))

    @property
    def model_count(self) -> int:
        return len({name for name, _ in self.prices})

    @property
    def age_hours(self) -> float:
        return max(0.0, (time.time() - self.fetched_at) / 3600) if self.fetched_at else 0.0


# --------------------------------------------------------------- 模型名映射
def tier_of(model_id: str) -> str:
    """走跨区推理还是本区。CloudWatch 的 ModelId 自带这个信息。"""
    return GLOBAL if model_id.startswith("global.") else STANDARD


def to_service_name(model_id: str) -> str | None:
    """CloudWatch 的 ModelId -> 价目表里的 servicename。

        anthropic.claude-opus-4-8                    -> Claude Opus 4.8 (…)
        global.anthropic.claude-opus-5               -> Claude Opus 5 (…)
        anthropic.claude-opus-4-6-v1                 -> Claude Opus 4.6 (…)
        anthropic.claude-haiku-4-5-20251001-v1:0     -> Claude Haiku 4.5 (…)

    认不出来（非 Claude、或者命名规则变了）就返回 None，由调用方显式报出来，
    绝不能悄悄按 0 算。
    """
    text = model_id
    for prefix in ("global.", "us.", "eu.", "apac.", "jp.", "au.", "us-gov."):
        if text.startswith(prefix):
            text = text[len(prefix) :]
            break
    if not text.startswith(MODEL_PREFIX):
        return None

    rest = text[len(MODEL_PREFIX) :].split(":")[0]
    parts = [
        part
        for part in rest.split("-")
        if part and not _DATE_PART.match(part) and not _VERSION_PART.match(part)
    ]
    if len(parts) < 2:
        return None

    family = parts[0].capitalize()          # opus -> Opus
    version = ".".join(parts[1:])           # 4, 8 -> 4.8
    return f"Claude {family} {version}{SERVICE_SUFFIX}"


# --------------------------------------------------------------- 拉取与解析
def _usage_body(usagetype: str) -> str:
    """USE1-MP:USE1_CacheReadInputTokenCount-Units -> CacheReadInputTokenCount"""
    body = usagetype.removesuffix("-Units")
    if ":" in body:
        body = body.split(":", 1)[1]
    if "_" in body:
        body = body.split("_", 1)[1]
    return body


def parse_offer(payload: dict) -> dict[tuple[str, str], ModelPrice]:
    """把 Price List 的原始 JSON 解析成 {(servicename, 档位): ModelPrice}。"""
    terms = payload.get("terms", {}).get("OnDemand", {})
    collected: dict[tuple[str, str], dict[str, float]] = {}

    for product in payload.get("products", {}).values():
        attrs = product.get("attributes", {})
        service_name = attrs.get("servicename", "")
        if not service_name.startswith("Claude "):
            continue
        hit = USAGE_TO_KIND.get(_usage_body(attrs.get("usagetype", "")).lower())
        if not hit:
            continue  # batch / 1h 缓存 / Reserved TPM 等，这一版不收
        kind, is_global = hit

        offers = terms.get(product.get("sku", ""))
        if not offers:
            continue
        for offer in offers.values():
            for dimension in offer.get("priceDimensions", {}).values():
                raw = dimension.get("pricePerUnit", {}).get("USD")
                if raw is None:
                    continue
                # 单位是 "1M tokens"，换算成每 token
                key = (service_name, GLOBAL if is_global else STANDARD)
                collected.setdefault(key, {})[kind] = float(raw) / 1_000_000

    return {
        (name, tier): ModelPrice(service_name=name, tier=tier, **rates)
        for (name, tier), rates in collected.items()
    }


def _cache_path() -> Path:
    return config.BASE_DIR / CACHE_NAME


def _read_cache() -> tuple[dict[tuple[str, str], ModelPrice], float] | None:
    path = _cache_path()
    if not path.is_file():
        return None
    try:
        blob = json.loads(path.read_text(encoding="utf-8"))
        prices = {
            (row["service_name"], row["tier"]): ModelPrice(**row)
            for row in blob.get("prices", [])
        }
        return prices, float(blob.get("fetched_at", 0))
    except (OSError, ValueError, TypeError):
        return None  # 副本坏了就当没有，下次重新拉


def _write_cache(prices: dict[tuple[str, str], ModelPrice], fetched_at: float) -> None:
    blob = {
        "offer_code": OFFER_CODE,
        "region": PRICE_REGION,
        "fetched_at": fetched_at,
        "prices": [vars(price) for price in prices.values()],
    }
    try:
        _cache_path().write_text(
            json.dumps(blob, ensure_ascii=False, indent=1), encoding="utf-8"
        )
    except OSError:
        pass  # 存不下就每次重拉，不该让页面失败


def fetch_offer() -> dict:
    """拉当前版本的价目表。公开端点，不带任何凭证。"""
    index_url = f"{BASE_URL}/offers/v1.0/aws/{OFFER_CODE}/current/region_index.json"
    with urllib.request.urlopen(index_url, timeout=FETCH_TIMEOUT) as response:
        regions = json.loads(response.read()).get("regions", {})
    entry = regions.get(PRICE_REGION)
    if not entry:
        raise PricingError(f"AWS 价目表里没有 {PRICE_REGION} 这个区域")

    with urllib.request.urlopen(
        BASE_URL + entry["currentVersionUrl"], timeout=FETCH_TIMEOUT * 3
    ) as response:
        return json.loads(response.read())


_lock = threading.Lock()
_table: PriceTable | None = None


def clear_cache() -> None:
    """丢掉内存里的价目表（磁盘副本不动）。"""
    global _table
    with _lock:
        _table = None


def load_prices(force: bool = False) -> PriceTable:
    """取价目表。内存 -> 磁盘 -> AWS，逐层回落。

    拉不到新的但有旧副本时，返回旧的并标 stale，页面上会提示；两样都没有才抛。
    """
    global _table
    with _lock:
        fresh_enough = (
            _table is not None
            and not _table.stale
            and time.time() - _table.fetched_at < CACHE_TTL
        )
        if fresh_enough and not force:
            return _table  # type: ignore[return-value]

    cached = _read_cache()
    if cached and not force and time.time() - cached[1] < CACHE_TTL:
        table = PriceTable(prices=cached[0], fetched_at=cached[1])
        with _lock:
            _table = table
        return table

    try:
        prices = parse_offer(fetch_offer())
        if not prices:
            raise PricingError("价目表里没有解析出任何 Claude 单价")
        now = time.time()
        _write_cache(prices, now)
        table = PriceTable(prices=prices, fetched_at=now)
    except (urllib.error.URLError, OSError, ValueError, PricingError) as exc:
        reason = f"{type(exc).__name__}: {exc}"
        if cached:
            table = PriceTable(
                prices=cached[0], fetched_at=cached[1], stale=True, error=reason
            )
        else:
            raise PricingError(
                f"拉不到 AWS 价目表，本地也没有副本：{reason}"
            ) from exc

    with _lock:
        _table = table
    return table
