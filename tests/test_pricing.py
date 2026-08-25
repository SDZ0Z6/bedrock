"""AWS 价目表的解析与模型名映射。

不联网：用一份照着真实结构裁剪的 Price List 片段做输入。真实端点的验证在
test_integration.py 里，标了 integration，默认跳过。

这里的重点是那些「看起来能跑、其实算错钱」的地方：
    - 两代 usagetype 命名要都认（老模型和新模型混在一张表里）；
    - Global 和标准价必须分开存，混了就是 10% 的系统性偏差；
    - 模型名映射认不出来时必须返回 None，绝不能猜一个名字出来。
"""

from __future__ import annotations

import json

import pytest

from bedrock_cost import pricing


def offer(entries: list[tuple[str, str, str]]) -> dict:
    """造一份 Price List 片段。

    层级照抄真实结构，这一点很容易搞错：terms.OnDemand 是**按 SKU 分层**的，
    每个 SKU 下面才是 "<SKU>.<报价条款码>"，再下面才是 priceDimensions。
    拍平成一层的话解析器一条都找不到。
    """
    products, terms = {}, {}
    for index, (service_name, usagetype, price) in enumerate(entries):
        code = f"SKU{index:013d}"
        products[code] = {
            "sku": code,
            "attributes": {
                "location": "US East (N. Virginia)",
                "regionCode": "us-east-1",
                "servicename": service_name,
                "usagetype": f"USE1-MP:USE1_{usagetype}-Units",
            },
        }
        terms[code] = {
            f"{code}.JRTCKXETXF": {
                "sku": code,
                "priceDimensions": {
                    f"{code}.JRTCKXETXF.6YS6EN2CT7": {
                        "unit": "1M tokens",
                        "pricePerUnit": {"USD": price},
                    }
                },
            }
        }
    return {"products": products, "terms": {"OnDemand": terms}}


OPUS = "Claude Opus 5 (Amazon Bedrock Edition)"
SONNET = "Claude Sonnet 4.5 (Amazon Bedrock Edition)"

# Opus 5 用新一代命名，Sonnet 4.5 用老一代——真实价目表里就是这样混着的
SAMPLE = offer([
    (OPUS, "input_tokens_standard", "5.5"),
    (OPUS, "input_tokens_global_standard", "5"),
    (OPUS, "output_tokens_standard", "27.5"),
    (OPUS, "output_tokens_global_standard", "25"),
    (OPUS, "cache_read_tokens_standard", "0.55"),
    (OPUS, "cache_read_tokens_global_standard", "0.5"),
    (OPUS, "cache_write_tokens_standard", "6.875"),
    (OPUS, "cache_write_tokens_global_standard", "6.25"),
    # 这几条是这一版口径外的，必须被忽略
    (OPUS, "input_tokens_batch", "2.75"),
    (OPUS, "cache_write_tokens_1h_standard", "11"),
    (OPUS, "Reserved_1Month_InputTPM_Global", "0.18"),
    (SONNET, "InputTokenCount", "3.3"),
    (SONNET, "InputTokenCount_Global", "3"),
    (SONNET, "OutputTokenCount", "16.5"),
    (SONNET, "OutputTokenCount_Global", "15"),
    (SONNET, "CacheReadInputTokenCount", "0.33"),
    (SONNET, "CacheReadInputTokenCount_Global", "0.3"),
    (SONNET, "CacheWriteInputTokenCount", "4.125"),
    (SONNET, "CacheWriteInputTokenCount_Global", "3.75"),
])


@pytest.fixture
def table():
    return pricing.parse_offer(SAMPLE)


# ------------------------------------------------------------------ 模型名映射
@pytest.mark.parametrize(
    "model_id, expected",
    [
        ("anthropic.claude-opus-4-8", "Claude Opus 4.8 (Amazon Bedrock Edition)"),
        ("anthropic.claude-opus-5", "Claude Opus 5 (Amazon Bedrock Edition)"),
        ("global.anthropic.claude-opus-5", "Claude Opus 5 (Amazon Bedrock Edition)"),
        ("us.anthropic.claude-sonnet-4-6", "Claude Sonnet 4.6 (Amazon Bedrock Edition)"),
        # 尾部的 -v1 不是版本号的一部分
        ("anthropic.claude-opus-4-6-v1", "Claude Opus 4.6 (Amazon Bedrock Edition)"),
        # 8 位日期段和 :0 后缀都要去掉
        ("anthropic.claude-haiku-4-5-20251001-v1:0", "Claude Haiku 4.5 (Amazon Bedrock Edition)"),
        ("anthropic.claude-sonnet-4-5-20250929-v1:0", "Claude Sonnet 4.5 (Amazon Bedrock Edition)"),
    ],
)
def test_模型名映射到价目表条目(model_id, expected):
    assert pricing.to_service_name(model_id) == expected


@pytest.mark.parametrize(
    "model_id",
    [
        "amazon.nova-pro-v1:0",       # 别的厂商
        "meta.llama3-70b",
        "2kbsta0lwebx",               # 没解析出来的推理配置 ID
        "anthropic.claude",           # 缺版本号
        "",
    ],
)
def test_认不出来的模型返回None而不是瞎猜(model_id):
    """认不出就得说认不出。猜一个名字出来会静默按错价算钱。"""
    assert pricing.to_service_name(model_id) is None


@pytest.mark.parametrize(
    "model_id, tier",
    [
        ("global.anthropic.claude-opus-5", pricing.GLOBAL),
        ("anthropic.claude-opus-5", pricing.STANDARD),
        ("us.anthropic.claude-opus-5", pricing.STANDARD),  # 地理级跨区走本区价
    ],
)
def test_计价档从ModelId前缀判断(model_id, tier):
    assert pricing.tier_of(model_id) == tier


