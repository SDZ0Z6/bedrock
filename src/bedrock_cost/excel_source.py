"""cred.xlsx 账号台账的读与写。

AK/SK 只在内存里传给 boto3：dataclass 的 repr 里被屏蔽，也不会进模板或日志。告警邮箱的
密码（MAIL_PASSWORD）同样处理，只交给 IMAP 登录用。
文件按 mtime+size 缓存，改完 Excel 刷新页面就能生效，不用重启服务。

写入（账号管理页用）走 create_account / update_account / set_enabled / delete_account
和两个开关入口，它们共用同一条流水线：加锁 -> 校验 -> 备份 -> 临时文件原子替换 ->
清缓存 -> 记审计。**只有新增和修改会先备份**：开关、停用 / 恢复、删除都不备份，
免得点几下开关就把有用的备份挤出那 20 份。

停用是软删：只把 ENABLED 列改成 FALSE，行本身留着。删除是把整行清空（凭证一起），
但也不删行。两种都让行号不会移动，account.key（"账号#行号"）和各处按它建的缓存键
就都还稳。
"""

from __future__ import annotations

import os
import re
import shutil
import tempfile
import threading
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

import openpyxl

from . import avatars, config, mail_inbox

# Excel 表头 -> 内部字段名。表头大小写、前后空格、列顺序都不敏感。
REQUIRED_COLUMNS = {
    "PARTNER": "partner",
    "ACCOUNT": "account",
    "BUDGET": "budget",
    "TAG_RATIO": "tag_ratio",
    "UNTAG_RATIO": "untag_ratio",
    "AK": "ak",
    "SK": "sk",
}

# 可选列。老台账没有这几列也能正常读：
#   TAG        缺失时回落到 .env 里的 TAG_KEY
#   ENABLED    缺失时所有账号都算启用（软删用的开关，见 set_enabled）
#   START_DATE 账号的启用日期。概览页的消费和余额从这一天累计到今天，缺失时
#              回落到 Cost Explorer 能查到的最早一天（见 dates.cumulative_range）
#   TG_ENABLED 这个账号要不要发 Telegram 告警。缺失 / 空着 = **不发**——告警是往
#              外发消息的，必须主动开，和 ENABLED 的「空着算启用」刻意相反。
#              新增账号时填了群就打开（见 create_account）；之后只在账号管理的表格里
#              点（见 set_tg_enabled），修改弹窗不碰它
#   TG_CHAT_IDS 告警发到哪些群。一格里放多个，逗号隔开（-100 开头的一串数字，
#              或 @频道名）。页面上是一行一个，存的时候拼成一格
#   MAIL_ENABLED  要不要收这个账号的告警邮箱（见 mail_alerts）。和 TG_ENABLED 一样空着算关，
#              新增时填了邮箱就打开，之后只在表格里点（set_mail_enabled）
#   MAIL_PROVIDER 邮箱平台，mail_inbox.PROVIDERS 里的 key（aliyun-sg、exmail……）
#   MAIL_ADDRESS  邮箱地址，一般是这个 AWS 账号的 root 邮箱
#   MAIL_PASSWORD 客户端登录密码（阿里邮箱是三方客户端安全密码）。和 SK 一样只进不出：
#              页面上不回显，审计日志里只记「已更新」
#   MAIL_SERVER   只有 MAIL_PROVIDER=custom 时才用：自己填的 IMAP 服务器 host:port
#   EMAIL      这个 AWS 账号的邮箱（root 邮箱）。号码一眼认不出是谁，页面和 TG 卡片上
#              都把它和号码一起写。新增、修改时必填，两个账号不能填同一个；老台账没有
#              这一列也能读，只是显示「未填账号邮箱」
#   LIFECYCLE  生命周期标签（正常、结算、风控……），一格里多个，逗号隔开。只是个标记，
#              不影响任何查询、汇总和告警。可选的标签在第二个工作表 LIFECYCLE 里
#   AVATAR     头像用的表情；空着就用邮箱首字母
#   AVATAR_COLOR 头像底色，0~7 选一组（见 avatars.py）；空着就按邮箱自动配
#   CUSTOMER   这个账号属于哪个客户（客户编号 C001…）；空着 = 库存。见下面「客户」一节
#   SETTLED    这个账号哪天结算的；空着 = 还没结算
OPTIONAL_COLUMNS = {
    "TAG": "tag_spec",
    "ENABLED": "enabled",
    "START_DATE": "start_date",
    "TG_ENABLED": "tg_enabled",
    "TG_CHAT_IDS": "tg_chat_ids",
    "MAIL_ENABLED": "mail_enabled",
    "MAIL_PROVIDER": "mail_provider",
    "MAIL_ADDRESS": "mail_address",
    "MAIL_PASSWORD": "mail_password",
    "MAIL_SERVER": "mail_server",
    "EMAIL": "email",
    "LIFECYCLE": "lifecycle",
    "AVATAR": "avatar_emoji",
    "AVATAR_COLOR": "avatar_color",
    "CUSTOMER": "customer",
    "SETTLED": "settled",
}

COLUMNS = {**REQUIRED_COLUMNS, **OPTIONAL_COLUMNS}

# TAG 列里 键 与 值 的分隔符。'$' 是 Cost Explorer 自己的分组键写法，一并兼容。
_TAG_SEPARATORS = "=:$"

# ---------------------------------------------------------------- 生命周期
# 可选的标签放在台账的第二个工作表里（NAME、COLOR 两列），和账号一起备份、一起拷走。
# 没有这张表就用下面三个默认的；第一次在页面上增删标签时才把表建出来。
LIFECYCLE_SHEET = "LIFECYCLE"
# 标签能选的颜色。只上在圆点和极浅的底上，字永远是墨色，所以不用担心对比度
LIFECYCLE_COLORS = {
    "green": ("绿", "#2c7652"),
    "gray": ("灰", "#87867f"),
    "red": ("红", "#bc3b2e"),
    "clay": ("陶土", "#d97757"),
    "amber": ("琥珀", "#c48200"),
    "blue": ("蓝", "#2f6aa8"),
    "teal": ("青", "#2f8f86"),
    "violet": ("紫", "#7b6fc0"),
}
DEFAULT_LIFECYCLE = (("正常", "green"), ("结算", "gray"), ("风控", "red"))
MAX_LIFECYCLE_NAME = 12
MAX_LIFECYCLE_TAGS = 30
# 一格里多个标签的分隔符：逗号（中英文）、顿号、分号、竖线都认，写回时统一成半角逗号。
# 不认空白——标签名里可能有空格
_LIFE_SEPARATORS = re.compile(r"[,，、;；|\n]+")


@dataclass(frozen=True)
class LifecycleTag:
    name: str
    color: str  # LIFECYCLE_COLORS 的 key

    @property
    def hex(self) -> str:
        return LIFECYCLE_COLORS.get(self.color, LIFECYCLE_COLORS["gray"])[1]


def split_lifecycle(value: object) -> tuple[str, ...]:
    """一格（或表单里的多选）拆成去重后的标签，保持原来的顺序。"""
    if value is None:
        return ()
    seen: dict[str, None] = {}
    for piece in _LIFE_SEPARATORS.split(_clean(value)):
        name = piece.strip()
        if name:
            seen.setdefault(name, None)
    return tuple(seen)


def _canon_lifecycle(value: object) -> str:
    return ",".join(split_lifecycle(value))


# ---------------------------------------------------------------- 客户
# 每个账号属于一个客户（账号表的 CUSTOMER 列），一个客户可以有多个账号；没有客户的账号在库存里。
# 客户本身放在工作表 CUSTOMERS，客户时间线上手动记的事放在 EVENTS，都和账号一起备份、一起拷走，
# 也能直接在 Excel 里改。第一次在页面上建客户、记事件时才把这两张表建出来。
#
# 账号在客户名下的阶段不单独存，按台账算（见 customers.stage_of）：SETTLED 有日期是已结算；
# 生命周期里有「结算」是待结算（被换下、额度用完），有「风控」是风控 · 待替换；其余是使用中。
# 所以在账号管理里给账号打上 / 去掉这两个标签，客户页上的阶段跟着变。
CUSTOMERS_SHEET = "CUSTOMERS"
CUSTOMER_HEADER = ("ID", "NAME", "REGION", "AVATAR", "STATUS", "SINCE", "NOTE")
EVENTS_SHEET = "EVENTS"
# TYPE 是事件类别（customers.EVENT_TYPES 的 key）；AMOUNT / BEFORE 是金额（分配时的额度、额度改前改后、
# 结算时的消费）；PEER 是替换时换上的新账号；SOURCE 是 manual（有人操作）或 auto——自动事件本身
# 每次按数据算出来，不存，表里只存对它的改动：改了日期、删掉了（KEY 认是哪一条，DELETED 是删掉）。
# CUSTOMER 空着的是账号在库存里时记的事（调额度、风控、停用 / 恢复），只在账号页的时间线上
EVENT_HEADER = (
    "ID", "DATE", "CUSTOMER", "ACCOUNT", "TYPE", "AMOUNT", "BEFORE", "PEER", "NOTE",
    "SOURCE", "KEY", "DELETED", "CREATED_AT", "ACTOR",
)
# 客户状态：台账里写中文，读的时候认中英文
CUSTOMER_STATUSES = {"on": "合作中", "pause": "暂停", "gone": "已流失"}
_STATUS_WORDS = {**{label: key for key, label in CUSTOMER_STATUSES.items()},
                 **{key: key for key in CUSTOMER_STATUSES}}
# 插画头像的个数（static/avatars/c01.webp … c10.webp）；0 = 没选，用名字的第一个字
CUSTOMER_AVATARS = 10
# 客户流程会自动改的三个生命周期标签。清单里被删了也照样认（写的时候顺手加回清单里）
TAG_NORMAL = "正常"
TAG_SETTLE = "结算"
TAG_RISK = "风控"
# 时间线上「记一笔」只是记下来的几类（备注、手动标的开始上量 / 上量终止 / 恢复上量）：不改钱、不改阶段，
# 所以账号页上也能改日期、删掉
NOTE_TYPES = ("note", "rampup", "stop", "resume")
_CUSTOMER_ID = re.compile(r"^C\d{3,}$")


@dataclass
class Customer:
    id: str                       # C001、C002……
    name: str
    region: str = ""              # 国家 / 地区代码（customers.REGIONS），空着 = 没填
    avatar: int = 0               # 1~10 是插画头像；0 = 没选，用名字的第一个字
    status: str = "on"            # CUSTOMER_STATUSES 的 key
    since: date | None = None     # 开始合作的日期
    note: str = ""
    row: int = 0

    @property
    def status_label(self) -> str:
        return CUSTOMER_STATUSES.get(self.status, CUSTOMER_STATUSES["on"])


@dataclass
class CustomerEvent:
    """时间线上手动记的一件事（或者对某条自动事件的改动，见 EVENT_HEADER 的注释）。"""

    id: int
    date: date
    customer: str
    account: str = ""             # 12 位账号 ID；整个客户的事（新客户、备注）是空的
    type: str = "note"
    amount: float | None = None
    before: float | None = None
    peer: str = ""                # 替换时换上的新账号
    note: str = ""
    source: str = "manual"        # manual / auto
    key: str = ""                 # 自动事件的标识（source=auto 时才有）
    deleted: bool = False
    created_at: datetime | None = None
    actor: str = ""
    row: int = 0


def _to_customer_id(value: object) -> str:
    return _clean(value).upper()


def _to_status(value: object) -> str:
    return _STATUS_WORDS.get(_clean(value).lower(), _STATUS_WORDS.get(_clean(value), "on"))


def _to_customer_avatar(value: object) -> int:
    text = _clean(value)
    if text.endswith(".0"):
        text = text[:-2]
    if text.isdecimal() and 0 <= int(text) <= CUSTOMER_AVATARS:
        return int(text)
    return 0


def _to_amount(value: object) -> float | None:
    if value is None or _clean(value) == "":
        return None
    return _to_number(value)


