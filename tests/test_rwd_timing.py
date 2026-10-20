from pathlib import Path

import pytest

from driftless_photometry.config import SessionConfig, Wavelength, demo_config
from driftless_photometry.hardware.rwd_controller_protocol import (
    RWDExcitationMode,
    RWDFunction,
    RWDInputEdge,
    RWDLed,
)
from driftless_photometry.hardware.rwd_timing import (
    RWDBrightnessCheck,
    RWDCameraFrame,
    RWDContinuityBreak,
    RWDExcitationPlan,
    RWDSegmentTracker,
    RWDSlotAssignment,
    RWDTTLClock,
)

W410, W470, W560 = Wavelength.CONTROL_410, Wavelength.GREEN_470, Wavelength.RED_560


def _config(tmp_path: Path, enabled: tuple[Wavelength, ...] = (W410, W470, W560), **camera):
    payload = demo_config(tmp_path, fiber_count=1, system="rwd").model_dump()
    for channel in payload["channels"]:
        channel["enabled"] = channel["wavelength_nm"] in enabled
    payload["channels"][1]["power_percent"] = 12.5
    payload["camera"].update(camera)
    return SessionConfig.model_validate(payload)


@pytest.mark.parametrize(
    ("enabled", "mode", "cameras"),
    [
        ((W410, W470, W560), RWDExcitationMode.INTERLEAVED_410_470_560, (1, 2)),
        ((W410, W470), RWDExcitationMode.INTERLEAVED_410_470, (1,)),
        ((W470, W560), RWDExcitationMode.INTERLEAVED_470_560, (1, 2)),
        ((W560,), RWDExcitationMode.CONTINUOUS, (2,)),
        ((W410,), RWDExcitationMode.CONTINUOUS, (1,)),
    ],
)
def test_plan_selects_board_mode_cycle_and_cameras(tmp_path: Path, enabled, mode, cameras) -> None:
    plan = RWDExcitationPlan.from_config(_config(tmp_path, enabled))

    assert plan.mode is mode
    assert plan.order == tuple(sorted(enabled))
    assert plan.cameras == cameras
    assert plan.reference_camera == (2 if enabled == (W560,) else 1)


def test_plan_slots_follow_the_board_cycle_per_camera(tmp_path: Path) -> None:
    plan = RWDExcitationPlan.from_config(_config(tmp_path))

    assert [plan.wavelength_for_slot(slot) for slot in range(6)] == [W410, W470, W560] * 2
    assert [plan.camera_for_slot(slot) for slot in range(6)] == [1, 1, 2, 1, 1, 2]
    assert plan.first_slot(1) == 0
    assert plan.first_slot(2) == 2
    assert [plan.next_slot(1, slot) for slot in (0, 1, 3)] == [1, 3, 4]
    assert plan.next_slot(2, 2) == 5
    with pytest.raises(ValueError, match="no enabled LED triggers camera 2"):
        RWDExcitationPlan.from_config(_config(tmp_path, (W410, W470))).first_slot(2)


def test_plan_commands_set_every_led_then_timing_and_mode(tmp_path: Path) -> None:
    plan = RWDExcitationPlan.from_config(
        _config(tmp_path, (W470, W560), requested_fps=25.0, exposure_us=12_340)
    )

    commands = plan.commands()

    functions = [command.function for command in commands]
    assert functions == [RWDFunction.LED_VALUE, RWDFunction.LED_ENABLE] * 3 + [
        RWDFunction.CAMERA_TIMING,
        RWDFunction.EXCITATION_MODE,
    ]
    by_led = {command.data[0]: command.data for command in commands[:6:2]}
    assert by_led[RWDLed.LED_410][2:] == (0).to_bytes(2, "big")
    assert by_led[RWDLed.LED_470][2:] == (1250).to_bytes(2, "big")
    assert by_led[RWDLed.LED_560][2:] == (500).to_bytes(2, "big")
    enables = {command.data[0]: command.data[1] for command in commands[1:6:2]}
    assert enables == {RWDLed.LED_410: 0, RWDLed.LED_470: 1, RWDLed.LED_560: 1}
    assert commands[6].data == (25).to_bytes(2, "big") + (1234).to_bytes(2, "big")
    assert commands[7].data[0] == RWDExcitationMode.INTERLEAVED_470_560


def _frames(plan, slots, *, camera_ns_per_s=1e9, offset_ns=0, host_offset_s=0.013, first_id=7):
    """Synthetic frames for the given slots; returns (camera, frame) in arrival order."""

    period = plan.period_s
    ids: dict[int, int] = {}
    frames = []
    for slot in slots:
        camera = plan.camera_for_slot(slot)
        frame_id = ids.setdefault(camera, first_id)
        ids[camera] = frame_id + 1
        trigger = slot * period
        frames.append(
            RWDCameraFrame(
                camera,
                frame_id,
                offset_ns + round(trigger * camera_ns_per_s),
                trigger + plan.exposure_s + host_offset_s,
            )
        )
    return frames


