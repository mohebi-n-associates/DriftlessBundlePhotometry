"""Checksummed exact-wire capture and deterministic replay for RWD bench work."""

from __future__ import annotations

import hashlib
import json
import math
import os
import struct
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO

from driftless_photometry.config import RWDSourceConfig
from driftless_photometry.faults import AcquisitionFault, AcquisitionFaultCode
from driftless_photometry.provenance import capture_runtime_provenance

from .client import RWDClientDiagnostics
from .domain import RWDReceivedRecord, RWDStreamMetadata, RWDWavelength
from .protocol import RWD_PROTOCOL_VERSION, RWDProtocolError, RWDStreamDecoder

_MAGIC = b"DBFRWDWIRE\x01"
_CHUNK_TAG = b"C"
_FOOTER_TAG = b"F"
_UINT32 = struct.Struct("<I")
_CHUNK_HEADER = struct.Struct("<QQI")
_DIGEST_BYTES = 32
_MAX_JSON_BYTES = 1024 * 1024
_MAX_CHUNK_BYTES = 64 * 1024 * 1024


class RWDWireCaptureError(RuntimeError):
    """Raised when an exact-wire capture is incomplete or fails integrity checks."""


@dataclass(frozen=True, slots=True)
class RWDWireChunk:
    """One exact TCP receive result with its session-relative receipt time."""

    sequence: int
    host_received_s: float
    payload: bytes


@dataclass(frozen=True, slots=True)
class RWDWireCaptureInspection:
    """Validated summary of a completed wire capture."""

    path: Path
    schema_version: int
    protocol_version: str
    created_at_utc: str
    chunk_count: int
    byte_count: int
    duration_s: float
    sha256: str
    terminal_outcome: str
    replay_contract: dict[str, object]
    runtime_provenance: dict[str, object]
    stream_metadata: RWDStreamMetadata | None


def rwd_replay_contract(source: RWDSourceConfig) -> dict[str, object]:
    """Return settings that must match to interpret and remap a capture safely."""

    return {
        "protocol_version": RWD_PROTOCOL_VERSION,
        "preamble_mode": source.preamble_mode.value,
        "timestamp_scale_s": source.timestamp_scale_s,
        "value_scale": source.value_scale,
        "enabled_wavelengths_nm": list(source.enabled_wavelengths_nm),
        "channel_mappings": [
            mapping.model_dump(mode="json") for mapping in source.channel_mappings
        ],
        "expected_machine_name": source.expected_machine_name,
    }


