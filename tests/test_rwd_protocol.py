import struct
from pathlib import Path

import pytest

from driftless_photometry.config import RWDPreambleMode
from driftless_photometry.rwd import (
    EVENT_RECORD_BYTES,
    FLUORESCENCE_RECORD_BYTES,
    RWDEventRecord,
    RWDFluorescenceRecord,
    RWDProtocolError,
    RWDStreamDecoder,
    RWDWavelength,
    parse_event_record,
    parse_fluorescence_record,
)

_FIXTURES = Path(__file__).parent / "fixtures" / "rwd"


def _fixture(name: str) -> bytes:
    return bytes.fromhex((_FIXTURES / name).read_text(encoding="ascii"))


def _fluorescence_record(
    mask: int,
    *,
    machine_name: bytes = b"RWD1",
    channel: int = 2,
) -> bytes:
    return b"".join(
        (
            machine_name,
            bytes((_FLUORESCENCE_TYPE, mask, channel)),
            struct.pack("<II", 4100, 410_000),
            struct.pack("<II", 4700, 470_000),
            struct.pack("<II", 5600, 560_000),
        )
    )


_FLUORESCENCE_TYPE = 0x01


def test_parse_golden_fluorescence_record_preserves_exact_wire_identity() -> None:
    raw = _fixture("fluorescence_all.hex")

    record = parse_fluorescence_record(raw)

    assert len(raw) == FLUORESCENCE_RECORD_BYTES
    assert record.machine_name == b"RWD1"
    assert record.device_channel == 2
    assert [sample.wavelength_nm for sample in record.samples] == [
        RWDWavelength.LED_410,
        RWDWavelength.LED_470,
        RWDWavelength.LED_560,
    ]
    assert [sample.timestamp_tick for sample in record.samples] == [0x01020304, 0, 2**32 - 1]
    assert [sample.raw_value for sample in record.samples] == [1234, 5678, 42]
    assert [sample.scaled_value for sample in record.samples] == pytest.approx(
        [1.234, 5.678, 0.042]
    )


def test_parse_golden_event_record_preserves_fixed_name_and_edge() -> None:
    raw = _fixture("event_on.hex")

    event = parse_event_record(raw)

    assert len(raw) == EVENT_RECORD_BYTES
    assert event.machine_name == b"RWD1"
    assert event.timestamp_tick == 42
    assert event.event_name == "lever press"
    assert event.active is True
    assert len(event.event_name_raw) == 20


@pytest.mark.parametrize(
    ("mask", "expected"),
    [
        (0x01, [RWDWavelength.LED_410]),
        (0x02, [RWDWavelength.LED_470]),
        (0x04, [RWDWavelength.LED_560]),
        (0x03, [RWDWavelength.LED_410, RWDWavelength.LED_470]),
        (0x05, [RWDWavelength.LED_410, RWDWavelength.LED_560]),
        (0x06, [RWDWavelength.LED_470, RWDWavelength.LED_560]),
        (0x07, list(RWDWavelength)),
    ],
)
def test_every_documented_wavelength_mask_emits_only_present_slots(
    mask: int,
    expected: list[RWDWavelength],
) -> None:
    record = parse_fluorescence_record(_fluorescence_record(mask))

    assert [sample.wavelength_nm for sample in record.samples] == expected


def test_incremental_decoder_handles_every_single_byte_fragment() -> None:
    payload = _fixture("fluorescence_all.hex") + _fixture("event_on.hex")
    decoder = RWDStreamDecoder(preamble_mode=RWDPreambleMode.NONE)
    records = []

    for value in payload:
        records.extend(decoder.feed(bytes((value,))))
    decoder.finish()

    assert [type(record) for record in records] == [RWDFluorescenceRecord, RWDEventRecord]
    assert decoder.diagnostics.bytes_received == len(payload)
    assert decoder.diagnostics.buffered_bytes == 0
    assert decoder.diagnostics.records_decoded == 2


def test_auto_preamble_and_coalesced_records_are_resolved_without_read_boundaries() -> None:
    payload = _fixture("mixed_with_banner.hex")
    decoder = RWDStreamDecoder(
        preamble_mode=RWDPreambleMode.AUTO,
        allowed_channels=frozenset({2}),
        expected_machine_name=b"RWD1",
    )

    records = list(decoder.feed(payload))
    decoder.finish()

    assert len(records) == 2
    assert decoder.diagnostics.resolved_preamble_mode is RWDPreambleMode.MACHINE_NAME_4
    assert decoder.diagnostics.preamble == b"RWD1"
    assert decoder.diagnostics.machine_name == b"RWD1"
    assert decoder.diagnostics.fluorescence_records == 1
    assert decoder.diagnostics.event_records == 1