def test_tracker_assigns_slots_from_each_cameras_first_frame(tmp_path: Path) -> None:
    plan = RWDExcitationPlan.from_config(_config(tmp_path))
    tracker = RWDSegmentTracker(plan, enabled_host_s=0.0)

    results = [tracker.observe(frame) for frame in _frames(plan, range(9))]

    assert results == [RWDSlotAssignment(slot, plan.wavelength_for_slot(slot)) for slot in range(9)]
    assert tracker.start_delay_s == pytest.approx(0.013)


def test_tracker_follows_camera_clock_rate_differences(tmp_path: Path) -> None:
    plan = RWDExcitationPlan.from_config(_config(tmp_path, (W410, W470)))
    tracker = RWDSegmentTracker(plan, enabled_host_s=0.0)

    frames = _frames(plan, range(400), camera_ns_per_s=1e9 * (1 + 80e-6), offset_ns=5_000)
    results = [tracker.observe(frame) for frame in frames]

    assert all(isinstance(result, RWDSlotAssignment) for result in results)
    assert tracker.camera_period_ns(1) == pytest.approx(50e6 * (1 + 80e-6))


@pytest.mark.parametrize(
    ("edit", "message"),
    [
        ("missing", "arrived after a missed exposure"),
        ("id_gap", "frame ID jumped from 9 to 11"),
        ("repeat", "repeated or reordered frame ID 9"),
        ("early", "arrived before its trigger slot"),
        ("jitter", "was off the trigger grid"),
    ],
)
def test_tracker_breaks_the_run_on_any_discontinuity(tmp_path: Path, edit, message) -> None:
    plan = RWDExcitationPlan.from_config(_config(tmp_path, (W410, W470)))
    tracker = RWDSegmentTracker(plan, enabled_host_s=0.0)
    frames = _frames(plan, range(6))
    for frame in frames[:3]:
        assert isinstance(tracker.observe(frame), RWDSlotAssignment)
    nxt = frames[3]
    if edit == "missing":
        nxt = _frames(plan, range(6))[4]
        nxt = RWDCameraFrame(1, 10, nxt.camera_timestamp_ns, nxt.host_received_s)
    elif edit == "id_gap":
        nxt = RWDCameraFrame(1, 11, nxt.camera_timestamp_ns, nxt.host_received_s)
    elif edit == "repeat":
        nxt = RWDCameraFrame(1, 9, nxt.camera_timestamp_ns, nxt.host_received_s)
    elif edit == "early":
        nxt = RWDCameraFrame(1, 10, nxt.camera_timestamp_ns - 40_000_000, nxt.host_received_s)
    else:
        nxt = RWDCameraFrame(1, 10, nxt.camera_timestamp_ns + 15_000_000, nxt.host_received_s)

    result = tracker.observe(nxt)

    assert isinstance(result, RWDContinuityBreak)
    assert not result.fatal
    assert message in result.reason


def test_tracker_rejects_a_camera_that_no_enabled_led_triggers(tmp_path: Path) -> None:
    plan = RWDExcitationPlan.from_config(_config(tmp_path, (W410, W470)))
    tracker = RWDSegmentTracker(plan, enabled_host_s=0.0)

    result = tracker.observe(RWDCameraFrame(2, 0, 0, 0.01))

    assert isinstance(result, RWDContinuityBreak)
    assert "no enabled LED triggers it" in result.reason


def test_tracker_flags_cameras_whose_first_frames_do_not_line_up(tmp_path: Path) -> None:
    plan = RWDExcitationPlan.from_config(_config(tmp_path))
    tracker = RWDSegmentTracker(plan, enabled_host_s=0.0)
    first, second, third, *_rest = _frames(plan, range(6))
    late_camera2 = RWDCameraFrame(2, third.frame_id, third.camera_timestamp_ns, 0.31)

    assert isinstance(tracker.observe(first), RWDSlotAssignment)
    assert isinstance(tracker.observe(second), RWDSlotAssignment)
    result = tracker.observe(late_camera2)

    assert isinstance(result, RWDContinuityBreak)
    assert "does not line up" in result.reason


def test_tracker_fails_when_the_board_period_differs_from_the_command(tmp_path: Path) -> None:
    plan = RWDExcitationPlan.from_config(_config(tmp_path, (W410, W470), requested_fps=30.0))
    tracker = RWDSegmentTracker(plan, enabled_host_s=0.0)
    # Firmware that rounded 30 Hz to a 33 ms period would drift 1 % per trigger.
    frames = _frames(plan, range(40), camera_ns_per_s=1e9 * 0.99)

    results = [tracker.observe(frame) for frame in frames]

    fatal = [result for result in results if isinstance(result, RWDContinuityBreak)]
    assert fatal and fatal[0].fatal
    assert "not the commanded 33.3300 ms" in fatal[0].reason


def _edge(tick_ms: int, line: int = 1, edge: str = "rising") -> RWDInputEdge:
    return RWDInputEdge(line, edge, tick_ms)  # type: ignore[arg-type]


