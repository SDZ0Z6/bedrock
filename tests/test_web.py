"""Web 层：访问控制、参数校验、页面结构、凭证不外泄。

CE 由 fake 替换，所以这一组跑得快也不花钱。
"""

from __future__ import annotations

from datetime import date

from pathlib import Path
from urllib.parse import quote

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
        # 401 而不是 200：Nginx 日志里可识别，fail2ban 才能据此封 IP
        assert response.status_code == 401
        assert client.get("/").status_code == 302

    def test_wrong_username_is_rejected(self, client):
        response = client.post("/login", data={"username": "someoneelse", "password": TEST_PASSWORD})
        assert response.status_code == 401
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
        assert response.status_code == 401

    def test_password_hash_is_accepted(self, app, monkeypatch):
        """上公网时用哈希而不是明文口令，AUTH_PASSWORD 会被忽略。"""
        from werkzeug.security import generate_password_hash

        monkeypatch.setattr(config, "AUTH_PASSWORD", "irrelevant")
        monkeypatch.setattr(config, "AUTH_PASSWORD_HASH", generate_password_hash("s3cret"))
        # 各用一个干净的 client：登录成功会建立会话，复用的话第二次会被
        # 「已登录」分支直接重定向走，测不到口令校验
        assert app.test_client().post(
            "/login", data={"username": TEST_USER, "password": "s3cret"}
        ).status_code == 302
        assert app.test_client().post(
            "/login", data={"username": TEST_USER, "password": "irrelevant"}
        ).status_code == 401


class TestProductionHardening:
    """公网部署要用到的几项，本地默认关着。"""

    def test_secure_cookie_is_off_by_default(self, app):
        assert app.config["SESSION_COOKIE_SECURE"] is False

    def test_secure_cookie_follows_config(self, monkeypatch):
        from bedrock_cost import create_app

        monkeypatch.setattr(config, "SESSION_COOKIE_SECURE", True)
        assert create_app(TESTING=True).config["SESSION_COOKIE_SECURE"] is True

    def test_cookie_flags_always_on(self, app):
        assert app.config["SESSION_COOKIE_HTTPONLY"] is True
        assert app.config["SESSION_COOKIE_SAMESITE"] == "Lax"

    def test_proxy_fix_only_when_trusted(self, monkeypatch):
        from werkzeug.middleware.proxy_fix import ProxyFix

        from bedrock_cost import create_app

        monkeypatch.setattr(config, "TRUST_PROXY", False)
        assert not isinstance(create_app(TESTING=True).wsgi_app, ProxyFix)
        monkeypatch.setattr(config, "TRUST_PROXY", True)
        assert isinstance(create_app(TESTING=True).wsgi_app, ProxyFix)

    def test_forwarded_for_cannot_be_spoofed_to_dodge_lockout(self, monkeypatch):
        """伪造 X-Forwarded-For 不能绕开按 IP 的登录锁定。

        取最右边那个值（Nginx 追加的真实对端），客户端塞在左边的假值无效。
        """
        from bedrock_cost import create_app

        monkeypatch.setattr(config, "TRUST_PROXY", True)
        monkeypatch.setattr(config, "AUTH_USERNAME", TEST_USER)
        monkeypatch.setattr(config, "AUTH_PASSWORD", TEST_PASSWORD)
        monkeypatch.setattr(config, "AUTH_PASSWORD_HASH", "")
        auth.clear_failures()
        client = create_app(TESTING=True, SECRET_KEY="k").test_client()

        codes = []
        for attempt in range(config.MAX_LOGIN_ATTEMPTS + 2):
            codes.append(
                client.post(
                    "/login",
                    data={"username": TEST_USER, "password": "bad"},
                    # 每次换一个伪造来源；真实对端始终是同一个
                    headers={"X-Forwarded-For": f"9.9.9.{attempt}, 203.0.113.7"},
                ).status_code
            )
        assert 429 in codes, "换假 IP 就绕开了锁定"
        auth.clear_failures()

    def test_client_ip_ignores_the_raw_header(self, app):
        with app.test_request_context(headers={"X-Forwarded-For": "1.2.3.4"}):
            assert auth.client_ip() != "1.2.3.4"


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
        # 脚本在 shell.html 里按类统一绑，filters-auto 丢了表单就变哑巴
        assert "querySelectorAll('form.filters-auto')" in html
        assert "form.submit()" in html
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


