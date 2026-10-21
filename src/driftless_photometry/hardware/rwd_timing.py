"""Wavelength identity and session timing for RWD exposures.

The RWD controller reports no per-exposure records. Under the scoped exception in the
agent contract (approved 2026-10-20), an exposure's wavelength is derived from:

1. camera identity: the board triggers camera 2 only for 560 nm and camera 1 only for
   410 and 470 nm (bench);
2. its position in the board's LED cycle, counted from the first trigger after the
   sequencer is enabled, which bench tests showed is always the cycle's first LED;
3. per-camera hardware frame-ID and timestamp continuity: any missing, duplicated,
   early, or late exposure ends the run and requires a sequencer restart.

Session time is the board's trigger grid: within one sequencer run, slot ``k`` is
``k / fps`` after the run's first trigger. TTL edges carry raw board-clock ticks, and
one offset maps them onto that grid (see ``RWDTTLClock``). Nothing here touches
hardware; ``rwd_rig`` drives the hardware.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass

from driftless_photometry.config import RWD_PROFILE, SessionConfig, Wavelength, profile_for
from driftless_photometry.timing import TickUnwrapper

from .rwd_controller_protocol import (
    RWDCommand,
    RWDExcitationMode,
    RWDInputEdge,
    RWDLed,
    camera_timing,
    excitation_mode,
    excitation_mode_for,
    led_enable,
    led_power,
    trigger_period_s,
)

_LED_FOR_WAVELENGTH = {Wavelength(led.wavelength_nm): led for led in RWDLed}


@dataclass(frozen=True, slots=True)
class RWDExcitationPlan:
    """The board settings and LED cycle for one RWD session."""

    order: tuple[Wavelength, ...]
    """Enabled LEDs in the order the board cycles them (410 → 470 → 560, bench)."""
    power_percent: Mapping[Wavelength, float]
    frame_rate_hz: int
    exposure_us: int
    mode: RWDExcitationMode

    @classmethod
    def from_config(cls, config: SessionConfig) -> RWDExcitationPlan:
        if profile_for(config.source) is not RWD_PROFILE:
            raise ValueError("an RWD excitation plan needs an RWD session configuration")
        enabled = config.enabled_channels
        order = tuple(sorted(channel.wavelength_nm for channel in enabled))
        power = {channel.wavelength_nm: float(channel.power_percent or 0.0) for channel in enabled}
        return cls(
            order=order,
            power_percent=power,
            frame_rate_hz=int(config.camera.requested_fps),
            exposure_us=int(config.camera.exposure_us),
            mode=excitation_mode_for(_LED_FOR_WAVELENGTH[w] for w in order),
        )

    @property
    def period_s(self) -> float:
        """The board's trigger period, which is 1/fps truncated to 10 us (bench)."""

        return trigger_period_s(self.frame_rate_hz)

    @property
    def exposure_s(self) -> float:
        return self.exposure_us * 1e-6

    @property
    def cameras(self) -> tuple[int, ...]:
        return tuple(sorted({RWD_PROFILE.camera_for(w) for w in self.order}))

    @property
    def reference_camera(self) -> int:
        """The camera triggered by each run's first slot."""

        return self.camera_for_slot(0)

    def wavelength_for_slot(self, slot: int) -> Wavelength:
        return self.order[slot % len(self.order)]

    def camera_for_slot(self, slot: int) -> int:
        return RWD_PROFILE.camera_for(self.wavelength_for_slot(slot))

    def first_slot(self, camera: int) -> int:
        return self.next_slot(camera, -1)

    def next_slot(self, camera: int, slot: int) -> int:
        """The first slot after ``slot`` that triggers ``camera``."""

        for candidate in range(slot + 1, slot + 1 + len(self.order)):
            if self.camera_for_slot(candidate) == camera:
                return candidate
        raise ValueError(f"no enabled LED triggers camera {camera}")

    def commands(self) -> tuple[RWDCommand, ...]:
        """Board settings in OFRS order; the sequencer must already be disabled."""

        commands: list[RWDCommand] = []
        for led in RWDLed:
            wavelength = Wavelength(led.wavelength_nm)
            commands.append(led_power(led, self.power_percent.get(wavelength, 0.0)))
            commands.append(led_enable(led, wavelength in self.power_percent))
        commands.append(camera_timing(self.frame_rate_hz, self.exposure_us / 1000))
        commands.append(excitation_mode(self.mode))
        return tuple(commands)

    def dark_commands(self) -> tuple[RWDCommand, ...]:
        """The same timing with every LED disabled at 0 %, for waiting on a TTL start.

        With the sequencer running this way the board reports TTL inputs, and it stays
        silent when the sequencer is off (bench, 2026-10-20).
        """

        commands: list[RWDCommand] = []
        for led in RWDLed:
            commands.append(led_power(led, 0.0))
            commands.append(led_enable(led, False))
        commands.append(camera_timing(self.frame_rate_hz, self.exposure_us / 1000))
        commands.append(excitation_mode(self.mode))
        return tuple(commands)


