#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成 DVD光驱助手 应用图标（PNG 64/256）。
输出：
  <project>/ICON.PNG           (64x64)
  <project>/ICON_256.PNG       (256x256)
  <project>/app/ui/images/icon-64.png   (64x64)
  <project>/app/ui/images/icon-256.png  (256x256)
"""
import math
import os
from PIL import Image, ImageDraw

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")


def draw_icon(size: int) -> Image.Image:
    """绘制一张 DVD 光盘图标"""
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    # --- 圆角背景（深色渐变）---
    bg = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    bgd = ImageDraw.Draw(bg)
    radius = int(size * 0.18)
    bgd.rounded_rectangle([0, 0, size - 1, size - 1], radius=radius,
                          fill=(26, 34, 51, 255))
    # 顶部浅渐变
    grad = Image.new("RGBA", (1, size), (0, 0, 0, 0))
    for y in range(size):
        t = y / size
        r = int(26 + 10 * (1 - t))
        g = int(34 + 12 * (1 - t))
        b = int(51 + 20 * (1 - t))
        grad.putpixel((0, y), (r, g, b, 255))
    grad = grad.resize((size, size))
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, size - 1, size - 1],
                                           radius=radius, fill=255)
    bg = Image.composite(grad, bg, mask)
    img = bg

    # --- 光盘本体 ---
    cx = cy = size / 2
    outer_r = size * 0.36          # 光盘外径
    hole_r = size * 0.125          # 中心孔

    # 用同心环营造金属/光碟质感
    rings = 42
    for i in range(rings):
        r0 = outer_r * (1 - i / rings)
        r1 = outer_r * (1 - (i + 1) / rings)
        t = i / rings
        # 蓝紫渐变 + 亮环（模拟读取面反光）
        base = (
            int(110 + 40 * t),
            int(150 + 60 * t),
            int(255 - 60 * t),
            255,
        )
        if i % 6 == 0:  # 高光环
            base = (230, 240, 255, 255)
        d.ellipse([cx - r0, cy - r0, cx + r0, cy + r0],
                  fill=base if False else tuple(base))

    # 中央孔
    d.ellipse([cx - hole_r, cy - hole_r, cx + hole_r, cy + hole_r],
              fill=(26, 34, 51, 255))

    # 光盘高光弧（左上）
    highlight = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    hd = ImageDraw.Draw(highlight)
    hd.pieslice([cx - outer_r * 1.05, cy - outer_r * 1.05,
                 cx + outer_r * 1.05, cy + outer_r * 1.05],
                start=-60, end=-15, fill=(255, 255, 255, 60))
    img = Image.alpha_composite(img, highlight)

    return img


def main():
    icon256 = draw_icon(256)
    icon64 = icon256.resize((64, 64), Image.LANCZOS)

    out = [
        (icon64, os.path.join(ROOT, "ICON.PNG")),
        (icon256, os.path.join(ROOT, "ICON_256.PNG")),
        (icon64, os.path.join(ROOT, "app", "ui", "images", "icon-64.png")),
        (icon256, os.path.join(ROOT, "app", "ui", "images", "icon-256.png")),
    ]
    for img, path in out:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        img.save(path, "PNG")
        print(f"  -> {path}  {img.size[0]}x{img.size[1]}")


if __name__ == "__main__":
    main()
