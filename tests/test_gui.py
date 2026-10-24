from array import array
from pathlib import Path

import numpy as np
import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import QAbstractSpinBox, QMessageBox
from rwd_bench import Bench

from driftless_photometry import __version__
from driftless_photometry.config import (
    CUSTOM_PROFILE,
    RWD_PROFILE,
    RWDSourceConfig,
    RWDTTLOutputConfig,
    SessionConfig,
    TraceDisplayConfig,
    Wavelength,
    demo_config,
)
from driftless_photometry.diagnostics import (
    FinalizationProgress,
    FinalizationStage,
)
from driftless_photometry.gui.main_window import MainWindow
from driftless_photometry.hardware.rwd_controller_protocol import RWDLed
from driftless_photometry.settings import save_default_settings


def run_preview_until_checked(window, qtbot, *, timeout: int = 15_000) -> None:
    """Start the open-ended preview, let every wavelength show twice, then stop it."""

    window.start_preview()
    wanted = 2 * len(window._build_config().enabled_channels) + 2
    qtbot.waitUntil(
        lambda: sum(window._camera_frame_counts.values()) >= wanted or window._thread is None,
        timeout=timeout,
    )
    window.stop_recording()
    qtbot.waitUntil(lambda: window._thread is None, timeout=timeout)


def test_gui_runs_simulator_without_blocking_and_writes_nwb(qtbot, tmp_path: Path) -> None:
    window = MainWindow(
        output_directory=tmp_path,
        default_duration_s=0.1,
        default_fibers=2,
        default_raw=False,
    )
    qtbot.addWidget(window)
    window.show()
    assert not window.start_button.isEnabled()
    assert window.preview_button.isEnabled()
    qtbot.mouseClick(window.preview_button, Qt.MouseButton.LeftButton)
    assert not window.preview_button.isEnabled() and window.stop_button.isEnabled()
    qtbot.waitUntil(lambda: sum(window._camera_frame_counts.values()) >= 8, timeout=5_000)
    qtbot.mouseClick(window.stop_button, Qt.MouseButton.LeftButton)
    qtbot.waitUntil(
        lambda: (
            (window.last_preview is not None or window.last_error is not None)
            and window._thread is None
        ),
        timeout=5_000,
    )
    assert window.preview_button.isEnabled()
    assert window.last_error is None
    assert window.last_preview is not None
    assert window.start_button.isEnabled()
    assert window.preview_status.property("state") == "passed"
    assert not list(tmp_path.glob("*.nwb"))
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
    assert not window.start_button.isEnabled()
    assert window.preview_button.isEnabled()
    assert not window.stop_button.isEnabled()
    assert window.system_status.text() == "COMPLETE"
    assert window.system_status.property("state") == "complete"
    assert window.progress_bar.value() == 1000
    for wavelength in CUSTOM_PROFILE.wavelengths:
        assert window.wavelength_image_items[wavelength].image.shape == (256, 256)
        assert window.wavelength_frame_labels[wavelength].text().startswith("Frame ")


@pytest.mark.parametrize("operation_state", ["recording", "draining"])
def test_gui_close_requests_safe_stop_and_waits_for_worker(
    qtbot,
    tmp_path: Path,
    operation_state: str,
) -> None:
    class RunningThread:
        def isRunning(self) -> bool:
            return True

    class StopTrackingWorker:
        def __init__(self) -> None:
            self.stop_requested = False

        def request_stop(self) -> None:
            self.stop_requested = True

    window = MainWindow(output_directory=tmp_path)
    qtbot.addWidget(window)
    worker = StopTrackingWorker()
    window._thread = RunningThread()
    window._worker = worker
    window._operation_kind = "recording"
    if operation_state == "draining":
        window._on_state("draining")
    event = QCloseEvent()

    window.closeEvent(event)

    assert event.isAccepted() is False
    assert window._close_pending is True
    assert worker.stop_requested is True
    assert window.stop_button.isEnabled() is False
    window._thread = None
    window._worker = None
    window._close_pending = False


def test_gui_presents_structured_finalization_progress(qtbot, tmp_path: Path) -> None:
    window = MainWindow(output_directory=tmp_path)
    qtbot.addWidget(window)

    window._on_finalization_progress(
        FinalizationProgress(
            stage=FinalizationStage.VALIDATING,
            completed_roi_files=1,
            total_roi_files=3,
            fiber_id="fiber_02",
        )
    )

    assert "Validating partial NWB for fiber_02" in window.live_status.text()
    assert "1/3 ROI files complete" in window.statusBar().currentMessage()


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

    assert window.size().width() <= 1366
    assert window.size().height() <= 768
    assert window.minimumWidth() <= 960
    assert window.minimumHeight() <= 600
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
    assert __version__ == "0.3.9"
    assert not window.windowIcon().isNull()
    assert not window._logo_pixmap.isNull()
    assert window.start_button.objectName() == "record"
    assert window.preview_button.objectName() == "preview"
    assert window.stop_button.objectName() == "attention"
    assert "QPushButton#attention:disabled" in window.styleSheet()
    assert "QScrollBar::handle:vertical" in window.styleSheet()


def test_gui_offers_only_custom_and_rwd_systems(qtbot, tmp_path: Path) -> None:
    window = MainWindow(output_directory=tmp_path, default_fibers=2)
    qtbot.addWidget(window)
    window.show()

    systems = [window.source_combo.itemData(i) for i in range(window.source_combo.count())]
    assert systems == ["custom", "rwd"]
    assert window.source_combo.currentData() == "custom"
    assert not window.rwd_group.isVisible()
    assert window.preview_button.isEnabled()
    assert not window.start_button.isEnabled()

    window.source_combo.setCurrentIndex(window.source_combo.findData("rwd"))

    assert window.rwd_group.isVisible()
    assert window.backend_badge.text() == "RWD hardware"
    assert window.preview_button.isEnabled()
    assert not window.start_button.isEnabled()
    assert "no fail-safe" in window.rwd_safety_label.text()
    assert "power switch" in window.rwd_safety_label.text()
    assert window.rwd_safety_label.isVisible()
    assert window.tabs.isTabVisible(2)
    assert not window.camera_calibration_group.isHidden()
    assert not window.calibration_panel_cam2.isHidden()
    assert window.calibration_panel_titles[1].text() == "CAMERA 1 — 410/470 NM"

    window.source_combo.setCurrentIndex(window.source_combo.findData("custom"))

    assert not window.rwd_group.isVisible()
    assert window.preview_button.isEnabled()
    assert window.backend_badge.text() == "Simulator"
    assert window.calibration_panel_cam2.isHidden()
    assert window.calibration_panel_titles[1].text() == "CAMERA"


def test_gui_round_trips_rwd_port_and_ttl_outputs(qtbot, tmp_path: Path) -> None:
    window = MainWindow(output_directory=tmp_path, default_fibers=2)
    qtbot.addWidget(window)
    source = RWDSourceConfig(
        controller_port="COM7",
        ttl_outputs=(RWDTTLOutputConfig(port=2, frequency_hz=12.5, pulse_width_ms=30),),
    )
    expected = demo_config(tmp_path, fiber_count=2, system="rwd").model_copy(
        update={"source": source}
    )

    window._apply_configuration(expected)
    restored = window._build_config()

    assert window.source_combo.currentData() == "rwd"
    assert restored.source == source
    window.rwd_output_checks[2].setChecked(False)
    window.rwd_port_combo.setCurrentIndex(0)
    assert window._build_config().source == RWDSourceConfig()


def test_gui_edits_tests_and_fires_ttl_output_programs(qtbot, tmp_path: Path, monkeypatch) -> None:
    from driftless_photometry.gui.output_program import OutputProgramDialog

    tested = []

    def tester(config, port):
        tested.append((port, config.source.ttl_outputs))
        return f"Output {port} drove pulses"

    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.StandardButton.Yes)
    window = MainWindow(output_directory=tmp_path, default_fibers=1, output_tester=tester)
    qtbot.addWidget(window)
    window.source_combo.setCurrentIndex(window.source_combo.findData("rwd"))

    dialog = OutputProgramDialog(
        window._rwd_output_programs[2],
        fibers=[("fiber_01", "Fiber 1")],
        wavelengths=[Wavelength.GREEN_470],
    )
    dialog.label_edit.setText("laser")
    dialog.trigger_combo.setCurrentIndex(dialog.trigger_combo.findData("manual"))
    assert not dialog.continuous_check.isEnabled()
    dialog.train_spin.setValue(2.0)
    dialog.cycles_spin.setValue(3)
    dialog.gap_spin.setValue(1.0)
    dialog._add_step(None)
    program = dialog.output()
    assert (program.trigger, program.cycles, len(program.steps)) == ("manual", 3, 1)
    dialog.trigger_combo.setCurrentIndex(dialog.trigger_combo.findData("dff_threshold"))
    assert not dialog.dff_group.isHidden()
    dialog.dff_threshold_spin.setValue(8.0)
    assert dialog.output().dff_trigger.threshold_percent == 8.0

    window.apply_output_program(program)
    assert "laser: 10 Hz, 10 ms · manual · 10 s program" in window.rwd_output_summaries[2].text()
    assert window._build_config().source.ttl_outputs == (program,)
    assert list(window.output_fire_buttons) == [2]
    assert not window.output_fire_buttons[2].isEnabled()

    window.start_output_test(2)
    qtbot.waitUntil(lambda: window._device_thread is None, timeout=5_000)
    assert tested == [(2, (program,))]
    assert window.last_output_test == "Output 2 drove pulses"

    window.source_combo.setCurrentIndex(window.source_combo.findData("custom"))
    assert window.output_fire_buttons == {}