def _to_datetime(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return value
    text = _clean(value)
    if not text:
        return None
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None

# 必填列空着时的占位显示值。写回时要按同一套规则反推行身份，所以提成常量。
NO_PARTNER = "(未填写上游)"
NO_ACCOUNT = "(未填写账号)"


class ExcelSourceError(RuntimeError):
    """台账文件缺失或表头不符合预期。"""


def parse_tag_spec(raw: str) -> tuple[str, str | None]:
    """解析 Excel TAG 列，返回 (标签键, 标签值)。

        'map-migrated=migXYT8EVQSVP'  -> ('map-migrated', 'migXYT8EVQSVP')
        'map-migrated'               -> ('map-migrated', None)  值为 None 表示
                                        「任意非空值都算 TAG」
        ''                           -> (config.TAG_KEY, None)
    """
    text = (raw or "").strip()
    if not text:
        return config.TAG_KEY, None

    positions = [text.find(sep) for sep in _TAG_SEPARATORS if text.find(sep) > 0]
    if not positions:
        return text, None

    cut = min(positions)
    key = text[:cut].strip()
    value = text[cut + 1 :].strip()
    if not key:
        return config.TAG_KEY, value or None
    return key, value or None


@dataclass
class Account:
    partner: str
    account: str
    budget: float
    tag_ratio: float
    untag_ratio: float
    ak: str = field(default="", repr=False)  # repr 屏蔽，防止意外打印凭证
    sk: str = field(default="", repr=False)
    row: int = 0
    tag_spec: str = ""
    enabled: bool = True
    # 启用日期。概览页从这一天累计消费到今天；None = 台账里没填
    start_date: date | None = None
    # Telegram 告警：开关 + 发到哪些群。开关默认关，必须主动开
    tg_enabled: bool = False
    tg_chat_ids: tuple[str, ...] = ()
    # 告警邮箱：开关 + 平台 / 地址 / 密码。开关同样默认关
    mail_enabled: bool = False
    mail_provider: str = ""
    mail_address: str = ""
    mail_password: str = field(default="", repr=False)
    mail_server: str = ""
    # 账号邮箱、生命周期、头像（都是给人认的，不影响查询）
    email: str = ""
    lifecycle: tuple[str, ...] = ()
    avatar_emoji: str = ""
    avatar_color: int | None = None
    # 属于哪个客户（空着 = 库存）、哪天结算的
    customer: str = ""
    settled: date | None = None

    @property
    def label(self) -> str:
        """图表、面包屑、下拉框里的短名：邮箱 @ 前面那段；没填邮箱用号码。"""
        return self.email.split("@", 1)[0] if self.email else self.account

    @property
    def avatar(self) -> avatars.Avatar:
        return avatars.avatar_for(self.email, self.account, self.partner, self.avatar_emoji, self.avatar_color)

    @property
    def tg_active(self) -> bool:
        """真的会发消息：开关开着、至少填了一个群、账号本身也在启用。"""
        return self.enabled and self.tg_enabled and bool(self.tg_chat_ids)

    @property
    def mail_box(self) -> mail_inbox.Mailbox | None:
        """要登录的邮箱。平台、地址、密码缺一样（或者自定义服务器认不出）就是 None。"""
        return mail_inbox.mailbox_for(self.mail_provider, self.mail_address, self.mail_password, self.mail_server)

    @property
    def mail_configured(self) -> bool:
        return self.mail_box is not None

    @property
    def mail_active(self) -> bool:
        """真的会去收信：开关开着、邮箱填全了、账号本身也在启用。"""
        return self.enabled and self.mail_enabled and self.mail_configured

    @property
    def mail_provider_label(self) -> str:
        return mail_inbox.provider_label(self.mail_provider)

    @property
    def mail_password_saved(self) -> bool:
        """页面上只能知道「存没存过」，密码本身永远不进模板。"""
        return bool(self.mail_password)

    @property
    def ak_masked(self) -> str:
        """页面上展示的 AK：只露头尾，中间打码。SK 任何情况下都不展示。"""
        if not self.ak:
            return ""
        if len(self.ak) <= 12:
            return self.ak[:4] + "…"
        return f"{self.ak[:8]}…{self.ak[-4:]}"

    @property
    def has_credentials(self) -> bool:
        return bool(self.ak and self.sk)

    @property
    def tag_key(self) -> str:
        return parse_tag_spec(self.tag_spec)[0]

    @property
    def tag_value(self) -> str | None:
        """None = 该标签键下任意非空值都算 TAG 消费。"""
        return parse_tag_spec(self.tag_spec)[1]

    @property
    def tag_label(self) -> str:
        """页面上展示的拆分依据。"""
        key, value = parse_tag_spec(self.tag_spec)
        return f"{key}={value}" if value else f"{key}（任意非空值）"

    @property
    def key(self) -> str:
        """缓存与页面锚点用的稳定标识。"""
        return f"{self.account or self.partner}#{self.row}"


def _clean(value: object) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _to_account_id(value: object) -> str:
    """账号 ID 在 Excel 里可能被存成数字，去掉浮点尾巴。"""
    text = _clean(value)
    if text.endswith(".0") and text[:-2].isdigit():
        text = text[:-2]
    return text


# START_DATE 认的几种写法。Excel 存成日期格式时 openpyxl 直接给 datetime，
# 手打成文本时才要走字符串解析。
_DATE_FORMATS = ("%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d", "%Y%m%d", "%d/%m/%Y")


def _to_date(value: object) -> date | None:
    """解析启用日期。解析不出来返回 None，等同于「没填」。

    刻意不猜：填了但认不出的值当成没填，页面上会显示「未设置」并提示，
    比悄悄用一个猜出来的日期去算累计消费安全。
    """
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = _clean(value)
    if not text:
        return None
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


# ENABLED 列里被当作「停用」的写法。空值一律算启用。
_FALSE_WORDS = {"0", "false", "no", "off", "n", "disabled", "停用", "禁用", "否"}


def _to_enabled(value: object) -> bool:
    """解析 ENABLED 列。空值 / 缺列 = 启用，只有明确写了否定词才算停用。"""
    if value is None:
        return True
    if isinstance(value, bool):
        return value
    text = _clean(value).lower()
    if not text:
        return True
    if text.endswith(".0") and text[:-2].isdigit():  # Excel 把 0 存成 0.0
        text = text[:-2]
    return text not in _FALSE_WORDS


# TG_ENABLED 里被当作「开」的写法。和 ENABLED 相反，这里空值算关。
_TRUE_WORDS = {"1", "true", "yes", "on", "y", "enabled", "启用", "开", "开启", "是"}


def _to_flag(value: object) -> bool:
    """解析 TG_ENABLED。空值 / 缺列 = 关，只有明确写了肯定词才算开。"""
    if isinstance(value, bool):
        return value
    text = _clean(value).lower()
    if text.endswith(".0") and text[:-2].isdigit():  # Excel 把 1 存成 1.0
        text = text[:-2]
    return text in _TRUE_WORDS


def _to_chat_id(value: object) -> str:
    """单个群组 ID。Excel 里可能被存成数字（-1001234567890），去掉浮点尾巴。"""
    text = _clean(value)
    if text.endswith(".0") and text[:-2].lstrip("-").isdigit():
        text = text[:-2]
    return text


# TG_CHAT_IDS 一格里多个 ID 的分隔符：逗号（中英文）、分号、空白都认。
# 手改 Excel 的人什么都可能用，读的时候宽松；写回时统一成半角逗号。
_CHAT_SEPARATORS = re.compile(r"[,，;；\s]+")


def _split_chat_ids(value: object) -> tuple[str, ...]:
    """把一格（或表单里的一个框）拆成去重后的 ID 列表，保持原来的顺序。"""
    if value is None:
        return ()
    if isinstance(value, (int, float)):  # 只填了一个、又被 Excel 当成了数字
        return (_to_chat_id(value),)
    seen: dict[str, None] = {}
    for piece in _CHAT_SEPARATORS.split(_clean(value)):
        chat = _to_chat_id(piece)
        if chat:
            seen.setdefault(chat, None)
    return tuple(seen)


def _canon_chat_ids(value: object) -> str:
    """台账里存的形式：逗号拼成一格。比较「改没改」也用这个形式。"""
    return ",".join(_split_chat_ids(value))


def _to_number(value: object, default: float = 0.0) -> float:
    """容错解析数字：支持千分位逗号、货币符号、百分号和空值。"""
    if isinstance(value, (int, float)):
        return float(value)
    text = _clean(value)
    if not text:
        return default
    percent = text.endswith("%")
    text = text.rstrip("%").replace(",", "").replace("$", "").replace("￥", "").strip()
    try:
        number = float(text)
    except ValueError:
        return default
    return number / 100 if percent else number


def _to_avatar_color(value: object) -> int | None:
    """AVATAR_COLOR：0~7 的一个数；空着、写错都当没选（按邮箱自动配色）。"""
    text = _clean(value)
    if text.endswith(".0"):
        text = text[:-2]
    # isdecimal 而不是 isdigit：「²」这种上标数字 isdigit 是真的，int() 却会抛错，一格写错整张台账就读不出来
    if text.isdecimal() and 0 <= int(text) < avatars.TONES:
        return int(text)
    return None


def _sheet_rows(workbook, name: str, header: tuple[str, ...]):
    """按表头名读一张附属工作表（CUSTOMERS / EVENTS）：每行一个 {列名: 值}。没有这张表是空的。
    表头大小写、顺序都不敏感，认不出的列忽略。"""
    if name not in workbook.sheetnames:
        return
    rows = workbook[name].iter_rows(values_only=True)
    try:
        head = next(rows)
    except StopIteration:
        return
    index = {}
    for position, cell in enumerate(head or ()):
        title = _clean(cell).upper()
        if title in header and title not in index:
            index[title] = position
    for row_number, row in enumerate(rows, start=2):
        if not row:
            continue
        values = {title: (row[position] if position < len(row) else None) for title, position in index.items()}
        yield row_number, values


def _read_customers(workbook) -> list[Customer]:
    customers: dict[str, Customer] = {}
    for row_number, values in _sheet_rows(workbook, CUSTOMERS_SHEET, CUSTOMER_HEADER):
        ident = _to_customer_id(values.get("ID"))
        name = _clean(values.get("NAME"))
        if not ident or ident in customers:
            continue
        customers[ident] = Customer(
            id=ident,
            name=name or ident,
            region=_clean(values.get("REGION")).upper(),
            avatar=_to_customer_avatar(values.get("AVATAR")),
            status=_to_status(values.get("STATUS")),
            since=_to_date(values.get("SINCE")),
            note=_clean(values.get("NOTE")),
            row=row_number,
        )
    return list(customers.values())


def _read_events(workbook) -> list[CustomerEvent]:
    events = []
    for row_number, values in _sheet_rows(workbook, EVENTS_SHEET, EVENT_HEADER):
        when = _to_date(values.get("DATE"))
        customer = _to_customer_id(values.get("CUSTOMER"))
        account = _to_account_id(values.get("ACCOUNT"))
        kind = _clean(values.get("TYPE")).lower()
        ident = _to_number(values.get("ID"), 0.0)
        # 库存里的账号记的事（调额度、风控、停用）没有客户，只有账号：账号页的时间线用
        if when is None or not kind or not (customer or account):
            continue
        events.append(CustomerEvent(
            id=int(ident) if ident > 0 else 0,
            date=when,
            customer=customer,
            account=account,
            type=kind,
            amount=_to_amount(values.get("AMOUNT")),
            before=_to_amount(values.get("BEFORE")),
            peer=_to_account_id(values.get("PEER")),
            note=_clean(values.get("NOTE")),
            source="auto" if _clean(values.get("SOURCE")).lower() == "auto" else "manual",
            key=_clean(values.get("KEY")),
            deleted=_to_flag(values.get("DELETED")),
            created_at=_to_datetime(values.get("CREATED_AT")),
            actor=_clean(values.get("ACTOR")),
            row=row_number,
        ))
    return events


def _read_lifecycle(workbook) -> list[LifecycleTag]:
    """第二个工作表 LIFECYCLE（NAME、COLOR 两列）。没有这张表就是默认的三个。"""
    if LIFECYCLE_SHEET not in workbook.sheetnames:
        return [LifecycleTag(name, color) for name, color in DEFAULT_LIFECYCLE]
    tags: dict[str, LifecycleTag] = {}
    for position, row in enumerate(workbook[LIFECYCLE_SHEET].iter_rows(values_only=True)):
        if position == 0 or not row:
            continue  # 表头
        name = _clean(row[0]) if len(row) > 0 else ""
        color = _clean(row[1]).lower() if len(row) > 1 else ""
        if name and name not in tags:
            tags[name] = LifecycleTag(name, color if color in LIFECYCLE_COLORS else "gray")
    return list(tags.values())


_cache_lock = threading.Lock()
_cache: dict[str, object] = {"stamp": None, "accounts": [], "lifecycle": [], "customers": [], "events": []}


@dataclass
class _Ledger:
    accounts: list[Account]
    lifecycle: list[LifecycleTag]
    customers: list[Customer]
    events: list[CustomerEvent]


def _read_workbook(path: Path) -> _Ledger:
    workbook = openpyxl.load_workbook(path, data_only=True, read_only=True)
    try:
        lifecycle = _read_lifecycle(workbook)
        customers = _read_customers(workbook)
        events = _read_events(workbook)
        sheet = workbook.worksheets[0]
        rows = sheet.iter_rows(values_only=True)
        try:
            header_row = next(rows)
        except StopIteration:
            raise ExcelSourceError(f"{path.name} 是空文件，没有表头。") from None

        # 表头列名 -> 列下标
        index: dict[str, int] = {}
        for position, cell in enumerate(header_row):
            name = _clean(cell).upper()
            if name in COLUMNS and COLUMNS[name] not in index:
                index[COLUMNS[name]] = position

        missing = [
            excel_name
            for excel_name, internal in REQUIRED_COLUMNS.items()
            if internal not in index
        ]
        if missing:
            raise ExcelSourceError(
                f"{path.name} 缺少必需的列：{', '.join(missing)}。"
                f"需要的表头是：{', '.join(REQUIRED_COLUMNS)}"
                f"（可选：{', '.join(OPTIONAL_COLUMNS)}）"
            )

        def cell(row: tuple, name: str):
            position = index.get(name)  # 可选列可能不存在
            if position is None:
                return None
            return row[position] if position < len(row) else None

        accounts: list[Account] = []
        for row_number, row in enumerate(rows, start=2):
            if row is None:
                continue
            partner = _clean(cell(row, "partner"))
            account_id = _to_account_id(cell(row, "account"))
            if not partner and not account_id:
                continue  # 跳过空行
            accounts.append(
                Account(
                    partner=partner or NO_PARTNER,
                    account=account_id or NO_ACCOUNT,
                    budget=_to_number(cell(row, "budget")),
                    tag_ratio=_to_number(cell(row, "tag_ratio"), 1.0),
                    untag_ratio=_to_number(cell(row, "untag_ratio"), 1.0),
                    ak=_clean(cell(row, "ak")),
                    sk=_clean(cell(row, "sk")),
                    row=row_number,
                    tag_spec=_clean(cell(row, "tag_spec")),
                    enabled=_to_enabled(cell(row, "enabled")),
                    start_date=_to_date(cell(row, "start_date")),
                    tg_enabled=_to_flag(cell(row, "tg_enabled")),
                    tg_chat_ids=_split_chat_ids(cell(row, "tg_chat_ids")),
                    mail_enabled=_to_flag(cell(row, "mail_enabled")),
                    mail_provider=_clean(cell(row, "mail_provider")),
                    mail_address=_clean(cell(row, "mail_address")),
                    mail_password=_clean(cell(row, "mail_password")),
                    mail_server=_clean(cell(row, "mail_server")),
                    email=_clean(cell(row, "email")),
                    lifecycle=split_lifecycle(cell(row, "lifecycle")),
                    avatar_emoji=_clean(cell(row, "avatar_emoji")),
                    avatar_color=_to_avatar_color(cell(row, "avatar_color")),
                    customer=_to_customer_id(cell(row, "customer")),
                    settled=_to_date(cell(row, "settled")),
                )
            )
        return _Ledger(accounts, lifecycle, customers, events)
    finally:
        workbook.close()


def clear_cache() -> None:
    """丢掉台账缓存，下次 load_accounts 会重新读文件。"""
    with _cache_lock:
        _cache["stamp"] = None
        _cache["accounts"] = []
        _cache["lifecycle"] = []
        _cache["customers"] = []
        _cache["events"] = []


def load_accounts(force: bool = False, include_disabled: bool = False) -> list[Account]:
    """读取账号。文件没变动时直接返回缓存。

    默认只返回启用中的账号——四个查询页都走这条路，停用的账号就此从额度汇总、
    图表和下拉里消失。只有账号管理页传 include_disabled=True 才看得到全部。
    缓存里存的始终是全量，过滤发生在返回时，所以两种视角共用一次文件读取。
    """
    path = config.EXCEL_PATH
    if not path.is_file():
        raise ExcelSourceError(f"找不到账号台账文件：{path}")

    stat = path.stat()
    stamp = (str(path), stat.st_mtime_ns, stat.st_size)
    with _cache_lock:
        if not force and _cache["stamp"] == stamp:
            cached = list(_cache["accounts"])  # type: ignore[arg-type]
            return cached if include_disabled else [a for a in cached if a.enabled]

    ledger = _read_workbook(path)
    with _cache_lock:
        _cache["stamp"] = stamp
        _cache["accounts"] = ledger.accounts
        _cache["lifecycle"] = ledger.lifecycle
        _cache["customers"] = ledger.customers
        _cache["events"] = ledger.events
    accounts = ledger.accounts
    return list(accounts) if include_disabled else [a for a in accounts if a.enabled]


def load_lifecycle(force: bool = False) -> list[LifecycleTag]:
    """可选的生命周期标签（台账第二个工作表）。和账号共用一次文件读取和缓存。"""
    load_accounts(force=force, include_disabled=True)
    with _cache_lock:
        return list(_cache["lifecycle"])  # type: ignore[arg-type]


def load_customers(force: bool = False) -> list[Customer]:
    """客户（工作表 CUSTOMERS），按编号排。和账号共用一次文件读取和缓存。"""
    load_accounts(force=force, include_disabled=True)
    with _cache_lock:
        return list(_cache["customers"])  # type: ignore[arg-type]


def load_events(force: bool = False) -> list[CustomerEvent]:
    """客户时间线上手动记的事和对自动事件的改动（工作表 EVENTS），含删掉的（deleted）。"""
    load_accounts(force=force, include_disabled=True)
    with _cache_lock:
        return list(_cache["events"])  # type: ignore[arg-type]


def lifecycle_colors(tags: list[LifecycleTag] | None = None) -> dict[str, str]:
    """标签名 -> 圆点颜色。账号上有、清单里已经没有的标签（手改过 Excel、或者标签被删了
    还没刷新）模板里按灰色画。"""
    return {tag.name: tag.hex for tag in (load_lifecycle() if tags is None else tags)}


# ==================================================================== 写入
# 账号管理页的几个入口都从这里走。设计约束有三条：
#   1. AK/SK 只在新建时写一次，之后任何编辑都不碰这两列（要换凭证就停用重建）；
#   2. 行号永远不动——account.key 里带行号，各处的缓存键也带，真删行会让下面所有
#      账号的 key 集体位移。停用只改 ENABLED 列；删除是把整行清空，行留在原处；
#   3. 写盘一律「临时文件、再 os.replace」，中途崩了不会留下半个文件。新增和修改
#      在这之前先备份一份；开关、停用 / 恢复、删除不备份。

LEDGER_MODE = 0o640  # 和 DEPLOY.md 里 chmod 640 的约定一致
BACKUP_DIR_NAME = "ledger-backups"
BACKUP_KEEP = 20  # 副本里是明文 AK/SK，不能无限堆
AUDIT_NAME = "ledger-audit.log"

# 内部字段名 -> Excel 表头，报错和审计日志里用它说人话
INTERNAL_TO_EXCEL = {internal: excel for excel, internal in COLUMNS.items()}

# 可编辑字段。凭证不在其中，这就是「编辑不能改 AK/SK」的唯一定义处。
# TG_ENABLED / MAIL_ENABLED 也不在：开关只在表格里点（set_tg_enabled / set_mail_enabled），
# 弹窗里没有它们。放进来的话，弹窗每保存一次，表单里「没有这个字段」就会被当成关，
# 悄悄把告警关掉。
# MAIL_PASSWORD 也不在：它是「填了才换、留空不动」，单独写（见 _write_mail_password）。
EDITABLE = (
    "partner", "account", "budget", "tag_ratio", "untag_ratio", "tag_spec", "start_date",
    "tg_chat_ids", "mail_provider", "mail_address", "mail_server",
    "email", "lifecycle", "avatar_emoji", "avatar_color", "customer",
)
# 新建时还要额外收凭证
CREATE_ONLY = ("ak", "sk")

# 把单元格原值按「读取时的规则」归一，用来判断某个字段是不是真的改了。
# 不这么做的话，Excel 里存成 1 的比率和表单里填的 1.0 会被当成一次变更，
# 每次点保存都白白备份 + 重写一遍文件。
_NORMALIZE = {
    "partner": lambda v: _clean(v) or NO_PARTNER,
    "account": lambda v: _to_account_id(v) or NO_ACCOUNT,
    "budget": lambda v: _to_number(v),
    "tag_ratio": lambda v: _to_number(v, 1.0),
    "untag_ratio": lambda v: _to_number(v, 1.0),
    "tag_spec": _clean,
    "start_date": _to_date,
    "tg_chat_ids": _canon_chat_ids,
    "mail_provider": _clean,
    "mail_address": _clean,
    "mail_server": _clean,
    "email": _clean,
    "lifecycle": _canon_lifecycle,
    "avatar_emoji": _clean,
    "avatar_color": _to_avatar_color,
    "customer": _to_customer_id,
}


_ACCOUNT_ID = re.compile(r"^\d{12}$")
_EMAIL = re.compile(r"^[^@\s,;]+@[^@\s,;]+\.[^@\s,;]+$")
MAX_MAIL_PASSWORD = 256
# 和 telegram.CHAT_ID_PATTERN 同一个规则。不直接 import：台账模块不该依赖发消息的模块
_CHAT_ID = re.compile(r"^(-?\d{5,20}|@[A-Za-z][A-Za-z0-9_]{4,31})$")
MAX_TG_CHATS = 10
_AK_SHAPE = re.compile(r"^[A-Z0-9]{16,128}$")

_write_lock = threading.Lock()


class LedgerConflict(ExcelSourceError):
    """要改的那一行已经不是页面上看到的账号了（文件被别的途径换过）。"""


class FormError(str):
    """一条校验错误。它本身就是那句话（页面照常显示、测试照常比对）；page 说它属于弹窗的
    哪一页：basic（基础信息）/ mail（告警邮箱）/ tg（Telegram 告警）。校验没过时弹窗停在
    第一条错误所在的那一页，有错的页签上标红点。"""

    page: str

    def __new__(cls, text: str, page: str = "basic"):
        error = super().__new__(cls, text)
        error.page = page
        return error


# ---------------------------------------------------------------- 表单校验
def _strict_number(text: str) -> float | None:
    """严格解析数字，解析不出来返回 None。

    和 _to_number 的宽松策略是有意分开的：读别人手填的表格要能容错，
    但表单里填了 "abc" 必须报错，不能悄悄变成 0。
    """
    body = text.rstrip("%").replace(",", "").replace("$", "").replace("￥", "").strip()
    if not body:
        return None
    try:
        value = float(body)
    except ValueError:
        return None
    if value != value or value in (float("inf"), float("-inf")):  # NaN / inf
        return None
    return value / 100 if text.strip().endswith("%") else value


def form_chat_ids(form) -> list[str]:
    """表单里的全部群组 ID：一行一个框，同名字段有多个（MultiDict.getlist）。

    每个框里也允许粘贴多个——有人会把一串 ID 一次粘进第一个框。拆开、去空、去重。
    """
    raw = form.getlist("tg_chat_ids") if hasattr(form, "getlist") else [form.get("tg_chat_ids")]
    seen: dict[str, None] = {}
    for value in raw:
        for chat in _split_chat_ids(value):
            seen.setdefault(chat, None)
    return list(seen)


def form_lifecycle(form) -> list[str]:
    """表单里勾选的生命周期（多选框，同名字段有多个）。"""
    raw = form.getlist("lifecycle") if hasattr(form, "getlist") else [form.get("lifecycle")]
    seen: dict[str, None] = {}
    for value in raw:
        for name in split_lifecycle(value):
            seen.setdefault(name, None)
    return list(seen)


def validate(
    form: dict,
    others: list[Account],
    creating: bool,
    current: Account | None = None,
    lifecycle: list[LifecycleTag] | None = None,
    customers: list[Customer] | None = None,
) -> tuple[dict, list[str]]:
    """把表单文本校验成可以写进台账的一行。

    返回 (清洗后的字段, 错误列表)。错误一次收齐再返回——填错三个字段却只被
    告知一个，用户要来回提交三次。告警邮箱和 TG 群的错误是 FormError，带着它属于弹窗的
    哪一页；其余的是普通字符串，都在「基础信息」那页。

    others 是「除自己以外的全部账号」，含已停用的：停用不等于账号 ID 可以被
    别人重用，否则恢复的时候就撞车了。current 是修改前的这个账号（新建时没有），
    用来判断邮箱密码能不能留空不改。lifecycle 是可选的标签清单；给了就只认清单里的
    （外加这个账号身上本来就有的，免得手改过的老标签让整张表单存不进去）。customers 是客户清单，
    给了就检查选的客户还在不在。
    """
    errors: list[str] = []
    data = {name: _clean(form.get(name)) for name in (*EDITABLE, *CREATE_ONLY)}
    data["mail_password"] = _clean(form.get("mail_password"))

    # 客户：表单里没有这一项（别处来的表单）就当没改，不能当成「改成库存」
    if "customer" in form:
        chosen = _to_customer_id(form.get("customer"))
        if chosen and customers is not None and chosen not in {c.id for c in customers}:
            errors.append("选的客户不在了，刷新页面再选。")
        data["customer"] = chosen
    else:
        data["customer"] = current.customer if current else ""

    # 账号邮箱：必填，两个账号不能是同一个（AWS 的 root 邮箱本来就不能重复）
    email = data["email"]
    if not email:
        errors.append("账号邮箱不能为空，填这个 AWS 账号的 root 邮箱。")
    elif len(email) > 254 or not _EMAIL.match(email):
        errors.append("账号邮箱格式不对，应该形如 name@example.com。")
    else:
        taken = next((a for a in others if a.email and a.email.lower() == email.lower()), None)
        if taken:
            errors.append(f"邮箱 {email} 已经是账号 {taken.account} 的了，不能两个账号填同一个。")

    tags = form_lifecycle(form)
    data["lifecycle"] = ",".join(tags)
    if lifecycle is not None:
        known = {tag.name for tag in lifecycle} | set(current.lifecycle if current else ())
        unknown = [name for name in tags if name not in known]
        if unknown:
            errors.append(f"生命周期里没有「{'」「'.join(unknown)}」，先在标签清单里加上。")
    # 长度只管新加的：账号身上本来就有的（手改 Excel 写长了的）照样能存，不然这一行就再也存不进去
    already = set(current.lifecycle) if current else set()
    too_long = [name for name in tags if len(name) > MAX_LIFECYCLE_NAME and name not in already]
    if too_long:
        errors.append(f"生命周期标签最多 {MAX_LIFECYCLE_NAME} 个字：「{'」「'.join(too_long)}」太长了。")

    # 头像只放一个字：填了一串（「Johanna」、几个表情）就只留第一个，不报错
    data["avatar_emoji"] = avatars.first_grapheme(data["avatar_emoji"].strip(" ,，"))
    raw_color = data["avatar_color"]
    data["avatar_color"] = _to_avatar_color(raw_color)
    if raw_color and data["avatar_color"] is None:
        errors.append("头像底色不对，请从色块里选。")

    if not data["partner"]:
        errors.append("上游不能为空。")
    elif len(data["partner"]) > 64:
        errors.append("上游名称最多 64 个字。")

    account = _to_account_id(data["account"])
    data["account"] = account
    if not account:
        errors.append("账号不能为空。")
    elif not _ACCOUNT_ID.match(account):
        errors.append("账号必须是 12 位数字的 AWS 账号 ID。")
    elif any(a.account == account for a in others):
        errors.append(f"账号 {account} 已经在台账里了，不能重复添加。")

    budget = _strict_number(data["budget"])
    if budget is None:
        errors.append("额度要填数字。")
    elif budget < 0:
        errors.append("额度不能是负数。")
    else:
        data["budget"] = budget

    for name, label in (("tag_ratio", "TAG 比率"), ("untag_ratio", "UNTAG 比率")):
        value = _strict_number(data[name])
        if value is None:
            errors.append(f"{label}要填数字。")
        elif value <= 0:
            errors.append(f"{label}必须大于 0。")
        elif value > 100:
            errors.append(f"{label}看起来不对（{value:g} 倍），请确认。")
        else:
            data[name] = value

    if data["tag_spec"]:
        tag_key, _ = parse_tag_spec(data["tag_spec"])
        if not tag_key:
            errors.append("TAG 解析不出标签键，正确写法形如 map-migrated=migXXXX。")

    # 启用日期可以留空（概览页会回落到 CE 最早可查日并标出来），但填了就必须能解析
    raw_start = data["start_date"]
    start_date = _to_date(raw_start)
    if raw_start and start_date is None:
        errors.append("启用日期认不出来，填成 2026-09-01 这样的格式。")
    elif start_date and start_date > date.today():
        errors.append("启用日期不能晚于今天。")
    else:
        data["start_date"] = start_date

    # Telegram：弹窗里只有群组 ID，开关在表格里（见 EDITABLE 的注释）
    chats = form_chat_ids(form)
    data["tg_chat_ids"] = ",".join(chats)
    bad = [chat for chat in chats if not _CHAT_ID.match(chat)]
    if bad:
        errors.append(FormError(
            f"群组 ID「{'」「'.join(bad)}」格式不对：群组是一串负数（超级群组以 -100 开头），"
            "频道可以写 @频道名。",
            "tg",
        ))
    elif len(chats) > MAX_TG_CHATS:
        errors.append(FormError(f"一个账号最多 {MAX_TG_CHATS} 个群，现在填了 {len(chats)} 个。", "tg"))

    # 告警邮箱的地址留空、却填了密码：要收的就是账号邮箱本身，地址照它填上
    if not data["mail_address"] and data["mail_password"] and email and _EMAIL.match(email):
        data["mail_address"] = email

    errors.extend(_validate_mail(data, creating, current))

    if creating:
        # 新建必须给凭证：没有 AK/SK 的账号在所有查询页都是查不出数的空壳
        if not data["ak"]:
            errors.append("AK 不能为空。")
        elif not _AK_SHAPE.match(data["ak"]):
            errors.append("AK 格式不对，应该是 16~128 位大写字母和数字（形如 AKIA…）。")
        if not data["sk"]:
            errors.append("SK 不能为空。")
        elif not 30 <= len(data["sk"]) <= 128 or any(c.isspace() for c in data["sk"]):
            errors.append("SK 格式不对，应该是 30~128 位、不含空格的字符串。")
    else:
        data.pop("ak", None)
        data.pop("sk", None)

    return data, errors


def _validate_mail(data: dict, creating: bool, current: Account | None) -> list[FormError]:
    """告警邮箱：可选。填了地址就要平台和密码，自定义平台还要服务器。

    地址清空 = 不收这个邮箱了：平台、服务器、密码一起清掉（密码在 update_account 里清）。
    修改时密码留空表示不改；但换了邮箱地址就必须重填——旧密码对新邮箱没有意义。
    """
    address = data["mail_address"]
    if not address:
        data["mail_provider"] = data["mail_server"] = data["mail_password"] = ""
        return []

    errors: list[str] = []
    provider = data["mail_provider"] or mail_inbox.DEFAULT_PROVIDER
    data["mail_provider"] = provider
    if len(address) > 254 or not _EMAIL.match(address):
        errors.append("告警邮箱的地址格式不对，应该形如 name@example.com。")
    if provider not in mail_inbox.BY_KEY:
        errors.append("不认识这个邮箱平台，请从下拉框里选。")
    elif provider == mail_inbox.CUSTOM:
        server = mail_inbox.parse_server(data["mail_server"])
        if server is None:
            errors.append("选了「其他平台」就要填 IMAP 服务器，形如 imap.example.com:993。")
        else:
            data["mail_server"] = f"{server[0]}:{server[1]}"
    else:
        data["mail_server"] = ""   # 预设平台的服务器地址写在代码里，台账里不存

    password = data["mail_password"]
    if password:
        if len(password) > MAX_MAIL_PASSWORD or any(c in password for c in "\r\n\t"):
            errors.append(f"邮箱密码看起来不对：最多 {MAX_MAIL_PASSWORD} 个字符，不能有换行。")
    elif creating or current is None:
        hint = mail_inbox.BY_KEY[provider].password_hint if provider in mail_inbox.BY_KEY else ""
        errors.append(f"填了告警邮箱就要填密码{f'（{hint}）' if hint else ''}。")
    elif not current.mail_password:
        errors.append("这个账号还没存过邮箱密码，要填上。")
    elif address.lower() != current.mail_address.lower():
        errors.append("换了邮箱地址，要重新填这个邮箱的密码。")
    return [FormError(error, "mail") for error in errors]


# ---------------------------------------------------------------- 落盘
def _backup(path: Path) -> Path:
    """写入前留一份带时间戳的副本，只保留最近 BACKUP_KEEP 份。"""
    folder = path.parent / BACKUP_DIR_NAME
    folder.mkdir(exist_ok=True)
    try:
        os.chmod(folder, 0o750)
    except OSError:
        pass  # Windows 上没有实际意义，失败也不该影响备份本身

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target = folder / f"{path.stem}-{stamp}{path.suffix}"
    serial = 1
    while target.exists():  # 同一秒内改了两次
        target = folder / f"{path.stem}-{stamp}-{serial}{path.suffix}"
        serial += 1
    shutil.copy2(path, target)
    try:
        os.chmod(target, LEDGER_MODE)
    except OSError:
        pass

    copies = sorted(folder.glob(f"{path.stem}-*{path.suffix}"))
    for old in copies[:-BACKUP_KEEP]:
        old.unlink(missing_ok=True)
    return target


def _atomic_save(workbook, path: Path) -> None:
    """存到同目录的临时文件再 os.replace 换上去。

    临时文件必须和目标同目录：跨文件系统的 replace 不是原子操作，而且 systemd
    单元里开了 PrivateTmp，/tmp 和 /opt/bedrock 根本不是一个挂载点。
    """
    handle, name = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.stem}-", suffix=".tmp")
    os.close(handle)
    temp = Path(name)
    try:
        workbook.save(temp)
        # mkstemp 建出来是 0600，台账的约定是 0640（属主 + 同组可读）
        try:
            os.chmod(temp, LEDGER_MODE)
        except OSError:
            pass
        os.replace(temp, path)
    except PermissionError as exc:
        temp.unlink(missing_ok=True)
        raise ExcelSourceError(
            f"写不了 {path.name}：文件正被其他程序占用（本机是不是用 Excel 打开着？），"
            "或者进程对所在目录没有写权限。台账没有被修改。"
        ) from exc
    except BaseException:
        temp.unlink(missing_ok=True)
        raise


