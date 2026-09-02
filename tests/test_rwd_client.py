import json
import socket
import struct
import threading
import time
from contextlib import AbstractContextManager, suppress
from pathlib import Path
from types import TracebackType

import pytest

from driftless_photometry.cli import main
from driftless_photometry.config import (
    RWDChannelMapping,
    RWDPreambleMode,
    RWDSourceConfig,
    SessionConfig,
    demo_config,
)
from driftless_photometry.faults import AcquisitionFault, AcquisitionFaultCode
from driftless_photometry.rwd import RWDEventRecord, RWDFluorescenceRecord, RWDStreamClient
from driftless_photometry.rwd.acquisition import RWDAcquisitionEngine
from driftless_photometry.settings import export_settings
from driftless_photometry.state import AcquisitionState
from driftless_photometry.storage import load_rwd_session_spool
from driftless_photometry.storage.rwd_spool import RWDSessionSpool


def _fluorescence_bytes(channel: int, tick: int, offset: int = 0) -> bytes:
    return b"".join(
        (
            b"RWD1",
            bytes((1, 0x07, channel)),
            struct.pack("<II", tick, 410_000 + offset),
            struct.pack("<II", tick, 470_000 + offset),
            struct.pack("<II", tick, 560_000 + offset),
        )
    )


def _event_bytes(tick: int, status: int = 0) -> bytes:
    return b"".join(
        (
            b"RWD1",
            bytes((2,)),
            struct.pack("<I", tick),
            b"lever press".ljust(20, b"\x00"),
            bytes((status,)),
        )
    )


class _ScriptedServer(AbstractContextManager["_ScriptedServer"]):
    def __init__(self, chunks: list[bytes], *, close_after_send: bool) -> None:
        self._chunks = chunks
        self._close_after_send = close_after_send
        self._release = threading.Event()
        self.accepted = threading.Event()
        self.finished = threading.Event()
        self.error: BaseException | None = None
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(1)
        self.port = int(self._listener.getsockname()[1])
        self._connection: socket.socket | None = None
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        try:
            self._listener.settimeout(2)
            connection, _address = self._listener.accept()
            self._connection = connection
            self.accepted.set()
            with connection:
                for chunk in self._chunks:
                    connection.sendall(chunk)
                if not self._close_after_send:
                    self._release.wait(timeout=2)
        except BaseException as error:
            if not self._release.is_set():
                self.error = error
        finally:
            self.finished.set()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc_value, traceback
        self._release.set()
        with suppress(OSError):
            self._listener.close()
        self._thread.join(timeout=2)
        if self._thread.is_alive():
            raise RuntimeError("scripted RWD server did not stop")
        if self.error is not None:
            raise self.error


def _source_config(port: int, *, read_timeout_s: float = 0.2) -> RWDSourceConfig:
    return RWDSourceConfig(
        host="127.0.0.1",
        port=port,
        connect_timeout_s=0.2,
        read_timeout_s=read_timeout_s,
        preamble_mode=RWDPreambleMode.NONE,
        enabled_wavelengths_nm=(410, 470, 560),
        channel_mappings=(
            RWDChannelMapping(device_channel=0, fiber_id="fiber_01", label="Fiber 1"),
            RWDChannelMapping(device_channel=1, fiber_id="fiber_02", label="Fiber 2"),
        ),
        expected_machine_name="RWD1",
        maximum_expected_record_rate_hz=100,
    )


def _session_config(tmp_path: Path, port: int) -> SessionConfig:
    base = demo_config(tmp_path, fiber_count=2, width_px=32, height_px=24)
    return base.model_copy(
        update={
            "session_id": "rwd-client-test",
            "source": _source_config(port),
        }
    )


def test_tcp_client_decodes_fragmented_and_coalesced_records() -> None:
    payload = _fluorescence_bytes(0, 100) + _event_bytes(150)
    chunks = [payload[:1], payload[1:17], payload[17:]]
    with _ScriptedServer(chunks, close_after_send=False) as server:
        client = RWDStreamClient(_source_config(server.port))

        records = list(client.records(0.06))

        assert server.accepted.is_set()
        assert [type(received.record) for received in records] == [
            RWDFluorescenceRecord,
            RWDEventRecord,
        ]
        assert [received.wire_sequence for received in records] == [0, 1]
        assert all(received.host_received_s >= 0 for received in records)
        assert client.stream_metadata is not None
        assert client.stream_metadata.machine_name == b"RWD1"
        assert client.diagnostics.decoder.records_decoded == 2
        assert client.diagnostics.decoder.bytes_received == len(payload)
        assert client.diagnostics.connected is False


