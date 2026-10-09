"""邮件告警：认邮件（mail_rules）、收信（mail_inbox）、收一轮发一轮（mail_alerts）。

不连任何真的邮箱服务器：FakeIMAP 扮演收件箱，并且对「只读」较真——取信没用 BODY.PEEK、
打开收件箱没用只读模式，都直接判失败。样例邮件见 mail_samples（全是假账号、假密钥）。
"""

from __future__ import annotations

import imaplib
import json
from datetime import datetime, timedelta, timezone
from email import policy

import pytest

from bedrock_cost import alerts, cards, config, events, excel_source, mail_alerts, mail_inbox, mail_rules, telegram
from bedrock_cost.mail_inbox import Mailbox, MailError, Session
from bedrock_cost.telegram import TelegramError

from . import mail_samples as samples
from .conftest import LEDGER_HEADER, LEDGER_ROWS, write_ledger

NOW = datetime(2026, 9, 30, 3, 0, tzinfo=timezone.utc)
ALPHA, BETA = "111111111111", "222222222222"
ALPHA_CHAT, BETA_CHAT, FIXED_CHAT = "-1001111111111", "-1002222222222", "-1009999999999"
PASSWORD = "S3cret-Client-Pass"


def raw(message) -> bytes:
    return message.as_bytes(policy=policy.SMTP)   # 真实邮件是 CRLF 换行


def read(make) -> mail_rules.Mail:
    return mail_rules.read_message(make(), 1)


def found(make) -> mail_rules.Finding:
    mail = read(make)
    return mail_rules.inspect(mail, mail_rules.classify(mail), NOW)


# ================================================================== 假的 IMAP 服务器
class FakeIMAP:
    """够 Session 用的收件箱：{UID: 原始邮件}。每条命令都记下来，好断言「只读」。"""

    def __init__(self, messages=None, *, uidvalidity=7, password=PASSWORD, capabilities=("IMAP4REV1", "ID")):
        self.messages: dict[int, bytes] = dict(messages or {})
        self.uidvalidity = uidvalidity
        self.password = password
        self.capabilities = capabilities
        self.commands: list[tuple] = []
        self.connections = 0
        self.logged_out = 0

    def add(self, uid: int, message) -> None:
        self.messages[uid] = raw(message)

    # 当 connect= 传进 Session：返回自己当连接
    def __call__(self, host, port, timeout):
        self.connections += 1
        self.host, self.port = host, port
        return self

    def login(self, user, password):
        self.commands.append(("LOGIN", user))
        if password != self.password:
            raise imaplib.IMAP4.error(b"[AUTHENTICATIONFAILED] Login fail. password is incorrect, " + password.encode())
        return "OK", [b"LOGIN completed"]

    def xatom(self, name, *args):
        self.commands.append((name, *args))
        return "OK", [b"ID completed"]

    def status(self, mailbox, names):
        self.commands.append(("STATUS", mailbox))
        nxt = max(self.messages, default=0) + 1
        return "OK", [f'"INBOX" (MESSAGES {len(self.messages)} UIDNEXT {nxt} UIDVALIDITY {self.uidvalidity})'.encode()]

    def select(self, mailbox, readonly=False):
        self.commands.append(("SELECT", mailbox, readonly))
        assert readonly, "收件箱必须只读地打开（EXAMINE）"
        return "OK", [str(len(self.messages)).encode()]

    def response(self, code):
        return code, [None]

    def uid(self, command, *args):
        self.commands.append(("UID", command, *args))
        if command == "SEARCH":
            criteria = args[-1]
            uids = sorted(self.messages)
            if criteria.startswith("UID "):
                low = int(criteria.split()[1].split(":")[0])
                # 真服务器的怪脾气：「n:*」没有新邮件时也会回最后那一封
                picked = [u for u in uids if u >= low] or uids[-1:]
            else:
                picked = uids
            return "OK", [" ".join(str(u) for u in picked).encode()]
        if command == "FETCH":
            wanted, spec = args
            assert "BODY.PEEK" in spec, "取信必须用 BODY.PEEK，不能把邮件标成已读"
            data: list = []
            for number, uid in enumerate((int(u) for u in wanted.split(",")), start=1):
                message = self.messages.get(uid)
                if message is None:
                    continue
                if "HEADER.FIELDS" in spec:
                    payload = message.split(b"\r\n\r\n", 1)[0] + b"\r\n\r\n"
                    meta = f"{number} (UID {uid} RFC822.SIZE {len(message)} BODY[HEADER.FIELDS] {{{len(payload)}}}"
                else:
                    payload = message
                    meta = f"{number} (UID {uid} BODY[] {{{len(payload)}}}"
                data += [(meta.encode(), payload), b")"]
            return "OK", data
        raise AssertionError(f"没想到会用 UID {command}")

    def logout(self):
        self.logged_out += 1
        return "BYE", [b"bye"]

    @property
    def fetched(self) -> list[str]:
        """每次 FETCH 要的是什么（("UID", "FETCH", 编号, 要的东西) 的最后一格）。"""
        return [c[3] for c in self.commands if c[:2] == ("UID", "FETCH")]


