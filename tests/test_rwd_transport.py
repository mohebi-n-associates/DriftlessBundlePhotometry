from types import SimpleNamespace

import pytest

from driftless_photometry.faults import AcquisitionFault, AcquisitionFaultCode
from driftless_photometry.hardware.rwd_board_simulator import SimulatedRWDBoard
from driftless_photometry.hardware.rwd_controller_protocol import (
    RWDExcitationMode,
    RWDFrameStreamDecoder,
    RWDFunction,
    RWDLed,
    RWDMessageType,
    camera_timing,
    device_enable,
    encode_rwd_message,
    excitation_mode,
    led_enable,
    led_power,
    model_query,
    output_config,
    output_enable,
    version_query,
)
from driftless_photometry.hardware.rwd_transport import (
    RWDControllerIdentity,
    RWDControllerTransport,
    candidate_rwd_ports,
)


class SteppingClock:
    """Deterministic clock that advances a little on every read."""

    def __init__(self, step_s: float = 0.001) -> None:
        self.now_s = 0.0
        self.step_s = step_s

    def __call__(self) -> float:
        self.now_s += self.step_s
        return self.now_s


@pytest.fixture
def clock() -> SteppingClock:
    return SteppingClock()


def _board(clock: SteppingClock, **kwargs: object) -> SimulatedRWDBoard:
    return SimulatedRWDBoard(clock=clock, **kwargs)


def _transport(board: SimulatedRWDBoard, clock: SteppingClock) -> RWDControllerTransport:
    return RWDControllerTransport(
        "COM_TEST",
        serial_factory=lambda _port: board,
        ack_timeout_s=0.05,
        handshake_timeout_s=0.2,
        clock=clock,
    )


def _light_on(transport: RWDControllerTransport) -> None:
    transport.command(camera_timing(20, 40.0))
    transport.command(led_power(RWDLed.LED_470, 5.0))
    transport.command(led_enable(RWDLed.LED_470, True))
    transport.command(excitation_mode(RWDExcitationMode.CONTINUOUS))
    transport.command(device_enable(True))


def test_open_verifies_identity_and_forces_a_known_off_state(clock: SteppingClock) -> None:
    board = _board(clock)
    board.state.sequencer_enabled = True
    board.state.led_enabled[RWDLed.LED_410] = True
    board.state.led_value[RWDLed.LED_410] = 500
    board.state.output_enabled[2] = True

    with _transport(board, clock) as transport:
        assert transport.identity == RWDControllerIdentity((2, 0, 0), 37619, 0)
        assert transport.identity.model_name == "R821"
        assert not board.state.light_on
        assert not any(board.state.output_enabled.values())

    commands = [m.function for m in board.received if m.message_type is RWDMessageType.COMMAND]
    assert commands[:3] == [
        RWDFunction.VERSION_QUERY,
        RWDFunction.QUERY_MODEL,
        RWDFunction.DEVICE_ENABLE,
    ]
    assert commands[3:7] == [RWDFunction.OUTPUT_ENABLE] * 4
    assert not board.is_open


def test_close_always_turns_excitation_off(clock: SteppingClock) -> None:
    board = _board(clock)
    with _transport(board, clock) as transport:
        _light_on(transport)
        assert board.state.light_on
        transport.command(output_config(1, 10.0, 20))
        transport.command(output_enable(1, True))
        assert board.state.output_enabled[1]

    assert not board.state.light_on
    assert not board.state.sequencer_enabled
    assert board.state.led_value[RWDLed.LED_470] == 0
    assert not board.state.output_enabled[1]


def test_close_after_an_error_still_turns_excitation_off(clock: SteppingClock) -> None:
    board = _board(clock)
    with pytest.raises(RuntimeError, match="operator abort"), _transport(board, clock) as transport:
        _light_on(transport)
        raise RuntimeError("operator abort")

    assert not board.state.light_on
    assert not board.is_open


def test_commands_are_confirmed_and_mismatches_are_faults(clock: SteppingClock) -> None:
    board = _board(clock)
    with _transport(board, clock) as transport:
        transport.command(camera_timing(20, 40.0))
        assert (board.state.frame_rate_hz, board.state.exposure_units) == (20, 4000)

        board.corrupt_confirmations = True
        with pytest.raises(AcquisitionFault) as fault:
            transport.command(led_power(RWDLed.LED_470, 5.0))
        assert fault.value.code is AcquisitionFaultCode.MALFORMED_CONTROLLER_STREAM
        assert "confirmed LED_VALUE" in str(fault.value)
        board.corrupt_confirmations = False


