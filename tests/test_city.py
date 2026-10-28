"""概览顶上的小城（city.py 摆、static/city.js 画）。

  · 一座正方形的城：n × n 个街区，棋盘格的马路，街区边上一圈人行道，路口斑马线；一条南北向的河，马路过河架桥；
  · 公司楼一个客户一栋（楼高按累计消费），一个客户时在城正中间，几个客户时分开放；仓库散在各个街区里（一个街区
    最多四块地，先占朝南、朝东的），屋顶和它的公司楼同一个颜色；结算了的是散在城里的小公园；库存在东北角的仓储区；
  · 客户少（不超过 ROOMY_UP_TO 个，线上现在就一个）街区大、仓库也大一号；客户多了紧凑一点；
  · 仓库大小按额度，屋顶的牌子写邮箱 @ 前面那段，底色按使用率（库存、读不到数的灰色）；最近一小时有调用的有小家伙
    搬货（怎么走 city.js 自己找），今天调用越多越多、走得越快；停了的、没调用的、读不到的、库存没有；
  · 停用账号存下来的账算进累计消费，结算了的也是一块小公园；流失了、名下又什么都没有的客户不画；
  · 概览上：开着小城的人有一份 #city-data 和 city.js（排在 mascots.js 前面），原来的像素风景照样在页面上（画不出来时换回去）；
    资料页上能关掉，关了就只有原来的风景、连脚本都不加载；只读用户也能关自己的；改名字头像不会把开关冲掉。
"""

from __future__ import annotations

import json
import math
import re
from datetime import date
from types import SimpleNamespace

import pytest

from bedrock_cost import city, config, cost_history, dashboard, users, welcome
from bedrock_cost.excel_source import Account, Customer

from .test_accounts import post
from .test_customer_pages import ALPHA, BETA, make
from .test_users import admin, lily, token  # noqa: F401  (fixture)
from .test_web import book, page_env, scrape, toasts, usage_states  # noqa: F401  (fixture / autouse)

STATIC = config.BASE_DIR / "src" / "bedrock_cost" / "static"


def links():
    return dict(link_account=lambda n: f"/account/{n}/", link_customer=lambda cid: f"/customers/{cid}",
                link_static=lambda path: f"/static/{path}")


def card(number: str, customer: str = "", budget: float = 1_000_000, spent: float = 250_000, state: str = "active",
         calls: float = 1000.0, error: bool = False) -> SimpleNamespace:
    """概览上一张卡片（dashboard.CardRow）里小城要用的那几样。"""
    pct = spent / budget * 100 if budget else None
    level = "none" if pct is None else "danger" if pct >= 90 else "warn" if pct >= 70 else "ok"
    return SimpleNamespace(
        key=f"{number}#2", account=number, email=f"{number[-4:]}@example.com", label=number[-4:], customer=customer,
        budget=budget, total_cost=spent, balance=budget - spent, usage_pct=pct, level=level, has_numbers=not error,
        state="error" if error else state, state_label=dashboard.STATES["error" if error else state],
        issue="读不到" if error else "",
        activity=SimpleNamespace(daily=[0.0] * 13 + [calls], sub="最近调用 3 分钟前" if state == "active" else ""))


def account(number: str, customer: str = "", settled: date | None = None, enabled: bool = True,
            budget: float = 1_000_000) -> Account:
    return Account(partner="P", account=number, budget=budget, tag_ratio=1, untag_ratio=1, row=2, customer=customer,
                   settled=settled, enabled=enabled, email=f"{number[-4:]}@example.com")


def people(*specs) -> list[Customer]:
    return [Customer(id=cid, name=name, avatar=avatar, status=status) for cid, name, avatar, status in specs]


def overlap(a: dict, b: dict, eps: float = 0.0) -> bool:
    """两块地压着没有（eps：挨着的边差一点点浮点误差不算压着）。"""
    return (a["x"] < b["x"] + b["w"] - eps and b["x"] < a["x"] + a["w"] - eps
            and a["z"] < b["z"] + b["d"] - eps and b["z"] < a["z"] + a["d"] - eps)


def inside(point, r: dict, slack: float = 0.01) -> bool:
    return r["x"] - slack <= point[0] <= r["x"] + r["w"] + slack and r["z"] - slack <= point[1] <= r["z"] + r["d"] + slack


def of(town: dict, kind: str) -> list[dict]:
    return [r for r in town["ground"] if r["k"] == kind]


