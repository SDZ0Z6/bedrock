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

**账号变动**：账号管理页上新增、停用、恢复一个账号时，给它的群发一张通知卡片
（notify_account）。跟 TG 开关走：开关开着、填了群才发。这类消息由 web 进程当场发，
不走 systemd timer。

**长什么样**：每条都是一张深色卡片图（cards.py 画）+ 图片下面一段文字（caption，
账号 ID 和关键数字）。卡片画不出来就退回只发那段文字，见 _send。

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

from . import cards, config, cost_explorer, telegram
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
from .cloudwatch_metrics import hourly_invocations
from .cost_estimate import estimate_split
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
) -> bool:
    """同一张卡片发给一个账号的全部群（只画一次）。**有一个群发成功就算发过了。**

    不要求全部成功：一个群 ID 坏了（bot 被踢了）就不让状态前进的话，下一小时
    其他好好的群会再收到一遍，每小时一遍，直到有人修好那个 ID。坏掉的那个会记成
    问题、让命令非零退出，journalctl 和 systemctl --failed 里看得到。

    全部失败（Telegram 整个连不上、Token 失效）才返回 False，下一小时重试。
    """
    delivered = False
    for chat_id in chat_ids:
        delivered = _deliver(chat_id, card, summary, send, log) or delivered
    return delivered


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


def _uid(account_id: str) -> str:
    # 只写账号 ID，不带上游（台账的 PARTNER 列）：所有 TG 消息都不出现上游
    return f"<code>{_esc(account_id)}</code>"


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


def daily_cards(today: date, rows: list[ReportRow]) -> list[Card]:
    """一个群的日报。账号多于 cards.MAX_TABLE_ROWS 个就分成几张发。"""
    size = cards.MAX_TABLE_ROWS
    chunks = [rows[start:start + size] for start in range(0, len(rows), size)] or [[]]
    return [
        _daily_card(today, chunk, len(rows), index, len(chunks))
        for index, chunk in enumerate(chunks, start=1)
    ]


def _daily_card(today: date, rows: list[ReportRow], everyone: int, index: int, total: int) -> Card:
    table: list[list[Cell]] = []
    notes: list[tuple[str, str]] = []
    lines: list[str] = []
    for row in rows:
        uid = row.account
        budget = Cell(_money(row.budget)) if row.budget > 0 else Cell("未设额度", "muted")
        if row.error:
            table.append([Cell(uid, mark="danger"), budget, Cell("查询失败", "danger"), Cell("—", "muted")])
            notes.append(("danger", f"{uid} 查询失败：{row.error}"))
            lines.append(f"{_uid(uid)} 查询失败")
            continue

        mark, flags = "", []
        if row.range_status != CUMULATIVE_OK and row.range_hint:
            mark = "warn"
            flags.append(_RANGE_SHORT.get(row.range_status, "启用日期有问题"))
            notes.append(("warn", f"{uid}：{row.range_hint}"))
        if row.overspent:
            mark = "danger"
            flags.append("已超出额度")
            notes.append(("danger", f"{uid} 已超出额度 {_money(-row.balance)}"))
        balance = (
            Cell(_money(row.balance), "danger" if row.balance < 0 else "ok")
            if row.budget > 0
            else Cell("—", "muted")
        )
        table.append([Cell(uid, mark=mark), budget, Cell(_money(row.total_cost)), balance])
        remaining = f" · 剩余额度 {_money(row.balance)}" if row.budget > 0 else ""
        flagged = f"（{'，'.join(flags)}）" if flags else ""
        lines.append(f"{_uid(uid)} 累计消费 {_money(row.total_cost)}{remaining}{flagged}")

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
            Uid(account.account),
            Tiles([
                Tile("检测时段", when),
                Tile("本小时调用次数", calls, tone="ok", big=True, edge=True),
            ]),
        ],
        footer="检测到该账号本小时开始产生调用。",
        caption=f"🟢 <b>用量开始</b> · {_uid(account.account)}\n{when} 这一小时调用 {calls} 次",
    )


