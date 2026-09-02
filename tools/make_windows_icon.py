"""Convert the packaged high-resolution PNG logo into a multi-size Windows icon."""

from __future__ import annotations

import argparse
from pathlib import Path

from PIL import Image


def make_icon(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with Image.open(source) as image:
        image.convert("RGBA").save(
            destination,
            format="ICO",
            sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)],
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    make_icon(args.source, args.destination)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
