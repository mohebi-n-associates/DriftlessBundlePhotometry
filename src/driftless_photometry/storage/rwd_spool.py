"""Checksummed, append-safe spool for mixed RWD trace/event records."""

from __future__ import annotations

import hashlib
import json
import os
import queue
import shutil
import threading
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from io import TextIOBase
from pathlib import Path
from typing import TypeVar

from driftless_photometry.config import RWDPreambleMode, RWDSourceConfig, SessionConfig
from driftless_photometry.diagnostics import SpoolDiagnostics
from driftless_photometry.domain import InvalidTimeInterval, SystemEvent
from driftless_photometry.provenance import RuntimeProvenance, capture_runtime_provenance
from driftless_photometry.rwd import (
    RWD_PROTOCOL_VERSION,
    RWDEventRecord,
    RWDFluorescenceRecord,
    RWDFluorescenceSample,
    RWDReceivedRecord,
    RWDSessionData,
    RWDStreamMetadata,
    RWDWavelength,
    normalize_rwd_session,
)

from .nwb import _safe_filename

_RWD_SPOOL_SCHEMA_VERSION = 1


class RWDSpoolError(RuntimeError):
    """RWD spool integrity, lifecycle, or background-writer failure."""


class RWDSpoolBackpressureError(RWDSpoolError):
    """The bounded RWD writer queue could not accept another record in time."""


@dataclass(frozen=True, slots=True)
class _RecordWork:
    received: RWDReceivedRecord


@dataclass(frozen=True, slots=True)
class _SystemEventWork:
    event: SystemEvent


@dataclass(frozen=True, slots=True)
class _InvalidTimeWork:
    interval: InvalidTimeInterval


@dataclass(frozen=True, slots=True)
class _MetadataWork:
    metadata: RWDStreamMetadata


@dataclass(frozen=True, slots=True)
class _StopWork:
    complete: bool


_Work = _RecordWork | _SystemEventWork | _InvalidTimeWork | _MetadataWork | _StopWork


@dataclass(frozen=True, slots=True)
class LoadedRWDSpool:
    path: Path
    config: SessionConfig
    data: RWDSessionData
    complete: bool
    schema_version: int


