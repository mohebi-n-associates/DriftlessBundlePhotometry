"""Normalize raw RWD ticks onto one explicit session time base."""

from __future__ import annotations

import math
from collections.abc import Hashable
from dataclasses import dataclass, replace

from driftless_photometry.config import RWDSourceConfig, SessionConfig

from .domain import (
    RWDFluorescenceRecord,
    RWDSessionData,
    RWDWavelength,
)

_TICK_MODULUS = 2**32
_HALF_TICK_MODULUS = _TICK_MODULUS // 2


class RWDClockDiscontinuity(RuntimeError):
    """Raw RWD timestamps moved backward without a uint32 rollover."""


@dataclass(frozen=True, slots=True)
class RWDNormalizedSample:
    wire_sequence: int
    host_received_s: float
    machine_name: bytes
    device_channel: int
    fiber_id: str
    wavelength_nm: RWDWavelength
    raw_timestamp_tick: int
    unwrapped_timestamp_tick: int
    timestamp_s: float
    raw_value: int
    scaled_value: float


@dataclass(frozen=True, slots=True)
class RWDNormalizedEvent:
    wire_sequence: int
    host_received_s: float
    machine_name: bytes
    raw_timestamp_tick: int
    unwrapped_timestamp_tick: int
    timestamp_s: float
    event_name_raw: bytes
    event_name: str
    status: int
    active: bool


@dataclass(frozen=True, slots=True)
class RWDNormalizedRecord:
    wire_sequence: int
    host_received_s: float
    machine_name: bytes
    timestamp_s: float
    record_type: str
    device_channel: int
    sample_count: int
    wavelength_mask: int


@dataclass(frozen=True, slots=True)
class RWDNormalizedSession:
    origin_unwrapped_tick: int | None
    samples: tuple[RWDNormalizedSample, ...]
    events: tuple[RWDNormalizedEvent, ...]
    records: tuple[RWDNormalizedRecord, ...]


class RWDTickResolver:
    """Resolve uint32 ticks independently per stream onto a nearby shared epoch."""

    def __init__(self) -> None:
        self._anchor: int | None = None
        self._previous: dict[Hashable, tuple[int, int]] = {}

    def resolve(self, key: Hashable, raw_tick: int, *, strict: bool = True) -> int:
        previous = self._previous.get(key)
        if previous is None:
            if self._anchor is None:
                unwrapped = raw_tick
            else:
                approximate_epoch = round((self._anchor - raw_tick) / _TICK_MODULUS)
                unwrapped = min(
                    (
                        raw_tick + epoch * _TICK_MODULUS
                        for epoch in range(approximate_epoch - 1, approximate_epoch + 2)
                    ),
                    key=lambda candidate: abs(candidate - self._anchor),
                )
        else:
            previous_raw, previous_unwrapped = previous
            epoch = (previous_unwrapped - previous_raw) // _TICK_MODULUS
            if raw_tick == previous_raw and strict:
                raise RWDClockDiscontinuity("RWD trace timestamps must strictly increase")
            if raw_tick == previous_raw:
                return previous_unwrapped
            if raw_tick < previous_raw:
                backward = previous_raw - raw_tick
                if backward <= _HALF_TICK_MODULUS:
                    raise RWDClockDiscontinuity(
                        "RWD timestamp moved backward without uint32 rollover: "
                        f"{previous_raw} -> {raw_tick}"
                    )
                epoch += 1
            unwrapped = raw_tick + epoch * _TICK_MODULUS
            if unwrapped <= previous_unwrapped:
                raise RWDClockDiscontinuity(
                    "RWD trace timestamps must strictly increase after rollover"
                )
        self._previous[key] = (raw_tick, unwrapped)
        self._anchor = unwrapped if self._anchor is None else max(self._anchor, unwrapped)
        return unwrapped


