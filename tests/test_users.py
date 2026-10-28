"""用户：管理员（.env 里的那一个）和只读用户（管理员在「用户管理」里建的），每个人自己的资料页，在线时间。

  · 只读用户能看概览、客户、账号的六个页签，能导出 CSV；改不了任何东西：页面上没有改东西的按钮，直接发请求也挡掉
    （auth._gate，所有不是 GET 的路由都过一遍，只放过改自己的密码、改自己的资料、在线时间的报到）；
  · 看不到账号管理、用户管理和设置，不能「刷新数据」、清空缓存；
  · 新建、重置密码时密码可以自己填，不填由系统生成，都只显示一次；重置密码、停用、删除以后登录着的马上失效；
  · 每个人在资料页（/me）改显示名和头像（一个字或表情 + 底色），只读用户在那里改密码；看自己的在线时间和登录记录，
    管理员在用户管理里点开别人也能看；
  · 在线时间：页面开着就算（每 30 秒报一次到，离上次不超过 GAP 秒才算），几个标签不重复算，报到不续登录有效期；
  · 概览顶上：欢迎回来、今天在线多久、一小片像素风景上走来走去的小家伙（各有脾气，说的话看场合）；九种背景按时间、
    季节自动挑，也能自己挑（记在资料里），小家伙跟着背景换台词、小道具、动作；
  · 管用户、改密码都记进操作日志（「用户」类），密码哪儿都不记。
"""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timedelta

import pytest

from bedrock_cost import avatars, config, excel_source, login_log, presence, users, welcome

from .conftest import LEDGER_ROWS, TEST_PASSWORD, TEST_USER
from .test_account_pages import fake_estimate  # noqa: F401  (fixture)
from .test_accounts import CSRF_PATTERN
from .test_web import book, nav_items, page_env, scrape, toasts, usage_states  # noqa: F401  (fixture / autouse)

ALPHA = str(LEDGER_ROWS[0][1])
FETCH = {"X-Requested-With": "fetch"}
PASSWORD_SHAPE = re.compile(r"[a-z2-9]{4}(?:-[a-z2-9]{4}){3}")
LOCAL = datetime.now().astimezone().tzinfo


# ====================================================================== 共用的料
def token(client, path: str = "/me") -> str:
    found = CSRF_PATTERN.search(client.get(path).get_data(as_text=True))
    assert found, f"{path} 上没有 CSRF 令牌"
    return found.group(1)


def manage(admin, what: str, who: str = "", headers=None, **fields):
    """管理员在用户管理里的动作：create / reset（fetch，回 JSON）、toggle / delete（表单）。who 是对哪个用户。"""
    path = "/users/" if what == "create" else f"/users/{who}/{what}"
    return admin.post(path, data={"csrf": token(admin, "/users/"), **fields}, headers=headers or {})


def create(admin, name: str = "lily", note: str = "销售 Lily", **fields) -> str:
    """新建一个只读用户，返回密码（系统生成的，或者 fields 里自己填的）。"""
    response = manage(admin, "create", headers=FETCH, name=name, note=note, **fields)
    data = response.get_json()
    assert response.status_code == 200 and data["ok"], data
    return data["password"]


def login(app, name: str, password: str):
    client = app.test_client()
    response = client.post("/login", data={"username": name, "password": password})
    return client, response


def audit_lines() -> list[str]:
    try:
        return (config.EXCEL_PATH.parent / excel_source.AUDIT_NAME).read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []


def at(hour: int, minute: int = 0, second: int = 0, day: int = 12) -> datetime:
    return datetime(2026, 10, day, hour, minute, second, tzinfo=LOCAL)


@pytest.fixture
def admin(logged_in, book, fake_costs):
    """管理员登录着，台账是 test_web 的 book（alpha、beta 启用，gamma 停用）。"""
    return logged_in


@pytest.fixture
def lily(app, admin):
    """管理员建的只读用户 lily，登录着的 client。"""
    client, response = login(app, "lily", create(admin))
    assert response.status_code == 302
    return client


@pytest.fixture
def everything(fake_cloudwatch, fake_service_quotas, fake_estimate):
    """账号页六个页签都能打开。"""


# ====================================================================== 存用户的文件
def test_生成的密码_4组4个字_不存明文(admin):
    password = create(admin)
    assert PASSWORD_SHAPE.fullmatch(password) and not set(password) & set("01ilo")
    saved = config.USERS_PATH.read_text(encoding="utf-8")
    assert password not in saved                                   # 只存哈希
    user = users.find("lily")
    assert user.note == "销售 Lily" and not user.disabled and user.stamp
    assert users.authenticate("lily", password) == ("ok", user)
    if sys.platform != "win32":
        assert oct(os.stat(config.USERS_PATH).st_mode & 0o777) == oct(users.FILE_MODE)


@pytest.mark.parametrize("name, why", [
    ("", "没填"), ("ab", "3 到 32"), ("有中文", "3 到 32"), ("has space", "3 到 32"), (".dot", "3 到 32"),
    ("x" * 33, "3 到 32"), (TEST_USER, "管理员"), (TEST_USER.upper(), "管理员"),
])
def test_用户名的规矩(app, name, why):
    with pytest.raises(users.UserError, match=why):
        users.create(name)


def test_用户名不分大小写_不能重名_登录记的是建的时候的写法(app, admin):
    password = create(admin, "Lily.W")
    with pytest.raises(users.UserError, match="已经有"):
        users.create("lily.w")
    client, response = login(app, "LILY.W", password)
    assert response.status_code == 302
    with client.session_transaction() as session:
        assert session["user"] == "Lily.W" and session["role"] == "viewer"
    assert login_log.recent()[0].user == "Lily.W"


def test_备注收成一行_截到60个字():
    user, _ = users.create("lily", "  第一行\n第二行  " + "长" * 80)
    assert user.note.startswith("第一行 第二行 ") and len(user.note) == users.MAX_NOTE


