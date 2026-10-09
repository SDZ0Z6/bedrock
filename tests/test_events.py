"""告警事件流（events.py）：追加、读最近的、裁剪、坏文件、从不抛、不落秘密。

谁在什么时候记一条（真发了才记、dry-run 不记）在 test_alerts.py 和 test_mail.py。
文件路径由 conftest 的 _scratch_events 指到每个用例自己的临时目录。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from bedrock_cost import config, events
from bedrock_cost.events import Event, record, recent

UTC = timezone.utc
T0 = datetime(2026, 10, 9, 3, 0, tzinfo=UTC)


def lines() -> list[str]:
    return config.ALERT_EVENTS_PATH.read_text(encoding="utf-8").splitlines()


class TestRecordAndRecent:
    def test_round_trip(self):
        written = record(
            "stopped", "用量中断", "09-28 14:00 这一小时没有任何调用",
            tone="error", account="111111111111", email="alpha@example.com", groups=2, when=T0,
        )
        assert recent() == [written] == [Event(
            when=T0, kind="stopped", account="111111111111", email="alpha@example.com",
            title="用量中断", text="09-28 14:00 这一小时没有任何调用", tone="error", groups=2,
        )]

    def test_one_json_line_per_event(self):
        record("test", "测试消息", when=T0)
        record("daily", "Bedrock 日报", "发到 1 个群", when=T0 + timedelta(minutes=1))
        assert [json.loads(line)["kind"] for line in lines()] == ["test", "daily"]
        assert json.loads(lines()[0])["when"] == "2026-10-09T03:00:00+00:00"

    def test_newest_first_and_limited(self):
        for minute in range(5):
            record("started", "用量开始", str(minute), when=T0 + timedelta(minutes=minute))
        assert [e.text for e in recent()] == ["4", "3", "2", "1", "0"]
        assert [e.text for e in recent(limit=2)] == ["4", "3"]
        assert recent(limit=0) == []

    def test_ordered_by_time_even_if_written_out_of_order(self):
        """几个进程交错写：按时间排，同一时刻的后写的在前。"""
        record("a", "t", "later", when=T0 + timedelta(hours=1))
        record("b", "t", "earlier", when=T0)
        record("c", "t", "same time, written last", when=T0 + timedelta(hours=1))
        assert [e.text for e in recent()] == ["same time, written last", "later", "earlier"]

    def test_filter_by_kind(self):
        for kind in ("daily", "started", "mail-abuse", "started"):
            record(kind, "t", when=T0)
        assert [e.kind for e in recent(kinds=["started", "mail-abuse"])] == ["started", "mail-abuse", "started"]
        assert [e.kind for e in recent(kinds="daily")] == ["daily"]     # 一个字符串也认
        assert recent(kinds=[]) == []

    def test_when_is_stored_as_aware_utc(self):
        kl = timezone(timedelta(hours=8))
        record("test", "t", when=datetime(2026, 10, 9, 11, 0, tzinfo=kl))
        (event,) = recent()
        assert event.when == T0 and event.when.utcoffset() == timedelta(0)

    def test_when_defaults_to_now(self):
        before = datetime.now(UTC).replace(microsecond=0)
        written = record("test", "t")
        assert before <= written.when <= datetime.now(UTC)
        assert recent() == [written]               # 精确到秒：返回的这条和读回来的一样

    def test_explicit_path(self, tmp_path):
        other = tmp_path / "elsewhere" / "events.jsonl"
        record("test", "t", when=T0, path=other)
        assert [e.kind for e in recent(path=other)] == ["test"]
        assert recent() == []                     # 默认的那个文件没动


class TestCleaning:
    def test_unknown_tone_becomes_info(self):
        record("test", "t", tone="danger")
        assert recent()[0].tone == "info"

    def test_text_is_one_short_line(self):
        record("test", "t", "第一行\n  第二行\t第三行")
        assert recent()[0].text == "第一行 第二行 第三行"
        record("test", "t", "长" * 500)
        text = recent()[0].text
        assert len(text) == events.MAX_TEXT and text.endswith("…")

    def test_negative_groups_become_zero(self):
        record("test", "t", groups=-3)
        assert recent()[0].groups == 0

    def test_secrets_never_reach_the_file(self):
        """看板给所有登录的人看：长得像 AK、Bot Token 的字符串先打码再落盘。"""
        key, token = "AKIAABCDEFGHIJKLMNOP", "123456789:AAH1bc2DEF3ghi4JKL5mno6PQR7stu8VWX9"
        record("test", f"key {key}", f"token {token} and key {key}")
        raw = config.ALERT_EVENTS_PATH.read_text(encoding="utf-8")
        assert key not in raw and token not in raw
        assert "AKIAABCD…MNOP" in raw and "<TOKEN>" in raw

    def test_masked_keys_are_left_alone(self):
        """卡片上已经打过码的（AKIAFAKE…1234）原样留着。"""
        record("mail-compromised", "t", "涉及的密钥 AKIAFAKE…1234")
        assert recent()[0].text == "涉及的密钥 AKIAFAKE…1234"


class TestBadFiles:
    def test_missing_file_is_empty(self):
        assert not config.ALERT_EVENTS_PATH.exists()
        assert recent() == []

    def test_corrupt_lines_are_skipped(self):
        good = {"when": "2026-10-09T03:00:00+00:00", "kind": "started", "account": "1", "email": "",
                "title": "用量开始", "text": "x", "tone": "ok", "groups": 1}
        config.ALERT_EVENTS_PATH.write_text(
            "\n".join([
                "{ not json",
                json.dumps(good, ensure_ascii=False),
                "",
                "[1, 2, 3]",
                json.dumps({"kind": "started"}),                                  # 没有时间
                json.dumps({**good, "when": "yesterday"}),                        # 时间读不懂
                json.dumps({**good, "kind": ""}),                                 # 没有类别
                json.dumps({**good, "when": "2026-10-09T04:00:00", "groups": "2", "tone": "nope",
                            "text": 7, "from_the_future": True}),                 # 能救的就救
                '{"when": "2026-10-09T05:00:00+00:00", "kind": "sto',            # 写到一半断电
                "[" * 100_000,                                                   # 嵌套到递归溢出
            ]),
            encoding="utf-8",
        )
        found = recent()
        assert [(e.kind, e.when.hour) for e in found] == [("started", 4), ("started", 3)]
        salvaged = found[0]
        assert salvaged.when.tzinfo is not None                # 没带时区的当 UTC
        assert (salvaged.groups, salvaged.tone, salvaged.text) == (0, "info", "")

    def test_a_half_written_last_line_does_not_swallow_the_next_event(self):
        config.ALERT_EVENTS_PATH.write_text('{"when": "2026-10-09T03:00:00+00:00", "ki', encoding="utf-8")
        record("test", "t", "after the crash", when=T0)
        assert [e.text for e in recent()] == ["after the crash"]

    def test_unreadable_file_is_empty(self, monkeypatch, tmp_path):
        folder = tmp_path / "a-folder"
        folder.mkdir()
        monkeypatch.setattr(config, "ALERT_EVENTS_PATH", folder)
        assert recent() == []


class TestNeverRaises:
    def test_a_failed_write_is_logged_not_raised(self, monkeypatch, tmp_path, caplog):
        """告警已经发出去了：流水写不进去只记一条日志，调用方什么都感觉不到。"""
        folder = tmp_path / "a-folder"            # 路径是个目录：打不开、写不进
        folder.mkdir()
        monkeypatch.setattr(config, "ALERT_EVENTS_PATH", folder)
        with caplog.at_level("WARNING", logger="bedrock_cost.events"):
            assert record("test", "t") is None
        assert "告警事件没有记下来" in caplog.text

    def test_nonsense_arguments_are_logged_not_raised(self, caplog):
        with caplog.at_level("WARNING", logger="bedrock_cost.events"):
            assert record("test", "t", groups="many") is None
        assert recent() == []


class TestTrim:
    @pytest.fixture(autouse=True)
    def _small(self, monkeypatch):
        monkeypatch.setattr(events, "MAX_LINES", 5)
        monkeypatch.setattr(events, "KEEP_LINES", 3)

    def test_keeps_the_newest_lines(self):
        for n in range(5):
            record("test", "t", str(n), when=T0 + timedelta(minutes=n))
        assert len(lines()) == 5                             # 还没超
        record("test", "t", "5", when=T0 + timedelta(minutes=5))
        assert len(lines()) == 3
        assert [e.text for e in recent()] == ["5", "4", "3"]

    def test_keeps_appending_after_a_trim(self):
        for n in range(8):
            record("test", "t", str(n), when=T0 + timedelta(minutes=n))
        assert len(lines()) <= 5
        assert recent()[0].text == "7"

    def test_no_temp_file_left_behind(self):
        for n in range(7):
            record("test", "t", str(n), when=T0 + timedelta(minutes=n))
        assert [p.name for p in config.ALERT_EVENTS_PATH.parent.iterdir()] == ["alert-events.jsonl"]


class TestConfig:
    def test_the_file_stays_out_of_git(self):
        """运行时的流水，和 alert-state.json 一样不进版本库。"""
        root = Path(__file__).resolve().parents[1]
        assert "alert-events.jsonl" in (root / ".gitignore").read_text(encoding="utf-8").split()
        assert "ALERT_EVENTS_PATH=alert-events.jsonl" in (root / ".env.example").read_text(encoding="utf-8")

    def test_tests_never_touch_the_real_file(self, tmp_path):
        assert config.ALERT_EVENTS_PATH == tmp_path / "alert-events.jsonl"
