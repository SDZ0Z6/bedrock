"""客户：账号在客户名下的阶段、客户的钱、时间线、月度对账单。只有计算，不碰 Flask，方便单独测。

**谁是谁的**：台账的 CUSTOMER 列写着账号属于哪个客户（空着 = 库存），一个客户可以有多个账号。
账号在客户名下走一遍：

    库存 → 使用中 → 风控 · 待替换 → 待结算 → 已结算

阶段不单独存，按台账和消费算（stage_of）：SETTLED 有日期是已结算；生命周期里有「结算」、或者额度
用完了是待结算；有「风控」是风控 · 待替换；其余是使用中。

**钱**：客户的预算、消费、余额**只算使用中的账号**。账号一被标记风控、换下或者额度用完，就从这三个数
里一起出去，它用掉的钱留在「历史消费」里——所以预算 − 消费 = 余额任何时候都对得上，结算只是确认线下
都对完了，不改数字。「还能用几天」= 余额 ÷ 近 7 天（不含今天）的日均。

**时间线**：有人做的事（分配、替换、结算、调整额度……）存在台账的 EVENTS 表里；按数据就能看出来的事
（开始上量、上量终止、恢复上量、额度到了 70% / 90% / 用完、AWS 发来的风控邮件）不存，每次按每天的
消费和告警流水现算。自动事件可以改日期、删掉，改动存在 EVENTS 表里（见 excel_source.EVENT_HEADER）。

**月度对账单**：按月是「月初余额 + 新分配 + 调整额度 − 消费 − 移出 = 月末余额」，移出是账号不算了
那天它还没用完的额度；按账号是每个账号每个月的消费。两种看法都只用每天的消费和台账算，最后一个月的
月末余额和页面上的余额是同一个数。
"""

from __future__ import annotations

import math
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from functools import cached_property

from . import config, cost_history, events as alert_events, usage_explorer
from .dates import cumulative_range, parse_date
from .excel_source import (
    CUSTOMER_AVATARS,
    CUSTOMER_STATUSES,
    NOTE_TYPES,
    TAG_RISK,
    TAG_SETTLE,
    Account,
    Customer,
    CustomerEvent,
)

# ---------------------------------------------------------------- 国家 / 地区
# 代码 -> 名字。顺序就是新建客户时的排列：常用的在前
REGIONS = [
    ("CN", "中国大陆"), ("HK", "中国香港"), ("MO", "中国澳门"), ("TW", "中国台湾"),
    ("SG", "新加坡"), ("MY", "马来西亚"), ("JP", "日本"), ("KR", "韩国"),
    ("TH", "泰国"), ("VN", "越南"), ("ID", "印度尼西亚"), ("PH", "菲律宾"),
    ("IN", "印度"), ("AE", "阿联酋"), ("SA", "沙特阿拉伯"), ("US", "美国"),
    ("CA", "加拿大"), ("GB", "英国"), ("DE", "德国"), ("FR", "法国"),
    ("NL", "荷兰"), ("AU", "澳大利亚"), ("OTHER", "其他地区"),
]
REGION_NAMES = dict(REGIONS)


def _star(cx: float, cy: float, r: float, turn: float = -90.0) -> str:
    points = []
    for i in range(10):
        angle = math.radians(turn + 36 * i)
        radius = r if i % 2 == 0 else r * 0.382
        points.append(f"{cx + radius * math.cos(angle):.2f},{cy + radius * math.sin(angle):.2f}")
    return f'<polygon points="{" ".join(points)}"/>'


def _stripes(colors: list[str], vertical: bool = False) -> str:
    out = []
    step = (30 if vertical else 20) / len(colors)
    for i, color in enumerate(colors):
        if vertical:
            out.append(f'<rect x="{i * step:.2f}" width="{step + 0.05:.2f}" height="20" fill="{color}"/>')
        else:
            out.append(f'<rect y="{i * step:.2f}" width="30" height="{step + 0.05:.2f}" fill="{color}"/>')
    return "".join(out)


def _union_jack(width: float = 30, height: float = 20) -> str:
    return (
        f'<rect width="{width}" height="{height}" fill="#012169"/>'
        f'<path d="M0 0L{width} {height}M{width} 0L0 {height}" stroke="#fff" stroke-width="{height * 0.2:.2f}"/>'
        f'<path d="M0 0L{width} {height}M{width} 0L0 {height}" stroke="#c8102e" stroke-width="{height * 0.07:.2f}"/>'
        f'<path d="M{width / 2} 0V{height}M0 {height / 2}H{width}" stroke="#fff" stroke-width="{height * 0.3:.2f}"/>'
        f'<path d="M{width / 2} 0V{height}M0 {height / 2}H{width}" stroke="#c8102e" stroke-width="{height * 0.17:.2f}"/>'
    )