def test_gui_places_camera2_circles_separately(qtbot, tmp_path: Path) -> None:
    window = MainWindow(output_directory=tmp_path, default_fibers=2)
    qtbot.addWidget(window)
    window.show()
    window.source_combo.setCurrentIndex(window.source_combo.findData("rwd"))
    assert len(window._roi_items_cam2) == 2
    before = window._build_config().rois[0]

    plot1 = window.calibration_plot.getPlotItem().items
    plot2 = window.calibration_plot_cam2.getPlotItem().items
    assert all(item in plot1 and item not in plot2 for item in window._roi_items)
    assert all(item in plot2 and item not in plot1 for item in window._roi_items_cam2)
    window._roi_items_cam2[0].setPos(
        [before.center_x_px - before.radius_px + 40, before.center_y_px - before.radius_px]
    )

    after = window._build_config().rois[0]
    assert (after.center_x_px, after.center_y_px) == (before.center_x_px, before.center_y_px)
    assert after.camera2_center_x_px == pytest.approx(before.center_x_px + 40)
    assert after.camera2_center_y_px == pytest.approx(before.center_y_px)
    assert "camera 2: x" in window.roi_summary.text()

    window.source_combo.setCurrentIndex(window.source_combo.findData("custom"))
    assert window._roi_items_cam2 == []
    assert not window.calibration_plot_cam2.getPlotItem().items[1:]


def test_gui_previews_and_records_rwd_hardware(qtbot, tmp_path: Path) -> None:
    benches = []

    def rig_factory(config):
        bench = Bench(config)
        benches.append(bench)
        return bench.rig

    window = MainWindow(output_directory=tmp_path, default_fibers=1, rig_factory=rig_factory)
    qtbot.addWidget(window)
    window.show()
    window.source_combo.setCurrentIndex(window.source_combo.findData("rwd"))
    window.duration_spin.setValue(1.0)

    run_preview_until_checked(window, qtbot)
    assert window.last_error is None, window.last_error
    assert window.last_preview is not None
    assert window.start_button.isEnabled()
    benches[-1].assert_all_off()

    window.warmup_check.setChecked(False)  # the warm-up reminder has its own test
    window.start_recording()
    qtbot.waitUntil(lambda: window._thread is None, timeout=30_000)
    assert window.last_error is None, window.last_error
    assert window.last_result is not None
    assert window.last_result.report.frame_count == 20
    benches[-1].assert_all_off()
    for wavelength in RWD_PROFILE.wavelengths:
        assert window.wavelength_frame_labels[wavelength].text().startswith("Frame ")
    np.testing.assert_array_equal(window._image_item.image, window._last_camera_images[1])
    np.testing.assert_array_equal(window._image_item_cam2.image, window._last_camera_images[2])


def test_gui_display_smoothing_is_time_based_and_does_not_change_raw_buffer(
    qtbot,
    tmp_path: Path,
) -> None:
    window = MainWindow(output_directory=tmp_path, default_fibers=1)
    qtbot.addWidget(window)
    config = demo_config(tmp_path, fiber_count=1)
    window._prepare_trace_curves(config)
    key = (Wavelength.GREEN_470, 0)
    window._trace_times[key] = array("d", [0.0, 0.1, 0.2])
    window._trace_values[key] = array("f", [0.0, 10.0, 20.0])
    window._trace_first_s = 0.0
    window._trace_latest_s = 0.2

    window.trace_smoothing_spin.setValue(0.15)
    window._refresh_trace_wavelength(Wavelength.GREEN_470)

    _, displayed = window._curves[key].getData()
    np.testing.assert_allclose(displayed, [0.0, 5.0, 15.0])
    assert list(window._trace_values[key]) == [0.0, 10.0, 20.0]


def test_gui_setting_change_invalidates_passed_preview(qtbot, tmp_path: Path) -> None:
    window = MainWindow(output_directory=tmp_path)
    qtbot.addWidget(window)
    config = window._build_config()
    window._preview_signature = window._configuration_signature(config)
    window._set_busy_controls(False)

    assert window.start_button.isEnabled()
    window.channel_voltages[Wavelength.GREEN_470].setValue(125)

    assert window._preview_signature is None
    assert not window.start_button.isEnabled()
    assert window.preview_status.property("state") == "required"


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
                "enabled": False,
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
    assert not window._roi_metadata_editors[1].enabled_check.isChecked()
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
    assert window.fibers_summary.value_label.text() == "3/3"
    next(iter(window.channel_checks.values())).setChecked(False)
    window.fiber_count_spin.setValue(5)

    assert window.channels_summary.value_label.text() == "2"
    assert window.fibers_summary.value_label.text() == "5/5"
    assert window.format_summary.value_label.text() == "5"
    assert len(window.trace_plots) == 5


def test_gui_enables_and_disables_each_roi_independently(qtbot, tmp_path: Path) -> None:
    window = MainWindow(output_directory=tmp_path, default_fibers=3)
    qtbot.addWidget(window)

    second = window._roi_metadata_editors[1]
    second.enabled_check.setChecked(False)
    config = window._build_config()

    assert [roi.enabled for roi in config.rois] == [True, False, True]
    assert config.enabled_rois == (config.rois[0], config.rois[2])
    assert window.roi_metadata_tabs.tabText(1) == "ROI 2 (off)"
    assert window._roi_items[1].opacity() == 0.25
    assert "Fiber 2 (disabled)" in window.roi_summary.text()
    assert window.fibers_summary.value_label.text() == "2/3"
    assert window.format_summary.value_label.text() == "2"
    assert len(window.trace_plots) == 2


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
        window._trace_times[(wavelength, 0)] = array("d", times)
        window._trace_values[(wavelength, 0)] = array("f", [baseline, baseline, baseline * 1.1])
    window._trace_first_s = 0.0
    window._trace_latest_s = 2.0
    window.trace_baseline_spin.setValue(0.5)
    window.trace_mode_combo.setCurrentIndex(window.trace_mode_combo.findData("dff"))

    assert window.trace_baseline_spin.isEnabled()
    for wavelength in CUSTOM_PROFILE.wavelengths:
        _, displayed = window._curves[(wavelength, 0)].getData()
        np.testing.assert_allclose(displayed, [0.0, 0.0, 10.0], atol=1e-5)

    window.trace_mode_combo.setCurrentIndex(window.trace_mode_combo.findData("absolute"))
    assert not window.trace_baseline_spin.isEnabled()
    _, absolute = window._curves[(Wavelength.RED_565, 0)].getData()
    np.testing.assert_allclose(absolute, [10_000.0, 10_000.0, 11_000.0])


def test_gui_shows_the_rwd_profile_when_rwd_is_selected(qtbot, tmp_path: Path) -> None:
    window = MainWindow(output_directory=tmp_path, default_fibers=2)
    qtbot.addWidget(window)
    window.show()
    custom_camera = window._session_template.camera

    window.source_combo.setCurrentIndex(window.source_combo.findData("rwd"))

    assert list(window.channel_checks) == list(RWD_PROFILE.wavelengths)
    slider = window.channel_voltages[Wavelength.GREEN_470]
    assert slider.maximum() == 1000
    assert window.channel_voltage_labels[Wavelength.GREEN_470].text() == "5.0 %"
    slider.setValue(125)
    assert window.channel_voltage_labels[Wavelength.GREEN_470].text() == "12.5 %"
    camera = window._session_template.camera
    assert (camera.width_px, camera.height_px) == (728, 544)
    for wavelength, group in window.wavelength_image_groups.items():
        assert group.isHidden() is (wavelength not in RWD_PROFILE.wavelengths)
    for wavelength_nm, check in window.trace_wavelength_checks.items():
        assert check.isVisible() is (wavelength_nm in (410, 470, 560))

    config = window._build_config()
    assert config.source == RWDSourceConfig()
    assert [channel.wavelength_nm for channel in config.channels] == list(RWD_PROFILE.wavelengths)
    green = config.channels[1]
    assert (green.voltage_v, green.power_percent) == (None, 12.5)
    assert all(roi.has_camera2_geometry for roi in config.rois)
    assert set(config.display.visible_wavelengths) <= {410, 470, 560}

    window.source_combo.setCurrentIndex(window.source_combo.findData("custom"))

    assert list(window.channel_checks) == list(CUSTOM_PROFILE.wavelengths)
    assert window._session_template.camera == custom_camera
    config = window._build_config()
    assert config.source != RWDSourceConfig()
    assert all(channel.power_percent is None for channel in config.channels)
    assert not any(roi.has_camera2_geometry for roi in config.rois)


