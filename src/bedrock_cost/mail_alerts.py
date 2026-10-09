"""邮件告警：定时收各账号的告警邮箱，认出 AWS 的几类要紧通知，发成 TG 卡片。

由 systemd timer 每 5 分钟调一次（bedrock-cost mail check），跑完就退出——和 alerts 一样
不塞进 web 进程：gunicorn 一重启就可能漏收或重发。

**收谁的**：台账里 MAIL_ENABLED 开着、邮箱填全了、账号本身也启用的（Account.mail_active）。
几个账号填了同一个邮箱只收一次。

**只读**：收件箱用 EXAMINE 打开、取信用 BODY.PEEK，不会把邮件标成已读（见 mail_inbox）。

**只发新的**：第一次收一个邮箱只记下「现在收到第几封了」，不补发旧邮件；之后每次只看比
上次新的。邮箱的编号体系变了（UIDVALIDITY）也当第一次。关掉邮件告警的邮箱状态会清掉，
再打开时同样从那一刻算起。

**认什么、贴什么**：见 mail_rules。五类：滥用报告、疑似被盗用、暂停 / 关闭、工单、root 安全；
其余一律不发。

**发到哪**：
  · 邮件里写了台账里的账号 ID → 这个账号的 TG 群（跟 TG 开关走）；
  · 没写账号 ID → 收到这封邮件的那个账号的群（几个账号共用一个邮箱时认不出是谁，发固定群）；
  · 写的账号 ID 不在台账里、或者那个账号的 TG 告警没开 → 固定群（MAIL_ALERT_CHAT_IDS）；
  · root 安全类（MFA 被停用、登录验证、重置密码）→ 先发固定群，没配固定群才发账号的群。
两边都没有就记成问题，这封跳过。

**去重**：同一个账号、同一类、同一个主题，MAIL_DEDUPE_MINUTES 分钟内只发一次——AWS 常把同一条
通知从 no-reply@amazonaws.com 和 health@aws.com 各发一遍。

**发不出去**：一封邮件的所有群都发失败（Telegram 连不上、Token 失效）就停在这封，下一轮重试；
连续 MAX_ATTEMPTS 轮都失败就跳过它、记成问题，免得一封发不出去的邮件卡住后面所有的。

**卡片上的账号**：账号 ID，认得出是台账里哪个账号时再跟上它的账号邮箱（写全）。发出去了的
记一条进告警事件流（events.py，运营看板读它），dry-run 不记。

状态在 MAIL_STATE_PATH 那个 JSON 里：每个邮箱的 UIDVALIDITY 和收到第几封、最近发过的告警。
"""

from __future__ import annotations

import html
import json
import os
import re
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import alerts, config, mail_inbox, mail_rules, telegram
from .alerts import RunSummary, Sender
from .cards import Card, Details, Notes, Quote, Row
from .excel_source import Account, load_accounts
from .mail_inbox import Connector, MailError, Mailbox, Session
from .mail_rules import Finding, Mail

STATE_VERSION = 1
MAX_ATTEMPTS = 6            # 一封邮件连续几轮发不出去就放弃（每 5 分钟一轮，约半小时）
MAX_NEW_PER_RUN = 500       # 一轮最多看多少封新邮件的信头
MAX_ALERTS_PER_RUN = 20     # 一轮最多发多少张卡片，剩下的下一轮接着发

ICONS = {"danger": "warning-red", "warn": "warning", "ok": "dot-green"}
EMOJI = {"danger": "🚨", "warn": "⚠️", "ok": "✅"}


# --------------------------------------------------------------- 状态
@dataclass
class BoxState:
    uidvalidity: int = 0
    last_uid: int = 0          # 处理到第几封了（UID）
    stuck_uid: int = 0         # 正在重试、一直发不出去的那封
    stuck_attempts: int = 0


@dataclass
class MailState:
    boxes: dict[str, BoxState] = field(default_factory=dict)
    recent: dict[str, str] = field(default_factory=dict)   # 去重指纹 -> 发出的时间（ISO，UTC）


