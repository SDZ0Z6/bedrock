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

from .conftest import LEDGER_ROWS, ledger_value

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
    for row in LEDGER_ROWS:
        assert ledger_value(row, "AK") not in html
        assert ledger_value(row, "SK") not in html


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


# ------------------------------------------------------------------ 告警用：按标签拆开
def _tagged(profile_info: ProfileInfo, tag: str) -> ProfileInfo:
    from dataclasses import replace

    return replace(profile_info, tag_value=tag)


def test_拆分估算按标签分开(accounts, fake_prices, fake_cloudwatch):
    """额度告警要套比率，所以得知道每一块钱是 TAG 还是 UNTAG。

    走带台账标签的推理配置 = TAG；直连原厂模型 = UNTAG（它确实没打标签）。
    """
    alpha = accounts[0]   # TAG 列是 map-migrated=migALPHA
    fake_cloudwatch["regions"] = {
        "us-east-1": {
            "global.anthropic.claude-opus-5": tokens(inp=1_000_000),   # 直连：$5
            "2kbsta0lwebx": tokens(inp=2_000_000),                     # 配置：$10
        }
    }
    fake_cloudwatch["profiles"] = {
        "us-east-1": {
            "2kbsta0lwebx": _tagged(
                profile("map-opus5", "anthropic.claude-opus-5", cross_region=True), "migALPHA"
            )
        }
    }
    split = cost_estimate.estimate_split(alpha, date(2026, 8, 14), date(2026, 8, 14))
    assert split.errors == []
    assert split.tag_raw == pytest.approx(10.0)
    assert split.untag_raw == pytest.approx(5.0)
    # ALPHA 的 UNTAG 比率是 1.05
    assert split.marked(alpha) == pytest.approx(10.0 + 5.0 * 1.05)


def test_拆分估算标签值对不上算无标签(accounts, fake_prices, fake_cloudwatch):
    """配置上打的是别人的 MAP ID，和台账对不上——和概览页一样算 UNTAG。"""
    fake_cloudwatch["regions"] = {"us-east-1": {"2kbsta0lwebx": tokens(inp=1_000_000)}}
    fake_cloudwatch["profiles"] = {
        "us-east-1": {
            "2kbsta0lwebx": _tagged(
                profile("x", "anthropic.claude-opus-5", cross_region=True), "migSOMEONEELSE"
            )
        }
    }
    split = cost_estimate.estimate_split(accounts[0], date(2026, 8, 14), date(2026, 8, 14))
    assert split.tag_raw == 0
    assert split.untag_raw == pytest.approx(5.0)


def test_拆分估算和明细表同一套定价(accounts, fake_prices, fake_cloudwatch):
    """合计必须和预估成本页的总额一致——两处用的是同一个价目表和计价档判定。"""
    fake_cloudwatch["regions"] = {
        "us-east-1": {
            "global.anthropic.claude-opus-5": tokens(inp=1_000_000, out=200_000, read=3_000_000),
            "us.anthropic.claude-sonnet-4-6": tokens(inp=500_000, write=100_000),
        }
    }
    day = date(2026, 8, 14)
    split = cost_estimate.estimate_split(accounts[0], day, day)
    report = cost_estimate.build_estimate(accounts[:1], day, day)
    assert split.tag_raw + split.untag_raw == pytest.approx(report.total_cost)


def test_拆分估算不认识的模型单独列出(accounts, fake_prices, fake_cloudwatch):
    """没单价的模型不能悄悄按 0 算——列出来，告警里会写明「按 0 算了」。"""
    fake_cloudwatch["regions"] = {"us-east-1": {"global.anthropic.claude-future-9": tokens(inp=1e6)}}
    split = cost_estimate.estimate_split(accounts[0], date(2026, 8, 14), date(2026, 8, 14))
    assert split.unpriced == ["anthropic.claude-future-9"]
    assert split.tag_raw + split.untag_raw == 0


def test_拆分估算区间为空时不查(accounts, fake_prices, fake_cloudwatch):
    split = cost_estimate.estimate_split(accounts[0], date(2026, 8, 15), date(2026, 8, 14))
    assert (split.tag_raw, split.untag_raw, split.errors) == (0.0, 0.0, [])


def test_拆分估算读不到价目表是错误(accounts, fake_cloudwatch, monkeypatch):
    def broken(force=False):
        raise pricing.PricingError("价目表下不来")

    monkeypatch.setattr(cost_estimate, "load_prices", broken)
    split = cost_estimate.estimate_split(accounts[0], date(2026, 8, 14), date(2026, 8, 14))
    assert split.errors == ["价目表下不来"]


