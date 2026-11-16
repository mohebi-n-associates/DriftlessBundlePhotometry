import time
from array import array
from pathlib import Path

import numpy as np
import pytest

from driftless_photometry.config import Wavelength, demo_config
from driftless_photometry.gui.main_window import MainWindow
from driftless_photometry.gui.trace_display import (
    HORIZONS,
    MAX_DISPLAY_POINTS,
    compact_history,
    compact_history_with_means,
    downsample_min_max,
    fitted_control_f0,
    refresh_interval_s,
    rolling_f0,
    trailing_mean,
)


def test_repeated_history_compaction_keeps_the_oldest_hours_visible() -> None:
    # Regression: compaction halved the whole history by point count on every pass, so
    # after 2.5 h at 40 Hz the first hour of the live trace was two points.
    rate, budget = 40.0, 16_000
    times = np.empty(0)
    values = np.empty(0)
    total = int(2.6 * 3600 * rate)
    for start in range(0, total, 4000):
        new_times = np.arange(start, min(total, start + 4000)) / rate
        new_values = np.sin(new_times)
        if start == 4000:
            new_values[123] = 50.0  # a brief spike early in the session
        times = np.concatenate((times, new_times))
        values = np.concatenate((values, new_values))
        if len(times) > 2 * budget:
            times, values = compact_history(times, values, budget)

    assert len(times) <= 2 * budget
    assert np.all(np.diff(times) > 0)
    for hour in range(2):
        assert np.count_nonzero((times >= hour * 3600) & (times < (hour + 1) * 3600)) > 1000
    assert values.max() == 50.0
    newest = times >= times[-1] - 60
    assert np.count_nonzero(newest) == 60 * rate + 1


def test_history_compaction_keeps_recent_samples_and_bucket_extrema() -> None:
    times = np.arange(1000, dtype=np.float64)
    values = np.cos(times)
    values[10] = np.nan

    x, y = compact_history(times, values, 100)

    assert len(x) <= 100
    np.testing.assert_array_equal(x[-25:], times[-25:])
    assert np.all(np.isfinite(y))
    assert y.max() == values[np.isfinite(values)].max()
    assert y.min() == values[np.isfinite(values)].min()


def _noisy_history(hours: float, rate: float = 40.0, budget: int = 16_000):
    """A noisy trace thinned the way the live display thins it, with its averages."""

    rng = np.random.default_rng(3)
    times = values = means = weights = np.empty(0)
    total = int(hours * 3600 * rate)
    for start in range(0, total, 4000):
        new_times = np.arange(start, min(total, start + 4000)) / rate
        new_values = 100.0 + rng.normal(0.0, 1.0, len(new_times))
        times = np.concatenate((times, new_times))
        values = np.concatenate((values, new_values))
        means = np.concatenate((means, new_values))
        weights = np.concatenate((weights, np.ones(len(new_times))))
        if len(times) > 2 * budget:
            times, values, means, weights = compact_history_with_means(
                times, values, means, weights, budget
            )
    return total, times, values, means, weights


def test_history_compaction_averages_account_for_every_sample() -> None:
    total, _, _, means, weights = _noisy_history(1.0)

    assert weights.sum() == pytest.approx(total)
    # Every sample's value is in the weighted averages, so the overall mean is exact.
    assert np.sum(weights * means) / weights.sum() == pytest.approx(100.0, abs=0.02)


def test_smoothed_old_history_is_not_noisier_than_recent_history() -> None:
    # Regression: smoothing averaged the kept bucket extremes of older history, which
    # are seconds apart, so it averaged almost nothing there. Old stretches looked far
    # noisier than recent ones, though the noise never changed.
    _, times, values, means, weights = _noisy_history(1.2)
    old = times < 1800
    recent = times > times[-1] - 60

    extremes = trailing_mean(times, values, window_s=1.0)
    averaged = trailing_mean(times, means, window_s=1.0, weights=weights)

    assert np.std(extremes[old]) > 5 * np.std(extremes[recent])
    assert np.std(averaged[old]) < 1.5 * np.std(averaged[recent])


def test_weighted_trailing_mean_counts_each_point_by_its_weight() -> None:
    times = np.asarray([0.0, 1.0, 2.0])
    values = np.asarray([0.0, 10.0, np.nan])
    weights = np.asarray([3.0, 1.0, 5.0])

    smoothed = trailing_mean(times, values, window_s=5.0, weights=weights)

    np.testing.assert_allclose(smoothed, [0.0, 2.5, 2.5])


def test_trailing_mean_uses_elapsed_time_and_preserves_input() -> None:
    times = np.asarray([0.0, 0.1, 0.4, 1.0], dtype=np.float64)
    values = np.asarray([1.0, 3.0, 5.0, 9.0], dtype=np.float32)
    original = values.copy()

    smoothed = trailing_mean(times, values, window_s=0.35)

    np.testing.assert_allclose(smoothed, [1.0, 2.0, 4.0, 9.0])
    np.testing.assert_array_equal(values, original)
    np.testing.assert_array_equal(trailing_mean(times, values, window_s=0), values)


