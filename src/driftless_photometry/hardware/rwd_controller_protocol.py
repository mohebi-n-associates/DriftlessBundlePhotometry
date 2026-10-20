"""Codec for the RWD fiber-photometry controller board's serial protocol.

The layout was recovered by interoperability analysis of RWD OFRS 2.0.0.38566
(``Common.SerialPort.RwdProtocol`` and ``OFRS.Core.FunctionModel.SerialPort``). See
``docs/rwd-direct-control.md`` for provenance and for which fields are still
unconfirmed; nothing here has been validated against physical hardware.

Wire format over the controller's FTDI virtual COM port (115200 baud, 8N1)::

    7E | FF 03 00 21 | type | seq_hi seq_lo | function | data... | crc_hi crc_lo | 7E

The CRC is CRC-16/MODBUS over ``FF 03 00 21`` through the end of ``data`` and is sent
high byte first. Between the flags, ``7E`` is escaped as ``7D 5E`` and ``7D`` as
``7D 5D``.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import IntEnum
from typing import Literal, TypeAlias

from .controller_protocol import ProtocolError

RWD_CONTROLLER_BAUDRATE = 115_200
RWD_FLAG = 0x7E
RWD_ESCAPE = 0x7D
RWD_FRAME_HEADER = b"\xff\x03\x00\x21"
RWD_VERSION_MAGIC = b"OFRS"
MAX_RWD_FRAME_BYTES = 4096
_MIN_FRAME_BYTES = len(RWD_FRAME_HEADER) + 4 + 2
_ESCAPED = {0x5E: RWD_FLAG, 0x5D: RWD_ESCAPE}


class RWDFrameError(ProtocolError):
    """An RWD controller frame or payload failed escaping, length, header, or CRC checks."""


class RWDMessageType(IntEnum):
    ERROR = 0
    COMMAND = 1
    ACK = 2
    REPLY = 3
    INFO = 4


class RWDFunction(IntEnum):
    """Function identifiers from OFRS ``OFRSProtocolFunctionID``."""

    LED_VALUE = 0x41
    LED_ENABLE = 0x42
    CAMERA_TIMING = 0x43
    EXCITATION_MODE = 0x44
    DEVICE_ENABLE = 0x45
    OUTPUT_ENABLE = 0x46
    OUTPUT_CONFIG = 0x48
    VERSION_QUERY = 0x49
    SET_LED_LIMITS = 0x4B
    # OFRS sends both its time check and its LED-limit query with this identifier.
    TIME_CHECK = 0x4C
    SET_MODEL = 0x4D
    QUERY_MODEL = 0x4E
    RESTORE_DEFAULTS = 0x4F
    FIRMWARE_UPGRADE = 0x52
    HOST_HEARTBEAT = 0x71
    LED_VALUE_REPLY = 0x21
    LED_ENABLE_REPLY = 0x22
    # Confirmations seen on firmware 2.0.0 (bench); absent from OFRS's identifier list.
    CAMERA_TIMING_REPLY = 0x23
    EXCITATION_MODE_REPLY = 0x24
    STATE_REPLY = 0x25
    VERSION_REPLY = 0x26
    VERSION_DETAIL_REPLY = 0x27
    INPUT_EDGE = 0x28
    BNC_PORT_REPLY = 0x29
    LED_LIMITS_REPLY = 0x2A
    MODEL_REPLY = 0x2B
    TIME_CHECK_REPLY = 0x2D
    CONTROLLER_HEARTBEAT = 0x31


# Factory calibration, model identity, defaults, and firmware: never sent by DBF.
UNSAFE_RWD_FUNCTIONS = frozenset(
    {
        RWDFunction.SET_LED_LIMITS,
        RWDFunction.SET_MODEL,
        RWDFunction.RESTORE_DEFAULTS,
        RWDFunction.FIRMWARE_UPGRADE,
    }
)


class RWDLed(IntEnum):
    """LED index used in RWD controller payloads."""

    LED_410 = 0
    LED_470 = 1
    LED_560 = 2

    @property
    def wavelength_nm(self) -> int:
        """Exact RWD excitation wavelength; RWD hardware uses 410/470/560 nm LEDs."""

        return (410, 470, 560)[self]


class RWDExcitationMode(IntEnum):
    """Board excitation modes: one LED is driven continuously, combinations interleave."""

    CONTINUOUS = 0
    INTERLEAVED_410_470 = 1
    INTERLEAVED_470_560 = 2
    INTERLEAVED_410_470_560 = 3


_INTERLEAVED_MODES = {
    frozenset({RWDLed.LED_410, RWDLed.LED_470}): RWDExcitationMode.INTERLEAVED_410_470,
    frozenset({RWDLed.LED_470, RWDLed.LED_560}): RWDExcitationMode.INTERLEAVED_470_560,
    frozenset(RWDLed): RWDExcitationMode.INTERLEAVED_410_470_560,
}


def excitation_mode_for(leds: Iterable[RWDLed]) -> RWDExcitationMode:
    """Return the board mode OFRS uses for an LED selection; 410+560 has none."""

    selected = frozenset(leds)
    if len(selected) == 1:
        return RWDExcitationMode.CONTINUOUS
    try:
        return _INTERLEAVED_MODES[selected]
    except KeyError:
        names = "+".join(str(led.wavelength_nm) for led in sorted(selected))
        raise ValueError(f"OFRS defines no excitation mode for LEDs {names or 'none'}") from None


@dataclass(frozen=True, slots=True)
class RWDMessage:
    message_type: RWDMessageType
    sequence: int
    function_id: int
    data: bytes = b""

    def __post_init__(self) -> None:
        if not 0 <= self.sequence <= 0xFFFF:
            raise ValueError("RWD message sequence must fit in 16 bits")
        if not 0 <= self.function_id <= 0xFF:
            raise ValueError("RWD function identifier must fit in 8 bits")

    @property
    def function(self) -> RWDFunction | None:
        try:
            return RWDFunction(self.function_id)
        except ValueError:
            return None

    @property
    def function_name(self) -> str:
        function = self.function
        return function.name if function is not None else f"0x{self.function_id:02X}"


@dataclass(frozen=True, slots=True)
class RWDMalformedFrame:
    """Escaped bytes between two flags that could not be decoded."""

    raw: bytes
    reason: str


RWDFrameResult: TypeAlias = RWDMessage | RWDMalformedFrame


@dataclass(frozen=True, slots=True)
class RWDCommand:
    """A message body whose sequence number is assigned by the sending transport."""

    function: RWDFunction
    data: bytes
    message_type: RWDMessageType = RWDMessageType.COMMAND

    def to_message(self, sequence: int) -> RWDMessage:
        return RWDMessage(self.message_type, sequence, self.function, self.data)


def crc16_modbus(data: bytes) -> int:
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc


def escape_rwd_body(body: bytes) -> bytes:
    escaped = bytearray()
    for byte in body:
        if byte == RWD_FLAG:
            escaped += b"\x7d\x5e"
        elif byte == RWD_ESCAPE:
            escaped += b"\x7d\x5d"
        else:
            escaped.append(byte)
    return bytes(escaped)


def unescape_rwd_body(body: bytes) -> bytes:
    unescaped = bytearray()
    index = 0
    while index < len(body):
        byte = body[index]
        if byte != RWD_ESCAPE:
            unescaped.append(byte)
            index += 1
            continue
        if index + 1 >= len(body) or body[index + 1] not in _ESCAPED:
            raise RWDFrameError("frame contains an invalid escape sequence")
        unescaped.append(_ESCAPED[body[index + 1]])
        index += 2
    return bytes(unescaped)


def encode_rwd_message(message: RWDMessage, *, allow_unsafe: bool = False) -> bytes:
    """Encode one complete flagged frame."""

    if (
        message.message_type is RWDMessageType.COMMAND
        and message.function_id in UNSAFE_RWD_FUNCTIONS
        and not allow_unsafe
    ):
        raise ValueError(f"refusing to encode unsafe RWD command {message.function_name}")
    body = (
        RWD_FRAME_HEADER
        + bytes([message.message_type, message.sequence >> 8, message.sequence & 0xFF])
        + bytes([message.function_id])
        + message.data
    )
    checksum = crc16_modbus(body).to_bytes(2, "big")
    return bytes([RWD_FLAG]) + escape_rwd_body(body + checksum) + bytes([RWD_FLAG])


def decode_rwd_frame(escaped: bytes) -> RWDMessage:
    """Decode the escaped bytes found between two flags."""

    body = unescape_rwd_body(escaped)
    if len(body) < _MIN_FRAME_BYTES:
        raise RWDFrameError("frame is shorter than the minimum RWD frame")
    if body[: len(RWD_FRAME_HEADER)] != RWD_FRAME_HEADER:
        raise RWDFrameError("frame header is not FF 03 00 21")
    payload, checksum = body[:-2], int.from_bytes(body[-2:], "big")
    if crc16_modbus(payload) != checksum:
        raise RWDFrameError("frame CRC mismatch")
    try:
        message_type = RWDMessageType(payload[4])
    except ValueError:
        raise RWDFrameError(f"unknown RWD message type {payload[4]}") from None
    return RWDMessage(
        message_type,
        int.from_bytes(payload[5:7], "big"),
        payload[7],
        bytes(payload[8:]),
    )


class RWDFrameStreamDecoder:
    """Split a serial byte stream into frames without trusting read boundaries.

    Malformed frames are returned as ``RWDMalformedFrame`` records so the caller can
    surface them and continue with the next frame. Buffering is bounded.
    """

    def __init__(self, max_frame_bytes: int = MAX_RWD_FRAME_BYTES) -> None:
        if max_frame_bytes < _MIN_FRAME_BYTES:
            raise ValueError("maximum frame size is smaller than the minimum RWD frame")
        self._max_frame_bytes = max_frame_bytes
        self._buffer = bytearray()
        self._synchronized = False
        self._discarding = False

    def feed(self, data: bytes) -> tuple[RWDFrameResult, ...]:
        results: list[RWDFrameResult] = []
        for byte in data:
            if byte == RWD_FLAG:
                if self._buffer and not self._discarding:
                    results.append(self._finish(bytes(self._buffer)))
                self._buffer.clear()
                self._synchronized = True
                self._discarding = False
                continue
            if not self._synchronized or self._discarding:
                continue
            self._buffer.append(byte)
            if len(self._buffer) > self._max_frame_bytes:
                results.append(
                    RWDMalformedFrame(bytes(self._buffer), "frame exceeds maximum length")
                )
                self._buffer.clear()
                self._discarding = True
        return tuple(results)

    @staticmethod
    def _finish(escaped: bytes) -> RWDFrameResult:
        try:
            return decode_rwd_frame(escaped)
        except RWDFrameError as error:
            return RWDMalformedFrame(escaped, str(error))


def _u16(value: int) -> bytes:
    return value.to_bytes(2, "big")


def led_power(led: RWDLed, percent: float) -> RWDCommand:
    """Set the LED drive as OFRS's 0-100 % power ratio, sent in 0.01 % steps."""

    if not 0.0 <= percent <= 100.0:
        raise ValueError("LED power must be between 0 and 100 percent")
    return RWDCommand(RWDFunction.LED_VALUE, bytes([led, 0]) + _u16(round(percent * 100)))


