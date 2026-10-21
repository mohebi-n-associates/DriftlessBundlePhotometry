"""LED warm-up bookkeeping for RWD's recommendation to run the LEDs before recording."""

from __future__ import annotations

import time
from collections.abc import Callable

RWD_WARMUP_S = 300.0
"""RWD's user manual recommends warming the LEDs up for 5 minutes before recording."""

WARMUP_COOLDOWN_S = 180.0
"""LEDs off for longer than this count as cold again. A short gap, such as running the
required preview between Live view and the recording, keeps the warm-up."""


class LEDWarmup:
    """Track how long the excitation LEDs have been on, bridging short off gaps."""

    def __init__(
        self,
        *,
        required_s: float = RWD_WARMUP_S,
        cooldown_s: float = WARMUP_COOLDOWN_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.required_s = required_s
        self._cooldown_s = cooldown_s
        self._clock = clock
        self._on_since: float | None = None
        self._off_at: float | None = None
        self._warm_at_off_s = 0.0

    @property
    def leds_are_on(self) -> bool:
        return self._on_since is not None

    def leds_on(self) -> None:
        if self._on_since is not None:
            return
        now = self._clock()
        carried = self._warm_at_off_s if self._recently_off(now) else 0.0
        self._on_since = now - carried

    def leds_off(self) -> None:
        if self._on_since is None:
            return
        now = self._clock()
        self._warm_at_off_s = now - self._on_since
        self._off_at = now
        self._on_since = None

    def warm_s(self) -> float:
        now = self._clock()
        if self._on_since is not None:
            return now - self._on_since
        return self._warm_at_off_s if self._recently_off(now) else 0.0

    @property
    def is_warm(self) -> bool:
        return self.warm_s() >= self.required_s

    def _recently_off(self, now: float) -> bool:
        return self._off_at is not None and now - self._off_at <= self._cooldown_s


def format_minutes(seconds: float) -> str:
    whole = max(0, int(seconds))
    return f"{whole // 60}:{whole % 60:02d}"
