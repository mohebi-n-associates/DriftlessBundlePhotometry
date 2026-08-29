"""Versioned, checksummed controller wire protocol and lifecycle guard."""

from __future__ import annotations

import json
import zlib
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

PROTOCOL_VERSION = 1
_PREFIX = b"DBP1|"
_MAX_LINE_BYTES = 16_384


class ProtocolError(RuntimeError):
    """A controller message failed framing, checksum, or schema validation."""


class MessageType(StrEnum):
    HELLO = "HELLO"
    READY = "READY"
    CONFIG = "CONFIG"
    ACK = "ACK"
    ARM = "ARM"
    START = "START"
    STOP = "STOP"
    ERROR = "ERROR"
    HEARTBEAT = "HEARTBEAT"
    EXPOSURE = "EXPOSURE"
    TTL = "TTL"


class ControllerState(StrEnum):
    DISCONNECTED = "DISCONNECTED"
    READY = "READY"
    ARMED = "ARMED"
    RUNNING = "RUNNING"
    ERROR = "ERROR"


@dataclass(frozen=True, slots=True)
class ControllerMessage:
    message_type: MessageType
    sequence: int
    payload: dict[str, Any]

    def __post_init__(self) -> None:
        if self.sequence < 0:
            raise ValueError("message sequence must be non-negative")


def encode_message(message: ControllerMessage) -> bytes:
    """Encode one canonical UTF-8 JSON message with a CRC32 and newline delimiter."""

    document = {
        "payload": message.payload,
        "sequence": message.sequence,
        "type": message.message_type.value,
        "version": PROTOCOL_VERSION,
    }
    body = json.dumps(document, sort_keys=True, separators=(",", ":")).encode("utf-8")
    checksum = f"{zlib.crc32(body) & 0xFFFFFFFF:08X}".encode("ascii")
    return _PREFIX + body + b"|" + checksum + b"\n"


def decode_message(line: bytes) -> ControllerMessage:
    """Decode and validate one complete wire line."""

    if len(line) > _MAX_LINE_BYTES:
        raise ProtocolError("controller message exceeds maximum length")
    stripped = line.rstrip(b"\r\n")
    if not stripped.startswith(_PREFIX):
        raise ProtocolError("controller message has an invalid prefix")
    try:
        body, checksum_text = stripped[len(_PREFIX) :].rsplit(b"|", 1)
    except ValueError as error:
        raise ProtocolError("controller message has no checksum") from error
    try:
        received_checksum = int(checksum_text, 16)
    except ValueError as error:
        raise ProtocolError("controller message checksum is not hexadecimal") from error
    expected_checksum = zlib.crc32(body) & 0xFFFFFFFF
    if received_checksum != expected_checksum:
        raise ProtocolError("controller message checksum mismatch")
    try:
        document = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ProtocolError("controller message is not valid UTF-8 JSON") from error
    if not isinstance(document, dict) or set(document) != {
        "payload",
        "sequence",
        "type",
        "version",
    }:
        raise ProtocolError("controller message schema is invalid")
    if document["version"] != PROTOCOL_VERSION:
        raise ProtocolError(f"unsupported controller protocol version: {document['version']}")
    if not isinstance(document["sequence"], int) or isinstance(document["sequence"], bool):
        raise ProtocolError("controller message sequence must be an integer")
    if document["sequence"] < 0:
        raise ProtocolError("controller message sequence must be non-negative")
    if not isinstance(document["payload"], dict):
        raise ProtocolError("controller message payload must be an object")
    try:
        message_type = MessageType(document["type"])
    except ValueError as error:
        raise ProtocolError(f"unknown controller message type: {document['type']}") from error
    return ControllerMessage(message_type, document["sequence"], document["payload"])


class MessageStreamDecoder:
    """Incrementally decode fragmented serial input without unbounded buffering."""

    def __init__(self) -> None:
        self._buffer = bytearray()

    def feed(self, data: bytes) -> tuple[ControllerMessage, ...]:
        self._buffer.extend(data)
        if len(self._buffer) > _MAX_LINE_BYTES and b"\n" not in self._buffer:
            self._buffer.clear()
            raise ProtocolError("unterminated controller message exceeds maximum length")
        messages: list[ControllerMessage] = []
        while True:
            delimiter = self._buffer.find(b"\n")
            if delimiter < 0:
                break
            line = bytes(self._buffer[: delimiter + 1])
            del self._buffer[: delimiter + 1]
            if line.strip():
                messages.append(decode_message(line))
        return tuple(messages)


class ControllerLifecycleGuard:
    """Reject unsafe or out-of-order controller state notifications."""

    def __init__(self) -> None:
        self.state = ControllerState.DISCONNECTED

    @property
    def leds_must_be_off(self) -> bool:
        return self.state is not ControllerState.RUNNING

    def observe(self, message: ControllerMessage) -> ControllerState:
        transitions = {
            (ControllerState.DISCONNECTED, MessageType.READY): ControllerState.READY,
            (ControllerState.READY, MessageType.ACK): ControllerState.READY,
            (ControllerState.READY, MessageType.ARM): ControllerState.ARMED,
            (ControllerState.ARMED, MessageType.START): ControllerState.RUNNING,
            (ControllerState.ARMED, MessageType.STOP): ControllerState.READY,
            (ControllerState.RUNNING, MessageType.STOP): ControllerState.READY,
        }
        if message.message_type is MessageType.ERROR:
            self.state = ControllerState.ERROR
            return self.state
        if message.message_type in {MessageType.HELLO, MessageType.HEARTBEAT}:
            return self.state
        if message.message_type in {MessageType.EXPOSURE, MessageType.TTL}:
            if self.state is not ControllerState.RUNNING:
                raise ProtocolError(
                    f"received {message.message_type} while controller is {self.state}"
                )
            return self.state
        target = transitions.get((self.state, message.message_type))
        if target is None:
            raise ProtocolError(
                f"invalid controller transition: {self.state} + {message.message_type}"
            )
        self.state = target
        return self.state

    def disconnected(self) -> None:
        self.state = ControllerState.DISCONNECTED