def _audit(note: str, actor: str) -> None:
    """记一行操作日志。凭证的值永远不写进去。"""
    line = f"{datetime.now():%Y-%m-%d %H:%M:%S}\t{actor or '?'}\t{note}\n"
    try:
        target = config.EXCEL_PATH.parent / AUDIT_NAME
        with target.open("a", encoding="utf-8") as handle:
            handle.write(line)
        try:
            os.chmod(target, LEDGER_MODE)
        except OSError:
            pass
    except OSError:
        pass  # 日志写不进去不该让整个操作失败


# ---------------------------------------------------------------- 定位与改写
def _header_index(sheet) -> dict[str, int]:
    """表头列名 -> 列号。

    注意是 1 起——openpyxl 的 cell() 用 1 起，而读取路径上的 index 是 0 起的
    元组下标，两者不能混用。
    """
    index: dict[str, int] = {}
    for column in range(1, sheet.max_column + 1):
        name = _clean(sheet.cell(row=1, column=column).value).upper()
        if name in COLUMNS and COLUMNS[name] not in index:
            index[COLUMNS[name]] = column
    return index


def _ensure_column(sheet, index: dict[str, int], internal: str) -> int:
    """取列号；老台账缺 TAG / ENABLED 这类可选列时，在表头右边补一列。"""
    if internal in index:
        return index[internal]
    column = sheet.max_column + 1
    sheet.cell(row=1, column=column, value=INTERNAL_TO_EXCEL[internal])
    index[internal] = column
    return column