def stopped_card(account: Account, hours: list[datetime], last_active: str) -> Card:
    first = _local_hour(hours[0])
    if len(hours) > 1:
        label, when, gist = "检测时段", f"{first} 起", f"连续 {len(hours)} 个小时没有任何调用"
        summary = f"{first} 起连续 {len(hours)} 个小时没有任何调用"
    else:
        label, when, gist = "当前检测时段", first, "本小时没有任何调用"
        summary = f"{first} 这一小时没有任何调用"
    blocks: list = [
        Uid(account.account),
        Tiles([Tile(label, when, big=True, edge=True, note=gist, note_tone="danger")]),
    ]
    lines = [f"🔴 <b>用量中断</b> · {_uid(account.account)}", summary]
    if last_active:
        previous = f"{_local_hour(datetime.fromisoformat(last_active))} 那一小时"
        blocks.append(Since("上一次有调用", previous))
        lines.append(f"上一次有调用：{previous}")
    return Card(
        kind="stopped",
        tone="danger",
        icon="dot-red",
        title="用量中断",
        badge="INTERRUPTED",
        subtitle="BEDROCK · USAGE ALERT",
        blocks=blocks,
        footer="请检查账号调用情况及相关服务状态。",
        caption=_caption(lines),
    )


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
        Uid(account.account),
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
            f"{'🚨' if used_up else '⚠️'} <b>{title}</b> · {_uid(account.account)}",
            f"额度已用 {pct:.1f}%，{gist}",
            f"累计消费 {_money(spent)} / 总额度 {_money(account.budget)}，剩余 {_money(balance)}",
        ]),
    )


def _moment(now: datetime | None = None) -> str:
    """服务器本地时间「2026-09-28 17:00」，卡片上的测试时间、启用 / 停用时间用。"""
    return (now or datetime.now(timezone.utc)).astimezone().strftime("%Y-%m-%d %H:%M")


def _budget(account: Account) -> str:
    return _money(account.budget) if account.budget > 0 else "未设额度"


def ping_card(account_id: str = "", now: datetime | None = None) -> Card:
    """测试消息。卡片照截图不放账号；account_id 只写在图片下面的文字里（命令行发的没有）。"""
    head = "🧪 <b>测试消息</b>" + (f" · {_uid(account_id)}" if account_id else "")
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


def send_test(chat_id: str, account_id: str = "") -> str:
    """账号管理页「发测试消息」和命令行 alerts test。失败抛 TelegramError。

    返回空串，或者「卡片画不出来、改发了文字」的说明——页面和命令行都会把它显示出来。
    """
    return _send(chat_id, ping_card(account_id))


# --------------------------------------------------------------- 账号变动
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
                ],
                uid=account.account,
            ),
        ],
        footer="系统已开始监控该账号的用量及额度情况。",
        footer_icon="satellite",
        footer_rule=False,
        caption=f"🟢 <b>新账号启用</b> · {_uid(account.account)}\n授信额度 {_budget(account)} · 启用时间 {when}",
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
                ],
                uid=account.account,
            ),
        ],
        footer="系统已恢复监控该账号的用量及额度情况。",
        footer_icon="satellite",
        footer_rule=False,
        caption=f"🟢 <b>账号恢复启用</b> · {_uid(account.account)}\n授信额度 {_budget(account)} · 恢复时间 {when}",
    )


def disabled_card(account: Account, spend: ReportRow, now: datetime | None = None) -> Card:
    """spend 是停用那一刻的累计消费（和概览页、日报同一个口径）。"""
    when = _moment(now)
    rows = [Row("账号状态", "已停用", tone="gray", pill=True), Row("停用时间", when)]
    if spend.error:
        rows.append(Row("停用前累计消费", "查询失败", tone="danger", bold=False))
        gist = "停用前累计消费查询失败"
    else:
        rows.append(Row("停用前累计消费", _money(spend.total_cost)))
        if account.budget > 0:
            rows.append(Row("停用前剩余额度", _money(spend.balance), tone="danger" if spend.balance < 0 else ""))
            gist = f"停用前累计消费 {_money(spend.total_cost)} · 剩余额度 {_money(spend.balance)}"
        else:
            gist = f"停用前累计消费 {_money(spend.total_cost)}"
    return Card(
        kind="disabled",
        tone="gray",
        icon="dot-gray",
        title="账号停用",
        badge="DEACTIVATED",
        subtitle="BEDROCK · ACCOUNT NOTIFICATION",
        blocks=[
            Status("pause-circle", "账号已停用", "该账号已从活动监控中停用"),
            Details(rows, uid=account.account),
        ],
        footer="系统已停止该账号的活动监控及相关告警。",
        footer_icon="satellite",
        footer_rule=False,
        caption=f"⚫ <b>账号停用</b> · {_uid(account.account)}\n{gist}",
    )


