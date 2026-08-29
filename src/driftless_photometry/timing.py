"""Deterministic wavelength scheduling and clock utilities."""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from .config import ChannelConfig, Wavelength


class WavelengthSchedule:
    """Cycle explicitly through enabled channels in configured order."""

    def __init__(self, channels: tuple[ChannelConfig, ...]) -> None:
        enabled = tuple(channel for channel in channels if channel.enabled)
        if not enabled:
            raise ValueError("at least one channel must be enabled")
        self._channels = enabled

    def channel_for_sequence(self, sequence: int) -> ChannelConfig:
        if sequence < 0:
            raise ValueError("sequence must be non-negative")
        return self._channels[sequence % len(self._channels)]

    @property
    def wavelengths(self) -> tuple[Wavelength, ...]:
        return tuple(channel.wavelength_nm for channel in self._channels)


class TickUnwrapper:
    """Unwrap a monotonically sampled unsigned hardware timer."""

    def __init__(self, bit_width: int = 32) -> None:
        if not 2 <= bit_width <= 64:
            raise ValueError("bit_width must be between 2 and 64")
        self._modulus = 1 << bit_width
        self._half_modulus = self._modulus // 2
        self._previous: int | None = None
        self._wraps = 0

    def unwrap(self, raw_tick: int) -> int:
        if not 0 <= raw_tick < self._modulus:
            raise ValueError("raw tick is outside timer range")
        if self._previous is not None and raw_tick < self._previous:
            backward = self._previous - raw_tick
            if backward > self._half_modulus:
                self._wraps += 1
            else:
                raise ValueError("out-of-order hardware tick")
        self._previous = raw_tick
        return raw_tick + self._wraps * self._modulus


@dataclass(slots=True)
class LinearClockMapper:
    """Affine map from controller microseconds to NWB session seconds."""

    controller_ticks_us: list[int] = field(default_factory=list)
    session_times_s: list[float] = field(default_factory=list)

    def add_anchor(self, controller_tick_us: int, session_time_s: float) -> None:
        if self.controller_ticks_us and controller_tick_us <= self.controller_ticks_us[-1]:
            raise ValueError("clock anchors must have increasing controller ticks")
        if self.session_times_s and session_time_s <= self.session_times_s[-1]:
            raise ValueError("clock anchors must have increasing session times")
        self.controller_ticks_us.append(controller_tick_us)
        self.session_times_s.append(session_time_s)

    def map(self, controller_tick_us: int) -> float:
        if not self.controller_ticks_us:
            raise RuntimeError("at least one clock anchor is required")
        ticks_s = np.asarray(self.controller_ticks_us, dtype=np.float64) * 1e-6
        session = np.asarray(self.session_times_s, dtype=np.float64)
        if len(ticks_s) == 1:
            return float(session[0] + controller_tick_us * 1e-6 - ticks_s[0])
        slope, intercept = np.polyfit(ticks_s, session, 1)
        return float(slope * controller_tick_us * 1e-6 + intercept)
