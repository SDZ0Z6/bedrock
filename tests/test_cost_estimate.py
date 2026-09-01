"""预估成本：token 量 × 单价。

不联网：CloudWatch 和价目表都换成 fake。

用例的重点是那些「数出来了但是错的」的地方：
    - 四个计数器互相独立，输入那一项不能减缓存（实测过：缓存读能比输入大 24 倍）；
    - 直连的 global.anthropic.claude-opus-5 和走推理配置的 anthropic.claude-opus-5
      是同一个模型，必须并成一行，否则表格里会出现两行同名的；
    - 计价档选错就是 10% 的系统性偏差；
    - 没在价目表里的模型不能按 0 悄悄算进总额。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from bedrock_cost import cost_estimate, pricing
from bedrock_cost.cloudwatch_metrics import ProfileInfo

from .conftest import LEDGER_ROWS

OPUS5 = "Claude Opus 5 (Amazon Bedrock Edition)"
SONNET46 = "Claude Sonnet 4.6 (Amazon Bedrock Edition)"

# $/1M tokens，照抄真实价目表的量级
RATES = {
    (OPUS5, pricing.GLOBAL): (5.0, 25.0, 0.5, 6.25),
    (OPUS5, pricing.STANDARD): (5.5, 27.5, 0.55, 6.875),
    (SONNET46, pricing.GLOBAL): (3.0, 15.0, 0.3, 3.75),
    (SONNET46, pricing.STANDARD): (3.3, 16.5, 0.33, 4.125),
}


@pytest.fixture
def fake_prices(monkeypatch):
    prices = {
        key: pricing.ModelPrice(
            service_name=key[0],
            tier=key[1],
            input=rates[0] / 1e6,
            output=rates[1] / 1e6,
            cache_read=rates[2] / 1e6,
            cache_write=rates[3] / 1e6,
        )
        for key, rates in RATES.items()
    }
    table = pricing.PriceTable(prices=prices, fetched_at=1.0)
    monkeypatch.setattr(cost_estimate, "load_prices", lambda force=False: table)
    return table


def profile(name: str, model: str, *, cross_region: bool) -> ProfileInfo:
    """造一个应用推理配置。跨区配置会多带一条不带区域的 modelArn。"""
    arns = [f"arn:aws:bedrock:us-east-1::foundation-model/{model}"]
    if cross_region:
        arns.insert(0, f"arn:aws:bedrock:::foundation-model/{model}")
    return ProfileInfo(name=name, model=model, tag_value="", model_arns=tuple(arns))


@pytest.fixture
def fake_cloudwatch(monkeypatch):
    """按 {区域: {ModelId: {指标: [每天的值]}}} 喂数据。

    返回一个可以被用例改写的 dict，改完直接生效。
    """
    state: dict = {"regions": {}, "profiles": {}, "days": 1}

    def fake_list(account, region):
        return sorted(state["regions"].get(region, {}))

    def fake_resolve(account, region):
        return state["profiles"].get(region, {}), True

    class FakeClient:
        def __init__(self, region):
            self.region = region

        def get_metric_data(self, **kwargs):
            results = []
            for query in kwargs["MetricDataQueries"]:
                stat = query["MetricStat"]
                model_id = stat["Metric"]["Dimensions"][0]["Value"]
                metric = stat["Metric"]["MetricName"]
                values = state["regions"].get(self.region, {}).get(model_id, {}).get(metric, [])
                start = kwargs["StartTime"]
                stamps = [start + timedelta(days=i) for i in range(len(values))]
                results.append(
                    {"Id": query["Id"], "Timestamps": stamps, "Values": list(values)}
                )
            return {"MetricDataResults": results}

    monkeypatch.setattr(cost_estimate, "list_model_ids", fake_list)
    monkeypatch.setattr(cost_estimate, "resolve_profiles", fake_resolve)
    monkeypatch.setattr(cost_estimate, "_client", lambda a, s, region: FakeClient(region))
    cost_estimate.clear_cache()
    yield state
    cost_estimate.clear_cache()


def tokens(inp=0.0, out=0.0, read=0.0, write=0.0, days=1) -> dict:
    """一个模型的四个指标，每天都是同样的量。"""
    return {
        "InputTokenCount": [inp] * days,
        "OutputTokenCount": [out] * days,
        "CacheReadInputTokenCount": [read] * days,
        "CacheWriteInputTokenCount": [write] * days,
    }


def build(accounts, cw, start=date(2026, 8, 14), end=date(2026, 8, 14), **kwargs):
    return cost_estimate.build_estimate(accounts, start, end, **kwargs)


# ------------------------------------------------------------------ 公式
def test_四种token各按各的单价算(accounts, fake_prices, fake_cloudwatch):
    fake_cloudwatch["regions"] = {
        "us-east-1": {
            "global.anthropic.claude-opus-5": tokens(
                inp=1_000_000, out=1_000_000, read=1_000_000, write=1_000_000
            )
        }
    }
    report = build(accounts[:1], fake_cloudwatch)
    assert report.total_cost == pytest.approx(5.0 + 25.0 + 0.5 + 6.25)


def test_输入不减缓存(accounts, fake_prices, fake_cloudwatch):
    """四个计数器互相独立。实测某天输入 32 亿、缓存读 765 亿——缓存读比输入
    大 24 倍，InputTokenCount 不可能包含它。减了就会把输入算成负数。
    """
    fake_cloudwatch["regions"] = {
        "us-east-1": {
            "global.anthropic.claude-opus-5": tokens(
                inp=1_000_000, read=24_000_000, write=2_000_000
            )
        }
    }
    report = build(accounts[:1], fake_cloudwatch)
    expected = 1 * 5.0 + 24 * 0.5 + 2 * 6.25
    assert report.total_cost == pytest.approx(expected)
    assert report.rows[0].count("input") == 1_000_000  # 原样保留，没被减过


def test_四个区相加(accounts, fake_prices, fake_cloudwatch):
    """四个美区单价一致（已核对），所以区域只是取数维度，直接相加。"""
    fake_cloudwatch["regions"] = {
        region: {"global.anthropic.claude-opus-5": tokens(inp=1_000_000)}
        for region in ("us-east-1", "us-east-2", "us-west-1", "us-west-2")
    }
    report = build(accounts[:1], fake_cloudwatch)
    assert report.rows[0].count("input") == 4_000_000
    assert report.total_cost == pytest.approx(4 * 5.0)


# ------------------------------------------------------------------ 计价档
def test_跨区和本区用不同单价(accounts, fake_prices, fake_cloudwatch):
    fake_cloudwatch["regions"] = {
        "us-east-1": {
            "global.anthropic.claude-opus-5": tokens(inp=1_000_000),
            "anthropic.claude-sonnet-4-6": tokens(inp=1_000_000),
        }
    }
    report = build(accounts[:1], fake_cloudwatch)
    by_label = {(r.label, r.tier): r for r in report.rows}
    assert by_label[("claude-opus-5", pricing.GLOBAL)].cost == pytest.approx(5.0)
    assert by_label[("claude-sonnet-4-6", pricing.STANDARD)].cost == pytest.approx(3.3)


def test_推理配置按无区域ARN判跨区(accounts, fake_prices, fake_cloudwatch):
    """判据是 modelArn 里有没有不带区域的那一条，不是猜配置名。"""
    fake_cloudwatch["regions"] = {
        "us-east-1": {"2kbsta0lwebx": tokens(inp=1_000_000)}
    }
    fake_cloudwatch["profiles"] = {
        "us-east-1": {
            "2kbsta0lwebx": profile("随便什么名字", "anthropic.claude-opus-5", cross_region=True)
        }
    }
    report = build(accounts[:1], fake_cloudwatch)
    assert report.rows[0].tier == pricing.GLOBAL
    assert report.rows[0].cost == pytest.approx(5.0)


def test_本区推理配置走本区价(accounts, fake_prices, fake_cloudwatch):
    fake_cloudwatch["regions"] = {"us-east-1": {"2kbsta0lwebx": tokens(inp=1_000_000)}}
    fake_cloudwatch["profiles"] = {
        "us-east-1": {
            # 名字里带 global 但 ARN 都带区域——以 ARN 为准
            "2kbsta0lwebx": profile("map-global-x-use1", "anthropic.claude-opus-5", cross_region=False)
        }
    }
    report = build(accounts[:1], fake_cloudwatch)
    assert report.rows[0].tier == pricing.STANDARD
    assert report.rows[0].cost == pytest.approx(5.5)


def test_PRICE_TIER能强制覆盖(accounts, fake_prices, fake_cloudwatch, monkeypatch):
    """自动判断万一判错，要有不改代码就能纠正的口子。"""
    monkeypatch.setattr(cost_estimate.config, "PRICE_TIER", pricing.STANDARD)
    fake_cloudwatch["regions"] = {
        "us-east-1": {"global.anthropic.claude-opus-5": tokens(inp=1_000_000)}
    }
    report = build(accounts[:1], fake_cloudwatch)
    assert report.rows[0].tier == pricing.STANDARD
    assert report.rows[0].cost == pytest.approx(5.5)


# ------------------------------------------------------------------ 归并
def test_直连和推理配置的同一个模型并成一行(accounts, fake_prices, fake_cloudwatch):
    """直连的 ModelId 带 global. 前缀，走配置的归并后不带——不剥前缀的话
    表格里会出现两行都叫 claude-opus-5 的，看着像重复数据。
    """
    fake_cloudwatch["regions"] = {
        "us-east-1": {
            "global.anthropic.claude-opus-5": tokens(inp=1_000_000),
            "2kbsta0lwebx": tokens(inp=3_000_000),
        }
    }
    fake_cloudwatch["profiles"] = {
        "us-east-1": {
            "2kbsta0lwebx": profile("p", "anthropic.claude-opus-5", cross_region=True)
        }
    }
    report = build(accounts[:1], fake_cloudwatch)
    assert [r.label for r in report.rows] == ["claude-opus-5"]
    assert report.rows[0].count("input") == 4_000_000


def test_同一模型两个计价档分开列(accounts, fake_prices, fake_cloudwatch):
    """单价不同就不能并行，否则那一行的单价列没法填。"""
    fake_cloudwatch["regions"] = {
        "us-east-1": {
            "global.anthropic.claude-opus-5": tokens(inp=1_000_000),
            "anthropic.claude-opus-5": tokens(inp=1_000_000),
        }
    }
    report = build(accounts[:1], fake_cloudwatch)
    assert len(report.rows) == 2
    assert {r.tier for r in report.rows} == {pricing.GLOBAL, pricing.STANDARD}
    assert report.total_cost == pytest.approx(5.0 + 5.5)


def test_多个账号合并统计(accounts, fake_prices, fake_cloudwatch):
    fake_cloudwatch["regions"] = {
        "us-east-1": {"global.anthropic.claude-opus-5": tokens(inp=1_000_000)}
    }
    one = build(accounts[:1], fake_cloudwatch).total_cost
    both = build(accounts, fake_cloudwatch, refresh=True).total_cost
    assert both == pytest.approx(one * len(accounts))


# ------------------------------------------------------------------ 没有单价的模型
def test_价目表里没有的模型不计入总额(accounts, fake_prices, fake_cloudwatch):
    """静默按 0 算是最糟的结果——总额看起来正常，其实少了一大块。"""
    fake_cloudwatch["regions"] = {
        "us-east-1": {
            "global.anthropic.claude-opus-5": tokens(inp=1_000_000),
            "amazon.nova-pro-v1:0": tokens(inp=999_000_000),
        }
    }
    report = build(accounts[:1], fake_cloudwatch)
    assert report.total_cost == pytest.approx(5.0)
    assert "amazon.nova-pro-v1:0" in report.unpriced
    unpriced_row = [r for r in report.rows if not r.priced]
    assert len(unpriced_row) == 1
    assert unpriced_row[0].cost == 0.0


def test_没有单价的模型也不进图表(accounts, fake_prices, fake_cloudwatch):
    fake_cloudwatch["regions"] = {
        "us-east-1": {
            "global.anthropic.claude-opus-5": tokens(inp=1_000_000),
            "amazon.nova-pro-v1:0": tokens(inp=999_000_000),
        }
    }
    report = build(accounts[:1], fake_cloudwatch)
    assert [s.name for s in report.series] == ["claude-opus-5"]


def test_价目表拉不到时整页报错而不是出零(accounts, fake_cloudwatch, monkeypatch):
    def boom(force=False):
        raise pricing.PricingError("端点不可达，本地也没副本")

    monkeypatch.setattr(cost_estimate, "load_prices", boom)
    report = build(accounts[:1], fake_cloudwatch)
    assert report.errors and "端点不可达" in report.errors[0]
    assert report.rows == []


def test_旧副本会被标记出来(accounts, fake_cloudwatch, monkeypatch):
    table = pricing.PriceTable(prices={}, fetched_at=1.0, stale=True, error="网络不通")
    monkeypatch.setattr(cost_estimate, "load_prices", lambda force=False: table)
    report = build(accounts[:1], fake_cloudwatch)
    assert report.price_stale is True
    assert report.price_error == "网络不通"


# ------------------------------------------------------------------ 时间序列
def test_每天一个桶(accounts, fake_prices, fake_cloudwatch):
    start, end = date(2026, 8, 10), date(2026, 8, 16)
    fake_cloudwatch["regions"] = {
        "us-east-1": {"global.anthropic.claude-opus-5": tokens(inp=1_000_000, days=7)}
    }
    report = build(accounts[:1], fake_cloudwatch, start=start, end=end)
    assert len(report.dates) == 7
    assert report.dates[0] == "2026-08-10" and report.dates[-1] == "2026-08-16"
    assert len(report.labels) == 7 and report.labels[0] == "08-10"


def test_每天的钱加起来等于总额(accounts, fake_prices, fake_cloudwatch):
    """图和汇总数字对不上是最容易被发现、也最伤信任的一类 bug。"""
    fake_cloudwatch["regions"] = {
        "us-east-1": {
            "global.anthropic.claude-opus-5": tokens(inp=1_000_000, out=500_000, days=5),
            "anthropic.claude-sonnet-4-6": tokens(read=9_000_000, write=1_000_000, days=5),
        }
    }
    report = build(
        accounts[:1], fake_cloudwatch, start=date(2026, 8, 10), end=date(2026, 8, 14)
    )
    assert sum(report.column_totals) == pytest.approx(report.total_cost)
    assert report.daily_average == pytest.approx(report.total_cost / 5)


def test_峰值指向金额最高的那天(accounts, fake_prices, fake_cloudwatch):
    fake_cloudwatch["regions"] = {
        "us-east-1": {
            "global.anthropic.claude-opus-5": {
                "InputTokenCount": [1_000_000, 9_000_000, 2_000_000],
                "OutputTokenCount": [0, 0, 0],
                "CacheReadInputTokenCount": [0, 0, 0],
                "CacheWriteInputTokenCount": [0, 0, 0],
            }
        }
    }
    report = build(
        accounts[:1], fake_cloudwatch, start=date(2026, 8, 10), end=date(2026, 8, 12)
    )
    index, value = report.peak
    assert index == 1
    assert value == pytest.approx(9 * 5.0)


def test_按类型统计的钱加起来等于总额(accounts, fake_prices, fake_cloudwatch):
    fake_cloudwatch["regions"] = {
        "us-east-1": {
            "global.anthropic.claude-opus-5": tokens(
                inp=1_000_000, out=2_000_000, read=3_000_000, write=4_000_000
            )
        }
    }
    report = build(accounts[:1], fake_cloudwatch)
    by_kind = sum(report.kind_cost(kind) for kind in pricing.KINDS)
    assert by_kind == pytest.approx(report.total_cost)
    assert report.kind_total("cache_read") == 3_000_000


def test_没有用量时不炸(accounts, fake_prices, fake_cloudwatch):
    report = build(accounts[:1], fake_cloudwatch)
    assert report.rows == [] and report.series == []
    assert report.total_cost == 0.0
    assert report.peak == (-1, 0.0)
    assert report.daily_average == 0.0
    assert not report.has_data


def test_取数出错时报出账号和区域(accounts, fake_prices, fake_cloudwatch, monkeypatch):
    def boom(account, region):
        raise RuntimeError("拒绝访问")

    monkeypatch.setattr(cost_estimate, "list_model_ids", boom)
    report = build(accounts[:1], fake_cloudwatch)
    assert report.errors
    assert accounts[0].account in report.errors[0]


# ------------------------------------------------------------------ 页面
def test_预估成本页能打开(logged_in, ledger, fake_prices, fake_cloudwatch):
    fake_cloudwatch["regions"] = {
        "us-east-1": {
            "global.anthropic.claude-opus-5": tokens(
                inp=1_000_000, out=1_000_000, read=1_000_000, write=1_000_000
            )
        }
    }
    response = logged_in.get("/cost-estimate?start=2026-08-14&end=2026-08-14")
    html = response.get_data(as_text=True)
    assert response.status_code == 200
    assert "claude-opus-5" in html
    # 四种 token 和四个单价列都在
    for label in ("输入 Token", "输出 Token", "缓存读 Token", "缓存写 Token"):
        assert label in html
    for label in ("输入价/M", "输出价/M", "缓存读价/M", "缓存写价/M"):
        assert label in html
    # 台账里两个账号，页面默认「全部账号」，所以是两份
    assert "$73.50" in html  # (5 + 25 + 0.5 + 6.25) × 2


def test_未登录进不去预估成本页(client, ledger):
    response = client.get("/cost-estimate")
    assert response.status_code == 302
    assert "/login" in response.headers["Location"]


def test_页面不泄露凭证(logged_in, ledger, fake_prices, fake_cloudwatch):
    fake_cloudwatch["regions"] = {
        "us-east-1": {"global.anthropic.claude-opus-5": tokens(inp=1_000_000)}
    }
    html = logged_in.get("/cost-estimate").get_data(as_text=True)
    for _, _, _, _, _, ak, sk, _ in LEDGER_ROWS:
        assert ak not in html
        assert sk not in html


def test_没有单价的模型会在页面上提示(logged_in, ledger, fake_prices, fake_cloudwatch):
    fake_cloudwatch["regions"] = {
        "us-east-1": {"amazon.nova-pro-v1:0": tokens(inp=1_000_000)}
    }
    html = logged_in.get("/cost-estimate").get_data(as_text=True)
    assert "amazon.nova-pro-v1:0" in html
    assert "没有对应条目" in html


def test_页面标注了这是牌价(logged_in, ledger, fake_prices, fake_cloudwatch):
    """不写清楚的话，有 EDP 折扣的人会以为这个数就是账单。"""
    html = logged_in.get("/cost-estimate").get_data(as_text=True)
    assert "牌价" in html
    assert "折扣" in html


def test_日期区间快捷项(logged_in, ledger, fake_prices, fake_cloudwatch):
    response = logged_in.get("/cost-estimate?preset=last7")
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert "chip-on" in html


def test_按账号筛选(logged_in, ledger, fake_prices, fake_cloudwatch, accounts):
    fake_cloudwatch["regions"] = {
        "us-east-1": {"global.anthropic.claude-opus-5": tokens(inp=1_000_000)}
    }
    response = logged_in.get(f"/cost-estimate?account={accounts[0].key}")
    assert response.status_code == 200
    assert accounts[0].partner in response.get_data(as_text=True)


# ------------------------------------------------------------------ 页面上的交互件
# 这三样都是「漏了也不报错、只是页面变哑巴」的东西，很容易改着改着就丢了
def test_筛选条改动即提交(logged_in, ledger, fake_prices, fake_cloudwatch):
    """账号和日期改了要自动重查。这一页的表单没有查询按钮，脚本丢了就等于失灵。"""
    html = logged_in.get("/cost-estimate").get_data(as_text=True)
    assert "estimate-filters" in html
    # 脚本在 shell.html 里按类统一绑，所以表单必须带上 filters-auto——
    # 光有 id 不起作用
    assert "filters-auto" in html
    assert "querySelectorAll('form.filters-auto')" in html
    assert "form.submit()" in html


def test_图表带悬浮提示(logged_in, ledger, fake_prices, fake_cloudwatch):
    """柱子上的 hover 提示靠三样东西：容器、数据、脚本，缺一个就没反应。"""
    fake_cloudwatch["regions"] = {
        "us-east-1": {"global.anthropic.claude-opus-5": tokens(inp=1_000_000)}
    }
    html = logged_in.get(
        "/cost-estimate?start=2026-08-14&end=2026-08-14"
    ).get_data(as_text=True)
    assert 'id="chart-tip"' in html
    assert 'id="chart-data"' in html
    assert "chart-hit" in html          # SVG 里的命中区
    assert "mouseenter" in html


def test_悬浮数据里有每个模型的金额(logged_in, ledger, fake_prices, fake_cloudwatch):
    import json
    import re

    fake_cloudwatch["regions"] = {
        "us-east-1": {"global.anthropic.claude-opus-5": tokens(inp=1_000_000)}
    }
    html = logged_in.get(
        "/cost-estimate?start=2026-08-14&end=2026-08-14"
    ).get_data(as_text=True)
    payload = re.search(
        r'<script id="chart-data" type="application/json">(.*?)</script>', html, re.S
    )
    assert payload
    buckets = json.loads(payload.group(1))
    assert len(buckets) == 1
    assert buckets[0]["rows"][0]["name"] == "claude-opus-5"
    assert buckets[0]["total"] > 0


def test_合计行不显示单价(logged_in, ledger, fake_prices, fake_cloudwatch):
    """单价是逐模型的，加总没有意义，那四格留空。"""
    import re

    fake_cloudwatch["regions"] = {
        "us-east-1": {"global.anthropic.claude-opus-5": tokens(inp=1_000_000)}
    }
    html = logged_in.get(
        "/cost-estimate?start=2026-08-14&end=2026-08-14"
    ).get_data(as_text=True)
    foot = re.search(r"<tfoot>(.*?)</tfoot>", html, re.S).group(1)
    assert "合计" in foot
    assert "$5.00" not in foot        # 单价不该出现在合计行
    assert 'colspan="4"' in foot      # 四格并成一个空格子
    assert "$5.00" in html            # 但明细行里还在


def test_UTC分桶(accounts, fake_prices, fake_cloudwatch):
    """CloudWatch 的 Period=86400 是按 UTC 零点切的，桶边界必须跟它一致。"""
    stamps, dates, labels = cost_estimate.build_days(date(2026, 8, 10), date(2026, 8, 12))
    assert stamps[0] == datetime(2026, 8, 10, tzinfo=timezone.utc)
    assert all(s.tzinfo == timezone.utc and s.hour == 0 for s in stamps)
    assert dates == ["2026-08-10", "2026-08-11", "2026-08-12"]
    assert labels == ["08-10", "08-11", "08-12"]