class RWDWireCaptureWriter:
    """Append exact socket chunks to a partial file and atomically promote on close."""

    def __init__(self, path: Path, source: RWDSourceConfig) -> None:
        self.path = Path(path)
        self.partial_path = self.path.with_name(f"{self.path.name}.partial")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists() or self.partial_path.exists():
            raise FileExistsError(f"RWD wire capture output already exists: {self.path}")
        self._file = self.partial_path.open("xb")
        self._chunk_count = 0
        self._byte_count = 0
        self._duration_s = 0.0
        self._stream_digest = hashlib.sha256()
        self._closed = False
        header = {
            "schema_version": 1,
            "protocol_version": RWD_PROTOCOL_VERSION,
            "created_at_utc": datetime.now(UTC).isoformat(),
            "replay_contract": rwd_replay_contract(source),
            "runtime_provenance": capture_runtime_provenance(
                adapter_name="rwd_tcp_wire_capture",
                adapter_version="1",
                protocol_version=RWD_PROTOCOL_VERSION,
            ).to_document(),
        }
        self._write_header(header)

    @property
    def chunk_count(self) -> int:
        return self._chunk_count

    def append(self, payload: bytes, host_received_s: float) -> None:
        """Append one non-empty exact TCP chunk from the client owner thread."""

        if self._closed:
            raise RuntimeError("RWD wire capture is closed")
        if not payload:
            raise ValueError("RWD wire capture chunks must not be empty")
        if (
            not math.isfinite(host_received_s)
            or host_received_s < self._duration_s
            or host_received_s < 0
        ):
            raise ValueError("RWD wire receipt times must be finite, non-negative, and monotonic")
        digest = hashlib.sha256(payload).digest()
        receipt_ns = round(host_received_s * 1_000_000_000)
        self._file.write(_CHUNK_TAG)
        self._file.write(_CHUNK_HEADER.pack(self._chunk_count, receipt_ns, len(payload)))
        self._file.write(digest)
        self._file.write(payload)
        self._file.flush()
        self._stream_digest.update(payload)
        self._chunk_count += 1
        self._byte_count += len(payload)
        self._duration_s = receipt_ns / 1_000_000_000

    def close(
        self,
        *,
        terminal_outcome: str,
        stream_metadata: RWDStreamMetadata | None,
    ) -> RWDWireCaptureInspection:
        """Write the integrity footer, fsync, and atomically promote the capture."""

        if self._closed:
            raise RuntimeError("RWD wire capture is already closed")
        if not terminal_outcome.strip():
            raise ValueError("RWD wire capture terminal outcome must not be empty")
        footer = {
            "chunk_count": self._chunk_count,
            "byte_count": self._byte_count,
            "duration_s": self._duration_s,
            "sha256": self._stream_digest.hexdigest(),
            "terminal_outcome": terminal_outcome,
            "stream_metadata": _metadata_to_json(stream_metadata),
        }
        encoded = _encode_json(footer)
        self._file.write(_FOOTER_TAG)
        self._file.write(_UINT32.pack(len(encoded)))
        self._file.write(encoded)
        self._file.flush()
        os.fsync(self._file.fileno())
        self._file.close()
        self._closed = True
        os.replace(self.partial_path, self.path)
        return inspect_rwd_wire_capture(self.path)

    def abort(self) -> None:
        """Close an incomplete capture while retaining its explicit partial suffix."""

        if not self._closed:
            self._file.flush()
            self._file.close()
            self._closed = True

    def _write_header(self, header: dict[str, object]) -> None:
        encoded = _encode_json(header)
        self._file.write(_MAGIC)
        self._file.write(_UINT32.pack(len(encoded)))
        self._file.write(encoded)


def inspect_rwd_wire_capture(path: Path) -> RWDWireCaptureInspection:
    """Read and checksum a completed capture without retaining payloads in memory."""

    capture_path = Path(path)
    with capture_path.open("rb") as stream:
        header = _read_header(stream)
        chunks = _read_chunks(stream)
        for _chunk in chunks:
            pass
        footer = chunks.footer
    return _inspection_from_parts(capture_path, header, footer)


def iter_rwd_wire_chunks(path: Path) -> Iterator[RWDWireChunk]:
    """Yield validated chunks and verify the whole capture before returning."""

    with Path(path).open("rb") as stream:
        _read_header(stream)
        chunks = _read_chunks(stream)
        yield from chunks
        if chunks.footer is None:
            raise RWDWireCaptureError("RWD wire capture has no footer")