def test_gui_round_trips_rwd_settings_with_camera2_circles(qtbot, tmp_path: Path) -> None:
    window = MainWindow(output_directory=tmp_path, default_fibers=2)
    qtbot.addWidget(window)
    original = demo_config(tmp_path, fiber_count=2, system="rwd")
    payload = original.model_dump()
    payload["channels"][0]["power_percent"] = 7.5
    payload["channels"][2]["enabled"] = False
    payload["rois"][1].update(
        camera2_center_x_px=400.0, camera2_center_y_px=300.0, camera2_radius_px=30.0
    )
    original = SessionConfig.model_validate(payload)

    window._apply_configuration(original)

    assert window.source_combo.currentData() == "rwd"
    assert window.channel_voltage_labels[Wavelength.CONTROL_410].text() == "7.5 %"
    rebuilt = window._build_config()
    assert [channel.enabled for channel in rebuilt.channels] == [True, True, False]
    assert rebuilt.channels[0].power_percent == 7.5
    assert rebuilt.camera.model_dump(exclude={"raw_capture"}) == original.camera.model_dump(
        exclude={"raw_capture"}
    )
    assert (
        rebuilt.rois[1].camera2_center_x_px,
        rebuilt.rois[1].camera2_center_y_px,
        rebuilt.rois[1].camera2_radius_px,
    ) == (400.0, 300.0, 30.0)


def test_gui_live_view_streams_both_cameras_and_stops_the_hardware(qtbot, tmp_path: Path) -> None:
    benches = []

    def rig_factory(config):
        bench = Bench(config)
        benches.append(bench)
        return bench.rig

    window = MainWindow(output_directory=tmp_path, default_fibers=1, rig_factory=rig_factory)
    qtbot.addWidget(window)
    window.show()
    window.source_combo.setCurrentIndex(window.source_combo.findData("rwd"))

    window.tabs.setCurrentIndex(1)
    assert window.live_button.window() is window
    assert window.tabs.widget(1).isAncestorOf(window.live_button)
    qtbot.mouseClick(window.live_button, Qt.MouseButton.LeftButton)
    assert window.tabs.currentIndex() == 1
    assert window.live_button.isEnabled()
    assert window.live_button.text() == "Stop live view"
    assert window.stop_button.isEnabled() and not window.preview_button.isEnabled()
    qtbot.waitUntil(lambda: {1, 2} <= set(window._last_camera_images), timeout=15_000)
    assert benches[-1].board.state.light_on
    qtbot.mouseClick(window.live_button, Qt.MouseButton.LeftButton)  # stops it
    qtbot.waitUntil(lambda: window._thread is None, timeout=15_000)

    assert window.last_error is None, window.last_error
    benches[-1].assert_all_off()
    assert window.live_button.isEnabled()
    assert window.live_button.text() == "Live view"
    assert "Live view stopped" in window.live_status.text()
    assert not list(tmp_path.glob("*.nwb"))
    assert not list(tmp_path.glob("*.photometry-spool"))
    np.testing.assert_array_equal(window._image_item_cam2.image, window._last_camera_images[2])
    np.testing.assert_array_equal(window._image_item.image, window._last_camera_images[1])


def test_gui_auto_contrast_is_display_only(qtbot, tmp_path: Path) -> None:
    window = MainWindow(output_directory=tmp_path, default_fibers=1)
    qtbot.addWidget(window)
    image = np.full((40, 40), 10, dtype=np.uint16)
    image[10:20, 10:20] = 200

    assert window.auto_contrast_check.isChecked()
    low, high = window._display_levels(image)
    assert low == 10 and 150 < high <= 200
    window.auto_contrast_check.setChecked(False)
    assert window._display_levels(image) == (0.0, 4095.0)
    assert image.max() == 200


def test_gui_camera_controls_follow_the_system_and_round_trip(qtbot, tmp_path: Path) -> None:
    window = MainWindow(output_directory=tmp_path, default_fibers=1)
    qtbot.addWidget(window)
    custom = window._session_template.camera
    assert window.fps_spin.value() == round(custom.requested_fps)
    assert window.exposure_spin.value() == pytest.approx(custom.exposure_us / 1000)

    window.source_combo.setCurrentIndex(window.source_combo.findData("rwd"))
    assert (window.fps_spin.minimum(), window.fps_spin.maximum()) == (10, 250)
    assert window.fps_spin.value() == 20
    assert window.exposure_spin.maximum() == pytest.approx(47.37)
    assert window.exposure_spin.minimum() == pytest.approx(0.5)
    window.fps_spin.setValue(50)
    assert window.exposure_spin.maximum() == pytest.approx(17.37)
    window.exposure_max_button.click()
    window.gain_spin.setValue(12.5)
    assert "17.37 ms" in window.camera_settings_hint.text()

    config = window._build_config()
    assert config.camera.requested_fps == 50.0
    assert config.camera.exposure_us == 17_370
    assert config.camera.gain == 12.5

    window.source_combo.setCurrentIndex(window.source_combo.findData("custom"))
    assert window.fps_spin.value() == round(custom.requested_fps)
    window._apply_configuration(config)
    assert (window.fps_spin.value(), window.gain_spin.value()) == (50, 12.5)
    assert window._build_config().camera == config.camera


def test_gui_live_view_applies_gain_live_and_restarts_for_timing(qtbot, tmp_path: Path) -> None:
    benches = []

    def rig_factory(config):
        bench = Bench(config)
        benches.append(bench)
        return bench.rig

    window = MainWindow(output_directory=tmp_path, default_fibers=1, rig_factory=rig_factory)
    qtbot.addWidget(window)
    window.show()
    window.source_combo.setCurrentIndex(window.source_combo.findData("rwd"))
    window.start_live_view()
    qtbot.waitUntil(lambda: bool(window._last_camera_images), timeout=15_000)
    assert window.camera_settings_group.isEnabled()

    window.gain_spin.setValue(20.0)
    qtbot.waitUntil(
        lambda: benches[-1].cameras is not None and benches[-1].cameras.gain_db == 20.0,
        timeout=15_000,
    )
    assert len(benches) == 1

    window.fps_spin.setValue(25)
    qtbot.waitUntil(lambda: len(benches) == 2 and window._thread is not None, timeout=15_000)
    benches[0].assert_all_off()
    qtbot.waitUntil(lambda: bool(benches[1].board.runs), timeout=15_000)
    assert benches[1].board.runs[0].frame_rate_hz == 25
    window.stop_recording()
    qtbot.waitUntil(lambda: window._thread is None, timeout=15_000)
    benches[1].assert_all_off()
    assert window.last_error is None, window.last_error


def test_gui_device_check_shows_each_device(qtbot, tmp_path: Path) -> None:
    from driftless_photometry.hardware.rwd_devices import DeviceStatus, RWDDeviceReport

    calls = []

    def checker(config):
        calls.append(config)
        return RWDDeviceReport(
            DeviceStatus("Controller", True, "R821 on COM3, firmware 2.0.0, build 37619"),
            (
                DeviceStatus("Camera 1 (410/470 nm)", True, "1800 U-040m, serial 0721G"),
                DeviceStatus("Camera 2 (560 nm)", False, "no camera with DeviceUserID 2"),
            ),
        )

    window = MainWindow(output_directory=tmp_path, default_fibers=1, device_checker=checker)
    qtbot.addWidget(window)
    window.source_combo.setCurrentIndex(window.source_combo.findData("rwd"))
    assert calls == []  # tests never auto-check real hardware

    window.start_device_check()
    qtbot.waitUntil(lambda: window._device_thread is None, timeout=10_000)

    assert len(calls) == 1
    assert window.device_labels["controller"].text().startswith("✓ Controller: R821 on COM3")
    assert window.device_labels["camera2"].property("state") == "failed"
    assert window.last_device_report is not None and not window.last_device_report.ok
    assert window.live_button.isEnabled()


