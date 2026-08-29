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