class RWDSessionSpool:
    """Single-owner background writer for source-only RWD records."""

    def __init__(
        self,
        config: SessionConfig,
        *,
        chunk_size: int = 128,
        queue_size: int = 512,
        submit_timeout_s: float = 2.0,
        runtime_provenance: RuntimeProvenance | None = None,
    ) -> None:
        if not isinstance(config.source, RWDSourceConfig):
            raise ValueError("RWD session spool requires an RWD acquisition source")
        if chunk_size <= 0 or queue_size <= 0 or submit_timeout_s <= 0:
            raise ValueError("chunk_size, queue_size, and submit timeout must be positive")
        self.config = config
        self.path = config.output_directory.expanduser().resolve() / (
            f"{_safe_filename(config.session_id)}.rwd-spool"
        )
        if self.path.exists():
            raise FileExistsError(f"RWD session spool already exists: {self.path}")
        self.path.mkdir(parents=True)
        (self.path / "chunks").mkdir()
        self._chunk_size = chunk_size
        self._submit_timeout_s = submit_timeout_s
        self._queue: queue.Queue[_Work] = queue.Queue(maxsize=queue_size)
        self._queue_capacity = queue_size
        self._peak_queue_depth = 0
        self._last_write_latency_s = 0.0
        self._maximum_write_latency_s = 0.0
        self._diagnostics_lock = threading.Lock()
        self._error: BaseException | None = None
        self._closed = False
        self._next_wire_sequence = 0
        self._metadata_submitted = False
        self._manifest: dict[str, object] = {
            "schema_version": _RWD_SPOOL_SCHEMA_VERSION,
            "source_kind": "rwd",
            "complete": False,
            "record_count": 0,
            "fluorescence_record_count": 0,
            "trace_sample_count": 0,
            "event_count": 0,
            "system_event_count": 0,
            "invalid_time_count": 0,
            "system_events_sha256": None,
            "invalid_times_sha256": None,
            "stream_metadata": None,
            "chunks": [],
        }
        _atomic_write_json(self.path / "config.json", config.model_dump(mode="json"))
        provenance = runtime_provenance or capture_runtime_provenance(
            adapter_name="rwd_read_only_stream",
            adapter_version="1",
            protocol_version=RWD_PROTOCOL_VERSION,
        )
        _atomic_write_json(self.path / "runtime_provenance.json", provenance.to_document())
        for log_name in ("system_events.jsonl", "invalid_times.jsonl"):
            with (self.path / log_name).open("x", encoding="utf-8") as stream:
                stream.flush()
                os.fsync(stream.fileno())
        self._manifest["config_sha256"] = _sha256(self.path / "config.json")
        self._manifest["runtime_provenance_sha256"] = _sha256(self.path / "runtime_provenance.json")
        self._manifest["system_events_sha256"] = _sha256(self.path / "system_events.jsonl")
        self._manifest["invalid_times_sha256"] = _sha256(self.path / "invalid_times.jsonl")
        self._write_manifest()
        self._thread = threading.Thread(
            target=self._worker,
            name=f"rwd-spool-{config.session_id}",
            daemon=True,
        )
        self._thread.start()

    def submit_record(self, received: RWDReceivedRecord) -> None:
        self._ensure_open()
        if received.wire_sequence != self._next_wire_sequence:
            raise ValueError(
                "RWD wire sequence must be contiguous: "
                f"expected {self._next_wire_sequence}, observed {received.wire_sequence}"
            )
        self._put(_RecordWork(received=received))
        self._next_wire_sequence += 1

    def submit_system_event(self, event: SystemEvent) -> None:
        self._ensure_open()
        self._put(_SystemEventWork(event=event))

    def submit_invalid_time(self, interval: InvalidTimeInterval) -> None:
        self._ensure_open()
        self._put(_InvalidTimeWork(interval=interval))

    def set_stream_metadata(self, metadata: RWDStreamMetadata) -> None:
        self._ensure_open()
        if self._metadata_submitted:
            raise RWDSpoolError("RWD stream metadata was already submitted")
        self._put(_MetadataWork(metadata=metadata))
        self._metadata_submitted = True

    @property
    def diagnostics(self) -> SpoolDiagnostics:
        with self._diagnostics_lock:
            chunks = self._manifest["chunks"]
            assert isinstance(chunks, list)
            return SpoolDiagnostics(
                queue_depth=self._queue.qsize(),
                queue_capacity=self._queue_capacity,
                peak_queue_depth=self._peak_queue_depth,
                committed_frames=int(self._manifest["record_count"]),
                committed_chunks=len(chunks),
                last_write_latency_s=self._last_write_latency_s,
                maximum_write_latency_s=self._maximum_write_latency_s,
            )

    def close(self, *, complete: bool = True) -> None:
        if self._closed:
            return
        try:
            self._enqueue_critical(_StopWork(complete=complete))
        except BaseException:
            self._thread.join()
            self._closed = True
            raise
        else:
            self._thread.join()
            self._closed = True
        self._raise_if_error()

    def abort(
        self,
        *,
        system_event: SystemEvent | None = None,
        invalid_time: InvalidTimeInterval | None = None,
    ) -> None:
        if self._closed:
            return
        if system_event is not None:
            self._enqueue_critical(_SystemEventWork(event=system_event))
        if invalid_time is not None:
            self._enqueue_critical(_InvalidTimeWork(interval=invalid_time))
        self.close(complete=False)

    def cleanup(self) -> None:
        if not self._closed:
            raise RWDSpoolError("cannot clean up an open RWD spool")
        remove_rwd_session_spool(self.path)

    def _ensure_open(self) -> None:
        self._raise_if_error()
        if self._closed:
            raise RWDSpoolError("RWD session spool is closed")

    def _put(self, work: _Work) -> None:
        try:
            self._queue.put(work, timeout=self._submit_timeout_s)
        except queue.Full as error:
            raise RWDSpoolBackpressureError("bounded RWD spool queue is full") from error
        self._record_queue_depth()
        self._raise_if_error()

    def _enqueue_critical(self, work: _Work) -> None:
        while True:
            self._raise_if_error()
            try:
                self._queue.put(work, timeout=0.1)
                self._record_queue_depth()
                return
            except queue.Full:
                continue

    def _record_queue_depth(self) -> None:
        with self._diagnostics_lock:
            self._peak_queue_depth = max(self._peak_queue_depth, self._queue.qsize())

    def _raise_if_error(self) -> None:
        if self._error is not None:
            raise RWDSpoolError("background RWD spool writer failed") from self._error

    def _worker(self) -> None:
        record_buffer: list[RWDReceivedRecord] = []
        system_path = self.path / "system_events.jsonl"
        invalid_path = self.path / "invalid_times.jsonl"
        try:
            with (
                system_path.open("a", encoding="utf-8") as system_file,
                invalid_path.open("a", encoding="utf-8") as invalid_file,
            ):
                while True:
                    work = self._queue.get()
                    if isinstance(work, _RecordWork):
                        record_buffer.append(work.received)
                        if len(record_buffer) >= self._chunk_size:
                            self._commit_timed_chunk(record_buffer)
                            record_buffer.clear()
                    elif isinstance(work, _SystemEventWork):
                        self._write_jsonl(system_file, asdict(work.event))
                        self._manifest["system_event_count"] = (
                            int(self._manifest["system_event_count"]) + 1
                        )
                        os.fsync(system_file.fileno())
                        self._manifest["system_events_sha256"] = _sha256(system_path)
                        self._write_manifest()
                    elif isinstance(work, _InvalidTimeWork):
                        self._write_jsonl(invalid_file, asdict(work.interval))
                        self._manifest["invalid_time_count"] = (
                            int(self._manifest["invalid_time_count"]) + 1
                        )
                        os.fsync(invalid_file.fileno())
                        self._manifest["invalid_times_sha256"] = _sha256(invalid_path)
                        self._write_manifest()
                    elif isinstance(work, _MetadataWork):
                        if self._manifest["stream_metadata"] is not None:
                            raise RWDSpoolError("RWD stream metadata was already committed")
                        self._manifest["stream_metadata"] = _metadata_document(work.metadata)
                        self._write_manifest()
                    else:
                        if record_buffer:
                            self._commit_timed_chunk(record_buffer)
                        if (
                            int(self._manifest["record_count"])
                            and self._manifest["stream_metadata"] is None
                        ):
                            raise RWDSpoolError(
                                "RWD stream metadata is required when records were committed"
                            )
                        system_file.flush()
                        os.fsync(system_file.fileno())
                        invalid_file.flush()
                        os.fsync(invalid_file.fileno())
                        self._manifest["system_events_sha256"] = _sha256(system_path)
                        self._manifest["invalid_times_sha256"] = _sha256(invalid_path)
                        self._manifest["complete"] = work.complete
                        self._write_manifest()
                        self._queue.task_done()
                        return
                    self._queue.task_done()
        except BaseException as error:
            self._error = error

    def _write_jsonl(self, stream: TextIOBase, document: object) -> None:
        stream.write(json.dumps(document, separators=(",", ":")) + "\n")
        stream.flush()

    def _commit_timed_chunk(self, records: list[RWDReceivedRecord]) -> None:
        started = time.perf_counter()
        self._commit_chunk(records)
        elapsed = time.perf_counter() - started
        with self._diagnostics_lock:
            self._last_write_latency_s = elapsed
            self._maximum_write_latency_s = max(self._maximum_write_latency_s, elapsed)

    def _commit_chunk(self, records: list[RWDReceivedRecord]) -> None:
        chunks = self._manifest["chunks"]
        assert isinstance(chunks, list)
        chunk_name = f"chunk_{len(chunks):06d}.jsonl"
        chunk_path = self.path / "chunks" / chunk_name
        temporary = chunk_path.with_suffix(".jsonl.tmp")
        with temporary.open("w", encoding="utf-8") as stream:
            for received in records:
                stream.write(json.dumps(_received_document(received), separators=(",", ":")) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, chunk_path)
        fluorescence_count = sum(
            isinstance(received.record, RWDFluorescenceRecord) for received in records
        )
        event_count = len(records) - fluorescence_count
        trace_sample_count = sum(
            len(received.record.samples)
            for received in records
            if isinstance(received.record, RWDFluorescenceRecord)
        )
        chunks.append(
            {
                "file": f"chunks/{chunk_name}",
                "count": len(records),
                "sha256": _sha256(chunk_path),
            }
        )
        self._manifest["record_count"] = int(self._manifest["record_count"]) + len(records)
        self._manifest["fluorescence_record_count"] = (
            int(self._manifest["fluorescence_record_count"]) + fluorescence_count
        )
        self._manifest["trace_sample_count"] = (
            int(self._manifest["trace_sample_count"]) + trace_sample_count
        )
        self._manifest["event_count"] = int(self._manifest["event_count"]) + event_count
        self._write_manifest()

    def _write_manifest(self) -> None:
        _atomic_write_json(self.path / "manifest.json", self._manifest)


