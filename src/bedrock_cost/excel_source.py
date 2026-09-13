"""cred.xlsx 账号台账的读与写。

AK/SK 只在内存里传给 boto3：dataclass 的 repr 里被屏蔽，也不会进模板或日志。
文件按 mtime+size 缓存，改完 Excel 刷新页面就能生效，不用重启服务。

写入（账号管理页用）走 create_account / update_account / set_enabled 三个入口，
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

from . import config

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

# 可选列。老台账没有这三列也能正常读：
#   TAG        缺失时回落到 .env 里的 TAG_KEY
#   ENABLED    缺失时所有账号都算启用（软删用的开关，见 set_enabled）
#   START_DATE 账号的启用日期。概览页的消费和余额从这一天累计到今天，缺失时
#              回落到 Cost Explorer 能查到的最早一天（见 dates.cumulative_range）
OPTIONAL_COLUMNS = {
    "TAG": "tag_spec",
    "ENABLED": "enabled",
    "START_DATE": "start_date",
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
EDITABLE = (
    "partner", "account", "budget", "tag_ratio", "untag_ratio", "tag_spec", "start_date",
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
}

_ACCOUNT_ID = re.compile(r"^\d{12}$")
_AK_SHAPE = re.compile(r"^[A-Z0-9]{16,128}$")

_write_lock = threading.Lock()


class LedgerConflict(ExcelSourceError):
    """要改的那一行已经不是页面上看到的账号了（文件被别的途径换过）。"""


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


def validate(form: dict, others: list[Account], creating: bool) -> tuple[dict, list[str]]:
    """把表单文本校验成可以写进台账的一行。

    返回 (清洗后的字段, 错误列表)。错误一次收齐再返回——填错三个字段却只被
    告知一个，用户要来回提交三次。

    others 是「除自己以外的全部账号」，含已停用的：停用不等于账号 ID 可以被
    别人重用，否则恢复的时候就撞车了。
    """
    errors: list[str] = []
    data = {name: _clean(form.get(name)) for name in (*EDITABLE, *CREATE_ONLY)}

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
        started = data.get("start_date")
        when = f"，启用日期 {started.isoformat()}" if started else "（未设启用日期）"
        return f"新增账号 {data['account']}（{data['partner']}），额度 {data['budget']:g}{when}"

    return _mutate(action, actor)


def update_account(key: str, data: dict, actor: str = "") -> str:
    """改一行的非凭证字段。

    AK/SK 两列一个字节都不碰——要换凭证的做法是停用旧账号、新建一条。
    没有任何字段变化时返回空串，不会产生备份，也不会动文件的 mtime。
    """

    def action(sheet, index) -> str:
        row = _locate(sheet, index, key)
        changes = _write_editable(sheet, index, row, data)
        if not changes:
            return ""
        return f"修改账号 {data['account']}：" + "；".join(changes)

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
