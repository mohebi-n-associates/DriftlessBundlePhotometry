import json
from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest
from rwd_bench import W410, W470, W560, Bench, rwd_config

from driftless_photometry.acquisition import AcquisitionEngine
from driftless_photometry.cli import main
from driftless_photometry.hardware.rwd_timing import RWDExcitationPlan
from driftless_photometry.rig_validation import (
    AcceptanceThresholds,
    EdgeRecords,
    FrameRecords,
    audit_edges,
    audit_frames,
    audit_recording,
    audit_session,
    flash_offsets,
    periodic_train_continuity,
)
from driftless_photometry.settings import export_settings

LENIENT = AcceptanceThresholds(max_invalid_fraction=1.0)


def _record(tmp_path: Path, *, duration_s=3.0, restart_after_frames=None, edges=()):
    config = rwd_config(tmp_path, fiber_count=2)
    bench = Bench(config)
    for line, rising, at_s in edges:
        bench.board.schedule_input_edge(line, rising, bench.clock.now_s + at_s)
    requested = []

    def progress(update):
        if restart_after_frames and not requested and update.frame_count >= restart_after_frames:
            bench.rig.request_restart("test")
            requested.append(True)

    result = AcquisitionEngine().run(config, bench.rig, duration_s=duration_s, on_progress=progress)
    bench.assert_all_off()
    return config, result


def _plan(tmp_path: Path, enabled=(W410, W470, W560)) -> RWDExcitationPlan:
    return RWDExcitationPlan.from_config(rwd_config(tmp_path, enabled))


def _frames(plan: RWDExcitationPlan, runs: list[tuple[float, int]]) -> FrameRecords:
    """Frames on the trigger grid for runs of (start_s, frame_count)."""

    times, wavelengths = [], []
    for start, count in runs:
        for k in range(count):
            times.append(start + k * plan.period_s)
            wavelengths.append(int(plan.wavelength_for_slot(k)))
    n = len(times)
    cameras = [2 if w == 560 else 1 for w in wavelengths]
    t = np.asarray(times)
    return FrameRecords(
        timestamps_s=t,
        sequences=np.arange(n),
        wavelengths_nm=np.asarray(wavelengths),
        camera_indices=np.asarray(cameras),
        camera_timestamps_ns=np.round((5.0 + t * (1 - 24e-6)) * 1e9).astype(np.int64),
        wavelength_sources=("phase_derived",) * n,
    )


def _statuses(checks):
    return {check.name: check.status for check in checks}


def test_a_clean_simulated_rwd_recording_passes_the_audit(tmp_path: Path) -> None:
    edges = [(1, True, 0.4), (1, False, 0.6), (1, True, 1.4), (1, False, 1.5)]
    _config, result = _record(tmp_path, edges=edges)

    assert len({report.path.parent for report in result.reports}) == 2  # one per animal
    session = audit_session(tmp_path)

    assert session["files"] == 2 and session["roi_files_agree"]
    assert session["passed"], json.dumps(session, indent=2)
    audit = session["recordings"][0]
    statuses = {check["name"]: check["status"] for check in audit["checks"]}
    for name in (
        "sequence_contiguous",
        "trigger_grid",
        "gaps_explained",
        "wavelength_cycle",
        "wavelength_source",
        "invalid_fraction",
        "camera_1_clock",
        "camera_2_clock",
        "ttl_alternation",
        "ttl_tick_spacing",
        "camera_delivery",
    ):
        assert statuses[name] == "pass", name
    assert audit["summary"]["runs"] == 1
    assert audit["summary"]["ttl_edges_per_line"] == {"1": 4}


def test_a_requested_restart_is_recorded_explained_and_audited(tmp_path: Path) -> None:
    _config, result = _record(tmp_path, restart_after_frames=20)

    audit = audit_recording(result.reports[0].path, LENIENT)

    assert audit.passed, audit.to_dict()
    assert audit.summary["runs"] == 2
    assert audit.summary["restarts"] == {"requested": 1, "unrequested": {}}
    # A requested restart's invalid span explains its gap but is not held against the
    # invalid-time limit, so the default thresholds still pass.
    assert audit.summary["invalid_s"] == 0
    assert audit.summary["requested_restart_invalid_s"] > 0
    assert _statuses(audit.checks)["gaps_explained"] == "pass"
    assert audit_recording(result.reports[0].path).passed


