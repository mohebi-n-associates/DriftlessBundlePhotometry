"""Offline decoder for USBPcap captures of the RWD controller's FTDI serial link.

A Wireshark/USBPcap capture taken while RWD's own software drives the hardware is the
ground truth for ``rwd_controller_protocol``. This module never touches hardware: it
reads a completed ``.pcapng`` or ``.pcap`` file, finds the USB device that carries RWD
controller frames, removes the two FTDI modem-status bytes that start every bulk-IN
packet, and decodes both directions.

Frame timestamps are the host USB timestamps of the transfer that completed each frame,
not controller time.
"""

from __future__ import annotations

import csv
import struct
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import BinaryIO

from .rwd_controller_protocol import (
    RWD_FLAG,
    RWD_FRAME_HEADER,
    RWDFrameResult,
    RWDFrameStreamDecoder,
    RWDFunction,
    RWDMalformedFrame,
    RWDMessage,
    RWDMessageType,
    describe_rwd_message,
)

LINKTYPE_USBPCAP = 249
FTDI_STATUS_BYTES = 2
FTDI_PACKET_SIZES = (64, 512)
_USBPCAP_HEADER = struct.Struct("<HQIHBHHBBI")
_INFO_PDO_TO_FDO = 0x01
_TRANSFER_BULK = 3
_ENDPOINT_IN = 0x80
_FRAME_START = bytes([RWD_FLAG]) + RWD_FRAME_HEADER
_PCAPNG_SECTION = 0x0A0D0D0A
_PCAP_MAGICS = {
    b"\xd4\xc3\xb2\xa1": ("<", 1e-6),
    b"\xa1\xb2\xc3\xd4": (">", 1e-6),
    b"\x4d\x3c\xb2\xa1": ("<", 1e-9),
    b"\xa1\xb2\x3c\x4d": (">", 1e-9),
}
_ROUTINE_FUNCTIONS = {RWDFunction.HOST_HEARTBEAT, RWDFunction.CONTROLLER_HEARTBEAT}


class RWDUSBCaptureError(RuntimeError):
    """A USB capture could not be read or did not contain RWD controller traffic."""


class RWDLinkDirection(StrEnum):
    HOST_TO_CONTROLLER = "host_to_controller"
    CONTROLLER_TO_HOST = "controller_to_host"


@dataclass(frozen=True, slots=True)
class USBPcapPacket:
    timestamp_s: float
    bus: int
    device: int
    endpoint: int
    transfer: int
    info: int
    payload: bytes

    @property
    def is_in(self) -> bool:
        return bool(self.endpoint & _ENDPOINT_IN)

    @property
    def carries_bulk_data(self) -> bool:
        """USBPcap records OUT data on submission and IN data on completion."""

        if self.transfer != _TRANSFER_BULK or not self.payload:
            return False
        completion = bool(self.info & _INFO_PDO_TO_FDO)
        return completion if self.is_in else not completion


@dataclass(frozen=True, slots=True)
class RWDCapturedFrame:
    timestamp_s: float
    direction: RWDLinkDirection
    result: RWDFrameResult


@dataclass(frozen=True, slots=True)
class RWDUSBCaptureDecode:
    path: Path
    bus: int
    device: int
    ftdi_packet_size: int
    frames: tuple[RWDCapturedFrame, ...]

    @property
    def malformed_count(self) -> int:
        return sum(isinstance(frame.result, RWDMalformedFrame) for frame in self.frames)

    def summary(self) -> dict[str, object]:
        """JSON-ready counts plus a timeline that omits heartbeats and acknowledgements."""

        origin = self.frames[0].timestamp_s if self.frames else 0.0
        counts: dict[str, Counter[str]] = {
            direction.value: Counter() for direction in RWDLinkDirection
        }
        timeline: list[dict[str, object]] = []
        for frame in self.frames:
            result = frame.result
            if isinstance(result, RWDMalformedFrame):
                counts[frame.direction.value]["malformed"] += 1
                timeline.append(
                    {
                        "t_s": round(frame.timestamp_s - origin, 6),
                        "direction": frame.direction.value,
                        "error": result.reason,
                        "raw_hex": result.raw.hex(" "),
                    }
                )
                continue
            counts[frame.direction.value][f"{result.message_type.name} {result.function_name}"] += 1
            if result.message_type is RWDMessageType.ACK or result.function in _ROUTINE_FUNCTIONS:
                continue
            timeline.append(
                {
                    "t_s": round(frame.timestamp_s - origin, 6),
                    "direction": frame.direction.value,
                    "type": result.message_type.name,
                    "sequence": result.sequence,
                    "function": result.function_name,
                    "data_hex": result.data.hex(" "),
                    "description": describe_rwd_message(result),
                }
            )
        return {
            "path": str(self.path),
            "usb_bus": self.bus,
            "usb_device": self.device,
            "ftdi_packet_size": self.ftdi_packet_size,
            "frames": len(self.frames),
            "malformed_frames": self.malformed_count,
            "duration_s": round(self.frames[-1].timestamp_s - origin, 6) if self.frames else 0.0,
            "counts": {direction: dict(sorted(c.items())) for direction, c in counts.items()},
            "timeline": timeline,
        }