BOX = Mailbox(host="imap.sg.aliyun.com", port=993, address="root-alpha@example.com", password=PASSWORD, provider="aliyun-sg")


# ================================================================== 认邮件
class TestClassify:
    @pytest.mark.parametrize("make, kind", [
        (samples.abuse, "abuse"),
        (samples.suspicious, "compromised"),
        (samples.health, "compromised"),
        (samples.root_review, "compromised"),
        (samples.case_new, "case"),
        (samples.case_reply, "case"),
        (samples.mfa, "root"),
        (samples.verify, "root"),
        (samples.suspended, "suspended"),
    ])
    def test_the_five_kinds(self, make, kind):
        assert mail_rules.classify(read(make)) == kind

    @pytest.mark.parametrize("make", samples.SILENT)
    def test_everything_else_is_silent(self, make):
        """Marketplace 的订阅和 offer、其他 AWS 邮件、非 AWS 邮件、冒充的，都不发。"""
        assert mail_rules.classify(read(make)) is None

    def test_a_case_reply_is_a_case_even_if_it_sounds_like_a_breach(self):
        """「RE:[CASE …] Suspicious activity…」是工单的跟进，照草案算工单（橙色）。"""
        assert found(samples.case_reply).tone == "warn"

    def test_only_headers_are_needed(self):
        """认类别只看信头：收一轮信时正文只给认出来的那几封取。"""
        message = samples.abuse()
        headers_only = mail_rules.read_headers(message)
        assert headers_only.body == ""
        assert mail_rules.classify(headers_only) == "abuse"

    @pytest.mark.parametrize("sender, expected", [
        ("no-reply@amazonaws.com", True),
        ("health@aws.com", True),
        ("no-reply@signin.aws", True),
        ("trustandsafety@support.aws.com", True),
        ("no-reply-aws@amazon.com", True),
        ("someone@notamazon.com", False),
        ("aws@example.com", False),
        ("", False),
    ])
    def test_aws_senders(self, sender, expected):
        assert mail_rules.is_aws(sender) is expected


