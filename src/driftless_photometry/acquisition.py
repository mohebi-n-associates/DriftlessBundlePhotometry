"""Headless acquisition coordinator shared by CLI and GUI."""

from __future__ import annotations

import math
import threading
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass

import numpy as np

from driftless_photometry.config import ChannelConfig, SessionConfig, Wavelength
from driftless_photometry.diagnostics import AcquisitionDiagnostics, FinalizationProgress
from driftless_photometry.domain import (
    EventMark,
    ExposureRecord,
    InvalidTimeInterval,
    SystemEvent,
    TraceSample,
    TTLEdge,
)
from driftless_photometry.events import TTLPulseClassifier
from driftless_photometry.faults import AcquisitionFault, AcquisitionFaultCode
from driftless_photometry.hardware import ExcitationCycle, RigSource, StartCondition
from driftless_photometry.provenance import capture_runtime_provenance
from driftless_photometry.roi import extract_circular_rois, rois_for_camera
from driftless_photometry.state import AcquisitionState, AcquisitionStateMachine
from driftless_photometry.storage import (
    NWBWriteReport,
    SessionSpool,
    SpoolBackpressureError,
    SpoolError,
    StorageCapacity,
    check_storage_capacity,
    load_session_spool,
    write_session_nwbs,
)


@dataclass(frozen=True, slots=True)
class AcquisitionProgress:
    frame_count: int
    sample: TraceSample
    image: np.ndarray
    diagnostics: AcquisitionDiagnostics | None = None
    camera_index: int = 1
    event_marks: tuple[EventMark, ...] = ()
    """Marks recorded since the previous progress report, for live display."""


@dataclass(frozen=True, slots=True)
class AcquisitionRunResult:
    reports: tuple[NWBWriteReport, ...]
    stopped_by_request: bool
    diagnostics: AcquisitionDiagnostics
    storage_capacity: StorageCapacity

    @property
    def report(self) -> NWBWriteReport:
        """Return the first ROI report for compatibility with single-ROI callers."""

        return self.reports[0]


@dataclass(frozen=True, slots=True)
class PreviewResult:
    """Validated, non-recording acquisition preflight summary."""

    frame_count: int
    roi_count: int
    duration_s: float
    wavelength_frame_counts: tuple[tuple[Wavelength, int], ...]
    maximum_saturation_fraction: float


def spool_chunk_size_for(config: SessionConfig) -> int:
    """Frames per spool commit: at least 16, and about a quarter second at high rates.

    Each commit costs about three fsyncs, so fixed 16-frame chunks commit 7.5 times per
    second at 120 frames/s; quarter-second chunks keep the rate near 4/s at any speed
    while a crash loses at most the uncommitted quarter second.
    """

    return max(16, round(config.camera.requested_fps / 4))


SPOOL_BUFFER_S = 30.0
"""Seconds of frames the spool queue holds while its writer is stalled."""
SPOOL_RAW_BUFFER_BYTES = 1_000_000_000
"""Memory cap for queued raw frames when full frames are retained."""


