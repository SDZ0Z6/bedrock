"""Web 层：访问控制、页面外壳（侧边栏、右上角弹窗、等待动效）、概览页、凭证不外泄。

CE、CloudWatch 和用量状态都由 fake 替换，所以这一组跑得快也不花钱。
账号页的五个页签和老网址的跳转在 test_account_pages.py，运营看板在 test_ops_page.py；
那两个文件也从这里拿共用的 fixture 和解析页面的小工具。

这一组按默认配置跑：运营看板（OPS_DASHBOARD）关着。页脚的措辞、仪表盘 SVG 的内部结构、
卡片里各行的具体排版都还在改，这里只认数和字，不认版式。
"""

from __future__ import annotations

import html as htmllib
import json
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from bedrock_cost import (
    activity,
    auth,
    config,
    cost_estimate,
    cost_explorer,
    dashboard,
    excel_source,
    ops_report,
    pricing,
    usage_explorer,
)
from bedrock_cost.aws_errors import QueryError
from bedrock_cost.cost_explorer import CostSplit

from .conftest import (
    LEDGER_HEADER,
    LEDGER_ROWS,
    RANGE_START,
    TEST_PASSWORD,
    TEST_USER,
    expected_marked,
    write_ledger,
)

STATIC = Path(config.__file__).parent / "static"


# ====================================================================== 共用的料
@pytest.fixture(autouse=True)
def page_env(monkeypatch):
    """页面层的几份模块级缓存换成空的，几个会被开发机 .env 改掉的口径钉死。

    概览的「近 30 天成本」缓存 2 小时、看板的逐日账缓存 2 小时、用量状态缓存 5 分钟，键里
    只有账号和日期：不换的话上一条用例查到的数会串到下一条（换成新 dict，用例结束自动换回）。
    货币符号、额度档位、缓存开关在 .env 里都能改，页面上的字跟着变，这里按默认值钉住。
    """
    monkeypatch.setattr(dashboard, "_trend_cache", {})
    monkeypatch.setattr(ops_report, "_cache", {})
    monkeypatch.setattr(activity, "_cache", {})
    monkeypatch.setattr(config, "CURRENCY_SYMBOL", "$")
    monkeypatch.setattr(config, "WARN_PCT", 70)
    monkeypatch.setattr(config, "DANGER_PCT", 90)
    monkeypatch.setattr(config, "CACHE_TTL", 900)


def usage_state(kind: str = "active", errors=()) -> activity.Activity:
    """一份用量状态：活跃（5 分钟前刚调用过）/ 已中断（3 小时前）/ 无调用 / 用量未知。"""
    now = datetime.now(timezone.utc)
    last = {"active": now - timedelta(minutes=5), "stopped": now - timedelta(hours=3)}.get(kind)
    return activity.Activity(
        kind=kind, last_call=last, daily=[3.0] * activity.SPARK_DAYS, errors=list(errors), checked_at=now
    )


@pytest.fixture(autouse=True)
def usage_states(monkeypatch):
    """用量状态（卡片和账号页页头上的「活跃 / 已中断」）不查 CloudWatch。

    默认都是活跃；用例按号码改：usage_states["222222222222"] = usage_state("stopped")。
    """
    states: dict[str, activity.Activity] = {}

    def fake(account, now=None, refresh=False):
        return states.get(account.account) or usage_state("active")

    monkeypatch.setattr(activity, "account_activity", fake)
    return states


# 台账多三列：账号邮箱、生命周期、启用。alpha（正常）、beta（风控）启用中，gamma 已停用。
BOOK_HEADER = [*LEDGER_HEADER, "EMAIL", "LIFECYCLE", "ENABLED"]
BOOK_ROWS = [
    [*LEDGER_ROWS[0], "alpha@example.com", "正常", True],
    [*LEDGER_ROWS[1], "beta@example.com", "风控", True],
    ["GAMMA", 333333333333, 1000, 1, 1, "AKIAFAKEGAMMA0000000", "z" * 40, "map-migrated=migGAMMA",
     RANGE_START, "gamma@example.com", "", False],
]


def rewrite(path, rows, header=BOOK_HEADER) -> None:
    write_ledger(path, header=header, rows=rows)
    excel_source.clear_cache()


def book_rows(**changes) -> list[list]:
    """BOOK_ROWS 的副本，改几格：book_rows(START_DATE={"222222222222": None})。"""
    rows = [list(row) for row in BOOK_ROWS]
    for column, by_number in changes.items():
        position = BOOK_HEADER.index(column)
        for row in rows:
            if str(row[1]) in by_number:
                row[position] = by_number[str(row[1])]
    return rows


@pytest.fixture
def book(ledger):
    """带邮箱、生命周期、停用账号的台账（还是 conftest 那份临时文件）。"""
    rewrite(ledger, BOOK_ROWS)
    return ledger


def scrape(html: str) -> str:
    """去掉标签、合并空白、反转义，拿来比页面上的字。"""
    return " ".join(htmllib.unescape(re.sub(r"<[^>]+>", " ", html)).split())


