import numpy as np
import pytest

from driftless_photometry.config import ROIConfig
from driftless_photometry.roi import circular_mask, extract_circular_rois, roi_bounding_box


def test_extracts_mean_without_mutating_source() -> None:
    frame = np.arange(100, dtype=np.uint16).reshape(10, 10)
    original = frame.copy()
    roi = ROIConfig(
        fiber_id="f1",
        label="Fiber 1",
        animal_id="animal-01",
        brain_region="NAc",
        sensor_type="dLight1.3b",
        center_x_px=5,
        center_y_px=5,
        radius_px=2,
    )
    mask = circular_mask(frame.shape, roi)
    result = extract_circular_rois(frame, (roi,), bit_depth=12)
    assert result.values[0] == pytest.approx(frame[mask].mean())
    assert result.pixel_counts[0] == mask.sum()
    np.testing.assert_array_equal(frame, original)


def test_saturation_fraction_uses_bit_depth() -> None:
    frame = np.zeros((9, 9), dtype=np.uint16)
    roi = ROIConfig(
        fiber_id="f1",
        label="Fiber 1",
        animal_id="animal-01",
        brain_region="NAc",
        sensor_type="dLight1.3b",
        center_x_px=4,
        center_y_px=4,
        radius_px=1,
    )
    mask = circular_mask(frame.shape, roi)
    frame[mask] = 4095
    result = extract_circular_rois(frame, (roi,), bit_depth=12)
    assert result.saturation_fractions[0] == pytest.approx(1.0)


def test_bounding_box_clamps_to_frame() -> None:
    rois = (
        ROIConfig(
            fiber_id="a",
            label="A",
            animal_id="animal-a",
            brain_region="NAc",
            sensor_type="dLight1.3b",
            center_x_px=5,
            center_y_px=5,
            radius_px=5,
        ),
        ROIConfig(
            fiber_id="b",
            label="B",
            animal_id="animal-b",
            brain_region="DMS",
            sensor_type="GRAB-DA2m",
            center_x_px=15,
            center_y_px=15,
            radius_px=4,
        ),
    )
    assert roi_bounding_box(rois, (20, 20), margin_px=2) == (0, 0, 20, 20)
