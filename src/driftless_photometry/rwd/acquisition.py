"""Headless coordinator for read-only RWD stream recording."""

from __future__ import annotations

import threading
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass

from driftless_photometry.config import RWDSourceConfig, SessionConfig
from driftless_photometry.diagnostics import FinalizationProgress, SpoolDiagnostics
from driftless_photometry.domain import InvalidTimeInterval, SystemEvent
from driftless_photometry.faults import AcquisitionFault, AcquisitionFaultCode
from driftless_photometry.provenance import capture_runtime_provenance
from driftless_photometry.state import AcquisitionState, AcquisitionStateMachine
from driftless_photometry.storage import (
    RWDNWBWriteReport,
    RWDSessionSpool,
    RWDSpoolBackpressureError,
    RWDSpoolError,
    RWDStorageCapacity,
    check_rwd_storage_capacity,
    load_rwd_session_spool,
    write_rwd_session_nwbs,
)

from .client import RWDClientDiagnostics, RWDRecordSource, RWDStreamClient
from .domain import RWDReceivedRecord
from .timing import RWDClockDiscontinuity


@dataclass(frozen=True, slots=True)
class RWDAcquisitionDiagnostics:
    """Current RWD connection/parser and bounded-spool diagnostics."""

    spool: SpoolDiagnostics
    client: RWDClientDiagnostics


@dataclass(frozen=True, slots=True)
class RWDAcquisitionProgress:
    """One committed wire record and the resulting diagnostics snapshot."""

    record_count: int
    received: RWDReceivedRecord
    diagnostics: RWDAcquisitionDiagnostics


@dataclass(frozen=True, slots=True)
class RWDAcquisitionRunResult:
    """Validated per-fiber outputs from one RWD connection."""

    reports: tuple[RWDNWBWriteReport, ...]
    stopped_by_request: bool
    diagnostics: RWDAcquisitionDiagnostics
    storage_capacity: RWDStorageCapacity

    @property
    def report(self) -> RWDNWBWriteReport:
        """Return the first mapped-fiber report for shared presentation code."""

        return self.reports[0]


RWDProgressCallback = Callable[[RWDAcquisitionProgress], None]
RWDStateCallback = Callable[[AcquisitionState], None]
RWDFinalizationCallback = Callable[[FinalizationProgress], None]


