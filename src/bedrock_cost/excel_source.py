"""cred.xlsx 账号台账的读与写。

AK/SK 只在内存里传给 boto3：dataclass 的 repr 里被屏蔽，也不会进模板或日志。告警邮箱的
密码（MAIL_PASSWORD）同样处理，只交给 IMAP 登录用。
文件按 mtime+size 缓存，改完 Excel 刷新页面就能生效，不用重启服务。

写入（账号管理页用）走 create_account / update_account / set_enabled 和两个开关入口，
它们共用同一条流水线：加锁 -> 校验 -> 备份 -> 临时文件原子替换 -> 清缓存 -> 记审计。
删除是软删：只把 ENABLED 列改成 FALSE，行本身留着。这样行号不会移动，
account.key（"账号#行号"）和各处按它建的缓存键就都还稳。
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

from . import config, mail_inbox

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
}

COLUMNS = {**REQUIRED_COLUMNS, **OPTIONAL_COLUMNS}

# TAG 列里 键 与 值 的分隔符。'$' 是 Cost Explorer 自己的分组键写法，一并兼容。
_TAG_SEPARATORS = "=:$"

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


_cache_lock = threading.Lock()
_cache: dict[str, object] = {"stamp": None, "accounts": []}


def _read_workbook(path: Path) -> list[Account]:
    workbook = openpyxl.load_workbook(path, data_only=True, read_only=True)
    try:
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
                )
            )
        return accounts
    finally:
        workbook.close()


def clear_cache() -> None:
    """丢掉台账缓存，下次 load_accounts 会重新读文件。"""
    with _cache_lock:
        _cache["stamp"] = None
        _cache["accounts"] = []


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

    accounts = _read_workbook(path)
    with _cache_lock:
        _cache["stamp"] = stamp
        _cache["accounts"] = accounts
    return list(accounts) if include_disabled else [a for a in accounts if a.enabled]


# ==================================================================== 写入
# 账号管理页的三个入口都从这里走。设计约束有三条：
#   1. AK/SK 只在新建时写一次，之后任何编辑都不碰这两列（要换凭证就停用重建）；
#   2. 删除是软删，只改 ENABLED 列，行号不动——account.key 里带行号，
#      各处的缓存键也带，真删行会让下面所有账号的 key 集体位移；
#   3. 写盘一律「先备份、再临时文件、最后 os.replace」，中途崩了不会留下半个文件。

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


def validate(
    form: dict, others: list[Account], creating: bool, current: Account | None = None
) -> tuple[dict, list[str]]:
    """把表单文本校验成可以写进台账的一行。

    返回 (清洗后的字段, 错误列表)。错误一次收齐再返回——填错三个字段却只被
    告知一个，用户要来回提交三次。告警邮箱和 TG 群的错误是 FormError，带着它属于弹窗的
    哪一页；其余的是普通字符串，都在「基础信息」那页。

    others 是「除自己以外的全部账号」，含已停用的：停用不等于账号 ID 可以被
    别人重用，否则恢复的时候就撞车了。current 是修改前的这个账号（新建时没有），
    用来判断邮箱密码能不能留空不改。
    """
    errors: list[str] = []
    data = {name: _clean(form.get(name)) for name in (*EDITABLE, *CREATE_ONLY)}
    data["mail_password"] = _clean(form.get("mail_password"))

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


def _mutate(action, actor: str) -> str:
    """写入流水线：加锁 -> 打开 -> action -> 备份 -> 原子替换 -> 清缓存 -> 审计。

    action(sheet, index) 返回一句审计描述；返回空表示「没有实际变化」，这时
    既不备份也不写盘。
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
                _backup(path)
                _atomic_save(workbook, path)
        finally:
            workbook.close()

    if not note:
        return ""
    clear_cache()
    _audit(note, actor)
    return note


def _show(value: object) -> str:
    """审计日志里的取值展示：2000.0 写成 2000，日期写成 2026-09-01，空值写成「空」。"""
    if isinstance(value, float):
        return f"{value:g}"
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return _clean(value) or "空"


def _write_editable(sheet, index: dict[str, int], row: int, data: dict) -> list[str]:
    """写入可编辑字段，返回真正发生变化的项（供审计和页面提示用）。"""
    changes: list[str] = []
    for name in EDITABLE:
        column = _ensure_column(sheet, index, name)
        before = sheet.cell(row=row, column=column).value
        after = data[name]
        if _NORMALIZE[name](before) == after:
            continue
        changes.append(
            f"{INTERNAL_TO_EXCEL[name]} {_show(before)} → {_show(after)}"
        )
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
        return f"新增账号 {data['account']}（{data['partner']}），额度 {data['budget']:g}{when}{tg}{mail}"

    return _mutate(action, actor)


def update_account(key: str, data: dict, actor: str = "") -> str:
    """改一行的非凭证字段。

    AK/SK 两列一个字节都不碰——要换凭证的做法是停用旧账号、新建一条。
    没有任何字段变化时返回空串，不会产生备份，也不会动文件的 mtime。
    """

    def action(sheet, index) -> str:
        row = _locate(sheet, index, key)
        changes = _write_editable(sheet, index, row, data)
        changes += _write_mail_password(sheet, index, row, data)
        # 开关不归弹窗管，但群组 ID 全删光了开关还开着，就成了「开着却没处发」——
        # 顺手关掉。和表格里「没填群开不了」是同一条规矩
        if not data["tg_chat_ids"] and "tg_enabled" in index:
            switch = sheet.cell(row=row, column=index["tg_enabled"])
            if _to_flag(switch.value):
                switch.value = False
                changes.append("TG_ENABLED 开 → 关（群组 ID 全删了）")
        # 邮箱地址删了：同理，邮件告警跟着关
        if not data.get("mail_address") and "mail_enabled" in index:
            switch = sheet.cell(row=row, column=index["mail_enabled"])
            if _to_flag(switch.value):
                switch.value = False
                changes.append("MAIL_ENABLED 开 → 关（告警邮箱删了）")
        if not changes:
            return ""
        return f"修改账号 {data['account']}：" + "；".join(changes)

    return _mutate(action, actor)


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
        return f"{'开启' if enabled else '关闭'}账号 {key.rpartition('#')[0]} 的 TG 告警"

    return _mutate(action, actor)


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
        return f"{'开启' if enabled else '关闭'}账号 {key.rpartition('#')[0]} 的邮件告警"

    return _mutate(action, actor)


def set_enabled(key: str, enabled: bool, actor: str = "") -> str:
    """软删 / 恢复：只翻 ENABLED 这一格，行本身留在原地。"""

    def action(sheet, index) -> str:
        row = _locate(sheet, index, key)
        column = _ensure_column(sheet, index, "enabled")
        if _to_enabled(sheet.cell(row=row, column=column).value) == enabled:
            return ""
        sheet.cell(row=row, column=column, value=bool(enabled))
        return f"{'恢复' if enabled else '停用'}账号 {key.rpartition('#')[0]}"

    return _mutate(action, actor)
