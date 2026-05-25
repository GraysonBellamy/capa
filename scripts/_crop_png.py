"""Inline helper: crop a region from a PNG.

Usage: uv run python scripts/_crop_png.py <src.png> <dst.png> <left> <top> <right> <bottom>
"""

from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image


def main() -> int:
    if len(sys.argv) != 7:
        print("usage: _crop_png.py <src> <dst> <left> <top> <right> <bottom>", file=sys.stderr)
        return 2
    src, dst, *coords = sys.argv[1:]
    left, top, right, bottom = (int(c) for c in coords)
    img = Image.open(src)
    out = img.crop((left, top, right, bottom))
    Path(dst).parent.mkdir(parents=True, exist_ok=True)
    out.save(dst, "PNG")
    print(f"{dst}: {out.size}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