@dataclass(frozen=True, slots=True)
class RWDCameraFrame:
    """The identity and timing fields of one delivered camera frame."""

    camera_index: int
    frame_id: int
    camera_timestamp_ns: int
    host_received_s: float


@dataclass(frozen=True, slots=True)
class RWDSlotAssignment:
    slot: int
    wavelength: Wavelength


@dataclass(frozen=True, slots=True)
class RWDContinuityBreak:
    """The run can no longer be attributed; ``fatal`` breaks cannot be fixed by a restart."""

    camera_index: int
    reason: str
    fatal: bool = False


@dataclass(slots=True)
class _CameraTrack:
    first_slot: int
    first_timestamp_ns: int
    last_slot: int
    last_frame_id: int
    last_timestamp_ns: int
    count: int = 1
    sum_x: float = 0.0
    sum_y: float = 0.0
    sum_xx: float = 0.0
    sum_xy: float = 0.0

    def add(self, slot: int, timestamp_ns: int, frame_id: int) -> None:
        x = float(slot - self.first_slot)
        y = float(timestamp_ns - self.first_timestamp_ns)
        self.count += 1
        self.sum_x += x
        self.sum_y += y
        self.sum_xx += x * x
        self.sum_xy += x * y
        self.last_slot = slot
        self.last_frame_id = frame_id
        self.last_timestamp_ns = timestamp_ns

    def period_ns(self, nominal_ns: float) -> float:
        """Least-squares camera-clock period; the nominal one until three frames exist."""

        if self.count < 3:
            return nominal_ns
        n = float(self.count)
        denominator = n * self.sum_xx - self.sum_x * self.sum_x
        if denominator <= 0:
            return nominal_ns
        return (n * self.sum_xy - self.sum_x * self.sum_y) / denominator


