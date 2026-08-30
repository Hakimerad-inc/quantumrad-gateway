"""Generate minimal valid PNG icons for the Tauri shell (S06-T1).

Produces PNG files in src-tauri/icons/ at the sizes Tauri expects for
bundling and tray icon.  Uses only the Python stdlib (zlib + struct).
"""

from __future__ import annotations

import struct
import zlib
from pathlib import Path


def _make_png(width: int, height: int, r: int, g: int, b: int) -> bytes:
    """Create a minimal valid PNG with a solid color fill."""
    raw = b""
    for _y in range(height):
        raw += b"\x00"  # filter byte = None
        for _x in range(width):
            raw += bytes([r, g, b, 255])
    compressed = zlib.compress(raw)

    def chunk(chunk_type: bytes, data: bytes) -> bytes:
        c = chunk_type + data
        return struct.pack(">I", len(data)) + c + struct.pack(">I", zlib.crc32(c) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)  # 8-bit RGBA
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", compressed)
        + chunk(b"IEND", b"")
    )


def main() -> None:
    icons_dir = Path(__file__).resolve().parent.parent / "src-tauri" / "icons"
    icons_dir.mkdir(parents=True, exist_ok=True)

    sizes = [(32, 32), (128, 128), (256, 256)]
    color = (56, 189, 248)  # accent blue (#38bdf8)

    for w, h in sizes:
        png = _make_png(w, h, *color)
        name = f"{w}x{w}.png" if w == h else f"{w}x{h}.png"
        (icons_dir / name).write_bytes(png)
        if w == 128:
            (icons_dir / "128x128@2x.png").write_bytes(_make_png(256, 256, *color))
        if w == 32:
            (icons_dir / "icon.png").write_bytes(png)

    # ICO container (Windows) — just a copy of the 32x32 PNG wrapped minimally
    png_32 = _make_png(32, 32, *color)
    ico_data = (
        struct.pack("<HHH", 0, 1, 1)  # reserved, type=1 (ICO), count=1
        + struct.pack("<BBBBHHII", 32, 32, 0, 0, 1, 32, len(png_32), 22)
        + png_32
    )
    (icons_dir / "icon.ico").write_bytes(ico_data)

    print(f"Generated icons in {icons_dir}")


if __name__ == "__main__":
    main()