def test_gui_checks_devices_automatically_in_the_application(qtbot, tmp_path: Path) -> None:
    from driftless_photometry.hardware.rwd_devices import DeviceStatus, RWDDeviceReport

    calls = []
    ok = DeviceStatus("x", True, "ok")

    def checker(config):
        calls.append(config)
        return RWDDeviceReport(ok, (ok, ok))

    window = MainWindow(
        output_directory=tmp_path, default_fibers=1, device_checker=checker, auto_check_devices=True
    )
    qtbot.addWidget(window)
    window.source_combo.setCurrentIndex(window.source_combo.findData("rwd"))
    qtbot.waitUntil(lambda: len(calls) == 1 and window._device_thread is None, timeout=10_000)
    window.source_combo.setCurrentIndex(window.source_combo.findData("custom"))
    window.source_combo.setCurrentIndex(window.source_combo.findData("rwd"))
    qtbot.waitUntil(lambda: len(calls) == 2 and window._device_thread is None, timeout=10_000)


def test_gui_roi_diameters_can_be_typed_per_camera(qtbot, tmp_path: Path) -> None:
    window = MainWindow(output_directory=tmp_path, default_fibers=2)
    qtbot.addWidget(window)
    window.source_combo.setCurrentIndex(window.source_combo.findData("rwd"))
    editor = window._roi_metadata_editors[0]
    before = window._build_config().rois[0]
    assert editor.diameter_spins[1].value() == pytest.approx(2 * before.radius_px)
    assert (editor.diameter_spins[1].minimum(), editor.diameter_spins[1].maximum()) == (40, 544)

    editor.diameter_spins[1].setValue(80.0)
    editor.diameter_spins[2].setValue(120.0)

    after = window._build_config().rois[0]
    assert after.radius_px == pytest.approx(40.0)
    assert (after.center_x_px, after.center_y_px) == pytest.approx(
        (before.center_x_px, before.center_y_px)
    )
    assert after.camera2_radius_px == pytest.approx(60.0)

    # Dragging a circle smaller than OFRS's 40 px minimum snaps back to it.
    circle = window._roi_items[0]
    circle.setSize([20, 20])
    assert editor.diameter_spins[1].value() == pytest.approx(40.0)
    assert window._build_config().rois[0].radius_px == pytest.approx(20.0)

    window.source_combo.setCurrentIndex(window.source_combo.findData("custom"))
    assert editor.diameter_spins[1].maximum() == 256


def test_gui_live_view_warms_up_the_leds_before_recording(
    qtbot, tmp_path: Path, monkeypatch
) -> None:
    clock = [0.0]
    asked: list[str] = []

    def question(_parent, _title, text, *_args):
        asked.append(text)
        return QMessageBox.StandardButton.No

    monkeypatch.setattr(QMessageBox, "question", question)
    window = MainWindow(
        output_directory=tmp_path,
        default_fibers=1,
        rig_factory=lambda config: Bench(config).rig,
        clock=lambda: clock[0],
    )
    qtbot.addWidget(window)
    window.source_combo.setCurrentIndex(window.source_combo.findData("rwd"))
    assert "LEDs cold" in window.warmup_label.text()

    # Cold LEDs: the reminder appears and "No" leaves recording unstarted.
    window._preview_signature = window._configuration_signature(window._build_config())
    window.start_recording()
    assert window._thread is None
    assert "have been on for 0:00" in asked[-1]

    window.start_live_view()
    qtbot.waitUntil(lambda: window._warmup.leds_are_on, timeout=15_000)
    clock[0] += 120
    window._update_warmup_label()
    assert "Warming up: 2:00 of 5:00" in window.warmup_label.text()
    clock[0] += 180
    window._update_warmup_label()
    assert window.warmup_label.text().startswith("✓ LEDs warmed up")
    window.stop_recording()
    qtbot.waitUntil(lambda: window._thread is None, timeout=15_000)
    assert not window._warmup.leds_are_on

    clock[0] += 60  # a preview's worth of gap keeps the warm-up
    asked.clear()
    assert window._confirm_warmup() and not asked
    clock[0] += 200
    assert not window._confirm_warmup() and asked
    window.warmup_check.setChecked(False)
    assert window._confirm_warmup()


def test_gui_simulator_runs_do_not_count_as_led_warm_up(qtbot, tmp_path: Path) -> None:
    window = MainWindow(output_directory=tmp_path, default_fibers=1)
    qtbot.addWidget(window)
    window.start_live_view()
    qtbot.waitUntil(lambda: bool(window._last_camera_images), timeout=15_000)
    assert not window._warmup.leds_are_on
    window.stop_recording()
    qtbot.waitUntil(lambda: window._thread is None, timeout=15_000)


def test_gui_start_and_stop_options_round_trip_without_a_new_preview(qtbot, tmp_path: Path) -> None:
    window = MainWindow(output_directory=tmp_path, default_fibers=1)
    qtbot.addWidget(window)
    assert window.start_delay_spin.isHidden() and window.stop_ttl_line_combo.isHidden()
    window._preview_signature = window._configuration_signature(window._build_config())

    window.start_mode_combo.setCurrentIndex(window.start_mode_combo.findData("ttl"))
    window.start_ttl_line_combo.setCurrentIndex(2)
    window.start_ttl_edge_combo.setCurrentIndex(1)
    window.stop_mode_combo.setCurrentIndex(window.stop_mode_combo.findData("ttl"))
    window.stop_ttl_line_combo.setCurrentIndex(1)
    assert not window.start_ttl_line_combo.isHidden() and window.start_delay_spin.isHidden()
    assert window._duration_label.text() == "Maximum length"

    config = window._build_config()
    assert config.recording.model_dump() == {
        "start": "ttl",
        "start_delay_s": 10.0,
        "start_ttl_line": 3,
        "start_ttl_edge": "falling",
        "stop": "ttl",
        "stop_ttl_line": 2,
        "stop_ttl_edge": "rising",
        "excitation": "continuous",
        "excitation_on_s": 60.0,
        "excitation_off_s": 240.0,
    }
    assert window._preview_signature == window._configuration_signature(config)

    assert window.excitation_on_spin.isHidden()
    window.excitation_mode_combo.setCurrentIndex(1)
    window.excitation_on_spin.setValue(30)
    assert not window.excitation_off_spin.isHidden()
    sequential = window._build_config().recording
    assert (sequential.excitation, sequential.excitation_on_s) == ("sequential", 30.0)
    window.excitation_mode_combo.setCurrentIndex(0)
    window.stop_mode_combo.setCurrentIndex(window.stop_mode_combo.findData("manual"))
    assert window.duration_spin.isEnabled()
    assert window._duration_label.text() == "Expected length"
    assert window._build_config().recording_limit_s == 24 * 60 * 60

    window._apply_configuration(config)
    assert window._build_config().recording == config.recording
    assert window.duration_spin.isEnabled()


def test_gui_cancels_a_recording_still_waiting_to_start(qtbot, tmp_path: Path) -> None:
    window = MainWindow(output_directory=tmp_path, default_fibers=1, default_duration_s=1.0)
    qtbot.addWidget(window)
    run_preview_until_checked(window, qtbot)
    window.start_mode_combo.setCurrentIndex(window.start_mode_combo.findData("delayed"))
    window.start_delay_spin.setValue(60.0)
    assert window.start_button.isEnabled()

    window.start_recording()
    qtbot.waitUntil(lambda: "Waiting for a 60 s delay" in window.live_status.text(), timeout=5_000)
    window.stop_recording()
    qtbot.waitUntil(lambda: window._thread is None, timeout=15_000)

    assert window.last_error is None
    assert window.last_result is None
    assert "nothing was recorded" in window.live_status.text()
    assert window.start_button.isEnabled()  # the passed preview still counts
    assert not list(tmp_path.rglob("*.nwb"))
    assert not list(tmp_path.rglob("*.photometry-spool"))


