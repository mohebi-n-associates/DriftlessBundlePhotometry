"""Append-safe, committed acquisition spool used before NWB finalization."""

from __future__ import annotations

import hashlib
import json
import os
import queue
import shutil
import threading
import time
from collections.abc import Iterator
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import IO

import numpy as np

from driftless_photometry.config import SessionConfig, Wavelength
from driftless_photometry.diagnostics import SpoolDiagnostics
from driftless_photometry.domain import (
    AcquisitionData,
    DroppedFrameEvent,
    EventMark,
    FramePacket,
    InvalidTimeInterval,
    SystemEvent,
    TraceSample,
    TTLEdge,
)
from driftless_photometry.provenance import (
    RuntimeProvenance,
    capture_runtime_provenance,
)

from .frames import FrameStream
from .nwb import _safe_filename

_SPOOL_SCHEMA_VERSION = 5
"""v5 adds the event-mark log; older DBF versions refuse a v5 spool rather than drop it."""
_SUPPORTED_SPOOL_SCHEMA_VERSIONS = {1, 2, 3, 4, _SPOOL_SCHEMA_VERSION}
_CHUNK_INDEX = "chunks.jsonl"
"""Schema v4+: one line per committed chunk, appended and fsynced. Earlier schemas kept
the list in the manifest, which made each commit rewrite every earlier entry and fell
behind at 120 frames/s after about 9000 chunks (bench, 2026-10-21)."""
_WAVELENGTH_SOURCE_CODES = {"controller": 0, "phase_derived": 1}
_WAVELENGTH_SOURCES = {code: name for name, code in _WAVELENGTH_SOURCE_CODES.items()}


class SpoolError(RuntimeError):
    """Base class for spool integrity and writer errors."""


class SpoolBackpressureError(SpoolError):
    """Raised when the bounded writer queue cannot accept data in time."""


@dataclass(frozen=True, slots=True)
class _FrameWork:
    frame_id: int
    timestamp_s: float
    sequence: int
    controller_tick_us: int
    wavelength_nm: int
    commanded_voltage_v: float | None
    commanded_power_percent: float | None
    camera_index: int
    wavelength_source: str
    camera_timestamp_ns: int | None
    host_received_s: float
    values: np.ndarray
    saturation_fractions: np.ndarray
    image: np.ndarray | None


@dataclass(frozen=True, slots=True)
class _TTLWork:
    edge: TTLEdge


@dataclass(frozen=True, slots=True)
class _InvalidTimeWork:
    interval: InvalidTimeInterval


@dataclass(frozen=True, slots=True)
class _SystemEventWork:
    event: SystemEvent


@dataclass(frozen=True, slots=True)
class _EventMarkWork:
    mark: EventMark


@dataclass(frozen=True, slots=True)
class _StopWork:
    complete: bool


_Work = _FrameWork | _TTLWork | _InvalidTimeWork | _SystemEventWork | _EventMarkWork | _StopWork


@dataclass(frozen=True, slots=True)
class LoadedSpool:
    path: Path
    config: SessionConfig
    data: AcquisitionData
    frames: FrameStream | None
    calibration_image: np.ndarray
    wavelength_images: dict[Wavelength, np.ndarray]
    complete: bool
    schema_version: int


