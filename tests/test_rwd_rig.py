import threading
from collections import Counter
from pathlib import Path

import numpy as np
import pytest
from rwd_bench import START_DELAY_S, W410, W470, W560, Bench, events_of, frames_of, invalid_of
from rwd_bench import rwd_config as _config

from driftless_photometry.config import (
    RWDSourceConfig,
    RWDTTLOutputConfig,
    SessionConfig,
    TTLInputConfig,
    demo_config,
)
from driftless_photometry.faults import AcquisitionFault, AcquisitionFaultCode
from driftless_photometry.hardware import ExcitationCycle, StartCondition
from driftless_photometry.hardware.rwd_controller_protocol import (
    RWDFunction,
    RWDLed,
    RWDMessage,
    RWDMessageType,
    encode_rwd_message,
)
from driftless_photometry.hardware.rwd_rig import RWDRig, RWDRigTiming


def test_rwd_rig_records_an_interleaved_three_led_session(tmp_path: Path) -> None:
    bench = Bench(_config(tmp_path))

    packets = bench.run(2.0)

    frames = frames_of(packets)
    assert len(frames) == 40
    exposures = [frame.exposure for frame in frames]
    assert [e.sequence for e in exposures] == list(range(40))
    assert [e.wavelength_nm for e in exposures] == [W410, W470, W560] * 13 + [W410]
    assert [e.camera_index for e in exposures] == [1, 1, 2] * 13 + [1]
    np.testing.assert_allclose([e.timestamp_s for e in exposures], np.arange(40) / 20)
    assert [e.controller_tick_us for e in exposures] == [k * 50_000 for k in range(40)]
    assert {e.wavelength_source for e in exposures} == {"phase_derived"}
    assert all(e.commanded_voltage_v is None for e in exposures)
    assert Counter(e.commanded_power_percent for e in exposures) == {5.0: 27, 12.5: 13}
    camera1 = [frame.frame_id for frame in frames if frame.exposure.camera_index == 1]
    assert camera1 == list(range(len(camera1)))
    assert all(frame.image.shape == (544, 728) for frame in frames)
    assert all(frame.camera_timestamp_ns is not None for frame in frames)
    assert np.all(np.diff([frame.host_received_s for frame in frames]) > 0)
    events = [event.event for event in events_of(packets)]
    assert events[:3] == [
        "rwd_controller_connected",
        "rwd_start_delay_measured",
        "rwd_sequencer_started",
    ]
    assert "rwd_sequencer_stopped" in events
    assert not invalid_of(packets)
    assert bench.camera_ids == ("1", "2")
    bench.assert_all_off()


def test_rwd_rig_sends_board_settings_before_starting_the_sequencer(tmp_path: Path) -> None:
    bench = Bench(_config(tmp_path, (W470, W560), requested_fps=25.0, exposure_us=8_000))

    bench.run(0.4)

    commands = [
        message
        for message in bench.board.received
        if message.message_type is RWDMessageType.COMMAND
    ]
    first_enable = next(
        index
        for index, message in enumerate(commands)
        if message.function is RWDFunction.DEVICE_ENABLE and message.data[0] == 1
    )
    before = {message.function for message in commands[:first_enable]}
    assert {
        RWDFunction.LED_VALUE,
        RWDFunction.LED_ENABLE,
        RWDFunction.CAMERA_TIMING,
        RWDFunction.EXCITATION_MODE,
    } <= before
    run = bench.board.runs[0]
    assert run.frame_rate_hz == 25
    assert run.exposure_units == 800
    assert run.excitation_mode == 2
    assert run.led_enabled == {RWDLed.LED_410: False, RWDLed.LED_470: True, RWDLed.LED_560: True}
    assert run.led_value[RWDLed.LED_470] == 1250


