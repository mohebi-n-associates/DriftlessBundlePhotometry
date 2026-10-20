"""Simulated RWD cameras triggered by a ``SimulatedRWDBoard``, on a virtual clock.

The cameras follow the bench-measured trigger behavior of an R821:

- each sequencer run triggers slot ``k`` at ``start + delay + k / fps``;
- interleaved modes cycle 410 → 470 → 560 nm (the enabled ones), starting on the first;
- the board triggers camera 1 for 410/470 nm and camera 2 for 560 nm;
- stopping the sequencer cuts the exposure in flight short.

Tests can make a camera miss a trigger, lose a frame after acquiring it, deliver a
frame late, disconnect, or run on a different trigger period. ``get`` advances the
shared ``VirtualClock`` to each frame's arrival, so sessions run instantly and
deterministically. This is a test aid, not evidence about the hardware.
"""

from __future__ import annotations

import heapq
import math
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

import numpy as np
from numpy.typing import NDArray

from driftless_photometry.config import SessionConfig

from .alvium import AlviumFrame, AlviumSettings
from .rwd_board_simulator import SimulatedRWDBoard, SimulatedRWDRun
from .rwd_controller_protocol import RWDExcitationMode, RWDLed

_MODE_LEDS = {
    RWDExcitationMode.INTERLEAVED_410_470: (RWDLed.LED_410, RWDLed.LED_470),
    RWDExcitationMode.INTERLEAVED_470_560: (RWDLed.LED_470, RWDLed.LED_560),
    RWDExcitationMode.INTERLEAVED_410_470_560: tuple(RWDLed),
}
_CAMERA_FOR_LED = {RWDLed.LED_410: "1", RWDLed.LED_470: "1", RWDLed.LED_560: "2"}
_FIBER_LEVEL = {RWDLed.LED_410: 600.0, RWDLed.LED_470: 1400.0, RWDLed.LED_560: 1000.0}


class VirtualClock:
    """A shared clock that advances a little on every read and jumps when told to."""

    def __init__(self, start_s: float = 100.0, step_s: float = 1e-5) -> None:
        self.now_s = start_s
        self.step_s = step_s

    def __call__(self) -> float:
        self.now_s += self.step_s
        return self.now_s

    def advance_to(self, at_s: float) -> None:
        self.now_s = max(self.now_s, at_s)


@dataclass(order=True, slots=True)
class _Pending:
    deliver_s: float
    order: int
    camera: str = field(compare=False)
    frame_id: int = field(compare=False)
    trigger_s: float = field(compare=False)
    led: RWDLed = field(compare=False)
    run: SimulatedRWDRun = field(compare=False)
    run_index: int = field(compare=False)
    slot: int = field(compare=False)
    lost: bool = field(compare=False)