def _reference_trailing_mean(times, values, window_s):
    """The original sample-by-sample loop, kept as the definition of the window."""
    output = np.full(len(values), np.nan)
    left, running_sum, finite_count = 0, 0.0, 0
    for right, value in enumerate(np.asarray(values, dtype=np.float64)):
        if np.isfinite(value):
            running_sum += value
            finite_count += 1
        while times[right] - times[left] > window_s:
            if np.isfinite(values[left]):
                running_sum -= values[left]
                finite_count -= 1
            left += 1
        if finite_count:
            output[right] = running_sum / finite_count
    return output


def test_trailing_mean_matches_the_sample_by_sample_definition() -> None:
    rng = np.random.default_rng(7)
    times = np.cumsum(rng.uniform(0.001, 0.05, 5_000))
    values = rng.normal(100.0, 5.0, 5_000).astype(np.float32)
    values[rng.choice(5_000, 200, replace=False)] = np.nan
    values[1_000:1_200] = np.nan  # a window with no finite samples stays NaN
    for window_s in (0.0, 0.03, 1.0, 7.5):
        np.testing.assert_allclose(
            trailing_mean(times, values, window_s=window_s),
            _reference_trailing_mean(times, values, window_s),
            rtol=1e-9,
            equal_nan=True,
        )


def test_trailing_mean_stays_fast_over_long_recordings() -> None:
    # The display smooths every ROI and wavelength on the GUI thread while recording;
    # a slow pass holds the GIL and starves camera delivery (frames lost, 2026-11-14).
    times = np.arange(200_000, dtype=np.float64) / 40.0
    values = np.random.default_rng(1).random(200_000).astype(np.float32)
    best = np.inf
    for _ in range(3):
        start = time.perf_counter()
        trailing_mean(times, values, window_s=1.0)
        best = min(best, time.perf_counter() - start)
    assert best < 0.1


def test_min_max_decimation_caps_points_and_preserves_brief_excursion() -> None:
    times = np.arange(20_000, dtype=np.float64) / 10
    values = np.sin(times).astype(np.float32)
    values[12_345] = 100.0

    display_times, display_values = downsample_min_max(
        times,
        values,
        first_index=0,
        max_points=400,
    )

    assert len(display_times) == len(display_values)
    assert len(display_times) <= 400
    assert display_values.max() == 100.0
    assert display_times[np.argmax(display_values)] == times[12_345]


def test_short_trace_is_copied_without_decimation() -> None:
    times = np.arange(10, dtype=np.float64)
    values = np.arange(10, dtype=np.float32)

    display_times, display_values = downsample_min_max(
        times,
        values,
        first_index=50,
        max_points=20,
    )

    np.testing.assert_array_equal(display_times, times)
    np.testing.assert_array_equal(display_values, values)
    assert not np.shares_memory(display_times, times)
    assert not np.shares_memory(display_values, values)


def test_trace_horizons_enable_as_recording_span_grows(qtbot, tmp_path: Path) -> None:
    window = MainWindow(output_directory=tmp_path, default_fibers=1)
    qtbot.addWidget(window)
    config = demo_config(tmp_path, fiber_count=1)

    assert HORIZONS == (
        ("15 s", 15.0),
        ("1 min", 60.0),
        ("10 min", 600.0),
        ("1 h", 3600.0),
        ("Full", None),
    )
    window._prepare_trace_curves(config)
    assert window._trace_horizon_s == 15.0
    assert window.horizon_buttons[15.0].isChecked()
    assert window.horizon_buttons[15.0].isEnabled()
    assert not window.horizon_buttons[60.0].isEnabled()

    window._update_trace_horizon_availability(60.0)
    assert window.horizon_buttons[15.0].isEnabled()
    assert window.horizon_buttons[60.0].isEnabled()
    assert not window.horizon_buttons[600.0].isEnabled()
    window.horizon_buttons[60.0].click()
    assert window._trace_horizon_s == 60.0
    assert window.horizon_buttons[60.0].isChecked()
    assert "QPushButton#horizon:checked" in window.styleSheet()


def test_full_trace_plot_sends_only_pixel_bounded_extrema(qtbot, tmp_path: Path) -> None:
    window = MainWindow(output_directory=tmp_path, default_fibers=1)
    qtbot.addWidget(window)
    window.resize(1200, 700)
    window.show()
    config = demo_config(tmp_path, fiber_count=1)
    window._prepare_trace_curves(config)
    count = 50_000
    times = np.arange(count, dtype=np.float64) / 10
    values = np.sin(times).astype(np.float32)
    values[25_000] = 500.0
    wavelength = Wavelength.CONTROL_405
    window._trace_times[(wavelength, 0)] = array("d", times)
    window._trace_values[(wavelength, 0)] = array("f", values)
    window._trace_first_s = float(times[0])
    window._trace_latest_s = float(times[-1])
    window._trace_horizon_s = None

    window._refresh_trace_wavelength(wavelength)

    drawn_times, drawn_values = window._curves[(wavelength, 0)].getData()
    assert len(drawn_times) <= MAX_DISPLAY_POINTS
    assert len(drawn_times) < count // 10
    assert np.max(drawn_values) == 500.0


