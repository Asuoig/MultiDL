"""生成扩展图标（RGBA PNG），复用 make_icon 的绘制逻辑。"""
import os
import sys
import zlib
import struct

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from make_icon import render  # noqa: E402

OUT_DIR = os.path.join(os.path.dirname(HERE), "extension", "icons")
SIZES = [16, 32, 48, 128]


def png_rgba(w, h, rows):
    raw = b"".join(b"\x00" + bytes(r) for r in rows)

    def chunk(tag, data):
        c = struct.pack(">I", len(data)) + tag + data
        return c + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0)   # 8bit RGBA
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    for s in SIZES:
        px = render(s)
        flat = []
        for row in px:
            line = bytearray()
            for r, g, b, a in row:
                line += bytes((r, g, b, a))
            flat.append(line)
        path = os.path.join(OUT_DIR, f"icon{s}.png")
        with open(path, "wb") as f:
            f.write(png_rgba(s, s, flat))
        print("wrote", path, os.path.getsize(path), "bytes")


if __name__ == "__main__":
    main()
