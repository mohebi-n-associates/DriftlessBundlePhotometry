"""Explicit recovery of committed RWD spools into trace-only NWB files."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from driftless_photometry.rwd import RWDFluorescenceRecord

from .rwd_nwb import RWDNWBWriteReport, recover_or_resume_rwd_session_nwbs
from .rwd_spool import load_rwd_session_spool, remove_rwd_session_spool


@dataclass(frozen=True, slots=True)
class RWDSpoolInspection:
    """Checksum-validated summary of a source-only RWD recovery spool."""

    path: Path
    session_id: str
    schema_version: int
    acquisition_complete: bool
    stream_record_count: int
    fluorescence_record_count: int
    trace_sample_count: int
    named_event_count: int
    invalid_time_count: int
    system_event_count: int
    fiber_file_count: int


@dataclass(frozen=True, slots=True)
class RWDRecoveryReport:
    """Validated RWD recovery result for the complete mapped-fiber file set."""

    nwbs: tuple[RWDNWBWriteReport, ...]
    spool_path: Path
    acquisition_was_complete: bool
    spool_removed: bool
    inspection: RWDSpoolInspection
    reused_existing_outputs: bool


def inspect_rwd_session_spool(path: Path) -> RWDSpoolInspection:
    """Checksum and summarize committed RWD records without mutating outputs."""

    loaded = load_rwd_session_spool(path)
    fluorescence_records = sum(
        isinstance(received.record, RWDFluorescenceRecord)
        for received in loaded.data.received_records
    )
    return RWDSpoolInspection(
        path=loaded.path,
        session_id=loaded.config.session_id,
        schema_version=loaded.schema_version,
        acquisition_complete=loaded.complete,
        stream_record_count=len(loaded.data.received_records),
        fluorescence_record_count=fluorescence_records,
        trace_sample_count=sum(
            len(getattr(received.record, "samples", ()))
            for received in loaded.data.received_records
        ),
        named_event_count=len(loaded.data.received_records) - fluorescence_records,
        invalid_time_count=len(loaded.data.invalid_times),
        system_event_count=len(loaded.data.system_events),
        fiber_file_count=len(loaded.config.enabled_rois),
    )


def discover_rwd_session_spools(
    root: Path,
    *,
    incomplete_only: bool = False,
) -> tuple[RWDSpoolInspection, ...]:
    """Inspect direct ``*.rwd-spool`` children under a chosen directory."""

    resolved = root.expanduser().resolve()
    if not resolved.is_dir():
        raise NotADirectoryError(f"RWD spool discovery root is not a directory: {resolved}")
    inspections = []
    for candidate in sorted(resolved.glob("*.rwd-spool")):
        if not candidate.is_dir():
            continue
        inspection = inspect_rwd_session_spool(candidate)
        if not incomplete_only or not inspection.acquisition_complete:
            inspections.append(inspection)
    return tuple(inspections)


def recover_rwd_session_spool(
    path: Path,
    *,
    keep_spool: bool = False,
) -> RWDRecoveryReport:
    """Recover committed RWD records and remove the spool only after validation."""

    loaded = load_rwd_session_spool(path)
    inspection = inspect_rwd_session_spool(path)
    recovery_event = (
        "rwd_spool_recovered_complete"
        if loaded.complete
        else "rwd_spool_recovered_incomplete_acquisition"
    )
    reports, reused_existing_outputs = recover_or_resume_rwd_session_nwbs(
        loaded.config,
        loaded.data,
        additional_system_events=(recovery_event,),
    )
    if not keep_spool:
        remove_rwd_session_spool(loaded.path)
    return RWDRecoveryReport(
        nwbs=reports,
        spool_path=loaded.path,
        acquisition_was_complete=loaded.complete,
        spool_removed=not keep_spool,
        inspection=inspection,
        reused_existing_outputs=reused_existing_outputs,
    )