class TestFindings:
    def test_abuse_names_the_revoked_access(self):
        finding = found(samples.abuse)
        assert (finding.title, finding.tone) == ("模型权限被撤销", "danger")
        facts = {fact.label: fact.value for fact in finding.facts}
        assert facts["处理动作"] == "撤销 Anthropic 模型的调用权限"
        assert facts["生效时间"] == datetime(2026, 9, 27, 6, 16, tzinfo=timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M")
        assert finding.account_ids == [samples.ACCOUNT]

    def test_abuse_links_stay_out(self):
        """申诉表单是第三方域名：不贴出去，只说「按邮件里的申诉方式」。"""
        finding = found(samples.abuse)
        assert finding.links == []
        assert "form.example.com" not in finding.excerpt
        assert "申诉" in finding.notes[0][1]

    def test_compromised_key_is_masked_everywhere(self):
        finding = found(samples.suspicious)
        facts = {fact.label: fact.value for fact in finding.facts}
        assert facts["涉及的密钥"] == "AKIAFAKE…1234"
        assert facts["IAM 用户"] == "ReportReader"
        assert samples.KEY not in finding.excerpt and "AKIAFAKE…1234" in finding.excerpt

    def test_compromised_deadline_counts_down(self):
        facts = {fact.label: fact for fact in found(samples.suspicious).facts}
        assert facts["回复截止"].value == "2026-10-02（还剩 2 天）"
        assert facts["回复截止"].tone == "danger"
        assert facts["服务状态"].pill is True

    def test_deadline_wording(self):
        from datetime import date

        day = date(2026, 10, 2)
        assert mail_rules.deadline_text(day, datetime(2026, 10, 2, 4, tzinfo=timezone.utc)).endswith("（今天截止）")
        assert mail_rules.deadline_text(day, datetime(2026, 10, 5, 4, tzinfo=timezone.utc)).endswith("（已过 3 天）")

    def test_root_password_review_without_a_key(self):
        facts = {fact.label: fact.value for fact in found(samples.root_review).facts}
        assert facts["可能泄露"] == "root 登录密码"
        assert "涉及的密钥" not in facts

    def test_case_fields_and_link(self):
        finding = found(samples.case_new)
        facts = {fact.label: fact for fact in finding.facts}
        assert facts["工单号"].value == samples.CASE
        assert (facts["紧急程度"].value, facts["紧急程度"].tone) == ("紧急", "danger")
        assert finding.title == "AWS 开了工单"
        (label, url), = finding.links
        assert url.startswith("https://console.aws.amazon.com/support/home#/case/")

    def test_case_reply_title(self):
        assert found(samples.case_reply).title == "AWS 工单有新回复"

    def test_root_kinds_go_to_the_fixed_group(self):
        for make in (samples.mfa, samples.verify):
            assert found(make).to_fixed is True
        assert found(samples.abuse).to_fixed is False

    def test_verification_codes_never_leave_the_mailbox(self):
        """登录验证邮件里有验证码：正文一个字都不取，连账号 ID 都只看主题。"""
        finding = found(samples.verify)
        assert finding.excerpt == ""
        assert "482913" not in str(finding)

    def test_suspended(self):
        finding = found(samples.suspended)
        assert (finding.title, finding.tone) == ("账号暂停通知", "danger")
        assert {fact.label for fact in finding.facts} == {"处理截止"}


class TestExcerpt:
    def test_keeps_what_happened_and_the_deadline(self):
        text = found(samples.suspicious).excerpt
        assert "limited your ability to use some AWS services" in text
        assert "contact AWS by 2026-10-02" in text

    def test_drops_greetings_steps_links_and_footers(self):
        text = found(samples.suspicious).excerpt
        for noise in ("Dear AWS Customer", "Step 1", "CloudTrail", "https://", "Amazon.com, Inc.", "====="):
            assert noise not in text
        # 客套话按句删：「请查看这封通知」「作为安全最佳实践……」
        assert "Please review the following notice" not in text
        assert "security best practice" not in text

    def test_drops_the_lines_already_shown_as_fields(self):
        text = found(samples.abuse).excerpt
        assert "Action:" not in text and "Effective date:" not in text
        assert "[1]" not in text and "[2]" not in text        # 脚注编号也去掉
        assert "As a result, in accordance" not in text

    def test_health_buttons_and_the_repeated_subject_are_gone(self):
        text = found(samples.health).excerpt
        for noise in ("AWS Health Event", "View in Notification Center", "View details in service console",
                      "[Action Required] Suspicious activity"):
            assert noise not in text
        assert text.startswith("Your AWS Account may have been accessed")

    def test_case_reply_stops_before_the_machine_translation(self):
        text = found(samples.case_reply).excerpt
        assert "still be at risk" in text
        assert "Machine translated" not in text and "If you have any questions" not in text

    def test_masks_email_addresses(self):
        text = mail_rules.excerpt("Your account root user alice@example.com tried to sign in.")
        assert "alice@example.com" not in text and "a***@example.com" in text

    def test_long_text_is_clipped_at_a_sentence(self):
        text = mail_rules.excerpt(" ".join(f"Sentence number {n} is here." for n in range(200)), limit=120)
        assert len(text) <= 121 and text.endswith(".…")

    def test_html_only_mail(self):
        message = samples.message(
            "no-reply@amazonaws.com", "Your AWS account has been suspended", "",
        )
        message.set_content(
            "<html><head><style>p{}</style></head><body><p>Dear AWS Customer,</p>"
            "<p>Your account&nbsp;has been <b>suspended</b>.</p><script>x()</script></body></html>",
            subtype="html",
        )
        mail = mail_rules.read_message(message)
        assert mail_rules.inspect(mail, "suspended", NOW).excerpt == "Your account has been suspended."

    def test_curly_apostrophes_in_html_mail(self):
        mail = read(samples.abuse)
        mail.body = mail.body.replace("Action: Revoke access to Anthropic models on Bedrock\n", "").replace("'", "’")
        assert mail_rules.inspect(mail, "abuse", NOW).title == "模型权限被撤销"


class TestLinks:
    @pytest.mark.parametrize("url, ok", [
        ("https://console.aws.amazon.com/support/home", True),
        ("https://docs.aws.amazon.com/bedrock/", True),
        ("https://repost.aws/knowledge-center/", True),
        ("http://console.aws.amazon.com/support/home", False),     # 不是 https
        ("https://console.aws.amazon.com.evil.example/", False),
        ("https://aws.amazon.com@evil.example/", False),
        ("https://form.typeform.com/to/x", False),
    ])
    def test_only_aws_links(self, url, ok):
        assert mail_rules.safe_link(url) is ok


# ================================================================== 收信
class TestServers:
    @pytest.mark.parametrize("text, expected", [
        ("imap.example.com", ("imap.example.com", 993)),
        ("IMAP.Example.com:143", ("imap.example.com", 143)),
        ("mail.example.co.uk:10993", ("mail.example.co.uk", 10993)),
        ("", None),
        ("localhost", None),
        ("imap.example.com:0", None),
        ("imap.example.com:abc", None),
        ("imap example.com", None),
    ])
    def test_parse_server(self, text, expected):
        assert mail_inbox.parse_server(text) == expected

    def test_presets_and_custom(self):
        assert mail_inbox.server_for("aliyun-sg") == ("imap.sg.aliyun.com", 993)
        assert mail_inbox.server_for("aliyun-cn") == ("imap.qiye.aliyun.com", 993)
        assert mail_inbox.server_for("custom", "imap.example.com") == ("imap.example.com", 993)
        assert mail_inbox.server_for("custom", "") is None
        assert mail_inbox.server_for("outlook") is None

    def test_every_preset_has_a_host_and_a_password_hint(self):
        for provider in mail_inbox.PROVIDERS:
            if provider.key != mail_inbox.CUSTOM:
                assert provider.host and provider.password_hint, provider.key

    def test_password_stays_out_of_repr(self):
        assert PASSWORD not in repr(BOX)


class TestSession:
    def test_opens_the_inbox_read_only_and_logs_out(self):
        server = FakeIMAP()
        server.add(1, samples.abuse())
        with Session(BOX, connect=server) as session:
            assert (session.uidvalidity, session.messages, session.baseline()) == (7, 1, 1)
        assert ("SELECT", "INBOX", True) in server.commands
        assert server.logged_out == 1

    def test_introduces_itself_when_the_server_asks_for_it(self):
        """网易邮箱不先报 ID，打开收件箱会报 Unsafe Login。"""
        server = FakeIMAP()
        with Session(BOX, connect=server):
            pass
        assert any(c[0] == "ID" for c in server.commands)
        quiet = FakeIMAP(capabilities=("IMAP4REV1",))
        with Session(BOX, connect=quiet):
            pass
        assert not any(c[0] == "ID" for c in quiet.commands)

    def test_new_uids_ignores_the_last_one_echoed_back(self):
        server = FakeIMAP()
        server.add(5, samples.abuse())
        with Session(BOX, connect=server) as session:
            assert session.new_uids(5) == []
            assert session.new_uids(4) == [5]

    def test_every_fetch_peeks(self):
        server = FakeIMAP()
        server.add(1, samples.abuse())
        with Session(BOX, connect=server) as session:
            heads = session.headers([1])
            assert heads[1][0]["Subject"].startswith("RE: Your AWS Abuse Report")
            assert session.message(1)["From"].addresses[0].addr_spec == "trustandsafety@support.aws.com"
        assert server.fetched and all("BODY.PEEK" in spec for spec in server.fetched)

    def test_uid_after_the_literal(self):
        """有的服务器把 UID 排在内容后面。"""
        data = [(b"1 (RFC822.SIZE 10 BODY[HEADER.FIELDS (SUBJECT)] {9}", b"Subject: x"), b" UID 42)"]
        assert mail_inbox._fetch_items(data) == [(42, b"Subject: x", 10)]

    def test_a_wrong_password_is_explained_without_echoing_it(self):
        server = FakeIMAP(password="another")
        with pytest.raises(MailError) as caught:
            with Session(BOX, connect=server):
                pass
        message = str(caught.value)
        assert "登录被拒" in message and "三方客户端安全密码" in message
        assert PASSWORD not in message                       # 服务器把密码带回来了也要擦掉
        assert server.logged_out == 1

    @pytest.mark.parametrize("error, words", [
        (__import__("socket").gaierror("nodename nor servname"), "找不到邮箱服务器"),
        (TimeoutError("timed out"), "超时"),
        (ConnectionRefusedError(111, "refused"), "拒绝连接"),
    ])
    def test_network_errors_are_explained(self, error, words):
        def connect(host, port, timeout):
            raise error

        with pytest.raises(MailError, match=words):
            with Session(BOX, connect=connect):
                pass

    def test_probe_counts_what_the_rules_would_send(self):
        server = FakeIMAP()
        for uid, make in enumerate((samples.abuse, samples.marketplace, samples.case_new, samples.offer), start=1):
            server.add(uid, make())
        line = mail_inbox.probe(BOX, mail_rules.classify_headers, connect=server)
        assert "收件箱里有 4 封邮件" in line and "有 2 封符合告警规则" in line
        assert "只读" in line


# ================================================================== 收一轮、发一轮
MAIL_HEADER = [*LEDGER_HEADER, "TG_ENABLED", "TG_CHAT_IDS", "MAIL_ENABLED", "MAIL_PROVIDER", "MAIL_ADDRESS",
               "MAIL_PASSWORD", "EMAIL"]


def mail_ledger(path, *, alpha=None, beta=None):
    """ALPHA 开着 TG 和邮件告警；BETA 只开 TG。两个都没填账号邮箱。关键字参数按列名覆盖。"""
    alpha_row = dict(TG_ENABLED=True, TG_CHAT_IDS=ALPHA_CHAT, MAIL_ENABLED=True, MAIL_PROVIDER="aliyun-sg",
                     MAIL_ADDRESS="root-alpha@example.com", MAIL_PASSWORD=PASSWORD, EMAIL=None)
    beta_row = dict(TG_ENABLED=True, TG_CHAT_IDS=BETA_CHAT, MAIL_ENABLED=False, MAIL_PROVIDER=None,
                    MAIL_ADDRESS=None, MAIL_PASSWORD=None, EMAIL=None)
    alpha_row.update(alpha or {})
    beta_row.update(beta or {})
    extra = MAIL_HEADER[len(LEDGER_HEADER):]
    rows = [
        [*LEDGER_ROWS[0], *(alpha_row[name] for name in extra)],
        [*LEDGER_ROWS[1], *(beta_row[name] for name in extra)],
    ]
    write_ledger(path, header=MAIL_HEADER, rows=rows)
    excel_source.clear_cache()


@pytest.fixture
def outbox(monkeypatch):
    """Telegram 那一头：[(群组 ID, 卡片)]。"""
    box = []
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "123:FAKE")
    monkeypatch.setattr(alerts, "_send", lambda chat, card: box.append((chat, card)) or "")
    return box


