"""每个页面底下那只像素小家伙（pet.py、static/pet.js、templates/_mascot.html 的 pet()）。

  · 登录后的每个页面都有一只（登录页没有）：一层图层、它这一页能说的话（看是哪一页，叫你的名字）、脚本；
  · 能收起来：右键「收起小家伙」（fetch）或者资料页上的开关（表单），记在自己的资料里（users.json 里这个人的 pet），
    哪个页面都不出来了，连脚本也不加载；资料页上再打开就回来。只读用户也能收自己的；
  · 改名字、头像不会把它的开关冲掉；开还是关不对、没带令牌都不改；
  · 台词：每一页都有自己的几句，墙上、顶上、被拎起来、落地……各有各的，不重样。
"""

from __future__ import annotations

import json
import re

from bedrock_cost import config, pet, users, welcome

from .conftest import TEST_USER
from .test_account_pages import fake_estimate  # noqa: F401  (fixture)
from .test_users import FETCH, admin, lily, token  # noqa: F401  (fixture)
from .test_web import book, page_env, scrape, toasts, usage_states  # noqa: F401  (fixture / autouse)

STATIC = config.BASE_DIR / "src" / "bedrock_cost" / "static"
PAGES = (("/", "overview"), ("/customers/", "customers"), ("/accounts/", "accounts"), ("/users/", "people"),
         ("/settings/audit", "settings"), ("/me", "me"))
LAYER = '<div class="pet-layer" data-pet-layer hidden>'


def conf_of(html: str) -> dict:
    found = re.search(r'<script id="pet-conf" type="application/json">(.*?)</script>', html, re.S)
    assert found, "页面上没有小家伙的配置"
    return json.loads(found.group(1))


def has_pet(html: str) -> bool:
    parts = (LAYER in html, 'id="pet-conf"' in html, 'src="/static/pet.js"' in html)
    assert len(set(parts)) == 1, parts          # 图层、配置、脚本要么都有，要么都没有
    return parts[0]


def pet_card(html: str) -> str:
    start = html.index('aria-labelledby="pet-title"')
    return html[start:html.index("</section>", start)]


def saved_pet(who: str = "admin") -> str:
    data = json.loads(config.USERS_PATH.read_text(encoding="utf-8"))
    look = data["admin"] if who == "admin" else next(u for u in data["users"] if u["name"] == who)
    return look["pet"]


# ====================================================================== 哪些页面有
def test_登录后的页面都有小家伙_说的话看是哪一页(app, admin):
    for path, page in PAGES:
        html = admin.get(path).get_data(as_text=True)
        assert has_pet(html), path
        conf = conf_of(html)
        assert conf["url"] == "/me/pet" and conf["csrf"] and conf["page"] == page, path
        talk = conf["lines"]
        assert talk["page"] == list(pet.PAGE_LINES[page]), path
        assert f"{TEST_USER}，欢迎回来！" in talk["context"]                      # 叫你的名字
        assert talk["poked"] == list(welcome.POKED) and talk["shared"] == list(pet.SHARED)
        for kind in ("climb", "ceiling", "held", "landed", "fall", "dodge", "arrive", "bye"):
            assert talk[kind] == list(getattr(pet, kind.upper())), kind
        # 欢迎条那几只的脾气话每页随机带几句
        assert len(talk["moods"]) == pet.MOOD_SAMPLE
        assert set(talk["moods"]) <= {line for lines in welcome.MOODS.values() for line in lines}
    assert not has_pet(app.test_client().get("/login").get_data(as_text=True))         # 登录页没有


def test_小家伙和欢迎条那几只是同一套格子_右键能收起来(admin):
    html = admin.get("/").get_data(as_text=True)
    layer = html[html.index(LAYER):html.index('<script id="pet-conf"')]
    assert '<button class="mascot pet" type="button" data-pet' in layer and "data-mascot" not in layer
    for frame in ("m-legs-a", "m-legs-b", "m-arms-up", "m-eyes m-eyes-front", "m-eyes m-eyes-side", "m-look"):
        assert layer.count(f'class="{frame}"') == 1, frame
    assert html.count('class="m-look"') == len(welcome.MASCOTS) + 1                  # 欢迎条那几只的眼睛也包了一层
    assert '<p class="pet-bubble" data-pet-bubble hidden></p>' in layer
    assert re.search(r'<div class="pet-menu" data-pet-menu role="menu"[^>]*hidden>', layer)
    assert "收起小家伙" in scrape(layer) and "在「我的资料」里能再打开" in scrape(layer)
    assert html.index('src="/static/app.js"') < html.index('src="/static/pet.js"')    # 收起来时的弹窗用 app.js 的 appToast


