import os
from pathlib import Path

import numpy as np
import pytest
from pynwb import NWBHDF5IO

from driftless_photometry.config import Wavelength, demo_config
from driftless_photometry.domain import ExposureRecord, FramePacket, TraceSample
from driftless_photometry.storage import (
    SessionSpool,
    discover_session_spools,
    inspect_session_spool,
    recover_session_spool,
)


def _submit_frames(spool: SessionSpool, count: int) -> None:
    for index in range(count):
        wavelength = list(Wavelength)[index % 3]
        image = np.full((24, 32), index + 10, dtype=np.uint16)
        exposure = ExposureRecord(index, index * 0.1, index * 100_000, wavelength, 1.0)
        packet = FramePacket(index + 1, image, exposure, index * 0.1 + 0.001)
        sample = TraceSample(
            timestamp_s=index * 0.1,
            frame_id=index + 1,
            sequence=index,
            controller_tick_us=index * 100_000,
            wavelength_nm=wavelength,
            values=np.asarray([100 + index, 200 + index], dtype=np.float32),
            saturation_fractions=np.zeros(2, dtype=np.float32),
        )
        spool.submit_frame(packet, sample)


def test_incomplete_spool_recovers_to_marked_valid_nwb(tmp_path: Path) -> None:
    config = demo_config(
        tmp_path,
        fiber_count=2,
        raw_capture=True,
        width_px=32,
        height_px=24,
    )
    spool = SessionSpool(config, chunk_size=2)
    _submit_frames(spool, 3)
    spool.abort()

    result = recover_session_spool(spool.path)

    assert result.acquisition_was_complete is False
    assert result.spool_removed is True
    assert not spool.path.exists()
    assert len(result.nwbs) == 2
    for roi_index, report in enumerate(result.nwbs):
        with NWBHDF5IO(report.path, mode="r", load_namespaces=True) as io:
            nwbfile = io.read()
            assert nwbfile.subject.subject_id == config.rois[roi_index].animal_id
            events = nwbfile.events["system_events"].to_dataframe()["event"].tolist()
            assert events == [
                "recording_started",
                "recording_stopped",
                "spool_recovered_incomplete_acquisition",
            ]
            assert nwbfile.acquisition["camera_frames"].data.shape == (3, 24, 32)


def test_recovery_can_retain_complete_spool(tmp_path: Path) -> None:
    config = demo_config(
        tmp_path,
        fiber_count=2,
        raw_capture=False,
        width_px=32,
        height_px=24,
    )
    spool = SessionSpool(config, chunk_size=2)
    _submit_frames(spool, 2)
    spool.close()

    result = recover_session_spool(spool.path, keep_spool=True)

    assert result.acquisition_was_complete is True
    assert result.spool_removed is False
    assert spool.path.exists()
    assert result.inspection.frame_count == 2
    assert result.inspection.trace_sample_count == 2
    assert result.inspection.roi_file_count == 2
    assert result.reused_existing_outputs is False


def test_recovery_is_idempotent_when_valid_outputs_already_exist(tmp_path: Path) -> None:
    config = demo_config(
        tmp_path,
        fiber_count=2,
        raw_capture=False,
        width_px=32,
        height_px=24,
    )
    spool = SessionSpool(config, chunk_size=2)
    _submit_frames(spool, 3)
    spool.close()

    first = recover_session_spool(spool.path, keep_spool=True)
    second = recover_session_spool(spool.path, keep_spool=True)

    assert second.reused_existing_outputs is True
    assert [report.path for report in second.nwbs] == [report.path for report in first.nwbs]
    assert all(report.path.exists() for report in second.nwbs)
    assert spool.path.exists()


def test_recovery_resumes_interrupted_multi_roi_promotion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = demo_config(
        tmp_path,
        fiber_count=2,
        raw_capture=False,
        width_px=32,
        height_px=24,
    )
    spool = SessionSpool(config, chunk_size=2)
    _submit_frames(spool, 3)
    spool.close()

    real_replace = os.replace
    promotion_count = 0

    def interrupt_second_promotion(source, destination) -> None:
        nonlocal promotion_count
        if str(source).endswith(".partial.nwb"):
            promotion_count += 1
            if promotion_count == 2:
                raise OSError("simulated process interruption during promotion")
        real_replace(source, destination)

    monkeypatch.setattr("driftless_photometry.storage.nwb.os.replace", interrupt_second_promotion)
    with pytest.raises(OSError, match="process interruption"):
        recover_session_spool(spool.path, keep_spool=True)

    final_paths = [
        path for path in tmp_path.glob("*.nwb") if not path.name.endswith(".partial.nwb")
    ]
    assert len(final_paths) == 1
    assert len(list(tmp_path.glob("*.partial.nwb"))) == 1
    monkeypatch.setattr("driftless_photometry.storage.nwb.os.replace", real_replace)

    resumed = recover_session_spool(spool.path, keep_spool=True)

    assert resumed.reused_existing_outputs is True
    assert len(resumed.nwbs) == 2
    assert all(report.path.exists() for report in resumed.nwbs)
    assert not list(tmp_path.glob("*.partial.nwb"))


def test_spool_discovery_is_direct_safe_and_reports_completeness(tmp_path: Path) -> None:
    complete_config = demo_config(tmp_path, fiber_count=2, width_px=32, height_px=24)
    complete_config = complete_config.model_copy(update={"session_id": "complete-session"})
    complete = SessionSpool(complete_config, chunk_size=2)
    _submit_frames(complete, 2)
    complete.close()

    incomplete_config = complete_config.model_copy(update={"session_id": "incomplete-session"})
    incomplete = SessionSpool(incomplete_config, chunk_size=2)
    _submit_frames(incomplete, 1)
    incomplete.abort()
    (tmp_path / "ordinary-directory").mkdir()
    nested_root = tmp_path / "ordinary-directory"
    nested_config = complete_config.model_copy(
        update={
            "session_id": "nested-session",
            "output_directory": nested_root,
        }
    )
    nested = SessionSpool(nested_config, chunk_size=2)
    _submit_frames(nested, 1)
    nested.abort()

    inspections = discover_session_spools(tmp_path)
    incomplete_inspections = discover_session_spools(tmp_path, incomplete_only=True)

    assert [item.session_id for item in inspections] == [
        "complete-session",
        "incomplete-session",
    ]
    assert [item.session_id for item in incomplete_inspections] == ["incomplete-session"]
    assert inspect_session_spool(complete.path).acquisition_complete is True
    assert all(item.path.parent == tmp_path for item in inspections)
