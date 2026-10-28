"""概览顶上的小城（static/city.js 用 three.js 画，方块体素风，斜 45° 看过去）：一座正方形的城，棋盘格的马路，
账号是散在城里的仓库，小家伙沿着人行道把箱子送到客户的公司楼。

  · 城：n × n 个街区，街区之间是马路（街区边上一圈人行道，路口有斑马线），最外面一圈也是马路；一条河从北往南
    穿过城，马路过河的地方架桥；南北两头各多一条草地，城正好是正方形。
  · 公司楼：一个客户一栋，楼越高累计消费越多（停用账号存下来的账也算），楼顶挂客户的头像和名字，门朝南，门前一小片
    广场。只有一个客户时在城正中间那个街区；几个客户时分在几个街区，彼此隔得远一点。每个客户一个颜色：公司楼的屋顶、
    它名下仓库的屋顶都是这个颜色。
  · 仓库：一个账号，散在各个街区里（一个街区最多四块地，风车一样四个角各一块，门朝各自那一边的马路；先摆朝南、
    朝东的，镜头看得见门）。大小按额度；门这一头的屋顶上一块牌子写邮箱 @ 前面那段，底色按使用率（绿 / 黄 / 红；
    读不到数、库存是灰的）。最近一小时有调用（用量状态是活跃）的门开着，小家伙从里面搬箱子送到它的公司楼
    （city.js 在人行道、斑马线、桥上找路），再空手回来；今天调用越多，搬的小家伙越多、走得越快。别的门拉着卷帘门。
  · 已结算的账号：仓库拆了，那块地成了一块小公园，和仓库一样散在城里。
  · 库存（还没分给客户的账号）：东北角一个仓储区，一排排灰屋顶的空仓库（放不下的散到城里，也是灰屋顶）。
  · 西南角是中心公园（喷泉、长椅、树）；剩下的街区是几栋小房子和树；河岸、城边的草地上种着树。
  · 客户少（不超过 ROOMY_UP_TO 个）的时候街区大一点（ROOMY）：仓库之间空得多、也大一号；客户多了紧凑一点（COMPACT）。

摆在哪、多大、什么颜色、悬浮和点开写什么都在这儿算好（好测），city.js 只管画、动、找路、悬浮、点开。坐标一格是 1：
x 往东，z 往南（three.js 里 y 朝上），城的西北角是 (0, 0)。用的全是概览已经查好的数（卡片的额度、消费、用量状态、
近 14 天调用；停用账号存下来的账），不多查 AWS。
"""

from __future__ import annotations

import math
import zlib
from dataclasses import dataclass, field
from typing import Callable

from . import cost_history, customers
from .filters import money0

# ---------------------------------------------------------------- 尺寸（一格 = 1）
ROAD = 2.0            # 马路宽
WALK = 0.6            # 人行道：每个街区边上一圈
RIVER_GAP = 4.0       # 河那一道缝：两岸各半格草 + 3 格河，代替那里的一条南北马路
APRON = 0.35          # 仓库门口到人行道之间那一小块砖地
LAMP_INSET = 0.12     # 路灯、红绿灯离街区外角多远（立在人行道的拐角上）
STOCK_LOT_W = 2.25    # 仓储区一块地的宽
STOCK_ROW = 3.2       # 仓储区一排：2.6 格地 + 0.6 格路
ROOMY_UP_TO = 2       # 客户不超过这么多个，街区大一点
SIDES = ("s", "e", "w", "n")    # 街区里四块地占的先后：朝南、朝东的门镜头看得见，先占


@dataclass(frozen=True)
class Spacing:
    """街区里的尺寸。"""
    block: float      # 一个街区多大（含边上一圈人行道）
    lot_w: float      # 仓库那块地沿着马路的宽
    lot_d: float      # 仓库那块地的进深（从人行道往里）
    office_w: float
    office_d: float
    grow: float       # 仓库放大多少（宽敞的时候大一号）


COMPACT = Spacing(block=9.0, lot_w=3.4, lot_d=3.2, office_w=4.4, office_d=3.4, grow=1.0)
ROOMY = Spacing(block=10.0, lot_w=3.8, lot_d=3.6, office_w=5.0, office_d=3.8, grow=1.15)

