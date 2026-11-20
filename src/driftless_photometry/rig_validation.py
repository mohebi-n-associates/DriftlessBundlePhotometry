"""Read-only acceptance audit of RWD recordings, for bench validation of the rig.

``audit_recording`` checks one finished NWB against the RWD rig's timing contract and
the acceptance thresholds in ``docs/rwd-rig-validation.md``: contiguous exposure
sequences, every run on the board's trigger grid with the LED cycle restarting at its
first wavelength, every gap explained by an invalid span or an excitation pause, the
cameras' hardware clocks consistent with the grid, TTL edges alternating and spaced
exactly as their board ticks, and no lost or incomplete camera frames.

``measure_ttl_flash_offsets`` measures the error of the TTL-to-exposure alignment from
a recording in which one TTL input and a light visible to a fiber are driven by the same
pulses. Nothing here writes to the files it reads.
"""

from __future__ import annotations

import hashlib
import re
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

import numpy as np
from numpy.typing import NDArray
from pynwb import NWBHDF5IO

from driftless_photometry.config import RWD_PROFILE, Wavelength, profile_for
from driftless_photometry.domain import WAVELENGTH_SOURCE_NAMES
from driftless_photometry.hardware.rwd_timing import BENCH_RESTART_REASON, RWDExcitationPlan
from driftless_photometry.settings import configuration_from_nwb

Status = Literal["pass", "fail", "info"]


@dataclass(frozen=True, slots=True)
class AcceptanceThresholds:
    """Pass limits for a bench recording. Proposed 2026-11-15; see the validation doc."""

    max_invalid_fraction: float = 0.001
    """Invalid time over the recording's span; 0.1 %."""
    max_grid_error_us: float = 1.0
    """Within a run, each step between exposures must be one trigger period to this."""
    max_camera_clock_ppm: float = 100.0
    """A camera clock's rate against the board's trigger grid (bench: about -24 ppm)."""
    max_camera_residual_us: float = 500.0
    """A frame's camera timestamp from the camera's linear fit to the grid."""
    max_ttl_tick_error_us: float = 1.0
    """Edge times must differ by exactly their board-tick intervals."""
    max_ttl_offset_ms: float = 2.0
    """The median TTL-to-exposure alignment error measured with a flash."""


@dataclass(frozen=True, slots=True)
class AuditCheck:
    name: str
    status: Status
    detail: str


@dataclass(frozen=True, slots=True)
class RecordingAudit:
    path: Path
    checks: tuple[AuditCheck, ...]
    summary: dict[str, Any] = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        return all(check.status != "fail" for check in self.checks)

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "passed": self.passed,
            "checks": [asdict(check) for check in self.checks],
            "summary": self.summary,
        }


@dataclass(frozen=True, slots=True)
class FrameRecords:
    """The camera_frames columns an audit needs, in recorded order."""

    timestamps_s: NDArray[np.float64]
    sequences: NDArray[np.int64]
    wavelengths_nm: NDArray[np.int64]
    camera_indices: NDArray[np.int64]
    camera_timestamps_ns: NDArray[np.int64]
    wavelength_sources: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class EdgeRecords:
    timestamps_s: NDArray[np.float64]
    lines: NDArray[np.int64]
    rising: NDArray[np.bool_]
    ticks_us: NDArray[np.int64]


@dataclass(frozen=True, slots=True)
class SystemEventRecord:
    timestamp_s: float
    event: str
    detail: str


_PASS, _FAIL, _INFO = "pass", "fail", "info"


def _check(name: str, ok: bool, detail: str) -> AuditCheck:
    return AuditCheck(name, _PASS if ok else _FAIL, detail)


# Frame timing ----------------------------------------------------------------------------
def run_starts(timestamps_s: NDArray[np.float64], period_s: float, tolerance_s: float) -> NDArray:
    """Indices where a new sequencer run begins: the first frame, and after any gap."""

    steps = np.diff(timestamps_s)
    breaks = np.flatnonzero(np.abs(steps - period_s) > tolerance_s) + 1
    return np.concatenate(([0], breaks)).astype(np.int64)