class SessionSpool:
    """Single-owner background writer for recoverable acquisition chunks."""

    def __init__(
        self,
        config: SessionConfig,
        *,
        chunk_size: int = 16,
        queue_size: int = 64,
        submit_timeout_s: float = 2.0,
        runtime_provenance: RuntimeProvenance | None = None,
    ) -> None:
        if chunk_size <= 0 or queue_size <= 0 or submit_timeout_s <= 0:
            raise ValueError("chunk_size, queue_size, and submit timeout must be positive")
        self.config = config
        self.path = config.output_directory.expanduser().resolve() / (
            f"{_safe_filename(config.session_id)}.photometry-spool"
        )
        if self.path.exists():
            raise FileExistsError(f"session spool already exists: {self.path}")
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
        self._last_manifest_write_s = 0.0
        self._wavelengths_submitted: set[Wavelength] = set()
        self._manifest = {
            "schema_version": _SPOOL_SCHEMA_VERSION,
            "complete": False,
            "raw_capture": config.camera.raw_capture,
            "frame_count": 0,
            "chunk_count": 0,
            "ttl_edge_count": 0,
            "invalid_time_count": 0,
            "system_event_count": 0,
            "event_mark_count": 0,
            "calibration_image": None,
            "wavelength_images": {},
        }
        _atomic_write_json(self.path / "config.json", config.model_dump(mode="json"))
        self._runtime_provenance = runtime_provenance or capture_runtime_provenance(
            adapter_name="not supplied",
            protocol_version="native_acquisition_v1",
        )
        _atomic_write_json(
            self.path / "runtime_provenance.json",
            self._runtime_provenance.to_document(),
        )
        self._write_manifest()
        self._thread = threading.Thread(
            target=self._worker,
            name=f"spool-{config.session_id}",
            daemon=True,
        )
        self._thread.start()

    def submit_frame(self, packet: FramePacket, sample: TraceSample) -> None:
        self._ensure_open()
        if packet.frame_id != sample.frame_id or packet.exposure.sequence != sample.sequence:
            raise ValueError("frame and trace sample identities do not match")
        wavelength = sample.wavelength_nm
        retain_reference = wavelength not in self._wavelengths_submitted
        image = packet.image.copy() if self.config.camera.raw_capture or retain_reference else None
        self._wavelengths_submitted.add(wavelength)
        work = _FrameWork(
            frame_id=packet.frame_id,
            timestamp_s=sample.timestamp_s,
            sequence=sample.sequence,
            controller_tick_us=sample.controller_tick_us,
            wavelength_nm=int(sample.wavelength_nm),
            commanded_voltage_v=packet.exposure.commanded_voltage_v,
            commanded_power_percent=packet.exposure.commanded_power_percent,
            camera_index=packet.exposure.camera_index,
            wavelength_source=packet.exposure.wavelength_source,
            camera_timestamp_ns=packet.camera_timestamp_ns,
            host_received_s=packet.host_received_s,
            values=sample.values.astype(np.float32, copy=True),
            saturation_fractions=sample.saturation_fractions.astype(np.float32, copy=True),
            image=image,
        )
        self._put(work)

    def submit_ttl(self, edge: TTLEdge) -> None:
        self._ensure_open()
        self._put(_TTLWork(edge=edge))

    def submit_invalid_time(self, interval: InvalidTimeInterval) -> None:
        self._ensure_open()
        self._put(_InvalidTimeWork(interval=interval))

    def submit_system_event(self, event: SystemEvent) -> None:
        self._ensure_open()
        self._put(_SystemEventWork(event=event))

    def submit_event_mark(self, mark: EventMark) -> None:
        self._ensure_open()
        self._put(_EventMarkWork(mark=mark))

    @property
    def diagnostics(self) -> SpoolDiagnostics:
        """Return a thread-safe snapshot without blocking the writer."""

        with self._diagnostics_lock:
            return SpoolDiagnostics(
                queue_depth=self._queue.qsize(),
                queue_capacity=self._queue_capacity,
                peak_queue_depth=self._peak_queue_depth,
                committed_frames=int(self._manifest["frame_count"]),
                committed_chunks=int(self._manifest["chunk_count"]),
                last_write_latency_s=self._last_write_latency_s,
                maximum_write_latency_s=self._maximum_write_latency_s,
            )

    def close(self, *, complete: bool = True) -> None:
        if self._closed:
            return
        self._enqueue_stop(_StopWork(complete=complete))
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
            raise SpoolError("cannot clean up an open spool")
        remove_session_spool(self.path)

    def _ensure_open(self) -> None:
        self._raise_if_error()
        if self._closed:
            raise SpoolError("session spool is closed")

    def _put(self, work: _Work) -> None:
        try:
            self._queue.put(work, timeout=self._submit_timeout_s)
        except queue.Full as error:
            raise SpoolBackpressureError("bounded spool queue is full") from error
        with self._diagnostics_lock:
            self._peak_queue_depth = max(self._peak_queue_depth, self._queue.qsize())
        self._raise_if_error()

    def _enqueue_critical(self, work: _Work) -> None:
        """Queue a fault record during cleanup without losing it to prior pressure."""

        while True:
            self._raise_if_error()
            try:
                self._queue.put(work, timeout=0.1)
                with self._diagnostics_lock:
                    self._peak_queue_depth = max(
                        self._peak_queue_depth,
                        self._queue.qsize(),
                    )
                return
            except queue.Full:
                continue

    def _enqueue_stop(self, work: _StopWork) -> None:
        while True:
            self._raise_if_error()
            try:
                self._queue.put(work, timeout=0.1)
                return
            except queue.Full:
                continue

    def _raise_if_error(self) -> None:
        if self._error is not None:
            raise SpoolError("background spool writer failed") from self._error

    def _worker(self) -> None:
        frame_buffer: list[_FrameWork] = []
        ttl_path = self.path / "ttl_edges.jsonl"
        invalid_path = self.path / "invalid_times.jsonl"
        system_path = self.path / "system_events.jsonl"
        mark_path = self.path / "event_marks.jsonl"
        try:
            with (
                ttl_path.open("a", encoding="utf-8") as ttl_file,
                invalid_path.open("a", encoding="utf-8") as invalid_file,
                system_path.open("a", encoding="utf-8") as system_file,
                mark_path.open("a", encoding="utf-8") as mark_file,
            ):
                while True:
                    work = self._queue.get()
                    if isinstance(work, _FrameWork):
                        frame_buffer.append(work)
                        if len(frame_buffer) >= self._chunk_size:
                            self._commit_timed_chunk(frame_buffer)
                            frame_buffer.clear()
                    elif isinstance(work, _TTLWork):
                        self._write_ttl(ttl_file, work.edge)
                    elif isinstance(work, _InvalidTimeWork):
                        self._write_invalid_time(invalid_file, work.interval)
                    elif isinstance(work, _SystemEventWork):
                        self._write_system_event(system_file, work.event)
                    elif isinstance(work, _EventMarkWork):
                        self._write_line(mark_file, asdict(work.mark), "event_mark_count")
                    else:
                        if frame_buffer:
                            self._commit_timed_chunk(frame_buffer)
                        ttl_file.flush()
                        os.fsync(ttl_file.fileno())
                        invalid_file.flush()
                        os.fsync(invalid_file.fileno())
                        system_file.flush()
                        os.fsync(system_file.fileno())
                        mark_file.flush()
                        os.fsync(mark_file.fileno())
                        self._manifest["complete"] = work.complete
                        self._write_manifest()
                        self._queue.task_done()
                        return
                    self._queue.task_done()
        except BaseException as error:
            self._error = error

    def _write_ttl(self, ttl_file: IO[str], edge: TTLEdge) -> None:
        ttl_file.write(json.dumps(asdict(edge), separators=(",", ":")) + "\n")
        ttl_file.flush()
        self._manifest["ttl_edge_count"] = int(self._manifest["ttl_edge_count"]) + 1

    def _write_invalid_time(
        self,
        invalid_file: IO[str],
        interval: InvalidTimeInterval,
    ) -> None:
        invalid_file.write(json.dumps(asdict(interval), separators=(",", ":")) + "\n")
        invalid_file.flush()
        self._manifest["invalid_time_count"] = int(self._manifest["invalid_time_count"]) + 1

    def _write_line(self, log_file: IO[str], record: dict, count_key: str) -> None:
        log_file.write(json.dumps(record, separators=(",", ":")) + "\n")
        log_file.flush()
        self._manifest[count_key] = int(self._manifest[count_key]) + 1

    def _write_system_event(self, system_file: IO[str], event: SystemEvent) -> None:
        system_file.write(json.dumps(asdict(event), separators=(",", ":")) + "\n")
        system_file.flush()
        self._manifest["system_event_count"] = int(self._manifest["system_event_count"]) + 1

    def _commit_timed_chunk(self, items: list[_FrameWork]) -> None:
        started = time.perf_counter()
        self._commit_chunk(items)
        elapsed = time.perf_counter() - started
        with self._diagnostics_lock:
            self._last_write_latency_s = elapsed
            self._maximum_write_latency_s = max(self._maximum_write_latency_s, elapsed)

    def _commit_chunk(self, items: list[_FrameWork]) -> None:
        chunk_index = int(self._manifest["chunk_count"])
        chunk_name = f"chunk_{chunk_index:06d}.npz"
        chunk_path = self.path / "chunks" / chunk_name
        temporary = chunk_path.with_suffix(".npz.tmp")
        arrays: dict[str, np.ndarray] = {
            "frame_ids": np.asarray([item.frame_id for item in items], dtype=np.uint64),
            "timestamps_s": np.asarray([item.timestamp_s for item in items], dtype=np.float64),
            "sequences": np.asarray([item.sequence for item in items], dtype=np.uint64),
            "controller_ticks_us": np.asarray(
                [item.controller_tick_us for item in items], dtype=np.uint64
            ),
            "wavelengths_nm": np.asarray([item.wavelength_nm for item in items], dtype=np.uint16),
            "commanded_voltages_v": np.asarray(
                [_or_nan(item.commanded_voltage_v) for item in items], dtype=np.float32
            ),
            "commanded_power_percent": np.asarray(
                [_or_nan(item.commanded_power_percent) for item in items], dtype=np.float32
            ),
            "camera_indices": np.asarray([item.camera_index for item in items], dtype=np.uint8),
            "wavelength_sources": np.asarray(
                [_WAVELENGTH_SOURCE_CODES[item.wavelength_source] for item in items],
                dtype=np.uint8,
            ),
            "camera_timestamps_ns": np.asarray(
                [
                    -1 if item.camera_timestamp_ns is None else item.camera_timestamp_ns
                    for item in items
                ],
                dtype=np.int64,
            ),
            "host_received_s": np.asarray(
                [item.host_received_s for item in items], dtype=np.float64
            ),
            "values": np.stack([item.values for item in items]).astype(np.float32, copy=False),
            "saturation_fractions": np.stack([item.saturation_fractions for item in items]).astype(
                np.float32, copy=False
            ),
        }
        if self.config.camera.raw_capture:
            arrays["frames"] = np.stack([item.image for item in items]).astype(
                np.uint16, copy=False
            )
        with temporary.open("wb") as stream:
            np.savez(stream, **arrays)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, chunk_path)
        checksum = _sha256(chunk_path)
        entry = {"file": f"chunks/{chunk_name}", "count": len(items), "sha256": checksum}
        with (self.path / _CHUNK_INDEX).open("a", encoding="utf-8") as index:
            index.write(json.dumps(entry, separators=(",", ":")) + "\n")
            index.flush()
            os.fsync(index.fileno())
        self._manifest["chunk_count"] = chunk_index + 1
        self._manifest["frame_count"] = int(self._manifest["frame_count"]) + len(items)
        images_before = len(self._manifest["wavelength_images"])
        first_chunk = self._manifest["calibration_image"] is None
        if self._manifest["calibration_image"] is None:
            calibration = items[0].image
            if calibration is None:
                raise SpoolError("first frame image is required for calibration recovery")
            self._commit_calibration(calibration)
        for item in items:
            wavelength_key = str(item.wavelength_nm)
            if wavelength_key in self._manifest["wavelength_images"]:
                continue
            if item.image is None:
                raise SpoolError(
                    f"first {item.wavelength_nm} nm frame is required for wavelength recovery"
                )
            self._commit_wavelength_image(item.wavelength_nm, item.image)
        # The fsynced chunk index is authoritative; the manifest's counts may lag it by up
        # to a second, which recovery tolerates, so rewrite it at most once a second.
        now = time.monotonic()
        new_images = first_chunk or len(self._manifest["wavelength_images"]) != images_before
        if new_images or now - self._last_manifest_write_s >= 1.0:
            self._write_manifest()
            self._last_manifest_write_s = now

    def _commit_calibration(self, image: np.ndarray) -> None:
        calibration_path = self.path / "calibration_frame.npy"
        temporary = calibration_path.with_suffix(".npy.tmp")
        with temporary.open("wb") as stream:
            np.save(stream, image.astype(np.uint16, copy=False), allow_pickle=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, calibration_path)
        self._manifest["calibration_image"] = {
            "file": calibration_path.name,
            "sha256": _sha256(calibration_path),
        }

    def _commit_wavelength_image(self, wavelength_nm: int, image: np.ndarray) -> None:
        image_path = self.path / f"wavelength_{wavelength_nm}_reference.npy"
        temporary = image_path.with_suffix(".npy.tmp")
        with temporary.open("wb") as stream:
            np.save(stream, image.astype(np.uint16, copy=False), allow_pickle=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, image_path)
        self._manifest["wavelength_images"][str(wavelength_nm)] = {
            "file": image_path.name,
            "sha256": _sha256(image_path),
        }

    def _write_manifest(self) -> None:
        _atomic_write_json(self.path / "manifest.json", self._manifest)


