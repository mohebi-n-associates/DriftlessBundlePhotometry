from pathlib import Path

import numpy as np
import pytest
from pynwb import NWBHDF5IO
from rwd_bench import W410, W470, W560, Bench, rwd_config

from driftless_photometry.acquisition import AcquisitionEngine, run_preview
from driftless_photometry.config import RecordingControlConfig, RWDSourceConfig
from driftless_photometry.roi import circular_mask, rois_for_camera
from driftless_photometry.settings import configuration_from_nwb
from driftless_photometry.storage import load_session_spool
from driftless_photometry.storage.recovery import recover_session_spool


def test_acquisition_fault_shuts_hardware_before_draining_spool(tmp_path: Path, monkeypatch):
    from driftless_photometry.storage import SessionSpool

    config = rwd_config(tmp_path, (W410, W470))
    bench = Bench(config)
    abort = SessionSpool.abort
    inspected = []

    def checked_abort(spool, **kwargs):
        bench.assert_all_off()
        assert not bench.board.is_open
        inspected.append(True)
        abort(spool, **kwargs)

    monkeypatch.setattr(SessionSpool, "abort", checked_abort)

    def fail(_progress):
        raise RuntimeError("display consumer failed")

    with pytest.raises(RuntimeError, match="display consumer failed") as caught:
        AcquisitionEngine().run(config, bench.rig, duration_s=1.0, on_progress=fail)
    assert caught.value.__traceback__ is not None  # Keeping it alive must not hold hardware.
    assert inspected == [True]
    bench.assert_all_off()


def test_sparse_ttl_stop_is_immediate_and_preview_ignores_it(tmp_path: Path):
    config = rwd_config(tmp_path, (W410, W470), fiber_count=1).model_copy(
        update={
            "recording": RecordingControlConfig(stop="ttl", stop_ttl_line=1, stop_ttl_edge="rising")
        }
    )
    bench = Bench(config)
    bench.board.schedule_input_edge(1, True, bench.clock.now_s + 0.4)
    result = AcquisitionEngine().run(config, bench.rig, duration_s=20)
    assert result.stopped_by_request
    assert result.report.frame_count < 12
    assert result.report.ttl_edge_count == 1
    bench.assert_all_off()
    preview = Bench(config)
    preview.board.schedule_input_edge(1, True, preview.clock.now_s + 0.4)
    result = run_preview(config, preview.rig, duration_s=1)
    assert result.frame_count == 20
    preview.assert_all_off()


def test_rwd_preview_checks_every_wavelength_on_simulated_hardware(tmp_path: Path) -> None:
    config = rwd_config(tmp_path, camera2_shift_px=60.0)
    bench = Bench(config)

    result = run_preview(config, bench.rig)

    counts = dict(result.wavelength_frame_counts)
    assert set(counts) == {W410, W470, W560}
    assert all(count >= 2 for count in counts.values())
    assert not list(tmp_path.glob("*.nwb"))
    bench.assert_all_off()


def test_rwd_recording_writes_camera_aware_nwbs(tmp_path: Path) -> None:
    config = rwd_config(tmp_path, camera2_shift_px=60.0, raw_capture=True)
    bench = Bench(config)
    bench.board.schedule_input_edge(1, True, bench.clock.now_s + 0.4)
    bench.pending_setup = lambda cams: cams.missed.add(("2", 0, 11))

    result = AcquisitionEngine().run(config, bench.rig, duration_s=1.5)

    bench.assert_all_off()
    assert len(result.reports) == 2
    report = result.reports[0]
    with NWBHDF5IO(report.path, mode="r", load_namespaces=True) as io:
        nwbfile = io.read()
        frames = nwbfile.events["camera_frames"].to_dataframe()
        assert set(frames["wavelength_source"]) == {"phase_derived"}
        assert set(frames.loc[frames["wavelength_nm"] == 560, "camera_index"]) == {2}
        assert set(frames.loc[frames["wavelength_nm"] != 560, "camera_index"]) == {1}
        assert (frames["camera_timestamp_ns"] > 0).all()
        assert "commanded_voltage_v" not in frames.columns
        assert set(frames["commanded_power_percent"]) == {5.0, 12.5}
        excitation = nwbfile.events["excitation_events"].to_dataframe()
        assert set(excitation["wavelength_source"]) == {"phase_derived"}
        power = nwbfile.stimulus["led_470_commanded_power"]
        assert power.unit == "percent"
        assert set(np.asarray(power.data[:])) == {12.5}
        assert "led_470_commanded_voltage" not in nwbfile.stimulus
        table = nwbfile.lab_meta_data["FiberPhotometry"].fiber_photometry_table
        detectors = {
            float(row["excitation_wavelength_in_nm"]): row["photodetector"].name
            for _, row in table.to_dataframe().iterrows()
        }
        assert detectors == {410.0: "rwd_camera_1", 470.0: "rwd_camera_1", 560.0: "rwd_camera_2"}
        assert nwbfile.devices["rwd_camera_2"].model.model_number == "Alvium 1800 U-040m"
        rois = nwbfile.processing["photometry"]["camera_rois"].to_dataframe()
        roi = config.enabled_rois[0]
        assert rois.iloc[0]["camera2_center_x_px"] == roi.camera2_center_x_px
        # The 560 nm traces come from camera 2's circle, where the fiber is lit.
        green = nwbfile.acquisition["raw_fluorescence_470"].data[:, 0]
        red = nwbfile.acquisition["raw_fluorescence_560"].data[:, 0]
        assert green.min() > 200 and red.min() > 200
        images = nwbfile.processing["photometry"]["wavelength_roi_images"].images
        overlay = images["roi_overlay_560nm"].data[:]
        (placed,) = rois_for_camera((roi,), 2)
        edge_y = round(placed.center_y_px)
        edge_x = round(placed.center_x_px + placed.radius_px)
        assert tuple(overlay[edge_y, edge_x]) != tuple(overlay[edge_y, edge_x - 20])
        series = nwbfile.acquisition["camera_frames"]
        assert series.control_description[:].tolist() == ["410 nm", "470 nm", "560 nm"]
        assert series.device is None
        invalid = nwbfile.intervals["invalid_times"].to_dataframe()
        assert "rwd_sequencer_restart" in invalid["source_event"].tolist()
        events = nwbfile.events["system_events"].to_dataframe()["event"].tolist()
        assert "rwd_sequencer_restart" in events
        assert "rwd_controller_connected" in events
        assert len(nwbfile.events["ttl_edges"]) == 1
        assert "phase-derived" in nwbfile.data_collection
    restored, warnings = configuration_from_nwb(report.path)
    assert warnings == []
    assert restored.source == RWDSourceConfig()
    assert restored.rois == config.rois