def test_文件坏了_不改_也不冲掉():
    config.USERS_PATH.write_text("{坏了", encoding="utf-8")
    assert users.load() == []                                      # 只读用户登不进来，管理员照常
    with pytest.raises(users.UserError, match="读不了"):
        users.create("lily")
    assert config.USERS_PATH.read_text(encoding="utf-8") == "{坏了"


@pytest.mark.parametrize("current, new, again, error", [
    ("", "new-password-1", "new-password-1", "现在的密码"),
    ("RIGHT", "new-password-1", "new-password-2", "两次"),
    ("RIGHT", "short", "short", "至少 8 位"),
    ("RIGHT", "LILY.LEE", "LILY.LEE", "用户名"),
    ("RIGHT", "RIGHT", "RIGHT", "一样"),
    ("wrong-password", "new-password-1", "new-password-1", "不对"),
])
def test_改密码的规矩(current, new, again, error):
    user, password = users.create("lily.lee")
    fill = lambda value: password if value == "RIGHT" else value  # noqa: E731
    with pytest.raises(users.UserError, match=error):
        users.change_password("lily.lee", fill(current), fill(new), fill(again))
    assert users.find("lily.lee") == user                          # 什么都没改


@pytest.mark.parametrize("password, error", [("", "至少 8 位"), ("short", "至少 8 位"), ("LILY-chan", None),
                                             ("x" * 129, "最多 128 位")])
def test_自己填的密码也有规矩(password, error):
    if error:
        with pytest.raises(users.UserError, match=error):
            users.create("lily", password=password)
        assert users.load() == []
    else:
        user, used = users.create("lily", password=password)
        assert used == password and users.authenticate("lily", password)[0] == "ok"
    with pytest.raises(users.UserError, match="用户名一样"):
        users.create("lily-chan", password="LILY-CHAN")


# ====================================================================== 头像、显示名
@pytest.mark.parametrize("label, chosen, color, text, emoji", [
    ("lily", "", None, "L", False),
    ("小王", "", None, "小", False),
    ("lily", "🐱", 3, "🐱", True),
    ("lily", "q", 5, "Q", False),
    ("lily", "👨‍👩‍👧 一家", None, "👨‍👩‍👧", True),
])
def test_人的头像和账号头像一个规矩(label, chosen, color, text, emoji):
    avatar = avatars.person_avatar("lily", label, chosen, color)
    assert (avatar.text, avatar.emoji) == (text, emoji)
    assert avatar.tone == (color if color is not None else avatars.tone_for("lily"))   # 没选底色按用户名，改名字颜色不变


@pytest.mark.parametrize("display, chosen, color, error", [
    ("  小  王  ", "🐱🐶", "3", None), ("长" * 21, "", "", "最多 20 个字"), ("小王", "", "8", "底色"), ("小王", "", "x", "底色"),
])
def test_资料的规矩(display, chosen, color, error):
    if error:
        with pytest.raises(users.UserError, match=error):
            users.clean_look(display, chosen, color)
    else:
        assert users.clean_look(display, chosen, color) == users.Look("小 王", "🐱", 3)


# ====================================================================== 在线时间
def test_在线时间_离上次不超过GAP才算():
    presence.touch("lily", at(9, 0, 0))                     # 今天第一次：从这里开始算
    assert presence.touch("lily", at(9, 0, 30)) == 30
    assert presence.touch("lily", at(9, 1, 30)) == 90       # 后台标签一分钟才报一次，也算
    assert presence.touch("lily", at(9, 10, 0)) == 90       # 隔了八分多钟（关了页面、合上电脑）：那段不算
    assert presence.touch("lily", at(9, 10, 30)) == 120
    assert presence.seconds_on("LILY", at(9).date()) == 120  # 不分大小写


