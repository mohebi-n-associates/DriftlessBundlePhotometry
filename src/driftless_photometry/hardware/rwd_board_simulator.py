"""In-memory RWD controller that behaves like the bench-tested R821 firmware.

It implements the serial-port surface used by ``RWDControllerTransport`` and models what
was measured on firmware 2.0.0, build 37619:

- acks carry four zero data bytes;
- setting commands are confirmed by replies 0x20 lower, and the camera-timing echo
  swaps the exposure bytes;
- output state changes are confirmed with ``BNC_PORT_REPLY``, and output configuration
  is acknowledged only;
- heartbeats are sent about once per second, and host heartbeats are not acked;
- input edges are reported only while the sequencer runs;
- the LEDs never turn off on their own.

It is a test and simulator aid, not evidence about any other firmware.
"""

from __future__ import annotations

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
        self._clock = clock
        self._heartbeat_interval_s = heartbeat_interval_s
        self._next_heartbeat_s = clock()
        self._decoder = RWDFrameStreamDecoder()
        self._outbox = bytearray()
        self._sequence = 0

    # Serial-port surface -------------------------------------------------------------
    @property
    def in_waiting(self) -> int:
        self._emit_heartbeats()
        return len(self._outbox)

    def read(self, size: int = 1) -> bytes:
        self._require_link()
        self._emit_heartbeats()
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

    def _emit_heartbeats(self) -> None:
        if self._heartbeat_interval_s is None or self.silent:
            return
        while self._clock() >= self._next_heartbeat_s:
            self._send(RWDMessageType.INFO, RWDFunction.CONTROLLER_HEARTBEAT, bytes(4))
            self._next_heartbeat_s += self._heartbeat_interval_s

    def _send(self, message_type: RWDMessageType, function: int, data: bytes) -> None:
        self._sequence = (self._sequence + 1) & 0xFFFF
        message = RWDMessage(message_type, self._sequence, function, data)
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
            state.sequencer_enabled = bool(data[0])
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