# 国旗是示意画法（30×20），只求一眼认得出，不求比例和细节分毫不差
FLAGS = {
    "CN": '<rect width="30" height="20" fill="#de2910"/><g fill="#ffde00">' + _star(5.5, 5.5, 3)
          + _star(10.5, 2.2, 1, -70) + _star(12.6, 4.4, 1, -40) + _star(12.6, 7.4, 1, -10)
          + _star(10.5, 9.4, 1, 20) + "</g>",
    "HK": '<rect width="30" height="20" fill="#de2910"/><g fill="#fff">'
          + "".join(f'<ellipse cx="15" cy="6.4" rx="2" ry="3.6" transform="rotate({a} 15 10)"/>'
                    for a in (0, 72, 144, 216, 288)) + "</g>",
    "MO": '<rect width="30" height="20" fill="#00785e"/><path d="M10.5 12.5q4.5-6 9 0q-4.5 2.4-9 0z" fill="#fff"/>'
          '<path d="M10 14.2h10M11 15.6h8" stroke="#fff" stroke-width=".8"/><g fill="#fbd116">'
          + "".join(_star(15 + 5.5 * math.cos(math.radians(a)), 9.5 + 5.5 * math.sin(math.radians(a)), .9)
                    for a in (-150, -120, -90, -60, -30)) + "</g>",
    "TW": '<rect width="30" height="20" fill="#fe0000"/><rect width="15" height="10" fill="#000095"/>'
          '<circle cx="7.5" cy="5" r="3" fill="#fff"/><circle cx="7.5" cy="5" r="1.9" fill="#000095"/>'
          '<circle cx="7.5" cy="5" r="1.5" fill="#fff"/>',
    "SG": '<rect width="30" height="10" fill="#ef3340"/><rect y="10" width="30" height="10" fill="#fff"/>'
          '<circle cx="6.6" cy="5" r="3.5" fill="#fff"/><circle cx="8" cy="5" r="3.3" fill="#ef3340"/><g fill="#fff">'
          + "".join(_star(10.6 + 1.7 * math.cos(math.radians(-90 + 72 * i)),
                          5 + 1.7 * math.sin(math.radians(-90 + 72 * i)), .62) for i in range(5)) + "</g>",
    "MY": _stripes(["#cc0001", "#fff"] * 7) + '<rect width="15" height="11.43" fill="#010066"/>'
          '<circle cx="6" cy="5.7" r="3.6" fill="#fc0"/><circle cx="7.1" cy="5.7" r="3" fill="#010066"/>'
          '<g fill="#fc0">' + _star(11.2, 5.7, 2.2) + "</g>",
    "JP": '<rect width="30" height="20" fill="#fff"/><circle cx="15" cy="10" r="6" fill="#bc002d"/>',
    "KR": '<rect width="30" height="20" fill="#fff"/><path d="M10 10a5 5 0 0 1 10 0z" fill="#cd2e3a"/>'
          '<path d="M10 10a5 5 0 0 0 10 0z" fill="#0047a0"/><g stroke="#000" stroke-width="1.1">'
          '<path d="M4 4.2l3-2M4.8 5.2l3-2M5.6 6.2l3-2M22.4 13.8l3 2M23.2 12.8l3 2M24 11.8l3 2'
          'M22.4 6.2l3-2M24 4.2l3-2M4 15.8l3 2M5.6 13.8l3 2"/></g>',
    "TH": _stripes(["#a51931", "#f4f5f8", "#2d2a4a", "#2d2a4a", "#f4f5f8", "#a51931"]),
    "VN": '<rect width="30" height="20" fill="#da251d"/><g fill="#ff0">' + _star(15, 10.4, 5.4) + "</g>",
    "ID": '<rect width="30" height="10" fill="#ce1126"/><rect y="10" width="30" height="10" fill="#fff"/>',
    "PH": '<rect width="30" height="10" fill="#0038a8"/><rect y="10" width="30" height="10" fill="#ce1126"/>'
          '<path d="M0 0L17.3 10L0 20z" fill="#fff"/><circle cx="6" cy="10" r="2.6" fill="#fcd116"/>',
    "IN": _stripes(["#ff9933", "#fff", "#138808"]) + '<circle cx="15" cy="10" r="2.6" fill="none" '
          'stroke="#000080" stroke-width=".7"/><circle cx="15" cy="10" r=".7" fill="#000080"/>',
    "AE": _stripes(["#00732f", "#fff", "#000"]) + '<rect width="8" height="20" fill="#ff0000"/>',
    "SA": '<rect width="30" height="20" fill="#006c35"/><path d="M8 8.6h14M9 10.4h12" stroke="#fff" '
          'stroke-width=".9"/><path d="M9 13.6h11l1-.8" stroke="#fff" stroke-width=".9" fill="none"/>',
    "US": '<rect width="30" height="20" fill="#fff"/>'
          + "".join(f'<rect y="{i * 20 / 13:.2f}" width="30" height="{20 / 13:.2f}" fill="#b22234"/>'
                    for i in range(0, 13, 2)) + '<rect width="13" height="10.77" fill="#3c3b6e"/>',
    "CA": '<rect width="30" height="20" fill="#fff"/><rect width="7.5" height="20" fill="#d80621"/>'
          '<rect x="22.5" width="7.5" height="20" fill="#d80621"/><path d="M15 4.2l1.1 2.2 1.6-.6-.6 3.4 1.9-1.6'
          ' .5 1.2 1.8-.3-.7 2.2.9.5-3.1 2.2.3 1.2-3.3-.4.1 3.4h-1l.1-3.4-3.3.4.3-1.2-3.1-2.2.9-.5-.7-2.2 1.8.3'
          ' .5-1.2 1.9 1.6-.6-3.4 1.6.6z" fill="#d80621"/>',
    "GB": _union_jack(),
    "DE": _stripes(["#000", "#dd0000", "#ffce00"]),
    "FR": _stripes(["#002395", "#fff", "#ed2939"], vertical=True),
    "NL": _stripes(["#ae1c28", "#fff", "#21468b"]),
    "AU": '<rect width="30" height="20" fill="#012169"/><svg width="15" height="10" viewBox="0 0 30 20">'
          + _union_jack() + '</svg><g fill="#fff">' + _star(7.5, 15, 2.2) + _star(22.5, 15.5, 1.1)
          + _star(19.5, 9, 1.1) + _star(22.5, 4, 1.1) + _star(25.5, 8, 1.1) + "</g>",
}


def flag_svg(code: str) -> str:
    """国旗（示意）。认不出的代码画一个灰色的地球。"""
    inner = FLAGS.get((code or "").upper())
    if inner is None:
        inner = ('<rect width="30" height="20" fill="#e8e6dc"/><circle cx="15" cy="10" r="5.5" fill="none" '
                 'stroke="#87867f" stroke-width="1.1"/><path d="M9.5 10h11M15 4.5c2 1.6 2.9 3.4 2.9 5.5S17 13.9 15 15.5'
                 'c-2-1.6-2.9-3.4-2.9-5.5S13 6.1 15 4.5z" fill="none" stroke="#87867f" stroke-width=".9"/>')
    return f'<svg class="flag" viewBox="0 0 30 20" aria-hidden="true">{inner}</svg>'


def region_name(code: str) -> str:
    return REGION_NAMES.get((code or "").upper(), "") if code else ""


# ---------------------------------------------------------------- 头像
# 没选插画头像的客户：名字的第一个字，底色从 10 个插画头像的背景色里挑（style.css 的 .lt-0 … .lt-5），
# 按客户编号定——改名字不会变色
LETTER_TONES = 6


def letter_of(name: str) -> str:
    """名字的第一个字；英文字母转大写。"""
    for char in (name or "").strip():
        if not char.isspace():
            return char.upper() if char.isascii() and char.isalpha() else char
    return "?"


def tone_of(ident: str) -> int:
    value = 0
    for char in ident or "":
        value = (value * 31 + ord(char)) & 0xFFFFFFFF
    return value % LETTER_TONES


def avatar_url_name(number: int, deco: bool = False) -> str:
    """static 下插画头像的文件名：avatars/c06.webp；deco 是客户页大头像用的、带左上角小装饰的那张。"""
    return f"avatars/c{number:02d}{'-deco' if deco else ''}.webp"