def merged_spans(spans: Iterable[tuple[float, float]], start_s: float, stop_s: float) -> float:
    """Total length of the union of ``spans`` clipped to [start_s, stop_s]."""

    clipped = sorted(
        (max(start_s, a), min(stop_s, b)) for a, b in spans if min(stop_s, b) > max(start_s, a)
    )
    total, current_start, current_stop = 0.0, None, None
    for a, b in clipped:
        if current_stop is None or a > current_stop:
            if current_stop is not None:
                total += current_stop - current_start
            current_start, current_stop = a, b
        else:
            current_stop = max(current_stop, b)
    if current_stop is not None:
        total += current_stop - current_start
    return total


def audit_frames(
    frames: FrameRecords,
    plan: RWDExcitationPlan,
    invalid_spans: Sequence[tuple[float, float]],
    pause_times_s: Sequence[float],
    thresholds: AcceptanceThresholds,
    *,
    requested_spans: Sequence[tuple[float, float]] = (),
) -> tuple[list[AuditCheck], dict[str, Any]]:
    """Check exposure identity and timing; pure, for any arrays in recorded order.

    ``requested_spans`` are invalid spans from restarts requested for bench validation:
    they explain gaps but do not count against the invalid-time limit.
    """

    checks: list[AuditCheck] = []
    summary: dict[str, Any] = {}
    t = frames.timestamps_s
    n = len(t)
    if n == 0:
        return [AuditCheck("frames", _FAIL, "the recording has no camera frames")], summary
    period = plan.period_s
    span_s = float(t[-1] - t[0] + period)
    summary["frames"] = n
    summary["span_s"] = round(span_s, 6)
    summary["period_s"] = period
    values, counts = np.unique(frames.wavelengths_nm, return_counts=True)
    summary["frames_per_wavelength"] = {
        str(int(w)): int(c) for w, c in zip(values, counts, strict=True)
    }

    seq_steps = np.diff(frames.sequences)
    bad_seq = int(np.count_nonzero(seq_steps != 1))
    checks.append(
        _check(
            "sequence_contiguous",
            bad_seq == 0,
            f"{bad_seq} break(s) in the exposure sequence of {n} frames"
            if bad_seq
            else f"{n} exposures numbered {int(frames.sequences[0])}..{int(frames.sequences[-1])}",
        )
    )

    steps = np.diff(t)
    tolerance = thresholds.max_grid_error_us * 1e-6
    short = int(np.count_nonzero(steps < period - tolerance))
    starts = run_starts(t, period, tolerance)
    summary["runs"] = len(starts)
    checks.append(
        _check(
            "trigger_grid",
            short == 0,
            f"{short} step(s) shorter than one trigger period ({period * 1e3:.3f} ms)"
            if short
            else f"{len(starts)} run(s); every step within a run is {period * 1e3:.3f} ms "
            f"to {thresholds.max_grid_error_us:g} us",
        )
    )

    unexplained = []
    for index in starts[1:]:
        gap_start, gap_stop = float(t[index - 1]), float(t[index])
        covered = any(
            a < gap_stop and b > gap_start for a, b in (*invalid_spans, *requested_spans)
        ) or any(gap_start <= p <= gap_stop for p in pause_times_s)
        if not covered:
            unexplained.append((gap_start, gap_stop))
    checks.append(
        _check(
            "gaps_explained",
            not unexplained,
            "every gap between runs is an invalid span or an excitation pause"
            if not unexplained
            else f"{len(unexplained)} gap(s) with no invalid span or pause, first "
            f"{unexplained[0][0]:.4f}-{unexplained[0][1]:.4f} s",
        )
    )

    run_of = np.repeat(np.arange(len(starts)), np.diff(np.concatenate((starts, [n]))))
    position = np.arange(n) - starts[run_of]
    order = np.asarray([int(w) for w in plan.order])
    expected_wavelength = order[position % len(order)]
    wrong_wavelength = int(np.count_nonzero(expected_wavelength != frames.wavelengths_nm))
    camera_of = {int(w): RWD_PROFILE.camera_for(Wavelength(int(w))) for w in plan.order}
    expected_camera = np.asarray([camera_of.get(int(w), -1) for w in frames.wavelengths_nm])
    wrong_camera = int(np.count_nonzero(expected_camera != frames.camera_indices))
    cycle = " -> ".join(str(w) for w in order)
    checks.append(
        _check(
            "wavelength_cycle",
            wrong_wavelength == 0 and wrong_camera == 0,
            f"every run starts at {order[0]} nm and follows {cycle}; cameras match"
            if wrong_wavelength == 0 and wrong_camera == 0
            else f"{wrong_wavelength} frame(s) off the {cycle} cycle, {wrong_camera} from "
            "the wrong camera",
        )
    )

    sources = Counter(frames.wavelength_sources)
    checks.append(
        _check(
            "wavelength_source",
            set(sources) == {"phase_derived"},
            "every exposure is marked phase_derived"
            if set(sources) == {"phase_derived"}
            else f"wavelength sources {dict(sources)}; RWD exposures must be phase_derived",
        )
    )

    invalid_s = merged_spans(invalid_spans, float(t[0]), float(t[-1] + period))
    fraction = invalid_s / span_s if span_s > 0 else 0.0
    summary["invalid_s"] = round(invalid_s, 6)
    summary["invalid_fraction"] = fraction
    if requested_spans:
        summary["requested_restart_invalid_s"] = round(
            merged_spans(requested_spans, float(t[0]), float(t[-1] + period)), 6
        )
    checks.append(
        _check(
            "invalid_fraction",
            fraction <= thresholds.max_invalid_fraction,
            f"{invalid_s:.3f} s invalid of {span_s:.1f} s ({fraction:.4%}; limit "
            f"{thresholds.max_invalid_fraction:.2%})"
            + (
                f"; requested restarts add {summary['requested_restart_invalid_s']:.3f} s"
                if requested_spans
                else ""
            ),
        )
    )

    clocks: dict[str, dict[str, float]] = {}
    for camera in sorted(set(int(c) for c in frames.camera_indices)):
        selected = (frames.camera_indices == camera) & (frames.camera_timestamps_ns >= 0)
        if np.count_nonzero(selected) < 3:
            continue
        x = t[selected]
        y = frames.camera_timestamps_ns[selected].astype(np.float64) / 1e9
        x0, y0 = float(x[0]), float(y[0])
        slope, intercept = np.polyfit(x - x0, y - y0, 1)
        residual = (y - y0) - (slope * (x - x0) + intercept)
        ppm = (slope - 1.0) * 1e6
        worst_us = float(np.max(np.abs(residual)) * 1e6)
        clocks[str(camera)] = {"ppm": round(float(ppm), 3), "max_residual_us": round(worst_us, 3)}
        ok = (
            abs(ppm) <= thresholds.max_camera_clock_ppm
            and worst_us <= thresholds.max_camera_residual_us
        )
        checks.append(
            _check(
                f"camera_{camera}_clock",
                ok,
                f"{ppm:+.2f} ppm against the trigger grid, largest residual {worst_us:.1f} us "
                f"(limits {thresholds.max_camera_clock_ppm:g} ppm, "
                f"{thresholds.max_camera_residual_us:g} us)",
            )
        )
    summary["camera_clocks"] = clocks
    return checks, summary


