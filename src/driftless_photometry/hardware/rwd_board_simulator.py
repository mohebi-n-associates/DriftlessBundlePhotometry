"""In-memory RWD controller that behaves like the bench-tested R821 firmware.

It implements the serial-port surface used by ``RWDControllerTransport`` and models what
was measured on firmware 2.0.0, build 37619:

- acks carry four zero data bytes;
- setting commands are confirmed by replies 0x20 lower, and the camera-timing echo
  swaps the exposure bytes;
- output state changes are confirmed with ``BNC_PORT_REPLY``, and output configuration
  is acknowledged only;
- heartbeats are sent about once per second, and host heartbeats are not acked;
- input edges are reported only while the sequencer runs, stamped with a free-running
  millisecond tick;
- the LEDs never turn off on their own.

Each sequencer run is recorded in ``runs`` so ``rwd_rig_simulator`` can trigger
simulated cameras from it.

It is a test and simulator aid, not evidence about any other firmware.
"""

from __future__ import annotations

import heapq
import math
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from .rwd_controller_protocol import (
    RWDFrameStreamDecoder,
    RWDFunction,
    RWDLed,
    RWDMalformedFrame,
    RWDMessage,
    RWDMessageType,
    encode_rwd_message,
)


@dataclass(slots=True)
class SimulatedRWDState:
    sequencer_enabled: bool = False
    excitation_mode: int = 0
    frame_rate_hz: int = 0
    exposure_units: int = 0
    led_enabled: dict[RWDLed, bool] = field(default_factory=lambda: dict.fromkeys(RWDLed, False))
    led_value: dict[RWDLed, int] = field(default_factory=lambda: dict.fromkeys(RWDLed, 0))
    output_enabled: dict[int, bool] = field(
        default_factory=lambda: dict.fromkeys(range(1, 5), False)
    )
    output_config: dict[int, bytes] = field(default_factory=dict)

    @property
    def light_on(self) -> bool:
        return self.sequencer_enabled and any(
            self.led_enabled[led] and self.led_value[led] > 0 for led in RWDLed
        )


@dataclass(slots=True)
class SimulatedRWDRun:
    """One sequencer run and the settings the board applied to it."""

    start_s: float
    excitation_mode: int
    frame_rate_hz: int
    exposure_units: int
    led_value: dict[RWDLed, int]
    led_enabled: dict[RWDLed, bool]
    stop_s: float | None = None


@dataclass(order=True, slots=True)
class _ScheduledEdge:
    deliver_s: float
    at_s: float = field(compare=False)
    line: int = field(compare=False)
    rising: bool = field(compare=False)