def test_lost_acks_are_resent_like_ofrs(clock: SteppingClock) -> None:
    board = _board(clock)
    with _transport(board, clock) as transport:
        before = len(board.commands(RWDFunction.DEVICE_ENABLE))
        board.drop_acks = 2
        transport.command(device_enable(False))
        attempts = board.commands(RWDFunction.DEVICE_ENABLE)[before:]

    assert len(attempts) == 3
    assert len({m.sequence for m in attempts}) == 1


def test_unacknowledged_commands_fault_and_close_still_releases_the_port(
    clock: SteppingClock,
) -> None:
    board = _board(clock)
    transport = _transport(board, clock)
    transport.open()
    board.silent = True

    with pytest.raises(AcquisitionFault) as fault:
        transport.command(device_enable(True))

    assert fault.value.code is AcquisitionFaultCode.CONTROLLER_DISCONNECT
    assert "after 4 attempts" in str(fault.value)
    with pytest.raises(AcquisitionFault, match="power switch"):
        transport.close()
    assert not board.is_open
    assert not transport.is_open


@pytest.mark.parametrize(
    ("board_kwargs", "code"),
    [
        ({"magic": b"XXXX"}, AcquisitionFaultCode.MALFORMED_CONTROLLER_STREAM),
        ({"heartbeat_interval_s": None}, AcquisitionFaultCode.CONTROLLER_DISCONNECT),
    ],
)
def test_handshake_rejects_other_devices_and_silence(
    clock: SteppingClock, board_kwargs: dict[str, object], code: AcquisitionFaultCode
) -> None:
    board = _board(clock, **board_kwargs)
    if "heartbeat_interval_s" in board_kwargs:
        board.silent = True

    with pytest.raises(AcquisitionFault) as fault:
        _transport(board, clock).open()

    assert fault.value.code is code
    assert not board.is_open


def test_open_reports_unavailable_ports(clock: SteppingClock) -> None:
    def missing(_port: str) -> SimulatedRWDBoard:
        raise OSError("port not found")

    transport = RWDControllerTransport("COM9", serial_factory=missing, clock=clock)
    with pytest.raises(AcquisitionFault) as fault:
        transport.open()
    assert fault.value.code is AcquisitionFaultCode.CONTROLLER_DISCONNECT
    assert "COM9" in str(fault.value)


def test_poll_returns_timed_edges_only_while_the_sequencer_runs(clock: SteppingClock) -> None:
    board = _board(clock)
    with _transport(board, clock) as transport:
        board.inject_input_edge(1, rising=True, tick_ms=1)
        assert transport.poll().edges == ()

        transport.command(device_enable(True))
        board.inject_input_edge(1, rising=True, tick_ms=4_194_820)
        board.inject_input_edge(1, rising=False, tick_ms=4_194_831)
        board.inject_raw(b"\x7e\x01\x02\x03\x7e")
        result = transport.poll()

    assert [(e.edge.line, e.edge.edge, e.edge.tick_ms) for e in result.edges] == [
        (1, "rising", 4_194_820),
        (1, "falling", 4_194_831),
    ]
    assert all(e.host_s > 0 for e in result.edges)
    assert len(result.malformed) == 1
    edge_acks = [
        m
        for m in board.received
        if m.message_type is RWDMessageType.ACK and m.function is RWDFunction.INPUT_EDGE
    ]
    assert len(edge_acks) == 2


def test_poll_sends_host_heartbeats_and_detects_controller_silence(
    clock: SteppingClock,
) -> None:
    board = _board(clock)
    with _transport(board, clock) as transport:
        clock.now_s += 1.5
        transport.poll()
        heartbeats = [m for m in board.received if m.function is RWDFunction.HOST_HEARTBEAT]
        assert heartbeats and heartbeats[-1].message_type is RWDMessageType.INFO

        board.silent = True
        clock.now_s += 6.0
        with pytest.raises(AcquisitionFault) as fault:
            transport.poll()
        assert fault.value.code is AcquisitionFaultCode.CONTROLLER_DISCONNECT
        board.silent = False


