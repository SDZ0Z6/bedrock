"""从 static/logo.jpg 生成网页用的派生图。

原图是 2730×1536、1.6 MB 的 JPEG，纸质米白底，不透明。直接放网页有两个问题：
太重（每页 1.6 MB），以及那块米白底会在任何非同色的背景上抠出一个方块。

这里做三件事：
  1. **扣底**：按「离纸底色多远」算 alpha，而不是硬阈值——水彩球的半透明边缘
     和铅笔线的抗锯齿都能平滑保留，直接二值化会留下锯齿和白边。
  2. **裁掉空白**：原图右侧约 28% 是纯空白，内容框只占 x 19%-72%。
  3. **按用途出三种尺寸**，都压到几十 KB。

改了原图或想调参数就重跑：

    python tools/make_logo_assets.py

小尺寸用的是**只截侧脸那一块**，不是整幅。实测把整幅缩到 24px（侧边栏品牌区的
实际大小）会糊成一团彩点；只留侧脸的话轮廓清楚，左上角还能带进一两个珊瑚球，
品牌色也在。

产物（都提交进仓库，部署时不需要跑这个脚本）：
    logo-mark.png    侧边栏与登录页品牌区，96×96，侧脸特写
    favicon.png      浏览器标签页，64×64，侧脸特写

登录页右侧那张大图是另一个文件（static/login-art.webp），不由这个脚本生成。
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
from PIL import Image

STATIC = Path(__file__).resolve().parent.parent / "src" / "bedrock_cost" / "static"
SOURCE = STATIC / "logo.jpg"

# 纸底色，取自原图四角
PAPER = np.array([229, 225, 221])

# 离纸底色的距离小于这个值算完全透明，大于 FULL 算完全不透明，中间线性过渡。
#
# 阈值卡在噪点和内容之间那道缝里：实测纸面颗粒在空白区最大到 58（距离的 90
# 分位只有 11），而真实笔触从 176 起跳，中间是空的。所以 55 以下一律当噪点抹掉，
# 170 以上算实心，中间那段留给水彩球的半透明边缘和铅笔线的抗锯齿。
#
# 别把 CLEAR 调得太低：纸面噪点会拿到一点点 alpha，肉眼看不出来，但会让
# getbbox() 认为整张图都有内容，裁不掉空白，文件也白白变大。
CLEAR, FULL = 55, 170


def cutout(image: Image.Image) -> Image.Image:
    """把纸底色变成透明，返回 RGBA。"""
    rgb = np.asarray(image.convert("RGB")).astype(np.int16)
    distance = np.abs(rgb - PAPER).sum(axis=2)

    alpha = (distance - CLEAR) / (FULL - CLEAR)
    alpha = np.clip(alpha, 0.0, 1.0)

    # 扣底之后，半透明像素里仍混着纸底色，边缘会发灰。把纸底色的贡献除掉，
    # 还原出颜料本身的颜色（标准的 un-premultiply）。
    safe = np.maximum(alpha[..., None], 0.15)
    pure = (rgb - PAPER * (1 - safe)) / safe
    pure = np.clip(pure, 0, 255)

    out = np.dstack([pure, alpha * 255]).astype(np.uint8)
    return Image.fromarray(out, mode="RGBA")


def content_box(image: Image.Image, min_pixels: int = 8) -> tuple[int, int, int, int] | None:
    """内容的外接框。

    不用 getbbox()：它只要有一个像素不透明就算数，个别残留噪点就能把框撑到全图。
    这里要求一行/一列至少有 min_pixels 个不透明像素才算「有内容」。
    """
    alpha = np.asarray(image)[..., 3] > 32
    rows = np.nonzero(alpha.sum(axis=1) >= min_pixels)[0]
    cols = np.nonzero(alpha.sum(axis=0) >= min_pixels)[0]
    if not len(rows) or not len(cols):
        return None
    return int(cols.min()), int(rows.min()), int(cols.max()) + 1, int(rows.max()) + 1


def trim(image: Image.Image, pad_ratio: float = 0.02) -> Image.Image:
    """裁掉四周的空白，留一点余量。"""
    box = content_box(image)
    if not box:
        return image
    left, top, right, bottom = box
    pad = int(max(image.width, image.height) * pad_ratio)
    return image.crop((
        max(0, left - pad),
        max(0, top - pad),
        min(image.width, right + pad),
        min(image.height, bottom + pad),
    ))


def head_crop(image: Image.Image) -> Image.Image:
    """截出侧脸那一块，给小尺寸用。

    按「深色墨线」的外接框来定位，不写死坐标——重画 logo 后只要构图相似就还能用。
    左上多留一些余量，好把最近的那颗珊瑚球带进画面，否则小图标会变成纯黑白，
    丢掉品牌色。
    """
    pixels = np.asarray(image)
    opaque = pixels[..., 3] > 60
    ink = opaque & (pixels[..., :3].astype(int).sum(axis=2) < 300)
    rows = np.nonzero(ink.sum(axis=1) >= 4)[0]
    cols = np.nonzero(ink.sum(axis=0) >= 4)[0]
    if not len(rows) or not len(cols):
        return image

    span = max(cols.max() - cols.min(), rows.max() - rows.min())
    return image.crop((
        max(0, int(cols.min() - span * 0.22)),   # 左边多留，带进珊瑚球
        max(0, int(rows.min() - span * 0.18)),
        min(image.width, int(cols.max() + span * 0.05)),
        min(image.height, int(rows.max() + span * 0.05)),
    ))


def square(image: Image.Image) -> Image.Image:
    """填成正方形（居中，四周透明），供需要 1:1 的场景用。"""
    side = max(image.size)
    canvas = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    canvas.paste(image, ((side - image.width) // 2, (side - image.height) // 2))
    return canvas


def save(image: Image.Image, name: str, width: int | None = None, box: int | None = None) -> None:
    out = image
    if width:
        out = out.resize((width, round(width * image.height / image.width)), Image.LANCZOS)
    if box:
        out = square(out).resize((box, box), Image.LANCZOS)

    # 这是一张颜色很少的插画（珊瑚水彩 + 黑线），量化成调色板能省一大半体积，
    # 肉眼看不出差别。quantize 会保留 alpha 通道。
    small = out.quantize(colors=128, method=Image.Quantize.FASTOCTREE)
    path = STATIC / name
    small.save(path, "PNG", optimize=True)
    print(f"   {name:<16} {out.size[0]}×{out.size[1]:<5} {path.stat().st_size / 1024:6.1f} KB")


def main() -> None:
    if not SOURCE.is_file():
        raise SystemExit(f"找不到原图：{SOURCE}")
    print(f"原图 {SOURCE.name}  {SOURCE.stat().st_size / 1024 / 1024:.2f} MB")

    full = trim(cutout(Image.open(SOURCE)))
    print(f"扣底并裁掉空白后：{full.size[0]}×{full.size[1]}")

    head = head_crop(full)
    print(f"侧脸特写：{head.size[0]}×{head.size[1]}")

    print("生成：")
    save(head, "logo-mark.png", box=96)
    save(head, "favicon.png", box=64)


if __name__ == "__main__":
    main()