def block_of(town: dict, x: float, z: float) -> tuple[int, int]:
    """(x, z) 在第几列、第几排的街区（按人行道围起来的那一圈找）。"""
    n, spacing = town["blocks"], city.ROOMY if len(town["offices"]) <= city.ROOMY_UP_TO else city.COMPACT
    lawns = sorted({(r["x"], r["z"]) for r in of(town, "lawn") + of(town, "yard")})
    xs, zs = sorted({x0 for x0, _ in lawns}), sorted({z0 for _, z0 in lawns})
    assert len(xs) == len(zs) == n
    col = next(i for i, x0 in enumerate(xs) if x0 - city.WALK <= x <= x0 + spacing.block)
    row = next(i for i, z0 in enumerate(zs) if z0 - city.WALK <= z <= z0 + spacing.block)
    return col, row


def one_customer(houses: int = 9, parks: int = 2):
    numbers = [f"5000000000{n:02d}" for n in range(houses + parks)]
    cards = [card(n, "C001", calls=100.0 * (i + 1)) for i, n in enumerate(numbers[:houses])]
    cards += [card(n, "C001") for n in numbers[houses:]]
    everyone = [account(n, "C001") for n in numbers[:houses]] + \
               [account(n, "C001", settled=date(2026, 9, 1)) for n in numbers[houses:]]
    return city.build(cards, everyone, people(("C001", "星河智能", 6, "on")), **links())


# ====================================================================== 城怎么摆
def test_正方形的城_马路_人行道_斑马线_河和桥():
    town = one_customer()
    width, depth = town["size"]
    assert width == depth                                                    # 正方形
    assert town["blocks"] == 3
    kinds = {r["k"] for r in town["ground"]}
    assert {"base", "road", "walk", "cross", "bridge", "water", "lawn", "plaza", "bank"} <= kinds
    # 东西向的马路（最北、每两排中间、最南）过河各一座桥；南北向的马路不过河
    roads = of(town, "road")
    lines = sorted({r["z"] for r in roads if r["dir"] == "ew"})
    assert len(lines) == town["blocks"] + 1 and len(of(town, "bridge")) == len(lines)
    water = of(town, "water")[0]
    walks = of(town, "walk")
    for bridge in of(town, "bridge"):
        assert bridge["x"] < water["x"] and bridge["x"] + bridge["w"] > water["x"] + water["w"]
        # 桥是马路加两边的人行道；两岸街区边上的人行道正好接到桥上
        assert bridge["z"] + city.WALK == pytest.approx(next(z for z in lines if abs(z - bridge["z"] - city.WALK) < 0.01))
        assert bridge["d"] == pytest.approx(city.ROAD + 2 * city.WALK)
        strips = [(bridge["z"], bridge["z"] + city.WALK), (bridge["z"] + bridge["d"] - city.WALK, bridge["z"] + bridge["d"])]
        for edge, side in ((bridge["x"], "west"), (bridge["x"] + bridge["w"], "east")):
            assert any(any(w["z"] <= z0 + 0.01 and w["z"] + w["d"] >= z1 - 0.01 for z0, z1 in strips)
                       and (w["x"] + w["w"] if side == "west" else w["x"]) == pytest.approx(edge) for w in walks), side
    assert not any(overlap(b, k, 0.001) for b in of(town, "bridge") for k in of(town, "bank"))     # 河岸的草让开桥
    assert not any(r["x"] < water["x"] + water["w"] and r["x"] + r["w"] > water["x"] for r in roads if r["dir"] == "ns")
    # 每个街区边上一圈人行道（四条），中间草地；斑马线连着路口两边的人行道
    assert len(of(town, "walk")) == 4 * town["blocks"] ** 2
    crossings = of(town, "cross")
    assert crossings and all(c["dir"] in ("x", "z") for c in crossings)
    assert all(any(overlap(c, w) or abs(c["x"] - (w["x"] + w["w"])) < 1.2 or abs(w["x"] - (c["x"] + c["w"])) < 1.2
                   for w in of(town, "walk")) for c in crossings)


