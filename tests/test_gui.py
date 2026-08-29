from pathlib import Path

from PySide6.QtCore import Qt

from driftless_photometry import __version__
from driftless_photometry.gui.main_window import MainWindow


def test_gui_runs_simulator_without_blocking_and_writes_nwb(qtbot, tmp_path: Path) -> None:
    window = MainWindow(
        output_directory=tmp_path,
        default_duration_s=0.1,
        default_fibers=2,
        default_raw=False,
    )
    qtbot.addWidget(window)
    window.show()
    qtbot.mouseClick(window.start_button, Qt.MouseButton.LeftButton)
    qtbot.waitUntil(
        lambda: (
            (window.last_result is not None or window.last_error is not None)
            and window._thread is None
        ),
        timeout=15_000,
    )
    assert window.last_error is None
    assert window.last_result is not None
    assert window.last_result.report.path.exists()
    assert window.last_result.report.frame_count == 3
    assert window.start_button.isEnabled()
    assert not window.stop_button.isEnabled()
    assert window.system_status.text() == "COMPLETE"
    assert window.system_status.property("state") == "complete"
    assert window.progress_bar.value() == 1000


def test_gui_roi_count_rebuilds_calibration_circles(qtbot, tmp_path: Path) -> None:
    window = MainWindow(output_directory=tmp_path, default_fibers=3)
    qtbot.addWidget(window)
    assert len(window._roi_items) == 3
    window.fiber_count_spin.setValue(9)
    assert len(window._roi_items) == 9
    assert "Fiber 9" in window.roi_summary.text()


def test_gui_uses_driftless_workflow_structure_and_state_styling(qtbot, tmp_path: Path) -> None:
    window = MainWindow(output_directory=tmp_path)
    qtbot.addWidget(window)
    window.show()

    assert window.tabs.count() == 2
    assert window.tabs.tabText(0) == "Acquire"
    assert window.tabs.tabText(1) == "Camera & fiber ROIs"
    assert window.system_status.text() == "READY"
    assert window.system_status.property("state") == "ready"
    assert window.backend_badge.text() == "Simulator"
    assert window.backend_badge.property("hardware") is False
    assert window.version_badge.text() == f"v{__version__}"
    assert __version__ == "0.1.0"
    assert not window.windowIcon().isNull()
    assert not window._logo_pixmap.isNull()
    assert window.start_button.objectName() == "record"
    assert window.stop_button.objectName() == "attention"
    assert "QPushButton#attention:disabled" in window.styleSheet()


def test_gui_overview_tracks_channel_and_fiber_configuration(qtbot, tmp_path: Path) -> None:
    window = MainWindow(output_directory=tmp_path, default_fibers=3)
    qtbot.addWidget(window)

    assert window.channels_summary.value_label.text() == "3"
    assert window.fibers_summary.value_label.text() == "3"
    next(iter(window.channel_checks.values())).setChecked(False)
    window.fiber_count_spin.setValue(5)

    assert window.channels_summary.value_label.text() == "2"
    assert window.fibers_summary.value_label.text() == "5"