def test_rwd_trace_values_use_each_cameras_geometry(tmp_path: Path) -> None:
    config = rwd_config(tmp_path, (W470, W560), camera2_shift_px=60.0)
    bench = Bench(config)
    engine = AcquisitionEngine()
    samples = []

    engine.run(
        config, bench.rig, duration_s=0.5, on_progress=lambda progress: samples.append(progress)
    )

    for progress in samples:
        image = progress.image
        camera = 2 if progress.sample.wavelength_nm is W560 else 1
        (placed, *_rest) = rois_for_camera(config.enabled_rois, camera)
        expected = image[circular_mask(image.shape, placed)].mean()
        assert progress.sample.values[0] == pytest.approx(expected)


def test_rwd_spool_round_trips_camera_and_provenance_columns(tmp_path: Path) -> None:
    config = rwd_config(tmp_path, fiber_count=1)
    bench = Bench(config)
    engine = AcquisitionEngine()
    state = {"count": 0}

    def stop_early(_progress) -> None:
        state["count"] += 1
        if state["count"] == 6:
            raise RuntimeError("simulated host crash")

    with pytest.raises(RuntimeError, match="simulated host crash"):
        engine.run(config, bench.rig, duration_s=2.0, on_progress=stop_early)
    bench.assert_all_off()

    (spool_path,) = tmp_path.glob("*.photometry-spool")
    loaded = load_session_spool(spool_path)
    data = loaded.data
    assert loaded.schema_version == 5
    assert data.frame_camera_indices == [1, 1, 2] * 2
    assert data.frame_wavelength_sources == ["phase_derived"] * 6
    assert data.frame_commanded_power_percent == [5.0, 12.5, 5.0] * 2
    assert all(np.isnan(value) for value in data.frame_commanded_voltages_v)
    assert all(value > 0 for value in data.frame_camera_timestamps_ns)

    recovered = recover_session_spool(spool_path)
    assert recovered.nwb.frame_count == 6


def test_rwd_dff_threshold_fires_its_output_program_and_marks_it(
    tmp_path: Path, monkeypatch
) -> None:
    from driftless_photometry.config import DffTriggerConfig, RWDTTLOutputConfig, SessionConfig
    from driftless_photometry.events import DffThresholdWatcher

    payload = rwd_config(tmp_path, (W410, W470)).model_dump()
    output = RWDTTLOutputConfig(
        port=2,
        frequency_hz=20.0,
        pulse_width_ms=10,
        label="opto",
        train_s=0.25,
        trigger="dff_threshold",
        dff_trigger=DffTriggerConfig(fiber_id="fiber_01"),
    )
    payload["source"] = RWDSourceConfig(ttl_outputs=(output,)).model_dump()
    config = SessionConfig.model_validate(payload)
    calls = []

    def observe(self, sample, camera_index):
        calls.append(sample.timestamp_s)
        return "dF/F crossed" if len(calls) == 6 else None

    monkeypatch.setattr(DffThresholdWatcher, "observe", observe)
    bench = Bench(config)

    result = AcquisitionEngine().run(config, bench.rig, duration_s=1.5)

    with NWBHDF5IO(result.reports[0].path, mode="r", load_namespaces=True) as io:
        nwbfile = io.read()
        marks = nwbfile.events["event_marks"].to_dataframe()
        events = nwbfile.events["system_events"].to_dataframe()
    trigger = events[events["event"] == "ttl_output_dff_trigger"]
    assert trigger["timestamp"].tolist() == pytest.approx([calls[5]])
    assert "ttl_output_triggered" in events["event"].tolist()
    assert marks["event_name"].tolist() == ["opto"]
    assert marks["duration"].tolist() == pytest.approx([0.25])
    assert marks["timestamp"].iloc[0] == pytest.approx(calls[5], abs=0.1)
    bench.assert_all_off()


def test_rwd_preview_with_power_changes_still_checks_each_first_frame(tmp_path: Path) -> None:
    configured = rwd_config(tmp_path, (W410, W470))
    payload = configured.model_dump()
    payload["channels"][1]["power_percent"] = 30.0  # the board runs 470 nm at 30 %
    bench = Bench(type(configured).model_validate(payload))

    with pytest.raises(RuntimeError, match="470 nm commanded intensity does not match"):
        run_preview(configured, bench.rig, duration_s=0.5, allow_power_changes=True)

    changing = Bench(configured)
    frames = []

    def change_after_first_frames(progress) -> None:
        frames.append(progress)
        if len(frames) == 4:
            changing.rig.request_led_power(W470, 20.0)

    result = run_preview(
        configured,
        changing.rig,
        duration_s=1.0,
        allow_power_changes=True,
        on_progress=change_after_first_frames,
    )
    assert dict(result.final_power_percent)[W470] == 20.0
