"""Explicit recovery of committed acquisition spools into canonical NWB files."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .nwb import NWBWriteReport, recover_or_resume_session_nwbs
from .spool import load_session_spool, remove_session_spool


@dataclass(frozen=True, slots=True)
class SpoolInspection:
    """Validated summary shown before a recovery mutates canonical outputs."""

    path: Path
    session_id: str
    schema_version: int
    acquisition_complete: bool
    frame_count: int
    trace_sample_count: int
    ttl_edge_count: int
    invalid_time_count: int
    system_event_count: int
    roi_file_count: int


@dataclass(frozen=True, slots=True)
class RecoveryReport:
    """Result of validating and finalizing all committed spool records."""

    nwbs: tuple[NWBWriteReport, ...]
    spool_path: Path
    acquisition_was_complete: bool
    spool_removed: bool
    inspection: SpoolInspection
    reused_existing_outputs: bool

    @property
    def nwb(self) -> NWBWriteReport:
        """Return the first ROI report for compatibility with single-ROI callers."""

        return self.nwbs[0]


def inspect_session_spool(path: Path) -> SpoolInspection:
    """Checksum and summarize committed records without creating or removing files."""

    loaded = load_session_spool(path)
    return SpoolInspection(
        path=loaded.path,
        session_id=loaded.config.session_id,
        schema_version=loaded.schema_version,
        acquisition_complete=loaded.complete,
        frame_count=len(loaded.data.frame_ids),
        trace_sample_count=sum(len(samples) for samples in loaded.data.traces.values()),
        ttl_edge_count=len(loaded.data.ttl_edges),
        invalid_time_count=len(loaded.data.invalid_times),
        system_event_count=len(loaded.data.system_events),
        roi_file_count=len(loaded.config.enabled_rois),
    )


def discover_session_spools(
    root: Path,
    *,
    incomplete_only: bool = False,
) -> tuple[SpoolInspection, ...]:
    """Inspect only direct, explicitly suffixed child spools under ``root``."""

    resolved = root.expanduser().resolve()
    if not resolved.is_dir():
        raise NotADirectoryError(f"spool discovery root is not a directory: {resolved}")
    inspections = []
    for candidate in sorted(resolved.glob("*.photometry-spool")):
        if not candidate.is_dir():
            continue
        inspection = inspect_session_spool(candidate)
        if not incomplete_only or not inspection.acquisition_complete:
            inspections.append(inspection)
    return tuple(inspections)


def recover_session_spool(path: Path, *, keep_spool: bool = False) -> RecoveryReport:
    """Recover complete or interrupted committed chunks and validate the resulting NWB.

    The spool is removed only after every ROI NWB has passed the full finalization
    gate. An incomplete acquisition remains explicitly marked in each NWB lifecycle
    event table.
    """

    loaded = load_session_spool(path)
    inspection = SpoolInspection(
        path=loaded.path,
        session_id=loaded.config.session_id,
        schema_version=loaded.schema_version,
        acquisition_complete=loaded.complete,
        frame_count=len(loaded.data.frame_ids),
        trace_sample_count=sum(len(samples) for samples in loaded.data.traces.values()),
        ttl_edge_count=len(loaded.data.ttl_edges),
        invalid_time_count=len(loaded.data.invalid_times),
        system_event_count=len(loaded.data.system_events),
        roi_file_count=len(loaded.config.enabled_rois),
    )
    recovery_event = (
        "spool_recovered_complete" if loaded.complete else "spool_recovered_incomplete_acquisition"
    )
    reports, reused_existing_outputs = recover_or_resume_session_nwbs(
        loaded.config,
        loaded.data,
        frames=loaded.frames,
        calibration_image=loaded.calibration_image,
        wavelength_images=loaded.wavelength_images,
        additional_system_events=(recovery_event,),
    )
    if not keep_spool:
        remove_session_spool(loaded.path)
    return RecoveryReport(
        nwbs=reports,
        spool_path=loaded.path,
        acquisition_was_complete=loaded.complete,
        spool_removed=not keep_spool,
        inspection=inspection,
        reused_existing_outputs=reused_existing_outputs,
    )