def test_一个客户_公司楼在城中间_仓库散在各个街区_门都开在人行道上():
    town = one_customer()
    office = town["offices"][0]
    assert office["name"] == "星河智能" and office["h"] == 8.0 and office["url"] == "/customers/C001"
    assert office["avatar"] == {"img": "/static/avatars/c06.webp"}
    assert block_of(town, office["door"][0], office["door"][1] - 1) == (1, 1)          # 正中间那个街区
    assert office["door"][1] == pytest.approx(office["z"] + office["d"])              # 门朝南，门前一片广场
    assert any(inside(office["door"], r) for r in of(town, "plaza"))
    houses = [h for h in town["houses"] if h["stage"] != "stock"]
    assert len(houses) == 9 and len(town["parks"]) == 2
    # 散开：一个街区最多两栋，至少占了五个街区；都不在公司楼、公园、仓储区那几个街区里
    spots = [block_of(town, h["x"] + h["w"] / 2, h["z"] + h["d"] / 2) for h in houses]
    assert max(spots.count(b) for b in set(spots)) <= 2 and len(set(spots)) >= 5
    assert (1, 1) not in spots and (0, 2) not in spots
    # 先占朝南、朝东的（镜头看得见门），屋顶和公司楼同一个颜色，小家伙送到这个公司楼
    faces = [h["face"] for h in houses]
    assert set(faces[:6]) <= {"s", "e"} and faces.count("s") >= faces.count("n")
    assert all(h["roof"] == office["roof"] and h["office"] == "C001" for h in houses)
    # 门开在房子的那一面上，门口一小块砖地，接着人行道
    for h in houses:
        door_x, door_z = h["door"]
        on_face = {"e": door_x == pytest.approx(h["x"] + h["w"]), "w": door_x == pytest.approx(h["x"]),
                   "s": door_z == pytest.approx(h["z"] + h["d"]), "n": door_z == pytest.approx(h["z"])}[h["face"]]
        assert on_face, h["id"]
        apron = next(r for r in of(town, "plaza") if inside(h["door"], r))
        assert any(overlap({**apron, "x": apron["x"] - 0.05, "z": apron["z"] - 0.05, "w": apron["w"] + 0.1,
                            "d": apron["d"] + 0.1}, w) for w in of(town, "walk")), h["id"]
    # 宽敞：仓库大一号；谁都不压着谁
    size = city.house_size(1_000_000)
    assert houses[0]["size"][:2] == [round(size[0] * 1.15, 3), round(size[1] * 1.15, 3)]
    things = houses + town["parks"] + [office]
    for i, a in enumerate(things):
        for b in things[i + 1:]:
            assert not overlap(a, b), (a.get("id"), b.get("id"))
    # 小公园也散在城里（占的是仓库的地），不在公司楼那个街区
    assert all(block_of(town, p["x"], p["z"]) != (1, 1) for p in town["parks"])


def five_customers():
    specs = [(f"C00{n}", f"客户{n}", 0, "on") for n in range(1, 6)]
    cards = [card(f"6000000000{n}{k}", f"C00{n}", spent=100_000 * n) for n in range(1, 6) for k in range(3)]
    everyone = [account(c.account, c.customer) for c in cards]
    return city.build(cards, everyone, people(*specs), **links())


def test_客户多了_公司楼分开放_屋顶一个客户一个颜色():
    town = five_customers()
    assert town["blocks"] == 4 and town["size"][0] == town["size"][1]
    names = [o["name"] for o in town["offices"]]
    assert names == ["客户5", "客户4", "客户3", "客户2", "客户1"]           # 累计消费多的在前
    heights = [o["h"] for o in town["offices"]]
    assert heights[0] == 8.0 and heights == sorted(heights, reverse=True) and heights[-1] > 3.0
    assert town["offices"][0]["avatar"] == {"letter": "客", "tone": town["offices"][0]["avatar"]["tone"]}
    # 公司楼一个街区一栋，彼此至少隔一个街区；每个客户一个屋顶颜色，它的仓库跟着
    spots = [block_of(town, o["door"][0], o["door"][1] - 1) for o in town["offices"]]
    assert len(set(spots)) == 5
    assert min(abs(a[0] - b[0]) + abs(a[1] - b[1]) for i, a in enumerate(spots) for b in spots[i + 1:]) >= 2
    roofs = {o["id"]: o["roof"] for o in town["offices"]}
    assert len(set(roofs.values())) == 5
    assert all(h["roof"] == roofs[h["office"]] for h in town["houses"])
    assert town["houses"][0]["size"] == list(city.house_size(1_000_000))   # 紧凑：仓库不放大
    # 一个客户的仓库不扎堆：三个仓库在三个街区里
    for cid in roofs:
        mine = [block_of(town, h["x"], h["z"]) for h in town["houses"] if h["office"] == cid]
        assert len(mine) == len(set(mine)) == 3, cid


