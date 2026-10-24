"""Named TTL events: classify each input pulse by its width, as OFRS's event names do."""

from __future__ import annotations

from collections.abc import Sequence

import numpy as np

from driftless_photometry.config import SessionConfig, TTLEventRuleConfig
from driftless_photometry.domain import EventMark, TraceSample, TTLEdge
from driftless_photometry.roi import rois_for_camera


class TTLPulseClassifier:
    """Pair each input's rising and falling edges and name the pulse by its width.

    A pulse runs from a rising edge to the next falling edge on the same input. Widths
    come from the controller ticks (1 ms resolution on RWD). The first rule whose
    [min, max) range holds the width names the pulse; unmatched pulses are not marked.
    """

    def __init__(self, rules: Sequence[TTLEventRuleConfig]) -> None:
        self._rules = tuple(rules)
        self._lines = {rule.line for rule in self._rules}
        self._rising: dict[int, TTLEdge] = {}

    def observe(self, edge: TTLEdge) -> EventMark | None:
        if edge.line not in self._lines:
            return None
        if edge.edge == "rising":
            self._rising[edge.line] = edge
            return None
        rising = self._rising.pop(edge.line, None)
        if rising is None:
            return None
        width_ms = (edge.controller_tick_us - rising.controller_tick_us) / 1000
        for rule in self._rules:
            if rule.line == edge.line and rule.min_width_ms <= width_ms < rule.max_width_ms:
                return EventMark(
                    timestamp_s=rising.timestamp_s,
                    name=rule.name,
                    source="ttl",
                    detail=f"TTL input {edge.line} ({edge.label}) pulse of {width_ms:g} ms",
                    ttl_line=edge.line,
                )
        return None


class DffThresholdWatcher:
    """Watch one ROI's live dF/F and report when it crosses an output's threshold.

    F0 is the median of the ROI's values at that wavelength over the recording's first
    ``baseline_s`` seconds, as the live dF/F display computes it. A trigger needs the
    signal to have been on the other side of the threshold first, and then waits out the
    refractory time and the program's own length.
    """

    def __init__(self, config: SessionConfig, port: int) -> None:
        output = next(o for o in config.source.ttl_outputs if o.port == port)  # type: ignore[union-attr]
        trigger = output.dff_trigger
        assert trigger is not None
        self.port = port
        self._trigger = trigger
        self._wait_s = max(trigger.refractory_s, output.program_s or 0.0)
        self._index: dict[int, int] = {}
        for camera in (1, 2):
            fibers = [roi.fiber_id for roi in rois_for_camera(config.enabled_rois, camera)]
            if trigger.fiber_id in fibers:
                self._index[camera] = fibers.index(trigger.fiber_id)
        self._baseline: list[float] = []
        self._f0: float | None = None
        self._armed = False
        self._last_trigger_s = -np.inf

    @classmethod
    def for_config(cls, config: SessionConfig) -> tuple[DffThresholdWatcher, ...]:
        outputs = getattr(config.source, "ttl_outputs", ())
        return tuple(cls(config, o.port) for o in outputs if o.dff_trigger is not None)

    def observe(self, sample: TraceSample, camera_index: int) -> str | None:
        trigger = self._trigger
        if sample.wavelength_nm != trigger.wavelength_nm or camera_index not in self._index:
            return None
        value = float(sample.values[self._index[camera_index]])
        if self._f0 is None:
            if sample.timestamp_s < trigger.baseline_s:
                if np.isfinite(value):
                    self._baseline.append(value)
                return None
            if not self._baseline:
                return None
            self._f0 = float(np.median(self._baseline))
            if abs(self._f0) <= np.finfo(np.float32).eps:
                return None
        dff = (value - self._f0) / self._f0 * 100.0
        beyond = (
            dff >= trigger.threshold_percent
            if trigger.direction == "above"
            else dff <= trigger.threshold_percent
        )
        if not beyond:
            self._armed = True
            return None
        if not self._armed or sample.timestamp_s - self._last_trigger_s < self._wait_s:
            return None
        self._armed = False
        self._last_trigger_s = sample.timestamp_s
        sign = ">=" if trigger.direction == "above" else "<="
        return (
            f"ROI {trigger.fiber_id} {int(trigger.wavelength_nm)} nm dF/F {dff:.1f} % "
            f"{sign} {trigger.threshold_percent:g} % (F0 {self._f0:.1f})"
        )