class RWDAcquisitionEngine:
    """Connect, parse, spool, validate, and finalize one bounded RWD run."""

    def __init__(
        self,
        *,
        spool_chunk_size: int = 128,
        spool_queue_size: int = 512,
        spool_submit_timeout_s: float = 2.0,
    ) -> None:
        if spool_chunk_size <= 0 or spool_queue_size <= 0 or spool_submit_timeout_s <= 0:
            raise ValueError("RWD spool chunk, queue, and submit timeout must be positive")
        self._state = AcquisitionStateMachine()
        self._spool_chunk_size = spool_chunk_size
        self._spool_queue_size = spool_queue_size
        self._spool_submit_timeout_s = spool_submit_timeout_s
        self._run_lock = threading.Lock()
        self._source: RWDRecordSource | None = None
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
        *,
        duration_s: float,
        source: RWDRecordSource | None = None,
        on_progress: RWDProgressCallback | None = None,
        on_state: RWDStateCallback | None = None,
        on_finalization: RWDFinalizationCallback | None = None,
    ) -> RWDAcquisitionRunResult:
        if not isinstance(config.source, RWDSourceConfig):
            raise ValueError("RWD acquisition engine requires an RWD acquisition source")
        if duration_s <= 0:
            raise ValueError("duration_s must be positive")
        if not self._run_lock.acquire(blocking=False):
            raise RuntimeError("an RWD acquisition is already running")
        resolved_source = source or RWDStreamClient(config.source)
        spool: RWDSessionSpool | None = None
        record_count = 0
        metadata_committed = False
        last_event_time_s = 0.0
        storage_capacity: RWDStorageCapacity | None = None
        self._source = resolved_source
        self._stop_requested.clear()
        try:
            if self.state is AcquisitionState.DISCONNECTED:
                self._transition(AcquisitionState.READY, on_state)
            if self.state is not AcquisitionState.READY:
                raise RuntimeError(f"RWD acquisition cannot start from state {self.state}")
            storage_capacity = check_rwd_storage_capacity(config, duration_s)
            self._transition(AcquisitionState.ARMED, on_state)
            spool = RWDSessionSpool(
                config,
                chunk_size=self._spool_chunk_size,
                queue_size=self._spool_queue_size,
                submit_timeout_s=self._spool_submit_timeout_s,
                runtime_provenance=capture_runtime_provenance(
                    adapter_name=resolved_source.adapter_name,
                    adapter_version=resolved_source.adapter_version,
                    protocol_version=resolved_source.protocol_version,
                ),
            )
            self._transition(AcquisitionState.RECORDING, on_state)
            for received in resolved_source.records(duration_s):
                if not metadata_committed:
                    metadata = resolved_source.stream_metadata
                    if metadata is None:
                        raise RuntimeError("RWD source emitted a record before stream metadata")
                    spool.set_stream_metadata(metadata)
                    metadata_committed = True
                spool.submit_record(received)
                record_count += 1
                last_event_time_s = received.host_received_s
                if on_progress is not None:
                    on_progress(
                        RWDAcquisitionProgress(
                            record_count=record_count,
                            received=received,
                            diagnostics=RWDAcquisitionDiagnostics(
                                spool=spool.diagnostics,
                                client=resolved_source.diagnostics,
                            ),
                        )
                    )
                if self._stop_requested.is_set():
                    resolved_source.stop()
            if record_count == 0:
                raise RuntimeError("RWD recording ended without receiving a record")

            self._transition(AcquisitionState.DRAINING, on_state)
            spool.close(complete=True)
            loaded = load_rwd_session_spool(spool.path)
            if not loaded.complete:
                raise RuntimeError("RWD spool did not reach a complete state")
            reports = write_rwd_session_nwbs(
                config,
                loaded.data,
                on_progress=on_finalization,
            )
            diagnostics = RWDAcquisitionDiagnostics(
                spool=spool.diagnostics,
                client=resolved_source.diagnostics,
            )
            spool.cleanup()
            self._transition(AcquisitionState.READY, on_state)
            return RWDAcquisitionRunResult(
                reports=reports,
                stopped_by_request=self._stop_requested.is_set(),
                diagnostics=diagnostics,
                storage_capacity=storage_capacity,
            )
        except BaseException as error:
            with suppress(BaseException):
                resolved_source.stop()
            if spool is not None:
                fault_code = _fault_code(error)
                invalid_time = None
                if record_count:
                    invalid_time = InvalidTimeInterval(
                        start_time_s=last_event_time_s,
                        stop_time_s=last_event_time_s,
                        reason=str(error) or type(error).__name__,
                        source_event=fault_code.value,
                    )
                with suppress(BaseException):
                    spool.abort(
                        system_event=SystemEvent(
                            timestamp_s=last_event_time_s,
                            event=f"acquisition_fault:{fault_code.value}",
                            detail=str(error),
                        ),
                        invalid_time=invalid_time,
                    )
            if self.state is not AcquisitionState.ERROR:
                self._transition(AcquisitionState.ERROR, on_state)
            raise
        finally:
            resolved_source.stop()
            self._source = None
            self._run_lock.release()

    def reset_error(self, on_state: RWDStateCallback | None = None) -> None:
        if self.state is not AcquisitionState.ERROR:
            raise RuntimeError("reset_error is only valid from ERROR")
        self._transition(AcquisitionState.READY, on_state)

    def _transition(
        self,
        target: AcquisitionState,
        callback: RWDStateCallback | None,
    ) -> None:
        self._state.transition(target)
        if callback is not None:
            callback(target)


def _fault_code(error: BaseException) -> AcquisitionFaultCode:
    if isinstance(error, AcquisitionFault):
        return error.code
    if isinstance(error, RWDClockDiscontinuity):
        return AcquisitionFaultCode.CLOCK_DISCONTINUITY
    if isinstance(error, RWDSpoolBackpressureError):
        return AcquisitionFaultCode.QUEUE_BACKPRESSURE
    if isinstance(error, RWDSpoolError | OSError):
        return AcquisitionFaultCode.STORAGE_WRITE_FAILURE
    return AcquisitionFaultCode.ACQUISITION_FAILURE
