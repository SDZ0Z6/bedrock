"""Telegram 告警：日报、用量切换、额度阈值、状态文件、发送客户端。

在 alerts 模块的边界上替换掉四样东西，全程不联网：
    cost_explorer.fetch_all / fetch_split   CE（日报 / 额度阈值的实账部分）
    hourly_invocations                      CW 每小时调用次数（用量切换）
    estimate_split                          CW 最近两天的估算（额度阈值）
    alerts._send                            发出去的卡片收进一个列表（记下卡片上的全部文字）

卡片本身怎么画的在 test_cards.py；这里的 _send 真身（画图 + sendPhoto、画不出来
退回发文字）在 TestSendCard。
"""

from __future__ import annotations

import io
import json
import urllib.error
from datetime import date, datetime, timedelta, timezone

import pytest

from bedrock_cost import alerts, cards, config, cost_explorer, excel_source, telegram
from bedrock_cost.alerts import AccountState, load_state, run_daily, run_hourly, save_state
from bedrock_cost.cost_estimate import SplitEstimate
from bedrock_cost.cost_explorer import CostSplit
from bedrock_cost.telegram import TelegramError

from .conftest import LEDGER_HEADER, LEDGER_ROWS, write_ledger

UTC = timezone.utc
ALPHA, BETA = "111111111111", "222222222222"
CHAT_A, CHAT_B = "-1001111111111", "-1002222222222"
# 周六 06:05 UTC：当前小时 06:00，最近一个完整小时是 05:00
NOW = datetime(2026, 9, 27, 6, 5, tzinfo=UTC)


def tg_ledger(path, settings: dict[str, tuple[bool, str]], **overrides) -> None:
    """在测试台账上加 TG 两列。settings: {账号: (开关, 群组 ID 或几个群组 ID)}。

    overrides 可以改某个账号的其他列，形如 BUDGET={ALPHA: 1000}。
    """
    # 基础台账没有的列（比如 ENABLED）按需补上；没被点名的账号留空，
    # 空着就是各列自己的默认值（ENABLED 空 = 启用）
    extra = [column for column in overrides if column not in LEDGER_HEADER]
    header = [*LEDGER_HEADER, *extra, "TG_ENABLED", "TG_CHAT_IDS"]
    rows = []
    for row in LEDGER_ROWS:
        row = [*row, *([None] * len(extra))]
        account = str(row[LEDGER_HEADER.index("ACCOUNT")])
        for column, per_account in overrides.items():
            if account in per_account:
                row[header.index(column)] = per_account[account]
        enabled, chats = settings.get(account, (False, ""))
        if not isinstance(chats, str):
            chats = ",".join(chats)          # 台账里一格存多个，逗号隔开
        rows.append([*row, enabled, chats])
    write_ledger(path, header=header, rows=rows)
    excel_source.clear_cache()


class Outbox(list):
    """发出去的卡片：[(群组 ID, 卡片上的全部文字)]；.cards 里是 (群组 ID, 卡片本身)。"""

    def __init__(self):
        super().__init__()
        self.cards: list[tuple[str, cards.Card]] = []

    def send(self, chat: str, card: cards.Card) -> str:
        self.append((chat, card.text()))
        self.cards.append((chat, card))
        return ""


@pytest.fixture
def sent(monkeypatch):
    box = Outbox()
    monkeypatch.setattr(alerts, "_send", box.send)
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "123456:FAKE-TOKEN")
    return box


def failing(message: str, only: str | None = None, then=None):
    """一个假的 _send：发往 only 这个群（不给就是所有群）时抛 TelegramError，其余交给 then。"""

    def send(chat, card):
        if only is None or chat == only:
            raise TelegramError(message)
        return then(chat, card)

    return send


@pytest.fixture
def state_file(tmp_path, monkeypatch):
    path = tmp_path / "alert-state.json"
    monkeypatch.setattr(config, "ALERT_STATE_PATH", path)
    return path


@pytest.fixture
def cw(monkeypatch):
    """CloudWatch 两个口子的开关。改这个 dict 就能控制下一次运行看到什么。"""
    knobs = {
        "invocations": {},      # {账号: [最近 N 小时的调用次数，从早到晚]}
        "inv_fail": False,
        "recent": {},           # {账号: (tag_raw, untag_raw)}——最近两天的估算
        "recent_errors": [],
        "calls": [],            # estimate_split 的调用记录 (账号, 起, 止)
    }

    def fake_invocations(account, hours, regions=None):
        if knobs["inv_fail"]:
            return None, ["us-east-1：ThrottlingException"]
        values = knobs["invocations"].get(account.account, [0.0] * len(hours))
        return list(values)[-len(hours):], []

    def fake_estimate(account, start, end, regions=None):
        knobs["calls"].append((account.account, start, end))
        tag, untag = knobs["recent"].get(account.account, (0.0, 0.0))
        return SplitEstimate(tag_raw=tag, untag_raw=untag, errors=list(knobs["recent_errors"]))

    monkeypatch.setattr(alerts, "hourly_invocations", fake_invocations)
    monkeypatch.setattr(alerts, "estimate_split", fake_estimate)
    return knobs


@pytest.fixture
def ce(monkeypatch):
    """额度阈值要的 CE 实账（截至前天）。"""
    knobs = {"values": {}, "error": None, "calls": []}

    def fake_split(account, start, end, refresh=False):
        knobs["calls"].append((account.account, start, end))
        if knobs["error"]:
            return CostSplit(error=knobs["error"])
        tag, untag = knobs["values"].get(account.account, (0.0, 0.0))
        return CostSplit(tag_raw=tag, untag_raw=untag)

    monkeypatch.setattr(cost_explorer, "fetch_split", fake_split)
    return knobs


