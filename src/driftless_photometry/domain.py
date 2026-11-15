"""Runtime records passed between acquisition services."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Literal

import numpy as np
from numpy.typing import NDArray

from .config import Wavelength
from .provenance import RuntimeProvenance

WavelengthSource = Literal["controller", "phase_derived"]
"""How an exposure's wavelength is known.

``controller`` means the timing controller reported it for that exposure.
``phase_derived`` means it was derived from camera identity and the sequence position
after a sequencer restart; only the RWD system does this (see the agent contract).
"""
WAVELENGTH_SOURCE_CODES: dict[str, int] = {"controller": 0, "phase_derived": 1}
"""How spools and NWB files store each exposure's wavelength source, as one small integer."""
WAVELENGTH_SOURCE_NAMES: dict[int, str] = {
    code: name for name, code in WAVELENGTH_SOURCE_CODES.items()
}


@dataclass(frozen=True, slots=True)
class ExposureRecord:
    """One camera exposure on the shared session clock.

    ``commanded_voltage_v`` is set by the Custom system and ``commanded_power_percent``
    by RWD hardware. ``controller_tick_us`` is the controller's own tick on Custom
    hardware; RWD exposures carry ticks derived from the board's trigger grid.
    """

    sequence: int
    timestamp_s: float
    controller_tick_us: int
    wavelength_nm: Wavelength
    commanded_voltage_v: float | None
    commanded_power_percent: float | None = None
    camera_index: int = 1
    wavelength_source: WavelengthSource = "controller"

    def __post_init__(self) -> None:
        if self.camera_index < 1:
            raise ValueError("camera_index starts at 1")
        if (self.commanded_voltage_v is None) == (self.commanded_power_percent is None):
            raise ValueError("an exposure commands either a voltage or a power percentage")


@dataclass(frozen=True, slots=True)
class FramePacket:
    """One camera frame in `[y, x]` orientation."""

    frame_id: int
    image: NDArray[np.uint16]
    exposure: ExposureRecord
    host_received_s: float
    camera_timestamp_ns: int | None = None
    """The camera's own hardware timestamp, when the camera reports one."""

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


class Column(Sequence):
    """A read-only per-frame column held as one NumPy array, used like a list.

    A long recording loaded back for saving has millions of exposures. As Python lists
    of boxed numbers and per-frame objects, each exposure took about 930 bytes, so a
    22 h session would need about 9 GB; as columns it takes under 200. Indexing returns
    Python scalars (or a row, for 2-D columns), ``np.asarray`` shares the array without
    copying, and a column equals any sequence with the same items.
    """

    __slots__ = ("array",)

    def __init__(self, array: np.ndarray) -> None:
        self.array = array

    def __len__(self) -> int:
        return len(self.array)

    def __getitem__(self, index):
        item = self.array[index]
        if isinstance(index, slice):
            return Column(item)
        return item if item.ndim else item.item()

    def __iter__(self):
        if self.array.ndim == 1:
            return iter(self.array.tolist())
        return iter(self.array)

    def __array__(self, dtype=None, copy=None):
        return self.array if dtype is None else self.array.astype(dtype, copy=False)

    def __eq__(self, other: object) -> bool:
        if isinstance(other, Column):
            other = other.array
        elif not isinstance(other, (Sequence, np.ndarray)) or isinstance(other, str):
            return NotImplemented
        other_array = np.asarray(other)
        return self.array.shape == other_array.shape and bool(
            np.array_equal(self.array, other_array)
        )

    __hash__ = None  # type: ignore[assignment]

    def __repr__(self) -> str:
        return f"Column({self.array!r})"


class TraceColumns(Sequence):
    """One wavelength's trace samples as columns; items are built as ``TraceSample``s.

    ``values`` and ``saturation_fractions`` are ``[time, roi]`` arrays.
    """

    def __init__(
        self,
        wavelength_nm: Wavelength,
        *,
        timestamps_s: np.ndarray,
        frame_ids: np.ndarray,
        sequences: np.ndarray,
        controller_ticks_us: np.ndarray,
        values: np.ndarray,
        saturation_fractions: np.ndarray,
    ) -> None:
        self.wavelength_nm = wavelength_nm
        self.timestamps_s = timestamps_s
        self.frame_ids = frame_ids
        self.sequences = sequences
        self.controller_ticks_us = controller_ticks_us
        self.values = values
        self.saturation_fractions = saturation_fractions

    def __len__(self) -> int:
        return len(self.timestamps_s)

    def __getitem__(self, index):
        if isinstance(index, slice):
            return [self[i] for i in range(*index.indices(len(self)))]
        return TraceSample(
            timestamp_s=float(self.timestamps_s[index]),
            frame_id=int(self.frame_ids[index]),
            sequence=int(self.sequences[index]),
            controller_tick_us=int(self.controller_ticks_us[index]),
            wavelength_nm=self.wavelength_nm,
            values=self.values[index],
            saturation_fractions=self.saturation_fractions[index],
        )


def trace_timestamps(samples: Sequence[TraceSample]) -> NDArray[np.float64]:
    """Every sample's timestamp, without building a sample at a time from columns."""

    if isinstance(samples, TraceColumns):
        return np.asarray(samples.timestamps_s, dtype=np.float64)
    return np.asarray([sample.timestamp_s for sample in samples], dtype=np.float64)


def trace_values(samples: Sequence[TraceSample], expected_rois: int) -> NDArray[np.float32]:
    """Every sample's per-ROI values as a ``[time, roi]`` array."""

    if isinstance(samples, TraceColumns):
        return np.asarray(samples.values, dtype=np.float32)
    if not samples:
        return np.empty((0, expected_rois), dtype=np.float32)
    rows = [np.asarray(sample.values, dtype=np.float32) for sample in samples]
    if any(row.shape != (expected_rois,) for row in rows):
        raise ValueError("trace sample ROI count does not match configuration")
    return np.stack(rows)


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


