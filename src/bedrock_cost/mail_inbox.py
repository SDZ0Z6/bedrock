"""收信：用 IMAP 只读地取账号邮箱里的新邮件。

每个账号的邮箱（平台、地址、密码）在台账里，从账号管理页填。这里只管「连上去、取邮件」，
认邮件、发告警在 mail_rules / mail_alerts。

几条硬规矩：
  · **只读**。收件箱用 EXAMINE 打开（select(readonly=True)），取信一律 BODY.PEEK——
    不会把邮件标成已读，也不会动任何标记。邮箱是人也在用的，后台程序不能替人「看过」。
  · **只走加密连接**。993 端口直接 SSL；143 端口必须 STARTTLS 成功，否则不登录——
    不能用明文把邮箱密码发出去。
  · **密码不出这个模块的错误信息**。服务器的报错有时会把登录参数原样带回来，往外抛之前
    先擦一遍（_redact）。
  · 大多数平台的客户端登录**不接受网页登录密码**：阿里邮箱要三方客户端安全密码，QQ / 163
    要授权码，Gmail 要应用专用密码。PROVIDERS 里记着每家要什么，登录失败时照着提示。

一次会话的用法：

    with Session(box) as session:
        session.uidvalidity, session.uidnext      # 收件箱的编号体系
        uids = session.new_uids(after=last_uid)   # 比 last_uid 新的邮件
        heads = session.headers(uids)             # 只取信头，便宜
        message = session.message(uid)            # 需要时再取整封
"""

from __future__ import annotations

import email
import imaplib
import re
import socket
import ssl
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from email import policy
from email.message import EmailMessage

from . import config

# 在任何人替换 imaplib 之前先把异常类拿住（测试会整个换掉 _open）
IMAPError = imaplib.IMAP4.error

INBOX = "INBOX"
# 取信头时要的几个字段。认邮件只看发件人和主题；Authentication-Results 用来挡冒充的
HEADER_FIELDS = "FROM TO SUBJECT DATE MESSAGE-ID AUTHENTICATION-RESULTS"
# 一次 FETCH 最多带多少个编号，免得命令行太长被服务器拒
FETCH_CHUNK = 100
# 超过这个大小的邮件不取正文（AWS 的通知都是几十 KB，几 MB 的只可能是带附件的别的邮件）
MAX_BODY_BYTES = 5 * 1024 * 1024


# ---------------------------------------------------------------- 平台
@dataclass(frozen=True)
class Provider:
    key: str
    label: str
    host: str = ""        # 空 = 自定义，服务器地址由用户填
    port: int = 993
    password_hint: str = ""   # 这家的客户端登录要填什么密码


# 页面下拉框的顺序就是这里的顺序。服务器地址来自各家的帮助文档：
#   阿里邮箱国际站 https://www.alibabacloud.com/help/en/alibaba-mail/latest/alibaba-mail-imap-pop-smtp-address-and-port-information
#   阿里邮箱中国站 https://help.aliyun.com/zh/document_detail/36576.html
# Outlook / Microsoft 365 没放：它们已经关掉了密码登录 IMAP，只能走 OAuth。
PROVIDERS: tuple[Provider, ...] = (
    Provider("aliyun-sg", "阿里邮箱 · 国际站（新加坡）", "imap.sg.aliyun.com", password_hint="三方客户端安全密码"),
    Provider("aliyun-hk", "阿里邮箱 · 国际站（香港）", "imap.hk.aliyun.com", password_hint="三方客户端安全密码"),
    Provider("aliyun-de", "阿里邮箱 · 国际站（德国）", "imap.de.alibabacloud.com", password_hint="三方客户端安全密码"),
    Provider("aliyun-us", "阿里邮箱 · 国际站（美国）", "imap.us.alibabacloud.com", password_hint="三方客户端安全密码"),
    Provider("aliyun-cn", "阿里邮箱 · 中国站", "imap.qiye.aliyun.com", password_hint="三方客户端安全密码"),
    Provider("exmail", "腾讯企业邮", "imap.exmail.qq.com", password_hint="客户端专用密码（开了安全登录时）"),
    Provider("qq", "QQ 邮箱", "imap.qq.com", password_hint="授权码"),
    Provider("163", "网易 163 邮箱", "imap.163.com", password_hint="授权码"),
    Provider("126", "网易 126 邮箱", "imap.126.com", password_hint="授权码"),
    Provider("netease-qiye", "网易企业邮", "imap.qiye.163.com", password_hint="客户端授权密码"),
    Provider("gmail", "Gmail", "imap.gmail.com", password_hint="应用专用密码"),
    Provider("custom", "其他平台（自己填 IMAP 服务器）"),
)
BY_KEY = {provider.key: provider for provider in PROVIDERS}
CUSTOM = "custom"
DEFAULT_PROVIDER = "aliyun-sg"

