"""RWD photometry hardware driven directly by DBF, as a ``RigSource``.

One ``RWDRig.packets`` call owns the controller and the cameras for one session:

1. connect to the controller, which forces the board all-off, and refuse any model
   other than the bench-validated R821;
2. open only the cameras that an enabled LED triggers, and send the board settings;
3. enable the sequencer and attribute each frame to a trigger slot (see
   ``rwd_timing``), merging both cameras into slot order;
4. on any continuity break, stop the sequencer, discard the unattributable frames,
   record an invalid span, and restart, as OFRS does;
5. on every exit path, send the board's off sequence.

The RWD board keeps its LEDs on until it is told otherwise (bench, 2026-10-20), so
the off sequence in ``RWDControllerTransport.close`` must run however the session
ends. The unit's power switch is the only hardware fail-safe.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterator
from contextlib import suppress
from dataclasses import dataclass
from typing import Protocol

from driftless_photometry.config import RWD_PROFILE, SessionConfig, profile_for
from driftless_photometry.domain import (
    ExposureRecord,
    FramePacket,
    InvalidTimeInterval,
    SystemEvent,
    TTLEdge,
)
from driftless_photometry.faults import AcquisitionFault, AcquisitionFaultCode

from .alvium import AlviumFrame, AlviumSettings, AlviumTriggeredCameras
from .base import RigPacket
from .rwd_controller_protocol import device_enable, output_config, output_enable
from .rwd_timing import (
    RWDBrightnessCheck,
    RWDCameraFrame,
    RWDContinuityBreak,
    RWDExcitationPlan,
    RWDSegmentTracker,
    RWDSlotAssignment,
    RWDTTLClock,
)
from .rwd_transport import RWDControllerIdentity, RWDControllerTransport, candidate_rwd_ports

RWD_SUPPORTED_MODEL_CODES = frozenset({0})
"""MODEL_REPLY codes DBF drives; 0 = R821 is the only bench-validated model."""


class RWDCameraSource(Protocol):
    def __enter__(self) -> RWDCameraSource: ...

    def __exit__(self, *exc_info: object) -> None: ...

    def get(self, timeout_s: float) -> AlviumFrame | None: ...


RWDCameraFactory = Callable[[AlviumSettings, tuple[str, ...]], RWDCameraSource]
RWDTransportFactory = Callable[[str], RWDControllerTransport]


def _open_alvium(settings: AlviumSettings, user_ids: tuple[str, ...]) -> RWDCameraSource:
    return AlviumTriggeredCameras(settings, user_ids=user_ids)


@dataclass(frozen=True, slots=True)
class RWDRigTiming:
    """Host-side waits and limits. None of them times an exposure."""

    poll_interval_s: float = 0.005
    first_frame_timeout_s: float = 2.0
    """How long after a sequencer start the first frame may take."""
    stall_timeout_s: float = 0.5
    """How late, past its expected arrival, a slot's frame may be."""
    drain_quiet_s: float = 0.15
    """Frames must stop for this long after a sequencer stop before a restart."""
    restart_limit: int = 3
    """Consecutive restarts allowed without one attributed frame."""
    start_delay_tolerance_fraction: float = 0.5
    """Allowed change, in trigger periods, of the enable-to-first-frame delay."""


@dataclass(slots=True)
class _Held:
    frame: AlviumFrame
    assignment: RWDSlotAssignment


@dataclass(slots=True)
class _Anchor:
    """A frame whose session time and camera timestamp are both known."""

    session_s: float
    camera_timestamp_ns: int
    period_ns: float
    host_received_s: float


