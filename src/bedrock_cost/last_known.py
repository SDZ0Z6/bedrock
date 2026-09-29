"""每个账号最近一次查询成功的累计消费。

CE 查询失败的时候（凭证被收了权限、限流、网络断了），概览页和日报拿它顶上，标出
「截至哪天」，而不是只写一句「查询失败」——账号的钱不会因为今天查不到就从表上消失。

存成一个小 JSON 文件（config.LAST_KNOWN_COSTS_PATH），不放内存：web 进程和 systemd
调起的日报任务是两个进程，谁查成功了都得让另一个也能用上。

**只在口径一样时才顶上**：起算日、TAG 的标签键和值、计费口径（COST_METRIC）、服务过滤
都要和这次要查的一致。改过启用日期或 TAG 之后，旧的数是另一个口径算出来的，拿来顶上
反而误导，宁可照旧显示查询失败。比率和额度不在里面：存的是乘比率之前的原始金额，显示时
照当前台账现乘，和查询成功时一样。

同一个口径下只留终点最晚的那一次：额度告警查的是「截至前天」，不能盖掉概览页刚存的
「截至今天」。

写文件用临时文件 + 原子替换，进程内加锁。两个进程恰好在同一瞬间各写一个账号时，后写的
会盖掉先写的那一条——下一次查询成功又会补回来，不值得为它上跨进程的文件锁。
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone

from . import config

VERSION = 1
_lock = threading.Lock()


@dataclass(frozen=True)
class Known:
    start: str          # 起算日（ISO）
    end: str            # 这次查询截到哪天（ISO）
    basis: str          # 口径：TAG 键值、计费口径、服务过滤，见 _basis
    tag_raw: float
    untag_raw: float
    currency: str
    saved_at: str       # 什么时候存的（UTC，ISO）

    @property
    def as_of(self) -> date:
        return date.fromisoformat(self.end)


def _basis(account) -> str:
    return "|".join(
        (account.tag_key or "", account.tag_value or "", config.COST_METRIC, ",".join(config.SERVICE_FILTER))
    )


def recall(account, start: date, end: date) -> Known | None:
    """同一口径下、终点不晚于 end 的最近一次成功结果；没有就是 None。"""
    entry = _load().get(account.account)
    if not isinstance(entry, dict):
        return None
    try:
        known = Known(**entry)
    except TypeError:          # 字段对不上（以后改过格式）：当没有
        return None
    if known.start != start.isoformat() or known.basis != _basis(account) or known.end > end.isoformat():
        return None
    return known


def remember(account, start: date, end: date, split) -> None:
    """记下一次成功的查询。同一口径下已经存着终点更晚的，就不动它。"""
    entry = Known(
        start=start.isoformat(),
        end=end.isoformat(),
        basis=_basis(account),
        tag_raw=split.tag_raw,
        untag_raw=split.untag_raw,
        currency=split.currency,
        saved_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )
    with _lock:
        accounts = _load()
        old = accounts.get(account.account)
        if (
            isinstance(old, dict)
            and old.get("start") == entry.start
            and old.get("basis") == entry.basis
            and str(old.get("end", "")) > entry.end
        ):
            return
        accounts[account.account] = asdict(entry)
        _save(accounts)


def _load() -> dict:
    try:
        raw = json.loads(config.LAST_KNOWN_COSTS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        # 没有或者坏了：当作什么都没记住。最坏的结果是照旧显示「查询失败」
        return {}
    accounts = raw.get("accounts") if isinstance(raw, dict) else None
    return accounts if isinstance(accounts, dict) else {}


def _save(accounts: dict) -> None:
    path = config.LAST_KNOWN_COSTS_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temp = tempfile.mkstemp(prefix=".last-known-", suffix=".json", dir=path.parent)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump({"version": VERSION, "accounts": accounts}, stream, ensure_ascii=False, indent=2)
        os.replace(temp, path)
    except BaseException:
        try:
            os.unlink(temp)
        except OSError:
            pass
        raise