def _last_data_row(sheet, index: dict[str, int]) -> int:
    """最后一行有内容的数据行。

    不能直接用 max_row：它把只设过格式的空行也算进去，据此追加会在中间留下
    一大段空行。判断标准和读取时一致——上游和账号都空就是空行。
    """
    last = 1
    for row in range(2, sheet.max_row + 1):
        partner = _clean(sheet.cell(row=row, column=index["partner"]).value)
        account = _clean(sheet.cell(row=row, column=index["account"]).value)
        if partner or account:
            last = row
    return last


def _locate(sheet, index: dict[str, int], key: str) -> int:
    """把 account.key 解成行号，并核对那一行确实还是这个账号。

    页面上拿到的 key 形如 "123456789012#3"。如果这期间有人 scp 覆盖了台账，
    行号可能已经指向另一个账号——那就宁可报错，也不能改错行。
    """
    ident, sep, row_text = key.rpartition("#")
    if not sep or not ident or not row_text.isdigit():
        raise ExcelSourceError(f"账号标识无法识别：{key}")

    row = int(row_text)
    if row < 2 or row > sheet.max_row:
        raise LedgerConflict("台账里已经没有这一行了，请刷新页面后重试。")

    found = _to_account_id(sheet.cell(row=row, column=index["account"]).value) or NO_ACCOUNT
    if found != ident:
        raise LedgerConflict(
            "台账内容和页面上看到的对不上（文件被其他方式改过？），本次修改没有写入。"
            "请刷新页面后重试。"
        )
    return row