def test_gui_records_markers_and_named_ttl_events_and_draws_them(qtbot, tmp_path: Path) -> None:
    import pyqtgraph as pg
    from pynwb import NWBHDF5IO

    window = MainWindow(output_directory=tmp_path, default_fibers=1, default_duration_s=2.5)
    qtbot.addWidget(window)
    window.marker_name_edits["1"].setText("Tone")
    check, line, low, high, name = window.ttl_event_rows[0]
    check.setChecked(True)
    line.setCurrentIndex(line.findData(1))
    low.setValue(50)
    high.setValue(150)
    name.setText("Lever")
    assert list(window.marker_buttons) == ["1"]
    assert not window.marker_buttons["1"].isEnabled()
    run_preview_until_checked(window, qtbot)

    window.start_recording()
    qtbot.waitUntil(lambda: window._trace_latest_s is not None, timeout=10_000)
    assert window.marker_buttons["1"].isEnabled()
    window.mark_event("1", "key")
    qtbot.mouseClick(window.marker_buttons["1"], Qt.MouseButton.LeftButton)
    window.mark_event("7", "key")  # no marker on key 7
    qtbot.waitUntil(lambda: window._thread is None, timeout=30_000)

    assert window.last_error is None, window.last_error
    with NWBHDF5IO(window.last_result.report.path, mode="r", load_namespaces=True) as io:
        marks = io.read().events["event_marks"].to_dataframe()
    operator = marks[marks["source"] != "ttl"]
    assert sorted(operator["source"]) == ["button", "key"]
    assert set(operator["event_name"]) == {"Tone"}
    ttl = marks[marks["source"] == "ttl"]
    assert ttl["timestamp"].round(6).tolist() == [0.0, 1.0, 2.0]
    assert set(ttl["event_name"]) == {"Lever"}
    assert "pulse of 100 ms" in ttl["detail"].iloc[0]
    # Event marks, not the thinner TTL edge lines drawn beside them.
    drawn = [
        item
        for item in window.trace_plots[0].items
        if isinstance(item, pg.InfiniteLine) and item.pen.widthF() != 1
    ]
    assert len(drawn) == len(window._event_marks_shown) >= 4
    assert not window.marker_buttons["1"].isEnabled()


def test_gui_event_settings_round_trip(qtbot, tmp_path: Path) -> None:
    window = MainWindow(output_directory=tmp_path, default_fibers=1)
    qtbot.addWidget(window)
    _check, label, edges = window.ttl_input_rows[3]
    label.setText("lick")
    edges.setCurrentIndex(edges.findData("rising"))
    window.ttl_input_rows[4][0].setChecked(False)
    window.marker_name_edits["2"].setText("Shock")
    rule = window.ttl_event_rows[1]
    rule[0].setChecked(True)
    rule[1].setCurrentIndex(rule[1].findData(2))
    rule[4].setText("Reward")

    config = window._build_config()

    assert [(t.line, t.label, t.edges.value) for t in config.ttl_inputs] == [
        (1, "behavior_1", "both"),
        (2, "behavior_2", "both"),
        (3, "lick", "rising"),
    ]
    assert [(m.key, m.name) for m in config.event_markers] == [("2", "Shock")]
    assert [(r.line, r.name) for r in config.ttl_events] == [(2, "Reward")]
    window._apply_configuration(demo_config(tmp_path, fiber_count=1))
    assert window._build_config().event_markers == ()
    window._apply_configuration(config)
    rebuilt = window._build_config()
    assert (rebuilt.ttl_inputs, rebuilt.event_markers, rebuilt.ttl_events) == (
        config.ttl_inputs,
        config.event_markers,
        config.ttl_events,
    )
    assert list(window.marker_buttons) == ["2"]


def test_gui_dff_control_fit_takes_f0_from_the_fitted_control(qtbot, tmp_path: Path) -> None:
    window = MainWindow(output_directory=tmp_path, default_fibers=1)
    qtbot.addWidget(window)
    window._prepare_trace_curves(demo_config(tmp_path, fiber_count=1))
    times = np.arange(0.0, 10.0, 0.1)
    control = 100.0 + 5.0 * np.sin(times)
    window._trace_times[(405, 0)] = array("d", times)
    window._trace_values[(405, 0)] = array("f", control)
    signal_times = times + 0.03
    # Signal = 3 x control + 20, plus a 10 % transient over the last second.
    signal = 3.0 * (100.0 + 5.0 * np.sin(signal_times)) + 20.0
    signal[-10:] *= 1.1
    window._trace_times[(470, 0)] = array("d", signal_times)
    window._trace_values[(470, 0)] = array("f", signal)
    window._trace_first_s = 0.0
    window._trace_latest_s = float(signal_times[-1])
    window._set_trace_horizon(None)

    window.trace_mode_combo.setCurrentIndex(window.trace_mode_combo.findData("dff_fit"))

    assert "control hidden" in window.trace_baseline_hint.text()
    _, displayed = window._curves[(470, 0)].getData()
    assert np.nanmax(np.abs(displayed[:50])) < 2.0
    assert np.nanmedian(displayed[-10:]) > 7.0
    assert not window._curves[(405, 0)].isVisible() and window._curves[(470, 0)].isVisible()
    window.trace_mode_combo.setCurrentIndex(window.trace_mode_combo.findData("dff_rolling"))
    assert window._curves[(405, 0)].isVisible()
    assert "preceding window" in window.trace_baseline_hint.text()
    _, rolling = window._curves[(470, 0)].getData()
    assert np.isfinite(rolling).all()
    window.trace_mode_combo.setCurrentIndex(window.trace_mode_combo.findData("dff_fit"))
    assert window._build_config().display.mode == "dff_fit"


def test_gui_measures_led_power_after_asking(qtbot, tmp_path: Path, monkeypatch) -> None:
    from driftless_photometry.hardware.rwd_devices import LEDPowerReading

    events = []

    class Meter:
        def close(self):
            events.append("closed")

    def measurer(config, meter):
        events.append([int(c.wavelength_nm) for c in config.enabled_channels])
        return (LEDPowerReading(470, 5.0, 0.123, 0.001, 10),)

    answers = iter([QMessageBox.StandardButton.No, QMessageBox.StandardButton.Yes])
    monkeypatch.setattr(QMessageBox, "question", lambda *args: next(answers))
    window = MainWindow(
        output_directory=tmp_path,
        default_fibers=1,
        power_meter_factory=Meter,
        power_measurer=measurer,
    )
    qtbot.addWidget(window)
    window.source_combo.setCurrentIndex(window.source_combo.findData("rwd"))

    window.start_power_measurement()  # "No"
    assert window._device_thread is None and not events
    window.start_power_measurement()
    qtbot.waitUntil(lambda: window._device_thread is None, timeout=5_000)

    assert events == [[410, 470, 560], "closed"]
    assert "470 nm at 5 %: 0.123 mW" in window.power_label.text()
    assert window.last_power_readings[0].power_mw == 0.123
    assert window._warmup.warm_s() >= 0 and not window._warmup.leds_are_on


def test_gui_scrolls_settings_and_traces_separately(qtbot, tmp_path: Path) -> None:
    window = MainWindow(output_directory=tmp_path, default_fibers=4)
    qtbot.addWidget(window)
    window.resize(1400, 900)
    window.show()
    window._prepare_trace_curves(demo_config(tmp_path, fiber_count=4))

    assert window.tabs.widget(0) is not window.controls_scroll
    assert window.controls_scroll.isAncestorOf(window.source_combo)
    assert window.trace_scroll.isAncestorOf(window.trace_workspace)
    assert window.trace_workspace.height() == 4 * 230
    assert window.trace_scroll.viewport().height() < window.trace_workspace.height()
    assert window.height() <= 900

    window.gain_slider.setValue(125)
    assert window.gain_spin.value() == 12.5
    window.gain_spin.setValue(3.0)
    assert window.gain_slider.value() == 30


def test_gui_preview_runs_until_stopped_and_takes_led_power_changes(qtbot, tmp_path: Path) -> None:
    benches = []

    def rig_factory(config):
        bench = Bench(config)
        benches.append(bench)
        return bench.rig

    window = MainWindow(output_directory=tmp_path, default_fibers=1, rig_factory=rig_factory)
    qtbot.addWidget(window)
    window.show()
    window.source_combo.setCurrentIndex(window.source_combo.findData("rwd"))
    slider = window.channel_voltages[Wavelength.GREEN_470]

    window.start_preview()
    qtbot.waitUntil(lambda: sum(window._camera_frame_counts.values()) >= 10, timeout=15_000)
    assert window._thread is not None  # still running: no fixed length
    assert slider.isEnabled() and not window.channel_checks[Wavelength.GREEN_470].isEnabled()
    slider.setValue(200)  # 20 %
    # The simulated rig floods the event queue, which can starve the 250 ms debounce,
    # so send at once once it is armed.
    assert window._power_timer.isActive()
    window._power_timer.stop()
    window._send_led_powers()
    qtbot.waitUntil(
        lambda: any(run.led_value[RWDLed.LED_470] == 2000 for run in benches[-1].board.runs),
        timeout=10_000,
    )
    seen = sum(window._camera_frame_counts.values())
    qtbot.waitUntil(lambda: sum(window._camera_frame_counts.values()) >= seen + 6, timeout=15_000)
    window.stop_recording()
    qtbot.waitUntil(lambda: window._thread is None, timeout=15_000)

    assert window.last_error is None, window.last_error
    assert dict(window.last_preview.final_power_percent)[Wavelength.GREEN_470] == 20.0
    assert window.start_button.isEnabled()  # it ended on the current powers
    assert window.channel_checks[Wavelength.GREEN_470].isEnabled()
    benches[-1].assert_all_off()

    slider.setValue(300)
    assert not window.start_button.isEnabled()