# ================================================================== 告警用：一个小时里的用量
class TestHourUsage:
    """「用量中断」卡片用的那一小时：按分钟取，定位最后一次调用，token × 牌价估花费。"""

    HOUR = datetime(2026, 9, 27, 5, tzinfo=timezone.utc)

    @pytest.fixture
    def minutes(self, monkeypatch, fake_prices):
        """按 {区域: {ModelId: {指标: {第几分钟: 值}}}} 喂数据，并记下每次查询的参数。"""
        state = {"regions": {}, "profiles": {}, "fail": set(), "queries": []}

        class FakeClient:
            def __init__(self, region):
                self.region = region

            def get_metric_data(self, **kwargs):
                if self.region in state["fail"]:
                    raise RuntimeError("ThrottlingException")
                state["queries"].append(kwargs)
                results = []
                for query in kwargs["MetricDataQueries"]:
                    stat = query["MetricStat"]
                    model_id = stat["Metric"]["Dimensions"][0]["Value"]
                    points = state["regions"][self.region].get(model_id, {}).get(stat["Metric"]["MetricName"], {})
                    stamps = [kwargs["StartTime"] + timedelta(minutes=m) for m in sorted(points)]
                    results.append({"Id": query["Id"], "Timestamps": stamps,
                                    "Values": [points[m] for m in sorted(points)]})
                return {"MetricDataResults": results}

        monkeypatch.setattr(cost_estimate, "list_model_ids", lambda a, region: sorted(state["regions"].get(region, {})))
        monkeypatch.setattr(cost_estimate, "resolve_profiles", lambda a, region: (state["profiles"].get(region, {}), True))
        monkeypatch.setattr(cost_estimate, "_client", lambda a, s, region: FakeClient(region))
        return state

    @staticmethod
    def account(**kw):
        from bedrock_cost.excel_source import Account

        base = dict(partner="P", account="111111111111", budget=0, tag_ratio=1.0, untag_ratio=1.0)
        base.update(kw)
        return Account(**base)

    def test_sums_the_hour_and_finds_the_last_minute(self, minutes):
        minutes["regions"] = {
            "us-east-1": {"global.anthropic.claude-opus-5": {
                "Invocations": {3: 10, 47: 2},
                "InputTokenCount": {3: 1_000_000, 47: 200_000},
                "OutputTokenCount": {3: 40_000},
            }},
            "us-west-2": {"global.anthropic.claude-opus-5": {"Invocations": {12: 5}, "InputTokenCount": {12: 300_000}}},
        }
        usage = cost_estimate.hour_usage(self.account(), self.HOUR)
        assert usage.invocations == 17
        assert usage.tokens == {"input": 1_500_000, "output": 40_000}
        assert usage.last_call == self.HOUR + timedelta(minutes=47)
        assert usage.errors == [] and usage.unpriced == []
        # 全球跨区的 Opus 5：输入 $5/M、输出 $25/M；直连模型算无标签
        assert usage.untag_raw == pytest.approx(1.5 * 5.0 + 0.04 * 25.0)
        assert usage.tag_raw == 0

    def test_asks_for_minutes_in_that_hour_only(self, minutes):
        minutes["regions"] = {"us-east-1": {"global.anthropic.claude-opus-5": {"Invocations": {0: 1}}}}
        cost_estimate.hour_usage(self.account(), self.HOUR + timedelta(minutes=30))   # 从整点算起
        (query,) = minutes["queries"]
        assert (query["StartTime"], query["EndTime"]) == (self.HOUR, self.HOUR + timedelta(hours=1))
        assert {q["MetricStat"]["Period"] for q in query["MetricDataQueries"]} == {60}
        assert {q["MetricStat"]["Metric"]["MetricName"] for q in query["MetricDataQueries"]} == {
            "Invocations", *cost_estimate.TOKEN_METRICS,
        }

    def test_tagged_traffic_is_split_out_for_the_ratios(self, minutes):
        info = profile("map-opus5", "anthropic.claude-opus-5", cross_region=True)
        info = ProfileInfo(name=info.name, model=info.model, tag_value="migALPHA", model_arns=info.model_arns)
        minutes["profiles"] = {"us-east-1": {"2kbsta0lwebx": info}}
        minutes["regions"] = {"us-east-1": {"2kbsta0lwebx": {"Invocations": {5: 1}, "InputTokenCount": {5: 1_000_000}}}}
        account = self.account(tag_spec="map-migrated=migALPHA", tag_ratio=1.0, untag_ratio=1.5)
        usage = cost_estimate.hour_usage(account, self.HOUR)
        assert usage.tag_raw == pytest.approx(5.0) and usage.untag_raw == 0
        assert usage.marked(account) == pytest.approx(5.0)

    def test_a_failing_region_is_reported_not_fatal(self, minutes):
        minutes["regions"] = {
            "us-east-1": {"global.anthropic.claude-opus-5": {"Invocations": {1: 3}}},
            "us-west-2": {"global.anthropic.claude-opus-5": {"Invocations": {2: 4}}},
        }
        minutes["fail"] = {"us-west-2"}
        usage = cost_estimate.hour_usage(self.account(), self.HOUR)
        assert usage.invocations == 3
        assert len(usage.errors) == 1 and usage.errors[0].startswith("us-west-2：")

    def test_unpriced_models_are_named(self, minutes):
        minutes["regions"] = {"us-east-1": {"anthropic.claude-mystery-9": {"InputTokenCount": {1: 1000}}}}
        usage = cost_estimate.hour_usage(self.account(), self.HOUR)
        assert usage.unpriced == ["anthropic.claude-mystery-9"]
        assert usage.tokens == {"input": 1000} and usage.untag_raw == 0

    def test_no_traffic_means_no_data(self, minutes):
        usage = cost_estimate.hour_usage(self.account(), self.HOUR)
        assert not usage.has_data and usage.last_call is None