class SimulatedRWDCameras:
    """Two simulated Alvium cameras with the ``AlviumTriggeredCameras`` read interface."""

    def __init__(
        self,
        board: SimulatedRWDBoard,
        clock: VirtualClock,
        config: SessionConfig,
        *,
        user_ids: Iterable[str] = ("1", "2"),
        settings: AlviumSettings | None = None,
        start_delay_s: float = 0.0005,
        readout_s: float = 0.003,
        jitter_s: float = 0.0004,
        camera_ppm: dict[str, float] | None = None,
        camera_offset_ns: dict[str, int] | None = None,
        trigger_period: Callable[[int], float] | None = None,
        seed: int = 3,
    ) -> None:
        self._board = board
        self._clock = clock
        self._user_ids = tuple(user_ids)
        exposure_us = settings.exposure_us if settings else float(config.camera.exposure_us)
        self._exposure_s = exposure_us * 1e-6
        self._start_delay_s = start_delay_s
        self._readout_s = readout_s
        self._jitter_s = jitter_s
        self._ppm = {"1": 25.0, "2": -15.0, **(camera_ppm or {})}
        self._offset_ns = {"1": 7_000_000_000, "2": 3_000_000_000, **(camera_offset_ns or {})}
        self._trigger_period = trigger_period or (lambda fps: 1.0 / fps)
        self._rng = np.random.default_rng(seed)
        self._shape = (config.camera.height_px, config.camera.width_px)
        self._masks = {
            "1": self._fiber_mask(
                (roi.center_x_px, roi.center_y_px, roi.radius_px) for roi in config.rois
            ),
            "2": self._fiber_mask(
                (
                    roi.camera2_center_x_px or roi.center_x_px,
                    roi.camera2_center_y_px or roi.center_y_px,
                    roi.camera2_radius_px or roi.radius_px,
                )
                for roi in config.rois
            ),
        }
        self._next_slot: dict[int, int] = {}
        self._next_id = dict.fromkeys(self._user_ids, 0)
        self._pending: list[_Pending] = []
        self._order = 0
        self.missed: set[tuple[str, int, int]] = set()
        """(camera, run, slot) triggers the camera never acquires."""
        self.lost: set[tuple[str, int, int]] = set()
        """(camera, run, slot) frames acquired (frame ID used) but never delivered."""
        self.late: dict[tuple[str, int, int], float] = {}
        """Extra delivery delay in seconds for (camera, run, slot)."""
        self.disconnect_at_s: float | None = None
        self.delivered: list[AlviumFrame] = []
        self.delivered_items: list[_Pending] = []
        self.open = False

    def __enter__(self) -> SimulatedRWDCameras:
        self.open = True
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.open = False

    def trigger_time(self, frame: AlviumFrame) -> float:
        """The clock time at which a delivered frame was triggered."""

        index = next(i for i, item in enumerate(self.delivered) if item is frame)
        return self.delivered_items[index].trigger_s

    def get(self, timeout_s: float) -> AlviumFrame | None:
        if not self.open:
            raise RuntimeError("simulated cameras are not open")
        horizon = self._clock.now_s + timeout_s
        while True:
            trigger = self._next_trigger()
            next_delivery = self._pending[0].deliver_s if self._pending else math.inf
            if next_delivery <= horizon and next_delivery <= (trigger[0] if trigger else math.inf):
                item = heapq.heappop(self._pending)
                self._clock.advance_to(item.deliver_s)
                if item.lost or self._disconnected(item.deliver_s):
                    continue
                frame = self._render(item)
                self.delivered.append(frame)
                self.delivered_items.append(item)
                return frame
            if trigger is not None and trigger[0] <= horizon:
                self._acquire(*trigger)
                continue
            self._clock.advance_to(horizon)
            return None

    # Simulation ----------------------------------------------------------------------
    def _next_trigger(self) -> tuple[float, int, int] | None:
        best: tuple[float, int, int] | None = None
        for index, run in enumerate(self._board.runs):
            slot = self._next_slot.get(index, 0)
            if run.frame_rate_hz <= 0:
                continue
            at_s = (
                run.start_s + self._start_delay_s + slot * self._trigger_period(run.frame_rate_hz)
            )
            if run.stop_s is not None and at_s >= run.stop_s:
                continue
            if best is None or at_s < best[0]:
                best = (at_s, index, slot)
        return best

    def _acquire(self, at_s: float, run_index: int, slot: int) -> None:
        self._next_slot[run_index] = slot + 1
        run = self._board.runs[run_index]
        led = self._led_for(run, slot)
        if led is None:
            return
        camera = _CAMERA_FOR_LED[led]
        key = (camera, run_index, slot)
        if camera not in self._user_ids or key in self.missed:
            return
        frame_id = self._next_id[camera]
        self._next_id[camera] += 1
        deliver = at_s + self._exposure_s + self._readout_s + self.late.get(key, 0.0)
        deliver += float(self._rng.uniform(0.0, self._jitter_s))
        self._order += 1
        heapq.heappush(
            self._pending,
            _Pending(
                deliver,
                self._order,
                camera,
                frame_id,
                at_s,
                led,
                run,
                run_index,
                slot,
                key in self.lost,
            ),
        )

    def _led_for(self, run: SimulatedRWDRun, slot: int) -> RWDLed | None:
        mode = RWDExcitationMode(run.excitation_mode)
        if mode is RWDExcitationMode.CONTINUOUS:
            enabled = [led for led in RWDLed if run.led_enabled[led]]
            return enabled[0] if enabled else None
        leds = _MODE_LEDS[mode]
        return leds[slot % len(leds)]

    def _disconnected(self, at_s: float) -> bool:
        return self.disconnect_at_s is not None and at_s >= self.disconnect_at_s

    def _render(self, item: _Pending) -> AlviumFrame:
        run = item.run
        lit = self._exposure_s
        if run.stop_s is not None and item.trigger_s + lit > run.stop_s:
            lit = max(0.0, run.stop_s - item.trigger_s)
        fraction = lit / self._exposure_s
        power = run.led_value[item.led] / 10_000 if run.led_enabled[item.led] else 0.0
        level = _FIBER_LEVEL[item.led] * fraction * (0.5 + 10.0 * power)
        image = np.full(self._shape, 100, dtype=np.uint16)
        image += (self._masks[item.camera] * min(level, 3900.0)).astype(np.uint16)
        timestamp_ns = self._offset_ns[item.camera] + round(
            item.trigger_s * (1.0 + self._ppm[item.camera] * 1e-6) * 1e9
        )
        return AlviumFrame(item.camera, item.frame_id, timestamp_ns, image, item.deliver_s)

    def _fiber_mask(self, circles: Iterable[tuple[float, float, float]]) -> NDArray[np.float32]:
        height, width = self._shape
        y, x = np.mgrid[0:height, 0:width]
        mask = np.zeros(self._shape, dtype=np.float32)
        for center_x, center_y, radius in circles:
            mask[(x - center_x) ** 2 + (y - center_y) ** 2 <= radius**2] = 1.0
        return mask
