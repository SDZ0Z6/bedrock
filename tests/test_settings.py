"""设置：操作日志（台账的每一次改动）、登录记录（成功、失败、锁定、退出）。

登录记录不记密码；用户名照记，登录失败的也记输进来的是什么（看是谁在试、试的什么名字）。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from bedrock_cost import config, excel_source, login_log

from .conftest import LEDGER_ROWS, TEST_PASSWORD, TEST_USER
from .test_accounts import _edit, admin, by_account, post  # noqa: F401  (admin 是 fixture)
from .test_web import nav_items, page_env, scrape, usage_states  # noqa: F401  (autouse)

ALPHA_NO = str(LEDGER_ROWS[0][1])


def get(client, path: str) -> str:
    response = client.get(path)
    assert response.status_code == 200, (path, response.status_code, response.headers.get("Location"))
    return response.get_data(as_text=True)


def logins_file() -> list[dict]:
    try:
        lines = config.LOGIN_EVENTS_PATH.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []
    return [json.loads(line) for line in lines]


# ====================================================================== 入口
@pytest.mark.parametrize("path", ["/settings/", "/settings/audit", "/settings/logins"])
def test_要登录(client, path):
    response = client.get(path)
    assert response.status_code == 302 and "/login" in response.headers["Location"]


def test_设置先到操作日志(admin):
    response = admin.get("/settings/")
    assert response.status_code == 302 and response.headers["Location"].endswith("/settings/audit")


def test_侧边栏底下有设置_点进去它是当前页(admin):
    html = get(admin, "/settings/audit")
    assert 'href="/settings/" title="设置"' in html[html.index('<div class="side-foot">'):]
    assert [title for _, title, on in nav_items(html) if on] == ["设置"]


# ====================================================================== 操作日志
def test_还没有操作日志(admin):
    html = get(admin, "/settings/audit")
    assert "还没有操作日志" in scrape(html)


def test_操作日志_新的在前_号码能点(admin):
    target = by_account(ALPHA_NO)
    _edit(admin, target, budget="250000")
    post(admin, "/accounts/toggle", key=target.key, enabled="0")
    html = get(admin, "/settings/audit")
    rows = html[html.index("<tbody>"):html.index("</tbody>")]
    text = scrape(rows)
    assert text.index(f"停用账号 {ALPHA_NO}") < text.index(f"修改账号 {ALPHA_NO}")
    assert f'href="/account/{ALPHA_NO}/timeline"' in rows
    assert TEST_USER in text and "BUDGET" in text
    assert "共 2 条" in scrape(html)


def test_操作日志能搜能筛(admin):
    target = by_account(ALPHA_NO)
    other = by_account(str(LEDGER_ROWS[1][1]))
    _edit(admin, target, budget="250000")
    post(admin, "/accounts/toggle", key=other.key, enabled="0")
    html = get(admin, f"/settings/audit?q={ALPHA_NO}")
    text = scrape(html[html.index("<tbody>"):html.index("</tbody>")])
    assert f"修改账号 {ALPHA_NO}" in text and other.account not in text
    assert "对得上的 1 条" in scrape(html)
    html = get(admin, "/settings/audit?kind=账号")
    assert 'aria-current="true">账号' in html
    assert get(admin, "/settings/audit?kind=告警").count("<tr>") == 0


def test_操作日志的格式不对的行跳过(admin, ledger):
    (ledger.parent / excel_source.AUDIT_NAME).write_text(
        "坏行\n2026-10-09 19:16:37\tadmin\t删除账号 590575330322（甲）\n\t\t\n", encoding="utf-8")
    html = get(admin, "/settings/audit")
    assert "删除账号" in scrape(html) and "坏行" not in scrape(html)


# ====================================================================== 登录记录
def test_登录成功记一行(logged_in):
    [entry] = logins_file()
    assert (entry["kind"], entry["user"]) == ("ok", TEST_USER)
    assert entry["ip"] and "when" in entry


def test_登录失败_记输进来的用户名_不记密码(client):
    client.post("/login", data={"username": TEST_USER, "password": "wrong-password-xyz"})
    client.post("/login", data={"username": "  someone   else ", "password": TEST_PASSWORD})
    raw = config.LOGIN_EVENTS_PATH.read_text(encoding="utf-8")
    assert [(entry["kind"], entry["user"]) for entry in logins_file()] == [("fail", TEST_USER), ("user", "someone else")]
    assert "wrong-password-xyz" not in raw and TEST_PASSWORD not in raw


def test_用户名太长_截短(client):
    client.post("/login", data={"username": "x" * 500, "password": "nope"})
    [entry] = logins_file()
    assert entry["user"] == "x" * login_log.MAX_USER


def test_框里输了中文也只是登录失败(client):
    """str 直接定时比较遇到非 ASCII 会抛 TypeError：以前这里是 500。"""
    response = client.post("/login", data={"username": "管理员", "password": "密码"})
    assert response.status_code == 401
    response = client.post("/login", data={"username": TEST_USER, "password": "密码"})
    assert response.status_code == 401
    assert [entry["kind"] for entry in logins_file()] == ["user", "fail"]


def test_令牌是中文也只是过期提示(logged_in):
    response = logged_in.post("/accounts/toggle", data={"csrf": "令牌", "key": "x", "enabled": "0"})
    assert response.status_code == 302


def test_失败太多_锁定也记(client):
    for _ in range(config.MAX_LOGIN_ATTEMPTS + 1):
        client.post("/login", data={"username": TEST_USER, "password": "nope"})
    kinds = [entry["kind"] for entry in logins_file()]
    assert kinds[-1] == "locked" and kinds.count("fail") == config.MAX_LOGIN_ATTEMPTS


def test_退出也记(logged_in):
    logged_in.get("/logout")
    assert [entry["kind"] for entry in logins_file()] == ["ok", "logout"]
    logged_in.get("/logout")      # 已经退出了再点一次：不再记
    assert len(logins_file()) == 2


def test_登录记录页(logged_in, client):
    login_log.record("user", user="guest-typo", ip="198.51.100.7", agent="curl/8.7.1")
    html = get(logged_in, "/settings/logins")
    text = scrape(html)
    assert "近 30 天登录成功 1" in text and "近 30 天登录失败 1" in text
    assert "你现在的 IP" in text and "用户名不对" in text and "curl/8.7.1" in text
    # 登录失败的那一行写着输进来的用户名（红字）
    assert '<td class="log-who tone-danger">guest-typo</td>' in html


def test_浏览器说人话():
    chrome = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
    edge = chrome + " Edg/141.0.0.0"
    safari = ("Mozilla/5.0 (iPhone; CPU iPhone OS 18_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) "
              "Version/18.6 Mobile/15E148 Safari/604.1")
    mac_firefox = "Mozilla/5.0 (Macintosh; Intel Mac OS X 14.6; rv:131.0) Gecko/20100101 Firefox/131.0"
    assert login_log.browser_of(chrome) == "Chrome 141 · Windows"
    assert login_log.browser_of(edge) == "Edge 141 · Windows"
    assert login_log.browser_of(safari) == "Safari 18 · iPhone"
    assert login_log.browser_of(mac_firefox) == "Firefox 131 · macOS"
    assert login_log.browser_of("") == "—"
    assert login_log.browser_of("x" * 60) == "x" * 40 + "…"


def test_只留最近的(tmp_path, monkeypatch):
    monkeypatch.setattr(login_log, "MAX_LINES", 10)
    monkeypatch.setattr(login_log, "KEEP_LINES", 6)
    path = tmp_path / "logins.jsonl"
    start = datetime(2026, 10, 1, tzinfo=timezone.utc)
    for minute in range(11):
        login_log.record("ok", user=TEST_USER, ip=f"192.0.2.{minute}", when=start + timedelta(minutes=minute), path=path)
    entries = login_log.recent(path=path)
    assert [entry.ip for entry in entries] == [f"192.0.2.{minute}" for minute in range(10, 4, -1)]


def test_坏行跳过_文件没有是空的(tmp_path):
    path = tmp_path / "logins.jsonl"
    assert login_log.recent(path=path) == []
    login_log.record("ok", user=TEST_USER, ip="192.0.2.1", path=path)
    with path.open("a", encoding="utf-8") as handle:
        handle.write("{坏的\n")
    assert [entry.kind for entry in login_log.recent(path=path)] == ["ok"]


def test_上一行没写完_新的另起一行(tmp_path):
    path = tmp_path / "logins.jsonl"
    path.write_text('{"when": "2026-10-01T00:00:00+00:00", "kind": "o', encoding="utf-8")   # 写到一半断电
    login_log.record("logout", user=TEST_USER, ip="192.0.2.1", path=path)
    assert [entry.kind for entry in login_log.recent(path=path)] == ["logout"]


def test_记不下来_登录照常(logged_in, monkeypatch, tmp_path):
    """登录记录的文件写不了（目录是个文件、磁盘满了……）：只记一条日志，登录、退出都不受影响。"""
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x", encoding="utf-8")
    monkeypatch.setattr(config, "LOGIN_EVENTS_PATH", blocker / "login-events.jsonl")
    assert logged_in.get("/logout").status_code == 302
    response = logged_in.post("/login", data={"username": TEST_USER, "password": TEST_PASSWORD})
    assert response.status_code == 302
