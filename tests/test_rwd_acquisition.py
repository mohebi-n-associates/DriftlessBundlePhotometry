from pathlib import Path

import numpy as np
import pytest
from pynwb import NWBHDF5IO
from rwd_bench import W410, W470, W560, Bench, rwd_config

from driftless_photometry.acquisition import AcquisitionEngine, run_preview
from driftless_photometry.config import RWDSourceConfig
from driftless_photometry.roi import circular_mask, rois_for_camera
from driftless_photometry.settings import configuration_from_nwb
from driftless_photometry.storage import load_session_spool
from driftless_photometry.storage.recovery import recover_session_spool


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
