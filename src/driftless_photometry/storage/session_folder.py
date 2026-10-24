"""One folder per recording: its NWB files, a detailed session log, and ROI maps.

Everything here is also inside every ROI's NWB file: the log as scratch text and the ROI
maps as images. The folder copies are for people browsing the output directory.
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
from driftless_photometry.config import SessionConfig, Wavelength, profile_for
from driftless_photometry.domain import AcquisitionData
from driftless_photometry.roi import render_annotated_rois, rois_for_camera

SESSION_LOG_NAME = "session_log.txt"
SETTINGS_NAME = "settings.json"
NWB_SESSION_LOG_SCRATCH_NAME = "driftless_session_log"


def _safe(value: str) -> str:
    safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in value).strip("._")
    if not safe:
        raise ValueError("session_id does not contain filename-safe characters")
    return safe


def with_session_folder(config: SessionConfig) -> SessionConfig:
    """The configuration with its output directory moved into the recording's own folder."""

    root = config.output_directory
    folder_name = _safe(config.session_id)
    if Path(root).name == folder_name:
        return config
    return config.model_copy(update={"output_directory": Path(root) / folder_name})


def without_session_folder(config: SessionConfig) -> SessionConfig:
    """The configuration with the operator's output root, as saved in settings snapshots.

    Reloading settings from an NWB must not nest the next recording inside this one.
    """

    root = Path(config.output_directory)
    if root.name == _safe(config.session_id):
        return config.model_copy(update={"output_directory": root.parent})
    return config


def roi_maps(
    config: SessionConfig,
    data: AcquisitionData,
    wavelength_images: dict[Wavelength, NDArray[np.uint16]] | None = None,
) -> dict[Wavelength, NDArray[np.uint8]]:
    """Every enabled ROI outlined on each wavelength's mean reference (or first frame).

    Fixed bit-depth scaling, as for the NWB diagnostic images; ROI colours follow ROI
    order (listed in the session log).
    """

    profile = profile_for(config.source)
    maps = {}
    wavelengths = set(data.mean_reference_images) | set(wavelength_images or {})
    for wavelength in sorted(wavelengths, key=int):
        mean = data.mean_reference_images.get(wavelength)
        image = mean.image if mean is not None else (wavelength_images or {})[wavelength]
        rois = rois_for_camera(config.enabled_rois, profile.camera_for(wavelength))
        maps[wavelength] = render_annotated_rois(image, rois, bit_depth=config.camera.bit_depth)
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
        lines.append(f"  ROI map {int(wavelength)} nm: {mean.describe(int(wavelength))}")

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
) -> Path:
    """Write the log, settings, and ROI maps into the recording's folder; return it."""

    from driftless_photometry.settings import settings_document

    folder = config.output_directory.expanduser().resolve()
    folder.mkdir(parents=True, exist_ok=True)
    log = data.session_log or session_log_text(config, data)
    if nwb_paths:
        log += "\nNWB files\n" + "".join(
            f"  {path.name} ({path.stat().st_size / 1e6:.1f} MB)\n"
            for path in nwb_paths
            if path.exists()
        )
    (folder / SESSION_LOG_NAME).write_text(log, encoding="utf-8")
    (folder / SETTINGS_NAME).write_text(
        json.dumps(
            settings_document(without_session_folder(config)),
            indent=2,
            sort_keys=True,
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    for wavelength, rgb in roi_maps(config, data, wavelength_images).items():
        write_png(folder / f"roi_map_{int(wavelength)}nm.png", rgb)
    return folder