@pytest.fixture
def server(ledger):
    mail_ledger(ledger)
    return FakeIMAP()


def check(server, **kw):
    return mail_alerts.run_check(NOW, connect=server, log=lambda *a: None, **kw)


def primed(server, outbox):
    """跑过第一轮（建好基线）的收件箱。"""
    server.add(1, samples.marketplace())
    check(server)
    assert outbox == []


class TestRunCheck:
    def test_first_run_only_takes_a_baseline(self, server, outbox):
        """旧邮件不补发：第一次只记下收到第几封。"""
        server.add(1, samples.abuse())
        server.add(2, samples.suspicious())
        summary = check(server)
        assert summary.ok and outbox == []
        state = json.loads(config.MAIL_STATE_PATH.read_text(encoding="utf-8"))
        assert state["mailboxes"]["root-alpha@example.com|imap.sg.aliyun.com:993"] == {
            "uidvalidity": 7, "last_uid": 2, "stuck_uid": 0, "stuck_attempts": 0,
        }
        assert server.fetched == []                        # 一封都没取

    def test_new_mail_goes_to_the_accounts_groups(self, server, outbox):
        primed(server, outbox)
        server.add(2, samples.abuse())
        summary = check(server)
        assert summary.ok and summary.sent == 1
        (chat, card), = outbox
        assert chat == ALPHA_CHAT and card.kind == "mail-abuse"
        assert "模型权限被撤销" in card.text() and ALPHA in card.text()

    def test_silent_mail_advances_without_sending(self, server, outbox):
        primed(server, outbox)
        for uid, make in enumerate(samples.SILENT, start=2):
            server.add(uid, make())
        check(server)
        assert outbox == []
        # 正文一封都没取：认类别只看信头
        assert not any("BODY.PEEK[]" in spec for spec in server.fetched)
        assert mail_alerts.load_state().boxes[BOX.key].last_uid == 1 + len(samples.SILENT)

    def test_nothing_is_sent_twice(self, server, outbox):
        primed(server, outbox)
        server.add(2, samples.case_new())
        check(server)
        check(server)
        assert len(outbox) == 1

    def test_the_health_copy_is_deduplicated(self, server, outbox):
        """AWS 从 no-reply@amazonaws.com 和 health@aws.com 各发一封一样的：只发一次。"""
        primed(server, outbox)
        server.add(2, samples.suspicious())
        server.add(3, samples.health())
        check(server)
        assert [card.kind for _, card in outbox] == ["mail-compromised"]

    def test_the_same_notice_a_day_later_is_sent_again(self, server, outbox):
        primed(server, outbox)
        server.add(2, samples.suspicious())
        check(server)
        server.add(3, samples.health())
        mail_alerts.run_check(NOW + timedelta(days=1), connect=server, log=lambda *a: None)
        assert len(outbox) == 2

    def test_root_mail_goes_to_the_fixed_group(self, server, outbox, monkeypatch):
        monkeypatch.setattr(config, "MAIL_ALERT_CHAT_IDS", (FIXED_CHAT,))
        primed(server, outbox)
        server.add(2, samples.mfa())
        server.add(3, samples.abuse())
        check(server)
        assert [(chat, card.kind) for chat, card in outbox] == [(FIXED_CHAT, "mail-root"), (ALPHA_CHAT, "mail-abuse")]

    def test_root_mail_falls_back_to_the_account_without_a_fixed_group(self, server, outbox):
        primed(server, outbox)
        server.add(2, samples.mfa())
        check(server)
        (chat, card), = outbox
        assert chat == ALPHA_CHAT
        assert ALPHA in card.text()        # 没写账号 ID 的邮件：就是收到它的那个账号

    def test_mail_naming_another_ledger_account_goes_there(self, server, outbox):
        primed(server, outbox)
        message = samples.message("no-reply-aws@amazon.com", "Amazon Web Services: New Support case: 1",
                                  samples.CASE_NEW.replace(ALPHA, BETA))
        server.add(2, message)
        check(server)
        assert [chat for chat, _ in outbox] == [BETA_CHAT]

    def test_an_account_outside_the_ledger_goes_to_the_fixed_group(self, server, outbox, monkeypatch):
        monkeypatch.setattr(config, "MAIL_ALERT_CHAT_IDS", (FIXED_CHAT,))
        primed(server, outbox)
        server.add(2, samples.message("no-reply-aws@amazon.com", "Amazon Web Services: New Support case: 1",
                                      samples.CASE_NEW.replace(ALPHA, "333333333333")))
        check(server)
        (chat, card), = outbox
        assert chat == FIXED_CHAT and "333333333333" in card.text()

    def test_tg_switched_off_falls_back_to_the_fixed_group(self, ledger, outbox, monkeypatch):
        mail_ledger(ledger, alpha={"TG_ENABLED": False})
        monkeypatch.setattr(config, "MAIL_ALERT_CHAT_IDS", (FIXED_CHAT,))
        server = FakeIMAP()
        primed(server, outbox)
        server.add(2, samples.abuse())
        check(server)
        assert [chat for chat, _ in outbox] == [FIXED_CHAT]

    def test_nowhere_to_send_is_a_problem_not_a_retry(self, ledger, outbox):
        mail_ledger(ledger, alpha={"TG_ENABLED": False})
        server = FakeIMAP()
        primed(server, outbox)
        server.add(2, samples.abuse())
        summary = check(server)
        assert outbox == [] and "没有地方发" in summary.problems[0]
        assert mail_alerts.load_state().boxes[BOX.key].last_uid == 2     # 跳过，不卡住后面的

    def test_a_failed_send_is_retried_next_round(self, server, outbox, monkeypatch):
        primed(server, outbox)
        server.add(2, samples.abuse())
        server.add(3, samples.case_new())

        def down(chat, card):
            raise TelegramError("连不上 Telegram")

        monkeypatch.setattr(alerts, "_send", down)
        summary = check(server)
        assert not summary.ok
        state = mail_alerts.load_state().boxes[BOX.key]
        assert (state.last_uid, state.stuck_uid, state.stuck_attempts) == (1, 2, 1)   # 停在第一封没发出去的

        monkeypatch.setattr(alerts, "_send", lambda chat, card: outbox.append((chat, card)) or "")
        check(server)
        assert [card.kind for _, card in outbox] == ["mail-abuse", "mail-case"]

    def test_gives_up_on_a_mail_after_enough_rounds(self, server, outbox, monkeypatch):
        primed(server, outbox)
        server.add(2, samples.abuse())
        monkeypatch.setattr(alerts, "_send", lambda chat, card: (_ for _ in ()).throw(TelegramError("坏了")))
        for _ in range(mail_alerts.MAX_ATTEMPTS - 1):
            check(server)
            assert mail_alerts.load_state().boxes[BOX.key].last_uid == 1
        summary = check(server)
        assert any("跳过这封" in problem for problem in summary.problems)
        assert mail_alerts.load_state().boxes[BOX.key].last_uid == 2

    def test_a_new_uidvalidity_takes_a_new_baseline(self, server, outbox):
        primed(server, outbox)
        server.add(2, samples.abuse())
        server.uidvalidity = 8
        check(server)
        assert outbox == []
        assert mail_alerts.load_state().boxes[BOX.key].uidvalidity == 8

    def test_a_shared_mailbox_is_read_once(self, ledger, outbox):
        """两个账号填了同一个邮箱：只登录一次，每封只发一次。"""
        same = {"MAIL_ENABLED": True, "MAIL_PROVIDER": "aliyun-sg", "MAIL_ADDRESS": "Root-Alpha@example.com",
                "MAIL_PASSWORD": PASSWORD}
        mail_ledger(ledger, beta=same)
        server = FakeIMAP()
        primed(server, outbox)
        assert server.connections == 1
        server.add(2, samples.abuse())
        check(server)
        assert [chat for chat, _ in outbox] == [ALPHA_CHAT]        # 邮件里写的是 ALPHA

    def test_switched_off_or_disabled_accounts_are_not_read(self, ledger, outbox):
        for tweak in ({"MAIL_ENABLED": False}, {"MAIL_PASSWORD": None}):
            mail_ledger(ledger, alpha=tweak)
            server = FakeIMAP()
            summary = check(server)
            assert summary.ok and server.connections == 0

    def test_switching_off_forgets_the_progress(self, ledger, server, outbox):
        """再打开时从那一刻算起，不补发关着的这段时间里到的邮件。"""
        primed(server, outbox)
        mail_ledger(ledger, alpha={"MAIL_ENABLED": False})
        check(server)
        assert mail_alerts.load_state().boxes == {}

    def test_missing_token_means_no_login(self, server):
        summary = check(server)
        assert summary.problems == ["没有配置 TELEGRAM_BOT_TOKEN"]
        assert server.connections == 0

    def test_a_login_failure_is_a_problem(self, ledger, outbox):
        mail_ledger(ledger, alpha={"MAIL_PASSWORD": "wrong"})
        summary = check(FakeIMAP())
        assert "root-alpha@example.com 收信失败：登录被拒" in summary.problems[0]
        assert "wrong" not in summary.problems[0]

    def test_dry_run_looks_back_without_touching_anything(self, server, outbox, tmp_path):
        for uid, make in enumerate((samples.abuse, samples.marketplace, samples.mfa), start=1):
            server.add(uid, make())
        lines = []
        summary = mail_alerts.run_check(
            NOW, connect=server, dry_run=True, look_back=10, save_dir=tmp_path / "cards", log=lines.append,
        )
        assert summary.sent == 2 and outbox == []
        assert sorted(p.name for p in (tmp_path / "cards").iterdir()) == [
            "01-mail-abuse-1001111111111.png", "02-mail-root-1001111111111.png",
        ]
        assert not config.MAIL_STATE_PATH.exists()
        assert not config.ALERT_EVENTS_PATH.exists()          # 也不记告警事件流

    def test_look_back_needs_dry_run(self, server):
        with pytest.raises(ValueError):
            check(server, look_back=5)


