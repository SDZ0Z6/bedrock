"""图例挑了几项时，纵轴在浏览器里重新算（app.js），算法要和服务端画图（chart.py）一模一样：
刻度取整（niceStep / _nice_step）、刻度上金额的写法（axisMoney / short_money）、次数的写法
（compactNumber / compact_number）、圆头柱子的路径（roundTop / _round_top）、线尾头像怎么错开
（endSpots / end_spots），以及段与段之间的缝、柱顶的圆角、线尾头像的半径和间距。

用 node 把 app.js 里那几个函数跑一遍对数；机器上没有 node 就跳过。
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from bedrock_cost import chart, config

APP_JS = Path(config.__file__).parent / "static" / "app.js"
START = "  const BAR_GAP = "      # app.js 里这几个函数从这一行起，到 easeInOut 之前
END = "  const easeInOut"

VALUES = [0, 0.01, 0.075, 0.2, 0.25, 0.8, 1, 2.5, 3.7, 12.5, 37.5, 99, 750, 999, 1000, 1500, 12_345, 99_999,
          100_000, 250_000, 999_999, 1e6, 2.5e6, 1.05e9, -1500,
          # 正好在一半上的：Python 取偶数那边（12.5 → 12、1.25K → 1.2K），JS 的 toFixed 往大的取，app.js 要照 Python
          0.125, 13.5, 999.5, 1250, 1_250_000]
ROUND_TOPS = [(10.5, 20.0, 8.0, 120.0, 4.0), (10.0, 100.0, 30.0, 100.5, 4.0), (0.0, 0.0, 96.0, 50.0, 4.0)]
# 线尾头像：(各条线末端的高低，从上到下排好, 顶, 底)——挨着的、挤出底的、一个的
SPOTS = [([40.0, 41.0, 60.0], 36.0, 276.0), ([250.0, 265.0, 270.0], 36.0, 276.0), ([100.0], 36.0, 276.0), ([], 36.0, 276.0)]


def helpers() -> str:
    source = APP_JS.read_text(encoding="utf-8")
    start, end = source.index(START), source.index(END)
    return source[start:end]


def test_缝_圆角_头像的半径和间距两边一样():
    match = re.search(r"const BAR_GAP = ([\d.]+), BAR_RADIUS = ([\d.]+), AVATAR_R = ([\d.]+), AVATAR_GAP = ([\d.]+);",
                      helpers())
    assert tuple(float(value) for value in match.groups()) == (
        chart.BAR_GAP, chart.BAR_RADIUS, chart.AVATAR_R, chart.AVATAR_GAP)


@pytest.mark.skipif(shutil.which("node") is None, reason="没有 node")
def test_刻度取整_写法_圆头路径_头像错开两边一样():
    script = helpers() + (
        "\nconst values = " + json.dumps(VALUES) + ";\n"
        "const rounds = " + json.dumps(ROUND_TOPS) + ";\n"
        "const spots = " + json.dumps(SPOTS) + ";\n"
        "console.log(JSON.stringify({ step: values.map(v => niceStep(v, 3)), step4: values.map(v => niceStep(v, 4)),"
        " money: values.map(v => axisMoney(v, '$')), count: values.map(v => compactNumber(v)),"
        " round: rounds.map(a => roundTop(...a)), spots: spots.map(a => endSpots(...a)) }));\n"
    )
    done = subprocess.run(["node", "-"], input=script, capture_output=True, text=True, encoding="utf-8",
                          timeout=30, check=True)
    js = json.loads(done.stdout)
    assert js["step"] == pytest.approx([chart._nice_step(v, 3) for v in VALUES], rel=1e-12)
    assert js["step4"] == pytest.approx([chart._nice_step(v, 4) for v in VALUES], rel=1e-12)
    assert js["money"] == [chart.short_money(v) for v in VALUES]
    assert js["count"] == [chart.compact_number(v) for v in VALUES]
    assert js["round"] == [chart._round_top(*args) for args in ROUND_TOPS]
    assert js["spots"] == [pytest.approx(chart.end_spots(*args)) for args in SPOTS]