def _mutate(action, actor: str, *, backup: bool) -> str:
    """写入流水线：加锁 -> 打开 -> action -> 备份 -> 原子替换 -> 清缓存 -> 审计。

    action(sheet, index) 返回一句审计描述；返回空表示「没有实际变化」，这时
    既不备份也不写盘。backup 只有新增和修改传 True：点开关、停用 / 恢复、删除都不
    备份——ledger-backups/ 只留最近 20 份，点几下开关就会把改字段之前的那几份挤掉。
    """
    path = config.EXCEL_PATH
    if not path.is_file():
        raise ExcelSourceError(f"找不到账号台账文件：{path}")

    with _write_lock:
        # 这里不能用 read_only（那种模式改不了），也不能用 data_only：
        # data_only=True 存回去会把公式替换成缓存值，等于毁掉表里所有公式。
        workbook = openpyxl.load_workbook(path)
        try:
            sheet = workbook.worksheets[0]
            index = _header_index(sheet)
            missing = [
                excel_name
                for excel_name, internal in REQUIRED_COLUMNS.items()
                if internal not in index
            ]
            if missing:
                raise ExcelSourceError(
                    f"{path.name} 缺少必需的列：{', '.join(missing)}，无法写入。"
                )
            note = action(sheet, index)
            if note:
                if backup:
                    _backup(path)
                _atomic_save(workbook, path)
        finally:
            workbook.close()

    if not note:
        return ""
    clear_cache()
    _audit(note, actor)
    return note


def _plain(value: float) -> str:
    """审计日志里的数：2000.0 写成 2000，一百万写成 1000000（不写成 1e+06），小数最多六位。"""
    text = f"{value:f}".rstrip("0").rstrip(".")
    return "0" if text in ("", "-0") else text


# 操作日志里事件的说法，和 customers.EVENT_TYPES 里的标题一样（tests/test_restore_and_audit.py 核对）
EVENT_TITLES = {
    "signup": "开始合作", "start": "启用账号", "assign": "分配账号", "unassign": "解绑", "budget": "调整额度",
    "rampup": "开始上量", "resume": "恢复上量", "stop": "上量终止", "quota": "额度预警", "mail": "AWS 邮件",
    "risk": "标记风控", "unrisk": "取消风控", "restore": "恢复使用", "replace": "替换账号", "settle": "结算",
    "disable": "停用", "enable": "恢复启用", "note": "备注",
}


def _account_label(sheet, index: dict[str, int], row: int) -> str:
    """操作日志里的一个账号：邮箱（号码）——光写号码看不出是谁。没填邮箱就只写号码。"""
    number = _to_account_id(sheet.cell(row=row, column=index["account"]).value) or NO_ACCOUNT
    email = _clean(sheet.cell(row=row, column=index["email"]).value) if "email" in index else ""
    return f"{email}（{number}）" if email else number


def _number_label(sheet, index: dict[str, int], number: str) -> str:
    """同上，按号码找那一行（事件里只存了号码）；台账里已经没有这个号码就只写号码。"""
    for row in range(2, sheet.max_row + 1):
        if number and _to_account_id(sheet.cell(row=row, column=index["account"]).value) == number:
            return _account_label(sheet, index, row)
    return number


def _customer_label(book, cid: str) -> str:
    """操作日志里的一个客户：名字（编号）。台账里找不到这个客户就只写编号。"""
    try:
        sheet, index, row = _customer_row(book, cid)
    except ExcelSourceError:
        return cid
    name = _clean(sheet.cell(row=row, column=index["NAME"]).value) if "NAME" in index else ""
    return f"{name}（{cid}）" if name else cid


def _show(value: object) -> str:
    """审计日志里的取值展示：2000.0 写成 2000，日期写成 2026-09-01，空值写成「空」。"""
    if isinstance(value, float):
        return _plain(value)
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return _clean(value) or "空"


class Note(str):
    """一次写入的审计描述：它本身就是写进审计日志的那句话。fields 是真正改了的字段（内部名），
    账号管理页的提示用它说人话（「改了额度和启用日期」），不用回头去拆审计日志里的字。"""

    fields: tuple[str, ...]

    def __new__(cls, text: str, fields=()):
        note = super().__new__(cls, text)
        note.fields = tuple(fields)
        return note


def _write_editable(sheet, index: dict[str, int], row: int, data: dict) -> dict[str, str]:
    """写入可编辑字段，返回真正发生变化的项 {字段: 审计里的那半句}（供审计和页面提示用）。"""
    changes: dict[str, str] = {}
    for name in EDITABLE:
        column = _ensure_column(sheet, index, name)
        before = sheet.cell(row=row, column=column).value
        after = data[name]
        if _NORMALIZE[name](before) == after:
            continue
        changes[name] = f"{INTERNAL_TO_EXCEL[name]} {_show(before)} → {_show(after)}"
        # 必须写 .value，不能用 cell(..., value=after)：openpyxl 的 cell() 把
        # value=None 当成「没给值」直接跳过，于是「把启用日期清空」会变成空操作
        # ——审计日志记了「→ 空」，单元格却纹丝不动。
        sheet.cell(row=row, column=column).value = after
    return changes


def _put_text(cell, value: str | None) -> None:
    """写一格文本。openpyxl 会把「=」开头的字符串当成公式存——密码可能就是「=」开头的。"""
    cell.value = value or None
    if value and value.startswith("="):
        cell.data_type = "s"


def _write_mail_password(sheet, index: dict[str, int], row: int, data: dict) -> list[str]:
    """邮箱密码：填了新的才换，留空不动；邮箱地址清空时一起清掉。

    审计日志里只说换没换，永远不写密码本身。
    """
    new = data.get("mail_password") or ""
    if data.get("mail_address") and not new:
        return []   # 留空 = 不修改
    if "mail_password" not in index and not new:
        return []   # 本来就没有这一列，也没什么可清的
    cell = sheet.cell(row=row, column=_ensure_column(sheet, index, "mail_password"))
    before = _clean(cell.value)
    if before == new:
        return []
    _put_text(cell, new)
    if not new:
        return ["MAIL_PASSWORD 已清除"]
    return ["MAIL_PASSWORD 已更新" if before else "MAIL_PASSWORD 已设置"]


# ---------------------------------------------------------------- 三个入口
def create_account(data: dict, actor: str = "") -> str:
    """在台账末尾追加一行。data 必须是 validate() 校验过的。"""

    def action(sheet, index) -> str:
        row = _last_data_row(sheet, index) + 1
        # data["account"] 是字符串，写进去也是文本格式——AWS 账号 ID 可能有前导零，
        # 存成数字会被吃掉
        # 同样写 .value：新增时启用日期可以留空，value=None 会被 openpyxl 跳过
        for name in (*EDITABLE, *CREATE_ONLY):
            sheet.cell(row=row, column=_ensure_column(sheet, index, name)).value = data[name]
        sheet.cell(row=row, column=_ensure_column(sheet, index, "enabled"), value=True)
        # 新增时填了群组 ID 就直接打开 TG 告警：填群本身就是「要往这些群发」的明确表态，
        # 群里马上会收到「新账号启用」，之后日报和告警也照常发。没填群就是关。
        # 两种都显式写，不指望这一格是空的：追加的那一行可能是手工清空过内容、
        # 却留着旧开关值的行
        chats = _split_chat_ids(data.get("tg_chat_ids"))
        sheet.cell(row=row, column=_ensure_column(sheet, index, "tg_enabled")).value = bool(chats)
        # 邮件告警同理：填了邮箱（validate 保证填了地址就有密码）就打开，没填就是关
        password = data.get("mail_password") if data.get("mail_address") else ""
        _put_text(sheet.cell(row=row, column=_ensure_column(sheet, index, "mail_password")), password)
        mail_on = bool(data.get("mail_address") and password)
        sheet.cell(row=row, column=_ensure_column(sheet, index, "mail_enabled")).value = mail_on
        started = data.get("start_date")
        when = f"，启用日期 {started.isoformat()}" if started else "（未设启用日期）"
        tg = f"，TG 告警已打开（{len(chats)} 个群）" if chats else ""
        mail = f"，邮件告警已打开（{data['mail_address']}）" if mail_on else ""
        # 新增时直接选了客户：客户时间线上记一条「分配账号」，日期取启用日期（没填就是今天）
        owner = ""
        if data.get("customer"):
            _customer_row(sheet.parent, data["customer"])
            _write_event(sheet.parent, when=started or date.today(), customer=data["customer"], kind="assign",
                         account=data["account"], amount=data["budget"], actor=actor)
            owner = f"，分给客户 {_customer_label(sheet.parent, data['customer'])}"
        return (f"新增账号 {_account_label(sheet, index, row)}，上游 {data['partner']}，额度 {_plain(data['budget'])}"
                f"{when}{tg}{mail}{owner}")

    return _mutate(action, actor, backup=True)


def update_account(key: str, data: dict, actor: str = "") -> str:
    """改一行的非凭证字段。

    AK/SK 两列一个字节都不碰——要换凭证的做法是停用旧账号、新建一条。
    没有任何字段变化时返回空串，不会产生备份，也不会动文件的 mtime。
    """

    def action(sheet, index) -> str:
        row = _locate(sheet, index, key)
        before = _customer_fields(sheet, index, row)
        changes = _write_editable(sheet, index, row, data)
        for line in _write_mail_password(sheet, index, row, data):
            changes["mail_password"] = line
        # 改了客户、额度、给账号打上 / 去掉「风控」：客户时间线上跟着记一笔（在同一次写入里）
        if {"customer", "budget", "lifecycle"} & set(changes):
            _log_account_change(sheet, index, row, before, actor)
        # 开关不归弹窗管，但群组 ID 全删光了开关还开着，就成了「开着却没处发」——
        # 顺手关掉。和表格里「没填群开不了」是同一条规矩
        if not data["tg_chat_ids"] and "tg_enabled" in index:
            switch = sheet.cell(row=row, column=index["tg_enabled"])
            if _to_flag(switch.value):
                switch.value = False
                changes["tg_enabled"] = "TG_ENABLED 开 → 关（群组 ID 全删了）"
        # 邮箱地址删了：同理，邮件告警跟着关
        if not data.get("mail_address") and "mail_enabled" in index:
            switch = sheet.cell(row=row, column=index["mail_enabled"])
            if _to_flag(switch.value):
                switch.value = False
                changes["mail_enabled"] = "MAIL_ENABLED 开 → 关（告警邮箱删了）"
        if not changes:
            return ""
        if "customer" in changes:      # 客户写名字（编号），没有客户写「库存」
            def owner(cid: str) -> str:
                return _customer_label(sheet.parent, cid) if cid else "库存"
            changes["customer"] = f"客户 {owner(before['customer'])} → {owner(_customer_fields(sheet, index, row)['customer'])}"
        return Note(f"修改账号 {_account_label(sheet, index, row)}：" + "；".join(changes.values()), changes)

    return _mutate(action, actor, backup=True)


def set_tg_enabled(key: str, enabled: bool, actor: str = "") -> str:
    """账号管理表格里的 TG 开关：只翻 TG_ENABLED 这一格。

    开的时候要求这一行已经有群组 ID——在锁里当场读文件判断，不信页面上看到的：
    页面可能是几分钟前打开的，群组 ID 早被别人清掉了。
    """

    def action(sheet, index) -> str:
        row = _locate(sheet, index, key)
        if enabled:
            chats_column = _ensure_column(sheet, index, "tg_chat_ids")
            if not _split_chat_ids(sheet.cell(row=row, column=chats_column).value):
                raise ExcelSourceError("这个账号还没有填群组 ID，先点「修改」填上再开。")
        column = _ensure_column(sheet, index, "tg_enabled")
        if _to_flag(sheet.cell(row=row, column=column).value) == enabled:
            return ""
        sheet.cell(row=row, column=column).value = bool(enabled)
        return f"{'开启' if enabled else '关闭'}账号 {_account_label(sheet, index, row)} 的 TG 告警"

    return _mutate(action, actor, backup=False)


def set_mail_enabled(key: str, enabled: bool, actor: str = "") -> str:
    """账号管理表格里的邮件开关：只翻 MAIL_ENABLED 这一格。

    开的时候要求这一行的邮箱已经填全（平台、地址、密码）——和 TG 开关一样，在锁里当场读
    文件判断，不信页面上看到的。
    """

    def action(sheet, index) -> str:
        row = _locate(sheet, index, key)
        if enabled:
            def value(name: str) -> str:
                return _clean(sheet.cell(row=row, column=index[name]).value) if name in index else ""

            box = mail_inbox.mailbox_for(
                value("mail_provider"), value("mail_address"), value("mail_password"), value("mail_server")
            )
            if box is None:
                raise ExcelSourceError("这个账号还没有填好告警邮箱（平台、地址、密码），先点「修改」填上再开。")
        column = _ensure_column(sheet, index, "mail_enabled")
        if _to_flag(sheet.cell(row=row, column=column).value) == enabled:
            return ""
        sheet.cell(row=row, column=column).value = bool(enabled)
        return f"{'开启' if enabled else '关闭'}账号 {_account_label(sheet, index, row)} 的邮件告警"

    return _mutate(action, actor, backup=False)