def quiet(*_args, **_kwargs):
    pass


# ================================================================== 发送客户端
class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture
def http(monkeypatch):
    """替换 urlopen。seen 收到请求，reply 决定怎么回。"""
    knobs = {"seen": [], "reply": lambda request: _Response(b'{"ok": true}')}
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "123456:SECRET-TOKEN")

    def fake_urlopen(request, timeout=None):
        knobs["seen"].append(request)
        return knobs["reply"](request)

    monkeypatch.setattr(telegram.urllib.request, "urlopen", fake_urlopen)
    return knobs


def _http_error(code: int, description: str):
    body = json.dumps({"ok": False, "error_code": code, "description": description}).encode()
    return urllib.error.HTTPError("https://x", code, "err", {}, io.BytesIO(body))


class TestTelegramClient:
    def test_posts_html_to_send_message(self, http):
        telegram.send_message(CHAT_A, "<b>hi</b>")
        request = http["seen"][0]
        assert request.full_url.endswith("/bot123456:SECRET-TOKEN/sendMessage")
        body = json.loads(request.data)
        assert body == {
            "chat_id": CHAT_A,
            "text": "<b>hi</b>",
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }

    @pytest.mark.parametrize(
        "code, description, hint",
        [
            (400, "Bad Request: chat not found", "群组 ID 不对"),
            (403, "Forbidden: bot was kicked from the group chat", "被移出"),
            (403, "Forbidden: bot is not a member of the group chat", "不在这个群里"),
            (401, "Unauthorized", "Token 无效"),
            (429, "Too Many Requests: retry after 5", "限流"),
        ],
    )
    def test_explains_telegram_errors(self, http, code, description, hint):
        """Telegram 出错时状态码是 4xx，但真正的原因在响应体的 description 里。"""
        def reply(request):
            raise _http_error(code, description)

        http["reply"] = reply
        with pytest.raises(TelegramError) as caught:
            telegram.send_message(CHAT_A, "x")
        assert hint in str(caught.value)

    def test_never_leaks_the_token(self, http):
        """Token 就在 URL 里，网络错误的文本很容易把它原样带出来。"""
        def reply(request):
            raise urllib.error.URLError(f"cannot reach {request.full_url}")

        http["reply"] = reply
        with pytest.raises(TelegramError) as caught:
            telegram.send_message(CHAT_A, "x")
        assert "SECRET-TOKEN" not in str(caught.value)
        assert "连不上" in str(caught.value)

    def test_refuses_without_a_token(self, monkeypatch):
        monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "")
        with pytest.raises(TelegramError, match="TELEGRAM_BOT_TOKEN"):
            telegram.send_message(CHAT_A, "x")

    def test_refuses_a_malformed_chat_id(self, http):
        with pytest.raises(TelegramError, match="格式不对"):
            telegram.send_message("not-a-chat", "x")
        assert http["seen"] == []   # 根本没发出去

    def test_refuses_an_oversized_message(self, http):
        """不做静默截断：半截消息比发不出去更容易误导人。"""
        with pytest.raises(TelegramError, match="4096"):
            telegram.send_message(CHAT_A, "x" * 5000)

    @pytest.mark.parametrize(
        "chat, ok",
        [
            ("-1001234567890", True),     # 超级群组
            ("-123456789", True),         # 普通群组
            ("123456789", True),          # 私聊
            ("@my_channel", True),        # 公开频道
            ("", False),
            ("abc", False),
            ("-12", False),
            ("@ab", False),
        ],
    )
    def test_chat_id_shapes(self, chat, ok):
        assert telegram.valid_chat_id(chat) is ok

    # ---- sendPhoto：告警卡片
    def test_posts_the_card_as_multipart(self, http):
        telegram.send_photo(CHAT_A, b"\x89PNG-bytes", "<b>日报</b> · <code>1</code>")
        request = http["seen"][0]
        assert request.full_url.endswith("/bot123456:SECRET-TOKEN/sendPhoto")
        content_type = request.get_header("Content-type")
        assert content_type.startswith("multipart/form-data; boundary=")
        boundary = content_type.split("boundary=")[1].encode()
        body = request.data
        assert body.endswith(b"--" + boundary + b"--\r\n")
        assert f'name="chat_id"\r\n\r\n{CHAT_A}\r\n'.encode() in body
        assert 'name="caption"\r\n\r\n<b>日报</b> · <code>1</code>\r\n'.encode() in body
        assert b'name="parse_mode"\r\n\r\nHTML\r\n' in body
        assert b'name="photo"; filename="card.png"\r\nContent-Type: image/png\r\n\r\n\x89PNG-bytes\r\n' in body

    def test_photo_errors_are_explained_too(self, http):
        def reply(request):
            raise _http_error(400, "Bad Request: not enough rights to send photos to the chat")

        http["reply"] = reply
        with pytest.raises(TelegramError, match="没有发图片的权限"):
            telegram.send_photo(CHAT_A, b"png", "x")

    def test_photo_never_leaks_the_token(self, http):
        def reply(request):
            raise urllib.error.URLError(f"cannot reach {request.full_url}")

        http["reply"] = reply
        with pytest.raises(TelegramError) as caught:
            telegram.send_photo(CHAT_A, b"png", "x")
        assert "SECRET-TOKEN" not in str(caught.value)

    def test_caption_limit_counts_what_telegram_shows(self, http):
        """1024 字的上限按显示出来的字数算：<code> 这些标签不算。"""
        tagged = "<code>1</code>" * 1000                 # 显示出来只有 1000 个字
        telegram.send_photo(CHAT_A, b"png", tagged)
        with pytest.raises(TelegramError, match="1024"):
            telegram.send_photo(CHAT_A, b"png", "x" * 1025)

    def test_visible_strips_tags_and_entities(self):
        assert telegram.visible("<b>余额</b> &lt;$1&gt; <code>12</code>") == "余额 <$1> 12"


