from array import array
from pathlib import Path

import numpy as np

from driftless_photometry.config import Wavelength, demo_config
from driftless_photometry.gui.main_window import MainWindow
from driftless_photometry.gui.trace_display import (
    HORIZONS,
    MAX_DISPLAY_POINTS,
    downsample_min_max,
)


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
