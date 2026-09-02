"""Pure incremental decoder for RWD fluorescence and event TCP records."""

from __future__ import annotations

import math
import struct
from collections.abc import Iterator
from dataclasses import dataclass

from driftless_photometry.config import RWDPreambleMode

from .domain import (
    RWDEventRecord,
    RWDFluorescenceRecord,
    RWDFluorescenceSample,
    RWDRecord,
    RWDWavelength,
)

FLUORESCENCE_RECORD_BYTES = 31
EVENT_RECORD_BYTES = 30
RWD_PROTOCOL_VERSION = "vendor_matlab_stream_evidence_v1"

_FLUORESCENCE_TYPE = 0x01
_EVENT_TYPE = 0x02
_RECORD_LENGTHS = {
    _FLUORESCENCE_TYPE: FLUORESCENCE_RECORD_BYTES,
    _EVENT_TYPE: EVENT_RECORD_BYTES,
}
_WAVELENGTH_SLOTS = (
    (0x01, RWDWavelength.LED_410, 7),
    (0x02, RWDWavelength.LED_470, 15),
    (0x04, RWDWavelength.LED_560, 23),
)


class RWDProtocolError(RuntimeError):
    """The stream cannot be decoded without inventing an undocumented boundary."""


@dataclass(frozen=True, slots=True)
class RWDDecoderDiagnostics:
    bytes_received: int
    records_decoded: int
    fluorescence_records: int
    event_records: int
    buffered_bytes: int
    configured_preamble_mode: RWDPreambleMode
    resolved_preamble_mode: RWDPreambleMode | None
    preamble: bytes | None
    machine_name: bytes | None
    failed: bool


def parse_fluorescence_record(
    record: bytes,
    *,
    value_scale: float = 0.001,
    allowed_channels: frozenset[int] | None = None,
    enabled_wavelengths: frozenset[RWDWavelength] | None = None,
    expected_machine_name: bytes | None = None,
) -> RWDFluorescenceRecord:
    """Parse one complete 31-byte vendor fluorescence record."""

    if len(record) != FLUORESCENCE_RECORD_BYTES:
        raise RWDProtocolError(f"RWD fluorescence record must be {FLUORESCENCE_RECORD_BYTES} bytes")
    if record[4] != _FLUORESCENCE_TYPE:
        raise RWDProtocolError("RWD fluorescence record has the wrong type byte")
    _validate_scale(value_scale)
    machine_name = record[:4]
    _validate_expected_machine(machine_name, expected_machine_name)
    mask = record[5]
    if mask & ~0x07:
        raise RWDProtocolError(f"RWD fluorescence mask has reserved bits set: 0x{mask:02X}")
    if mask == 0:
        raise RWDProtocolError("RWD fluorescence record has no present wavelength")
    device_channel = record[6]
    if allowed_channels is not None and device_channel not in allowed_channels:
        raise RWDProtocolError(f"unmapped RWD device channel: {device_channel}")

    samples = []
    for bit, wavelength, offset in _WAVELENGTH_SLOTS:
        if not mask & bit:
            continue
        if enabled_wavelengths is not None and wavelength not in enabled_wavelengths:
            raise RWDProtocolError(f"RWD record contains disabled wavelength: {int(wavelength)} nm")
        timestamp_tick, raw_value = struct.unpack_from("<II", record, offset)
        samples.append(
            RWDFluorescenceSample(
                wavelength_nm=wavelength,
                timestamp_tick=timestamp_tick,
                raw_value=raw_value,
                scaled_value=raw_value * value_scale,
            )
        )
    try:
        return RWDFluorescenceRecord(
            machine_name=machine_name,
            device_channel=device_channel,
            samples=tuple(samples),
        )
    except ValueError as error:  # Defensive consistency for all malformed wire values.
        raise RWDProtocolError(str(error)) from error


def parse_event_record(
    record: bytes,
    *,
    expected_machine_name: bytes | None = None,
) -> RWDEventRecord:
    """Parse one complete 30-byte vendor event record."""

    if len(record) != EVENT_RECORD_BYTES:
        raise RWDProtocolError(f"RWD event record must be {EVENT_RECORD_BYTES} bytes")
    if record[4] != _EVENT_TYPE:
        raise RWDProtocolError("RWD event record has the wrong type byte")
    machine_name = record[:4]
    _validate_expected_machine(machine_name, expected_machine_name)
    timestamp_tick = struct.unpack_from("<I", record, 5)[0]
    try:
        event = RWDEventRecord(
            machine_name=machine_name,
            timestamp_tick=timestamp_tick,
            event_name_raw=record[9:29],
            status=record[29],
        )
    except ValueError as error:
        raise RWDProtocolError(str(error)) from error
    try:
        name = event.event_name
    except UnicodeDecodeError as error:
        raise RWDProtocolError("RWD event name is not ASCII") from error
    if not name:
        raise RWDProtocolError("RWD event name is empty")
    if any(ord(character) < 0x20 or ord(character) > 0x7E for character in name):
        raise RWDProtocolError("RWD event name contains non-printable ASCII")
    return event


