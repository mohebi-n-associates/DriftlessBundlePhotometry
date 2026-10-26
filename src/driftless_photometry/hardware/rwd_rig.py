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

import math
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import suppress
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Protocol

from driftless_photometry.config import RWD_PROFILE, SessionConfig, Wavelength, profile_for
from driftless_photometry.domain import (
    EventMark,
    ExposureRecord,
    FramePacket,
    InvalidTimeInterval,
    SystemEvent,
    TTLEdge,
)
from driftless_photometry.faults import AcquisitionFault, AcquisitionFaultCode

from .alvium import AlviumFrame, AlviumSettings, AlviumTriggeredCameras
from .base import ExcitationCycle, RigPacket, StartCondition
from .rwd_controller_protocol import (
    RWDLed,
    device_enable,
    led_power,
    output_config,
    output_enable,
)
from .rwd_outputs import OutputProgramRun
from .rwd_timing import (
    RWDBrightnessCheck,
    RWDCameraFrame,
    RWDContinuityBreak,
    RWDExcitationPlan,
    RWDSegmentTracker,
    RWDSlotAssignment,
    RWDTTLClock,
)
from .rwd_transport import (
    RWDControllerIdentity,
    RWDControllerTransport,
    candidate_rwd_ports,
    poll_until,
)

RWD_SUPPORTED_MODEL_CODES = frozenset({0})
"""MODEL_REPLY codes DBF drives; 0 = R821 is the only bench-validated model."""


class RWDCameraSource(Protocol):
    def __enter__(self) -> RWDCameraSource: ...

    def __exit__(self, *exc_info: object) -> None: ...

    def get(self, timeout_s: float) -> AlviumFrame | None: ...

    def set_gain(self, gain_db: float, user_id: str | None = None) -> None: ...


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
    start_delay_check_min_period_s: float = 0.02
    """Below this period (above 50 Hz) host arrival scatter rivals half a period, so the
    start-delay check is skipped; at 120 Hz it fired falsely 6 times in 2.5 h (bench)."""


@dataclass(slots=True)
class _Held:
    frame: AlviumFrame
    assignment: RWDSlotAssignment


@dataclass(slots=True)
class _Pause:
    resume_host_s: float
    resume_s: float
    discarded: int = 0


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
        self._recording_stop = False
        self._gain_lock = threading.Lock()
        self._pending_gains: dict[int | None, float] = {}
        self._pending_power: dict[Wavelength, float] = {}
        self._output_triggers: list[tuple[int, str]] = []
        self.identity: RWDControllerIdentity | None = None
        self.port: str | None = None

    @property
    def plan(self) -> RWDExcitationPlan:
        return self._plan

    def stop(self) -> None:
        self._stop.set()

    def prepare_recording(self) -> None:
        """Enable the recording stop trigger; previews do not act on this input."""

        self._recording_stop = True

    def request_gain(self, gain_db: float, camera: int | None = None) -> None:
        """Ask the running session to change camera 1's or 2's gain, or both (thread-safe).

        For live view and preview: gain changes the recorded scale, so recordings keep the
        gains they started with.
        """

        if gain_db < 0:
            raise ValueError("gain must be non-negative")
        with self._gain_lock:
            if camera is None:
                self._pending_gains.clear()
            self._pending_gains[camera] = float(gain_db)

    def trigger_output(self, port: int, reason: str = "manual") -> None:
        """Start ``port``'s output program in the running session (thread-safe).

        Only outputs with a manual or dF/F trigger respond, and a program that is still
        running ignores new triggers.
        """

        with self._gain_lock:
            self._output_triggers.append((port, reason))

    def _take_output_triggers(self) -> list[tuple[int, str]]:
        with self._gain_lock:
            triggers, self._output_triggers = self._output_triggers, []
        return triggers

    def request_led_power(self, wavelength: Wavelength, percent: float) -> None:
        """Ask the running session to change one enabled LED's power (thread-safe).

        For preview and live view, while the operator sets LED powers. The session stops
        the sequencer, sends the new settings, and starts a new run, as at the start of
        a session; the gap between runs is marked invalid. Recordings keep one power.
        """

        if not 0 <= percent <= 100:
            raise ValueError("LED power must be between 0 and 100 %")
        with self._gain_lock:
            self._pending_power[Wavelength(wavelength)] = float(percent)

    def _take_pending_power(self) -> dict[Wavelength, float]:
        with self._gain_lock:
            power, self._pending_power = self._pending_power, {}
        return power

    def _take_pending_gain(self) -> dict[int | None, float]:
        with self._gain_lock:
            gains, self._pending_gains = self._pending_gains, {}
        return gains

    def packets(
        self,
        duration_s: float,
        start: StartCondition | None = None,
        cycle: ExcitationCycle | None = None,
    ) -> Iterator[RigPacket]:
        if duration_s <= 0:
            raise ValueError("duration_s must be positive")
        # Clear now, not when the generator starts: a Stop that arrives before the
        # first packet is requested must still end the session.
        self._stop.clear()
        return self._session_packets(duration_s, start, cycle)

    def _session_packets(
        self, duration_s: float, start: StartCondition | None, cycle: ExcitationCycle | None
    ) -> Iterator[RigPacket]:
        transport = self._connect()
        cameras: RWDCameraSource | None = None
        try:
            settings = AlviumSettings(
                exposure_us=float(self._config.camera.exposure_us),
                gain_db=float(self._config.camera.gain),
                camera2_gain_db=self._config.camera.camera2_gain,
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
                pending_gain=self._take_pending_gain,
                output_triggers=self._take_output_triggers,
                pending_power=self._take_pending_power,
                recording_stop=self._recording_stop,
            )
            yield from session.run(duration_s, start, cycle)
        finally:
            self._recording_stop = False
            try:
                transport.close()
            finally:
                if cameras is not None:
                    with suppress(Exception):
                        cameras.__exit__(None, None, None)

    def _connect(self) -> RWDControllerTransport:
        transport, port, identity = connect_rwd_controller(
            self._config.source.controller_port,  # type: ignore[union-attr]
            transport_factory=self._transport_factory,
            port_finder=self._port_finder,
        )
        self.identity = identity
        self.port = port
        return transport


