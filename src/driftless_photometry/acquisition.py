"""Headless acquisition coordinator shared by CLI and GUI."""

from __future__ import annotations

import threading
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass

import numpy as np

from driftless_photometry.config import NativeSourceConfig, SessionConfig, Wavelength
from driftless_photometry.diagnostics import AcquisitionDiagnostics, FinalizationProgress
from driftless_photometry.domain import InvalidTimeInterval, SystemEvent, TraceSample
from driftless_photometry.faults import AcquisitionFault, AcquisitionFaultCode
from driftless_photometry.hardware import RigSource
from driftless_photometry.provenance import capture_runtime_provenance
from driftless_photometry.roi import extract_circular_rois
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
    if not isinstance(config.source, NativeSourceConfig):
        raise ValueError("native preview requires a native acquisition source")
    if planned_duration_s <= 0:
        raise ValueError("preview duration must be positive")
    expected_channels = {channel.wavelength_nm: channel for channel in config.enabled_channels}
    wavelength_counts = {wavelength: 0 for wavelength in expected_channels}
    frame_count = 0
    last_frame_id = -1
    last_sequence = -1
    last_controller_tick = -1
    last_timestamp = -np.inf
    maximum_saturation = 0.0
    try:
        for rig_packet in source.packets(planned_duration_s):
            packet = rig_packet.frame
            exposure = packet.exposure
            if packet.frame_id <= last_frame_id:
                raise RuntimeError("preview camera frame IDs are not strictly increasing")
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
            if not np.isclose(exposure.commanded_voltage_v, channel.voltage_v):
                raise RuntimeError(
                    f"preview {int(exposure.wavelength_nm)} nm commanded voltage does not "
                    "match configuration"
                )
            extracted = extract_circular_rois(
                packet.image,
                config.enabled_rois,
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
            last_frame_id = packet.frame_id
            last_sequence = exposure.sequence
            last_controller_tick = exposure.controller_tick_us
            last_timestamp = exposure.timestamp_s
            if on_progress is not None:
                on_progress(
                    AcquisitionProgress(
                        frame_count=frame_count,
                        sample=sample,
                        image=packet.image,
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


ProgressCallback = Callable[[AcquisitionProgress], None]
StateCallback = Callable[[AcquisitionState], None]
FinalizationCallback = Callable[[FinalizationProgress], None]


class AcquisitionEngine:
    """Coordinate matched frames, extraction, spool, and NWB finalization."""

    def __init__(self, *, spool_chunk_size: int = 16, spool_queue_size: int = 64) -> None:
        self._state = AcquisitionStateMachine()
        self._spool_chunk_size = spool_chunk_size
        self._spool_queue_size = spool_queue_size
        self._run_lock = threading.Lock()
        self._source: RigSource | None = None
        self._stop_requested = threading.Event()

    @property
    def state(self) -> AcquisitionState:
        return self._state.state

    def stop(self) -> None:
        self._stop_requested.set()
        if self._source is not None:
            self._source.stop()

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
        if not isinstance(config.source, NativeSourceConfig):
            raise ValueError("native acquisition engine requires a native acquisition source")
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
        try:
            if self.state is AcquisitionState.DISCONNECTED:
                self._transition(AcquisitionState.READY, on_state)
            if self.state is not AcquisitionState.READY:
                raise RuntimeError(f"acquisition cannot start from state {self.state}")
            storage_capacity = check_storage_capacity(config, duration_s)
            self._transition(AcquisitionState.ARMED, on_state)
            spool = SessionSpool(
                config,
                chunk_size=self._spool_chunk_size,
                queue_size=self._spool_queue_size,
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
            self._transition(AcquisitionState.RECORDING, on_state)
            for rig_packet in source.packets(duration_s):
                packet = rig_packet.frame
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
                    config.enabled_rois,
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
                for edge in rig_packet.ttl_edges:
                    spool.submit_ttl(edge)
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
                last_sequence = sequence
                last_controller_tick = packet.exposure.controller_tick_us
                last_timestamp = timestamp
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
                        )
                    )
                if self._stop_requested.is_set():
                    break
            if frame_count == 0:
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
