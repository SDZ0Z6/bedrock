"""Telegram 告警：每天 9 点的日报 + 每小时的用量切换与额度阈值。

由 systemd timer 调起，每次跑完就退出（见 __main__ 的 alerts 子命令）：

    bedrock-cost alerts daily     每天 09:00（服务器时区 Asia/Kuala_Lumpur）
    bedrock-cost alerts hourly    每小时第 5 分钟

刻意不塞进 web 进程：gunicorn 一重启就可能漏发或重发，以后改成多 worker 还会一条
消息发 N 遍。timer 带 Persistent=true，服务器宕机回来会补跑一次。

**发给谁**：台账里 TG_ENABLED 开着、TG_CHAT_IDS 至少填了一个群、账号本身也启用
的账号（Account.tg_active）。停用的账号一条都不发。一个账号可以有多个群，每条消息
发给它的全部群；日报里这个账号出现在它所在的每一个群的表里。

**三种消息**：

  日报      CE 实账，自启用日期累计到今天——和概览页调的是同一组函数
            （cumulative_range → cost_explorer.fetch_all → report.build_row），
            数字一分不差。按群组 ID 拆，每个群只看到自己的账号。
  用量切换  无用量 → 有用量、有用量 → 无用量时各发一条；持续在用不刷屏。
            「无用量」= 最近 TELEGRAM_IDLE_HOURS 个整点小时全是零调用。
  额度阈值  累计消费 = CE 实账（截至前天）+ CW 估算（昨天 + 今天），都套
            TAG/UNTAG 比率。50/80/90/100% 每档只发一次。

**账号变动**：账号管理页上新增、停用、恢复一个账号，或者打开 / 关闭它的邮件告警时，
给它的群发一张通知卡片（notify_account）。跟 TG 开关走：开关开着、填了群才发。这类消息
由 web 进程当场发，不走 systemd timer。

**长什么样**：每条都是一张深色卡片图（cards.py 画）+ 图片下面一段文字（caption，
账号 ID、账号邮箱和关键数字）。卡片画不出来就退回只发那段文字，见 _send。

**记流水**：至少发到了一个群的告警记一条进事件流（events.py，运营看板读它）。同一张卡片
发给几个群只记一条，日报一次运行记一条；dry-run 不记。记不下来不影响这条告警算发成功。

为什么额度阈值是 CE + CW 拼起来的：CE 有一到两天延迟，光用 CE 今天花的钱要后天
才看得到；光用 CW 又有个坑——ListMetrics 只列近两周有数据的模型，三周前用过、
最近没用的模型会从累计里悄悄消失。所以历史用 CE（完整），最近两天用 CW（及时，
而且落在两周窗口里）。两段按 UTC 日期切开，不重叠。

跨次运行要记住的东西放在 ALERT_STATE_PATH 那个 JSON 里，见 AccountState。
"""

from __future__ import annotations

import html
import itertools
import json
import os
import tempfile
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from . import cards, config, cost_explorer, events, telegram
from .cards import (
    Card,
    Cell,
    Details,
    Divider,
    Info,
    Meter,
    Notes,
    Paragraph,
    Row,
    Since,
    Status,
    Table,
    Tile,
    Tiles,
    Uid,
)
from .chart import compact_number
from .cloudwatch_metrics import hourly_invocations
from .cost_estimate import HourUsage, estimate_split, hour_usage
from .dates import (
    CUMULATIVE_CLAMPED,
    CUMULATIVE_FUTURE,
    CUMULATIVE_MISSING,
    CUMULATIVE_OK,
    cumulative_range,
)
from .excel_source import Account, load_accounts
from .report import ReportRow, build_row
from .telegram import TelegramError

STATE_VERSION = 1

# CE 的数据有一到两天延迟：截至「今天往前数两天」的那部分才算落定
CE_SETTLED_LAG_DAYS = 2


# --------------------------------------------------------------- 状态
@dataclass
class AccountState:
    """一个账号跨次运行要记住的东西。按 12 位账号号码存——台账校验保证它唯一。"""

    # 上次看到的用量状态；None = 还没有基线（刚开启告警）。第一次只记基线不发消息，
    # 否则一开启就收到一条「开始有用量」，而那其实是早就在用了
    active: bool | None = None
    last_active_hour: str = ""        # 最近一个有调用的整点小时，ISO 格式、UTC
    # 「额度|启用日期」。其中任何一个变了都等于开了新的一期额度，已发档位清零
    period: str = ""
    fired: list[float] = field(default_factory=list)
    # CE 实账的缓存。CE 按请求收费，小时任务每小时都要这个数，所以一天只查一次：
    # key 里带着区间起止和 TAG 写法，任何一个变了才重查
    ce_key: str = ""
    ce_tag_raw: float = 0.0
    ce_untag_raw: float = 0.0


def load_state(path=None) -> dict[str, AccountState]:
    path = path or config.ALERT_STATE_PATH
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        # 文件坏了就从头来：最坏的结果是重新建一次基线、把已达到的档位再报一遍，
        # 比整个告警任务起不来强
        return {}
    known = set(AccountState.__dataclass_fields__)
    result: dict[str, AccountState] = {}
    for account_id, values in (raw.get("accounts") or {}).items():
        if isinstance(values, dict):
            result[account_id] = AccountState(**{k: v for k, v in values.items() if k in known})
    return result


