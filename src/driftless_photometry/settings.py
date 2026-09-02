"""Versioned application settings and NWB configuration recovery."""

from __future__ import annotations

import json
import os
import sys
import warnings as python_warnings
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
from pydantic import ValidationError
from pynwb import NWBHDF5IO

from driftless_photometry import __version__
from driftless_photometry.config import (
    CameraConfig,
    ChannelConfig,
    EdgeSelection,
    ROIConfig,
    SessionConfig,
    SignalRole,
    TTLInputConfig,
    Wavelength,
)

SETTINGS_FORMAT_V1 = "driftless_bundle_photometry_settings_v1"
SETTINGS_FORMAT = "driftless_bundle_photometry_settings_v2"
SETTINGS_SUFFIX = ".settings.json"
DEFAULT_SETTINGS_NAME = "default_settings.json"
NWB_SETTINGS_SCRATCH_NAME = "driftless_bundle_photometry_settings_json"

_ABOUT = (
    "Complete Driftless Bundle Photometry settings, including session metadata, "
    "the discriminated native or RWD acquisition source, camera/controller "
    "configuration, excitation channels, display preferences, and every "
    "subject-specific fiber definition."
)


def documents_directory() -> Path | None:
    """Return the real Documents directory, including redirected Windows folders."""

    if sys.platform == "win32":
        try:
            import winreg

            key_path = r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders"
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path) as key:
                raw, _ = winreg.QueryValueEx(key, "Personal")
            redirected = Path(os.path.expandvars(str(raw))).expanduser()
            if redirected.is_dir():
                return redirected
        except (ImportError, OSError, ValueError):
            pass
    candidate = Path.home() / "Documents"
    return candidate if candidate.is_dir() else None


def settings_root() -> Path:
    """Visible operator-owned settings directory, overridable for tests and deployments."""

    override = os.environ.get("DBF_SETTINGS_DIR")
    if override:
        return Path(override).expanduser()
    return (documents_directory() or Path.home()) / "Driftless Bundle Photometry"


def default_settings_path() -> Path:
    override = os.environ.get("DBF_DEFAULT_SETTINGS")
    if override:
        return Path(override).expanduser()
    return settings_root() / DEFAULT_SETTINGS_NAME


def settings_document(config: SessionConfig) -> dict[str, Any]:
    """Build the human-readable, versioned settings document."""

    return {
        "format": SETTINGS_FORMAT,
        "about": _ABOUT,
        "application_version": __version__,
        "exported_utc": datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "settings": config.model_dump(mode="json"),
    }


def settings_json(config: SessionConfig, *, indent: int | None = 2) -> str:
    return json.dumps(
        settings_document(config),
        indent=indent,
        sort_keys=True,
        ensure_ascii=False,
    ) + ("\n" if indent is not None else "")


def _config_from_document(
    document: object,
    source_name: str,
) -> tuple[SessionConfig, list[str]]:
    if not isinstance(document, dict):
        raise ValueError(f"{source_name} does not contain a settings object")
    declared = document.get("format")
    if declared not in {SETTINGS_FORMAT, SETTINGS_FORMAT_V1}:
        raise ValueError(
            f"{source_name} is not a Driftless Bundle Photometry settings file "
            f"(expected format {SETTINGS_FORMAT!r}, found {declared!r})"
        )
    values = document.get("settings")
    if not isinstance(values, dict):
        raise ValueError(f"{source_name} has no 'settings' object")
    warnings: list[str] = []
    if declared == SETTINGS_FORMAT_V1:
        if "source" in values:
            raise ValueError(f"{source_name} declares settings v1 but contains the v2 source field")
        values = dict(values)
        values["source"] = {"kind": "native"}
        warnings.append(
            f"{source_name} used settings v1 and was migrated explicitly to the native "
            "camera/controller source; v1 predates system selection and RWD settings."
        )
    try:
        return SessionConfig.model_validate(values), warnings
    except ValidationError as error:
        raise ValueError(f"{source_name} contains invalid settings: {error}") from error


def export_settings(path: str | Path, config: SessionConfig) -> Path:
    """Atomically write every validated setting as readable JSON."""

    target = Path(path).expanduser()
    if target.suffix.lower() != ".json":
        target = target.with_suffix(".json")
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_name(f".{target.name}.partial")
    try:
        partial.write_text(settings_json(config), encoding="utf-8")
        partial.replace(target)
    except Exception:
        partial.unlink(missing_ok=True)
        raise
    return target


def import_settings(path: str | Path) -> SessionConfig:
    """Load settings, emitting a warning when an older format is migrated."""

    config, migration_warnings = import_settings_with_warnings(path)
    for warning in migration_warnings:
        python_warnings.warn(warning, UserWarning, stacklevel=2)
    return config