class RWDRig:
    """Drive an RWD controller and its Alvium cameras for one session at a time."""

    adapter_name = "rwd_direct"
    adapter_version = "1"
    protocol_version = "rwd_serial_r821_fw2.0.0+phase_derived_wavelength_v1"

    def __init__(
        self,
        config: SessionConfig,
        *,
        transport_factory: RWDTransportFactory = RWDControllerTransport,
        camera_factory: RWDCameraFactory = _open_alvium,
        port_finder: Callable[[], tuple[str, ...]] = candidate_rwd_ports,
        clock: Callable[[], float] = time.monotonic,
        timing: RWDRigTiming | None = None,
    ) -> None:
        if profile_for(config.source) is not RWD_PROFILE:
            raise ValueError("RWDRig needs an RWD session configuration")
        self._config = config
        self._plan = RWDExcitationPlan.from_config(config)
        self._transport_factory = transport_factory
        self._camera_factory = camera_factory
        self._port_finder = port_finder
        self._clock = clock
        self._timing = timing or RWDRigTiming()
        self._stop = threading.Event()
        self.identity: RWDControllerIdentity | None = None
        self.port: str | None = None

    @property
    def plan(self) -> RWDExcitationPlan:
        return self._plan

    def stop(self) -> None:
        self._stop.set()

    def packets(self, duration_s: float) -> Iterator[RigPacket]:
        if duration_s <= 0:
            raise ValueError("duration_s must be positive")
        self._stop.clear()
        transport = self._connect()
        cameras: RWDCameraSource | None = None
        try:
            settings = AlviumSettings(
                exposure_us=float(self._config.camera.exposure_us),
                gain_db=float(self._config.camera.gain),
            )
            user_ids = tuple(str(camera) for camera in self._plan.cameras)
            cameras = self._camera_factory(settings, user_ids)
            cameras.__enter__()
            session = _RWDSession(
                self._config,
                self._plan,
                transport,
                cameras,
                clock=self._clock,
                timing=self._timing,
                stop=self._stop,
                identity=self.identity,
                port=self.port or "",
            )
            yield from session.run(duration_s)
        finally:
            try:
                transport.close()
            finally:
                if cameras is not None:
                    with suppress(Exception):
                        cameras.__exit__(None, None, None)

    def _connect(self) -> RWDControllerTransport:
        configured = self._config.source.controller_port  # type: ignore[union-attr]
        if configured:
            ports = (configured,)
        else:
            ports = self._port_finder()
            if not ports:
                raise AcquisitionFault(
                    AcquisitionFaultCode.CONTROLLER_DISCONNECT,
                    "no FTDI serial port for an RWD controller was found; connect the "
                    "controller's USB cable and switch the unit on",
                )
            if len(ports) > 1:
                raise AcquisitionFault(
                    AcquisitionFaultCode.CONTROLLER_DISCONNECT,
                    f"several FTDI serial ports were found ({', '.join(ports)}); choose the "
                    "RWD controller's port in the RWD settings",
                )
        port = ports[0]
        transport = self._transport_factory(port)
        identity = transport.open()
        if identity.model_code not in RWD_SUPPORTED_MODEL_CODES:
            transport.close()
            raise AcquisitionFault(
                AcquisitionFaultCode.CONTROLLER_DISCONNECT,
                f"the RWD controller on {port} reports model {identity.model_name}; DBF "
                "drives only the bench-validated R821",
            )
        self.identity = identity
        self.port = port
        return transport