def normalize_rwd_session(
    config: SessionConfig,
    data: RWDSessionData,
) -> RWDNormalizedSession:
    """Map wire-order ticks to seconds while preserving every raw value."""

    if not isinstance(config.source, RWDSourceConfig):
        raise ValueError("RWD normalization requires an RWD acquisition source")
    source = config.source
    mapping_by_channel = {
        mapping.device_channel: mapping.fiber_id for mapping in source.channel_mappings
    }
    enabled_wavelengths = {RWDWavelength(value) for value in source.enabled_wavelengths_nm}
    resolver = RWDTickResolver()
    samples: list[RWDNormalizedSample] = []
    events: list[RWDNormalizedEvent] = []
    records: list[RWDNormalizedRecord] = []

    for expected_wire_sequence, received in enumerate(data.received_records):
        if received.wire_sequence != expected_wire_sequence:
            raise ValueError(
                "RWD wire sequence is not contiguous: "
                f"expected {expected_wire_sequence}, observed {received.wire_sequence}"
            )
        record = received.record
        if (
            data.stream_metadata is not None
            and record.machine_name != data.stream_metadata.machine_name
        ):
            raise ValueError("RWD record machine name does not match stream metadata")
        if isinstance(record, RWDFluorescenceRecord):
            fiber_id = mapping_by_channel.get(record.device_channel)
            if fiber_id is None:
                raise ValueError(f"unmapped RWD device channel: {record.device_channel}")
            mask = 0
            for sample in record.samples:
                if sample.wavelength_nm not in enabled_wavelengths:
                    raise ValueError(
                        f"RWD sample contains disabled wavelength: {int(sample.wavelength_nm)} nm"
                    )
                expected_scaled = sample.raw_value * source.value_scale
                if not math.isclose(
                    sample.scaled_value,
                    expected_scaled,
                    rel_tol=0,
                    abs_tol=max(1e-12, abs(expected_scaled) * 1e-12),
                ):
                    raise ValueError("RWD scaled value does not match persisted value_scale")
                key = ("fluorescence", record.device_channel, sample.wavelength_nm)
                unwrapped = resolver.resolve(
                    key,
                    sample.timestamp_tick,
                )
                mask |= {
                    RWDWavelength.LED_410: 0x01,
                    RWDWavelength.LED_470: 0x02,
                    RWDWavelength.LED_560: 0x04,
                }[sample.wavelength_nm]
                samples.append(
                    RWDNormalizedSample(
                        wire_sequence=received.wire_sequence,
                        host_received_s=received.host_received_s,
                        machine_name=record.machine_name,
                        device_channel=record.device_channel,
                        fiber_id=fiber_id,
                        wavelength_nm=sample.wavelength_nm,
                        raw_timestamp_tick=sample.timestamp_tick,
                        unwrapped_timestamp_tick=unwrapped,
                        timestamp_s=0.0,
                        raw_value=sample.raw_value,
                        scaled_value=sample.scaled_value,
                    )
                )
            records.append(
                RWDNormalizedRecord(
                    wire_sequence=received.wire_sequence,
                    host_received_s=received.host_received_s,
                    machine_name=record.machine_name,
                    timestamp_s=0.0,
                    record_type="fluorescence",
                    device_channel=record.device_channel,
                    sample_count=len(record.samples),
                    wavelength_mask=mask,
                )
            )
        else:
            unwrapped = resolver.resolve(
                "events",
                record.timestamp_tick,
                strict=False,
            )
            event_name = record.event_name
            events.append(
                RWDNormalizedEvent(
                    wire_sequence=received.wire_sequence,
                    host_received_s=received.host_received_s,
                    machine_name=record.machine_name,
                    raw_timestamp_tick=record.timestamp_tick,
                    unwrapped_timestamp_tick=unwrapped,
                    timestamp_s=0.0,
                    event_name_raw=record.event_name_raw,
                    event_name=event_name,
                    status=record.status,
                    active=record.active,
                )
            )
            records.append(
                RWDNormalizedRecord(
                    wire_sequence=received.wire_sequence,
                    host_received_s=received.host_received_s,
                    machine_name=record.machine_name,
                    timestamp_s=0.0,
                    record_type="event",
                    device_channel=-1,
                    sample_count=0,
                    wavelength_mask=0,
                )
            )
    all_ticks = [sample.unwrapped_timestamp_tick for sample in samples]
    all_ticks.extend(event.unwrapped_timestamp_tick for event in events)
    origin = min(all_ticks, default=None)
    if origin is None:
        return RWDNormalizedSession(None, (), (), ())
    normalized_samples = tuple(
        replace(
            sample,
            timestamp_s=(sample.unwrapped_timestamp_tick - origin) * source.timestamp_scale_s,
        )
        for sample in samples
    )
    normalized_events = tuple(
        replace(
            event,
            timestamp_s=(event.unwrapped_timestamp_tick - origin) * source.timestamp_scale_s,
        )
        for event in events
    )
    times_by_sequence: dict[int, list[float]] = {}
    for sample in normalized_samples:
        times_by_sequence.setdefault(sample.wire_sequence, []).append(sample.timestamp_s)
    for event in normalized_events:
        times_by_sequence.setdefault(event.wire_sequence, []).append(event.timestamp_s)
    normalized_records = tuple(
        replace(record, timestamp_s=min(times_by_sequence[record.wire_sequence]))
        for record in records
    )
    return RWDNormalizedSession(origin, normalized_samples, normalized_events, normalized_records)
