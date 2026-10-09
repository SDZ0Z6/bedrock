"""Jinja 过滤器：金额、百分比、比率、紧凑数字、模型短名，以及注册到 app 上的样子。"""

from __future__ import annotations

import pytest

from bedrock_cost import chart, config, filters
from bedrock_cost.filters import compact, money, money0, pct, ratio, short_model


@pytest.fixture(autouse=True)
def _dollar(monkeypatch):
    """开发机的 .env 可以换币种符号；这里的期望值一律按 $ 写。"""
    monkeypatch.setattr(config, "CURRENCY_SYMBOL", "$")


class TestMoney:
    @pytest.mark.parametrize(
        "value, expected",
        [
            (0, "$0.00"),
            (12.3, "$12.30"),
            (1234567.891, "$1,234,567.89"),
            (-12.3, "-$12.30"),
            (-1234.5, "-$1,234.50"),
        ],
    )
    def test_formats(self, value, expected):
        assert money(value) == expected

    def test_negative_sign_goes_before_the_symbol(self):
        """和告警卡片一个写法：-$12.30，不是 $-12.30。"""
        assert money(-12.3) == "-$12.30"
        assert "$-" not in money(-0.5)

    def test_none_is_a_dash(self):
        assert money(None) == "—"

    def test_uses_the_configured_symbol(self, monkeypatch):
        monkeypatch.setattr(config, "CURRENCY_SYMBOL", "￥")
        assert money(-5) == "-￥5.00"


class TestMoney0:
    @pytest.mark.parametrize(
        "value, expected",
        [
            (0, "$0"),
            (72101.6, "$72,102"),
            (999.4, "$999"),
            (-1234.4, "-$1,234"),
        ],
    )
    def test_whole_dollars_with_separators(self, value, expected):
        assert money0(value) == expected

    def test_none_is_a_dash(self):
        assert money0(None) == "—"


class TestPct:
    @pytest.mark.parametrize(
        "value, expected",
        [(0, "0.00%"), (12.3, "12.30%"), (1234.5, "1,234.50%"), (-5, "-5.00%")],
    )
    def test_two_decimals(self, value, expected):
        assert pct(value) == expected

    def test_none_is_a_dash(self):
        """没有额度时使用率是 None，页面上显示一道杠，不是 0%。"""
        assert pct(None) == "—"


class TestRatio:
    @pytest.mark.parametrize(
        "value, expected",
        [
            (1.05, "1.05"),
            (1.0, "1"),
            (1.1, "1.1"),
            (1.23456, "1.2346"),
            (0, "0"),
            (0.5, "0.5"),
            (10, "10"),
        ],
    )
    def test_strips_trailing_zeros_only(self, value, expected):
        """1.0500 -> 1.05，1.0000 -> 1；但 10 不能被剥成 1。"""
        assert ratio(value) == expected


class TestCompact:
    @pytest.mark.parametrize(
        "value, expected",
        [
            (0, "0"),
            (5, "5"),
            (2.5, "2.5"),
            (46_200, "46.2K"),
            (308_400_000, "308.4M"),
            (1_500_000_000, "1.50B"),
            (-46_200, "-46.2K"),
        ],
    )
    def test_formats(self, value, expected):
        assert compact(value) == expected

    def test_same_as_the_chart_axis(self):
        """表格和图上的刻度是同一套写法，不能各写一份。"""
        for value in (0, 7, 1234, 98_765_432):
            assert compact(value) == chart.compact_number(value)

    def test_none_is_a_dash(self):
        assert compact(None) == "—"


class TestShortModel:
    @pytest.mark.parametrize(
        "name, expected",
        [
            ("claude-sonnet-4-5-20250929-v1:0", "Sonnet 4.5"),
            ("anthropic.claude-sonnet-4-5-20250929-v1:0", "Sonnet 4.5"),
            ("global.anthropic.claude-opus-5", "Opus 5"),
            ("claude-opus-4-8", "Opus 4.8"),
            ("anthropic.claude-haiku-4-5-20251001-v1:0", "Haiku 4.5"),
            # 日期戳不能被读成小版本号：4-20250514 是 4，不是 4.2
            ("claude-sonnet-4-20250514-v1:0", "Sonnet 4"),
            ("Claude Opus 4.6 (Amazon Bedrock Edition)", "Opus 4.6"),
            ("claude-fable-5", "Fable 5"),
        ],
    )
    def test_known_models(self, name, expected):
        assert short_model(name) == expected

    @pytest.mark.parametrize(
        "name",
        ["未知配置 2kbsta0lwebx", "其他", "amazon.nova-2-lite", "claude-instant-v1", ""],
    )
    def test_unknown_names_come_back_unchanged(self, name):
        assert short_model(name) == name


class TestLoadingMedia:
    def test_prefers_webm(self, tmp_path):
        (tmp_path / "loading.mp4").write_bytes(b"x")
        (tmp_path / "loading.webm").write_bytes(b"x")
        assert filters.loading_media(str(tmp_path)) == "loading.webm"

    def test_falls_back_to_mp4(self, tmp_path):
        (tmp_path / "loading.mp4").write_bytes(b"x")
        assert filters.loading_media(str(tmp_path)) == "loading.mp4"

    def test_nothing_there(self, tmp_path):
        assert filters.loading_media(str(tmp_path)) == ""
        assert filters.loading_media(None) == ""

    def test_a_directory_with_that_name_does_not_count(self, tmp_path):
        (tmp_path / "loading.webm").mkdir()
        assert filters.loading_media(str(tmp_path)) == ""


class TestRegistered:
    """模板里用到的过滤器和全局都挂在 app 上，并且就是这里这几个函数。"""

    def test_filters(self, app):
        registered = app.jinja_env.filters
        for name in ("money", "money0", "pct", "ratio", "compact", "short_model"):
            assert registered[name] is getattr(filters, name)

    def test_globals_come_from_chart(self, app):
        """图例、表格色块、热力格的配色只在 chart.py 定义一份。"""
        env = app.jinja_env.globals
        assert env["series_color"] is chart.color_for
        assert env["heat_style"] is chart.heat_style
        assert env["heat_ramp"] is chart.HEAT_RAMP
        assert callable(env["csrf_token"])

    def test_renders_in_a_template(self, app):
        template = app.jinja_env.from_string(
            "{{ -12.3|money }} {{ 72101.6|money0 }} {{ none|pct }} "
            "{{ 1.05|ratio }} {{ 46200|compact }} {{ name|short_model }} {{ series_color(-1) }}"
        )
        rendered = template.render(name="claude-sonnet-4-5-20250929-v1:0")
        assert rendered == f"-$12.30 $72,102 — 1.05 46.2K Sonnet 4.5 {chart.OTHER_COLOR}"