# ================================================================== 状态文件
class TestState:
    def test_missing_file_means_no_state(self, state_file):
        assert load_state() == {}

    def test_round_trip(self, state_file):
        save_state({ALPHA: AccountState(active=True, fired=[50.0], ce_key="k", ce_untag_raw=9.5)})
        loaded = load_state()
        assert loaded[ALPHA].active is True
        assert loaded[ALPHA].fired == [50.0]
        assert loaded[ALPHA].ce_untag_raw == 9.5

    def test_corrupt_file_starts_over(self, state_file):
        """文件坏了宁可重建基线，也不要让整个告警任务起不来。"""
        state_file.write_text("{ not json", encoding="utf-8")
        assert load_state() == {}

    def test_unknown_fields_are_ignored(self, state_file):
        """以后加减字段时，老状态文件还能读。"""
        state_file.write_text(
            json.dumps({"version": 1, "accounts": {ALPHA: {"active": False, "from_the_future": 1}}}),
            encoding="utf-8",
        )
        assert load_state()[ALPHA].active is False

    def test_no_temp_file_left_behind(self, state_file):
        save_state({ALPHA: AccountState()})
        assert [p.name for p in state_file.parent.iterdir()] == ["alert-state.json"]


# ================================================================== 日报
class TestDaily:
    TODAY = date(2026, 8, 17)

    def test_nothing_to_send_when_no_account_opted_in(self, ledger, fake_costs, sent):
        """TG 开关默认关：台账没这两列，就一条都不发。"""
        summary = run_daily(self.TODAY, log=quiet)
        assert summary.ok and summary.sent == 0 and sent == []

    def test_sends_one_table_per_chat(self, ledger, fake_costs, sent):
        tg_ledger(ledger, {ALPHA: (True, CHAT_A), BETA: (True, CHAT_B)})
        run_daily(self.TODAY, log=quiet)
        assert sorted(chat for chat, _ in sent) == [CHAT_A, CHAT_B]
        text_a = dict(sent)[CHAT_A]
        assert ALPHA in text_a and BETA not in text_a   # 每个群只看到自己的账号

    def test_accounts_sharing_a_chat_share_one_message(self, ledger, fake_costs, sent):
        tg_ledger(ledger, {ALPHA: (True, CHAT_A), BETA: (True, CHAT_A)})
        run_daily(self.TODAY, log=quiet)
        assert len(sent) == 1
        assert ALPHA in sent[0][1] and BETA in sent[0][1]

    def test_numbers_match_the_overview_page(self, ledger, fake_costs, sent, logged_in):
        """同一组函数算的，和概览页一分不差。"""
        tg_ledger(ledger, {ALPHA: (True, CHAT_A)})
        run_daily(self.TODAY, log=quiet)
        # fake CE：每天 TAG 100 / UNTAG 50，08-01~08-17 共 17 天；ALPHA 的 UNTAG 比率 1.05
        spent = 100 * 17 * 1 + 50 * 17 * 1.05
        text = sent[0][1]
        assert f"${spent:,.2f}" in text
        assert f"${500000 - spent:,.2f}" in text

    def test_table_has_the_four_columns(self, ledger, fake_costs, sent):
        tg_ledger(ledger, {ALPHA: (True, CHAT_A)})
        run_daily(self.TODAY, log=quiet)
        card = sent.cards[0][1]
        table = card.blocks[0]
        assert table.columns == ["UID", "授信额度", "累计消费", "剩余额度"]
        assert [cell.text for cell in table.rows[0]][:2] == [ALPHA, "$500,000.00"]
        assert card.kind == "daily" and card.badge == "DAILY REPORT"

    def test_problem_rows_are_red_and_explained(self, ledger, sent, monkeypatch):
        """查询失败、超额的数字标红，原因逐条写在表格下面。"""
        tg_ledger(ledger, {ALPHA: (True, CHAT_A), BETA: (True, CHAT_A)})
        monkeypatch.setattr(
            cost_explorer, "fetch_all",
            lambda accounts, ranges, refresh=False: {
                accounts[0].key: CostSplit(error="AK 无效或已删除（InvalidClientTokenId）"),
                accounts[1].key: CostSplit(untag_raw=200000.0),    # × 1.10 = 220,000，额度 100,000
            },
        )
        run_daily(self.TODAY, log=quiet)
        card = sent.cards[0][1]
        failed, over = card.blocks[0].rows
        assert (failed[2].text, failed[2].tone) == ("查询失败", "danger")
        assert (over[3].text, over[3].tone) == ("-$120,000.00", "danger")   # 负号在 $ 前面
        assert failed[0].mark == over[0].mark == "danger"
        notes = dict((text.split()[0], text) for _, text in card.blocks[1].items)
        assert "InvalidClientTokenId" in notes[ALPHA]
        assert "已超出额度 $120,000.00" in notes[BETA]
        assert "（已超出额度）" in card.caption

    def test_many_accounts_are_split_into_several_cards(self, ledger, fake_costs, sent, monkeypatch):
        """一张图放太多行，Telegram 一压缩就看不清了：每张最多 MAX_TABLE_ROWS 个账号。"""
        monkeypatch.setattr(cards, "MAX_TABLE_ROWS", 1)
        tg_ledger(ledger, {ALPHA: (True, CHAT_A), BETA: (True, CHAT_A)})
        run_daily(self.TODAY, log=quiet)
        daily = [card for _, card in sent.cards]
        assert [card.subtitle for card in daily] == ["2026-08-17 · 第 1/2 张", "2026-08-17 · 第 2/2 张"]
        assert all(card.footer.startswith("账号数量：2") for card in daily)

    def test_skips_opted_out_and_disabled_accounts(self, ledger, fake_costs, sent):
        tg_ledger(ledger, {ALPHA: (False, CHAT_A), BETA: (True, CHAT_B)}, ENABLED={BETA: False})
        run_daily(self.TODAY, log=quiet)
        assert sent == []   # ALPHA 没开，BETA 开了但账号已停用

    def test_flags_a_missing_start_date(self, ledger, fake_costs, sent):
        tg_ledger(ledger, {ALPHA: (True, CHAT_A)}, START_DATE={ALPHA: None})
        run_daily(self.TODAY, log=quiet)
        assert "未填启用日期" in sent[0][1]

    def test_a_failed_ce_query_is_shown_not_hidden(self, ledger, sent, monkeypatch):
        tg_ledger(ledger, {ALPHA: (True, CHAT_A)})
        monkeypatch.setattr(
            cost_explorer, "fetch_all",
            lambda accounts, ranges, refresh=False: {a.key: CostSplit(error="AccessDenied") for a in accounts},
        )
        summary = run_daily(self.TODAY, log=quiet)
        assert "查询失败" in sent[0][1]
        assert not summary.ok   # 命令以非零退出，systemctl --failed 看得到

    def test_no_token_is_a_problem_not_a_crash(self, ledger, fake_costs, monkeypatch):
        tg_ledger(ledger, {ALPHA: (True, CHAT_A)})
        monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "")
        summary = run_daily(self.TODAY, log=quiet)
        assert not summary.ok
        assert "TELEGRAM_BOT_TOKEN" in summary.problems[0]

    def test_dry_run_sends_nothing(self, ledger, fake_costs, sent):
        tg_ledger(ledger, {ALPHA: (True, CHAT_A)})
        printed = []
        run_daily(self.TODAY, dry_run=True, log=printed.append)
        assert sent == []
        assert any(ALPHA in line for line in printed)

    def test_a_send_failure_is_reported(self, ledger, fake_costs, monkeypatch):
        tg_ledger(ledger, {ALPHA: (True, CHAT_A)})
        monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "t")
        monkeypatch.setattr(alerts, "_send", failing("群组 ID 不对"))
        summary = run_daily(self.TODAY, log=quiet)
        assert summary.sent == 0
        assert "群组 ID 不对" in summary.problems[0]


