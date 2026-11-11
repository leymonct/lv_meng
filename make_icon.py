# -*- coding: utf-8 -*-
"""生成程序图标 Lumin.ico。

素材优先使用 icon_src.png（DeepSeek 鲸鱼娘二创形象），把它居中裁成正方形、
加上圆角（可选），再生成多尺寸 .ico 供 exe 与 NSIS 安装程序使用。

如果 icon_src.png 不存在，则回退到用 Pillow 手绘的「梦」字图标。
"""
import os

from PIL import Image, ImageDraw, ImageFont

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "icon_src.png")
OUT = os.path.join(HERE, "Lumin.ico")

# 圆角占边长比例（0=不圆角）。Windows 桌面/任务栏图标通常显示为圆角，
# 这里给图片本身也加一点圆角，避免方角在深色任务栏上突兀。
CORNER_RATIO = 0.18


def _center_crop_square(img):
    """居中裁成正方形（取较短边）。"""
    w, h = img.size
    side = min(w, h)
    left = (w - side) // 2
    top = (h - side) // 2
    return img.crop((left, top, left + side, top + side))


def _rounded(img, ratio):
    """给 RGBA 图加圆角透明蒙版。"""
    s = img.size[0]
    mask = Image.new("L", (s, s), 0)
    d = ImageDraw.Draw(mask)
    d.rounded_rectangle([0, 0, s - 1, s - 1], radius=int(s * ratio), fill=255)
    img.putalpha(mask)
    return img


def _from_source(size):
    """从 icon_src.png 生成指定尺寸的 RGBA 图标。"""
    img = Image.open(SRC).convert("RGBA")
    img = _center_crop_square(img)
    img = img.resize((size, size), Image.LANCZOS)
    if CORNER_RATIO > 0:
        img = _rounded(img, CORNER_RATIO)
    return img


# ---- 兜底：手绘「梦」字图标（无 icon_src.png 时使用） ----


def _vgradient(size, top, bottom):
    w, h = size
    img = Image.new("RGB", size)
    px = img.load()
    for y in range(h):
        t = y / max(1, h - 1)
        r = int(top[0] + (bottom[0] - top[0]) * t)
        g = int(top[1] + (bottom[1] - top[1]) * t)
        b = int(top[2] + (bottom[2] - top[2]) * t)
        for x in range(w):
            px[x, y] = (r, g, b)
    return img


def _font(size):
    candidates = [
        "C:/Windows/Fonts/msyh.ttc",
        "C:/Windows/Fonts/msyhbd.ttc",
        "C:/Windows/Fonts/simhei.ttf",
        "C:/Windows/Fonts/simfang.ttf",
        "C:/Windows/Fonts/arial.ttf",
    ]
    for p in candidates:
        if os.path.exists(p):
            try:
                return ImageFont.truetype(p, size)
            except Exception:
                continue
    return ImageFont.load_default()


def _fallback_icon(size):
    s = size
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    bg = _vgradient((s, s), (18, 84, 62), (40, 160, 110))
    img.paste(bg, (0, 0))
    mask = Image.new("L", (s, s), 0)
    md = ImageDraw.Draw(mask)
    md.rounded_rectangle([0, 0, s - 1, s - 1], radius=int(s * 0.22), fill=255)
    img.putalpha(mask)
    d = ImageDraw.Draw(img)
    font = _font(int(s * 0.50))
    ch = "梦"
    bbox = d.textbbox((0, 0), ch, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    x = (s - tw) / 2 - bbox[0]
    y = (s - th) / 2 - bbox[1]
    d.text((x, y), ch, font=font, fill=(255, 255, 255, 255))
    return img


def draw_icon(size):
    if os.path.exists(SRC):
        return _from_source(size)
    return _fallback_icon(size)


def build_ico(path=OUT):
    sizes = [256, 128, 64, 48, 32, 16]
    imgs = [draw_icon(s) for s in sizes]
    imgs[0].save(path, format="ICO", sizes=[(s, s) for s in sizes])
    return path


if __name__ == "__main__":
    build_ico()
    print("已生成", OUT, os.path.getsize(OUT), "字节",
          "（来源:", "鲸鱼娘素材" if os.path.exists(SRC) else "手绘兜底", "）")