def test_fitted_control_f0_recovers_the_linear_relation() -> None:
    control_times = np.arange(0.0, 10.0, 0.1)
    control = 100.0 + 5.0 * np.sin(control_times)
    signal_times = control_times + 0.05
    signal = 2.0 * (100.0 + 5.0 * np.sin(signal_times)) + 30.0

    f0 = fitted_control_f0(signal_times, signal, control_times, control, signal_times[10:20])

    assert f0 is not None
    np.testing.assert_allclose(f0, signal[10:20], rtol=2e-3)


def test_fitted_control_f0_needs_a_varying_control() -> None:
    times = np.arange(0.0, 1.0, 0.1)
    flat = np.full(len(times), 50.0)

    assert fitted_control_f0(times, flat + 1, times, flat, times) is None
    assert fitted_control_f0(times[:2], flat[:2], times[:2], flat[:2], times) is None


def test_rolling_f0_follows_drift_but_not_a_brief_transient() -> None:
    times = np.arange(0.0, 120.0, 0.05)
    drift = 100.0 + 0.5 * times  # slow bleaching-like drift
    values = drift.copy()
    values[(times >= 90.0) & (times < 91.0)] *= 1.2  # a 1 s transient

    f0 = rolling_f0(times, values, 20.0, times)

    late = times > 30
    assert np.allclose(f0[late & (times < 89)], drift[late & (times < 89)] - 5.0, atol=0.7)
    assert abs(f0[times == 90.5][0] - (drift[times == 90.5][0] - 5.0)) < 1.0
    dff = (values - f0) / f0 * 100
    assert dff[times == 90.5][0] > 15.0
    assert np.isfinite(f0).all()


def test_trace_lines_break_where_the_raw_samples_pause() -> None:
    import numpy as np

    from driftless_photometry.gui.trace_display import break_at_gaps

    raw = np.concatenate((np.arange(0, 2, 1 / 40), np.arange(4, 6, 1 / 40)))
    values = np.ones(len(raw))
    times, shown = break_at_gaps(raw, raw, values)
    assert len(times) == len(raw) + 1
    (gap,) = np.flatnonzero(np.isnan(shown))
    assert times[gap - 1] < times[gap] < times[gap + 1]
    assert times[gap] == (raw[79] + raw[80]) / 2

    steady = np.arange(0, 10, 1 / 40)
    same_times, same = break_at_gaps(steady, steady, np.ones(len(steady)))
    assert len(same_times) == len(steady) and not np.isnan(same).any()

    # Downsampled display points may be sparse; only raw pauses break the line.
    coarse = steady[::40]
    _coarse_times, coarse_values = break_at_gaps(steady, coarse, np.ones(len(coarse)))
    assert not np.isnan(coarse_values).any()


def test_refresh_interval_follows_screen_resolution() -> None:
    # A short span redraws ten times a second; a long one only as often as new data
    # moves the trace by a pixel, and at least once a second.
    assert refresh_interval_s(15.0, 1500.0) == 0.1
    assert refresh_interval_s(600.0, 1500.0) == 0.4
    assert refresh_interval_s(3600.0, 1500.0) == 1.0
    assert refresh_interval_s(15.0, 0.0) == 0.1


def test_time_axis_mode_follows_the_visible_end() -> None:
    from driftless_photometry.gui.trace_display import time_axis_mode

    assert [time_axis_mode(v) for v in (0, 59.9, 60, 3599, 3600, 9000)] == [
        "s", "s", "min", "min", "h", "h",
    ]  # fmt: skip


def test_time_ticks_use_round_clock_steps() -> None:
    from driftless_photometry.gui.trace_display import time_tick_spacing

    assert time_tick_spacing(15, 15) == (1.0, 0.2)
    assert time_tick_spacing(545, 15) == (60.0, 15.0)
    assert time_tick_spacing(7200, 15) == (600.0, 120.0)
    assert time_tick_spacing(7200, 6) == (1800.0, 600.0)
    assert time_tick_spacing(0, 10) == (1.0, 0.5)


def test_session_time_labels() -> None:
    from driftless_photometry.gui.trace_display import format_session_time as fmt

    assert fmt(12, 1, "s") == "12"
    assert fmt(12.5, 0.5, "s") == "12.5"
    assert fmt(65, 5, "min") == "1:05"
    assert fmt(500.5, 0.5, "min") == "8:20.5"
    assert fmt(119.999, 1, "min") == "2:00"  # rounding carries into the minute
    assert fmt(3900, 300, "h") == "1:05:00"
    assert fmt(-30, 10, "min") == "-0:30"
