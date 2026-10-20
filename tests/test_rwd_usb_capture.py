import csv
import json
import random
import struct
from pathlib import Path

import pytest

from driftless_photometry.cli import main
from driftless_photometry.hardware import (
    RWDFunction,
    RWDLed,
    RWDMalformedFrame,
    RWDMessage,
    RWDMessageType,
    RWDUSBCaptureError,
    decode_rwd_usb_capture,
    encode_rwd_message,
    write_rwd_usb_decode_csv,
)
from driftless_photometry.hardware.rwd_controller_protocol import (
    ack_for,
    device_enable,
    led_power,
    version_query,
)
from driftless_photometry.hardware.rwd_usb_capture import (
    RWDLinkDirection,
    read_usbpcap_packets,
    strip_ftdi_status,
)

CONTROLLER = (1, 5)
CAMERA = (1, 7)
_IN, _OUT = 0x81, 0x02
_SUBMIT, _COMPLETE = 0, 1


def _reply(function: RWDFunction, sequence: int, data: bytes) -> RWDMessage:
    return RWDMessage(RWDMessageType.REPLY, sequence, function, data)


VERSION = _reply(RWDFunction.VERSION_REPLY, 16, b"\x02\x00\x01\x00")
VERSION_DETAIL = _reply(RWDFunction.VERSION_DETAIL_REPLY, 17, b"\x00\x00\x96\xc6OFRS")
HEARTBEAT = RWDMessage(RWDMessageType.INFO, 18, RWDFunction.CONTROLLER_HEARTBEAT, bytes(4))
EDGE = _reply(RWDFunction.INPUT_EDGE, 19, b"\x00\x00\x00\x12\x34\x00")
QUERY = version_query().to_message(1)
ESCAPED_START = device_enable(True).to_message(0x7E7D)
POWER = led_power(RWDLed.LED_470, 30.0).to_message(3)


def _usbpcap(device: tuple[int, int], endpoint: int, info: int, payload: bytes = b"") -> bytes:
    bus, address = device
    header = struct.pack("<HQIHBHHBBI", 27, 1, 0, 9, info, bus, address, endpoint, 3, len(payload))
    return header + payload


def _ftdi_in(data: bytes, packet_size: int) -> bytes:
    chunks = [data[i : i + packet_size - 2] for i in range(0, len(data), packet_size - 2)]
    return b"".join(b"\x01\x60" + chunk for chunk in chunks or [b""])


def _corrupt(frame: bytes) -> bytes:
    damaged = bytearray(frame)
    damaged[7] ^= 0x02
    return bytes(damaged)


def _session(packet_size: int) -> list[tuple[float, bytes]]:
    """A synthetic handshake plus noise from a second high-rate bulk device."""

    noise = random.Random(7)
    replies = encode_rwd_message(VERSION) + encode_rwd_message(VERSION_DETAIL)
    heartbeat = encode_rwd_message(HEARTBEAT)
    burst = (
        encode_rwd_message(EDGE)
        + _corrupt(encode_rwd_message(HEARTBEAT))
        + encode_rwd_message(HEARTBEAT) * 40
    )
    return [
        (1.000, _usbpcap(CONTROLLER, _OUT, _SUBMIT, encode_rwd_message(QUERY))),
        (1.001, _usbpcap(CONTROLLER, _OUT, _COMPLETE)),
        (1.005, _usbpcap(CAMERA, _IN, _COMPLETE, bytes(noise.randrange(256) for _ in range(4096)))),
        (1.010, _usbpcap(CONTROLLER, _IN, _COMPLETE, b"\x01\x60")),
        (1.020, _usbpcap(CONTROLLER, _IN, _COMPLETE, _ftdi_in(replies, packet_size))),
        (1.030, _usbpcap(CONTROLLER, _IN, _COMPLETE, _ftdi_in(heartbeat[:7], packet_size))),
        (1.040, _usbpcap(CONTROLLER, _IN, _COMPLETE, _ftdi_in(heartbeat[7:], packet_size))),
        (1.045, _usbpcap(CAMERA, _OUT, _SUBMIT, b"\x7e\x00\x01\x7e")),
        (
            1.050,
            _usbpcap(
                CONTROLLER,
                _OUT,
                _SUBMIT,
                encode_rwd_message(ack_for(VERSION)) + encode_rwd_message(ESCAPED_START),
            ),
        ),
        (1.055, _usbpcap(CONTROLLER, _OUT, _SUBMIT, encode_rwd_message(POWER))),
        (1.060, _usbpcap(CONTROLLER, _IN, _COMPLETE, _ftdi_in(burst, packet_size))),
    ]