def set_account_budget(key: str, budget: float, actor: str = "", note: str = "") -> str:
    """只改一个账号的额度（账号管理表格里点额度旁边的笔、账号页的「调整额度」）。

    和客户页的「调整额度」一样记一条「调整额度」（改前 → 改后、谁、几点几分）：分给了客户的记在那个客户
    名下，库存里的只记账号。额度没变返回空串，不写文件。
    """
    if budget != budget or budget in (float("inf"), float("-inf")):
        raise ExcelSourceError("额度要填数字。")
    if budget < 0:
        raise ExcelSourceError("额度不能是负数。")

    def action(sheet, index) -> str:
        row = _locate(sheet, index, key)
        fields = _customer_fields(sheet, index, row)
        before = fields["budget"]
        if before == budget:
            return ""
        sheet.cell(row=row, column=_ensure_column(sheet, index, "budget")).value = budget
        _write_event(sheet.parent, when=date.today(), customer=fields["customer"], kind="budget",
                     account=fields["account"], amount=budget, before=before, note=note, actor=actor)
        owner = f"（客户 {_customer_label(sheet.parent, fields['customer'])}）" if fields["customer"] else "（库存）"
        return Note(f"修改账号 {_account_label(sheet, index, row)}：BUDGET {_plain(before)} → {_plain(budget)}{owner}",
                    {"budget": ""})

    return _mutate(action, actor, backup=True)


def set_enabled(key: str, enabled: bool, actor: str = "") -> str:
    """软删 / 恢复：翻 ENABLED 这一格，行本身留在原地。时间线上记一条「停用」/「恢复启用」。"""

    def action(sheet, index) -> str:
        row = _locate(sheet, index, key)
        column = _ensure_column(sheet, index, "enabled")
        if _to_enabled(sheet.cell(row=row, column=column).value) == enabled:
            return ""
        fields = _customer_fields(sheet, index, row)
        sheet.cell(row=row, column=column, value=bool(enabled))
        if fields["account"] != NO_ACCOUNT:
            _write_event(sheet.parent, when=date.today(), customer=fields["customer"],
                         kind="enable" if enabled else "disable", account=fields["account"], actor=actor)
        return f"{'恢复' if enabled else '停用'}账号 {_account_label(sheet, index, row)}"

    return _mutate(action, actor, backup=False)


def delete_account(key: str, actor: str = "") -> str:
    """真删除：把这一行整行清空——额度、比率、TAG、AK/SK、TG 群、告警邮箱全都清掉。

    和停用不同，这是连凭证一起彻底去掉，页面上确认时要重新输一遍登录密码。
    但**不删行**：删行会让下面所有账号的行号上移，account.key 和各处的缓存键跟着错位
    （见模块说明）。清空的行读取时当空行跳过；以后新增账号追加在最后一个非空行后面，
    这一行就空着留在原处。按约定删除不备份（只有新增和修改才备份）。
    """

    def action(sheet, index) -> str:
        row = _locate(sheet, index, key)
        partner = _clean(sheet.cell(row=row, column=index["partner"]).value) or NO_PARTNER
        who = _account_label(sheet, index, row)       # 清空之前先记下是谁
        for column in range(1, sheet.max_column + 1):
            # 写 .value = None 才真的清空（cell(..., value=None) 会被 openpyxl 当成没给值）
            sheet.cell(row=row, column=column).value = None
        return f"删除账号 {who}，上游 {partner}"

    return _mutate(action, actor, backup=False)


# ---------------------------------------------------------------- 生命周期
# 表格里直接点改某个账号的标签、在标签清单里增删标签，都不备份（只有新增和修改账号才备份）。
def set_lifecycle(key: str, tags: list[str], actor: str = "") -> str:
    """账号管理表格里直接改一个账号的生命周期：只写 LIFECYCLE 这一格。"""
    clean = list(dict.fromkeys(name.strip() for name in tags if name and name.strip()))

    def action(sheet, index) -> str:
        row = _locate(sheet, index, key)
        column = _ensure_column(sheet, index, "lifecycle")
        before = split_lifecycle(sheet.cell(row=row, column=column).value)
        # 长度只管新加的：这一格里本来就有的长标签（手改 Excel 写的）不挡保存
        too_long = [name for name in clean if len(name) > MAX_LIFECYCLE_NAME and name not in before]
        if too_long:
            raise ExcelSourceError(
                f"生命周期标签最多 {MAX_LIFECYCLE_NAME} 个字：「{'」「'.join(too_long)}」太长了。"
            )
        if list(before) == clean:
            return ""
        fields = _customer_fields(sheet, index, row)
        sheet.cell(row=row, column=column).value = ",".join(clean) or None
        # 打上 / 去掉「风控」：客户时间线上记一笔
        _log_account_change(sheet, index, row, fields, actor)
        return (f"账号 {_account_label(sheet, index, row)} 的生命周期："
                f"{'、'.join(before) or '空'} → {'、'.join(clean) or '空'}")

    return _mutate(action, actor, backup=False)


def _lifecycle_sheet(book):
    """拿到 LIFECYCLE 工作表；还没有就建一张，先写上默认的三个，再在上面增删。"""
    if LIFECYCLE_SHEET in book.sheetnames:
        return book[LIFECYCLE_SHEET]
    sheet = book.create_sheet(LIFECYCLE_SHEET)
    sheet.append(["NAME", "COLOR"])
    for name, color in DEFAULT_LIFECYCLE:
        sheet.append([name, color])
    return sheet


def _sheet_tags(sheet) -> list[tuple[int, str]]:
    """LIFECYCLE 表里的 (行号, 名字)，跳过空行。"""
    return [
        (row, _clean(sheet.cell(row=row, column=1).value))
        for row in range(2, sheet.max_row + 1)
        if _clean(sheet.cell(row=row, column=1).value)
    ]


def add_lifecycle(name: str, color: str, actor: str = "") -> str:
    """在标签清单里加一个。"""
    name = _clean(name)
    if not name:
        raise ExcelSourceError("标签名不能为空。")
    if len(name) > MAX_LIFECYCLE_NAME:
        raise ExcelSourceError(f"标签名最多 {MAX_LIFECYCLE_NAME} 个字。")
    if _LIFE_SEPARATORS.search(name):
        raise ExcelSourceError("标签名里不能有逗号、顿号、分号或竖线。")
    if color not in LIFECYCLE_COLORS:
        raise ExcelSourceError("请从色块里选一个颜色。")

    def action(sheet, index) -> str:
        tags = _lifecycle_sheet(sheet.parent)
        existing = _sheet_tags(tags)
        if any(found == name for _, found in existing):
            raise ExcelSourceError(f"已经有「{name}」这个标签了。")
        if len(existing) >= MAX_LIFECYCLE_TAGS:
            raise ExcelSourceError(f"标签最多 {MAX_LIFECYCLE_TAGS} 个。")
        tags.append([name, color])
        return f"新增生命周期标签「{name}」（{LIFECYCLE_COLORS[color][0]}色）"

    return _mutate(action, actor, backup=False)


def remove_lifecycle(name: str, actor: str = "") -> str:
    """从标签清单里删掉一个，打了这个标签的账号上一起去掉。"""
    name = _clean(name)

    def action(sheet, index) -> str:
        tags = _lifecycle_sheet(sheet.parent)
        rows = [row for row, found in _sheet_tags(tags) if found == name]
        if not rows:
            return ""
        for row in reversed(rows):
            tags.delete_rows(row)   # 清单表单独一张，删行不影响账号的行号
        touched = 0
        if "lifecycle" in index:
            column = index["lifecycle"]
            for row in range(2, sheet.max_row + 1):
                cell = sheet.cell(row=row, column=column)
                current = split_lifecycle(cell.value)
                if name in current:
                    kept = [tag for tag in current if tag != name]
                    cell.value = ",".join(kept) or None
                    touched += 1
        extra = f"（{touched} 个账号上的一起去掉）" if touched else ""
        return f"删除生命周期标签「{name}」{extra}"

    return _mutate(action, actor, backup=False)


# ==================================================================== 客户：写入
# 页面上的几个动作（新建 / 修改客户、分配、替换、标记风控、结算、调整额度、解绑、记一笔、改 / 删时间线上
# 的事）都从这里走，和账号的写入共用同一条流水线（_mutate：加锁、原子替换、清缓存、记审计）。每个动作
# 改账号和记事件在**同一次写入**里完成，不会出现「账号换了、时间线上没记」的半截状态。
# 只有新建、修改客户和调整额度会先备份（改的是人填的资料和钱），其余和开关一样不备份。
#
# 每个动作在锁里当场读文件核对：账号还是不是这个客户的、是不是已经结算了、新账号是不是还在库存里——
# 页面可能是几分钟前打开的，不信页面上看到的。

def _sheet_with_header(book, name: str, header: tuple[str, ...]):
    """拿到附属工作表；还没有就建一张。表头缺的列在右边补上（老表加了新列也能写）。"""
    sheet = book[name] if name in book.sheetnames else book.create_sheet(name)
    titles = [_clean(sheet.cell(row=1, column=c).value).upper() for c in range(1, sheet.max_column + 1)]
    if not any(titles):
        for column, title in enumerate(header, start=1):
            sheet.cell(row=1, column=column, value=title)
        return sheet
    for title in header:
        if title not in titles:
            titles.append(title)
            sheet.cell(row=1, column=len(titles), value=title)
    return sheet


def _titles(sheet, header: tuple[str, ...]) -> dict[str, int]:
    """附属工作表的 表头 -> 列号（1 起）。"""
    index: dict[str, int] = {}
    for column in range(1, sheet.max_column + 1):
        title = _clean(sheet.cell(row=1, column=column).value).upper()
        if title in header and title not in index:
            index[title] = column
    return index


def _next_free_row(sheet, index: dict[str, int]) -> int:
    """最后一行有内容的行的下一行（只设过格式的空行不算）。"""
    last = 1
    for row in range(2, sheet.max_row + 1):
        if any(_clean(sheet.cell(row=row, column=column).value) for column in index.values()):
            last = row
    return last + 1


def _put(cell, value) -> None:
    """写一格：字符串走 _put_text（「=」开头的不当公式），空串写成空格子。"""
    if isinstance(value, str):
        _put_text(cell, value)
    else:
        cell.value = value


def _customer_row(book, cid: str) -> tuple[object, dict[str, int], int]:
    """客户在 CUSTOMERS 表里的 (表, 表头, 行号)。没有这个客户就报错。"""
    if CUSTOMERS_SHEET not in book.sheetnames:
        raise ExcelSourceError(f"台账里没有客户 {cid}，请刷新页面后重试。")
    sheet = book[CUSTOMERS_SHEET]
    index = _titles(sheet, CUSTOMER_HEADER)
    if "ID" in index:
        for row in range(2, sheet.max_row + 1):
            if _to_customer_id(sheet.cell(row=row, column=index["ID"]).value) == cid:
                return sheet, index, row
    raise ExcelSourceError(f"台账里没有客户 {cid}，请刷新页面后重试。")


def _write_event(
    book, *, when: date, customer: str, kind: str, account: str = "", amount: float | None = None,
    before: float | None = None, peer: str = "", note: str = "", source: str = "manual", key: str = "",
    deleted: bool = False, actor: str = "",
) -> int:
    """在 EVENTS 表末尾记一件事，返回它的编号。"""
    sheet = _sheet_with_header(book, EVENTS_SHEET, EVENT_HEADER)
    index = _titles(sheet, EVENT_HEADER)
    taken = [_to_number(sheet.cell(row=row, column=index["ID"]).value, 0.0) for row in range(2, sheet.max_row + 1)]
    ident = int(max(taken, default=0.0)) + 1
    row = _next_free_row(sheet, index)
    values = {
        "ID": ident, "DATE": when, "CUSTOMER": customer, "ACCOUNT": account, "TYPE": kind,
        "AMOUNT": amount, "BEFORE": before, "PEER": peer, "NOTE": note, "SOURCE": source, "KEY": key,
        "DELETED": True if deleted else None, "CREATED_AT": datetime.now().replace(microsecond=0),
        "ACTOR": actor,
    }
    for title, value in values.items():
        _put(sheet.cell(row=row, column=index[title]), value)
    return ident


def _customer_fields(sheet, index: dict[str, int], row: int) -> dict:
    """账号这一行和客户有关的几格，改之前、改之后各取一份比对用。"""
    def value(name: str):
        return sheet.cell(row=row, column=index[name]).value if name in index else None

    return {
        "account": _to_account_id(value("account")) or NO_ACCOUNT,
        "customer": _to_customer_id(value("customer")),
        "budget": _to_number(value("budget")),
        "lifecycle": split_lifecycle(value("lifecycle")),
        "settled": _to_date(value("settled")),
        "enabled": _to_enabled(value("enabled")),
        "start_date": _to_date(value("start_date")),
    }


