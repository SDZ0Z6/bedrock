"""源码要能在 Python 3.10 / 3.11 上跑（pyproject 的 requires-python >= 3.10，线上服务器是 3.11）。

开发机是 3.12+，PEP 701 以后 f-string 宽松了很多，写着顺手的几种写法在 3.11 上直接 SyntaxError，
整个服务起不来（上过一次线：chart.py 里 f-string 的花括号中带了 `\\"`）。3.12+ 的 ast.parse 带
feature_version 也查不出这一类，所以这里按 token 扫：f-string 的花括号里（表达式部分）

  · 不能有反斜杠（包括里面字符串的转义）；
  · 不能用和外面同一种引号（外面是三引号的除外）；
  · 不能有注释；
  · 单引号的 f-string，表达式不能跨行。

在 3.10 / 3.11 上跑这个用例时没有 FSTRING_* 这几种 token，就直接 compile 每个文件——那时 compile 本身就会报错。
"""

from __future__ import annotations

import io
import sys
import tokenize
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SOURCES = sorted([*(ROOT / "src").rglob("*.py"), *(ROOT / "tests").rglob("*.py")])
PREFIX = "rRbBuUfF"


def _quote(text: str) -> str:
    body = text.lstrip(PREFIX)
    return body[:3] if body[:3] in ('"""', "'''") else body[:1]


def problems(source: str) -> list[str]:
    """3.12 以前不认的 f-string 写法：[「第几行：什么问题」]。"""
    found: list[str] = []
    stack: list[dict] = []          # 正在里面的 f-string：外层引号、花括号深度
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        kind, text, (row, _), _, _ = token
        name = tokenize.tok_name[kind]
        if name == "FSTRING_START":
            if stack and stack[-1]["braces"] > 0:
                outer = stack[-1]["quote"]
                if len(outer) == 1 and _quote(text) == outer:
                    found.append(f"{row}: f-string 里套 f-string 用了同一种引号 {outer}")
            stack.append({"quote": _quote(text), "braces": 0})
            continue
        if name == "FSTRING_END":
            stack.pop()
            continue
        if not stack or name == "FSTRING_MIDDLE":
            continue
        top = stack[-1]
        if name == "OP" and text == "{":
            top["braces"] += 1
        elif name == "OP" and text == "}":
            top["braces"] -= 1
        if top["braces"] <= 0:
            continue
        if "\\" in text:
            found.append(f"{row}: f-string 的花括号里有反斜杠：{text}")
        if name == "STRING" and len(top["quote"]) == 1 and _quote(text) == top["quote"]:
            found.append(f"{row}: f-string 的花括号里用了和外面一样的引号：{text}")
        if name == "COMMENT":
            found.append(f"{row}: f-string 的花括号里有注释")
        if name == "NL" and len(top["quote"]) == 1:
            found.append(f"{row}: 单引号 f-string 的表达式跨行了")
    return found


@pytest.mark.parametrize("path", SOURCES, ids=lambda p: str(p.relative_to(ROOT)))
def test_f_strings_parse_on_python_310(path):
    source = path.read_text(encoding="utf-8")
    if sys.version_info < (3, 12):
        compile(source, str(path), "exec")
        return
    assert problems(source) == []


def test_the_checker_catches_what_broke_production():
    bad = 'x = f\'<text{"" if ok else " display=\\"none\\""}>\'\n'
    assert problems(bad) and "反斜杠" in problems(bad)[0]
    assert problems('d = {}\nx = f"{d["k"]}"\n')
    assert problems('x = f"{1 + 2}"\ny = f"""{"same quote is fine in triple"}"""\n') == []