# ================================================================== 用量切换
class TestUsageSwitch:
    @pytest.fixture(autouse=True)
    def _setup(self, ledger, state_file, cw, ce, sent):
        tg_ledger(ledger, {ALPHA: (True, CHAT_A)})
        # 额度阈值这条线在这组用例里保持安静：累计消费为 0
        self.cw, self.sent, self.state_file = cw, sent, state_file

    def run(self, invocations, now=NOW):
        self.cw["invocations"][ALPHA] = invocations
        return run_hourly(now, log=quiet)

    def texts(self):
        return [text for _, text in self.sent]

    def test_first_run_only_records_a_baseline(self):
        """刚开启时已经在用，不能发「开始有用量」——那其实是早就在用了。"""
        self.run([120])
        assert self.sent == []
        assert load_state()[ALPHA].active is True

    def test_idle_to_active_says_it_started(self):
        self.run([0])
        self.run([350], now=NOW + timedelta(hours=1))
        assert len(self.sent) == 1
        assert "用量开始" in self.texts()[0]
        assert "350" in self.texts()[0]
        card = self.sent.cards[0][1]
        assert (card.kind, card.tone, card.badge) == ("started", "ok", "ACTIVE")

    def test_active_to_idle_says_it_stopped(self):
        self.run([10])
        self.run([0], now=NOW + timedelta(hours=1))
        assert "用量中断" in self.texts()[0]
        assert "上一次有调用" in self.texts()[0]
        card = self.sent.cards[0][1]
        assert (card.kind, card.tone, card.badge) == ("stopped", "danger", "INTERRUPTED")

    def test_steady_use_does_not_repeat(self):
        """持续在用不刷屏：只有切换那一刻才发。"""
        self.run([0])
        for hour in range(1, 6):
            self.run([100], now=NOW + timedelta(hours=hour))
        assert len(self.sent) == 1

    def test_steady_idle_does_not_repeat(self):
        self.run([10])
        for hour in range(1, 6):
            self.run([0], now=NOW + timedelta(hours=hour))
        assert len(self.sent) == 1

    def test_idle_hours_setting_needs_the_whole_window_empty(self, monkeypatch):
        """TELEGRAM_IDLE_HOURS=3：只空了最后一小时不算中断，空满三小时才算。"""
        monkeypatch.setattr(config, "TELEGRAM_IDLE_HOURS", 3)
        self.run([5, 5, 5])
        self.run([5, 5, 0], now=NOW + timedelta(hours=1))
        self.run([5, 0, 0], now=NOW + timedelta(hours=2))
        assert self.sent == []
        self.run([0, 0, 0], now=NOW + timedelta(hours=3))
        assert "用量中断" in self.texts()[0]
        assert "连续 3 个小时没有任何调用" in self.texts()[0]

    def test_a_cloudwatch_failure_never_reads_as_idle(self):
        """读不到的零不是真的零——否则 CW 一限流就误报「用量中断」。"""
        self.run([10])
        self.cw["inv_fail"] = True
        summary = self.run([0], now=NOW + timedelta(hours=1))
        assert self.sent == []
        assert load_state()[ALPHA].active is True
        assert not summary.ok

    def test_a_failed_send_is_retried_next_hour(self, monkeypatch):
        """发失败就不落状态：下一小时条件还成立会再发一次，而不是就此丢掉。"""
        self.run([0])
        monkeypatch.setattr(alerts, "_send", failing("网络抖了"))
        self.run([50], now=NOW + timedelta(hours=1))
        assert load_state()[ALPHA].active is False   # 还没切过去

        monkeypatch.setattr(alerts, "_send", self.sent.send)
        self.run([60], now=NOW + timedelta(hours=2))
        assert "用量开始" in self.texts()[0]

    def test_turning_alerts_off_forgets_the_state(self, ledger):
        """再开启时从头建基线，不拿几周前的旧状态去比，免得一开就收到过时的「中断」。"""
        self.run([10])
        tg_ledger(ledger, {ALPHA: (False, CHAT_A)})
        run_hourly(NOW + timedelta(hours=1), log=quiet)
        assert ALPHA not in load_state()

    def test_dry_run_leaves_state_untouched(self):
        self.cw["invocations"][ALPHA] = [10]
        run_hourly(NOW, dry_run=True, log=quiet)
        assert not self.state_file.exists()