def avatar_info(customer):
    """模板里画客户头像要的：插画（image / deco 两张图）或者名字的第一个字（letter、tone）。"""
    from types import SimpleNamespace
    if customer.avatar:
        return SimpleNamespace(image=avatar_url_name(customer.avatar), deco=avatar_url_name(customer.avatar, deco=True),
                               letter="", tone=0)
    return SimpleNamespace(image="", deco="", letter=letter_of(customer.name), tone=tone_of(customer.id))


def customer_of(ident: str):
    """客户编号 -> 客户（读台账缓存，几乎不花时间）。没有这个客户、台账读不了都是 None。"""
    if not ident:
        return None
    from .excel_source import load_customers
    try:
        return next((c for c in load_customers() if c.id == ident), None)
    except Exception:
        return None


# ---------------------------------------------------------------- 客户资料的表单
MAX_NAME = 40
MAX_NOTE = 500


def validate_customer(form, others: list[Customer], today: date | None = None) -> tuple[dict, list[str]]:
    """新建 / 修改客户的表单 -> (清洗后的字段, 错误列表)。others 是除自己以外的客户（查重名用）。"""
    today = today or date.today()
    errors: list[str] = []
    name = " ".join(str(form.get("name") or "").split())
    if not name:
        errors.append("客户名字不能为空。")
    elif len(name) > MAX_NAME:
        errors.append(f"客户名字最多 {MAX_NAME} 个字。")
    elif any(other.name.lower() == name.lower() for other in others):
        errors.append(f"已经有叫「{name}」的客户了。")

    region = str(form.get("region") or "").strip().upper()
    if region and region not in REGION_NAMES:
        errors.append("不认识这个国家 / 地区，请从列表里选。")

    raw_avatar = str(form.get("avatar") or "0").strip()
    avatar = int(raw_avatar) if raw_avatar.isdecimal() else -1
    if not 0 <= avatar <= CUSTOMER_AVATARS:
        errors.append("头像不对，请从列表里选。")
        avatar = 0

    status = str(form.get("status") or "on").strip()
    if status not in CUSTOMER_STATUSES:
        errors.append("状态不对，请重新选。")
        status = "on"

    raw_since = str(form.get("since") or "").strip()
    since = parse_date(raw_since) if raw_since else today
    if since is None:
        errors.append("开始合作的日期认不出来，填成 2026-09-01 这样的格式。")
    elif since > today:
        errors.append("开始合作的日期不能晚于今天。")

    note = str(form.get("note") or "").strip()
    if len(note) > MAX_NOTE:
        errors.append(f"备注最多 {MAX_NOTE} 个字。")

    return {"name": name, "region": region, "avatar": avatar, "status": status, "since": since,
            "note": note}, errors


# ---------------------------------------------------------------- 阶段
STAGES = {
    "use": ("使用中", "ok"),
    "risk": ("风控 · 待替换", "danger"),
    "pending": ("待结算", "warn"),
    "settled": ("已结算", "none"),
}
STAGE_ORDER = {"risk": 0, "pending": 1, "use": 2, "settled": 3}
# 客户页「名下账号」表格：使用中的在上面（STAGE_ORDER 是别处用的：替换弹窗默认选中风控的那个）
TABLE_ORDER = {"use": 0, "risk": 1, "pending": 2, "settled": 3}
# 表格上面按阶段筛选的签
STAGE_SHORT = {"use": "使用中", "risk": "风控", "pending": "待结算", "settled": "已结算"}


def stage_of(account: Account, spent: float | None = None) -> str:
    """账号在客户名下的阶段。spent 是累计的折算后消费，用来看额度用完没有；不知道就是 None。"""
    if account.settled is not None:
        return "settled"
    if TAG_SETTLE in account.lifecycle:
        return "pending"
    if spent is not None and account.budget > 0 and spent >= account.budget:
        return "pending"
    if TAG_RISK in account.lifecycle:
        return "risk"
    return "use"


# ---------------------------------------------------------------- 每天的消费
@dataclass
class Series:
    dates: list[str] = field(default_factory=list)
    raw: list[float] = field(default_factory=list)
    marked: list[float] = field(default_factory=list)
    error: object | None = None          # aws_errors.QueryError
    stale_as_of: date | None = None      # 查询失败，金额是存下来的、截至这一天