_HOST = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,62}[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,62}[A-Za-z0-9])?)+$")


def parse_server(text: str) -> tuple[str, int] | None:
    """自定义平台填的 IMAP 服务器：「imap.example.com」或「imap.example.com:993」。

    端口不写默认 993。连的时候 143 走 STARTTLS，其他端口一律按 SSL 连——不支持明文。
    认不出返回 None。
    """
    text = (text or "").strip()
    if not text:
        return None
    host, sep, port_text = text.rpartition(":")
    if not sep:
        host, port_text = text, "993"
    if not _HOST.match(host) or not port_text.isdigit():
        return None
    port = int(port_text)
    if not 0 < port < 65536:
        return None
    return host.lower(), port


def server_for(provider: str, custom: str = "") -> tuple[str, int] | None:
    """平台 -> (服务器, 端口)。自定义平台看用户填的；不认识的平台返回 None。"""
    if provider == CUSTOM:
        return parse_server(custom)
    known = BY_KEY.get(provider)
    if known is None or not known.host:
        return None
    return known.host, known.port


def provider_label(provider: str) -> str:
    known = BY_KEY.get(provider)
    return known.label if known else (provider or "未选平台")


@dataclass(frozen=True)
class Mailbox:
    """要登录的一个邮箱。密码不进 repr，也不参与比较。"""

    host: str
    port: int
    address: str
    password: str = field(default="", repr=False, compare=False)
    provider: str = ""

    @property
    def key(self) -> str:
        """状态文件里的键：同一个邮箱不管挂在几个账号下面都只收一次。"""
        return f"{self.address.strip().lower()}|{self.host}:{self.port}"

    @property
    def password_hint(self) -> str:
        known = BY_KEY.get(self.provider)
        return known.password_hint if known else ""


def mailbox_for(provider: str, address: str, password: str, custom: str = "") -> Mailbox | None:
    """台账 / 表单里的几格 -> Mailbox。缺地址、密码或服务器认不出时返回 None。"""
    server = server_for(provider, custom)
    if not (address and password and server):
        return None
    host, port = server
    return Mailbox(host=host, port=port, address=address.strip(), password=password, provider=provider)


# ---------------------------------------------------------------- 错误
class MailError(RuntimeError):
    """收信失败。消息已经是给人看的中文，密码已擦掉。"""


def _redact(text: str, box: Mailbox) -> str:
    if box.password and len(box.password) >= 4:
        text = text.replace(box.password, "***")
    return text


def _server_says(exc: BaseException) -> str:
    """imaplib 把服务器的原话包成 "b'...'"，剥掉这层壳。"""
    text = str(exc).strip()
    found = re.fullmatch(r"b(['\"])(.*)\1", text, re.S)
    return found.group(2) if found else text


def _explain(exc: BaseException, box: Mailbox, stage: str) -> str:
    where = f"{box.host}:{box.port}"
    if isinstance(exc, socket.gaierror):
        return f"找不到邮箱服务器 {box.host}（域名解析失败），检查平台或服务器地址"
    if isinstance(exc, (socket.timeout, TimeoutError)):
        return f"连接 {where} 超时：服务器没响应，或者这台机器出不去 {box.port} 端口"
    if isinstance(exc, ConnectionRefusedError):
        return f"{where} 拒绝连接：端口不对，或者这个服务器不提供 IMAP"
    if isinstance(exc, ssl.SSLError):
        return f"和 {where} 建不起加密连接（{exc.reason or exc}）"
    if isinstance(exc, IMAPError):
        said = _server_says(exc)
        if stage == "login":
            need = f"密码要填{box.password_hint}，网页登录密码一般登不上；" if box.password_hint else ""
            return (
                f"登录被拒：检查邮箱地址和密码。{need}"
                f"也确认邮箱后台已经开了 IMAP / 三方客户端登录（服务器说：{said}）"
            )
        if stage == "select":
            return f"打不开收件箱（服务器说：{said}）"
        return f"邮箱服务器报错（服务器说：{said}）"
    if isinstance(exc, UnicodeEncodeError):
        return "邮箱地址或密码里有 IMAP 不支持的字符（只能是英文、数字和常见符号）"
    if isinstance(exc, OSError):
        return f"连不上 {where}：{exc.strerror or exc}"
    return f"{type(exc).__name__}: {exc}"


