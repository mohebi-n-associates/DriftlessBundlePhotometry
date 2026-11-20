"""Where a recording's files go: ``<output root>/<animal ID>/<YYYY-MM-DD>/Photometry/``.

The date folder is the animal's day; DBF writes only inside its ``Photometry`` folder,
so other data from that day can sit beside it. That folder holds the animal's ROI NWB
files plus the session's log, settings, and ROI maps. One recording with several animals
writes those three into every animal's folder. Every name carries the recording's start
date and time, so a second recording on the same day adds files instead of replacing
them.

The log, settings, and ROI maps are also inside every ROI's NWB file: the log and settings
as scratch text and the maps as images. The folder copies are for people browsing the
output directory.
"""

from __future__ import annotations

import json
import struct
import zlib
from collections import Counter
from datetime import timedelta
from pathlib import Path

import numpy as np
from numpy.typing import NDArray

from driftless_photometry import __version__
from driftless_photometry.config import ROIConfig, SessionConfig, Wavelength, profile_for
from driftless_photometry.domain import AcquisitionData
from driftless_photometry.roi import (
    auto_contrast_levels,
    render_annotated_rois,
    rois_for_camera,
)

SESSION_LOG_NAME = "session_log.txt"
SETTINGS_NAME = "settings.json"
NWB_SESSION_LOG_SCRATCH_NAME = "driftless_session_log"


PHOTOMETRY_FOLDER_NAME = "Photometry"
"""DBF's folder inside an animal's day, leaving room beside it for other data."""

MISSING_ANIMAL_ID = "NA"
"""The animal ID, and so the folder, of an ROI whose animal was left blank."""


def _filename_safe(value: str) -> str:
    return "".join(c if c.isalnum() or c in "-_." else "_" for c in value).strip("._")


def _safe(value: str) -> str:
    safe = _filename_safe(value)
    if not safe:
        raise ValueError("session_id does not contain filename-safe characters")
    return safe


def animal_folder(config: SessionConfig, roi: ROIConfig) -> Path:
    """``<output root>/<animal ID>/<YYYY-MM-DD>/Photometry``: where this ROI's files go.

    The date is the recording's start in local time, as in the session log.
    """

    return (
        config.output_directory.expanduser().resolve()
        / animal_name(roi)
        / f"{config.session_start_time.astimezone():%Y-%m-%d}"
        / PHOTOMETRY_FOLDER_NAME
    )


def animal_name(roi: ROIConfig) -> str:
    """The ROI's animal ID as used in folder and file names."""

    return _filename_safe(roi.animal_id) or MISSING_ANIMAL_ID


def session_stamp(config: SessionConfig) -> str:
    """``YYYY-MM-DD_HHMMSS``: the recording's local start, which ends every file name.

    The time keeps a second recording of the same animal on the same day from taking
    the first one's names.
    """

    return f"{config.session_start_time.astimezone():%Y-%m-%d_%H%M%S}"


def session_file_name(config: SessionConfig, roi: ROIConfig, name: str) -> str:
    """The log, settings, or ROI-map name in ``roi``'s animal folder."""

    return f"{animal_name(roi)}_{session_stamp(config)}_{name}"


def without_session_folder(config: SessionConfig) -> SessionConfig:
    """The configuration with the operator's output root.

    Before 0.4.4 a recording was written to ``<root>/<session ID>/`` and its spool kept
    that folder as its output directory. Recovering such a spool, or reloading settings
    from its NWB, must use the root rather than nest inside the old folder.
    """

    root = Path(config.output_directory)
    if root.name == _safe(config.session_id):
        return config.model_copy(update={"output_directory": root.parent})
    return config