def connect_rwd_controller(
    configured_port: str | None,
    *,
    transport_factory: RWDTransportFactory = RWDControllerTransport,
    port_finder: Callable[[], tuple[str, ...]] = candidate_rwd_ports,
) -> tuple[RWDControllerTransport, str, RWDControllerIdentity]:
    """Open the controller (forcing it all-off) and refuse models other than the R821."""

    if configured_port:
        ports: tuple[str, ...] = (configured_port,)
    else:
        ports = port_finder()
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
    transport = transport_factory(port)
    identity = transport.open()
    if identity.model_code not in RWD_SUPPORTED_MODEL_CODES:
        transport.close()
        raise AcquisitionFault(
            AcquisitionFaultCode.CONTROLLER_DISCONNECT,
            f"the RWD controller on {port} reports model {identity.model_name}; DBF "
            "drives only the bench-validated R821",
        )
    return transport, port, identity


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
        pending_gain: Callable[[], dict[int | None, float]] = dict,
        output_triggers: Callable[[], list[tuple[int, str]]] = list,
        pending_power: Callable[[], dict[Wavelength, float]] = dict,
        recording_stop: bool = False,
    ) -> None:
        self._recording_stop = recording_stop
        self._pending_power = pending_power
        self._pending_gain = pending_gain
        self._output_triggers = output_triggers
        self._outputs = {
            output.port: output for output in getattr(config.source, "ttl_outputs", ())
        }
        self._output_runs: dict[int, OutputProgramRun] = {}
        self._output_settings: dict[int, tuple[float, int]] = {}
        self._marks: list[EventMark] = []
        self._last_frame_host_s: float | None = None
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
        self._host_origin_utc = datetime.now(UTC)
        self._session_start_time: datetime | None = None
        self._ttl_sequence = 0
        self._ignored_edges = 0
        self._early_edges = 0
        self._retransmissions = 0
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
        self._run_latest_received_s: float | None = None
        self._open_invalid: tuple[float, str] | None = None
        self._start_trigger: tuple[str, float] | None = None
        # Sequential excitation
        self._cycle: ExcitationCycle | None = None
        self._window_end_s = math.inf
        self._pause_due = False
        self._pause: _Pause | None = None
        self._resuming: tuple[float, int] | None = None

    # Main loop -------------------------------------------------------------------------
    def run(
        self,
        duration_s: float,
        start: StartCondition | None = None,
        cycle: ExcitationCycle | None = None,
    ) -> Iterator[RigPacket]:
        self._cycle = cycle
        if cycle is not None:
            self._window_end_s = cycle.on_s
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
        try:
            if start is not None and not self._wait_for_start(start):
                self._event(
                    "recording_start_cancelled",
                    f"stopped while waiting for {start.describe()}; nothing was recorded",
                )
                yield self._packet(None)
                return
            for command in self._plan.commands():
                transport.command(command)
            self._start_run()
            self._start_outputs()
            if self._start_trigger is not None:
                detail, read_host_s = self._start_trigger
                self._event(
                    "recording_start_trigger",
                    f"{detail}; the sequencer started "
                    f"{(self._enabled_host_s - read_host_s) * 1e3:.0f} ms after it was read",
                )
            finished = False
            while not finished and not self._stop.is_set():
                for camera, gain in self._pending_gain().items():
                    self._cameras.set_gain(gain, None if camera is None else str(camera))
                    which = "both cameras" if camera is None else f"camera {camera}"
                    self._event("rwd_camera_gain_changed", f"{gain:g} dB on {which}")
                power = self._pending_power()
                if power and self._pause is None:
                    self._change_led_power(power)
                frame = self._cameras.get(self._timing.poll_interval_s)
                self._poll_controller()
                if self._stop.is_set():
                    break
                self._service_outputs()
                if self._pause is not None:
                    if frame is not None:
                        self._pause.discarded += 1
                    if self._clock() >= self._pause.resume_host_s:
                        self._resume_excitation()
                    self._release_edges(final=False)
                    if self._edges or self._events or self._invalid or self._marks:
                        yield self._packet(None)
                    continue
                if frame is not None:
                    self._accept(frame)
                self._check_stall(frame_received=frame is not None)
                for packet in self._emit_ready(duration_s):
                    if packet is None:
                        finished = True
                        break
                    yield packet
                if self._pause_due and not finished:
                    finished = not self._pause_excitation(duration_s)
                self._release_edges(final=False)
            if self._stop.is_set() and not finished:
                discarded = len(self._held)
                self._event(
                    "rwd_stop_requested",
                    f"{discarded} frame(s) after the stop request were not recorded",
                )
            transport.command(device_enable(False))
            self._event("rwd_sequencer_stopped", f"after {self._run_index + 1} run(s)")
            diagnostics = getattr(self._cameras, "diagnostics", None)
            if diagnostics is not None:
                self._event(
                    "rwd_camera_diagnostics",
                    f"{diagnostics.delivered} frame(s) delivered, {diagnostics.incomplete} "
                    f"incomplete, {diagnostics.queue_overflows} lost to a full host queue",
                )
            self._close_open_invalid(self._session_now_s() + self._period_s)
            self._release_edges(final=True)
            if self._ignored_edges:
                self._event(
                    "rwd_ttl_edges_not_configured",
                    f"{self._ignored_edges} edge(s) on TTL lines without a configured input",
                )
            if self._early_edges:
                self._event(
                    "rwd_ttl_edges_before_recording",
                    f"{self._early_edges} edge(s) before the first exposure were not recorded",
                )
            if self._retransmissions:
                self._event(
                    "rwd_controller_retransmissions",
                    f"{self._retransmissions} repeated controller message(s) were acknowledged "
                    "and ignored",
                )
            if self._edges or self._events or self._invalid:
                yield self._packet(None)
        finally:
            with suppress(AcquisitionFault, OSError):
                transport.command(device_enable(False))

    # Start conditions ---------------------------------------------------------------------
    def _wait_for_start(self, start: StartCondition) -> bool:
        """Wait with every LED off for the start condition; False if Stop came first."""

        if start.ttl_line is None:
            # The cameras' blocking read is the wait, so a virtual clock advances too.
            retransmissions, stopped = poll_until(
                self._transport,
                self._clock() + start.delay_s,
                clock=self._clock,
                wait=lambda _s: self._cameras.get(self._timing.poll_interval_s),
                stopped=self._stop.is_set,
            )
            self._retransmissions += retransmissions
            if stopped:
                return False
            self._event("recording_start_trigger", f"started after a {start.delay_s:g} s delay")
            return True
        # The board reports TTL inputs only while its sequencer runs, so arm it dark.
        for command in self._plan.dark_commands():
            self._transport.command(command)
        self._transport.command(device_enable(True))
        frames = other_edges = 0
        trigger = None
        try:
            while trigger is None and not self._stop.is_set():
                if self._cameras.get(self._timing.poll_interval_s) is not None:
                    frames += 1
                result = self._transport.poll()
                self._retransmissions += result.retransmissions
                for received in result.edges:
                    edge = received.edge
                    if (
                        trigger is None
                        and edge.line == start.ttl_line
                        and edge.edge == start.ttl_edge
                    ):
                        trigger = received
                    else:
                        other_edges += 1
        finally:
            self._transport.command(device_enable(False))
        if trigger is None:
            return False
        frames += self._drain_frames(service_outputs=False)
        self._start_trigger = (
            f"TTL {trigger.edge.edge} edge on input {trigger.edge.line} at board tick "
            f"{trigger.edge.tick_ms} ms; while armed with the LEDs off, {frames} camera "
            f"frame(s) and {other_edges} other TTL edge(s) were not recorded",
            trigger.host_s,
        )
        return True

    def _drain_frames(self, *, service_outputs: bool = True) -> int:
        """After a sequencer stop, wait until the cameras fall quiet; return frames dropped.

        While a TTL start is still armed, nothing has been recorded yet, so output programs
        (and any queued manual or dF/F triggers) must wait for the recording.
        """

        discarded = 0
        quiet_deadline = self._clock() + self._timing.drain_quiet_s
        while self._clock() < quiet_deadline:
            frame = self._cameras.get(self._timing.poll_interval_s)
            self._poll_controller()
            if service_outputs:
                self._service_outputs()
            if frame is not None:
                discarded += 1
                quiet_deadline = self._clock() + self._timing.drain_quiet_s
        return discarded

    # Sequential excitation -----------------------------------------------------------------
    def _pause_excitation(self, duration_s: float) -> bool:
        """End a lit window and run the board dark; False if no window is left.

        The sequencer keeps running with every LED disabled at 0 %, so TTL inputs are
        still reported (and a TTL stop still works) while nothing is lit.
        """

        cycle = self._cycle
        assert cycle is not None
        self._pause_due = False
        paused_s = self._window_end_s
        resume_s = paused_s + cycle.off_s
        if resume_s >= duration_s - 1e-9:
            return False
        discarded = len(self._held)
        self._held.clear()
        self._transport.command(device_enable(False))
        discarded += self._drain_frames()
        for command in self._plan.dark_commands():
            self._transport.command(command)
        self._transport.command(device_enable(True))
        self._tracker = None
        if self._run_last_host_s is not None and self._last_emitted_s is not None:
            # The last frame arrived one start delay after its trigger.
            trigger_host_s = self._run_last_host_s - (self._baseline_start_delay_s or 0.0)
            resume_host_s = trigger_host_s + (resume_s - self._last_emitted_s)
        else:
            resume_host_s = self._clock() + cycle.off_s
        # Resuming drains the dark run's frames first, so begin that much early.
        resume_host_s -= self._timing.drain_quiet_s + 0.015
        self._pause = _Pause(resume_host_s, resume_s)
        self._event(
            "excitation_paused",
            f"sequential excitation ({cycle.describe()}): LEDs dark until {resume_s:g} s; "
            f"{discarded} frame(s) past the window were not recorded; TTL inputs are still "
            "reported",
            at_s=paused_s,
        )
        return True

    def _resume_excitation(self) -> None:
        pause = self._pause
        assert pause is not None
        self._transport.command(device_enable(False))
        dark_frames = pause.discarded + self._drain_frames()
        for command in self._plan.commands():
            self._transport.command(command)
        self._pause = None
        self._resuming = (pause.resume_s, dark_frames)
        self._start_run()

    def _change_led_power(self, power: dict[Wavelength, float]) -> None:
        updated = {
            wavelength: power.get(wavelength, percent)
            for wavelength, percent in self._plan.power_percent.items()
        }
        if updated == dict(self._plan.power_percent):
            return
        # The R821 takes LED_VALUE while its sequencer runs and applies it within one
        # LED cycle, with no missed trigger or frame (bench, 2026-10-26), so the LED cycle,
        # and every frame's wavelength, carry on without a restart.
        for led in RWDLed:
            wavelength = Wavelength(led.wavelength_nm)
            if (
                wavelength in updated
                and updated[wavelength] != self._plan.power_percent[wavelength]
            ):
                self._transport.command(led_power(led, updated[wavelength]))
        self._plan = replace(self._plan, power_percent=updated)
        self._event(
            "rwd_led_power_changed",
            ", ".join(f"{int(w)} nm {p:g} %" for w, p in updated.items())
            + "; applied without restarting the LED cycle, from the next exposure",
            at_s=self._first_unemitted_s(),
        )

    # Sequencer runs ----------------------------------------------------------------------
    def _start_outputs(self) -> None:
        """Start the programs that run from the recording's start; arm the others."""

        outputs = tuple(self._outputs.values())
        for output in outputs:
            if output.trigger == "recording_start":
                self._output_runs[output.port] = OutputProgramRun(output, self._enabled_host_s)
        self._service_outputs()
        if outputs:
            self._event(
                "rwd_ttl_outputs_started",
                "; ".join(_describe_output(output) for output in outputs),
            )

    def _service_outputs(self) -> None:
        """Start queued triggered programs and send every program's due train changes."""

        now = self._clock()
        for port, reason in self._output_triggers():
            output = self._outputs.get(port)
            if output is None or output.trigger == "recording_start":
                self._event(
                    "ttl_output_trigger_ignored",
                    f"output {port} has no triggered program ({reason})",
                )
                continue
            running = self._output_runs.get(port)
            if running is not None and not running.done:
                self._event(
                    "ttl_output_trigger_ignored",
                    f"output {port}'s program is still running ({reason})",
                )
                continue
            self._output_runs[port] = OutputProgramRun(output, now)
            self._event("ttl_output_triggered", f"output {port}: {reason}")
        for port, run in self._output_runs.items():
            for action in run.due(now):
                train = action.train
                if not action.starts:
                    self._transport.command(output_enable(port, False))
                    continue
                settings = (train.frequency_hz, train.pulse_width_ms)
                if self._output_settings.get(port) != settings:
                    self._transport.command(output_config(port, *settings))
                    self._output_settings[port] = settings
                self._transport.command(output_enable(port, True))
                output = run.output
                if output.mark_stimulation:
                    length = 0.0 if train.stop_s is None else train.stop_s - train.start_s
                    self._marks.append(
                        EventMark(
                            timestamp_s=self._session_at_host(self._clock()),
                            name=output.label or f"Output {port}",
                            source="output",
                            detail=train.describe(port) + "; host-timed",
                            duration_s=length,
                        )
                    )

    def _session_at_host(self, host_s: float) -> float:
        """Place a host instant on the session clock through the latest recorded frame."""

        if self._last_frame_host_s is not None and self._last_emitted_s is not None:
            trigger_host_s = self._last_frame_host_s - (self._baseline_start_delay_s or 0.0)
            return max(0.0, self._last_emitted_s + host_s - trigger_host_s)
        if self._run_index < 0:
            return 0.0  # before run 0 nothing is on the session clock yet
        return max(0.0, host_s - self._enabled_host_s)

    def _start_run(self) -> None:
        self._run_index += 1
        self._held.clear()
        self._next_slot = 0
        self._run_emitted = 0
        self._run_last_host_s = None
        self._run_latest_received_s = None
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
        discarded += self._drain_frames()
        if self._open_invalid is None:
            self._open_invalid = (start_s, reason)
        self._event(
            "rwd_sequencer_restart",
            f"{reason}; {discarded} frame(s) could not be attributed and were not recorded; "
            "TTL edges may not be reported while the sequencer is stopped",
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
        self._run_latest_received_s = max(
            frame.host_received_s, self._run_latest_received_s or frame.host_received_s
        )
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
            elif self._period_s >= self._timing.start_delay_check_min_period_s:
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
            # The first trigger lies between sending ENABLE and receiving its completed
            # frame minus exposure. Use the interval midpoint, retaining its uncertainty.
            bound_s = max(0.0, frame.host_received_s - self._plan.exposure_s - self._enabled_host_s)
            self._session_start_time = self._host_origin_utc + timedelta(
                seconds=self._enabled_host_s - self._host_origin_s + bound_s / 2
            )
            self._event(
                "session_utc_origin",
                f"host UTC estimate {self._session_start_time.isoformat()}; "
                f"trigger/receipt uncertainty ±{bound_s / 2:.6f} s; "
                "host clock accuracy is not measured",
            )
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
        if self._resuming is not None and self._cycle is not None:
            planned_s, dark_frames = self._resuming
            self._resuming = None
            # Windows stay on the schedule from session zero, not on the late restart.
            self._window_end_s = planned_s + self._cycle.on_s
            self._event(
                "excitation_resumed",
                f"LEDs lit {(run_start - planned_s) * 1e3:+.0f} ms from the planned "
                f"{planned_s:g} s; {dark_frames} camera frame(s) while dark were not recorded",
                at_s=run_start,
            )
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
            if session_s >= self._window_end_s - 1e-9:
                self._held[self._next_slot] = held
                self._pause_due = True
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
        self._last_frame_host_s = frame.host_received_s
        self._restarts_without_frames = 0
        self._ttl.observe_frame(frame.host_received_s, session_s)
        if camera == 1:
            message = self._brightness.observe(wavelength, float(frame.image.mean()))
            if message is not None:
                self._event("rwd_brightness_qc_mismatch", message, at_s=session_s)
        return self._packet(packet)

    def _camera_context(self) -> str:
        """Settings and camera counters that explain a missing frame."""

        text = (
            f"{self._plan.frame_rate_hz} Hz, exposure {self._plan.exposure_us / 1000:g} ms, "
            f"trigger period {self._period_s * 1e3:.3f} ms, LEDs "
            + ", ".join(f"{int(w)} nm {p:g} %" for w, p in self._plan.power_percent.items())
        )
        diagnostics = getattr(self._cameras, "diagnostics", None)
        if diagnostics is not None:
            text += (
                f"; cameras delivered {diagnostics.delivered} frame(s), "
                f"{diagnostics.incomplete} incomplete, {diagnostics.queue_overflows} lost"
            )
        return text

    def _check_stall(self, *, frame_received: bool) -> None:
        """Restart when the next slot's frame is missing, judged by frame arrival times.

        A busy host only delays reading the camera queue. So a slot counts as missing
        only once a frame that arrived well after it has been read, or when the queue is
        empty and nothing has arrived for the stall timeout.
        """

        tracker = self._tracker
        if tracker is None:
            return
        now = self._clock()
        delay = tracker.start_delay_s
        if delay is None:
            if (
                not frame_received
                and now - self._enabled_host_s > self._timing.first_frame_timeout_s
            ):
                self._restart(
                    f"no camera frame within {self._timing.first_frame_timeout_s:g} s of the "
                    f"sequencer start ({self._camera_context()})"
                )
            return
        if self._run_last_host_s is None:
            slot0_host_s = self._enabled_host_s + delay
            expected_s = slot0_host_s + self._plan.exposure_s + self._next_slot * self._period_s
        else:
            # Track the last arrival so host/board clock drift never accumulates.
            expected_s = self._run_last_host_s + self._period_s
        limit_s = expected_s + self._timing.stall_timeout_s
        latest = self._run_latest_received_s
        later_frame_read = latest is not None and latest > limit_s
        silent = (
            not frame_received
            and now > limit_s
            and (latest is None or now - latest > self._timing.stall_timeout_s)
        )
        if later_frame_read or silent:
            camera = self._plan.camera_for_slot(self._next_slot)
            self._restart(
                f"camera {camera} delivered no frame for trigger slot {self._next_slot} "
                f"within {self._timing.stall_timeout_s:g} s"
            )

    # Controller input ---------------------------------------------------------------------
    def _poll_controller(self) -> None:
        result = self._transport.poll()
        self._retransmissions += result.retransmissions
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
            recording = self._config.recording
            if (
                self._recording_stop
                and recording.stop == "ttl"
                and received.edge.line == recording.stop_ttl_line
                and received.edge.edge == recording.stop_ttl_edge
            ):
                # Stop on receipt; alignment may hold this edge for ten seconds.
                # Final release still preserves its board tick and aligned timestamp.
                self._stop.set()

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
            if aligned.session_s < 0:
                self._early_edges += 1
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
            event_marks=tuple(self._marks),
            session_start_time=self._session_start_time,
            stop_requested=self._recording_stop and self._stop.is_set(),
        )
        self._session_start_time = None
        self._edges.clear()
        self._invalid.clear()
        self._events.clear()
        self._marks.clear()
        return packet

    def _cycle_text(self) -> str:
        return " -> ".join(f"{int(w)} nm" for w in self._plan.order)


def _describe_output(output) -> str:
    text = f"output {output.port}: {output.frequency_hz:g} Hz, {output.pulse_width_ms} ms pulses"
    if output.label:
        text += f" ({output.label})"
    if output.train_s is not None:
        trigger = {
            "recording_start": "from the start",
            "manual": "on a manual trigger",
            "dff_threshold": "on a dF/F threshold",
        }[output.trigger]
        text += f", a {output.program_s:g} s program {trigger}"
    return text
