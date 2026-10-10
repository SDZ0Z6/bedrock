"""登录记录：每次登录成功、失败、被锁定、退出都记一行，设置页的「登录记录」读它。

和告警流水（events）一样是一行一条 JSON（JSON Lines）：追加一行是一次写入，几个线程同时登录也不会
互相覆盖。超过 MAX_LINES 行就重写一遍，只留最新的 KEEP_LINES 行（临时文件 + 原子替换）。

别的网站替用户提交登录表单（「一键登录」）时，浏览器带的 Origin 是那个网站：记在 source 里，设置页上看得出哪些
是从哪个平台一键登录进来的。这个头能伪造，只当参考；IP 才是靠得住的。

**不记密码**：密码一个字都不记。用户名照记，登录失败时也记输进来的是什么（fail 是用户名对、密码错；
user 是用户名就不对）——看是谁在试、试的是什么名字就靠它。要知道：有人把密码错填进用户名框的话，
那一次的「用户名」就是密码，会出现在登录记录里。IP 和浏览器（User-Agent，截短）照记。

**记不下来不碍事**：写文件出任何错都只记一条日志，登录照常。读的时候文件没有、有坏行都跳过。
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import threading
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from . import config

MAX_LINES = 1200     # 超过这么多行就裁一次
KEEP_LINES = 1000    # 裁完留最新的这么多行
MAX_AGENT = 200      # User-Agent 最长记多少字
MAX_USER = 64        # 用户名最长记多少字
MAX_SOURCE = 120     # 来源网站最长记多少字
KINDS = {
    "ok": ("登录成功", "ok"),
    "fail": ("密码不对", "error"),
    "user": ("用户名不对", "error"),
    "locked": ("失败太多，锁定中", "error"),
    "logout": ("退出", "none"),
}

_lock = threading.Lock()
_log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Entry:
    when: datetime      # 带时区（存的是 UTC）
    kind: str           # KINDS 的 key
    user: str           # 用户名；登录失败、被锁定时是输进来的那个（可能是错的）
    ip: str
    agent: str          # User-Agent，截到 MAX_AGENT 字
    source: str = ""    # 登录表单是从哪个网站提交的（别的平台一键登录）；本站登录页提交的、退出是空的

    @property
    def label(self) -> str:
        return KINDS.get(self.kind, (self.kind, "none"))[0]

    @property
    def tone(self) -> str:
        return KINDS.get(self.kind, (self.kind, "none"))[1]

    @property
    def failed(self) -> bool:
        return self.kind in ("fail", "user", "locked")

    @property
    def browser(self) -> str:
        return browser_of(self.agent)


def browser_of(agent: str) -> str:
    """User-Agent 说人话：「Chrome 141 · Windows」。认不出来就原样截短。"""
    if not agent:
        return "—"
    name = ""
    for pattern, label in ((r"Edg/(\d+)", "Edge"), (r"OPR/(\d+)", "Opera"), (r"Firefox/(\d+)", "Firefox"),
                           (r"Chrome/(\d+)", "Chrome"), (r"Version/(\d+)[\d.]*\s.*Safari", "Safari")):
        found = re.search(pattern, agent)
        if found:
            name = f"{label} {found.group(1)}"
            break
    system = ""
    for needle, label in (("iPhone", "iPhone"), ("iPad", "iPad"), ("Android", "Android"), ("Windows", "Windows"),
                          ("Mac OS X", "macOS"), ("CrOS", "ChromeOS"), ("Linux", "Linux")):
        if needle in agent:
            system = label
            break
    if not name and not system:
        return agent[:40] + ("…" if len(agent) > 40 else "")
    return " · ".join(bit for bit in (name, system) if bit)


def _path(path: Path | None) -> Path:
    return path or config.LOGIN_EVENTS_PATH


def record(kind: str, *, user: str = "", ip: str = "", agent: str = "", source: str = "",
           when: datetime | None = None, path: Path | None = None) -> Entry | None:
    """记一条（追加一行）。返回记下的那条；没记下来返回 None。"""
    entry = Entry(
        when=(when or datetime.now(timezone.utc)).astimezone(timezone.utc),
        kind=kind if kind in KINDS else "fail",
        user=" ".join((user or "").split())[:MAX_USER],
        ip=(ip or "")[:64],
        agent=(agent or "")[:MAX_AGENT],
        source=(source or "")[:MAX_SOURCE],
    )
    payload = asdict(entry)
    payload["when"] = entry.when.isoformat(timespec="seconds")
    data = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
    target = _path(path)
    try:
        with _lock:
            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                existing = target.read_bytes()
            except FileNotFoundError:
                existing = b""
            if existing and not existing.endswith(b"\n"):
                data = b"\n" + data      # 上一行没写完（写到一半断电）：另起一行，别搭进那行坏的里
            with open(target, "ab") as stream:
                stream.write(data)
            if existing.count(b"\n") + 1 > MAX_LINES:
                _trim(target)
    except Exception as exc:  # 记不下来不能让登录本身出错
        _log.warning("登录记录没有写下来（%s: %s）", type(exc).__name__, exc)
        return None
    return entry


def _trim(target: Path) -> None:
    """只留最新的 KEEP_LINES 行（调用方已经拿着锁）。"""
    with target.open(encoding="utf-8", errors="replace") as handle:
        lines = handle.readlines()
    fd, temp = tempfile.mkstemp(prefix=".login-events-", suffix=".jsonl", dir=target.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.writelines(lines[-KEEP_LINES:])
        os.replace(temp, target)
    except BaseException:
        try:
            os.unlink(temp)
        except OSError:
            pass
        raise


def recent(limit: int = 500, path: Path | None = None) -> list[Entry]:
    """最近的 limit 条，新的在前。文件没有、坏行都跳过。"""
    try:
        with _path(path).open(encoding="utf-8", errors="replace") as handle:
            lines = handle.readlines()
    except OSError:
        return []
    out: list[Entry] = []
    for line in reversed(lines):
        if len(out) >= limit:
            break
        try:
            data = json.loads(line)
            when = datetime.fromisoformat(data["when"])
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
            out.append(Entry(when=when, kind=str(data.get("kind", "")), user=str(data.get("user", "")),
                             ip=str(data.get("ip", "")), agent=str(data.get("agent", "")),
                             source=str(data.get("source", ""))))
        except (ValueError, KeyError, TypeError, AttributeError):
            continue
    return out