def test_gui_starts_on_rwd_when_the_application_default_is_rwd(qtbot, tmp_path: Path) -> None:
    window = MainWindow(output_directory=tmp_path, default_fibers=1, default_system="rwd")
    qtbot.addWidget(window)

    assert window.source_combo.currentData() == "rwd"
    assert isinstance(window._build_config().source, RWDSourceConfig)


def test_gui_logs_failures_to_the_settings_folder(qtbot, tmp_path: Path, monkeypatch) -> None:
    from driftless_photometry.settings import settings_root

    monkeypatch.setattr(QMessageBox, "critical", lambda *args: None)
    window = MainWindow(output_directory=tmp_path, default_fibers=1)
    qtbot.addWidget(window)

    window._on_preview_failed("Traceback: no camera frame within 2 s")

    log = (settings_root() / "logs" / "dbf-errors.log").read_text(encoding="utf-8")
    assert "Preview failed" in log and "no camera frame within 2 s" in log


def test_gui_keeps_event_settings_the_panel_cannot_show(qtbot, tmp_path: Path) -> None:
    payload = demo_config(tmp_path, fiber_count=1).model_dump()
    payload["event_markers"] = [{"key": "2", "name": "Tone"}, {"key": "7", "name": "Shock"}]
    payload["ttl_events"] = [
        {"line": 1, "min_width_ms": 10 * i, "max_width_ms": 10 * i + 5, "name": f"E{i}"}
        for i in range(6)
    ]
    config = SessionConfig.model_validate(payload)
    window = MainWindow(output_directory=tmp_path, default_fibers=1)
    qtbot.addWidget(window)

    window._apply_configuration(config)
    rebuilt = window._build_config()

    assert [(m.key, m.name) for m in rebuilt.event_markers] == [("2", "Tone"), ("7", "Shock")]
    assert [rule.name for rule in rebuilt.ttl_events] == [f"E{i}" for i in range(6)]
    assert "keys 7" in window.events_extra_label.text()
    assert "2 more named TTL event(s)" in window.events_extra_label.text()


def test_gui_trace_axis_shows_plain_pixel_intensity(qtbot, tmp_path: Path) -> None:
    window = MainWindow(output_directory=tmp_path, default_fibers=1)
    qtbot.addWidget(window)
    window._prepare_trace_curves(demo_config(tmp_path, fiber_count=1))
    axis = window.trace_plots[0].getAxis("left")

    assert axis.labelText == "Mean pixel intensity" and axis.labelUnits == "counts"
    assert not axis.autoSIPrefix
    window.trace_mode_combo.setCurrentIndex(window.trace_mode_combo.findData("dff"))
    assert axis.labelText == "dF/F" and not axis.autoSIPrefix


def test_gui_sets_a_separate_gain_for_rwd_camera_2(qtbot, tmp_path: Path) -> None:
    window = MainWindow(output_directory=tmp_path, default_fibers=1)
    qtbot.addWidget(window)
    window.source_combo.setCurrentIndex(window.source_combo.findData("custom"))
    assert window._build_config().camera.camera2_gain is None

    window.source_combo.setCurrentIndex(window.source_combo.findData("rwd"))
    window.gain_slider.setValue(118)
    window.gain2_slider.setValue(20)
    camera = window._build_config().camera
    assert (camera.gain, camera.camera2_gain) == (11.8, 2.0)

    window._load_camera_controls(camera.model_copy(update={"camera2_gain": 4.5}))
    assert window.gain2_spin.value() == 4.5 and window.gain2_slider.value() == 45


def test_gui_sets_its_own_windows_taskbar_identity(monkeypatch) -> None:
    import sys

    from driftless_photometry.gui import app

    calls = []
    shell = type("Shell", (), {"SetCurrentProcessExplicitAppUserModelID": calls.append})
    fake_ctypes = type("CTypes", (), {"windll": type("WinDLL", (), {"shell32": shell})})
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setitem(sys.modules, "ctypes", fake_ctypes)

    app._set_windows_app_id()

    assert calls == [app.WINDOWS_APP_ID]


def test_gui_live_view_draws_traces_and_roi_readouts(qtbot, tmp_path: Path) -> None:
    benches = []

    def rig_factory(config):
        bench = Bench(config)
        benches.append(bench)
        return bench.rig

    window = MainWindow(output_directory=tmp_path, default_fibers=1, rig_factory=rig_factory)
    qtbot.addWidget(window)
    window.show()
    window.source_combo.setCurrentIndex(window.source_combo.findData("rwd"))

    window.start_live_view()
    qtbot.waitUntil(
        lambda: all(len(window._trace_times[(w, 0)]) >= 2 for w in (410, 470, 560)),
        timeout=15_000,
    )
    readout = window.calibration_readouts[1].text()
    assert readout.startswith("Fiber 1: ") and readout.endswith("counts @ 470 nm")
    assert window._readout_wavelength(1) == 470
    assert "@ 560 nm" in window.calibration_readouts[2].text()
    _, values = window._curves[(470, 0)].getData()
    assert values is not None and len(values) >= 1

    # Moving a circle changes what the traces measure straight away.
    before = window._current_live_rois()
    circle = window._roi_items[0]
    circle.setPos([circle.pos()[0] + 30, circle.pos()[1]])
    assert window._current_live_rois() != before
    window.stop_recording()
    qtbot.waitUntil(lambda: window._thread is None, timeout=15_000)
    benches[-1].assert_all_off()


def test_gui_draws_ttl_edges_with_toggles_and_keeps_per_roi_notes(qtbot, tmp_path: Path) -> None:
    import pyqtgraph as pg
    from pynwb import NWBHDF5IO

    window = MainWindow(output_directory=tmp_path, default_fibers=2, default_duration_s=2.5)
    qtbot.addWidget(window)
    window.source_combo.setCurrentIndex(window.source_combo.findData("custom"))
    run_preview_until_checked(window, qtbot)

    window.start_recording()
    qtbot.waitUntil(lambda: window._trace_latest_s is not None, timeout=10_000)
    assert window.note_edit.isEnabled()
    assert all(check.isChecked() for check in window.note_roi_checks.values())
    window.note_roi_checks["fiber_01"].setChecked(False)  # only ROI 2
    window.note_edit.setText("fiber 2 bumped")
    window.add_note()
    window.note_roi_checks["fiber_01"].setChecked(True)  # both ROIs
    window.note_edit.setText("saline injection")
    window.add_note()
    qtbot.waitUntil(lambda: window._thread is None, timeout=30_000)
    assert window.last_error is None, window.last_error
    assert not window.note_edit.isEnabled()

    def ttl_lines(row):
        items = window.trace_plots[row].items
        return [
            i
            for i in items
            if isinstance(i, pg.InfiniteLine)
            and i.pen.style() == Qt.PenStyle.DashLine
            and i.pen.widthF() == 1
        ]

    assert window._ttl_graphics and ttl_lines(0)
    window.ttl_line_checks[1].setChecked(False)
    assert all(
        not item.isVisible()
        for line, graphics in window._ttl_graphics
        if line == 1
        for _plot, item in graphics
    )
    window.ttl_marker_check.setChecked(False)
    assert all(not item.isVisible() for _l, g in window._ttl_graphics for _p, item in g)
    history = window.notes_history.toPlainText()
    assert "ROI 2 · fiber 2 bumped" in history
    assert "ROI 1, ROI 2 · saline injection" in history

    paths = [report.path for report in window.last_result.reports]
    tables = []
    for path in paths:
        with NWBHDF5IO(path, mode="r", load_namespaces=True) as io:
            marks = io.read().events["event_marks"].to_dataframe()
        tables.append(marks[marks["source"] == "note"][["event_name", "fiber_id"]].values.tolist())
    assert tables[0] == [["saline injection", "fiber_01"]]
    assert sorted(map(tuple, tables[1])) == [
        ("fiber 2 bumped", "fiber_02"),
        ("saline injection", "fiber_02"),
    ]