class RWDWireReplaySource:
    """Feed a completed exact-wire capture through the production decoder unpaced."""

    adapter_name = "rwd_wire_replay"
    adapter_version = "1"
    protocol_version = RWD_PROTOCOL_VERSION

    def __init__(self, config: RWDSourceConfig, path: Path) -> None:
        self._config = config
        self._path = Path(path)
        self.inspection = inspect_rwd_wire_capture(self._path)
        expected = rwd_replay_contract(config)
        if self.inspection.replay_contract != expected:
            raise ValueError("RWD wire capture replay contract does not match the settings")
        self._decoder = _decoder_for(config)
        self._stop_requested = threading.Event()
        self._connected = False
        self._chunk_count = 0
        self._stream_metadata: RWDStreamMetadata | None = None
        self._started = False

    @property
    def stream_metadata(self) -> RWDStreamMetadata | None:
        return self._stream_metadata

    @property
    def diagnostics(self) -> RWDClientDiagnostics:
        return RWDClientDiagnostics(
            connected=self._connected,
            stop_requested=self._stop_requested.is_set(),
            socket_reads=self._chunk_count,
            decoder=self._decoder.diagnostics,
        )

    def records(self, duration_s: float) -> Iterator[RWDReceivedRecord]:
        if duration_s <= 0:
            raise ValueError("RWD replay duration must be positive")
        if self._started:
            raise RuntimeError("an RWD wire replay source cannot be reused")
        self._started = True
        self._connected = True
        wire_sequence = 0
        try:
            for chunk in iter_rwd_wire_chunks(self._path):
                if self._stop_requested.is_set():
                    break
                self._chunk_count += 1
                try:
                    parsed_records = self._decoder.feed(chunk.payload)
                except RWDProtocolError as error:
                    raise AcquisitionFault(
                        AcquisitionFaultCode.MALFORMED_RWD_STREAM,
                        str(error),
                    ) from error
                for record in parsed_records:
                    self._capture_stream_metadata()
                    yield RWDReceivedRecord(
                        wire_sequence=wire_sequence,
                        host_received_s=chunk.host_received_s,
                        record=record,
                    )
                    wire_sequence += 1
            try:
                self._decoder.finish()
            except RWDProtocolError as error:
                raise AcquisitionFault(
                    AcquisitionFaultCode.MALFORMED_RWD_STREAM,
                    str(error),
                ) from error
            if (
                self.inspection.stream_metadata is not None
                and self._stream_metadata != self.inspection.stream_metadata
            ):
                raise RWDWireCaptureError(
                    "decoded RWD stream metadata does not match the capture footer"
                )
        finally:
            self._connected = False

    def stop(self) -> None:
        self._stop_requested.set()

    def _capture_stream_metadata(self) -> None:
        diagnostics = self._decoder.diagnostics
        if diagnostics.resolved_preamble_mode is None or diagnostics.machine_name is None:
            raise RuntimeError("RWD decoder produced a record without stream metadata")
        metadata = RWDStreamMetadata(
            resolved_preamble_mode=diagnostics.resolved_preamble_mode,
            preamble=diagnostics.preamble,
            machine_name=diagnostics.machine_name,
        )
        if self._stream_metadata is None:
            self._stream_metadata = metadata
        elif self._stream_metadata != metadata:
            raise RuntimeError("RWD stream metadata changed during one capture")


class _ChunkIterator(Iterator[RWDWireChunk]):
    def __init__(self, stream: BinaryIO) -> None:
        self._stream = stream
        self._sequence = 0
        self._byte_count = 0
        self._duration_s = 0.0
        self._stream_digest = hashlib.sha256()
        self.footer: dict[str, object] | None = None

    def __next__(self) -> RWDWireChunk:
        if self.footer is not None:
            raise StopIteration
        tag = self._stream.read(1)
        if tag == b"":
            raise RWDWireCaptureError("RWD wire capture is truncated before its footer")
        if tag == _FOOTER_TAG:
            self.footer = _read_json_block(self._stream, "footer")
            if self._stream.read(1):
                raise RWDWireCaptureError("RWD wire capture has trailing bytes")
            self._validate_footer()
            raise StopIteration
        if tag != _CHUNK_TAG:
            raise RWDWireCaptureError(f"unknown RWD wire capture tag {tag!r}")
        sequence, receipt_ns, payload_length = _CHUNK_HEADER.unpack(
            _read_exact(self._stream, _CHUNK_HEADER.size, "chunk header")
        )
        if sequence != self._sequence:
            raise RWDWireCaptureError(
                f"RWD wire chunk sequence {sequence} does not match {self._sequence}"
            )
        if payload_length == 0 or payload_length > _MAX_CHUNK_BYTES:
            raise RWDWireCaptureError(f"invalid RWD wire chunk length {payload_length}")
        expected_digest = _read_exact(self._stream, _DIGEST_BYTES, "chunk digest")
        payload = _read_exact(self._stream, payload_length, "chunk payload")
        if hashlib.sha256(payload).digest() != expected_digest:
            raise RWDWireCaptureError(f"RWD wire chunk {sequence} checksum mismatch")
        received_s = receipt_ns / 1_000_000_000
        if received_s < self._duration_s:
            raise RWDWireCaptureError("RWD wire receipt times are not monotonic")
        self._sequence += 1
        self._byte_count += payload_length
        self._duration_s = received_s
        self._stream_digest.update(payload)
        return RWDWireChunk(sequence, received_s, payload)

    def _validate_footer(self) -> None:
        assert self.footer is not None
        expected = {
            "chunk_count": self._sequence,
            "byte_count": self._byte_count,
            "duration_s": self._duration_s,
            "sha256": self._stream_digest.hexdigest(),
        }
        for key, value in expected.items():
            if self.footer.get(key) != value:
                raise RWDWireCaptureError(
                    f"RWD wire footer {key} does not match the captured chunks"
                )