class TestModelUsagePage:
    def test_renders(self, logged_in, ledger, fake_cloudwatch):
        html = logged_in.get("/model-usage").get_data(as_text=True)
        assert "模型用量" in html
        assert "调用次数" in html

    def test_sidebar_has_three_entries(self, logged_in, ledger, fake_cloudwatch):
        html = logged_in.get("/model-usage").get_data(as_text=True)
        for href in ('href="/"', 'href="/cost-usage"', 'href="/model-usage"'):
            assert href in html

    def test_region_is_not_a_filter_anymore(self, logged_in, ledger, fake_cloudwatch):
        """区域改成 2×2 小倍数图，筛选项已移除。"""
        html = logged_in.get("/model-usage").get_data(as_text=True)
        form = html[html.index('id="metric-filters"') : html.index("</form>")]
        assert 'name="region"' not in form
        assert "可多选" not in form

    def test_four_panels_one_per_region(self, logged_in, ledger, fake_cloudwatch):
        html = logged_in.get("/model-usage").get_data(as_text=True)
        assert html.count('class="panel"') == 4
        for code in ("us-east-1", "us-east-2", "us-west-1", "us-west-2"):
            assert f'data-region="{code}"' in html

    def test_one_shared_legend_not_four(self, logged_in, ledger, fake_cloudwatch):
        """小倍数图必须共用一份图例，否则同一个颜色的含义会各图不同。"""
        html = logged_in.get("/model-usage").get_data(as_text=True)
        assert html.count('<div class="legend">') == 1

    def test_each_panel_shows_its_own_total(self, logged_in, ledger, fake_cloudwatch):
        html = logged_in.get("/model-usage").get_data(as_text=True)
        assert html.count('class="panel-total"') == 4

    def test_table_has_a_column_per_region(self, logged_in, ledger, fake_cloudwatch):
        html = logged_in.get("/model-usage").get_data(as_text=True)
        assert html.count('class="num region-col"') == 4
        assert "四区合计" in html

    def test_crosshairs_are_linked_across_panels(self, logged_in, ledger, fake_cloudwatch):
        html = logged_in.get("/model-usage").get_data(as_text=True)
        assert "moveAll" in html
        assert html.count('class="chart-overlay"') == 4

    @pytest.mark.parametrize(
        "query, expected",
        [
            ("metric=invocations", "调用次数"),
            ("metric=input_tokens", "输入 Token"),
            ("metric=output_tokens", "输出 Token"),
            ("metric=total_tokens", "总 Token"),
            ("tags=all", "全部"),
            ("tags=tagged", "仅有标签"),
            ("tags=untagged", "仅无标签"),
            ("period=1m", "1 分钟"),
            ("period=1d", "1 天"),
            ("win=6h", "近 6 小时"),
            ("metric=nonsense", "调用次数"),
            ("tags=nonsense", "全部"),
            ("period=nonsense", "粒度"),
            ("start=abc&end=def", "无法识别"),
        ],
    )
    def test_params(self, logged_in, ledger, fake_cloudwatch, query, expected):
        response = logged_in.get(f"/model-usage?{query}")
        assert response.status_code == 200
        assert expected in response.get_data(as_text=True)

    def test_long_window_at_fine_granularity_is_coarsened(self, logged_in, ledger, fake_cloudwatch):
        html = logged_in.get("/model-usage?win=30d&period=1m").get_data(as_text=True)
        assert "粒度已自动调整" in html

    def test_auto_submits_without_query_button(self, logged_in, ledger, fake_cloudwatch):
        html = logged_in.get("/model-usage").get_data(as_text=True)
        form = html[html.index('id="metric-filters"') : html.index("</form>")]
        assert "filters-auto" in html
        assert form.count("<button") == form.count("<noscript><button") == 1
        assert "querySelectorAll('form.filters-auto')" in html

    def test_line_chart_not_stacked_bars(self, logged_in, ledger, fake_cloudwatch):
        html = logged_in.get("/model-usage").get_data(as_text=True)
        assert 'class="chart-lines"' in html
        assert 'class="chart-overlay"' in html
        assert 'class="chart-bars"' not in html

    def test_declares_its_data_source_and_no_double_counting(self, logged_in, ledger, fake_cloudwatch):
        html = logged_in.get("/model-usage").get_data(as_text=True)
        assert "AWS/Bedrock" in html
        assert "ContextWindow" in html  # 说明它是子集、未计入

    def test_no_credential_leak(self, logged_in, ledger, fake_cloudwatch):
        html = logged_in.get("/model-usage").get_data(as_text=True)
        for secret in ("AKIAFAKEALPHA0000000", "x" * 40, TEST_PASSWORD):
            assert secret not in html

    def test_missing_ledger_still_lets_you_change_params(self, logged_in, tmp_path, monkeypatch):
        monkeypatch.setattr(config, "EXCEL_PATH", tmp_path / "gone.xlsx")
        response = logged_in.get("/model-usage")
        assert response.status_code == 200
        html = response.get_data(as_text=True)
        assert "找不到" in html
        assert 'id="metric-filters"' in html


