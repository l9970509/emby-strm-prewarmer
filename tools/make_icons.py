"""生成飞牛应用图标（纯标准库，无需 Pillow）。

产物：ICON.PNG(64) / ICON_256.PNG(256) / app/ui/images/icon_64.png、icon_256.png
画法：圆角渐变底 + 白色播放三角 + 一段预加载环。
"""

from __future__ import annotations

import struct
import zlib
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent / "fpk" / "embyprewarm"

C1 = (43, 58, 143)     # 左上 深靛蓝
C2 = (47, 182, 255)    # 右下 亮蓝
WHITE = (255, 255, 255)


def inside_rounded_rect(x: float, y: float, s: float, r: float) -> bool:
    if x < 0 or y < 0 or x > s or y > s:
        return False
    cx = min(max(x, r), s - r)
    cy = min(max(y, r), s - r)
    dx = x - cx
    dy = y - cy
    return dx * dx + dy * dy <= r * r


def inside_triangle(x: float, y: float, s: float) -> bool:
    ax, ay = 0.375 * s, 0.295 * s
    bx, by = 0.375 * s, 0.705 * s
    cx, cy = 0.715 * s, 0.5 * s
    d1 = (x - bx) * (ay - by) - (ax - bx) * (y - by)
    d2 = (x - cx) * (by - cy) - (bx - cx) * (y - cy)
    d3 = (x - ax) * (cy - ay) - (cx - ax) * (y - ay)
    has_neg = d1 < 0 or d2 < 0 or d3 < 0
    has_pos = d1 > 0 or d2 > 0 or d3 > 0
    return not (has_neg and has_pos)


def ring_hit(x: float, y: float, s: float) -> bool:
    cx = cy = 0.5 * s
    radius = 0.355 * s
    thick = 0.05 * s
    dx, dy = x - cx, y - cy
    dist = (dx * dx + dy * dy) ** 0.5
    if abs(dist - radius) > thick / 2:
        return False
    # 两段弧，留出上下缺口，形成"刷新/预加载"的感觉
    import math

    angle = (math.degrees(math.atan2(dy, dx)) + 360) % 360
    gaps = ((70, 110), (250, 290))
    return not any(a <= angle <= b for a, b in gaps)


def render(size: int, ss: int = 4) -> bytes:
    s = float(size)
    r = 0.235 * s
    rows = []
    for py in range(size):
        row = bytearray([0])
        for px in range(size):
            acc_r = acc_g = acc_b = acc_a = 0
            hits = 0
            for i in range(ss):
                for j in range(ss):
                    x = px + (i + 0.5) / ss
                    y = py + (j + 0.5) / ss
                    if not inside_rounded_rect(x, y, s, r):
                        continue
                    hits += 1
                    t = (x + y) / (2.0 * s)
                    if inside_triangle(x, y, s) or ring_hit(x, y, s):
                        cr, cg, cb = WHITE
                    else:
                        cr = C1[0] + (C2[0] - C1[0]) * t
                        cg = C1[1] + (C2[1] - C1[1]) * t
                        cb = C1[2] + (C2[2] - C1[2]) * t
                    acc_r += cr
                    acc_g += cg
                    acc_b += cb
                    acc_a += 255
            total = ss * ss
            if hits == 0:
                row += bytes((0, 0, 0, 0))
            else:
                row += bytes(
                    (
                        round(acc_r / hits),
                        round(acc_g / hits),
                        round(acc_b / hits),
                        round(acc_a / total),
                    )
                )
        rows.append(bytes(row))
    raw = b"".join(rows)

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(raw, 9))
        + chunk(b"IEND", b"")
    )


def main() -> None:
    targets = [
        (BASE / "ICON.PNG", 64),
        (BASE / "ICON_256.PNG", 256),
        (BASE / "app" / "ui" / "images" / "icon_64.png", 64),
        (BASE / "app" / "ui" / "images" / "icon_256.png", 256),
    ]
    cache: dict[int, bytes] = {}
    for path, size in targets:
        if size not in cache:
            cache[size] = render(size)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(cache[size])
        print(f"写入 {path} ({size}x{size}, {len(cache[size])} 字节)")


if __name__ == "__main__":
    main()
