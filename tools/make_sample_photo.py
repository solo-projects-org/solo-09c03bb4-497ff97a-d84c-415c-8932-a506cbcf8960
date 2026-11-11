#!/usr/bin/env python3
"""生成随附样例照片：倒木横卧林道（纯标准库输出 PNG，无第三方依赖）"""
import math
import os
import random
import struct
import zlib

W, H = 800, 600
buf = bytearray(W * H * 3)


def px(x, y, rgb):
    if 0 <= x < W and 0 <= y < H:
        i = (y * W + x) * 3
        buf[i:i + 3] = bytes(rgb)


def lerp(c1, c2, t):
    return tuple(round(a + (b - a) * t) for a, b in zip(c1, c2))


def vgrad(y0, y1, c_top, c_bot):
    for y in range(y0, y1):
        t = (y - y0) / max(1, y1 - y0 - 1)
        c = lerp(c_top, c_bot, t)
        for x in range(W):
            px(x, y, c)


def fill_circle(cx, cy, r, rgb):
    for y in range(max(0, cy - r), min(H, cy + r + 1)):
        for x in range(max(0, cx - r), min(W, cx + r + 1)):
            if (x - cx) ** 2 + (y - cy) ** 2 <= r * r:
                px(x, y, rgb)


def seg_dist2(x, y, x1, y1, x2, y2):
    dx, dy = x2 - x1, y2 - y1
    L2 = dx * dx + dy * dy
    if L2 == 0:
        return (x - x1) ** 2 + (y - y1) ** 2
    t = max(0.0, min(1.0, ((x - x1) * dx + (y - y1) * dy) / L2))
    return (x - x1 - t * dx) ** 2 + (y - y1 - t * dy) ** 2


def thick_seg(x1, y1, x2, y2, half, rgb):
    x0, x3 = sorted((int(x1), int(x2)))
    y0, y3 = sorted((int(y1), int(y2)))
    for y in range(max(0, y0 - half), min(H, y3 + half + 1)):
        for x in range(max(0, x0 - half), min(W, x3 + half + 1)):
            if seg_dist2(x, y, x1, y1, x2, y2) <= half * half:
                px(x, y, rgb)


def triangle(cx, base_y, half_w, top_y, rgb):
    for y in range(max(0, top_y), min(H, base_y)):
        t = (y - top_y) / max(1, base_y - top_y)
        hw = half_w * t
        for x in range(int(cx - hw), int(cx + hw) + 1):
            px(x, y, rgb)


rng = random.seed(20261111) or random.Random(20261111)

# 天空
vgrad(0, 270, (140, 196, 228), (219, 238, 247))
# 远山
for y in range(210, 300):
    t = (y - 210) / 90
    c = lerp((96, 138, 108), (122, 160, 120), t)
    for x in range(W):
        ridge = 235 + 18 * math.sin(x / 90.0) + 8 * math.sin(x / 31.0)
        if y >= ridge:
            px(x, y, c)
# 林缘树梢
for i in range(28):
    cx = rng.randint(0, W)
    triangle(cx, 310, rng.randint(22, 40), rng.randint(215, 250),
             (34, rng.randint(80, 105), 48))
# 草地
vgrad(300, H, (110, 152, 78), (66, 108, 52))
# 土路（透视梯形）
for y in range(300, H):
    t = (y - 300) / (H - 300)
    half = 30 + 230 * t
    cx = 400 + 30 * math.sin(t * 2.2)
    c = lerp((168, 150, 122), (138, 120, 95), t)
    for x in range(max(0, int(cx - half)), min(W, int(cx + half))):
        px(x, y, c)
# 路面车辙
for y in range(300, H):
    t = (y - 300) / (H - 300)
    half = 30 + 230 * t
    cx = 400 + 30 * math.sin(t * 2.2)
    for s in (-0.45, 0.45):
        x = int(cx + s * half)
        for w in range(max(2, int(6 * t))):
            px(x + w, y, (110, 95, 74))

# 倒木主干（横卧路面，先深色描边再浅色本体）
TX1, TY1, TX2, TY2, TH = 60, 400, 740, 452, 30
thick_seg(TX1, TY1, TX2, TY2, TH + 6, (58, 38, 20))
thick_seg(TX1, TY1, TX2, TY2, TH, (111, 74, 42))
# 木纹
for off in (-14, -4, 7, 16):
    thick_seg(TX1 + 10, TY1 + off, TX2 - 10, TY2 + off, 2, (86, 55, 30))
# 枝杈
for bx, by, ex, ey in ((220, 408, 180, 320), (380, 418, 420, 330),
                       (520, 428, 490, 345), (640, 440, 690, 360)):
    thick_seg(bx, by, ex, ey, 6, (74, 48, 26))
# 根部土球（左端）
fill_circle(TX1 - 6, TY1 - 6, 46, (70, 52, 34))
fill_circle(TX1 - 14, TY1 - 20, 30, (52, 96, 44))
fill_circle(TX1 + 8, TY1 - 34, 22, (60, 110, 50))
# 右端截面年轮
ECX, ECY, ER = TX2, TY2, TH - 2
for y in range(ECY - ER, ECY + ER + 1):
    for x in range(ECX - ER, ECX + ER + 1):
        d2 = (x - ECX) ** 2 + (y - ECY) ** 2
        if d2 <= ER * ER:
            d = math.sqrt(d2) / ER
            ring = 0.82 if (d * 7) % 1 < 0.3 else 1.0
            px(x, y, tuple(round(c * ring) for c in (201, 160, 107)))
# 草丛
for _ in range(160):
    gx = rng.randint(0, W - 1)
    gy = rng.randint(330, H - 1)
    if rng.random() < 0.5:
        thick_seg(gx, gy, gx + rng.randint(-3, 3), gy - rng.randint(5, 12),
                  1, (40, 86, 36))


def write_png(path):
    raw = b"".join(
        b"\x00" + bytes(buf[y * W * 3:(y + 1) * W * 3]) for y in range(H)
    )

    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data +
                struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    png = (b"\x89PNG\r\n\x1a\n"
           + chunk(b"IHDR", struct.pack(">IIBBBBB", W, H, 8, 2, 0, 0, 0))
           + chunk(b"IDAT", zlib.compress(raw, 9))
           + chunk(b"IEND", b""))
    with open(path, "wb") as f:
        f.write(png)


if __name__ == "__main__":
    out = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "..", "app", "static", "sample-photo.png")
    write_png(os.path.abspath(out))
    print("written:", os.path.abspath(out))
