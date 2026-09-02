from array import array
from pathlib import Path

import pyqtgraph as pg
import pytest
from PySide6.QtCore import QPoint, QPointF, Qt

from driftless_photometry.config import Wavelength, demo_config
from driftless_photometry.gui.main_window import MainWindow
from driftless_photometry.gui.plot_interaction import TraceViewBox, dominant_drag_axis


class _DragEvent:
    def __init__(self, start: QPointF, stop: QPointF) -> None:
        self._start = start
        self._stop = stop
        self.accepted = False

    def button(self) -> Qt.MouseButton:
        return Qt.MouseButton.LeftButton

    def buttonDownPos(self, _button: Qt.MouseButton | None = None) -> QPointF:
        return self._start

    def pos(self) -> QPointF:
        return self._stop

    def isFinish(self) -> bool:
        return True

    def accept(self) -> None:
        self.accepted = True


class _WheelEvent:
    def __init__(self, angle_y: int) -> None:
        self._angle_y = angle_y
        self.accepted = False
        self.ignored = False

    def pixelDelta(self) -> QPoint:
        return QPoint()

    def angleDelta(self) -> QPoint:
        return QPoint(0, self._angle_y)

    def accept(self) -> None:
        self.accepted = True

    def ignore(self) -> None:
        self.ignored = True


class _DoubleClickEvent:
    def __init__(self) -> None:
        self.accepted = False

    def button(self) -> Qt.MouseButton:
        return Qt.MouseButton.LeftButton

    def accept(self) -> None:
        self.accepted = True


def test_dominant_drag_dimension_selects_one_zoom_axis() -> None:
    assert dominant_drag_axis(QPointF(0, 0), QPointF(100, 20)) == "x"
    assert dominant_drag_axis(QPointF(0, 0), QPointF(20, 100)) == "y"
    assert dominant_drag_axis(QPointF(0, 0), QPointF(20, 20)) == "x"


def test_trace_view_box_emits_axis_specific_box_zoom_and_double_click_reset(qtbot) -> None:
    view_box = TraceViewBox()
    plot = pg.PlotWidget(viewBox=view_box)
    qtbot.addWidget(plot)
    plot.resize(400, 250)
    plot.show()
    selected: list[tuple[str, float, float]] = []
    resets: list[bool] = []
    view_box.range_selected.connect(lambda axis, low, high: selected.append((axis, low, high)))
    view_box.reset_requested.connect(lambda: resets.append(True))

    horizontal = _DragEvent(QPointF(50, 80), QPointF(250, 95))
    view_box.mouseDragEvent(horizontal)
    vertical = _DragEvent(QPointF(150, 30), QPointF(165, 190))
    view_box.mouseDragEvent(vertical)
    double_click = _DoubleClickEvent()
    view_box.mouseDoubleClickEvent(double_click)

    assert horizontal.accepted and vertical.accepted
    assert [axis for axis, _low, _high in selected] == ["x", "y"]
    assert all(high > low for _axis, low, high in selected)
    assert double_click.accepted
    assert resets == [True]


def test_trace_wheel_scrolls_acquire_page_instead_of_zooming(qtbot, tmp_path: Path) -> None:
    window = MainWindow(output_directory=tmp_path)
    qtbot.addWidget(window)
    window.show()
    acquire_page = window.tabs.widget(0)
    scroll_bar = acquire_page.verticalScrollBar()
    scroll_bar.setRange(0, 1000)
    scroll_bar.setValue(500)
    event = _WheelEvent(120)

    window.trace_workspace.wheelEvent(event)

    assert event.accepted
    assert not event.ignored
    assert scroll_bar.value() < 500


def test_roi_rows_share_x_zoom_and_double_click_resets_all_rows(qtbot, tmp_path: Path) -> None:
    window = MainWindow(output_directory=tmp_path, default_fibers=3)
    qtbot.addWidget(window)
    window.show()
    config = demo_config(tmp_path, fiber_count=3)
    window._prepare_trace_curves(config)
    for wavelength in Wavelength:
        for roi_index in range(3):
            window._trace_times[(wavelength, roi_index)] = array("d", [0.0, 5.0, 10.0])
            window._trace_values[(wavelength, roi_index)] = array("f", [100.0, 110.0, 105.0])
    window._trace_first_s = 0.0
    window._trace_latest_s = 10.0

    window.trace_view_boxes[1].range_selected.emit("x", 2.0, 8.0)

    assert window._trace_manual_x_range == (2.0, 8.0)
    for plot in window.trace_plots.values():
        low, high = plot.viewRange()[0]
        assert low == pytest.approx(2.0)
        assert high == pytest.approx(8.0)

    window.trace_view_boxes[2].reset_requested.emit()

    assert window._trace_manual_x_range is None
    for plot in window.trace_plots.values():
        low, high = plot.viewRange()[0]
        assert low == pytest.approx(0.0)
        assert high == pytest.approx(10.0)