def fetch_series(accounts: list[Account], today: date, refresh: bool = False) -> dict[str, Series]:
    """每个账号从启用日期（或 CE 最早能查到的那天）到今天每天的消费（AWS 原价和折算后），并发查。

    停用的账号不查 CE，用存下来的历史（cost_history）；一次都没存过的才查一次。查询失败的用存下来的顶着。
    """
    def one(account: Account) -> tuple[Series, bool]:
        if not account.enabled:
            saved = cost_history.recall(account.account)
            if saved is not None:
                return Series(saved.dates, saved.raw, saved.marked), False
        start, end, _ = cumulative_range(account.start_date, today)
        dates, raw, marked, cached, error = usage_explorer.account_series(account, start, end, "daily", refresh)
        if error is None:
            return Series(dates, raw, marked), not cached
        saved = cost_history.recall(account.account)
        if saved is not None:
            return Series(saved.dates, saved.raw, saved.marked, error, saved.as_of), False
        return Series(error=error), False

    if not accounts:
        return {}
    workers = max(1, min(config.MAX_WORKERS, len(accounts)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(one, accounts))
    out = {}
    fresh_ones = []
    for account, (series, fresh) in zip(accounts, results):
        out[account.key] = series
        if fresh and series.dates:
            fresh_ones.append((account.account, series.dates, series.raw, series.marked))
    cost_history.remember_many(fresh_ones)
    return out


# ---------------------------------------------------------------- 一个客户名下的一个账号
@dataclass
class Holding:
    account: Account
    series: Series
    stage: str = "use"
    joined: date | None = None        # 分给这个客户的那天
    left: date | None = None          # 从这天起不算进余额（风控 / 换下 / 用完 / 结算）
    left_why: str = ""
    via: str = ""                     # 是替换哪个账号换上来的（号码）
    budget_changes: list[tuple[date, float]] = field(default_factory=list)   # (哪天, 改了多少)

    @property
    def key(self) -> str:
        return self.account.key

    @property
    def number(self) -> str:
        return self.account.account

    @cached_property
    def cumulative(self) -> list[float]:
        total, out = 0.0, []
        for value in self.series.marked:
            total += value
            out.append(total)
        return out

    @property
    def spent(self) -> float:
        return self.cumulative[-1] if self.cumulative else 0.0

    @property
    def raw_spent(self) -> float:
        return sum(self.series.raw)

    @property
    def has_numbers(self) -> bool:
        return bool(self.series.dates)

    @property
    def remaining(self) -> float:
        return self.account.budget - self.spent

    @property
    def usage_pct(self) -> float | None:
        return self.spent / self.account.budget * 100 if self.account.budget > 0 else None

    @property
    def level(self) -> str:
        pct = self.usage_pct
        if pct is None:
            return "none"
        return "danger" if pct >= config.DANGER_PCT else "warn" if pct >= config.WARN_PCT else "ok"

    @property
    def stage_label(self) -> str:
        return STAGES[self.stage][0]

    @property
    def stage_tone(self) -> str:
        return STAGES[self.stage][1]

    def budget_on(self, day: date) -> float:
        """那一天的额度：现在的额度减去那天之后加的。"""
        return self.account.budget - sum(delta for when, delta in self.budget_changes if when > day)

    def spent_through(self, day: date) -> float:
        """到那一天（含）为止的累计消费。"""
        iso = day.isoformat()
        total = 0.0
        for when, value in zip(self.series.dates, self.cumulative):
            if when > iso:
                break
            total = value
        return total

    @cached_property
    def used_up_on(self) -> date | None:
        """累计消费第一次到额度的那天（按当时的额度）。"""
        for when, value in zip(self.series.dates, self.cumulative):
            day = date.fromisoformat(when)
            budget = self.budget_on(day)
            if budget > 0 and value >= budget:
                return day
        return None

    def daily(self, start: date, end: date) -> list[float]:
        """start 到 end 每天的折算后消费（没有数的天是 0）。"""
        values = dict(zip(self.series.dates, self.series.marked))
        span = (end - start).days + 1
        return [values.get((start + timedelta(days=i)).isoformat(), 0.0) for i in range(max(0, span))]


# ---------------------------------------------------------------- 时间线
# 事件类别：名字、筛选分组、颜色（style.css 的 --k-*）、图标（customer.html 里的 icon 宏）
EVENT_TYPES = {
    "signup": ("开始合作", "misc", "misc", "user"),
    "start": ("启用账号", "acct", "acct", "power"),      # 账号页的时间线才有：台账里的启用日期
    "assign": ("分配账号", "acct", "acct", "assign"),
    "unassign": ("解绑", "acct", "misc", "back"),
    "budget": ("调整额度", "money", "money", "budget"),
    "rampup": ("开始上量", "use", "up", "trend"),
    "resume": ("恢复上量", "use", "up", "play"),
    "stop": ("上量终止", "use", "warn", "pause"),
    "quota": ("额度预警", "use", "warn", "gauge"),
    "mail": ("AWS 邮件", "risk", "risk", "mail"),
    "risk": ("标记风控", "risk", "risk", "flag"),
    "unrisk": ("取消风控", "risk", "misc", "flag"),
    "replace": ("替换账号", "acct", "acct", "swap"),
    "settle": ("结算", "money", "money", "check"),
    "disable": ("停用", "acct", "misc", "power"),
    "enable": ("恢复启用", "acct", "misc", "power"),
    "note": ("备注", "misc", "misc", "note"),
}
CATEGORIES = [("acct", "账号"), ("use", "用量"), ("risk", "风控"), ("money", "结算、额度"), ("misc", "其他")]
# 「记一笔」能记的几类
MANUAL_KINDS = NOTE_TYPES
# 「记一笔」弹窗里的类型：上面几类只是记下来；「标记风控」和名下账号里的「标记风控」一样，生命周期换成风控、
# 从这天起不算进余额（所以一定要选账号）
NOTE_KINDS = MANUAL_KINDS + ("risk",)
# 「还能用几天」：日均不到半分钱就当最近没有消费；超过一年的不写具体天数和日期。账号刚风控、只剩零星
# 几分钱的时候，余额 ÷ 日均是天文数字，加到今天上连日期都放不下（线上出过 OverflowError）
MIN_DAILY = 0.005
LONG_RUNWAY_DAYS = 365
# 同一天的几件事按这个顺序排；同一组里的（标记 / 取消风控、停用 / 恢复）按记下来的先后
_TYPE_ORDER = {kind: position for position, group in enumerate((
    ("signup",), ("start",), ("assign",), ("budget",), ("rampup",), ("resume",), ("quota",), ("stop",), ("mail",),
    ("risk", "unrisk"), ("replace",), ("settle",), ("disable", "enable"), ("unassign",), ("note",),
)) for kind in group}
# 告警流水里算「AWS 风控邮件」的几类（mail_rules 的 abuse / compromised / suspended）
RISK_MAIL_KINDS = ("mail-abuse", "mail-compromised", "mail-suspended")


@dataclass
class Item:
    """时间线上的一件事。"""

    ident: str                  # 手动的是编号（"12"），自动的是 "auto:<key>"
    date: date
    kind: str
    title: str
    text: str = ""
    account: str = ""           # 12 位号码
    peer: str = ""
    source: str = "manual"      # manual / auto
    actor: str = ""
    created_at: datetime | None = None
    tone: str = ""              # 颜色，空着按类别
    order: int = 0
    customer: str = ""          # 那时候归哪个客户（账号页的时间线用；空着 = 库存）

    @property
    def category(self) -> str:
        return EVENT_TYPES.get(self.kind, EVENT_TYPES["note"])[1]

    @property
    def color(self) -> str:
        return self.tone or EVENT_TYPES.get(self.kind, EVENT_TYPES["note"])[2]

    @property
    def icon(self) -> str:
        return EVENT_TYPES.get(self.kind, EVENT_TYPES["note"])[3]

    @property
    def auto(self) -> bool:
        return self.source == "auto"


def _money(value: float | None) -> str:
    if value is None:
        return "—"
    sign = "-" if value < 0 else ""
    return f"{sign}{config.CURRENCY_SYMBOL}{abs(value):,.0f}"


def short_money(value: float) -> str:
    return _short(value)


def _short(value: float) -> str:
    """额度的短写法：$1M、$1.5M、$500K。"""
    from .chart import compact_money
    text = compact_money(value, config.CURRENCY_SYMBOL)
    match = re.match(r"^(\D*)(\d+(?:\.\d+)?)([KMB]?)$", text)
    if not match:
        return text
    head, number, unit = match.groups()
    if "." in number:
        number = number.rstrip("0").rstrip(".")
    return f"{head}{number}{unit}"


def _manual_item(event: CustomerEvent, partners: dict[str, str]) -> Item:
    name = EVENT_TYPES.get(event.type, EVENT_TYPES["note"])[0]
    note = event.note
    text = note
    if event.type == "assign":
        bits = [f"额度 {_money(event.amount)}" if event.amount is not None else ""]
        partner = partners.get(event.account)
        if partner:
            bits.append(f"上游 {partner}")
        text = " · ".join(bit for bit in bits if bit)
    elif event.type == "budget":
        if event.before is not None and event.amount is not None:
            delta = event.amount - event.before
            text = f"{_money(event.before)} → {_money(event.amount)}（{'+' if delta >= 0 else '−'}{_money(abs(delta))}）"
        if note:
            text = f"{text} · {note}" if text else note
    elif event.type == "risk":
        text = " · ".join(bit for bit in (f"已用 {_money(event.amount)}" if event.amount is not None else "", note) if bit)
    elif event.type == "unrisk":
        text = note or "回到使用中"
    elif event.type == "replace":
        bits = [note, f"上游补发 {_short(event.amount)}" if event.amount else ""]
        text = " · ".join(bit for bit in bits if bit)
    elif event.type == "settle":
        bits = [f"消费 {_money(event.amount)}" if event.amount is not None else "", note]
        text = " · ".join(bit for bit in bits if bit)
    elif event.type == "unassign":
        text = " · ".join(bit for bit in ("回到库存", note) if bit)
    elif event.type == "disable":
        text = note or "不再查 Cost Explorer"
    elif event.type == "enable":
        text = note or "又开始查 Cost Explorer"
    elif event.type in ("rampup", "resume", "stop") and not note:
        text = "手动记的"
    return Item(
        ident=str(event.id), date=event.date, kind=event.type if event.type in EVENT_TYPES else "note",
        title=name, text=text, account=event.account, peer=event.peer, source="manual",
        actor=event.actor, created_at=event.created_at, order=event.id, customer=event.customer,
    )


def item_payload(item: Item) -> dict:
    """时间线上一件事交给 static/timeline.js 的那部分（卡片上那一行是谁由页面自己补：who / peer / none / keys）。"""
    return {
        "id": item.ident, "date": item.date.isoformat(), "kind": item.kind, "title": item.title, "text": item.text,
        "cat": item.category, "color": item.color, "icon": item.icon, "auto": item.auto, "actor": item.actor,
        "created": item.created_at.strftime("%Y-%m-%d %H:%M:%S") if item.created_at else "",
    }


def account_face(number: str, by_number: dict[str, Account]) -> dict | None:
    """时间线卡片上的一个账号：号码、邮箱（没填就是号码）、小头像。台账里已经没有的号码也画得出来。"""
    if not number:
        return None
    account = by_number.get(number)
    if account is None:
        return {"number": number, "label": number, "av": {"text": number[:1], "css": "av-7"}}
    return {"number": number, "label": account.email or number,
            "av": {"text": account.avatar.text, "css": account.avatar.css}}


def auto_items(holding: Holding, today: date, mails: list | None = None) -> list[Item]:
    """按每天的消费和告警流水能看出来的事。key 里带着账号和原本的日期，改动（改日期、删掉）按它认。"""
    items: list[Item] = []
    number = holding.number
    series = holding.series
    joined = holding.joined
    # 最近两天 Cost Explorer 还没出完账，看起来像「没量」其实只是没出数：判断上量终止不看这两天
    settled_day = (today - timedelta(days=2)).isoformat()

    def add(kind: str, day: str, title: str, text: str, suffix: str = "", tone: str = "") -> None:
        key = f"{kind}{suffix}:{number}:{day}"
        items.append(Item(ident=f"auto:{key}", date=date.fromisoformat(day), kind=kind, title=title, text=text,
                          account=number, source="auto", tone=tone))

    # 账号不算了（风控、换下、用完、结算）以后，它没量是意料之中的事，不再记上量、终止
    left = holding.left.isoformat() if holding.left else None
    state, low_start, low_run = "idle", "", 0
    for day, value in zip(series.dates, series.marked):
        if joined is not None and day < joined.isoformat():
            continue
        if left is not None and day >= left:
            break
        if state in ("idle", "stopped"):
            if value >= config.RAMPUP_DAILY:
                kind = "rampup" if state == "idle" else "resume"
                add(kind, day, EVENT_TYPES[kind][0], f"当天消费 {_money(value)}")
                state, low_run = "active", 0
        elif day <= settled_day:
            if value < config.STOP_DAILY:
                low_run += 1
                if low_run == 1:
                    low_start = day
                if low_run >= config.STOP_DAYS:
                    add("stop", low_start, "上量终止", f"连续 {config.STOP_DAYS} 天低于 {_money(config.STOP_DAILY)}")
                    state, low_run = "stopped", 0
            else:
                low_run = 0

    # 额度预警：累计消费第一次到 70% / 90% / 用完（按当时的额度）；加了额度又回到线下的，可以再报一次
    marks = sorted({float(config.WARN_PCT), float(config.DANGER_PCT), 100.0})
    fired: set[float] = set()
    for day, total in zip(series.dates, holding.cumulative):
        budget = holding.budget_on(date.fromisoformat(day))
        if budget <= 0:
            continue
        pct = total / budget * 100
        for mark in list(fired):
            if pct < mark:
                fired.discard(mark)
        crossed = [mark for mark in marks if pct >= mark and mark not in fired]
        if not crossed:
            continue
        fired.update(crossed)
        if joined is not None and day < joined.isoformat():
            continue
        if left is not None and day > left:
            break
        top = max(crossed)       # 一天跨过好几档只记最高的那档
        if top >= 100:
            add("quota", day, "额度用完", f"已用 {_money(total)} · 进入待结算", "100", tone="risk")
        else:
            add("quota", day, f"额度 {top:g}%", f"已用 {_money(total)}", f"{top:g}")

    for mail in mails or ():
        local = mail.when.astimezone().date()
        if joined is not None and local < joined:
            continue
        # 告警的标题是「AWS 邮件：账号暂停通知」，卡片标题已经是「AWS 邮件」，去掉前缀
        title = re.sub(r"^AWS\s*邮件[：:]\s*", "", mail.title or "")
        text = title if not mail.text else f"{title} · {mail.text}"
        key_day = mail.when.astimezone().strftime("%Y-%m-%dT%H%M%S")
        key = f"mail:{number}:{key_day}"
        items.append(Item(ident=f"auto:{key}", date=local, kind="mail", title="AWS 邮件",
                          text=text[:80] + ("…" if len(text) > 80 else ""), account=number, source="auto"))
    return items


# ---------------------------------------------------------------- 告警流
@dataclass
class FeedItem:
    when: datetime
    kind: str
    title: str
    text: str
    tone: str
    account: str
    email: str

    @property
    def via(self) -> str:
        return "mail" if self.kind.startswith("mail-") else "tg"


# ---------------------------------------------------------------- 一个客户
@dataclass
class MonthLine:
    """对账单「按月」点开一个月以后的一行：一个账号这个月的进出。"""
    number: str
    added: float = 0.0
    delta: float = 0.0
    used: float = 0.0
    out: float = 0.0
    why: str = ""


@dataclass
class MonthRow:
    month: str                  # 2026-09
    start: float = 0.0
    added: float = 0.0
    added_accounts: list = field(default_factory=list)      # [(号码, 金额)]
    delta: float = 0.0
    delta_accounts: list = field(default_factory=list)      # [(号码, 金额)]
    used: float = 0.0
    out: float = 0.0
    out_accounts: list = field(default_factory=list)        # [(号码, 金额, 原因)]
    end: float = 0.0
    current: bool = False
    lines: dict = field(default_factory=dict)               # 号码 -> MonthLine，按「名下账号」表格的顺序

    def line(self, number: str) -> MonthLine:
        return self.lines.setdefault(number, MonthLine(number))

    @property
    def label(self) -> str:
        return f"{int(self.month[5:])} 月"

    @property
    def year(self) -> str:
        return self.month[:4]


class CustomerView:
    """一个客户：名下的账号、钱、时间线、对账单。"""

    def __init__(self, customer: Customer, holdings: list[Holding], events: list[CustomerEvent],
                 today: date, accounts_by_number: dict[str, Account], mails: dict[str, list] | None = None,
                 feed: list[FeedItem] | None = None):
        self.customer = customer
        self.holdings = sorted(holdings, key=lambda h: (STAGE_ORDER[h.stage], h.joined or date.min), reverse=False)
        self.today = today
        self.events = events
        self.accounts_by_number = accounts_by_number
        self.mails = mails or {}
        self.feed = feed or []

    # ------------------------------------------------------------ 账号
    @property
    def in_use(self) -> list[Holding]:
        return [h for h in self.holdings if h.stage == "use"]

    @property
    def listed(self) -> list[Holding]:
        """「名下账号」表格的默认顺序：使用中、风控、待结算、已结算，同一档里先分配的在前。"""
        return sorted(self.holdings, key=lambda h: (TABLE_ORDER[h.stage], h.joined or date.min))

    @property
    def counts(self) -> dict[str, int]:
        stages = [h.stage for h in self.holdings]
        return {
            "total": len(stages), "use": stages.count("use"), "risk": stages.count("risk"),
            "pending": stages.count("pending"), "settled": stages.count("settled"),
            "bad": stages.count("risk") + stages.count("pending"),
        }

    @property
    def missing(self) -> list[Holding]:
        """查不到消费、也没有存下来的数的账号：钱上算不进去，页面要说一声。"""
        return [h for h in self.holdings if not h.has_numbers]

    @property
    def stale(self) -> list[Holding]:
        return [h for h in self.holdings if h.series.stale_as_of is not None]

    # ------------------------------------------------------------ 钱
    @property
    def budget(self) -> float:
        return sum(h.account.budget for h in self.in_use)

    @property
    def spent(self) -> float:
        return sum(h.spent for h in self.in_use)

    @property
    def balance(self) -> float:
        return self.budget - self.spent

    @property
    def usage_pct(self) -> float | None:
        return self.spent / self.budget * 100 if self.budget > 0 else None

    @property
    def level(self) -> str:
        pct = self.usage_pct
        if pct is None:
            return "none"
        return "danger" if pct >= config.DANGER_PCT else "warn" if pct >= config.WARN_PCT else "ok"

    @property
    def lifetime(self) -> float:
        return sum(h.spent for h in self.holdings)

    @property
    def lifetime_raw(self) -> float:
        return sum(h.raw_spent for h in self.holdings)

    @property
    def partners(self) -> list[str]:
        return list(dict.fromkeys(h.account.partner for h in self.holdings if h.stage != "settled"))

    def daily(self, days: int = 30) -> tuple[list[date], dict[str, list[float]]]:
        """近 days 天每天的消费，按账号（号码）分开。"""
        start = self.today - timedelta(days=days - 1)
        stamps = [start + timedelta(days=i) for i in range(days)]
        return stamps, {h.number: h.daily(start, self.today) for h in self.holdings}

    @property
    def avg7(self) -> float:
        """近 7 天（不含今天，今天还没过完）的日均。"""
        end = self.today - timedelta(days=1)
        start = end - timedelta(days=6)
        return sum(sum(h.daily(start, end)) for h in self.holdings) / 7

    @property
    def burn(self) -> float:
        """往后推「还能用几天」用的日均：近 7 天的日均，不到半分钱的当 0（最近没有消费）。"""
        avg = self.avg7
        return avg if avg >= MIN_DAILY else 0.0

    @property
    def days_left(self) -> float | None:
        """还能用几天：余额 ÷ 日均。没有余额是 0，最近没量（日均不到半分钱）是 None。"""
        if self.balance <= 0:
            return 0.0
        burn = self.burn
        return self.balance / burn if burn else None

    @property
    def long_runway(self) -> bool:
        """一年以上才用完：页面上不写具体天数和日期，写「一年以上」。"""
        left = self.days_left
        return left is not None and left > LONG_RUNWAY_DAYS

    @property
    def runs_out_on(self) -> date | None:
        """哪天前后用完；一年以上的不算日期（太远了，加到今天上可能连日期都放不下）。"""
        left = self.days_left
        if left is None or left > LONG_RUNWAY_DAYS:
            return None
        return self.today + timedelta(days=math.floor(left))

    def balance_on(self, day: date) -> float:
        """那一天结束时的余额：那天在用的账号，各自还剩多少。"""
        total = 0.0
        for holding in self.holdings:
            if holding.joined is not None and holding.joined > day:
                continue
            if holding.left is not None and holding.left <= day:
                continue
            total += holding.budget_on(day) - holding.spent_through(day)
        return total

    def balance_history(self, days: int = 21) -> list[tuple[date, float]]:
        start = self.today - timedelta(days=days - 1)
        stamps = [start + timedelta(days=i) for i in range(days)]
        history = [(day, self.balance_on(day)) for day in stamps[:-1]]
        history.append((self.today, self.balance))     # 今天就是页面上的余额，一分不差
        return history

    # ------------------------------------------------------------ 时间线
    @cached_property
    def timeline(self) -> list[Item]:
        partners = {a.account: a.partner for a in self.accounts_by_number.values()}
        overrides = {e.key: e for e in self.events if e.source == "auto" and e.key}
        items: list[Item] = []
        if self.customer.since:
            items.append(Item(ident="since", date=self.customer.since, kind="signup", title="开始合作",
                              text=self.customer.name, source="auto", order=-1))
        for event in self.events:
            if event.source == "manual" and not event.deleted:
                items.append(_manual_item(event, partners))
        for holding in self.holdings:
            for item in auto_items(holding, self.today, self.mails.get(holding.number)):
                override = overrides.get(item.ident[len("auto:"):])
                if override is not None:
                    if override.deleted:
                        continue
                    item.date = override.date
                items.append(item)
        items.sort(key=lambda item: (item.date, _TYPE_ORDER.get(item.kind, 50), item.order))
        return items

    @property
    def last_item(self) -> Item | None:
        real = [item for item in self.timeline if item.kind != "signup"]
        return real[-1] if real else (self.timeline[-1] if self.timeline else None)

    # ------------------------------------------------------------ 月度对账单
    @property
    def months(self) -> list[str]:
        """对账单的月份：从最早分到账号、或者最早有消费的那个月，到这个月。

        不从「开始合作」算：合作了几个月才分账号的，前面那几个月一行全是 0，只是占地方。
        """
        firsts = [self.today]
        for holding in self.holdings:
            if holding.joined:
                firsts.append(holding.joined)
            spent = next((day for day, value in zip(holding.series.dates, holding.series.marked) if value), None)
            if spent:
                firsts.append(date.fromisoformat(spent))
        cursor = min(firsts).replace(day=1)
        out = []
        while cursor <= self.today:
            out.append(cursor.isoformat()[:7])
            cursor = (cursor + timedelta(days=32)).replace(day=1)
        return out

    def monthly(self, holding: Holding) -> dict[str, tuple[float, float]]:
        """一个账号每个月的 (折算后, AWS 原价)。"""
        out: dict[str, list[float]] = {}
        for day, raw, marked in zip(holding.series.dates, holding.series.raw, holding.series.marked):
            cell = out.setdefault(day[:7], [0.0, 0.0])
            cell[0] += marked
            cell[1] += raw
        return {month: (cell[0], cell[1]) for month, cell in out.items()}

    @cached_property
    def statement(self) -> list[MonthRow]:
        """按月：月初余额 + 新分配 + 调整额度 − 消费 − 移出 = 月末余额。"""
        months = self.months
        rows = {month: MonthRow(month=month, current=month == self.today.isoformat()[:7]) for month in months}
        first, last = months[0], months[-1]

        def bucket(day: date | None) -> str:
            # 改过日期的事件可能落在区间外（早于开始合作、晚于今天），就近算进头一个月 / 这个月
            month = (day or self.today).isoformat()[:7]
            return min(max(month, first), last)

        for holding in self.listed:
            changes = sum(delta for _, delta in holding.budget_changes)
            initial = holding.account.budget - changes
            row = rows[bucket(holding.joined)]
            row.added += initial
            row.added_accounts.append((holding.number, initial))
            row.line(holding.number).added += initial
            for when, delta in holding.budget_changes:
                row = rows[bucket(when)]
                row.delta += delta
                row.delta_accounts.append((holding.number, delta))
                row.line(holding.number).delta += delta
            for month, (marked, _) in self.monthly(holding).items():
                row = rows[min(max(month, first), last)]
                row.used += marked
                if abs(marked) >= 0.005:
                    row.line(holding.number).used += marked
            if holding.stage != "use":
                row = rows[bucket(holding.left)]
                rest = holding.remaining
                row.out += rest
                row.out_accounts.append((holding.number, rest, holding.left_why))
                line = row.line(holding.number)
                line.out += rest
                line.why = holding.left_why
        balance = 0.0
        for month in months:
            row = rows[month]
            row.start = balance
            balance = row.start + row.added + row.delta - row.used - row.out
            row.end = balance
        return [rows[month] for month in months]

    def statement_matrix(self) -> list[tuple[Holding, dict[str, tuple[float, float]]]]:
        """按账号：每个账号每个月的消费。"""
        return [(holding, self.monthly(holding)) for holding in self.holdings]


# ---------------------------------------------------------------- 组装
def _joined_and_via(account: Account, events: list[CustomerEvent]) -> tuple[date | None, str]:
    """分给这个客户的那天（最近一次分配或者替换上来），以及是替换谁换上来的。

    一个账号只会属于一个客户，所以它启用以来的消费都算这个客户的：分配日期晚于启用日期的（上线那天把
    用了几个月的老账号分过来），按启用日期算——不然前面几个月的时间线和对账单就都没了。
    """
    joined, via = None, ""
    for event in events:
        if event.deleted or event.source != "manual":
            continue
        if event.type == "assign" and event.account == account.account:
            if joined is None or event.date >= joined:
                joined, via = event.date, ""
        elif event.type == "replace" and event.peer == account.account:
            if joined is None or event.date >= joined:
                joined, via = event.date, event.account
    if joined is not None and account.start_date is not None:
        joined = min(joined, account.start_date)
    return joined or account.start_date, via


def _left(holding: Holding, events: list[CustomerEvent], since: date | None = None) -> tuple[date | None, str]:
    """不在使用中的账号：从哪天起不算进余额、为什么。只看 since（默认分给这个客户那天）以后的事。"""
    if holding.stage == "use":
        return None, ""
    number = holding.number
    joined = since or holding.joined or date.min
    candidates: list[tuple[date, str]] = []
    for event in events:
        if event.deleted or event.source != "manual" or event.account != number or event.date < joined:
            continue
        if event.type == "risk":
            candidates.append((event.date, "风控"))
        elif event.type == "replace":
            candidates.append((event.date, "换下"))
    if holding.used_up_on and holding.used_up_on >= joined:
        candidates.append((holding.used_up_on, "用完"))
    if holding.account.settled:
        candidates.append((holding.account.settled, "结算"))
    if not candidates:
        return holding.account.settled or holding.joined, "结算" if holding.account.settled else ""
    # 同一天的几个原因：先标的风控、再换下……说最早的那个
    priority = {"风控": 0, "换下": 1, "用完": 2, "结算": 3}
    return min(candidates, key=lambda pair: (pair[0], priority[pair[1]]))


def build_views(customers: list[Customer], accounts: list[Account], events: list[CustomerEvent],
                today: date, refresh: bool = False, with_alerts: bool = True) -> dict[str, CustomerView]:
    """几个客户的 CustomerView。accounts 是全部账号（含停用的），events 是 EVENTS 表全部。"""
    ids = {customer.id for customer in customers}
    bound = [a for a in accounts if a.customer in ids]
    series = fetch_series(bound, today, refresh)
    by_number = {a.account: a for a in accounts}
    numbers = {a.account for a in bound}

    mails: dict[str, list] = {}
    feed: dict[str, list[FeedItem]] = {}
    if with_alerts and numbers:
        email_of = {a.account: a.email for a in accounts}
        owner = {a.account: a.customer for a in bound}
        for event in alert_events.recent(limit=alert_events.KEEP_LINES):
            if event.account not in numbers or event.kind == "daily":
                continue
            if event.kind in RISK_MAIL_KINDS:
                mails.setdefault(event.account, []).append(event)
            feed.setdefault(owner[event.account], []).append(FeedItem(
                when=event.when, kind=event.kind, title=event.title, text=event.text, tone=event.tone,
                account=event.account, email=event.email or email_of.get(event.account, ""),
            ))

    views: dict[str, CustomerView] = {}
    for customer in customers:
        mine = [e for e in events if e.customer == customer.id]
        holdings = []
        for account in bound:
            if account.customer != customer.id:
                continue
            data = series.get(account.key) or Series()
            holding = Holding(account=account, series=data)
            holding.joined, holding.via = _joined_and_via(account, mine)
            holding.budget_changes = [
                (e.date, (e.amount or 0.0) - (e.before or 0.0)) for e in mine
                if e.type == "budget" and e.account == account.account and not e.deleted
                and e.source == "manual" and e.amount is not None and e.before is not None
            ]
            holding.stage = stage_of(account, holding.spent if holding.has_numbers else None)
            holding.left, holding.left_why = _left(holding, mine)
            holdings.append(holding)
        views[customer.id] = CustomerView(
            customer, holdings, mine, today, by_number,
            mails={h.number: mails.get(h.number, []) for h in holdings},
            feed=feed.get(customer.id, []),
        )
    return views


# ---------------------------------------------------------------- 一个账号自己的时间线
def _owners(number: str, events: list[CustomerEvent]) -> list[tuple[date, int, str]]:
    """这个账号归属的变化 [(从哪天起, 事件编号, 客户编号；空 = 回到库存)]，按先后排好。"""
    changes = []
    for event in events:
        if event.deleted or event.source != "manual":
            continue
        if (event.type == "assign" and event.account == number) or (event.type == "replace" and event.peer == number):
            changes.append((event.date, event.id, event.customer))
        elif event.type == "unassign" and event.account == number:
            changes.append((event.date, event.id, ""))
    return sorted(changes)


def risk_mails(number: str) -> list:
    """告警流水里这个账号的 AWS 风控邮件（滥用、盗用、暂停）。"""
    return [event for event in alert_events.recent(limit=alert_events.KEEP_LINES)
            if event.account == number and event.kind in RISK_MAIL_KINDS]


def account_timeline(account: Account, events: list[CustomerEvent], series: Series, today: date,
                     mails: list | None = None) -> list[Item]:
    """一个账号自己的时间线：从启用到今天，中间换过几个客户、解绑过都在——解绑以后也追得回来。

    手动记的按 ACCOUNT 认（替换上来的按 PEER），不管记在哪个客户名下，还是在库存时记的（CUSTOMER 空着）；
    按数据算的（上量、终止、额度预警、AWS 邮件）和客户页一个算法，只是从启用日期算起。客户页上对自动事件的
    改动（改日期、删掉）这里照样认。每件事的 customer 是那时候它归哪个客户。
    """
    number = account.account
    manual = [event for event in events if event.source == "manual" and not event.deleted]
    owners = _owners(number, events)

    def owner_on(day: date) -> str:
        current = "" if owners else account.customer      # 一条归属记录都没有（手改 Excel 分的）：一直是它
        for when, _, cid in owners:
            if when > day:
                break
            current = cid
        return current

    holding = Holding(account=account, series=series, joined=account.start_date)
    holding.budget_changes = [
        (event.date, (event.amount or 0.0) - (event.before or 0.0)) for event in manual
        if event.type == "budget" and event.account == number and event.amount is not None and event.before is not None
    ]
    holding.stage = stage_of(account, holding.spent if holding.has_numbers else None)
    # 不算了的那天只在现在这一段（最近一次分过来以后）里找：上一个客户那里标过的风控不算
    tenure = owners[-1][0] if owners and owners[-1][2] else None
    holding.left, holding.left_why = _left(holding, manual, since=tenure)

    items: list[Item] = []
    if account.start_date:
        items.append(Item(ident="start", date=account.start_date, kind="start", title="启用账号",
                          text=f"上游 {account.partner}", account=number, source="auto", order=-1,
                          customer=owner_on(account.start_date)))
    for event in manual:
        if event.account == number or event.peer == number:
            items.append(_manual_item(event, {number: account.partner}))
    overrides = {event.key: event for event in events if event.source == "auto" and event.key}
    for item in auto_items(holding, today, mails):
        override = overrides.get(item.ident[len("auto:"):])
        if override is not None:
            if override.deleted:
                continue
            item.date = override.date
        item.customer = owner_on(item.date)
        items.append(item)
    items.sort(key=lambda item: (item.date, _TYPE_ORDER.get(item.kind, 50), item.order))
    return items


def stock(accounts: list[Account]) -> list[Account]:
    """库存：还没分给任何客户的账号（停用的不算，分不出去）。打着「风控」的排在最后。"""
    return sorted((a for a in accounts if not a.customer and a.enabled), key=lambda a: TAG_RISK in a.lifecycle)


def customer_ids(customers: list[Customer]) -> set[str]:
    return {customer.id for customer in customers}


_NUMBER = re.compile(r"^\d{12}$")


def is_account_number(text: str) -> bool:
    return bool(_NUMBER.match(text or ""))