# TTL edges -------------------------------------------------------------------------------
def audit_edges(
    edges: EdgeRecords,
    invalid_spans: Sequence[tuple[float, float]],
    thresholds: AcceptanceThresholds,
) -> tuple[list[AuditCheck], dict[str, Any]]:
    """Check TTL edge order and board-tick spacing; pure."""

    if len(edges.timestamps_s) == 0:
        return [AuditCheck("ttl_edges", _INFO, "no TTL edges were recorded")], {}
    checks: list[AuditCheck] = []
    per_line: dict[str, int] = {}
    repeated_outside, repeated_inside = 0, 0
    for line in sorted(set(int(v) for v in edges.lines)):
        selected = np.flatnonzero(edges.lines == line)
        selected = selected[np.argsort(edges.timestamps_s[selected], kind="stable")]
        per_line[str(line)] = len(selected)
        rising = edges.rising[selected]
        times = edges.timestamps_s[selected]
        for k in np.flatnonzero(rising[1:] == rising[:-1]) + 1:
            a, b = float(times[k - 1]), float(times[k])
            if any(s <= b and e >= a for s, e in invalid_spans):
                repeated_inside += 1
            else:
                repeated_outside += 1
    detail = f"{len(edges.timestamps_s)} edge(s) on lines {per_line}"
    if repeated_inside:
        detail += f"; {repeated_inside} repeated edge(s) across invalid spans"
    checks.append(
        _check(
            "ttl_alternation",
            repeated_outside == 0,
            detail
            if repeated_outside == 0
            else f"{repeated_outside} repeated rising or falling edge(s) outside invalid "
            f"spans; {detail}",
        )
    )
    trains = periodic_train_continuity(edges)
    if trains:
        checks.append(
            AuditCheck(
                "ttl_train_phase",
                _INFO,
                "; ".join(
                    f"line {line}: a {period * 1e3:.0f} ms pulse train kept its phase across "
                    f"{kept} of {gaps} gap(s)"
                    for line, (period, kept, gaps) in trains.items()
                ),
            )
        )
    offsets = edges.timestamps_s - edges.ticks_us / 1e6
    spread_us = float((np.max(offsets) - np.min(offsets)) * 1e6)
    checks.append(
        _check(
            "ttl_tick_spacing",
            spread_us <= thresholds.max_ttl_tick_error_us,
            f"edge times follow their board ticks to {spread_us:.3f} us (limit "
            f"{thresholds.max_ttl_tick_error_us:g} us)",
        )
    )
    return checks, {"ttl_edges_per_line": per_line}


