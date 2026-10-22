"""Regressions for the safety, data integrity, and workflow review."""

import gc
import threading
import weakref
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import pytest
from rwd_bench import W410, W470, Bench, rwd_config

from driftless_photometry.acquisition import AcquisitionEngine, AcquisitionProgress
from driftless_photometry.config import RecordingControlConfig, demo_config
from driftless_photometry.domain import TraceSample
from driftless_photometry.gui.display_mailbox import DisplayMailbox
from driftless_photometry.gui.main_window import MainWindow
from driftless_photometry.gui.trace_display import rolling_f0
from driftless_photometry.hardware.rwd_devices import drive_rwd_output_test
from driftless_photometry.hardware.rwd_transport import RWDControllerTransport
from driftless_photometry.storage import load_session_spool


def test_slow_display_has_one_notification_bounded_images_and_preserved_extrema(tmp_path: Path):
    config = demo_config(tmp_path, fiber_count=1, width_px=32, height_px=24)
    mailbox = DisplayMailbox(config)
    refs, notifications = [], 0
    wavelength = config.enabled_channels[0].wavelength_nm
    for index in range(5000):
        image = np.full((24, 32), index, dtype=np.uint16)
        refs.append(weakref.ref(image))
        value = 100_000.0 if index == 123 else 100.0
        sample = TraceSample(
            index / 120,
            index,
            index,
            index * 8333,
            wavelength,
            np.asarray([value], dtype=np.float32),
            np.zeros(1, dtype=np.float32),
        )
        notifications += mailbox.publish(AcquisitionProgress(index + 1, sample, image))
    gc.collect()
    assert notifications == 1
    assert sum(ref() is not None for ref in refs) == 1
    batch = mailbox.take()
    assert batch.images[0].frame_count == 5000
    assert len(batch.points) <= mailbox.MAX_POINTS
    assert max(point.value for point in batch.points) == 100_000
    assert batch.aggregated
    assert mailbox.take().images == ()


def test_short_rolling_window_does_not_allocate_a_grid_for_the_entire_session():
    times = np.linspace(0, 86_400, 10_000)
    values = np.full(len(times), 100, dtype=np.float32)
    baseline = rolling_f0(times, values, 0.1, times[-100:])
    np.testing.assert_array_equal(baseline, np.full(100, 100))


def test_adding_and_removing_fibers_preserves_both_cameras(qtbot, tmp_path: Path):
    window = MainWindow(output_directory=tmp_path, default_fibers=2)
    qtbot.addWidget(window)
    window._apply_configuration(rwd_config(tmp_path, fiber_count=2, camera2_shift_px=50))
    window._roi_items[0].setPos((101, 115))
    window._roi_items_cam2[0].setPos((201, 215))
    window._roi_metadata_editors[0].animal_id_edit.setText("subject-a")
    before = window._roi_configs_from_controls()[0]
    window.fiber_count_spin.setValue(3)
    assert window._roi_configs_from_controls()[0] == before
    window.fiber_count_spin.setValue(1)
    assert window._roi_configs_from_controls()[0] == before


def test_stop_is_visible_everywhere_and_recording_locks_circles(qtbot, tmp_path: Path):
    window = MainWindow(output_directory=tmp_path)
    qtbot.addWidget(window)
    window.show()
    window._operation_kind = "recording"
    window._set_busy_controls(True)
    for tab in range(window.tabs.count()):
        window.tabs.setCurrentIndex(tab)
        assert window.global_stop_button.isVisible()
        assert window.global_stop_button.isEnabled()
    assert not any(item.isEnabled() for item in window._roi_items)
    window._operation_kind = "live"
    window._set_busy_controls(True)
    assert all(item.isEnabled() for item in window._roi_items)


def test_demo_ignores_hardware_defaults_and_refuses_hardware_import(
    qtbot, tmp_path: Path, monkeypatch
):
    def hardware_defaults():
        raise AssertionError("demo must not load the hardware defaults")

    monkeypatch.setattr(
        "driftless_photometry.gui.main_window.load_default_settings_with_warnings",
        hardware_defaults,
    )
    window = MainWindow(
        output_directory=tmp_path, demo_only=True, default_system="rwd", auto_check_devices=True
    )
    qtbot.addWidget(window)
    assert window.source_combo.currentData() == "custom"
    assert not window.source_combo.model().item(1).isEnabled()
    assert not window._auto_check_devices
    with pytest.raises(ValueError, match="Demo mode"):
        window._apply_configuration(rwd_config(tmp_path))


def test_delayed_start_time_survives_failure_and_spool_recovery(tmp_path: Path):
    before = datetime.now(UTC)
    config = rwd_config(tmp_path, (W410, W470), fiber_count=1).model_copy(
        update={
            "session_start_time": before - timedelta(hours=1),
            "recording": RecordingControlConfig(start="delayed", start_delay_s=5),
        }
    )
    bench = Bench(config)
    engine = AcquisitionEngine()

    def fail(_progress):
        raise RuntimeError("injected consumer failure")

    with pytest.raises(RuntimeError, match="injected"):
        engine.run(config, bench.rig, duration_s=1, on_progress=fail)
    loaded = load_session_spool(engine.recovery_path)
    assert loaded.config.session_start_time >= before + timedelta(seconds=4.9)
    assert loaded.data.frame_timestamps_s[0] == 0
    assert any(e.event == "session_utc_origin" for e in loaded.data.system_events)
    bench.assert_all_off()


def test_output_test_cancellation_closes_board(tmp_path: Path):
    config = rwd_config(tmp_path)
    bench = Bench(config)
    with pytest.raises(InterruptedError, match="stopped"):
        drive_rwd_output_test(
            config,
            1,
            cancelled=lambda: True,
            transport_factory=lambda port: RWDControllerTransport(
                port, serial_factory=lambda _: bench.board, clock=bench.clock
            ),
            port_finder=lambda: ("COM9",),
            clock=bench.clock,
        )
    bench.assert_all_off()


def test_recovery_scan_reports_corrupt_spools_without_hiding_valid_ones(tmp_path: Path):
    from test_recovery import _submit_frames

    from driftless_photometry.gui.recovery import inspect_recovery_folder
    from driftless_photometry.storage import SessionSpool

    config = demo_config(tmp_path, fiber_count=2, width_px=32, height_px=24)
    spool = SessionSpool(config)
    _submit_frames(spool, 3)
    spool.abort()
    (tmp_path / "bad.photometry-spool").mkdir()
    results = inspect_recovery_folder(tmp_path)
    assert len(results) == 2
    assert sum(inspection is not None for _path, inspection, _error in results) == 1
    assert sum(bool(error) for _path, _inspection, error in results) == 1


def test_recovery_dialog_defers_close_without_blocking_gui(qtbot, tmp_path: Path, monkeypatch):
    from driftless_photometry.gui.recovery import RecoveryDialog

    entered, release = threading.Event(), threading.Event()

    def scan(_root):
        entered.set()
        release.wait(5)
        return []

    monkeypatch.setattr("driftless_photometry.gui.recovery.inspect_recovery_folder", scan)
    dialog = RecoveryDialog(tmp_path)
    qtbot.addWidget(dialog)
    dialog.show()
    qtbot.waitUntil(entered.is_set)
    dialog.reject()
    assert dialog.isVisible()
    assert dialog._close_pending
    release.set()
    qtbot.waitUntil(lambda: dialog._thread is None)
    assert not dialog.isVisible()