def save_state(states: dict[str, AccountState], path=None) -> None:
    """临时文件 + 原子替换：写到一半断电，旧文件还是完整的。"""
    path = path or config.ALERT_STATE_PATH
    payload = {
        "version": STATE_VERSION,
        "accounts": {key: asdict(value) for key, value in sorted(states.items())},
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temp = tempfile.mkstemp(prefix=".alert-state-", suffix=".json", dir=path.parent)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
        os.replace(temp, path)
    except BaseException:
        try:
            os.unlink(temp)
        except OSError:
            pass
        raise


# --------------------------------------------------------------- 发送
@dataclass
class RunSummary:
    """一次运行的结果。有任何失败就让命令以非零退出，systemctl --failed 看得到。"""

    sent: int = 0
    problems: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems


# 发一张卡片。返回空串；卡片画不出来、退回发了文字时返回一句说明（记成问题）。
# 发送失败抛 TelegramError。
Sender = Callable[[str, Card], str]


def _send(chat_id: str, card: Card) -> str:
    """真的发出去：卡片画成图片用 sendPhoto 发，caption 跟在图片下面。

    画不出来（字体文件丢了、Pillow 出错）就退回只发 caption 那段文字——告警不能
    因为画图丢掉。这时返回一句说明，调用方记成问题、让命令非零退出。
    """
    try:
        png = card.png()
    except Exception as exc:  # 画图的任何失败都一样处理：退回发文字
        telegram.send_message(chat_id, card.caption)
        return f"卡片画不出来，改发了文字（{type(exc).__name__}: {exc}）"
    telegram.send_photo(chat_id, png, card.caption)
    return ""


def _deliver(chat_id: str, card: Card, summary: RunSummary, send: Sender, log) -> bool:
    try:
        note = send(chat_id, card)
    except TelegramError as exc:
        summary.problems.append(f"发往 {chat_id} 失败：{exc}")
        log(f"[发送失败] {chat_id}：{exc}")
        return False
    if note:
        summary.problems.append(f"发往 {chat_id}：{note}")
        log(f"[注意] {chat_id}：{note}")
    summary.sent += 1
    return True


def _deliver_all(
    chat_ids: tuple[str, ...], card: Card, summary: RunSummary, send: Sender, log
) -> int:
    """同一张卡片发给一个账号的全部群（只画一次），返回发成功了几个群。
    **有一个群发成功就算发过了。**

    不要求全部成功：一个群 ID 坏了（bot 被踢了）就不让状态前进的话，下一小时
    其他好好的群会再收到一遍，每小时一遍，直到有人修好那个 ID。坏掉的那个会记成
    问题、让命令非零退出，journalctl 和 systemctl --failed 里看得到。

    全部失败（Telegram 整个连不上、Token 失效）才返回 0，下一小时重试。
    """
    # 每个群都要发到：sum 把生成器走完，不会因为前面成功了就跳过后面的
    return sum(_deliver(chat_id, card, summary, send, log) for chat_id in chat_ids)


# 卡片的颜色 -> 事件流的 tone（看板按它上色）。停用、关邮件告警那种灰卡片只是通知
_EVENT_TONES = {"danger": "error", "warn": "warn", "ok": "ok", "info": "info", "gray": "info"}


def _gist(card: Card) -> str:
    """一张卡片的一句话：caption 的第二行（第一行是标题和账号），去掉 HTML。"""
    lines = telegram.visible(card.caption).splitlines()
    return lines[1] if len(lines) > 1 else ""


def _record(card: Card, groups: int, account_id: str = "", email: str = "") -> None:
    """发出去了（至少一个群）的一张卡片记一条事件，运营看板读它。只在真发的时候调，dry-run 不调。

    记不下来 events.record 自己记日志、不抛：告警已经发出去了，不能因为流水没记上就算失败。
    """
    events.record(
        kind=card.kind,
        title=card.title,
        text=_gist(card),
        tone=_EVENT_TONES.get(card.tone, "info"),
        account=account_id,
        email=email,
        groups=groups,
    )


def _dry_run_sender(log, save_dir: Path | None = None) -> Sender:
    """只打印不发。卡片照样画一遍：画不出来在上线前就能看到。

    给了 save_dir 就把画好的卡片存成 PNG，在服务器上也能拿下来看一眼长什么样。
    """
    numbers = itertools.count(1)

    def send(chat_id: str, card: Card) -> str:
        log(f"----- [dry-run] 发往 {chat_id} -----\n{card.text()}\n")
        try:
            png = card.png()
        except Exception as exc:  # 和 _send 一样：画不出来就说出来
            return f"卡片画不出来，真发的时候会改发文字（{type(exc).__name__}: {exc}）"
        if save_dir is not None:
            save_dir.mkdir(parents=True, exist_ok=True)
            path = save_dir / f"{next(numbers):02d}-{card.kind}-{chat_id.lstrip('-@')}.png"
            path.write_bytes(png)
            log(f"卡片存到了 {path}\n")
        return ""

    return send


def _targets() -> list[Account]:
    # force=True：这是一个独立的短命进程，没有可以复用的 mtime 缓存
    return [a for a in load_accounts(force=True) if a.tg_active]


# --------------------------------------------------------------- 卡片
def _esc(text: object) -> str:
    return html.escape(str(text), quote=False)


def _money(value: float) -> str:
    """负数写成 -$12.30，不是 $-12.30。"""
    amount = f"{config.CURRENCY_SYMBOL}{abs(value):,.2f}"
    return f"-{amount}" if round(value, 2) < 0 else amount


def _pct(value: float) -> str:
    return f"{value:g}%"


def _local_hour(stamp: datetime) -> str:
    """UTC 整点 → 服务器本地时间的「09-27 14:00」。"""
    return stamp.astimezone().strftime("%m-%d %H:%M")


def _uid(account_id: str, email: str = "") -> str:
    """caption 里的账号：「<code>号码</code> · 账号邮箱」，没填邮箱就只有号码。

    号码一眼认不出是谁，邮箱写全、不打码。不带上游（台账的 PARTNER 列）：所有 TG 消息都不出现上游。
    """
    uid = f"<code>{_esc(account_id)}</code>"
    return f"{uid} · {_esc(email)}" if email else uid


def _caption(lines: list[str], overflow: str = "") -> str:
    """拼 caption。Telegram 限 1024 字，放不下就截掉后面的行，最后补一句 overflow。"""
    kept: list[str] = []
    for line in lines:
        if len(telegram.visible("\n".join([*kept, line, overflow]))) > telegram.MAX_CAPTION_CHARS:
            kept.append(overflow)
            break
        kept.append(line)
    return "\n".join(kept)


# 日报 caption 里，启用日期有问题的那一行后面跟的简短说明（卡片的「需要注意」写全句）
_RANGE_SHORT = {
    CUMULATIVE_MISSING: "未填启用日期",
    CUMULATIVE_CLAMPED: "启用日期早于 CE 保留期",
    CUMULATIVE_FUTURE: "启用日期晚于今天",
}


def daily_cards(today: date, rows: list[ReportRow], emails: dict[str, str] | None = None) -> list[Card]:
    """一个群的日报。账号多于 cards.MAX_TABLE_ROWS 个就分成几张发。

    emails 是 {账号 ID: 账号邮箱}：ReportRow 上没有邮箱，由调用方从台账里查好传进来。
    """
    size = cards.MAX_TABLE_ROWS
    chunks = [rows[start:start + size] for start in range(0, len(rows), size)] or [[]]
    return [
        _daily_card(today, chunk, len(rows), index, len(chunks), emails or {})
        for index, chunk in enumerate(chunks, start=1)
    ]


def _daily_card(
    today: date, rows: list[ReportRow], everyone: int, index: int, total: int, emails: dict[str, str]
) -> Card:
    table: list[list[Cell]] = []
    notes: list[tuple[str, str]] = []
    lines: list[str] = []
    for row in rows:
        uid = row.account
        email = emails.get(uid, "")
        budget = Cell(_money(row.budget)) if row.budget > 0 else Cell("未设额度", "muted")
        if not row.has_numbers:
            table.append([Cell(uid, mark="danger", note=email), budget, Cell("查询失败", "danger"), Cell("—", "muted")])
            notes.append(("danger", f"{uid} 查询失败：{row.error}"))
            lines.append(f"{_uid(uid, email)} 查询失败")
            continue

        mark, flags = "", []
        stale = f"{row.stale_as_of:%m-%d}" if row.stale_as_of else ""
        if stale:
            # 今天查询失败，但有上一次成功的数：照常显示金额、标橙，并写明截至哪天
            mark = "danger"
            flags.append(f"截至 {stale}，今天查询失败")
            notes.append(("danger", f"{uid} 今天查询失败：{row.error}。表里是截至 {stale} 的累计消费"))
        if row.range_status != CUMULATIVE_OK and row.range_hint:
            mark = mark or "warn"
            flags.append(_RANGE_SHORT.get(row.range_status, "启用日期有问题"))
            notes.append(("warn", f"{uid}：{row.range_hint}"))
        if row.overspent:
            mark = "danger"
            flags.append("已超出额度")
            notes.append(("danger", f"{uid} 已超出额度 {_money(-row.balance)}"))
        balance = (
            Cell(_money(row.balance), "danger" if row.balance < 0 else ("warn" if stale else "ok"))
            if row.budget > 0
            else Cell("—", "muted")
        )
        spent = Cell(_money(row.total_cost), "warn" if stale else "", note=f"截至 {stale}" if stale else "")
        table.append([Cell(uid, mark=mark, note=email), budget, spent, balance])
        remaining = f" · 剩余额度 {_money(row.balance)}" if row.budget > 0 else ""
        flagged = f"（{'，'.join(flags)}）" if flags else ""
        lines.append(f"{_uid(uid, email)} 累计消费 {_money(row.total_cost)}{remaining}{flagged}")

    blocks: list = [Table(["UID", "授信额度", "累计消费", "剩余额度"], table)]
    if notes:
        blocks.append(Notes("需要注意", notes))
    split = total > 1
    return Card(
        kind="daily",
        tone="ok",
        icon="chart",
        title="Bedrock 日报",
        badge="DAILY REPORT",
        subtitle=today.isoformat() + (f" · 第 {index}/{total} 张" if split else ""),
        subtitle_icon="calendar",
        blocks=blocks,
        footer=f"账号数量：{everyone}" + (f"（这张 {len(rows)} 个）" if split else ""),
        footer_right="自动生成 · 每日播报",
        caption=_caption(
            [f"📊 <b>Bedrock 日报</b> · {today.isoformat()}" + (f"（{index}/{total}）" if split else ""), *lines],
            overflow="……其余账号见图片",
        ),
    )


def started_card(account: Account, hour: datetime, count: float) -> Card:
    when, calls = _local_hour(hour), f"{count:,.0f}"
    return Card(
        kind="started",
        tone="ok",
        icon="dot-green",
        title="用量开始",
        badge="ACTIVE",
        subtitle="BEDROCK · USAGE ALERT",
        blocks=[
            Uid(account.account, email=account.email),
            Tiles([
                Tile("检测时段", when),
                Tile("本小时调用次数", calls, tone="ok", big=True, edge=True),
            ]),
        ],
        footer="检测到该账号本小时开始产生调用。",
        caption=f"🟢 <b>用量开始</b> · {_uid(account.account, account.email)}\n{when} 这一小时调用 {calls} 次",
    )


def stopped_card(
    account: Account, hours: list[datetime], last_active: str, usage: HourUsage | None = None
) -> Card:
    """用量中断。usage 是上一次有调用的那一小时的用量（hour_usage）：有它就写到分钟的
    最后一次调用时间，外加那一小时的调用次数、token 和预估费用；取不到就只写到小时。"""
    first = _local_hour(hours[0])
    if len(hours) > 1:
        label, when, gist = "检测时段", f"{first} 起", f"连续 {len(hours)} 个小时没有任何调用"
        summary = f"{first} 起连续 {len(hours)} 个小时没有任何调用"
    else:
        label, when, gist = "当前检测时段", first, "本小时没有任何调用"
        summary = f"{first} 这一小时没有任何调用"
    blocks: list = [
        Uid(account.account, email=account.email),
        Tiles([Tile(label, when, big=True, edge=True, note=gist, note_tone="danger")]),
    ]
    lines = [f"🔴 <b>用量中断</b> · {_uid(account.account, account.email)}", summary]
    footer = "请检查账号调用情况及相关服务状态。"
    if last_active:
        known = usage is not None and usage.has_data
        if known and usage.last_call is not None:
            previous = _local_hour(usage.last_call)          # 精确到分钟
        else:
            previous = f"{_local_hour(datetime.fromisoformat(last_active))} 那一小时"
        blocks.append(Since("上一次有调用", previous))
        lines.append(f"上一次有调用：{previous}")
        if known:
            blocks.append(_usage_details(account, usage))
            lines.append(_usage_line(account, usage))
            notes = _usage_notes(usage)
            if notes:
                blocks.append(Notes("需要注意", notes))
            footer += "预估费用按 CloudWatch Token × AWS 牌价估算，已套台账的 TAG / UNTAG 比率。"
    return Card(
        kind="stopped",
        tone="danger",
        icon="dot-red",
        title="用量中断",
        badge="INTERRUPTED",
        subtitle="BEDROCK · USAGE ALERT",
        blocks=blocks,
        footer=footer,
        caption=_caption(lines),
    )


def _usage_span(usage: HourUsage) -> str:
    return f"{_local_hour(usage.start)}–{usage.end.astimezone():%H:%M}"


def _usage_details(account: Account, usage: HourUsage) -> Details:
    """上一次有调用的那一小时：调用次数、token、预估费用。"""
    tokens = usage.tokens
    rows = [
        Row("调用次数", f"{usage.invocations:,.0f} 次"),
        Row("Token 用量", f"输入 {compact_number(tokens.get('input', 0))} · 输出 {compact_number(tokens.get('output', 0))}"),
    ]
    cached = [
        f"{label} {compact_number(tokens[kind])}"
        for kind, label in (("cache_read", "读"), ("cache_write", "写"))
        if tokens.get(kind)
    ]
    if cached:
        rows.append(Row("缓存 Token", " · ".join(cached)))
    rows.append(Row("预估费用", _money(usage.marked(account))))
    return Details(rows, title=f"上一次有调用的那一小时（{_usage_span(usage)}）")


def _usage_line(account: Account, usage: HourUsage) -> str:
    return (
        f"那一小时（{_usage_span(usage)}）调用 {usage.invocations:,.0f} 次 · "
        f"Token {compact_number(usage.total_tokens)} · 预估 {_money(usage.marked(account))}"
    )


def _usage_notes(usage: HourUsage) -> list[tuple[str, str]]:
    notes: list[tuple[str, str]] = []
    if usage.errors:
        notes.append(("warn", f"有 {len(usage.errors)} 个区读不到 CloudWatch，上面的用量和费用可能偏低"))
    if usage.unpriced:
        notes.append(("warn", f"这些模型没有单价、按 0 算了：{'、'.join(usage.unpriced)}"))
    return notes


def quota_card(
    account: Account,
    threshold: float,
    pct: float,
    spent: float,
    status: str,
    since: date,
    unpriced: list[str],
) -> Card:
    used_up = threshold >= 100
    tone = "danger" if threshold >= 90 else "warn"
    title = "额度已用完" if used_up else "额度预警"
    balance = account.budget - spent
    blocks: list = [
        Uid(account.account, email=account.email),
        Meter("额度使用率", f"{pct:.1f}%", pct / 100, f"已消费 {_money(spent)}", f"总额度 {_money(account.budget)}"),
        Divider(),
        Tiles([
            Tile("累计消费", _money(spent)),
            Tile("剩余额度", _money(balance), tone="danger" if balance < 0 else "ok"),
        ]),
        Info(
            "统计口径",
            [
                f"累计周期：自 {since.isoformat()} 起",
                "• 截至前天：Cost Explorer 实账",
                "• 最近两天：CloudWatch Token × 牌价估算",
            ],
            icon="pin",
        ),
    ]
    warnings: list[tuple[str, str]] = []
    if status != CUMULATIVE_OK:
        warnings.append(("warn", "台账未填启用日期，或启用日期早于 Cost Explorer 的保留期——实际消费可能更高"))
    if unpriced:
        warnings.append(("warn", f"这些模型没有单价、按 0 算了：{'、'.join(unpriced)}"))
    if warnings:
        blocks.append(Notes("需要注意", warnings))
    gist = "已经用完" if used_up else f"超过 {_pct(threshold)} 提醒线"
    return Card(
        kind="quota",
        tone=tone,
        icon="warning-red" if tone == "danger" else "warning",
        title=title,
        badge=f"{pct:.1f}% USED",
        subtitle="BEDROCK · CREDIT USAGE ALERT",
        blocks=blocks,
        caption=_caption([
            f"{'🚨' if used_up else '⚠️'} <b>{title}</b> · {_uid(account.account, account.email)}",
            f"额度已用 {pct:.1f}%，{gist}",
            f"累计消费 {_money(spent)} / 总额度 {_money(account.budget)}，剩余 {_money(balance)}",
        ]),
    )


def _moment(now: datetime | None = None) -> str:
    """服务器本地时间「2026-09-28 17:00」，卡片上的测试时间、启用 / 停用时间用。"""
    return (now or datetime.now(timezone.utc)).astimezone().strftime("%Y-%m-%d %H:%M")


def _budget(account: Account) -> str:
    return _money(account.budget) if account.budget > 0 else "未设额度"


def ping_card(account_id: str = "", now: datetime | None = None, email: str = "") -> Card:
    """测试消息。卡片照截图不放账号；account_id（和账号邮箱）只写在图片下面的文字里
    （命令行发的没有）。"""
    head = "🧪 <b>测试消息</b>" + (f" · {_uid(account_id, email)}" if account_id else "")
    return Card(
        kind="test",
        tone="info",
        icon="test-tube",
        title="测试消息",
        badge="TEST",
        subtitle="BEDROCK · BOT NOTIFICATION TEST",
        blocks=[
            Status("check-circle", "Telegram Bot 运行正常", "测试消息已成功触发", tone="ok"),
            Divider(),
            Paragraph(
                "这是一条 Bedrock 监控系统的测试消息，用于验证 Telegram Bot 的消息推送功能。",
                size=15, label="测试内容",
            ),
            Details(
                [
                    Row("监控服务", "AWS Bedrock"),
                    Row("消息推送", "正常", tone="ok", bold=False),
                    Row("测试时间", _moment(now)),
                ],
                ruled=True,
            ),
        ],
        footer_rule=False,
        caption=f"{head}\nTelegram Bot 运行正常，测试消息已成功触发。",
    )


def send_test(chat_id: str, account_id: str = "", email: str = "") -> str:
    """账号管理页「发测试消息」和命令行 alerts test。失败抛 TelegramError。

    返回空串，或者「卡片画不出来、改发了文字」的说明——页面和命令行都会把它显示出来。
    一次只发一个群，发成功了记一条事件（groups=1）。
    """
    card = ping_card(account_id, email=email)
    note = _send(chat_id, card)
    _record(card, 1, account_id, email)
    return note


# --------------------------------------------------------------- 账号变动
def _mail_row(account: Account, state: str, tone: str = "ok") -> list[Row]:
    """账号卡片上顺带说一句邮件告警的状态：新增时填了邮箱就开、停用就停、恢复就恢复。
    没开邮件告警的账号不加这一行。"""
    if not (account.mail_enabled and account.mail_configured):
        return []
    return [Row("邮件告警", state, tone=tone)]


def created_card(account: Account, now: datetime | None = None) -> Card:
    when = _moment(now)
    return Card(
        kind="created",
        tone="ok",
        icon="dot-green",
        title="新账号启用",
        badge="ACTIVATED",
        subtitle="BEDROCK · ACCOUNT NOTIFICATION",
        blocks=[
            Status("check-circle", "账号已成功启用", "新账号已加入 Bedrock 监控", tone="ok"),
            Details(
                [
                    Row("账号状态", "运行中", tone="ok", pill=True),
                    Row("授信额度", _budget(account)),
                    Row("启用时间", when),
                    *_mail_row(account, "已开启"),
                ],
                uid=account.account,
                email=account.email,
            ),
        ],
        footer="系统已开始监控该账号的用量及额度情况。",
        footer_icon="satellite",
        footer_rule=False,
        caption=(
            f"🟢 <b>新账号启用</b> · {_uid(account.account, account.email)}\n"
            f"授信额度 {_budget(account)} · 启用时间 {when}"
        ),
    )


def restored_card(account: Account, now: datetime | None = None) -> Card:
    """恢复一个停用过的账号。没有截图，照「新账号启用」那张的样子来。"""
    when = _moment(now)
    return Card(
        kind="restored",
        tone="ok",
        icon="dot-green",
        title="账号恢复启用",
        badge="REACTIVATED",
        subtitle="BEDROCK · ACCOUNT NOTIFICATION",
        blocks=[
            Status("check-circle", "账号已恢复启用", "该账号已重新加入 Bedrock 监控", tone="ok"),
            Details(
                [
                    Row("账号状态", "运行中", tone="ok", pill=True),
                    Row("授信额度", _budget(account)),
                    Row("恢复时间", when),
                    *_mail_row(account, "已恢复"),
                ],
                uid=account.account,
                email=account.email,
            ),
        ],
        footer="系统已恢复监控该账号的用量及额度情况。",
        footer_icon="satellite",
        footer_rule=False,
        caption=(
            f"🟢 <b>账号恢复启用</b> · {_uid(account.account, account.email)}\n"
            f"授信额度 {_budget(account)} · 恢复时间 {when}"
        ),
    )


def disabled_card(account: Account, spend: ReportRow, now: datetime | None = None) -> Card:
    """spend 是停用那一刻的累计消费（和概览页、日报同一个口径）。"""
    when = _moment(now)
    rows = [Row("账号状态", "已停用", tone="gray", pill=True), Row("停用时间", when)]
    if not spend.has_numbers:
        rows.append(Row("停用前累计消费", "查询失败", tone="danger", bold=False))
        gist = "停用前累计消费查询失败"
    else:
        # 这一刻查不到、但有上一次成功的数：照样写上，标明截至哪天
        stale = f"（截至 {spend.stale_as_of:%m-%d}）" if spend.stale_as_of else ""
        rows.append(Row("停用前累计消费", _money(spend.total_cost) + stale, tone="warn" if stale else ""))
        if account.budget > 0:
            tone = "danger" if spend.balance < 0 else ("warn" if stale else "")
            rows.append(Row("停用前剩余额度", _money(spend.balance) + stale, tone=tone))
            gist = f"停用前累计消费 {_money(spend.total_cost)} · 剩余额度 {_money(spend.balance)}{stale}"
        else:
            gist = f"停用前累计消费 {_money(spend.total_cost)}{stale}"
    rows += _mail_row(account, "已停止", tone="muted")
    return Card(
        kind="disabled",
        tone="gray",
        icon="dot-gray",
        title="账号停用",
        badge="DEACTIVATED",
        subtitle="BEDROCK · ACCOUNT NOTIFICATION",
        blocks=[
            Status("pause-circle", "账号已停用", "该账号已从活动监控中停用"),
            Details(rows, uid=account.account, email=account.email),
        ],
        footer="系统已停止该账号的活动监控及相关告警。",
        footer_icon="satellite",
        footer_rule=False,
        caption=f"⚫ <b>账号停用</b> · {_uid(account.account, account.email)}\n{gist}",
    )


def mail_on_card(account: Account, now: datetime | None = None) -> Card:
    """在账号管理页打开了邮件告警。

    告警邮箱写全、不打码：号码认不出是谁，群里靠邮箱认是哪个账号在收信。打码的只有邮件原文
    节选里引用的地址——那些可能是别人的（见 mail_rules.excerpt）。
    """
    when = _moment(now)
    mailbox = account.mail_address or "—"
    return Card(
        kind="mail-on",
        tone="ok",
        icon="dot-green",
        title="邮件告警已开启",
        badge="MAIL ALERT ON",
        subtitle="BEDROCK · ACCOUNT NOTIFICATION",
        blocks=[
            Status("check-circle", "已开始监控告警邮箱", "AWS 发来的封号、盗用、滥用、工单通知会发到群里", tone="ok"),
            Details(
                [
                    Row("监控状态", "监控中", tone="ok", pill=True),
                    Row("告警邮箱", mailbox),
                    Row("邮箱平台", account.mail_provider_label),
                    Row("开启时间", when),
                ],
                uid=account.account,
                email=account.email,
            ),
        ],
        footer="每 5 分钟收一次信；只发开启之后新到的邮件，旧邮件不补发。",
        footer_icon="satellite",
        footer_rule=False,
        caption=(
            f"🟢 <b>邮件告警已开启</b> · {_uid(account.account, account.email)}\n"
            f"告警邮箱 {_esc(mailbox)} · 开启时间 {when}"
        ),
    )


def mail_off_card(account: Account, now: datetime | None = None) -> Card:
    """关掉了邮件告警：表格里点了关，或者修改弹窗里把告警邮箱清空了。告警邮箱同样写全。"""
    when = _moment(now)
    mailbox = account.mail_address or "—"
    return Card(
        kind="mail-off",
        tone="gray",
        icon="dot-gray",
        title="邮件告警已关闭",
        badge="MAIL ALERT OFF",
        subtitle="BEDROCK · ACCOUNT NOTIFICATION",
        blocks=[
            Status("pause-circle", "已停止监控告警邮箱", "这个邮箱之后收到的 AWS 通知不再发到群里"),
            Details(
                [
                    Row("监控状态", "已关闭", tone="gray", pill=True),
                    Row("告警邮箱", mailbox),
                    Row("关闭时间", when),
                ],
                uid=account.account,
                email=account.email,
            ),
        ],
        footer="重新打开之后，只发打开以后新到的邮件。",
        footer_icon="satellite",
        footer_rule=False,
        caption=(
            f"⚫ <b>邮件告警已关闭</b> · {_uid(account.account, account.email)}\n"
            f"告警邮箱 {_esc(mailbox)} · 关闭时间 {when}"
        ),
    )


def _spend(account: Account, today: date) -> ReportRow:
    """这一刻的累计消费：和概览页、日报同一组函数（有缓存就用缓存）。"""
    period = cumulative_range(account.start_date, today)
    split = cost_explorer.fetch_all([account], {account.key: period[:2]}).get(account.key)
    return build_row(account, split or cost_explorer.CostSplit(error="未取到数据"), period)


def notify_account(
    event: str, account: Account, *, now: datetime | None = None, log=print
) -> RunSummary | None:
    """账号管理页上新增（created）/ 停用（disabled）/ 恢复（restored）了一个账号，或者打开
    （mail_on）/ 关闭（mail_off）了它的邮件告警：给它的群发卡片。

    跟 TG 开关走：这个账号的 TG 告警没开或者没填群，就不发，返回 None。账号变动看的是开关
    和群，不看 tg_active——停用的时候账号已经不算 tg_active 了，停用通知正是它的最后一条
    消息。邮件告警的开关则要账号本身也启用着：停用的账号本来就一条消息都不发。
    发送失败不影响台账（台账在这之前已经写好了），只记在返回的 RunSummary 里。
    """
    if event in ("mail_on", "mail_off"):
        if not account.tg_active:
            return None
    elif not (account.tg_enabled and account.tg_chat_ids):
        return None
    summary = RunSummary()
    if not telegram.configured():
        summary.problems.append("服务器没有配置 TELEGRAM_BOT_TOKEN，TG 通知没有发")
        return summary
    now = now or datetime.now(timezone.utc)
    if event == "created":
        card = created_card(account, now)
    elif event == "restored":
        card = restored_card(account, now)
    elif event == "disabled":
        card = disabled_card(account, _spend(account, now.astimezone().date()), now)
    elif event == "mail_on":
        card = mail_on_card(account, now)
    elif event == "mail_off":
        card = mail_off_card(account, now)
    else:
        raise ValueError(f"不认识的账号变动：{event}")
    groups = _deliver_all(account.tg_chat_ids, card, summary, _send, log)
    if groups:
        _record(card, groups, account.account, account.email)
    return summary


# --------------------------------------------------------------- 日报
def run_daily(
    today: date | None = None, *, dry_run: bool = False, save_dir: Path | None = None, log=print
) -> RunSummary:
    summary = RunSummary()
    today = today or date.today()
    targets = _targets()
    if not targets:
        log("没有开启 TG 告警的账号，日报不发。")
        return summary
    if not dry_run and not telegram.configured():
        summary.problems.append("没有配置 TELEGRAM_BOT_TOKEN")
        log("没有配置 TELEGRAM_BOT_TOKEN，日报发不出去。")
        return summary

    # 和概览页同一组函数：区间、CE 查询、行的口径全都一样
    periods = {a.key: cumulative_range(a.start_date, today) for a in targets}
    ranges = {key: (start, end) for key, (start, end, _) in periods.items()}
    splits = cost_explorer.fetch_all(targets, ranges)

    by_chat: dict[str, list[ReportRow]] = {}
    for account in targets:
        split = splits.get(account.key) or cost_explorer.CostSplit(error="未取到数据")
        row = build_row(account, split, periods[account.key])
        for chat_id in account.tg_chat_ids:
            by_chat.setdefault(chat_id, []).append(row)
        if row.error:
            summary.problems.append(f"{account.account} 查不到 CE：{row.error}")

    # 表里 UID 下面写账号邮箱。ReportRow 上没有邮箱，按账号 ID 从台账里查
    emails = {account.account: account.email for account in targets if account.email}
    send = _dry_run_sender(log, save_dir) if dry_run else _send
    reached: list[str] = []
    for chat_id, rows in by_chat.items():
        # 分成几张发的，哪一张发成功了都算这个群收到了日报
        if sum(_deliver(chat_id, card, summary, send, log) for card in daily_cards(today, rows, emails)):
            reached.append(chat_id)
    if reached and not dry_run:
        _record_daily(today, by_chat, reached)
    return summary


def _record_daily(today: date, by_chat: dict[str, list[ReportRow]], reached: list[str]) -> None:
    """日报一次运行记一条事件：发到了几个群、一共几个账号，有没有查询失败、超额、没发出去的群。"""
    rows = {row.account: row for chat_id in reached for row in by_chat[chat_id]}   # 几个群里都有的只算一次
    failed = sum(1 for row in rows.values() if row.error)
    over = sum(1 for row in rows.values() if row.overspent)
    missed = len(by_chat) - len(reached)
    parts = [f"{today.isoformat()} 的日报发到 {len(reached)} 个群，共 {len(rows)} 个账号"]
    if failed:
        parts.append(f"{failed} 个查询失败")
    if over:
        parts.append(f"{over} 个已超出额度")
    if missed:
        parts.append(f"{missed} 个群没发出去")
    events.record(
        kind="daily",
        title="Bedrock 日报",
        text="，".join(parts),
        tone="warn" if len(parts) > 1 else "ok",
        groups=len(reached),
    )


# --------------------------------------------------------------- 小时任务
def _check_usage(
    account: Account,
    state: AccountState,
    hours: list[datetime],
    summary: RunSummary,
    send: Sender,
    log,
    *,
    dry_run: bool,
) -> None:
    counts, failed = hourly_invocations(account, hours)
    if counts is None:
        # 读不到就不判定：缺一个区的零不能当成真的零，否则会误报「用量中断」
        reason = "；".join(map(str, failed))   # 可能是 aws_errors.QueryError，str() 才是那一行字
        summary.problems.append(f"{account.account} 读不到 CloudWatch：{reason}")
        log(f"[跳过用量判定] {account.account}：{reason}")
        return

    if counts[-1] > 0:
        now_active = True
    elif not any(counts):
        now_active = False
    else:
        # 窗口里有调用、只是最后一小时没有：还没连续空满 N 小时，不算中断
        return

    latest = hours[-1].isoformat()
    if state.active is None:
        state.active = now_active  # 第一次只记基线
        if now_active:
            state.last_active_hour = latest
        return
    if state.active == now_active:
        if now_active:
            state.last_active_hour = latest
        return

    if now_active:
        card = started_card(account, hours[-1], counts[-1])
    else:
        card = stopped_card(account, hours, state.last_active_hour, _last_usage(account, state.last_active_hour, log))
    # 发出去了才落状态：全部群都失败就保持原状，下一小时条件还成立会再发一次
    groups = _deliver_all(account.tg_chat_ids, card, summary, send, log)
    if groups:
        state.active = now_active
        if now_active:
            state.last_active_hour = latest
        if not dry_run:
            _record(card, groups, account.account, account.email)


def _last_usage(account: Account, last_active: str, log) -> HourUsage | None:
    """「用量中断」卡片上要写的那一小时用量。取不到也照发中断告警，只是少几行。"""
    if not last_active:
        return None
    try:
        usage = hour_usage(account, datetime.fromisoformat(last_active))
    except Exception as exc:  # 明细是锦上添花，任何失败都不能拦住中断告警
        log(f"[用量明细] {account.account}：{type(exc).__name__}: {exc}")
        return None
    for error in usage.errors:
        log(f"[用量明细] {account.account}：{error}")
    return usage


def _check_quota(
    account: Account,
    state: AccountState,
    now: datetime,
    summary: RunSummary,
    send: Sender,
    log,
    *,
    dry_run: bool,
) -> None:
    if account.budget <= 0:
        return  # 没填额度，使用率无从谈起

    period = f"{account.budget:g}|{account.start_date.isoformat() if account.start_date else ''}"
    if state.period != period:
        state.period = period
        state.fired = []

    # 按 UTC 日期切：CE 和 CW 的日桶都是 UTC 的，本地日期会让两段在凌晨重叠 8 小时
    utc_today = now.astimezone(timezone.utc).date()
    since, _, status = cumulative_range(account.start_date, utc_today)

    # 截至前天：CE 实账（一天查一次，结果存在状态里）
    ce_end = utc_today - timedelta(days=CE_SETTLED_LAG_DAYS)
    ce_tag = ce_untag = 0.0
    if since <= ce_end:
        key = f"{since.isoformat()}|{ce_end.isoformat()}|{account.tag_spec}"
        if state.ce_key == key:
            ce_tag, ce_untag = state.ce_tag_raw, state.ce_untag_raw
        else:
            split = cost_explorer.fetch_split(account, since, ce_end)
            if split.error:
                summary.problems.append(f"{account.account} 查不到 CE：{split.error}")
                log(f"[跳过额度判定] {account.account}：CE {split.error}")
                return
            ce_tag, ce_untag = split.tag_raw, split.untag_raw
            state.ce_key, state.ce_tag_raw, state.ce_untag_raw = key, ce_tag, ce_untag

    # 昨天 + 今天：CW 估算
    recent = estimate_split(account, max(since, ce_end + timedelta(days=1)), utc_today)
    if recent.errors:
        # 缺了最近两天就会低估，低估的数去判阈值没意义，这一轮先不判
        reason = "；".join(map(str, recent.errors))   # 同上：可能是 QueryError
        summary.problems.append(f"{account.account} 读不到 CloudWatch：{reason}")
        log(f"[跳过额度判定] {account.account}：CW {reason}")
        return

    spent = ce_tag * account.tag_ratio + ce_untag * account.untag_ratio + recent.marked(account)
    pct = spent / account.budget * 100
    crossed = [t for t in config.TELEGRAM_THRESHOLDS if pct >= t and t not in state.fired]
    if not crossed:
        return

    # 一次跨过好几档（比如刚开启告警时已经 85%）只发最高那一档，别连着刷三条
    top = max(crossed)
    card = quota_card(account, top, pct, spent, status, since, recent.unpriced)
    groups = _deliver_all(account.tg_chat_ids, card, summary, send, log)
    if groups:
        state.fired = sorted(set(state.fired) | set(crossed))
        if not dry_run:
            _record(card, groups, account.account, account.email)


def run_hourly(
    now: datetime | None = None, *, dry_run: bool = False, save_dir: Path | None = None, log=print
) -> RunSummary:
    summary = RunSummary()
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    current = now.replace(minute=0, second=0, microsecond=0)
    # 最近 N 个**完整**的整点小时，从早到晚。当前这个小时还没过完，不算
    hours = [current - timedelta(hours=n) for n in range(config.TELEGRAM_IDLE_HOURS, 0, -1)]

    targets = _targets()
    states = load_state()
    if not targets:
        log("没有开启 TG 告警的账号。")
    elif not dry_run and not telegram.configured():
        summary.problems.append("没有配置 TELEGRAM_BOT_TOKEN")
        log("没有配置 TELEGRAM_BOT_TOKEN，告警发不出去。")
        return summary

    send = _dry_run_sender(log, save_dir) if dry_run else _send
    for account in targets:
        state = states.setdefault(account.account, AccountState())
        _check_usage(account, state, hours, summary, send, log, dry_run=dry_run)
        _check_quota(account, state, now, summary, send, log, dry_run=dry_run)
        if not dry_run:
            save_state(states)  # 每个账号处理完就落盘，中途挂了也不会重发前面的

    # 关掉告警的账号把状态也清掉：以后再开启时从头建基线，不拿几周前的旧状态
    # 去比，免得一开启就收到一条过时的「用量中断」
    active_ids = {a.account for a in targets}
    stale = [key for key in states if key not in active_ids]
    if stale and not dry_run:
        for key in stale:
            del states[key]
        save_state(states)
    return summary
