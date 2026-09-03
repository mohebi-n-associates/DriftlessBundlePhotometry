"""Single-owner, stoppable TCP client for the read-only RWD stream."""

from __future__ import annotations

import socket
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import suppress
from dataclasses import dataclass
from typing import Protocol

from driftless_photometry.config import RWDSourceConfig
from driftless_photometry.faults import AcquisitionFault, AcquisitionFaultCode

from .domain import RWDReceivedRecord, RWDStreamMetadata, RWDWavelength
from .protocol import (
    RWD_PROTOCOL_VERSION,
    RWDDecoderDiagnostics,
    RWDProtocolError,
    RWDStreamDecoder,
)

_RECEIVE_POLL_S = 0.05
_STOP_DRAIN_LIMIT_S = 0.25
_RECEIVE_BYTES = 64 * 1024


@dataclass(frozen=True, slots=True)
class RWDClientDiagnostics:
    """Connection and decoder counters safe to expose during a run."""

    connected: bool
    stop_requested: bool
    socket_reads: int
    decoder: RWDDecoderDiagnostics


class RWDRecordSource(Protocol):
    """Dependency-injected source used by the RWD acquisition coordinator."""

    adapter_name: str
    adapter_version: str
    protocol_version: str

    @property
    def stream_metadata(self) -> RWDStreamMetadata | None: ...

    @property
    def diagnostics(self) -> RWDClientDiagnostics: ...

    def records(self, duration_s: float) -> Iterator[RWDReceivedRecord]: ...

    def stop(self) -> None: ...


