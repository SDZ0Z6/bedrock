"""认 AWS 发到账号邮箱的邮件：是哪一类、取哪些字段、原文里哪几段值得贴出来。

只认五类，其余一律不发——Marketplace 的订阅 / offer 通知、其他 AWS 邮件、非 AWS 邮件：

  abuse        Trust & Safety 的滥用报告，最要紧的是「撤销 Anthropic 模型权限」   红
  compromised  账号疑似被盗用：密钥或 root 密码可能泄露，部分服务已被限制         红
  suspended    账号被暂停、关闭，资源被终止                                     红
  case         AWS 开的工单、工单的跟进回复                                     橙
  root         root 账号安全变更：MFA 被停用、登录要验证身份、重置密码           橙

**认类别只看信头**（发件人 + 主题），不用取正文；取字段和节选才看正文。

发件人必须是 AWS 的域名。收件服务器在 Authentication-Results 里明说 DMARC 没通过的当冒充：
群里的人会照着告警去操作，假冒的「AWS 通知」不能借 bot 的嘴说出来。同样的道理，卡片和
文字里只放 AWS 自己域名下的链接（申诉表单之类的第三方链接只说「见邮件」）。

**节选**只留「发生了什么、要做什么、最后期限」：去掉称呼、客套、操作步骤、链接、
机器翻译说明和页脚；已经写进字段的几行（Action / Case ID……）也不重复。密钥只露头尾，
邮箱地址打码。登录验证、重置密码这两种邮件里有验证码或重置链接，正文一个字都不取。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from email.message import EmailMessage
from email.utils import parseaddr, parsedate_to_datetime
from html.parser import HTMLParser
from urllib.parse import urlsplit

# 发件人域名：这些域名本身或者它们的子域名。aws 是顶级域名（signin.aws、marketplace.aws）
AWS_DOMAINS = ("amazonaws.com", "amazon.com", "aws.com", "aws")
# 卡片和文字里允许出现的链接：AWS 控制台、文档、re:Post
SAFE_LINK_DOMAINS = ("aws.amazon.com", "repost.aws")

# 节选最多多少字。卡片上大约八九行，放得下「发生了什么 + 最后期限」
EXCERPT_LIMIT = 700


@dataclass
class Mail:
    """一封邮件里用得到的部分。body 只在认出类别之后才取。"""

    uid: int = 0
    sender_name: str = ""
    sender: str = ""          # 地址，小写
    subject: str = ""
    received: datetime | None = None
    auth: str = ""            # Authentication-Results，挡冒充用
    body: str = ""            # 纯文本正文


@dataclass
class Fact:
    """卡片详情面板里的一行。"""

    label: str
    value: str
    tone: str = ""
    pill: bool = False


@dataclass
class Finding:
    """一封认出来的邮件：卡片上要写什么。"""

    kind: str
    title: str
    tone: str                 # danger / warn / ok
    badge: str
    subtitle: str
    account_ids: list[str] = field(default_factory=list)   # 邮件里的 12 位账号 ID，主题里的排前面
    facts: list[Fact] = field(default_factory=list)
    excerpt: str = ""
    notes: list[tuple[str, str]] = field(default_factory=list)   # (颜色, 一句话)
    links: list[tuple[str, str]] = field(default_factory=list)   # (文字, 地址)，只有 AWS 的
    to_fixed: bool = False    # root 类：优先发固定群（root 是运维管的，不是用账号的人）


# ---------------------------------------------------------------- 信头
def _header(message: EmailMessage, name: str) -> str:
    try:
        value = message.get(name)
    except Exception:   # 信头坏得解析不了：当没有
        return ""
    return re.sub(r"\s+", " ", str(value)).strip() if value is not None else ""


def _sender(message: EmailMessage) -> tuple[str, str]:
    try:
        raw = message.get("From")
    except Exception:
        return "", ""
    if raw is None:
        return "", ""
    addresses = getattr(raw, "addresses", None)
    if addresses:
        return addresses[0].display_name or "", addresses[0].addr_spec or ""
    return parseaddr(str(raw))


def _received(message: EmailMessage) -> datetime | None:
    try:
        raw = message.get("Date")
    except Exception:
        return None
    if raw is None:
        return None
    stamp = getattr(raw, "datetime", None)
    if stamp is None:
        try:
            stamp = parsedate_to_datetime(str(raw))
        except (TypeError, ValueError, IndexError):
            return None
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)


def read_headers(message: EmailMessage, uid: int = 0) -> Mail:
    name, address = _sender(message)
    try:
        auth = " ".join(str(value) for value in message.get_all("Authentication-Results") or [])
    except Exception:
        auth = ""
    return Mail(
        uid=uid,
        sender_name=name.strip(),
        sender=address.strip().lower(),
        subject=_header(message, "Subject"),
        received=_received(message),
        auth=auth,
    )


def read_message(message: EmailMessage, uid: int = 0) -> Mail:
    mail = read_headers(message, uid)
    mail.body = body_text(message)
    return mail


# ---------------------------------------------------------------- 正文
class _Text(HTMLParser):
    BLOCK = {
        "p", "div", "br", "tr", "li", "ul", "ol", "table", "section", "article", "blockquote",
        "h1", "h2", "h3", "h4", "h5", "h6", "hr", "header", "footer",
    }
    SKIP = {"script", "style", "head", "title"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.skipping = 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.skipping += 1
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_startendtag(self, tag, attrs):
        if tag in self.BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self.SKIP:
            self.skipping = max(0, self.skipping - 1)
        elif tag in self.BLOCK and tag != "br":
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.skipping:
            self.parts.append(data)


def html_to_text(markup: str) -> str:
    """HTML 邮件 -> 纯文本：块级标签换行，脚本和样式丢掉，实体还原。"""
    parser = _Text()
    parser.feed(markup)
    parser.close()
    lines = [re.sub(r"[ \t\f\v\xa0]+", " ", line).strip() for line in "".join(parser.parts).split("\n")]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def body_text(message: EmailMessage) -> str:
    """正文的纯文本。有 text/plain 就用它，只有 HTML 就转一下。"""
    try:
        part = message.get_body(preferencelist=("plain", "html"))
    except Exception:
        part = None
    if part is None:
        return ""
    try:
        content = part.get_content()
    except Exception:   # 字符集写错、编码坏了：尽量按字节解出来
        payload = part.get_payload(decode=True) or b""
        try:
            content = payload.decode(part.get_content_charset() or "utf-8", "replace")
        except LookupError:
            content = payload.decode("utf-8", "replace")
    if not isinstance(content, str):
        return ""
    if part.get_content_type() == "text/html":
        content = html_to_text(content)
    return content


def _normalize(text: str) -> str:
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n").replace("\xa0", " ")
    lines = [line.rstrip() for line in text.split("\n")]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


# ---------------------------------------------------------------- 认类别
_MARKETPLACE = re.compile(r"\bAWS Marketplace\b|\bMarketplace (?:offer|subscription)", re.I)
_CASE = re.compile(r"\[CASE\s+\d+\]|\bNew Support case\b", re.I)
_ABUSE = re.compile(r"\bAbuse Report\b", re.I)
_COMPROMISED = re.compile(
    r"Suspicious activity in your AWS account"
    r"|review your AWS Account and credentials"
    r"|\bcompromised\b"
    r"|\bunauthori[sz]ed (?:access|activity|use|usage)\b",
    re.I,
)
_ROOT_MFA = re.compile(r"\bMulti-Factor Authentication\b.*\b(?:Deactivated|Disabled|Removed)\b|\bMFA\b.*\b(?:deactivated|disabled|removed)\b", re.I)
_ROOT_SIGNIN = re.compile(r"^\s*Verify your identity\b|\bunusual sign[- ]?in\b", re.I)
_ROOT_PASSWORD = re.compile(r"\bPassword Assistance\b|\bpassword (?:reset|recovery)\b|\bpassword (?:has been |was )?changed\b", re.I)
_SUSPENDED = re.compile(r"\bsuspen(?:d|ded|sion)\b|\bterminat(?:ed|ion)\b|\bclos(?:ed|ure)\b", re.I)


def is_aws(sender: str) -> bool:
    domain = (sender or "").rpartition("@")[2].strip().lower()
    return bool(domain) and any(domain == d or domain.endswith("." + d) for d in AWS_DOMAINS)


def spoofed(auth: str) -> bool:
    """收件服务器明说 DMARC 没通过：发件人是冒充的。没有这个信头不算冒充。"""
    return bool(re.search(r"\bdmarc\s*=\s*fail\b", auth or "", re.I))


def classify(mail: Mail) -> str | None:
    """这封要不要发、属于哪一类。只看发件人和主题。"""
    if not is_aws(mail.sender) or spoofed(mail.auth):
        return None
    subject = mail.subject or ""
    local = mail.sender.partition("@")[0]
    if _MARKETPLACE.search(subject):
        return None
    if _CASE.search(subject):
        return "case"
    if _ABUSE.search(subject) or "trustandsafety" in local or "abuse" in local:
        return "abuse"
    if _COMPROMISED.search(subject):
        return "compromised"
    if _ROOT_MFA.search(subject) or _ROOT_SIGNIN.search(subject) or _ROOT_PASSWORD.search(subject):
        return "root"
    if _SUSPENDED.search(subject):
        return "suspended"
    return None


def classify_headers(message: EmailMessage) -> str | None:
    return classify(read_headers(message))


# ---------------------------------------------------------------- 打码 / 链接 / 账号
_KEY = re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{12,124}\b")
_EMAIL = re.compile(r"([A-Za-z0-9._%+-])[A-Za-z0-9._%+-]*@((?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,})")
_LINK = re.compile(r"https?://[^\s<>\"')\]]+")
_ACCOUNT_ID = re.compile(r"(?<!\d)\d{12}(?!\d)")


def mask_key(key: str) -> str:
    """和账号管理页一样：只露前 8 位和后 4 位。"""
    return f"{key[:8]}…{key[-4:]}" if len(key) > 12 else key[:4] + "…"


def mask_email(text: str) -> str:
    """邮箱地址打码：只留第一个字母和域名（r***@example.com）。TG 群里可能有用账号的人，
    root 邮箱不该让他们看到全貌。"""
    return _EMAIL.sub(lambda m: f"{m.group(1)}***@{m.group(2)}", text or "")


def _mask(text: str) -> str:
    return mask_email(_KEY.sub(lambda m: mask_key(m.group(0)), text))


def safe_link(url: str) -> bool:
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    host = (parts.hostname or "").lower()
    return parts.scheme == "https" and any(host == d or host.endswith("." + d) for d in SAFE_LINK_DOMAINS)


def account_ids(subject: str, text: str) -> list[str]:
    """邮件里出现的 12 位账号 ID，去重，主题里的排在前面。"""
    seen: dict[str, None] = {}
    for source in (subject, text):
        for found in _ACCOUNT_ID.findall(source or ""):
            seen.setdefault(found, None)
    return list(seen)


# ---------------------------------------------------------------- 节选
_STOP = [re.compile(pattern, re.I) for pattern in (
    r"^[=_-]{5,}",
    r"^Machine translated message",
    r"^To share your experience or contact us again",
    r"^To contact us again about this",
    r"^\*?\s*Please note:? this e-?mail was sent from an address",
    r"^Note, this e-?mail was sent from an address",
    r"^Don't miss messages from AWS",
    r"^Amazon Web Services, Inc\. is a subsidiary",
    r"^This message was produced and distributed by",
    r"^(?:Sincerely|Regards|Best regards|Kind regards|Thanks|Thank you),?\s*$",
    r"^Thank you for using Amazon Web Services",
    r"^\[\d+\]\s*https?://",
    r"^AWS Trust & Safety Center",
    r"^Follow the instructions below",
    r"^We ask that you please follow the instructions below",
    r"^Step \d+\b",
    r"^If you have any questions",
    r"^For additional help",
)]
# 单独成行的客套和按钮文字。按行丢：AWS Health 的邮件把它们挨着排，中间不一定有空行
_DROP_LINE = [re.compile(pattern, re.I) for pattern in (
    r"^(?:Dear [^,\n]{1,60}|Hello|Hi|Greetings)[,.!]?$",
    r"^Greetings from (?:Amazon Web Services|AWS)[^.]*\.$",
    r"^AWS Health Event$",
    r"^View (?:in Notification Center|details in service console|original message)$",
    r"^\(If you will connect by federation",
)]
# 整段的套话。按段丢：纯文本邮件可能在 76 个字符处硬换行，一句话会跨好几行
_DROP_PARAGRAPH = [re.compile(pattern, re.I) for pattern in (
    r"^The details of your case are as follows:?$",
    r"^As a result, in accordance with your agreement",
)]
# 已经写进卡片字段的几行，节选里不再重复
_FIELD_LINE = re.compile(r"^(?:Action|AWS account ID|Account ID|Effective date|Case ID|Severity)\s*:", re.I)
# 一次性验证码之类的行：不管哪一类邮件都不取
_SECRET_LINE = re.compile(r"verification code|security code|one[- ]time (?:pass(?:word|code)|code)|验证码|\b\d{6}\s*$", re.I)
_NOISE_SENTENCE = [re.compile(pattern, re.I) for pattern in (
    r"^Please review (?:this|the following) notice",
    r"security best practice",
    r"^For more detailed instructions",
)]
_REF = re.compile(r"\s*\[\d+\]")
_SENTENCE = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\[(\"'])")


def _clip(text: str, limit: int) -> str:
    """超长就在句号处截断，补一个省略号。"""
    if len(text) <= limit:
        return text
    head = text[:limit]
    cut = max(head.rfind(mark) for mark in (". ", ".\n", "! ", "? ", "。"))
    if cut < limit * 0.5:
        cut = head.rfind(" ")
        return head[: cut if cut > 0 else limit].rstrip() + "…"
    return head[: cut + 1].rstrip() + "…"


def excerpt(text: str, subject: str = "", limit: int = EXCERPT_LIMIT) -> str:
    """原文里值得贴出来的几段，每段一行。见模块说明。"""
    title = (subject or "").strip().lower()
    kept: list[str] = []
    for line in _normalize(text).split("\n"):
        stripped = line.strip()
        if any(pattern.search(stripped) for pattern in _STOP):
            break
        if _FIELD_LINE.match(stripped) or _SECRET_LINE.search(stripped):
            continue
        if any(pattern.search(stripped) for pattern in _DROP_LINE):
            continue
        if title and stripped.lower() == title:
            continue   # AWS Health 会把主题在正文里再写一遍
        kept.append(stripped)

    paragraphs: list[str] = []
    for block in "\n".join(kept).split("\n\n"):
        paragraph = " ".join(line for line in block.split("\n") if line).strip()
        if not paragraph or any(pattern.search(paragraph) for pattern in _DROP_PARAGRAPH):
            continue
        paragraph = _LINK.sub("", _REF.sub("", paragraph))
        paragraph = re.sub(r"\s{2,}", " ", paragraph).strip()
        sentences = [
            sentence for sentence in _SENTENCE.split(paragraph)
            if sentence and not any(pattern.search(sentence) for pattern in _NOISE_SENTENCE)
        ]
        paragraph = " ".join(sentences).strip()
        if re.search(r"[A-Za-z一-鿿]{2}", paragraph):
            paragraphs.append(paragraph)
    return _clip(_mask("\n".join(paragraphs)), limit)


# ---------------------------------------------------------------- 取字段
def _field(text: str, pattern: str, flags: int = re.I | re.M) -> str:
    found = re.search(pattern, text or "", flags)
    return found.group(1).strip() if found else ""


def _local_time(raw: str) -> str:
    """「2026-09-27 06:16 (UTC)」-> 服务器本地时间「2026-09-27 14:16」。认不出就原样。"""
    found = re.search(r"(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2})", raw or "")
    if not found:
        return raw
    if not re.search(r"\b(?:UTC|GMT|Z)\b|\+00:?00", raw):
        return f"{found.group(1)} {found.group(2)}"
    stamp = datetime.fromisoformat(f"{found.group(1)}T{found.group(2)}").replace(tzinfo=timezone.utc)
    return stamp.astimezone().strftime("%Y-%m-%d %H:%M")


def _deadline(text: str) -> date | None:
    found = re.search(r"\bby (\d{4}-\d{2}-\d{2})\b", text or "")
    if not found:
        return None
    try:
        return date.fromisoformat(found.group(1))
    except ValueError:
        return None


def deadline_text(deadline: date, now: datetime) -> str:
    left = (deadline - now.astimezone().date()).days
    if left > 0:
        return f"{deadline.isoformat()}（还剩 {left} 天）"
    if left == 0:
        return f"{deadline.isoformat()}（今天截止）"
    return f"{deadline.isoformat()}（已过 {-left} 天）"


def _links(text: str, wanted: tuple[tuple[str, str], ...]) -> list[tuple[str, str]]:
    """wanted = ((文字, 地址里要有的片段), ...)。只挑 AWS 自己域名下的。"""
    found: list[tuple[str, str]] = []
    urls = [url.rstrip(".,;") for url in _LINK.findall(text or "")]
    for label, needle in wanted:
        for url in urls:
            if needle in url and safe_link(url):
                found.append((label, url))
                break
    return found


_SEVERITY = {"urgent": "紧急", "critical": "严重", "high": "高", "normal": "一般", "low": "低"}
_RESTORED = re.compile(r"\b(?:has|have) been restored\b|\bwe have restored\b|\brestored your access\b", re.I)


def _abuse(mail: Mail, text: str, ids: list[str], now: datetime) -> Finding:
    action = _field(text, r"^\s*Action\s*:\s*(.+?)\s*$")
    effective = _field(text, r"^\s*Effective date\s*:\s*(.+?)\s*$")
    # HTML 邮件里的撇号可能是弯的（’）
    revoked = bool(re.search(r"\brevoke\b", action, re.I) or re.search(r"restrict your account.?s access", text, re.I))
    restored = not revoked and bool(_RESTORED.search(text))
    facts: list[Fact] = []
    if action:
        plain = "撤销 Anthropic 模型的调用权限" if re.search(r"revoke access to anthropic models", action, re.I) else action
        facts.append(Fact("处理动作", plain, "danger" if revoked else ""))
    if effective:
        facts.append(Fact("生效时间", _local_time(effective)))
    notes: list[tuple[str, str]] = []
    if revoked:
        title, tone, badge = "模型权限被撤销", "danger", "ACCESS REVOKED"
        notes.append(("warn", "不同意的话，按邮件里的申诉方式直接向模型提供方申诉；提供方通知 AWS 之后才会恢复"))
    elif restored:
        title, tone, badge = "模型权限已恢复", "ok", "ACCESS RESTORED"
    else:
        title, tone, badge = "收到 AWS 滥用报告", "danger", "ABUSE REPORT"
        notes.append(("warn", "按邮件要求排查并回复 AWS，逾期不处理可能被进一步限制"))
    return Finding(
        kind="abuse", title=title, tone=tone, badge=badge, subtitle="AWS · TRUST & SAFETY",
        account_ids=ids, facts=facts, excerpt=excerpt(text, mail.subject), notes=notes,
    )


def _compromised(mail: Mail, text: str, ids: list[str], now: datetime) -> Finding:
    keys = list(dict.fromkeys(_KEY.findall(text)))
    user = _field(text, r"belonging to (?:the )?(?:IAM )?user ([\w+=,.@-]+?)[,.]?(?:\s|$)")
    deadline = _deadline(text)
    limited = bool(re.search(r"limited your ability to use some AWS services", text, re.I))
    root = not keys and bool(re.search(r"root (?:account|user) password", text, re.I))
    facts: list[Fact] = []
    if keys:
        facts.append(Fact("涉及的密钥", "、".join(mask_key(key) for key in keys[:2])))
    if user:
        facts.append(Fact("IAM 用户", user))
    if root:
        facts.append(Fact("可能泄露", "root 登录密码", "danger"))
    if limited:
        facts.append(Fact("服务状态", "部分服务已受限", "danger", pill=True))
    if deadline:
        facts.append(Fact("回复截止", deadline_text(deadline, now), "danger"))
    notes: list[tuple[str, str]] = []
    if deadline:
        notes.append(("danger", f"{deadline:%m-%d} 前不回复 AWS 工单，账号可能被封停"))
    if keys:
        notes.append(("warn", "先换掉这把密钥：新建一把、把程序切过去、停用旧的，再回复 AWS 工单"))
    if root:
        notes.append(("warn", "改 root 密码、给 root 开 MFA、查 CloudTrail 有没有陌生的用户和密钥，再回复 AWS 工单"))
    return Finding(
        kind="compromised", title="账号疑似被盗用", tone="danger", badge="ACTION REQUIRED",
        subtitle="AWS · SECURITY NOTICE", account_ids=ids, facts=facts,
        excerpt=excerpt(text, mail.subject), notes=notes,
        links=_links(text, (("去 AWS Support Center 回复", "console.aws.amazon.com/support"),)),
    )


def _suspended(mail: Mail, text: str, ids: list[str], now: datetime) -> Finding:
    subject = mail.subject.lower()
    if "suspen" in subject:
        title = "账号暂停通知"
    elif "terminat" in subject:
        title = "资源终止通知"
    else:
        title = "账号关闭通知"
    facts: list[Fact] = []
    deadline = _deadline(text)
    if deadline:
        facts.append(Fact("处理截止", deadline_text(deadline, now), "danger"))
    return Finding(
        kind="suspended", title=title, tone="danger", badge="ACCOUNT STATUS",
        subtitle="AWS · ACCOUNT NOTICE", account_ids=ids, facts=facts,
        excerpt=excerpt(text, mail.subject),
        notes=[("danger", "尽快登录 AWS 控制台查看原因，按邮件要求处理并回复 AWS")],
        links=_links(text, (("去 AWS Support Center", "console.aws.amazon.com/support"),)),
    )


def _case(mail: Mail, text: str, ids: list[str], now: datetime) -> Finding:
    subject = mail.subject
    case_id = (
        _field(subject, r"\[CASE\s+(\d+)\]")
        or _field(subject, r"New Support case:?\s*(\d+)")
        or _field(text, r"^\s*Case ID\s*:\s*(\d+)")
    )
    severity = _field(text, r"^\s*Severity\s*:\s*([A-Za-z-]+)")
    new = bool(re.search(r"\bNew Support case\b", subject, re.I))
    facts: list[Fact] = []
    if case_id:
        facts.append(Fact("工单号", case_id))
    if severity:
        urgent = severity.lower() in ("urgent", "critical")
        facts.append(Fact("紧急程度", _SEVERITY.get(severity.lower(), severity), "danger" if urgent else ""))
    return Finding(
        kind="case", title="AWS 开了工单" if new else "AWS 工单有新回复", tone="warn",
        badge="SUPPORT CASE", subtitle="AWS · SUPPORT CASE", account_ids=ids, facts=facts,
        excerpt=excerpt(text, mail.subject),
        notes=[("warn", "在 AWS 控制台的 Support Center 里回复这个工单，邮件地址不收回信")],
        links=_links(text, (("打开 AWS 工单", "console.aws.amazon.com/support"),)),
    )


def _root(mail: Mail, text: str, ids: list[str], now: datetime) -> Finding:
    subject = mail.subject
    base = dict(kind="root", tone="warn", badge="ROOT USER", subtitle="AWS · ROOT USER SECURITY",
                account_ids=ids, to_fixed=True)
    if _ROOT_SIGNIN.search(subject):
        return Finding(**base, title="root 账号登录要验证身份", notes=[
            ("warn", "有人在登录这个账号的 root 用户，AWS 把验证码发到了邮箱。不是你们自己在登录的话，马上修改 root 密码"),
            ("muted", "验证码只在邮箱里看，不会发到群里"),
        ])
    if _ROOT_PASSWORD.search(subject):
        return Finding(**base, title="root 账号在重置密码", notes=[
            ("warn", "有人申请重置 root 密码。不是你们自己的话，马上检查邮箱和 root 账号的安全设置"),
            ("muted", "重置链接只在邮箱里看，不会发到群里"),
        ])
    return Finding(**base, title="root 账号的 MFA 被停用", excerpt=excerpt(text, subject), notes=[
        ("warn", "不是你们自己停用的话，马上登录控制台重新绑定 MFA，并修改 root 密码"),
    ])


_INSPECT = {
    "abuse": _abuse,
    "compromised": _compromised,
    "suspended": _suspended,
    "case": _case,
    "root": _root,
}


def inspect(mail: Mail, kind: str, now: datetime | None = None) -> Finding:
    """认出类别之后，按类别取字段、挑节选。mail.body 要已经填好。"""
    text = _normalize(mail.body)
    # 登录验证、重置密码的正文里有验证码 / 重置链接：连账号 ID 都只从主题里找
    secret = kind == "root" and bool(_ROOT_SIGNIN.search(mail.subject) or _ROOT_PASSWORD.search(mail.subject))
    ids = account_ids(mail.subject, "" if secret else text)
    return _INSPECT[kind](mail, "" if secret else text, ids, now or datetime.now(timezone.utc))