def import_settings_with_warnings(path: str | Path) -> tuple[SessionConfig, list[str]]:
    """Load settings and return every explicit migration notice."""

    source = Path(path).expanduser()
    try:
        document = json.loads(source.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise ValueError(f"{source.name} is not valid JSON: {error}") from error
    return _config_from_document(document, source.name)


def save_default_settings(config: SessionConfig) -> Path:
    return export_settings(default_settings_path(), config)


def load_default_settings() -> SessionConfig | None:
    path = default_settings_path()
    return import_settings(path) if path.is_file() else None


def load_default_settings_with_warnings() -> tuple[SessionConfig | None, list[str]]:
    path = default_settings_path()
    if not path.is_file():
        return None, []
    return import_settings_with_warnings(path)


def configuration_from_nwb(path: str | Path) -> tuple[SessionConfig, list[str]]:
    """Restore exact embedded settings, or recover available fields from legacy DBF NWBs."""

    source = Path(path).expanduser().resolve()
    try:
        with NWBHDF5IO(source, mode="r", load_namespaces=True) as io:
            nwbfile = io.read()
            if NWB_SETTINGS_SCRATCH_NAME in nwbfile.scratch:
                raw = nwbfile.get_scratch(NWB_SETTINGS_SCRATCH_NAME)
                if isinstance(raw, bytes):
                    raw = raw.decode("utf-8")
                if isinstance(raw, np.ndarray) and raw.shape == ():
                    raw = raw.item()
                try:
                    document = json.loads(str(raw))
                except json.JSONDecodeError as error:
                    raise ValueError(
                        f"{source.name} has a malformed embedded settings snapshot"
                    ) from error
                return _config_from_document(document, source.name)
        return _legacy_configuration_from_nwbs(source)
    except (OSError, ValueError):
        raise
    except Exception as error:
        raise ValueError(f"{source.name} could not be read as a DBF NWB file: {error}") from error


def _legacy_configuration_from_nwbs(source: Path) -> tuple[SessionConfig, list[str]]:
    """Best-effort import for DBF NWBs written before exact snapshots were embedded."""

    stem_parts = source.stem.rsplit("__", 2)
    prefix = stem_parts[0] if len(stem_parts) == 3 else source.stem
    candidates = sorted(source.parent.glob(f"{prefix}__*.nwb"))
    if source not in candidates:
        candidates.append(source)

    rois: list[ROIConfig] = []
    base_session_id: str | None = None
    shared: dict[str, Any] | None = None
    channels: tuple[ChannelConfig, ...] = ()
    camera: CameraConfig | None = None
    ttl_inputs: tuple[TTLInputConfig, ...] = ()
    duration_s = 5.0

    for candidate in candidates:
        with NWBHDF5IO(candidate, mode="r", load_namespaces=True) as io:
            nwbfile = io.read()
            try:
                roi_table = nwbfile.processing["photometry"]["camera_rois"].to_dataframe()
                photometry_table = nwbfile.lab_meta_data[
                    "FiberPhotometry"
                ].fiber_photometry_table.to_dataframe()
            except KeyError as error:
                if candidate == source:
                    raise ValueError(f"{source.name} is not a compatible DBF NWB file") from error
                continue
            if len(roi_table) != 1 or photometry_table.empty or nwbfile.subject is None:
                if candidate == source:
                    raise ValueError(f"{source.name} has incomplete legacy DBF metadata")
                continue
            roi_row = roi_table.iloc[0]
            fiber_id = str(roi_row["fiber_id"])
            candidate_session = str(nwbfile.session_id or nwbfile.identifier)
            candidate_base = (
                candidate_session[: -(len(fiber_id) + 2)]
                if candidate_session.endswith(f"__{fiber_id}")
                else candidate_session
            )
            if base_session_id is None:
                base_session_id = candidate_base
            if candidate_base != base_session_id:
                continue

            first_path = photometry_table.iloc[0]
            indicator = first_path["indicator"]
            subject = nwbfile.subject
            rois.append(
                ROIConfig(
                    fiber_id=fiber_id,
                    label=str(roi_row["label"]),
                    animal_id=str(subject.subject_id),
                    brain_region=str(first_path["location"]),
                    sensor_type=str(indicator.label),
                    subject_age=str(subject.age) if subject.age else None,
                    subject_sex=str(subject.sex or "U"),
                    center_x_px=float(roi_row["center_x_px"]),
                    center_y_px=float(roi_row["center_y_px"]),
                    radius_px=float(roi_row["radius_px"]),
                )
            )
            if candidate != source and shared is not None:
                continue

            shared = {
                "session_description": str(nwbfile.session_description),
                "experimenter": str((nwbfile.experimenter or ("not specified",))[0]),
                "session_start_time": nwbfile.session_start_time,
                "lab": nwbfile.lab,
                "institution": nwbfile.institution,
            }
            channels = _legacy_channels(photometry_table)
            camera = _legacy_camera(nwbfile, photometry_table)
            ttl_inputs = _legacy_ttl_inputs(nwbfile)
            duration_s = _legacy_duration(nwbfile)

    if not rois or shared is None or camera is None or not channels or base_session_id is None:
        raise ValueError(f"{source.name} has insufficient legacy DBF metadata to restore settings")
    rois.sort(key=lambda roi: roi.fiber_id)
    config = SessionConfig(
        session_id=base_session_id,
        session_description=shared["session_description"],
        experimenter=shared["experimenter"],
        output_directory=source.parent,
        recording_duration_s=duration_s,
        session_start_time=shared["session_start_time"],
        camera=camera,
        rois=tuple(rois),
        channels=channels,
        ttl_inputs=ttl_inputs,
        lab=shared["lab"],
        institution=shared["institution"],
    )
    warnings = [
        "This legacy NWB predates exact settings snapshots. Available metadata and all "
        f"{len(rois)} sibling ROI files were restored; display preferences, disabled "
        "channel settings, unobserved TTL inputs, and unavailable camera fields use defaults."
    ]
    return config, warnings


def _legacy_channels(table: Any) -> tuple[ChannelConfig, ...]:
    channels: list[ChannelConfig] = []
    for _, row in table.iterrows():
        wavelength = Wavelength(round(float(row["excitation_wavelength_in_nm"])))
        command = row["commanded_voltage_series"]
        channels.append(
            ChannelConfig(
                wavelength_nm=wavelength,
                enabled=True,
                voltage_v=float(np.asarray(command.data)[0]),
                role=SignalRole(str(row["signal_role"])),
                emission_wavelength_nm=float(row["emission_wavelength_in_nm"]),
            )
        )
    return tuple(sorted(channels, key=lambda channel: int(channel.wavelength_nm)))


def _legacy_camera(nwbfile: Any, table: Any) -> CameraConfig:
    raw_capture = "camera_frames" in nwbfile.acquisition
    if raw_capture:
        height, width = nwbfile.acquisition["camera_frames"].data.shape[-2:]
    else:
        images = nwbfile.processing["photometry"].get("wavelength_roi_images")
        if images is not None:
            raw_names = sorted(name for name in images.images if name.startswith("raw_reference_"))
            height, width = images.images[raw_names[0]].data.shape[:2]
        else:
            height = width = 256
    first = table.iloc[0]
    detector = first["photodetector"]
    serial = str(detector.serial_number or "")
    exposure_us = round(float(first["excitation_source"].exposure_time_in_s) * 1e6)
    timestamps = np.asarray(nwbfile.events["camera_frames"]["timestamp"].data, dtype=np.float64)
    differences = np.diff(timestamps)
    positive = differences[differences > 0]
    requested_fps = float(1.0 / np.median(positive)) if len(positive) else 30.0
    return CameraConfig(
        model_name=str(getattr(detector.model, "model_number", "Thorlabs CS505MU")),
        serial_number=None if serial in {"", "not supplied"} else serial,
        width_px=int(width),
        height_px=int(height),
        bit_depth=12,
        exposure_us=exposure_us,
        gain=float(getattr(detector.model, "gain", 0.0)),
        requested_fps=requested_fps,
        raw_capture=raw_capture,
    )


def _legacy_ttl_inputs(nwbfile: Any) -> tuple[TTLInputConfig, ...]:
    if "ttl_edges" not in nwbfile.events:
        return ()
    frame = nwbfile.events["ttl_edges"].to_dataframe()
    inputs: list[TTLInputConfig] = []
    for line, rows in frame.groupby("line"):
        observed = set(str(value) for value in rows["edge"])
        edges = (
            EdgeSelection.BOTH
            if observed == {"rising", "falling"}
            else EdgeSelection.RISING
            if "rising" in observed
            else EdgeSelection.FALLING
        )
        inputs.append(TTLInputConfig(line=int(line), label=str(rows.iloc[0]["label"]), edges=edges))
    return tuple(sorted(inputs, key=lambda item: item.line))


def _legacy_duration(nwbfile: Any) -> float:
    timestamps = np.asarray(nwbfile.events["camera_frames"]["timestamp"].data, dtype=np.float64)
    return max(0.1, float(timestamps[-1])) if len(timestamps) else 5.0