MAX_WORKERS = 3       # 一个仓库最多几只小家伙搬货
SIGN_CHARS = 16       # 牌子上最多几个字
ROOFS = ("#d97757", "#6f8fb0", "#5f9e8f", "#d6a341", "#9b7ca3", "#c76b6b", "#7d9a5a", "#b38b6d")
STOCK_ROOF = "#9a978e"
STAGES = {"use": "使用中", "risk": "风控 · 待替换", "pending": "待结算", "stock": "库存"}


@dataclass
class _Group:
    """一个客户：公司楼、名下的仓库和小公园。"""
    customer: object
    houses: list[dict] = field(default_factory=list)
    parks: list[dict] = field(default_factory=list)
    lifetime: float = 0.0
    balance: float = 0.0
    in_use: int = 0
    calls: float = 0.0


def _r(value: float) -> float:
    return round(value, 3)


def _rect(kind: str, x: float, z: float, w: float, d: float, **extra) -> dict:
    return {"k": kind, "x": _r(x), "z": _r(z), "w": _r(w), "d": _r(d), **extra}


def scale_of(budget: float) -> float:
    """额度换成 0 ~ 1：10 万是 0，300 万以上是 1，按对数。仓库多大由它定。"""
    if budget <= 0:
        return 0.0
    return max(0.0, min(1.0, math.log10(budget / 1e5) / math.log10(30)))


def house_size(budget: float) -> tuple[float, float, float]:
    """仓库的 (进深, 宽, 高)：额度越大越大，最小的也看得清，最大的也放得进紧凑的一块地（COMPACT 的 lot_d × lot_w）。"""
    s = scale_of(budget)
    return _r(1.8 + 0.8 * s), _r(1.6 + 0.6 * s), _r(1.0 + 1.6 * s)


def office_height(lifetime: float, top: float) -> float:
    """公司楼多高：3 ~ 8 格，按累计消费占最多那个客户的比例开方（差得多的也看得出高矮）。"""
    if top <= 0 or lifetime <= 0:
        return 3.0
    return _r(3.0 + 5.0 * math.sqrt(min(1.0, lifetime / top)))


def workers_of(calls: float, top: float) -> tuple[int, float]:
    """最近一小时有调用的仓库，今天调用多少次 -> 几只小家伙搬、走多快（倍数）。最忙的那个 3 只、走得最快；
    今天还没几次（刚过半夜，今天这一格还是空的）也有 1 只慢慢搬——它最近一小时明明在调用。"""
    if calls <= 0 or top <= 0:
        return 1, 0.8
    share = math.sqrt(min(1.0, calls / top))
    return 1 + round((MAX_WORKERS - 1) * share), _r(0.8 + 0.6 * share)


def blocks_per_side(customers_count: int, lots: int, stock: bool) -> int:
    """正方形的城每边几个街区：公司楼一个客户一个街区，仓库、小公园大约两块地一个街区，再加公园、仓储区；至少 3。"""
    need = customers_count + math.ceil(lots / 2) + 1 + (1 if stock else 0)
    return max(3, math.ceil(math.sqrt(need)))


def _calls_today(card) -> float:
    daily = getattr(card.activity, "daily", None) or []
    return float(daily[-1]) if daily else 0.0


