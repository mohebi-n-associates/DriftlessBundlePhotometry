"""Typed records emitted by the vendor RWD fluorescence/event stream."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import IntEnum
from typing import TypeAlias

from driftless_photometry.config import RWDPreambleMode
from driftless_photometry.domain import InvalidTimeInterval, SystemEvent
from driftless_photometry.provenance import RuntimeProvenance

_UINT32_MAX = 2**32 - 1


class RWDWavelength(IntEnum):
    """Exact wavelength labels used by the RWD wire format."""

    LED_410 = 410
    LED_470 = 470
    LED_560 = 560


def _validate_machine_name(value: bytes) -> None:
    if len(value) != 4:
        raise ValueError("RWD machine name must contain exactly four bytes")


def _validate_uint32(value: int, field_name: str) -> None:
    if not 0 <= value <= _UINT32_MAX:
        raise ValueError(f"{field_name} must fit an unsigned 32-bit integer")


@dataclass(frozen=True, slots=True)
class RWDFluorescenceSample:
    """One wavelength value from a fixed-size RWD fluorescence record."""

    wavelength_nm: RWDWavelength
    timestamp_tick: int
    raw_value: int
    scaled_value: float

    def __post_init__(self) -> None:
        _validate_uint32(self.timestamp_tick, "RWD fluorescence timestamp")
        _validate_uint32(self.raw_value, "RWD fluorescence value")
        if not math.isfinite(self.scaled_value):
            raise ValueError("RWD scaled fluorescence value must be finite")


@dataclass(frozen=True, slots=True)
class RWDFluorescenceRecord:
    """A channel record containing one or more explicitly present wavelengths."""

    machine_name: bytes
    device_channel: int
    samples: tuple[RWDFluorescenceSample, ...]

    def __post_init__(self) -> None:
        _validate_machine_name(self.machine_name)
        if not 0 <= self.device_channel <= 255:
            raise ValueError("RWD device channel must fit an unsigned byte")
        if not self.samples:
            raise ValueError("RWD fluorescence record must contain at least one sample")
        wavelengths = [sample.wavelength_nm for sample in self.samples]
        if len(wavelengths) != len(set(wavelengths)):
            raise ValueError("RWD fluorescence wavelengths must be unique within a record")


@dataclass(frozen=True, slots=True)
class RWDEventRecord:
    """One RWD named event edge; status 0 is ON and status 1 is OFF."""

    machine_name: bytes
    timestamp_tick: int
    event_name_raw: bytes
    status: int

    def __post_init__(self) -> None:
        _validate_machine_name(self.machine_name)
        _validate_uint32(self.timestamp_tick, "RWD event timestamp")
        if len(self.event_name_raw) != 20:
            raise ValueError("RWD event name field must contain exactly 20 bytes")
        if self.status not in {0, 1}:
            raise ValueError("RWD event status must be 0 (ON) or 1 (OFF)")

    @property
    def active(self) -> bool:
        return self.status == 0

    @property
    def event_name(self) -> str:
        """Decode ASCII while retaining the exact raw field alongside it."""

        return self.event_name_raw.rstrip(b"\x00 ").decode("ascii", errors="strict")


RWDRecord: TypeAlias = RWDFluorescenceRecord | RWDEventRecord


@dataclass(frozen=True, slots=True)
class RWDReceivedRecord:
    """One parsed record with host-local receipt provenance and wire order."""

    wire_sequence: int
    host_received_s: float
    record: RWDRecord

    def __post_init__(self) -> None:
        if self.wire_sequence < 0:
            raise ValueError("RWD wire sequence must be non-negative")
        if not math.isfinite(self.host_received_s) or self.host_received_s < 0:
            raise ValueError("RWD host receipt time must be finite and non-negative")


@dataclass(frozen=True, slots=True)
class RWDStreamMetadata:
    """Connection-level identity resolved by the incremental decoder."""

    resolved_preamble_mode: RWDPreambleMode
    preamble: bytes | None
    machine_name: bytes

    def __post_init__(self) -> None:
        _validate_machine_name(self.machine_name)
        if self.preamble is not None and len(self.preamble) != 4:
            raise ValueError("RWD preamble must contain exactly four bytes")
        if self.resolved_preamble_mode is RWDPreambleMode.NONE and self.preamble is not None:
            raise ValueError("RWD no-preamble metadata cannot contain preamble bytes")
        if self.resolved_preamble_mode is RWDPreambleMode.MACHINE_NAME_4 and self.preamble is None:
            raise ValueError("RWD machine-name preamble metadata requires preamble bytes")


@dataclass(slots=True)
class RWDSessionData:
    """Committed RWD records and provenance used by recovery and NWB finalization."""

    received_records: list[RWDReceivedRecord] = field(default_factory=list)
    system_events: list[SystemEvent] = field(default_factory=list)
    invalid_times: list[InvalidTimeInterval] = field(default_factory=list)
    runtime_provenance: RuntimeProvenance | None = None
    stream_metadata: RWDStreamMetadata | None = None
