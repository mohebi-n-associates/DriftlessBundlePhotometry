from __future__ import annotations

import pytest
from pydantic import ValidationError

from driftless_photometry.config import TTLEventRuleConfig
from driftless_photometry.domain import EventMark, TTLEdge
from driftless_photometry.events import TTLPulseClassifier


def _edge(line: int, edge: str, tick_ms: int, label: str = "lever") -> TTLEdge:
    return TTLEdge(tick_ms / 1000, tick_ms * 1000, line, label, edge, edge == "rising", 0)


def test_pulses_are_named_by_width_at_their_rising_edge() -> None:
    classifier = TTLPulseClassifier(
        (
            TTLEventRuleConfig(line=1, min_width_ms=0, max_width_ms=20, name="Short"),
            TTLEventRuleConfig(line=1, min_width_ms=20, max_width_ms=100, name="Long"),
        )
    )

    assert classifier.observe(_edge(1, "rising", 1000)) is None
    assert classifier.observe(_edge(1, "falling", 1010)) == EventMark(
        1.0, "Short", "ttl", "TTL input 1 (lever) pulse of 10 ms"
    )
    classifier.observe(_edge(1, "rising", 2000))
    assert classifier.observe(_edge(1, "falling", 2020)).name == "Long"  # [min, max)
    classifier.observe(_edge(1, "rising", 3000))
    assert classifier.observe(_edge(1, "falling", 3100)) is None  # no rule fits


def test_unpaired_edges_and_other_inputs_are_not_marked() -> None:
    classifier = TTLPulseClassifier((TTLEventRuleConfig(line=2, max_width_ms=1000, name="Tone"),))

    assert classifier.observe(_edge(2, "falling", 500)) is None  # no rising edge yet
    assert classifier.observe(_edge(1, "rising", 600)) is None
    assert classifier.observe(_edge(1, "falling", 700)) is None
    classifier.observe(_edge(2, "rising", 800))
    classifier.observe(_edge(2, "rising", 900))  # a repeated rise restarts the pulse
    assert classifier.observe(_edge(2, "falling", 950)).timestamp_s == pytest.approx(0.9)


def test_ttl_event_rules_need_a_positive_width_range() -> None:
    with pytest.raises(ValidationError, match="maximum width above its minimum"):
        TTLEventRuleConfig(line=1, min_width_ms=50, max_width_ms=50, name="Bad")