# ================================================================== 额度阈值
class TestQuotaThresholds:
    BUDGET = 1000.0

    @pytest.fixture(autouse=True)
    def _setup(self, ledger, state_file, cw, ce, sent):
        # ALPHA：额度 1000，TAG 比率 1，UNTAG 比率 1.05
        tg_ledger(ledger, {ALPHA: (True, CHAT_A)}, BUDGET={ALPHA: self.BUDGET})
        cw["invocations"][ALPHA] = [1]   # 用量这条线保持「一直在用」，不产生消息
        self.cw, self.ce, self.sent = cw, ce, sent

    def spend(self, ce_untag=0.0, recent_untag=0.0, ce_tag=0.0, recent_tag=0.0):
        self.ce["values"][ALPHA] = (ce_tag, ce_untag)
        self.cw["recent"][ALPHA] = (recent_tag, recent_untag)

    def run(self, now=NOW):
        return run_hourly(now, log=quiet)

    def quota_texts(self):
        return [text for _, text in self.sent if "额度" in text]

    def test_below_every_threshold_is_quiet(self):
        self.spend(ce_untag=100)            # 105 / 1000 = 10.5%
        self.run()
        assert self.quota_texts() == []

    def test_crossing_fifty_percent_alerts_once(self):
        self.spend(ce_untag=500)            # 525 = 52.5%
        self.run()
        self.run(NOW + timedelta(hours=1))
        self.run(NOW + timedelta(hours=2))
        texts = self.quota_texts()
        assert len(texts) == 1
        assert "超过 50% 提醒线" in texts[0]
        assert "52.5%" in texts[0]

    def test_each_threshold_fires_as_it_is_crossed(self):
        self.spend(ce_untag=500)                         # 52.5%
        self.run()
        self.spend(ce_untag=500, recent_untag=300)       # 52.5% + 31.5% = 84%
        self.run(NOW + timedelta(hours=1))
        self.spend(ce_untag=500, recent_untag=400)       # 94.5%
        self.run(NOW + timedelta(hours=2))
        self.spend(ce_untag=500, recent_untag=500)       # 105%
        self.run(NOW + timedelta(hours=3))
        quota = [card for _, card in self.sent.cards if card.kind == "quota"]
        assert [card.caption.splitlines()[1] for card in quota] == [
            "额度已用 52.5%，超过 50% 提醒线",
            "额度已用 84.0%，超过 80% 提醒线",
            "额度已用 94.5%，超过 90% 提醒线",
            "额度已用 105.0%，已经用完",
        ]
        # 90% 起卡片变红，100% 换标题
        assert [(card.title, card.tone) for card in quota] == [
            ("额度预警", "warn"), ("额度预警", "warn"), ("额度预警", "danger"), ("额度已用完", "danger"),
        ]

    def test_a_big_jump_sends_only_the_top_threshold(self):
        """刚开启告警时已经 95%：发一条「已用 90%」，别连着刷 50/80/90 三条。"""
        self.spend(ce_untag=905)                         # 950.25 = 95.0%
        self.run()
        texts = self.quota_texts()
        assert len(texts) == 1 and "超过 90% 提醒线" in texts[0]
        assert load_state()[ALPHA].fired == [50.0, 80.0, 90.0]

    def test_ratios_are_applied_like_the_overview_page(self):
        """TAG × TAG_RATIO + UNTAG × UNTAG_RATIO，和概览页同口径。"""
        self.spend(ce_tag=200, ce_untag=200, recent_tag=50, recent_untag=50)
        self.run()
        # (200+50)×1 + (200+50)×1.05 = 512.5
        assert "$512.50" in self.quota_texts()[0]

    def test_a_budget_change_starts_a_new_period(self, ledger):
        """额度改了等于开了新的一期，已发的档位清零。"""
        self.spend(ce_untag=500)
        self.run()
        tg_ledger(ledger, {ALPHA: (True, CHAT_A)}, BUDGET={ALPHA: 2000.0})
        # CE 那段同一天内是缓存的（过去的花费不会因为额度改了而变），
        # 所以新增的花费从 CW 最近两天那段进来：525 + 525 = 1050 / 2000 = 52.5%
        self.spend(ce_untag=500, recent_untag=500)
        self.run(NOW + timedelta(hours=1))
        assert len(self.quota_texts()) == 2

    def test_a_start_date_change_starts_a_new_period(self, ledger):
        self.spend(ce_untag=500)
        self.run()
        tg_ledger(ledger, {ALPHA: (True, CHAT_A)}, BUDGET={ALPHA: self.BUDGET},
                  START_DATE={ALPHA: date(2026, 9, 1)})
        self.run(NOW + timedelta(hours=1))
        assert len(self.quota_texts()) == 2

    def test_ce_is_queried_once_a_day(self):
        """CE 按请求收费，小时任务每小时都要这个数，所以一天只查一次。"""
        self.spend(ce_untag=10)
        for hour in range(6):
            self.run(NOW + timedelta(hours=hour))
        assert len(self.ce["calls"]) == 1

    def test_ce_is_queried_again_when_the_day_rolls_over(self):
        self.spend(ce_untag=10)
        self.run()
        self.run(NOW + timedelta(days=1))
        assert len(self.ce["calls"]) == 2

    def test_ce_and_cw_windows_meet_without_overlapping(self):
        """按 UTC 日期切：CE 到前天为止，CW 从昨天开始，中间不重不漏。"""
        self.spend(ce_untag=10)
        self.run()                                       # UTC 今天 = 09-27
        _, ce_start, ce_end = self.ce["calls"][0]
        _, cw_start, cw_end = self.cw["calls"][0]
        assert ce_start == date(2026, 8, 1)              # 台账的启用日期
        assert ce_end == date(2026, 9, 25)
        assert cw_start == ce_end + timedelta(days=1) == date(2026, 9, 26)
        assert cw_end == date(2026, 9, 27)

    def test_a_brand_new_account_uses_cw_only(self, ledger):
        """启用还不满两天：CE 那段是空的，整段都由 CW 估算。"""
        tg_ledger(ledger, {ALPHA: (True, CHAT_A)}, BUDGET={ALPHA: self.BUDGET},
                  START_DATE={ALPHA: date(2026, 9, 27)})
        self.run()
        assert self.ce["calls"] == []
        assert self.cw["calls"][0][1:] == (date(2026, 9, 27), date(2026, 9, 27))

    def test_a_ce_failure_skips_the_check(self):
        """缺了实账那段会严重低估，拿低估的数判阈值没意义，这一轮先不判。"""
        self.ce["error"] = "ThrottlingException"
        self.spend(ce_untag=900)
        summary = self.run()
        assert self.quota_texts() == []
        assert not summary.ok

    def test_a_cw_failure_skips_the_check(self):
        self.cw["recent_errors"] = ["us-east-1：boom"]
        self.spend(ce_untag=900)
        summary = self.run()
        assert self.quota_texts() == []
        assert not summary.ok

    def test_no_budget_means_no_percentage(self, ledger):
        tg_ledger(ledger, {ALPHA: (True, CHAT_A)}, BUDGET={ALPHA: 0})
        self.spend(ce_untag=900)
        self.run()
        assert self.quota_texts() == []

    def test_a_failed_send_is_retried(self, monkeypatch):
        self.spend(ce_untag=500)
        monkeypatch.setattr(alerts, "_send", failing("网络抖了"))
        self.run()
        assert load_state()[ALPHA].fired == []           # 没发出去就不算发过

        monkeypatch.setattr(alerts, "_send", self.sent.send)
        self.run(NOW + timedelta(hours=1))
        assert len(self.quota_texts()) == 1

    def test_message_explains_the_basis(self):
        self.spend(ce_untag=500)
        self.run()
        text = self.quota_texts()[0]
        assert "Cost Explorer 实账" in text and "CloudWatch" in text
        card = self.sent.cards[0][1]
        tiles = {tile.label: tile.value for tile in card.blocks[3].tiles}
        assert tiles == {"累计消费": "$525.00", "剩余额度": "$475.00"}