def roi_maps(
    config: SessionConfig,
    data: AcquisitionData,
    wavelength_images: dict[Wavelength, NDArray[np.uint16]] | None = None,
    *,
    auto_contrast: bool = False,
) -> dict[Wavelength, NDArray[np.uint8]]:
    """Every enabled ROI outlined on each wavelength's mean reference (or first frame).

    Fixed bit-depth scaling by default, as for the NWB diagnostic images; with
    ``auto_contrast``, the live view's Auto contrast stretch. Each circle is named
    "ROI n - label" in its colour; colours follow ROI order (listed in the session log).
    """

    profile = profile_for(config.source)
    maps = {}
    wavelengths = set(data.mean_reference_images) | set(wavelength_images or {})
    for wavelength in sorted(wavelengths, key=int):
        mean = data.mean_reference_images.get(wavelength)
        image = mean.image if mean is not None else (wavelength_images or {})[wavelength]
        rois = rois_for_camera(config.enabled_rois, profile.camera_for(wavelength))
        levels = auto_contrast_levels(image) if auto_contrast else None
        maps[wavelength] = render_annotated_rois(
            image, rois, bit_depth=config.camera.bit_depth, levels=levels, labeled=True
        )
    return maps


def write_png(path: Path, rgb: NDArray[np.uint8]) -> None:
    """Write an RGB uint8 image as a PNG without any imaging dependency."""

    if rgb.dtype != np.uint8 or rgb.ndim != 3 or rgb.shape[2] != 3:
        raise ValueError("PNG images must be height x width x 3 uint8")
    height, width, _ = rgb.shape
    rows = b"".join(b"\x00" + rgb[row].tobytes() for row in range(height))

    def chunk(kind: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload))
            + kind
            + payload
            + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
        )

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    png = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(rows, 6))
        + chunk(b"IEND", b"")
    )
    path.write_bytes(png)


def session_log_text(config: SessionConfig, data: AcquisitionData) -> str:
    """A human-readable account of the recording, built from the recorded data only."""

    start = config.session_start_time

    def clock(seconds: float) -> str:
        wall = (start + timedelta(seconds=seconds)).astimezone()
        return f"{seconds:10.3f} s  {wall:%H:%M:%S}.{wall.microsecond // 1000:03d}"

    lines = [
        "Driftless Bundle Photometry session log",
        "=" * 40,
        f"DBF version:        {__version__}",
        f"Session:            {config.session_id}",
        f"Description:        {config.session_description}",
        f"Experimenter:       {config.experimenter}",
        f"Lab / institution:  {config.lab or '-'} / {config.institution or '-'}",
        f"Started:            {start.astimezone():%Y-%m-%d %H:%M:%S %Z} ({start.isoformat()})",
        f"System:             {profile_for(config.source).display_name}",
        f"Camera:             {config.camera.requested_fps:g} Hz, exposure "
        f"{config.camera.exposure_us / 1000:g} ms, gain {config.camera.gain:g} dB"
        + (
            f" (camera 2: {config.camera.camera2_gain:g} dB)"
            if config.camera.camera2_gain is not None
            else ""
        ),
        "LEDs:               "
        + ", ".join(
            f"{int(c.wavelength_nm)} nm "
            + (f"{c.power_percent:g} %" if c.power_percent is not None else f"{c.voltage_v:g} V")
            for c in config.enabled_channels
        ),
        f"Start / stop:       {config.recording.start} / {config.recording.stop}"
        f" (excitation {config.recording.excitation})",
        "",
        "ROIs (map colour order follows this list)",
    ]
    for index, roi in enumerate(config.enabled_rois, start=1):
        lines.append(
            f"  {index}. {roi.label} ({roi.fiber_id}): animal {roi.animal_id}, "
            f"{roi.brain_region}, {roi.sensor_type}; camera 1 x {roi.center_x_px:.1f} "
            f"y {roi.center_y_px:.1f} r {roi.radius_px:.1f} px"
            + (
                f"; camera 2 x {roi.camera2_center_x_px:.1f} y {roi.camera2_center_y_px:.1f} "
                f"r {roi.camera2_radius_px:.1f} px"
                if roi.has_camera2_geometry
                else ""
            )
        )
    for ttl in config.ttl_inputs:
        owners = ", ".join(ttl.fiber_ids) if ttl.fiber_ids else "all ROIs"
        lines.append(f"  TTL input {ttl.line}: {ttl.label} ({ttl.edges.value} edges; {owners})")

    counts = Counter(data.frame_wavelengths_nm)
    span = (
        f"{data.frame_timestamps_s[0]:.3f}-{data.frame_timestamps_s[-1]:.3f} s"
        if data.frame_timestamps_s
        else "no frames"
    )
    lines += [
        "",
        "Recording",
        f"  Frames: {len(data.frame_ids):,} ({span}); "
        + ", ".join(f"{w} nm {n:,}" for w, n in sorted(counts.items())),
        f"  TTL edges: {len(data.ttl_edges):,} ("
        + ", ".join(
            f"input {line} {n:,}"
            for line, n in sorted(Counter(edge.line for edge in data.ttl_edges).items())
        )
        + ")",
        f"  Dropped-frame events: {len(data.dropped_frames)}; invalid spans: "
        f"{len(data.invalid_times)} totalling "
        f"{sum(i.stop_time_s - i.start_time_s for i in data.invalid_times):.3f} s",
    ]
    for wavelength, mean in sorted(data.mean_reference_images.items(), key=lambda i: int(i[0])):
        low, high = auto_contrast_levels(mean.image)
        lines.append(
            f"  ROI map {int(wavelength)} nm: {mean.describe(int(wavelength))} "
            f"Auto contrast {low:.0f}-{high:.0f} counts (0.5-99.9th percentile)."
        )

    entries = [
        (event.timestamp_s, "event", f"{event.event}: {event.detail}".rstrip(": "))
        for event in data.system_events
    ]
    entries += [
        (
            mark.timestamp_s,
            mark.source,
            mark.name
            + (f" [{mark.fiber_id}]" if mark.fiber_id else "")
            + (f" - {mark.detail}" if mark.detail else "")
            + (f" ({mark.duration_s:g} s)" if mark.duration_s else ""),
        )
        for mark in data.event_marks
    ]
    entries += [
        (
            interval.start_time_s,
            "invalid",
            f"{interval.start_time_s:.3f}-{interval.stop_time_s:.3f} s: {interval.reason}",
        )
        for interval in data.invalid_times
    ]
    entries += [
        (
            drop.timestamp_s,
            "dropped",
            f"{drop.missing_count} frame(s) missing after sequence {drop.expected_sequence - 1}",
        )
        for drop in data.dropped_frames
    ]
    lines += ["", "Timeline (session time, local wall time, kind, detail)"]
    for timestamp_s, kind, detail in sorted(entries, key=lambda e: (e[0], e[1])):
        lines.append(f"  {clock(timestamp_s)}  {kind:<8} {detail}")
    return "\n".join(lines) + "\n"