class TestMailCards:
    @pytest.mark.parametrize("make", samples.ALERTING)
    def test_every_kind_renders(self, make):
        mail = read(make)
        finding = mail_rules.inspect(mail, mail_rules.classify(mail), NOW)
        card = mail_alerts.mail_card(finding, ALPHA, mail)
        assert card.png().startswith(b"\x89PNG")
        assert len(telegram.visible(card.caption)) <= telegram.MAX_CAPTION_CHARS

    def test_same_layout_as_the_other_cards(self):
        """标题行 + 详情面板（账号 UID + 字段 + 收到时间）+ 原文节选 + 怎么处理 + 署名。"""
        mail = read(samples.suspicious)
        card = mail_alerts.mail_card(mail_rules.inspect(mail, "compromised", NOW), ALPHA, mail)
        details, quote, notes = card.blocks
        assert isinstance(details, cards.Details) and details.uid == ALPHA
        assert details.rows[-1].label == "收到时间"
        assert isinstance(quote, cards.Quote) and quote.heading == mail.subject
        assert isinstance(notes, cards.Notes) and notes.title == "怎么处理"
        assert card.signature == config.TELEGRAM_CARD_SIGNATURE
        assert (card.tone, card.icon) == ("danger", "warning-red")

    def test_caption_quotes_the_excerpt_and_links_to_aws_only(self):
        mail = read(samples.case_new)
        card = mail_alerts.mail_card(mail_rules.inspect(mail, "case", NOW), ALPHA, mail)
        assert "<blockquote>Amazon Web Services has opened case" in card.caption
        assert '<a href="https://console.aws.amazon.com/support/home#/case/' in card.caption

    def test_a_long_excerpt_is_clipped_to_fit_the_caption(self):
        mail = read(samples.suspicious)
        finding = mail_rules.inspect(mail, "compromised", NOW)
        finding.excerpt = "Word " * 600
        card = mail_alerts.mail_card(finding, ALPHA, mail)
        assert len(telegram.visible(card.caption)) <= telegram.MAX_CAPTION_CHARS
        assert "<blockquote>" in card.caption and "主题：" in card.caption

    def test_the_partner_never_appears(self, server, outbox):
        primed(server, outbox)
        for uid, make in enumerate(samples.ALERTING, start=2):
            server.add(uid, make())
        check(server)
        assert outbox
        for _, card in outbox:
            assert "ALPHA" not in card.text() and "BETA" not in card.text()

    def test_long_row_values_are_clipped_not_overlapping(self):
        row = cards.Row("处理动作", "x" * 400)
        card = cards.Card(kind="t", tone="warn", icon="warning", title="t", badge="B", subtitle="s", caption="c",
                          blocks=[cards.Details([row])])
        assert card.png().startswith(b"\x89PNG")
        assert cards._fit("x" * 400, cards._font(16, 700), 100).endswith("…")