def load_state(path: Path | None = None) -> MailState:
    path = path or config.MAIL_STATE_PATH
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return MailState()
    except (OSError, ValueError):
        # 文件坏了就从头来：最坏是每个邮箱重新建一次基线，漏掉这几分钟里到的邮件
        return MailState()
    known = set(BoxState.__dataclass_fields__)
    boxes = {
        key: BoxState(**{k: v for k, v in values.items() if k in known})
        for key, values in (raw.get("mailboxes") or {}).items()
        if isinstance(values, dict)
    }
    recent = {k: v for k, v in (raw.get("recent") or {}).items() if isinstance(v, str)}
    return MailState(boxes=boxes, recent=recent)


def save_state(state: MailState, path: Path | None = None) -> None:
    """临时文件 + 原子替换：写到一半断电，旧文件还是完整的。"""
    path = path or config.MAIL_STATE_PATH
    payload = {
        "version": STATE_VERSION,
        "mailboxes": {key: asdict(value) for key, value in sorted(state.boxes.items())},
        "recent": dict(sorted(state.recent.items())),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temp = tempfile.mkstemp(prefix=".mail-state-", suffix=".json", dir=path.parent)
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


def _forget(state: MailState, now: datetime) -> None:
    """去重记录只留一天：比去重窗口长得多，又不会无限堆。"""
    keep = timedelta(minutes=max(config.MAIL_DEDUPE_MINUTES * 2, 24 * 60))
    for key, stamp in list(state.recent.items()):
        try:
            old = now - datetime.fromisoformat(stamp) > keep
        except (TypeError, ValueError):
            old = True
        if old:
            del state.recent[key]


# --------------------------------------------------------------- 卡片
def mail_card(finding: Finding, account_id: str, mail: Mail, email: str = "") -> Card:
    """一封认出来的邮件 -> 一张卡片。和其他告警卡片同一套样式：

        标题行（图标 + 标题 + 徽章）/ 详情面板（账号 UID + 账号邮箱 + 关键字段 + 收到时间）/
        邮件原文（原主题 + 节选）/ 怎么处理 / 底部发件人 + 署名

    email 是台账里这个账号的邮箱；账号不在台账里、认不出是哪个账号时是空的。
    """
    rows = [Row(fact.label, fact.value, tone=fact.tone, pill=fact.pill) for fact in finding.facts]
    rows.append(Row("收到时间", alerts._moment(mail.received) if mail.received else "—", bold=False))
    blocks: list = [
        Details(rows, uid=account_id, email=email),
        Quote("邮件原文（节选）" if finding.excerpt else "邮件主题", finding.excerpt, heading=mail.subject),
    ]
    if finding.notes:
        blocks.append(Notes("怎么处理", finding.notes))
    sender = f"{mail.sender_name} <{mail.sender}>" if mail.sender_name else mail.sender
    return Card(
        kind=f"mail-{finding.kind}",
        tone=finding.tone,
        icon=ICONS.get(finding.tone, "warning"),
        title=finding.title,
        badge=finding.badge,
        subtitle=finding.subtitle,
        blocks=blocks,
        footer=f"来自 {sender}" if sender else "",
        caption=_mail_caption(finding, account_id, mail, email),
    )


def _mail_caption(finding: Finding, account_id: str, mail: Mail, email: str = "") -> str:
    """图片下面的文字：标题、关键字段、主题、原文节选（引用块）、AWS 链接。

    节选最长，放不下就只截它，别的行都保住——整段不能超过 Telegram 的 1024 字。
    """
    esc = alerts._esc
    head = f"{EMOJI.get(finding.tone, '📧')} <b>{esc(finding.title)}</b>"
    if account_id:
        head += f" · {alerts._uid(account_id, email)}"
    lines = [head]
    facts = " · ".join(f"{fact.label} {fact.value}" for fact in finding.facts)
    if facts:
        lines.append(esc(facts))
    lines.append(f"主题：{esc(mail.subject)}")
    links = " · ".join(
        f'<a href="{html.escape(url, quote=True)}">{esc(label)}</a>' for label, url in finding.links
    )
    if finding.excerpt:
        used = len(telegram.visible("\n".join([*lines, links]))) + 2
        room = telegram.MAX_CAPTION_CHARS - used - 1
        if room >= 60:
            quote = mail_rules._clip(finding.excerpt, room)
            lines.append(f"<blockquote>{esc(quote)}</blockquote>")
    if links:
        lines.append(links)
    return alerts._caption(lines)


# --------------------------------------------------------------- 发到哪
def _target(finding: Finding, owners: list[Account], ledger: dict[str, Account]) -> tuple[str, Account | None]:
    """这封邮件说的是哪个账号：(卡片上写的账号 ID, 台账里对应的账号或 None)。"""
    for about in finding.account_ids:
        if about in ledger:
            return about, ledger[about]
    if finding.account_ids:
        return finding.account_ids[0], None      # 写了账号，但不在台账里
    if len(owners) == 1:
        return owners[0].account, owners[0]      # 没写账号：就是收到这封邮件的那个
    return "", None                              # 几个账号共用一个邮箱，认不出是谁


def _chats(finding: Finding, account: Account | None) -> tuple[str, ...]:
    fixed = tuple(config.MAIL_ALERT_CHAT_IDS)
    own = account.tg_chat_ids if account is not None and account.tg_active else ()
    if finding.to_fixed:
        return fixed or own
    return own or fixed


def _fingerprint(finding: Finding, account_id: str, mail: Mail) -> str:
    subject = re.sub(r"^(?:\s*(?:re|fw|fwd)\s*:)+", "", mail.subject, flags=re.I).strip().lower()
    return f"{account_id}|{finding.kind}|{subject}"


def _mailboxes(accounts: list[Account]) -> dict[str, tuple[Mailbox, list[Account]]]:
    """要收的邮箱 -> (邮箱, 挂着它的账号)。几个账号填了同一个邮箱只收一次。"""
    boxes: dict[str, tuple[Mailbox, list[Account]]] = {}
    for account in accounts:
        box = account.mail_box if account.mail_active else None
        if box is not None:
            boxes.setdefault(box.key, (box, []))[1].append(account)
    return boxes


# --------------------------------------------------------------- 收一个邮箱
@dataclass
class _Run:
    """一轮里各个邮箱共用的东西。"""

    ledger: dict[str, Account]
    state: MailState
    now: datetime
    summary: RunSummary
    send: Sender
    log: object
    connect: Connector | None
    dry_run: bool              # dry-run 不记告警事件流


def _check_box(box: Mailbox, owners: list[Account], run: _Run, *, look_back: int = 0) -> None:
    who = "、".join(account.account for account in owners)
    log = run.log
    known = run.state.boxes.get(box.key)
    try:
        with Session(box, connect=run.connect) as session:
            if look_back:
                # dry-run 预览：不看状态，直接看最近几封；状态反正不落盘
                progress = BoxState(uidvalidity=session.uidvalidity)
                uids = session.recent(look_back)
            elif known is None or known.uidvalidity != session.uidvalidity:
                if known is not None:
                    log(f"[{who}] 邮箱的编号体系变了（UIDVALIDITY），重新建基线")
                run.state.boxes[box.key] = BoxState(uidvalidity=session.uidvalidity, last_uid=session.baseline())
                log(f"[{who}] 第一次收 {box.address}：记下当前位置，只发之后新到的邮件")
                return
            else:
                progress = known
                uids = session.new_uids(progress.last_uid)[:MAX_NEW_PER_RUN]
            if not uids:
                return

            heads = session.headers(uids)
            sent = 0
            for uid in uids:
                head = heads.get(uid)
                mail = mail_rules.read_headers(head[0], uid) if head else None
                kind = mail_rules.classify(mail) if mail else None
                if kind is None:
                    progress.last_uid = max(progress.last_uid, uid)
                    continue
                if sent >= MAX_ALERTS_PER_RUN:
                    log(f"[{who}] 这一轮已经发了 {MAX_ALERTS_PER_RUN} 条，剩下的下一轮接着发")
                    break
                sent += 1
                if not _alert(session, uid, kind, head[1], mail, owners, progress, run):
                    break   # 发不出去：停在这封，下一轮从它重试
    except MailError as exc:
        run.summary.problems.append(f"{box.address} 收信失败：{exc}")
        log(f"[收信失败] {box.address}：{exc}")


def _alert(
    session: Session, uid: int, kind: str, size: int, mail: Mail,
    owners: list[Account], progress: BoxState, run: _Run,
) -> bool:
    """取正文、拼卡片、发出去。返回 False = 这封一个群都没发出去，停下来下一轮重试。"""
    if size <= mail_inbox.MAX_BODY_BYTES:
        message = session.message(uid)
        if message is None:            # 刚被人删了
            progress.last_uid = max(progress.last_uid, uid)
            return True
        mail.body = mail_rules.body_text(message)
    finding = mail_rules.inspect(mail, kind, run.now)
    account_id, account = _target(finding, owners, run.ledger)
    label = account_id or "认不出账号"

    def done() -> bool:
        progress.last_uid = max(progress.last_uid, uid)
        progress.stuck_uid = progress.stuck_attempts = 0
        return True

    chats = _chats(finding, account)
    if not chats:
        run.summary.problems.append(
            f"{label}「{mail.subject}」没有地方发：这个账号没开 TG 告警，也没有配置 MAIL_ALERT_CHAT_IDS"
        )
        run.log(f"[没有地方发] {label}：{mail.subject}")
        return done()

    fingerprint = _fingerprint(finding, account_id, mail)
    sent_at = run.state.recent.get(fingerprint)
    if sent_at:
        try:
            fresh = run.now - datetime.fromisoformat(sent_at) < timedelta(minutes=config.MAIL_DEDUPE_MINUTES)
        except (TypeError, ValueError):
            fresh = False
        if fresh:
            run.log(f"[{label}] {config.MAIL_DEDUPE_MINUTES} 分钟内发过一样的，跳过：{mail.subject}")
            return done()

    email = account.email if account is not None else ""
    card = mail_card(finding, account_id, mail, email)
    groups = alerts._deliver_all(chats, card, run.summary, run.send, run.log)
    if groups:
        run.state.recent[fingerprint] = run.now.isoformat()
        if not run.dry_run:
            alerts._record(card, groups, account_id, email)
        return done()

    if progress.stuck_uid == uid:
        progress.stuck_attempts += 1
    else:
        progress.stuck_uid, progress.stuck_attempts = uid, 1
    if progress.stuck_attempts >= MAX_ATTEMPTS:
        run.summary.problems.append(f"{label}「{mail.subject}」连续 {MAX_ATTEMPTS} 轮都没发出去，跳过这封")
        return done()
    return False


# --------------------------------------------------------------- 入口
def run_check(
    now: datetime | None = None,
    *,
    dry_run: bool = False,
    save_dir: Path | None = None,
    look_back: int = 0,
    log=print,
    connect: Connector | None = None,
) -> RunSummary:
    """收一轮信。look_back > 0 只能和 dry_run 一起用：不看状态，把最近几封按规则过一遍。"""
    if look_back and not dry_run:
        raise ValueError("look_back 只能和 dry_run 一起用：真发的时候不补发旧邮件")
    summary = RunSummary()
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    accounts = load_accounts(force=True)   # 独立的短命进程，没有可以复用的缓存
    boxes = _mailboxes(accounts)
    state = load_state()
    if not boxes:
        log("没有打开邮件告警的账号。")
    elif not dry_run and not telegram.configured():
        summary.problems.append("没有配置 TELEGRAM_BOT_TOKEN")
        log("没有配置 TELEGRAM_BOT_TOKEN，邮件告警发不出去，这一轮不收信。")
        return summary

    _forget(state, now)
    run = _Run(
        ledger={account.account: account for account in accounts},
        state=state,
        now=now,
        summary=summary,
        send=alerts._dry_run_sender(log, save_dir) if dry_run else alerts._send,
        log=log,
        connect=connect,
        dry_run=dry_run,
    )
    for box, owners in boxes.values():
        _check_box(box, owners, run, look_back=look_back)
        if not dry_run:
            save_state(state)   # 每个邮箱收完就落盘，中途挂了也不会重发前面的

    # 不再收的邮箱把状态清掉：以后再打开时重新建基线，不补发中间这段时间的邮件
    stale = [key for key in state.boxes if key not in boxes]
    if stale and not dry_run:
        for key in stale:
            del state.boxes[key]
        save_state(state)
    return summary


def check_account(account_id: str, *, connect: Connector | None = None) -> str:
    """命令行 mail test：登录这个账号的告警邮箱，只读地看一眼。失败抛 MailError。"""
    accounts = load_accounts(force=True, include_disabled=True)
    account = next((a for a in accounts if a.account == account_id), None)
    if account is None:
        raise MailError(f"台账里没有账号 {account_id}")
    box = account.mail_box
    if box is None:
        raise MailError(f"账号 {account_id} 还没有填好告警邮箱（平台、地址、密码）")
    return mail_inbox.probe(box, mail_rules.classify_headers, connect=connect)
