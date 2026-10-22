"""生成应用图标（纯标准库，不依赖 Pillow）。

ICO 支持内嵌 PNG（Vista 起），所以只要会写 PNG 就能做图标：
先在一个大画布上按矢量规则着色，再盒式降采样到各尺寸，小尺寸下也不会糊成一团。
"""

from __future__ import annotations

import struct
import zlib
from pathlib import Path

INK = (0x16, 0x16, 0x16)  # 底板
SIGNAL = (0xFF, 0xFA, 0x00)  # 强调色（和界面同一个黄）
SIZES = (16, 24, 32, 48, 64, 128, 256)
SUPERSAMPLE = 4


def _covers(x: float, y: float) -> tuple[int, int, int, int]:
    """返回该点的 RGBA。坐标是 0..1 的归一化坐标。"""
    cut = 0.22
    # 右下角切角（和界面里的 cut-br 一个意思）
    if x > 1 - cut and y > 1 - cut and (x - (1 - cut)) + (y - (1 - cut)) > cut:
        return (0, 0, 0, 0)

    # 上面一条向右的箭头
    if 0.20 <= x <= 0.70 and 0.28 <= y <= 0.42:
        return (*SIGNAL, 255)
    if 0.70 <= x <= 0.86 and 0.21 <= y <= 0.49:
        # 三角箭头：越靠右可用高度越小
        t = (x - 0.70) / 0.16
        half = 0.14 * (1 - t)
        if abs(y - 0.35) <= half:
            return (*SIGNAL, 255)

    # 下面一条向左的箭头
    if 0.30 <= x <= 0.80 and 0.58 <= y <= 0.72:
        return (*SIGNAL, 255)
    if 0.14 <= x <= 0.30 and 0.51 <= y <= 0.79:
        t = (0.30 - x) / 0.16
        half = 0.14 * (1 - t)
        if abs(y - 0.65) <= half:
            return (*SIGNAL, 255)

    return (*INK, 255)


def _render(size: int) -> bytes:
    """渲染成 RGBA 原始像素（自上而下）。"""
    big = size * SUPERSAMPLE
    samples = [[_covers((x + 0.5) / big, (y + 0.5) / big) for x in range(big)] for y in range(big)]

    rows = []
    for y in range(size):
        row = bytearray()
        for x in range(size):
            r = g = b = a = 0
            for dy in range(SUPERSAMPLE):
                for dx in range(SUPERSAMPLE):
                    pr, pg, pb, pa = samples[y * SUPERSAMPLE + dy][x * SUPERSAMPLE + dx]
                    r += pr * pa
                    g += pg * pa
                    b += pb * pa
                    a += pa
            if a:
                row += bytes((r // a, g // a, b // a, a // (SUPERSAMPLE**2)))
            else:
                row += b"\x00\x00\x00\x00"
        rows.append(bytes(row))
    return b"".join(rows)


def _png(size: int) -> bytes:
    raw = _render(size)

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    # 每行前面加一个 filter 字节 0
    scanlines = b"".join(b"\x00" + raw[y * size * 4 : (y + 1) * size * 4] for y in range(size))
    ihdr = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(scanlines, 9))
        + chunk(b"IEND", b"")
    )


def build(out: Path) -> Path:
    images = [(size, _png(size)) for size in SIZES]
    header = struct.pack("<HHH", 0, 1, len(images))
    offset = 6 + 16 * len(images)
    entries = b""
    blob = b""
    for size, data in images:
        entries += struct.pack(
            "<BBBBHHII",
            0 if size >= 256 else size,
            0 if size >= 256 else size,
            0,
            0,
            1,
            32,
            len(data),
            offset,
        )
        offset += len(data)
        blob += data
    out.write_bytes(header + entries + blob)
    return out


if __name__ == "__main__":
    target = build(Path(__file__).with_name("netswitcher.ico"))
    print(f"{target}  {target.stat().st_size} bytes")
