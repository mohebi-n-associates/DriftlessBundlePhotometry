"""Headless acquisition coordinator shared by CLI and GUI."""

from __future__ import annotations

import threading
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass

import numpy as np

from driftless_photometry.config import SessionConfig
from driftless_photometry.domain import TraceSample
from driftless_photometry.hardware import RigSource
from driftless_photometry.roi import extract_circular_rois
from driftless_photometry.state import AcquisitionState, AcquisitionStateMachine
from driftless_photometry.storage import (
    NWBWriteReport,
    SessionSpool,
    load_session_spool,
    write_session_nwbs,
)


@dataclass(frozen=True, slots=True)
class AcquisitionProgress:
    frame_count: int
    sample: TraceSample
    image: np.ndarray


@dataclass(frozen=True, slots=True)
class AcquisitionRunResult:
    reports: tuple[NWBWriteReport, ...]
    stopped_by_request: bool

    @property
    def report(self) -> NWBWriteReport:
        """Return the first ROI report for compatibility with single-ROI callers."""

        return self.reports[0]


ProgressCallback = Callable[[AcquisitionProgress], None]
StateCallback = Callable[[AcquisitionState], None]


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
        self._source = source
        self._stop_requested.clear()
        try:
            if self.state is AcquisitionState.DISCONNECTED:
                self._transition(AcquisitionState.READY, on_state)
            if self.state is not AcquisitionState.READY:
                raise RuntimeError(f"acquisition cannot start from state {self.state}")
            self._transition(AcquisitionState.ARMED, on_state)
            spool = SessionSpool(
                config,
                chunk_size=self._spool_chunk_size,
                queue_size=self._spool_queue_size,
            )
            self._transition(AcquisitionState.RECORDING, on_state)
            for rig_packet in source.packets(duration_s):
                packet = rig_packet.frame
                sequence = packet.exposure.sequence
                timestamp = packet.exposure.timestamp_s
                if sequence <= last_sequence:
                    raise RuntimeError("controller exposure sequence is not strictly increasing")
                if timestamp <= last_timestamp:
                    raise RuntimeError("camera exposure timestamps are not strictly increasing")
                if packet.exposure.controller_tick_us <= last_controller_tick:
                    raise RuntimeError("controller ticks are not strictly increasing")
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
                last_sequence = sequence
                last_controller_tick = packet.exposure.controller_tick_us
                last_timestamp = timestamp
                if on_progress is not None:
                    on_progress(
                        AcquisitionProgress(
                            frame_count=frame_count,
                            sample=sample,
                            image=packet.image,
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
            )
            spool.cleanup()
            self._transition(AcquisitionState.READY, on_state)
            return AcquisitionRunResult(
                reports=reports,
                stopped_by_request=self._stop_requested.is_set(),
            )
        except BaseException:
            if spool is not None:
                with suppress(BaseException):
                    spool.abort()
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
