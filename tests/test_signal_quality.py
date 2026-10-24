from driftless_photometry.signal_quality import SignalQualityMonitor


def feed(monitor, start_s, stop_s, value, roi=0, wavelength=470, rate_hz=20):
    for index in range(round((stop_s - start_s) * rate_hz)):
        monitor.add(roi, wavelength, start_s + index / rate_hz, value)


def test_a_steady_bright_fiber_has_no_warnings() -> None:
    monitor = SignalQualityMonitor()
    feed(monitor, 0.0, 30.0, 1500.0)
    assert monitor.warnings(29.95) == {}


def test_a_dark_fiber_is_flagged_after_two_seconds() -> None:
    monitor = SignalQualityMonitor()
    feed(monitor, 0.0, 1.5, 22.0)
    assert monitor.warnings(1.45) == {}
    feed(monitor, 1.5, 3.0, 22.0)
    assert monitor.warnings(2.95) == {0: ["dark at 470 nm (22 counts)"]}


def test_a_sudden_drop_is_shown_for_a_while_then_becomes_drift() -> None:
    monitor = SignalQualityMonitor()
    feed(monitor, 0.0, 6.0, 1000.0)
    feed(monitor, 6.0, 7.0, 500.0)
    assert monitor.warnings(6.95) == {0: ["sudden 50% drop at 470 nm"]}
    feed(monitor, 7.0, 24.0, 500.0)
    # The first 10 s (6 s bright, 4 s dropped) are the drift baseline.
    (text,) = monitor.warnings(23.95)[0]
    assert text.startswith("drifted -3") and text.endswith("at 470 nm since the start")


def test_a_brief_jump_clears_after_it_is_shown() -> None:
    monitor = SignalQualityMonitor()
    feed(monitor, 0.0, 6.0, 1000.0)
    feed(monitor, 6.0, 7.0, 1600.0)
    assert monitor.warnings(6.95) == {0: ["sudden 60% jump at 470 nm"]}
    feed(monitor, 7.0, 30.0, 1000.0)
    assert monitor.warnings(29.95) == {}


def test_slow_drift_from_the_start_is_flagged() -> None:
    monitor = SignalQualityMonitor()
    for second in range(40):
        feed(monitor, second, second + 1, 1000.0 * (1 - 0.012 * second))
    (text,) = monitor.warnings(39.95)[0]
    assert text.startswith("drifted -3") and text.endswith("at 470 nm since the start")


def test_warnings_are_per_roi_and_wavelength() -> None:
    monitor = SignalQualityMonitor()
    feed(monitor, 0.0, 3.0, 1500.0, roi=0)
    feed(monitor, 0.0, 3.0, 10.0, roi=1, wavelength=560)
    assert monitor.warnings(2.95) == {1: ["dark at 560 nm (10 counts)"]}
