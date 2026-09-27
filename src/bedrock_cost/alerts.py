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

为什么额度阈值是 CE + CW 拼起来的：CE 有一到两天延迟，光用 CE 今天花的钱要后天
才看得到；光用 CW 又有个坑——ListMetrics 只列近两周有数据的模型，三周前用过、
最近没用的模型会从累计里悄悄消失。所以历史用 CE（完整），最近两天用 CW（及时，
而且落在两周窗口里）。两段按 UTC 日期切开，不重叠。

跨次运行要记住的东西放在 ALERT_STATE_PATH 那个 JSON 里，见 AccountState。
"""

from __future__ import annotations

import html
import json
import os
import tempfile
import unicodedata
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone

from . import config, cost_explorer, telegram
from .cloudwatch_metrics import hourly_invocations
from .cost_estimate import estimate_split
from .dates import CUMULATIVE_OK, cumulative_range
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


Sender = Callable[[str, str], None]


def _deliver(chat_id: str, text: str, summary: RunSummary, send: Sender, log) -> bool:
    try:
        send(chat_id, text)
    except TelegramError as exc:
        summary.problems.append(f"发往 {chat_id} 失败：{exc}")
        log(f"[发送失败] {chat_id}：{exc}")
        return False
    summary.sent += 1
    return True


def _deliver_all(
    chat_ids: tuple[str, ...], text: str, summary: RunSummary, send: Sender, log
) -> bool:
    """同一条消息发给一个账号的全部群。**有一个群发成功就算发过了。**

    不要求全部成功：一个群 ID 坏了（bot 被踢了）就不让状态前进的话，下一小时
    其他好好的群会再收到一遍，每小时一遍，直到有人修好那个 ID。坏掉的那个会记成
    问题、让命令非零退出，journalctl 和 systemctl --failed 里看得到。

    全部失败（Telegram 整个连不上、Token 失效）才返回 False，下一小时重试。
    """
    delivered = False
    for chat_id in chat_ids:
        delivered = _deliver(chat_id, text, summary, send, log) or delivered
    return delivered


def _dry_run_sender(log) -> Sender:
    def send(chat_id: str, text: str) -> None:
        log(f"----- [dry-run] 发往 {chat_id} -----\n{text}\n")
    return send


def _targets() -> list[Account]:
    # force=True：这是一个独立的短命进程，没有可以复用的 mtime 缓存
    return [a for a in load_accounts(force=True) if a.tg_active]


# --------------------------------------------------------------- 格式
def _esc(text: object) -> str:
    return html.escape(str(text), quote=False)


def _money(value: float) -> str:
    return f"{config.CURRENCY_SYMBOL}{value:,.2f}"


def _pct(value: float) -> str:
    return f"{value:g}%"


def _width(text: str) -> int:
    """等宽字体里的显示宽度：中文占两格。<pre> 里要按这个对齐，按字符数对会歪。"""
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in text)


def _pad(text: str, width: int, right: bool = False) -> str:
    gap = " " * max(0, width - _width(text))
    return gap + text if right else text + gap


def _local_hour(stamp: datetime) -> str:
    """UTC 整点 → 服务器本地时间的「09-27 14:00」。"""
    return stamp.astimezone().strftime("%m-%d %H:%M")


def _account_line(account: Account) -> str:
    # 只写账号 ID，不带上游（台账的 PARTNER 列）：所有 TG 消息都不出现上游
    return f"账号 <code>{_esc(account.account)}</code>"


def format_daily(today: date, rows: list[ReportRow]) -> str:
    """一个群的日报。表格放 <pre> 里对齐，脚注写口径和需要注意的行。"""
    header = ("UID", "消费", "余额")
    cells = []
    for row in rows:
        if row.error:
            cells.append((row.account, "查询失败", "—"))
        else:
            cells.append((row.account, _money(row.total_cost), _money(row.balance)))
    widths = [max(_width(line[i]) for line in [header, *cells]) for i in range(3)]

    def line(values: tuple[str, str, str]) -> str:
        return "  ".join(
            (
                _pad(values[0], widths[0]),
                _pad(values[1], widths[1], right=True),
                _pad(values[2], widths[2], right=True),
            )
        ).rstrip()

    table = "\n".join([line(header), *(line(c) for c in cells)])
    notes = []
    for row in rows:
        if row.error:
            notes.append(f"❌ {_esc(row.account)} 查询失败：{_esc(row.error)}")
        elif row.range_status != CUMULATIVE_OK and row.range_hint:
            notes.append(f"⚠️ {_esc(row.account)}：{_esc(row.range_hint)}")
        if not row.error and row.overspent:
            notes.append(f"🚨 {_esc(row.account)} 已超出额度")

    parts = [
        f"📊 <b>Bedrock 日报</b> · {today.isoformat()}",
        f"<pre>{_esc(table)}</pre>",
        "消费自各账号的启用日期累计至今天（Cost Explorer 实账），余额 = 额度 − 消费。",
    ]
    if notes:
        parts.append("\n".join(notes))
    return "\n".join(parts)


def format_started(account: Account, hour: datetime, count: float) -> str:
    return "\n".join([
        "🟢 <b>开始有用量</b>",
        _account_line(account),
        f"{_local_hour(hour)} 这一小时调用 {count:,.0f} 次",
    ])


def format_stopped(account: Account, hours: list[datetime], last_active: str) -> str:
    span = (
        f"{_local_hour(hours[0])} 起的 {len(hours)} 个小时"
        if len(hours) > 1
        else f"{_local_hour(hours[0])} 这一小时"
    )
    lines = [
        "🔴 <b>用量中断</b>",
        _account_line(account),
        f"{span}没有任何调用",
    ]
    if last_active:
        lines.append(f"上一次有调用：{_local_hour(datetime.fromisoformat(last_active))} 那一小时")
    return "\n".join(lines)


def format_quota(
    account: Account,
    threshold: float,
    pct: float,
    spent: float,
    status: str,
    since: date,
    unpriced: list[str],
) -> str:
    icon, title = ("🚨", "额度已用完") if threshold >= 100 else ("⚠️", f"额度已用 {_pct(threshold)}")
    balance = account.budget - spent
    lines = [
        f"{icon} <b>{title}</b>",
        _account_line(account),
        f"累计消费 {_money(spent)} / 额度 {_money(account.budget)}（{pct:.1f}%）",
        f"余额 {_money(balance)}",
        f"口径：自 {since.isoformat()} 累计；截至前天为 Cost Explorer 实账，"
        "最近两天为 CloudWatch token × 牌价估算",
    ]
    if status != CUMULATIVE_OK:
        lines.append("⚠️ 台账未填启用日期，或启用日期早于 Cost Explorer 的保留期——实际消费可能更高")
    if unpriced:
        lines.append(f"⚠️ 这些模型没有单价、按 0 算了：{_esc('、'.join(unpriced))}")
    return "\n".join(lines)


def format_test(account_label: str) -> str:
    lines = ["✅ <b>测试消息</b>", "Bedrock 成本监控的 bot 可以往这个群发消息了。"]
    if account_label:
        lines.append(f"账号：{_esc(account_label)}")
    lines += [
        "",
        "开启 TG 告警后，这个群会收到：",
        "· 每天 09:00 的消费日报",
        "· 用量开始 / 中断时的提醒",
        f"· 额度用到 {' / '.join(_pct(t) for t in config.TELEGRAM_THRESHOLDS)} 时的告警",
    ]
    return "\n".join(lines)


def send_test(chat_id: str, account_label: str = "") -> None:
    """账号管理页「发测试消息」按钮。失败抛 TelegramError，页面上显示原因。"""
    telegram.send_message(chat_id, format_test(account_label))


# --------------------------------------------------------------- 日报
def run_daily(
    today: date | None = None, *, dry_run: bool = False, log=print
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

    send = _dry_run_sender(log) if dry_run else telegram.send_message
    for chat_id, rows in by_chat.items():
        _deliver(chat_id, format_daily(today, rows), summary, send, log)
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

    text = (
        format_started(account, hours[-1], counts[-1])
        if now_active
        else format_stopped(account, hours, state.last_active_hour)
    )
    # 发出去了才落状态：全部群都失败就保持原状，下一小时条件还成立会再发一次
    if _deliver_all(account.tg_chat_ids, text, summary, send, log):
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
    text = format_quota(account, top, pct, spent, status, since, recent.unpriced)
    if _deliver_all(account.tg_chat_ids, text, summary, send, log):
        state.fired = sorted(set(state.fired) | set(crossed))


def run_hourly(
    now: datetime | None = None, *, dry_run: bool = False, log=print
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

    send = _dry_run_sender(log) if dry_run else telegram.send_message
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