# ---------------------------------------------------------------- 连接
def _open(host: str, port: int, timeout: float) -> imaplib.IMAP4:
    """建一条加密连接。143 走 STARTTLS，其余端口直接 SSL。测试里整个替换掉这个函数。"""
    context = ssl.create_default_context()
    if port == 143:
        conn = imaplib.IMAP4(host, port, timeout=timeout)
        try:
            conn.starttls(ssl_context=context)
        except Exception:
            try:
                conn.shutdown()
            except OSError:
                pass
            raise
        return conn
    return imaplib.IMAP4_SSL(host, port, ssl_context=context, timeout=timeout)


Connector = Callable[[str, int, float], imaplib.IMAP4]

_UID = re.compile(rb"\bUID (\d+)")
_SIZE = re.compile(rb"\bRFC822\.SIZE (\d+)")
_STATUS = {name: re.compile(rb"\b" + name.encode() + rb" (\d+)") for name in ("MESSAGES", "UIDNEXT", "UIDVALIDITY")}


def _check(typ: str, data, what: str) -> None:
    if typ != "OK":
        detail = b" ".join(d for d in data if isinstance(d, bytes)).decode("utf-8", "replace")
        raise IMAPError(f"{what} {typ} {detail}".strip())


def _fetch_items(data: Iterable) -> list[tuple[int, bytes, int]]:
    """FETCH 的结果 -> [(UID, 内容, 大小)]。

    imaplib 把每封邮件拆成 (描述, 内容) 元组，后面跟一个 b')'。UID 在描述里——但也有
    服务器把 UID 排在内容后面，那它就在紧跟着的那个 bytes 里。两处都找。
    """
    items = list(data or [])
    out: list[tuple[int, bytes, int]] = []
    for index, item in enumerate(items):
        if not (isinstance(item, tuple) and len(item) == 2):
            continue
        meta, payload = item
        tail = items[index + 1] if index + 1 < len(items) and isinstance(items[index + 1], bytes) else b""
        uid = _UID.search(meta) or _UID.search(tail)
        if not uid:
            continue
        size = _SIZE.search(meta) or _SIZE.search(tail)
        out.append((int(uid.group(1)), payload or b"", int(size.group(1)) if size else len(payload or b"")))
    return out


