"""Runtime records passed between acquisition services."""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
from numpy.typing import NDArray

from .config import Wavelength
from .provenance import RuntimeProvenance


@dataclass(frozen=True, slots=True)
class ExposureRecord:
    sequence: int
    timestamp_s: float
    controller_tick_us: int
    wavelength_nm: Wavelength
    commanded_voltage_v: float


@dataclass(frozen=True, slots=True)
class FramePacket:
    """One camera frame in `[y, x]` orientation."""

    frame_id: int
    image: NDArray[np.uint16]
    exposure: ExposureRecord
    host_received_s: float

    def __post_init__(self) -> None:
        if self.frame_id < 0:
            raise ValueError("frame_id must be non-negative")
        if self.image.dtype != np.uint16:
            raise TypeError("camera image must have dtype uint16")
        if self.image.ndim != 2:
            raise ValueError("camera image must be two-dimensional [y, x]")


@dataclass(frozen=True, slots=True)
class TTLEdge:
    timestamp_s: float
    controller_tick_us: int
    line: int
    label: str
    edge: str
    new_level: bool
    sequence: int

    def __post_init__(self) -> None:
        if self.line not in range(1, 5):
            raise ValueError("TTL line must be between 1 and 4")
        if self.edge not in {"rising", "falling"}:
            raise ValueError("edge must be rising or falling")


@dataclass(frozen=True, slots=True)
class TraceSample:
    timestamp_s: float
    frame_id: int
    sequence: int
    controller_tick_us: int
    wavelength_nm: Wavelength
    values: NDArray[np.float32]
    saturation_fractions: NDArray[np.float32]


@dataclass(frozen=True, slots=True)
class DroppedFrameEvent:
    timestamp_s: float
    expected_sequence: int
    observed_sequence: int
    missing_count: int
    reason: str = "controller sequence gap"


@dataclass(frozen=True, slots=True)
class InvalidTimeInterval:
    """A continuous session span whose scientific validity is compromised."""

    start_time_s: float
    stop_time_s: float
    reason: str
    source_event: str

    def __post_init__(self) -> None:
        if not np.isfinite(self.start_time_s) or not np.isfinite(self.stop_time_s):
            raise ValueError("invalid-time bounds must be finite")
        if self.start_time_s < 0 or self.stop_time_s < self.start_time_s:
            raise ValueError("invalid-time bounds must be ordered and non-negative")
        if not self.reason or not self.source_event:
            raise ValueError("invalid-time reason and source_event are required")


@dataclass(slots=True)
class AcquisitionData:
    """Completed non-frame session data used by the NWB finalizer."""

    traces: dict[Wavelength, list[TraceSample]] = field(
        default_factory=lambda: {wavelength: [] for wavelength in Wavelength}
    )
    ttl_edges: list[TTLEdge] = field(default_factory=list)
    dropped_frames: list[DroppedFrameEvent] = field(default_factory=list)
    invalid_times: list[InvalidTimeInterval] = field(default_factory=list)
    frame_ids: list[int] = field(default_factory=list)
    frame_timestamps_s: list[float] = field(default_factory=list)
    frame_sequences: list[int] = field(default_factory=list)
    frame_ticks_us: list[int] = field(default_factory=list)
    frame_wavelengths_nm: list[int] = field(default_factory=list)
    frame_commanded_voltages_v: list[float] = field(default_factory=list)
    frame_host_received_s: list[float] = field(default_factory=list)
    frame_saturation_fractions: list[NDArray[np.float32]] = field(default_factory=list)
    frame_saved_indices: list[int] = field(default_factory=list)
    runtime_provenance: RuntimeProvenance | None = None

    def append_frame(self, packet: FramePacket, sample: TraceSample, saved_index: int) -> None:
        self.traces[sample.wavelength_nm].append(sample)
        self.frame_ids.append(packet.frame_id)
        self.frame_timestamps_s.append(packet.exposure.timestamp_s)
        self.frame_sequences.append(packet.exposure.sequence)
        self.frame_ticks_us.append(packet.exposure.controller_tick_us)
        self.frame_wavelengths_nm.append(int(packet.exposure.wavelength_nm))
        self.frame_commanded_voltages_v.append(packet.exposure.commanded_voltage_v)
        self.frame_host_received_s.append(packet.host_received_s)
        self.frame_saturation_fractions.append(sample.saturation_fractions.copy())
        self.frame_saved_indices.append(saved_index)
