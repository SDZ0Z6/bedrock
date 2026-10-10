"""只改额度（账号管理表格里那支笔、账号页的「调整额度」），以及账号页「编辑」打开修改弹窗、改完回账号页。"""

from __future__ import annotations

import re
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest

from bedrock_cost import auth, excel_source

from .conftest import LEDGER_ROWS, TEST_USER
from .test_accounts import _edit, admin, by_account, page, post, token  # noqa: F401  (admin 是 fixture)
from .test_customer_pages import ALPHA, make
from .test_web import page_env, scrape, usage_states  # noqa: F401  (autouse)

ALPHA_NO = str(LEDGER_ROWS[0][1])
FETCH = {"X-Requested-With": "fetch"}


def budget_events() -> list[excel_source.CustomerEvent]:
    return [e for e in excel_source.load_events(force=True) if e.type == "budget"]


# ====================================================================== 账号管理：额度旁边的笔
def test_表格里每个账号的额度旁边有一支笔(admin):
    html = page(admin)
    assert html.count("data-budget-edit") >= len(LEDGER_ROWS)
    assert 'id="budget-pop" popover' in html and 'action="/accounts/budget"' in html


def test_填新的额度_回JSON_时间线上记一笔(admin):
    target = by_account(ALPHA_NO)
    response = admin.post("/accounts/budget", data={"csrf": token(admin), "key": target.key, "budget": "250,000",
                                                    "note": "客户补了"}, headers=FETCH)
    result = response.get_json()
    assert response.status_code == 200 and result["ok"] and result["budget"] == 250000
    assert result["budget_text"] == "$250,000.00" and "→ $250,000" in result["text"]
    assert by_account(ALPHA_NO).budget == 250000
    [event] = budget_events()
    assert (event.customer, event.before, event.amount, event.note, event.actor) == ("", target.budget, 250000,
                                                                                      "客户补了", TEST_USER)


def test_分给了客户的_记在客户名下(admin, fake_costs):
    make(admin)
    post(admin, "/customers/C001/assign", account=[ALPHA])
    admin.post("/accounts/budget", data={"csrf": token(admin), "key": ALPHA, "budget": "900000"}, headers=FETCH)
    [event] = budget_events()
    assert event.customer == "C001"


@pytest.mark.parametrize("value, words", [("abc", "要填数字"), ("-5", "不能是负数"), ("", "要填数字")])
def test_填错了不改(admin, value, words):
    target = by_account(ALPHA_NO)
    response = admin.post("/accounts/budget", data={"csrf": token(admin), "key": target.key, "budget": value},
                          headers=FETCH)
    result = response.get_json()
    assert response.status_code == 400 and not result["ok"] and words in result["text"]
    assert by_account(ALPHA_NO).budget == target.budget and not budget_events()


def test_和原来一样_不写文件(admin):
    target = by_account(ALPHA_NO)
    response = admin.post("/accounts/budget", data={"csrf": token(admin), "key": target.key,
                                                    "budget": str(target.budget)}, headers=FETCH)
    assert response.get_json()["title"] == "没有改动" and not budget_events()


def test_账号已经不在了(admin):
    response = admin.post("/accounts/budget", data={"csrf": token(admin), "key": "999999999999#9", "budget": "1"},
                          headers=FETCH)
    assert response.status_code == 409


def test_没开JS时是普通提交_回账号管理(admin):
    target = by_account(ALPHA_NO)
    response = post(admin, "/accounts/budget", key=target.key, budget="123456")
    assert response.status_code == 302 and response.headers["Location"].endswith("/accounts/")
    assert by_account(ALPHA_NO).budget == 123456


# ====================================================================== 账号页：调整额度
# 页头在六个页签上都一样；用时间线页签看（它只查按天的消费，测试里是假的，不会去连 AWS）
def test_账号页有调整额度(admin, fake_costs):
    response = admin.get(f"/account/{ALPHA_NO}/timeline")
    html = response.get_data(as_text=True)
    assert response.status_code == 200
    dialog = re.search(r'<dialog class="modal modal-sm" id="dlg-acct-budget">(.*?)</dialog>', html, re.S).group(1)
    assert 'action="/accounts/budget"' in dialog and 'name="add"' in dialog
    assert f'name="next" value="/account/{ALPHA_NO}/timeline"' in dialog
    assert "data-open-budget" in html


