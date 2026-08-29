"""Explicit recovery of committed acquisition spools into canonical NWB files."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .nwb import NWBWriteReport, write_session_nwb
from .spool import load_session_spool, remove_session_spool


@dataclass(frozen=True, slots=True)
class RecoveryReport:
    """Result of validating and finalizing all committed spool records."""

    nwb: NWBWriteReport
    spool_path: Path
    acquisition_was_complete: bool
    spool_removed: bool


def recover_session_spool(path: Path, *, keep_spool: bool = False) -> RecoveryReport:
    """Recover complete or interrupted committed chunks and validate the resulting NWB.

    The spool is removed only after the NWB file has passed the full finalization gate.
    An incomplete acquisition remains explicitly marked in the NWB lifecycle events.
    """

    loaded = load_session_spool(path)
    recovery_event = (
        "spool_recovered_complete" if loaded.complete else "spool_recovered_incomplete_acquisition"
    )
    report = write_session_nwb(
        loaded.config,
        loaded.data,
        frames=loaded.frames,
        calibration_image=loaded.calibration_image,
        additional_system_events=(recovery_event,),
    )
    if not keep_spool:
        remove_session_spool(loaded.path)
    return RecoveryReport(
        nwb=report,
        spool_path=loaded.path,
        acquisition_was_complete=loaded.complete,
        spool_removed=not keep_spool,
    )
