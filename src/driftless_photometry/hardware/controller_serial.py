"""Single-owner serial transport for the versioned timing-controller protocol."""

from __future__ import annotations

import threading
from collections.abc import Callable
from contextlib import suppress
from typing import Protocol

from .controller_protocol import (
    PROTOCOL_VERSION,
    ControllerMessage,
    MessageStreamDecoder,
    MessageType,
    encode_message,
)


class SerialLike(Protocol):
    is_open: bool
    in_waiting: int

    def write(self, data: bytes) -> int: ...

    def read(self, size: int = 1) -> bytes: ...

    def flush(self) -> None: ...

    def close(self) -> None: ...


SerialFactory = Callable[..., SerialLike]


class SerialControllerTransport:
    """Own one serial port and make a best-effort STOP before every normal close."""

    def __init__(
        self,
        port: str,
        *,
        baudrate: int = 1_000_000,
        timeout_s: float = 0.1,
        serial_factory: SerialFactory | None = None,
    ) -> None:
        if not port:
            raise ValueError("serial port must not be empty")
        if baudrate <= 0 or timeout_s <= 0:
            raise ValueError("baudrate and timeout must be positive")
        self._port_name = port
        self._baudrate = baudrate
        self._timeout_s = timeout_s
        self._serial_factory = serial_factory
        self._serial: SerialLike | None = None
        self._decoder = MessageStreamDecoder()
        self._next_sequence = 0
        self._write_lock = threading.Lock()

    @property
    def is_open(self) -> bool:
        return self._serial is not None and self._serial.is_open

    def open(self) -> None:
        if self.is_open:
            raise RuntimeError("controller serial transport is already open")
        factory = self._serial_factory
        if factory is None:
            from serial import Serial

            factory = Serial
        self._serial = factory(
            port=self._port_name,
            baudrate=self._baudrate,
            timeout=self._timeout_s,
            write_timeout=self._timeout_s,
        )
        self.send(
            MessageType.HELLO,
            {"host": "driftless-bundle-photometry", "protocol_version": PROTOCOL_VERSION},
        )

    def send(self, message_type: MessageType, payload: dict[str, object]) -> int:
        serial_port = self._require_open()
        with self._write_lock:
            sequence = self._next_sequence
            encoded = encode_message(ControllerMessage(message_type, sequence, payload))
            written = serial_port.write(encoded)
            if written != len(encoded):
                raise OSError(f"short controller serial write: {written} of {len(encoded)} bytes")
            serial_port.flush()
            self._next_sequence += 1
            return sequence

    def read_available(self) -> tuple[ControllerMessage, ...]:
        serial_port = self._require_open()
        waiting = serial_port.in_waiting
        if waiting <= 0:
            return ()
        return self._decoder.feed(serial_port.read(waiting))

    def close(self) -> None:
        serial_port = self._serial
        if serial_port is None:
            return
        try:
            if serial_port.is_open:
                with suppress(OSError, RuntimeError):
                    self.send(MessageType.STOP, {"reason": "host_transport_close"})
        finally:
            serial_port.close()
            self._serial = None
            self._decoder = MessageStreamDecoder()

    def _require_open(self) -> SerialLike:
        if not self.is_open or self._serial is None:
            raise RuntimeError("controller serial transport is not open")
        return self._serial

    def __enter__(self) -> SerialControllerTransport:
        self.open()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()
