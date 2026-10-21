from __future__ import annotations

import pytest

from driftless_photometry.gui.warmup import LEDWarmup, format_minutes


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_warmup_counts_led_on_time_until_five_minutes() -> None:
    clock = Clock()
    warmup = LEDWarmup(clock=clock)
    assert warmup.warm_s() == 0 and not warmup.is_warm

    warmup.leds_on()
    clock.now += 299
    assert warmup.warm_s() == pytest.approx(299) and not warmup.is_warm
    warmup.leds_on()  # a second "on" while lit changes nothing
    clock.now += 1
    assert warmup.is_warm


def test_warmup_bridges_short_gaps_and_goes_cold_after_long_ones() -> None:
    clock = Clock()
    warmup = LEDWarmup(cooldown_s=180, clock=clock)
    warmup.leds_on()
    clock.now += 200
    warmup.leds_off()

    clock.now += 60  # e.g. the preview between Live view and recording
    assert warmup.warm_s() == pytest.approx(200)
    warmup.leds_on()
    clock.now += 100
    assert warmup.is_warm
    warmup.leds_off()

    clock.now += 181
    assert warmup.warm_s() == 0
    warmup.leds_on()
    assert warmup.warm_s() == 0
    warmup.leds_off()
    warmup.leds_off()  # a second "off" changes nothing


def test_format_minutes() -> None:
    assert format_minutes(0) == "0:00"
    assert format_minutes(75.9) == "1:15"
    assert format_minutes(300) == "5:00"
    assert format_minutes(-3) == "0:00"