def test_tcp_client_distinguishes_clean_boundary_disconnect_from_truncation() -> None:
    with _ScriptedServer([_event_bytes(10)], close_after_send=True) as server:
        client = RWDStreamClient(_source_config(server.port))
        iterator = client.records(1)

        assert isinstance(next(iterator).record, RWDEventRecord)
        with pytest.raises(AcquisitionFault) as disconnected:
            next(iterator)

        assert disconnected.value.code is AcquisitionFaultCode.RWD_DISCONNECT

    with _ScriptedServer([_event_bytes(10)[:12]], close_after_send=True) as server:
        client = RWDStreamClient(_source_config(server.port))
        with pytest.raises(AcquisitionFault) as truncated:
            list(client.records(1))

        assert truncated.value.code is AcquisitionFaultCode.MALFORMED_RWD_STREAM
        assert "truncated" in str(truncated.value)


def test_tcp_client_surfaces_malformed_type_and_idle_timeout() -> None:
    with _ScriptedServer([b"RWD1\x99"], close_after_send=False) as server:
        client = RWDStreamClient(_source_config(server.port))
        with pytest.raises(AcquisitionFault) as malformed:
            list(client.records(1))
        assert malformed.value.code is AcquisitionFaultCode.MALFORMED_RWD_STREAM
        assert "unknown RWD record type" in str(malformed.value)

    with _ScriptedServer([], close_after_send=False) as server:
        client = RWDStreamClient(_source_config(server.port, read_timeout_s=0.05))
        with pytest.raises(AcquisitionFault) as timed_out:
            list(client.records(1))
        assert timed_out.value.code is AcquisitionFaultCode.RWD_READ_TIMEOUT


def test_tcp_client_stop_unblocks_idle_read_promptly() -> None:
    with _ScriptedServer([], close_after_send=False) as server:
        client = RWDStreamClient(_source_config(server.port, read_timeout_s=1))
        result: list[object] = []

        def consume() -> None:
            try:
                result.extend(client.records(5))
            except BaseException as error:
                result.append(error)

        thread = threading.Thread(target=consume)
        thread.start()
        assert server.accepted.wait(timeout=1)
        started = time.monotonic()
        client.stop()
        thread.join(timeout=0.5)

        assert not thread.is_alive()
        assert time.monotonic() - started < 0.5
        assert result == []
        assert client.diagnostics.stop_requested is True


def test_tcp_client_connect_failure_has_stable_fault_code() -> None:
    reservation = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    reservation.bind(("127.0.0.1", 0))
    port = int(reservation.getsockname()[1])
    reservation.close()
    client = RWDStreamClient(_source_config(port))

    with pytest.raises(AcquisitionFault) as caught:
        list(client.records(0.1))

    assert caught.value.code is AcquisitionFaultCode.RWD_CONNECT_FAILURE


def test_headless_rwd_engine_finalizes_valid_tcp_session(tmp_path: Path) -> None:
    payload = b"".join(
        (
            _fluorescence_bytes(0, 100),
            _fluorescence_bytes(1, 100, 1000),
            _event_bytes(150),
            _fluorescence_bytes(0, 200, 10),
            _fluorescence_bytes(1, 200, 1010),
            _event_bytes(250, 1),
        )
    )
    with _ScriptedServer([payload], close_after_send=False) as server:
        config = _session_config(tmp_path, server.port)
        states = []
        progress = []
        engine = RWDAcquisitionEngine(spool_chunk_size=2, spool_queue_size=4)

        result = engine.run(
            config,
            duration_s=0.08,
            on_state=states.append,
            on_progress=progress.append,
        )

    assert len(result.reports) == 2
    assert result.reports[0].stream_record_count == 6
    assert result.reports[0].trace_sample_count == 6
    assert result.reports[0].named_event_count == 2
    assert result.stopped_by_request is False
    assert result.storage_capacity.estimated_record_count == 8
    assert result.diagnostics.spool.committed_frames == 6
    assert len(progress) == 6
    assert states == [
        AcquisitionState.READY,
        AcquisitionState.ARMED,
        AcquisitionState.RECORDING,
        AcquisitionState.DRAINING,
        AcquisitionState.READY,
    ]
    assert engine.state is AcquisitionState.READY
    assert not list(tmp_path.glob("*.rwd-spool"))


def test_rwd_settings_cli_runs_the_headless_tcp_bridge(tmp_path: Path, capsys) -> None:
    payload = _fluorescence_bytes(0, 100) + _fluorescence_bytes(1, 100, 1000)
    with _ScriptedServer([payload], close_after_send=False) as server:
        config = _session_config(tmp_path, server.port).model_copy(
            update={"session_id": "rwd-cli-test"}
        )
        settings_path = export_settings(tmp_path / "rwd.settings.json", config)

        exit_code = main(
            [
                "--rwd-settings",
                str(settings_path),
                "--duration",
                "0.06",
            ]
        )

    result = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert result["source_kind"] == "rwd"
    assert result["stream_records"] == 2
    assert result["trace_samples"] == 3
    assert result["named_events"] == 0
    assert result["fiber_files"] == 2