def test_gui_assigns_ttl_inputs_to_rois(qtbot, tmp_path: Path) -> None:
    window = MainWindow(output_directory=tmp_path, default_fibers=2)
    qtbot.addWidget(window)
    assert window.ttl_roi_buttons[1].text() == "All ROIs"

    window._toggle_ttl_roi(1, "fiber_02", True)
    assert window.ttl_roi_buttons[1].text() == "ROI 2"
    config = window._build_config()
    assert next(t for t in config.ttl_inputs if t.line == 1).fiber_ids == ("fiber_02",)
    assert config.ttl_lines_for("fiber_01") == {2, 3, 4}

    window._set_ttl_rois(1, ())
    window._apply_configuration(config)
    assert window.ttl_roi_buttons[1].text() == "ROI 2"
    assert window._build_config().ttl_inputs == config.ttl_inputs


def test_gui_rows_menu_shows_only_checked_roi_traces(qtbot, tmp_path: Path) -> None:
    window = MainWindow(output_directory=tmp_path, default_fibers=6)
    qtbot.addWidget(window)
    window.resize(1400, 900)
    window.show()
    window._prepare_trace_curves(demo_config(tmp_path, fiber_count=6))
    assert window.trace_workspace.height() == 6 * 230
    assert len(window.trace_row_actions) == 6
    assert window.trace_rows_button.text() == "All ROIs"

    window.trace_rows_menu.actions()[1].trigger()  # Select none
    assert not any(plot.isVisible() for plot in window.trace_plots.values())
    assert window.trace_rows_button.text() == "None"
    window.trace_row_actions[3].setChecked(True)  # ROI 4 alone fills the trace area
    qtbot.wait(50)
    assert [plot.isVisible() for plot in window.trace_plots.values()] == [
        False,
        False,
        False,
        True,
        False,
        False,
    ]
    viewport = window.trace_scroll.viewport().height()
    assert window.trace_workspace.height() >= viewport - 4
    assert window.trace_plots[3].height() >= viewport - 30 > 230
    assert window.trace_plots[3].getAxis("bottom").labelText == "Session time"
    assert window.trace_rows_button.text() == "ROI 4"

    window.trace_row_actions[1].setChecked(True)  # two rows share the area
    qtbot.wait(50)
    assert window.trace_rows_button.text() == "ROI 2, ROI 4"
    assert window.trace_plots[1].isVisible() and window.trace_plots[3].isVisible()
    assert window.trace_workspace.height() >= max(2 * 230, viewport - 4)
    assert abs(window.trace_plots[1].height() - window.trace_plots[3].height()) < 30
    assert not window.trace_plots[1].getAxis("bottom").labelText

    window.trace_rows_menu.actions()[0].trigger()  # Select all
    qtbot.wait(50)
    assert all(plot.isVisible() for plot in window.trace_plots.values())
    assert all(action.isChecked() for action in window.trace_row_actions.values())
    assert window.trace_workspace.height() == 6 * 230

    window.trace_row_actions[0].setChecked(False)
    window._prepare_trace_curves(demo_config(tmp_path, fiber_count=6))  # next run keeps it
    assert not window.trace_row_actions[0].isChecked()
    assert not window.trace_plots[0].isVisible()
    assert window.trace_rows_button.text() == "5 of 6 ROIs"


def test_gui_names_each_roi_beside_its_circle(qtbot, tmp_path: Path) -> None:
    window = MainWindow(output_directory=tmp_path, default_fibers=3)
    qtbot.addWidget(window)
    circles = window._roi_items + window._roi_items_cam2
    assert len(window._roi_labels) == len(circles)
    names = [label.toPlainText() for _plot, label in window._roi_labels]
    assert names[:3] == ["ROI 1 - Fiber 1", "ROI 2 - Fiber 2", "ROI 3 - Fiber 3"]

    window._roi_metadata_editors[1].label_edit.setText("NAc left")
    assert window._roi_labels[1][1].toPlainText() == "ROI 2 - NAc left"

    circle = window._roi_items[0]
    circle.setPos((200.0, 150.0))  # dragging moves the label with it
    x, y, radius = window._circle_geometry(circle)
    label = window._roi_labels[0][1]
    assert label.pos().x() == pytest.approx(x)
    assert label.pos().y() == pytest.approx(y - radius - 2)

    window._roi_metadata_editors[2].enabled_check.setChecked(False)
    assert window._roi_labels[2][1].opacity() == pytest.approx(0.25)

    shown = [w for w in window.wavelength_image_plots if w in window._active_profile.wavelengths]
    texts = [
        item for _plot, item in window._wavelength_roi_overlays if hasattr(item, "toPlainText")
    ]
    assert len(texts) == 3 * len(shown)
    assert "ROI 2 - NAc left" in {item.toPlainText() for item in texts}


@pytest.mark.parametrize("kind", ["preview", "recording"])
def test_gui_rows_ticked_back_after_none_fill_the_trace_area(qtbot, tmp_path: Path, kind) -> None:
    # Ticking rows back keeps the workspace height, so no resize re-lays them out.
    window = MainWindow(output_directory=tmp_path, default_fibers=5)
    qtbot.addWidget(window)
    window.resize(2000, 1400)
    window.show()
    window._operation_kind = kind
    window._prepare_trace_curves(demo_config(tmp_path, fiber_count=5))
    window.trace_rows_menu.actions()[1].trigger()  # Select none
    viewport = window.trace_scroll.viewport().height()
    assert viewport > 2 * 230
    window.trace_row_actions[0].setChecked(True)
    qtbot.wait(50)
    assert window.trace_plots[0].height() >= viewport - 40
    window.trace_row_actions[1].setChecked(True)
    qtbot.wait(50)
    heights = [window.trace_plots[row].height() for row in (0, 1)]
    assert sum(heights) >= viewport - 40
    assert min(heights) > viewport / 2 - 60


def test_gui_locks_roi_circles_and_applies_one_diameter_to_all(qtbot, tmp_path: Path) -> None:
    window = MainWindow(output_directory=tmp_path, default_fibers=3)
    qtbot.addWidget(window)
    first = window._roi_items[0]
    x, y, _r = window._circle_geometry(window._roi_items[2])
    first.setSize([60.0, 60.0])
    window._roi_metadata_editors[0].apply_diameter_button.click()
    for circle in window._roi_items:
        assert window._circle_geometry(circle)[2] == pytest.approx(30.0)
    assert window._circle_geometry(window._roi_items[2])[:2] == pytest.approx((x, y))
    assert window._roi_metadata_editors[2].diameter_spins[1].value() == pytest.approx(60.0)

    window.roi_lock_check.setChecked(True)
    assert not any(circle.translatable for circle in window._roi_items)
    assert not any(h.isVisible() for c in window._roi_items for h in c.getHandles())
    assert not window._roi_metadata_editors[1].diameter_spins[1].isEnabled()
    assert not window._roi_metadata_editors[1].apply_diameter_button.isEnabled()
    window.fiber_count_spin.setValue(4)  # rebuilt circles stay locked
    assert len(window._roi_items) == 4
    assert not any(circle.translatable for circle in window._roi_items)

    window.roi_lock_check.setChecked(False)
    assert all(circle.translatable for circle in window._roi_items)
    assert window._roi_metadata_editors[3].diameter_spins[1].isEnabled()


def test_gui_bleach_runs_the_leds_for_a_set_time_then_logs_it(qtbot, tmp_path: Path) -> None:
    from driftless_photometry.gui.bleach import BleachDialog, BleachPlan, led_duty_fraction

    benches = []

    def rig_factory(config):
        bench = Bench(config)
        benches.append(bench)
        return bench.rig

    window = MainWindow(output_directory=tmp_path, default_fibers=1, rig_factory=rig_factory)
    qtbot.addWidget(window)
    window.source_combo.setCurrentIndex(window.source_combo.findData("rwd"))
    assert window._preparation_events() == ()

    dialog = BleachDialog(window, wavelengths_nm=(410, 470), duty_fraction=0.3, uses_percent=True)
    assert not dialog.start_button.isEnabled()  # safety confirmation first
    dialog.safety_check.setChecked(True)
    assert dialog.start_button.isEnabled() and dialog.plan().duration_s == 600.0
    assert led_duty_fraction(47_370, 20, 3) == pytest.approx(0.3158, abs=1e-4)

    window.start_bleach(BleachPlan(duration_s=1.5, percent=80))
    assert window.live_button.text() == "Stop bleaching"
    assert not window.bleach_button.isEnabled()
    assert all(c.power_percent == 80 for c in window._bleach["config"].channels if c.power_percent)
    # It stops by itself; the record exists only if frames arrived with the LEDs lit.
    qtbot.waitUntil(lambda: window._thread is None, timeout=30_000)

    assert window.last_error is None, window.last_error
    benches[-1].assert_all_off()
    assert window.live_button.text() == "Live view" and window.bleach_button.isEnabled()
    ((event, detail),) = window._preparation_events()
    assert event == "patch_cord_bleach"
    assert "of 0.025 min at 80 % LED power" in detail
    assert "Patch-cord bleach finished" in window.statusBar().currentMessage()


