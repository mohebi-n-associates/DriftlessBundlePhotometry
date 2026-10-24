"""Display-only trace windowing and anchored min/max decimation."""

from __future__ import annotations

import numpy as np
from numpy.typing import NDArray

HORIZONS: tuple[tuple[str, float | None], ...] = (
    ("15 s", 15.0),
    ("1 min", 60.0),
    ("10 min", 600.0),
    ("1 h", 3600.0),
    ("Full", None),
)

MAX_DISPLAY_POINTS = 4000
MAX_HISTORY_POINTS = 8 * MAX_DISPLAY_POINTS


def compact_history(times: NDArray, values: NDArray, budget: int) -> tuple[NDArray, NDArray]:
    """Bound display memory, retaining endpoints and chronological bucket extrema."""
    x, y = downsample_min_max(times[1:-1], values[1:-1], first_index=1, max_points=budget - 2)
    x = np.concatenate((times[:1], x, times[-1:]))
    y = np.concatenate((values[:1], y, values[-1:]))
    unique = np.concatenate(([True], np.diff(x) > 0))
    return x[unique], y[unique]


def trailing_mean(
    times: NDArray[np.float64],
    values: NDArray[np.floating],
    *,
    window_s: float,
) -> NDArray[np.float64]:
    """Return a display-only time-windowed trailing mean in linear time."""

    x = np.asarray(times, dtype=np.float64)
    y = np.asarray(values, dtype=np.float64)
    if x.ndim != 1 or y.ndim != 1 or x.shape != y.shape:
        raise ValueError("times and values must be one-dimensional arrays of equal length")
    if not np.isfinite(window_s) or window_s < 0:
        raise ValueError("smoothing window must be finite and non-negative")
    if len(x) > 1 and np.any(np.diff(x) <= 0):
        raise ValueError("smoothing timestamps must strictly increase")
    if window_s == 0 or len(x) == 0:
        return y.copy()
    output = np.full(len(y), np.nan, dtype=np.float64)
    left = 0
    running_sum = 0.0
    finite_count = 0
    for right, value in enumerate(y):
        if np.isfinite(value):
            running_sum += value
            finite_count += 1
        while x[right] - x[left] > window_s:
            departing = y[left]
            if np.isfinite(departing):
                running_sum -= departing
                finite_count -= 1
            left += 1
        if finite_count:
            output[right] = running_sum / finite_count
    return output


def compact_duration(seconds: float) -> str:
    """Return a short human-readable span for the availability hint."""

    if seconds < 90:
        return f"{seconds:.0f} s"
    if seconds < 3600:
        return f"{seconds / 60:.4g} min"
    return f"{seconds / 3600:.4g} h"


