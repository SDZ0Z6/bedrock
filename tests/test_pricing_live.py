"""打 AWS 真实价目表端点的用例。

默认跳过（pyproject 的 addopts 排掉了 integration 标记）。要跑：

    pytest -m integration tests/test_pricing_live.py

**不需要任何 AWS 凭证，也不花钱**——AWS 的 Price List 批量端点是公开的。这一点和
其他 integration 用例不同，那些要真实 AK/SK 且按请求计费。

存在的意义是当哨兵：价目表的结构或命名是 AWS 说了算的，哪天他们改了 service code、
改了 usagetype 写法、或者新模型换了一套命名，这几条会先炸，而不是等到页面上悄悄
少算一个模型的钱。
"""

from __future__ import annotations

import pytest

from bedrock_cost import pricing

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def live():
    try:
        return pricing.parse_offer(pricing.fetch_offer())
    except Exception as exc:  # 网络不通就跳过，不算失败
        pytest.skip(f"拉不到 AWS 价目表：{type(exc).__name__}: {exc}")


def test_能从公开端点拉到Claude单价(live):
    assert len({name for name, _ in live}) >= 10, "Claude 模型数量少得反常"


def test_现役模型都有完整的四个单价(live):
    """这几个是台账账号真实在用的。缺任何一个单价，那个模型就会被判成「无单价」，
    整块花费从总额里消失——宁可在这里炸，也别在页面上悄悄少算。
    """
    current = [
        "Claude Opus 5",
        "Claude Opus 4.8",
        "Claude Sonnet 5",
        "Claude Sonnet 4.6",
        "Claude Haiku 4.5",
    ]
    missing = []
    for model in current:
        name = model + pricing.SERVICE_SUFFIX
        for tier in (pricing.GLOBAL, pricing.STANDARD):
            price = live.get((name, tier))
            if price is None or not price.complete:
                missing.append(f"{model}({tier})")
    assert not missing, f"这些模型的单价不全：{missing}"


def test_跨区不会比本区贵(live):
    """两档价一旦解析串了，估算会稳定偏 10%。

    注意不能断言「跨区严格更便宜」：实测 Claude Sonnet 4 的两档价是一样的
    （都是 $3.00/1M 输入），AWS 并非对所有模型都给跨区折扣。
    """
    cheaper = 0
    for name, _ in {key for key in live}:
        standard = live.get((name, pricing.STANDARD))
        cross = live.get((name, pricing.GLOBAL))
        if not (standard and cross and standard.complete and cross.complete):
            continue
        for kind in pricing.KINDS:
            assert cross.rate(kind) <= standard.rate(kind), f"{name} 的 {kind} 跨区反而更贵"
        if any(cross.rate(k) < standard.rate(k) for k in pricing.KINDS):
            cheaper += 1
    # 至少有几个模型两档价确实不同——全都相等的话说明解析把两档并成一个了
    assert cheaper >= 5, "没有任何模型的两档价存在差异，解析多半串了"


def test_单价在合理量级(live):
    """防的是单位换算错位——少除一个 1e6 的话数字会离谱到没法看。"""
    price = live[(("Claude Opus 5" + pricing.SERVICE_SUFFIX), pricing.GLOBAL)]
    assert 0.1 < price.per_million("input") < 100
    assert 1 < price.per_million("output") < 500
    # 缓存读远比输入便宜，缓存写比输入贵——这个关系错了说明四种口径接串了
    assert price.cache_read < price.input < price.cache_write


def test_模型名映射在真实价目表上能对上(live):
    """CloudWatch 给的 ModelId 必须能映射到价目表里真实存在的条目。"""
    seen = {name for name, _ in live}
    for model_id in (
        "global.anthropic.claude-opus-5",
        "anthropic.claude-opus-4-8",
        "anthropic.claude-sonnet-4-6",
        "anthropic.claude-haiku-4-5-20251001-v1:0",
    ):
        name = pricing.to_service_name(model_id)
        assert name in seen, f"{model_id} 映射出的 {name} 不在价目表里"


def test_四个美区同价():
    """代码只拉 us-east-1 一份，前提是四个美区单价一致。这条守住这个前提。"""
    import json
    import urllib.request

    base = pricing.BASE_URL
    try:
        with urllib.request.urlopen(
            f"{base}/offers/v1.0/aws/{pricing.OFFER_CODE}/current/region_index.json",
            timeout=pricing.FETCH_TIMEOUT,
        ) as response:
            regions = json.loads(response.read())["regions"]
    except Exception as exc:
        pytest.skip(f"拉不到区域索引：{exc}")

    name = "Claude Opus 5" + pricing.SERVICE_SUFFIX
    reference = None
    for region in ("us-east-1", "us-east-2", "us-west-1", "us-west-2"):
        if region not in regions:
            continue
        with urllib.request.urlopen(
            base + regions[region]["currentVersionUrl"], timeout=pricing.FETCH_TIMEOUT * 3
        ) as response:
            table = pricing.parse_offer(json.loads(response.read()))
        price = table.get((name, pricing.GLOBAL))
        if price is None:
            continue
        rates = tuple(price.rate(kind) for kind in pricing.KINDS)
        if reference is None:
            reference = (region, rates)
        else:
            assert rates == reference[1], f"{region} 和 {reference[0]} 单价不一致"
    assert reference is not None, "一个美区的价目表都没拉到"
