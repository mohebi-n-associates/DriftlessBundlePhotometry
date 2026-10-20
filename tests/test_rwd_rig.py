from collections import Counter
from pathlib import Path

import numpy as np
import pytest

from driftless_photometry.config import (
    RWDSourceConfig,
    SessionConfig,
    TTLInputConfig,
    Wavelength,
    demo_config,
)
from driftless_photometry.faults import AcquisitionFault, AcquisitionFaultCode
from driftless_photometry.hardware.rwd_board_simulator import SimulatedRWDBoard
from driftless_photometry.hardware.rwd_controller_protocol import (
    RWDFunction,
    RWDLed,
    RWDMessage,
    RWDMessageType,
    encode_rwd_message,
)
from driftless_photometry.hardware.rwd_rig import RWDRig, RWDRigTiming
from driftless_photometry.hardware.rwd_rig_simulator import SimulatedRWDCameras, VirtualClock
from driftless_photometry.hardware.rwd_transport import RWDControllerTransport

W410, W470, W560 = Wavelength.CONTROL_410, Wavelength.GREEN_470, Wavelength.RED_560
START_DELAY_S = 0.0005


def _config(tmp_path: Path, enabled=(W410, W470, W560), **camera) -> SessionConfig:
    payload = demo_config(tmp_path, fiber_count=2, system="rwd").model_dump()
    for channel in payload["channels"]:
        channel["enabled"] = channel["wavelength_nm"] in enabled
    payload["channels"][1]["power_percent"] = 12.5
    payload["camera"].update(camera)
    return SessionConfig.model_validate(payload)


class Bench:
    """A simulated R821 with its two cameras on one virtual clock."""

    def __init__(
        self,
        config: SessionConfig,
        *,
        model_code: int = 0,
        ports: tuple[str, ...] = ("COM9",),
        timing: RWDRigTiming | None = None,
        **camera_options,
    ) -> None:
        self.clock = VirtualClock()
        self.board = SimulatedRWDBoard(clock=self.clock, model_code=model_code)
        self.cameras: SimulatedRWDCameras | None = None
        self.opened_ports: list[str] = []
        self.camera_ids: tuple[str, ...] = ()

        def transport(port: str) -> RWDControllerTransport:
            self.opened_ports.append(port)
            return RWDControllerTransport(
                port, serial_factory=lambda _: self.board, clock=self.clock
            )

        def cameras(settings, user_ids):
            self.camera_ids = user_ids
            self.cameras = SimulatedRWDCameras(
                self.board,
                self.clock,
                config,
                user_ids=user_ids,
                settings=settings,
                start_delay_s=START_DELAY_S,
                **camera_options,
            )
            self.pending_setup(self.cameras)
            return self.cameras

        self.pending_setup = lambda cams: None
        self.rig = RWDRig(
            config,
            transport_factory=transport,
            camera_factory=cameras,
            port_finder=lambda: ports,
            clock=self.clock,
            timing=timing,
        )

    def run(self, duration_s: float):
        return list(self.rig.packets(duration_s))

    def assert_all_off(self) -> None:
        state = self.board.state
        assert not state.sequencer_enabled
        assert not any(state.led_enabled.values())
        assert not any(state.led_value.values())
        assert not any(state.output_enabled.values())
        assert not state.light_on

    def session_s(self, clock_s: float, run: int = 0) -> float:
        """Session time of a clock instant, given session zero at run 0's first trigger."""

        return clock_s - (self.board.runs[0].start_s + START_DELAY_S)


def _frames(packets):
    return [packet.frame for packet in packets if packet.frame is not None]


def _events(packets):
    return [event for packet in packets for event in packet.system_events]


def _invalid(packets):
    return [interval for packet in packets for interval in packet.invalid_times]


def test_rwd_rig_records_an_interleaved_three_led_session(tmp_path: Path) -> None:
    bench = Bench(_config(tmp_path))

    packets = bench.run(2.0)

    frames = _frames(packets)
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
    events = [event.event for event in _events(packets)]
    assert events[:3] == [
        "rwd_controller_connected",
        "rwd_start_delay_measured",
        "rwd_sequencer_started",
    ]
    assert "rwd_sequencer_stopped" in events
    assert not _invalid(packets)
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

    frames = _frames(bench.run(1.0))

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
    events = {event.event: event.detail for event in _events(packets)}
    assert "rwd_ttl_clock_aligned" in events
    assert events["rwd_ttl_edges_not_configured"].startswith("1 edge")


def test_rwd_rig_restarts_after_a_missed_exposure_and_marks_the_span(tmp_path: Path) -> None:
    bench = Bench(_config(tmp_path))
    bench.pending_setup = lambda cams: cams.missed.add(("2", 0, 8))

    packets = bench.run(2.0)

    frames = _frames(packets)
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
    (interval,) = _invalid(packets)
    assert interval.start_time_s == pytest.approx(0.40)
    assert interval.stop_time_s == pytest.approx(run1[0].timestamp_s)
    assert interval.source_event == "rwd_sequencer_restart"
    assert "missed exposure" in interval.reason
    restart = next(e for e in _events(packets) if e.event == "rwd_sequencer_restart")
    assert "could not be attributed" in restart.detail
    assert restart.timestamp_s == pytest.approx(0.40)
    bench.assert_all_off()


def test_rwd_rig_restarts_after_a_frame_lost_in_transfer(tmp_path: Path) -> None:
    bench = Bench(_config(tmp_path, (W410, W470)))
    bench.pending_setup = lambda cams: cams.lost.add(("1", 0, 5))

    packets = bench.run(1.5)

    (interval,) = _invalid(packets)
    assert interval.start_time_s == pytest.approx(0.25)
    assert "frame ID jumped" in interval.reason
    frames = _frames(packets)
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
    events = _events(packets)
    reasons = [event.detail for event in events if event.event == "rwd_sequencer_restart"]
    assert len(reasons) == 2
    assert "first trigger may have been missed" in reasons[1]
    frames = _frames(packets)
    third = bench.session_s(bench.board.runs[2].start_s + START_DELAY_S)
    after = [f.exposure for f in frames if f.exposure.timestamp_s >= third - 1e-6]
    assert after[0].wavelength_nm is W410
    assert after[0].timestamp_s == pytest.approx(third, abs=1e-4)
    # Nothing from the misattributable run 1 was recorded.
    assert all(
        f.exposure.timestamp_s < 0.30 or f.exposure.timestamp_s >= third - 1e-6 for f in frames
    )
    (interval,) = _invalid(packets)
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


def test_rwd_rig_fails_after_repeated_restarts_without_frames(tmp_path: Path) -> None:
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
        if len(_frames(received)) == 7:
            bench.rig.stop()

    frames = _frames(received)
    assert len(frames) == 7
    assert received[-1].frame is None
    events = [event.event for event in _events(received)]
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


def test_rwd_rig_records_malformed_controller_frames(tmp_path: Path) -> None:
    bench = Bench(_config(tmp_path))
    packets = bench.rig.packets(1.0)
    received = [next(packets) for _index in range(4)]
    frame = bytearray(
        encode_rwd_message(RWDMessage(RWDMessageType.REPLY, 9, RWDFunction.INPUT_EDGE, bytes(6)))
    )
    frame[-3] ^= 0xFF
    bench.board.inject_raw(bytes(frame))
    received.extend(packets)

    (interval,) = _invalid(received)
    assert "a TTL edge may have been lost" in interval.reason
    assert "rwd_malformed_controller_frame" in [event.event for event in _events(received)]
    assert len(_frames(received)) == 20


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
