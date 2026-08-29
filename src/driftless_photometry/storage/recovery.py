"""Explicit recovery of committed acquisition spools into canonical NWB files."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .nwb import NWBWriteReport, write_session_nwbs
from .spool import load_session_spool, remove_session_spool


@dataclass(frozen=True, slots=True)
class RecoveryReport:
    """Result of validating and finalizing all committed spool records."""

    nwbs: tuple[NWBWriteReport, ...]
    spool_path: Path
    acquisition_was_complete: bool
    spool_removed: bool

    @property
    def nwb(self) -> NWBWriteReport:
        """Return the first ROI report for compatibility with single-ROI callers."""

        return self.nwbs[0]


def recover_session_spool(path: Path, *, keep_spool: bool = False) -> RecoveryReport:
    """Recover complete or interrupted committed chunks and validate the resulting NWB.

    The spool is removed only after every ROI NWB has passed the full finalization
    gate. An incomplete acquisition remains explicitly marked in each NWB lifecycle
    event table.
    """

    loaded = load_session_spool(path)
    recovery_event = (
        "spool_recovered_complete" if loaded.complete else "spool_recovered_incomplete_acquisition"
    )
    reports = write_session_nwbs(
        loaded.config,
        loaded.data,
        frames=loaded.frames,
        calibration_image=loaded.calibration_image,
        additional_system_events=(recovery_event,),
    )
    if not keep_spool:
        remove_session_spool(loaded.path)
    return RecoveryReport(
        nwbs=reports,
        spool_path=loaded.path,
        acquisition_was_complete=loaded.complete,
        spool_removed=not keep_spool,
    )
