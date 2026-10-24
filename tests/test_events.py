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
        1.0, "Short", "ttl", "TTL input 1 (lever) pulse of 10 ms", ttl_line=1
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


def _rwd_dff_config(tmp_path, **trigger):
    from driftless_photometry.config import (
        DffTriggerConfig,
        RWDSourceConfig,
        RWDTTLOutputConfig,
        SessionConfig,
        demo_config,
    )

    payload = demo_config(tmp_path, fiber_count=2, system="rwd").model_dump()
    output = RWDTTLOutputConfig(
        port=1,
        frequency_hz=20.0,
        pulse_width_ms=10,
        train_s=1.0,
        trigger="dff_threshold",
        dff_trigger=DffTriggerConfig(fiber_id="fiber_02", **trigger),
    )
    payload["source"] = RWDSourceConfig(ttl_outputs=(output,)).model_dump()
    return SessionConfig.model_validate(payload)


def _sample(t: float, values, wavelength=470):
    import numpy as np

    from driftless_photometry.config import Wavelength
    from driftless_photometry.domain import TraceSample

    array = np.asarray(values, dtype=np.float32)
    return TraceSample(t, 0, 0, 0, Wavelength(wavelength), array, np.zeros_like(array))


def test_dff_watcher_triggers_on_crossings_after_its_baseline(tmp_path) -> None:
    from driftless_photometry.events import DffThresholdWatcher

    config = _rwd_dff_config(tmp_path, threshold_percent=10.0, baseline_s=1.0, refractory_s=2.0)
    (watcher,) = DffThresholdWatcher.for_config(config)

    assert watcher.observe(_sample(0.5, [999.0, 100.0]), 1) is None  # baseline
    assert watcher.observe(_sample(1.1, [999.0, 100.0]), 1) is None  # armed below
    assert watcher.observe(_sample(1.2, [999.0, 100.0], wavelength=410), 1) is None
    reason = watcher.observe(_sample(1.3, [999.0, 112.0]), 1)
    assert reason is not None and "fiber_02 470 nm dF/F 12.0 %" in reason
    assert watcher.observe(_sample(1.4, [999.0, 115.0]), 1) is None  # not re-armed
    watcher.observe(_sample(1.5, [999.0, 100.0]), 1)
    assert watcher.observe(_sample(2.0, [999.0, 120.0]), 1) is None  # refractory
    watcher.observe(_sample(3.4, [999.0, 100.0]), 1)
    assert watcher.observe(_sample(3.5, [999.0, 120.0]), 1) is not None


def test_dff_trigger_settings_must_name_an_enabled_roi_and_wavelength(tmp_path) -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="not an enabled wavelength"):
        _rwd_dff_config(tmp_path, wavelength_nm=405)
