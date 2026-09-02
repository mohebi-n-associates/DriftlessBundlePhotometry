"""Source-neutral live trace points for GUI and future replay consumers."""

from __future__ import annotations

import math
from dataclasses import dataclass

from driftless_photometry.acquisition import AcquisitionProgress
from driftless_photometry.config import RWDSourceConfig, SessionConfig
from driftless_photometry.rwd.domain import RWDFluorescenceRecord, RWDReceivedRecord
from driftless_photometry.rwd.timing import RWDTickResolver


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


def native_live_trace_points(
    config: SessionConfig,
    progress: AcquisitionProgress,
) -> tuple[LiveTracePoint, ...]:
    """Project one native ROI-vector sample without changing its source values."""

    sample = progress.sample
    if len(sample.values) != len(config.enabled_rois):
        raise ValueError("native live sample ROI count does not match configuration")
    return tuple(
        LiveTracePoint(
            timestamp_s=sample.timestamp_s,
            fiber_id=roi.fiber_id,
            wavelength_nm=int(sample.wavelength_nm),
            value=float(sample.values[index]),
        )
        for index, roi in enumerate(config.enabled_rois)
    )


class RWDLiveTraceProjector:
    """Incrementally project mapped RWD values for display only.

    The first resolved device tick is display time zero. Exact raw ticks and the
    canonical offline origin remain owned by the spool/NWB path, so this projection
    cannot alter scientific storage.
    """

    def __init__(self, config: SessionConfig) -> None:
        if not isinstance(config.source, RWDSourceConfig):
            raise ValueError("RWD live trace projection requires an RWD source")
        self._source = config.source
        self._fiber_by_channel = {
            mapping.device_channel: mapping.fiber_id for mapping in config.source.channel_mappings
        }
        self._resolver = RWDTickResolver()
        self._origin: int | None = None

    def project(self, received: RWDReceivedRecord) -> tuple[LiveTracePoint, ...]:
        record = received.record
        if not isinstance(record, RWDFluorescenceRecord):
            return ()
        try:
            fiber_id = self._fiber_by_channel[record.device_channel]
        except KeyError as error:
            raise ValueError(f"unmapped RWD device channel: {record.device_channel}") from error
        points = []
        for sample in record.samples:
            unwrapped = self._resolver.resolve(
                ("fluorescence", record.device_channel, sample.wavelength_nm),
                sample.timestamp_tick,
            )
            if self._origin is None:
                self._origin = unwrapped
            points.append(
                LiveTracePoint(
                    timestamp_s=(unwrapped - self._origin) * self._source.timestamp_scale_s,
                    fiber_id=fiber_id,
                    wavelength_nm=int(sample.wavelength_nm),
                    value=sample.scaled_value,
                )
            )
        return tuple(points)
