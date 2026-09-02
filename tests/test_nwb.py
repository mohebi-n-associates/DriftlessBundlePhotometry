import json
from pathlib import Path

import h5py
import numpy as np
import pytest
from pynwb import NWBHDF5IO, validate

from driftless_photometry.config import Wavelength, demo_config
from driftless_photometry.domain import (
    AcquisitionData,
    ExposureRecord,
    FramePacket,
    InvalidTimeInterval,
    SystemEvent,
    TraceSample,
    TTLEdge,
)
from driftless_photometry.provenance import (
    NWB_RUNTIME_PROVENANCE_SCRATCH_NAME,
    RuntimeProvenance,
)
from driftless_photometry.settings import (
    NWB_SETTINGS_SCRATCH_NAME,
    configuration_from_nwb,
)
from driftless_photometry.storage import write_session_nwbs


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
    data.runtime_provenance = RuntimeProvenance(
        application_version="test-application",
        python_version="test-python",
        operating_system="test-os",
        adapter_name="golden-rig",
        adapter_version="2",
        protocol_version="golden-protocol-v3",
        dependencies=(("pynwb", "test-version"),),
    )
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
            commanded_voltage_v=0.5 + index * 0.1,
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
            saturation_fractions=np.asarray([index / 100, (index + 1) / 100], dtype=np.float32),
        )
        data.append_frame(packet, sample, index if raw_capture else -1)
    data.ttl_edges.extend(
        [
            TTLEdge(0.10, 100_000, 1, "behavior_1", "rising", True, 10),
            TTLEdge(0.20, 200_000, 1, "behavior_1", "falling", False, 11),
        ]
    )
    data.invalid_times.append(
        InvalidTimeInterval(
            start_time_s=0.10,
            stop_time_s=0.15,
            reason="synthetic clock uncertainty",
            source_event="test_clock_discontinuity",
        )
    )
    data.system_events.append(
        SystemEvent(
            timestamp_s=0.15,
            event="acquisition_fault:test_clock_discontinuity",
            detail="synthetic fault detail",
        )
    )
    return config, data, frames


def test_golden_nwb_round_trip_with_embedded_frames(tmp_path: Path) -> None:
    config, data, frames = _golden_data(tmp_path, raw_capture=True)
    wavelength_images = {wavelength: frames[index] for index, wavelength in enumerate(Wavelength)}
    reports = write_session_nwbs(
        config,
        data,
        frames=frames,
        calibration_image=frames[0],
        wavelength_images=wavelength_images,
    )
    assert len(reports) == 2
    assert {report.path.name for report in reports} == {
        f"{config.session_id}__animal-01__fiber_01.nwb",
        f"{config.session_id}__animal-02__fiber_02.nwb",
    }

    for roi_index, report in enumerate(reports):
        assert report.path.exists()
        assert report.frame_count == 6
        assert report.trace_sample_count == 6
        assert report.ttl_edge_count == 2
        assert validate(path=report.path) == []
        assert not report.path.with_name(f"{report.path.stem}.partial.nwb").exists()

        with NWBHDF5IO(report.path, mode="r", load_namespaces=True) as io:
            nwbfile = io.read()
            assert NWB_SETTINGS_SCRATCH_NAME in nwbfile.scratch
            provenance = json.loads(str(nwbfile.get_scratch(NWB_RUNTIME_PROVENANCE_SCRATCH_NAME)))
            assert provenance["adapter_name"] == "golden-rig"
            assert provenance["protocol_version"] == "golden-protocol-v3"
            assert nwbfile.subject.subject_id == config.rois[roi_index].animal_id
            assert nwbfile.acquisition["camera_frames"].data.dtype == np.dtype("uint16")
            assert nwbfile.acquisition["camera_frames"].data.shape == (6, 24, 32)
            assert len(nwbfile.events["camera_frames"]) == 6
            frame_events = nwbfile.events["camera_frames"].to_dataframe()
            assert frame_events["camera_frame_id"].tolist() == data.frame_ids
            assert frame_events["controller_sequence"].tolist() == data.frame_sequences
            assert frame_events["controller_tick_us"].tolist() == data.frame_ticks_us
            assert frame_events["wavelength_nm"].tolist() == data.frame_wavelengths_nm
            np.testing.assert_allclose(
                frame_events["commanded_voltage_v"],
                data.frame_commanded_voltages_v,
            )
            np.testing.assert_allclose(
                frame_events["host_received_s"],
                data.frame_host_received_s,
            )
            np.testing.assert_allclose(
                frame_events["roi_saturation_fraction"],
                [values[roi_index] for values in data.frame_saturation_fractions],
            )
            excitation_events = nwbfile.events["excitation_events"].to_dataframe()
            assert len(excitation_events) == 6
            assert excitation_events["controller_sequence"].tolist() == data.frame_sequences
            assert excitation_events["controller_tick_us"].tolist() == data.frame_ticks_us
            assert excitation_events["wavelength_nm"].tolist() == data.frame_wavelengths_nm
            np.testing.assert_allclose(
                excitation_events["commanded_voltage_v"],
                data.frame_commanded_voltages_v,
            )
            assert len(nwbfile.events["ttl_edges"]) == 2
            invalid_times = nwbfile.intervals["invalid_times"].to_dataframe()
            np.testing.assert_allclose(invalid_times["start_time"], [0.10])
            np.testing.assert_allclose(invalid_times["stop_time"], [0.15])
            assert invalid_times["reason"].tolist() == ["synthetic clock uncertainty"]
            assert invalid_times["source_event"].tolist() == ["test_clock_discontinuity"]
            system_events = nwbfile.events["system_events"].to_dataframe()
            assert system_events["event"].tolist() == [
                "recording_started",
                "recording_stopped",
                "acquisition_fault:test_clock_discontinuity",
            ]
            assert system_events.iloc[-1]["detail"] == "synthetic fault detail"
            assert len(nwbfile.processing["photometry"]["camera_rois"]) == 1
            images = nwbfile.processing["photometry"]["wavelength_roi_images"].images
            assert len(images) == 6
            for wavelength in Wavelength:
                raw = images[f"raw_reference_{int(wavelength)}nm"].data
                overlay = images[f"roi_overlay_{int(wavelength)}nm"].data
                np.testing.assert_array_equal(raw, wavelength_images[wavelength])
                assert overlay.dtype == np.dtype("uint8")
                assert overlay.shape == (24, 32, 3)
                assert np.any(overlay[:, :, 0] != overlay[:, :, 1])
            table = nwbfile.lab_meta_data["FiberPhotometry"].fiber_photometry_table
            assert len(table) == 3
            assert set(table.to_dataframe()["location"]) == {config.rois[roi_index].brain_region}
            indicators = nwbfile.lab_meta_data["FiberPhotometry"].fiber_photometry_indicators
            assert indicators.indicators["indicator_01"].label == config.rois[roi_index].sensor_type
            for wavelength in Wavelength:
                series = nwbfile.acquisition[f"raw_fluorescence_{int(wavelength)}"]
                assert series.data.shape == (2, 1)
                samples = data.traces[wavelength]
                np.testing.assert_array_equal(
                    series.data[:, 0],
                    [sample.values[roi_index] for sample in samples],
                )
                frame_indices = [
                    index
                    for index, observed in enumerate(data.frame_wavelengths_nm)
                    if observed == int(wavelength)
                ]
                voltage = nwbfile.stimulus[f"led_{int(wavelength)}_commanded_voltage"]
                np.testing.assert_allclose(
                    voltage.data[:],
                    [data.frame_commanded_voltages_v[index] for index in frame_indices],
                )

        restored, warnings = configuration_from_nwb(report.path)
        assert restored == config
        assert warnings == []