def main_part(html: str) -> str:
    """页面正文，去掉页脚（页脚的措辞常改，不拿它断言）。"""
    return re.sub(r'<footer class="meta">.*?</footer>', "", html, flags=re.S)


@dataclass
class Toast:
    tone: str
    title: str
    sub: str
    text: str
    detail: str
    auto: bool


def toasts(html: str) -> list[Toast]:
    """右上角那一摞弹窗（不含给 appToast 克隆用的模板）。"""
    stack = html[html.index('id="toasts"') : html.index("<template data-toast-template")]
    found = []
    for chunk in stack.split('<div class="toast toast-')[1:]:
        def grab(pattern: str) -> str:
            match = re.search(pattern, chunk, re.S)
            return htmllib.unescape(match.group(1).strip()) if match else ""

        found.append(Toast(
            tone=chunk.split('"', 1)[0],
            title=grab(r'class="toast-title">(.*?)</span>'),
            sub=grab(r'class="toast-sub">(.*?)</span>'),
            text=grab(r'class="toast-text">(.*?)</p>'),
            detail=grab(r"<pre>(.*?)</pre>"),
            auto="data-auto" in chunk.split(">", 1)[0],
        ))
    return found


def notes(html: str) -> list[str]:
    """数据口径的提示（<p class="note">），不是报错。"""
    return [scrape(text) for text in re.findall(r'<p class="note[^"]*">(.*?)</p>', html, re.S)]


def ce_failure(number: str, reason: str = "凭证缺少 ce:GetCostAndUsage 权限", **extra) -> CostSplit:
    """一个账号查不到 Cost Explorer：error 是老的一行字，problem 是结构化的原因。"""
    problem = QueryError(
        account=number, reason=reason, code="AccessDeniedException", action="ce:GetCostAndUsage", kind="denied",
        detail=f"An error occurred (AccessDeniedException) when calling the GetCostAndUsage operation: "
               f"User arn:aws:iam::{number}:user/reader is not authorized to perform: ce:GetCostAndUsage",
    )
    return CostSplit(error=f"{reason}（AccessDeniedException）", problem=problem, **extra)


def fail_cumulative(monkeypatch, failures: dict[str, CostSplit]) -> None:
    """概览（和看板的额度）那份累计查询里，这几个账号换成给定的结果，其余照 fake_costs。"""
    original = cost_explorer.fetch_all

    def fetch_all(accounts, ranges, refresh=False):
        splits = original(accounts, ranges, refresh)
        splits.update({a.key: failures[a.account] for a in accounts if a.account in failures})
        return splits

    monkeypatch.setattr(cost_explorer, "fetch_all", fetch_all)


def fail_daily(monkeypatch, numbers: set[str], reason: str = "Cost Explorer 请求过于频繁，请稍后重试") -> None:
    """按天的那条 CE 通路（概览的近 30 天、账号页的成本页签、看板的逐日账）对这几个账号报错。"""
    original = usage_explorer._fetch_account

    def fetch(account, start, end, dimension, granularity, dates, refresh):
        if account.account in numbers:
            error = QueryError(account=account.account, reason=reason, code="ThrottlingException",
                               detail=f"ThrottlingException: Rate exceeded ({account.account})", kind="throttled")
            return {}, "USD", False, error
        return original(account, start, end, dimension, granularity, dates, refresh)

    monkeypatch.setattr(usage_explorer, "_fetch_account", fetch)


@pytest.fixture
def quiet_estimate(monkeypatch):
    """预估页签不查价目表、也不查 CloudWatch：空价目表、没有用量。"""
    table = pricing.PriceTable(prices={}, fetched_at=1.0)
    monkeypatch.setattr(cost_estimate, "load_prices", lambda force=False: table)
    monkeypatch.setattr(cost_estimate, "_fetch_region", lambda account, region, start, end, stamps: ({}, False, None))
    cost_estimate.clear_cache()


# ---------------------------------------------------------------------- 只给这个文件用
@pytest.fixture(autouse=True)
def ops_dashboard_off(monkeypatch):
    """这个文件按默认配置跑：运营看板关着。开发机的 .env 可能打开了它，先钉成关（得在建 app 之前）。"""
    monkeypatch.setattr(config, "OPS_DASHBOARD", False, raising=False)