def walk_grid(town: dict) -> set[tuple[int, int]]:
    """city.js 里小家伙能走的格子（0.2 一格）：人行道、斑马线、门口的砖地、桥两边的人行道（栏杆里面）。"""
    cell, grid = 0.2, set()
    cols, rows = math.ceil(town["size"][0] / cell), math.ceil(town["size"][1] / cell)

    def mark(x0, z0, x1, z1):
        for j in range(max(0, math.floor(z0 / cell)), min(rows - 1, math.ceil(z1 / cell) - 1) + 1):
            for i in range(max(0, math.floor(x0 / cell)), min(cols - 1, math.ceil(x1 / cell) - 1) + 1):
                grid.add((i, j))
    for r in town["ground"]:
        if r["k"] in ("walk", "cross", "plaza"):
            mark(r["x"], r["z"], r["x"] + r["w"], r["z"] + r["d"])
        elif r["k"] == "bridge":
            mark(r["x"], r["z"] + 0.1, r["x"] + r["w"], r["z"] + 0.6)
            mark(r["x"], r["z"] + r["d"] - 0.6, r["x"] + r["w"], r["z"] + r["d"] - 0.1)
    return grid


def test_每个仓库都走得到自己的公司楼_过河走桥():
    out = {"e": (1, 0), "w": (-1, 0), "s": (0, 1), "n": (0, -1)}
    for town in (one_customer(), five_customers()):
        grid = walk_grid(town)
        at = lambda x, z: (math.floor(x / 0.2), math.floor(z / 0.2))  # noqa: E731
        crossed = 0
        for office in town["offices"]:
            start = at(office["door"][0], office["door"][1] + 0.5)
            assert start in grid
            seen, todo = {start}, [start]
            while todo:
                i, j = todo.pop()
                for di, dj in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                    nxt = (i + di, j + dj)
                    if nxt in grid and nxt not in seen:
                        seen.add(nxt)
                        todo.append(nxt)
            for h in town["houses"]:
                if h.get("office") == office["id"]:
                    dx, dz = out[h["face"]]
                    assert at(h["door"][0] + dx * 0.2, h["door"][1] + dz * 0.2) in seen, h["id"]
                    river = of(town, "water")[0]["x"]
                    crossed += (h["door"][0] < river) != (office["door"][0] < river)
        assert crossed                                       # 真有隔着河的，走得过去是因为桥接上了


def test_每个街区两盏路灯_立在人行道的拐角上():
    for town in (one_customer(), five_customers()):
        lamps = [p for p in town["props"] if p["k"] == "lamp"]
        assert len(lamps) == 2 * town["blocks"] ** 2
        walks = of(town, "walk")
        assert all(any(inside((p["x"], p["z"]), w, 0) for w in walks) for p in lamps)
        things = town["houses"] + town["parks"] + town["offices"]
        assert not any(inside((p["x"], p["z"]), h, 0) for p in lamps for h in things)


def test_有斑马线的路口都有红绿灯_立在路灯旁边那两个拐角上():
    for town in (one_customer(), five_customers()):
        signals = [p for p in town["props"] if p["k"] == "signal"]
        lamps = {(p["x"], p["z"]) for p in town["props"] if p["k"] == "lamp"}
        crossings = of(town, "cross")
        assert signals and {s["at"] for s in signals} == {c["at"] for c in crossings}     # 有斑马线的路口都有
        walks = of(town, "walk")
        for s in signals:
            assert any(inside((s["x"], s["z"]), w, 0) for w in walks) and (s["x"], s["z"]) not in lamps
            assert any(c["at"] == s["at"] and inside((s["x"], s["z"]), c, 0.35) for c in crossings)   # 挨着自己路口的斑马线
        for at in {s["at"] for s in signals}:
            assert sorted(s["corner"] for s in signals if s["at"] == at) in (["ne"], ["sw"], ["ne", "sw"])
        # 城中间的十字路口两根都有
        assert any(sorted(s["corner"] for s in signals if s["at"] == at) == ["ne", "sw"] for at in {s["at"] for s in signals})


def test_街区够不够():
    assert city.blocks_per_side(1, 11, True) == 3 and city.blocks_per_side(5, 30, True) == 5
    assert city.blocks_per_side(1, 0, False) == 3 and city.blocks_per_side(2, 20, True) == 4
    for customers in range(1, 7):
        for lots in range(0, 40, 3):
            n = city.blocks_per_side(customers, lots, True)
            # 公司楼、公园、仓储区各占一个，剩下的街区每个四块地，放得下
            assert (n * n - customers - 2) * 4 >= lots


