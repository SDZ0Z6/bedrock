"""共享 fixture。

默认所有用例都不联网也不花钱：Cost Explorer 调用被 fake 替换，台账指向临时
xlsx，登录口令也换成固定值（不依赖开发机上的 .env）。

真正调用 CE 的用例集中在 test_integration.py，标了 `integration`，默认跳过。
"""

from __future__ import annotations

import urllib.request
from datetime import date

import openpyxl
import pytest

from bedrock_cost import auth, config, cost_explorer, create_app, excel_source, usage_explorer
from bedrock_cost.cost_explorer import CostSplit

TEST_USER = "tester"
TEST_PASSWORD = "test-password-123"

# 台账里的两个账号。SK 用假值，永远不会真的发出去。
# 两个账号的启用日期都填 RANGE_START：概览页按启用日期累计，下钻页按显式区间查，
# 两边区间一致，「下钻各维度加总 == 概览总消费」这条不变量才比得起来。
RANGE_START = date(2026, 8, 1)
LEDGER_HEADER = [
    "PARTNER", "ACCOUNT", "BUDGET", "TAG_RATIO", "UNTAG_RATIO", "AK", "SK", "TAG", "START_DATE",
]
LEDGER_ROWS = [
    ["ALPHA", 111111111111, 500000, 1, 1.05, "AKIAFAKEALPHA0000000", "x" * 40,
     "map-migrated=migALPHA", RANGE_START],
    ["BETA", 222222222222, 100000, 1, 1.10, "AKIAFAKEBETA00000000", "y" * 40,
     "map-migrated=migBETA", RANGE_START],
]

# fake 的每日消费：打了台账标签的和没打的各一份。
# 概览页和下钻页的 fake 都从这两个常量算，所以两边的合计天然一致——
# 「下钻各维度加总 == 概览总消费」这条不变量才测得有意义。
DAILY_TAGGED_RAW = 100.0
DAILY_UNTAGGED_RAW = 50.0


def ledger_value(row: list, column: str):
    """按表头名取台账行里的值。

    别按下标取——台账加一列（比如 START_DATE）就会把所有位置解包的地方打断。
    """
    return row[LEDGER_HEADER.index(column)]


def ledger_without(column: str) -> tuple[list, list]:
    """去掉某一列之后的 (表头, 数据行)，用来测「老台账没有这一列」。"""
    position = LEDGER_HEADER.index(column)
    header = [name for name in LEDGER_HEADER if name != column]
    rows = [
        [value for index, value in enumerate(row) if index != position]
        for row in LEDGER_ROWS
    ]
    return header, rows


def day_count(start: date, end: date) -> int:
    return (end - start).days + 1


def expected_marked(account, start: date, end: date) -> float:
    """某个账号在区间内的加价后金额（fake 数据下的理论值）。"""
    days = day_count(start, end)
    return (
        DAILY_TAGGED_RAW * days * account.tag_ratio
        + DAILY_UNTAGGED_RAW * days * account.untag_ratio
    )


def write_ledger(path, header=None, rows=None) -> None:
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.append(list(header if header is not None else LEDGER_HEADER))
    for row in rows if rows is not None else LEDGER_ROWS:
        sheet.append(list(row))
    workbook.save(path)


@pytest.fixture(autouse=True)
def _no_network(request, monkeypatch):
    """兜底：哪个用例忘了替换发送口，也不能真的往 Telegram（或别处）发请求。

    Telegram 客户端和公开价目表都走 urllib。要看请求长什么样的用例自己再替换一次
    urlopen（见 test_alerts 的 http），后替换的生效；标了 integration 的本来就要联网。

    开发机的 .env 里可能配着真的 Bot Token：测试里一律当没配，要发的用例自己设一个假的。
    """
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "")
    if request.node.get_closest_marker("integration"):
        return

    def refuse(target, *args, **kwargs):
        raise AssertionError(f"测试想联网：{getattr(target, 'full_url', target)}")

    monkeypatch.setattr(urllib.request, "urlopen", refuse)


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    """临时台账，并让 config 指向它。"""
    path = tmp_path / "cred.xlsx"
    write_ledger(path)
    monkeypatch.setattr(config, "EXCEL_PATH", path)
    excel_source.clear_cache()
    yield path
    excel_source.clear_cache()


