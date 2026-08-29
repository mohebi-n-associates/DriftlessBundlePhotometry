from pathlib import Path

import numpy as np
from pynwb import NWBHDF5IO

from driftless_photometry.config import Wavelength, demo_config
from driftless_photometry.domain import ExposureRecord, FramePacket, TraceSample
from driftless_photometry.storage import SessionSpool, recover_session_spool


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
    with NWBHDF5IO(result.nwb.path, mode="r", load_namespaces=True) as io:
        nwbfile = io.read()
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
