"""Batched trace-row markers: TTL edge lines and shaded invalid spans.

A long recording can hold thousands of TTL edges. Drawn as one pyqtgraph item per edge
on every row, they made each repaint re-examine every item; one item per row and TTL
input paints only the edges in view, at most one line per pixel column. Display only:
recorded edges and spans are unaffected.
"""

from __future__ import annotations

from collections.abc import Sequence

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import QLineF, QRectF, Qt
from PySide6.QtGui import QBrush, QPainter, QPen


class _ViewSpanningItem(pg.GraphicsObject):
    """An item that covers its view: its bounds follow the visible range."""

    def __init__(self) -> None:
        super().__init__()
        self.setAcceptedMouseButtons(Qt.MouseButton.NoButton)

    def boundingRect(self) -> QRectF:
        view = self.viewRect()
        return QRectF() if view is None else view.normalized()

    def viewTransformChanged(self) -> None:
        self.prepareGeometryChange()

    def dataBounds(self, axis: int, frac: float = 1.0, orthoRange: object = None) -> None:
        return None  # markers never widen auto-range

    def _pixel_width(self) -> float:
        width = self.pixelWidth()
        return width if width and np.isfinite(width) and width > 0 else 0.0


class VerticalLinesItem(_ViewSpanningItem):
    """Full-height lines at sorted x positions, painted in one pass."""

    def __init__(self, pen: QPen) -> None:
        super().__init__()
        self._pen = pen
        self.positions = np.empty(0, dtype=np.float64)

    def set_positions(self, positions: Sequence[float] | np.ndarray) -> None:
        self.positions = np.sort(np.asarray(positions, dtype=np.float64))
        self.update()

    def visible_positions(self) -> np.ndarray:
        """The x positions to draw: those in view, one per pixel column."""

        view = self.viewRect()
        if view is None or not len(self.positions):
            return np.empty(0, dtype=np.float64)
        view = view.normalized()
        low = int(np.searchsorted(self.positions, view.left(), side="left"))
        high = int(np.searchsorted(self.positions, view.right(), side="right"))
        shown = self.positions[low:high]
        pixel = self._pixel_width()
        if pixel and len(shown) > 1:
            columns = np.floor((shown - view.left()) / pixel)
            _unique, first = np.unique(columns, return_index=True)
            shown = shown[first]
        return shown

    def paint(self, painter: QPainter, *_args: object) -> None:
        view = self.viewRect()
        shown = self.visible_positions()
        if view is None or not len(shown):
            return
        view = view.normalized()
        painter.setPen(self._pen)
        painter.drawLines([QLineF(x, view.top(), x, view.bottom()) for x in shown])


class ShadedSpansItem(_ViewSpanningItem):
    """Full-height shaded spans, each with a reason shown on hover."""

    def __init__(self, brush: QBrush) -> None:
        super().__init__()
        self._brush = brush
        self.spans: list[tuple[float, float, str]] = []
        self.setAcceptHoverEvents(True)

    def set_spans(self, spans: Sequence[tuple[float, float, str]]) -> None:
        self.spans = sorted(spans)
        self.update()

    def reasons_at(self, x: float) -> list[str]:
        return [reason for start, stop, reason in self.spans if start <= x <= stop]

    def hoverMoveEvent(self, event: object) -> None:
        reasons = self.reasons_at(event.pos().x())
        self.setToolTip("\n".join(f"Invalid: {reason}" for reason in reasons))
        super().hoverMoveEvent(event)

    def hoverLeaveEvent(self, event: object) -> None:
        self.setToolTip("")
        super().hoverLeaveEvent(event)

    def paint(self, painter: QPainter, *_args: object) -> None:
        view = self.viewRect()
        if view is None or not self.spans:
            return
        view = view.normalized()
        minimum = self._pixel_width()  # a span narrower than a pixel stays visible
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(self._brush)
        for start, stop, _reason in self.spans:
            if stop < view.left() or start > view.right():
                continue
            painter.drawRect(QRectF(start, view.top(), max(stop - start, minimum), view.height()))