# ====================================================================== 仓库、牌子、小家伙
def test_仓库大小按额度_牌子写余额_小家伙按今天的调用():
    cards = [card("700000000001", "C001", budget=2_500_000, spent=500_000, calls=9000.0),
             card("700000000002", "C001", budget=500_000, spent=470_000, calls=100.0),
             card("700000000003", "C001", state="stopped", calls=50.0),
             card("700000000004", "C001", state="idle", calls=0.0),
             card("700000000005", "C001", error=True)]
    everyone = [account(c.account, "C001", budget=c.budget) for c in cards]
    town = city.build(cards, everyone, people(("C001", "星河智能", 6, "on")), **links())
    by = {h["id"]: h for h in town["houses"]}
    big, small = by["700000000001"], by["700000000002"]
    assert all(a > b for a, b in zip(big["size"], small["size"]))
    # 屋顶的牌子写邮箱 @ 前面那段，底色按使用率；读不到数的灰色
    assert big["sign"] == {"text": "0001", "tone": "ok"} and small["sign"] == {"text": "0002", "tone": "danger"}
    assert by["700000000005"]["sign"] == {"text": "0005", "tone": "none"}
    assert (big["workers"], small["workers"]) == (3, 1) and big["speed"] > small["speed"] > 0
    assert by["700000000003"]["workers"] == by["700000000004"]["workers"] == by["700000000005"]["workers"] == 0
    tip = big["tip"]
    assert tip["head"] == "0001@example.com" and tip["total"] == "余额 $2,000,000" and tip["foot"] == "点一下看特写"
    # 点开的卡片：大字是余额，使用率那一条，近 14 天的调用，按钮进账号页
    info = big["card"]
    assert (info["kind"], info["title"], info["sub"], info["big"], info["big_label"]) == \
        ("house", "0001@example.com", "星河智能", "$2,000,000", "余额")
    assert info["pct"] == 20.0 and info["tone"] == "ok" and info["calls14"] == [0] * 13 + [9000]
    assert ["今天调用", "9,000 次"] in info["facts"] and (info["link"], info["url"]) == ("进账号页", "/account/700000000001/")
    assert by["700000000005"]["card"]["big"] == "—" and by["700000000005"]["card"]["pct"] is None
    assert ["今天调用", "9,000 次"] in tip["rows"] and ["已用", "$500,000 · 20%"] in tip["rows"]
    assert ["状态", "活跃 · 最近调用 3 分钟前"] in tip["rows"] and big["url"] == "/account/700000000001/"
    office = town["offices"][0]
    assert office["tip"]["total"] == f"累计消费 ${sum(c.total_cost for c in cards if c.has_numbers):,.0f}"
    assert office["card"]["big"] == f"${sum(c.total_cost for c in cards if c.has_numbers):,.0f}"
    assert ["今天调用", "10,150 次"] in office["card"]["facts"] and office["card"]["link"] == "进客户页"
    # 点公司楼：地上铺出名下五个仓库走到这儿的路，有小家伙在搬的两条在动
    assert office["tip"]["foot"] == "点一下看各个仓库走到这儿的路"
    assert office["card"]["note"] == "地上一路小方块是 5 个仓库走到这儿的路；往这边挪的 2 条，现在有小家伙在搬"
    # 余额合计只算使用中的（这几个都是使用中，读不到数的不算）
    assert ["余额合计", f"${sum(c.balance for c in cards if c.has_numbers):,.0f}"] in office["tip"]["rows"]


def test_牌子太长截一截():
    long = card("700000000009", "C001")
    long.label = "a-really-long-email-prefix"
    town = city.build([long], [account("700000000009", "C001")], people(("C001", "星河智能", 6, "on")), **links())
    text = town["houses"][0]["sign"]["text"]
    assert len(text) == city.SIGN_CHARS and text == long.label[:city.SIGN_CHARS - 1] + "…"


def test_额度_累计消费_调用换成大小高矮快慢():
    assert city.scale_of(100_000) == 0 and city.scale_of(3_000_000) == 1 and city.scale_of(0) == 0
    sizes = [city.house_size(b) for b in (100_000, 500_000, 1_000_000, 3_000_000, 9_000_000)]
    assert sizes == sorted(sizes) and sizes[-1] == sizes[-2]                 # 300 万以上一样大
    depth, width, _ = sizes[-1]
    assert depth <= city.COMPACT.lot_d - city.APRON and width <= city.COMPACT.lot_w - 0.2   # 紧凑的地也放得下
    assert city.office_height(0, 100) == 3.0 and city.office_height(100, 100) == 8.0
    assert 3.0 < city.office_height(25, 100) < city.office_height(64, 100) < 8.0
    assert city.workers_of(100, 100) == (3, 1.4) and city.workers_of(1, 100)[0] == 1
    # 最近一小时在调用、今天这一格还是空的（刚过半夜）：也有一只慢慢搬
    assert city.workers_of(0, 100) == (1, 0.8) and city.workers_of(50, 0) == (1, 0.8)


