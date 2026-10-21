"""Named TTL events: classify each input pulse by its width, as OFRS's event names do."""

from __future__ import annotations

from collections.abc import Sequence

from driftless_photometry.config import TTLEventRuleConfig
from driftless_photometry.domain import EventMark, TTLEdge


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
                )
        return None
