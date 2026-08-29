from pathlib import Path

import numpy as np
from pynwb import NWBHDF5IO, validate

from driftless_photometry.config import Wavelength, demo_config
from driftless_photometry.domain import (
    AcquisitionData,
    ExposureRecord,
    FramePacket,
    TraceSample,
    TTLEdge,
)
from driftless_photometry.storage import write_session_nwb


def _golden_data(tmp_path: Path, *, raw_capture: bool) -> tuple:
    config = demo_config(
        tmp_path,
        fiber_count=2,
        raw_capture=raw_capture,
        width_px=32,
        height_px=24,
    )
    frames = np.empty((6, 24, 32), dtype=np.uint16)
    data = AcquisitionData()
    wavelengths = list(Wavelength)
    for index in range(6):
        wavelength = wavelengths[index % 3]
        image = np.full((24, 32), 100 + index, dtype=np.uint16)
        frames[index] = image
        exposure = ExposureRecord(
            sequence=index,
            timestamp_s=index * 0.05,
            controller_tick_us=index * 50_000,
            wavelength_nm=wavelength,
            commanded_voltage_v=1.0,
        )
        packet = FramePacket(
            frame_id=1000 + index,
            image=image,
            exposure=exposure,
            host_received_s=index * 0.05 + 0.001,
        )
        sample = TraceSample(
            timestamp_s=exposure.timestamp_s,
            frame_id=packet.frame_id,
            sequence=index,
            controller_tick_us=exposure.controller_tick_us,
            wavelength_nm=wavelength,
            values=np.asarray([100 + index, 200 + index], dtype=np.float32),
            saturation_fractions=np.zeros(2, dtype=np.float32),
        )
        data.append_frame(packet, sample, index if raw_capture else -1)
    data.ttl_edges.extend(
        [
            TTLEdge(0.10, 100_000, 1, "behavior_1", "rising", True, 10),
            TTLEdge(0.20, 200_000, 1, "behavior_1", "falling", False, 11),
        ]
    )
    return config, data, frames


def test_golden_nwb_round_trip_with_embedded_frames(tmp_path: Path) -> None:
    config, data, frames = _golden_data(tmp_path, raw_capture=True)
    report = write_session_nwb(config, data, frames=frames, calibration_image=frames[0])
    assert report.path.exists()
    assert report.frame_count == 6
    assert report.trace_sample_count == 6
    assert report.ttl_edge_count == 2
    assert validate(path=report.path) == []
    assert not report.path.with_name(f"{report.path.stem}.partial.nwb").exists()

    with NWBHDF5IO(report.path, mode="r", load_namespaces=True) as io:
        nwbfile = io.read()
        assert nwbfile.acquisition["camera_frames"].data.dtype == np.dtype("uint16")
        assert nwbfile.acquisition["camera_frames"].data.shape == (6, 24, 32)
        assert len(nwbfile.events["camera_frames"]) == 6
        assert len(nwbfile.events["ttl_edges"]) == 2
        assert len(nwbfile.processing["photometry"]["camera_rois"]) == 2
        assert len(nwbfile.lab_meta_data["FiberPhotometry"].fiber_photometry_table) == 6
        for wavelength in Wavelength:
            assert nwbfile.acquisition[f"raw_fluorescence_{int(wavelength)}"].data.shape == (2, 2)


def test_nwb_without_raw_frames_retains_frame_provenance(tmp_path: Path) -> None:
    config, data, frames = _golden_data(tmp_path, raw_capture=False)
    report = write_session_nwb(config, data, calibration_image=frames[0])
    with NWBHDF5IO(report.path, mode="r", load_namespaces=True) as io:
        nwbfile = io.read()
        assert "camera_frames" not in nwbfile.acquisition
        event_frame = nwbfile.events["camera_frames"].to_dataframe()
        assert len(event_frame) == 6
        assert not event_frame["raw_saved"].any()
        assert set(event_frame["raw_image_index"]) == {-1}


def test_rejects_raw_frame_count_mismatch_before_writing(tmp_path: Path) -> None:
    config, data, frames = _golden_data(tmp_path, raw_capture=True)
    try:
        write_session_nwb(config, data, frames=frames[:-1], calibration_image=frames[0])
    except ValueError as error:
        assert "raw frame count" in str(error)
    else:  # pragma: no cover - assertion produces a clearer error than pytest.raises here
        raise AssertionError("expected raw frame count mismatch")
    assert not list(tmp_path.glob("*.nwb*"))