def write_session_artifacts(
    config: SessionConfig,
    data: AcquisitionData,
    *,
    nwb_paths: tuple[Path, ...] = (),
    wavelength_images: dict[Wavelength, NDArray[np.uint16]] | None = None,
) -> tuple[Path, ...]:
    """Write the log, settings, and ROI maps into every animal's folder; return the folders."""

    from driftless_photometry.settings import settings_document

    root = config.output_directory.expanduser().resolve()
    log = data.session_log or session_log_text(config, data)
    if nwb_paths:
        log += "\nNWB files (under the output folder)\n" + "".join(
            f"  {path.relative_to(root).as_posix() if path.is_relative_to(root) else path}"
            f" ({path.stat().st_size / 1e6:.1f} MB)\n"
            for path in nwb_paths
            if path.exists()
        )
    settings = (
        json.dumps(
            settings_document(without_session_folder(config)),
            indent=2,
            sort_keys=True,
            ensure_ascii=False,
        )
        + "\n"
    )
    maps = roi_maps(config, data, wavelength_images, auto_contrast=True)
    # One ROI per animal folder; ROIs sharing an animal share the folder and these files.
    folders = {animal_folder(config, roi): roi for roi in reversed(config.enabled_rois)}
    for folder, roi in folders.items():
        folder.mkdir(parents=True, exist_ok=True)
        (folder / session_file_name(config, roi, SESSION_LOG_NAME)).write_text(
            log, encoding="utf-8"
        )
        (folder / session_file_name(config, roi, SETTINGS_NAME)).write_text(
            settings, encoding="utf-8"
        )
        for wavelength, rgb in maps.items():
            name = session_file_name(config, roi, f"roi_map_{int(wavelength)}nm.png")
            write_png(folder / name, rgb)
    return tuple(reversed(folders))
