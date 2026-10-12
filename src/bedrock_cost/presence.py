"""在线时间：每个人每天开着页面的时间（「页面开着就算」，放在后台的标签页也算）。

登录以后打开的每个页面（侧边栏那一套）每 PING_SECONDS 秒报一次到（app.js → POST /presence）。浏览器会把
后台标签页的定时器放慢到一分钟一次，锁屏、休眠时整个停住。服务端记着这个人上一次报到的时间：这次离上次
不超过 GAP 秒，就把中间这一段算进今天；超过了（页面都关了、电脑休眠了）就从这次重新算起。几个标签页同时开着，
报到交错着来，每一段只算一次，不会翻倍。跨过半夜的那一段只算半夜以后的。

存在 presence.json（PRESENCE_PATH）：{"用户名小写": {"last": "ISO 时间", "days": {"2026-10-12": 秒}}}，
每人只留最近 KEEP_DAYS 天。写不下来不碍事（只记一条日志）：在线时间不是要紧的数，不能让页面因为它出错。
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from datetime import date, datetime, timedelta
from pathlib import Path

from . import config

PING_SECONDS = 30    # 页面多久报一次到（app.js 里是同一个数）
GAP = 150            # 离上次报到超过这么多秒，中间那段不算（后台标签一分钟才报一次，留够余量）
KEEP_DAYS = 35

_lock = threading.Lock()
_log = logging.getLogger(__name__)


def _path() -> Path:
    return config.PRESENCE_PATH


def _now() -> datetime:
    return datetime.now().astimezone()


def _read() -> dict:
    try:
        data = json.loads(_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write(data: dict) -> None:
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix=".presence-", suffix=".json", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, separators=(",", ":"))
        os.replace(temp, path)
    except BaseException:
        try:
            os.unlink(temp)
        except OSError:
            pass
        raise


def _days(entry) -> dict:
    days = entry.get("days") if isinstance(entry, dict) else None
    return {day: int(seconds) for day, seconds in days.items() if isinstance(seconds, (int, float))} \
        if isinstance(days, dict) else {}


def touch(name: str, now: datetime | None = None) -> int:
    """报一次到，返回今天累计的秒数。"""
    now = (now or _now()).astimezone()
    key = (name or "").strip().casefold()
    if not key:
        return 0
    with _lock:
        data = _read()
        entry = data.get(key) if isinstance(data.get(key), dict) else {}
        days = _days(entry)
        today = now.date().isoformat()
        try:
            last = datetime.fromisoformat(entry["last"]).astimezone()
        except (KeyError, TypeError, ValueError):
            last = None
        if last is not None and timedelta(0) < now - last <= timedelta(seconds=GAP):
            midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
            start = max(last, midnight)      # 跨过半夜的那一段，只算半夜以后的
            days[today] = days.get(today, 0) + max(0, round((now - start).total_seconds()))
        days.setdefault(today, 0)
        cutoff = (now.date() - timedelta(days=KEEP_DAYS - 1)).isoformat()
        data[key] = {"last": now.isoformat(timespec="seconds"),
                     "days": {day: seconds for day, seconds in sorted(days.items()) if day >= cutoff}}
        try:
            _write(data)
        except OSError as exc:  # 记不下来不能让页面出错
            _log.warning("在线时间没有写下来（%s: %s）", type(exc).__name__, exc)
        return days[today]


def seconds_on(name: str, day: date | None = None) -> int:
    """这个人这一天（默认今天）开着页面的秒数。"""
    day = day or _now().date()
    return _days(_read().get((name or "").strip().casefold())).get(day.isoformat(), 0)


def recent_days(name: str, count: int = 7, today: date | None = None) -> list[tuple[date, int]]:
    """最近 count 天（今天在最后）：[(日期, 秒数)]。"""
    today = today or _now().date()
    days = _days(_read().get((name or "").strip().casefold()))
    return [(day, days.get(day.isoformat(), 0))
            for day in (today - timedelta(days=offset) for offset in range(count - 1, -1, -1))]


def today_all(today: date | None = None) -> dict[str, int]:
    """所有人今天的秒数：用户名小写 -> 秒（用户管理的列表一次拿全）。"""
    today = (today or _now().date()).isoformat()
    return {key: _days(entry).get(today, 0) for key, entry in _read().items()}


def forget(name: str) -> None:
    """删掉一个用户时，这个用户的在线时间也一起删掉：以后再建同名的用户，从零算起。"""
    key = (name or "").strip().casefold()
    with _lock:
        data = _read()
        if data.pop(key, None) is not None:
            try:
                _write(data)
            except OSError as exc:
                _log.warning("在线时间没有删掉（%s: %s）", type(exc).__name__, exc)


def label(seconds: int) -> str:
    """说人话：不到 1 分钟、13 分钟、2 小时 5 分、3 小时。"""
    minutes = int(seconds) // 60
    if minutes < 1:
        return "不到 1 分钟"
    if minutes < 60:
        return f"{minutes} 分钟"
    hours, rest = divmod(minutes, 60)
    return f"{hours} 小时" + (f" {rest} 分" if rest else "")
