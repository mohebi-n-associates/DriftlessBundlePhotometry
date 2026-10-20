"""Single-owner serial transport for the RWD photometry controller.

Behavior follows bench measurements on an R821 (firmware 2.0.0, build 37619); see
``docs/rwd-direct-control.md``. The board has no watchdog or link-loss fail-safe, so
this transport sends the full off sequence after its handshake and again on every
close, including after errors.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from typing import Protocol

from driftless_photometry.faults import AcquisitionFault, AcquisitionFaultCode

from .rwd_controller_protocol import (
    RWD_CONTROLLER_BAUDRATE,
    RWD_MODEL_CODES,
    RWDCommand,
    RWDFrameError,
    RWDFrameStreamDecoder,
    RWDFunction,
    RWDInputEdge,
    RWDLed,
    RWDMalformedFrame,
    RWDMessage,
    RWDMessageType,
    ack_for,
    device_enable,
    encode_rwd_message,
    host_heartbeat,
    led_enable,
    led_power,
    model_query,
    output_enable,
    parse_input_edge,
    parse_version_detail,
    parse_version_reply,
    version_query,
)

RWD_OUTPUT_PORTS = (1, 2, 3, 4)
# Each setting command is confirmed by a reply whose function ID is 0x20 lower (bench).
_CONFIRMATION = {
    RWDFunction.LED_VALUE: RWDFunction.LED_VALUE_REPLY,
    RWDFunction.LED_ENABLE: RWDFunction.LED_ENABLE_REPLY,
    RWDFunction.CAMERA_TIMING: RWDFunction.CAMERA_TIMING_REPLY,
    RWDFunction.EXCITATION_MODE: RWDFunction.EXCITATION_MODE_REPLY,
    RWDFunction.DEVICE_ENABLE: RWDFunction.STATE_REPLY,
}


class RWDSerialLike(Protocol):
    is_open: bool
    in_waiting: int

    def write(self, data: bytes) -> int | None: ...

    def read(self, size: int = 1) -> bytes: ...

    def flush(self) -> None: ...

    def close(self) -> None: ...


RWDSerialFactory = Callable[[str], RWDSerialLike]


def open_rwd_serial(port: str) -> RWDSerialLike:
    """Open the controller's COM port at 115200 8N1 with DTR and RTS held low, like OFRS."""

    from serial import Serial

    link = Serial()
    link.port = port
    link.baudrate = RWD_CONTROLLER_BAUDRATE
    link.bytesize, link.parity, link.stopbits = 8, "N", 1
    link.timeout = 0.02
    link.write_timeout = 0.5
    link.dtr = False
    link.rts = False
    link.open()
    return link


def candidate_rwd_ports() -> tuple[str, ...]:
    """COM ports behind an FTDI FT232R bridge; only the handshake proves an RWD controller."""

    from serial.tools import list_ports

    return tuple(
        sorted(
            port.device
            for port in list_ports.comports()
            if (port.vid, port.pid) == (0x0403, 0x6001)
        )
    )


@dataclass(frozen=True, slots=True)
class RWDControllerIdentity:
    firmware: tuple[int, int, int]
    build: int
    model_code: int

    @property
    def model_name(self) -> str:
        return RWD_MODEL_CODES.get(self.model_code, f"unknown model code {self.model_code}")


@dataclass(frozen=True, slots=True)
class RWDReceivedEdge:
    host_s: float
    edge: RWDInputEdge


@dataclass(frozen=True, slots=True)
class RWDPollResult:
    edges: tuple[RWDReceivedEdge, ...] = ()
    malformed: tuple[RWDMalformedFrame, ...] = ()


