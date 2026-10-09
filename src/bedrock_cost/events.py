"""告警事件流：每条真的发出去了的 TG 告警记一行，运营看板的「最近告警」读它。

日报、用量开始 / 中断、额度预警、账号新增 / 停用 / 恢复、邮件告警开关、测试消息（alerts），
以及 AWS 邮件告警（mail_alerts）——至少发到了一个群，就往 ALERT_EVENTS_PATH 里追加一行
JSON：什么时候、哪一类、哪个账号、一句话说了什么、发到了几个群。同一张卡片发给几个群只记
一条；日报一次运行记一条。dry-run 不记。

**为什么是一行一条（JSON Lines）**：写它的有三个进程——web 进程（账号变动、测试消息）和两个
systemd timer（小时告警 / 日报、邮件告警）。整份读出来、改完再写回去，两个进程一撞就丢一份；
追加一行是一次写入（O_APPEND），撞上了也只是先后顺序不同。

**只留最近的**：超过 MAX_LINES 行就重写一遍，只留最新的 KEEP_LINES 行（临时文件 + 原子替换，
和其他状态文件一样）。重写的那一瞬间另一个进程恰好追加的那一条会丢——看板上的流水，不值得为它
上跨进程的文件锁。

**记不下来不碍事**：告警已经发出去了，流水只是给人看的。写文件出任何错都只记一条日志、不往外抛，
更不能让发出去的告警算成失败。读的时候文件没有、有坏行（写到一半断电、手改坏了）都跳过。

**不记秘密**：只有账号 ID、账号邮箱和卡片上本来就写着的话，Token、密码、AK 一概不进来。
看板给所有登录的人看，所以再兜一层：长得像 AK 或 Bot Token 的字符串先打码再落盘。
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import threading
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from . import config

MAX_LINES = 2500     # 超过这么多行就裁一次
KEEP_LINES = 2000    # 裁完留最新的这么多行
MAX_TEXT = 200       # 一句话最长多少字，再长截断补省略号
TONES = ("error", "warn", "ok", "info")

_lock = threading.Lock()
_log = logging.getLogger(__name__)

# 兜底打码：调用方本来就不该传这些，万一传了也不落盘明文
_ACCESS_KEY = re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{12,124}\b")
_BOT_TOKEN = re.compile(r"\b\d{5,}:[A-Za-z0-9_-]{30,}")


@dataclass(frozen=True)
class Event:
    when: datetime      # 发出去的时间，带时区（存的是 UTC）
    kind: str           # 卡片的 kind：daily / started / stopped / quota / created / disabled / restored /
                        # mail-on / mail-off / test；邮件告警是 mail-<类别>（mail-abuse、mail-case……）
    account: str        # 12 位账号 ID。日报这种不只一个账号的、认不出账号的邮件告警是 ""
    email: str          # 账号邮箱；没填、或者不知道是哪个账号时是 ""
    title: str          # 卡片标题（「用量中断」）
    text: str           # 一句话，纯文本
    tone: str           # error / warn / ok / info，看板按它上色
    groups: int         # 发到了几个群


def record(
    kind: str,
    title: str,
    text: str = "",
    *,
    tone: str = "info",
    account: str = "",
    email: str = "",
    groups: int = 1,
    when: datetime | None = None,
    path: Path | None = None,
) -> Event | None:
    """记一条（追加一行）。返回记下的那条；没记下来返回 None。

    **从不抛**：任何失败都只记一条 warning 日志。tone 不认识的当 info；when 不给就是现在，
    不带时区的按本地时间算，精确到秒（和文件里存的一样，返回的这条和 recent 读回来的相等）。
    """
    try:
        event = Event(
            when=(when or datetime.now(timezone.utc)).astimezone(timezone.utc).replace(microsecond=0),
            kind=_clean(kind, 40),
            account=_clean(account, 40),
            email=_clean(email, 254),
            title=_clean(title, 60),
            text=_clean(text, MAX_TEXT),
            tone=tone if tone in TONES else "info",
            groups=max(0, int(groups)),
        )
        _append(path or config.ALERT_EVENTS_PATH, event)
    except Exception as exc:  # 流水是锦上添花：任何失败都不能让已经发出去的告警算失败
        _log.warning("告警事件没有记下来（%s: %s）", type(exc).__name__, exc)
        return None
    return event


def recent(limit: int = 50, kinds: Iterable[str] | None = None, *, path: Path | None = None) -> list[Event]:
    """最近的 limit 条，新的在前。kinds 给了就只要这几类（卡片的 kind，见 Event）。

    文件没有、读不了是空列表；坏行跳过，不影响别的行。
    """
    if limit <= 0:
        return []
    if isinstance(kinds, str):
        kinds = [kinds]             # recent(kinds="daily") 别被拆成一个个字母
    wanted = set(kinds) if kinds is not None else None
    try:
        raw = (path or config.ALERT_EVENTS_PATH).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    found = [
        event for event in map(_parse, raw.splitlines())
        if event is not None and (wanted is None or event.kind in wanted)
    ]
    # 文件本来就是按时间追加的；几个进程交错写时再按时间排一次。同一时刻的，后写的在前
    found.reverse()
    found.sort(key=lambda event: event.when, reverse=True)
    return found[:limit]


def _clean(value: object, limit: int) -> str:
    """一行纯文本：换行和连续空白并成一个空格，长得像密钥的打码，太长截断。"""
    text = " ".join(str(value or "").split())
    text = _ACCESS_KEY.sub(lambda m: f"{m.group(0)[:8]}…{m.group(0)[-4:]}", text)
    text = _BOT_TOKEN.sub("<TOKEN>", text)
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _append(path: Path, event: Event) -> None:
    payload = asdict(event)
    payload["when"] = event.when.isoformat()
    data = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
    with _lock:
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            existing = path.read_bytes()
        except FileNotFoundError:
            existing = b""
        if existing and not existing.endswith(b"\n"):
            # 上一行没写完（写到一半断电）：另起一行，别把这一条也搭进那行坏的里
            data = b"\n" + data
        # 一行一次写入，追加模式：几个进程同时追加，各自的行不会交错
        with open(path, "ab") as stream:
            stream.write(data)
        if existing.count(b"\n") + 1 > MAX_LINES:
            _trim(path)


def _trim(path: Path) -> None:
    """只留最新的 KEEP_LINES 行。临时文件 + 原子替换：写到一半断电，旧文件还是完整的。"""
    lines = path.read_bytes().splitlines(keepends=True)
    if len(lines) <= MAX_LINES:
        return
    handle, temp = tempfile.mkstemp(prefix=".alert-events-", suffix=".jsonl", dir=path.parent)
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.writelines(lines[-KEEP_LINES:])
        os.replace(temp, path)
    except BaseException:
        try:
            os.unlink(temp)
        except OSError:
            pass
        raise


def _parse(line: str) -> Event | None:
    """一行 -> Event。空行、坏 JSON、缺时间或类别的都是 None。"""
    try:
        data = json.loads(line)
        when = datetime.fromisoformat(data["when"])
        kind = data["kind"]
    except (ValueError, TypeError, KeyError, RecursionError):   # 嵌套几万层的坏行 json 会递归溢出
        return None
    if not isinstance(kind, str) or not kind:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)   # 存的都是 UTC；手改过、没带时区的也当 UTC

    def text(name: str) -> str:
        value = data.get(name)
        return value if isinstance(value, str) else ""

    groups = data.get("groups")
    return Event(
        when=when,
        kind=kind,
        account=text("account"),
        email=text("email"),
        title=text("title"),
        text=text("text"),
        tone=data.get("tone") if data.get("tone") in TONES else "info",
        groups=groups if isinstance(groups, int) and not isinstance(groups, bool) and groups >= 0 else 0,
    )
