"""Coordinated trace zooming and page-scroll behavior."""

from __future__ import annotations

import weakref
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
        # Views drawn on a second y-axis of the same row; a vertical zoom takes the same
        # slice of the plot height on each of them. Held weakly: they refer back to this
        # view, and a Python reference cycle between Qt objects must not form.
        self.linked_y_views: weakref.WeakSet[pg.ViewBox] = weakref.WeakSet()

    def zoom_y(self, low: float, high: float) -> None:
        """Show ``low``-``high`` here and the same height fraction on linked y views."""

        own_low, own_high = self.viewRange()[1]
        span = own_high - own_low
        if span > 0:
            for view in list(self.linked_y_views):
                view_low, view_high = view.viewRange()[1]
                view_span = view_high - view_low
                view.setYRange(
                    view_low + (low - own_low) / span * view_span,
                    view_low + (high - own_low) / span * view_span,
                    padding=0.0,
                )
        self.setYRange(low, high, padding=0.0)

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
                self.zoom_y(low, high)
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


class SecondaryTraceViewBox(pg.ViewBox):
    """A trace row's right-axis view: x follows the row's main view, y is its own.

    It is a child of the plot item, so it lays out, hides, and is removed with its row,
    and it sits just under the main view so every mouse gesture still reaches the main
    view; that view forwards vertical zooms here through ``linked_y_views``.
    """

    def __init__(self, plot: pg.PlotItem) -> None:
        super().__init__(enableMenu=False)
        self.setMouseEnabled(x=False, y=False)
        self.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
        self.setAcceptHoverEvents(False)
        self._main = weakref.ref(plot.vb)
        self.setParentItem(plot)
        self.setZValue(plot.vb.zValue() - 1)
        self.setXLink(plot.vb)
        axis = plot.getAxis("right")
        axis.linkToView(self)
        axis.setGrid(False)  # the left axis's grid serves the whole row
        plot.vb.sigResized.connect(self.follow_main_view)
        if isinstance(plot.vb, TraceViewBox):
            plot.vb.linked_y_views.add(self)
        self.follow_main_view()

    def follow_main_view(self, *_args: object) -> None:
        main = self._main()
        if main is not None:
            self.setGeometry(main.geometry())


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