def test_restart_requests_wait_for_a_recorded_frame_and_do_not_outlive_a_session(
    tmp_path: Path,
) -> None:
    config = rwd_config(tmp_path, (W410, W470))
    bench = Bench(config)
    bench.rig.request_restart("stale")
    packets = bench.run(1.0)  # packets() drops requests made before the session
    assert not [e for p in packets for e in p.system_events if e.event == "rwd_sequencer_restart"]


def test_audit_flags_wrong_wavelengths_unexplained_gaps_and_sequence_breaks(
    tmp_path: Path,
) -> None:
    plan = _plan(tmp_path)
    clean = _frames(plan, [(0.0, 30), (2.0, 30)])
    checks, summary = audit_frames(clean, plan, [(1.4, 2.0)], [], LENIENT)
    assert all(check.status == "pass" for check in checks), checks
    assert summary["runs"] == 2

    checks, _ = audit_frames(clean, plan, [], [], LENIENT)
    assert _statuses(checks)["gaps_explained"] == "fail"
    checks, _ = audit_frames(clean, plan, [], [1.6], LENIENT)
    assert _statuses(checks)["gaps_explained"] == "pass"  # an excitation pause

    swapped = clean.wavelengths_nm.copy()
    swapped[[3, 4]] = swapped[[4, 3]]
    checks, _ = audit_frames(
        replace(clean, wavelengths_nm=swapped), plan, [(1.4, 2.0)], [], LENIENT
    )
    assert _statuses(checks)["wavelength_cycle"] == "fail"

    sequences = clean.sequences.copy()
    sequences[10:] += 1
    checks, _ = audit_frames(replace(clean, sequences=sequences), plan, [(1.4, 2.0)], [], LENIENT)
    assert _statuses(checks)["sequence_contiguous"] == "fail"

    late = clean.camera_timestamps_ns.copy()
    late[7] += 2_000_000  # 2 ms off the camera's fit
    checks, _ = audit_frames(replace(clean, camera_timestamps_ns=late), plan, [], [1.6], LENIENT)
    assert _statuses(checks)[f"camera_{clean.camera_indices[7]}_clock"] == "fail"


def test_audit_counts_invalid_time_once_where_spans_overlap(tmp_path: Path) -> None:
    plan = _plan(tmp_path)
    frames = _frames(plan, [(0.0, 300)])
    spans = [(1.0, 1.5), (1.2, 1.8)]
    _checks, summary = audit_frames(frames, plan, spans, [], LENIENT)
    assert summary["invalid_s"] == pytest.approx(0.8)


def test_periodic_ttl_trains_report_whether_they_kept_phase_across_gaps() -> None:
    period = 0.2
    kept = np.concatenate((np.arange(0, 4, period), np.arange(5.0, 8, period)))
    shifted = np.concatenate((np.arange(0, 4, period), np.arange(5.07, 8, period)))
    rises = np.concatenate((kept, shifted))
    lines = np.concatenate((np.full(len(kept), 2), np.full(len(shifted), 3)))
    edges = EdgeRecords(
        timestamps_s=rises,
        lines=lines,
        rising=np.ones(len(rises), dtype=bool),
        ticks_us=np.round(rises * 1e6).astype(np.int64),
    )

    trains = periodic_train_continuity(edges)

    assert trains[2][1:] == (1, 1) and trains[3][1:] == (0, 1)
    assert trains[2][0] == pytest.approx(period)


def test_audit_flags_repeated_ttl_edges_and_times_off_their_ticks() -> None:
    ticks = np.asarray([1_000_000, 1_200_000, 2_000_000, 2_100_000])
    good = EdgeRecords(
        timestamps_s=ticks / 1e6 - 0.75,
        lines=np.asarray([1, 1, 1, 1]),
        rising=np.asarray([True, False, True, False]),
        ticks_us=ticks,
    )
    checks, summary = audit_edges(good, [], AcceptanceThresholds())
    assert _statuses(checks) == {"ttl_alternation": "pass", "ttl_tick_spacing": "pass"}
    assert summary == {"ttl_edges_per_line": {"1": 4}}

    repeated = replace(good, rising=np.asarray([True, True, True, False]))
    assert _statuses(audit_edges(repeated, [], AcceptanceThresholds())[0])["ttl_alternation"] == (
        "fail"
    )
    # Edges may go unreported while the sequencer is stopped: inside an invalid span it
    # is reported, not failed.
    checks, _ = audit_edges(repeated, [(0.3, 1.0)], AcceptanceThresholds())
    assert _statuses(checks)["ttl_alternation"] == "pass"

    shifted = good.timestamps_s.copy()
    shifted[2] += 0.001
    checks, _ = audit_edges(replace(good, timestamps_s=shifted), [], AcceptanceThresholds())
    assert _statuses(checks)["ttl_tick_spacing"] == "fail"