def _read_exact(stream: BinaryIO, size: int) -> bytes:
    data = stream.read(size)
    if len(data) != size:
        raise RWDUSBCaptureError("capture file is truncated")
    return data


def _pcapng_records(stream: BinaryIO) -> Iterator[tuple[float, int, bytes]]:
    endian = "<"
    interfaces: list[tuple[int, float]] = []
    while head := stream.read(8):
        if len(head) != 8:
            raise RWDUSBCaptureError("capture file is truncated")
        if int.from_bytes(head[:4], "little") == _PCAPNG_SECTION:
            byte_order = _read_exact(stream, 4)
            if byte_order == b"\x4d\x3c\x2b\x1a":
                endian = "<"
            elif byte_order == b"\x1a\x2b\x3c\x4d":
                endian = ">"
            else:
                raise RWDUSBCaptureError("pcapng section has an invalid byte-order magic")
            total = struct.unpack(f"{endian}I", head[4:8])[0]
            _read_exact(stream, total - 12)
            interfaces = []
            continue
        block_type, total = struct.unpack(f"{endian}II", head)
        if total < 12 or total % 4:
            raise RWDUSBCaptureError("pcapng block has an invalid length")
        body = _read_exact(stream, total - 12)
        _read_exact(stream, 4)
        if block_type == 1:
            interfaces.append(_interface_description(body, endian))
        elif block_type == 6:
            interface, high, low, captured, _ = struct.unpack_from(f"{endian}IIIII", body)
            if interface >= len(interfaces):
                raise RWDUSBCaptureError("pcapng packet references an undefined interface")
            linktype, resolution_s = interfaces[interface]
            yield ((high << 32) | low) * resolution_s, linktype, body[20 : 20 + captured]


def _interface_description(body: bytes, endian: str) -> tuple[int, float]:
    linktype = struct.unpack_from(f"{endian}H", body)[0]
    resolution_s = 1e-6
    offset = 8
    while offset + 4 <= len(body):
        code, length = struct.unpack_from(f"{endian}HH", body, offset)
        if code == 0:
            break
        if code == 9 and length >= 1:
            value = body[offset + 4]
            resolution_s = 2.0 ** -(value & 0x7F) if value & 0x80 else 10.0**-value
        offset += 4 + length + (-length % 4)
    return linktype, resolution_s


def _pcap_records(stream: BinaryIO) -> Iterator[tuple[float, int, bytes]]:
    header = _read_exact(stream, 24)
    endian, fraction_s = _PCAP_MAGICS[header[:4]]
    linktype = struct.unpack_from(f"{endian}I", header, 20)[0] & 0x0FFFFFFF
    while record := stream.read(16):
        if len(record) != 16:
            raise RWDUSBCaptureError("capture file is truncated")
        seconds, fraction, captured, _ = struct.unpack(f"{endian}IIII", record)
        yield seconds + fraction * fraction_s, linktype, _read_exact(stream, captured)


def read_usbpcap_packets(path: Path) -> Iterator[USBPcapPacket]:
    """Yield USBPcap packets from a pcapng or pcap file; other link types are skipped."""

    with path.open("rb") as stream:
        magic = stream.read(4)
        stream.seek(0)
        if int.from_bytes(magic, "little") == _PCAPNG_SECTION:
            records = _pcapng_records(stream)
        elif magic in _PCAP_MAGICS:
            records = _pcap_records(stream)
        else:
            raise RWDUSBCaptureError(f"{path} is not a pcap or pcapng capture")
        for timestamp_s, linktype, data in records:
            if linktype != LINKTYPE_USBPCAP or len(data) < _USBPCAP_HEADER.size:
                continue
            header_length, _, _, _, info, bus, device, endpoint, transfer, length = (
                _USBPCAP_HEADER.unpack_from(data)
            )
            yield USBPcapPacket(
                timestamp_s,
                bus,
                device,
                endpoint,
                transfer,
                info,
                bytes(data[header_length : header_length + length]),
            )


