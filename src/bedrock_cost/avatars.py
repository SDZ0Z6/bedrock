"""账号头像：默认是账号邮箱的首字母，台账里填了别的就用它的第一个字。

颜色默认按邮箱（没填邮箱时按账号号码）算出来，八组底色里取一组（style.css 的
.av-0 … .av-7），所以同一个账号在概览、下拉框、账号页里永远是同一个颜色——
认熟了之后看颜色就知道是谁。台账里选了底色（AVATAR_COLOR）就用选的那组。

圆圈里只放一个字：不管台账里写的是一个表情、一个字母，还是一整个名字（「Johanna」），
都只取第一个字——一个「字」按人眼看到的算（带肤色的表情、国旗、ZWJ 拼起来的家庭表情
都是一个），不是按码位算。
"""

from __future__ import annotations

import re
import zlib
from dataclasses import dataclass

TONES = 8

# 跟在前一个字后面、和它拼成一个字的码位：组合附加符、变体选择符（FE0F 让符号显示成
# 彩色表情）、肤色、标签序列（英格兰旗那种）、键帽（1️⃣ 的那个框）
_EXTEND = re.compile(
    "[̀-ͯ᪰-᫿᷀-᷿⃐-⃿︀-️︠-︯"
    "\U0001f3fb-\U0001f3ff\U000e0020-\U000e007f]"
)
_ZWJ = "‍"


def _regional(char: str) -> bool:
    """国旗是两个「区域指示符」拼成的（🇨🇳 = 🇨 + 🇳）。"""
    return "\U0001f1e6" <= char <= "\U0001f1ff"


def first_grapheme(text: str) -> str:
    """去掉首尾空白之后的第一个字（按人眼看到的算）。空串返回空串。"""
    chars = list((text or "").strip())
    if not chars:
        return ""
    if _regional(chars[0]):
        return "".join(chars[:2]) if len(chars) > 1 and _regional(chars[1]) else chars[0]
    out = chars[0]
    index = 1
    while index < len(chars):
        char = chars[index]
        if _EXTEND.match(char):
            out += char
            index += 1
        elif char == _ZWJ and index + 1 < len(chars):
            # 👨‍👩‍👧 这种：ZWJ 把后面一个字也粘进来，后面可能还跟着肤色、变体选择符
            out += char + chars[index + 1]
            index += 2
        else:
            break
    return out


@dataclass(frozen=True)
class Avatar:
    text: str     # 一个字母，或者一个表情 / 符号
    tone: int     # 0 … TONES-1，对应 .av-N
    emoji: bool   # 不是字母或数字（表情、符号）：字号放大一点

    @property
    def css(self) -> str:
        return f"av-{self.tone}" + (" avatar-emoji" if self.emoji else "")


def tone_for(key: str) -> int:
    # crc32 只是用来把字符串均匀地散到几种颜色上，和安全无关
    return zlib.crc32(key.strip().lower().encode("utf-8")) % TONES


def _letter(char: str) -> str:
    """字母转大写；大写之后变成两个字的（ß → SS）就保持原样，圆圈里只放一个字。"""
    upper = char.upper()
    return upper if len(upper) == 1 else char


def avatar_for(
    email: str, number: str, partner: str = "", emoji: str = "", color: int | None = None
) -> Avatar:
    """email 是账号邮箱，number 是账号号码，partner 是上游；emoji、color 是台账里选的。

    颜色没选就按邮箱定，没邮箱按号码定；字取台账里填的那个的第一个字，没填就取邮箱首字母，
    没邮箱取上游的首字——号码的首位是个数字，认不出是谁。
    """
    tone = color if color is not None and 0 <= color < TONES else tone_for(email or number or partner or "?")
    chosen = first_grapheme(emoji)
    if chosen:
        if len(chosen) == 1 and chosen.isalnum():
            return Avatar(text=_letter(chosen), tone=tone, emoji=False)
        return Avatar(text=chosen, tone=tone, emoji=True)
    source = email or partner or number or "?"
    letter = next((ch for ch in source if ch.isalnum()), "?")
    return Avatar(text=_letter(letter), tone=tone, emoji=False)
