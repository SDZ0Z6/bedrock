"""从 cred.xlsx 读取账号台账。

AK/SK 只在内存里传给 boto3：dataclass 的 repr 里被屏蔽，也不会进模板或日志。
文件按 mtime+size 缓存，改完 Excel 刷新页面就能生效，不用重启服务。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
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

# 可选列。缺失时回落到 .env 里的 TAG_KEY。
OPTIONAL_COLUMNS = {
    "TAG": "tag_spec",
}

COLUMNS = {**REQUIRED_COLUMNS, **OPTIONAL_COLUMNS}

# TAG 列里 键 与 值 的分隔符。'$' 是 Cost Explorer 自己的分组键写法，一并兼容。
_TAG_SEPARATORS = "=:$"


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
                    partner=partner or "(未填写上游)",
                    account=account_id or "(未填写账号)",
                    budget=_to_number(cell(row, "budget")),
                    tag_ratio=_to_number(cell(row, "tag_ratio"), 1.0),
                    untag_ratio=_to_number(cell(row, "untag_ratio"), 1.0),
                    ak=_clean(cell(row, "ak")),
                    sk=_clean(cell(row, "sk")),
                    row=row_number,
                    tag_spec=_clean(cell(row, "tag_spec")),
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


def load_accounts(force: bool = False) -> list[Account]:
    """读取全部账号。文件没变动时直接返回缓存。"""
    path = config.EXCEL_PATH
    if not path.is_file():
        raise ExcelSourceError(f"找不到账号台账文件：{path}")

    stat = path.stat()
    stamp = (str(path), stat.st_mtime_ns, stat.st_size)
    with _cache_lock:
        if not force and _cache["stamp"] == stamp:
            return list(_cache["accounts"])  # type: ignore[arg-type]

    accounts = _read_workbook(path)
    with _cache_lock:
        _cache["stamp"] = stamp
        _cache["accounts"] = accounts
    return list(accounts)