def load_session_spool(path: Path) -> LoadedSpool:
    """Load and checksum all committed chunks, including an incomplete spool."""

    path = path.expanduser().resolve()
    manifest = json.loads((path / "manifest.json").read_text(encoding="utf-8"))
    schema_version = int(manifest.get("schema_version", -1))
    if schema_version not in _SUPPORTED_SPOOL_SCHEMA_VERSIONS:
        raise SpoolError("unsupported spool schema version")
    config = SessionConfig.model_validate_json((path / "config.json").read_text(encoding="utf-8"))
    data = AcquisitionData()
    raw_chunk_paths: list[Path] = []
    first_frame: np.ndarray | None = None
    last_frame: np.ndarray | None = None
    global_saved_index = 0
    expected_sequence = 0
    previous_timestamp: float | None = None
    channel_voltages = {
        int(channel.wavelength_nm): float(channel.voltage_v or 0.0)
        for channel in config.enabled_channels
    }
    for entry in _chunk_entries(path, manifest, schema_version):
        chunk_path = path / entry["file"]
        if _sha256(chunk_path) != entry["sha256"]:
            raise SpoolError(f"spool chunk checksum mismatch: {chunk_path.name}")
        with np.load(chunk_path, allow_pickle=False) as chunk:
            count = len(chunk["frame_ids"])
            if count != entry["count"]:
                raise SpoolError(f"spool chunk count mismatch: {chunk_path.name}")
            if config.camera.raw_capture:
                raw_chunk_paths.append(chunk_path)
                if first_frame is None:
                    first_frame = chunk["frames"][0].astype(np.uint16, copy=True)
                last_frame = chunk["frames"][-1].astype(np.uint16, copy=True)
            for index in range(count):
                wavelength = Wavelength(int(chunk["wavelengths_nm"][index]))
                if schema_version >= 2:
                    commanded_voltage_v = float(chunk["commanded_voltages_v"][index])
                    host_received_s = float(chunk["host_received_s"][index])
                else:
                    commanded_voltage_v = channel_voltages[int(wavelength)]
                    host_received_s = float(chunk["timestamps_s"][index])
                if schema_version >= 3:
                    commanded_power_percent = float(chunk["commanded_power_percent"][index])
                    camera_index = int(chunk["camera_indices"][index])
                    wavelength_source = _WAVELENGTH_SOURCES[int(chunk["wavelength_sources"][index])]
                    camera_timestamp_ns = int(chunk["camera_timestamps_ns"][index])
                else:
                    commanded_power_percent = float("nan")
                    camera_index = 1
                    wavelength_source = "controller"
                    camera_timestamp_ns = -1
                sample = TraceSample(
                    timestamp_s=float(chunk["timestamps_s"][index]),
                    frame_id=int(chunk["frame_ids"][index]),
                    sequence=int(chunk["sequences"][index]),
                    controller_tick_us=int(chunk["controller_ticks_us"][index]),
                    wavelength_nm=wavelength,
                    values=chunk["values"][index].astype(np.float32, copy=True),
                    saturation_fractions=chunk["saturation_fractions"][index].astype(
                        np.float32, copy=True
                    ),
                )
                if sample.sequence < expected_sequence:
                    raise SpoolError("controller sequences are not strictly increasing")
                if sample.sequence > expected_sequence:
                    data.dropped_frames.append(
                        DroppedFrameEvent(
                            timestamp_s=sample.timestamp_s,
                            expected_sequence=expected_sequence,
                            observed_sequence=sample.sequence,
                            missing_count=sample.sequence - expected_sequence,
                        )
                    )
                    if previous_timestamp is not None:
                        data.invalid_times.append(
                            InvalidTimeInterval(
                                start_time_s=previous_timestamp,
                                stop_time_s=sample.timestamp_s,
                                reason=(
                                    f"missing {sample.sequence - expected_sequence} controller "
                                    "exposure record(s)"
                                ),
                                source_event="controller_sequence_gap",
                            )
                        )
                expected_sequence = sample.sequence + 1
                data.traces[wavelength].append(sample)
                data.frame_ids.append(sample.frame_id)
                data.frame_timestamps_s.append(sample.timestamp_s)
                data.frame_sequences.append(sample.sequence)
                data.frame_ticks_us.append(sample.controller_tick_us)
                data.frame_wavelengths_nm.append(int(wavelength))
                data.frame_commanded_voltages_v.append(commanded_voltage_v)
                data.frame_commanded_power_percent.append(commanded_power_percent)
                data.frame_camera_indices.append(camera_index)
                data.frame_wavelength_sources.append(wavelength_source)
                data.frame_camera_timestamps_ns.append(camera_timestamp_ns)
                data.frame_host_received_s.append(host_received_s)
                data.frame_saturation_fractions.append(sample.saturation_fractions.copy())
                data.frame_saved_indices.append(
                    global_saved_index if config.camera.raw_capture else -1
                )
                if config.camera.raw_capture:
                    global_saved_index += 1
                previous_timestamp = sample.timestamp_s

    complete = bool(manifest["complete"])
    data.ttl_edges.extend(
        TTLEdge(**record) for record in _log_records(path / "ttl_edges.jsonl", complete)
    )
    invalid_records = _log_records(path / "invalid_times.jsonl", complete)
    data.invalid_times.extend(InvalidTimeInterval(**record) for record in invalid_records)
    explicit_invalid_count = len(invalid_records)
    data.system_events.extend(
        SystemEvent(**record) for record in _log_records(path / "system_events.jsonl", complete)
    )
    if schema_version >= 5:
        data.event_marks.extend(
            EventMark(**record) for record in _log_records(path / "event_marks.jsonl", complete)
        )

    if schema_version >= 2:
        provenance_document = json.loads(
            (path / "runtime_provenance.json").read_text(encoding="utf-8")
        )
        data.runtime_provenance = RuntimeProvenance.from_document(provenance_document)
    else:
        data.runtime_provenance = capture_runtime_provenance(
            adapter_name="legacy_spool_v1",
            adapter_version="0.1.6-or-earlier",
            protocol_version="missing_per_frame_host_receipt_and_intensity_provenance",
        )
        if data.frame_timestamps_s:
            data.invalid_times.append(
                InvalidTimeInterval(
                    start_time_s=data.frame_timestamps_s[0],
                    stop_time_s=data.frame_timestamps_s[-1],
                    reason=(
                        "legacy spool schema v1 did not persist actual per-frame commanded "
                        "voltage or host-receipt time"
                    ),
                    source_event="legacy_spool_schema_v1",
                )
            )

    calibration_entry = manifest.get("calibration_image")
    if not calibration_entry:
        raise SpoolError("spool has no committed calibration image")
    calibration_path = path / calibration_entry["file"]
    if _sha256(calibration_path) != calibration_entry["sha256"]:
        raise SpoolError("calibration image checksum mismatch")
    calibration_image = np.load(calibration_path, allow_pickle=False)
    wavelength_images: dict[Wavelength, np.ndarray] = {}
    for wavelength_text, image_entry in manifest.get("wavelength_images", {}).items():
        wavelength = Wavelength(int(wavelength_text))
        image_path = path / image_entry["file"]
        if _sha256(image_path) != image_entry["sha256"]:
            raise SpoolError(f"{int(wavelength)} nm reference image checksum mismatch")
        wavelength_images[wavelength] = np.load(image_path, allow_pickle=False)
    if not wavelength_images and data.frame_wavelengths_nm:
        wavelength_images[Wavelength(data.frame_wavelengths_nm[0])] = calibration_image.copy()
    frames = None
    if raw_chunk_paths:
        if first_frame is None or last_frame is None:
            raise SpoolError("raw spool has no readable frame endpoints")
        chunk_paths = tuple(raw_chunk_paths)

        def iter_frames() -> Iterator[np.ndarray]:
            for raw_chunk_path in chunk_paths:
                with np.load(raw_chunk_path, allow_pickle=False) as chunk:
                    for frame in chunk["frames"]:
                        yield frame.astype(np.uint16, copy=True)

        frames = FrameStream(
            count=len(data.frame_ids),
            frame_shape=(config.camera.height_px, config.camera.width_px),
            iterator_factory=iter_frames,
            first_frame=first_frame,
            last_frame=last_frame,
        )
    if schema_version >= 4:
        # A crash between the index append and the manifest rewrite leaves the index one
        # chunk ahead; the checksummed index is authoritative.
        if len(data.frame_ids) < manifest["frame_count"]:
            raise SpoolError("chunk index is missing frames the manifest recorded")
    elif len(data.frame_ids) != manifest["frame_count"]:
        raise SpoolError("manifest frame count does not match committed chunks")
    # A complete spool must match its manifest exactly. An interrupted one may hold
    # log lines written after the last manifest update, but never fewer.
    for observed, expected, what in (
        (len(data.ttl_edges), int(manifest["ttl_edge_count"]), "TTL count does not match edge log"),
        (
            explicit_invalid_count,
            int(manifest.get("invalid_time_count", 0)),
            "invalid-time count does not match interval log",
        ),
        (
            len(data.system_events),
            int(manifest.get("system_event_count", 0)),
            "system-event count does not match event log",
        ),
        (
            len(data.event_marks),
            int(manifest.get("event_mark_count", 0)),
            "event-mark count does not match mark log",
        ),
    ):
        if observed < expected or (complete and observed != expected):
            raise SpoolError(f"manifest {what}")
    return LoadedSpool(
        path=path,
        config=config,
        data=data,
        frames=frames,
        calibration_image=calibration_image,
        wavelength_images=wavelength_images,
        complete=bool(manifest["complete"]),
        schema_version=schema_version,
    )