def downsample_min_max(
    times: NDArray[np.float64],
    values: NDArray[np.floating],
    *,
    first_index: int,
    max_points: int,
) -> tuple[NDArray[np.float64], NDArray[np.float64]]:
    """Return at most ``max_points`` while retaining each bucket's extrema.

    Bucket widths are powers of two and boundaries are anchored to absolute sample
    index. Extrema remain in their original temporal order, so brief excursions stay
    visible without sending every acquired point to the graphics pipeline.
    """

    x = np.asarray(times, dtype=np.float64)
    y = np.asarray(values, dtype=np.float64)
    if x.ndim != 1 or y.ndim != 1 or x.shape != y.shape:
        raise ValueError("times and values must be one-dimensional arrays of equal length")
    if first_index < 0:
        raise ValueError("first_index must be non-negative")
    if max_points < 2:
        raise ValueError("max_points must be at least 2")
    if len(x) <= max_points:
        return x.copy(), y.copy()

    target_buckets = max(1, max_points // 2)
    samples_per_bucket = len(x) / target_buckets
    bucket_width = 1 << int(np.ceil(np.log2(max(1.0, samples_per_bucket))))
    lead = first_index % bucket_width
    bucket_count = int(np.ceil((lead + len(x)) / bucket_width))
    while bucket_count * 2 > max_points:
        bucket_width *= 2
        lead = first_index % bucket_width
        bucket_count = int(np.ceil((lead + len(x)) / bucket_width))
    padded_count = bucket_count * bucket_width
    padded_values = np.full(padded_count, np.nan, dtype=np.float64)
    padded_values[lead : lead + len(y)] = y
    buckets = padded_values.reshape(bucket_count, bucket_width)
    finite = np.isfinite(buckets)
    usable = finite.any(axis=1)
    safe_min = np.where(finite, buckets, np.inf)
    safe_max = np.where(finite, buckets, -np.inf)
    minimum_offsets = np.argmin(safe_min, axis=1)
    maximum_offsets = np.argmax(safe_max, axis=1)
    bases = np.arange(bucket_count, dtype=np.int64) * bucket_width - lead
    minimum_indices = bases + minimum_offsets
    maximum_indices = bases + maximum_offsets
    first_extrema = np.minimum(minimum_indices, maximum_indices)
    second_extrema = np.maximum(minimum_indices, maximum_indices)
    indices = np.column_stack((first_extrema, second_extrema)).reshape(-1)
    valid_indices = np.repeat(usable, 2) & (indices >= 0) & (indices < len(x))
    selected = indices[valid_indices]
    return x[selected].copy(), y[selected].copy()


FIT_MAX_POINTS = 20_000
"""Least-squares fits use at most this many evenly spaced samples, so long sessions stay
responsive; the fit of a slowly varying control barely changes."""


def fitted_control_f0(
    signal_times: NDArray[np.float64],
    signal_values: NDArray[np.floating],
    control_times: NDArray[np.float64],
    control_values: NDArray[np.floating],
    at_times: NDArray[np.float64],
) -> NDArray[np.float64] | None:
    """F0 for dF/F from the isosbestic control trace (410 nm on RWD, 405 nm on Custom).

    The control is interpolated onto the signal's sample times and fitted to the signal
    by least squares, ``signal ~ slope * control + intercept``, over every sample both
    traces cover. The fitted control at ``at_times`` is F0. Returns None until three
    finite pairs exist, or while the control does not vary.
    """

    if len(control_times) < 3 or len(signal_times) < 3:
        return None
    # Fit on evenly strided samples of both traces, so the cost stays bounded however
    # long the recording runs; a slowly varying control barely changes the fit.
    first = int(np.searchsorted(signal_times, control_times[0], side="left"))
    last = int(np.searchsorted(signal_times, control_times[-1], side="right"))
    stride = max(1, (last - first) // FIT_MAX_POINTS)
    times = signal_times[first:last:stride]
    signal = np.asarray(signal_values[first:last:stride], dtype=np.float64)
    control_stride = max(1, len(control_times) // FIT_MAX_POINTS)
    control_grid = control_times[::control_stride]
    control = np.interp(
        times, control_grid, np.asarray(control_values[::control_stride], dtype=np.float64)
    )
    finite = np.isfinite(control) & np.isfinite(signal)
    if np.count_nonzero(finite) < 3 or np.ptp(control[finite]) <= 0:
        return None
    slope, intercept = np.polyfit(control[finite], signal[finite], 1)
    if len(at_times) == 0:
        return np.empty(0, dtype=np.float64)
    # Evaluate on just the control samples around the requested times.
    low = max(0, int(np.searchsorted(control_times, at_times[0], side="right")) - 1)
    high = min(len(control_times), int(np.searchsorted(control_times, at_times[-1])) + 1)
    fitted = np.interp(
        at_times,
        control_times[low:high],
        np.asarray(control_values[low:high], dtype=np.float64),
    )
    return slope * fitted + intercept


ROLLING_BINS_PER_WINDOW = 30
"""Rolling F0 takes the median of this many bin means per window, so it stays cheap over
hours of samples while one transient barely moves it."""


def rolling_f0(
    times: NDArray[np.float64],
    values: NDArray[np.floating],
    window_s: float,
    at_times: NDArray[np.float64],
) -> NDArray[np.float64]:
    """A trailing baseline: at each of ``at_times``, the median of the preceding window.

    Samples are averaged in bins of ``window_s / ROLLING_BINS_PER_WINDOW``; F0 at a bin is
    the median of the bin means over the window ending there, and is interpolated to
    ``at_times``. Before a full window has passed, the window is whatever came before.
    """

    if window_s <= 0:
        raise ValueError("the rolling window must be positive")
    values = np.asarray(values, dtype=np.float64)
    finite = np.isfinite(values)
    times, values = times[finite], values[finite]
    if len(times) == 0 or len(at_times) == 0:
        return np.full(len(at_times), np.nan)
    width = window_s / ROLLING_BINS_PER_WINDOW
    start = times[0]
    index = np.floor((times - start) / width).astype(np.int64)
    edges = np.flatnonzero(np.diff(index)) + 1
    starts = np.concatenate(([0], edges))
    sums = np.add.reduceat(values, starts)
    counts = np.diff(np.concatenate((starts, [len(values)])))
    means = sums / counts
    bins = index[starts]
    # Evaluate only the bins surrounding requested times. A dense grid from session
    # zero grew with duration/window and could allocate gigabytes for a short window.
    position = np.clip((at_times - start) / width - 1, 0, bins[-1])
    left = np.floor(position).astype(np.int64)
    right = np.minimum(left + 1, bins[-1])
    needed, inverse = np.unique(np.concatenate((left, right)), return_inverse=True)
    baseline = np.empty(len(needed), dtype=np.float64)
    for offset in range(0, len(needed), 1024):
        grid = needed[offset : offset + 1024, None] - np.arange(ROLLING_BINS_PER_WINDOW)[None, :]
        lookup = np.searchsorted(bins, grid, side="right") - 1
        windows = means[np.maximum(lookup, 0)]
        windows[grid < 0] = np.nan
        baseline[offset : offset + len(grid)] = np.nanmedian(windows, axis=1)
    n = len(at_times)
    lower, upper = baseline[inverse[:n]], baseline[inverse[n:]]
    return lower + (upper - lower) * (position - left)


GAP_MIN_S = 0.25
"""A pause in a trace longer than this, and than GAP_FACTOR sample periods, is a gap."""
GAP_FACTOR = 4.0


def break_at_gaps(
    raw_times: NDArray[np.float64],
    display_times: NDArray[np.float64],
    display_values: NDArray[np.floating],
) -> tuple[NDArray[np.float64], NDArray[np.float64]]:
    """Insert a NaN wherever the raw samples pause, so the line is not drawn across it.

    Gaps are found in ``raw_times`` (the samples behind the display), not in the
    downsampled display points, whose spacing grows with the visible span.
    """

    values = np.asarray(display_values, dtype=np.float64)
    if len(raw_times) < 3 or len(display_times) == 0:
        return display_times, values
    recent = np.diff(raw_times[-512:])
    period = float(np.median(recent)) if len(recent) else 0.0
    threshold = max(GAP_MIN_S, GAP_FACTOR * period)
    gaps = np.flatnonzero(np.diff(raw_times) > threshold)
    if len(gaps) == 0:
        return display_times, values
    middles = (raw_times[gaps] + raw_times[gaps + 1]) / 2.0
    positions = np.searchsorted(display_times, middles)
    return (
        np.insert(display_times, positions, middles),
        np.insert(values, positions, np.nan),
    )
