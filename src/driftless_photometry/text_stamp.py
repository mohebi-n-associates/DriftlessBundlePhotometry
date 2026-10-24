"""Stamp short labels into derived RGB images with a built-in 5x7 bitmap font.

Used for ROI names on saved ROI maps, so imaging stays free of font or imaging
dependencies. Lower-case text is drawn in capitals; unknown characters become '?'.
"""

from __future__ import annotations

import numpy as np
from numpy.typing import NDArray

_GLYPH_ROWS = 7
_GLYPH_COLUMNS = 5
_GLYPHS: dict[str, str] = {
    " ": "00000 00000 00000 00000 00000 00000 00000",
    "A": "01110 10001 10001 11111 10001 10001 10001",
    "B": "11110 10001 10001 11110 10001 10001 11110",
    "C": "01110 10001 10000 10000 10000 10001 01110",
    "D": "11110 10001 10001 10001 10001 10001 11110",
    "E": "11111 10000 10000 11110 10000 10000 11111",
    "F": "11111 10000 10000 11110 10000 10000 10000",
    "G": "01110 10001 10000 10111 10001 10001 01111",
    "H": "10001 10001 10001 11111 10001 10001 10001",
    "I": "01110 00100 00100 00100 00100 00100 01110",
    "J": "00111 00010 00010 00010 00010 10010 01100",
    "K": "10001 10010 10100 11000 10100 10010 10001",
    "L": "10000 10000 10000 10000 10000 10000 11111",
    "M": "10001 11011 10101 10101 10001 10001 10001",
    "N": "10001 10001 11001 10101 10011 10001 10001",
    "O": "01110 10001 10001 10001 10001 10001 01110",
    "P": "11110 10001 10001 11110 10000 10000 10000",
    "Q": "01110 10001 10001 10001 10101 10010 01101",
    "R": "11110 10001 10001 11110 10100 10010 10001",
    "S": "01111 10000 10000 01110 00001 00001 11110",
    "T": "11111 00100 00100 00100 00100 00100 00100",
    "U": "10001 10001 10001 10001 10001 10001 01110",
    "V": "10001 10001 10001 10001 10001 01010 00100",
    "W": "10001 10001 10001 10101 10101 10101 01010",
    "X": "10001 10001 01010 00100 01010 10001 10001",
    "Y": "10001 10001 01010 00100 00100 00100 00100",
    "Z": "11111 00001 00010 00100 01000 10000 11111",
    "0": "01110 10001 10011 10101 11001 10001 01110",
    "1": "00100 01100 00100 00100 00100 00100 01110",
    "2": "01110 10001 00001 00010 00100 01000 11111",
    "3": "11111 00010 00100 00010 00001 10001 01110",
    "4": "00010 00110 01010 10010 11111 00010 00010",
    "5": "11111 10000 11110 00001 00001 10001 01110",
    "6": "00110 01000 10000 11110 10001 10001 01110",
    "7": "11111 00001 00010 00100 01000 01000 01000",
    "8": "01110 10001 10001 01110 10001 10001 01110",
    "9": "01110 10001 10001 01111 00001 00010 01100",
    "-": "00000 00000 00000 11111 00000 00000 00000",
    "_": "00000 00000 00000 00000 00000 00000 11111",
    ".": "00000 00000 00000 00000 00000 01100 01100",
    ":": "00000 01100 01100 00000 01100 01100 00000",
    "(": "00010 00100 01000 01000 01000 00100 00010",
    ")": "01000 00100 00010 00010 00010 00100 01000",
    "/": "00000 00001 00010 00100 01000 10000 00000",
    "#": "01010 01010 11111 01010 11111 01010 01010",
    "+": "00000 00100 00100 11111 00100 00100 00000",
    "?": "01110 10001 00001 00010 00100 00000 00100",
}


def _glyph(character: str) -> NDArray[np.bool_]:
    rows = _GLYPHS.get(character.upper(), _GLYPHS["?"]).split()
    return np.array([[bit == "1" for bit in row] for row in rows], dtype=bool)


def text_mask(text: str, scale: int = 1) -> NDArray[np.bool_]:
    """The pixels of ``text`` at ``scale``, one blank column between characters."""

    if scale < 1:
        raise ValueError("scale must be at least 1")
    if not text:
        return np.zeros((_GLYPH_ROWS * scale, 0), dtype=bool)
    gap = np.zeros((_GLYPH_ROWS, 1), dtype=bool)
    pieces: list[NDArray[np.bool_]] = []
    for index, character in enumerate(text):
        if index:
            pieces.append(gap)
        pieces.append(_glyph(character))
    mask = np.concatenate(pieces, axis=1)
    return np.kron(mask, np.ones((scale, scale), dtype=bool)).astype(bool)


def stamp_text(
    image: NDArray[np.uint8],
    text: str,
    *,
    top: int,
    left: int,
    color: tuple[int, int, int],
    scale: int = 1,
) -> None:
    """Draw ``text`` into an RGB image in place, with a dark outline for legibility.

    Parts falling outside the image are clipped.
    """

    mask = text_mask(text, scale)
    # One-pixel dark halo around every stroke so labels read on bright and dark tissue.
    padded = np.pad(mask, 1)
    halo = np.zeros_like(padded)
    for dy in (-1, 0, 1):
        for dx in (-1, 0, 1):
            halo |= np.roll(np.roll(padded, dy, axis=0), dx, axis=1)
    for layer, value in ((halo, (0, 0, 0)), (padded, color)):
        height, width = layer.shape
        y0, x0 = top - 1, left - 1
        ys = slice(max(0, y0), min(image.shape[0], y0 + height))
        xs = slice(max(0, x0), min(image.shape[1], x0 + width))
        if ys.start >= ys.stop or xs.start >= xs.stop:
            return
        clipped = layer[ys.start - y0 : ys.stop - y0, xs.start - x0 : xs.stop - x0]
        image[ys, xs][clipped] = value