class RWDStreamDecoder:
    """Decode fixed RWD records across arbitrary TCP fragmentation/coalescing.

    Callers must exhaust each iterator returned by :meth:`feed` before feeding more
    bytes. This generator interface yields every valid record preceding a malformed
    record in the same TCP chunk before surfacing the fail-closed error.
    """

    def __init__(
        self,
        *,
        value_scale: float = 0.001,
        preamble_mode: RWDPreambleMode = RWDPreambleMode.AUTO,
        allowed_channels: frozenset[int] | None = None,
        enabled_wavelengths: frozenset[RWDWavelength] | None = None,
        expected_machine_name: bytes | None = None,
    ) -> None:
        _validate_scale(value_scale)
        if expected_machine_name is not None and (
            not isinstance(expected_machine_name, bytes) or len(expected_machine_name) != 4
        ):
            raise ValueError("expected RWD machine name must contain exactly four bytes")
        self._value_scale = value_scale
        self._configured_preamble_mode = preamble_mode
        self._allowed_channels = allowed_channels
        self._enabled_wavelengths = enabled_wavelengths
        self._configured_machine_name = expected_machine_name
        self._machine_name = expected_machine_name
        self._resolved_preamble_mode: RWDPreambleMode | None = None
        self._preamble: bytes | None = None
        self._buffer = bytearray()
        self._bytes_received = 0
        self._records_decoded = 0
        self._fluorescence_records = 0
        self._event_records = 0
        self._failed = False
        self._feed_active = False

    @property
    def diagnostics(self) -> RWDDecoderDiagnostics:
        return RWDDecoderDiagnostics(
            bytes_received=self._bytes_received,
            records_decoded=self._records_decoded,
            fluorescence_records=self._fluorescence_records,
            event_records=self._event_records,
            buffered_bytes=len(self._buffer),
            configured_preamble_mode=self._configured_preamble_mode,
            resolved_preamble_mode=self._resolved_preamble_mode,
            preamble=self._preamble,
            machine_name=self._machine_name,
            failed=self._failed,
        )

    def feed(self, data: bytes) -> Iterator[RWDRecord]:
        """Consume bytes and yield all newly complete records in wire order."""

        if self._failed:
            raise RWDProtocolError("RWD decoder is failed and cannot accept more bytes")
        if self._feed_active:
            raise RuntimeError("the previous RWD decoder feed iterator was not exhausted")
        self._feed_active = True
        try:
            self._buffer.extend(data)
            self._bytes_received += len(data)
            self._resolve_preamble_if_possible()
            while self._resolved_preamble_mode is not None and len(self._buffer) >= 5:
                record_type = self._buffer[4]
                record_length = _RECORD_LENGTHS.get(record_type)
                if record_length is None:
                    self._fail(f"unknown RWD record type: 0x{record_type:02X}")
                if len(self._buffer) < record_length:
                    break
                raw_record = bytes(self._buffer[:record_length])
                del self._buffer[:record_length]
                try:
                    record = self._parse_record(raw_record, record_type)
                    self._observe_machine_name(record.machine_name)
                except RWDProtocolError:
                    self._failed = True
                    raise
                self._records_decoded += 1
                if isinstance(record, RWDFluorescenceRecord):
                    self._fluorescence_records += 1
                else:
                    self._event_records += 1
                yield record
        finally:
            self._feed_active = False

    def finish(self) -> None:
        """Surface a truncated preamble or record when the TCP stream reaches EOF."""

        if self._failed:
            raise RWDProtocolError("RWD decoder failed before end of stream")
        if self._feed_active:
            raise RuntimeError("cannot finish while an RWD feed iterator is active")
        if self._buffer:
            self._fail(f"truncated RWD stream at EOF with {len(self._buffer)} buffered byte(s)")

    def _resolve_preamble_if_possible(self) -> None:
        if self._resolved_preamble_mode is not None:
            return
        mode = self._configured_preamble_mode
        if mode is RWDPreambleMode.NONE:
            self._resolved_preamble_mode = mode
            return
        if mode is RWDPreambleMode.MACHINE_NAME_4:
            if len(self._buffer) < 4:
                return
            self._consume_preamble()
            return
        if len(self._buffer) < 5:
            return
        if self._buffer[4] in _RECORD_LENGTHS:
            self._resolved_preamble_mode = RWDPreambleMode.NONE
        else:
            self._consume_preamble()

    def _consume_preamble(self) -> None:
        self._preamble = bytes(self._buffer[:4])
        del self._buffer[:4]
        self._resolved_preamble_mode = RWDPreambleMode.MACHINE_NAME_4
        self._observe_machine_name(self._preamble)

    def _parse_record(self, raw_record: bytes, record_type: int) -> RWDRecord:
        if record_type == _FLUORESCENCE_TYPE:
            return parse_fluorescence_record(
                raw_record,
                value_scale=self._value_scale,
                allowed_channels=self._allowed_channels,
                enabled_wavelengths=self._enabled_wavelengths,
                expected_machine_name=self._machine_name,
            )
        return parse_event_record(
            raw_record,
            expected_machine_name=self._machine_name,
        )

    def _observe_machine_name(self, observed: bytes) -> None:
        if self._machine_name is None:
            self._machine_name = observed
            return
        if observed != self._machine_name:
            self._fail(
                f"RWD machine name changed: expected {self._machine_name!r}, observed {observed!r}"
            )

    def _fail(self, message: str) -> None:
        self._failed = True
        raise RWDProtocolError(message)


def _validate_scale(value_scale: float) -> None:
    if not math.isfinite(value_scale) or value_scale <= 0:
        raise ValueError("RWD fluorescence value scale must be finite and positive")


def _validate_expected_machine(observed: bytes, expected: bytes | None) -> None:
    if expected is not None and observed != expected:
        raise RWDProtocolError(
            f"RWD machine name mismatch: expected {expected!r}, observed {observed!r}"
        )
