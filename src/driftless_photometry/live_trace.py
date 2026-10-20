"""Source-neutral live trace points for GUI and future replay consumers."""

from __future__ import annotations

import math
from dataclasses import dataclass

from driftless_photometry.acquisition import AcquisitionProgress
from driftless_photometry.config import SessionConfig


@dataclass(frozen=True, slots=True)
class LiveTracePoint:
    """One immutable raw-source value addressed by stable fiber and wavelength."""

    timestamp_s: float
    fiber_id: str
    wavelength_nm: int
    value: float

    def __post_init__(self) -> None:
        if not math.isfinite(self.timestamp_s):
            raise ValueError("live trace timestamp must be finite")
        if not self.fiber_id:
            raise ValueError("live trace fiber_id is required")
        if self.wavelength_nm <= 0:
            raise ValueError("live trace wavelength must be positive")
        if not math.isfinite(self.value):
            raise ValueError("live trace value must be finite")


def camera_live_trace_points(
    config: SessionConfig,
    progress: AcquisitionProgress,
) -> tuple[LiveTracePoint, ...]:
    """Project one camera ROI-vector sample without changing its source values."""

    sample = progress.sample
    if len(sample.values) != len(config.enabled_rois):
        raise ValueError("live sample ROI count does not match configuration")
    return tuple(
        LiveTracePoint(
            timestamp_s=sample.timestamp_s,
            fiber_id=roi.fiber_id,
            wavelength_nm=int(sample.wavelength_nm),
            value=float(sample.values[index]),
        )
        for index, roi in enumerate(config.enabled_rois)
    )