def test_ttl_clock_freezes_one_offset_from_the_earliest_arrivals() -> None:
    clock = RWDTTLClock(exposure_s=0.01, settle_edges=3)
    zero_board_s = 1000.0
    host_minus_board_s = 50.0
    for slot in range(40):
        session_s = slot * 0.05
        latency = 0.01 + 0.003 + (0.002 if slot % 3 else 0.0)
        clock.observe_frame(zero_board_s + session_s + host_minus_board_s + latency, session_s)
    for board_s, latency in ((1000.300, 0.006), (1000.900, 0.002), (1001.200, 0.009)):
        clock.add_edge(_edge(round(board_s * 1000)), board_s + host_minus_board_s + latency)
        if board_s < 1001.2:
            assert clock.release(board_s + host_minus_board_s + latency) == ()

    released = clock.release(1051.3)

    assert clock.offset_s == pytest.approx(zero_board_s + 0.003 - 0.002)
    assert [edge.session_s for edge in released] == pytest.approx([0.299, 0.899, 1.199])
    assert [edge.tick_us for edge in released] == [1_000_300_000, 1_000_900_000, 1_001_200_000]
    clock.add_edge(_edge(1_001_500), 1051.6)
    assert [edge.session_s for edge in clock.release(1051.6)] == pytest.approx([1.499])


def test_ttl_clock_releases_after_its_settle_time_or_at_the_end() -> None:
    clock = RWDTTLClock(exposure_s=0.01, settle_edges=10, settle_s=5.0)
    clock.observe_frame(10.02, 0.0)
    clock.add_edge(_edge(100), 10.05)
    assert clock.release(14.0) == ()
    assert len(clock.release(15.1)) == 1

    unanchored = RWDTTLClock(exposure_s=0.01)
    unanchored.add_edge(_edge(100), 10.05)
    assert unanchored.release(10.1, final=False) == ()
    with pytest.raises(ValueError, match="no camera frame can anchor them"):
        unanchored.release(10.1, final=True)


def test_ttl_clock_unwraps_board_ticks_and_rejects_backwards_ticks() -> None:
    clock = RWDTTLClock(exposure_s=0.01)
    clock.add_edge(_edge((1 << 32) - 2), 1.0)
    clock.add_edge(_edge(3), 1.005)
    clock.observe_frame(1.0, 0.0)
    released = clock.release(2.0, final=True)
    assert released[1].tick_us - released[0].tick_us == 5_000

    with pytest.raises(ValueError, match="out-of-order"):
        clock.add_edge(_edge(1), 2.0)


def test_brightness_check_flags_a_run_that_starts_swapped(tmp_path: Path) -> None:
    plan = RWDExcitationPlan.from_config(_config(tmp_path))
    check = RWDBrightnessCheck(plan)
    check.start_segment(0)
    for index in range(12):
        assert check.observe(W410, 500.0 + index % 2) is None
        assert check.observe(W470, 900.0 + index % 2) is None

    check.start_segment(1)
    assert check.observe(W410, 901.0) is None
    message = check.observe(W470, 500.0)
    assert message is not None and "look swapped" in message

    check.start_segment(2)
    assert check.observe(W410, 500.5) is None
    assert check.observe(W470, 900.5) is None


def test_brightness_check_stays_quiet_when_it_cannot_tell(tmp_path: Path) -> None:
    single = RWDBrightnessCheck(RWDExcitationPlan.from_config(_config(tmp_path, (W470, W560))))
    assert not single.applies

    plan = RWDExcitationPlan.from_config(_config(tmp_path))
    check = RWDBrightnessCheck(plan)
    for index in range(12):
        check.observe(W410, 700.0 + 20 * (index % 2))
        check.observe(W470, 705.0 + 20 * (index % 2))
    check.start_segment(1)
    check.observe(W410, 725.0)
    assert check.observe(W470, 700.0) is None


@pytest.mark.parametrize(
    ("fps", "period_ms"), [(10, 100.0), (15, 66.66), (20, 50.0), (30, 33.33), (250, 4.0)]
)
def test_plan_uses_the_boards_measured_trigger_period(tmp_path: Path, fps, period_ms) -> None:
    plan = RWDExcitationPlan.from_config(
        _config(tmp_path, (W470,), requested_fps=float(fps), exposure_us=1000)
    )

    assert plan.period_s == pytest.approx(period_ms / 1000, abs=1e-12)


def test_tracker_accepts_crystal_offsets_but_not_a_wrong_period_model(tmp_path: Path) -> None:
    plan = RWDExcitationPlan.from_config(_config(tmp_path, (W410, W470), requested_fps=30.0))
    crystal = RWDSegmentTracker(plan, enabled_host_s=0.0)
    frames = _frames(plan, range(60), camera_ns_per_s=1e9 * (1 - 24e-6))
    assert all(isinstance(crystal.observe(frame), RWDSlotAssignment) for frame in frames)

    wrong = RWDSegmentTracker(plan, enabled_host_s=0.0)
    # Timing at exactly 1/30 s would be 1000 ppm off the board's 33.330 ms.
    frames = _frames(plan, range(60), camera_ns_per_s=1e9 * (1 + 1e-3))
    results = [wrong.observe(frame) for frame in frames]
    assert any(isinstance(result, RWDContinuityBreak) and result.fatal for result in results)