# ====================================================================== 登录
# 每个入口都要登录。账号页的深链（带号码、带查询参数）也一样；看板在 test_ops_page 里
PROTECTED = [
    "/", "/account/", "/account/switch?account=111111111111",
    "/account/111111111111/", "/account/111111111111/cost", "/account/111111111111/usage",
    "/account/111111111111/quota", "/account/111111111111/estimate",
    "/cost-usage", "/model-usage", "/model-quota", "/cost-estimate", "/cache/clear",
]


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
            data={"username": TEST_USER, "password": TEST_PASSWORD, "next": "/account/111111111111/cost?dim=tag"},
        )
        assert response.headers["Location"] == "/account/111111111111/cost?dim=tag"

    def test_a_deep_link_survives_the_login_round_trip(self, client):
        """收藏的账号页网址：没登录先去登录页，登完回到原来那一页（带着查询参数）。"""
        target = "/account/222222222222/usage?metric=input_tokens&win=24h"
        login = client.get(target).headers["Location"]
        page = client.get(login).get_data(as_text=True)
        next_value = htmllib.unescape(re.search(r'name="next" value="([^"]*)"', page).group(1))
        assert next_value == target
        response = client.post("/login", data={"username": TEST_USER, "password": TEST_PASSWORD, "next": next_value})
        assert response.headers["Location"] == target

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


# ====================================================================== 外壳
NAV_ITEM = re.compile(r'<a class="nav-item (nav-on)?"\s+href="([^"]+)" title="([^"]*)"')
SHELL_PAGES = ["/", "/account/111111111111/", "/account/111111111111/cost"]


def nav_items(html: str) -> list[tuple[str, str, bool]]:
    """侧边栏的入口：(链接, 名字, 是不是当前页)。退出登录不算。"""
    return [(href, title, bool(on)) for on, href, title in NAV_ITEM.findall(html)]


class TestShell:
    @pytest.mark.parametrize(
        "path, current",
        [("/", "概览"), ("/account/111111111111/", "账号"), ("/account/111111111111/cost", "账号")],
    )
    def test_sidebar_entries_and_the_current_one(self, logged_in, ledger, fake_costs, fake_cloudwatch, path, current):
        items = nav_items(logged_in.get(path).get_data(as_text=True))
        # 「账号」进的是最近看的那个账号，所以链接是 /account/ 而不是某个号码
        assert [(href, title) for href, title, _ in items] == [
            ("/", "概览"), ("/account/", "账号"), ("/accounts/", "账号管理"),
        ]
        assert [title for _, title, on in items if on] == [current]

    @pytest.mark.parametrize("path", SHELL_PAGES)
    def test_collapse_control_and_pre_paint_script(self, logged_in, ledger, fake_costs, fake_cloudwatch, path):
        html = logged_in.get(path).get_data(as_text=True)
        assert 'class="side-toggle"' in html
        # 首绘前套用收起状态，避免「先展开再收起」的闪动
        assert "localStorage.getItem('sidebar')" in html
        assert html.index("localStorage.getItem('sidebar')") < html.index("<body")

    def test_nav_items_have_titles_for_the_collapsed_rail(self, logged_in, ledger, fake_costs):
        html = logged_in.get("/").get_data(as_text=True)
        for title in ('title="概览"', 'title="账号"', 'title="账号管理"', 'title="退出登录"'):
            assert title in html

    def test_logout_is_an_icon_link_not_a_text_button(self, logged_in, ledger, fake_costs):
        html = logged_in.get("/").get_data(as_text=True)
        assert 'class="nav-item side-exit"' in html
        assert "btn-sm" not in html

    def test_messages_live_in_the_top_right_stack(self, logged_in, ledger, fake_costs):
        """报错和操作结果都在右上角那一摞里：放在 <main> 外面，加载时不会跟着内容一起压暗；
        页面脚本用 fetch 做完的操作照着模板克隆一条（app.js 的 appToast）。"""
        html = logged_in.get("/").get_data(as_text=True)
        assert html.index("</main>") < html.index('<div class="toast-stack" id="toasts">')
        for tone in ("ok", "info", "warn", "error"):
            assert f'<template data-toast-template="{tone}">' in html
        assert '<script src="/static/app.js"></script>' in html
        script = (STATIC / "app.js").read_text(encoding="utf-8")
        assert "window.appToast = function (tone, title, text, sub)" in script
        assert "textContent = title" in script          # 文字一律 textContent，不拼 HTML

    def test_flash_messages_become_toasts(self, logged_in, ledger, fake_costs):
        """操作结果（绿的、蓝的）几秒后自己走；要注意的、出错的留着等人关。"""
        done = toasts(logged_in.get("/cache/clear", follow_redirects=True).get_data(as_text=True))
        assert [(t.tone, t.auto) for t in done] == [("ok", True)]
        assert done[0].title.startswith("已清空缓存")

        missing = toasts(logged_in.get("/account/999999999999/", follow_redirects=True).get_data(as_text=True))
        assert [(t.tone, t.title, t.auto) for t in missing] == [("warn", "台账里没有账号 999999999999。", False)]