# ====================================================================== 收起来、再打开
def test_右键收起来_哪个页面都不出来了_资料页上再打开(admin):
    response = admin.post("/me/pet", data={"csrf": token(admin), "pet": "off"}, headers=FETCH)
    assert response.status_code == 200 and response.get_json() == {"ok": True, "pet": "off"}
    assert saved_pet() == users.PET_OFF
    for path, _ in PAGES:
        assert not has_pet(admin.get(path).get_data(as_text=True)), path
    card = pet_card(admin.get("/me").get_data(as_text=True))
    assert "收起来了" in scrape(card) and 'aria-checked="false"' in card and 'name="pet" value="on"' in card
    # 资料页上的开关：普通表单，存好回资料页
    response = admin.post("/me/pet", data={"csrf": token(admin), "pet": "on"})
    assert response.status_code == 302 and response.headers["Location"] == "/me"
    page = admin.get("/me").get_data(as_text=True)
    assert [toast.title for toast in toasts(page)] == ["小家伙回来了"]
    assert saved_pet() == ""
    card = pet_card(page)
    assert "在页面上" in scrape(card) and 'aria-checked="true"' in card and 'name="pet" value="off"' in card
    assert has_pet(admin.get("/customers/").get_data(as_text=True))
    # 资料页上关掉
    admin.post("/me/pet", data={"csrf": token(admin), "pet": "off"})
    assert [toast.title for toast in toasts(admin.get("/me").get_data(as_text=True))] == ["小家伙收起来了"]
    assert saved_pet() == users.PET_OFF


def test_只读用户也能收起自己的小家伙(admin, lily):
    response = lily.post("/me/pet", data={"csrf": token(lily), "pet": "off"}, headers=FETCH)
    assert response.status_code == 200 and response.get_json()["ok"]
    assert users.find("lily").look.pet == users.PET_OFF and saved_pet("lily") == users.PET_OFF
    assert not has_pet(lily.get("/").get_data(as_text=True))
    assert has_pet(admin.get("/").get_data(as_text=True))                            # 管理员的还在


def test_改名字头像不会把小家伙的开关冲掉(admin):
    admin.post("/me/pet", data={"csrf": token(admin), "pet": "off"}, headers=FETCH)
    admin.post("/me/scene", data={"csrf": token(admin), "scene": "snow"}, headers=FETCH)
    admin.post("/me", data={"csrf": token(admin), "display": "老板", "avatar_text": "", "avatar_color": ""})
    saved = json.loads(config.USERS_PATH.read_text(encoding="utf-8"))["admin"]
    assert saved["display"] == "老板" and saved["scene"] == "snow" and saved["pet"] == users.PET_OFF
    # 换背景也不动它
    admin.post("/me/scene", data={"csrf": token(admin), "scene": ""}, headers=FETCH)
    assert saved_pet() == users.PET_OFF


def test_开还是关不对_没带令牌都不改(admin):
    bad = admin.post("/me/pet", data={"csrf": token(admin), "pet": "maybe"}, headers=FETCH)
    assert bad.status_code == 400 and not bad.get_json()["ok"]
    assert admin.post("/me/pet", data={"pet": "off"}, headers=FETCH).status_code == 403
    form = admin.post("/me/pet", data={"csrf": token(admin), "pet": "maybe"})
    assert form.status_code == 302 and form.headers["Location"] == "/me"
    assert [toast.title for toast in toasts(admin.get("/me").get_data(as_text=True))] == ["没改成"]
    form = admin.post("/me/pet", data={"pet": "off"})
    assert form.status_code == 302 and form.headers["Location"] == "/me"
    assert [toast.title for toast in toasts(admin.get("/me").get_data(as_text=True))] == \
        ["表单已过期（会话可能已重启），请刷新页面后重试。"]
    assert not config.USERS_PATH.exists()                                              # 一个字都没存
    assert has_pet(admin.get("/").get_data(as_text=True))