class TestCacheClear:
    def test_redirects_back(self, logged_in, ledger, fake_costs):
        response = logged_in.get("/cache/clear")
        assert response.status_code == 302


class TestQuotaSorting:
    """表头点一次升、再一次降、第三次回到服务端排好的默认分组。

    排序在客户端做，所以这里只能守住「料齐不齐」：aria-sort、排序类型、
    以及每个单元格的规范值 data-sort——渲染出来的文字带千分位、还有
    「未列出」「无权限」，直接拿 textContent 排必错。
    """

    def head(self, logged_in):
        html = logged_in.get("/model-quota").get_data(as_text=True)
        return html, html[html.index("<thead>") : html.index("</thead>")]

    def test_every_column_is_sortable(self, logged_in, ledger, fake_service_quotas):
        _html, thead = self.head(logged_in)
        assert thead.count("sortable") == 7          # 七列全带
        assert thead.count('aria-sort="none"') == 7
        assert thead.count('data-sort-type="number"') == 2   # TPM / TPD
        assert thead.count('data-sort-type="text"') == 5

    def test_cells_carry_a_canonical_sort_value(
        self, logged_in, ledger, fake_service_quotas
    ):
        html, _thead = self.head(logged_in)
        body = html[html.index("<tbody>") : html.index("</tbody>")]
        # 数字用未格式化的原值，不是带千分位的显示文本
        assert 'data-sort="30000000"' in body
        assert 'data-sort="43200000000"' in body
        assert 'data-sort="us-east-1"' in body
        assert 'data-sort="111111111111"' in body
        assert 'data-sort="Claude Opus 4.8"' in body

    def test_the_script_is_there(self, logged_in, ledger, fake_service_quotas):
        html, _thead = self.head(logged_in)
        assert "th.sortable" in html
        assert "aria-sort" in html
        assert "cellIndex" in html

    def test_no_account_separator_line(self, logged_in, ledger, fake_service_quotas):
        """账号之间那道重线已移除，只留模型分组的细线。"""
        html, _thead = self.head(logged_in)
        assert "account-start" not in html
        assert "group-start" in html

    def test_account_separator_css_is_gone(self):
        from bedrock_cost import filters

        css = (Path(filters.__file__).parent / "static" / "style.css").read_text(
            encoding="utf-8"
        )
        assert "tr.account-start" not in css
        # 自定义排序时分组线要隐掉，不然线会散落在没意义的位置
        assert ".quota-table.is-sorted tr.group-start td { border-top: none; }" in css