class TestOpsDashboardSwitch:
    """运营看板先下线：OPS_DASHBOARD 默认关，关着时没有这个页面，哪儿也不出现入口。
    打开之后的样子在 test_ops_page.py。"""

    def test_off_by_default_there_is_no_page_and_no_entry(self, logged_in, ledger, fake_costs):
        assert logged_in.get("/ops/").status_code == 404
        html = logged_in.get("/").get_data(as_text=True)
        assert "运营看板" not in [title for _, title, _ in nav_items(html)]
        assert 'href="/ops/' not in html                  # 概览「近 30 天成本」旁边的入口也不出现


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

    def test_submit_listener_lets_page_scripts_cancel_first(self, logged_in, ledger, fake_costs):
        """表单提交挂在冒泡阶段、跳过 defaultPrevented：页面脚本拦下来改用 fetch（或者
        「点两下才删」的第一下）时不能盖上遮罩，否则页面就卡在「加载中」。"""
        html = logged_in.get("/").get_data(as_text=True)
        listener = re.search(r"document\.addEventListener\('submit', function \(event\) \{([^{}]*)\}(\s*,\s*true)?\);", html)
        assert listener, "提交的监听不见了"
        assert "if (event.defaultPrevented) return;" in listener.group(1)
        assert listener.group(2) is None                # 没有第三个参数 true：不是捕获阶段

    def test_custom_range_fields_wait_for_the_apply_button(self, logged_in, ledger, fake_costs):
        """时间下拉里的自定义起止要两个都填完、点「应用」才提交，不能改一个就交。"""
        html = logged_in.get("/").get_data(as_text=True)
        assert "if (field.closest('[data-range-picker]')) continue;" in html

    def test_nav_links_carry_the_destination_text(self, logged_in, ledger, fake_costs):
        """文案说的是**目标页**在等什么，所以挂在链接上。"""
        html = logged_in.get("/").get_data(as_text=True)
        for href, text in (("/", "正在查 Cost Explorer"), ("/account/", "正在读这个账号"), ("/accounts/", "正在读台账")):
            item = re.search(rf'href="{re.escape(href)}" title="[^"]*"\s+data-loading="([^"]*)"', html)
            assert item and item.group(1) == text

    def test_each_page_has_its_own_text(self, logged_in, ledger, fake_service_quotas):
        """筛选提交后还留在同一页，用的是本页自己的文案。"""
        html = logged_in.get("/account/111111111111/quota").get_data(as_text=True)
        overlay = html[html.index('id="page-loading"') :]
        assert "正在拉四个区的配额" in overlay
        assert "首次约 45 秒" in overlay

    def test_quota_refresh_link_says_it_skips_the_cache(self, logged_in, ledger, fake_service_quotas):
        html = logged_in.get("/account/111111111111/quota").get_data(as_text=True)
        assert 'data-loading="正在重新拉四个区的配额"' in html


# ====================================================================== 概览
def cards(html: str) -> dict[str, str]:
    """概览上的账号卡片：号码 -> 这张卡片的 HTML，按页面上的先后。号码从卡片唯一的链接上取。"""
    found = {}
    for card in re.findall(r'<article class="acct-card".*?</article>', html, re.S):
        found[re.search(r'href="/account/(\d+)/"', card).group(1)] = card
    return found


def accounts_by_number() -> dict:
    return {a.account: a for a in excel_source.load_accounts(include_disabled=True)}


def spent_so_far(number: str) -> float:
    """fake_costs 下这个账号从启用日期累计到今天的折算后消费。"""
    return expected_marked(accounts_by_number()[number], RANGE_START, date.today())


