"""告警卡片：Card 模型和把它画成 PNG 的 render()。

alerts 怎么拼卡片、发给谁在 test_alerts.py；这里只管「画出来对不对」：尺寸在
Telegram 的限制之内、边框颜色跟着卡片的语气走、折行不把英文单词劈开、字体跟着包走。
"""

from __future__ import annotations

import io
from datetime import date, datetime, timezone
from pathlib import Path

import pytest
from PIL import Image

from bedrock_cost import alerts, cards, config, telegram
from bedrock_cost.cards import Card, _font, _wrap
from bedrock_cost.cost_explorer import CostSplit
from bedrock_cost.dates import CUMULATIVE_OK, cumulative_range
from bedrock_cost.excel_source import Account
from bedrock_cost.report import build_row

UTC = timezone.utc
TODAY = date(2026, 9, 28)
JEFF = Account(
    partner="上游", account="120121147269", budget=1000, tag_ratio=1.0, untag_ratio=1.0,
    start_date=date(2026, 8, 1),
)


def picture(card: Card) -> Image.Image:
    return Image.open(io.BytesIO(card.png()))


def row(account: Account, untag_raw: float = 0.0, error: str | None = None):
    split = CostSplit(error=error) if error else CostSplit(untag_raw=untag_raw)
    return build_row(account, split, cumulative_range(account.start_date, TODAY))


def every_card() -> dict[str, Card]:
    """每一种卡片各一张，示例数字。"""
    return {
        "daily": alerts.daily_cards(TODAY, [row(JEFF, 41.27)])[0],
        "started": alerts.started_card(JEFF, datetime(2026, 9, 28, 5, tzinfo=UTC), 1234),
        "stopped": alerts.stopped_card(JEFF, [datetime(2026, 9, 28, 6, tzinfo=UTC)], "2026-09-28T05:00:00+00:00"),
        "quota": alerts.quota_card(JEFF, 50, 50.3, 503.2, CUMULATIVE_OK, JEFF.start_date, []),
        "test": alerts.ping_card(JEFF.account),
    }


def local(stamp: datetime, pattern: str = "%m-%d %H:%M") -> str:
    """卡片上的时间是服务器本地时间：按跑测试这台机器的时区算期望值，不写死 UTC+8。"""
    return stamp.astimezone().strftime(pattern)


def distance(a, b) -> float:
    return sum((x - y) ** 2 for x, y in zip(a, b)) ** 0.5


