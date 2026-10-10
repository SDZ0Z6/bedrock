"""操作日志、登录记录记下「从哪来」：

- 操作日志：网页上的每次改动最后多一列 IP（大家共用一个账号、别的平台一键登录进来的也是它时，光写用户名分不出
  是谁）。命令行、定时任务改的没有请求，不带。设置页上 IP 写在「谁」下面，能按 IP 搜。老日志只有三列，照样读。
  「改了什么」里的换行、制表符换成空格，多行的客户备注不会把一行日志拆开。
- 登录记录：登录表单是从别的网站提交过来的（别的平台一键登录），记下那个网站；本站登录页提交的写「本站」。
"""

from __future__ import annotations

import re

from bedrock_cost import excel_source, settings_pages

from .conftest import LEDGER_ROWS, TEST_PASSWORD, TEST_USER
from .test_accounts import _edit, admin, by_account, post  # noqa: F401  (admin 是 fixture)
from .test_customer_pages import make
from .test_settings import get, logins_file
from .test_web import page_env, scrape, usage_states  # noqa: F401  (autouse)

ALPHA_NO = str(LEDGER_ROWS[0][1])


def audit_raw(ledger) -> list[str]:
    return (ledger.parent / excel_source.AUDIT_NAME).read_text(encoding="utf-8").splitlines()


# ====================================================================== 操作日志
def test_网页上的改动带上IP(admin, ledger):
    _edit(admin, by_account(ALPHA_NO), budget="250000")
    [line] = audit_raw(ledger)
    assert line.split("\t")[-1] == "127.0.0.1" and line.split("\t")[1] == TEST_USER
    html = get(admin, "/settings/audit")
    who = re.search(r'<td class="log-who">(.*?)</td>', html, re.S).group(1)
    assert scrape(who) == f"{TEST_USER} 127.0.0.1 你"                # 测试里你就是 127.0.0.1
    assert "对得上的 1 条" in scrape(get(admin, "/settings/audit?q=127.0.0.1"))


def test_命令行改的不带IP(ledger):
    excel_source.set_lifecycle(f"{ALPHA_NO}#2", ["风控"], actor="cli")
    [line] = audit_raw(ledger)
    assert len(line.split("\t")) == 3 and line.split("\t")[1] == "cli"


def test_老日志照样读(ledger):
    (ledger.parent / excel_source.AUDIT_NAME).write_text(
        "2026-10-01 10:00:00\tadmin\t停用账号 111111111111\n"
        "2026-10-02 10:00:00\tadmin\t修改客户 C001：NOTE 空 → 一\t二\n"            # 以前没换掉的制表符
        "2026-10-03 10:00:00\tadmin\t恢复账号 111111111111\t198.51.100.7\n", encoding="utf-8")
    lines = settings_pages.audit_lines()
    assert [(line.note, line.ip) for line in lines] == [
        ("恢复账号 111111111111", "198.51.100.7"),
        ("修改客户 C001：NOTE 空 → 一\t二", ""),
        ("停用账号 111111111111", ""),
    ]


def test_多行的备注不会把一行日志拆开(admin, ledger):
    make(admin)
    post(admin, "/customers/C001/update", name="星河智能", region="CN", avatar="6", status="on",
         since="2026-06-03", note="第一行\n第二行\tTAB")
    lines = audit_raw(ledger)
    assert all(line.count("\t") <= 3 for line in lines)
    assert any("第一行 第二行 TAB" in line for line in lines)


# ====================================================================== 登录记录
def login(client, **headers):
    return client.post("/login", data={"username": TEST_USER, "password": TEST_PASSWORD}, headers=headers)


def test_别的平台一键登录_记下是哪个网站(client):
    login(client, Origin="https://portal.example.com")
    [entry] = logins_file()
    assert (entry["kind"], entry["source"]) == ("ok", "portal.example.com")
    html = get(client, "/settings/logins")
    row = re.search(r"<tbody>(.*?)</tbody>", html, re.S).group(1)
    assert "portal.example.com 从这个网站提交" in scrape(row)


def test_本站登录页提交的_来源是本站(client):
    login(client, Origin="http://localhost")                   # 测试客户端的 Host 就是 localhost
    login(client.application.test_client(), Referer="http://localhost/login")
    assert [entry["source"] for entry in logins_file()] == ["", ""]


def test_没有Origin就看Referer_密码错的也记(client):
    client.post("/login", data={"username": TEST_USER, "password": "wrong"},
                headers={"Referer": "https://portal.example.com/apps/42"})
    [entry] = logins_file()
    assert (entry["kind"], entry["source"]) == ("fail", "portal.example.com")