class TestOverviewPage:
    def test_three_charts_on_top(self, logged_in, book, fake_costs):
        """额度仪表、近 30 天每天的成本（按账号堆叠）、消费构成的气泡。"""
        html = logged_in.get("/").get_data(as_text=True)
        bento = html[html.index('<section class="bento"') : html.index('<div class="overview-bar">')]
        assert [scrape(t) for t in re.findall(r'<h2 class="viz-title">(.*?)</h2>', bento)] == [
            "额度使用", "近 30 天成本", "消费构成",
        ]
        assert 'class="chart-svg bar-chart"' in bento
        assert 'class="bubble-svg"' in bento

    def test_cost_trend_stacks_each_account_per_day(self, logged_in, book, fake_costs):
        """每天一根柱，按账号分色。悬浮提示的数据藏在页面里，app.js 按 data-tip-src 找到它。"""
        html = logged_in.get("/").get_data(as_text=True)
        assert 'id="trend-chart" data-tip-src="trend-data"' in html
        raw = re.search(r'<script id="trend-data" type="application/json">(.*?)</script>', html, re.S)
        buckets = json.loads(raw.group(1))
        assert len(buckets) == 30
        today = date.today()
        assert buckets[-1]["label"] == today.strftime("%m-%d")
        assert buckets[0]["label"] == (today - timedelta(days=29)).strftime("%m-%d")
        # 一天：alpha 100 × 1 + 50 × 1.05，beta 100 × 1 + 50 × 1.10，都是折算后的
        assert buckets[-1]["total"] == "$307.50"
        assert {row["name"]: row["value"] for row in buckets[-1]["rows"]} == {"alpha": "$152.50", "beta": "$155.00"}
        assert "$9,225" in scrape(html)                    # 30 天合计
        legend = re.findall(r'<button class="legend-toggle"[^>]*>.*?</i>(\w+)</button>', html, re.S)
        assert sorted(legend) == ["alpha", "beta"]

    def test_spend_share_is_each_accounts_cumulative_spend(self, logged_in, book, fake_costs):
        html = logged_in.get("/").get_data(as_text=True)
        share = html[html.index(">消费构成<") : html.index('<div class="overview-bar">')]
        for number, name in (("111111111111", "alpha"), ("222222222222", "beta")):
            assert f'data-name="{name}" data-value="${spent_so_far(number):,.2f}"' in share

    def test_gauge_adds_up_the_cards(self, logged_in, book, fake_costs):
        """仪表上的合计 = 各张卡片的累计消费相加；停用的 gamma 不算。"""
        html = logged_in.get("/").get_data(as_text=True)
        spent = spent_so_far("111111111111") + spent_so_far("222222222222")
        gauge = scrape(html[html.index(">额度使用<") : html.index(">近 30 天成本<")])
        assert "额度 $600,000" in gauge
        assert f"使用率 {spent / 600_000 * 100:.2f}%" in gauge
        assert f"余额 ${600_000 - spent:,.2f}" in gauge
        assert "查询失败 0 个" in gauge

    def test_one_card_per_enabled_account(self, logged_in, book, fake_costs):
        """停用的账号不上概览；每张卡片唯一的链接是「查看详情」，进这个账号的页面。"""
        html = logged_in.get("/").get_data(as_text=True)
        found = cards(html)
        assert set(found) == {"111111111111", "222222222222"}
        for number, card in found.items():
            assert re.findall(r'href="([^"]+)"', card) == [f"/account/{number}/"]
            assert ">查看详情</a>" in card
        assert "gamma" not in scrape(html)

    def test_card_shows_who_and_since_when(self, logged_in, book, fake_costs):
        """头像、邮箱、号码、上游、启用日期、第几天、生命周期、用量状态。只认字，不认排版。"""
        card = cards(logged_in.get("/").get_data(as_text=True))["222222222222"]
        text = scrape(card)
        days = (date.today() - RANGE_START).days + 1
        assert re.search(r'class="avatar[^"]*"[^>]*><span>B</span>', card)       # 邮箱首字母
        for words in ("beta@example.com", "222222222222", "BETA", RANGE_START.isoformat(), "风控", "最近调用 5 分钟前"):
            assert words in text
        assert re.search(rf"(?<!\d){days}(?!\d)", text)                          # 启用第几天
        assert "k-active" in card                                                # 状态点的颜色

    def test_card_shows_usage_and_balance(self, logged_in, book, fake_costs):
        card = cards(logged_in.get("/").get_data(as_text=True))["222222222222"]
        spent = spent_so_far("222222222222")
        usage = spent / 100_000 * 100
        text = scrape(card)
        for words in (f"{usage:.2f}%", f"${100_000 - spent:,.2f}", f"${spent:,.2f}", "$100,000.00"):
            assert words in text

    def test_card_without_an_email_falls_back_to_the_number(self, logged_in, ledger, fake_costs):
        """老台账没有 EMAIL 列：卡片照样出来，认人靠号码，头像用上游的首字母。"""
        card = cards(logged_in.get("/").get_data(as_text=True))["111111111111"]
        assert "111111111111" in scrape(card)
        assert re.search(r'class="avatar[^"]*"[^>]*><span>A</span>', card)

    def test_filter_chips_count_accounts(self, logged_in, book, fake_costs, usage_states):
        """两组筛选签：生命周期（清单里的标签都列，没有账号的是 0）、状态（只列有账号的）。"""
        usage_states["222222222222"] = usage_state("stopped")
        html = logged_in.get("/").get_data(as_text=True)

        def chips(group):
            block = re.search(rf'data-group="{group}">(.*?)</div>', html, re.S).group(1)
            return [(value, scrape(label)) for value, label in
                    re.findall(r'<button class="fchip" type="button" data-value="([^"]*)"[^>]*>(.*?)</button>', block, re.S)]

        assert chips("life") == [("", "全部 2"), ("正常", "正常 1"), ("结算", "结算 0"), ("风控", "风控 1")]
        assert chips("state") == [("", "全部 2"), ("active", "活跃 1"), ("stopped", "已中断 1")]
        usage_states["111111111111"] = usage_state("unknown", errors=["us-east-1：被拒绝"])
        html_error = logged_in.get("/").get_data(as_text=True)
        block = re.search(r'data-group="state">(.*?)</div>', html_error, re.S).group(1)
        assert [(value, scrape(label)) for value, label in re.findall(
            r'<button class="fchip" type="button" data-value="([^"]*)"[^>]*>(.*?)</button>', block, re.S)] == [
            ("", "全部 2"), ("stopped", "已中断 1"), ("error", "异常 1"),
        ]
        assert html.count('aria-pressed="true"') == 2                          # 两组默认都是「全部」
        # 卡片上带着筛选要用的值
        card = cards(html)["222222222222"]
        assert 'data-life="风控"' in card and 'data-state="stopped"' in card

    def test_problems_come_first(self, logged_in, book, fake_costs, usage_states, monkeypatch):
        """默认排序「有问题的排前面」：查询失败 → 用量中断 → 其余按使用率从高到低。"""
        def order():
            return list(cards(logged_in.get("/").get_data(as_text=True)))

        assert order() == ["222222222222", "111111111111"]          # 都正常：beta 用得多
        usage_states["111111111111"] = usage_state("stopped")
        assert order() == ["111111111111", "222222222222"]          # 中断的提前
        fail_cumulative(monkeypatch, {"222222222222": ce_failure("222222222222")})
        assert order() == ["222222222222", "111111111111"]          # 查不到的比中断的还靠前

    def test_there_is_no_sort_menu(self, logged_in, book, fake_costs):
        """排序下拉拿掉了：卡片就按服务端排好的顺序（有问题的在前，见 test_problems_come_first）。"""
        html = logged_in.get("/").get_data(as_text=True)
        assert 'id="acct-sort"' not in html and "overview-sort" not in html
        card = cards(html)["111111111111"]
        for attribute in ("data-risk=", "data-usage=", "data-balance=", "data-order="):
            assert attribute not in card

    def test_has_no_date_filter(self, logged_in, book, fake_costs):
        """额度是一次性发的，拿某个可选区间的消费去比它没有意义。

        所以这一页没有日期筛选：消费和余额都是「从各账号的启用日期累计到今天」。
        """
        html = logged_in.get("/").get_data(as_text=True)
        assert 'name="start"' not in html
        assert 'name="end"' not in html
        assert "data-range-picker>" not in html
        assert "各自从启用日期累计到" in html

    def test_refresh_button_sits_with_the_filters(self, logged_in, book, fake_costs):
        """有报表时「刷新数据」在筛选签的右边，和筛选签一个样子；页头右边留给报错弹窗。"""
        html = logged_in.get("/").get_data(as_text=True)
        bar = html[html.index('<div class="overview-bar">') : html.index('<section class="acct-grid"')]
        button = re.search(r'<a class="refresh-btn" href="/\?refresh=1"[^>]*>(.*?)</a>', bar, re.S)
        assert button and scrape(button.group(1)) == "刷新数据"
        assert '<div class="page-actions"></div>' in html

    def test_old_bookmarks_with_dates_still_work(self, logged_in, book, fake_costs):
        """带着旧书签上的 start/end 进来不该报错，忽略掉就行。"""
        response = logged_in.get("/?start=2026-08-01&end=2026-08-17&preset=mtd")
        assert response.status_code == 200
        assert "累计" in response.get_data(as_text=True)

    def test_flags_accounts_without_a_start_date(self, logged_in, book, fake_costs):
        """没填启用日期只能从 CE 最早可查日兜底，余额会偏高：卡片上标出来，页面上说一句。"""
        rewrite(book, book_rows(START_DATE={"222222222222": None}))
        html = logged_in.get("/").get_data(as_text=True)
        found = cards(html)
        assert "未填" in scrape(found["222222222222"]) and "tone-warn" in found["222222222222"]
        assert "未填" not in scrape(found["111111111111"]) and "tone-warn" not in found["111111111111"]
        assert any(text.startswith("1 个账号的累计区间不完整") for text in notes(html))

    def test_flags_accounts_older_than_ce_retention(self, logged_in, book, fake_costs):
        """启用日期早于保留期时更早的消费查不到，余额偏高，同样要在那张卡片上标出来。"""
        rewrite(book, book_rows(START_DATE={"111111111111": date(2020, 1, 1)}))
        html = logged_in.get("/").get_data(as_text=True)
        assert any(text.startswith("1 个账号的累计区间不完整") for text in notes(html))
        card = cards(html)["111111111111"]
        assert "保留期" in card or "tone-warn" in card

    def test_missing_ledger_shows_a_message_not_a_500(self, logged_in, tmp_path, monkeypatch):
        monkeypatch.setattr(config, "EXCEL_PATH", tmp_path / "gone.xlsx")
        response = logged_in.get("/")
        assert response.status_code == 200
        html = response.get_data(as_text=True)
        assert '<div class="alert alert-error"><div><strong>无法生成报表：</strong>找不到账号台账文件' in html
        # 没有报表就没有筛选条，强制刷新挪到页头
        actions = re.search(r'<div class="page-actions">(.*?)</div>', html, re.S).group(1)
        assert 'href="/?refresh=1"' in actions


