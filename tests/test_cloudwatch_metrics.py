"""CloudWatch 数据层：模型名归一、口径切换、区域合并、折叠。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from bedrock_cost import cloudwatch_metrics as cwm
from bedrock_cost.usage_explorer import MAX_SERIES, OTHER_LABEL
from bedrock_cost.windows import MetricWindow

NOW = datetime(2026, 8, 17, 12, 0, tzinfo=timezone.utc)

# 真实账号里观察到的两种 ModelId 形态
PROFILES = {
    "2kbsta0lwebx": cwm.ProfileInfo(
        "map-global-claude-opus-4-8-use1", "anthropic.claude-opus-4-8", "migALPHA"),
    "op1t8r3a2578": cwm.ProfileInfo(
        "map-global-claude-opus-5-use1", "anthropic.claude-opus-5", "migALPHA"),
}


def window(hours: int = 6, period: str = "1h") -> MetricWindow:
    return MetricWindow(start=NOW - timedelta(hours=hours), end=NOW, period_key=period)


class TestNameNormalisation:
    @pytest.mark.parametrize(
        "raw, expected",
        [
            ("global.anthropic.claude-opus-5", "anthropic.claude-opus-5"),
            ("us.anthropic.claude-sonnet-5", "anthropic.claude-sonnet-5"),
            ("eu.anthropic.claude-opus-4-8", "anthropic.claude-opus-4-8"),
            ("anthropic.claude-opus-5", "anthropic.claude-opus-5"),  # 没有前缀就别动
        ],
    )
    def test_strip_cris_prefix(self, raw, expected):
        assert cwm.strip_cris_prefix(raw) == expected

    @pytest.mark.parametrize(
        "raw, expected",
        [
            ("global.anthropic.claude-opus-5", "claude-opus-5"),
            ("anthropic.claude-sonnet-4-5-20250929-v1:0", "claude-sonnet-4-5-20250929-v1:0"),
        ],
    )
    def test_short_model_name(self, raw, expected):
        assert cwm.short_model_name(raw) == expected

    @pytest.mark.parametrize("raw", ["2kbsta0lwebx", "op1t8r3a2578", "11ge55f4m22u"])
    def test_recognises_profile_ids(self, raw):
        assert cwm.looks_like_profile_id(raw)

    @pytest.mark.parametrize(
        "raw",
        [
            "global.anthropic.claude-opus-5",
            "anthropic.claude-opus-5",
            "2kbsta0lweb",  # 11 位
            "2KBSTA0LWEBX",  # 大写
            "",
        ],
    )
    def test_rejects_non_profile_ids(self, raw):
        assert not cwm.looks_like_profile_id(raw)

    @pytest.mark.parametrize(
        "name, region, expected",
        [
            ("map-global-claude-opus-4-8-use1", "us-east-1", "map-global-claude-opus-4-8"),
            ("map-global-claude-opus-4-8-usw2", "us-west-2", "map-global-claude-opus-4-8"),
            # 后缀和数据实际所属区域不符时不能乱剥
            ("map-global-claude-opus-4-8-use1", "us-west-2", "map-global-claude-opus-4-8-use1"),
            ("no-region-suffix", "us-east-1", "no-region-suffix"),
        ],
    )
    def test_strip_region_suffix_only_matches_its_own_region(self, name, region, expected):
        assert cwm.strip_region_suffix(name, region) == expected


class TestGroupKey:
    def test_always_groups_by_model(self):
        """图例一律按模型：配置和直连都归到同一条线上。"""
        assert cwm.group_key("2kbsta0lwebx", PROFILES) == "claude-opus-4-8"
        assert cwm.group_key("global.anthropic.claude-opus-4-8", PROFILES) == "claude-opus-4-8"

    def test_unresolved_profile_still_gets_a_label(self):
        """没有 bedrock 权限时也要能出图。"""
        assert cwm.group_key("zzzzzzzzzzzz", {}) == "未知配置 zzzzzzzzzzzz"


class TestIsTagged:
    """有标签 = 走了带正确 map-migrated 值的推理配置。规则与 cost_explorer 一致。"""

    def test_profile_with_matching_value_is_tagged(self, ledger, accounts):
        alpha = accounts[0]  # 台账 TAG=map-migrated=migALPHA
        assert cwm.is_tagged("2kbsta0lwebx", PROFILES, alpha) is True

    def test_profile_with_a_different_value_is_untagged(self, ledger, accounts):
        """值不对就不算——和成本页一样，打错 MAP ID 不能白蹭标签。"""
        beta = accounts[1]  # 台账 TAG=map-migrated=migBETA，配置上是 migALPHA
        assert cwm.is_tagged("2kbsta0lwebx", PROFILES, beta) is False

    def test_direct_model_is_untagged(self, ledger, accounts):
        assert cwm.is_tagged("global.anthropic.claude-opus-5", PROFILES, accounts[0]) is False

    def test_unresolved_profile_is_untagged(self, ledger, accounts):
        assert cwm.is_tagged("zzzzzzzzzzzz", {}, accounts[0]) is False

    def test_profile_without_the_tag_is_untagged(self, ledger, accounts):
        bare = {"aaaaaaaaaaaa": cwm.ProfileInfo("bare", "anthropic.claude-opus-5", "")}
        assert cwm.is_tagged("aaaaaaaaaaaa", bare, accounts[0]) is False

    def test_key_only_ledger_accepts_any_non_empty_value(self, tmp_path, monkeypatch):
        """台账只给键时，任意非空值都算有标签。"""
        from bedrock_cost import config, excel_source

        from .conftest import write_ledger

        path = tmp_path / "keyonly.xlsx"
        write_ledger(path, rows=[["P", 1, 1000, 1, 1.05, "ak", "sk", "map-migrated"]])
        monkeypatch.setattr(config, "EXCEL_PATH", path)
        excel_source.clear_cache()
        account = excel_source.load_accounts(force=True)[0]
        assert account.tag_value is None
        assert cwm.is_tagged("2kbsta0lwebx", PROFILES, account) is True


class TestBuildGrid:
    def test_bucket_count_and_alignment(self):
        stamps, labels = cwm.build_grid(window(6, "1h"))
        assert len(stamps) == len(labels) == 6
        assert all(int(s.timestamp()) % 3600 == 0 for s in stamps)

    def test_end_is_exclusive(self):
        win = window(6, "1h")
        stamps, _ = cwm.build_grid(win)
        assert stamps[-1] < win.end

    def test_label_format_follows_granularity(self):
        _, hourly = cwm.build_grid(window(6, "1h"))
        assert ":" in hourly[0]
        _, daily = cwm.build_grid(MetricWindow(NOW - timedelta(days=5), NOW, "1d"))
        assert ":" not in daily[0]


@pytest.fixture
def fake_cw(monkeypatch):
    """替换掉发现和取数，不联网。

    造两个账号 × 两个区，同一个模型在不同区都有量——用来验区域是真的合并了。
    """
    def fake_list(account, region):
        return ["global.anthropic.claude-opus-5", "2kbsta0lwebx", "op1t8r3a2578"]

    def fake_profiles(account, region):
        return PROFILES, True

    def fake_fetch(account, region, win, metric_key):
        stamps, _ = cwm.build_grid(win)
        width = len(stamps)
        return (
            {
                "global.anthropic.claude-opus-5": [10.0] * width,
                "2kbsta0lwebx": [5.0] * width,
                "op1t8r3a2578": [1.0] * width,
            },
            False,
            None,
        )

    monkeypatch.setattr(cwm, "list_model_ids", fake_list)
    monkeypatch.setattr(cwm, "resolve_profiles", fake_profiles)
    monkeypatch.setattr(cwm, "_fetch_region", fake_fetch)
    cwm.clear_cache()


class TestBuildMetrics:
    def test_regions_are_merged_into_one_line_per_model(self, ledger, accounts, fake_cw):
        win = window(6, "1h")
        one = cwm.build_metrics(accounts[:1], ["us-east-1"], win, "invocations", "all")
        four = cwm.build_metrics(accounts[:1], list(cwm.REGIONS), win, "invocations", "all")
        # 序列条数不变，金额是四倍——说明合并了而不是拆成四条
        assert {s.name for s in one.series} == {s.name for s in four.series}
        assert round(four.total, 6) == round(one.total * 4, 6)

    def test_profiles_roll_into_their_underlying_model(self, ledger, accounts, fake_cw):
        win = window(6, "1h")
        report = cwm.build_metrics(accounts[:1], ["us-east-1"], win, "invocations", "all")
        by_name = {s.name: s.total for s in report.series}
        # opus-5 = 直连 10 + 走配置 op1t8r3a2578 的 1，合成同一条线
        assert by_name["claude-opus-5"] == pytest.approx(11.0 * len(report.timestamps))
        assert by_name["claude-opus-4-8"] == pytest.approx(5.0 * len(report.timestamps))


class TestTagFilter:
    """fake 数据：直连 opus-5 每桶 10（无标签），两个配置 5 + 1（ALPHA 有标签）。"""

    def test_split_adds_up_to_the_unfiltered_total(self, ledger, accounts, fake_cw):
        report = cwm.build_metrics(accounts[:1], ["us-east-1"], window(), "invocations", "all")
        assert round(report.tagged_total + report.untagged_total, 6) == round(report.total, 6)

    def test_split_values(self, ledger, accounts, fake_cw):
        report = cwm.build_metrics(accounts[:1], ["us-east-1"], window(), "invocations", "all")
        width = len(report.timestamps)
        assert report.tagged_total == pytest.approx(6.0 * width)    # 5 + 1
        assert report.untagged_total == pytest.approx(10.0 * width)  # 直连

    def test_tagged_only_excludes_direct_traffic(self, ledger, accounts, fake_cw):
        report = cwm.build_metrics(accounts[:1], ["us-east-1"], window(), "invocations", "tagged")
        width = len(report.timestamps)
        assert report.total == pytest.approx(6.0 * width)
        by_name = {s.name: s.total for s in report.series}
        assert by_name["claude-opus-5"] == pytest.approx(1.0 * width)  # 只剩配置那份

    def test_untagged_only_excludes_profile_traffic(self, ledger, accounts, fake_cw):
        report = cwm.build_metrics(accounts[:1], ["us-east-1"], window(), "invocations", "untagged")
        width = len(report.timestamps)
        assert report.total == pytest.approx(10.0 * width)
        assert "claude-opus-4-8" not in {s.name for s in report.series}

    def test_split_is_unaffected_by_the_filter(self, ledger, accounts, fake_cw):
        """切到「仅有标签」时，汇总处仍要显示被排除掉的那部分有多少。"""
        totals = []
        for tag_filter in ("all", "tagged", "untagged"):
            report = cwm.build_metrics(
                accounts[:1], ["us-east-1"], window(), "invocations", tag_filter
            )
            totals.append((report.tagged_total, report.untagged_total))
        assert len(set(totals)) == 1

    def test_filtered_totals_sum_back_to_all(self, ledger, accounts, fake_cw):
        args = (accounts[:1], ["us-east-1"], window(), "invocations")
        every = cwm.build_metrics(*args, "all").total
        tagged = cwm.build_metrics(*args, "tagged").total
        untagged = cwm.build_metrics(*args, "untagged").total
        assert round(tagged + untagged, 6) == round(every, 6)

    def test_wrong_tag_value_counts_as_untagged(self, ledger, accounts, fake_cw):
        """BETA 台账要的是 migBETA，配置上是 migALPHA，所以它全是无标签。"""
        beta = cwm.build_metrics(accounts[1:2], ["us-east-1"], window(), "invocations", "all")
        assert beta.tagged_total == 0.0
        assert beta.untagged_total == pytest.approx(beta.total)

    def test_shares(self, ledger, accounts, fake_cw):
        report = cwm.build_metrics(accounts[:1], ["us-east-1"], window(), "invocations", "all")
        assert report.tagged_share == pytest.approx(6 / 16 * 100)
        assert report.untagged_share == pytest.approx(10 / 16 * 100)

    def test_shares_are_none_when_there_is_nothing(self, fake_cw):
        report = cwm.build_metrics([], list(cwm.REGIONS), window(), "invocations", "all")
        assert report.tagged_share is None and report.untagged_share is None

    def test_unknown_filter_falls_back_to_all(self, ledger, accounts, fake_cw):
        report = cwm.build_metrics(accounts[:1], ["us-east-1"], window(), "invocations", "nonsense")
        assert report.tag_filter == cwm.DEFAULT_TAG_FILTER
        assert report.is_filtered is False

    def test_unreadable_tags_are_flagged(self, ledger, accounts, monkeypatch, fake_cw):
        """读不到标签时全部会落进无标签，这件事必须能被页面发现。"""
        monkeypatch.setattr(cwm, "resolve_profiles", lambda a, r: ({}, False))
        cwm.clear_cache()
        report = cwm.build_metrics(accounts[:1], ["us-east-1"], window(), "invocations", "all")
        assert report.tags_resolved is False
        assert report.tagged_total == 0.0

    def test_no_profiles_at_all_is_not_flagged(self, ledger, accounts, monkeypatch):
        """账号根本没用配置时，「读不到标签」不该误报。"""
        monkeypatch.setattr(cwm, "list_model_ids", lambda a, r: [])
        monkeypatch.setattr(cwm, "resolve_profiles", lambda a, r: ({}, False))
        monkeypatch.setattr(
            cwm, "_fetch_region",
            lambda acc, reg, win, mk: (
                {"global.anthropic.claude-opus-5": [1.0] * len(cwm.build_grid(win)[0])}, False, None
            ),
        )
        cwm.clear_cache()
        report = cwm.build_metrics(accounts[:1], ["us-east-1"], window(), "invocations", "all")
        assert report.tags_resolved is True

    def test_rows_and_columns_agree(self, ledger, accounts, fake_cw):
        report = cwm.build_metrics(accounts, list(cwm.REGIONS), window(), "invocations", "all")
        assert round(sum(report.column_totals), 6) == round(report.total, 6)

    def test_series_length_matches_grid(self, ledger, accounts, fake_cw):
        report = cwm.build_metrics(accounts, ["us-east-1"], window(6, "1h"), "invocations", "all")
        assert all(len(s.values) == len(report.timestamps) for s in report.series)

    def test_series_sorted_descending(self, ledger, accounts, fake_cw):
        report = cwm.build_metrics(accounts, list(cwm.REGIONS), window(), "invocations", "all")
        totals = [s.total for s in report.series]
        assert totals == sorted(totals, reverse=True)

    def test_unknown_metric_and_filter_fall_back(self, ledger, accounts, fake_cw):
        report = cwm.build_metrics(accounts, ["us-east-1"], window(), "nonsense", "nonsense")
        assert report.metric_key == cwm.DEFAULT_METRIC
        assert report.tag_filter == cwm.DEFAULT_TAG_FILTER

    def test_unknown_region_is_dropped_and_defaults_applied(self, ledger, accounts, fake_cw):
        report = cwm.build_metrics(accounts, ["mars-west-1"], window(), "invocations", "all")
        assert report.regions == cwm.DEFAULT_REGIONS

    def test_no_accounts_gives_an_empty_report(self, fake_cw):
        report = cwm.build_metrics([], list(cwm.REGIONS), window(), "invocations", "all")
        assert report.series == []
        assert report.total == 0.0
        assert report.peak == (-1, 0.0)
        assert report.has_data is False

    def test_errors_are_collected_not_raised(self, ledger, accounts, monkeypatch, fake_cw):
        monkeypatch.setattr(
            cwm, "_fetch_region", lambda *a, **k: ({}, False, "凭证缺少 cloudwatch 权限")
        )
        report = cwm.build_metrics(accounts, ["us-east-1"], window(), "invocations", "all")
        # 每个 (账号, 区域) 组合各报一条，出错的账号能被指名
        assert len(report.errors) == len(accounts)
        assert all(a.partner in " ".join(map(str, report.errors)) for a in accounts)
        # 一行字的样子没变：「上游 / 账号 @ 区域：原因」
        assert str(report.errors[0]) == (
            f"{accounts[0].partner} / {accounts[0].account} @ us-east-1：凭证缺少 cloudwatch 权限"
        )
        assert report.series == []

    def test_one_bad_region_does_not_sink_the_others(self, ledger, accounts, monkeypatch, fake_cw):
        good = cwm._fetch_region

        def flaky(account, region, win, metric_key):
            if region == "us-west-1":
                return {}, False, "该区不可用"
            return good(account, region, win, metric_key)

        monkeypatch.setattr(cwm, "_fetch_region", flaky)
        cwm.clear_cache()
        report = cwm.build_metrics(accounts, list(cwm.REGIONS), window(), "invocations", "all")
        assert report.errors and report.series
        assert report.total > 0

    def test_folds_the_tail_into_other_with_grey(self, ledger, accounts, monkeypatch):
        def many(account, region, win, metric_key):
            stamps, _ = cwm.build_grid(win)
            return ({f"model-{i:02d}": [float(30 - i)] * len(stamps) for i in range(15)}, False, None)

        monkeypatch.setattr(cwm, "list_model_ids", lambda a, r: [])
        monkeypatch.setattr(cwm, "resolve_profiles", lambda a, r: ({}, True))
        monkeypatch.setattr(cwm, "_fetch_region", many)
        cwm.clear_cache()
        report = cwm.build_metrics(accounts[:1], ["us-east-1"], window(), "invocations", "all")

        assert len(report.series) == MAX_SERIES + 1
        assert report.series[-1].name == OTHER_LABEL
        assert report.series[-1].slot == -1
        assert report.folded_count == 15 - MAX_SERIES
        # 折叠不能丢量
        expected = sum(30 - i for i in range(15)) * len(report.timestamps)
        assert round(report.total, 4) == round(float(expected), 4)

    def test_peak_locates_the_busiest_bucket(self, ledger, accounts, monkeypatch):
        def spiky(account, region, win, metric_key):
            stamps, _ = cwm.build_grid(win)
            values = [1.0] * len(stamps)
            values[2] = 99.0
            return ({"m": values}, False, None)

        monkeypatch.setattr(cwm, "list_model_ids", lambda a, r: [])
        monkeypatch.setattr(cwm, "resolve_profiles", lambda a, r: ({}, True))
        monkeypatch.setattr(cwm, "_fetch_region", spiky)
        cwm.clear_cache()
        report = cwm.build_metrics(accounts[:1], ["us-east-1"], window(), "invocations", "all")
        assert report.peak == (2, 99.0)


class TestRegionPanels:
    """2×2 小倍数图的前提：四个面板的序列集合、顺序、颜色槽必须完全一致。"""

    def test_one_panel_per_region_in_order(self, ledger, accounts, fake_cw):
        report = cwm.build_metrics(accounts, list(cwm.REGIONS), window(), "invocations", "all")
        assert [p.region for p in report.panels] == list(cwm.REGIONS)

    def test_panels_share_names_order_and_slots(self, ledger, accounts, fake_cw):
        report = cwm.build_metrics(accounts, list(cwm.REGIONS), window(), "invocations", "all")
        names = {tuple(s.name for s in p.series) for p in report.panels}
        slots = {tuple(s.slot for s in p.series) for p in report.panels}
        assert len(names) == 1, "同一个模型在不同面板里必须同名同序"
        assert len(slots) == 1, "同一个模型在四张图里必须是同一个颜色"

    def test_panel_series_match_the_shared_legend(self, ledger, accounts, fake_cw):
        report = cwm.build_metrics(accounts, list(cwm.REGIONS), window(), "invocations", "all")
        assert tuple(s.name for s in report.panels[0].series) == tuple(
            s.name for s in report.series
        )

    def test_panel_totals_sum_to_the_grand_total(self, ledger, accounts, fake_cw):
        report = cwm.build_metrics(accounts, list(cwm.REGIONS), window(), "invocations", "all")
        assert round(sum(p.total for p in report.panels), 6) == round(report.total, 6)

    def test_panel_series_lengths_match_the_grid(self, ledger, accounts, fake_cw):
        report = cwm.build_metrics(accounts, list(cwm.REGIONS), window(), "invocations", "all")
        width = len(report.timestamps)
        assert all(len(s.values) == width for p in report.panels for s in p.series)

    def test_region_without_a_model_gets_zeros_not_a_missing_series(
        self, ledger, accounts, monkeypatch
    ):
        """某个区没跑过某个模型时补零，序列不能缺——否则面板间颜色会错位。"""

        def uneven(account, region, win, metric_key):
            stamps, _ = cwm.build_grid(win)
            width = len(stamps)
            if region == "us-east-1":
                return {"model-a": [5.0] * width, "model-b": [3.0] * width}, False, None
            return {"model-a": [1.0] * width}, False, None

        monkeypatch.setattr(cwm, "list_model_ids", lambda a, r: [])
        monkeypatch.setattr(cwm, "resolve_profiles", lambda a, r: ({}, True))
        monkeypatch.setattr(cwm, "_fetch_region", uneven)
        cwm.clear_cache()
        report = cwm.build_metrics(accounts[:1], list(cwm.REGIONS), window(), "invocations", "all")

        assert all(len(p.series) == len(report.series) for p in report.panels)
        west = next(p for p in report.panels if p.region == "us-west-2")
        model_b = next(s for s in west.series if s.name == "model-b")
        assert model_b.total == 0.0

    def test_shared_peak_is_a_single_series_max_not_a_sum(self, ledger, accounts, fake_cw):
        """折线是叠放不是堆叠，Y 轴上限取单条最大值，取和会让所有线压扁。"""
        report = cwm.build_metrics(accounts, list(cwm.REGIONS), window(), "invocations", "all")
        biggest_single = max(s.peak for p in report.panels for s in p.series)
        assert report.shared_peak == biggest_single
        assert report.shared_peak <= max(v for p in report.panels for v in p.column_totals)

    def test_region_totals_map(self, ledger, accounts, fake_cw):
        report = cwm.build_metrics(accounts, list(cwm.REGIONS), window(), "invocations", "all")
        assert set(report.region_totals) == set(cwm.REGIONS)

    def test_empty_report_has_no_panels(self, fake_cw):
        report = cwm.build_metrics([], list(cwm.REGIONS), window(), "invocations", "all")
        assert report.panels == []
        assert report.shared_peak == 0.0

    def test_panel_with_no_traffic_reports_empty(self, ledger, accounts, monkeypatch):
        def only_east(account, region, win, metric_key):
            stamps, _ = cwm.build_grid(win)
            if region == "us-east-1":
                return {"m": [4.0] * len(stamps)}, False, None
            return {}, False, None

        monkeypatch.setattr(cwm, "list_model_ids", lambda a, r: [])
        monkeypatch.setattr(cwm, "resolve_profiles", lambda a, r: ({}, True))
        monkeypatch.setattr(cwm, "_fetch_region", only_east)
        cwm.clear_cache()
        report = cwm.build_metrics(accounts[:1], list(cwm.REGIONS), window(), "invocations", "all")
        by_region = {p.region: p for p in report.panels}
        assert by_region["us-east-1"].has_data is True
        assert by_region["us-west-2"].has_data is False


class TestMetricDefinitions:
    def test_total_tokens_sums_input_and_output(self):
        assert cwm.METRICS["total_tokens"][1] == ("InputTokenCount", "OutputTokenCount")

    def test_units_separate_counts_from_tokens(self):
        assert cwm.METRICS["invocations"][2] == "次"
        assert cwm.METRICS["input_tokens"][2] == "token"

    def test_four_us_regions(self):
        assert set(cwm.REGIONS) == {"us-east-1", "us-east-2", "us-west-1", "us-west-2"}


# ------------------------------------------------------------------ 告警用：每小时调用次数
class TestHourlyInvocations:
    """Telegram「用量切换」告警的数据源：几个整点小时里四个区合计的调用次数。"""

    HOURS = [datetime(2026, 9, 27, h, tzinfo=timezone.utc) for h in (3, 4, 5)]

    @pytest.fixture
    def fake(self, monkeypatch):
        """{区域: {ModelId: {小时: 次数}}}，fail 里的区域会抛错。"""
        knobs = {"data": {}, "fail": set()}

        def fake_list(account, region):
            if region in knobs["fail"]:
                raise RuntimeError("ThrottlingException")
            return sorted(knobs["data"].get(region, {}))

        class Client:
            def __init__(self, region):
                self.region = region

            def get_metric_data(self, **kwargs):
                results = []
                for query in kwargs["MetricDataQueries"]:
                    stat = query["MetricStat"]
                    assert stat["Metric"]["MetricName"] == "Invocations"
                    assert stat["Period"] == 3600
                    model_id = stat["Metric"]["Dimensions"][0]["Value"]
                    points = knobs["data"][self.region][model_id]
                    results.append({
                        "Id": query["Id"],
                        "Timestamps": list(points),
                        "Values": list(points.values()),
                    })
                return {"MetricDataResults": results}

        monkeypatch.setattr(cwm, "list_model_ids", fake_list)
        monkeypatch.setattr(cwm, "_client", lambda a, s, region: Client(region))
        return knobs

    def test_sums_models_and_regions_per_hour(self, accounts, fake):
        h3, h4, h5 = self.HOURS
        fake["data"] = {
            "us-east-1": {
                "global.anthropic.claude-opus-5": {h3: 10.0, h5: 1.0},
                "2kbsta0lwebx": {h3: 5.0},
            },
            "us-west-2": {"global.anthropic.claude-opus-5": {h4: 7.0}},
        }
        counts, failed = cwm.hourly_invocations(accounts[0], self.HOURS)
        assert failed == []
        assert counts == [15.0, 7.0, 1.0]

    def test_no_models_means_zero_not_unknown(self, accounts, fake):
        counts, failed = cwm.hourly_invocations(accounts[0], self.HOURS)
        assert counts == [0.0, 0.0, 0.0] and failed == []

    def test_one_failed_region_makes_the_whole_answer_unknown(self, accounts, fake):
        """缺一个区的零不能当成真的零，否则那个区的调用会被误报成「用量中断」。"""
        fake["data"] = {"us-east-1": {"global.anthropic.claude-opus-5": {self.HOURS[-1]: 3.0}}}
        fake["fail"] = {"us-west-2"}
        counts, failed = cwm.hourly_invocations(accounts[0], self.HOURS)
        assert counts is None
        assert len(failed) == 1 and "us-west-2" in failed[0]

    def test_ignores_points_outside_the_window(self, accounts, fake):
        early = datetime(2026, 9, 27, 1, tzinfo=timezone.utc)
        fake["data"] = {"us-east-1": {"m": {early: 99.0, self.HOURS[0]: 2.0}}}
        counts, _ = cwm.hourly_invocations(accounts[0], self.HOURS)
        assert counts == [2.0, 0.0, 0.0]