def _block(block_type: int, body: bytes) -> bytes:
    body += b"\0" * (-len(body) % 4)
    total = len(body) + 12
    return struct.pack("<II", block_type, total) + body + struct.pack("<I", total)


def _write_pcapng(path: Path, packets: list[tuple[float, bytes]]) -> Path:
    blocks = [
        _block(0x0A0D0D0A, struct.pack("<IHHq", 0x1A2B3C4D, 1, 0, -1)),
        _block(1, struct.pack("<HHI", 1, 0, 65535)),
        _block(1, struct.pack("<HHI", 249, 0, 65535) + struct.pack("<HHB3x", 9, 1, 9) + bytes(4)),
        _block(6, struct.pack("<IIIII", 0, 0, 0, 4, 4) + b"\xde\xad\xbe\xef"),
    ]
    for timestamp_s, data in packets:
        ticks = round(timestamp_s * 1e9)
        header = struct.pack("<IIIII", 1, ticks >> 32, ticks & 0xFFFFFFFF, len(data), len(data))
        blocks.append(_block(6, header + data))
    path.write_bytes(b"".join(blocks))
    return path


def _write_pcap(path: Path, packets: list[tuple[float, bytes]]) -> Path:
    records = [struct.pack("<IHHiIII", 0xA1B2C3D4, 2, 4, 0, 0, 65535, 249)]
    for timestamp_s, data in packets:
        seconds = int(timestamp_s)
        micros = round((timestamp_s - seconds) * 1e6)
        records.append(struct.pack("<IIII", seconds, micros, len(data), len(data)) + data)
    path.write_bytes(b"".join(records))
    return path


def _messages(decode, direction: RWDLinkDirection) -> list[object]:
    return [frame.result for frame in decode.frames if frame.direction is direction]


def test_decodes_pcapng_session_and_selects_controller_device(tmp_path: Path) -> None:
    decode = decode_rwd_usb_capture(_write_pcapng(tmp_path / "session.pcapng", _session(64)))

    assert (decode.bus, decode.device, decode.ftdi_packet_size) == (*CONTROLLER, 64)
    assert _messages(decode, RWDLinkDirection.HOST_TO_CONTROLLER) == [
        QUERY,
        ack_for(VERSION),
        ESCAPED_START,
        POWER,
    ]
    inbound = _messages(decode, RWDLinkDirection.CONTROLLER_TO_HOST)
    assert inbound[:4] == [VERSION, VERSION_DETAIL, HEARTBEAT, EDGE]
    assert isinstance(inbound[4], RWDMalformedFrame)
    assert inbound[4].reason == "frame CRC mismatch"
    assert inbound[5:] == [HEARTBEAT] * 40
    assert decode.malformed_count == 1

    split_heartbeat = next(f for f in decode.frames if f.result == HEARTBEAT)
    assert split_heartbeat.timestamp_s == pytest.approx(1.040)
    assert [f.timestamp_s for f in decode.frames] == sorted(f.timestamp_s for f in decode.frames)


def test_detects_high_speed_ftdi_packets_in_legacy_pcap(tmp_path: Path) -> None:
    decode = decode_rwd_usb_capture(_write_pcap(tmp_path / "session.pcap", _session(512)))

    assert decode.ftdi_packet_size == 512
    assert _messages(decode, RWDLinkDirection.CONTROLLER_TO_HOST)[:4] == [
        VERSION,
        VERSION_DETAIL,
        HEARTBEAT,
        EDGE,
    ]
    assert decode.malformed_count == 1