def periodic_train_continuity(
    edges: EdgeRecords, *, tolerance_s: float = 0.0015
) -> dict[int, tuple[float, int, int]]:
    """For lines carrying a steady pulse train: (period, gaps keeping phase, gaps).

    A gap is a pause of more than 1.5 periods between rising edges, such as a sequencer
    restart while edges go unreported. Board ticks run on, so a train that kept running
    resumes a whole number of periods later; one that paused and restarted does not.
    """

    result: dict[int, tuple[float, int, int]] = {}
    for line in sorted(set(int(v) for v in edges.lines)):
        rises = np.sort(edges.timestamps_s[(edges.lines == line) & edges.rising])
        if len(rises) < 10:
            continue
        intervals = np.diff(rises)
        period = float(np.median(intervals))
        if period <= 0 or np.mean(np.abs(intervals - period) <= tolerance_s) < 0.8:
            continue
        long = intervals[intervals > 1.5 * period]
        if not len(long):
            continue
        cycles = long / period
        kept = int(np.count_nonzero(np.abs(cycles - np.round(cycles)) * period <= tolerance_s))
        result[line] = (period, kept, len(long))
    return result


# System events ---------------------------------------------------------------------------
_DIAGNOSTICS = re.compile(r"(\d+) frame\(s\) delivered, (\d+) incomplete, (\d+) lost")
_REQUESTED = BENCH_RESTART_REASON


def audit_events(
    events: Sequence[SystemEventRecord], dropped_frame_events: int
) -> tuple[list[AuditCheck], dict[str, Any]]:
    checks: list[AuditCheck] = []
    names = Counter(event.event for event in events)
    summary: dict[str, Any] = {"system_events": dict(sorted(names.items()))}

    diagnostics = [e for e in events if e.event == "rwd_camera_diagnostics"]
    match = _DIAGNOSTICS.search(diagnostics[-1].detail) if diagnostics else None
    if match:
        delivered, incomplete, lost = (int(v) for v in match.groups())
        checks.append(
            _check(
                "camera_delivery",
                incomplete == 0 and lost == 0,
                f"{delivered} frame(s) delivered, {incomplete} incomplete, {lost} lost to a "
                "full host queue",
            )
        )
    else:
        checks.append(AuditCheck("camera_delivery", _INFO, "no camera diagnostics recorded"))

    restarts = [e for e in events if e.event == "rwd_sequencer_restart"]
    requested = sum(e.detail.startswith(_REQUESTED) for e in restarts)
    reasons = Counter(
        e.detail.split(";")[0] for e in restarts if not e.detail.startswith(_REQUESTED)
    )
    summary["restarts"] = {"requested": requested, "unrequested": dict(reasons)}
    checks.append(
        AuditCheck(
            "restarts",
            _INFO,
            f"{len(restarts)} sequencer restart(s): {requested} requested, "
            f"{len(restarts) - requested} from detected problems",
        )
    )

    for name, label in (
        ("rwd_brightness_qc_mismatch", "brightness QC mismatches (410/470 may be swapped)"),
        ("rwd_malformed_controller_frame", "malformed controller frames"),
    ):
        count = names.get(name, 0)
        checks.append(_check(name, count == 0, f"{count} {label}"))
    checks.append(
        _check(
            "dropped_frames",
            dropped_frame_events == 0,
            f"{dropped_frame_events} dropped-frame event(s)",
        )
    )
    for name in ("recording_start_trigger", "rwd_controller_connected"):
        for event in events:
            if event.event == name:
                checks.append(AuditCheck(name, _INFO, event.detail))
    return checks, summary