class RWDSegmentTracker:
    """Assign trigger slots to the frames of one sequencer run and detect any break.

    A run starts when the sequencer is enabled. Each camera's first frame takes that
    camera's first slot in the LED cycle; later frames must follow on the camera's own
    hardware clock with consecutive frame IDs.
    """

    def __init__(
        self,
        plan: RWDExcitationPlan,
        *,
        enabled_host_s: float,
        tolerance_fraction: float = 0.25,
        period_tolerance: float = 2e-4,
        frames_for_period_check: int = 20,
    ) -> None:
        if not 0 < tolerance_fraction < 0.5:
            raise ValueError("tolerance_fraction must be between 0 and 0.5")
        self._plan = plan
        self._enabled_host_s = enabled_host_s
        self._tolerance_fraction = tolerance_fraction
        self._period_tolerance = period_tolerance
        self._frames_for_period_check = frames_for_period_check
        self._tracks: dict[int, _CameraTrack] = {}
        self._slot0_host_s: float | None = None
        self._slot0_camera: int | None = None

    @property
    def start_delay_s(self) -> float | None:
        """Host-time delay from the enable command to slot 0, including camera latency."""

        if self._slot0_host_s is None:
            return None
        return self._slot0_host_s - self._enabled_host_s

    def first_timestamp_ns(self, camera: int) -> int | None:
        track = self._tracks.get(camera)
        return None if track is None else track.first_timestamp_ns

    def camera_period_ns(self, camera: int) -> float | None:
        track = self._tracks.get(camera)
        if track is None:
            return None
        return track.period_ns(self._plan.period_s * 1e9)

    def last_slot(self, camera: int) -> int | None:
        track = self._tracks.get(camera)
        return None if track is None else track.last_slot

    def observe(self, frame: RWDCameraFrame) -> RWDSlotAssignment | RWDContinuityBreak:
        plan = self._plan
        camera = frame.camera_index
        if camera not in plan.cameras:
            return RWDContinuityBreak(
                camera, f"camera {camera} delivered a frame but no enabled LED triggers it"
            )
        track = self._tracks.get(camera)
        if track is None:
            return self._first_frame(frame)
        if frame.frame_id <= track.last_frame_id:
            return RWDContinuityBreak(
                camera,
                f"camera {camera} repeated or reordered frame ID {frame.frame_id} after "
                f"{track.last_frame_id}",
            )
        if frame.frame_id != track.last_frame_id + 1:
            missing = frame.frame_id - track.last_frame_id - 1
            return RWDContinuityBreak(
                camera,
                f"camera {camera} frame ID jumped from {track.last_frame_id} to "
                f"{frame.frame_id}; {missing} frame(s) were not delivered",
            )
        nominal_ns = plan.period_s * 1e9
        period_ns = track.period_ns(nominal_ns)
        expected = plan.next_slot(camera, track.last_slot)
        predicted = track.first_timestamp_ns + (expected - track.first_slot) * period_ns
        residual_ns = frame.camera_timestamp_ns - predicted
        if abs(residual_ns) > self._tolerance_fraction * nominal_ns:
            step = (frame.camera_timestamp_ns - track.last_timestamp_ns) / period_ns
            expected_step = expected - track.last_slot
            if step - expected_step > 0.5:
                kind = "arrived after a missed exposure"
            elif step < expected_step - 0.5:
                kind = "arrived before its trigger slot"
            else:
                kind = "was off the trigger grid"
            return RWDContinuityBreak(
                camera,
                f"camera {camera} frame {frame.frame_id} {kind} "
                f"({residual_ns / 1e6:+.2f} ms from slot {expected})",
            )
        track.add(expected, frame.camera_timestamp_ns, frame.frame_id)
        if track.count >= self._frames_for_period_check:
            ratio = track.period_ns(nominal_ns) / nominal_ns
            if abs(ratio - 1.0) > self._period_tolerance:
                return RWDContinuityBreak(
                    camera,
                    f"RWD trigger period is {track.period_ns(nominal_ns) / 1e6:.4f} ms, not "
                    f"the commanded {nominal_ns / 1e6:.4f} ms at {plan.frame_rate_hz} Hz; "
                    "this frame rate is not supported by the board as commanded",
                    fatal=True,
                )
        return RWDSlotAssignment(expected, plan.wavelength_for_slot(expected))

    def _first_frame(self, frame: RWDCameraFrame) -> RWDSlotAssignment | RWDContinuityBreak:
        plan = self._plan
        camera = frame.camera_index
        slot = plan.first_slot(camera)
        slot0_host_s = frame.host_received_s - plan.exposure_s - slot * plan.period_s
        if self._slot0_host_s is None:
            self._slot0_host_s = slot0_host_s
            self._slot0_camera = camera
        elif abs(slot0_host_s - self._slot0_host_s) > plan.period_s / 2:
            offset_ms = (slot0_host_s - self._slot0_host_s) * 1e3
            return RWDContinuityBreak(
                camera,
                f"camera {camera}'s first frame does not line up with camera "
                f"{self._slot0_camera}'s ({offset_ms:+.1f} ms); a first trigger may have "
                "been missed",
            )
        self._tracks[camera] = _CameraTrack(
            first_slot=slot,
            first_timestamp_ns=frame.camera_timestamp_ns,
            last_slot=slot,
            last_frame_id=frame.frame_id,
            last_timestamp_ns=frame.camera_timestamp_ns,
        )
        return RWDSlotAssignment(slot, plan.wavelength_for_slot(slot))


@dataclass(frozen=True, slots=True)
class RWDAlignedEdge:
    edge: RWDInputEdge
    tick_us: int
    """The board tick, unwrapped, in microseconds (1 ms resolution)."""
    session_s: float


@dataclass(slots=True)
class _PendingEdge:
    received: RWDInputEdge
    tick_us: int
    host_s: float


class RWDTTLClock:
    """Map board-clock TTL edges onto the session clock defined by the trigger grid.

    The board's millisecond tick and its trigger grid share one oscillator, so a single
    offset (the board time of session zero) maps every edge exactly up to that offset.
    The offset is estimated from host arrival times: frames arrive at least one exposure
    after their trigger, and edges arrive at least a serial transfer after they happen.
    The estimate uses the earliest arrivals of both near each edge, so its remaining
    error is the difference between the camera readout and serial delivery floors (a few
    milliseconds; bench characterization is pending).

    Edges are held until ``settle_edges`` have arrived or ``settle_s`` has passed since
    the first, then the offset is frozen so intervals between edges stay exact.
    """

    def __init__(
        self,
        *,
        exposure_s: float,
        settle_edges: int = 8,
        settle_s: float = 10.0,
        window_s: float = 2.0,
    ) -> None:
        self.exposure_s = exposure_s
        self.settle_edges = settle_edges
        self.settle_s = settle_s
        self.window_s = window_s
        self.offset_s: float | None = None
        """Board time of session zero, in seconds, once frozen."""
        self.edges_used = 0
        self._unwrapper = TickUnwrapper(32)
        self._frames: list[tuple[float, float]] = []
        self._pending: list[_PendingEdge] = []

    @property
    def aligned(self) -> bool:
        return self.offset_s is not None

    def observe_frame(self, host_received_s: float, session_s: float) -> None:
        if self.offset_s is None:
            self._frames.append((host_received_s, host_received_s - session_s))

    def add_edge(self, edge: RWDInputEdge, host_s: float) -> None:
        """Queue one edge; raises ValueError if board ticks go backwards."""

        tick_us = self._unwrapper.unwrap(edge.tick_ms) * 1000
        self._pending.append(_PendingEdge(edge, tick_us, host_s))

    def release(self, host_now_s: float, *, final: bool = False) -> tuple[RWDAlignedEdge, ...]:
        """Return edges that can be placed on the session clock, freezing the offset."""

        if not self._pending:
            return ()
        if self.offset_s is None:
            settled = (
                len(self._pending) >= self.settle_edges
                or host_now_s - self._pending[0].host_s >= self.settle_s
            )
            if not (settled or final):
                return ()
            offset = self._estimate_offset()
            if offset is None:
                if not final:
                    return ()
                raise ValueError("TTL edges arrived but no camera frame can anchor them")
            self.offset_s = offset
            self.edges_used = len(self._pending)
            self._frames.clear()
        released = tuple(
            RWDAlignedEdge(item.received, item.tick_us, item.tick_us / 1e6 - self.offset_s)
            for item in self._pending
        )
        self._pending.clear()
        return released

    def _estimate_offset(self) -> float | None:
        best: float | None = None
        for item in self._pending:
            nearby = [
                offset
                for host_s, offset in self._frames
                if abs(host_s - item.host_s) <= self.window_s
            ]
            if not nearby:
                continue
            candidate = min(nearby) - (item.host_s - item.tick_us / 1e6)
            best = candidate if best is None else max(best, candidate)
        return None if best is None else best - self.exposure_s