# ====================================================================== 结算、库存、停用、流失
def test_结算了的是小公园_库存在仓储区_停用的账也算进累计():
    cost_history.remember_many([("800000000009", ["2026-07-01", "2026-07-02"], [500.0, 500.0], [600.0, 600.0])])
    cards = [card("800000000001", "C001", spent=100_000), card("800000000002", "C001", spent=300_000),
             card("800000000003", "", spent=0, calls=500.0)]
    everyone = [account("800000000001", "C001"), account("800000000002", "C001", settled=date(2026, 9, 3)),
                account("800000000003", ""), account("800000000009", "C001", settled=date(2026, 8, 1), enabled=False),
                account("800000000010", "C001", enabled=False)]
    town = city.build(cards, everyone, people(("C001", "星河智能", 6, "on")), **links())
    assert sorted(p["id"] for p in town["parks"]) == ["800000000002", "800000000009"]
    park = next(p for p in town["parks"] if p["id"] == "800000000009")
    assert park["tip"]["total"] == "已结算 · 2026-08-01" and ["用了", "$1,200"] in park["tip"]["rows"]
    assert park["url"] == "/account/800000000009/" and "小公园" in park["tip"]["foot"]
    assert park["card"]["big"] == "已结算" and ["结算", "2026-08-01"] in park["card"]["facts"]
    stock = next(h for h in town["houses"] if h["id"] == "800000000003")
    assert stock["stage"] == "stock" and stock["sign"] == {"text": "0003", "tone": "none"}
    assert stock["workers"] == 0 and stock["face"] == "s" and "office" not in stock    # 库存没有公司楼可送
    assert stock["roof"] == city.STOCK_ROOF and block_of(town, stock["x"], stock["z"]) == (town["blocks"] - 1, 0)
    assert any(inside((stock["x"], stock["z"]), r) for r in of(town, "yard"))     # 东北角的仓储区
    assert ["客户", "库存（还没分给客户）"] in stock["tip"]["rows"]
    assert [h["id"] for h in town["houses"] if h["stage"] != "stock"] == ["800000000001"]   # 停用没结算的不画
    # 累计消费：在用的两个 + 停用的那个存下来的账
    assert town["offices"][0]["tip"]["total"] == "累计消费 $401,200"
    assert ["账号", "1 个在用 · 2 个已结算"] in town["offices"][0]["tip"]["rows"]


def test_库存多了_仓储区放不下的散到城里():
    cards = [card(f"81000000000{n}", "") for n in range(9)] + [card("810000000099", "C001")]
    everyone = [account(c.account, c.customer) for c in cards]
    town = city.build(cards, everyone, people(("C001", "星河智能", 6, "on")), **links())
    stock = [h for h in town["houses"] if h["stage"] == "stock"]
    yard = of(town, "yard")[0]
    in_yard = [h for h in stock if inside((h["x"], h["z"]), yard)]
    assert len(stock) == 9 and 0 < len(in_yard) < 9
    assert all(h["roof"] == city.STOCK_ROOF for h in stock)              # 散出去的也是灰屋顶


def test_流失了又什么都没有的客户不画_暂停的照样画():
    specs = [("C001", "星河智能", 6, "on"), ("C002", "走了的", 0, "gone"), ("C003", "歇着的", 0, "pause")]
    town = city.build([card("900000000001", "C001")], [account("900000000001", "C001")], people(*specs), **links())
    assert [o["id"] for o in town["offices"]] == ["C001", "C003"]
    paused = town["offices"][1]
    assert paused["tip"]["foot"] == "点一下看特写" and paused["card"]["note"] == ""       # 名下没仓库，没有路可铺
    assert ["状态", "暂停"] in town["offices"][1]["tip"]["rows"]


# ====================================================================== 概览上
def city_of(html: str):
    found = re.search(r'<script id="city-data" type="application/json">(.*?)</script>', html, re.S)
    return json.loads(found.group(1)) if found else None


def test_概览上是一座小城_原来的风景也在(admin):
    make(admin)
    post(admin, "/customers/C001/assign", account=[ALPHA, BETA])
    html = admin.get("/").get_data(as_text=True)
    town = city_of(html)
    assert town and [o["name"] for o in town["offices"]] == ["星河智能"]
    houses = {h["id"]: h for h in town["houses"]}
    assert set(houses) == {"111111111111", "222222222222"}                 # gamma 停用了、没结算，不画
    assert houses["222222222222"]["stage"] == "risk" and houses["111111111111"]["stage"] == "use"
    assert houses["111111111111"]["url"] == "/account/111111111111/"
    assert town["offices"][0]["avatar"] == {"img": "/static/avatars/c06.webp"}
    # 画布、三个按钮、脚本排在 mascots.js 前面（先挂上 is-city，像素风景那几只就不动了）；原来的风景照样在（画不出来时换回去）
    assert '<div class="city" data-city hidden>' in html and html.count('data-city-zoom=') == 3
    assert html.index('<script type="module" src="/static/city.js">') < html.index('src="/static/mascots.js"')
    assert "data-mascot-stage" in html and 'id="mascot-talk"' in html