# NWB reading -----------------------------------------------------------------------------
def _column(table: Any, name: str) -> np.ndarray:
    return np.asarray(table[name].data[:])


def _text(values: Iterable[Any]) -> tuple[str, ...]:
    return tuple(v.decode("utf-8") if isinstance(v, bytes) else str(v) for v in values)


def _wavelength_sources(values: np.ndarray) -> tuple[str, ...]:
    """Names from DBF 0.4.1+ codes, or from the text stored by earlier versions."""

    if values.dtype.kind in "iu":
        return tuple(WAVELENGTH_SOURCE_NAMES.get(int(v), f"unknown code {int(v)}") for v in values)
    return _text(values)


def _read_records(nwbfile: Any) -> tuple[FrameRecords, EdgeRecords, list, list, int]:
    table = nwbfile.events["camera_frames"]
    frames = FrameRecords(
        timestamps_s=_column(table, "timestamp").astype(np.float64),
        sequences=_column(table, "controller_sequence").astype(np.int64),
        wavelengths_nm=_column(table, "wavelength_nm").astype(np.int64),
        camera_indices=_column(table, "camera_index").astype(np.int64),
        camera_timestamps_ns=_column(table, "camera_timestamp_ns").astype(np.int64),
        wavelength_sources=_wavelength_sources(_column(table, "wavelength_source")),
    )
    if "ttl_edges" in nwbfile.events:
        ttl = nwbfile.events["ttl_edges"]
        edges = EdgeRecords(
            timestamps_s=_column(ttl, "timestamp").astype(np.float64),
            lines=_column(ttl, "line").astype(np.int64),
            rising=np.asarray([e == "rising" for e in _text(_column(ttl, "edge"))], dtype=bool),
            ticks_us=_column(ttl, "controller_tick_us").astype(np.int64),
        )
    else:
        none = np.empty(0, dtype=np.int64)
        edges = EdgeRecords(np.empty(0), none, np.empty(0, dtype=bool), none)
    system = nwbfile.events["system_events"]
    events = [
        SystemEventRecord(float(t), e, d)
        for t, e, d in zip(
            _column(system, "timestamp"),
            _text(_column(system, "event")),
            _text(_column(system, "detail")),
            strict=True,
        )
    ]
    invalid: list[tuple[float, float, str]] = []
    if nwbfile.intervals is not None and "invalid_times" in nwbfile.intervals:
        spans = nwbfile.intervals["invalid_times"]
        invalid = list(
            zip(
                _column(spans, "start_time").tolist(),
                _column(spans, "stop_time").tolist(),
                _text(_column(spans, "reason")),
                strict=True,
            )
        )
    dropped = 0
    if "dropped_frame_events" in nwbfile.events:
        dropped = len(nwbfile.events["dropped_frame_events"])
    return frames, edges, events, invalid, dropped


def frame_digest(frames: FrameRecords) -> str:
    """A digest of the shared exposure records, equal in every ROI file of a session."""

    digest = hashlib.sha256()
    for array in (
        frames.timestamps_s,
        frames.sequences,
        frames.wavelengths_nm,
        frames.camera_indices,
        frames.camera_timestamps_ns,
    ):
        digest.update(np.ascontiguousarray(array).tobytes())
    return digest.hexdigest()


