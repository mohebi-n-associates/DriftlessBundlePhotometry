"""Conservative storage estimates and acquisition preflight checks."""

from __future__ import annotations

import math
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from driftless_photometry.config import RWDSourceConfig, SessionConfig

_DEFAULT_RESERVE_BYTES = 64 * 1024 * 1024
_NWB_AND_SPOOL_OVERHEAD_FACTOR = 2.5
_FRAME_METADATA_BYTES = 128
_RWD_SPOOL_BYTES_PER_RECORD = 1024
_RWD_NWB_BYTES_PER_RECORD_PER_FIBER = 768


class InsufficientStorageError(RuntimeError):
    """The output volume does not have enough conservative free space."""


@dataclass(frozen=True, slots=True)
class StorageCapacity:
    """Storage required by a planned acquisition and available on its volume."""

    estimated_frame_count: int
    estimated_session_bytes: int
    required_free_bytes: int
    available_bytes: int
    checked_path: Path


@dataclass(frozen=True, slots=True)
class RWDStorageCapacity:
    """Conservative RWD record-storage requirement and available volume space."""

    estimated_record_count: int
    estimated_session_bytes: int
    required_free_bytes: int
    available_bytes: int
    checked_path: Path


def estimate_session_bytes(config: SessionConfig, duration_s: float) -> tuple[int, int]:
    """Return conservative frame count and spool-plus-NWB byte estimate."""

    if duration_s <= 0:
        raise ValueError("duration_s must be positive")
    frame_count = math.ceil(config.camera.requested_fps * duration_s)
    roi_trace_bytes = len(config.enabled_rois) * 8
    per_frame = _FRAME_METADATA_BYTES + roi_trace_bytes
    roi_file_count = len(config.enabled_rois)
    if config.camera.raw_capture:
        # Raw frames exist once in the spool and once in every independently useful ROI NWB.
        per_frame += config.camera.width_px * config.camera.height_px * 2 * (1 + roi_file_count)
    references = (
        (1 + len(config.enabled_channels))
        * config.camera.width_px
        * config.camera.height_px
        * 2
        * (1 + roi_file_count)
    )
    estimated = math.ceil((frame_count * per_frame + references) * _NWB_AND_SPOOL_OVERHEAD_FACTOR)
    return frame_count, estimated


def check_storage_capacity(
    config: SessionConfig,
    duration_s: float,
    *,
    reserve_bytes: int = _DEFAULT_RESERVE_BYTES,
    disk_usage: Callable[[Path], shutil._ntuple_diskusage] = shutil.disk_usage,
) -> StorageCapacity:
    """Reject acquisition before arming when conservative free space is insufficient."""

    if reserve_bytes < 0:
        raise ValueError("reserve_bytes must be non-negative")
    checked_path = _nearest_existing_parent(config.output_directory)
    frame_count, estimated_bytes = estimate_session_bytes(config, duration_s)
    available_bytes = disk_usage(checked_path).free
    required_bytes = estimated_bytes + reserve_bytes
    capacity = StorageCapacity(
        estimated_frame_count=frame_count,
        estimated_session_bytes=estimated_bytes,
        required_free_bytes=required_bytes,
        available_bytes=available_bytes,
        checked_path=checked_path,
    )
    if available_bytes < required_bytes:
        raise InsufficientStorageError(
            "insufficient storage for acquisition: "
            f"need {required_bytes:,} free bytes including reserve, "
            f"found {available_bytes:,} on {checked_path}"
        )
    return capacity


def estimate_rwd_session_bytes(config: SessionConfig, duration_s: float) -> tuple[int, int]:
    """Return configured worst-case RWD records and spool-plus-NWB bytes."""

    if not isinstance(config.source, RWDSourceConfig):
        raise ValueError("RWD capacity estimate requires an RWD acquisition source")
    if duration_s <= 0:
        raise ValueError("duration_s must be positive")
    record_count = math.ceil(config.source.maximum_expected_record_rate_hz * duration_s)
    per_record = _RWD_SPOOL_BYTES_PER_RECORD + (
        len(config.enabled_rois) * _RWD_NWB_BYTES_PER_RECORD_PER_FIBER
    )
    estimated = math.ceil(record_count * per_record * _NWB_AND_SPOOL_OVERHEAD_FACTOR)
    return record_count, estimated


def check_rwd_storage_capacity(
    config: SessionConfig,
    duration_s: float,
    *,
    reserve_bytes: int = _DEFAULT_RESERVE_BYTES,
    disk_usage: Callable[[Path], shutil._ntuple_diskusage] = shutil.disk_usage,
) -> RWDStorageCapacity:
    """Reject an RWD run before connecting when its configured bound will not fit."""

    if reserve_bytes < 0:
        raise ValueError("reserve_bytes must be non-negative")
    checked_path = _nearest_existing_parent(config.output_directory)
    record_count, estimated_bytes = estimate_rwd_session_bytes(config, duration_s)
    available_bytes = disk_usage(checked_path).free
    required_bytes = estimated_bytes + reserve_bytes
    capacity = RWDStorageCapacity(
        estimated_record_count=record_count,
        estimated_session_bytes=estimated_bytes,
        required_free_bytes=required_bytes,
        available_bytes=available_bytes,
        checked_path=checked_path,
    )
    if available_bytes < required_bytes:
        raise InsufficientStorageError(
            "insufficient storage for RWD acquisition: "
            f"need {required_bytes:,} free bytes including reserve, "
            f"found {available_bytes:,} on {checked_path}"
        )
    return capacity


def _nearest_existing_parent(path: Path) -> Path:
    candidate = path.expanduser().resolve()
    while not candidate.exists():
        parent = candidate.parent
        if parent == candidate:
            raise FileNotFoundError(f"no existing parent for output path: {path}")
        candidate = parent
    if not candidate.is_dir():
        candidate = candidate.parent
    return candidate