# ================================================================== 不带上游
class TestNoPartnerInMessages:
    """群里只看得到账号 ID：日报、用量切换、额度阈值都不带上游（台账的 PARTNER 列）。"""

    PARTNER = "UPSTREAM-SECRET"

    @pytest.fixture(autouse=True)
    def _setup(self, ledger, state_file, cw, ce, sent, fake_costs):
        tg_ledger(ledger, {ALPHA: (True, CHAT_A)}, PARTNER={ALPHA: self.PARTNER}, BUDGET={ALPHA: 1000.0})
        self.cw, self.ce, self.sent = cw, ce, sent

    def texts(self) -> list[str]:
        texts = [text for _, text in self.sent]
        assert texts, "这条用例应该有消息发出去"
        for text in texts:
            assert ALPHA in text                      # 账号 ID 照写
            assert self.PARTNER not in text
        return texts

    def test_daily(self):
        run_daily(date(2026, 8, 17), log=quiet)
        self.texts()

    def test_usage_started_and_stopped(self):
        for hour, calls in enumerate([0, 42, 0]):     # 基线 → 开始有用量 → 用量中断
            self.cw["invocations"][ALPHA] = [calls]
            run_hourly(NOW + timedelta(hours=hour), log=quiet)
        texts = self.texts()
        assert any("用量开始" in t for t in texts) and any("用量中断" in t for t in texts)

    def test_quota(self):
        self.cw["invocations"][ALPHA] = [1]
        self.ce["values"][ALPHA] = (0.0, 900.0)        # × UNTAG 比率 1.05 = 94.5%
        run_hourly(NOW, log=quiet)
        assert any("超过 90% 提醒线" in t for t in self.texts())


