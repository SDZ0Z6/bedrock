"""Web 层：访问控制、参数校验、页面结构、凭证不外泄。

CE 由 fake 替换，所以这一组跑得快也不花钱。
"""

from __future__ import annotations

from datetime import date

import pytest

from bedrock_cost import auth, config
from bedrock_cost.dates import preset_range

from .conftest import TEST_PASSWORD, TEST_USER

PROTECTED = ["/", "/cost-usage", "/cache/clear"]


class TestAuth:
    @pytest.mark.parametrize("path", PROTECTED)
    def test_anonymous_is_redirected_to_login(self, client, path):
        response = client.get(path)
        assert response.status_code == 302
        assert "/login" in response.headers["Location"]

    def test_wrong_password_is_rejected(self, client):
        response = client.post("/login", data={"username": TEST_USER, "password": "nope"})
        assert response.status_code == 200
        assert client.get("/").status_code == 302

    def test_wrong_username_is_rejected(self, client):
        response = client.post("/login", data={"username": "someoneelse", "password": TEST_PASSWORD})
        assert response.status_code == 200
        assert client.get("/").status_code == 302

    def test_login_then_logout(self, client, ledger, fake_costs):
        client.post("/login", data={"username": TEST_USER, "password": TEST_PASSWORD})
        assert client.get("/").status_code == 200
        client.get("/logout")
        assert client.get("/").status_code == 302

    def test_lockout_after_repeated_failures(self, client):
        auth.clear_failures()
        codes = [
            client.post("/login", data={"username": TEST_USER, "password": "bad"}).status_code
            for _ in range(config.MAX_LOGIN_ATTEMPTS + 2)
        ]
        assert 429 in codes
        auth.clear_failures()

    def test_external_next_is_refused(self, client):
        response = client.post(
            "/login",
            data={"username": TEST_USER, "password": TEST_PASSWORD, "next": "https://evil.example.com/x"},
        )
        assert "evil.example.com" not in response.headers.get("Location", "")

    def test_internal_next_is_honoured(self, client):
        response = client.post(
            "/login",
            data={"username": TEST_USER, "password": TEST_PASSWORD, "next": "/cost-usage"},
        )
        assert response.headers["Location"] == "/cost-usage"

    def test_empty_password_config_locks_everyone_out(self, client, monkeypatch):
        monkeypatch.setattr(config, "AUTH_PASSWORD", "")
        monkeypatch.setattr(config, "AUTH_PASSWORD_HASH", "")
        response = client.post("/login", data={"username": TEST_USER, "password": ""})
        assert response.status_code == 200


class TestShell:
    @pytest.mark.parametrize("path", ["/", "/cost-usage"])
    def test_sidebar_has_both_pages_and_marks_the_current_one(self, logged_in, ledger, fake_costs, path):
        html = logged_in.get(path).get_data(as_text=True)
        assert 'href="/"' in html and 'href="/cost-usage"' in html
        assert "nav-on" in html

    @pytest.mark.parametrize("path", ["/", "/cost-usage"])
    def test_collapse_control_and_pre_paint_script(self, logged_in, ledger, fake_costs, path):
        html = logged_in.get(path).get_data(as_text=True)
        assert 'class="side-toggle"' in html
        # 首绘前套用收起状态，避免「先展开再收起」的闪动
        assert "localStorage.getItem('sidebar')" in html
        assert html.index("localStorage.getItem('sidebar')") < html.index("<body")

    @pytest.mark.parametrize("path", ["/", "/cost-usage"])
    def test_nav_items_have_titles_for_the_collapsed_rail(self, logged_in, ledger, fake_costs, path):
        html = logged_in.get(path).get_data(as_text=True)
        for title in ('title="概览"', 'title="成本和使用情况"', 'title="退出登录"'):
            assert title in html

    def test_logout_is_an_icon_link_not_a_text_button(self, logged_in, ledger, fake_costs):
        html = logged_in.get("/").get_data(as_text=True)
        assert 'class="nav-item side-exit"' in html
        assert "btn-sm" not in html


class TestOverviewPage:
    def test_renders_all_eight_columns(self, logged_in, ledger, fake_costs):
        html = logged_in.get("/").get_data(as_text=True)
        for header in ("上游", "账号", "预算", "TAG 消费", "UNTAG 消费", "总消费", "使用率", "余额"):
            assert header in html

    def test_keeps_its_query_buttons(self, logged_in, ledger, fake_costs):
        """概览页保留查询/强制刷新，只有下钻页改成了自动提交。"""
        html = logged_in.get("/").get_data(as_text=True)
        assert ">查询</button>" in html
        assert "强制刷新" in html

    def test_missing_ledger_shows_a_message_not_a_500(self, logged_in, tmp_path, monkeypatch):
        monkeypatch.setattr(config, "EXCEL_PATH", tmp_path / "gone.xlsx")
        response = logged_in.get("/")
        assert response.status_code == 200
        assert "找不到" in response.get_data(as_text=True)


