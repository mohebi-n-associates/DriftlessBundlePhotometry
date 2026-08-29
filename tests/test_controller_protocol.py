import sys

import pytest

from driftless_photometry.hardware import (
    ControllerLifecycleGuard,
    ControllerMessage,
    ControllerState,
    MessageStreamDecoder,
    MessageType,
    ProtocolError,
    ThorlabsSDKUnavailable,
    decode_message,
    encode_message,
    require_sdk,
    sdk_status,
)


def _message(message_type: MessageType, sequence: int = 1) -> ControllerMessage:
    return ControllerMessage(message_type, sequence, {"voltage_v": 1.25})


def test_protocol_round_trip_and_fragmented_stream() -> None:
    first = _message(MessageType.EXPOSURE, 7)
    second = _message(MessageType.TTL, 8)
    encoded = encode_message(first) + encode_message(second)
    decoder = MessageStreamDecoder()

    assert decoder.feed(encoded[:5]) == ()
    assert decoder.feed(encoded[5:19]) == ()
    assert decoder.feed(encoded[19:]) == (first, second)


def test_protocol_rejects_corruption_and_unknown_version() -> None:
    encoded = bytearray(encode_message(_message(MessageType.READY)))
    encoded[10] ^= 1
    with pytest.raises(ProtocolError, match="checksum mismatch"):
        decode_message(bytes(encoded))

    wrong_version = encode_message(_message(MessageType.READY)).replace(
        b'"version":1', b'"version":2'
    )
    with pytest.raises(ProtocolError, match="checksum mismatch"):
        decode_message(wrong_version)


def test_controller_lifecycle_guard_defaults_to_safe_led_state() -> None:
    guard = ControllerLifecycleGuard()
    assert guard.leds_must_be_off
    assert guard.observe(_message(MessageType.READY)) is ControllerState.READY
    assert guard.observe(_message(MessageType.ARM)) is ControllerState.ARMED
    assert guard.observe(_message(MessageType.START)) is ControllerState.RUNNING
    assert not guard.leds_must_be_off
    assert guard.observe(_message(MessageType.EXPOSURE)) is ControllerState.RUNNING
    assert guard.observe(_message(MessageType.TTL)) is ControllerState.RUNNING
    assert guard.observe(_message(MessageType.STOP)) is ControllerState.READY
    assert guard.leds_must_be_off
    with pytest.raises(ProtocolError, match="invalid controller transition"):
        guard.observe(_message(MessageType.START))
    guard.observe(_message(MessageType.ERROR))
    assert guard.leds_must_be_off
    guard.disconnected()
    assert guard.state is ControllerState.DISCONNECTED


@pytest.mark.skipif(sys.platform == "win32", reason="non-Windows boundary test")
def test_thorlabs_sdk_is_lazy_and_fails_helpfully_off_windows() -> None:
    status = sdk_status()
    assert status.platform_supported is False
    assert status.sdk_importable is False
    with pytest.raises(ThorlabsSDKUnavailable, match="Windows"):
        require_sdk()