class RWDControllerTransport:
    """Own one RWD controller link. Every method must be called from the same thread."""

    def __init__(
        self,
        port: str,
        *,
        serial_factory: RWDSerialFactory = open_rwd_serial,
        ack_timeout_s: float = 0.8,
        resends: int = 3,
        handshake_timeout_s: float = 3.0,
        host_heartbeat_interval_s: float = 1.0,
        controller_silence_limit_s: float = 5.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not port:
            raise ValueError("serial port must not be empty")
        if min(ack_timeout_s, handshake_timeout_s, host_heartbeat_interval_s) <= 0:
            raise ValueError("timeouts and intervals must be positive")
        if controller_silence_limit_s <= host_heartbeat_interval_s:
            raise ValueError("controller silence limit must exceed the heartbeat interval")
        if resends < 0:
            raise ValueError("resends must be non-negative")
        self._port_name = port
        self._serial_factory = serial_factory
        self._ack_timeout_s = ack_timeout_s
        self._resends = resends
        self._handshake_timeout_s = handshake_timeout_s
        self._heartbeat_interval_s = host_heartbeat_interval_s
        self._silence_limit_s = controller_silence_limit_s
        self._clock = clock
        self._serial: RWDSerialLike | None = None
        self._decoder = RWDFrameStreamDecoder()
        self._sequence = 0
        self._acked: set[int] = set()
        self._replies: list[RWDMessage] = []
        self._edges: list[RWDReceivedEdge] = []
        self._malformed: list[RWDMalformedFrame] = []
        self._last_controller_frame_s = 0.0
        self._last_host_heartbeat_s = 0.0
        self.identity: RWDControllerIdentity | None = None

    @property
    def is_open(self) -> bool:
        return self._serial is not None and self._serial.is_open

    def open(self) -> RWDControllerIdentity:
        """Connect, verify the controller identity, and force a known all-off state."""

        if self.is_open:
            raise RuntimeError("RWD controller transport is already open")
        try:
            self._serial = self._serial_factory(self._port_name)
        except (OSError, ValueError) as error:
            raise AcquisitionFault(
                AcquisitionFaultCode.CONTROLLER_DISCONNECT,
                f"could not open RWD controller port {self._port_name}: {error}",
            ) from error
        try:
            self._last_controller_frame_s = self._last_host_heartbeat_s = self._clock()
            self.identity = self._handshake()
            self.all_off()
        except BaseException:
            self.close()
            raise
        return self.identity

    def command(self, body: RWDCommand) -> None:
        """Send one command, resending until acknowledged and, where known, confirmed."""

        self._sequence = (self._sequence + 1) & 0xFFFF
        message = body.to_message(self._sequence)
        expected = _CONFIRMATION.get(body.function)
        for _attempt in range(self._resends + 1):
            self._replies.clear()
            self._acked.discard(message.sequence)
            self._write(message)
            deadline = self._clock() + self._ack_timeout_s
            while self._clock() < deadline:
                self._read_once()
                if message.sequence in self._acked and (
                    expected is None or self._confirmation(expected) is not None
                ):
                    self._acked.discard(message.sequence)
                    self._check_confirmation(message, self._confirmation(expected))
                    return
        raise AcquisitionFault(
            AcquisitionFaultCode.CONTROLLER_DISCONNECT,
            f"RWD controller did not acknowledge {message.function_name} "
            f"after {self._resends + 1} attempts",
        )

    def poll(self) -> RWDPollResult:
        """Read pending input, keep the link alive, and return edges and malformed frames."""

        self._read_once()
        now = self._clock()
        if now - self._last_host_heartbeat_s >= self._heartbeat_interval_s:
            self._sequence = (self._sequence + 1) & 0xFFFF
            self._write(host_heartbeat().to_message(self._sequence))
            self._last_host_heartbeat_s = now
        if now - self._last_controller_frame_s > self._silence_limit_s:
            raise AcquisitionFault(
                AcquisitionFaultCode.CONTROLLER_DISCONNECT,
                f"RWD controller sent nothing for more than {self._silence_limit_s:g} s",
            )
        result = RWDPollResult(tuple(self._edges), tuple(self._malformed))
        self._edges.clear()
        self._malformed.clear()
        self._replies.clear()
        self._acked.clear()
        return result

    def all_off(self) -> None:
        """Stop the sequencer, every TTL output, and every LED (enable off, power 0 %)."""

        self.command(device_enable(False))
        for port in RWD_OUTPUT_PORTS:
            self.command(output_enable(port, False))
        for led in RWDLed:
            self.command(led_enable(led, False))
            self.command(led_power(led, 0.0))

    def close(self) -> None:
        """Send the off sequence (best effort) and release the port, even after errors.

        The sequencer disable goes first. If the board does not acknowledge it, the
        remaining off commands are skipped rather than waiting on a dead link.
        """

        link = self._serial
        if link is None:
            return
        try:
            if link.is_open:
                with suppress(AcquisitionFault, OSError):
                    self.all_off()
        finally:
            try:
                link.close()
            finally:
                self._serial = None
                self._decoder = RWDFrameStreamDecoder()

    def __enter__(self) -> RWDControllerTransport:
        self.open()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def _handshake(self) -> RWDControllerIdentity:
        self._replies.clear()
        for body in (version_query(), model_query()):
            self._sequence += 1
            self._write(body.to_message(self._sequence))
        deadline = self._clock() + self._handshake_timeout_s
        while self._clock() < deadline:
            self._read_once()
            version = self._confirmation(RWDFunction.VERSION_REPLY)
            detail = self._confirmation(RWDFunction.VERSION_DETAIL_REPLY)
            model = self._confirmation(RWDFunction.MODEL_REPLY)
            if version and detail and model:
                try:
                    firmware = parse_version_reply(version.data)
                    build, magic_ok = parse_version_detail(detail.data)
                except RWDFrameError as error:
                    raise AcquisitionFault(
                        AcquisitionFaultCode.MALFORMED_CONTROLLER_STREAM, str(error)
                    ) from error
                if not magic_ok or not model.data:
                    raise AcquisitionFault(
                        AcquisitionFaultCode.MALFORMED_CONTROLLER_STREAM,
                        f"{self._port_name} answered but is not an RWD photometry controller",
                    )
                return RWDControllerIdentity(firmware, build, model.data[0])
        raise AcquisitionFault(
            AcquisitionFaultCode.CONTROLLER_DISCONNECT,
            f"no RWD controller handshake on {self._port_name} within "
            f"{self._handshake_timeout_s:g} s",
        )

    def _confirmation(self, function: RWDFunction | None) -> RWDMessage | None:
        return next((reply for reply in self._replies if reply.function is function), None)

    @staticmethod
    def _check_confirmation(sent: RWDMessage, reply: RWDMessage | None) -> None:
        if reply is None:
            return
        expected = sent.data
        if sent.function is RWDFunction.LED_ENABLE:
            expected = sent.data[:2]
        elif sent.function is RWDFunction.CAMERA_TIMING:
            # The board reports the exposure bytes swapped; it applies them as sent (bench).
            expected = sent.data[:2] + sent.data[2:4][::-1]
        if reply.data != expected:
            raise AcquisitionFault(
                AcquisitionFaultCode.MALFORMED_CONTROLLER_STREAM,
                f"RWD controller confirmed {sent.function_name} as {reply.data.hex(' ')}, "
                f"expected {expected.hex(' ')}",
            )

    def _write(self, message: RWDMessage) -> None:
        link = self._require_open()
        frame = encode_rwd_message(message)
        try:
            link.write(frame)
            link.flush()
        except OSError as error:
            raise AcquisitionFault(
                AcquisitionFaultCode.CONTROLLER_DISCONNECT,
                f"RWD controller link failed while writing: {error}",
            ) from error

    def _read_once(self) -> None:
        link = self._require_open()
        try:
            chunk = link.read(max(1, link.in_waiting))
        except OSError as error:
            raise AcquisitionFault(
                AcquisitionFaultCode.CONTROLLER_DISCONNECT,
                f"RWD controller link failed while reading: {error}",
            ) from error
        if not chunk:
            return
        now = self._clock()
        for result in self._decoder.feed(chunk):
            if isinstance(result, RWDMalformedFrame):
                self._malformed.append(result)
                continue
            self._last_controller_frame_s = now
            self._dispatch(result, now)

    def _dispatch(self, message: RWDMessage, now: float) -> None:
        if message.message_type is RWDMessageType.ACK:
            self._acked.add(message.sequence)
            return
        if message.message_type in {RWDMessageType.COMMAND, RWDMessageType.REPLY}:
            self._write(ack_for(message))
        if message.function is RWDFunction.INPUT_EDGE:
            try:
                self._edges.append(RWDReceivedEdge(now, parse_input_edge(message.data)))
            except RWDFrameError as error:
                self._malformed.append(RWDMalformedFrame(message.data, str(error)))
        elif message.message_type is RWDMessageType.REPLY:
            self._replies.append(message)

    def _require_open(self) -> RWDSerialLike:
        if self._serial is None or not self._serial.is_open:
            raise AcquisitionFault(
                AcquisitionFaultCode.CONTROLLER_DISCONNECT, "RWD controller transport is not open"
            )
        return self._serial