def test_资料里的开关只认off():
    assert users._look({"pet": "off"}).pet == users.PET_OFF
    for raw in ("on", "", 1, None, "OFF"):
        assert users._look({"pet": raw}).pet == "", raw
    assert users.Look().pet_on and not users.Look(pet=users.PET_OFF).pet_on
    assert users._look_json(users.Look(pet=users.PET_OFF))["pet"] == users.PET_OFF


# ====================================================================== 台词
def test_每一页都有自己的台词_各种情形都有_不重样():
    # 代码里出现过的 active_page（模板里 shell.html 按它挑这一页的话）都有台词
    source = "\n".join(path.read_text(encoding="utf-8") for path in (config.BASE_DIR / "src" / "bedrock_cost").glob("*.py"))
    pages = set(re.findall(r'active_page"?\s*[=:]\s*"(\w+)"', source)) | {"me", "people"}
    assert pages and pages <= set(pet.PAGE_LINES), pages - set(pet.PAGE_LINES)
    assert all(len(lines) >= 5 for lines in pet.PAGE_LINES.values())
    pools = (pet.SHARED, pet.CLIMB, pet.CEILING, pet.HELD, pet.LANDED, pet.FALL, pet.DODGE, pet.ARRIVE, pet.BYE)
    assert all(len(pool) >= 2 for pool in pools) and len(pet.SHARED) >= 10
    every = [line for lines in pet.PAGE_LINES.values() for line in lines] + [line for pool in pools for line in pool]
    assert len(set(every)) == len(every)
    person = users.Person(name="lily", admin=False)
    talk = pet.lines(person, "nowhere")
    assert talk["page"] == [] and "lily，欢迎回来！" in talk["context"]                  # 不认识的页面只是不说这一页的事


def test_小家伙的脚本和样式():
    js = (STATIC / "pet.js").read_text(encoding="utf-8")
    for piece in ("const LOOP = ['floor', 'right', 'ceiling', 'left'];",         # 逆时针一圈四条边
                  "sessionStorage.setItem(KEY, JSON.stringify(data))",              # 换页面接着在原来的地方
                  "window.addEventListener('pagehide', save);",
                  "pet.setPointerCapture(event.pointerId)",                        # 拖
                  "pet.addEventListener('contextmenu'",                            # 右键菜单
                  "body.append('pet', 'off');",
                  "headers: { 'X-Requested-With': 'fetch' }",
                  "window.matchMedia('(prefers-reduced-motion: reduce)')",
                  "bubble.textContent = said;"):                                   # 台词一律 textContent
        assert piece in js, piece
    assert "innerHTML" not in js
    # 不用等人也偶尔说一句（刚上墙、刚上顶有时候也说）；欢迎条上那几只在说、它自己的对话框开着时不插嘴
    for piece in ("const CHAT_EVERY = [35000, 80000], CHAT_FOR = 3200;", "if (now >= chatAt) {",
                  "if (Math.random() < 0.4) chat('ceiling');", "if (Math.random() < 0.3) chat('climb');",
                  "if (hovered || press || !menu.hidden || document.hidden || !bubble.hidden) return;",
                  "if (document.querySelector('.mascot-bubble:not([hidden])')) return;"):
        assert piece in js, piece
    css = (STATIC / "style.css").read_text(encoding="utf-8")
    for rule in (".pet-layer { position: fixed; inset: 0; z-index: 25; overflow: hidden; pointer-events: none; }",
                 ".mascot-sprite .m-look { transform: translate(calc(var(--lx, 0) * 1px), calc(var(--ly, 0) * 1px)); }",
                 ".mascot.pet:not(.on-floor) .mascot-shadow { display: none; }",
                 "@media print { .pet-layer { display: none !important; } }"):
        assert rule in css, rule