def spool_queue_size_for(config: SessionConfig) -> int:
    """Queue capacity in frames, so a disk stall is measured in seconds, not frames.

    A fixed 64-frame queue lasted half a second at 120 frames/s, and a single 6 s write
    stall then ended a recording (bench, 2026-10-21). Trace-only frames are small, so
    30 s costs a few megabytes; retained raw frames are capped at about 1 GB.
    """

    frames = math.ceil(config.camera.requested_fps * SPOOL_BUFFER_S)
    if config.camera.raw_capture:
        frame_bytes = config.camera.width_px * config.camera.height_px * 2
        frames = min(frames, SPOOL_RAW_BUFFER_BYTES // frame_bytes)
    return max(64, frames)


LIVE_VIEW_LIMIT_S = 600.0
"""Live view stops by itself after this long, so the LEDs are never left running."""


@dataclass(frozen=True, slots=True)
class LiveFrame:
    """One camera frame shown by live view; nothing about it is recorded."""

    frame_count: int
    camera_index: int
    wavelength_nm: Wavelength
    timestamp_s: float
    image: np.ndarray


def run_live_view(
    config: SessionConfig,
    source: RigSource,
    *,
    on_frame: Callable[[LiveFrame], None],
    duration_s: float = LIVE_VIEW_LIMIT_S,
    max_rate_hz: float = 15.0,
) -> int:
    """Stream frames for focusing and ROI placement until stopped; return the frame count.

    Nothing is validated or written. Each camera and wavelength is handed to
    ``on_frame`` at most ``max_rate_hz`` times per second, so the display keeps up at
    any acquisition rate.
    """

    if duration_s <= 0 or max_rate_hz <= 0:
        raise ValueError("live-view duration and display rate must be positive")
    shape = (config.camera.height_px, config.camera.width_px)
    minimum_gap_s = 1.0 / max_rate_hz
    last_shown: dict[tuple[int, Wavelength], float] = {}
    frame_count = 0
    try:
        for rig_packet in source.packets(duration_s):
            packet = rig_packet.frame
            if packet is None:
                continue
            if tuple(packet.image.shape) != shape:
                raise RuntimeError("live-view camera frame shape does not match configuration")
            frame_count += 1
            exposure = packet.exposure
            key = (exposure.camera_index, exposure.wavelength_nm)
            previous = last_shown.get(key)
            if previous is not None and packet.host_received_s - previous < minimum_gap_s:
                continue
            last_shown[key] = packet.host_received_s
            on_frame(
                LiveFrame(
                    frame_count=frame_count,
                    camera_index=exposure.camera_index,
                    wavelength_nm=exposure.wavelength_nm,
                    timestamp_s=exposure.timestamp_s,
                    image=packet.image,
                )
            )
    finally:
        source.stop()
    return frame_count


def preview_duration_s(config: SessionConfig) -> float:
    """Return a short duration that should exercise every enabled wavelength twice."""

    return max(0.5, 2 * len(config.enabled_channels) / config.camera.requested_fps)


def run_preview(
    config: SessionConfig,
    source: RigSource,
    *,
    duration_s: float | None = None,
    on_progress: ProgressCallback | None = None,
) -> PreviewResult:
    """Exercise and validate acquisition inputs without creating scientific output."""

    planned_duration_s = preview_duration_s(config) if duration_s is None else duration_s
    if planned_duration_s <= 0:
        raise ValueError("preview duration must be positive")
    expected_channels = {channel.wavelength_nm: channel for channel in config.enabled_channels}
    wavelength_counts = {wavelength: 0 for wavelength in expected_channels}
    frame_count = 0
    last_frame_ids: dict[int, int] = {}
    last_sequence = -1
    last_controller_tick = -1
    last_timestamp = -np.inf
    maximum_saturation = 0.0
    try:
        for rig_packet in source.packets(planned_duration_s):
            packet = rig_packet.frame
            if packet is None:
                continue
            exposure = packet.exposure
            if packet.frame_id <= last_frame_ids.get(exposure.camera_index, -1):
                raise RuntimeError(
                    f"preview camera {exposure.camera_index} frame IDs are not strictly increasing"
                )
            if exposure.sequence <= last_sequence:
                raise RuntimeError("preview exposure sequence is not strictly increasing")
            if exposure.timestamp_s <= last_timestamp:
                raise RuntimeError("preview exposure timestamps are not strictly increasing")
            if exposure.controller_tick_us <= last_controller_tick:
                raise RuntimeError("preview controller ticks are not strictly increasing")
            if packet.image.dtype != np.uint16:
                raise TypeError("preview camera frames must preserve uint16 values")
            if tuple(packet.image.shape) != (
                config.camera.height_px,
                config.camera.width_px,
            ):
                raise RuntimeError("preview camera frame shape does not match configuration")
            channel = expected_channels.get(exposure.wavelength_nm)
            if channel is None:
                raise RuntimeError(
                    f"preview received disabled or unknown wavelength "
                    f"{int(exposure.wavelength_nm)} nm"
                )
            if not _commanded_intensity_matches(exposure, channel):
                raise RuntimeError(
                    f"preview {int(exposure.wavelength_nm)} nm commanded intensity does not "
                    "match configuration"
                )
            extracted = extract_circular_rois(
                packet.image,
                rois_for_camera(config.enabled_rois, exposure.camera_index),
                bit_depth=config.camera.bit_depth,
            )
            if not np.all(np.isfinite(extracted.values)):
                raise RuntimeError("preview produced non-finite ROI values")
            saturation = extracted.saturation_fractions
            if not np.all(np.isfinite(saturation)) or np.any((saturation < 0) | (saturation > 1)):
                raise RuntimeError("preview produced invalid ROI saturation fractions")
            if len(saturation):
                maximum_saturation = max(maximum_saturation, float(np.max(saturation)))
            sample = TraceSample(
                timestamp_s=exposure.timestamp_s,
                frame_id=packet.frame_id,
                sequence=exposure.sequence,
                controller_tick_us=exposure.controller_tick_us,
                wavelength_nm=exposure.wavelength_nm,
                values=extracted.values,
                saturation_fractions=saturation,
            )
            frame_count += 1
            wavelength_counts[exposure.wavelength_nm] += 1
            last_frame_ids[exposure.camera_index] = packet.frame_id
            last_sequence = exposure.sequence
            last_controller_tick = exposure.controller_tick_us
            last_timestamp = exposure.timestamp_s
            if on_progress is not None:
                on_progress(
                    AcquisitionProgress(
                        frame_count=frame_count,
                        sample=sample,
                        image=packet.image,
                        camera_index=exposure.camera_index,
                    )
                )
    finally:
        source.stop()
    if frame_count == 0:
        raise RuntimeError("preview ended without receiving a camera frame")
    missing = [wavelength for wavelength, count in wavelength_counts.items() if count < 2]
    if missing:
        rendered = ", ".join(f"{int(wavelength)} nm" for wavelength in missing)
        raise RuntimeError(f"preview did not observe two frames for wavelength(s): {rendered}")
    return PreviewResult(
        frame_count=frame_count,
        roi_count=len(config.enabled_rois),
        duration_s=planned_duration_s,
        wavelength_frame_counts=tuple(wavelength_counts.items()),
        maximum_saturation_fraction=maximum_saturation,
    )


def _commanded_intensity_matches(exposure: ExposureRecord, channel: ChannelConfig) -> bool:
    if channel.voltage_v is not None:
        return exposure.commanded_voltage_v is not None and bool(
            np.isclose(exposure.commanded_voltage_v, channel.voltage_v)
        )
    return (
        exposure.commanded_power_percent is not None
        and channel.power_percent is not None
        and bool(np.isclose(exposure.commanded_power_percent, channel.power_percent))
    )


ProgressCallback = Callable[[AcquisitionProgress], None]
StateCallback = Callable[[AcquisitionState], None]
FinalizationCallback = Callable[[FinalizationProgress], None]


class AcquisitionCancelled(RuntimeError):
    """Stop came before the recording's first frame, so nothing was recorded."""


class AcquisitionEngine:
    """Coordinate matched frames, extraction, spool, and NWB finalization."""

    def __init__(
        self, *, spool_chunk_size: int | None = None, spool_queue_size: int | None = None
    ) -> None:
        self._state = AcquisitionStateMachine()
        self._spool_chunk_size = spool_chunk_size
        self._spool_queue_size = spool_queue_size
        self._run_lock = threading.Lock()
        self._source: RigSource | None = None
        self._stop_requested = threading.Event()
        self._marks_lock = threading.Lock()
        self._pending_marks: list[tuple[float, str, str]] = []

    @property
    def state(self) -> AcquisitionState:
        return self._state.state

    def stop(self) -> None:
        self._stop_requested.set()
        if self._source is not None:
            self._source.stop()

    def mark_event(
        self, name: str, source: str = "key", *, at_monotonic_s: float | None = None
    ) -> None:
        """Record a named operator event at ``at_monotonic_s`` (now by default).

        Thread-safe. The host instant is placed on the session clock through the most
        recently received camera frame, so it lags the true instant by that frame's
        delivery delay (one exposure plus readout, typically tens of milliseconds).
        """

        if source not in {"key", "button"}:
            raise ValueError("operator marks come from a key or a button")
        if not name:
            raise ValueError("event marks need a name")
        pressed_s = time.monotonic() if at_monotonic_s is None else at_monotonic_s
        with self._marks_lock:
            self._pending_marks.append((pressed_s, name, source))

    def run(
        self,
        config: SessionConfig,
        source: RigSource,
        *,
        duration_s: float,
        on_progress: ProgressCallback | None = None,
        on_state: StateCallback | None = None,
        on_finalization: FinalizationCallback | None = None,
    ) -> AcquisitionRunResult:
        if duration_s <= 0:
            raise ValueError("duration_s must be positive")
        if not self._run_lock.acquire(blocking=False):
            raise RuntimeError("an acquisition is already running")
        spool: SessionSpool | None = None
        frame_count = 0
        last_sequence = -1
        last_controller_tick = -1
        last_timestamp = -np.inf
        first_transport_offset_s: float | None = None
        clock_residual_s = 0.0
        maximum_absolute_clock_residual_s = 0.0
        dropped_frames = 0
        storage_capacity: StorageCapacity | None = None
        self._source = source
        self._stop_requested.clear()
        with self._marks_lock:
            self._pending_marks.clear()
        last_frame_monotonic_s: float | None = None
        new_marks: list[EventMark] = []
        classifier = TTLPulseClassifier(config.ttl_events)
        try:
            if self.state is AcquisitionState.DISCONNECTED:
                self._transition(AcquisitionState.READY, on_state)
            if self.state is not AcquisitionState.READY:
                raise RuntimeError(f"acquisition cannot start from state {self.state}")
            storage_capacity = check_storage_capacity(config, duration_s)
            start = StartCondition.for_recording(config)
            cycle = ExcitationCycle.for_recording(config)
            # Ask for packets before reporting ARMED: a Stop from then on must be kept.
            packets = (
                source.packets(duration_s)
                if start is None and cycle is None
                else source.packets(duration_s, start=start, cycle=cycle)
            )
            self._transition(AcquisitionState.ARMED, on_state)
            spool = SessionSpool(
                config,
                chunk_size=self._spool_chunk_size or spool_chunk_size_for(config),
                queue_size=self._spool_queue_size or spool_queue_size_for(config),
                runtime_provenance=capture_runtime_provenance(
                    adapter_name=str(
                        getattr(
                            source,
                            "adapter_name",
                            f"{type(source).__module__}.{type(source).__qualname__}",
                        )
                    ),
                    adapter_version=str(getattr(source, "adapter_version", "not supplied")),
                    protocol_version=str(getattr(source, "protocol_version", "not supplied")),
                ),
            )
            recording = config.recording
            stop_on_ttl = recording.stop == "ttl"
            if start is None:
                self._transition(AcquisitionState.RECORDING, on_state)
            # Otherwise waiting for the start condition stays ARMED until the first frame.

            def submit_mark(mark: EventMark) -> None:
                spool.submit_event_mark(mark)
                new_marks.append(mark)

            def flush_operator_marks() -> None:
                with self._marks_lock:
                    pending = self._pending_marks[:]
                    self._pending_marks.clear()
                for pressed_s, name, mark_source in pending:
                    if last_frame_monotonic_s is None:
                        submit_mark(
                            EventMark(0.0, name, mark_source, "set before the first exposure")
                        )
                        continue
                    at_s = last_timestamp + (pressed_s - last_frame_monotonic_s)
                    submit_mark(EventMark(max(0.0, at_s), name, mark_source))

            def submit_edges(edges: tuple[TTLEdge, ...]) -> None:
                nonlocal stop_on_ttl
                for edge in edges:
                    spool.submit_ttl(edge)
                    named = classifier.observe(edge)
                    if named is not None:
                        submit_mark(named)
                    if (
                        stop_on_ttl
                        and edge.line == recording.stop_ttl_line
                        and edge.edge == recording.stop_ttl_edge
                    ):
                        stop_on_ttl = False
                        spool.submit_system_event(
                            SystemEvent(
                                timestamp_s=edge.timestamp_s,
                                event="recording_stop_trigger",
                                detail=f"TTL {edge.edge} edge on input {edge.line} "
                                f"({edge.label}) stopped the recording",
                            )
                        )
                        self.stop()

            for rig_packet in packets:
                for interval in rig_packet.invalid_times:
                    spool.submit_invalid_time(interval)
                for event in rig_packet.system_events:
                    spool.submit_system_event(event)
                packet = rig_packet.frame
                if packet is None:
                    submit_edges(rig_packet.ttl_edges)
                    continue
                if self.state is AcquisitionState.ARMED:
                    self._transition(AcquisitionState.RECORDING, on_state)
                sequence = packet.exposure.sequence
                timestamp = packet.exposure.timestamp_s
                if sequence <= last_sequence:
                    raise AcquisitionFault(
                        AcquisitionFaultCode.CLOCK_DISCONTINUITY,
                        "controller exposure sequence is not strictly increasing",
                    )
                if timestamp <= last_timestamp:
                    raise AcquisitionFault(
                        AcquisitionFaultCode.CLOCK_DISCONTINUITY,
                        "camera exposure timestamps are not strictly increasing",
                    )
                if packet.exposure.controller_tick_us <= last_controller_tick:
                    raise AcquisitionFault(
                        AcquisitionFaultCode.CLOCK_DISCONTINUITY,
                        "controller ticks are not strictly increasing",
                    )
                if tuple(packet.image.shape) != (
                    config.camera.height_px,
                    config.camera.width_px,
                ):
                    raise RuntimeError("camera frame shape changed during acquisition")
                extracted = extract_circular_rois(
                    packet.image,
                    rois_for_camera(config.enabled_rois, packet.exposure.camera_index),
                    bit_depth=config.camera.bit_depth,
                )
                sample = TraceSample(
                    timestamp_s=timestamp,
                    frame_id=packet.frame_id,
                    sequence=sequence,
                    controller_tick_us=packet.exposure.controller_tick_us,
                    wavelength_nm=packet.exposure.wavelength_nm,
                    values=extracted.values,
                    saturation_fractions=extracted.saturation_fractions,
                )
                spool.submit_frame(packet, sample)
                submit_edges(rig_packet.ttl_edges)
                frame_count += 1
                if last_sequence >= 0 and sequence > last_sequence + 1:
                    dropped_frames += sequence - last_sequence - 1
                transport_offset_s = packet.host_received_s - timestamp
                if first_transport_offset_s is None:
                    first_transport_offset_s = transport_offset_s
                clock_residual_s = transport_offset_s - first_transport_offset_s
                maximum_absolute_clock_residual_s = max(
                    maximum_absolute_clock_residual_s,
                    abs(clock_residual_s),
                )
                # Marks set before this frame arrived are placed through the previous one.
                flush_operator_marks()
                last_sequence = sequence
                last_controller_tick = packet.exposure.controller_tick_us
                last_timestamp = timestamp
                last_frame_monotonic_s = time.monotonic()
                marks = tuple(new_marks)
                new_marks.clear()
                if on_progress is not None:
                    on_progress(
                        AcquisitionProgress(
                            frame_count=frame_count,
                            sample=sample,
                            image=packet.image,
                            diagnostics=AcquisitionDiagnostics(
                                spool=spool.diagnostics,
                                dropped_frames=dropped_frames,
                                clock_residual_s=clock_residual_s,
                                maximum_absolute_clock_residual_s=(
                                    maximum_absolute_clock_residual_s
                                ),
                            ),
                            camera_index=packet.exposure.camera_index,
                            event_marks=marks,
                        )
                    )
                # After stop(), keep reading until the source ends so that it can hand
                # over the events it observed while shutting down.
            if frame_count:
                flush_operator_marks()
            if frame_count == 0:
                if self._stop_requested.is_set():
                    spool.close(complete=False)
                    spool.cleanup()
                    spool = None
                    if self.state is AcquisitionState.RECORDING:
                        self._transition(AcquisitionState.DRAINING, on_state)
                    self._transition(AcquisitionState.READY, on_state)
                    raise AcquisitionCancelled(
                        "the recording was stopped before its first frame; nothing was recorded"
                    )
                raise RuntimeError("recording ended without receiving a camera frame")

            self._transition(AcquisitionState.DRAINING, on_state)
            spool.close(complete=True)
            loaded = load_session_spool(spool.path)
            if not loaded.complete:
                raise RuntimeError("spool did not reach a complete state")
            reports = write_session_nwbs(
                config,
                loaded.data,
                frames=loaded.frames,
                calibration_image=loaded.calibration_image,
                wavelength_images=loaded.wavelength_images,
                on_progress=on_finalization,
            )
            diagnostics = AcquisitionDiagnostics(
                spool=spool.diagnostics,
                dropped_frames=dropped_frames,
                clock_residual_s=clock_residual_s,
                maximum_absolute_clock_residual_s=maximum_absolute_clock_residual_s,
            )
            spool.cleanup()
            self._transition(AcquisitionState.READY, on_state)
            return AcquisitionRunResult(
                reports=reports,
                stopped_by_request=self._stop_requested.is_set(),
                diagnostics=diagnostics,
                storage_capacity=storage_capacity,
            )
        except AcquisitionCancelled:
            raise
        except BaseException as error:
            with suppress(BaseException):
                source.stop()
            if spool is not None:
                fault_code = _fault_code(error)
                event_timestamp = max(0.0, last_timestamp) if np.isfinite(last_timestamp) else 0.0
                invalid_time = None
                if frame_count:
                    invalid_time = InvalidTimeInterval(
                        start_time_s=event_timestamp,
                        stop_time_s=event_timestamp,
                        reason=str(error) or type(error).__name__,
                        source_event=fault_code.value,
                    )
                with suppress(BaseException):
                    spool.abort(
                        system_event=SystemEvent(
                            timestamp_s=event_timestamp,
                            event=f"acquisition_fault:{fault_code.value}",
                            detail=str(error),
                        ),
                        invalid_time=invalid_time,
                    )
            if self.state is not AcquisitionState.ERROR:
                self._transition(AcquisitionState.ERROR, on_state)
            raise
        finally:
            source.stop()
            self._source = None
            self._run_lock.release()

    def reset_error(self, on_state: StateCallback | None = None) -> None:
        if self.state is not AcquisitionState.ERROR:
            raise RuntimeError("reset_error is only valid from ERROR")
        self._transition(AcquisitionState.READY, on_state)

    def _transition(
        self,
        target: AcquisitionState,
        callback: StateCallback | None,
    ) -> None:
        self._state.transition(target)
        if callback is not None:
            callback(target)


def _fault_code(error: BaseException) -> AcquisitionFaultCode:
    if isinstance(error, AcquisitionFault):
        return error.code
    if isinstance(error, SpoolBackpressureError):
        return AcquisitionFaultCode.QUEUE_BACKPRESSURE
    if isinstance(error, SpoolError | OSError):
        return AcquisitionFaultCode.STORAGE_WRITE_FAILURE
    return AcquisitionFaultCode.ACQUISITION_FAILURE