class TestOverviewProblems:
    """查不到的数不在页面中间插红条，统一从右上角说；卡片上再标一句。"""

    def test_a_failed_account_is_a_toast_and_marked_on_its_card(self, logged_in, book, fake_costs, monkeypatch):
        fail_cumulative(monkeypatch, {"222222222222": ce_failure("222222222222")})
        html = logged_in.get("/").get_data(as_text=True)

        [toast] = toasts(html)
        assert (toast.tone, toast.title, toast.sub) == ("error", "查不到 Cost Explorer", "beta@example.com · 222222222222")
        assert toast.text == "凭证缺少 ce:GetCostAndUsage 权限。卡片上已标出"
        assert toast.detail.startswith("beta@example.com · 222222222222：An error occurred (AccessDeniedException)")
        assert not toast.auto                                   # 报错留着，等人关

        found = cards(html)
        assert list(found)[0] == "222222222222"                  # 有问题的排前面
        card = found["222222222222"]
        assert 'data-state="error"' in card                     # 消费查不到：状态是异常
        assert re.search(r'<p class="acct-status k-error">.*?异常</p>', card, re.S)
        assert "消费查询失败：凭证缺少 ce:GetCostAndUsage 权限" in scrape(card)
        assert "没有查到消费" in scrape(card)
        assert "查询失败 1 个" in scrape(html)
        assert 'class="alert' not in html                         # 页面中间没有红条

    def test_accounts_failing_for_the_same_reason_share_one_toast(self, logged_in, book, fake_costs, monkeypatch):
        fail_cumulative(monkeypatch, {n: ce_failure(n) for n in ("111111111111", "222222222222")})
        [toast] = toasts(logged_in.get("/").get_data(as_text=True))
        assert (toast.title, toast.sub) == ("2 个账号查不到 Cost Explorer", "")
        assert sorted(line.split("：")[0] for line in toast.detail.splitlines()) == [
            "alpha@example.com · 111111111111", "beta@example.com · 222222222222",
        ]

    def test_unknown_usage_is_a_toast(self, logged_in, book, fake_costs, usage_states):
        denied = [
            QueryError(account="111111111111", region=region, kind="denied", denied_by="scp",
                       reason="被组织的 SCP（服务控制策略）显式拒绝 cloudwatch:ListMetrics",
                       detail=f"AccessDenied in {region}: explicit deny in a service control policy")
            for region in ("us-east-1", "us-west-2")
        ]
        usage_states["111111111111"] = usage_state("unknown", errors=denied)
        html = logged_in.get("/").get_data(as_text=True)

        [toast] = toasts(html)
        assert (toast.title, toast.sub) == ("读不到 CloudWatch", "alpha@example.com · 111111111111")
        assert toast.text == "被组织的 SCP（服务控制策略）显式拒绝 cloudwatch:ListMetrics"
        assert toast.detail.count("explicit deny in a service control policy") == 2
        card = cards(html)["111111111111"]
        # 读不到用量就是账号在报错：状态叫异常，红色，和查不到消费的筛到一起
        assert 'data-state="error"' in card
        assert re.search(r'<p class="acct-status k-error">.*?异常</p>', card, re.S)
        assert "读不到 CloudWatch" in scrape(card)

    def test_daily_cost_failure_is_reported_once(self, logged_in, book, fake_costs, monkeypatch):
        """近 30 天那张图另查一遍 CE。累计查得到、按天查不到：单独说一句；
        两边都查不到的账号只报累计那一条，不重复。"""
        fail_daily(monkeypatch, {"111111111111"})
        [toast] = toasts(logged_in.get("/").get_data(as_text=True))
        assert (toast.title, toast.sub, toast.text) == (
            "查不到每天的成本", "alpha@example.com · 111111111111", "Cost Explorer 请求过于频繁，请稍后重试",
        )

        fail_cumulative(monkeypatch, {"111111111111": ce_failure("111111111111")})
        assert [t.title for t in toasts(logged_in.get("/").get_data(as_text=True))] == ["查不到 Cost Explorer"]