def _plan_and_session(path: Path) -> tuple[RWDExcitationPlan, str]:
    config, _warnings = configuration_from_nwb(path)
    if profile_for(config.source) is not RWD_PROFILE:
        raise ValueError(f"{path.name} is not an RWD recording; the audit covers RWD only")
    return RWDExcitationPlan.from_config(config), config.session_id


def _plan_for(path: Path) -> RWDExcitationPlan:
    return _plan_and_session(path)[0]


def audit_recording(
    path: str | Path, thresholds: AcceptanceThresholds | None = None
) -> RecordingAudit:
    """Audit one RWD NWB file read-only."""

    path = Path(path)
    thresholds = thresholds or AcceptanceThresholds()
    plan, session_id = _plan_and_session(path)
    with NWBHDF5IO(path, mode="r", load_namespaces=True) as io:
        frames, edges, events, invalid, dropped = _read_records(io.read())
    pauses = [e.timestamp_s for e in events if e.event == "excitation_paused"]
    requested = [(a, b) for a, b, reason in invalid if _REQUESTED in reason]
    unrequested = [(a, b) for a, b, reason in invalid if _REQUESTED not in reason]
    checks, summary = audit_frames(
        frames, plan, unrequested, pauses, thresholds, requested_spans=requested
    )
    edge_checks, edge_summary = audit_edges(
        edges, [(a, b) for a, b, _reason in invalid], thresholds
    )
    event_checks, event_summary = audit_events(events, dropped)
    summary.update(edge_summary)
    summary.update(event_summary)
    summary["frame_digest"] = frame_digest(frames)
    summary["session_id"] = session_id
    return RecordingAudit(path, tuple(checks + edge_checks + event_checks), summary)


def recording_files(path: str | Path | Sequence[Path]) -> tuple[Path, ...]:
    """The finished NWB files at ``path``: the file(s) given, or every one in a folder."""

    if not isinstance(path, str | Path):
        return tuple(Path(file) for file in path)
    path = Path(path)
    if path.is_file():
        return (path,)
    return tuple(sorted(p for p in path.rglob("*.nwb") if ".partial" not in p.name))


def audit_session(
    path: str | Path | Sequence[Path], thresholds: AcceptanceThresholds | None = None
) -> dict[str, Any]:
    """Audit every NWB at ``path`` and check that each session's ROI files agree.

    A folder may hold several recordings (an animal's day, or a whole output root);
    files are compared only with the others of their own session.
    """

    files = recording_files(path)
    if not files:
        raise FileNotFoundError(f"no finished .nwb files at {path}")
    audits = [audit_recording(file, thresholds) for file in files]
    digests: dict[str, set[str]] = {}
    for audit in audits:
        digests.setdefault(audit.summary["session_id"], set()).add(audit.summary["frame_digest"])
    consistent = all(len(found) == 1 for found in digests.values())
    passed = all(a.passed for a in audits) and consistent
    return {
        "passed": passed,
        "files": len(audits),
        "roi_files_agree": consistent,
        "thresholds": asdict(thresholds or AcceptanceThresholds()),
        "recordings": [a.to_dict() for a in audits],
    }


# TTL-to-exposure offset ------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class FlashOffset:
    """One flash onset seen in one wavelength's trace."""

    rise_s: float
    wavelength_nm: int
    offset_ms: float | None
    """TTL edge time minus the light onset estimated from the partly lit exposure."""
    bound_ms: tuple[float, float] | None
    """When no exposure was partly lit: the range the offset must lie in."""