def test_auto_mode_detects_normal_record_without_preamble() -> None:
    decoder = RWDStreamDecoder(preamble_mode=RWDPreambleMode.AUTO)

    records = list(decoder.feed(_fixture("fluorescence_all.hex")))

    assert len(records) == 1
    assert decoder.diagnostics.resolved_preamble_mode is RWDPreambleMode.NONE
    assert decoder.diagnostics.preamble is None


def test_explicit_preamble_waits_for_exact_banner_then_record() -> None:
    decoder = RWDStreamDecoder(
        preamble_mode=RWDPreambleMode.MACHINE_NAME_4,
        expected_machine_name=b"RWD1",
    )

    assert list(decoder.feed(b"RW")) == []
    assert list(decoder.feed(b"D1")) == []
    records = list(decoder.feed(_fixture("event_on.hex")))

    assert len(records) == 1
    assert decoder.diagnostics.preamble == b"RWD1"


@pytest.mark.parametrize(
    ("record", "message"),
    [
        (_fluorescence_record(0x00), "no present wavelength"),
        (_fluorescence_record(0x81), "reserved bits"),
        (b"RWD1\x01", "must be 31 bytes"),
    ],
)
def test_fluorescence_parser_rejects_malformed_records(record: bytes, message: str) -> None:
    with pytest.raises(RWDProtocolError, match=message):
        parse_fluorescence_record(record)


def test_parser_rejects_unknown_channel_and_disabled_wavelength() -> None:
    with pytest.raises(RWDProtocolError, match=r"unmapped.*7"):
        parse_fluorescence_record(
            _fluorescence_record(0x01, channel=7),
            allowed_channels=frozenset({2}),
        )
    with pytest.raises(RWDProtocolError, match="disabled wavelength: 560"):
        parse_fluorescence_record(
            _fluorescence_record(0x04),
            enabled_wavelengths=frozenset({RWDWavelength.LED_410}),
        )


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ((29, 2), "status"),
        ((9, 0xFF), "not ASCII"),
    ],
)
def test_event_parser_rejects_invalid_status_and_name(
    mutation: tuple[int, int],
    message: str,
) -> None:
    raw = bytearray(_fixture("event_on.hex"))
    raw[mutation[0]] = mutation[1]

    with pytest.raises(RWDProtocolError, match=message):
        parse_event_record(bytes(raw))


def test_event_parser_rejects_empty_padded_name() -> None:
    raw = bytearray(_fixture("event_on.hex"))
    raw[9:29] = b"\x00" * 20

    with pytest.raises(RWDProtocolError, match="name is empty"):
        parse_event_record(bytes(raw))


def test_event_parser_rejects_control_bytes_inside_name() -> None:
    raw = bytearray(_fixture("event_on.hex"))
    raw[14] = 0

    with pytest.raises(RWDProtocolError, match="non-printable ASCII"):
        parse_event_record(bytes(raw))


def test_valid_records_before_malformed_type_are_yielded_before_fail_closed_error() -> None:
    payload = _fixture("event_on.hex") + b"RWD1\x99"
    decoder = RWDStreamDecoder(preamble_mode=RWDPreambleMode.NONE)
    decoded = decoder.feed(payload)

    first = next(decoded)
    assert isinstance(first, RWDEventRecord)
    with pytest.raises(RWDProtocolError, match="unknown RWD record type"):
        next(decoded)

    assert decoder.diagnostics.records_decoded == 1
    assert decoder.diagnostics.failed is True
    with pytest.raises(RWDProtocolError, match="failed"):
        list(decoder.feed(b""))


def test_decoder_surfaces_truncated_tail_at_eof_after_complete_records() -> None:
    payload = _fixture("fluorescence_all.hex") + _fixture("event_on.hex")[:-1]
    decoder = RWDStreamDecoder(preamble_mode=RWDPreambleMode.NONE)

    records = list(decoder.feed(payload))

    assert len(records) == 1
    assert decoder.diagnostics.buffered_bytes == EVENT_RECORD_BYTES - 1
    with pytest.raises(RWDProtocolError, match=r"truncated.*29 buffered"):
        decoder.finish()


def test_machine_name_is_stable_across_preamble_and_records() -> None:
    decoder = RWDStreamDecoder(
        preamble_mode=RWDPreambleMode.MACHINE_NAME_4,
        expected_machine_name=b"RWD1",
    )
    payload = b"RWD1" + _fluorescence_record(0x01, machine_name=b"RWD2")

    with pytest.raises(RWDProtocolError, match="machine name mismatch"):
        list(decoder.feed(payload))


def test_decoder_rejects_unexhausted_feed_iterator() -> None:
    decoder = RWDStreamDecoder(preamble_mode=RWDPreambleMode.NONE)
    first_feed = decoder.feed(_fixture("event_on.hex") + _fixture("event_on.hex"))
    assert isinstance(next(first_feed), RWDEventRecord)

    with pytest.raises(RuntimeError, match="not exhausted"):
        list(decoder.feed(b""))

    first_feed.close()