class TestSecurity:
    PAGES = [
        "/", "/account/111111111111/", "/account/111111111111/cost", "/account/111111111111/cost?dim=tag",
        "/account/111111111111/usage", "/account/111111111111/quota", "/account/111111111111/estimate",
        "/account/222222222222/",
    ]

    @pytest.mark.parametrize("path", PAGES)
    def test_never_leaks_credentials_or_password(
        self, logged_in, ledger, fake_costs, fake_cloudwatch, fake_service_quotas, quiet_estimate, path
    ):
        response = logged_in.get(path)
        assert response.status_code == 200
        html = response.get_data(as_text=True)
        for secret in ("AKIAFAKEALPHA0000000", "AKIAFAKEBETA00000000", "x" * 40, "y" * 40, TEST_PASSWORD):
            assert secret not in html

    def test_aws_error_text_is_escaped(self, logged_in, ledger, fake_costs, monkeypatch):
        """弹窗里的「原因」和「查看详情」是 AWS 的原话，原样插进页面就是 XSS。"""
        evil = '<img src=x onerror="alert(1)">'
        failure = ce_failure("111111111111", reason=f"AWS 拒绝了这次请求 {evil}")
        fail_cumulative(monkeypatch, {"111111111111": failure})
        html = logged_in.get("/").get_data(as_text=True)
        assert "<img src=x" not in html
        assert "&lt;img src=x onerror=" in html
        assert evil in toasts(html)[0].text                     # 反转义后还是那句原话

    @pytest.mark.parametrize("path", ["/", "/account/111111111111/"])
    def test_security_headers(self, logged_in, ledger, fake_costs, fake_cloudwatch, path):
        headers = logged_in.get(path).headers
        assert headers["X-Content-Type-Options"] == "nosniff"
        assert headers["X-Frame-Options"] == "DENY"
        assert headers["Referrer-Policy"] == "same-origin"
        assert headers["Cache-Control"] == "no-store"

    def test_no_hardcoded_colors_outside_the_palette(self, logged_in, ledger, fake_costs):
        """图表色板的唯一来源是 chart.py，模板里不许另写一套。看的是成本页签：面积图、图例、
        表格色块都在。生命周期标签的颜色是台账里选的（excel_source），不算图表色。"""
        from bedrock_cost import chart

        html = logged_in.get("/account/111111111111/cost").get_data(as_text=True)
        allowed = {
            *chart.SERIES_COLORS,
            chart.OTHER_COLOR,
            chart.SURFACE,
            chart.GRID,
            chart.BASELINE,
            chart.TICK_TEXT,
            chart.LABEL_TEXT,
            *(hex_ for _, hex_ in excel_source.LIFECYCLE_COLORS.values()),
        }
        assert set(re.findall(r"#[0-9a-fA-F]{6}", html)) <= allowed