class TestOverviewTagNote:
    """标签值没匹配上时的提示条已从概览页撤掉，但数据层照旧在算。"""

    def test_page_has_no_note_row(self, logged_in, ledger, fake_costs):
        html = logged_in.get("/").get_data(as_text=True)
        assert 'class="row-note"' not in html

    def test_data_layer_still_computes_it(self):
        """只是不渲染，逻辑没删——想恢复只改模板。"""
        import inspect

        from bedrock_cost import cost_explorer
        from bedrock_cost.cost_explorer import CostSplit

        # note 是在 _query 里算的（两个分支都还在）
        source = inspect.getsource(cost_explorer._query)
        assert "未匹配到任何消费" in source
        assert "全部计入 UNTAG" in source
        assert "note" in inspect.signature(CostSplit).parameters


class TestAlertIcons:
    """报错和告警前面要有图标。这件事只能在 CSS 里表达，所以直接查静态文件——
    走 ::before 是为了一处改动覆盖全站 15 处 alert，包括 flash 那种运行时才
    知道类别的。"""

    def css(self):
        from bedrock_cost import filters

        return (Path(filters.__file__).parent / "static" / "style.css").read_text(
            encoding="utf-8"
        )

    def test_error_and_warn_have_icons(self):
        text = self.css()
        assert '.alert-error::before { content: "❌"; }' in text
        assert '.alert-warn::before  { content: "⚠️"; }' in text

    def test_success_stays_plain(self):
        """只有报错和告警要图标，成功消息不加。"""
        assert ".alert-ok::before" not in self.css()

    def test_a_real_warning_renders_with_the_class(
        self, logged_in, ledger, fake_service_quotas
    ):
        """图标靠类名生效，所以告警必须真的带上 alert-warn。"""
        html = logged_in.get("/model-quota?account=nope").get_data(as_text=True)
        assert "alert alert-warn" in html
        assert "已切回全部账号" in html


class TestLoadingOverlay:
    """等待动效。全站服务端同步渲染，动效盖在旧页面上，脚本丢了就只剩白等。"""

    def test_overlay_is_on_every_shell_page(self, logged_in, ledger, fake_costs):
        html = logged_in.get("/").get_data(as_text=True)
        assert 'id="page-loading"' in html
        assert 'class="page-loading-art"' in html
        assert "/static/loading." in html   # 具体是 webm 还是 mp4 由素材决定
        # 三个点：第一个常亮，后两个靠 CSS 动画依次出现
        assert "<i>.</i><i>.</i><i>.</i>" in html

    def test_prefers_the_alpha_webm(self, tmp_path):
        """带 alpha 的只能是 webm，有就优先用；都没有就不渲染 video。"""
        from bedrock_cost.filters import loading_media

        assert loading_media(str(tmp_path)) == ""
        (tmp_path / "loading.mp4").write_bytes(b"x")
        assert loading_media(str(tmp_path)) == "loading.mp4"
        (tmp_path / "loading.webm").write_bytes(b"x")
        assert loading_media(str(tmp_path)) == "loading.webm"

    def test_overlay_sits_outside_the_dimmed_content(self, logged_in, ledger, fake_costs):
        """.content.is-loading 是整块压暗，动效在里面会被一起压暗。"""
        html = logged_in.get("/").get_data(as_text=True)
        assert html.index("</main>") < html.index('id="page-loading"')

    def test_script_covers_links_and_forms(self, logged_in, ledger, fake_costs):
        html = logged_in.get("/").get_data(as_text=True)
        assert "a[href]" in html                                # 站内链接点了就盖
        assert "querySelectorAll('form.filters-auto')" in html  # 筛选改动即提交
        assert "addEventListener('submit'" in html              # POST 表单

    def test_nav_links_carry_the_destination_text(self, logged_in, ledger, fake_costs):
        """文案说的是**目标页**在等什么，所以挂在链接上。"""
        html = logged_in.get("/").get_data(as_text=True)
        assert 'data-loading="正在拉四个区的配额"' in html
        assert 'data-loading="正在拉 CloudWatch 指标"' in html

    def test_each_page_has_its_own_text(self, logged_in, ledger, fake_service_quotas):
        """筛选提交后还留在同一页，用的是本页自己的文案。"""
        html = logged_in.get("/model-quota").get_data(as_text=True)
        assert "正在拉四个区的配额" in html
        assert "首次约 45 秒" in html

    def test_quota_refresh_link_says_it_skips_the_cache(
        self, logged_in, ledger, fake_service_quotas
    ):
        html = logged_in.get("/model-quota").get_data(as_text=True)
        assert 'data-loading="正在重新拉四个区的配额"' in html