def _log_account_change(sheet, index: dict[str, int], row: int, before: dict, actor: str) -> None:
    """账号管理里改了客户、额度、「风控」标签：时间线上跟着记一笔。

    分给了客户的记在那个客户名下（客户页、账号页的时间线上都有）；库存里的没有客户，只记账号——
    账号页的时间线按账号看，换过几个客户、解绑过都追得回来。
    """
    after = _customer_fields(sheet, index, row)
    book = sheet.parent
    today = date.today()
    number = after["account"]
    if before["customer"] != after["customer"]:
        if before["customer"]:
            _write_event(book, when=today, customer=before["customer"], kind="unassign",
                         account=before["account"], note="在账号管理里改了客户", actor=actor)
        if after["customer"]:
            _customer_row(book, after["customer"])
            # 在账号管理里给老账号填客户（多半是上线那天归类）：分配日期记启用日期
            started = after["start_date"]
            _write_event(book, when=started if started and started < today else today, customer=after["customer"],
                         kind="assign", account=number, amount=after["budget"], actor=actor)
        # 换了客户：结算日期是上一个客户那边的事
        if after["settled"] is not None:
            sheet.cell(row=row, column=index["settled"]).value = None
        return
    owner = after["customer"]
    if before["budget"] != after["budget"]:
        _write_event(book, when=today, customer=owner, kind="budget", account=number,
                     amount=after["budget"], before=before["budget"], actor=actor)
    had, has = TAG_RISK in before["lifecycle"], TAG_RISK in after["lifecycle"]
    if had != has:
        _write_event(book, when=today, customer=owner, kind="risk" if has else "unrisk", account=number,
                     actor=actor)


def _ensure_listed(book, names) -> None:
    """客户流程打上的标签不在清单里（被人删了）就加回去，免得账号管理里显示成清单外的灰标签。"""
    defaults = dict(DEFAULT_LIFECYCLE)
    if LIFECYCLE_SHEET not in book.sheetnames and all(name in defaults for name in names):
        return   # 没有清单表 = 用默认的三个，本来就在
    tags = _lifecycle_sheet(book)
    present = {found for _, found in _sheet_tags(tags)}
    for name in names:
        if name not in present:
            tags.append([name, defaults.get(name, "gray")])


def _retag(sheet, index: dict[str, int], row: int, add=(), remove=()) -> None:
    """改账号的生命周期：加上 add、去掉 remove，别的标签原样留着、顺序不动。"""
    column = _ensure_column(sheet, index, "lifecycle")
    before = split_lifecycle(sheet.cell(row=row, column=column).value)
    after = [name for name in before if name not in remove]
    for name in add:
        if name not in after:
            after.append(name)
    if list(before) != after:
        sheet.cell(row=row, column=column).value = ",".join(after) or None
        _ensure_listed(sheet.parent, [name for name in add if name not in before])


def _restart(sheet, index: dict[str, int], row: int, tags) -> None:
    """从库存分出去（分配、替换上来）的账号：上一段合作留下的「结算」去掉；没有「风控」的标上「正常」。

    「风控」不动：那是 AWS 那边的事，分给哪个客户都还在——客户页上它就是「风控 · 待替换」，不算进预算和余额。
    要取消，在客户页「取消风控」或者账号管理里改生命周期。
    """
    risky = TAG_RISK in tags
    _retag(sheet, index, row, add=() if risky else (TAG_NORMAL,), remove=(TAG_SETTLE,))


def _owned_row(sheet, index: dict[str, int], key: str, cid: str, *, allow_settled: bool = False) -> int:
    """账号在台账里的行号，并核对它确实是这个客户的（还没结算）。"""
    row = _locate(sheet, index, key)
    fields = _customer_fields(sheet, index, row)
    if fields["customer"] != cid:
        raise LedgerConflict(f"账号 {fields['account']} 已经不是这个客户的了，请刷新页面后重试。")
    if fields["settled"] is not None and not allow_settled:
        raise LedgerConflict(f"账号 {fields['account']} 已经结算了，请刷新页面后重试。")
    return row


_CUSTOMER_NORMALIZE = {
    "ID": _to_customer_id,
    "NAME": _clean,
    "REGION": lambda v: _clean(v).upper(),
    "AVATAR": _to_customer_avatar,
    "STATUS": lambda v: CUSTOMER_STATUSES[_to_status(v)],
    "SINCE": _to_date,
    "NOTE": _clean,
}


def _write_customer(sheet, index: dict[str, int], row: int, ident: str, data: dict) -> dict[str, str]:
    """写客户的一行，返回改了的项 {表头: 审计里的那半句}。"""
    values = {
        "ID": ident,
        "NAME": data["name"],
        "REGION": (data.get("region") or "").upper(),
        "AVATAR": int(data.get("avatar") or 0),
        "STATUS": CUSTOMER_STATUSES.get(data.get("status") or "on", CUSTOMER_STATUSES["on"]),
        "SINCE": data.get("since"),
        "NOTE": data.get("note") or "",
    }
    changes: dict[str, str] = {}
    for title, value in values.items():
        cell = sheet.cell(row=row, column=index[title])
        if _CUSTOMER_NORMALIZE[title](cell.value) == value:
            continue
        changes[title] = f"{title} {_show(cell.value)} → {_show(value)}"
        # 头像 0 = 没选，格子留空
        _put(cell, None if title == "AVATAR" and not value else value)
    return changes


def create_customer(data: dict, actor: str = "") -> str:
    """新建一个客户，返回它的编号（C001、C002……按顺序往下排）。data 是 customers.validate_customer 校验过的。"""
    made: dict[str, str] = {}

    def action(sheet, index) -> str:
        customers = _sheet_with_header(sheet.parent, CUSTOMERS_SHEET, CUSTOMER_HEADER)
        cindex = _titles(customers, CUSTOMER_HEADER)
        numbers = [
            int(text[1:]) for text in (
                _to_customer_id(customers.cell(row=row, column=cindex["ID"]).value)
                for row in range(2, customers.max_row + 1)
            ) if _CUSTOMER_ID.match(text)
        ]
        ident = f"C{max(numbers, default=0) + 1:03d}"
        _write_customer(customers, cindex, _next_free_row(customers, cindex), ident, data)
        made["id"] = ident
        return f"新建客户 {data['name']}（{ident}）"

    _mutate(action, actor, backup=True)
    return made.get("id", "")


def update_customer(cid: str, data: dict, actor: str = "") -> str:
    """改客户资料。什么都没变就返回空串，不写文件。"""

    def action(sheet, index) -> str:
        customers, cindex, row = _customer_row(sheet.parent, cid)
        changes = _write_customer(customers, cindex, row, cid, data)
        if not changes:
            return ""
        return Note(f"修改客户 {_customer_label(sheet.parent, cid)}：" + "；".join(changes.values()),
                    [title.lower() for title in changes])

    return _mutate(action, actor, backup=True)


def assign_accounts(cid: str, keys: list[str], when: date, actor: str = "") -> str:
    """把库存里的账号分给客户：写上客户编号，每个账号记一条「分配账号」。

    生命周期见 _restart：去掉上一段合作的「结算」，「风控」留着（AWS 那边的事，分给谁都还在）。
    """
    if not keys:
        raise ExcelSourceError("先选要分配的账号。")

    def action(sheet, index) -> str:
        book = sheet.parent
        _customer_row(book, cid)
        column = _ensure_column(sheet, index, "customer")
        labels = []
        for key in keys:
            row = _locate(sheet, index, key)
            fields = _customer_fields(sheet, index, row)
            if fields["customer"]:
                raise LedgerConflict(f"账号 {fields['account']} 已经分给客户 {fields['customer']} 了，不在库存里。")
            _put(sheet.cell(row=row, column=column), cid)
            if "settled" in index:
                sheet.cell(row=row, column=index["settled"]).value = None
            _restart(sheet, index, row, fields["lifecycle"])
            _write_event(book, when=when, customer=cid, kind="assign", account=fields["account"],
                         amount=fields["budget"], actor=actor)
            labels.append(_account_label(sheet, index, row))
        return f"客户 {_customer_label(book, cid)}：分配账号 {'、'.join(labels)}，日期 {when.isoformat()}"

    return _mutate(action, actor, backup=False)


def unassign_account(cid: str, key: str, when: date, actor: str = "", note: str = "") -> str:
    """解绑：账号回到库存（客户编号和结算日期清掉），客户时间线上记一条「解绑」。生命周期不动。"""

    def action(sheet, index) -> str:
        row = _owned_row(sheet, index, key, cid, allow_settled=True)
        fields = _customer_fields(sheet, index, row)
        sheet.cell(row=row, column=index["customer"]).value = None
        if "settled" in index:
            sheet.cell(row=row, column=index["settled"]).value = None
        _write_event(sheet.parent, when=when, customer=cid, kind="unassign", account=fields["account"],
                     note=note, actor=actor)
        return f"客户 {_customer_label(sheet.parent, cid)}：解绑账号 {_account_label(sheet, index, row)}，回到库存"

    return _mutate(action, actor, backup=False)


def mark_risk(cid: str, key: str, when: date, actor: str = "", spent: float | None = None,
              note: str = "") -> str:
    """标记风控：生命周期加上「风控」、去掉「正常」，记一条「标记风控」。已经是风控的返回空串。"""

    def action(sheet, index) -> str:
        row = _owned_row(sheet, index, key, cid)
        fields = _customer_fields(sheet, index, row)
        if TAG_RISK in fields["lifecycle"]:
            return ""
        _retag(sheet, index, row, add=(TAG_RISK,), remove=(TAG_NORMAL,))
        _write_event(sheet.parent, when=when, customer=cid, kind="risk", account=fields["account"],
                     amount=spent, note=note, actor=actor)
        return (f"客户 {_customer_label(sheet.parent, cid)}：账号 {_account_label(sheet, index, row)} 标记风控，"
                f"日期 {when.isoformat()}")

    return _mutate(action, actor, backup=False)


def clear_risk(cid: str, key: str, when: date, actor: str = "") -> str:
    """取消风控（标错了）：去掉「风控」、换回「正常」，记一条「取消风控」。"""

    def action(sheet, index) -> str:
        row = _owned_row(sheet, index, key, cid)
        fields = _customer_fields(sheet, index, row)
        if TAG_RISK not in fields["lifecycle"]:
            return ""
        add = () if TAG_SETTLE in fields["lifecycle"] else (TAG_NORMAL,)
        _retag(sheet, index, row, add=add, remove=(TAG_RISK,))
        _write_event(sheet.parent, when=when, customer=cid, kind="unrisk", account=fields["account"], actor=actor)
        return f"客户 {_customer_label(sheet.parent, cid)}：账号 {_account_label(sheet, index, row)} 取消风控"

    return _mutate(action, actor, backup=False)


def restore_account(key: str, when: date, actor: str = "", note: str = "", cid: str = "") -> str:
    """恢复使用：AWS 解除了风控、换下来的账号还要接着用。生命周期去掉「风控」「结算」、标上「正常」，
    记一条「恢复使用」。

    分给了客户的（cid 是页面上看到的客户，对不上就报错）回到使用中：没用完的额度重新算进预算和余额，
    对账单上那次「移出」不再算（就当它没离开过），时间线上之前的风控、替换都留着。库存里的只改生命周期。
    已经结算的不能恢复；本来就没有「风控」「结算」的返回空串。额度用完的恢复了也还是待结算，由调用方先挡。
    """

    def action(sheet, index) -> str:
        row = _owned_row(sheet, index, key, cid) if cid else _locate(sheet, index, key)
        fields = _customer_fields(sheet, index, row)
        owner = fields["customer"]
        if owner and fields["settled"] is not None:
            raise ExcelSourceError("这个账号已经结算了，不能恢复。要再用，先解绑回库存再分配。")
        if TAG_RISK not in fields["lifecycle"] and TAG_SETTLE not in fields["lifecycle"]:
            return ""
        _retag(sheet, index, row, add=(TAG_NORMAL,), remove=(TAG_RISK, TAG_SETTLE))
        _write_event(sheet.parent, when=when, customer=owner, kind="restore", account=fields["account"], note=note,
                     actor=actor)
        who = _account_label(sheet, index, row)
        if owner:
            return f"客户 {_customer_label(sheet.parent, owner)}：账号 {who} 恢复使用，日期 {when.isoformat()}"
        return f"账号 {who}（库存）恢复使用，日期 {when.isoformat()}"

    return _mutate(action, actor, backup=False)