def test_summary_timeline_omits_routine_traffic(tmp_path: Path) -> None:
    summary = decode_rwd_usb_capture(
        _write_pcapng(tmp_path / "session.pcapng", _session(64))
    ).summary()

    assert summary["frames"] == 49
    assert summary["malformed_frames"] == 1
    assert summary["counts"]["controller_to_host"]["INFO CONTROLLER_HEARTBEAT"] == 41
    timeline = summary["timeline"]
    assert [entry.get("function") for entry in timeline] == [
        "VERSION_QUERY",
        "VERSION_REPLY",
        "VERSION_DETAIL_REPLY",
        "DEVICE_ENABLE",
        "LED_VALUE",
        "INPUT_EDGE",
        None,
    ]
    assert timeline[0]["t_s"] == 0.0
    assert timeline[2]["description"] == "build 38598, OFRS magic present"
    assert timeline[4]["description"] == "470 nm power 30.00%"
    assert timeline[-1]["error"] == "frame CRC mismatch"


def test_csv_lists_every_frame(tmp_path: Path) -> None:
    decode = decode_rwd_usb_capture(_write_pcapng(tmp_path / "session.pcapng", _session(64)))
    output = tmp_path / "decoded.csv"
    write_rwd_usb_decode_csv(decode, output)

    with output.open(newline="", encoding="utf-8") as stream:
        rows = list(csv.DictReader(stream))
    assert len(rows) == len(decode.frames)
    assert rows[0]["function"] == "VERSION_QUERY"
    assert rows[0]["relative_s"] == "0.000000"
    assert [row["valid"] for row in rows].count("false") == 1


def test_rejects_captures_without_controller_traffic(tmp_path: Path) -> None:
    camera_only = [packet for packet in _session(64) if b"\x01\x00\x07\x00" in packet[1][:27]]
    path = _write_pcapng(tmp_path / "camera.pcapng", camera_only)
    with pytest.raises(RWDUSBCaptureError, match=r"bulk devices seen: \[\(1, 7\)\]"):
        decode_rwd_usb_capture(path)

    not_capture = tmp_path / "notes.txt"
    not_capture.write_text("not a capture", encoding="utf-8")
    with pytest.raises(RWDUSBCaptureError, match="not a pcap"):
        decode_rwd_usb_capture(not_capture)

    truncated = tmp_path / "truncated.pcapng"
    truncated.write_bytes(_write_pcapng(tmp_path / "full.pcapng", _session(64)).read_bytes()[:-5])
    with pytest.raises(RWDUSBCaptureError, match="truncated"):
        decode_rwd_usb_capture(truncated)


def test_reader_skips_non_usb_link_types_and_strips_ftdi_status(tmp_path: Path) -> None:
    packets = list(read_usbpcap_packets(_write_pcapng(tmp_path / "s.pcapng", _session(64))))
    assert len(packets) == len(_session(64))
    assert packets[0].timestamp_s == pytest.approx(1.0)
    assert strip_ftdi_status(b"\x01\x60ab\x01\x60cd", 4) == b"abcd"
    assert strip_ftdi_status(b"raw", 0) == b"raw"


def test_cli_decodes_capture_to_json_and_csv(tmp_path: Path, capsys) -> None:
    capture = _write_pcapng(tmp_path / "session.pcapng", _session(64))
    output = tmp_path / "decoded.csv"

    assert main(["--decode-rwd-usb-capture", str(capture), "--rwd-usb-csv", str(output)]) == 0

    summary = json.loads(capsys.readouterr().out)
    assert (summary["usb_device"], summary["malformed_frames"]) == (5, 1)
    assert output.exists()
    with pytest.raises(SystemExit):
        main(["--rwd-usb-csv", str(output)])
