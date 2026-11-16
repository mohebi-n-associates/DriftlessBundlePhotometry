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

MIN_REFRESH_INTERVAL_S = 0.1
MAX_REFRESH_INTERVAL_S = 1.0


def refresh_interval_s(span_s: float, width_px: float) -> float:
    """How often a live trace spanning ``span_s`` on ``width_px`` pixels needs redrawing.

    New data moves the trace by one pixel every ``span_s / width_px`` seconds, and a
    redraw sooner changes nothing visible, so long spans redraw less often. The
    interval stays between 0.1 s, so short spans look live, and 1 s, so the newest data
    never waits long.
    """

    if width_px <= 0 or not np.isfinite(span_s):
        return MIN_REFRESH_INTERVAL_S
    return min(MAX_REFRESH_INTERVAL_S, max(MIN_REFRESH_INTERVAL_S, span_s / width_px))


TimeAxisMode = str  # "s", "min", or "h"

TIME_AXIS_LABELS = {
    "s": "Session time (s)",
    "min": "Session time (min:s)",
    "h": "Session time (h:min:s)",
}

_NICE_TIME_STEPS_S = (
    0.01, 0.02, 0.05, 0.1, 0.2, 0.5, 1, 2, 5, 10, 15, 30,
    60, 120, 300, 600, 900, 1800, 3600, 7200, 10800, 21600, 43200, 86400,
)  # fmt: skip


def time_axis_mode(visible_max_s: float) -> TimeAxisMode:
    """Seconds below a minute, min:s below an hour, then h:min:s."""

    if visible_max_s < 60:
        return "s"
    return "min" if visible_max_s < 3600 else "h"


def time_tick_spacing(span_s: float, max_ticks: float) -> tuple[float, float]:
    """Major and minor tick steps: round clock steps, at most ``max_ticks`` majors."""

    if not np.isfinite(span_s) or span_s <= 0:
        return 1.0, 0.5
    wanted = span_s / max(1.0, max_ticks)
    major = next((s for s in _NICE_TIME_STEPS_S if s >= wanted), _NICE_TIME_STEPS_S[-1])
    smaller = [
        s
        for s in _NICE_TIME_STEPS_S
        if s < major and abs(major / s - round(major / s)) < 1e-9 and major / s <= 6
    ]
    if not smaller:
        return float(major), float(major / 2)
    # About four or five minor steps per major one: 1 min -> 15 s, 10 min -> 2 min.
    minor = min(smaller, key=lambda s: (abs(major / s - 4.5), major / s))
    return float(major), float(minor)