@pytest.mark.parametrize("true_offset_ms", [0.0, 3.0, -1.5])
def test_flash_offsets_recover_the_ttl_to_exposure_error(true_offset_ms: float) -> None:
    period, exposure = 0.05, 0.045
    times = np.arange(0, 60, period)
    rng = np.random.default_rng(1)
    onsets = 2.0 + np.arange(20) * 2.5 + rng.uniform(0, period, 20)
    lit = np.zeros_like(times)
    for onset in onsets:
        lit += np.clip((times + exposure - onset) / exposure, 0, 1) * (times < onset + 1.0)
    values = 100.0 + 400.0 * lit + rng.normal(0, 0.5, len(times))
    rises = onsets + true_offset_ms / 1e3
    falls = onsets + 1.0 + true_offset_ms / 1e3

    results = flash_offsets(times, values, rises, falls, exposure_s=exposure, wavelength_nm=470)

    measured = [r.offset_ms for r in results if r.offset_ms is not None]
    assert len(measured) >= 15
    assert np.median(measured) == pytest.approx(true_offset_ms, abs=0.3)
    for result in results:
        if result.bound_ms is not None:
            low, high = result.bound_ms
            assert low - 0.5 <= true_offset_ms <= high + 0.5


def test_cli_audits_an_output_folder_and_writes_a_report(tmp_path: Path, capsys) -> None:
    _config, _result = _record(tmp_path / "rec")
    report = tmp_path / "audit.json"

    assert main(["--audit-recording", str(tmp_path / "rec"), "--report", str(report)]) == 0

    printed = json.loads(capsys.readouterr().out)
    assert printed == json.loads(report.read_text(encoding="utf-8"))
    assert printed["passed"] and printed["files"] == 2


def test_cli_audit_rejects_custom_recordings(tmp_path: Path) -> None:
    assert (
        main(["--headless", "--duration", "0.04", "--fibers", "1", "--output", str(tmp_path)]) == 0
    )
    with pytest.raises(ValueError, match="not an RWD recording"):
        main(["--audit-recording", str(tmp_path)])


def test_cli_bench_record_restarts_on_schedule_and_audits(tmp_path: Path) -> None:
    import driftless_photometry.cli as cli

    config = rwd_config(tmp_path / "out", fiber_count=1)
    settings = export_settings(tmp_path / "rwd.json", config)
    bench = Bench(config.model_copy(update={"recording_duration_s": 2.0}))

    args = cli.build_parser().parse_args(
        ["--bench-record", str(settings), "--duration", "2", "--restart-every", "0.5"]
    )
    result = cli._bench_record(args, rig_factory=lambda _config: bench.rig, clock=bench.clock)

    audit = result["recordings"][0]
    assert audit["summary"]["restarts"]["requested"] >= 1
    statuses = {check["name"]: check["status"] for check in audit["checks"]}
    assert statuses["gaps_explained"] == "pass" and statuses["wavelength_cycle"] == "pass"
    (audit_path,) = (tmp_path / "out").glob("rig-audit_*.json")
    assert Path(result["audit_path"]) == audit_path
    assert json.loads(audit_path.read_text(encoding="utf-8")) == result
    assert result["finalization_s"] >= 0 and result["recording_wall_s"] >= 0
    assert result["peak_memory_mb"] > 0
    bench.assert_all_off()


@pytest.mark.parametrize(
    "argv",
    [
        ["--flash-line", "1"],
        ["--restart-every", "5"],
        ["--report", "x.json"],
        ["--bench-record", "s.json", "--restart-every", "0"],
    ],
)
def test_cli_validation_options_need_their_mode(argv) -> None:
    with pytest.raises(SystemExit):
        main(argv)


def test_an_output_folder_with_two_recordings_compares_files_within_each_session(
    tmp_path: Path,
) -> None:
    from datetime import timedelta

    first, _result = _record(tmp_path, duration_s=1.0)
    second = first.model_copy(
        update={
            "session_id": "second-session",
            "session_start_time": first.session_start_time + timedelta(minutes=1),
        }
    )
    AcquisitionEngine().run(second, Bench(second).rig, duration_s=1.5)

    session = audit_session(tmp_path)

    assert session["files"] == 4 and session["roi_files_agree"]
    assert {r["summary"]["session_id"] for r in session["recordings"]} == {
        first.session_id,
        "second-session",
    }