class _RWDSession:
    """The state of one RWD recording; created and run by ``RWDRig.packets``."""

    def __init__(
        self,
        config: SessionConfig,
        plan: RWDExcitationPlan,
        transport: RWDControllerTransport,
        cameras: RWDCameraSource,
        *,
        clock: Callable[[], float],
        timing: RWDRigTiming,
        stop: threading.Event,
        identity: RWDControllerIdentity | None,
        port: str,
    ) -> None:
        self._config = config
        self._plan = plan
        self._transport = transport
        self._cameras = cameras
        self._clock = clock
        self._timing = timing
        self._stop = stop
        self._identity = identity
        self._port = port
        self._period_s = plan.period_s
        self._ttl_lines = {ttl.line: ttl for ttl in config.ttl_inputs}
        self._ttl = RWDTTLClock(exposure_s=plan.exposure_s)
        self._brightness = RWDBrightnessCheck(plan)
        self._host_origin_s = clock()
        self._ttl_sequence = 0
        self._ignored_edges = 0
        self._edges: list[TTLEdge] = []
        self._events: list[SystemEvent] = []
        self._invalid: list[InvalidTimeInterval] = []
        # Session progress
        self._last_emitted_s: float | None = None
        self._last_sequence = -1
        self._anchors: dict[int, _Anchor] = {}
        self._baseline_start_delay_s: float | None = None
        self._restarts_without_frames = 0
        # The current sequencer run
        self._run_index = -1
        self._tracker: RWDSegmentTracker | None = None
        self._enabled_host_s = 0.0
        self._run_start_s: float | None = None
        self._run_sequence_base = 0
        self._next_slot = 0
        self._held: dict[int, _Held] = {}
        self._run_emitted = 0
        self._run_last_host_s: float | None = None
        self._open_invalid: tuple[float, str] | None = None

    # Main loop -------------------------------------------------------------------------
    def run(self, duration_s: float) -> Iterator[RigPacket]:
        transport = self._transport
        identity = self._identity
        detail = f"port {self._port}"
        if identity is not None:
            firmware = ".".join(str(part) for part in identity.firmware)
            detail = (
                f"{identity.model_name} on {self._port}, firmware {firmware}, "
                f"build {identity.build}; wavelengths are phase-derived"
            )
        self._event("rwd_controller_connected", detail)
        for command in self._plan.commands():
            transport.command(command)
        self._start_outputs()
        self._start_run()
        try:
            finished = False
            while not finished and not self._stop.is_set():
                frame = self._cameras.get(self._timing.poll_interval_s)
                self._poll_controller()
                if frame is not None:
                    self._accept(frame)
                self._check_stall()
                for packet in self._emit_ready(duration_s):
                    if packet is None:
                        finished = True
                        break
                    yield packet
                self._release_edges(final=False)
            if self._stop.is_set() and not finished:
                discarded = len(self._held)
                self._event(
                    "rwd_stop_requested",
                    f"{discarded} frame(s) after the stop request were not recorded",
                )
            transport.command(device_enable(False))
            self._event("rwd_sequencer_stopped", f"after {self._run_index + 1} run(s)")
            self._close_open_invalid(self._session_now_s() + self._period_s)
            self._release_edges(final=True)
            if self._ignored_edges:
                self._event(
                    "rwd_ttl_edges_not_configured",
                    f"{self._ignored_edges} edge(s) on TTL lines without a configured input",
                )
            if self._edges or self._events or self._invalid:
                yield self._packet(None)
        finally:
            with suppress(AcquisitionFault, OSError):
                transport.command(device_enable(False))

    # Sequencer runs ----------------------------------------------------------------------
    def _start_outputs(self) -> None:
        outputs = self._config.source.ttl_outputs  # type: ignore[union-attr]
        for output in outputs:
            self._transport.command(
                output_config(output.port, output.frequency_hz, output.pulse_width_ms)
            )
            self._transport.command(output_enable(output.port, True))
        if outputs:
            self._event(
                "rwd_ttl_outputs_started",
                "; ".join(
                    f"output {output.port}: {output.frequency_hz:g} Hz, "
                    f"{output.pulse_width_ms} ms pulses"
                    + (f" ({output.label})" if output.label else "")
                    for output in outputs
                ),
            )

    def _start_run(self) -> None:
        self._run_index += 1
        self._held.clear()
        self._next_slot = 0
        self._run_emitted = 0
        self._run_last_host_s = None
        self._run_start_s = None
        self._run_sequence_base = self._last_sequence + 1
        self._brightness.start_segment(self._run_index)
        # Stamp before sending: the confirmation returns through the FTDI latency timer
        # (up to 16 ms by default), but the board starts as soon as the command arrives.
        self._enabled_host_s = self._clock()
        self._transport.command(device_enable(True))
        self._tracker = RWDSegmentTracker(self._plan, enabled_host_s=self._enabled_host_s)

    def _restart(self, reason: str) -> None:
        if self._run_emitted == 0:
            self._restarts_without_frames += 1
        else:
            self._restarts_without_frames = 1
        if self._restarts_without_frames > self._timing.restart_limit:
            raise AcquisitionFault(
                AcquisitionFaultCode.CAMERA_DISCONNECT,
                f"RWD acquisition could not attribute frames after "
                f"{self._timing.restart_limit} sequencer restarts; last problem: {reason}",
            )
        start_s = self._first_unemitted_s()
        discarded = len(self._held)
        self._transport.command(device_enable(False))
        quiet_deadline = self._clock() + self._timing.drain_quiet_s
        while self._clock() < quiet_deadline:
            frame = self._cameras.get(self._timing.poll_interval_s)
            self._poll_controller()
            if frame is not None:
                discarded += 1
                quiet_deadline = self._clock() + self._timing.drain_quiet_s
        if self._open_invalid is None:
            self._open_invalid = (start_s, reason)
        self._event(
            "rwd_sequencer_restart",
            f"{reason}; {discarded} frame(s) could not be attributed and were not recorded; "
            "TTL inputs are not reported while the sequencer is stopped",
            at_s=start_s,
        )
        self._start_run()

    def _first_unemitted_s(self) -> float:
        if self._run_start_s is not None:
            return self._run_start_s + self._next_slot * self._period_s
        if self._last_emitted_s is not None:
            return self._last_emitted_s + self._period_s
        return 0.0

    # Frames ------------------------------------------------------------------------------
    def _accept(self, frame: AlviumFrame) -> None:
        tracker = self._tracker
        assert tracker is not None
        try:
            camera = int(frame.camera_user_id)
        except ValueError:
            camera = 0
        result = tracker.observe(
            RWDCameraFrame(camera, frame.frame_id, frame.camera_timestamp_ns, frame.host_received_s)
        )
        if isinstance(result, RWDContinuityBreak):
            if result.fatal:
                raise AcquisitionFault(AcquisitionFaultCode.CLOCK_DISCONTINUITY, result.reason)
            self._restart(result.reason)
            return
        if result.slot == 0 and not self._begin_run_timing(frame):
            return
        self._held[result.slot] = _Held(frame, result)

    def _begin_run_timing(self, frame: AlviumFrame) -> bool:
        """Place a run's slot 0 on the session clock; False if a restart was needed."""

        tracker = self._tracker
        assert tracker is not None
        delay = tracker.start_delay_s
        if delay is not None:
            if self._baseline_start_delay_s is None:
                self._baseline_start_delay_s = delay
                self._event(
                    "rwd_start_delay_measured",
                    f"enable command to first frame {delay * 1e3:.2f} ms, including one "
                    "exposure and the camera readout",
                )
            else:
                change = delay - self._baseline_start_delay_s
                limit = self._timing.start_delay_tolerance_fraction * self._period_s
                if abs(change) > limit:
                    self._restart(
                        f"the first frame after a sequencer restart arrived {change * 1e3:+.1f} "
                        "ms off the delay measured at session start; its first trigger may "
                        "have been missed"
                    )
                    return False
        if self._last_emitted_s is None:
            run_start = 0.0
        else:
            run_start = self._run_start_from_anchor(frame)
            minimum = self._last_emitted_s + self._period_s
            if run_start < minimum - 1e-9:
                raise AcquisitionFault(
                    AcquisitionFaultCode.CLOCK_DISCONTINUITY,
                    f"RWD run {self._run_index} would start at {run_start:.6f} s, before the "
                    f"previous run ended at {self._last_emitted_s:.6f} s",
                )
        self._run_start_s = run_start
        self._close_open_invalid(run_start)
        self._event(
            "rwd_sequencer_started",
            f"run {self._run_index}: LED cycle {self._cycle_text()} at "
            f"{self._plan.frame_rate_hz} Hz",
            at_s=run_start,
        )
        return True

    def _run_start_from_anchor(self, frame: AlviumFrame) -> float:
        camera = int(frame.camera_user_id)
        anchor = self._anchors.get(camera)
        if anchor is not None:
            elapsed_slots = (frame.camera_timestamp_ns - anchor.camera_timestamp_ns) / (
                anchor.period_ns
            )
            return anchor.session_s + elapsed_slots * self._period_s
        # No earlier frame from this camera: fall back to host arrival times.
        latest = max(self._anchors.values(), key=lambda item: item.session_s)
        start = latest.session_s + (frame.host_received_s - latest.host_received_s)
        self._event(
            "rwd_run_start_host_timed",
            f"run {self._run_index} start placed from host arrival times (millisecond "
            "scatter) because its first camera had no earlier frame",
            at_s=start,
        )
        return start

    def _emit_ready(self, duration_s: float) -> Iterator[RigPacket | None]:
        if self._run_start_s is None:
            return
        while self._next_slot in self._held:
            held = self._held.pop(self._next_slot)
            session_s = self._run_start_s + self._next_slot * self._period_s
            if session_s >= duration_s - 1e-9:
                yield None
                return
            yield self._frame_packet(held, session_s)
            self._next_slot += 1

    def _frame_packet(self, held: _Held, session_s: float) -> RigPacket:
        frame = held.frame
        wavelength = held.assignment.wavelength
        camera = int(frame.camera_user_id)
        sequence = self._run_sequence_base + held.assignment.slot
        exposure = ExposureRecord(
            sequence=sequence,
            timestamp_s=session_s,
            controller_tick_us=round(session_s * 1e6),
            wavelength_nm=wavelength,
            commanded_voltage_v=None,
            commanded_power_percent=self._plan.power_percent[wavelength],
            camera_index=camera,
            wavelength_source="phase_derived",
        )
        packet = FramePacket(
            frame_id=frame.frame_id,
            image=frame.image,
            exposure=exposure,
            host_received_s=frame.host_received_s - self._host_origin_s,
            camera_timestamp_ns=frame.camera_timestamp_ns,
        )
        tracker = self._tracker
        assert tracker is not None
        self._anchors[camera] = _Anchor(
            session_s,
            frame.camera_timestamp_ns,
            tracker.camera_period_ns(camera) or self._period_s * 1e9,
            frame.host_received_s,
        )
        self._last_emitted_s = session_s
        self._last_sequence = sequence
        self._run_emitted += 1
        self._run_last_host_s = frame.host_received_s
        self._restarts_without_frames = 0
        self._ttl.observe_frame(frame.host_received_s, session_s)
        if camera == 1:
            message = self._brightness.observe(wavelength, float(frame.image.mean()))
            if message is not None:
                self._event("rwd_brightness_qc_mismatch", message, at_s=session_s)
        return self._packet(packet)

    def _check_stall(self) -> None:
        tracker = self._tracker
        if tracker is None:
            return
        now = self._clock()
        delay = tracker.start_delay_s
        if delay is None:
            if now - self._enabled_host_s > self._timing.first_frame_timeout_s:
                self._restart(
                    f"no camera frame within {self._timing.first_frame_timeout_s:g} s of the "
                    "sequencer start"
                )
            return
        if self._run_last_host_s is None:
            slot0_host_s = self._enabled_host_s + delay
            expected_s = slot0_host_s + self._plan.exposure_s + self._next_slot * self._period_s
        else:
            # Track the last arrival so host/board clock drift never accumulates.
            expected_s = self._run_last_host_s + self._period_s
        if now > expected_s + self._timing.stall_timeout_s:
            camera = self._plan.camera_for_slot(self._next_slot)
            self._restart(
                f"camera {camera} delivered no frame for trigger slot {self._next_slot} "
                f"within {self._timing.stall_timeout_s:g} s"
            )

    # Controller input ---------------------------------------------------------------------
    def _poll_controller(self) -> None:
        result = self._transport.poll()
        for malformed in result.malformed:
            at_s = self._session_now_s()
            self._invalid.append(
                InvalidTimeInterval(
                    start_time_s=at_s,
                    stop_time_s=at_s + self._period_s,
                    reason="malformed RWD controller frame; a TTL edge may have been lost",
                    source_event="malformed_controller_stream",
                )
            )
            self._event("rwd_malformed_controller_frame", malformed.reason, at_s=at_s)
        for received in result.edges:
            try:
                self._ttl.add_edge(received.edge, received.host_s)
            except ValueError as error:
                raise AcquisitionFault(
                    AcquisitionFaultCode.MALFORMED_CONTROLLER_STREAM,
                    f"RWD controller TTL ticks went backwards: {error}",
                ) from error

    def _release_edges(self, *, final: bool) -> None:
        was_aligned = self._ttl.aligned
        try:
            released = self._ttl.release(self._clock(), final=final)
        except ValueError as error:
            self._event("rwd_ttl_edges_unaligned", str(error))
            return
        if released and not was_aligned and self._ttl.offset_s is not None:
            self._event(
                "rwd_ttl_clock_aligned",
                f"session zero is board tick {self._ttl.offset_s * 1e3:.3f} ms, estimated "
                f"from {self._ttl.edges_used} edge(s) and frame arrival times; edges keep "
                "their raw board ticks",
            )
        for aligned in released:
            ttl = self._ttl_lines.get(aligned.edge.line)
            if ttl is None:
                self._ignored_edges += 1
                continue
            if ttl.edges.value not in {"both", aligned.edge.edge}:
                continue
            self._edges.append(
                TTLEdge(
                    timestamp_s=aligned.session_s,
                    controller_tick_us=aligned.tick_us,
                    line=aligned.edge.line,
                    label=ttl.label,
                    edge=aligned.edge.edge,
                    new_level=aligned.edge.edge == "rising",
                    sequence=self._ttl_sequence,
                )
            )
            self._ttl_sequence += 1

    # Records -----------------------------------------------------------------------------
    def _session_now_s(self) -> float:
        return 0.0 if self._last_emitted_s is None else self._last_emitted_s

    def _event(self, event: str, detail: str, *, at_s: float | None = None) -> None:
        timestamp = self._session_now_s() if at_s is None else at_s
        self._events.append(SystemEvent(max(0.0, timestamp), event, detail))

    def _close_open_invalid(self, stop_s: float) -> None:
        if self._open_invalid is None:
            return
        start_s, reason = self._open_invalid
        self._open_invalid = None
        self._invalid.append(
            InvalidTimeInterval(
                start_time_s=max(0.0, start_s),
                stop_time_s=max(start_s, stop_s, 0.0),
                reason=f"RWD sequencer restart: {reason}",
                source_event="rwd_sequencer_restart",
            )
        )

    def _packet(self, frame: FramePacket | None) -> RigPacket:
        packet = RigPacket(
            frame=frame,
            ttl_edges=tuple(self._edges),
            invalid_times=tuple(self._invalid),
            system_events=tuple(self._events),
        )
        self._edges.clear()
        self._invalid.clear()
        self._events.clear()
        return packet

    def _cycle_text(self) -> str:
        return " -> ".join(f"{int(w)} nm" for w in self._plan.order)