# ---------------------------------------------------------------- 一栋一栋
def _house(card, account, owner_name: str, top_calls: float, link_account: Callable[[str], str]) -> dict:
    stock = not owner_name
    if stock:
        stage = "stock"
    elif account is None:
        stage = "use"
    else:
        stage = customers.stage_of(account, card.total_cost if card.has_numbers else None)
    depth, width, height = house_size(card.budget)
    calls = _calls_today(card)
    # 搬货的小家伙：最近一小时有调用的才有；库存没有公司楼可送，没有
    workers, speed = workers_of(calls, top_calls) if card.state == "active" and not stock else (0, 1.0)
    # 屋顶的牌子：邮箱 @ 前面那段（没填邮箱是号码），太长的截一截；底色按使用率，库存、读不到数的是灰的
    name = card.label if len(card.label) <= SIGN_CHARS else card.label[:SIGN_CHARS - 1] + "…"
    sign = {"text": name, "tone": "none" if stock or not card.has_numbers else card.level}
    status = card.state_label
    if card.activity.sub and card.state != "error":
        status += " · " + card.activity.sub
    owner = owner_name or "库存（还没分给客户）"
    rows = [["客户", owner]]
    if not stock:
        rows.append(["阶段", STAGES.get(stage, stage)])
    rows.append(["额度", money0(card.budget)])
    used = ""
    if card.has_numbers:
        used = money0(card.total_cost)
        if card.usage_pct is not None:
            used += f" · {card.usage_pct:.0f}%"
        rows.append(["已用", used])
    rows.append(["今天调用", f"{calls:,.0f} 次"])
    rows.append(["状态", status])
    total = f"余额 {money0(card.balance)}" if card.has_numbers else "查不到消费：" + (card.issue or "读不到")
    # 点开的那张卡片：大字是余额，下面一条使用率，再下面近 14 天每天的调用次数
    facts = [["额度", money0(card.budget)]]
    if used:
        facts.append(["已用", used])
    facts += [["今天调用", f"{calls:,.0f} 次"]] + ([] if stock else [["阶段", STAGES.get(stage, stage)]]) + [["状态", status]]
    daily = getattr(card.activity, "daily", None) or []
    return {
        "id": card.account, "stage": stage, "state": card.state, "size": [depth, width, height],
        "sign": sign, "workers": workers, "speed": speed, "url": link_account(card.account),
        "tip": {"head": card.email or card.label, "total": total, "rows": rows, "foot": "点一下看特写"},
        "card": {"kind": "house", "title": card.email or card.label, "sub": owner,
                 "big": money0(card.balance) if card.has_numbers else "—", "big_label": "余额",
                 "pct": _r(card.usage_pct) if card.has_numbers and card.usage_pct is not None else None,
                 "tone": card.level if card.has_numbers else "none", "facts": facts,
                 "calls14": [round(value) for value in daily], "link": "进账号页", "url": link_account(card.account)},
    }


def _park(number: str, email: str, label: str, owner: str, settled, spent: float | None,
          link_account: Callable[[str], str]) -> dict:
    rows = [["用了", money0(spent)]] if spent is not None else []
    facts = [["结算", settled.strftime("%Y-%m-%d")]] + ([["一共用了", money0(spent)]] if spent is not None else [])
    return {
        "id": number, "url": link_account(number),
        "tip": {"head": email or label, "total": f"已结算 · {settled.strftime('%Y-%m-%d')}", "rows": rows,
                "foot": "仓库拆了，这块地成了小公园 · 点一下看特写"},
        "card": {"kind": "park", "title": email or label, "sub": owner, "big": "已结算", "big_label": "",
                 "pct": None, "tone": "none", "facts": facts, "calls14": [], "link": "进账号页",
                 "url": link_account(number)},
    }


# ---------------------------------------------------------------- 拼起来
def build(cards, accounts, customer_list, link_account: Callable[[str], str],
          link_customer: Callable[[str], str], link_static: Callable[[str], str]) -> dict:
    """概览上的卡片（dashboard.CardRow，在用的账号）、全部账号（含停用的）、全部客户 -> city.js 要的那一份。"""
    by_key = {a.key: a for a in accounts}
    groups = {c.id: _Group(customer=c) for c in customer_list}
    top_calls = max((_calls_today(card) for card in cards if card.state == "active"), default=0.0)
    stock: list[dict] = []
    for card in cards:
        account = by_key.get(card.key)
        group = groups.get(card.customer)
        if group is not None and card.has_numbers:
            group.lifetime += card.total_cost
        if account is not None and account.settled is not None:
            # 结算了：仓库拆了，是城里的一块小公园（没有客户的结算账号不画）
            if group is not None:
                group.parks.append(_park(card.account, card.email, card.label, group.customer.name, account.settled,
                                         card.total_cost if card.has_numbers else None, link_account))
            continue
        house = _house(card, account, group.customer.name if group else "", top_calls, link_account)
        if group is None:
            stock.append(house)
            continue
        house["office"] = group.customer.id
        group.houses.append(house)
        group.calls += _calls_today(card)
        if house["stage"] == "use":
            group.in_use += 1
            if card.has_numbers:
                group.balance += card.balance
    # 停用的账号：钱算进累计消费（存下来的账），结算了的也是一块小公园
    disabled = [a for a in accounts if not a.enabled and a.customer in groups]
    saved = cost_history.recall_many([a.account for a in disabled])
    for account in disabled:
        group = groups[account.customer]
        spent = sum(saved[account.account].marked) if account.account in saved else None
        if spent:
            group.lifetime += spent
        if account.settled is not None:
            group.parks.append(_park(account.account, account.email, account.label, group.customer.name,
                                     account.settled, spent, link_account))

    # 有公司楼的客户：名下有东西的，加上还在合作、名下暂时没有账号的；按累计消费从多到少
    shown = [g for g in groups.values() if g.houses or g.parks or g.customer.status != "gone"]
    shown.sort(key=lambda g: (-g.lifetime, g.customer.id))
    for group in shown:
        group.houses.sort(key=lambda h: (["use", "risk", "pending"].index(h["stage"])
                                         if h["stage"] in ("use", "risk", "pending") else 3,
                                         -h["workers"], -h["size"][2]))
    top_life = max((g.lifetime for g in shown), default=0.0)
    return _Layout(shown, stock, top_life, link_customer, link_static).result()


