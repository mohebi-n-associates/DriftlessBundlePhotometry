from pathlib import Path
from types import SimpleNamespace

import pytest

from driftless_photometry.acquisition import AcquisitionEngine
from driftless_photometry.config import demo_config
from driftless_photometry.hardware import SimulatedRig
from driftless_photometry.storage import (
    InsufficientStorageError,
    check_storage_capacity,
    estimate_session_bytes,
)


def test_capacity_estimate_is_bounded_by_configured_frame_rate_and_raw_policy(
    tmp_path: Path,
) -> None:
    traces_only = demo_config(
        tmp_path,
        fiber_count=2,
        raw_capture=False,
        width_px=32,
        height_px=24,
    )
    with_raw = traces_only.model_copy(
        update={"camera": traces_only.camera.model_copy(update={"raw_capture": True})}
    )
    nine_roi_raw = demo_config(
        tmp_path,
        fiber_count=9,
        raw_capture=True,
        width_px=32,
        height_px=24,
    )

    frame_count, traces_only_bytes = estimate_session_bytes(traces_only, 10.0)
    raw_frame_count, raw_bytes = estimate_session_bytes(with_raw, 10.0)
    _nine_roi_frames, nine_roi_bytes = estimate_session_bytes(nine_roi_raw, 10.0)

    assert frame_count == raw_frame_count == 300
    assert traces_only_bytes > 0
    assert raw_bytes > traces_only_bytes
    assert nine_roi_bytes > raw_bytes * 3


def test_storage_preflight_rejects_insufficient_space_before_acquisition(
    tmp_path: Path,
) -> None:
    output = tmp_path / "not-created" / "session"
    config = demo_config(output, fiber_count=1, raw_capture=True)

    with pytest.raises(InsufficientStorageError, match="insufficient storage"):
        check_storage_capacity(
            config,
            5.0,
            reserve_bytes=1_000,
            disk_usage=lambda _path: SimpleNamespace(free=999),
        )

    assert not output.exists()


@pytest.mark.parametrize(
    ("requested_fps", "raw_capture"),
    [(300.0, False), (60.0, True)],
)
def test_small_frame_simulator_capacity_bounds_complete_without_queue_loss(
    tmp_path: Path,
    requested_fps: float,
    raw_capture: bool,
) -> None:
    base = demo_config(
        tmp_path,
        fiber_count=1,
        raw_capture=raw_capture,
        width_px=32,
        height_px=24,
    )
    config = base.model_copy(
        update={"camera": base.camera.model_copy(update={"requested_fps": requested_fps})}
    )

    result = AcquisitionEngine(spool_chunk_size=32, spool_queue_size=64).run(
        config,
        SimulatedRig(config, seed=47),
        duration_s=1.0,
    )

    assert result.report.frame_count == int(requested_fps)
    assert result.diagnostics.dropped_frames == 0
    assert result.diagnostics.spool.peak_queue_depth <= 64
    assert result.diagnostics.spool.committed_frames == int(requested_fps)