@pytest.fixture
def accounts(ledger):
    return excel_source.load_accounts(force=True)


@pytest.fixture
def fake_costs(monkeypatch):
    """把两条 CE 通路都换成固定数值，互相对得上。"""

    def fake_fetch_all(account_list, ranges, refresh=False):
        # 区间按账号给：概览页每个账号的起点可能不同
        splits = {}
        for account in account_list:
            start, end = ranges[account.key]
            days = day_count(start, end)
            splits[account.key] = CostSplit(
                tag_raw=DAILY_TAGGED_RAW * days,
                untag_raw=DAILY_UNTAGGED_RAW * days,
                currency="USD",
            )
        return splits

    def fake_fetch_account(account, start, end, dimension, granularity, dates, refresh):
        width = len(dates)

        def cell(raw: float, is_tag: bool) -> list[float]:
            rate = account.tag_ratio if is_tag else account.untag_ratio
            return [raw, raw * rate]

        # 按桶摊平：按日时每天一份，按月时把整月的量压进一个桶
        per_bucket = day_count(start, end) / width if width else 0
        tagged = DAILY_TAGGED_RAW * per_bucket
        untagged = DAILY_UNTAGGED_RAW * per_bucket

        if dimension == "service":
            rows = {
                "Claude Opus 5 (Amazon Bedrock Edition)": [cell(tagged, True) for _ in range(width)],
                "Claude Sonnet 5 (Amazon Bedrock Edition)": [cell(untagged, False) for _ in range(width)],
            }
        elif dimension == "tag":
            rows = {
                account.tag_value or "tagged": [cell(tagged, True) for _ in range(width)],
                usage_explorer.UNTAGGED_LABEL: [cell(untagged, False) for _ in range(width)],
            }
        else:
            label = f"{account.partner} / {account.account}"
            merged = [
                [
                    tagged + untagged,
                    tagged * account.tag_ratio + untagged * account.untag_ratio,
                ]
                for _ in range(width)
            ]
            rows = {label: merged}
        return rows, "USD", False, None

    monkeypatch.setattr(cost_explorer, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(usage_explorer, "_fetch_account", fake_fetch_account)
    cost_explorer.clear_cache()
    usage_explorer.clear_cache()


@pytest.fixture
def fake_cloudwatch(monkeypatch):
    """替换掉 CloudWatch 的发现与取数，不联网。"""
    from bedrock_cost import cloudwatch_metrics as cwm

    # 一个带正确标签的配置；直连模型没有标签
    profiles = {
        "2kbsta0lwebx": cwm.ProfileInfo(
            name="map-global-claude-opus-4-8-use1",
            model="anthropic.claude-opus-4-8",
            tag_value="migALPHA",
        ),
    }

    def fake_list(account, region):
        return ["global.anthropic.claude-opus-5", "2kbsta0lwebx"]

    def fake_fetch(account, region, win, metric_key):
        stamps, _ = cwm.build_grid(win)
        width = len(stamps)
        return (
            {
                "global.anthropic.claude-opus-5": [12.0] * width,
                "2kbsta0lwebx": [7.0] * width,
            },
            False,
            None,
        )

    monkeypatch.setattr(cwm, "list_model_ids", fake_list)
    monkeypatch.setattr(cwm, "resolve_profiles", lambda a, r: (profiles, True))
    monkeypatch.setattr(cwm, "_fetch_region", fake_fetch)
    cwm.clear_cache()


@pytest.fixture
def app(monkeypatch):
    monkeypatch.setattr(config, "AUTH_USERNAME", TEST_USER)
    monkeypatch.setattr(config, "AUTH_PASSWORD", TEST_PASSWORD)
    monkeypatch.setattr(config, "AUTH_PASSWORD_HASH", "")
    auth.clear_failures()
    return create_app(TESTING=True, SECRET_KEY="test-secret-key")


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def logged_in(client):
    response = client.post(
        "/login", data={"username": TEST_USER, "password": TEST_PASSWORD}
    )
    assert response.status_code == 302, "fixture 自己就登录失败了"
    return client


@pytest.fixture
def fake_service_quotas(monkeypatch):
    """替换 Service Quotas 和 Bedrock 推理配置，返回实测样式的数据，不联网。

    配额页按应用推理配置的 ARN 逐条列，所以两套数据都要给。配额值按区不同
    （照实测：Opus 4.6 只有 us-east-1 提到了 6M），页面上的区域列和
    「没跟上提额」标记才有东西可测。
    """
    from bedrock_cost import quotas

    day = "Global cross-region model inference tokens per day for "
    minute = "Global cross-region model inference tokens per minute for "

    def q(name, value):
        # 照实测：日配额不可调，分钟配额可申请提额
        return {
            "QuotaName": name,
            "Value": value,
            "QuotaCode": "L-X",
            "Adjustable": " per minute " in name,
        }

    # 两个账号的 AK 不一样，据此给出各自的账号号码。多账号时 ARN 必须互不相同，
    # 配额也要不同，不然「一行一条 ARN」和「最高值不跨账号」都测不出来。
    ACCOUNT_BY_AK = {
        "AKIAFAKEALPHA0000000": "111111111111",
        "AKIAFAKEBETA00000000": "222222222222",
    }

    def quotas_for(region, acct):
        # 提额按区批，而且只有 ALPHA 那个账号提了：BETA 四个区都是 3M。
        # 于是「最高值按账号分组」一旦写错，BETA 会被整片标黄。
        raised = region == "us-east-1" and acct == "111111111111"
        return [
            q(day + "Anthropic Claude Opus 4.8", 43_200_000_000),
            q(minute + "Anthropic Claude Opus 4.8", 30_000_000),
            q(day + "Anthropic Claude Opus 4.6 V1", 8_640_000_000 if raised else 4_320_000_000),
            q(minute + "Anthropic Claude Opus 4.6 V1", 6_000_000 if raised else 3_000_000),
            q(day + "Anthropic Claude Sonnet 4.5 V1 1M Context Length", 1_440_000_000),
            q(minute + "Anthropic Claude Sonnet 4.5 V1 1M Context Length", 1_000_000),
            q(day + "Amazon Nova 2 Lite", 11_520_000_000),      # 非 Claude，应被排除
            q("Batch inference job size (in GB) for Claude Opus 5", 1000),  # 无关配额
        ]

    # 账号自建的应用推理配置。配置名是账号自己起的，和模型名没关系。
    PROFILES = [
        ("anthropic.claude-opus-4-8", "claude48oupsauto_0706"),
        ("anthropic.claude-opus-4-6-v1", "claude46Oupsauto_wjc_0529"),
    ]

    def profiles_for(region, acct):
        summaries = []
        for index, (model, name) in enumerate(PROFILES):
            pid = f"{acct[:2]}{region.replace('-', '')}{index}"
            summaries.append({
                "inferenceProfileId": pid,
                "inferenceProfileName": name,
                "inferenceProfileArn": (
                    f"arn:aws:bedrock:{region}:{acct}"
                    f":application-inference-profile/{pid}"
                ),
                # 跨区配置会多带一条不带区域的 modelArn
                "models": [
                    {"modelArn": f"arn:aws:bedrock:::foundation-model/{model}"},
                    {"modelArn": f"arn:aws:bedrock:{region}::foundation-model/{model}"},
                ],
                "status": "ACTIVE",
                "type": "APPLICATION",
            })
        return summaries

    class _Paginator:
        def __init__(self, region, acct):
            self.region, self.acct = region, acct

        def paginate(self, **kwargs):
            return iter([{"Quotas": quotas_for(self.region, self.acct)}])

    class _Client:
        def __init__(self, region, acct):
            self.region, self.acct = region, acct

        def get_paginator(self, name):
            return _Paginator(self.region, self.acct)

        def list_inference_profiles(self, **kwargs):
            return {"inferenceProfileSummaries": profiles_for(self.region, self.acct)}

    def factory(service, **kw):
        # boto3.client 收到的 AK 就是账号身份，据此分派
        acct = ACCOUNT_BY_AK.get(kw.get("aws_access_key_id", ""), "999999999999")
        return _Client(kw.get("region_name", ""), acct)

    monkeypatch.setattr(quotas.boto3, "client", factory)
    quotas.clear_cache()
    yield
    quotas.clear_cache()