def flash_offsets(
    times_s: NDArray[np.float64],
    values: NDArray[np.floating],
    rises_s: Sequence[float],
    falls_s: Sequence[float],
    *,
    exposure_s: float,
    wavelength_nm: int,
    baseline_s: float = 1.0,
    margin_s: float = 0.005,
    search_s: float = 0.02,
) -> list[FlashOffset]:
    """Estimate when each flash began from one trace, and compare with its TTL edge.

    A frame's session time is its trigger, and it integrates light for ``exposure_s``.
    A flash that starts at ``onset`` inside that window lights a fraction
    ``f = (t + exposure - onset) / exposure`` of it, so ``onset = t + exposure (1 - f)``.
    Each flash must stay on longer than two exposures and stand clear of the noise.
    """

    t = np.asarray(times_s, dtype=np.float64)
    v = np.asarray(values, dtype=np.float64)
    falls = np.sort(np.asarray(falls_s, dtype=np.float64))
    results: list[FlashOffset] = []
    for rise in rises_s:
        later = falls[falls > rise]
        fall = float(later[0]) if len(later) else float(t[-1] + exposure_s)
        dark = (t >= rise - baseline_s) & (t + exposure_s <= rise - margin_s) & np.isfinite(v)
        lit = (t >= rise + exposure_s + margin_s) & (t + exposure_s <= fall - margin_s)
        lit &= np.isfinite(v)
        if np.count_nonzero(dark) < 3 or np.count_nonzero(lit) < 2:
            continue
        base = float(np.median(v[dark]))
        noise = 1.4826 * float(np.median(np.abs(v[dark] - base)))
        step = float(np.median(v[lit])) - base
        if step <= 10 * max(noise, 1e-9):
            continue
        # A frame counts as partly lit once its excess clears five noise widths, so a
        # nearly dark or nearly full frame is never taken for a fully dark or lit one.
        floor = min(0.2, max(1e-3, 5 * noise / step))
        near = np.flatnonzero(
            (t > rise - exposure_s - search_s) & (t < rise + search_s) & np.isfinite(v)
        )
        fractions = (v[near] - base) / step
        partial = near[(fractions > floor) & (fractions < 1 - floor)]
        if len(partial):
            k = int(partial[0])
            f = (v[k] - base) / step
            onset = t[k] + exposure_s * (1.0 - f)
            results.append(FlashOffset(float(rise), wavelength_nm, (rise - onset) * 1e3, None))
            continue
        before = near[fractions <= floor]
        after = near[fractions >= 1 - floor]
        if len(before) and len(after):
            last_dark, first_lit = t[before[-1]], t[after[0]]
            low, high = rise - first_lit, rise - (last_dark + exposure_s)
            results.append(FlashOffset(float(rise), wavelength_nm, None, (low * 1e3, high * 1e3)))
    return results


def measure_ttl_flash_offsets(
    path: str | Path,
    line: int,
    thresholds: AcceptanceThresholds | None = None,
) -> dict[str, Any]:
    """Measure TTL-to-exposure alignment from flashes driven with TTL input ``line``."""

    path = Path(path)
    thresholds = thresholds or AcceptanceThresholds()
    plan = _plan_for(path)
    measurements: list[FlashOffset] = []
    with NWBHDF5IO(path, mode="r", load_namespaces=True) as io:
        nwbfile = io.read()
        _frames, edges, _events, _invalid, _dropped = _read_records(nwbfile)
        on_line = edges.lines == line
        rises = edges.timestamps_s[on_line & edges.rising].tolist()
        falls = edges.timestamps_s[on_line & ~edges.rising].tolist()
        for wavelength in plan.order:
            name = f"raw_fluorescence_{int(wavelength)}"
            if name not in nwbfile.acquisition:
                continue
            series = nwbfile.acquisition[name]
            measurements += flash_offsets(
                np.asarray(series.timestamps[:]),
                np.asarray(series.data[:]).reshape(-1),
                rises,
                falls,
                exposure_s=plan.exposure_s,
                wavelength_nm=int(wavelength),
            )
    offsets = np.asarray([m.offset_ms for m in measurements if m.offset_ms is not None])
    bounded = [m for m in measurements if m.offset_ms is None]
    result: dict[str, Any] = {
        "path": str(path),
        "line": line,
        "rising_edges": len(rises),
        "measured": len(offsets),
        "bounded_only": len(bounded),
        "measurements": [asdict(m) for m in measurements],
    }
    if len(offsets):
        median = float(np.median(offsets))
        result.update(
            median_ms=round(median, 3),
            mad_ms=round(float(np.median(np.abs(offsets - median))), 3),
            min_ms=round(float(np.min(offsets)), 3),
            max_ms=round(float(np.max(offsets)), 3),
            passed=abs(median) <= thresholds.max_ttl_offset_ms,
            limit_ms=thresholds.max_ttl_offset_ms,
        )
    else:
        result["passed"] = None
    return result
