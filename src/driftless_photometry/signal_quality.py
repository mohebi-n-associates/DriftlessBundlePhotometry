"""Per-ROI signal checks on the live traces: dark fibers, sudden changes, and drift.

Display diagnostics only: they read the same ROI means the traces show and never alter
recorded data. A recording also logs each new warning as a system event, so the NWB
files say when a fiber went dark or moved.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

DARK_COUNTS = 50.0
"""A mean below this (12-bit counts) means a dark fiber: disconnected, or LED too low."""
DARK_HOLD_S = 2.0
STEP_FRACTION = 0.30
"""A 1 s mean this far from the 5 s before it is a sudden change (fiber moved?)."""
STEP_SHOWN_S = 15.0
DRIFT_FRACTION = 0.30
"""A 10 s mean this far from the first 10 s is drift."""
BASELINE_S = 10.0
_HISTORY_S = 12.0


@dataclass
class _Series:
    points: deque[tuple[float, float]] = field(default_factory=deque)
    first_s: float | None = None
    start_sum: float = 0.0
    start_count: int = 0
    step: tuple[float, str] | None = None

    def mean(self, start_s: float, stop_s: float) -> float | None:
        values = [v for t, v in self.points if start_s < t <= stop_s]
        return sum(values) / len(values) if values else None


class SignalQualityMonitor:
    """Feed ROI means as they arrive; ask for each ROI's current warnings."""

    def __init__(self) -> None:
        self._series: dict[tuple[int, int], _Series] = {}

    def add(self, roi: int, wavelength_nm: int, timestamp_s: float, value: float) -> None:
        series = self._series.setdefault((roi, wavelength_nm), _Series())
        # The drift baseline starts with the first lit sample, so a fiber connected
        # after the start is not compared with its dark period.
        if series.first_s is None and value >= DARK_COUNTS:
            series.first_s = timestamp_s
        if series.first_s is not None and timestamp_s - series.first_s <= BASELINE_S:
            series.start_sum += value
            series.start_count += 1
        series.points.append((timestamp_s, value))
        while series.points and series.points[0][0] < timestamp_s - _HISTORY_S:
            series.points.popleft()

    def warnings(self, now_s: float) -> dict[int, list[str]]:
        """Each ROI's warnings at session time ``now_s``, most serious first."""

        found: dict[int, list[str]] = {}
        for (roi, wavelength), series in sorted(self._series.items()):
            text = self._check(series, wavelength, now_s)
            if text:
                found.setdefault(roi, []).append(text)
        return found

    @staticmethod
    def _check(series: _Series, wavelength: int, now_s: float) -> str | None:
        recent = series.mean(now_s - 1.0, now_s)
        if recent is None:
            return None
        if recent < DARK_COUNTS:
            # Dark only once the data have stayed dark for DARK_HOLD_S.
            held = series.mean(now_s - DARK_HOLD_S, now_s)
            covered = series.points[0][0] <= now_s - DARK_HOLD_S + 0.1
            if covered and held is not None and held < DARK_COUNTS:
                return f"dark at {wavelength} nm ({recent:.0f} counts)"
            return None
        before = series.mean(now_s - 6.0, now_s - 1.0)
        if before is not None and before >= DARK_COUNTS:
            change = recent / before - 1.0
            if abs(change) >= STEP_FRACTION:
                word = "jump" if change > 0 else "drop"
                series.step = (now_s, f"sudden {abs(change):.0%} {word} at {wavelength} nm")
        if series.step is not None:
            if now_s - series.step[0] <= STEP_SHOWN_S:
                return series.step[1]
            series.step = None
        if (
            series.first_s is not None
            and now_s - series.first_s >= 2 * BASELINE_S
            and series.start_count
        ):
            start = series.start_sum / series.start_count
            current = series.mean(now_s - BASELINE_S, now_s)
            if start >= DARK_COUNTS and current is not None:
                change = current / start - 1.0
                if abs(change) >= DRIFT_FRACTION:
                    return f"drifted {change:+.0%} at {wavelength} nm since the start"
        return None