class TestCostUsagePage:
    def test_auto_submits_without_query_buttons(self, logged_in, ledger, fake_costs):
        html = logged_in.get("/cost-usage").get_data(as_text=True)
        form = html[html.index('id="usage-filters"') : html.index("</form>")]
        assert "filters-auto" in html
        assert "强制刷新" not in form
        # 只在 noscript 里保留一个兜底提交按钮
        assert form.count("<button") == form.count("<noscript><button") == 1
        assert "addEventListener('change', go)" in html
        assert "is-loading" in html  # 提交时压暗，不闪骨架屏

    @pytest.mark.parametrize(
        "query, expected",
        [
            ("dim=nonsense", "按服务堆叠"),
            ("granularity=hourly", "按日"),
            ("dim=tag", "按标签堆叠"),
            ("dim=account", "按账号堆叠"),
            ("granularity=monthly", "按月"),
            ("account=doesnotexist", "已切回「全部账号」"),
            ("start=abc&end=def", "格式无法识别"),
            ("start=2026-08-17&end=2026-08-01", "自动调换"),
            ("start=2000-01-01", "Cost Explorer 仅保留"),
        ],
    )
    def test_bad_or_varied_params_are_handled(self, logged_in, ledger, fake_costs, query, expected):
        response = logged_in.get(f"/cost-usage?{query}")
        assert response.status_code == 200
        assert expected in response.get_data(as_text=True)

    def test_single_account_selection(self, logged_in, ledger, fake_costs, accounts):
        html = logged_in.get(f"/cost-usage?account={accounts[0].key}").get_data(as_text=True)
        assert accounts[0].partner in html

    def test_long_daily_range_suggests_monthly(self, logged_in, ledger, fake_costs):
        html = logged_in.get(
            "/cost-usage?start=2025-10-01&end=2026-08-17&granularity=daily"
        ).get_data(as_text=True)
        assert "可以把粒度切成" in html

    def test_shows_both_amount_bases(self, logged_in, ledger, fake_costs):
        html = logged_in.get("/cost-usage").get_data(as_text=True)
        assert "加价后合计" in html
        assert "原始 CE 金额" in html

    def test_preset_highlight_survives_a_dimension_change(self, logged_in, ledger, fake_costs):
        """表单不带 preset，高亮靠区间反查，所以换维度后不能掉。"""
        start, end = preset_range("last30", date.today())
        html = logged_in.get(
            f"/cost-usage?start={start}&end={end}&dim=tag"
        ).get_data(as_text=True)
        assert "chip-on" in html

    def test_legend_present_for_multiple_series(self, logged_in, ledger, fake_costs):
        html = logged_in.get("/cost-usage").get_data(as_text=True)
        assert 'class="legend"' in html

    def test_detail_table_is_the_table_view_of_the_chart(self, logged_in, ledger, fake_costs):
        html = logged_in.get("/cost-usage").get_data(as_text=True)
        assert "成本和使用情况明细" in html
        assert "sticky-col" in html


class TestSecurity:
    @pytest.mark.parametrize(
        "path", ["/", "/cost-usage", "/cost-usage?dim=tag", "/cost-usage?dim=account"]
    )
    def test_never_leaks_credentials_or_password(self, logged_in, ledger, fake_costs, path):
        html = logged_in.get(path).get_data(as_text=True)
        for secret in ("AKIAFAKEALPHA0000000", "x" * 40, "y" * 40, TEST_PASSWORD):
            assert secret not in html

    def test_security_headers(self, logged_in, ledger, fake_costs):
        headers = logged_in.get("/").headers
        assert headers["X-Content-Type-Options"] == "nosniff"
        assert headers["X-Frame-Options"] == "DENY"
        assert headers["Referrer-Policy"] == "same-origin"

    def test_no_hardcoded_colors_outside_the_palette(self, logged_in, ledger, fake_costs):
        """图表色板的唯一来源是 chart.py，模板里不许另写一套。"""
        import re

        from bedrock_cost import chart

        html = logged_in.get("/cost-usage").get_data(as_text=True)
        allowed = {
            *chart.SERIES_COLORS,
            chart.OTHER_COLOR,
            chart.SURFACE,
            chart.GRID,
            chart.BASELINE,
            chart.TICK_TEXT,
            chart.LABEL_TEXT,
        }
        assert set(re.findall(r"#[0-9a-fA-F]{6}", html)) <= allowed


class TestCacheClear:
    def test_redirects_back(self, logged_in, ledger, fake_costs):
        response = logged_in.get("/cache/clear")
        assert response.status_code == 302