@dataclass(frozen=True, slots=True)
class SystemEvent:
    """A timestamped lifecycle or fault event with stable classification."""

    timestamp_s: float
    event: str
    detail: str = ""

    def __post_init__(self) -> None:
        if not np.isfinite(self.timestamp_s) or self.timestamp_s < 0:
            raise ValueError("system-event timestamp must be finite and non-negative")
        if not self.event:
            raise ValueError("system-event classification is required")


EVENT_MARK_SOURCES = frozenset({"key", "button", "ttl", "output", "note"})
NOTE_MAX_CHARACTERS = 1000


@dataclass(frozen=True, slots=True)
class EventMark:
    """A named experimental event: a marker the operator set, or a classified TTL pulse.

    Keyboard and button marks are timed on the host clock, mapped onto session time
    through the most recent camera frame. TTL marks take their pulse's rising edge.
    Output marks are the trains DBF drove on a TTL output, such as optogenetic
    stimulation, host-timed; ``duration_s`` is the train's planned length. Notes are
    the operator's timestamped text, host-timed like markers, for one ROI
    (``fiber_id``) or, when ``fiber_id`` is empty, for every ROI.
    """

    timestamp_s: float
    name: str
    source: str
    detail: str = ""
    duration_s: float = 0.0
    fiber_id: str = ""
    ttl_line: int = 0
    """The TTL input a named TTL event came from; 0 for every other source."""

    def __post_init__(self) -> None:
        if not np.isfinite(self.timestamp_s) or self.timestamp_s < 0:
            raise ValueError("event-mark timestamp must be finite and non-negative")
        if not self.name:
            raise ValueError("event marks need a name")
        if self.source not in EVENT_MARK_SOURCES:
            raise ValueError(f"event-mark source must be one of {sorted(EVENT_MARK_SOURCES)}")
        if not np.isfinite(self.duration_s) or self.duration_s < 0:
            raise ValueError("event-mark duration must be finite and non-negative")
        if self.source == "note" and len(self.name) > NOTE_MAX_CHARACTERS:
            raise ValueError(f"notes hold at most {NOTE_MAX_CHARACTERS} characters")

    def applies_to(self, fiber_id: str) -> bool:
        """Whether this mark belongs with ``fiber_id``'s data (all marks but other ROIs' notes)."""

        return not self.fiber_id or self.fiber_id == fiber_id


def _or_nan(value: float | None) -> float:
    return float("nan") if value is None else float(value)


MEAN_REFERENCE_START_S = 5.0
"""Mean reference images start this far into a recording, once the LEDs have settled."""
MEAN_REFERENCE_FRAMES = 10


@dataclass(frozen=True, slots=True)
class MeanReferenceImage:
    """The mean of ``frames`` consecutive camera frames of one wavelength (derived)."""

    image: NDArray[np.uint16]
    first_s: float
    last_s: float
    frames: int

    def describe(self, wavelength_nm: int) -> str:
        return (
            f"Mean of {self.frames} consecutive {wavelength_nm} nm frames from "
            f"{self.first_s:.3f} to {self.last_s:.3f} s, rounded to uint16; derived."
        )


@dataclass(slots=True)
class AcquisitionData:
    """Completed non-frame session data used by the NWB finalizer."""

    traces: dict[Wavelength, list[TraceSample]] = field(
        default_factory=lambda: {wavelength: [] for wavelength in Wavelength}
    )
    ttl_edges: list[TTLEdge] = field(default_factory=list)
    dropped_frames: list[DroppedFrameEvent] = field(default_factory=list)
    invalid_times: list[InvalidTimeInterval] = field(default_factory=list)
    system_events: list[SystemEvent] = field(default_factory=list)
    event_marks: list[EventMark] = field(default_factory=list)
    mean_reference_images: dict[Wavelength, MeanReferenceImage] = field(default_factory=dict)
    session_log: str = ""
    """Human-readable session log; written into every NWB and the recording folder."""
    frame_ids: list[int] = field(default_factory=list)
    frame_timestamps_s: list[float] = field(default_factory=list)
    frame_sequences: list[int] = field(default_factory=list)
    frame_ticks_us: list[int] = field(default_factory=list)
    frame_wavelengths_nm: list[int] = field(default_factory=list)
    frame_commanded_voltages_v: list[float] = field(default_factory=list)
    """Commanded LED voltage per frame; NaN where the system commands percent."""
    frame_commanded_power_percent: list[float] = field(default_factory=list)
    """Commanded LED power per frame; NaN where the system commands volts."""
    frame_camera_indices: list[int] = field(default_factory=list)
    frame_wavelength_sources: list[str] = field(default_factory=list)
    frame_camera_timestamps_ns: list[int] = field(default_factory=list)
    """The camera's own hardware timestamp per frame; -1 where none was reported."""
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
        exposure = packet.exposure
        self.frame_commanded_voltages_v.append(_or_nan(exposure.commanded_voltage_v))
        self.frame_commanded_power_percent.append(_or_nan(exposure.commanded_power_percent))
        self.frame_camera_indices.append(exposure.camera_index)
        self.frame_wavelength_sources.append(exposure.wavelength_source)
        self.frame_camera_timestamps_ns.append(
            -1 if packet.camera_timestamp_ns is None else packet.camera_timestamp_ns
        )
        self.frame_host_received_s.append(packet.host_received_s)
        self.frame_saturation_fractions.append(sample.saturation_fractions.copy())
        self.frame_saved_indices.append(saved_index)
