"""客户页要的每个账号每天的消费，在磁盘上存一份。

客户页按账号查 Cost Explorer 的日粒度消费（usage_explorer.account_series，内存里缓存 15 分钟）。
每查成功一次，就把这一整段（每天的 AWS 原价和折算后）存进 CUSTOMER_COSTS_PATH，两个地方用它：

  · 停用的账号不再定时查 CE（省钱），客户页要它的历史时用存下来的；
  · 查询失败时拿上一次的顶着，页面上标出「截至哪天」。

只有 web 进程写它（和 last_known 一样）。写坏了、读不了都当没有，不影响页面：最多是停用账号的
历史显示成「没有数据」。不存任何秘密，只有账号号码和金额。
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

from . import config

_lock = threading.Lock()
_log = logging.getLogger(__name__)


@dataclass
class Saved:
    """存下来的一段：从 start 起每天一格，到 as_of 为止。"""

    start: date
    raw: list[float]
    marked: list[float]
    as_of: date

    @property
    def dates(self) -> list[str]:
        return [(self.start + timedelta(days=offset)).isoformat() for offset in range(len(self.marked))]


def _path() -> Path:
    return config.CUSTOMER_COSTS_PATH


def _read() -> dict:
    try:
        data = json.loads(_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def remember(number: str, dates: list[str], raw: list[float], marked: list[float]) -> None:
    """存一个账号刚查到的一整段（dates 是连续的每一天）。存不下来只记日志。"""
    remember_many([(number, dates, raw, marked)])


def remember_many(entries) -> None:
    """一次存几个账号（客户页一次查了好几个，只写一遍文件）。entries 是 (号码, dates, raw, marked)。"""
    batch = {}
    for number, dates, raw, marked in entries:
        if not number or not dates or len(dates) != len(marked) or len(raw) != len(marked):
            continue
        batch[number] = {
            "start": dates[0],
            "as_of": dates[-1],
            "raw": [round(value, 6) for value in raw],
            "marked": [round(value, 6) for value in marked],
        }
    if not batch:
        return
    with _lock:
        try:
            data = _read()
            data.update(batch)
            path = _path()
            path.parent.mkdir(parents=True, exist_ok=True)
            handle, temp = tempfile.mkstemp(prefix=".customer-costs-", suffix=".json", dir=path.parent)
            try:
                with os.fdopen(handle, "w", encoding="utf-8") as stream:
                    json.dump(data, stream, ensure_ascii=False)
                os.replace(temp, path)
            except BaseException:
                try:
                    os.unlink(temp)
                except OSError:
                    pass
                raise
        except OSError as exc:
            _log.warning("客户页的消费历史没有存下来（%s: %s）", type(exc).__name__, exc)


def recall(number: str) -> Saved | None:
    """上一次存下来的那一段；没有、或者存坏了就是 None。"""
    with _lock:
        entry = _read().get(number)
    if not isinstance(entry, dict):
        return None
    try:
        start = date.fromisoformat(entry["start"])
        as_of = date.fromisoformat(entry["as_of"])
        raw = [float(value) for value in entry["raw"]]
        marked = [float(value) for value in entry["marked"]]
    except (KeyError, TypeError, ValueError):
        return None
    if len(raw) != len(marked):
        return None
    return Saved(start=start, raw=raw, marked=marked, as_of=as_of)
