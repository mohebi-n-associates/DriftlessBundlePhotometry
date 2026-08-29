"""Coordinated trace zooming and page-scroll behavior."""

from __future__ import annotations

from typing import Literal

import pyqtgraph as pg
from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtWidgets import QScrollArea


def dominant_drag_axis(start: QPointF, stop: QPointF) -> Literal["x", "y"]:
    """Choose one zoom axis; the longer drag dimension wins."""

    return "x" if abs(stop.x() - start.x()) >= abs(stop.y() - start.y()) else "y"


class TraceViewBox(pg.ViewBox):
    """ViewBox with FLIP-style one-axis range-box zoom and reset."""

    range_selected = Signal(str, float, float)
    reset_requested = Signal()

    def __init__(self) -> None:
        super().__init__(enableMenu=False)
        self.setMouseMode(self.RectMode)

    def mouseDragEvent(self, event: object, axis: int | None = None) -> None:
        if axis is not None or event.button() != Qt.MouseButton.LeftButton:
            super().mouseDragEvent(event, axis=axis)
            return
        event.accept()
        start = event.buttonDownPos(Qt.MouseButton.LeftButton)
        stop = event.pos()
        if not event.isFinish():
            self.updateScaleBox(start, stop)
            return

        self.rbScaleBox.hide()
        if max(abs(stop.x() - start.x()), abs(stop.y() - start.y())) < 8:
            return
        data_rect = self.childGroup.mapRectFromParent(QRectF(start, stop)).normalized()
        selected_axis = dominant_drag_axis(start, stop)
        if selected_axis == "x":
            low, high = data_rect.left(), data_rect.right()
            if high > low:
                self.setXRange(low, high, padding=0.0)
                self.range_selected.emit("x", low, high)
        else:
            low, high = data_rect.top(), data_rect.bottom()
            if high > low:
                self.setYRange(low, high, padding=0.0)
                self.range_selected.emit("y", low, high)

    def mouseDoubleClickEvent(self, event: object) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            event.accept()
            self.rbScaleBox.hide()
            self.reset_requested.emit()
            return
        super().mouseDoubleClickEvent(event)

    def wheelEvent(self, event: object, axis: int | None = None) -> None:
        event.ignore()


class ScrollableTraceWorkspace(pg.GraphicsLayoutWidget):
    """Send wheel movement to the containing page instead of zooming plots."""

    def _containing_scroll_area(self) -> QScrollArea | None:
        parent = self.parentWidget()
        while parent is not None:
            if isinstance(parent, QScrollArea):
                return parent
            parent = parent.parentWidget()
        return None

    def wheelEvent(self, event: object) -> None:
        area = self._containing_scroll_area()
        if area is None:
            event.ignore()
            return
        pixel_delta = event.pixelDelta().y()
        angle_delta = event.angleDelta().y()
        if pixel_delta:
            distance = pixel_delta
        elif angle_delta:
            distance = angle_delta / 120.0 * area.verticalScrollBar().singleStep() * 3
        else:
            event.ignore()
            return
        bar = area.verticalScrollBar()
        bar.setValue(bar.value() - round(distance))
        event.accept()