def _spend(account: Account, today: date) -> ReportRow:
    """这一刻的累计消费：和概览页、日报同一组函数（有缓存就用缓存）。"""
    period = cumulative_range(account.start_date, today)
    split = cost_explorer.fetch_all([account], {account.key: period[:2]}).get(account.key)
    return build_row(account, split or cost_explorer.CostSplit(error="未取到数据"), period)


def notify_account(
    event: str, account: Account, *, now: datetime | None = None, log=print
) -> RunSummary | None:
    """账号管理页上新增（created）/ 停用（disabled）/ 恢复（restored）了一个账号：给它的群发卡片。

    跟 TG 开关走：这个账号的 TG 告警没开或者没填群，就不发，返回 None。看的是开关和群，
    不看 tg_active——停用的时候账号已经不算 tg_active 了，停用通知正是它的最后一条消息。
    发送失败不影响台账（台账在这之前已经写好了），只记在返回的 RunSummary 里。
    """
    if not (account.tg_enabled and account.tg_chat_ids):
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
    else:
        raise ValueError(f"不认识的账号变动：{event}")
    _deliver_all(account.tg_chat_ids, card, summary, _send, log)
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

    send = _dry_run_sender(log, save_dir) if dry_run else _send
    for chat_id, rows in by_chat.items():
        for card in daily_cards(today, rows):
            _deliver(chat_id, card, summary, send, log)
    return summary


# --------------------------------------------------------------- 小时任务
def _check_usage(
    account: Account,
    state: AccountState,
    hours: list[datetime],
    summary: RunSummary,
    send: Sender,
    log,
) -> None:
    counts, failed = hourly_invocations(account, hours)
    if counts is None:
        # 读不到就不判定：缺一个区的零不能当成真的零，否则会误报「用量中断」
        summary.problems.append(f"{account.account} 读不到 CloudWatch：{'；'.join(failed)}")
        log(f"[跳过用量判定] {account.account}：{'；'.join(failed)}")
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

    card = (
        started_card(account, hours[-1], counts[-1])
        if now_active
        else stopped_card(account, hours, state.last_active_hour)
    )
    # 发出去了才落状态：全部群都失败就保持原状，下一小时条件还成立会再发一次
    if _deliver_all(account.tg_chat_ids, card, summary, send, log):
        state.active = now_active
        if now_active:
            state.last_active_hour = latest


def _check_quota(
    account: Account,
    state: AccountState,
    now: datetime,
    summary: RunSummary,
    send: Sender,
    log,
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
        summary.problems.append(f"{account.account} 读不到 CloudWatch：{'；'.join(recent.errors)}")
        log(f"[跳过额度判定] {account.account}：CW {'；'.join(recent.errors)}")
        return

    spent = ce_tag * account.tag_ratio + ce_untag * account.untag_ratio + recent.marked(account)
    pct = spent / account.budget * 100
    crossed = [t for t in config.TELEGRAM_THRESHOLDS if pct >= t and t not in state.fired]
    if not crossed:
        return

    # 一次跨过好几档（比如刚开启告警时已经 85%）只发最高那一档，别连着刷三条
    top = max(crossed)
    card = quota_card(account, top, pct, spent, status, since, recent.unpriced)
    if _deliver_all(account.tg_chat_ids, card, summary, send, log):
        state.fired = sorted(set(state.fired) | set(crossed))


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
        _check_usage(account, state, hours, summary, send, log)
        _check_quota(account, state, now, summary, send, log)
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