# ================================================================== 多个群
class TestMultipleChats:
    """一个账号可以发到多个群：每条消息发给它的全部群。"""

    CHAT_C = "-1003333333333"

    def test_daily_lists_the_account_in_every_one_of_its_chats(self, ledger, fake_costs, sent):
        tg_ledger(ledger, {ALPHA: (True, (CHAT_A, CHAT_B)), BETA: (True, CHAT_B)})
        run_daily(date(2026, 8, 17), log=quiet)
        by_chat = dict(sent)
        assert set(by_chat) == {CHAT_A, CHAT_B}
        assert ALPHA in by_chat[CHAT_A] and BETA not in by_chat[CHAT_A]
        assert ALPHA in by_chat[CHAT_B] and BETA in by_chat[CHAT_B]   # 同群的账号合成一张表

    def test_hourly_events_go_to_every_chat(self, ledger, state_file, cw, ce, sent):
        tg_ledger(ledger, {ALPHA: (True, (CHAT_A, CHAT_B, self.CHAT_C))})
        cw["invocations"][ALPHA] = [0]
        run_hourly(NOW, log=quiet)                              # 基线：无用量
        cw["invocations"][ALPHA] = [42]
        run_hourly(NOW + timedelta(hours=1), log=quiet)
        started = [chat for chat, text in sent if "用量开始" in text]
        assert started == [CHAT_A, CHAT_B, self.CHAT_C]
        assert len({id(card) for _, card in sent.cards}) == 1    # 同一张卡片，只画一次

    def test_one_broken_chat_does_not_cause_repeats_elsewhere(
        self, ledger, state_file, cw, ce, sent, monkeypatch
    ):
        """一个群 ID 坏了（bot 被踢了）不能拖住状态——否则好好的那几个群每小时都会
        再收到一遍，直到有人修好那个 ID。有一个群发成功就算发过了。"""
        tg_ledger(ledger, {ALPHA: (True, (CHAT_A, CHAT_B))})
        monkeypatch.setattr(alerts, "_send", failing("bot 已经被移出这个群了", only=CHAT_B, then=sent.send))
        cw["invocations"][ALPHA] = [0]
        run_hourly(NOW, log=quiet)
        cw["invocations"][ALPHA] = [42]
        summary = run_hourly(NOW + timedelta(hours=1), log=quiet)
        assert load_state()[ALPHA].active is True               # 状态前进了
        assert not summary.ok and "被移出" in summary.problems[0]  # 坏掉的那个报出来

        run_hourly(NOW + timedelta(hours=2), log=quiet)
        assert [chat for chat, text in sent if "用量开始" in text] == [CHAT_A]   # 没有重发

    def test_every_chat_failing_is_retried(self, ledger, state_file, cw, ce, sent, monkeypatch):
        """全部失败（Telegram 整个连不上、Token 失效）才不落状态，下一小时重试。"""
        tg_ledger(ledger, {ALPHA: (True, (CHAT_A, CHAT_B))})
        cw["invocations"][ALPHA] = [0]
        run_hourly(NOW, log=quiet)
        monkeypatch.setattr(alerts, "_send", failing("连不上 Telegram"))
        cw["invocations"][ALPHA] = [42]
        run_hourly(NOW + timedelta(hours=1), log=quiet)
        assert load_state()[ALPHA].active is False

    def test_thresholds_follow_the_same_rule(self, ledger, state_file, cw, ce, sent, monkeypatch):
        tg_ledger(ledger, {ALPHA: (True, (CHAT_A, CHAT_B))}, BUDGET={ALPHA: 1000.0})
        cw["invocations"][ALPHA] = [1]
        ce["values"][ALPHA] = (0.0, 500.0)                      # 52.5%
        monkeypatch.setattr(alerts, "_send", failing("群组 ID 不对", only=CHAT_B, then=sent.send))
        run_hourly(NOW, log=quiet)
        run_hourly(NOW + timedelta(hours=1), log=quiet)
        assert [chat for chat, text in sent if "超过 50% 提醒线" in text] == [CHAT_A]
        assert load_state()[ALPHA].fired == [50.0]


# ================================================================== 命令行
class TestCli:
    def test_test_needs_a_chat(self):
        from bedrock_cost.__main__ import main

        assert main(["alerts", "test"]) == 2

    def test_dry_run_exits_cleanly(self, ledger, fake_costs, capsys):
        from bedrock_cost.__main__ import main

        assert main(["alerts", "daily", "--dry-run"]) == 0

    def test_dry_run_can_save_the_cards(self, ledger, fake_costs, tmp_path, capsys):
        """服务器上没有屏幕：存成 PNG 拿下来看。只存图，不发、不改状态。"""
        from bedrock_cost.__main__ import main

        tg_ledger(ledger, {ALPHA: (True, CHAT_A)})
        out = tmp_path / "cards"
        assert main(["alerts", "daily", "--dry-run", "--save", str(out)]) == 0
        saved = sorted(out.iterdir())
        assert [p.name for p in saved] == [f"01-daily-{CHAT_A.lstrip('-')}.png"]
        assert saved[0].read_bytes().startswith(b"\x89PNG")
        assert "卡片存到了" in capsys.readouterr().out

    def test_save_needs_dry_run(self, tmp_path):
        from bedrock_cost.__main__ import main

        assert main(["alerts", "daily", "--save", str(tmp_path)]) == 2

    def test_problems_give_a_non_zero_exit(self, ledger, fake_costs, monkeypatch):
        """systemd 靠退出码判断成败，失败了 systemctl --failed 才看得到。"""
        from bedrock_cost.__main__ import main

        tg_ledger(ledger, {ALPHA: (True, CHAT_A)})
        monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "")
        assert main(["alerts", "daily"]) == 1

    def test_no_arguments_still_starts_the_web_server(self, monkeypatch):
        """systemd 的 bedrock.service 和 run.bat 都是不带参数调起的，行为不能变。"""
        from bedrock_cost import __main__ as entry

        called = []
        monkeypatch.setattr(entry, "_serve", lambda: called.append(1) or 0)
        assert entry.main([]) == 0
        assert called == [1]