def format_session_time(value_s: float, spacing_s: float, mode: TimeAxisMode) -> str:
    """One tick label: ``12`` in seconds mode, ``8:20`` in minutes, ``1:05:00`` in hours.

    Fractions of a second are shown only when ticks are less than a second apart.
    """

    decimals = 0 if spacing_s >= 1 else min(2, int(np.ceil(-np.log10(spacing_s) - 1e-9)))
    sign = "-" if value_s < 0 else ""
    value = round(abs(value_s), decimals)
    if mode == "s":
        return f"{sign}{value:.{decimals}f}"
    seconds = value % 60
    width = 2 if decimals == 0 else 3 + decimals
    second_text = f"{seconds:0{width}.{decimals}f}"
    minutes = int(value // 60)
    if mode == "min":
        return f"{sign}{minutes}:{second_text}"
    return f"{sign}{minutes // 60}:{minutes % 60:02d}:{second_text}"


def compact_history(times: NDArray, values: NDArray, budget: int) -> tuple[NDArray, NDArray]:
    """Bound display memory to ``budget`` points, keeping recent samples and extrema.

    See ``compact_history_with_means``, which this calls without averages.
    """

    x, y, _, _ = compact_history_with_means(times, values, values, np.ones(len(values)), budget)
    return x, y


def compact_history_with_means(
    times: NDArray,
    values: NDArray,
    means: NDArray,
    weights: NDArray,
    budget: int,
) -> tuple[NDArray, NDArray, NDArray, NDArray]:
    """Bound display memory to ``budget`` points, keeping recent samples and extrema.

    The newest quarter of the budget stays unthinned. Older samples fall into time
    buckets, half evenly spaced and half narrowing toward the present, and each bucket
    keeps its minimum and maximum in time order. The buckets are set by time, not by
    point count, so compacting an already compacted history again only merges
    neighbouring buckets, and every stretch of the session keeps points however often
    this runs. (Halving by point count thinned the oldest data again on every pass, until
    the first hour of a long recording was a few points joined by straight lines.)

    ``means`` and ``weights`` give each point the average of the samples it stands for
    and how many there are: the value itself and 1 for an original sample. Each kept
    point gets its bucket's weighted average and an equal share of its weight, so a
    smoothed view can average the samples rather than the extremes that were kept.
    """

    x = np.asarray(times, dtype=np.float64)
    y = np.asarray(values, dtype=np.float64)
    m = np.asarray(means, dtype=np.float64)
    w = np.asarray(weights, dtype=np.float64)
    if x.ndim != 1 or x.shape != y.shape or x.shape != m.shape or x.shape != w.shape:
        raise ValueError(
            "times, values, means, and weights must be one-dimensional arrays of equal length"
        )
    if budget < 16:
        raise ValueError("budget must be at least 16")
    if len(x) <= budget:
        return x.copy(), y.copy(), m.copy(), w.copy()
    recent = budget // 4
    split = len(x) - recent
    newest = x[-1]
    youngest_age = newest - x[split - 1]
    oldest_age = newest - x[0]
    # Half the buckets are even in time, so the Full view has a floor everywhere; half
    # narrow with age, so the recent minutes and hours stay detailed.
    half = (budget - recent) // 4
    edges = np.linspace(x[0], x[split - 1], half + 1)
    if youngest_age > 0 and oldest_age > youngest_age:
        ages = youngest_age * (oldest_age / youngest_age) ** np.linspace(1.0, 0.0, half + 1)
        edges = np.union1d(edges, newest - ages)
    bucket_count = len(edges) - 1
    old_x, old_y = x[:split], y[:split]
    all_buckets = np.clip(np.searchsorted(edges, old_x, side="right") - 1, 0, bucket_count - 1)
    finite = np.isfinite(old_y)
    buckets, positions = all_buckets[finite], np.flatnonzero(finite)
    finite_y = old_y[finite]
    by_value = np.lexsort((finite_y, buckets))
    first = np.concatenate(([True], np.diff(buckets[by_value]) != 0))
    last = np.concatenate((first[1:], [True]))
    kept = np.unique(np.concatenate((positions[by_value[first]], positions[by_value[last]])))
    old_m, old_w = m[:split], w[:split]
    averaged = finite & np.isfinite(old_m) & (old_w > 0)
    weight_sums = np.bincount(
        all_buckets[averaged], weights=old_w[averaged], minlength=bucket_count
    )
    weighted_sums = np.bincount(
        all_buckets[averaged], weights=(old_w * old_m)[averaged], minlength=bucket_count
    )
    kept_buckets = all_buckets[kept]
    kept_per_bucket = np.bincount(kept_buckets, minlength=bucket_count)
    bucket_weight = weight_sums[kept_buckets]
    with np.errstate(divide="ignore", invalid="ignore"):
        kept_means = np.where(
            bucket_weight > 0, weighted_sums[kept_buckets] / bucket_weight, np.nan
        )
    kept_weights = bucket_weight / kept_per_bucket[kept_buckets]
    return (
        np.concatenate((old_x[kept], x[split:])),
        np.concatenate((old_y[kept], y[split:])),
        np.concatenate((kept_means, m[split:])),
        np.concatenate((kept_weights, w[split:])),
    )


def trailing_mean(
    times: NDArray[np.float64],
    values: NDArray[np.floating],
    *,
    window_s: float,
    weights: NDArray[np.floating] | None = None,
) -> NDArray[np.float64]:
    """Return a display-only time-windowed trailing mean in linear time.

    ``weights``, if given, counts how many samples each value stands for, as in a
    thinned display history whose points carry their bucket's average.
    """

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
    # Vectorized: this runs for every ROI and wavelength on the GUI thread, and a Python
    # loop over a long recording held the GIL long enough to starve camera delivery.
    # Sample i averages the finite samples j <= i with x[i] - x[j] <= window_s.
    if weights is None:
        w = np.ones(len(y), dtype=np.float64)
    else:
        w = np.asarray(weights, dtype=np.float64)
        if w.shape != y.shape:
            raise ValueError("weights must match values")
    finite = np.isfinite(y) & (w > 0)
    sums = np.concatenate(([0.0], np.cumsum(np.where(finite, w * y, 0.0))))
    counts = np.concatenate(([0.0], np.cumsum(np.where(finite, w, 0.0))))
    left = np.searchsorted(x, x - window_s, side="left")
    right = np.arange(1, len(x) + 1)
    window_counts = counts[right] - counts[left]
    output = np.full(len(y), np.nan, dtype=np.float64)
    # Weights are at least 0.5, so a smaller difference is cumulative-sum rounding.
    np.divide(sums[right] - sums[left], window_counts, out=output, where=window_counts > 0.25)
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


def gap_midpoints(raw_times: NDArray[np.float64]) -> NDArray[np.float64]:
    """The middle of every pause in ``raw_times``, which must be unthinned samples."""

    raw_times = np.asarray(raw_times, dtype=np.float64)
    if len(raw_times) < 3:
        return np.empty(0, dtype=np.float64)
    recent = np.diff(raw_times[-512:])
    period = float(np.median(recent)) if len(recent) else 0.0
    threshold = max(GAP_MIN_S, GAP_FACTOR * period)
    gaps = np.flatnonzero(np.diff(raw_times) > threshold)
    return (raw_times[gaps] + raw_times[gaps + 1]) / 2.0


def break_at_gaps(
    raw_times: NDArray[np.float64],
    display_times: NDArray[np.float64],
    display_values: NDArray[np.floating],
    known_gaps_s: NDArray[np.float64] | None = None,
) -> tuple[NDArray[np.float64], NDArray[np.float64]]:
    """Insert a NaN wherever the samples paused, so the line is not drawn across it.

    Gaps are found in ``raw_times``, unthinned samples behind the display, not in the
    downsampled display points, whose spacing grows with the visible span. Where the
    display history was thinned, its points are far apart everywhere, so gaps found
    before thinning are passed as ``known_gaps_s`` instead (midpoints, in seconds).
    """

    values = np.asarray(display_values, dtype=np.float64)
    if len(display_times) == 0:
        return display_times, values
    middles = gap_midpoints(raw_times)
    if known_gaps_s is not None and len(known_gaps_s):
        known = np.asarray(known_gaps_s, dtype=np.float64)
        known = known[(known > display_times[0]) & (known < display_times[-1])]
        middles = np.sort(np.concatenate((known, middles)))
    if len(middles) == 0:
        return display_times, values
    positions = np.searchsorted(display_times, middles)
    return (
        np.insert(display_times, positions, middles),
        np.insert(values, positions, np.nan),
    )