def saved_city(who: str = "admin") -> str:
    data = json.loads(config.USERS_PATH.read_text(encoding="utf-8"))
    look = data["admin"] if who == "admin" else next(u for u in data["users"] if u["name"] == who)
    return look["city"]


def city_card(html: str) -> str:
    start = html.index('aria-labelledby="city-title"')
    return html[start:html.index("</section>", start)]


def test_资料页能关掉小城_关了就是原来的像素风景(admin):
    card_html = city_card(admin.get("/me").get_data(as_text=True))
    assert "开着" in scrape(card_html) and 'aria-checked="true"' in card_html and 'name="city" value="off"' in card_html
    response = admin.post("/me/city", data={"csrf": token(admin), "city": "off"})
    assert response.status_code == 302 and response.headers["Location"] == "/me"
    page = admin.get("/me").get_data(as_text=True)
    assert [t.title for t in toasts(page)] == ["小城关了"] and saved_city() == users.CITY_OFF
    assert "关了" in scrape(city_card(page)) and 'name="city" value="on"' in city_card(page)
    html = admin.get("/").get_data(as_text=True)
    assert city_of(html) is None and "city.js" not in html and "data-city" not in html
    assert "data-mascot-stage" in html                                       # 原来的风景
    admin.post("/me/city", data={"csrf": token(admin), "city": "on"})
    assert [t.title for t in toasts(admin.get("/me").get_data(as_text=True))] == ["小城开回来了"]
    assert saved_city() == "" and city_of(admin.get("/").get_data(as_text=True)) is not None


def test_只读用户也能关自己的小城_开还是关不对不改(admin, lily):
    response = lily.post("/me/city", data={"csrf": token(lily), "city": "off"})
    assert response.status_code == 302 and users.find("lily").look.city == users.CITY_OFF
    assert city_of(lily.get("/").get_data(as_text=True)) is None
    assert city_of(admin.get("/").get_data(as_text=True)) is not None      # 管理员的还开着
    admin.post("/me/city", data={"csrf": token(admin), "city": "maybe"})
    assert [t.title for t in toasts(admin.get("/me").get_data(as_text=True))] == ["没改成"]
    admin.post("/me/city", data={"city": "off"})                             # 没带令牌
    assert city_of(admin.get("/").get_data(as_text=True)) is not None


def test_改名字头像_换背景_收小家伙都不会把小城的开关冲掉(admin):
    admin.post("/me/city", data={"csrf": token(admin), "city": "off"})
    admin.post("/me", data={"csrf": token(admin), "display": "老板", "avatar_text": "", "avatar_color": ""})
    admin.post("/me/scene", data={"csrf": token(admin), "scene": "snow"}, headers={"X-Requested-With": "fetch"})
    admin.post("/me/pet", data={"csrf": token(admin), "pet": "off"}, headers={"X-Requested-With": "fetch"})
    saved = json.loads(config.USERS_PATH.read_text(encoding="utf-8"))["admin"]
    assert saved["display"] == "老板" and saved["scene"] == "snow" and saved["pet"] == users.PET_OFF
    assert saved["city"] == users.CITY_OFF


def test_资料里的小城开关只认off():
    assert users._look({"city": "off"}).city == users.CITY_OFF
    for raw in ("on", "", 1, None, "OFF"):
        assert users._look({"city": raw}).city == "", raw
    assert users.Look().city_on and not users.Look(city=users.CITY_OFF).city_on
    assert users._look_json(users.Look(city=users.CITY_OFF))["city"] == users.CITY_OFF