@pytest.mark.parametrize("raw_capture", [False, True])
def test_legacy_per_roi_nwbs_restore_available_sibling_settings(
    tmp_path: Path, raw_capture: bool
) -> None:
    config, data, frames = _golden_data(tmp_path, raw_capture=raw_capture)
    reports = write_session_nwbs(
        config,
        data,
        frames=frames if raw_capture else None,
        calibration_image=frames[0],
        wavelength_images={
            wavelength: frames[index] for index, wavelength in enumerate(Wavelength)
        },
    )
    for report in reports:
        with h5py.File(report.path, mode="r+") as handle:
            del handle[f"scratch/{NWB_SETTINGS_SCRATCH_NAME}"]

    restored, warnings = configuration_from_nwb(reports[0].path)

    assert len(restored.rois) == 2
    assert [roi.animal_id for roi in restored.rois] == ["animal-01", "animal-02"]
    assert [channel.wavelength_nm for channel in restored.channels] == list(Wavelength)
    assert restored.camera.raw_capture is raw_capture
    assert restored.output_directory == tmp_path
    assert warnings and "legacy NWB" in warnings[0]


def test_nwb_without_raw_frames_retains_frame_provenance(tmp_path: Path) -> None:
    config, data, frames = _golden_data(tmp_path, raw_capture=False)
    wavelength_images = {wavelength: frames[index] for index, wavelength in enumerate(Wavelength)}
    reports = write_session_nwbs(
        config,
        data,
        calibration_image=frames[0],
        wavelength_images=wavelength_images,
    )
    assert len(reports) == 2
    for report in reports:
        with NWBHDF5IO(report.path, mode="r", load_namespaces=True) as io:
            nwbfile = io.read()
            assert "camera_frames" not in nwbfile.acquisition
            event_frame = nwbfile.events["camera_frames"].to_dataframe()
            assert len(event_frame) == 6
            assert not event_frame["raw_saved"].any()
            assert set(event_frame["raw_image_index"]) == {-1}
            assert "wavelength_roi_images" in nwbfile.processing["photometry"].data_interfaces


def test_rejects_raw_frame_count_mismatch_before_writing(tmp_path: Path) -> None:
    config, data, frames = _golden_data(tmp_path, raw_capture=True)
    try:
        write_session_nwbs(config, data, frames=frames[:-1], calibration_image=frames[0])
    except ValueError as error:
        assert "raw frame count" in str(error)
    else:  # pragma: no cover - assertion produces a clearer error than pytest.raises here
        raise AssertionError("expected raw frame count mismatch")
    assert not list(tmp_path.glob("*.nwb*"))