# ================================================================== 发卡片
class TestSendCard:
    """alerts._send：画成图片用 sendPhoto 发；画不出来就退回发 caption 文字。"""

    @pytest.fixture
    def wire(self, monkeypatch):
        """Telegram 那一头：记下发了图片还是文字。"""
        box = {"photos": [], "texts": []}
        monkeypatch.setattr(telegram, "send_photo", lambda chat, png, caption: box["photos"].append((chat, png, caption)))
        monkeypatch.setattr(telegram, "send_message", lambda chat, text: box["texts"].append((chat, text)))
        return box

    @pytest.fixture
    def cannot_draw(self, monkeypatch):
        def boom(card):
            raise OSError("cannot open resource")        # 字体文件丢了时 Pillow 就这么报

        monkeypatch.setattr(cards, "render", boom)

    def test_sends_the_card_as_a_photo(self, wire):
        card = alerts.ping_card(ALPHA)
        assert alerts._send(CHAT_A, card) == ""
        chat, png, caption = wire["photos"][0]
        assert (chat, caption) == (CHAT_A, card.caption)
        assert png.startswith(b"\x89PNG")
        assert wire["texts"] == []

    def test_falls_back_to_the_caption_when_the_card_cannot_be_drawn(self, wire, cannot_draw):
        """告警不能因为画图丢掉：画不出来就只发图片下面那段文字。"""
        card = alerts.ping_card(ALPHA)
        note = alerts._send(CHAT_A, card)
        assert wire["photos"] == []
        assert wire["texts"] == [(CHAT_A, card.caption)]
        assert "卡片画不出来，改发了文字" in note and "cannot open resource" in note

    def test_the_fallback_still_counts_as_sent_but_is_reported(
        self, ledger, state_file, cw, ce, wire, cannot_draw, monkeypatch
    ):
        """发出去了，状态照常前进（不然下一小时会重发）；但命令非零退出，有人会去看。"""
        monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "t")
        tg_ledger(ledger, {ALPHA: (True, CHAT_A)})
        cw["invocations"][ALPHA] = [0]
        run_hourly(NOW, log=quiet)
        cw["invocations"][ALPHA] = [42]
        summary = run_hourly(NOW + timedelta(hours=1), log=quiet)
        assert "用量开始" in telegram.visible(wire["texts"][0][1])
        assert load_state()[ALPHA].active is True
        assert not summary.ok and "卡片画不出来" in summary.problems[0]

    def test_one_card_is_drawn_once_for_every_chat(self, wire, monkeypatch):
        drawn = []
        real = cards.render
        monkeypatch.setattr(cards, "render", lambda card: drawn.append(card) or real(card))
        card = alerts.ping_card(ALPHA)
        summary = alerts.RunSummary()
        alerts._deliver_all((CHAT_A, CHAT_B, "-1003333333333"), card, summary, alerts._send, quiet)
        assert len(wire["photos"]) == 3 and len(drawn) == 1

    def test_dry_run_draws_the_card_too(self, ledger, fake_costs, sent, cannot_draw):
        """dry-run 也真的画一遍：字体坏了在上线前就能看到。"""
        tg_ledger(ledger, {ALPHA: (True, CHAT_A)})
        summary = run_daily(date(2026, 8, 17), dry_run=True, log=quiet)
        assert not summary.ok and "卡片画不出来" in summary.problems[0]
        assert sent == []


# ================================================================== 账号变动通知
class TestNotifyAccount:
    @pytest.fixture
    def account(self, ledger):
        tg_ledger(ledger, {ALPHA: (True, (CHAT_A, CHAT_B))})
        return next(a for a in excel_source.load_accounts(force=True) if a.account == ALPHA)

    def test_goes_to_every_chat(self, account, sent):
        summary = alerts.notify_account("created", account, log=quiet)
        assert summary.ok and summary.sent == 2
        assert [(chat, card.kind) for chat, card in sent.cards] == [(CHAT_A, "created"), (CHAT_B, "created")]

    def test_skips_accounts_with_tg_off(self, ledger, sent):
        tg_ledger(ledger, {ALPHA: (False, CHAT_A)})
        account = next(a for a in excel_source.load_accounts(force=True) if a.account == ALPHA)
        assert alerts.notify_account("created", account, log=quiet) is None
        assert sent == []

    def test_disabled_reads_the_spend_like_the_overview(self, account, sent, fake_costs):
        alerts.notify_account("disabled", account, now=NOW, log=quiet)
        card = sent.cards[0][1]
        rows = {row.label: row.value for row in card.blocks[1].rows}
        from bedrock_cost.dates import cumulative_range

        from .conftest import expected_marked

        # fake CE 按天给数：区间是启用日期到 NOW 那一刻的本地日期，和概览页同一个口径
        start, end, _ = cumulative_range(account.start_date, NOW.astimezone().date())
        assert rows["停用前累计消费"] == f"${expected_marked(account, start, end):,.2f}"

    def test_unknown_event_is_a_bug(self, account, sent):
        with pytest.raises(ValueError):
            alerts.notify_account("renamed", account, log=quiet)