def test_gui_flags_a_dark_roi_on_its_trace_row(qtbot, tmp_path: Path) -> None:
    from driftless_photometry.live_trace import LiveTracePoint

    window = MainWindow(output_directory=tmp_path, default_fibers=2)
    qtbot.addWidget(window)
    config = demo_config(tmp_path, fiber_count=2)
    window._prepare_trace_curves(config)
    fibers = [roi.fiber_id for roi in config.enabled_rois]
    wavelength = window._active_trace_wavelengths[0]
    for index in range(80):
        t = index / 20
        window._append_live_trace_points(
            [
                LiveTracePoint(t, fibers[0], wavelength, 1500.0),
                LiveTracePoint(t, fibers[1], wavelength, 12.0),
            ]
        )
    assert window._signal_warnings == {1: f"dark at {wavelength} nm (12 counts)"}
    assert "⚠ dark" in window.trace_plots[1].titleLabel.text
    assert "⚠" not in window.trace_plots[0].titleLabel.text
    for index in range(80, 600):  # reconnected; the brief jump clears after 15 s
        window._append_live_trace_points(
            [
                LiveTracePoint(index / 20, fibers[0], wavelength, 1500.0),
                LiveTracePoint(index / 20, fibers[1], wavelength, 1500.0),
            ]
        )
    assert "⚠" not in window.trace_plots[1].titleLabel.text


def test_gui_crosstalk_test_reads_live_view_and_is_logged(qtbot, tmp_path: Path) -> None:
    window = MainWindow(output_directory=tmp_path, default_fibers=2)
    qtbot.addWidget(window)
    window.show()
    window._open_crosstalk()
    dialog = window._crosstalk_dialog
    assert dialog is not None and not dialog.dark_button.isEnabled()

    window.start_live_view()
    assert dialog.dark_button.isEnabled()
    dialog.dark_button.click()
    qtbot.waitUntil(lambda: dialog.dark is not None, timeout=20_000)
    assert set(dialog.dark) == set(window._active_trace_wavelengths)
    assert all(len(means) == 2 for means in dialog.dark.values())
    window.stop_recording()
    qtbot.waitUntil(lambda: window._thread is None, timeout=15_000)
    assert not dialog.dark_button.isEnabled()

    window._set_crosstalk_summary("470 nm max 1.0 % (fiber_01 -> fiber_02)")
    assert ("crosstalk_test", "470 nm max 1.0 % (fiber_01 -> fiber_02)") in (
        window._preparation_events()
    )


def test_gui_previews_the_rwd_led_and_camera_timing(qtbot, tmp_path: Path) -> None:
    window = MainWindow(output_directory=tmp_path, default_fibers=1)
    qtbot.addWidget(window)
    assert window.timing_diagram.isHidden()  # Custom system: no RWD timing
    window.source_combo.setCurrentIndex(window.source_combo.findData("rwd"))
    assert not window.timing_diagram.isHidden()
    window.fps_spin.setValue(120)
    window.exposure_max_button.click()
    rows = dict(window.timing_diagram.rows())
    assert list(rows) == ["410 nm", "470 nm", "560 nm", "Camera 1", "Camera 2"]
    assert [w for _start, _stop, w in rows["Camera 1"]] == [410, 470, 410, 470]
    assert [w for _start, _stop, w in rows["Camera 2"]] == [560, 560]
    start, stop, _w = rows["470 nm"][0]
    assert start == pytest.approx(1 / 120, abs=1e-5)
    assert stop - start == pytest.approx(window.exposure_spin.value() / 1000)
    assert "sampled at 40.00 Hz" in window.timing_summary.text()

    checks = window._channel_controls["rwd"][0]
    for wavelength, check in checks.items():
        check.setChecked(int(wavelength) == 470)
    assert list(dict(window.timing_diagram.rows())) == ["470 nm", "Camera 1"]
    assert "470 nm is lit continuously" in window.timing_summary.text()


def test_gui_switches_between_dark_and_light_themes_and_remembers(qtbot, tmp_path: Path) -> None:
    from driftless_photometry.gui.theme import PLOT_COLORS, load_theme

    window = MainWindow(output_directory=tmp_path, default_fibers=2)
    qtbot.addWidget(window)
    window._prepare_trace_curves(demo_config(tmp_path, fiber_count=2))
    assert window._theme == "dark" and window.theme_button.text() == "☀ Light"
    assert "#081521" in window.styleSheet()

    window.theme_button.click()
    assert window._theme == "light" and window.theme_button.text() == "☾ Dark"
    assert "#081521" not in window.styleSheet() and "#f3f6f9" in window.styleSheet()
    light = PLOT_COLORS["light"]
    assert window.trace_workspace.backgroundBrush().color().name() == light.background
    axis = window.trace_plots[0].getAxis("left")
    assert axis.textPen().color().name() == light.foreground
    assert load_theme() == "light"  # remembered for the next start

    again = MainWindow(output_directory=tmp_path, default_fibers=1)
    qtbot.addWidget(again)
    assert again._theme == "light" and "#f3f6f9" in again.styleSheet()
    again.theme_button.click()
    assert load_theme() == "dark" and "#081521" in again.styleSheet()


def test_gui_shows_each_rois_snr_per_wavelength_once_a_minute(qtbot, tmp_path: Path) -> None:
    import math

    from driftless_photometry.live_trace import LiveTracePoint

    window = MainWindow(output_directory=tmp_path, default_fibers=1)
    qtbot.addWidget(window)
    config = demo_config(tmp_path, fiber_count=1)
    window._operation_kind = "live"
    window._prepare_trace_curves(config)
    assert "SNR: preview or record" in window.trace_plots[0].titleLabel.text

    window._operation_kind = "preview"
    window._prepare_trace_curves(config)
    fiber = config.enabled_rois[0].fiber_id
    wavelengths = window._active_trace_wavelengths
    rng = np.random.default_rng(5)

    def feed(start_s, stop_s):
        for index in range(round(start_s * 40), round(stop_s * 40)):
            t = index / 40
            window._append_live_trace_points(
                [
                    LiveTracePoint(
                        t, fiber, w, 1000 + 10 * math.sin(2 * math.pi * t) + rng.normal()
                    )
                    for w in wavelengths
                ]
            )

    feed(0, 30)
    assert "SNR after 1 min" in window.trace_plots[0].titleLabel.text
    feed(30, 61)
    title = window.trace_plots[0].titleLabel.text
    assert title.count("SNR ") == 1 and "after 1 min" not in title
    first = dict(window._snr_values[0])
    assert set(first) == set(wavelengths)
    assert all(50 < snr < 90 for snr, _fs in first.values())
    feed(61, 100)
    assert window._snr_values[0] == first  # steady until a minute has passed
    feed(100, 122)
    assert window._snr_values[0] != first


def test_gui_locks_roi_circles_while_recording_but_not_in_preview(qtbot, tmp_path: Path) -> None:
    window = MainWindow(
        output_directory=tmp_path, default_duration_s=1.0, default_fibers=2, default_raw=False
    )
    qtbot.addWidget(window)
    window.show()

    def editable():
        circles = window._roi_items
        return all(c.isEnabled() and c.translatable for c in circles) and all(
            h.isVisible() for c in circles for h in c.getHandles()
        )

    window.start_preview()
    qtbot.waitUntil(lambda: window._trace_latest_s is not None, timeout=10_000)
    assert window._operation_kind == "preview" and editable()
    window.stop_recording()
    qtbot.waitUntil(lambda: window._thread is None, timeout=15_000)
    run_preview_until_checked(window, qtbot)  # recording needs a preview of these circles

    window.start_recording()
    qtbot.waitUntil(lambda: window._trace_latest_s is not None, timeout=10_000)
    assert window._operation_kind == "recording" and window._thread is not None
    circles = window._roi_items
    assert not any(c.isEnabled() or c.translatable for c in circles)
    assert not any(h.isVisible() for c in circles for h in c.getHandles())
    assert not window.roi_lock_check.isChecked()  # the operator's own lock is unchanged
    qtbot.waitUntil(lambda: window._thread is None, timeout=30_000)
    assert window.last_error is None, window.last_error
    assert editable()  # unlocked again after the recording
    assert window._roi_metadata_editors[0].diameter_spins[1].isEnabled()