class TestRender:
    @pytest.mark.parametrize("kind", ["daily", "started", "stopped", "quota", "test"])
    def test_every_card_is_a_png_telegram_accepts(self, kind):
        """Telegram 要求宽 + 高 ≤ 10000、长宽比 ≤ 20；宽 1200 在普通画质的 1280 以内，不会再被压。"""
        image = picture(every_card()[kind])
        width, height = image.size
        assert image.format == "PNG"
        assert width == cards.WIDTH * cards.OUTPUT_SCALE == 1200
        assert 400 < height < 3000
        assert width + height <= 10000 and height / width <= 20

    @pytest.mark.parametrize("kind, tone", [("started", "ok"), ("stopped", "danger"), ("quota", "warn")])
    def test_the_border_takes_the_card_colour(self, kind, tone):
        image = picture(every_card()[kind]).convert("RGB")
        x = round((cards.MARGIN + 0.75) * cards.OUTPUT_SCALE)          # 左边框的正中
        assert distance(image.getpixel((x, image.height // 2)), cards.TONES[tone].edge) < 60

    def test_outside_the_card_is_black(self):
        image = picture(every_card()["daily"]).convert("RGB")
        assert image.getpixel((2, 2)) == (0, 0, 0)

    def test_a_card_is_drawn_only_once(self, monkeypatch):
        """同一张卡片发给几个群，只画一次。"""
        drawn = []
        real = cards.render
        monkeypatch.setattr(cards, "render", lambda card: drawn.append(1) or real(card))
        card = alerts.ping_card("")
        assert card.png() is card.png()
        assert drawn == [1]

    def test_more_rows_make_a_taller_card(self):
        one = picture(alerts.daily_cards(TODAY, [row(JEFF, 1)])[0]).height
        five = picture(alerts.daily_cards(TODAY, [row(JEFF, 1)] * 5)[0]).height
        assert five - one == 4 * cards.Table.ROW * cards.OUTPUT_SCALE

    def test_a_full_daily_card_still_fits(self):
        """放满 MAX_TABLE_ROWS 个账号、每个都超额带一条说明，图和 caption 都还在 Telegram 的限制里。"""
        card = alerts.daily_cards(TODAY, [row(JEFF, 2000)] * cards.MAX_TABLE_ROWS)[0]
        width, height = picture(card).size
        assert width + height <= 10000
        assert len(telegram.visible(card.caption)) <= telegram.MAX_CAPTION_CHARS

    def test_a_caption_that_would_be_too_long_is_cut(self, monkeypatch):
        monkeypatch.setattr(cards, "MAX_TABLE_ROWS", 40)
        card = alerts.daily_cards(TODAY, [row(JEFF, 2000)] * 40)[0]
        shown = telegram.visible(card.caption)
        assert len(shown) <= telegram.MAX_CAPTION_CHARS
        assert shown.endswith("……其余账号见图片")

    def test_long_notes_wrap_instead_of_running_off_the_card(self):
        error = "An error occurred (AccessDeniedException) when calling the GetCostAndUsage operation: " * 3
        short = alerts.daily_cards(TODAY, [row(JEFF, error="x")])[0]
        long = alerts.daily_cards(TODAY, [row(JEFF, error=error)])[0]
        assert picture(long).height > picture(short).height

    def test_every_card_is_signed(self, monkeypatch):
        monkeypatch.setattr(config, "TELEGRAM_CARD_SIGNATURE", "signed by the test")
        for card in every_card().values():
            assert card.signature == "signed by the test"
            assert card.text().endswith("signed by the test")

    def test_the_signature_can_be_switched_off(self, monkeypatch):
        """.env 里写 TELEGRAM_CARD_SIGNATURE= 就不画这一行。"""
        hour = datetime(2026, 9, 28, 5, tzinfo=UTC)
        monkeypatch.setattr(config, "TELEGRAM_CARD_SIGNATURE", "signed")
        signed = picture(alerts.started_card(JEFF, hour, 1)).height
        monkeypatch.setattr(config, "TELEGRAM_CARD_SIGNATURE", "")
        card = alerts.started_card(JEFF, hour, 1)
        assert "signed" not in card.text()
        assert picture(card).height == signed - 20 * cards.OUTPUT_SCALE

    def test_an_unknown_icon_is_a_bug_not_a_blank(self):
        card = alerts.ping_card("")
        card.icon = "no-such-icon"
        with pytest.raises(ValueError, match="no-such-icon"):
            cards.render(card)


class TestCardText:
    def test_has_the_caption_and_everything_drawn(self):
        """dry-run 打印、测试断言都靠它：卡片上画的每一个字都在。"""
        text = every_card()["stopped"].text()
        for piece in (
            "用量中断", "INTERRUPTED", "BEDROCK · USAGE ALERT", "账号 UID", "120121147269",
            "当前检测时段", local(datetime(2026, 9, 28, 6, tzinfo=UTC)), "本小时没有任何调用", "上一次有调用",
            "请检查账号调用情况及相关服务状态。",
        ):
            assert piece in text
        assert "<code>" not in text              # caption 按 Telegram 显示出来的样子


class TestWrap:
    def test_chinese_can_break_anywhere(self):
        text = "台账未填启用日期，按最早可查日起算" * 6
        lines = _wrap(text, _font(14), 200)
        assert len(lines) > 1 and "".join(lines) == text
        assert all(_font(14).getlength(line) <= 200 * cards._K for line in lines)

    def test_english_words_are_not_split(self):
        # 行宽放得下最长的那个词（模型 ID），但放不下整句
        text = "anthropic.claude-fable-5-1-v1:0 was not found in the price table at all"
        words = set(text.split())
        lines = _wrap(text, _font(14), 300)
        assert len(lines) > 1
        for line in lines:
            assert set(line.split()) <= words

    def test_a_word_longer_than_the_line_is_cut(self):
        arn = "arn:aws:bedrock:us-east-1:123456789012:application-inference-profile/" + "x" * 60
        lines = _wrap(arn, _font(14), 200)
        assert len(lines) > 1 and "".join(lines) == arn

    def test_newlines_are_kept(self):
        assert _wrap("第一行\n第二行", _font(14), 500) == ["第一行", "第二行"]

    def test_an_opening_bracket_never_ends_a_line(self):
        """「权限（」换行、括号里的字跑到下一行很难看：左括号跟着内容挪下去。"""
        text = "626052499798 今天查询失败：凭证缺少 ce:GetCostAndUsage 权限（AccessDeniedException）。"
        font = _font(14)
        width = font.getlength(text[: text.index("（") + 1]) / cards._K + 1   # 刚好放得下「……权限（」
        lines = _wrap(text, font, width)
        assert "".join(lines) == text                        # 一个字都没丢
        assert not any(line.endswith("（") for line in lines)
        assert lines[1].startswith("（AccessDeniedException")

    def test_no_lonely_last_character(self):
        """最后一行不会只剩「能。」这样一两个字：从上一行挪两个字下来。"""
        text = "这是一条 Bedrock 监控系统的测试消息，用于验证 Telegram Bot 的消息推送功能。"
        font = _font(15)
        # 找一个刚好会把最后两个字挤到下一行的宽度
        width = font.getlength(text[:-2]) / cards._K + 1
        lines = _wrap(text, font, width)
        assert "".join(lines) == text
        assert len(lines[-1]) >= 4


class TestFonts:
    def test_fonts_ship_with_the_package(self):
        """不用系统字体：服务器上什么中文字体都没装也画得出来。"""
        assert cards.SANS_FONT.is_file() and cards.MONO_FONT.is_file()
        for name in ("OFL-NotoSansSC.txt", "OFL-JetBrainsMono.txt"):
            assert "SIL Open Font License" in (cards.FONT_DIR / name).read_text(encoding="utf-8")

    def test_pyproject_packages_them(self):
        pyproject = (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text(encoding="utf-8")
        assert '"fonts/*"' in pyproject
        assert '"Pillow' in pyproject

    def test_bold_is_really_bold(self):
        """可变字体要真的切到粗体：同样的英文和数字，粗体更宽。（汉字不管粗细都是一个字宽，比不出来）"""
        sample = "DAILY REPORT $1,234.56"
        assert _font(20, 700).getlength(sample) > _font(20, 400).getlength(sample)


class TestMoney:
    @pytest.mark.parametrize(
        "value, shown",
        [(1234.5, "$1,234.50"), (0, "$0.00"), (-12.3, "-$12.30"), (-0.001, "$0.00")],
    )
    def test_minus_goes_before_the_dollar(self, value, shown):
        assert alerts._money(value) == shown


class TestPingCard:
    """测试消息照截图：蓝框、试管图标、「Telegram Bot 运行正常」、三行详情。"""

    NOW = datetime(2026, 9, 28, 9, tzinfo=UTC)

    def test_matches_the_screenshot(self):
        card = alerts.ping_card("120121147269", self.NOW)
        assert (card.tone, card.icon, card.badge) == ("info", "test-tube", "TEST")
        assert card.subtitle == "BEDROCK · BOT NOTIFICATION TEST"
        status, _, paragraph, details = card.blocks
        assert (status.title, status.detail) == ("Telegram Bot 运行正常", "测试消息已成功触发")
        assert paragraph.label == "测试内容"
        assert [(row.label, row.value) for row in details.rows] == [
            ("监控服务", "AWS Bedrock"),
            ("消息推送", "正常"),
            ("测试时间", local(self.NOW, "%Y-%m-%d %H:%M")),
        ]
        assert not card.footer_rule

    def test_the_account_is_only_in_the_caption(self):
        """截图的卡片上没有账号；账号 ID 写在图片下面，群里搜得到。"""
        card = alerts.ping_card("120121147269", self.NOW)
        drawn = [s for block in card.blocks for s in block.strings()]
        assert "120121147269" not in drawn
        assert "120121147269" in card.caption

    def test_renders(self):
        assert picture(alerts.ping_card("", self.NOW)).width == 1200


class TestAccountCards:
    NOW = datetime(2026, 9, 28, 9, tzinfo=UTC)

    def test_created(self):
        card = alerts.created_card(JEFF, self.NOW)
        assert (card.kind, card.tone, card.badge, card.title) == ("created", "ok", "ACTIVATED", "新账号启用")
        details = card.blocks[1]
        assert details.uid == "120121147269"
        assert [(r.label, r.value) for r in details.rows] == [
            ("账号状态", "运行中"), ("授信额度", "$1,000.00"), ("启用时间", local(self.NOW, "%Y-%m-%d %H:%M")),
        ]
        assert card.footer == "系统已开始监控该账号的用量及额度情况。"

    def test_disabled_shows_the_spend_so_far(self):
        card = alerts.disabled_card(JEFF, row(JEFF, 503.2), self.NOW)
        assert (card.kind, card.tone, card.badge) == ("disabled", "gray", "DEACTIVATED")
        rows = {r.label: r.value for r in card.blocks[1].rows}
        assert rows["账号状态"] == "已停用"
        assert rows["停用前累计消费"] == "$503.20" and rows["停用前剩余额度"] == "$496.80"

    def test_disabled_says_so_when_the_spend_cannot_be_read(self):
        card = alerts.disabled_card(JEFF, row(JEFF, error="AccessDenied"), self.NOW)
        rows = {r.label: (r.value, r.tone) for r in card.blocks[1].rows}
        assert rows["停用前累计消费"] == ("查询失败", "danger")
        assert "停用前剩余额度" not in rows

    def test_restored(self):
        card = alerts.restored_card(JEFF, self.NOW)
        assert (card.kind, card.badge, card.title) == ("restored", "REACTIVATED", "账号恢复启用")
        assert [r.label for r in card.blocks[1].rows] == ["账号状态", "授信额度", "恢复时间"]

    @pytest.mark.parametrize("build", [alerts.created_card, alerts.restored_card])
    def test_render(self, build):
        assert picture(build(JEFF, self.NOW)).width == 1200

    def test_disabled_renders(self):
        assert picture(alerts.disabled_card(JEFF, row(JEFF, 1), self.NOW)).width == 1200


class TestTableNotes:
    def test_a_note_under_a_cell_makes_that_row_taller(self):
        """「截至 09-28」这种小字放在值下面，这一行要长高，别的行不变。"""
        columns = ["UID", "授信额度", "累计消费", "剩余额度"]
        plain = [cards.Cell("111111111111"), cards.Cell("$1"), cards.Cell("$2"), cards.Cell("$3")]
        noted = [cards.Cell("222222222222"), cards.Cell("$1"), cards.Cell("$2", note="截至 09-28"), cards.Cell("$3")]
        table = cards.Table(columns, [plain, noted])
        height = table.layout(cards._Pen(), 0, 0, 500, cards.TONES["ok"])
        assert height == cards.Table.HEAD + cards.Table.ROW + cards.Table.NOTED_ROW
        assert "截至 09-28" in table.strings()