# ------------------------------------------------------------------ 解析
def test_两代usagetype命名都能认(table):
    """新老模型在同一张价目表里用不同的命名，漏认一种就整个模型没价。"""
    new_style = table[(OPUS, pricing.STANDARD)]
    old_style = table[(SONNET, pricing.STANDARD)]
    assert new_style.complete and old_style.complete


def test_单价换算成每token(table):
    """价目表里的单位是 1M tokens，内部一律存每 token，免得到处乘除。"""
    price = table[(OPUS, pricing.STANDARD)]
    assert price.input == pytest.approx(5.5 / 1_000_000)
    assert price.per_million("input") == pytest.approx(5.5)
    assert price.per_million("output") == pytest.approx(27.5)


def test_Global和标准价分开存(table):
    """混了就是 10% 的系统性偏差——实测过，这是最容易错的一处。"""
    standard = table[(OPUS, pricing.STANDARD)]
    cross = table[(OPUS, pricing.GLOBAL)]
    assert standard.per_million("input") == pytest.approx(5.5)
    assert cross.per_million("input") == pytest.approx(5.0)
    assert standard.input > cross.input


def test_口径外的SKU被忽略(table):
    """batch（五折）、1 小时缓存写、Reserved TPM 都不该混进按需单价里。"""
    price = table[(OPUS, pricing.STANDARD)]
    assert price.per_million("input") == pytest.approx(5.5)      # 不是 batch 的 2.75
    assert price.per_million("cache_write") == pytest.approx(6.875)  # 不是 1h 的 11


def test_算钱(table):
    price = table[(OPUS, pricing.GLOBAL)]
    cost = price.cost({
        "input": 1_000_000,
        "output": 1_000_000,
        "cache_read": 1_000_000,
        "cache_write": 1_000_000,
    })
    assert cost == pytest.approx(5 + 25 + 0.5 + 6.25)


def test_四个单价缺一个就算不完整():
    """缺价的模型必须能被识别出来，页面上要显式报出来而不是按 0 算。"""
    partial = pricing.parse_offer(offer([(OPUS, "input_tokens_standard", "5.5")]))
    assert not partial[(OPUS, pricing.STANDARD)].complete


def test_非Claude的条目不进表():
    payload = offer([("Amazon Nova Pro (Amazon Bedrock Edition)", "InputTokenCount", "0.8")])
    assert pricing.parse_offer(payload) == {}


# ------------------------------------------------------------------ 缓存与回落
def test_落盘副本能读回来(tmp_path, monkeypatch):
    monkeypatch.setattr(pricing.config, "BASE_DIR", tmp_path)
    prices = pricing.parse_offer(SAMPLE)
    pricing._write_cache(prices, 1_700_000_000.0)

    restored, stamp = pricing._read_cache()
    assert stamp == 1_700_000_000.0
    assert restored[(OPUS, pricing.GLOBAL)].per_million("input") == pytest.approx(5.0)


def test_拉不到时回落到旧副本并标记(tmp_path, monkeypatch):
    """AWS 端点临时不可达不该让整页崩掉，但必须让用户知道单价可能过时。"""
    monkeypatch.setattr(pricing.config, "BASE_DIR", tmp_path)
    pricing._write_cache(pricing.parse_offer(SAMPLE), 1.0)  # 很旧的副本
    monkeypatch.setattr(
        pricing, "fetch_offer", lambda: (_ for _ in ()).throw(OSError("网络不通"))
    )
    pricing.clear_cache()

    table = pricing.load_prices()
    assert table.stale is True
    assert "网络不通" in (table.error or "")
    assert table.get(OPUS, pricing.GLOBAL) is not None


def test_既拉不到也没有副本时报错(tmp_path, monkeypatch):
    monkeypatch.setattr(pricing.config, "BASE_DIR", tmp_path)
    monkeypatch.setattr(
        pricing, "fetch_offer", lambda: (_ for _ in ()).throw(OSError("网络不通"))
    )
    pricing.clear_cache()

    with pytest.raises(pricing.PricingError):
        pricing.load_prices()


def test_新鲜的副本不会重新联网(tmp_path, monkeypatch):
    monkeypatch.setattr(pricing.config, "BASE_DIR", tmp_path)
    pricing._write_cache(pricing.parse_offer(SAMPLE), __import__("time").time())

    def boom():
        raise AssertionError("副本还新鲜，不该联网")

    monkeypatch.setattr(pricing, "fetch_offer", boom)
    pricing.clear_cache()
    assert pricing.load_prices().model_count == 2


def test_坏掉的副本当作没有(tmp_path, monkeypatch):
    monkeypatch.setattr(pricing.config, "BASE_DIR", tmp_path)
    (tmp_path / pricing.CACHE_NAME).write_text("{ 这不是 json", encoding="utf-8")
    assert pricing._read_cache() is None


def test_usagetype主体提取():
    assert pricing._usage_body("USE1-MP:USE1_CacheReadInputTokenCount-Units") == "CacheReadInputTokenCount"
    assert pricing._usage_body("USE1-MP:USE1_cache_read_tokens_global_standard-Units") == "cache_read_tokens_global_standard"
    assert pricing._usage_body("USW2-MP:USW2_InputTokenCount_Global-Units") == "InputTokenCount_Global"


def test_解析真实结构的片段不会因为多余字段炸掉():
    """真实 SKU 上还有一堆我们不看的属性，多出来的字段不该影响解析。"""
    payload = json.loads(json.dumps(SAMPLE))
    for product in payload["products"].values():
        product["attributes"].update(
            {"operation": "", "locationType": "AWS Region", "servicecode": "AWSMarketplace"}
        )
    assert pricing.parse_offer(payload)[(OPUS, pricing.GLOBAL)].complete
