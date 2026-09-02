import hashlib
import json
from pathlib import Path

import numpy as np
import pytest

from driftless_photometry.config import Wavelength, demo_config
from driftless_photometry.domain import (
    ExposureRecord,
    FramePacket,
    InvalidTimeInterval,
    TraceSample,
    TTLEdge,
)
from driftless_photometry.provenance import RuntimeProvenance
from driftless_photometry.storage import SessionSpool, SpoolError, load_session_spool


def _submit_frames(spool: SessionSpool, count: int) -> list[np.ndarray]:
    images = []
    for index in range(count):
        wavelength = list(Wavelength)[index % 3]
        image = np.full((24, 32), index + 10, dtype=np.uint16)
        images.append(image)
        exposure = ExposureRecord(
            index,
            index * 0.1,
            index * 100_000,
            wavelength,
            0.5 + index * 0.1,
        )
        packet = FramePacket(index + 1, image, exposure, index * 0.1 + 0.001)
        sample = TraceSample(
            timestamp_s=index * 0.1,
            frame_id=index + 1,
            sequence=index,
            controller_tick_us=index * 100_000,
            wavelength_nm=wavelength,
            values=np.asarray([100 + index, 200 + index], dtype=np.float32),
            saturation_fractions=np.asarray([index / 100, (index + 1) / 100], dtype=np.float32),
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
    provenance = RuntimeProvenance(
        application_version="test-application",
        python_version="test-python",
        operating_system="test-os",
        adapter_name="test-adapter",
        adapter_version="4",
        protocol_version="test-protocol-v5",
        dependencies=(("numpy", "test-version"),),
    )
    spool = SessionSpool(
        config,
        chunk_size=2,
        queue_size=4,
        runtime_provenance=provenance,
    )
    images = _submit_frames(spool, 5)
    spool.submit_ttl(TTLEdge(0.1, 100_000, 1, "behavior_1", "rising", True, 1))
    interval = InvalidTimeInterval(0.1, 0.2, "test interval", "test_fault")
    spool.submit_invalid_time(interval)
    spool.close()

    loaded = load_session_spool(spool.path)
    assert loaded.complete is True
    assert loaded.config == config
    assert loaded.data.frame_ids == [1, 2, 3, 4, 5]
    assert len(loaded.data.ttl_edges) == 1
    assert loaded.data.invalid_times == [interval]
    assert loaded.data.runtime_provenance == provenance
    np.testing.assert_allclose(
        loaded.data.frame_commanded_voltages_v,
        [0.5, 0.6, 0.7, 0.8, 0.9],
    )
    np.testing.assert_allclose(
        loaded.data.frame_host_received_s,
        [0.001, 0.101, 0.201, 0.301, 0.401],
    )
    np.testing.assert_allclose(
        np.stack(loaded.data.frame_saturation_fractions),
        [[0.00, 0.01], [0.01, 0.02], [0.02, 0.03], [0.03, 0.04], [0.04, 0.05]],
    )
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


def test_legacy_v1_spool_is_recoverable_with_missing_provenance_marked(
    tmp_path: Path,
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

    manifest_path = spool.path / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    for entry in manifest["chunks"]:
        chunk_path = spool.path / entry["file"]
        with np.load(chunk_path, allow_pickle=False) as chunk:
            legacy_arrays = {
                name: chunk[name].copy()
                for name in chunk.files
                if name not in {"commanded_voltages_v", "host_received_s"}
            }
        with chunk_path.open("wb") as stream:
            np.savez(stream, **legacy_arrays)
        entry["sha256"] = hashlib.sha256(chunk_path.read_bytes()).hexdigest()
    manifest["schema_version"] = 1
    manifest.pop("invalid_time_count")
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    (spool.path / "runtime_provenance.json").unlink()

    loaded = load_session_spool(spool.path)

    assert loaded.schema_version == 1
    assert loaded.data.frame_commanded_voltages_v == [1.0, 1.0, 1.0]
    assert loaded.data.frame_host_received_s == loaded.data.frame_timestamps_s
    assert loaded.data.runtime_provenance is not None
    assert loaded.data.runtime_provenance.adapter_name == "legacy_spool_v1"
    assert loaded.data.invalid_times[-1].source_event == "legacy_spool_schema_v1"


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
