"""生成网站 logo：Claude 的像素小吉祥物（和等待动效 loading.webm、登录页插画里是同一只）。

格子是照 static/loading.webm 里那只量出来的：10 列 × 8 行，一格一个像素。
    # 身体（陶土色，和主题的 --accent 同一个色）
    o 眼睛（墨色）
    . 空

小图标就该是像素画：按格子直接画成矩形，SVG 多大都清楚；PNG 按整数倍放大、不插值，
边缘不会糊。改了格子或颜色就重跑：

    python tools/make_mascot_assets.py

产物（都提交进仓库，部署时不需要跑这个脚本）：
    mascot.svg            侧边栏和登录页的品牌标、浏览器标签页图标（支持 SVG 的浏览器）
    favicon.png           浏览器标签页，64×64，透明底（不支持 SVG 图标的浏览器）
    apple-touch-icon.png  苹果设备加到主屏幕，180×180，象牙白底（iOS 会把透明的地方填成黑）
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

STATIC = Path(__file__).resolve().parent.parent / "src" / "bedrock_cost" / "static"

GRID = [
    ".########.",
    ".#o####o#.",
    "##########",
    "##########",
    ".########.",
    ".########.",
    ".#.#..#.#.",
    ".#.#..#.#.",
]
BODY = "#d97757"
EYES = "#141413"
PAPER = "#faf9f5"


def _runs(row: str, mark: str) -> list[tuple[int, int]]:
    """一行里连续的 mark 段：(起点, 长度)。按段画矩形，SVG 小、拼缝也不会露白。"""
    out, start = [], None
    for x, cell in enumerate(row + "."):
        if cell == mark and start is None:
            start = x
        elif cell != mark and start is not None:
            out.append((start, x - start))
            start = None
    return out


def svg() -> str:
    width, height = len(GRID[0]), len(GRID)
    rects = []
    for y, row in enumerate(GRID):
        # 眼睛那两格也先当身体画上，再在上面盖墨色的方块，身体的轮廓就是完整的一块
        body_row = row.replace("o", "#")
        rects += [f'<rect x="{x}" y="{y}" width="{w}" height="1" fill="{BODY}"/>' for x, w in _runs(body_row, "#")]
    for y, row in enumerate(GRID):
        rects += [f'<rect x="{x}" y="{y}" width="{w}" height="1" fill="{EYES}"/>' for x, w in _runs(row, "o")]
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" width="{width * 3}" '
        f'height="{height * 3}" shape-rendering="crispEdges">' + "".join(rects) + "</svg>\n"
    )


def png(size: int, scale: int, background: str | None) -> Image.Image:
    image = Image.new("RGBA", (size, size), background or (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    left = (size - len(GRID[0]) * scale) // 2
    top = (size - len(GRID) * scale) // 2
    for y, row in enumerate(GRID):
        for x, cell in enumerate(row):
            if cell == ".":
                continue
            colour = EYES if cell == "o" else BODY
            draw.rectangle(
                [left + x * scale, top + y * scale, left + (x + 1) * scale - 1, top + (y + 1) * scale - 1],
                fill=colour,
            )
    return image


def main() -> None:
    (STATIC / "mascot.svg").write_text(svg(), encoding="utf-8")
    png(64, 6, None).save(STATIC / "favicon.png", optimize=True)
    png(180, 14, PAPER).convert("RGB").save(STATIC / "apple-touch-icon.png", optimize=True)
    for name in ("mascot.svg", "favicon.png", "apple-touch-icon.png"):
        print(f"{name}: {(STATIC / name).stat().st_size} B")


if __name__ == "__main__":
    main()