def load_rwd_session_spool(path: Path) -> LoadedRWDSpool:
    """Checksum and load every committed RWD record without modifying the spool."""

    resolved = path.expanduser().resolve()
    manifest = json.loads((resolved / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("schema_version") != _RWD_SPOOL_SCHEMA_VERSION:
        raise RWDSpoolError("unsupported RWD spool schema version")
    if manifest.get("source_kind") != "rwd":
        raise RWDSpoolError("RWD spool manifest has the wrong source kind")
    for file_name, checksum_field in (
        ("config.json", "config_sha256"),
        ("runtime_provenance.json", "runtime_provenance_sha256"),
        ("system_events.jsonl", "system_events_sha256"),
        ("invalid_times.jsonl", "invalid_times_sha256"),
    ):
        candidate = resolved / file_name
        expected_checksum = manifest.get(checksum_field)
        if not candidate.is_file() or _sha256(candidate) != expected_checksum:
            raise RWDSpoolError(f"RWD spool checksum mismatch: {file_name}")
    config = SessionConfig.model_validate_json(
        (resolved / "config.json").read_text(encoding="utf-8")
    )
    if not isinstance(config.source, RWDSourceConfig):
        raise RWDSpoolError("RWD spool configuration has the wrong source kind")
    provenance = RuntimeProvenance.from_document(
        json.loads((resolved / "runtime_provenance.json").read_text(encoding="utf-8"))
    )
    data = RWDSessionData(runtime_provenance=provenance)
    for entry in manifest["chunks"]:
        chunk_path = resolved / entry["file"]
        if _sha256(chunk_path) != entry["sha256"]:
            raise RWDSpoolError(f"RWD spool chunk checksum mismatch: {chunk_path.name}")
        lines = [line for line in chunk_path.read_text(encoding="utf-8").splitlines() if line]
        if len(lines) != entry["count"]:
            raise RWDSpoolError(f"RWD spool chunk count mismatch: {chunk_path.name}")
        data.received_records.extend(_received_from_document(json.loads(line)) for line in lines)

    _load_event_log(
        resolved / "system_events.jsonl",
        data.system_events,
        SystemEvent,
    )
    _load_event_log(
        resolved / "invalid_times.jsonl",
        data.invalid_times,
        InvalidTimeInterval,
    )
    metadata_document = manifest.get("stream_metadata")
    if metadata_document is not None:
        data.stream_metadata = _metadata_from_document(metadata_document)
    elif data.received_records:
        raise RWDSpoolError("RWD spool with records has no stream metadata")

    fluorescence_count = sum(
        isinstance(received.record, RWDFluorescenceRecord) for received in data.received_records
    )
    trace_sample_count = sum(
        len(received.record.samples)
        for received in data.received_records
        if isinstance(received.record, RWDFluorescenceRecord)
    )
    event_count = len(data.received_records) - fluorescence_count
    expected_counts = {
        "record_count": len(data.received_records),
        "fluorescence_record_count": fluorescence_count,
        "trace_sample_count": trace_sample_count,
        "event_count": event_count,
        "system_event_count": len(data.system_events),
        "invalid_time_count": len(data.invalid_times),
    }
    for field, observed in expected_counts.items():
        if int(manifest.get(field, -1)) != observed:
            raise RWDSpoolError(f"RWD manifest {field} does not match committed records")
    try:
        normalize_rwd_session(config, data)
    except (ValueError, RuntimeError) as error:
        raise RWDSpoolError(f"RWD spool records violate the session contract: {error}") from error
    return LoadedRWDSpool(
        path=resolved,
        config=config,
        data=data,
        complete=bool(manifest["complete"]),
        schema_version=int(manifest["schema_version"]),
    )


def remove_rwd_session_spool(path: Path) -> None:
    resolved = path.expanduser().resolve()
    manifest_path = resolved / "manifest.json"
    config_path = resolved / "config.json"
    if resolved.name.endswith(".rwd-spool") and manifest_path.is_file() and config_path.is_file():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if (
            manifest.get("schema_version") == _RWD_SPOOL_SCHEMA_VERSION
            and manifest.get("source_kind") == "rwd"
        ):
            shutil.rmtree(resolved)
            return
    raise RWDSpoolError(f"refusing to remove unrecognized RWD spool path: {resolved}")


def _received_document(received: RWDReceivedRecord) -> dict[str, object]:
    record = received.record
    base: dict[str, object] = {
        "wire_sequence": received.wire_sequence,
        "host_received_s": received.host_received_s,
        "machine_name_hex": record.machine_name.hex(),
    }
    if isinstance(record, RWDFluorescenceRecord):
        base.update(
            kind="fluorescence",
            device_channel=record.device_channel,
            samples=[
                {
                    "wavelength_nm": int(sample.wavelength_nm),
                    "timestamp_tick": sample.timestamp_tick,
                    "raw_value": sample.raw_value,
                    "scaled_value": sample.scaled_value,
                }
                for sample in record.samples
            ],
        )
    else:
        base.update(
            kind="event",
            timestamp_tick=record.timestamp_tick,
            event_name_hex=record.event_name_raw.hex(),
            status=record.status,
        )
    return base


def _received_from_document(document: dict[str, object]) -> RWDReceivedRecord:
    machine_name = bytes.fromhex(str(document["machine_name_hex"]))
    if document["kind"] == "fluorescence":
        sample_documents = document["samples"]
        if not isinstance(sample_documents, list):
            raise RWDSpoolError("RWD fluorescence samples must be a list")
        record = RWDFluorescenceRecord(
            machine_name=machine_name,
            device_channel=int(document["device_channel"]),
            samples=tuple(
                RWDFluorescenceSample(
                    wavelength_nm=RWDWavelength(int(sample["wavelength_nm"])),
                    timestamp_tick=int(sample["timestamp_tick"]),
                    raw_value=int(sample["raw_value"]),
                    scaled_value=float(sample["scaled_value"]),
                )
                for sample in sample_documents
            ),
        )
    elif document["kind"] == "event":
        record = RWDEventRecord(
            machine_name=machine_name,
            timestamp_tick=int(document["timestamp_tick"]),
            event_name_raw=bytes.fromhex(str(document["event_name_hex"])),
            status=int(document["status"]),
        )
    else:
        raise RWDSpoolError(f"unknown RWD spool record kind: {document['kind']!r}")
    return RWDReceivedRecord(
        wire_sequence=int(document["wire_sequence"]),
        host_received_s=float(document["host_received_s"]),
        record=record,
    )


def _metadata_document(metadata: RWDStreamMetadata) -> dict[str, object]:
    return {
        "resolved_preamble_mode": metadata.resolved_preamble_mode.value,
        "preamble_hex": None if metadata.preamble is None else metadata.preamble.hex(),
        "machine_name_hex": metadata.machine_name.hex(),
    }


def _metadata_from_document(document: dict[str, object]) -> RWDStreamMetadata:
    preamble_hex = document["preamble_hex"]
    return RWDStreamMetadata(
        resolved_preamble_mode=RWDPreambleMode(str(document["resolved_preamble_mode"])),
        preamble=None if preamble_hex is None else bytes.fromhex(str(preamble_hex)),
        machine_name=bytes.fromhex(str(document["machine_name_hex"])),
    )


_EventLogRecord = TypeVar("_EventLogRecord", SystemEvent, InvalidTimeInterval)


def _load_event_log(
    path: Path,
    target: list[_EventLogRecord],
    record_type: Callable[..., _EventLogRecord],
) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        if line:
            target.append(record_type(**json.loads(line)))


def _atomic_write_json(path: Path, value: object) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(value, stream, indent=2, sort_keys=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()
