from pathlib import Path

import numpy as np
import pytest

from driftless_photometry.config import Wavelength, demo_config
from driftless_photometry.domain import ExposureRecord, FramePacket, TraceSample, TTLEdge
from driftless_photometry.storage import SessionSpool, SpoolError, load_session_spool


def _submit_frames(spool: SessionSpool, count: int) -> list[np.ndarray]:
    images = []
    for index in range(count):
        wavelength = list(Wavelength)[index % 3]
        image = np.full((24, 32), index + 10, dtype=np.uint16)
        images.append(image)
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
    return images


@pytest.mark.parametrize("raw_capture", [False, True])
def test_spool_commits_and_round_trips(tmp_path: Path, raw_capture: bool) -> None:
    config = demo_config(
        tmp_path,
        fiber_count=2,
        raw_capture=raw_capture,
        width_px=32,
        height_px=24,
    )
    spool = SessionSpool(config, chunk_size=2, queue_size=4)
    images = _submit_frames(spool, 5)
    spool.submit_ttl(TTLEdge(0.1, 100_000, 1, "behavior_1", "rising", True, 1))
    spool.close()

    loaded = load_session_spool(spool.path)
    assert loaded.complete is True
    assert loaded.config == config
    assert loaded.data.frame_ids == [1, 2, 3, 4, 5]
    assert len(loaded.data.ttl_edges) == 1
    np.testing.assert_array_equal(loaded.calibration_image, images[0])
    assert set(loaded.wavelength_images) == set(Wavelength)
    for index, wavelength in enumerate(Wavelength):
        np.testing.assert_array_equal(loaded.wavelength_images[wavelength], images[index])
    if raw_capture:
        assert loaded.frames is not None
        np.testing.assert_array_equal(loaded.frames.to_array(), np.stack(images))
        assert loaded.data.frame_saved_indices == [0, 1, 2, 3, 4]
    else:
        assert loaded.frames is None
        assert loaded.data.frame_saved_indices == [-1] * 5

    spool.cleanup()
    assert not spool.path.exists()


def test_spool_detects_corrupted_committed_chunk(tmp_path: Path) -> None:
    config = demo_config(
        tmp_path,
        fiber_count=2,
        raw_capture=True,
        width_px=32,
        height_px=24,
    )
    spool = SessionSpool(config, chunk_size=2)
    _submit_frames(spool, 2)
    spool.close()
    chunk = next((spool.path / "chunks").glob("*.npz"))
    with chunk.open("ab") as stream:
        stream.write(b"corrupt")
    with pytest.raises(SpoolError, match="checksum mismatch"):
        load_session_spool(spool.path)


def test_spool_detects_corrupted_wavelength_reference(tmp_path: Path) -> None:
    config = demo_config(
        tmp_path,
        fiber_count=2,
        raw_capture=False,
        width_px=32,
        height_px=24,
    )
    spool = SessionSpool(config, chunk_size=3)
    _submit_frames(spool, 3)
    spool.close()
    reference = spool.path / "wavelength_470_reference.npy"
    with reference.open("ab") as stream:
        stream.write(b"corrupt")

    with pytest.raises(SpoolError, match="470 nm reference image checksum mismatch"):
        load_session_spool(spool.path)


def test_aborted_spool_remains_recoverable(tmp_path: Path) -> None:
    config = demo_config(
        tmp_path,
        fiber_count=2,
        raw_capture=False,
        width_px=32,
        height_px=24,
    )
    spool = SessionSpool(config, chunk_size=4)
    _submit_frames(spool, 3)
    spool.abort()
    loaded = load_session_spool(spool.path)
    assert loaded.complete is False
    assert loaded.data.frame_ids == [1, 2, 3]


def test_background_writer_failure_is_surfaced_and_spool_is_preserved(
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
    spool = SessionSpool(config, chunk_size=1, queue_size=2)

    def fail_chunk(items) -> None:
        del items
        raise OSError("simulated spool disk failure")

    monkeypatch.setattr(spool, "_commit_chunk", fail_chunk)
    with pytest.raises(SpoolError, match="background spool writer failed"):
        _submit_frames(spool, 1)
        spool.close()
    assert spool.path.exists()
    assert (spool.path / "manifest.json").exists()