class RWDStreamClient:
    """Own one TCP connection on the thread that consumes :meth:`records`.

    ``stop`` may be called from another thread. Reads poll at a short interval so a
    clean stop is prompt; any already-buffered partial record gets a short drain
    window and is surfaced as truncation if the peer never completes it.
    """

    adapter_name = "rwd_tcp_read_only"
    adapter_version = "1"
    protocol_version = RWD_PROTOCOL_VERSION

    def __init__(
        self,
        config: RWDSourceConfig,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        on_wire_chunk: Callable[[bytes, float], None] | None = None,
    ) -> None:
        self._config = config
        self._monotonic = monotonic
        self._on_wire_chunk = on_wire_chunk
        self._decoder = RWDStreamDecoder(
            value_scale=config.value_scale,
            preamble_mode=config.preamble_mode,
            allowed_channels=frozenset(
                mapping.device_channel for mapping in config.channel_mappings
            ),
            enabled_wavelengths=frozenset(
                RWDWavelength(wavelength) for wavelength in config.enabled_wavelengths_nm
            ),
            expected_machine_name=(
                None
                if config.expected_machine_name is None
                else config.expected_machine_name.encode("ascii")
            ),
        )
        self._stop_requested = threading.Event()
        self._lock = threading.Lock()
        self._socket: socket.socket | None = None
        self._connected = False
        self._socket_reads = 0
        self._owner_thread_id: int | None = None
        self._started = False
        self._stream_metadata: RWDStreamMetadata | None = None

    @property
    def stream_metadata(self) -> RWDStreamMetadata | None:
        with self._lock:
            return self._stream_metadata

    @property
    def diagnostics(self) -> RWDClientDiagnostics:
        with self._lock:
            connected = self._connected
            socket_reads = self._socket_reads
        return RWDClientDiagnostics(
            connected=connected,
            stop_requested=self._stop_requested.is_set(),
            socket_reads=socket_reads,
            decoder=self._decoder.diagnostics,
        )

    def records(self, duration_s: float) -> Iterator[RWDReceivedRecord]:
        if duration_s <= 0:
            raise ValueError("RWD record duration must be positive")
        owner = threading.get_ident()
        with self._lock:
            if self._started:
                raise RuntimeError("an RWD stream client cannot be reused")
            self._started = True
            self._owner_thread_id = owner
        try:
            connection = self._connect()
        except AcquisitionFault as error:
            if (
                error.code is AcquisitionFaultCode.RWD_CONNECT_FAILURE
                and self._stop_requested.is_set()
            ):
                return
            raise
        started = self._monotonic()
        deadline = started + duration_s
        last_byte_at = started
        stop_started_at: float | None = None
        wire_sequence = 0
        try:
            while True:
                now = self._monotonic()
                stopping = self._stop_requested.is_set() or now >= deadline
                if stopping and stop_started_at is None:
                    stop_started_at = now
                buffered = self._decoder.diagnostics.buffered_bytes
                if stopping and buffered == 0:
                    self._decoder.finish()
                    return
                if stop_started_at is not None and now - stop_started_at >= _STOP_DRAIN_LIMIT_S:
                    self._finish_or_fault("RWD stop reached a truncated record")
                if now - last_byte_at >= self._config.read_timeout_s:
                    raise AcquisitionFault(
                        AcquisitionFaultCode.RWD_READ_TIMEOUT,
                        f"RWD stream produced no bytes for {self._config.read_timeout_s:g} s",
                    )
                poll_s = min(
                    _RECEIVE_POLL_S,
                    max(0.001, self._config.read_timeout_s - (now - last_byte_at)),
                )
                connection.settimeout(poll_s)
                try:
                    chunk = connection.recv(_RECEIVE_BYTES)
                except TimeoutError:
                    continue
                except OSError as error:
                    if self._stop_requested.is_set():
                        self._finish_or_fault("RWD socket stopped with a truncated record")
                        return
                    raise AcquisitionFault(
                        AcquisitionFaultCode.RWD_DISCONNECT,
                        f"RWD socket read failed: {error}",
                    ) from error
                with self._lock:
                    self._socket_reads += 1
                received_at = self._monotonic()
                if not chunk:
                    try:
                        self._decoder.finish()
                    except RWDProtocolError as error:
                        raise AcquisitionFault(
                            AcquisitionFaultCode.MALFORMED_RWD_STREAM,
                            str(error),
                        ) from error
                    raise AcquisitionFault(
                        AcquisitionFaultCode.RWD_DISCONNECT,
                        "RWD peer closed the TCP stream",
                    )
                last_byte_at = received_at
                if self._on_wire_chunk is not None:
                    self._on_wire_chunk(chunk, received_at - started)
                try:
                    parsed_records = self._decoder.feed(chunk)
                    for record in parsed_records:
                        self._capture_stream_metadata()
                        yield RWDReceivedRecord(
                            wire_sequence=wire_sequence,
                            host_received_s=received_at - started,
                            record=record,
                        )
                        wire_sequence += 1
                except RWDProtocolError as error:
                    raise AcquisitionFault(
                        AcquisitionFaultCode.MALFORMED_RWD_STREAM,
                        str(error),
                    ) from error
        finally:
            self._close_socket()

    def stop(self) -> None:
        self._stop_requested.set()
        with self._lock:
            connection = self._socket
            connected = self._connected
        if connection is not None and not connected:
            with suppress(OSError):
                connection.close()

    def _connect(self) -> socket.socket:
        if self._stop_requested.is_set():
            raise AcquisitionFault(
                AcquisitionFaultCode.RWD_CONNECT_FAILURE,
                "RWD connection was stopped before it started",
            )
        last_error: OSError | None = None
        try:
            addresses = socket.getaddrinfo(
                self._config.host,
                self._config.port,
                type=socket.SOCK_STREAM,
            )
        except OSError as error:
            raise AcquisitionFault(
                AcquisitionFaultCode.RWD_CONNECT_FAILURE,
                f"could not resolve RWD host {self._config.host!r}: {error}",
            ) from error
        for family, socktype, protocol, _canonical_name, address in addresses:
            connection = socket.socket(family, socktype, protocol)
            connection.settimeout(self._config.connect_timeout_s)
            with self._lock:
                self._socket = connection
            try:
                connection.connect(address)
            except OSError as error:
                last_error = error
                with suppress(OSError):
                    connection.close()
                if self._stop_requested.is_set():
                    break
                continue
            with self._lock:
                self._connected = True
            return connection
        message = "RWD connection was stopped" if self._stop_requested.is_set() else str(last_error)
        raise AcquisitionFault(
            AcquisitionFaultCode.RWD_CONNECT_FAILURE,
            f"could not connect to RWD at {self._config.host}:{self._config.port}: {message}",
        )

    def _capture_stream_metadata(self) -> None:
        diagnostics = self._decoder.diagnostics
        if diagnostics.resolved_preamble_mode is None or diagnostics.machine_name is None:
            raise RuntimeError("RWD decoder produced a record without stream metadata")
        metadata = RWDStreamMetadata(
            resolved_preamble_mode=diagnostics.resolved_preamble_mode,
            preamble=diagnostics.preamble,
            machine_name=diagnostics.machine_name,
        )
        with self._lock:
            if self._stream_metadata is None:
                self._stream_metadata = metadata
            elif self._stream_metadata != metadata:
                raise RuntimeError("RWD stream metadata changed during one connection")

    def _finish_or_fault(self, message: str) -> None:
        try:
            self._decoder.finish()
        except RWDProtocolError as error:
            raise AcquisitionFault(
                AcquisitionFaultCode.MALFORMED_RWD_STREAM,
                f"{message}: {error}",
            ) from error

    def _close_socket(self) -> None:
        with self._lock:
            connection = self._socket
            self._socket = None
            self._connected = False
        if connection is not None:
            with suppress(OSError):
                connection.close()
