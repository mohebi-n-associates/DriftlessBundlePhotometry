"""Circular fiber ROI geometry and extraction."""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from math import ceil, floor

import numpy as np
from numpy.typing import NDArray

from .config import ROIConfig

_ANNOTATION_COLORS: tuple[tuple[int, int, int], ...] = (
    (67, 214, 223),
    (183, 229, 50),
    (242, 177, 52),
    (224, 68, 80),
    (155, 122, 246),
    (255, 126, 182),
    (86, 203, 157),
    (83, 155, 245),
    (238, 139, 74),
)


@dataclass(frozen=True, slots=True)
class ROIExtraction:
    values: NDArray[np.float32]
    saturation_fractions: NDArray[np.float32]
    pixel_counts: NDArray[np.int32]


def rois_for_camera(rois: tuple[ROIConfig, ...], camera_index: int) -> tuple[ROIConfig, ...]:
    """Return the circles as placed on ``camera_index``; camera 2 uses its own geometry."""

    if camera_index == 1:
        return rois
    if camera_index != 2:
        raise ValueError(f"no ROI geometry for camera {camera_index}")
    placed = []
    for roi in rois:
        if not roi.has_camera2_geometry:
            raise ValueError(f"ROI {roi.fiber_id} has no camera-2 geometry")
        placed.append(
            roi.model_copy(
                update={
                    "center_x_px": roi.camera2_center_x_px,
                    "center_y_px": roi.camera2_center_y_px,
                    "radius_px": roi.camera2_radius_px,
                }
            )
        )
    return tuple(placed)


def circular_mask(shape: tuple[int, int], roi: ROIConfig) -> NDArray[np.bool_]:
    """Return a boolean `[y, x]` mask including pixels on the circle boundary."""

    height, width = shape
    if height <= 0 or width <= 0:
        raise ValueError("mask shape must be positive")
    yy, xx = np.ogrid[:height, :width]
    distance_sq = (xx - roi.center_x_px) ** 2 + (yy - roi.center_y_px) ** 2
    return distance_sq <= roi.radius_px**2


@lru_cache(maxsize=32)
def _cached_mask(
    shape: tuple[int, int], center_x_px: float, center_y_px: float, radius_px: float
) -> NDArray[np.bool_]:
    """A read-only circular mask; extraction reuses it for every frame of a session."""

    height, width = shape
    if height <= 0 or width <= 0:
        raise ValueError("mask shape must be positive")
    yy, xx = np.ogrid[:height, :width]
    mask = (xx - center_x_px) ** 2 + (yy - center_y_px) ** 2 <= radius_px**2
    mask.setflags(write=False)
    return mask


def extract_circular_rois(
    frame: NDArray[np.uint16],
    rois: tuple[ROIConfig, ...],
    *,
    bit_depth: int,
) -> ROIExtraction:
    """Extract mean raw counts and saturation fraction for every ROI."""

    if frame.dtype != np.uint16 or frame.ndim != 2:
        raise TypeError("frame must be a two-dimensional uint16 array")
    if not 1 <= bit_depth <= 16:
        raise ValueError("bit_depth must be between 1 and 16")
    saturation_value = (1 << bit_depth) - 1
    values = np.empty(len(rois), dtype=np.float32)
    saturation = np.empty(len(rois), dtype=np.float32)
    counts = np.empty(len(rois), dtype=np.int32)
    for index, roi in enumerate(rois):
        mask = _cached_mask(frame.shape, roi.center_x_px, roi.center_y_px, roi.radius_px)
        pixels = frame[mask]
        if pixels.size == 0:
            raise ValueError(f"ROI {roi.fiber_id} contains no pixels")
        values[index] = np.float32(pixels.mean(dtype=np.float64))
        saturation[index] = np.float32(np.count_nonzero(pixels >= saturation_value) / pixels.size)
        counts[index] = pixels.size
    return ROIExtraction(values, saturation, counts)


def roi_bounding_box(
    rois: tuple[ROIConfig, ...],
    frame_shape: tuple[int, int],
    *,
    margin_px: int = 0,
) -> tuple[int, int, int, int]:
    """Return a clamped `(x, y, width, height)` crop containing all circles."""

    if not rois:
        raise ValueError("at least one ROI is required")
    if margin_px < 0:
        raise ValueError("margin_px must be non-negative")
    height, width = frame_shape
    left = max(0, floor(min(roi.center_x_px - roi.radius_px for roi in rois)) - margin_px)
    top = max(0, floor(min(roi.center_y_px - roi.radius_px for roi in rois)) - margin_px)
    right = min(width, ceil(max(roi.center_x_px + roi.radius_px for roi in rois)) + margin_px)
    bottom = min(height, ceil(max(roi.center_y_px + roi.radius_px for roi in rois)) + margin_px)
    return left, top, right - left, bottom - top


AUTO_CONTRAST_PERCENTILES = (0.5, 99.9)
"""The live display's Auto contrast stretch, reused for auto-contrast saved maps."""


def auto_contrast_levels(frame: NDArray[np.uint16]) -> tuple[float, float]:
    """Display levels from the frame's own dim and bright pixels."""

    low, high = (float(v) for v in np.percentile(frame, AUTO_CONTRAST_PERCENTILES))
    return low, max(high, low + 1.0)


def render_annotated_rois(
    frame: NDArray[np.uint16],
    rois: tuple[ROIConfig, ...],
    *,
    bit_depth: int,
    levels: tuple[float, float] | None = None,
) -> NDArray[np.uint8]:
    """Render fixed-scale RGB diagnostic imagery with colored circular ROI outlines.

    The source frame is never mutated or normalized per frame. Counts are mapped to
    8-bit grayscale using the configured camera bit depth, then the derived RGB copy
    receives a three-pixel-wide outline for every ROI.
    """

    if frame.dtype != np.uint16 or frame.ndim != 2:
        raise TypeError("frame must be a two-dimensional uint16 array")
    if not 1 <= bit_depth <= 16:
        raise ValueError("bit_depth must be between 1 and 16")
    maximum = (1 << bit_depth) - 1
    if levels is None:
        scaled = np.minimum(frame, maximum).astype(np.uint32)
        grayscale = ((scaled * 255) // maximum).astype(np.uint8)
    else:
        # A derived display stretch (auto contrast); the raw frame is untouched.
        low, high = levels
        stretched = (frame.astype(np.float64) - low) / (high - low)
        grayscale = np.rint(np.clip(stretched, 0.0, 1.0) * 255).astype(np.uint8)
    annotated = np.repeat(grayscale[:, :, np.newaxis], 3, axis=2)
    yy, xx = np.ogrid[: frame.shape[0], : frame.shape[1]]
    for index, roi in enumerate(rois):
        distance_sq = (xx - roi.center_x_px) ** 2 + (yy - roi.center_y_px) ** 2
        inner_radius = max(0.0, roi.radius_px - 1.5)
        outer_radius = roi.radius_px + 1.5
        outline = (distance_sq >= inner_radius**2) & (distance_sq <= outer_radius**2)
        annotated[outline] = _ANNOTATION_COLORS[index % len(_ANNOTATION_COLORS)]
    return annotated