def test_link_loss_is_a_controller_disconnect(clock: SteppingClock) -> None:
    board = _board(clock)
    transport = _transport(board, clock)
    transport.open()
    board.failed = True

    with pytest.raises(AcquisitionFault) as fault:
        transport.poll()

    assert fault.value.code is AcquisitionFaultCode.CONTROLLER_DISCONNECT
    with pytest.raises(AcquisitionFault, match="power switch"):
        transport.close()
    assert not transport.is_open


def test_shutdown_attempts_all_commands_after_lost_acknowledgements(clock: SteppingClock):
    board = _board(clock)
    transport = _transport(board, clock)
    transport.open()
    _light_on(transport)
    transport.command(output_enable(1, True))
    board.drop_acks = 2
    before = len(board.received)
    with pytest.raises(AcquisitionFault, match="shutdown unconfirmed"):
        transport.close()
    commands = board.received[before:]
    assert any(m.function is RWDFunction.LED_VALUE for m in commands)
    assert any(m.function is RWDFunction.LED_ENABLE for m in commands)
    assert not any(board.state.led_enabled.values())
    assert not any(board.state.led_value.values())
    assert not any(board.state.output_enabled.values())
    assert not board.is_open


def test_simulator_reproduces_bench_confirmations(clock: SteppingClock) -> None:
    board = _board(clock, heartbeat_interval_s=None)
    for sequence, body in enumerate((version_query(), model_query(), camera_timing(20, 40.0)), 1):
        board.write(encode_rwd_message(body.to_message(sequence)))

    replies = [
        (m.function, m.data)
        for m in RWDFrameStreamDecoder().feed(board.read(4096))
        if m.message_type is RWDMessageType.REPLY
    ]
    assert replies == [
        (RWDFunction.VERSION_REPLY, bytes.fromhex("02000000")),
        (RWDFunction.VERSION_DETAIL_REPLY, b"\x00\x00\x92\xf3OFRS"),
        (RWDFunction.MODEL_REPLY, b"\x00"),
        (RWDFunction.CAMERA_TIMING_REPLY, bytes.fromhex("0014a00f")),
    ]


def test_candidate_ports_list_ft232r_bridges(monkeypatch: pytest.MonkeyPatch) -> None:
    ports = [
        SimpleNamespace(device="COM7", vid=0x0403, pid=0x6001),
        SimpleNamespace(device="COM3", vid=0x0403, pid=0x6001),
        SimpleNamespace(device="COM4", vid=0x2341, pid=0x0043),
    ]
    monkeypatch.setattr("serial.tools.list_ports.comports", lambda: ports)
    assert candidate_rwd_ports() == ("COM3", "COM7")


def test_transport_validates_its_settings() -> None:
    with pytest.raises(ValueError, match="empty"):
        RWDControllerTransport("")
    with pytest.raises(ValueError, match="silence limit"):
        RWDControllerTransport("COM3", controller_silence_limit_s=0.5)
    with pytest.raises(ValueError, match="resends"):
        RWDControllerTransport("COM3", resends=-1)


def test_poll_acks_board_retransmissions_but_reports_each_edge_once() -> None:
    clock = SteppingClock()
    board = SimulatedRWDBoard(clock=clock)
    transport = RWDControllerTransport("COM9", serial_factory=lambda _: board, clock=clock)
    transport.open()
    transport.command(device_enable(True))
    board.edge_retransmissions = 2
    board.inject_input_edge(1, True, 1_000)
    frame = bytes(board._outbox)
    board.inject_raw(frame + frame)

    result = transport.poll()

    assert [received.edge.tick_ms for received in result.edges] == [1_000]
    assert result.retransmissions == 2
    acks = [
        message
        for message in board.received
        if message.message_type is RWDMessageType.ACK and message.function is RWDFunction.INPUT_EDGE
    ]
    assert len(acks) == 3


def test_poll_never_waits_on_an_idle_link() -> None:
    class CountingLink:
        is_open = True
        in_waiting = 0

        def __init__(self) -> None:
            self.reads = 0

        def read(self, size: int = 1) -> bytes:
            self.reads += 1
            return b""

    clock = SteppingClock()
    board = SimulatedRWDBoard(clock=clock)
    transport = RWDControllerTransport("COM9", serial_factory=lambda _: board, clock=clock)
    transport.open()
    link = CountingLink()
    transport._serial = link  # type: ignore[assignment]

    transport._read_once(block=False)

    assert link.reads == 0