def test_在线时间_几个标签同时开着不重复算():
    presence.touch("lily", at(9, 0, 0))
    for second in range(10, 301, 10):                      # 两个标签每 30 秒各报一次，交错着来
        presence.touch("lily", at(9, second // 60, second % 60))
    assert presence.seconds_on("lily", at(9).date()) == 300


def test_在线时间_跨过半夜只算半夜以后的():
    presence.touch("lily", at(23, 59, 0, day=11))
    presence.touch("lily", at(23, 59, 50, day=11))
    assert presence.touch("lily", at(0, 0, 40)) == 40
    assert [seconds for _, seconds in presence.recent_days("lily", 2, at(0).date())] == [50, 40]


def test_在线时间只留最近几天_删用户时一起删():
    presence.touch("lily", at(9) - timedelta(days=60))
    presence.touch("lily", at(9))
    data = json.loads(config.PRESENCE_PATH.read_text(encoding="utf-8"))
    assert list(data["lily"]["days"]) == [at(9).date().isoformat()]
    presence.forget("lily")
    assert "lily" not in json.loads(config.PRESENCE_PATH.read_text(encoding="utf-8"))


@pytest.mark.parametrize("seconds, text", [(0, "不到 1 分钟"), (59, "不到 1 分钟"), (60, "1 分钟"), (3599, "59 分钟"),
                                           (3600, "1 小时"), (7980, "2 小时 13 分")])
def test_在线时间说人话(seconds, text):
    assert presence.label(seconds) == text


def test_报到回今天多久_不续登录有效期(admin):
    page = admin.get("/")
    assert page.headers.getlist("Set-Cookie")                # 平时每个请求都续一次
    response = admin.post("/presence", data={"csrf": token(admin)}, headers=FETCH)
    assert response.status_code == 200 and response.get_json()["ok"]
    assert response.get_json()["label"] == "不到 1 分钟"
    assert response.headers.getlist("Set-Cookie") == []       # 报到不续：页面开着不动，到点照样要重新登录


def test_报到令牌不对只回403_不弹表单过期(admin):
    response = admin.post("/presence", data={}, headers=FETCH)
    assert response.status_code == 403 and response.get_json() == {"ok": False}
    assert toasts(admin.get("/").get_data(as_text=True)) == []


def test_每个页面都会报到(admin):
    html = admin.get("/customers/").get_data(as_text=True)
    box = json.loads(re.search(r'<script id="presence" type="application/json">(.*?)</script>', html).group(1))
    assert box["url"] == "/presence" and box["every"] == presence.PING_SECONDS and box["csrf"]


# ====================================================================== 用户管理（管理员）
def test_侧边栏有用户管理_设置回到两个页签(admin):
    html = admin.get("/users/").get_data(as_text=True)
    assert ("/users/", "用户管理", True) in nav_items(html)
    settings = admin.get("/settings/audit").get_data(as_text=True)
    assert re.findall(r'class="acct-tab[^"]*" href="([^"]+)"', settings) == ["/settings/audit", "/settings/logins"]
    assert admin.get("/settings/users").status_code == 404


def test_列表_管理员在第一行(admin):
    html = admin.get("/users/").get_data(as_text=True)
    rows = [scrape(row) for row in re.findall(r"<tr[^>]*>(.*?)</tr>", html, re.S)]
    assert rows[0] == "用户 角色 备注 今天在线 最近登录 操作"
    assert rows[1].startswith(f"{TEST_USER[0].upper()} {TEST_USER} @{TEST_USER} 管理员 你 .env 里的账号")
    assert "还没有只读用户" in scrape(html)


def test_新建用户_密码只在回话里_列表里有_记操作日志(admin):
    password = create(admin)
    html = admin.get("/users/").get_data(as_text=True)
    rows = [scrape(row) for row in re.findall(r"<tr[^>]*>(.*?)</tr>", html, re.S)]
    assert [row for row in rows if "@lily" in row] == ["L lily @lily 只读 销售 Lily 不到 1 分钟 还没登录过"]
    # 最后一列是一排图标（和账号管理一样），悬停看是什么
    for label in ("重置 lily 的密码", "停用 lily", "删除 lily"):
        assert re.search(rf'<(button) class="icon-btn[^"]*"[^>]*aria-label="{label}">\s*<svg', html), label
    assert 'href="/users/lily"' in html and password not in html
    assert audit_lines()[-1].split("\t")[1:3] == [TEST_USER, "新建用户 lily（只读），备注：销售 Lily"]
    assert not [line for line in audit_lines() if password in line]


def test_新建时密码可以自己填(app, admin):
    response = manage(admin, "create", headers=FETCH, name="lily", password_mode="custom", password="lily-picked-9")
    data = response.get_json()
    assert data["ok"] and data["password"] == "lily-picked-9" and data["custom"] and data["profile"] == "/users/lily"
    assert login(app, "lily", "lily-picked-9")[1].status_code == 302
    short = manage(admin, "create", headers=FETCH, name="lucy", password_mode="custom", password="short")
    assert short.status_code == 400 and short.get_json()["message"] == "密码至少 8 位。"
    assert users.find("lucy") is None


def test_新建用户校验不过_回原因(admin):
    create(admin)
    response = manage(admin, "create", headers=FETCH, name="LILY")
    assert response.status_code == 400 and response.get_json() == {"ok": False, "message": "已经有叫 LILY 的用户了。"}


def test_没带令牌不行(admin):
    response = admin.post("/users/", data={"name": "lily"}, headers=FETCH)
    assert response.status_code == 302 and users.load() == []


def test_重置密码_旧的不能用_登录着的退出(app, admin, lily):
    old = users.find("lily")
    data = manage(admin, "reset", "lily", FETCH).get_json()
    assert data["ok"] and data["name"] == "lily" and PASSWORD_SHAPE.fullmatch(data["password"]) and not data["custom"]
    response = lily.get("/")
    assert response.status_code == 302 and "/login" in response.headers["Location"]
    assert "登录已失效" in lily.get("/login").get_data(as_text=True)
    assert users.authenticate("lily", "whatever")[0] == "fail" and users.find("lily").stamp != old.stamp
    assert login(app, "lily", data["password"])[1].status_code == 302
    assert audit_lines()[-1].split("\t")[2] == "重置用户 lily 的密码"


def test_重置时密码也可以自己填(app, admin, lily):
    data = manage(admin, "reset", "lily", FETCH, password_mode="custom", password="brand-new-pass").get_json()
    assert data["password"] == "brand-new-pass" and data["custom"]
    assert login(app, "lily", "brand-new-pass")[1].status_code == 302


def test_停用_登录着的退出_登不进来_恢复以后原来的密码能登(app, admin, lily):
    response = manage(admin, "toggle", "lily", action="disable")
    assert response.status_code == 302 and response.headers["Location"] == "/users/"
    assert users.find("lily").disabled
    assert lily.get("/").status_code == 302
    _, wrong = login(app, "lily", "not-the-password")
    assert wrong.status_code == 401 and "用户名或密码错误" in wrong.get_data(as_text=True)
    html = admin.get("/users/").get_data(as_text=True)
    assert "已停用" in scrape(html) and 'aria-label="恢复 lily"' in html and 'aria-label="停用 lily"' not in html
    manage(admin, "toggle", "lily", action="enable", next="/users/lily")
    assert not users.find("lily").disabled
    assert [line.split("\t")[2] for line in audit_lines()[-2:]] == ["停用用户 lily", "恢复用户 lily"]


def test_在资料页上停用_回资料页(admin, lily):
    response = manage(admin, "toggle", "lily", action="disable", next="/users/lily")
    assert response.headers["Location"] == "/users/lily"
    assert "已停用" in scrape(admin.get("/users/lily").get_data(as_text=True))


def test_停用的用户密码对了才说停用(app, admin):
    password = create(admin)
    manage(admin, "toggle", "lily", action="disable")
    _, response = login(app, "lily", password)
    assert response.status_code == 403 and "这个用户已停用" in response.get_data(as_text=True)
    assert login_log.recent()[0].kind == "disabled" and login_log.recent()[0].failed
    manage(admin, "toggle", "lily", action="enable")
    assert login(app, "lily", password)[1].status_code == 302


def test_删除_登录着的退出_在线时间一起删(app, admin, lily):
    lily.post("/presence", data={"csrf": token(lily)}, headers=FETCH)
    assert "lily" in json.loads(config.PRESENCE_PATH.read_text(encoding="utf-8"))
    manage(admin, "delete", "lily")
    assert users.load() == []
    assert lily.get("/").status_code == 302
    assert "lily" not in json.loads(config.PRESENCE_PATH.read_text(encoding="utf-8"))
    assert audit_lines()[-1].split("\t")[2] == "删除用户 lily"
    assert [toast.title for toast in toasts(admin.get("/users/").get_data(as_text=True))] == ["已删除"]


def test_删掉以后再建同名的_旧会话也不算(app, admin, lily):
    manage(admin, "delete", "lily")
    create(admin)
    assert lily.get("/").status_code == 302                       # stamp 是新的


def test_登录记录标着只读用户_操作日志有用户类(admin, lily):
    logins = admin.get("/settings/logins").get_data(as_text=True)
    assert re.search(r'<td class="log-who ">lily<small>只读用户</small></td>', logins)
    assert "还没登录过" not in scrape(admin.get("/users/").get_data(as_text=True))
    audit = admin.get("/settings/audit?kind=用户").get_data(as_text=True)
    assert "新建用户 lily（只读）" in scrape(audit)


# ====================================================================== 资料页
def test_自己的资料页_改名字和头像(admin):
    html = admin.get("/me").get_data(as_text=True)
    assert "我的资料" in html and "data-look-form" in html
    assert "管理员的密码在服务器的" in scrape(html) and 'action="/password"' not in html
    response = admin.post("/me", data={"csrf": token(admin), "display": "老板", "avatar_text": "🦊", "avatar_color": "2"})
    assert response.status_code == 302 and response.headers["Location"] == "/me"
    saved = json.loads(config.USERS_PATH.read_text(encoding="utf-8"))["admin"]
    assert saved == {"display": "老板", "avatar_text": "🦊", "avatar_color": 2, "scene": "", "pet": "", "city": ""}
    page = admin.get("/").get_data(as_text=True)
    foot = page[page.index('<div class="side-foot">'):page.index("</aside>")]
    assert "🦊 老板 管理员" in scrape(foot) and "av-2 avatar-emoji" in foot
    assert "欢迎回来，<b>老板</b>" in page


def test_资料不对_说哪里不对(admin):
    admin.post("/me", data={"csrf": token(admin), "display": "长" * 21, "avatar_text": "", "avatar_color": ""})
    assert [toast.title for toast in toasts(admin.get("/me").get_data(as_text=True))] == ["资料没保存"]
    assert not config.USERS_PATH.exists()


def test_只读用户的资料页_改资料_改密码_看自己的登录记录(app, admin, lily):
    login(app, "lily", "wrong-guess")                             # 有人拿 lily 的名字试过
    html = lily.get("/me").get_data(as_text=True)
    assert "data-look-form" in html and 'action="/password"' in html and "data-online-today" in html
    section = html[html.index('id="logins-title"'):]
    table = scrape(section[section.index("<table"):section.index("</table>")])
    assert table.count("密码不对") == 1 and table.count("登录成功") == 1 and TEST_USER not in table
    response = lily.post("/me", data={"csrf": token(lily), "display": "Lily 酱", "avatar_text": "", "avatar_color": "5"})
    assert response.status_code == 302 and users.find("lily").look == users.Look("Lily 酱", "", 5)
    assert "欢迎回来，<b>Lily 酱</b>" in lily.get("/").get_data(as_text=True)
    assert "Lily 酱" in scrape(admin.get("/users/").get_data(as_text=True))   # 管理员的列表上也换了


def test_管理员点开别人的资料_只看_能管(admin, lily):
    lily.post("/presence", data={"csrf": token(lily)}, headers=FETCH)
    html = admin.get("/users/lily").get_data(as_text=True)
    assert "data-look-form" not in html and 'action="/password"' not in html and "data-online-today" not in html
    assert 'data-user-reset="/users/lily/reset"' in html and 'action="/users/lily/toggle"' in html
    assert 'data-user-delete="/users/lily/delete"' in html
    assert scrape(html[html.index('<nav class="crumbs"'):html.index("</nav>", html.index('<nav class="crumbs"'))]) == "用户管理 › lily"
    assert "登录成功" in scrape(html[html.index('id="logins-title"'):])
    assert admin.get(f"/users/{TEST_USER}").headers["Location"] == "/me"
    missing = admin.get("/users/nobody")
    assert missing.status_code == 302 and missing.headers["Location"] == "/users/"


# ====================================================================== 概览顶上的欢迎条
def banner_of(html: str) -> str:
    start = html.index('<section class="welcome"')
    return html[start:html.index("</section>", start)]


def talk_of(banner: str) -> dict:
    return json.loads(re.search(r'<script id="mascot-talk" type="application/json">(.*?)</script>', banner, re.S).group(1))


def test_欢迎回来_今天在线多久_小家伙(admin):
    presence.touch(TEST_USER, datetime.now().astimezone() - timedelta(seconds=90))
    presence.touch(TEST_USER)
    html = admin.get("/").get_data(as_text=True)
    banner = banner_of(html)
    assert scrape(banner).startswith(f"{TEST_USER[0].upper()} 欢迎回来， {TEST_USER} 今天在线 1 分钟 ·")
    assert html.index('<section class="welcome"') < html.index("<h1>概览</h1>")     # 在「概览」标题上面
    mascots = re.findall(r'<button class="mascot" type="button" data-mascot data-start="[\d.]+" data-mood="(\w+)"', banner)
    assert mascots == [spec.mood for spec in welcome.MASCOTS] and set(mascots) == set(welcome.MOODS)
    for frame in ("m-legs-a", "m-legs-b", "m-arms-up", "m-eyes m-eyes-side"):
        assert banner.count(f'class="{frame}"') == len(welcome.MASCOTS), frame
    talk = talk_of(banner)
    assert {"人类怎么了？", "人类快去工作！"} <= set(talk["shared"]) and talk["poked"] == list(welcome.POKED)
    assert {mood: len(lines) for mood, lines in talk["moods"].items()} == {mood: len(lines) for mood, lines in welcome.MOODS.items()}
    assert f"{TEST_USER}，欢迎回来！" in talk["context"]
    assert 'src="/static/mascots.js"' in banner


def test_小家伙的话够多_不重样():
    every = list(welcome.SHARED) + [line for lines in welcome.MOODS.values() for line in lines] + list(welcome.POKED)
    assert len(every) >= 80 and len(set(every)) == len(every)
    assert all(len(lines) >= 10 for lines in welcome.MOODS.values())


def test_小家伙说的话看时间_星期几_在线多久():
    person = users.Person(name="lily", admin=False)
    monday, friday, sunday = at(3, day=12), at(9, day=16), at(16, day=18)     # 2026-10-12 是周一
    assert "这么晚了还不睡？人类真奇怪。" in welcome.context_lines(person, 0, monday)
    assert "周一……人类也觉得很难吧？" in welcome.context_lines(person, 0, monday)
    assert {"早上好！咖啡喝了吗？", "周五啦！周末想好去哪了吗？"} <= set(welcome.context_lines(person, 0, friday))
    assert "周日还来看账单，你是真的爱。" in welcome.context_lines(person, 0, sunday)
    assert "今天已经在线 2 小时 30 分 了，起来走两步吧。" in welcome.context_lines(person, 9000, sunday)
    assert "刚来呀？先喝口水。" in welcome.context_lines(person, 30, sunday)
    assert not [line for line in welcome.context_lines(person, 600, sunday) if "起来走两步" in line or "刚来" in line]


def test_小家伙看概览上的数说事():
    person = users.Person(name="lily", admin=False, look=users.Look(display="小王"))
    facts = welcome.Facts(accounts=9, active=5, quiet=3, risky=2, usage_pct=33.3, balance=6_600_000, top="nova",
                          customers=4)
    lines = welcome.context_lines(person, 600, at(16), facts)
    assert {"小王，欢迎回来！", "今天有 5 个账号在干活。", "3 个账号在发呆，要不要去看看？", "2 个账号打着风控……不是我干的。",
            "额度已经用掉 33% 了。", "余额还有 $6.6M，稳住。", "nova 最能花，我看见了。", "4 个客户在用，人类真受欢迎。"} <= set(lines)
    quiet = welcome.context_lines(person, 600, at(16), welcome.Facts())
    assert not [line for line in quiet if "账号" in line or "额度" in line or "余额" in line]


def test_概览上的数是从卡片上拿的(admin):
    html = admin.get("/").get_data(as_text=True)
    context = talk_of(banner_of(html))["context"]
    assert [line for line in context if line.startswith("额度已经用掉")]          # book 里有额度、有消费
    assert [line for line in context if line.endswith("最能花，我看见了。")]


# ---------------------------------------------------------------- 风景
def test_电脑宽屏上欢迎条大一号():
    """一格多画几个像素、风景放大多少都由 CSS 定（--grow 整数，像素画才不糊）；脚本按 --zoom、--pan 走得快一点、挪得多一点。"""
    static = config.BASE_DIR / "src" / "bedrock_cost" / "static"
    css = (static / "style.css").read_text(encoding="utf-8")
    wide = css[css.index("@media (min-width: 1180px) {"):]
    wide = wide[:wide.index("\n}\n")]
    assert ".welcome { --zoom: 1.4; --grow: 2;" in wide and ".mascot-stage { --pan: 76; min-height: 172px; }" in wide
    assert ".welcome-hi { font-size: 32px; }" in wide
    assert ".mascot-sprite { display: block; width: calc(var(--px) * 10px); height: calc(var(--px) * 8px);" in css
    assert ".scene .px { width: calc(var(--w) * var(--c) * var(--zoom, 1) * 1px);" in css
    js = (static / "mascots.js").read_text(encoding="utf-8")
    assert "zoom = parseFloat(css.getPropertyValue('--zoom')) || 1;" in js and "const step = m.speed * zoom * dt * m.dir;" in js


def test_小家伙脚下有风景(admin):
    banner = banner_of(admin.get("/").get_data(as_text=True))
    assert '<div class="scene" data-scene aria-hidden="true">' in banner
    for layer in ("sky", "far", "mid"):
        assert f'data-layer="{layer}"' in banner
    assert banner.count('class="scene-cloud px"') == 3 and banner.count("data-tree") == 4
    assert banner.count("data-tuft") >= 10 and "data-puffs" in banner and "data-weather" in banner
    # 九种背景用的是同一套东西：太阳、月亮、星星、彩虹、水坑都画着，哪样露出来由 CSS 按 data-kind 管
    for part in ("scene-sun", "scene-moon", "scene-star", "scene-rainbow", "scene-puddle"):
        assert f'class="{part}' in banner, part
    assert banner.index('<div class="scene') < banner.index('<button class="mascot"')    # 风景在小家伙后面


# ---------------------------------------------------------------- 背景
def _day(rainy: bool, month: int) -> int:
    """这个月里第一个（不）下雨的日子。"""
    return next(day for day in range(1, 29) if welcome.rainy(at(9, day=day).replace(month=month).date()) == rainy)


@pytest.mark.parametrize("month, hour, rainy, kind", [
    (10, 3, False, "night"), (10, 20, False, "night"), (7, 22, False, "fireflies"), (7, 5, True, "fireflies"),
    (10, 17, True, "dusk"), (1, 18, False, "dusk"),
    (1, 10, False, "snow"), (12, 15, True, "snow"),                     # 冬天下的是雪
    (5, 9, True, "rain"), (5, 15, True, "rainbow"), (3, 10, True, "rain"),
    (3, 10, False, "sakura"), (4, 16, False, "sakura"),
    (9, 10, False, "autumn"), (11, 13, False, "autumn"), (10, 15, True, "rainbow"),
    (6, 12, False, "day"), (8, 16, False, "day"),
])
def test_背景按时间和季节自动挑(month, hour, rainy, kind):
    now = at(hour, day=_day(rainy, month)).replace(month=month)
    assert welcome.auto_scene(now) == kind


def test_下雨的日子大约一周一天_同一天谁看都一样():
    from datetime import date
    days = [date(2026, 1, 1) + timedelta(days=offset) for offset in range(365)]
    count = sum(1 for day in days if welcome.rainy(day))
    assert 30 < count < 80 and welcome.rainy(days[0]) == welcome.rainy(date(2026, 1, 1))


def test_彩虹是一圈一圈的():
    bands = {band for *_, band in welcome.RAINBOW}
    assert bands == set(range(6))
    assert all(0 <= x and x + w <= 36 and 0 <= y < 18 for x, y, w, _ in welcome.RAINBOW)
    top = sorted((x, w, band) for x, y, w, band in welcome.RAINBOW if y == 0)
    assert top and all(band == 0 for *_, band in top)                    # 最上面一行是最外圈（红）


def test_每个背景都有台词_睡着的有梦话():
    assert set(welcome.SCENE_LINES) == set(welcome.SCENES)
    assert all(len(lines) >= 5 for lines in welcome.SCENE_LINES.values()) and len(welcome.SLEEPY) >= 5
    every = [line for lines in welcome.SCENE_LINES.values() for line in lines] + list(welcome.SLEEPY)
    assert len(set(every)) == len(every)


def test_欢迎条带着背景_右上角能换(admin):
    html = admin.get("/").get_data(as_text=True)
    banner = banner_of(html)
    auto = welcome.auto_scene(datetime.now())
    assert f'data-mascot-stage data-kind="{auto}" data-auto="{auto}"' in banner
    picks = re.findall(r'data-scene-pick="(\w*)" aria-pressed="(\w+)"', banner)
    assert picks == [("", "true")] + [(key, "false") for key in welcome.SCENES]     # 没挑过：自动
    assert scrape(banner[banner.index('<button class="scene-pick"'):banner.index('</button>', banner.index('<button class="scene-pick"'))]) \
        == f"{welcome.SCENES[auto]} · 自动"
    conf = json.loads(re.search(r'<script id="scene-conf" type="application/json">(.*?)</script>', banner, re.S).group(1))
    assert conf["url"] == "/me/scene" and conf["csrf"] and conf["labels"] == welcome.SCENES
    talk = talk_of(banner)
    assert talk["scenes"] == {kind: list(lines) for kind, lines in welcome.SCENE_LINES.items()}
    assert talk["sleepy"] == list(welcome.SLEEPY)


def test_挑背景_记在自己的资料里_只读用户也能挑(admin, lily):
    response = lily.post("/me/scene", data={"csrf": token(lily), "scene": "snow"}, headers=FETCH)
    assert response.status_code == 200 and response.get_json() == {"ok": True, "scene": "snow", "kind": "snow", "label": "下雪"}
    assert users.find("lily").look.scene == "snow"
    banner = banner_of(lily.get("/").get_data(as_text=True))
    assert 'data-kind="snow"' in banner and 'data-scene-pick="snow" aria-pressed="true"' in banner
    assert "data-scene-label>下雪</span>" in banner                                   # 自己挑的不写「自动」
    # 改名字、头像不会把挑的背景冲掉
    lily.post("/me", data={"csrf": token(lily), "display": "Lily", "avatar_text": "", "avatar_color": ""})
    assert users.find("lily").look.scene == "snow"
    # 换回自动
    back = lily.post("/me/scene", data={"csrf": token(lily), "scene": ""}, headers=FETCH).get_json()
    assert back["ok"] and back["scene"] == "" and back["kind"] == welcome.auto_scene(datetime.now())
    assert users.find("lily").look.scene == ""
    # 管理员挑的存在 admin 那一项
    admin.post("/me/scene", data={"csrf": token(admin), "scene": "sakura"}, headers=FETCH)
    assert json.loads(config.USERS_PATH.read_text(encoding="utf-8"))["admin"]["scene"] == "sakura"


def test_没有的背景_没带令牌都不行(lily):
    bad = lily.post("/me/scene", data={"csrf": token(lily), "scene": "volcano"}, headers=FETCH)
    assert bad.status_code == 400 and bad.get_json()["message"] == "没有这个背景。"
    assert lily.post("/me/scene", data={"scene": "snow"}, headers=FETCH).status_code == 403
    assert users.find("lily").look.scene == ""


def test_背景的颜色_小道具_天上掉的都在():
    static = config.BASE_DIR / "src" / "bedrock_cost" / "static"
    css = (static / "style.css").read_text(encoding="utf-8")
    for kind in welcome.SCENES:
        if kind != "day":
            assert f'[data-kind="{kind}"]' in css, kind
        assert f".scene-chip.is-{kind}" in css, kind
    for rule in ('.mascot-stage[data-kind="rain"] .prop-umbrella { display: block; }',
                 '.mascot-stage[data-kind="snow"] .mascot-sprite .m-scarf { display: inline; }',
                 '.mascot-stage:is([data-kind="sakura"], [data-kind="autumn"]) .prop-petal { display: block; }',
                 ".mascot.is-sleeping .mascot-zzz { display: block; }",
                 ".mascot.is-slip .mascot-sprite { animation: mascot-slip .7s ease-in-out; }"):
        assert rule in css, rule
    js = (static / "mascots.js").read_text(encoding="utf-8")
    for kind in ("rain", "snow", "sakura", "autumn", "fireflies"):
        assert f"    {kind}: {{ cls: 'wx-" in js, kind
    assert "const SLEEPY_KINDS = ['night', 'fireflies'];" in js
    assert "if (stage.dataset.kind === 'rain' && trees.length && Math.random() < 0.6) {" in js     # 下雨往树底下躲


def test_风景跟着小家伙动_减少动态效果时都不动():
    static = config.BASE_DIR / "src" / "bedrock_cost" / "static"
    css = (static / "style.css").read_text(encoding="utf-8")
    js = (static / "mascots.js").read_text(encoding="utf-8")
    # 视差：三层按镜头各挪一点，越远挪得越多；镜头是几只小家伙站的地方平均一下
    assert "transform: translateX(calc(var(--cam, 0) * var(--pan) * 1px)); }" in css                 # 天
    assert "transform: translateX(calc(var(--cam, 0) * var(--pan) * .4px)); }" in css                # 树
    assert "stage.style.setProperty('--cam', cam.toFixed(4));" in js
    assert "const MID_DEPTH = 0.4;" in js and "return tree.x + cam * pan * MID_DEPTH;" in js and "--pan: 56;" in css
    # 踩草、树晃、扬灰
    assert "tuft.el.classList.toggle('is-pressed', tuft.pressed);" in js and "rustle(tree);" in js and "puff(m);" in js
    assert ("@media (prefers-reduced-motion: reduce) {\n"
            "  .mascot-sprite, .mascot-sprite *, .mascot-bubble, .scene * { animation: none !important; }") in css
    assert "if (still.matches) return;" in js


def test_小家伙不用等人也偶尔说一句():
    """隔半分钟上下挑一只没人理的自己说一句（停下来举手，说三秒）；已经有对话框开着（欢迎条上的、页面底下那只的）、
    页面在后台、欢迎条滚出屏幕时这一回不说。减少动态效果时也说：说话不算动。"""
    js = (config.BASE_DIR / "src" / "bedrock_cost" / "static" / "mascots.js").read_text(encoding="utf-8")
    assert "const CHAT_EVERY = [22000, 45000];" in js and "window.setTimeout(chatter, rand([8000, 16000]));" in js
    assert "document.querySelector('.mascot-bubble:not([hidden]), .pet-bubble:not([hidden])')" in js
    assert "if (document.hidden" in js and "if (box.bottom < 0 || box.top > window.innerHeight) return;" in js
    assert "if (m.held || m.sleeping || m.chatting) continue;" in js                # 说话的时候停下来
    assert js.index("window.setTimeout(chatter") < js.index("if (still.matches) return;")


# ====================================================================== 只读用户：看得到什么
def test_侧边栏_没有账号管理用户管理和设置_底下是自己(lily):
    html = lily.get("/").get_data(as_text=True)
    assert [title for _, title, _ in nav_items(html)] == ["概览", "客户", "账号"]
    foot = html[html.index('<div class="side-foot">'):html.index("</aside>")]
    assert "L lily 只读" in scrape(foot) and 'href="/me"' in foot


@pytest.mark.parametrize("path", ["/accounts/", f"/accounts/?edit={ALPHA}%232", "/settings/", "/settings/audit",
                                  "/settings/logins", "/users/", "/users/lily", "/cache/clear"])
def test_去不了账号管理_用户管理和设置(lily, path):
    response = lily.get(path)
    assert response.status_code == 302 and response.headers["Location"] == "/"
    assert [toast.title for toast in toasts(lily.get("/").get_data(as_text=True))] == ["这一页只有管理员能看"]


@pytest.mark.parametrize("path, target", [
    ("/?refresh=1", "/"),
    (f"/account/{ALPHA}/cost?start=2026-09-01&refresh=1&dim=service", f"/account/{ALPHA}/cost?start=2026-09-01&dim=service"),
    (f"/account/{ALPHA}/usage?tags=a&refresh=1&tags=b", f"/account/{ALPHA}/usage?tags=a&tags=b"),
])
def test_刷新数据拿掉再跳(lily, path, target):
    response = lily.get(path)
    assert response.status_code == 302 and response.headers["Location"] == target


VIEWER_PAGES = ["/", "/customers/", "/customers/C001", f"/account/{ALPHA}/", f"/account/{ALPHA}/cost",
                f"/account/{ALPHA}/usage", f"/account/{ALPHA}/estimate", f"/account/{ALPHA}/timeline",
                f"/account/{ALPHA}/quota"]


@pytest.fixture
def with_customer(admin):
    """客户 C001，alpha 分给了它，记了一笔：客户页、账号页上改东西的按钮都该有（管理员）/ 都该没有（只读用户）。"""
    admin.post("/customers/create", data={"csrf": token(admin, "/customers/"), "name": "星河智能", "region": "CN",
                                          "avatar": "6", "status": "on", "since": "2026-06-03"})
    admin.post("/customers/C001/assign", data={"csrf": token(admin, "/customers/C001"), "account": [f"{ALPHA}#2"]})
    admin.post("/customers/C001/events", data={"csrf": token(admin, "/customers/C001"), "kind": "note", "note": "起量"})
    assert excel_source.load_customers(force=True)


# 改东西的按钮、弹窗和去管理页的链接（认属性，不认字：「调整额度」这些字在对账单的表头、时间线上也有）
EDIT_MARKERS = ['method="post"', 'href="/cache/clear"', "data-refresh", "refresh=1", "data-open-budget", "data-open-restore",
                'data-open="dlg-', "data-act=", 'id="event-date-form"', 'href="/accounts/', 'href="/settings/', 'href="/users/']


@pytest.mark.parametrize("path", VIEWER_PAGES)
def test_页面上没有改东西的按钮(app, admin, with_customer, everything, path):
    seen = admin.get(path).get_data(as_text=True)
    assert [marker for marker in EDIT_MARKERS if marker in seen], "管理员那边本来就该有几个"
    client, _ = login(app, "lily", create(admin))
    response = client.get(path)
    assert response.status_code == 200
    assert [marker for marker in EDIT_MARKERS if marker in response.get_data(as_text=True)] == []


def test_时间线上的卡片只看详情(lily, with_customer):
    for path in (f"/account/{ALPHA}/timeline", "/customers/C001"):
        html = lily.get(path).get_data(as_text=True)
        assert "const canEdit = false;" in html
    assert "点卡片看详情" in scrape(lily.get("/customers/C001").get_data(as_text=True))


def test_名下账号表没有操作那一列(lily, with_customer):
    html = lily.get("/customers/C001").get_data(as_text=True)
    table = html[html.index('<table class="holding-table"'):]
    table = table[:table.index("</table>")]
    assert scrape(table[:table.index("</thead>")]) == "账号 阶段 额度使用 分配"
    assert 'colspan="4"' in table and 'colspan="5"' not in table


def test_能导出CSV(lily, with_customer):
    statement = lily.get("/customers/C001/statement.csv")
    assert statement.status_code == 200 and statement.mimetype == "text/csv"
    cost = lily.get(f"/account/{ALPHA}/cost.csv")
    assert cost.status_code == 200 and cost.mimetype == "text/csv"


# ====================================================================== 只读用户：改不了
VIEWER_OK = ("auth.login", "auth.password", "profile.save", "profile.scene", "profile.pet", "profile.city",
             "profile.presence")


def _post_rules(app) -> list[str]:
    """所有收 POST 的路由（只读用户本来就能提交的那几个除外），参数填上一个能找到的账号 / 客户 / 用户。"""
    paths = []
    for rule in app.url_map.iter_rules():
        if "POST" not in rule.methods or rule.endpoint in VIEWER_OK:
            continue
        values = {name: {"number": ALPHA, "cid": "C001", "name": "lily"}[name] for name in rule.arguments}
        paths.append(rule.build(values)[1])
    return sorted(paths)


def test_所有POST路由都挡_台账和用户一个字节都不变(app, admin, with_customer, lily):
    paths = _post_rules(app)
    assert {"/accounts/create", "/accounts/delete", "/customers/C001/assign", f"/account/{ALPHA}/events",
            "/users/", "/users/lily/reset", "/users/lily/delete"} <= set(paths)
    ledger, people = config.EXCEL_PATH.read_bytes(), config.USERS_PATH.read_bytes()
    form = {"csrf": token(lily), "key": f"{ALPHA}#2", "name": "lily", "action": "disable", "account": f"{ALPHA}#2",
            "kind": "note", "note": "x", "add": "1", "lifecycle": "正常", "password": TEST_PASSWORD}
    for path in paths:
        response = lily.post(path, data=form, headers={"Referer": "http://localhost/customers/C001"})
        assert response.status_code == 302 and response.headers["Location"] == "/customers/C001", path
        fetched = lily.post(path, data=form, headers=FETCH)
        assert fetched.status_code == 403 and fetched.get_json() == {"ok": False, "message": "只读用户不能改东西。"}, path
    assert config.EXCEL_PATH.read_bytes() == ledger and config.USERS_PATH.read_bytes() == people
    assert "只读用户不能改东西" in [toast.title for toast in toasts(lily.get("/").get_data(as_text=True))]


def test_挡下来回本站的那一页_别的站回概览(lily):
    response = lily.post("/customers/create", data={"csrf": token(lily)}, headers={"Referer": "https://evil.example/x"})
    assert response.headers["Location"] == "/"


# ====================================================================== 只读用户：改自己的密码
def change(client, current: str, new: str, again: str | None = None, headers=FETCH):
    return client.post("/password", data={"csrf": token(client), "current": current, "new_password": new,
                                          "again": new if again is None else again, "next": "/me"},
                       headers=headers)


def test_改自己的密码_这个会话接着用_别处登录着的退出(app, admin):
    password = create(admin)
    here, _ = login(app, "lily", password)
    there, _ = login(app, "lily", password)
    response = change(here, password, "my-own-secret-9")
    assert response.status_code == 200 and response.get_json()["ok"]
    assert here.get("/").status_code == 200
    assert there.get("/").status_code == 302
    assert login(app, "lily", password)[1].status_code == 401
    assert login(app, "lily", "my-own-secret-9")[1].status_code == 302
    assert audit_lines()[-1].split("\t")[1:3] == ["lily", "用户 lily 改了自己的密码"]
    assert not [line for line in audit_lines() if "my-own-secret-9" in line or password in line]


def test_改密码出错写在框下面(app, admin):
    password = create(admin)
    client, _ = login(app, "lily", password)
    assert change(client, "wrong-one", "my-own-secret-9").get_json()["message"] == "现在的密码不对。"
    assert change(client, password, "my-own-secret-9", "other-secret-9").get_json()["message"] == "两次输的新密码不一样。"
    assert change(client, password, "short").get_json()["message"] == "密码至少 8 位。"
    assert users.authenticate("lily", password)[0] == "ok"


def test_改密码输错现在的密码算失败次数(app, admin, monkeypatch):
    monkeypatch.setattr(config, "MAX_LOGIN_ATTEMPTS", 3)
    password = create(admin)
    client, _ = login(app, "lily", password)
    for _ in range(3):
        assert change(client, "wrong-one", "my-own-secret-9").status_code == 400
    response = change(client, password, "my-own-secret-9")
    assert response.status_code == 429 and "失败次数过多" in response.get_json()["message"]


def test_没开JS整页提交_回资料页说结果(app, admin):
    password = create(admin)
    client, _ = login(app, "lily", password)
    response = change(client, password, "my-own-secret-9", headers={})
    assert response.status_code == 302 and response.headers["Location"] == "/me"
    assert [toast.title for toast in toasts(client.get("/").get_data(as_text=True))] == ["密码改好了"]


def test_管理员的密码在env里改(admin):
    response = admin.post("/password", data={"csrf": token(admin), "current": TEST_PASSWORD, "new_password": "x" * 12,
                                             "again": "x" * 12}, headers=FETCH)
    assert response.status_code == 400 and response.get_json()["message"] == "admin 的密码在 .env 里改"


# ====================================================================== 会话
def test_加用户之前登录的管理员会话照样能用(client, book, fake_costs):
    with client.session_transaction() as session:
        session["user"] = TEST_USER                               # 那时候会话里只有 user
    assert client.get("/accounts/").status_code == 200


def test_env里换了管理员的名字_旧会话失效(admin, monkeypatch):
    monkeypatch.setattr(config, "AUTH_USERNAME", "someone-else")
    response = admin.get("/")
    assert response.status_code == 302 and "/login" in response.headers["Location"]


def test_只读用户的会话不能冒充管理员(app, admin):
    """会话是签过名的，改不了；这里看的是：名字和管理员一样、角色是只读的会话，照样按只读用户对（还要对 stamp）。"""
    client = app.test_client()
    with client.session_transaction() as session:
        session.update(user=TEST_USER, role="viewer", stamp="x")
    assert client.get("/accounts/").status_code == 302
    assert client.get("/").status_code == 302                       # 没有叫这个名字的只读用户：当没登录