def replace_account(cid: str, old_key: str, new_key: str, reason: str, when: date, actor: str = "",
                    spent: float | None = None, risky: bool = False) -> str:
    """替换：旧账号进入待结算（加「结算」，risky 时连「风控」一起），库存里的新账号分给这个客户。

    记一条「替换账号」（旧 → 新、原因、新账号的额度）；risky 而旧账号还没标过风控的，先补一条「标记风控」。
    """

    def action(sheet, index) -> str:
        book = sheet.parent
        old_row = _owned_row(sheet, index, old_key, cid)
        new_row = _locate(sheet, index, new_key)
        old = _customer_fields(sheet, index, old_row)
        new = _customer_fields(sheet, index, new_row)
        if new_row == old_row:
            raise ExcelSourceError("新账号和旧账号是同一个。")
        if new["customer"]:
            raise LedgerConflict(f"账号 {new['account']} 已经分给客户 {new['customer']} 了，不在库存里。")
        if risky and TAG_RISK not in old["lifecycle"]:
            _write_event(book, when=when, customer=cid, kind="risk", account=old["account"], amount=spent,
                         actor=actor)
        _retag(sheet, index, old_row, add=(TAG_RISK, TAG_SETTLE) if risky else (TAG_SETTLE,), remove=(TAG_NORMAL,))
        _put(sheet.cell(row=new_row, column=_ensure_column(sheet, index, "customer")), cid)
        if "settled" in index:
            sheet.cell(row=new_row, column=index["settled"]).value = None
        _restart(sheet, index, new_row, new["lifecycle"])
        _write_event(book, when=when, customer=cid, kind="replace", account=old["account"], peer=new["account"],
                     amount=new["budget"], before=spent, note=reason, actor=actor)
        return (f"客户 {_customer_label(book, cid)}：账号 {_account_label(sheet, index, old_row)} 换成 "
                f"{_account_label(sheet, index, new_row)}，原因 {reason or '没写'}，日期 {when.isoformat()}")

    return _mutate(action, actor, backup=False)


def settle_account(cid: str, key: str, when: date, actor: str = "", spent: float | None = None,
                   disable: bool = False, note: str = "") -> str:
    """结算：写上结算日期、生命周期加「结算」，记一条「结算」（这个账号最后的消费）。disable 时顺手停用。"""

    def action(sheet, index) -> str:
        row = _owned_row(sheet, index, key, cid)
        fields = _customer_fields(sheet, index, row)
        sheet.cell(row=row, column=_ensure_column(sheet, index, "settled")).value = when
        _retag(sheet, index, row, add=(TAG_SETTLE,), remove=(TAG_NORMAL,))
        off = ""
        if disable and fields["enabled"]:
            sheet.cell(row=row, column=_ensure_column(sheet, index, "enabled")).value = False
            off = "，同时停用"
        _write_event(sheet.parent, when=when, customer=cid, kind="settle", account=fields["account"],
                     amount=spent, note=note, actor=actor)
        if off:
            _write_event(sheet.parent, when=when, customer=cid, kind="disable", account=fields["account"],
                         actor=actor)
        return (f"客户 {_customer_label(sheet.parent, cid)}：账号 {_account_label(sheet, index, row)} 结算，"
                f"日期 {when.isoformat()}{off}")

    return _mutate(action, actor, backup=False)


def set_budget(cid: str, key: str, budget: float, when: date, actor: str = "", note: str = "",
               revive: bool = False) -> str:
    """调整账号额度（加额度），记一条「调整额度」（改前 → 改后）。

    revive：这个账号是因为额度用完才进的待结算，加完额度又够用了——去掉「结算」、换回「正常」，
    回到使用中（由调用方按消费判断，这里只管改标签）。
    """
    if budget < 0:
        raise ExcelSourceError("额度不能是负数。")

    def action(sheet, index) -> str:
        row = _owned_row(sheet, index, key, cid)
        fields = _customer_fields(sheet, index, row)
        if fields["budget"] == budget:
            return ""
        sheet.cell(row=row, column=index["budget"]).value = budget
        if revive and TAG_RISK not in fields["lifecycle"]:
            _retag(sheet, index, row, add=(TAG_NORMAL,), remove=(TAG_SETTLE,))
        _write_event(sheet.parent, when=when, customer=cid, kind="budget", account=fields["account"],
                     amount=budget, before=fields["budget"], note=note, actor=actor)
        return (f"客户 {_customer_label(sheet.parent, cid)}：账号 {_account_label(sheet, index, row)} 的额度 "
                f"{_plain(fields['budget'])} → {_plain(budget)}，日期 {when.isoformat()}")

    return _mutate(action, actor, backup=True)


def add_customer_event(cid: str, kind: str, when: date, actor: str = "", account: str = "",
                       note: str = "") -> str:
    """时间线上手动记一笔（备注、手动标的开始上量 / 上量终止 / 恢复上量）。"""

    def action(sheet, index) -> str:
        _customer_row(sheet.parent, cid)
        ident = _write_event(sheet.parent, when=when, customer=cid, kind=kind, account=account, note=note,
                             actor=actor)
        which = f"，账号 {_number_label(sheet, index, account)}" if account else ""
        return (f"客户 {_customer_label(sheet.parent, cid)} 的时间线记一笔：#{ident}「{EVENT_TITLES.get(kind, kind)}」"
                f"{which}，日期 {when.isoformat()}")

    return _mutate(action, actor, backup=False)


def add_account_event(key: str, kind: str, when: date, actor: str = "", note: str = "",
                      spent: float | None = None) -> str:
    """账号页的时间线上手动记一笔：记在这个账号现在归的那个客户名下（库存里的只记账号）。

    kind 是 "risk"（标记风控）时和客户页的一样改生命周期：加上「风控」、去掉「正常」，分给了客户的从这天起
    不算进余额。已经是风控的返回空串；已经结算的不用再标。spent 是到现在用了多少，记在那一条上。
    """

    def action(sheet, index) -> str:
        row = _locate(sheet, index, key)
        fields = _customer_fields(sheet, index, row)
        number, owner = fields["account"], fields["customer"]
        if kind == "risk":
            if TAG_RISK in fields["lifecycle"]:
                return ""
            if owner and fields["settled"] is not None:
                raise ExcelSourceError("这个账号已经结算了，不用再标风控。")
            _retag(sheet, index, row, add=(TAG_RISK,), remove=(TAG_NORMAL,))
            _write_event(sheet.parent, when=when, customer=owner, kind="risk", account=number, amount=spent,
                         note=note, actor=actor)
            who = _account_label(sheet, index, row)
            if owner:
                return f"客户 {_customer_label(sheet.parent, owner)}：账号 {who} 标记风控，日期 {when.isoformat()}"
            return f"账号 {who}（库存）标记风控，日期 {when.isoformat()}"
        if kind not in NOTE_TYPES:
            raise ExcelSourceError(f"认不出这一类：{kind}")
        ident = _write_event(sheet.parent, when=when, customer=owner, kind=kind, account=number, note=note,
                             actor=actor)
        what = f"#{ident}「{EVENT_TITLES[kind]}」"
        who = _account_label(sheet, index, row)
        if owner:
            return (f"客户 {_customer_label(sheet.parent, owner)} 的时间线记一笔：{what}，账号 {who}，"
                    f"日期 {when.isoformat()}")
        return f"账号 {who}（库存）的时间线记一笔：{what}，日期 {when.isoformat()}"

    return _mutate(action, actor, backup=False)


def change_account_event(number: str, ident: str, actor: str = "", *, when: date | None = None,
                         delete: bool = False) -> str:
    """账号页的时间线上改一笔手动记的事的日期，或者删掉它（打上 DELETED，行留着）。

    只认这个账号的、「记一笔」那几类（NOTE_TYPES）：分配、调额度、风控、替换、结算这些连着钱和阶段，
    去客户页的时间线上改。
    """
    if not delete and when is None:
        raise ExcelSourceError("没有给新的日期。")
    if not ident.isdigit():
        raise ExcelSourceError(f"认不出这条事件：{ident}")

    def action(sheet, index) -> str:
        events = _sheet_with_header(sheet.parent, EVENTS_SHEET, EVENT_HEADER)
        eindex = _titles(events, EVENT_HEADER)
        row = next((r for r in range(2, events.max_row + 1)
                    if int(_to_number(events.cell(row=r, column=eindex["ID"]).value, 0.0)) == int(ident)), None)
        if row is None or _to_account_id(events.cell(row=row, column=eindex["ACCOUNT"]).value) != number:
            raise LedgerConflict("时间线上已经没有这条了，请刷新页面后重试。")
        kind = _clean(events.cell(row=row, column=eindex["TYPE"]).value).lower()
        auto = _clean(events.cell(row=row, column=eindex["SOURCE"]).value).lower() == "auto"
        if auto or kind not in NOTE_TYPES:
            raise ExcelSourceError("这一条连着钱和阶段，要去客户页的时间线上改。")
        before = _to_date(events.cell(row=row, column=eindex["DATE"]).value)
        what = f"#{ident}「{EVENT_TITLES[kind]}」"
        head = f"账号 {_number_label(sheet, index, number)} 的时间线"
        if delete:
            if _to_flag(events.cell(row=row, column=eindex["DELETED"]).value):
                return ""
            events.cell(row=row, column=eindex["DELETED"]).value = True
            return f"{head}：删掉 {what}（日期 {_show(before)}）"
        if before == when:
            return ""
        events.cell(row=row, column=eindex["DATE"]).value = when
        return f"{head}：{what} 的日期 {_show(before)} → {when.isoformat()}"

    return _mutate(action, actor, backup=False)


def _event_row(sheet, index: dict[str, int], cid: str, ident: int) -> int:
    for row in range(2, sheet.max_row + 1):
        if (int(_to_number(sheet.cell(row=row, column=index["ID"]).value, 0.0)) == ident
                and _to_customer_id(sheet.cell(row=row, column=index["CUSTOMER"]).value) == cid):
            return row
    raise LedgerConflict("时间线上已经没有这条了，请刷新页面后重试。")


def _override_row(sheet, index: dict[str, int], cid: str, key: str) -> int | None:
    """对某条自动事件的改动记在哪一行（同一条只留一行改动）。"""
    for row in range(2, sheet.max_row + 1):
        if (_clean(sheet.cell(row=row, column=index["SOURCE"]).value).lower() == "auto"
                and _clean(sheet.cell(row=row, column=index["KEY"]).value) == key
                and _to_customer_id(sheet.cell(row=row, column=index["CUSTOMER"]).value) == cid):
            return row
    return None


def change_customer_event(cid: str, ident: str, actor: str = "", *, when: date | None = None,
                          delete: bool = False, auto_kind: str = "", auto_account: str = "",
                          auto_date: date | None = None, auto_title: str = "") -> str:
    """改时间线上一件事的日期，或者删掉它。

    ident 是手动事件的编号（"12"），或者自动事件的标识（"auto:…"）。手动的直接改那一行（删掉是打上
    DELETED，行留着）；自动的本身不存，记一行改动：改了日期、删掉了——删掉的以后不会再自动生成。
    auto_kind / auto_account / auto_date 是自动事件原来的类别、账号、日期，记在改动那一行里方便人看；
    auto_title 是它在时间线上的标题（「额度 70%」这种），写进操作日志。
    """
    if not delete and when is None:
        raise ExcelSourceError("没有给新的日期。")

    def action(sheet, index) -> str:
        book = sheet.parent
        _customer_row(book, cid)
        events = _sheet_with_header(book, EVENTS_SHEET, EVENT_HEADER)
        eindex = _titles(events, EVENT_HEADER)
        if ident.startswith("auto:"):
            key = ident[len("auto:"):]
            row = _override_row(events, eindex, cid, key)
            if row is None:
                _write_event(book, when=when or auto_date or date.today(), customer=cid, kind=auto_kind or "auto",
                             account=auto_account, source="auto", key=key, deleted=delete, actor=actor)
            else:
                if when is not None:
                    events.cell(row=row, column=eindex["DATE"]).value = when
                events.cell(row=row, column=eindex["DELETED"]).value = True if delete else None
                _put(events.cell(row=row, column=eindex["ACTOR"]), actor)
            done = "删掉" if delete else f"改到 {when.isoformat()}"
            bits = [_number_label(sheet, index, auto_account) if auto_account else "",
                    f"原本 {auto_date.isoformat()}" if auto_date else ""]
            which = "，".join(bit for bit in bits if bit)
            return (f"客户 {_customer_label(book, cid)} 的时间线：自动记的「{auto_title or EVENT_TITLES.get(auto_kind, auto_kind)}」"
                    f"{f'（{which}）' if which else ''}{done}")
        if not ident.isdigit():
            raise ExcelSourceError(f"认不出这条事件：{ident}")
        row = _event_row(events, eindex, cid, int(ident))
        kind = _clean(events.cell(row=row, column=eindex["TYPE"]).value).lower()
        number = _to_account_id(events.cell(row=row, column=eindex["ACCOUNT"]).value)
        before = _to_date(events.cell(row=row, column=eindex["DATE"]).value)
        what = f"#{ident}「{EVENT_TITLES.get(kind, kind)}」" + (f"（账号 {_number_label(sheet, index, number)}）" if number else "")
        head = f"客户 {_customer_label(book, cid)} 的时间线"
        if delete:
            if _to_flag(events.cell(row=row, column=eindex["DELETED"]).value):
                return ""
            events.cell(row=row, column=eindex["DELETED"]).value = True
            return f"{head}：删掉 {what}，原本的日期 {_show(before)}"
        if before == when:
            return ""
        events.cell(row=row, column=eindex["DATE"]).value = when
        return f"{head}：{what} 的日期 {_show(before)} → {when.isoformat()}"

    return _mutate(action, actor, backup=False)
