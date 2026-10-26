"""生成应用图标 assets/icon.ico（纯标准库，不依赖 Pillow）。

设计：深蓝圆角方块 + 白色下箭头 + 底部进度条。
"""
import os
import struct

OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "assets", "icon.ico")
SIZES = [16, 24, 32, 48, 64, 128, 256]

BG_TOP = (37, 99, 235)
BG_BOT = (29, 78, 216)
FG = (255, 255, 255)


def lerp(a, b, t):
    return tuple(int(round(a[i] + (b[i] - a[i]) * t)) for i in range(3))


def rounded_alpha(x, y, s, radius):
    """圆角矩形的覆盖度 0..1（1px 抗锯齿）。"""
    r = radius
    cx = min(max(x + 0.5, r), s - r)
    cy = min(max(y + 0.5, r), s - r)
    dx, dy = (x + 0.5) - cx, (y + 0.5) - cy
    d = (dx * dx + dy * dy) ** 0.5
    return max(0.0, min(1.0, r - d + 0.5))


def inside_tri(x, y, s):
    """箭头三角：以 s 为基准的相对坐标。"""
    cx = s / 2.0
    top = s * 0.36
    bot = s * 0.63
    half = s * 0.19
    if y < top or y > bot:
        return False
    t = (y - top) / (bot - top)
    return abs(x - cx) <= half * (1.0 - t)


def inside_stem(x, y, s):
    cx = s / 2.0
    half = s * 0.055
    return abs(x - cx) <= half and s * 0.17 <= y <= s * 0.40


def inside_bar(x, y, s):
    x0, x1 = s * 0.27, s * 0.73
    y0, y1 = s * 0.72, s * 0.79
    return x0 <= x <= x1 and y0 <= y <= y1


def render(s):
    px = []
    radius = max(2.0, s * 0.20)
    for y in range(s):
        row = []
        for x in range(s):
            a_bg = rounded_alpha(x, y, s, radius)
            if a_bg <= 0:
                row.append((0, 0, 0, 0))
                continue
            col = lerp(BG_TOP, BG_BOT, y / max(1, s - 1))
            fg = inside_tri(x, y, s) or inside_stem(x, y, s) or inside_bar(x, y, s)
            if fg:
                col = FG
            row.append((col[0], col[1], col[2], int(round(255 * a_bg))))
        px.append(row)
    return px


def bmp_bytes(px, s):
    """32bpp BGRA，自底向上 + AND 掩码。"""
    hdr = struct.pack("<IiiHHIIiiII", 40, s, s * 2, 1, 32, 0, 0, 0, 0, 0, 0)
    body = bytearray()
    for y in range(s - 1, -1, -1):
        for x in range(s):
            r, g, b, a = px[y][x]
            body += bytes((b, g, r, a))
    mask_row = ((s + 31) // 32) * 4
    mask = bytes(mask_row * s)
    return hdr + bytes(body) + mask


def main():
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    images = [(s, bmp_bytes(render(s), s)) for s in SIZES]
    n = len(images)
    out = bytearray(struct.pack("<HHH", 0, 1, n))
    offset = 6 + 16 * n
    for s, data in images:
        out += struct.pack("<BBBBHHII", 0 if s >= 256 else s, 0 if s >= 256 else s,
                           0, 0, 1, 32, len(data), offset)
        offset += len(data)
    for _, data in images:
        out += data
    with open(OUT, "wb") as f:
        f.write(bytes(out))
    print("wrote", OUT, len(out), "bytes")


if __name__ == "__main__":
    main()