class SimulatedRWDBoard:
    """A serial-port stand-in for the RWD controller board."""

    def __init__(
        self,
        *,
        firmware: tuple[int, int, int] = (2, 0, 0),
        build: int = 37619,
        model_code: int = 0,
        magic: bytes = b"OFRS",
        heartbeat_interval_s: float | None = 1.0,
        tick_origin_ms: int = 5_000_000,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.firmware = firmware
        self.build = build
        self.model_code = model_code
        self.magic = magic
        self.state = SimulatedRWDState()
        self.received: list[RWDMessage] = []
        self.malformed_from_host: list[RWDMalformedFrame] = []
        self.is_open = True
        self.failed = False
        self.drop_acks = 0
        self.silent = False
        self.corrupt_confirmations = False
        self.edge_retransmissions = 0
        """Extra copies of each edge report, as the board sends when acks are late."""
        self.report_edges_while_stopped = False
        """Report edges with the sequencer stopped, as seen once on the bench."""
        self._clock = clock
        self._heartbeat_interval_s = heartbeat_interval_s
        self._next_heartbeat_s = clock()
        self._decoder = RWDFrameStreamDecoder()
        self._outbox = bytearray()
        self._last_frame_start = 0
        self._sequence = 0
        self.runs: list[SimulatedRWDRun] = []
        self._boot_s = clock()
        self._tick_origin_ms = tick_origin_ms
        self._scheduled: list[_ScheduledEdge] = []

    # Serial-port surface -------------------------------------------------------------
    @property
    def in_waiting(self) -> int:
        self._require_link()
        self._emit_due()
        return len(self._outbox)

    def read(self, size: int = 1) -> bytes:
        self._require_link()
        self._emit_due()
        chunk = bytes(self._outbox[:size])
        del self._outbox[:size]
        return chunk

    def write(self, data: bytes) -> int:
        self._require_link()
        for result in self._decoder.feed(data):
            if isinstance(result, RWDMalformedFrame):
                self.malformed_from_host.append(result)
            else:
                self._handle(result)
        return len(data)

    def flush(self) -> None:
        self._require_link()

    def close(self) -> None:
        self.is_open = False

    # Test controls -------------------------------------------------------------------
    def inject_input_edge(self, line: int, rising: bool, tick_ms: int) -> None:
        """Report an edge on BNC input ``line`` (1-4) if the sequencer is running."""

        if self.state.sequencer_enabled:
            data = bytes([line - 1]) + tick_ms.to_bytes(4, "big") + bytes([0 if rising else 1])
            self._reply(RWDFunction.INPUT_EDGE, data)

    def schedule_input_edge(
        self, line: int, rising: bool, at_s: float, *, latency_s: float = 0.002
    ) -> None:
        """Change BNC input ``line`` at clock time ``at_s``; the report arrives later."""

        heapq.heappush(self._scheduled, _ScheduledEdge(at_s + latency_s, at_s, line, rising))

    def tick_ms(self, at_s: float) -> int:
        """The board's free-running millisecond tick at clock time ``at_s``."""

        return self._tick_origin_ms + math.floor((at_s - self._boot_s) * 1000)

    def running_at(self, at_s: float) -> bool:
        return any(
            run.start_s <= at_s and (run.stop_s is None or at_s < run.stop_s) for run in self.runs
        )

    def inject_raw(self, data: bytes) -> None:
        self._outbox += data

    def commands(self, function: RWDFunction) -> list[RWDMessage]:
        return [
            m
            for m in self.received
            if m.function is function and m.message_type is RWDMessageType.COMMAND
        ]

    # Firmware behavior ---------------------------------------------------------------
    def _require_link(self) -> None:
        if self.failed or not self.is_open:
            raise OSError("simulated RWD controller link is down")

    def _emit_due(self) -> None:
        now = self._clock()
        while self._scheduled and self._scheduled[0].deliver_s <= now:
            edge = heapq.heappop(self._scheduled)
            reportable = self.running_at(edge.at_s) or self.report_edges_while_stopped
            if reportable and not self.silent:
                data = (
                    bytes([edge.line - 1])
                    + self.tick_ms(edge.at_s).to_bytes(4, "big")
                    + bytes([0 if edge.rising else 1])
                )
                self._reply(RWDFunction.INPUT_EDGE, data)
                frame = self._outbox[self._last_frame_start :]
                self._outbox += bytes(frame) * self.edge_retransmissions
        self._emit_heartbeats()

    def _emit_heartbeats(self) -> None:
        if self._heartbeat_interval_s is None or self.silent:
            return
        while self._clock() >= self._next_heartbeat_s:
            self._send(RWDMessageType.INFO, RWDFunction.CONTROLLER_HEARTBEAT, bytes(4))
            self._next_heartbeat_s += self._heartbeat_interval_s

    def _send(self, message_type: RWDMessageType, function: int, data: bytes) -> None:
        self._sequence = (self._sequence + 1) & 0xFFFF
        message = RWDMessage(message_type, self._sequence, function, data)
        self._last_frame_start = len(self._outbox)
        self._outbox += encode_rwd_message(message, allow_unsafe=True)

    def _reply(self, function: RWDFunction, data: bytes) -> None:
        if self.corrupt_confirmations and function is not RWDFunction.INPUT_EDGE:
            data = bytes(b ^ 0x01 for b in data)
        self._send(RWDMessageType.REPLY, function, data)

    def _handle(self, message: RWDMessage) -> None:
        self.received.append(message)
        if message.message_type is not RWDMessageType.COMMAND or self.silent:
            return
        if self.drop_acks:
            self.drop_acks -= 1
            return
        self._ack(message)
        data, state, function = message.data, self.state, message.function
        if function is RWDFunction.VERSION_QUERY:
            self._reply(RWDFunction.VERSION_REPLY, bytes([*self.firmware, 0]))
            self._reply(
                RWDFunction.VERSION_DETAIL_REPLY, self.build.to_bytes(4, "big") + self.magic
            )
        elif function is RWDFunction.QUERY_MODEL:
            self._reply(RWDFunction.MODEL_REPLY, bytes([self.model_code]))
        elif function is RWDFunction.LED_VALUE:
            state.led_value[RWDLed(data[0])] = int.from_bytes(data[2:4], "big")
            self._reply(RWDFunction.LED_VALUE_REPLY, data)
        elif function is RWDFunction.LED_ENABLE:
            state.led_enabled[RWDLed(data[0])] = bool(data[1])
            self._reply(RWDFunction.LED_ENABLE_REPLY, data[:2])
        elif function is RWDFunction.CAMERA_TIMING:
            state.frame_rate_hz = int.from_bytes(data[0:2], "big")
            state.exposure_units = int.from_bytes(data[2:4], "big")
            self._reply(RWDFunction.CAMERA_TIMING_REPLY, data[:2] + data[2:4][::-1])
        elif function is RWDFunction.EXCITATION_MODE:
            state.excitation_mode = data[0]
            self._reply(RWDFunction.EXCITATION_MODE_REPLY, data)
        elif function is RWDFunction.DEVICE_ENABLE:
            enabled = bool(data[0])
            if enabled and not state.sequencer_enabled:
                self.runs.append(
                    SimulatedRWDRun(
                        start_s=self._clock(),
                        excitation_mode=state.excitation_mode,
                        frame_rate_hz=state.frame_rate_hz,
                        exposure_units=state.exposure_units,
                        led_value=dict(state.led_value),
                        led_enabled=dict(state.led_enabled),
                    )
                )
            elif state.sequencer_enabled and not enabled and self.runs:
                self.runs[-1].stop_s = self._clock()
            state.sequencer_enabled = enabled
            self._reply(RWDFunction.STATE_REPLY, data)
        elif function is RWDFunction.OUTPUT_ENABLE:
            port, enabled = data[0], bool(data[1])
            if state.output_enabled.get(port) != enabled:
                state.output_enabled[port] = enabled
                self._reply(RWDFunction.BNC_PORT_REPLY, data[:2])
        elif function is RWDFunction.OUTPUT_CONFIG:
            state.output_config[data[0]] = data[1:]

    def _ack(self, message: RWDMessage) -> None:
        self._outbox += encode_rwd_message(
            RWDMessage(RWDMessageType.ACK, message.sequence, message.function_id, bytes(4)),
            allow_unsafe=True,
        )