class TestCacheClear:
    def test_redirects_back(self, logged_in, ledger, fake_costs):
        response = logged_in.get("/cache/clear", headers={"Referer": "/account/111111111111/cost?dim=tag"})
        assert response.status_code == 302
        assert response.headers["Location"] == "/account/111111111111/cost?dim=tag"
        assert logged_in.get("/cache/clear").headers["Location"] == "/"

    def test_drops_the_overview_trend(self, logged_in, ledger, fake_costs, monkeypatch):
        """「近 30 天成本」单独缓存 2 小时，清空缓存也要清掉它。"""
        assert toasts(logged_in.get("/").get_data(as_text=True)) == []
        fail_daily(monkeypatch, {"111111111111"})
        assert toasts(logged_in.get("/").get_data(as_text=True)) == []          # 还是缓存里那份
        logged_in.get("/cache/clear")
        errors = [t.title for t in toasts(logged_in.get("/").get_data(as_text=True)) if t.tone == "error"]
        assert errors == ["查不到每天的成本"]


class TestOverviewTagNote:
    """标签值没匹配上时的提示条已从概览页撤掉，但数据层照旧在算。"""

    def test_page_has_no_note_row(self, logged_in, ledger, fake_costs):
        html = logged_in.get("/").get_data(as_text=True)
        assert 'class="row-note"' not in html

    def test_data_layer_still_computes_it(self):
        """只是不渲染，逻辑没删——想恢复只改模板。"""
        import inspect

        from bedrock_cost.cost_explorer import CostSplit

        # note 是在 _query 里算的（两个分支都还在）
        source = inspect.getsource(cost_explorer._query)
        assert "未匹配到任何消费" in source
        assert "全部计入 UNTAG" in source
        assert "note" in inspect.signature(CostSplit).parameters


class TestNotices:
    """三种提示：查询失败走右上角弹窗；整页出不来是页面里的 .alert；数据口径是轻的 .note。"""

    def css(self) -> str:
        return (STATIC / "style.css").read_text(encoding="utf-8")

    def test_icons_are_masks_in_the_text_colour_not_emoji(self):
        """一处定义的遮罩图标，跟着字色走：报错红、要注意黄、口径说明灰。"""
        text = self.css()
        assert ".alert::before,\n.note::before {" in text
        assert "mask: var(--icon) center / contain no-repeat;" in text
        for rule in (".alert-error { --icon:", ".alert-warn,\n.note-warn { --icon:", ".alert-ok { --icon:"):
            assert rule in text
        assert ".note {\n  --icon:" in text
        assert "❌" not in text and "⚠️" not in text

    def test_a_page_that_cannot_render_is_an_alert(self, logged_in, tmp_path, monkeypatch):
        monkeypatch.setattr(config, "EXCEL_PATH", tmp_path / "gone.xlsx")
        html = logged_in.get("/").get_data(as_text=True)
        assert '<div class="alert alert-error">' in html
        assert toasts(html) == []

    def test_data_caveats_are_notes(self, logged_in, ledger, fake_costs):
        """「这个数要这么读」不是出错：留在页面上，不弹窗、不用红条。"""
        from .conftest import ledger_without

        header, rows = ledger_without("START_DATE")
        write_ledger(ledger, header=header, rows=rows)
        excel_source.clear_cache()
        html = logged_in.get("/").get_data(as_text=True)
        assert any("个账号的累计区间不完整" in text for text in notes(html))
        assert 'class="alert' not in html
        assert toasts(html) == []