class TestModelQuotaPage:
    """配额页：不查 CloudWatch，按应用推理配置的 ARN 逐条列 TPM / TPD。"""

    def test_renders(self, logged_in, ledger, fake_service_quotas):
        html = logged_in.get("/model-quota").get_data(as_text=True)
        assert "模型配额" in html
        for header in ("ARN ID", "模型名称", "模型 ID", "区域", "TPM", "TPD"):
            assert f">{header}</th>" in html

    def test_ratio_column_is_gone(self, logged_in, ledger, fake_service_quotas):
        """「日 ÷ 分」是两条配额自己的比值，跟实际限流无关，已移除。"""
        html = logged_in.get("/model-quota").get_data(as_text=True)
        assert "日 ÷ 分" not in html
        assert "每日 Token 配额" not in html
        assert "每分钟 Token 配额" not in html

    def test_lists_one_row_per_arn(self, logged_in, ledger, fake_service_quotas):
        """默认全部账号：2 账号 × 2 模型 × 4 区 = 16 条 ARN，每条一行。"""
        html = logged_in.get("/model-quota").get_data(as_text=True)
        for acct in ("111111111111", "222222222222"):
            for region in ("us-east-1", "us-east-2", "us-west-1", "us-west-2"):
                for index in (0, 1):
                    assert f"{acct[:2]}{region.replace('-', '')}{index}" in html
        # 完整 ARN 每行都是同一套前缀，重复十六遍没有信息量，不显示
        assert "application-inference-profile/" not in html

    def test_shows_base_model_id_from_the_arn(self, logged_in, ledger, fake_service_quotas):
        html = logged_in.get("/model-quota").get_data(as_text=True)
        assert "anthropic.claude-opus-4-6-v1" in html

    def test_shows_the_model_name_not_the_profile_name(
        self, logged_in, ledger, fake_service_quotas
    ):
        """模型名称一列放配额名，账号自己起的配置名不显示。"""
        html = logged_in.get("/model-quota").get_data(as_text=True)
        assert "Claude Opus 4.6 V1" in html
        assert "claude46Oupsauto_wjc_0529" not in html

    def test_region_column_is_just_the_code(self, logged_in, ledger, fake_service_quotas):
        """区域列只放区域码，中文名去掉。"""
        html = logged_in.get("/model-quota").get_data(as_text=True)
        assert "<code>us-east-1</code>" in html
        for chinese in ("弗吉尼亚", "俄亥俄", "北加州", "俄勒冈"):
            assert chinese not in html

    def test_quotas_show_the_exact_number(self, logged_in, ledger, fake_service_quotas):
        """TPM / TPD 显示完整数目，不是 30.0M 这种压缩写法。"""
        html = logged_in.get("/model-quota").get_data(as_text=True)
        assert "30,000,000" in html
        assert "43,200,000,000" in html
        assert "43.20B" not in html
        # 可调性副行去掉了：日配额永远不可调、分钟配额永远可提额，逐行重复没意义
        assert "可申请提额" not in html
        assert "不可调" not in html

    def test_flags_regions_that_missed_a_quota_increase(
        self, logged_in, ledger, fake_service_quotas
    ):
        """提额按区批：us-east-1 提到了 6M，另外三个区还是 3M，必须标出来。"""
        html = logged_in.get("/model-quota").get_data(as_text=True)
        assert "个模型的配额四区不一致" in html
        assert "6,000,000" in html and "3,000,000" in html
        body = html[html.index("<tbody>") : html.index("</tbody>")]
        lagging = [r for r in body.split("<tr") if "tone-warn" in r]
        assert len(lagging) == 3
        # 只有提过额那个账号的 Opus 4.6 会标，而且 us-east-1 自己不算
        assert all("111111111111" in r and "Claude Opus 4.6 V1" in r for r in lagging)
        assert not any('data-sort="us-east-1"' in r for r in lagging)

    def test_sidebar_has_four_entries(self, logged_in, ledger, fake_service_quotas):
        html = logged_in.get("/model-quota").get_data(as_text=True)
        for href in ('href="/"', 'href="/cost-usage"', 'href="/model-usage"', 'href="/model-quota"'):
            assert href in html

    def test_has_three_filters(self, logged_in, ledger, fake_service_quotas):
        """账号 / 模型名称 / 区域三个筛选，都在同一个自动提交的表单里。"""
        html = logged_in.get("/model-quota").get_data(as_text=True)
        form = html[html.index('id="quota-filters"') : html.index("</form>")]
        for name in ('name="account"', 'name="model"', 'name="region"'):
            assert name in form
        assert "filters-auto" in html   # 类在 <form> 标签上，在切片之前
        # 区域默认全部一起列，差异才比得出来；筛选是想单看某个区时才用
        assert '<option value="" selected>全部区域</option>' in form
        assert ">区域</th>" in html

    def test_region_filter_narrows_the_table(self, logged_in, ledger, fake_service_quotas):
        html = logged_in.get("/model-quota?region=us-east-2").get_data(as_text=True)
        body = html[html.index("<tbody>") : html.index("</tbody>")]
        assert "us-east-2" in body
        assert "us-west-2" not in body
        assert "已筛选" in html

    def test_model_filter_narrows_the_table(self, logged_in, ledger, fake_service_quotas):
        html = logged_in.get("/model-quota?model=Claude+Opus+4.8").get_data(as_text=True)
        body = html[html.index("<tbody>") : html.index("</tbody>")]
        assert "Claude Opus 4.8" in body
        assert "Claude Opus 4.6 V1" not in body
        # 下拉里别的模型还得在，不然筛完就换不回去了
        form = html[html.index('id="quota-filters"') : html.index("</form>")]
        assert "Claude Opus 4.6 V1" in form

    def test_bad_filters_are_dropped_with_a_note(self, logged_in, ledger, fake_service_quotas):
        html = logged_in.get("/model-quota?region=eu-west-9").get_data(as_text=True)
        assert "区域参数无效" in html
        html = logged_in.get("/model-quota?model=Claude+Nonexistent+9").get_data(as_text=True)
        assert "已取消模型筛选" in html

    def test_all_accounts_is_the_default(self, logged_in, ledger, fake_service_quotas):
        """逐行列 ARN、不做跨账号汇总，所以「全部账号」在这一页是安全的。

        早先禁掉它是怕求和/求平均把一个快满的账号藏进平均值里，
        那个顾虑对「一行一条 ARN」的清单不成立。
        """
        html = logged_in.get("/model-quota").get_data(as_text=True)
        form = html[html.index('id="quota-filters"') : html.index("</form>")]
        assert '<option value="all" selected>全部账号</option>' in form
        assert "全部账号（2 个）" in html

    def test_account_column_shows_the_number(self, logged_in, ledger, fake_service_quotas):
        """账号列放 12 位号码；上游名做悬浮提示，不占列宽。"""
        html = logged_in.get("/model-quota").get_data(as_text=True)
        assert ">账号</th>" in html
        assert '<code title="ALPHA">111111111111</code>' in html
        assert '<code title="BETA">222222222222</code>' in html

    def test_single_account_narrows_the_table(self, logged_in, ledger, fake_service_quotas, accounts):
        # key 里的 # 必须编码，否则会被当成 URL 片段、整个参数丢掉
        # （浏览器提交表单时自己会编码，这里是手写 URL）
        key = quote(accounts[0].key, safe='')
        html = logged_in.get(f"/model-quota?account={key}").get_data(as_text=True)
        body = html[html.index("<tbody>") : html.index("</tbody>")]
        assert "111111111111" in body
        assert "222222222222" not in body

    def test_quota_increase_is_judged_per_account(
        self, logged_in, ledger, fake_service_quotas
    ):
        """两个账号的配额池互不相干。ALPHA 的 us-east-1 提到了 6M，BETA 四个区
        都是 3M——不能拿 ALPHA 的 6M 去把 BETA 整片标黄。
        """
        html = logged_in.get("/model-quota").get_data(as_text=True)
        body = html[html.index("<tbody>") : html.index("</tbody>")]
        rows = body.split("<tr")
        alpha_lagging = [r for r in rows if "111111111111" in r and "tone-warn" in r]
        beta_lagging = [r for r in rows if "222222222222" in r and "tone-warn" in r]
        assert len(alpha_lagging) == 3      # us-east-2 / us-west-1 / us-west-2
        assert beta_lagging == []
        assert "1 个模型的配额四区不一致" in html

    def test_defaults_to_first_account(self, logged_in, ledger, fake_service_quotas, accounts):
        html = logged_in.get("/model-quota").get_data(as_text=True)
        assert accounts[0].partner in html

    def test_unknown_account_falls_back(self, logged_in, ledger, fake_service_quotas):
        html = logged_in.get("/model-quota?account=nope").get_data(as_text=True)
        assert "已切回全部账号" in html

    def test_shows_only_claude(self, logged_in, ledger, fake_service_quotas):
        html = logged_in.get("/model-quota").get_data(as_text=True)
        assert "Claude Opus 4.8" in html
        assert "Nova" not in html
        assert "Cohere" not in html

    def test_long_context_shows_up_as_having_no_profile(
        self, logged_in, ledger, fake_service_quotas
    ):
        """1M 上下文是独立配额、没有对应的应用配置，要落在下面那张表里。"""
        html = logged_in.get("/model-quota").get_data(as_text=True)
        assert "1M Context Length" in html
        assert "有配额，但账号下没有应用推理配置" in html
        assert "独立配额" in html

    def test_no_how_to_read_section(self, logged_in, ledger, fake_service_quotas):
        """说明卡整块移除，页面只留表格。"""
        html = logged_in.get("/model-quota").get_data(as_text=True)
        assert "这一页怎么读" not in html

    def test_quota_api_failure_is_shown_not_fatal(self, logged_in, ledger, monkeypatch):
        from bedrock_cost import quotas

        def broken(*a, **k):
            raise RuntimeError("AccessDeniedException: servicequotas denied")

        monkeypatch.setattr(quotas.boto3, "client", broken)
        quotas.clear_cache()
        response = logged_in.get("/model-quota")
        assert response.status_code == 200
        assert "读不到 Service Quotas" in response.get_data(as_text=True)

    def test_degrades_when_profiles_are_denied(
        self, logged_in, ledger, fake_service_quotas, monkeypatch
    ):
        """SCP 拒绝 ListInferenceProfiles 时，配额表照出，ARN 两列留空。"""
        from bedrock_cost import quotas

        monkeypatch.setattr(
            quotas,
            "fetch_app_profiles",
            lambda account, region: ([], "AccessDeniedException: service control policy"),
        )
        quotas.clear_cache()
        html = logged_in.get("/model-quota").get_data(as_text=True)
        assert "Claude Opus 4.8" in html          # 配额本身照常
        assert "无权限" in html                    # ARN 一列
        assert "service control policy" in html   # 原因留给用户

    def test_no_credential_leak(self, logged_in, ledger, fake_service_quotas):
        html = logged_in.get("/model-quota").get_data(as_text=True)
        for secret in ("AKIAFAKEALPHA0000000", "x" * 40, TEST_PASSWORD):
            assert secret not in html

    def test_anonymous_is_blocked(self, client):
        response = client.get("/model-quota")
        assert response.status_code == 302
        assert "/login" in response.headers["Location"]

