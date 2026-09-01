"""校验图表分类色板。

chart.py 的模块文档一直引用这个脚本，但它此前没进过仓库。2026-09 改浅色主题时
补上了——换底色必须重跑，否则「已验证」只是一句没人能复核的话。

跑法：

    python scripts/validate_palette.py

四项检查，全部针对 chart.SURFACE 这个实际卡片底色：

1. **对比度** —— 每个色对底色 ≥ 3:1。柱子是图形元素，WCAG 的门槛是 3:1 而不是
   文字的 4.5:1。
2. **亮度带** —— 分类色板要的是亮度**接近**，不是拉开。亮度有梯度会让人读出
   并不存在的大小顺序（那是连续型色板才该有的）。所以这里查的是整条色板的
   L* 跨度够不够窄。
3. **色度下限** —— C* 太低的色会显得脏，和中性灰的「其他」撞。
4. **色觉障碍分离度** —— 在红色盲、绿色盲、蓝色盲三种模拟下算 ΔE2000。
   这是最容易被忽略、也最容易出事的一项：一张图里两条线在色觉正常的人眼里
   分得开，在 8% 的男性眼里可能完全一样。

关于第 4 项的门槛：这套色相是项目原有的，本来就不是为色觉障碍优化的
（红色盲下蓝与紫只差 ΔE 1.8）。**这个脚本不负责把它改好**——那是另一件事，
会动到所有人已经习惯的颜色。它只负责守住「不要更差」：门槛取自改动前的实测值，
所以任何让分离度倒退的改色都会被拦下来。

只有第 1 项是硬性失败——它有 WCAG 的客观门槛，也是换底色时唯一真正会变的量。
其余三项报数值并对照基线，用来防退化。

ΔE 用 CIEDE2000；色觉模拟用 Machado 2009 的矩阵（severity = 1.0），作用在
线性 RGB 上。
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from bedrock_cost.chart import OTHER_COLOR, SERIES_COLORS, SURFACE  # noqa: E402

MIN_CONTRAST = 3.0        # WCAG 图形元素门槛，硬性
MIN_CHROMA = 10.0         # 低于这个会显得发灰（「其他」是中性色，不适用）
COMFORTABLE = 10.0        # ΔE 到这个量级两个色已经明显不同，小幅漂移无所谓
MAX_LIGHTNESS_SPAN = 14.0 # 整条色板的 L* 跨度上限：分类色板要平，不要有梯度。
                          # 本项目这套色相一直在 13 上下，取 14 是给它留的余量

# 色觉障碍下两两 ΔE2000 的基线，取自改浅色主题之前的实测值。
# 只用来防退化，不是「达标线」——这套色相本来就没为色觉障碍优化过。
CVD_BASELINE = {"常视觉": 13.9, "红色盲": 1.8, "绿色盲": 3.9, "蓝色盲": 5.6}

# Machado 2009，severity 1.0，作用于线性 RGB
CVD = {
    "红色盲": (
        (0.152286, 1.052583, -0.204868),
        (0.114503, 0.786281, 0.099216),
        (-0.003882, -0.048116, 1.051998),
    ),
    "绿色盲": (
        (0.367322, 0.860646, -0.227968),
        (0.280085, 0.672501, 0.047413),
        (-0.011820, 0.042940, 0.968881),
    ),
    "蓝色盲": (
        (1.255528, -0.076749, -0.178779),
        (-0.078411, 0.930809, 0.147602),
        (0.004733, 0.691367, 0.303900),
    ),
}


def to_rgb(value: str) -> tuple[float, float, float]:
    h = value.lstrip("#")
    return tuple(int(h[i : i + 2], 16) / 255 for i in (0, 2, 4))  # type: ignore[return-value]


def to_linear(c: float) -> float:
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def linear_rgb(value: str) -> tuple[float, float, float]:
    return tuple(to_linear(c) for c in to_rgb(value))  # type: ignore[return-value]


def relative_luminance(value: str) -> float:
    r, g, b = linear_rgb(value)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(a: str, b: str) -> float:
    la, lb = relative_luminance(a), relative_luminance(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def to_lab(value: str) -> tuple[float, float, float]:
    r, g, b = linear_rgb(value)
    # sRGB -> XYZ (D65)
    x = (0.4124564 * r + 0.3575761 * g + 0.1804375 * b) / 0.95047
    y = 0.2126729 * r + 0.7151522 * g + 0.0721750 * b
    z = (0.0193339 * r + 0.1191920 * g + 0.9503041 * b) / 1.08883

    def f(t: float) -> float:
        return t ** (1 / 3) if t > 216 / 24389 else (841 / 108) * t + 4 / 29

    fx, fy, fz = f(x), f(y), f(z)
    return 116 * fy - 16, 500 * (fx - fy), 200 * (fy - fz)


def delta_e(one: str, two: str) -> float:
    """CIEDE2000。"""
    l1, a1, b1 = to_lab(one)
    l2, a2, b2 = to_lab(two)

    avg_l = (l1 + l2) / 2
    c1, c2 = math.hypot(a1, b1), math.hypot(a2, b2)
    avg_c = (c1 + c2) / 2
    g = 0.5 * (1 - math.sqrt(avg_c**7 / (avg_c**7 + 25**7))) if avg_c else 0.0

    a1p, a2p = a1 * (1 + g), a2 * (1 + g)
    c1p, c2p = math.hypot(a1p, b1), math.hypot(a2p, b2)
    avg_cp = (c1p + c2p) / 2

    h1p = math.degrees(math.atan2(b1, a1p)) % 360
    h2p = math.degrees(math.atan2(b2, a2p)) % 360

    if c1p * c2p == 0:
        dhp = 0.0
    elif abs(h2p - h1p) <= 180:
        dhp = h2p - h1p
    else:
        dhp = h2p - h1p - 360 if h2p > h1p else h2p - h1p + 360

    dlp = l2 - l1
    dcp = c2p - c1p
    dhp_final = 2 * math.sqrt(c1p * c2p) * math.sin(math.radians(dhp) / 2)

    if c1p * c2p == 0:
        avg_hp = h1p + h2p
    elif abs(h1p - h2p) <= 180:
        avg_hp = (h1p + h2p) / 2
    elif h1p + h2p < 360:
        avg_hp = (h1p + h2p + 360) / 2
    else:
        avg_hp = (h1p + h2p - 360) / 2

    t = (
        1
        - 0.17 * math.cos(math.radians(avg_hp - 30))
        + 0.24 * math.cos(math.radians(2 * avg_hp))
        + 0.32 * math.cos(math.radians(3 * avg_hp + 6))
        - 0.20 * math.cos(math.radians(4 * avg_hp - 63))
    )
    sl = 1 + (0.015 * (avg_l - 50) ** 2) / math.sqrt(20 + (avg_l - 50) ** 2)
    sc = 1 + 0.045 * avg_cp
    sh = 1 + 0.015 * avg_cp * t
    rt = (
        -2
        * math.sqrt(avg_cp**7 / (avg_cp**7 + 25**7))
        * math.sin(math.radians(60 * math.exp(-(((avg_hp - 275) / 25) ** 2))))
    )
    return math.sqrt(
        (dlp / sl) ** 2
        + (dcp / sc) ** 2
        + (dhp_final / sh) ** 2
        + rt * (dcp / sc) * (dhp_final / sh)
    )


def simulate(value: str, kind: str) -> str:
    """模拟某种色觉障碍下看到的颜色。"""
    matrix = CVD[kind]
    lin = linear_rgb(value)
    out = []
    for row in matrix:
        mixed = sum(row[i] * lin[i] for i in range(3))
        mixed = max(0.0, min(1.0, mixed))
        srgb = 12.92 * mixed if mixed <= 0.0031308 else 1.055 * mixed ** (1 / 2.4) - 0.055
        out.append(round(srgb * 255))
    return "#%02x%02x%02x" % tuple(out)


def main() -> int:
    palette = list(SERIES_COLORS) + [OTHER_COLOR]
    names = [f"{i}" for i in range(1, len(SERIES_COLORS) + 1)] + ["其他"]
    failures: list[str] = []
    warnings: list[str] = []

    print(f"卡片底色 {SURFACE}，共 {len(palette)} 个色\n")

    print("1) 对比度（≥ %.1f）" % MIN_CONTRAST)
    for name, value in zip(names, palette):
        ratio = contrast(value, SURFACE)
        ok = ratio >= MIN_CONTRAST
        print(f"   {name:<4}{value}  {ratio:5.2f}  {'PASS' if ok else 'FAIL'}")
        if not ok:
            failures.append(f"{name} 对比度 {ratio:.2f}")

    print("\n2) 亮度带与色度（分类色板要平，跨度越小越好）")
    labs = [to_lab(v) for v in palette]
    for name, value, (light, a, b) in sorted(
        zip(names, palette, labs), key=lambda item: item[2][0]
    ):
        chroma = math.hypot(a, b)
        note = "  色度偏低" if name != "其他" and chroma < MIN_CHROMA else ""
        if note:
            warnings.append(f"{name} 色度只有 {chroma:.1f}")
        print(f"   {name:<4}{value}  L*{light:5.1f}  C*{chroma:5.1f}{note}")

    span = max(lab[0] for lab in labs) - min(lab[0] for lab in labs)
    print(f"   L* 跨度 {span:.1f}（上限 {MAX_LIGHTNESS_SPAN:.0f}）")
    if span > MAX_LIGHTNESS_SPAN:
        warnings.append(f"亮度跨度 {span:.1f}，色板会读出并不存在的大小顺序")

    print("\n3) 色觉障碍下的分离度（对照改动前的基线，只防退化）")
    for kind in ("常视觉", *CVD):
        worst = (999.0, "", "")
        for i in range(len(palette)):
            for j in range(i + 1, len(palette)):
                one = palette[i] if kind == "常视觉" else simulate(palette[i], kind)
                two = palette[j] if kind == "常视觉" else simulate(palette[j], kind)
                distance = delta_e(one, two)
                if distance < worst[0]:
                    worst = (distance, names[i], names[j])
        base = CVD_BASELINE[kind]
        # 只在低分区防退化。ΔE 到了 COMFORTABLE 以上，两个色已经「明显不同」，
        # 再掉个零点几毫无意义；要守的是 1.8、3.9 这种本来就危险的配对。
        # 允许 0.2 的浮动：色值微调本来就会让最差那一对轻微漂移。
        ok = worst[0] >= COMFORTABLE or worst[0] >= base - 0.2
        print(
            f"   {kind:<6} 最差一对 {worst[1]} vs {worst[2]}：ΔE {worst[0]:5.1f}"
            f"   基线 {base:5.1f}  {'PASS' if ok else '退步了'}"
        )
        if not ok:
            failures.append(f"{kind} 下最差分离度从 {base:.1f} 退到 {worst[0]:.1f}")

    print()
    if warnings:
        print("提醒（不算失败）：")
        for item in warnings:
            print("   ", item)
        print()
    if failures:
        print(f"未通过 {len(failures)} 项：")
        for item in failures:
            print("   ", item)
        return 1
    print("全部通过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