@pytest.mark.parametrize(
    ("enabled", "wavelengths", "cameras", "user_ids"),
    [
        ((W410, W470), [W410, W470], [1, 1], ("1",)),
        ((W470, W560), [W470, W560], [1, 2], ("1", "2")),
        ((W560,), [W560], [2], ("2",)),
        ((W410,), [W410], [1], ("1",)),
    ],
)
def test_rwd_rig_runs_every_board_mode(tmp_path: Path, enabled, wavelengths, cameras, user_ids):
    bench = Bench(_config(tmp_path, enabled))

    frames = frames_of(bench.run(1.0))

    assert len(frames) == 20
    cycle = len(wavelengths)
    assert [f.exposure.wavelength_nm for f in frames] == wavelengths * (20 // cycle)
    assert [f.exposure.camera_index for f in frames] == cameras * (20 // cycle)
    assert bench.camera_ids == user_ids
    bench.assert_all_off()


def test_rwd_rig_places_ttl_edges_on_the_trigger_grid(tmp_path: Path) -> None:
    payload = _config(tmp_path).model_dump()
    payload["ttl_inputs"] = [
        TTLInputConfig(line=1, label="lick").model_dump(),
        TTLInputConfig(line=2, label="cue", edges="rising").model_dump(),
    ]
    bench = Bench(SessionConfig.model_validate(payload))
    start = bench.clock.now_s
    schedule = [
        (1, True, 0.3101),
        (1, False, 0.3602),
        (2, True, 0.9),
        (2, False, 0.95),
        (3, True, 1.2),
        (1, True, 1.5004),
        (1, False, 1.6509),
    ]
    for line, rising, offset in schedule:
        bench.board.schedule_input_edge(line, rising, start + offset)

    packets = bench.run(2.5)

    edges = [edge for packet in packets for edge in packet.ttl_edges]
    assert [(edge.line, edge.edge, edge.label) for edge in edges] == [
        (1, "rising", "lick"),
        (1, "falling", "lick"),
        (2, "rising", "cue"),
        (1, "rising", "lick"),
        (1, "falling", "lick"),
    ]
    assert [edge.sequence for edge in edges] == list(range(5))
    truth = [bench.session_s(start + offset) for line, _r, offset in schedule if line == 1]
    lick = [edge.timestamp_s for edge in edges if edge.line == 1]
    # One frozen offset: errors stay within the readout/serial latency floors and 1 ms ticks.
    np.testing.assert_allclose(lick, truth, atol=0.005)
    ticks = [edge.controller_tick_us for edge in edges if edge.line == 1]
    np.testing.assert_allclose(np.diff(lick), np.diff(ticks) / 1e6, atol=1e-9)
    assert ticks[0] == bench.board.tick_ms(start + 0.3101) * 1000
    events = {event.event: event.detail for event in events_of(packets)}
    assert "rwd_ttl_clock_aligned" in events
    assert events["rwd_ttl_edges_not_configured"].startswith("1 edge")


def test_rwd_rig_restarts_after_a_missed_exposure_and_marks_the_span(tmp_path: Path) -> None:
    bench = Bench(_config(tmp_path))
    bench.pending_setup = lambda cams: cams.missed.add(("2", 0, 8))

    packets = bench.run(2.0)

    frames = frames_of(packets)
    exposures = [frame.exposure for frame in frames]
    assert [e.sequence for e in exposures] == list(range(len(exposures)))
    run0 = exposures[:8]
    assert [e.wavelength_nm for e in run0] == [W410, W470, W560] * 2 + [W410, W470]
    run1 = exposures[8:]
    assert [e.wavelength_nm for e in run1[:6]] == [W410, W470, W560] * 2
    second_run = bench.board.runs[1]
    expected_start = bench.session_s(second_run.start_s + START_DELAY_S)
    assert run1[0].timestamp_s == pytest.approx(expected_start, abs=1e-4)
    np.testing.assert_allclose(np.diff([e.timestamp_s for e in run1]), 0.05, atol=1e-9)
    assert exposures[-1].timestamp_s < 2.0
    (interval,) = invalid_of(packets)
    assert interval.start_time_s == pytest.approx(0.40)
    assert interval.stop_time_s == pytest.approx(run1[0].timestamp_s)
    assert interval.source_event == "rwd_sequencer_restart"
    assert "missed exposure" in interval.reason
    restart = next(e for e in events_of(packets) if e.event == "rwd_sequencer_restart")
    assert "could not be attributed" in restart.detail
    assert restart.timestamp_s == pytest.approx(0.40)
    bench.assert_all_off()


def test_rwd_rig_restarts_after_a_frame_lost_in_transfer(tmp_path: Path) -> None:
    bench = Bench(_config(tmp_path, (W410, W470)))
    bench.pending_setup = lambda cams: cams.lost.add(("1", 0, 5))

    packets = bench.run(1.5)

    (interval,) = invalid_of(packets)
    assert interval.start_time_s == pytest.approx(0.25)
    assert "frame ID jumped" in interval.reason
    frames = frames_of(packets)
    assert frames[5].exposure.wavelength_nm is W410
    assert frames[5].exposure.timestamp_s > 0.25 + 0.15


def test_rwd_rig_retries_a_restart_whose_first_trigger_was_missed(tmp_path: Path) -> None:
    bench = Bench(_config(tmp_path, (W410, W470)))

    def setup(cams):
        cams.missed.add(("1", 0, 6))  # forces run 1
        cams.missed.add(("1", 1, 0))  # run 1 would start on 470 nm

    bench.pending_setup = setup

    packets = bench.run(2.0)

    assert len(bench.board.runs) == 3
    events = events_of(packets)
    reasons = [event.detail for event in events if event.event == "rwd_sequencer_restart"]
    assert len(reasons) == 2
    assert "first trigger may have been missed" in reasons[1]
    frames = frames_of(packets)
    third = bench.session_s(bench.board.runs[2].start_s + START_DELAY_S)
    after = [f.exposure for f in frames if f.exposure.timestamp_s >= third - 1e-6]
    assert after[0].wavelength_nm is W410
    assert after[0].timestamp_s == pytest.approx(third, abs=1e-4)
    # Nothing from the misattributable run 1 was recorded.
    assert all(
        f.exposure.timestamp_s < 0.30 or f.exposure.timestamp_s >= third - 1e-6 for f in frames
    )
    (interval,) = invalid_of(packets)
    assert interval.stop_time_s == pytest.approx(after[0].timestamp_s)


def test_rwd_rig_fails_when_the_board_period_is_not_the_commanded_rate(tmp_path: Path) -> None:
    bench = Bench(
        _config(tmp_path, (W410, W470), requested_fps=30.0),
        trigger_period=lambda fps: (1000 // fps) / 1000,
    )

    with pytest.raises(AcquisitionFault, match="not the commanded") as raised:
        bench.run(3.0)

    assert raised.value.code is AcquisitionFaultCode.CLOCK_DISCONTINUITY
    bench.assert_all_off()
    assert bench.cameras is not None and not bench.cameras.open


def test_rwd_rig_fails_after_repeated_restarts_withoutframes_of(tmp_path: Path) -> None:
    bench = Bench(_config(tmp_path))
    bench.pending_setup = lambda cams: setattr(cams, "disconnect_at_s", bench.clock.now_s + 0.5)

    with pytest.raises(AcquisitionFault, match="could not attribute frames") as raised:
        bench.run(10.0)

    assert raised.value.code is AcquisitionFaultCode.CAMERA_DISCONNECT
    assert len(bench.board.runs) == 1 + RWDRigTiming().restart_limit
    bench.assert_all_off()


def test_rwd_rig_controller_loss_closes_the_cameras(tmp_path: Path) -> None:
    bench = Bench(_config(tmp_path))
    packets = bench.rig.packets(5.0)
    for _index in range(5):
        next(packets)
    bench.board.failed = True

    with pytest.raises(AcquisitionFault) as raised:
        list(packets)

    assert raised.value.code is AcquisitionFaultCode.CONTROLLER_DISCONNECT
    assert bench.cameras is not None and not bench.cameras.open


def test_rwd_rig_stop_request_ends_with_the_board_off(tmp_path: Path) -> None:
    bench = Bench(_config(tmp_path))
    received = []
    for packet in bench.rig.packets(30.0):
        received.append(packet)
        if len(frames_of(received)) == 7:
            bench.rig.stop()

    frames = frames_of(received)
    assert len(frames) == 7
    assert received[-1].frame is None
    events = [event.event for event in events_of(received)]
    assert "rwd_stop_requested" in events
    assert "rwd_sequencer_stopped" in events
    # Frames that may have been cut short by the stop are never recorded.
    fiber = frames[0].image > 200
    for wavelength in (W410, W470):
        means = [f.image[fiber].mean() for f in frames if f.exposure.wavelength_nm is wavelength]
        assert max(means) - min(means) < 1.0
    bench.assert_all_off()


def test_rwd_rig_turns_the_board_off_when_the_consumer_abandons_it(tmp_path: Path) -> None:
    bench = Bench(_config(tmp_path))
    packets = bench.rig.packets(30.0)
    next(packets)
    next(packets)
    assert bench.board.state.light_on

    packets.close()

    bench.assert_all_off()
    assert bench.cameras is not None and not bench.cameras.open


def test_rwd_rig_records_malformed_controllerframes_of(tmp_path: Path) -> None:
    bench = Bench(_config(tmp_path))
    packets = bench.rig.packets(1.0)
    received = [next(packets) for _index in range(4)]
    frame = bytearray(
        encode_rwd_message(RWDMessage(RWDMessageType.REPLY, 9, RWDFunction.INPUT_EDGE, bytes(6)))
    )
    frame[-3] ^= 0xFF
    bench.board.inject_raw(bytes(frame))
    received.extend(packets)

    (interval,) = invalid_of(received)
    assert "a TTL edge may have been lost" in interval.reason
    assert "rwd_malformed_controller_frame" in [event.event for event in events_of(received)]
    assert len(frames_of(received)) == 20


def test_rwd_rig_refuses_models_other_than_the_r821(tmp_path: Path) -> None:
    bench = Bench(_config(tmp_path), model_code=1)

    with pytest.raises(AcquisitionFault, match="drives only the bench-validated R821"):
        bench.run(1.0)

    assert bench.cameras is None
    assert not bench.board.is_open


def test_rwd_rig_port_selection(tmp_path: Path) -> None:
    with pytest.raises(AcquisitionFault, match="no FTDI serial port"):
        Bench(_config(tmp_path), ports=()).run(1.0)
    with pytest.raises(AcquisitionFault, match="choose the RWD controller's port"):
        Bench(_config(tmp_path), ports=("COM3", "COM4")).run(1.0)

    payload = _config(tmp_path).model_dump()
    payload["source"] = RWDSourceConfig(controller_port="COM7").model_dump()
    bench = Bench(SessionConfig.model_validate(payload), ports=("COM3", "COM4"))
    bench.run(0.2)
    assert bench.opened_ports == ["COM7"]
    assert bench.rig.port == "COM7"


def test_rwd_rig_needs_rwd_settings(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="needs an RWD session configuration"):
        RWDRig(demo_config(tmp_path))


def test_rwd_rig_drives_configured_ttl_outputs_until_the_end(tmp_path: Path) -> None:
    payload = _config(tmp_path).model_dump()
    payload["source"] = RWDSourceConfig(
        ttl_outputs=(RWDTTLOutputConfig(port=3, frequency_hz=5.0, pulse_width_ms=50, label="cue"),)
    ).model_dump()
    bench = Bench(SessionConfig.model_validate(payload))
    packets = bench.rig.packets(1.0)
    first = next(packets)

    assert bench.board.state.output_enabled == {1: False, 2: False, 3: True, 4: False}
    assert bench.board.state.output_config[3] == (500).to_bytes(2, "big") + (50).to_bytes(2, "big")
    rest = list(packets)
    events = {event.event: event.detail for event in events_of([first, *rest])}
    assert events["rwd_ttl_outputs_started"] == "output 3: 5 Hz, 50 ms pulses (cue)"
    bench.assert_all_off()


def test_rwd_rig_handles_retransmitted_and_out_of_run_ttl_edges(tmp_path: Path) -> None:
    bench = Bench(_config(tmp_path))
    bench.board.edge_retransmissions = 2
    bench.board.report_edges_while_stopped = True
    start = bench.clock.now_s
    bench.board.schedule_input_edge(1, True, start - 0.5)
    bench.pending_setup = lambda cams: cams.missed.add(("2", 0, 8))
    for offset in (0.2, 0.25):
        bench.board.schedule_input_edge(1, offset == 0.2, start + offset)

    packets = bench.run(2.0)

    edges = [edge for packet in packets for edge in packet.ttl_edges]
    assert [edge.edge for edge in edges] == ["rising", "falling"]
    events = {event.event: event.detail for event in events_of(packets)}
    assert events["rwd_ttl_edges_before_recording"].startswith("1 edge")
    assert "repeated controller message" in events["rwd_controller_retransmissions"]
    assert "may not be reported" in events["rwd_sequencer_restart"]


def test_rwd_rig_does_not_restart_when_the_host_falls_behind(tmp_path: Path) -> None:
    bench = Bench(_config(tmp_path, requested_fps=50.0))
    received = []
    for packet in bench.rig.packets(3.0):
        received.append(packet)
        if len(frames_of(received)) in (20, 60):
            # The host stalls; frames wait in the camera queue meanwhile.
            bench.clock.advance_to(bench.clock.now_s + 0.8)

    frames = frames_of(received)
    assert len(frames) == 150
    assert [f.exposure.sequence for f in frames] == list(range(150))
    assert "rwd_sequencer_restart" not in [event.event for event in events_of(received)]
    assert not invalid_of(received)


def test_rwd_rig_times_non_divisor_rates_on_the_boards_period(tmp_path: Path) -> None:
    bench = Bench(_config(tmp_path, (W410, W470), requested_fps=30.0))

    frames = frames_of(bench.run(2.0))

    times = [frame.exposure.timestamp_s for frame in frames]
    np.testing.assert_allclose(np.diff(times), 0.03333, atol=1e-12)
    assert len(frames) == 61


def test_rwd_rig_does_not_restart_on_host_scatter_at_120_hz(tmp_path: Path) -> None:
    bench = Bench(_config(tmp_path, requested_fps=120.0, exposure_us=5000))

    def setup(cams):
        cams.missed.add(("2", 0, 50))  # forces run 1
        # Run 1's first frames arrive 6 ms late: more than half a 8.3 ms period.
        cams.late[("1", 1, 0)] = 0.006
        cams.late[("1", 1, 1)] = 0.006
        cams.late[("2", 1, 2)] = 0.006

    bench.pending_setup = setup

    packets = bench.run(2.0)

    restarts = [e for e in events_of(packets) if e.event == "rwd_sequencer_restart"]
    assert len(restarts) == 1
    assert "missed exposure" in restarts[0].detail
    np.testing.assert_allclose(
        np.diff([f.exposure.timestamp_s for f in frames_of(packets)[:40]]), 0.008333, atol=1e-9
    )


def test_rwd_rig_reports_camera_delivery_counts_at_the_end(tmp_path: Path) -> None:
    bench = Bench(_config(tmp_path, (W410, W470)))
    bench.pending_setup = lambda cams: cams.lost.add(("1", 0, 5))

    packets = bench.run(1.0)

    events = {event.event: event.detail for event in events_of(packets)}
    assert events["rwd_camera_diagnostics"].endswith("1 incomplete, 0 lost to a full host queue")


def test_rwd_rig_waits_for_a_delayed_start_with_the_leds_off(tmp_path: Path) -> None:
    bench = Bench(_config(tmp_path, (W410, W470)))
    began = bench.clock.now_s

    packets = list(bench.rig.packets(1.0, start=StartCondition(delay_s=30.0)))

    assert len(bench.board.runs) == 1
    assert bench.board.runs[0].start_s - began >= 30.0
    events = {event.event: event.detail for event in events_of(packets)}
    assert events["recording_start_trigger"] == "started after a 30 s delay"
    assert len(frames_of(packets)) == 20
    bench.assert_all_off()


def test_rwd_rig_arms_dark_and_starts_on_the_chosen_ttl_edge(tmp_path: Path) -> None:
    bench = Bench(_config(tmp_path, (W410, W470, W560)))
    board = bench.board
    now = bench.clock.now_s
    board.schedule_input_edge(1, True, now + 4.0)  # wrong line
    board.schedule_input_edge(2, False, now + 5.0)  # wrong edge
    board.schedule_input_edge(2, True, now + 6.0)  # the start edge

    packets = list(bench.rig.packets(1.0, start=StartCondition(ttl_line=2, ttl_edge="rising")))

    armed, recorded = board.runs
    assert not any(armed.led_enabled.values()) and not any(armed.led_value.values())
    assert armed.stop_s is not None and armed.stop_s >= now + 6.0
    assert all(recorded.led_enabled.values())
    assert recorded.start_s > armed.stop_s
    frames = frames_of(packets)
    assert len(frames) == 20
    delivered = bench.cameras.delivered_items
    recorded_ids = {(str(frame.exposure.camera_index), frame.frame_id) for frame in frames}
    assert recorded_ids <= {(str(i.camera), i.frame_id) for i in delivered if i.run_index == 1}
    armed_frames = sum(item.run_index == 0 for item in delivered)
    events = {event.event: event.detail for event in events_of(packets)}
    detail = events["recording_start_trigger"]
    assert detail.startswith(
        f"TTL rising edge on input 2 at board tick {board.tick_ms(now + 6.0)} ms"
    )
    assert "2 other TTL edge(s) were not recorded" in detail
    assert f"{armed_frames} camera frame(s)" in detail
    assert "rwd_sequencer_restart" not in events
    bench.assert_all_off()


def test_rwd_rig_stopped_while_armed_switches_off_and_records_nothing(tmp_path: Path) -> None:
    bench = Bench(_config(tmp_path, (W410, W470)))
    timer = threading.Timer(0.2, bench.rig.stop)
    timer.start()

    packets = list(bench.rig.packets(1.0, start=StartCondition(ttl_line=1)))

    timer.join()
    assert frames_of(packets) == []
    events = {event.event: event.detail for event in events_of(packets)}
    assert "a rising edge on TTL input 1" in events["recording_start_cancelled"]
    assert len(bench.board.runs) == 1
    bench.assert_all_off()


def test_rwd_rig_sequential_excitation_records_lit_windows_and_dark_ttl(tmp_path: Path) -> None:
    bench = Bench(_config(tmp_path, (W410, W470)))
    board = bench.board
    connected = bench.clock.now_s
    board.schedule_input_edge(1, True, connected + 2.0)  # while the LEDs are dark
    board.schedule_input_edge(1, False, connected + 2.05)

    packets = list(bench.rig.packets(5.5, cycle=ExcitationCycle(on_s=1.0, off_s=2.0)))

    lit0, dark, lit1 = board.runs
    lit = {RWDLed.LED_410: True, RWDLed.LED_470: True, RWDLed.LED_560: False}
    assert lit0.led_enabled == lit1.led_enabled == lit
    assert not any(dark.led_enabled.values()) and not any(dark.led_value.values())
    times = [frame.exposure.timestamp_s for frame in frames_of(packets)]
    first, second = [t for t in times if t < 1.0], [t for t in times if t >= 1.0]
    assert len(first) == 20
    assert abs(second[0] - 3.0) < 0.05  # relit on schedule within one frame period
    assert max(second) < 4.0 and len(second) in {20, 21}
    sequences = [frame.exposure.sequence for frame in frames_of(packets)]
    assert sequences == list(range(len(times)))
    events = [event for event in events_of(packets)]
    names = [event.event for event in events]
    assert names.count("excitation_paused") == 1 and names.count("excitation_resumed") == 1
    paused = next(event for event in events if event.event == "excitation_paused")
    assert paused.timestamp_s == pytest.approx(1.0)
    assert "rwd_sequencer_restart" not in names
    edges = [edge for packet in packets for edge in packet.ttl_edges]
    assert [edge.edge for edge in edges] == ["rising", "falling"]
    assert edges[0].timestamp_s == pytest.approx(bench.session_s(connected + 2.0), abs=0.005)
    assert not invalid_of(packets)
    bench.assert_all_off()


def _outputs_config(tmp_path: Path, *outputs: RWDTTLOutputConfig) -> SessionConfig:
    payload = _config(tmp_path, (W410, W470)).model_dump()
    payload["source"] = RWDSourceConfig(ttl_outputs=outputs).model_dump()
    return SessionConfig.model_validate(payload)


def _output_switches(board, port: int) -> list[bool]:
    return [
        bool(message.data[1])
        for message in board.commands(RWDFunction.OUTPUT_ENABLE)
        if message.data[0] == port
    ]


def test_rwd_rig_runs_a_timed_output_program_and_marks_each_train(tmp_path: Path) -> None:
    program = RWDTTLOutputConfig(
        port=2,
        frequency_hz=10.0,
        pulse_width_ms=20,
        label="laser",
        delay_s=0.2,
        train_s=0.3,
        gap_s=0.2,
        cycles=2,
    )
    bench = Bench(_outputs_config(tmp_path, program))

    packets = bench.run(2.0)

    # Connect forces everything off; then on, off, on, off; close forces off again.
    assert _output_switches(bench.board, 2) == [False, True, False, True, False, False]
    marks = [mark for packet in packets for mark in packet.event_marks]
    assert [mark.name for mark in marks] == ["laser", "laser"]
    assert [mark.source for mark in marks] == ["output", "output"]
    assert [mark.timestamp_s for mark in marks] == pytest.approx([0.2, 0.7], abs=0.02)
    assert [mark.duration_s for mark in marks] == pytest.approx([0.3, 0.3])
    assert "10 Hz, 20 ms pulses for 0.3 s" in marks[0].detail
    events = {event.event: event.detail for event in events_of(packets)}
    assert events["rwd_ttl_outputs_started"].endswith("a 1.2 s program from the start")
    bench.assert_all_off()


def test_rwd_rig_starts_manual_programs_once_at_a_time(tmp_path: Path) -> None:
    manual = RWDTTLOutputConfig(
        port=4, frequency_hz=20.0, pulse_width_ms=10, trigger="manual", train_s=0.5
    )
    bench = Bench(_outputs_config(tmp_path, manual))
    packets = []
    for packet in bench.rig.packets(2.0):
        packets.append(packet)
        if len(frames_of(packets)) == 10:
            bench.rig.trigger_output(4)
            bench.rig.trigger_output(4)  # still running: ignored
            bench.rig.trigger_output(1)  # no program on output 1

    assert _output_switches(bench.board, 4) == [False, True, False, False]
    marks = [mark for packet in packets for mark in packet.event_marks]
    assert len(marks) == 1 and marks[0].timestamp_s == pytest.approx(0.5, abs=0.06)
    events = [event for event in events_of(packets)]
    names = [event.event for event in events]
    assert names.count("ttl_output_triggered") == 1
    assert names.count("ttl_output_trigger_ignored") == 2
    bench.assert_all_off()


def test_rwd_rig_changes_led_power_mid_session_with_a_marked_gap(tmp_path: Path) -> None:
    bench = Bench(_config(tmp_path, (W410, W470)))
    packets = []
    for packet in bench.rig.packets(2.0):
        packets.append(packet)
        if len(frames_of(packets)) == 10:
            bench.rig.request_led_power(W470, 20.0)
            bench.rig.request_led_power(W410, 5.0)  # unchanged

    frames = frames_of(packets)
    powers = [
        (f.exposure.commanded_power_percent, f.exposure.timestamp_s)
        for f in frames
        if f.exposure.wavelength_nm == W470
    ]
    assert powers[0][0] == 12.5 and powers[-1][0] == 20.0
    assert [run.led_value[RWDLed.LED_470] for run in bench.board.runs] == [1250, 2000]
    events = {event.event: event.detail for event in events_of(packets)}
    assert events["rwd_led_power_changed"].startswith("410 nm 5 %, 470 nm 20 %")
    (gap,) = invalid_of(packets)
    assert gap.reason == "RWD sequencer restart: LED power changed"
    assert all(not gap.start_time_s <= t < gap.stop_time_s for _p, t in powers)
    bench.assert_all_off()