def led_enable(led: RWDLed, enabled: bool) -> RWDCommand:
    return RWDCommand(RWDFunction.LED_ENABLE, bytes([led, int(enabled), 0, 0]))


def max_exposure_ms(frame_rate_hz: int) -> float:
    """OFRS's exposure ceiling: the integer frame period minus 2.63 ms of board dead time."""

    return (1000 // frame_rate_hz) - 2.63


def camera_timing(frame_rate_hz: int, exposure_ms: float) -> RWDCommand:
    """Set the trigger rate and exposure (sent in 10 us units) generated by the board."""

    if not 10 <= frame_rate_hz <= 250:
        raise ValueError("RWD frame rate must be between 10 and 250 Hz")
    ceiling = max_exposure_ms(frame_rate_hz)
    if not 0.5 <= exposure_ms <= ceiling + 1e-9:
        raise ValueError(f"exposure at {frame_rate_hz} Hz must be between 0.5 and {ceiling:.2f} ms")
    return RWDCommand(
        RWDFunction.CAMERA_TIMING, _u16(frame_rate_hz) + _u16(round(exposure_ms * 100))
    )


def excitation_mode(mode: RWDExcitationMode) -> RWDCommand:
    return RWDCommand(RWDFunction.EXCITATION_MODE, bytes([mode, 0, 0, 0]))


def device_enable(enabled: bool) -> RWDCommand:
    """Enable or disable the board's trigger/excitation sequencer (OFRS ``DeviceEnable``).

    OFRS disables it, re-sends and confirms every setting, then re-enables it when a
    recording starts, so that exposure counting begins at a known sequence phase.
    """

    return RWDCommand(RWDFunction.DEVICE_ENABLE, bytes([int(enabled), 0, 0, 0]))


def _require_output_port(port: int) -> None:
    if port not in range(1, 5):
        raise ValueError("RWD output port must be between 1 and 4")


def output_enable(port: int, enabled: bool) -> RWDCommand:
    _require_output_port(port)
    return RWDCommand(RWDFunction.OUTPUT_ENABLE, bytes([port, int(enabled)]))


def output_config(port: int, frequency_hz: float, pulse_width_ms: int) -> RWDCommand:
    """Configure a TTL output's pulse train (frequency in 0.01 Hz steps, width in ms)."""

    _require_output_port(port)
    if not 0.0 < frequency_hz <= 655.35:
        raise ValueError("RWD output frequency must be above 0 and at most 655.35 Hz")
    if not 0 <= pulse_width_ms <= 0xFFFF:
        raise ValueError("RWD output pulse width must be between 0 and 65535 ms")
    return RWDCommand(
        RWDFunction.OUTPUT_CONFIG,
        bytes([port]) + _u16(round(frequency_hz * 100)) + _u16(pulse_width_ms),
    )


def version_query() -> RWDCommand:
    return RWDCommand(RWDFunction.VERSION_QUERY, bytes(4))


def model_query() -> RWDCommand:
    return RWDCommand(RWDFunction.QUERY_MODEL, bytes(4))


def host_heartbeat() -> RWDCommand:
    return RWDCommand(RWDFunction.HOST_HEARTBEAT, bytes(4), RWDMessageType.INFO)


def ack_for(message: RWDMessage) -> RWDMessage:
    """OFRS acknowledges a received command or reply by echoing it with type ACK."""

    return RWDMessage(RWDMessageType.ACK, message.sequence, message.function_id, message.data)


def parse_version_reply(data: bytes) -> tuple[int, int, int]:
    if len(data) < 4:
        raise RWDFrameError("version reply is shorter than 4 bytes")
    return data[0], data[1], data[2]


def parse_version_detail(data: bytes) -> tuple[int, bool]:
    """Return the build number and whether the reply carries the ``OFRS`` magic."""

    if len(data) < 8:
        raise RWDFrameError("version detail reply is shorter than 8 bytes")
    return int.from_bytes(data[0:4], "big"), data[4:8] == RWD_VERSION_MAGIC


@dataclass(frozen=True, slots=True)
class RWDInputEdge:
    line: int
    edge: Literal["rising", "falling"]
    unconfirmed_tick: int
    """Bytes 1-4 of the payload; OFRS ignores them and their meaning is unconfirmed."""


def parse_input_edge(data: bytes) -> RWDInputEdge:
    if len(data) < 6:
        raise RWDFrameError("input edge payload is shorter than 6 bytes")
    if data[0] > 3:
        raise RWDFrameError(f"input edge reports unknown line index {data[0]}")
    edges: dict[int, Literal["rising", "falling"]] = {0: "rising", 1: "falling"}
    if data[5] not in edges:
        raise RWDFrameError(f"input edge reports unknown edge code {data[5]}")
    return RWDInputEdge(data[0] + 1, edges[data[5]], int.from_bytes(data[1:5], "big"))


_MODE_TEXT = {
    RWDExcitationMode.CONTINUOUS: "single LED, continuous",
    RWDExcitationMode.INTERLEAVED_410_470: "410+470 interleaved",
    RWDExcitationMode.INTERLEAVED_470_560: "470+560 interleaved",
    RWDExcitationMode.INTERLEAVED_410_470_560: "410+470+560 interleaved",
}


def describe_rwd_message(message: RWDMessage) -> str:
    """Best-effort diagnostic text; the raw bytes remain authoritative."""

    data, function = message.data, message.function
    if message.message_type is RWDMessageType.ACK:
        return f"ack of {message.function_name}"
    try:
        led = f"{RWDLed(data[0]).wavelength_nm} nm" if data else ""
    except ValueError:
        led = f"LED index {data[0]}"
    if function in {RWDFunction.LED_VALUE, RWDFunction.LED_VALUE_REPLY} and len(data) >= 4:
        value = int.from_bytes(data[2:4], "big")
        return f"{led} power {value / 100:.2f}%" if data[1] == 0 else f"{led} value {value}"
    if function in {RWDFunction.LED_ENABLE, RWDFunction.LED_ENABLE_REPLY} and len(data) >= 2:
        return f"{led} {'on' if data[1] else 'off'}"
    if function is RWDFunction.CAMERA_TIMING and len(data) >= 4:
        exposure = int.from_bytes(data[2:4], "big")
        return f"{int.from_bytes(data[0:2], 'big')} Hz, exposure {exposure / 100:.2f} ms"
    if function is RWDFunction.CAMERA_TIMING_REPLY and len(data) >= 4:
        fps = int.from_bytes(data[0:2], "big")
        return f"{fps} Hz, exposure bytes {data[2:4].hex(' ')} (byte order unconfirmed)"
    if function in {RWDFunction.EXCITATION_MODE, RWDFunction.EXCITATION_MODE_REPLY} and data:
        try:
            return _MODE_TEXT[RWDExcitationMode(data[0])]
        except ValueError:
            return f"unknown excitation mode {data[0]}"
    if function in {RWDFunction.DEVICE_ENABLE, RWDFunction.STATE_REPLY} and data:
        return "sequencer enabled" if data[0] else "sequencer disabled"
    if function is RWDFunction.OUTPUT_ENABLE and len(data) >= 2:
        return f"Output{data[0]} {'on' if data[1] else 'off'}"
    if function is RWDFunction.OUTPUT_CONFIG and len(data) >= 5:
        frequency = int.from_bytes(data[1:3], "big") / 100
        return f"Output{data[0]} {frequency:g} Hz, pulse {int.from_bytes(data[3:5], 'big')} ms"
    try:
        if function is RWDFunction.VERSION_REPLY:
            return "firmware {}.{}.{}".format(*parse_version_reply(data))
        if function is RWDFunction.VERSION_DETAIL_REPLY:
            build, magic_ok = parse_version_detail(data)
            return f"build {build}, OFRS magic {'present' if magic_ok else 'missing'}"
        if function is RWDFunction.INPUT_EDGE:
            edge = parse_input_edge(data)
            return f"Input{edge.line} {edge.edge} edge, bytes 1-4 = {edge.unconfirmed_tick}"
    except RWDFrameError as error:
        return f"malformed payload: {error}"
    if function in {RWDFunction.HOST_HEARTBEAT, RWDFunction.CONTROLLER_HEARTBEAT}:
        return "heartbeat"
    if function is RWDFunction.MODEL_REPLY and data:
        return f"model code {data[0]}"
    return ""
