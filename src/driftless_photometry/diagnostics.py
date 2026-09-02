"""Structured acquisition, writer, and finalization diagnostics."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class FinalizationStage(StrEnum):
    """Observable stages in the validated multi-ROI NWB transaction."""

    PREPARING = "preparing"
    WRITING = "writing"
    VALIDATING = "validating"
    PROMOTING = "promoting"
    COMPLETE = "complete"


@dataclass(frozen=True, slots=True)
class SpoolDiagnostics:
    """Snapshot of bounded-queue pressure and committed writer work."""

    queue_depth: int
    queue_capacity: int
    peak_queue_depth: int
    committed_frames: int
    committed_chunks: int
    last_write_latency_s: float
    maximum_write_latency_s: float


@dataclass(frozen=True, slots=True)
class AcquisitionDiagnostics:
    """Acquisition integrity and storage diagnostics at one instant."""

    spool: SpoolDiagnostics
    dropped_frames: int
    clock_residual_s: float
    maximum_absolute_clock_residual_s: float


@dataclass(frozen=True, slots=True)
class FinalizationProgress:
    """Progress through writing, validating, and promoting the ROI file set."""

    stage: FinalizationStage
    completed_roi_files: int
    total_roi_files: int
    fiber_id: str | None = None
