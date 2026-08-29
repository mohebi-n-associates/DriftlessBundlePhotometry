from array import array
from pathlib import Path

import numpy as np
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QAbstractSpinBox

from driftless_photometry import __version__
from driftless_photometry.config import SessionConfig, TraceDisplayConfig, Wavelength, demo_config
from driftless_photometry.gui.main_window import MainWindow
from driftless_photometry.settings import save_default_settings


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

    assert window.size().width() == 2300
    assert window.size().height() == 1300
    assert window.minimumWidth() == 1408
    assert window.minimumHeight() == 792
    assert window.tabs.count() == 3
    assert window.tabs.tabText(0) == "Acquire"
    assert window.tabs.tabText(1) == "Camera & fiber ROIs"
    assert window.tabs.tabText(2) == "Live wavelength images"
    assert len(window.trace_plots) == 3
    assert window.system_status.text() == "READY"
    assert window.system_status.property("state") == "ready"
    assert window.backend_badge.text() == "Simulator"
    assert window.backend_badge.property("hardware") is False
    assert window.version_badge.text() == f"v{__version__}"
    assert __version__ == "0.1.5"
    assert not window.windowIcon().isNull()
    assert not window._logo_pixmap.isNull()
    assert window.start_button.objectName() == "record"
    assert window.stop_button.objectName() == "attention"
    assert "QPushButton#attention:disabled" in window.styleSheet()
    assert "QScrollBar::handle:vertical" in window.styleSheet()


def test_gui_removes_stepper_buttons_from_every_spin_box(qtbot, tmp_path: Path) -> None:
    window = MainWindow(output_directory=tmp_path)
    qtbot.addWidget(window)

    spin_boxes = window.findChildren(QAbstractSpinBox)
    assert spin_boxes
    assert all(
        spin_box.buttonSymbols() == QAbstractSpinBox.ButtonSymbols.NoButtons
        for spin_box in spin_boxes
    )


def test_gui_loads_complete_default_settings_at_startup(qtbot, tmp_path: Path) -> None:
    base = demo_config(tmp_path / "loaded-output", fiber_count=2, raw_capture=True)
    rois = (
        base.rois[0].model_copy(
            update={
                "animal_id": "mouse-A",
                "brain_region": "NAc shell",
                "sensor_type": "dLight1.3b",
                "center_x_px": 45.0,
                "center_y_px": 55.0,
                "radius_px": 9.0,
            }
        ),
        base.rois[1].model_copy(
            update={
                "animal_id": "mouse-B",
                "brain_region": "DMS",
                "sensor_type": "GRAB-DA2m",
                "center_x_px": 175.0,
                "center_y_px": 145.0,
                "radius_px": 11.0,
            }
        ),
    )
    channels = tuple(
        channel.model_copy(
            update={
                "enabled": channel.wavelength_nm is not Wavelength.RED_565,
                "voltage_v": {
                    Wavelength.CONTROL_405: 1.05,
                    Wavelength.GREEN_470: 1.25,
                    Wavelength.RED_565: 1.45,
                }[channel.wavelength_nm],
            }
        )
        for channel in base.channels
    )
    payload = base.model_dump()
    payload.update(
        session_description="Restored cohort protocol",
        experimenter="A. Researcher",
        recording_duration_s=42.0,
        lab="Neural Dynamics",
        institution="Example University",
        rois=rois,
        channels=channels,
        display=TraceDisplayConfig(
            horizon_s=600.0,
            visible_wavelengths=(Wavelength.CONTROL_405,),
            mode="dff",
            dff_baseline_s=15.0,
        ),
    )
    expected = SessionConfig.model_validate(payload)
    save_default_settings(expected)

    window = MainWindow(output_directory=tmp_path / "ignored", default_fibers=1)
    qtbot.addWidget(window)
    restored = window._build_config()

    assert window.save_settings_button.text() == "Save JSON…"
    assert window.load_settings_button.text() == "Load JSON…"
    assert window.load_nwb_settings_button.text() == "Load NWB…"
    assert window.default_settings_button.text() == "Set as default"
    assert window.fiber_count_spin.value() == 2
    assert window._roi_metadata_editors[0].animal_id_edit.text() == "mouse-A"
    assert window._roi_metadata_editors[1].brain_region_edit.text() == "DMS"
    assert window.trace_mode_combo.currentData() == "dff"
    assert window._trace_horizon_s == 600.0
    assert not window.trace_wavelength_checks[Wavelength.GREEN_470].isChecked()
    assert restored.model_dump(exclude={"session_id", "session_start_time"}) == (
        expected.model_dump(exclude={"session_id", "session_start_time"})
    )


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
    assert len(window.trace_plots) == 5


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


def test_gui_overlays_wavelengths_per_roi_with_visibility_controls(qtbot, tmp_path: Path) -> None:
    window = MainWindow(output_directory=tmp_path, default_fibers=2)
    qtbot.addWidget(window)
    config = demo_config(tmp_path, fiber_count=2)
    window._prepare_trace_curves(config)

    assert len(window.trace_plots) == 2
    assert len(window._curves) == 6
    assert all(len(plot.listDataItems()) == 3 for plot in window.trace_plots.values())
    assert "animal-01" in window.trace_plots[0].titleLabel.text

    window.trace_wavelength_checks[Wavelength.GREEN_470].setChecked(False)
    assert all(
        not window._curves[(Wavelength.GREEN_470, roi_index)].isVisible() for roi_index in range(2)
    )
    assert all(
        window._curves[(Wavelength.CONTROL_405, roi_index)].isVisible() for roi_index in range(2)
    )


def test_gui_dff_normalizes_each_roi_and_wavelength_independently(qtbot, tmp_path: Path) -> None:
    window = MainWindow(output_directory=tmp_path, default_fibers=1)
    qtbot.addWidget(window)
    config = demo_config(tmp_path, fiber_count=1)
    window._prepare_trace_curves(config)
    times = np.asarray([0.0, 1.0, 2.0], dtype=np.float64)
    baselines = {
        Wavelength.CONTROL_405: 100.0,
        Wavelength.GREEN_470: 1_000.0,
        Wavelength.RED_565: 10_000.0,
    }
    for wavelength, baseline in baselines.items():
        window._trace_times[wavelength] = array("d", times)
        window._trace_values[(wavelength, 0)] = array("f", [baseline, baseline, baseline * 1.1])
    window._trace_first_s = 0.0
    window._trace_latest_s = 2.0
    window.trace_baseline_spin.setValue(0.5)
    window.trace_mode_combo.setCurrentIndex(window.trace_mode_combo.findData("dff"))

    assert window.trace_baseline_spin.isEnabled()
    for wavelength in Wavelength:
        _, displayed = window._curves[(wavelength, 0)].getData()
        np.testing.assert_allclose(displayed, [0.0, 0.0, 10.0], atol=1e-5)

    window.trace_mode_combo.setCurrentIndex(window.trace_mode_combo.findData("absolute"))
    assert not window.trace_baseline_spin.isEnabled()
    _, absolute = window._curves[(Wavelength.RED_565, 0)].getData()
    np.testing.assert_allclose(absolute, [10_000.0, 10_000.0, 11_000.0])