class RWDBrightnessCheck:
    """QC only: flag a run whose first camera-1 frames look like 410 and 470 swapped.

    It never assigns or changes a wavelength. It applies only when camera 1 images both
    410 and 470 nm and their mean frame brightness is clearly different.
    """

    def __init__(
        self,
        plan: RWDExcitationPlan,
        *,
        history_frames: int = 10,
        separation_sigmas: float = 4.0,
    ) -> None:
        self.plan = plan
        self.history_frames = history_frames
        self.separation_sigmas = separation_sigmas
        self._stats: dict[Wavelength, list[float]] = {}
        self._segment_first: dict[Wavelength, float] = {}
        self._held: list[tuple[Wavelength, float]] = []
        self._segment = 0
        self._checked = True

    @property
    def applies(self) -> bool:
        return {Wavelength.CONTROL_410, Wavelength.GREEN_470} <= set(self.plan.order)

    def start_segment(self, index: int) -> None:
        self._segment = index
        self._segment_first.clear()
        self._held.clear()
        self._checked = index == 0

    def observe(self, wavelength: Wavelength, mean_value: float) -> str | None:
        """Record one camera-1 frame mean; return a message if the new run looks swapped."""

        if not self.applies or wavelength not in (Wavelength.CONTROL_410, Wavelength.GREEN_470):
            return None
        if self._checked:
            self._add(wavelength, mean_value)
            return None
        # Keep a new run's frames out of the history until they have been compared.
        self._segment_first.setdefault(wavelength, mean_value)
        self._held.append((wavelength, mean_value))
        if len(self._segment_first) < 2:
            return None
        self._checked = True
        message = self._compare()
        for held_wavelength, held_value in self._held:
            self._add(held_wavelength, held_value)
        self._held.clear()
        return message

    def _add(self, wavelength: Wavelength, mean_value: float) -> None:
        values = self._stats.setdefault(wavelength, [0.0, 0.0, 0.0])
        values[0] += 1
        delta = mean_value - values[1]
        values[1] += delta / values[0]
        values[2] += delta * (mean_value - values[1])

    def _compare(self) -> str | None:
        low, high = Wavelength.CONTROL_410, Wavelength.GREEN_470
        stats = [self._stats.get(w) for w in (low, high)]
        if any(s is None or s[0] < self.history_frames for s in stats):
            return None
        (n_low, mean_low, m2_low), (n_high, mean_high, m2_high) = stats  # type: ignore[misc]
        sigma = max(math.sqrt(m2_low / (n_low - 1)), math.sqrt(m2_high / (n_high - 1)), 1e-9)
        if abs(mean_low - mean_high) < self.separation_sigmas * sigma:
            return None
        first_low, first_high = self._segment_first[low], self._segment_first[high]
        direct = abs(first_low - mean_low) + abs(first_high - mean_high)
        swapped = abs(first_low - mean_high) + abs(first_high - mean_low)
        if swapped < direct:
            return (
                f"run {self._segment}: the first 410 nm frame (mean {first_low:.1f}) and "
                f"470 nm frame (mean {first_high:.1f}) look swapped against earlier means "
                f"{mean_low:.1f} and {mean_high:.1f}"
            )
        return None