class _Layout:
    """把公司楼、仓库、小公园、公园、仓储区摆进一座正方形的城：n × n 个街区，马路、人行道、斑马线、河和桥。"""

    def __init__(self, groups: list[_Group], stock: list[dict], top_life: float,
                 link_customer: Callable[[str], str], link_static: Callable[[str], str]):
        self.groups = groups
        self.top_life = top_life
        self.link_customer = link_customer
        self.link_static = link_static
        self.spacing = ROOMY if len(groups) <= ROOMY_UP_TO else COMPACT
        self.ground: list[dict] = []
        self.offices: list[dict] = []
        self.houses: list[dict] = []
        self.parks: list[dict] = []
        self.trees: list[list[float]] = []
        self.props: list[dict] = []
        inner = self.spacing.block - 2 * WALK
        self.stock_cols = max(1, math.floor((inner - 0.4) / STOCK_LOT_W))
        self.stock_rows = max(1, math.floor((inner - 0.4) / STOCK_ROW))
        room = self.stock_cols * self.stock_rows
        self.stock, self.loose_stock = stock[:room], stock[room:]
        self._arrange()

    # ------------------------------------------------------------ 街区
    def _lots(self) -> list[tuple[str, dict, object]]:
        """要占地的：各个客户的仓库轮流排（一个客户的不会一个挨一个），再是小公园，最后是仓储区放不下的库存。"""
        queues = [list(g.houses) for g in self.groups]
        out: list[tuple[str, dict, object]] = []
        while any(queues):
            for g, queue in zip(self.groups, queues):
                if queue:
                    out.append(("house", queue.pop(0), g))
        out += [("park", park, g) for g in self.groups for park in g.parks]
        out += [("house", house, None) for house in self.loose_stock]
        return out

    def _arrange(self) -> None:
        sp = self.spacing
        lots = self._lots()
        n = blocks_per_side(len(self.groups), len(lots), bool(self.stock))
        self.n = n
        self.river_after = max(0, n // 2 - 1)          # 河在第几列街区的东边（从 0 数）
        verge = (RIVER_GAP - ROAD) / 2                 # 南北两头各多一条草地：东西方向多了一条河，城正好是正方形
        self.xs, x = [], ROAD
        for c in range(n):
            self.xs.append(x)
            x += sp.block + (RIVER_GAP if c == self.river_after else ROAD)
        self.width = self.xs[-1] + sp.block + ROAD
        self.zs = [verge + ROAD + r * (sp.block + ROAD) for r in range(n)]
        self.depth = self.zs[-1] + sp.block + ROAD + verge
        self.verge = verge
        self._streets()

        # 每个街区干什么：东北角仓储区，西南角公园，公司楼从正中间起、一个比一个隔得远，剩下的摆仓库、小公园
        roles: dict[tuple[int, int], tuple[str, object]] = {}
        if self.stock:
            roles[(n - 1, 0)] = ("stock", None)
        roles[(0, n - 1)] = ("park", None)
        free = [(c, r) for r in range(n) for c in range(n) if (c, r) not in roles]
        middle = ((n - 1) / 2, (n - 1) / 2)

        def apart(a, b):
            return abs(a[0] - b[0]) + abs(a[1] - b[1])

        offices: list[tuple[int, int]] = []
        for group in self.groups:
            if not free:
                break
            if offices:
                spot = max(free, key=lambda b: (min(apart(b, o) for o in offices), -apart(b, middle), -b[1], -b[0]))
            else:
                spot = min(free, key=lambda b: (apart(b, middle), b[1], b[0]))
            offices.append(spot)
            free.remove(spot)
            roles[spot] = ("office", group)
        # 仓库、小公园散开：街区按号码打乱一个固定的顺序，一轮一块地轮着放（先都占朝南的，再朝东的……），
        # 同一个客户的尽量不进同一个街区
        free.sort(key=lambda b: zlib.crc32(f"{b[0]},{b[1]},{n}".encode("ascii")))
        taken: dict[tuple[int, int], list[tuple[str, tuple[str, dict, object]]]] = {b: [] for b in free}
        spots = [(side, b) for side in SIDES for b in free]
        for lot in lots[:len(spots)]:
            pick = next((s for s in spots if all(other[2] is not lot[2] for _, other in taken[s[1]])), spots[0])
            spots.remove(pick)
            taken[pick[1]].append((pick[0], lot))
        for (c, r), role in sorted(roles.items(), key=lambda kv: (kv[0][1], kv[0][0])):
            getattr(self, "_" + role[0] + "_block")(role[1], self.xs[c], self.zs[r])
        rank = {g.customer.id: i for i, g in enumerate(self.groups)}
        self.offices.sort(key=lambda o: rank[o["id"]])           # 跟客户一样按累计消费排
        for b in free:
            x0, z0 = self.xs[b[0]], self.zs[b[1]]
            if taken[b]:
                self._lots_block(taken[b], x0, z0)
            else:
                self._trees_block(None, x0, z0)

    def _streets(self) -> None:
        """底下一整块是马路（base），街区凸起来一点（人行道一圈 + 中间草地，_ground_of），河（water）挖下去，
        东西向的马路过河的地方架桥（bridge）。road 是给 city.js 画中间虚线的；cross 是斑马线（dir 是人往哪个方向走）。
        小家伙只走人行道、斑马线、桥、门口的砖地。"""
        sp, n = self.spacing, self.n
        width, depth = self.width, self.depth
        self.ground.append(_rect("base", 0, 0, width, depth))
        river_x = self.xs[self.river_after] + sp.block
        self.river_x = river_x
        road_x = [0.0] + [self.xs[c] + sp.block for c in range(n) if c != self.river_after]
        road_z = [self.zs[0] - ROAD] + [z + sp.block for z in self.zs]
        for z in road_z:
            self.ground.append(_rect("road", 0, z, river_x, ROAD, dir="ew"))
            self.ground.append(_rect("road", river_x + RIVER_GAP, z, width - river_x - RIVER_GAP, ROAD, dir="ew"))
            # 桥 = 马路 + 两边各一条人行道，正好接上两岸街区边上的人行道（小家伙过河走这儿）
            self.ground.append(_rect("bridge", river_x, z - WALK, RIVER_GAP, ROAD + 2 * WALK))
        for x in road_x:
            self.ground.append(_rect("road", x, road_z[0], ROAD, road_z[-1] + ROAD - road_z[0], dir="ns"))
        # 斑马线：每个路口四条（伸到城外的、过河的不画）。有斑马线的路口都有红绿灯，立在路口东北、西南两个拐角上
        # （另两个拐角是路灯）；at 是第几个路口：斑马线、红绿灯都记着，city.js 按它错开各个路口变灯的时候
        edge_x, edge_z = (road_x[0], road_x[-1]), (road_z[0], road_z[-1])
        at = 0
        for x in road_x:
            for z in road_z:
                walks = []
                if x not in edge_x:
                    if z != road_z[0]:
                        walks.append(_rect("cross", x, z - 0.95, ROAD, 0.8, dir="x", at=at))
                    if z != road_z[-1]:
                        walks.append(_rect("cross", x, z + ROAD + 0.15, ROAD, 0.8, dir="x", at=at))
                if z not in edge_z:
                    if x != road_x[0] and abs(x - (river_x + RIVER_GAP)) > 0.01:
                        walks.append(_rect("cross", x - 0.95, z, 0.8, ROAD, dir="z", at=at))
                    if x != road_x[-1] and abs(x + ROAD - river_x) > 0.01:
                        walks.append(_rect("cross", x + ROAD + 0.15, z, 0.8, ROAD, dir="z", at=at))
                if not walks:
                    continue
                self.ground += walks
                if x != road_x[-1] and z != road_z[0]:
                    self.props.append({"k": "signal", "x": _r(x + ROAD + LAMP_INSET), "z": _r(z - LAMP_INSET),
                                       "at": at, "corner": "ne"})
                if x != road_x[0] and z != road_z[-1]:
                    self.props.append({"k": "signal", "x": _r(x - LAMP_INSET), "z": _r(z + ROAD + LAMP_INSET),
                                       "at": at, "corner": "sw"})
                at += 1
        self.ground.append(_rect("water", river_x + 0.5, 0, RIVER_GAP - 1, depth))
        # 河岸、南北两头的草地，种几棵树（桥头空着）
        for z in self.zs:
            self.ground.append(_rect("bank", river_x, z + WALK, 0.5, sp.block - 2 * WALK))
            self.ground.append(_rect("bank", river_x + RIVER_GAP - 0.5, z + WALK, 0.5, sp.block - 2 * WALK))
            for k in range(math.floor(sp.block / 3)):
                for tx, tz, size in ((river_x + 0.25, z + 1.5 + k * 3, 0.55), (river_x + RIVER_GAP - 0.25, z + 2.6 + k * 3, 0.5)):
                    if tz + size <= z + sp.block - WALK:
                        self.trees.append([_r(tx), _r(tz), size])
        for z0 in (0.0, depth - self.verge):
            self.ground.append(_rect("bank", 0, z0, river_x, self.verge))
            self.ground.append(_rect("bank", river_x + RIVER_GAP, z0, width - river_x - RIVER_GAP, self.verge))
            for k in range(math.floor(width / 3.4)):
                tx = 1.7 + k * 3.4
                if not river_x - 0.4 < tx < river_x + RIVER_GAP + 0.4:
                    self.trees.append([_r(tx), _r(z0 + self.verge / 2), 0.45])

    def _ground_of(self, x0: float, z0: float, paved: bool = False) -> None:
        """一个街区的地：边上一圈人行道（四条），中间草地（仓储区是水泥地）；西北、东南两个拐角上各一盏路灯
        （黄昏、夜里亮），每个路口斜对着两盏。"""
        b, w = self.spacing.block, WALK
        self.ground += [_rect("walk", x0, z0, b, w), _rect("walk", x0, z0 + b - w, b, w),
                        _rect("walk", x0, z0 + w, w, b - 2 * w), _rect("walk", x0 + b - w, z0 + w, w, b - 2 * w)]
        self.ground.append(_rect("yard" if paved else "lawn", x0 + w, z0 + w, b - 2 * w, b - 2 * w))
        for lx, lz in ((x0 + LAMP_INSET, z0 + LAMP_INSET), (x0 + b - LAMP_INSET, z0 + b - LAMP_INSET)):
            self.props.append({"k": "lamp", "x": _r(lx), "z": _r(lz)})

    # ------------------------------------------------------------ 公司楼
    def _office_block(self, group: _Group, x0: float, z0: float) -> None:
        sp = self.spacing
        self._ground_of(x0, z0)
        customer = group.customer
        roof = ROOFS[self.groups.index(group) % len(ROOFS)]
        cx = x0 + sp.block / 2
        ox, oz = cx - sp.office_w / 2, z0 + WALK + 0.9
        door = oz + sp.office_d
        # 门前一小片广场通到南边的人行道，两边几棵树、两张长椅
        self.ground.append(_rect("plaza", cx - 1.4, door, 2.8, z0 + sp.block - WALK - door))
        for dx in (-1, 1):
            self.trees.append([_r(cx + dx * (sp.office_w / 2 + 1.0)), _r(oz + 0.9), 0.75])
            self.trees.append([_r(cx + dx * (sp.office_w / 2 + 1.0)), _r(door + 1.3), 0.6])
            self.props.append({"k": "bench", "x": _r(cx + dx * 1.85), "z": _r(door + 1.6), "r": 1 if dx < 0 else 3})
        av = customers.avatar_info(customer)
        avatar = {"img": self.link_static(av.image)} if av.image else {"letter": av.letter, "tone": av.tone}
        counts = f"{group.in_use} 个在用"
        others = len(group.houses) - group.in_use
        if others:
            counts += f" · {others} 个风控或待结算"
        if group.parks:
            counts += f" · {len(group.parks)} 个已结算"
        rows = [["账号", counts]]
        if group.in_use:
            rows.append(["余额合计", money0(group.balance)])
        rows.append(["状态", customer.status_label])
        facts = [["账号", counts]] + ([["余额合计", money0(group.balance)]] if group.in_use else []) + \
                [["今天调用", f"{group.calls:,.0f} 次"], ["状态", customer.status_label]]
        url = self.link_customer(customer.id)
        # 点开时 city.js 把名下每个仓库走到这儿的路铺出来（有小家伙在搬的那几条在动）
        note, foot = "", "点一下看特写"
        if group.houses:
            busy = sum(1 for h in group.houses if h["workers"])
            note = f"地上一路小方块是 {len(group.houses)} 个仓库走到这儿的路"
            if busy:
                note += f"；往这边挪的 {busy} 条，现在有小家伙在搬"
            foot = "点一下看各个仓库走到这儿的路"
        self.offices.append({
            "id": customer.id, "name": customer.name, "x": _r(ox), "z": _r(oz), "w": sp.office_w, "d": sp.office_d,
            "h": office_height(group.lifetime, self.top_life), "roof": roof, "door": [_r(cx), _r(door)],
            "avatar": avatar, "url": url,
            "tip": {"head": customer.name, "total": f"累计消费 {money0(group.lifetime)}", "rows": rows, "foot": foot},
            "card": {"kind": "office", "title": customer.name, "sub": customer.status_label,
                     "big": money0(group.lifetime), "big_label": "累计消费", "pct": None, "tone": "none",
                     "facts": facts, "calls14": [], "note": note, "link": "进客户页", "url": url},
        })

    # ------------------------------------------------------------ 散着的仓库、小公园
    def _slot(self, side: str, x0: float, z0: float) -> tuple[float, float, float, float]:
        """街区里的一块地 (x, z, 宽, 深)：风车一样四个角各一块——朝南的在东南角，朝东的在东北角，朝北的在西北角，
        朝西的在西南角，门都开在那一边的人行道上，四块互相不挨着，中间空一块种树。"""
        sp = self.spacing
        b, w, lw, ld = sp.block, WALK, sp.lot_w, sp.lot_d
        if side == "s":
            return x0 + b - w - lw, z0 + b - w - ld, lw, ld
        if side == "e":
            return x0 + b - w - ld, z0 + w, ld, lw
        if side == "n":
            return x0 + w, z0 + w, lw, ld
        return x0 + w, z0 + b - w - lw, ld, lw

    def _lots_block(self, taken, x0: float, z0: float) -> None:
        sp = self.spacing
        self._ground_of(x0, z0)
        used = set()
        for side, (kind, item, group) in taken:
            used.add(side)
            x, z, w, d = self._slot(side, x0, z0)
            if kind == "park":
                item.update(x=_r(x + 0.15), z=_r(z + 0.15), w=_r(w - 0.3), d=_r(d - 0.3))
                self.parks.append(item)
            else:
                roof = ROOFS[self.groups.index(group) % len(ROOFS)] if group is not None else STOCK_ROOF
                self._place_house(item, side, x, z, w, d, roof)
        for side in SIDES:
            if side not in used:
                x, z, w, d = self._slot(side, x0, z0)
                self.trees.append([_r(x + w / 2), _r(z + d / 2), 0.75])
        self.trees.append([_r(x0 + sp.block / 2), _r(z0 + sp.block / 2), 0.6])

    def _place_house(self, house: dict, side: str, x: float, z: float, w: float, d: float, roof: str) -> None:
        grow = self.spacing.grow
        if grow != 1:
            # 街区大，仓库也大一号（还是放得进一块地，门口留着那一小块砖地）
            house["size"] = [_r(house["size"][0] * grow), _r(house["size"][1] * grow), _r(house["size"][2] * (1 + (grow - 1) * 2 / 3))]
        depth, wide, _ = house["size"]
        if side == "s":
            front, mid = z + d - APRON, x + w / 2
            house.update(x=_r(mid - wide / 2), z=_r(front - depth), w=wide, d=depth, face="s", door=[_r(mid), _r(front)])
            self.ground.append(_rect("plaza", mid - 0.5, front, 1.0, APRON))
        elif side == "n":
            front, mid = z + APRON, x + w / 2
            house.update(x=_r(mid - wide / 2), z=_r(front), w=wide, d=depth, face="n", door=[_r(mid), _r(front)])
            self.ground.append(_rect("plaza", mid - 0.5, z, 1.0, APRON))
        elif side == "e":
            front, mid = x + w - APRON, z + d / 2
            house.update(x=_r(front - depth), z=_r(mid - wide / 2), w=depth, d=wide, face="e", door=[_r(front), _r(mid)])
            self.ground.append(_rect("plaza", front, mid - 0.5, APRON, 1.0))
        else:
            front, mid = x + APRON, z + d / 2
            house.update(x=_r(front), z=_r(mid - wide / 2), w=depth, d=wide, face="w", door=[_r(front), _r(mid)])
            self.ground.append(_rect("plaza", x, mid - 0.5, APRON, 1.0))
        house["roof"] = roof
        self.houses.append(house)

    # ------------------------------------------------------------ 公园、仓储区、空着的街区
    def _park_block(self, _item, x0: float, z0: float) -> None:
        b = self.spacing.block
        self._ground_of(x0, z0)
        cx, cz = x0 + b / 2, z0 + b / 2
        self.ground.append(_rect("plaza", cx - 2, cz - 2, 4, 4))
        self.props.append({"k": "fountain", "x": _r(cx), "z": _r(cz)})
        for dx, dz, turn in ((-1.6, 0, 3), (1.6, 0, 1), (0, -1.6, 0), (0, 1.6, 2)):      # 都面朝喷泉
            self.props.append({"k": "bench", "x": _r(cx + dx), "z": _r(cz + dz), "r": turn})
        for qx in (-1, 1):
            for qz in (-1, 1):
                bx, bz = cx + qx * b * 0.3, cz + qz * b * 0.3
                self.trees.append([_r(bx), _r(bz), 0.85])
                self.props.append({"k": "flowers", "x": _r(cx + qx * 2.6), "z": _r(cz + qz * 0.9)})

    def _stock_block(self, _item, x0: float, z0: float) -> None:
        self._ground_of(x0, z0, paved=True)
        inner = self.spacing.block - 2 * WALK
        left = x0 + WALK + (inner - self.stock_cols * STOCK_LOT_W) / 2
        for n, house in enumerate(self.stock):
            row, col = divmod(n, self.stock_cols)
            lot_x = left + col * STOCK_LOT_W
            lot_z = z0 + WALK + 0.2 + row * STOCK_ROW
            depth_h, width_h, _ = house["size"]
            # 仓储区的地窄一点：宽、进深都按地收一收，门朝南
            w = min(width_h + 0.2, STOCK_LOT_W - 0.35)
            d = min(depth_h, STOCK_ROW - 0.6 - 0.2)
            front = lot_z + STOCK_ROW - 0.6
            cx = lot_x + STOCK_LOT_W / 2
            house.update(x=_r(cx - w / 2), z=_r(front - d), w=_r(w), d=_r(d), face="s", door=[_r(cx), _r(front)],
                         roof=STOCK_ROOF)
            self.houses.append(house)
        rows = math.ceil(len(self.stock) / self.stock_cols)
        spare = inner - 0.2 - rows * STOCK_ROW
        if spare >= 1.4:
            for col in range(2):
                self.props.append({"k": "crates", "x": _r(x0 + WALK + inner * (0.3 + 0.4 * col)),
                                   "z": _r(z0 + self.spacing.block - WALK - spare / 2)})

    def _trees_block(self, _item, x0: float, z0: float) -> None:
        b = self.spacing.block
        self._ground_of(x0, z0)
        colors = ("#c97a5d", "#7f97b5", "#d4a85c")
        spots = ((0.16, 0.18), (0.56, 0.22), (0.24, 0.62))
        seed = zlib.crc32(f"{x0},{z0}".encode("ascii"))
        for n, (fx, fz) in enumerate(spots):
            self.props.append({"k": "home", "x": _r(x0 + b * fx), "z": _r(z0 + b * fz), "w": 2.2, "d": 1.8, "h": 1.3,
                               "c": colors[(seed + n) % len(colors)]})
        for fx, fz, size in ((0.82, 0.2, 0.8), (0.78, 0.55, 0.65), (0.6, 0.82, 0.9), (0.85, 0.82, 0.7), (0.15, 0.85, 0.6)):
            self.trees.append([_r(x0 + b * fx), _r(z0 + b * fz), size])

    def result(self) -> dict:
        return {
            "size": [_r(self.width), _r(self.depth)],
            "blocks": self.n,
            "ground": self.ground,
            "offices": self.offices,
            "houses": self.houses,
            "parks": self.parks,
            "trees": self.trees,
            "props": self.props,
        }