def test_账号页填加多少_改完回账号页(admin, fake_costs):
    target = by_account(ALPHA_NO)
    response = post(admin, "/accounts/budget", key=target.key, add="-1000", note="退了一点",
                    next=f"/account/{ALPHA_NO}/cost?preset=mtd")
    assert response.status_code == 302 and response.headers["Location"] == f"/account/{ALPHA_NO}/cost?preset=mtd"
    assert by_account(ALPHA_NO).budget == target.budget - 1000
    [event] = budget_events()
    assert (event.before, event.amount, event.note) == (target.budget, target.budget - 1000, "退了一点")


def test_加多少不能是0_也不能减成负数(admin):
    target = by_account(ALPHA_NO)
    for add in ("0", str(-target.budget - 1)):
        response = admin.post("/accounts/budget", data={"csrf": token(admin), "key": target.key, "add": add},
                              headers=FETCH)
        assert response.status_code == 400
    assert by_account(ALPHA_NO).budget == target.budget


# ====================================================================== 账号页「编辑」：打开修改弹窗，改完、取消都回来
def test_编辑链接带着回来的地址(admin, fake_costs):
    response = admin.get(f"/account/{ALPHA_NO}/timeline")
    html = response.get_data(as_text=True)
    assert response.status_code == 200
    href = re.search(r'href="(/accounts/\?[^"]*)"[^>]*>(?:(?!</a>).)*编辑资料</a>', html, re.S).group(1).replace("&amp;", "&")
    assert parse_qs(urlsplit(href).query) == {"edit": [by_account(ALPHA_NO).key],
                                              "back": [f"/account/{ALPHA_NO}/timeline"]}


def test_账号管理按edit打开那一行的修改弹窗(admin):
    target = by_account(ALPHA_NO)
    html = admin.get("/accounts/?" + urlencode({"edit": target.key, "back": f"/account/{ALPHA_NO}/"})).get_data(as_text=True)
    opened = re.findall(r'<dialog class="modal" id="(dlg-edit-\d+)"\s*data-reopen data-back="([^"]*)">', html)
    assert opened == [("dlg-edit-1", f"/account/{ALPHA_NO}/")]
    assert f'<input type="hidden" name="next" value="/account/{ALPHA_NO}/">' in html
    # 值是台账里的（不是空的回填）
    dialog = html[html.index('id="dlg-edit-1"'):html.index("</dialog>", html.index('id="dlg-edit-1"'))]
    assert f'value="{target.email}"' in dialog


def test_back不是本站的地址就不认(admin):
    target = by_account(ALPHA_NO)
    html = admin.get("/accounts/?" + urlencode({"edit": target.key, "back": "//evil.example.com/x"})).get_data(as_text=True)
    assert "evil.example.com" not in html


def test_改完回账号页(admin):
    target = by_account(ALPHA_NO)
    response = _edit(admin, target, budget="250000", next=f"/account/{ALPHA_NO}/")
    assert response.status_code == 302 and response.headers["Location"] == f"/account/{ALPHA_NO}/"


def test_校验没过_弹窗带着回来的地址重新打开(admin):
    target = by_account(ALPHA_NO)
    response = _edit(admin, target, budget="abc", next=f"/account/{ALPHA_NO}/")
    html = response.get_data(as_text=True)
    assert response.status_code == 400
    assert f'data-reopen data-back="/account/{ALPHA_NO}/"' in html


@pytest.mark.parametrize("target, expected", [
    ("/account/1/", "/account/1/"), ("/accounts/?q=1", "/accounts/?q=1"),
    ("//evil.example.com", "/"), ("/\\evil.example.com", "/"), ("https://evil.example.com", "/"), ("", "/"), (None, "/"),
])
def test_只跳回本站(target, expected):
    assert auth.safe_next(target, "/") == expected


def test_登录后的next也只认本站(client):
    response = client.post("/login", data={"username": TEST_USER, "password": "test-password-123",
                                            "next": "//evil.example.com/steal"})
    assert response.status_code == 302 and "evil.example.com" not in response.headers["Location"]


# ====================================================================== 客户页：名下账号点进账号页、「调整额度」
def test_名下账号点邮箱进账号页(admin, fake_costs):
    make(admin)
    post(admin, "/customers/C001/assign", account=[ALPHA])
    html = admin.get("/customers/C001").get_data(as_text=True)
    assert re.search(rf'<a class="holding-link" href="/account/{ALPHA_NO}/"', html)
    assert ">调整额度</button>" in html and ">加额度<" not in html
    assert "<h2>调整额度</h2>" in html and scrape(html).count("加额度") == 0
