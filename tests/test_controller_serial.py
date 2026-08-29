from collections.abc import Callable

from driftless_photometry.hardware import (
    ControllerMessage,
    MessageType,
    SerialControllerTransport,
    decode_message,
    encode_message,
)


class _FakeSerial:
    def __init__(self, incoming: bytes = b"") -> None:
        self.is_open = True
        self.incoming = bytearray(incoming)
        self.writes: list[bytes] = []
        self.flush_count = 0

    @property
    def in_waiting(self) -> int:
        return len(self.incoming)

    def write(self, data: bytes) -> int:
        self.writes.append(data)
        return len(data)

    def read(self, size: int = 1) -> bytes:
        result = bytes(self.incoming[:size])
        del self.incoming[:size]
        return result

    def flush(self) -> None:
        self.flush_count += 1

    def close(self) -> None:
        self.is_open = False


def _factory(fake: _FakeSerial) -> Callable[..., _FakeSerial]:
    def make_serial(**kwargs) -> _FakeSerial:
        assert kwargs == {
            "port": "COM7",
            "baudrate": 1_000_000,
            "timeout": 0.1,
            "write_timeout": 0.1,
        }
        return fake

    return make_serial


def test_transport_hello_read_and_safe_close() -> None:
    ready = ControllerMessage(MessageType.READY, 10, {"firmware": "test"})
    fake = _FakeSerial(encode_message(ready))
    transport = SerialControllerTransport("COM7", serial_factory=_factory(fake))

    transport.open()
    assert decode_message(fake.writes[0]).message_type is MessageType.HELLO
    assert transport.read_available() == (ready,)
    assert transport.send(MessageType.CONFIG, {"fps": 30}) == 1
    transport.close()

    assert decode_message(fake.writes[1]).message_type is MessageType.CONFIG
    assert decode_message(fake.writes[2]).message_type is MessageType.STOP
    assert fake.is_open is False
    assert transport.is_open is False


def test_transport_context_closes_after_error() -> None:
    fake = _FakeSerial()
    try:
        with SerialControllerTransport("COM7", serial_factory=_factory(fake)) as transport:
            assert transport.read_available() == ()
            raise ValueError("application failure")
    except ValueError:
        pass
    assert decode_message(fake.writes[-1]).message_type is MessageType.STOP
    assert fake.is_open is False