# ====================================================================== 脚本、样式、three.js
def test_小城的脚本_样式_three_js():
    js = (STATIC / "city.js").read_text(encoding="utf-8")
    for piece in ("await import('./vendor/three/three.module.min.js')",      # 确定要画了才加载 three.js
                  "canvas.getContext('webgl2'",                            # 没有 WebGL2 就不挂 is-city
                  "welcome.classList.add('is-city');",
                  "document.dispatchEvent(new CustomEvent('city:off'));",  # 画不出来换回像素风景
                  "window.matchMedia('(prefers-reduced-motion: reduce)')",
                  "new IntersectionObserver(",                             # 滚出屏幕不画
                  "canvas.addEventListener('wheel'",                       # 滚轮缩放……
                  "pan: event.button === 2",                               # 右键拖着平移，不弹菜单
                  "canvas.addEventListener('contextmenu', function (event) { event.preventDefault(); });",
                  "function astar(from, to) {",                            # 小家伙自己找路……
                  "function straighten(points) {",
                  "if (random() < 0.3) m.pauseAt = m.total * (0.3 + random() * 0.4);",   # ……偶尔停下来张望
                  "{ passive: false }",
                  "if (!zoomTo(view.zoom * factor, groundAt(event.clientX, event.clientY), Math.min(1, view.zoom))) return;",
                  "function open(item) {",                                 # 点开：飞过去看特写，弹卡片
                  "document.addEventListener('keydown', function (event) { if (event.key === 'Escape' && focus) close(); });",
                  "go.href = info.url;",
                  "function moveClouds(dt) {",                            # 云慢慢飘
                  "function showRoutes(office) {",                         # 点公司楼：铺出各个仓库走到这儿的路
                  "const to = frame(points, view.yaw, FOCUS_TILT);",
                  "bubble.textContent = line();",
                  # 背景跟着欢迎条：一开始按 data-kind，换了跟着 scene:change 换；雨雪花瓣落叶萤火虫，减少动态效果时不撒
                  "setScene(stage ? stage.dataset.kind : 'day');",
                  "if (stage) stage.addEventListener('scene:change', function (event) { setScene(event.detail); });",
                  "function setWeather(kind) {",
                  "if (!f || still.matches) return;",
                  "lightsMesh.material = s.lamps ? glowMat : lambert;",            # 黄昏、夜里窗户和路灯亮了
                  "for (const p of data.props) if (p.k === 'lamp' || p.k === 'signal') walkable[cellOf(p.x, p.z)] = 0;",
                  # 红绿灯：南北、东西轮着放行，小家伙到了路口红灯就在马路牙子上等
                  "function mayCross(at, dir, time) {",
                  "m.waiting = m.pauseLeft <= 0 && redAhead(m, time);",
                  "if (m.waiting) return '在路口等红灯';",
                  "host.dataset.kind = s.kind;"):
        assert piece in js, piece
    assert "innerHTML" not in js                                             # 提示、对话框一律 textContent
    # 九种背景一种不少（和 welcome.SCENES 对得上）
    block = js[js.index("  const SCENES = {"):js.index("  };", js.index("  const SCENES = {"))]
    assert re.findall(r"^    (\w+): \{", block, re.M) == list(welcome.SCENES)
    for fall in ("rain", "snow", "petals", "leaves", "fireflies"):
        assert f"    {fall}: {{ count: " in js, fall
    mascots = (STATIC / "mascots.js").read_text(encoding="utf-8")
    assert "if (stage.closest('.welcome.is-city') && !mascots.late) {" in mascots
    assert "document.addEventListener('city:off', function () { mascots.late = true; mascots(); }, { once: true });" in mascots
    # 换背景的按钮开着小城也能用：先挂上按钮，再看是不是小城；换了发 scene:change
    assert mascots.index("    picker();") < mascots.index("if (stage.closest('.welcome.is-city') && !mascots.late) {")
    assert "stage.dispatchEvent(new CustomEvent('scene:change', { detail: kind }));" in mascots
    assert "stage.addEventListener('scene:change', function (event) { applyKind(event.detail); });" in mascots
    css = (STATIC / "style.css").read_text(encoding="utf-8")
    for rule in (".welcome.is-city .mascot-stage > :not(.scene-pick):not(.scene-menu) { display: none; }",
                 ".welcome.is-city .scene-pick { top: 14px; right: 14px; pointer-events: auto; }",
                 ".welcome.is-city.has-city-card .scene-pick { display: none; }",
                 '.city:is([data-kind="night"], [data-kind="fireflies"]) { background: linear-gradient(',
                 '.city[data-kind="rainbow"]::before {', ".city[hidden] { display: none; }",
                 ".city-bubble { bottom: auto;", ".city-veil[hidden], .city-card[hidden] { display: none; }"):
        assert rule in css, rule
    vendor = STATIC / "vendor" / "three"
    core, module = (vendor / "three.core.min.js").read_text(encoding="utf-8"), (vendor / "three.module.min.js").read_text(encoding="utf-8")
    assert 'const t="185"' in core and "SPDX-License-Identifier: MIT" in core and "SPDX-License-Identifier: MIT" in module
    assert 'from"./three.core.min.js"' in module
    assert "MIT License" in (vendor / "LICENSE").read_text(encoding="utf-8")