def _decoder_for(config: RWDSourceConfig) -> RWDStreamDecoder:
    return RWDStreamDecoder(
        value_scale=config.value_scale,
        preamble_mode=config.preamble_mode,
        allowed_channels=frozenset(mapping.device_channel for mapping in config.channel_mappings),
        enabled_wavelengths=frozenset(
            RWDWavelength(wavelength) for wavelength in config.enabled_wavelengths_nm
        ),
        expected_machine_name=(
            None
            if config.expected_machine_name is None
            else config.expected_machine_name.encode("ascii")
        ),
    )


def _inspection_from_parts(
    path: Path,
    header: dict[str, object],
    footer: dict[str, object] | None,
) -> RWDWireCaptureInspection:
    if footer is None:
        raise RWDWireCaptureError("RWD wire capture has no footer")
    metadata = _metadata_from_json(footer.get("stream_metadata"))
    try:
        return RWDWireCaptureInspection(
            path=path,
            schema_version=int(header["schema_version"]),
            protocol_version=str(header["protocol_version"]),
            created_at_utc=str(header["created_at_utc"]),
            chunk_count=int(footer["chunk_count"]),
            byte_count=int(footer["byte_count"]),
            duration_s=float(footer["duration_s"]),
            sha256=str(footer["sha256"]),
            terminal_outcome=str(footer["terminal_outcome"]),
            replay_contract=dict(header["replay_contract"]),
            runtime_provenance=dict(header["runtime_provenance"]),
            stream_metadata=metadata,
        )
    except (KeyError, TypeError, ValueError) as error:
        raise RWDWireCaptureError(f"invalid RWD wire capture metadata: {error}") from error


def _read_header(stream: BinaryIO) -> dict[str, object]:
    if _read_exact(stream, len(_MAGIC), "magic") != _MAGIC:
        raise RWDWireCaptureError("not a supported RWD wire capture")
    header = _read_json_block(stream, "header")
    if header.get("schema_version") != 1:
        raise RWDWireCaptureError(
            f"unsupported RWD wire capture schema {header.get('schema_version')!r}"
        )
    if header.get("protocol_version") != RWD_PROTOCOL_VERSION:
        raise RWDWireCaptureError(
            f"unsupported RWD protocol version {header.get('protocol_version')!r}"
        )
    return header


def _read_chunks(stream: BinaryIO) -> _ChunkIterator:
    return _ChunkIterator(stream)


def _read_json_block(stream: BinaryIO, label: str) -> dict[str, object]:
    (length,) = _UINT32.unpack(_read_exact(stream, _UINT32.size, f"{label} length"))
    if length == 0 or length > _MAX_JSON_BYTES:
        raise RWDWireCaptureError(f"invalid RWD wire capture {label} length {length}")
    try:
        value = json.loads(_read_exact(stream, length, label).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RWDWireCaptureError(f"invalid RWD wire capture {label}: {error}") from error
    if not isinstance(value, dict):
        raise RWDWireCaptureError(f"RWD wire capture {label} must be an object")
    return value


def _read_exact(stream: BinaryIO, length: int, label: str) -> bytes:
    value = stream.read(length)
    if len(value) != length:
        raise RWDWireCaptureError(f"RWD wire capture is truncated in {label}")
    return value


def _encode_json(value: dict[str, object]) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _metadata_to_json(metadata: RWDStreamMetadata | None) -> dict[str, object] | None:
    if metadata is None:
        return None
    return {
        "resolved_preamble_mode": metadata.resolved_preamble_mode.value,
        "preamble_hex": None if metadata.preamble is None else metadata.preamble.hex(),
        "machine_name_hex": metadata.machine_name.hex(),
    }


def _metadata_from_json(value: object) -> RWDStreamMetadata | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise RWDWireCaptureError("invalid RWD wire stream metadata")
    from driftless_photometry.config import RWDPreambleMode

    try:
        preamble_hex = value["preamble_hex"]
        return RWDStreamMetadata(
            resolved_preamble_mode=RWDPreambleMode(str(value["resolved_preamble_mode"])),
            preamble=None if preamble_hex is None else bytes.fromhex(str(preamble_hex)),
            machine_name=bytes.fromhex(str(value["machine_name_hex"])),
        )
    except (KeyError, TypeError, ValueError) as error:
        raise RWDWireCaptureError(f"invalid RWD wire stream metadata: {error}") from error
