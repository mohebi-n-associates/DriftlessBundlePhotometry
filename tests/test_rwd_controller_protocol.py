import pytest

from driftless_photometry.hardware import (
    ProtocolError,
    RWDExcitationMode,
    RWDFrameError,
    RWDFrameStreamDecoder,
    RWDFunction,
    RWDLed,
    RWDMalformedFrame,
    RWDMessage,
    RWDMessageType,
    decode_rwd_frame,
    encode_rwd_message,
)
from driftless_photometry.hardware.rwd_controller_protocol import (
    RWDCommand,
    ack_for,
    camera_timing,
    crc16_modbus,
    describe_rwd_message,
    device_enable,
    excitation_mode_for,
    host_heartbeat,
    led_enable,
    led_power,
    output_config,
    output_enable,
    parse_input_edge,
    parse_version_detail,
    parse_version_reply,
    version_query,
)


def _reply(function: RWDFunction, data: bytes, sequence: int = 9) -> RWDMessage:
    return RWDMessage(RWDMessageType.REPLY, sequence, function, data)


def _corrupted_sequence_frame() -> bytes:
    escaped = bytearray(encode_rwd_message(version_query().to_message(1))[1:-1])
    escaped[6] ^= 0x02
    return bytes(escaped)


def test_crc16_modbus_matches_standard_check_value() -> None:
    assert crc16_modbus(b"123456789") == 0x4B37


# Produced offline by OFRS 2.0.0.38566's own MessageConverter/FrameConverter. They pin
# byte compatibility with RWD's software; they are not bench captures from the board.
OFRS_REFERENCE_FRAMES = [
    (RWDMessageType.COMMAND, 1, 0x41, "01000BB8", "7EFF0300210100014101000BB8AE527E"),
    (RWDMessageType.COMMAND, 2, 0x42, "01010000", "7EFF03002101000242010100001F407E"),
    (RWDMessageType.COMMAND, 3, 0x49, "00000000", "7EFF030021010003490000000033B47E"),
    (
        RWDMessageType.REPLY,
        0x7E7D,
        0x29,
        "7E7D007E",
        "7EFF030021037D5E7D5D297D5E7D5D007D5EEB297E",
    ),
    (RWDMessageType.INFO, 18, 0x31, "00000000", "7EFF030021040012310000000047D77E"),
    (RWDMessageType.COMMAND, 0xFFFF, 0x48, "0107D00005", "7EFF03002101FFFF480107D00005E3BC7E"),
]


@pytest.mark.parametrize(
    ("message_type", "sequence", "function_id", "data", "frame"), OFRS_REFERENCE_FRAMES
)
def test_frames_match_ofrs_reference_encoder(
    message_type: RWDMessageType, sequence: int, function_id: int, data: str, frame: str
) -> None:
    message = RWDMessage(message_type, sequence, function_id, bytes.fromhex(data))

    assert encode_rwd_message(message) == bytes.fromhex(frame)
    assert decode_rwd_frame(bytes.fromhex(frame)[1:-1]) == message


def test_command_builders_produce_reference_payloads() -> None:
    assert led_power(RWDLed.LED_470, 30.0).to_message(1).data == bytes.fromhex("01000BB8")
    assert led_enable(RWDLed.LED_470, True).to_message(2).data == bytes.fromhex("01010000")
    assert version_query().to_message(3).data == bytes(4)
    assert output_config(1, 20.0, 5).data == bytes.fromhex("0107D00005")


def test_round_trip_escapes_flag_and_escape_bytes() -> None:
    message = RWDMessage(RWDMessageType.REPLY, 0x7E7D, 0x29, b"\x7e\x7d\x00\x7e")
    encoded = encode_rwd_message(message)

    assert encoded[0] == encoded[-1] == 0x7E
    assert 0x7E not in encoded[1:-1]
    assert decode_rwd_frame(encoded[1:-1]) == message


def test_stream_decoder_handles_fragmentation_and_flag_styles() -> None:
    first = version_query().to_message(1)
    second = host_heartbeat().to_message(2)
    separate_flags = encode_rwd_message(first) + encode_rwd_message(second)
    decoder = RWDFrameStreamDecoder()

    results = [
        result for byte in b"\x01\x02" + separate_flags for result in decoder.feed(bytes([byte]))
    ]
    assert results == [first, second]

    shared_flag = encode_rwd_message(first)[:-1] + encode_rwd_message(second)
    assert RWDFrameStreamDecoder().feed(shared_flag) == (first, second)


@pytest.mark.parametrize(
    ("escaped", "reason"),
    [
        (_corrupted_sequence_frame(), "CRC mismatch"),
        (b"\xff\x03\x00\x21\x7d\x01\x00\x00\x00\x00\x00", "invalid escape"),
        (b"\xff\x03\x00", "shorter than the minimum"),
        (b"\xff\x03\x00\x22\x01\x00\x01\x49\x00\x00", "header"),
    ],
)
def test_stream_decoder_reports_malformed_frames_and_continues(escaped: bytes, reason: str) -> None:
    valid = host_heartbeat().to_message(4)
    results = RWDFrameStreamDecoder().feed(b"\x7e" + escaped + b"\x7e" + encode_rwd_message(valid))

    assert len(results) == 2
    assert isinstance(results[0], RWDMalformedFrame)
    assert reason in results[0].reason
    assert results[1] == valid


def test_unknown_message_type_is_malformed() -> None:
    body = b"\xff\x03\x00\x21\x09\x00\x01\x49"
    escaped = body + crc16_modbus(body).to_bytes(2, "big")
    with pytest.raises(RWDFrameError, match="unknown RWD message type"):
        decode_rwd_frame(escaped)
    assert issubclass(RWDFrameError, ProtocolError)