class Session:
    """一次 IMAP 会话：连上、登录、只读地打开收件箱。with 用完自动退出登录。"""

    def __init__(self, box: Mailbox, *, timeout: float | None = None, connect: Connector | None = None):
        self.box = box
        self.timeout = float(timeout or config.MAIL_TIMEOUT)
        self.connect = connect or _open
        self.conn: imaplib.IMAP4 | None = None
        self.messages = 0
        self.uidvalidity = 0
        self.uidnext: int | None = None

    # ------------------------------------------------------------ 进出
    def __enter__(self) -> Session:
        stage = "connect"
        try:
            self.conn = self.connect(self.box.host, self.box.port, self.timeout)
            stage = "login"
            self.conn.login(self.box.address, self.box.password)
            stage = "id"
            self._introduce()
            stage = "select"
            self._open_inbox()
        except MailError:
            self._close()
            raise
        except Exception as exc:
            self._close()
            raise MailError(_redact(_explain(exc, self.box, stage), self.box)) from None
        return self

    def __exit__(self, *exc_info) -> None:
        self._close()

    def _close(self) -> None:
        if self.conn is None:
            return
        try:
            self.conn.logout()
        except Exception:
            pass  # 退出登录失败无所谓，连接反正要断
        self.conn = None

    def _introduce(self) -> None:
        """RFC 2971 的 ID 命令。网易邮箱不先报上客户端名字，打开收件箱会报 Unsafe Login。

        服务器没声明支持就不发；发了失败也不要紧。
        """
        capabilities = getattr(self.conn, "capabilities", ()) or ()
        if "ID" not in capabilities:
            return
        try:
            self.conn.xatom("ID", '("name" "bedrock-cost" "version" "1.0")')
        except Exception:
            pass

    def _open_inbox(self) -> None:
        # 先 STATUS 再 EXAMINE：有的服务器不许对已经打开的邮箱发 STATUS
        try:
            typ, data = self.conn.status(INBOX, "(MESSAGES UIDNEXT UIDVALIDITY)")
        except IMAPError:
            typ, data = "NO", []
        if typ == "OK" and data and isinstance(data[0], bytes):
            status = data[0]
            values = {name: pattern.search(status) for name, pattern in _STATUS.items()}
            if values["UIDVALIDITY"]:
                self.uidvalidity = int(values["UIDVALIDITY"].group(1))
            if values["UIDNEXT"]:
                self.uidnext = int(values["UIDNEXT"].group(1))
            if values["MESSAGES"]:
                self.messages = int(values["MESSAGES"].group(1))

        typ, data = self.conn.select(INBOX, readonly=True)   # EXAMINE：只读
        _check(typ, data, "EXAMINE")
        if data and isinstance(data[0], bytes) and data[0].strip().isdigit():
            self.messages = int(data[0])
        if not self.uidvalidity:
            _, value = self.conn.response("UIDVALIDITY")
            if value and value[0]:
                self.uidvalidity = int(value[0])
        if self.uidnext is None:
            _, value = self.conn.response("UIDNEXT")
            if value and value[0]:
                self.uidnext = int(value[0])

    # ------------------------------------------------------------ 取信
    def _run(self, what: str, call):
        try:
            return call()
        except MailError:
            raise
        except Exception as exc:
            raise MailError(_redact(_explain(exc, self.box, what), self.box)) from None

    def all_uids(self) -> list[int]:
        def call():
            typ, data = self.conn.uid("SEARCH", None, "ALL")
            _check(typ, data, "SEARCH")
            return sorted(int(n) for n in b" ".join(d for d in data if d).split())

        return self._run("search", call)

    def baseline(self) -> int:
        """「从这之后算新邮件」的那个编号：收件箱里现在最大的 UID。"""
        if self.uidnext:
            return self.uidnext - 1
        uids = self.all_uids()
        return uids[-1] if uids else 0

    def new_uids(self, after: int) -> list[int]:
        """比 after 新的邮件，从旧到新。

        注意 "UID n:*" 在没有新邮件时也会返回最后那一封（* 就是最大的 UID），所以要再滤一遍。
        """

        def call():
            typ, data = self.conn.uid("SEARCH", None, f"UID {after + 1}:*")
            _check(typ, data, "SEARCH")
            return sorted(n for n in (int(x) for x in b" ".join(d for d in data if d).split()) if n > after)

        return self._run("search", call)

    def headers(self, uids: list[int]) -> dict[int, tuple[EmailMessage, int]]:
        """只取信头（BODY.PEEK，不标已读）。返回 {UID: (信头, 整封大小)}。"""
        found: dict[int, tuple[EmailMessage, int]] = {}
        for start in range(0, len(uids), FETCH_CHUNK):
            chunk = ",".join(str(uid) for uid in uids[start:start + FETCH_CHUNK])

            def call(chunk=chunk):
                typ, data = self.conn.uid(
                    "FETCH", chunk, f"(UID RFC822.SIZE BODY.PEEK[HEADER.FIELDS ({HEADER_FIELDS})])"
                )
                _check(typ, data, "FETCH")
                return _fetch_items(data)

            for uid, payload, size in self._run("fetch", call):
                found[uid] = (email.message_from_bytes(payload, policy=policy.default), size)
        return found

    def message(self, uid: int) -> EmailMessage | None:
        """取整封（BODY.PEEK[]，不标已读）。邮件已经被删了返回 None。"""

        def call():
            typ, data = self.conn.uid("FETCH", str(uid), "(UID BODY.PEEK[])")
            _check(typ, data, "FETCH")
            return _fetch_items(data)

        for found_uid, payload, _ in self._run("fetch", call):
            if found_uid == uid:
                return email.message_from_bytes(payload, policy=policy.default)
        return None

    def recent(self, count: int) -> list[int]:
        """收件箱里最新的 count 封（从旧到新）。"""
        uids = self.all_uids()
        return uids[-count:] if count > 0 else []


def probe(box: Mailbox, classify=None, *, look_back: int = 50, connect: Connector | None = None) -> str:
    """账号管理页的「测试连接」和命令行 mail test：登录、只读打开收件箱，说一句结果。

    给了 classify（信头 -> 类别或 None）就顺手看一眼最近 look_back 封里有几封会发告警，
    让人知道规则在这个邮箱上认不认得出东西。失败抛 MailError。
    """
    with Session(box, connect=connect) as session:
        line = f"连上了，收件箱里有 {session.messages} 封邮件"
        if classify is None or not session.messages:
            return line + "（只读，没有改动任何邮件）。"
        uids = session.recent(look_back)
        heads = session.headers(uids)
        matched = sum(1 for uid in uids if uid in heads and classify(heads[uid][0]))
        return (
            f"{line}。最近 {len(uids)} 封里有 {matched} 封符合告警规则——旧邮件不会补发，"
            "打开邮件告警之后新到的才发（只读，没有改动任何邮件）。"
        )