class TestMailEmailAndEvents:
    """认得出是台账里哪个账号：卡片和 caption 跟上它的账号邮箱（写全）。发出去了的记一条告警事件。"""

    MAIL = "alpha.root@example.com"

    @pytest.fixture
    def mailed(self, ledger, outbox):
        """ALPHA 在台账里填了账号邮箱，收件箱已经建好基线。"""
        mail_ledger(ledger, alpha={"EMAIL": self.MAIL})
        server = FakeIMAP()
        primed(server, outbox)
        return server

    def test_the_card_carries_the_account_email(self, mailed, outbox):
        mailed.add(2, samples.abuse())
        check(mailed)
        (_, card), = outbox
        details = card.blocks[0]
        assert (details.uid, details.email) == (ALPHA, self.MAIL)
        assert f"<code>{ALPHA}</code> · {self.MAIL}" in card.caption

    def test_the_quoted_mail_text_is_still_masked(self):
        """写全的只是台账里的账号邮箱；邮件原文节选里引用的地址可能是别人的，照旧打码。"""
        assert mail_rules.excerpt("Contact alice@example.com about it.") == "Contact a***@example.com about it."

    def test_an_account_outside_the_ledger_has_no_email(self, mailed, outbox, monkeypatch):
        monkeypatch.setattr(config, "MAIL_ALERT_CHAT_IDS", (FIXED_CHAT,))
        mailed.add(2, samples.message("no-reply-aws@amazon.com", "Amazon Web Services: New Support case: 1",
                                      samples.CASE_NEW.replace(ALPHA, "333333333333")))
        check(mailed)
        (_, card), = outbox
        assert card.blocks[0].email == ""
        assert self.MAIL not in card.text()
        assert "<code>333333333333</code> · " not in card.caption

    def test_delivered_mail_alerts_are_recorded(self, mailed, outbox):
        mailed.add(2, samples.abuse())
        check(mailed)
        (event,) = events.recent()
        assert (event.kind, event.title, event.tone, event.groups) == ("mail-abuse", "模型权限被撤销", "error", 1)
        assert (event.account, event.email) == (ALPHA, self.MAIL)
        assert event.text.startswith("处理动作 撤销 Anthropic 模型的调用权限")

    def test_a_deduplicated_copy_is_recorded_once(self, server, outbox):
        primed(server, outbox)
        server.add(2, samples.suspicious())
        server.add(3, samples.health())
        check(server)
        assert [event.kind for event in events.recent()] == ["mail-compromised"]

    def test_a_mail_nobody_can_be_matched_to_is_recorded_without_an_account(self, ledger, outbox, monkeypatch):
        """两个账号共用一个邮箱、邮件里又没写账号 ID：认不出是谁，事件里也不写账号。"""
        shared = {"MAIL_ENABLED": True, "MAIL_PROVIDER": "aliyun-sg", "MAIL_ADDRESS": "root-alpha@example.com",
                  "MAIL_PASSWORD": PASSWORD}
        mail_ledger(ledger, alpha={"EMAIL": self.MAIL}, beta=shared)
        monkeypatch.setattr(config, "MAIL_ALERT_CHAT_IDS", (FIXED_CHAT,))
        server = FakeIMAP()
        primed(server, outbox)
        server.add(2, samples.mfa())
        check(server)
        (event,) = events.recent()
        assert (event.kind, event.account, event.email, event.groups) == ("mail-root", "", "", 1)

    def test_a_failed_send_records_nothing(self, mailed, outbox, monkeypatch):
        mailed.add(2, samples.abuse())
        monkeypatch.setattr(alerts, "_send", lambda chat, card: (_ for _ in ()).throw(TelegramError("坏了")))
        check(mailed)
        assert events.recent() == []


class TestCli:
    def test_save_needs_dry_run(self, capsys):
        from bedrock_cost.__main__ import main

        assert main(["mail", "check", "--save", "x"]) == 2
        assert main(["mail", "check", "--recent", "5"]) == 2

    def test_test_needs_an_account(self):
        from bedrock_cost.__main__ import main

        assert main(["mail", "test"]) == 2

    def test_test_reports_a_missing_mailbox(self, ledger, capsys):
        from bedrock_cost.__main__ import main

        assert main(["mail", "test", "--account", ALPHA]) == 1
        assert "还没有填好告警邮箱" in capsys.readouterr().out