def test_stream_decoder_bounds_oversized_frames_and_resynchronizes() -> None:
    valid = version_query().to_message(5)
    decoder = RWDFrameStreamDecoder(max_frame_bytes=32)

    results = decoder.feed(b"\x7e" + b"\x55" * 100 + b"\x7e" + encode_rwd_message(valid))

    assert isinstance(results[0], RWDMalformedFrame)
    assert results[0].reason == "frame exceeds maximum length"
    assert len(results[0].raw) == 33
    assert results[1:] == (valid,)
    with pytest.raises(ValueError, match="minimum"):
        RWDFrameStreamDecoder(max_frame_bytes=4)


@pytest.mark.parametrize(
    "function",
    [
        RWDFunction.SET_LED_LIMITS,
        RWDFunction.SET_MODEL,
        RWDFunction.RESTORE_DEFAULTS,
        RWDFunction.FIRMWARE_UPGRADE,
    ],
)
def test_unsafe_commands_are_refused_unless_explicitly_allowed(function: RWDFunction) -> None:
    message = RWDCommand(function, b"\x01").to_message(1)
    with pytest.raises(ValueError, match="unsafe"):
        encode_rwd_message(message)
    assert decode_rwd_frame(encode_rwd_message(message, allow_unsafe=True)[1:-1]) == message


def test_command_builders_encode_ofrs_units_and_validate_ranges() -> None:
    assert camera_timing(60, 13.37).data == bytes.fromhex("00 3C 05 39")
    assert output_config(1, 20.0, 5).data == bytes.fromhex("01 07 D0 00 05")
    assert output_enable(4, True).data == b"\x04\x01"
    assert device_enable(False).data == bytes(4)
    assert host_heartbeat().message_type is RWDMessageType.INFO

    for invalid in (
        lambda: led_power(RWDLed.LED_410, 100.01),
        lambda: camera_timing(60, 13.38),
        lambda: camera_timing(60, 0.49),
        lambda: camera_timing(9, 5.0),
        lambda: output_enable(0, True),
        lambda: output_config(5, 20.0, 5),
        lambda: output_config(1, 0.0, 5),
        lambda: output_config(1, 20.0, -1),
    ):
        with pytest.raises(ValueError):
            invalid()


def test_excitation_mode_selection_matches_ofrs_table() -> None:
    assert excitation_mode_for([RWDLed.LED_560]) is RWDExcitationMode.CONTINUOUS
    assert (
        excitation_mode_for([RWDLed.LED_470, RWDLed.LED_410])
        is RWDExcitationMode.INTERLEAVED_410_470
    )
    assert (
        excitation_mode_for([RWDLed.LED_560, RWDLed.LED_470])
        is RWDExcitationMode.INTERLEAVED_470_560
    )
    assert excitation_mode_for(list(RWDLed)) is RWDExcitationMode.INTERLEAVED_410_470_560
    with pytest.raises(ValueError, match="410\\+560"):
        excitation_mode_for([RWDLed.LED_410, RWDLed.LED_560])
    with pytest.raises(ValueError, match="none"):
        excitation_mode_for([])
    assert [led.wavelength_nm for led in RWDLed] == [410, 470, 560]


def test_ack_echoes_sequence_function_and_data() -> None:
    reply = _reply(RWDFunction.INPUT_EDGE, b"\x00\x00\x00\x00\x10\x01", sequence=513)
    assert ack_for(reply) == RWDMessage(RWDMessageType.ACK, 513, 0x28, reply.data)


def test_reply_parsers_and_descriptions() -> None:
    assert parse_version_reply(b"\x02\x01\x07\x00") == (2, 1, 7)
    assert parse_version_detail(b"\x00\x00\x96\xc6OFRS") == (38598, True)
    assert parse_version_detail(b"\x00\x00\x00\x01XXXX") == (1, False)
    edge = parse_input_edge(b"\x02\x00\x00\x12\x34\x01")
    assert (edge.line, edge.edge, edge.unconfirmed_tick) == (3, "falling", 0x1234)
    for payload in (b"\x00", b"\x04\x00\x00\x00\x00\x00", b"\x00\x00\x00\x00\x00\x02"):
        with pytest.raises(RWDFrameError):
            parse_input_edge(payload)
    with pytest.raises(RWDFrameError):
        parse_version_reply(b"\x01")

    assert describe_rwd_message(led_power(RWDLed.LED_470, 30.0).to_message(1)) == (
        "470 nm power 30.00%"
    )
    assert describe_rwd_message(camera_timing(100, 7.0).to_message(1)) == (
        "100 Hz, exposure 7.00 ms"
    )
    assert describe_rwd_message(output_config(2, 10.0, 15).to_message(1)) == (
        "Output2 10 Hz, pulse 15 ms"
    )
    assert describe_rwd_message(_reply(RWDFunction.INPUT_EDGE, b"\x00")) == (
        "malformed payload: input edge payload is shorter than 6 bytes"
    )
    assert describe_rwd_message(ack_for(version_query().to_message(1))) == "ack of VERSION_QUERY"
    unknown = RWDMessage(RWDMessageType.REPLY, 1, 0x99)
    assert (unknown.function, unknown.function_name, describe_rwd_message(unknown)) == (
        None,
        "0x99",
        "",
    )


def test_message_fields_are_range_checked() -> None:
    with pytest.raises(ValueError, match="16 bits"):
        RWDMessage(RWDMessageType.COMMAND, 0x10000, 0x41)
    with pytest.raises(ValueError, match="8 bits"):
        RWDMessage(RWDMessageType.COMMAND, 1, 0x100)