def test_rwd_cli_captures_and_replays_exact_wire_stream(tmp_path: Path, capsys) -> None:
    payload = _fluorescence_bytes(0, 100) + _fluorescence_bytes(1, 100, 1000)
    capture_path = tmp_path / "live-bench.rwd-wire"
    with _ScriptedServer([payload], close_after_send=False) as server:
        live_config = _session_config(tmp_path / "live", server.port).model_copy(
            update={"session_id": "rwd-wire-live"}
        )
        live_settings = export_settings(tmp_path / "live.settings.json", live_config)

        assert (
            main(
                [
                    "--rwd-settings",
                    str(live_settings),
                    "--rwd-wire-capture",
                    str(capture_path),
                    "--duration",
                    "0.06",
                ]
            )
            == 0
        )

    live_result = json.loads(capsys.readouterr().out)
    assert live_result["wire_capture"]["path"] == str(capture_path)
    assert live_result["wire_capture"]["bytes"] == len(payload)
    assert live_result["wire_capture"]["terminal_outcome"] == "completed_duration"

    replay_config = live_config.model_copy(
        update={
            "session_id": "rwd-wire-replay",
            "output_directory": tmp_path / "replay",
        }
    )
    replay_settings = export_settings(tmp_path / "replay.settings.json", replay_config)

    assert (
        main(
            [
                "--rwd-settings",
                str(replay_settings),
                "--rwd-replay",
                str(capture_path),
                "--duration",
                "0.001",
            ]
        )
        == 0
    )
    replay_result = json.loads(capsys.readouterr().out)
    assert replay_result["stream_records"] == live_result["stream_records"] == 2
    assert replay_result["trace_samples"] == live_result["trace_samples"] == 3
    assert replay_result["wire_replay"]["sha256"] == live_result["wire_capture"]["sha256"]

    assert main(["--inspect-rwd-capture", str(capture_path)]) == 0
    inspected = json.loads(capsys.readouterr().out)
    assert inspected["sha256"] == live_result["wire_capture"]["sha256"]
    assert inspected["resolved_preamble_mode"] == "none"
    assert inspected["machine_name"] == "RWD1"


def test_headless_rwd_engine_journals_malformed_stream_after_committed_record(
    tmp_path: Path,
) -> None:
    payload = _fluorescence_bytes(0, 100) + b"RWD1\x99"
    with _ScriptedServer([payload], close_after_send=False) as server:
        config = _session_config(tmp_path, server.port)
        engine = RWDAcquisitionEngine(spool_chunk_size=1)

        with pytest.raises(AcquisitionFault) as caught:
            engine.run(config, duration_s=1)

    assert caught.value.code is AcquisitionFaultCode.MALFORMED_RWD_STREAM
    assert engine.state is AcquisitionState.ERROR
    spools = list(tmp_path.glob("*.rwd-spool"))
    assert len(spools) == 1
    loaded = load_rwd_session_spool(spools[0])
    assert loaded.complete is False
    assert len(loaded.data.received_records) == 1
    assert loaded.data.system_events[-1].event == ("acquisition_fault:malformed_rwd_stream")
    assert loaded.data.invalid_times[-1].source_event == "malformed_rwd_stream"
    engine.reset_error()
    assert engine.state is AcquisitionState.READY


def test_headless_rwd_engine_surfaces_socket_to_spool_backpressure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = b"".join(_fluorescence_bytes(0, 100 + index, index) for index in range(8))
    writer_entered = threading.Event()
    producer_observed_full_queue = threading.Event()
    release_writer = threading.Event()
    original_commit = RWDSessionSpool._commit_chunk
    original_put = RWDSessionSpool._put

    def blocked_commit(self, records) -> None:
        writer_entered.set()
        assert release_writer.wait(timeout=2)
        original_commit(self, records)

    def observed_put(self, work) -> None:
        if writer_entered.is_set() and self._queue.full():
            producer_observed_full_queue.set()
        original_put(self, work)

    monkeypatch.setattr(RWDSessionSpool, "_commit_chunk", blocked_commit)
    monkeypatch.setattr(RWDSessionSpool, "_put", observed_put)
    with _ScriptedServer([payload], close_after_send=False) as server:
        config = _session_config(tmp_path, server.port).model_copy(
            update={"session_id": "rwd-pressure-test"}
        )
        engine = RWDAcquisitionEngine(
            spool_chunk_size=1,
            spool_queue_size=1,
            spool_submit_timeout_s=0.02,
        )
        outcome: list[BaseException] = []

        def run_engine() -> None:
            try:
                engine.run(config, duration_s=1)
            except BaseException as error:
                outcome.append(error)

        thread = threading.Thread(target=run_engine)
        thread.start()
        assert writer_entered.wait(timeout=1)
        assert producer_observed_full_queue.wait(timeout=1)
        time.sleep(0.06)
        release_writer.set()
        thread.join(timeout=2)

    assert not thread.is_alive()
    assert len(outcome) == 1
    assert "queue is full" in str(outcome[0])
    spool = load_rwd_session_spool(next(tmp_path.glob("*.rwd-spool")))
    assert spool.data.system_events[-1].event == "acquisition_fault:queue_backpressure"
