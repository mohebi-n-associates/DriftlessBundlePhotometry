"""Stops the mouse wheel from changing values while the operator scrolls a page."""

from __future__ import annotations

from PySide6.QtCore import QEvent, QObject, QPointF
from PySide6.QtGui import QWheelEvent
from PySide6.QtWidgets import (
    QAbstractScrollArea,
    QAbstractSlider,
    QAbstractSpinBox,
    QApplication,
    QComboBox,
    QScrollBar,
    QWidget,
)

_GUARDED = (QAbstractSlider, QAbstractSpinBox, QComboBox)


class WheelGuard(QObject):
    """Gives the wheel to the page instead of to sliders, spin boxes, and dropdowns.

    Qt changes these controls' values when the wheel turns over them, so scrolling a page
    could alter an LED power or a camera setting unnoticed. The guard takes every wheel
    event aimed at one, focused or not, and scrolls the scroll area that holds the control
    instead. Scroll bars, and the open list of a dropdown, keep the wheel.
    """

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if (
            event.type() != QEvent.Type.Wheel
            or not isinstance(watched, _GUARDED)
            or isinstance(watched, QScrollBar)
            or not isinstance(event, QWheelEvent)
        ):
            return False
        area = _scroll_area_holding(watched)
        if area is not None:
            viewport = area.viewport()
            global_position = event.globalPosition()
            forwarded = QWheelEvent(
                QPointF(viewport.mapFromGlobal(global_position.toPoint())),
                global_position,
                event.pixelDelta(),
                event.angleDelta(),
                event.buttons(),
                event.modifiers(),
                event.phase(),
                event.inverted(),
            )
            QApplication.sendEvent(viewport, forwarded)
        # Accepted, so Qt does not also offer the event to the control's parents.
        event.accept()
        return True


def _scroll_area_holding(widget: QWidget) -> QAbstractScrollArea | None:
    """The nearest scroll area the widget sits in, not one the widget itself owns."""

    parent = widget.parentWidget()
    while parent is not None:
        if isinstance(parent, QAbstractScrollArea):
            return parent
        if parent.isWindow():
            return None
        parent = parent.parentWidget()
    return None


def install_wheel_guard() -> WheelGuard | None:
    """Guard every window of the running application; safe to call more than once."""

    app = QApplication.instance()
    if app is None:
        return None
    guard = app.findChild(WheelGuard, "dbfWheelGuard")
    if guard is None:
        guard = WheelGuard(app)
        guard.setObjectName("dbfWheelGuard")
        app.installEventFilter(guard)
    return guard
