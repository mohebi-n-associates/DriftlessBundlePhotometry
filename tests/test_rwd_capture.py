import struct
from pathlib import Path

import pytest

from driftless_photometry.config import (
    RWDChannelMapping,
    RWDPreambleMode,
    RWDSourceConfig,
)
from driftless_photometry.rwd import (
    RWDEventRecord,
    RWDFluorescenceRecord,
    RWDWireCaptureError,
    RWDWireCaptureWriter,
    RWDWireReplaySource,
    inspect_rwd_wire_capture,
    iter_rwd_wire_chunks,
)
from driftless_photometry.rwd.domain import RWDStreamMetadata


def _source_config(*, value_scale: float = 0.001) -> RWDSourceConfig:
    return RWDSourceConfig(
        host="127.0.0.1",
        port=9000,
        preamble_mode=RWDPreambleMode.NONE,
        value_scale=value_scale,
        enabled_wavelengths_nm=(410, 470, 560),
        channel_mappings=(
            RWDChannelMapping(device_channel=7, fiber_id="fiber_01", label="Mouse A"),
        ),
        expected_machine_name="RWD1",
    )


def _fluorescence_bytes(tick: int) -> bytes:
    return b"".join(
        (
            b"RWD1",
            bytes((1, 0x07, 7)),
            struct.pack("<II", tick, 410_000),
            struct.pack("<II", tick, 470_000),
            struct.pack("<II", tick, 560_000),
        )
    )


def _event_bytes(tick: int) -> bytes:
    return b"".join(
        (
            b"RWD1",
            bytes((2,)),
            struct.pack("<I", tick),
            b"lever press".ljust(20, b"\x00"),
            b"\x00",
        )
    )


def _complete_capture(path: Path) -> tuple[Path, bytes]:
    payload = _fluorescence_bytes(100) + _event_bytes(120)
    writer = RWDWireCaptureWriter(path, _source_config())
    writer.append(payload[:13], 0.01)
    writer.append(payload[13:], 0.02)
    inspection = writer.close(
        terminal_outcome="completed_duration",
        stream_metadata=RWDStreamMetadata(RWDPreambleMode.NONE, None, b"RWD1"),
    )
    assert inspection.path == path
    return path, payload


def test_wire_capture_round_trips_exact_chunks_and_integrity_metadata(tmp_path: Path) -> None:
    path, payload = _complete_capture(tmp_path / "bench.rwd-wire")

    inspection = inspect_rwd_wire_capture(path)
    chunks = list(iter_rwd_wire_chunks(path))

    assert not path.with_name(f"{path.name}.partial").exists()
    assert inspection.chunk_count == 2
    assert inspection.byte_count == len(payload)
    assert inspection.duration_s == 0.02
    assert inspection.terminal_outcome == "completed_duration"
    assert inspection.stream_metadata == RWDStreamMetadata(RWDPreambleMode.NONE, None, b"RWD1")
    assert [chunk.sequence for chunk in chunks] == [0, 1]
    assert [chunk.host_received_s for chunk in chunks] == [0.01, 0.02]
    assert b"".join(chunk.payload for chunk in chunks) == payload
    with pytest.raises(FileExistsError, match="already exists"):
        RWDWireCaptureWriter(path, _source_config())


def test_wire_capture_rejects_corruption_and_incomplete_partial(tmp_path: Path) -> None:
    path, _payload = _complete_capture(tmp_path / "bench.rwd-wire")
    damaged = tmp_path / "damaged.rwd-wire"
    encoded = bytearray(path.read_bytes())
    payload_offset = encoded.rindex(b"RWD1")
    encoded[payload_offset] ^= 0x01
    damaged.write_bytes(encoded)

    with pytest.raises(RWDWireCaptureError, match="checksum mismatch"):
        inspect_rwd_wire_capture(damaged)

    incomplete = RWDWireCaptureWriter(tmp_path / "incomplete.rwd-wire", _source_config())
    incomplete.append(b"RWD1", 0.01)
    partial_path = incomplete.partial_path
    incomplete.abort()
    with pytest.raises(RWDWireCaptureError, match="truncated before its footer"):
        inspect_rwd_wire_capture(partial_path)


def test_wire_replay_uses_production_decoder_and_preserves_receipt_times(
    tmp_path: Path,
) -> None:
    path, _payload = _complete_capture(tmp_path / "bench.rwd-wire")
    replay = RWDWireReplaySource(_source_config(), path)

    received = list(replay.records(0.001))

    assert [type(item.record) for item in received] == [
        RWDFluorescenceRecord,
        RWDEventRecord,
    ]
    assert [item.wire_sequence for item in received] == [0, 1]
    assert [item.host_received_s for item in received] == [0.02, 0.02]
    assert replay.stream_metadata == RWDStreamMetadata(RWDPreambleMode.NONE, None, b"RWD1")
    assert replay.diagnostics.socket_reads == 2
    assert replay.diagnostics.decoder.records_decoded == 2
    assert replay.diagnostics.connected is False


def test_wire_replay_rejects_changed_scientific_contract(tmp_path: Path) -> None:
    path, _payload = _complete_capture(tmp_path / "bench.rwd-wire")

    with pytest.raises(ValueError, match="replay contract"):
        RWDWireReplaySource(_source_config(value_scale=0.01), path)
