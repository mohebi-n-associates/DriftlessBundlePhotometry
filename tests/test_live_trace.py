from pathlib import Path

import numpy as np

from driftless_photometry.acquisition import AcquisitionProgress
from driftless_photometry.config import demo_config
from driftless_photometry.domain import TraceSample
from driftless_photometry.live_trace import camera_live_trace_points


def test_camera_live_trace_projection_preserves_stable_fibers_and_values(
    tmp_path: Path,
) -> None:
    config = demo_config(tmp_path, fiber_count=2)
    sample = TraceSample(
        timestamp_s=1.25,
        frame_id=4,
        sequence=3,
        controller_tick_us=1_250_000,
        wavelength_nm=config.enabled_channels[1].wavelength_nm,
        values=np.asarray([10.5, 20.5], dtype=np.float32),
        saturation_fractions=np.zeros(2, dtype=np.float32),
    )
    progress = AcquisitionProgress(4, sample, np.zeros((256, 256), dtype=np.uint16))

    points = camera_live_trace_points(config, progress)

    assert [(point.fiber_id, point.wavelength_nm, point.value) for point in points] == [
        ("fiber_01", 470, 10.5),
        ("fiber_02", 470, 20.5),
    ]
    assert all(point.timestamp_s == 1.25 for point in points)