def _log_records(path: Path, complete: bool) -> list[dict]:
    """Read a JSON-lines log; an interrupted spool may end in one torn line."""

    if not path.exists():
        return []
    lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line]
    records = []
    for number, line in enumerate(lines):
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            if not complete and number == len(lines) - 1:
                break
            raise SpoolError(f"{path.name} line {number + 1} is corrupt") from None
    return records


def _chunk_entries(path: Path, manifest: dict, schema_version: int) -> list[dict]:
    if schema_version < 4:
        return list(manifest["chunks"])
    index_path = path / _CHUNK_INDEX
    if not index_path.exists():
        return []
    entries = []
    lines = index_path.read_text(encoding="utf-8").splitlines()
    for number, line in enumerate(lines):
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            if number == len(lines) - 1:
                break  # a torn final line from a crash mid-append
            raise SpoolError(f"chunk index line {number + 1} is corrupt") from None
    return entries


def remove_session_spool(path: Path) -> None:
    """Remove only a recognizable session spool after successful recovery/finalization."""

    resolved = path.expanduser().resolve()
    manifest_path = resolved / "manifest.json"
    config_path = resolved / "config.json"
    if (
        resolved.name.endswith(".photometry-spool")
        and manifest_path.is_file()
        and config_path.is_file()
    ):
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest.get("schema_version") in _SUPPORTED_SPOOL_SCHEMA_VERSIONS:
            shutil.rmtree(resolved)
            return
    raise SpoolError(f"refusing to remove unrecognized spool path: {resolved}")


def _or_nan(value: float | None) -> float:
    return float("nan") if value is None else float(value)


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