def strip_ftdi_status(payload: bytes, packet_size: int) -> bytes:
    """Remove the two modem-status bytes FTDI places at the start of every IN packet."""

    if packet_size == 0:
        return payload
    return b"".join(
        payload[offset + FTDI_STATUS_BYTES : offset + packet_size]
        for offset in range(0, len(payload), packet_size)
    )


def _decode_stream(
    chunks: list[tuple[float, bytes]], direction: RWDLinkDirection
) -> list[RWDCapturedFrame]:
    decoder = RWDFrameStreamDecoder()
    return [
        RWDCapturedFrame(timestamp_s, direction, result)
        for timestamp_s, chunk in chunks
        for result in decoder.feed(chunk)
    ]


def _valid_count(frames: list[RWDCapturedFrame]) -> int:
    return sum(isinstance(frame.result, RWDMessage) for frame in frames)


def decode_rwd_usb_capture(path: Path) -> RWDUSBCaptureDecode:
    """Decode every RWD controller frame in a completed USBPcap capture.

    The capture is read twice so that bulk-IN data from other devices, such as cameras,
    is never held in memory: the first pass scores devices by their small outbound
    traffic, and the second keeps only the selected controller's packets.
    """

    outbound: dict[tuple[int, int], bytearray] = {}
    seen: set[tuple[int, int]] = set()
    for packet in read_usbpcap_packets(path):
        if not packet.carries_bulk_data:
            continue
        seen.add((packet.bus, packet.device))
        if not packet.is_in:
            outbound.setdefault((packet.bus, packet.device), bytearray()).extend(packet.payload)
    scores = {key: data.count(_FRAME_START) for key, data in outbound.items()}
    if not scores or max(scores.values()) == 0:
        raise RWDUSBCaptureError(
            f"no USB device in {path} carried RWD controller frames; "
            f"bulk devices seen: {sorted(seen)}"
        )
    bus, device = max(scores, key=lambda key: scores[key])
    selected = [
        packet
        for packet in read_usbpcap_packets(path)
        if packet.carries_bulk_data and (packet.bus, packet.device) == (bus, device)
    ]
    host_frames = _decode_stream(
        [(p.timestamp_s, p.payload) for p in selected if not p.is_in],
        RWDLinkDirection.HOST_TO_CONTROLLER,
    )
    inbound = [(p.timestamp_s, p.payload) for p in selected if p.is_in]
    candidates = {
        size: _decode_stream(
            [(t, strip_ftdi_status(chunk, size)) for t, chunk in inbound],
            RWDLinkDirection.CONTROLLER_TO_HOST,
        )
        for size in (*FTDI_PACKET_SIZES, 0)
    }
    packet_size = max(candidates, key=lambda size: _valid_count(candidates[size]))
    frames = sorted(
        host_frames + candidates[packet_size],
        key=lambda frame: (
            frame.timestamp_s,
            frame.direction is RWDLinkDirection.CONTROLLER_TO_HOST,
        ),
    )
    return RWDUSBCaptureDecode(path, bus, device, packet_size, tuple(frames))


def write_rwd_usb_decode_csv(decode: RWDUSBCaptureDecode, path: Path) -> None:
    """Write every decoded or malformed frame, including heartbeats and acknowledgements."""

    origin = decode.frames[0].timestamp_s if decode.frames else 0.0
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.writer(stream)
        writer.writerow(
            [
                "usb_timestamp_s",
                "relative_s",
                "direction",
                "valid",
                "message_type",
                "sequence",
                "function_id",
                "function",
                "data_hex",
                "description",
                "error",
            ]
        )
        for frame in decode.frames:
            prefix = [
                f"{frame.timestamp_s:.6f}",
                f"{frame.timestamp_s - origin:.6f}",
                frame.direction.value,
            ]
            result = frame.result
            if isinstance(result, RWDMalformedFrame):
                writer.writerow(
                    [*prefix, "false", "", "", "", "", result.raw.hex(" "), "", result.reason]
                )
                continue
            writer.writerow(
                [
                    *prefix,
                    "true",
                    result.message_type.name,
                    result.sequence,
                    f"0x{result.function_id:02X}",
                    result.function_name,
                    result.data.hex(" "),
                    describe_rwd_message(result),
                    "",
                ]
            )
