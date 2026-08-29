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
    assert len(window.last_result.reports) == 2
    assert all(report.path.exists() for report in window.last_result.reports)
    assert window.last_result.report.frame_count == 3
    assert window.start_button.isEnabled()
    assert not window.stop_button.isEnabled()
    assert window.system_status.text() == "COMPLETE"
    assert window.system_status.property("state") == "complete"
    assert window.progress_bar.value() == 1000
    for wavelength in window.wavelength_image_items:
        assert window.wavelength_image_items[wavelength].image.shape == (256, 256)
        assert window.wavelength_frame_labels[wavelength].text().startswith("Frame ")


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

    assert window.tabs.count() == 3
    assert window.tabs.tabText(0) == "Acquire"
    assert window.tabs.tabText(1) == "Camera & fiber ROIs"
    assert window.tabs.tabText(2) == "Live wavelength images"
    assert window.system_status.text() == "READY"
    assert window.system_status.property("state") == "ready"
    assert window.backend_badge.text() == "Simulator"
    assert window.backend_badge.property("hardware") is False
    assert window.version_badge.text() == f"v{__version__}"
    assert __version__ == "0.1.3"
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
    assert window.format_summary.value_label.text() == "5"


def test_gui_uses_voltage_sliders_and_per_roi_subject_metadata(qtbot, tmp_path: Path) -> None:
    window = MainWindow(output_directory=tmp_path, default_fibers=2)
    qtbot.addWidget(window)

    slider = next(iter(window.channel_voltages.values()))
    assert slider.orientation() == Qt.Orientation.Horizontal
    assert slider.minimum() == 0
    assert slider.maximum() == 500
    slider.setValue(175)
    assert next(iter(window.channel_voltage_labels.values())).text() == "1.75 V"

    assert window.roi_metadata_tabs.count() == 2
    first = window._roi_metadata_editors[0]
    second = window._roi_metadata_editors[1]
    first.animal_id_edit.setText("mouse-A")
    first.brain_region_edit.setText("NAc shell")
    first.sensor_type_edit.setText("dLight1.3b")
    second.animal_id_edit.setText("mouse-B")
    second.brain_region_edit.setText("DMS")
    second.sensor_type_edit.setText("GRAB-DA2m")

    config = window._build_config()
    assert config.experimenter == window.experimenter_edit.text()
    assert config.channels[0].voltage_v == 1.75
    assert config.rois[0].animal_id == "mouse-A"
    assert config.rois[0].brain_region == "NAc shell"
    assert config.rois[0].sensor_type == "dLight1.3b"
    assert config.rois[1].animal_id == "mouse-B"
