import numpy as np
import pytest

from driftless_photometry.config import ROIConfig
from driftless_photometry.roi import (
    circular_mask,
    extract_circular_rois,
    render_annotated_rois,
    roi_bounding_box,
    rois_for_camera,
)


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


def test_roi_annotation_is_fixed_scale_and_does_not_mutate_source() -> None:
    roi = ROIConfig(
        fiber_id="f1",
        label="Fiber 1",
        animal_id="animal-01",
        brain_region="NAc",
        sensor_type="dLight1.3b",
        center_x_px=5,
        center_y_px=5,
        radius_px=3,
    )
    frame = np.full((12, 12), 2048, dtype=np.uint16)
    original = frame.copy()

    annotated = render_annotated_rois(frame, (roi,), bit_depth=12)

    assert annotated.dtype == np.uint8
    assert annotated.shape == (12, 12, 3)
    np.testing.assert_array_equal(frame, original)
    np.testing.assert_array_equal(annotated[5, 5], [127, 127, 127])
    np.testing.assert_array_equal(annotated[5, 8], [67, 214, 223])


def _roi(**geometry) -> ROIConfig:
    return ROIConfig(
        fiber_id="f1",
        label="Fiber 1",
        animal_id="animal-01",
        brain_region="NAc",
        sensor_type="dLight1.3b",
        center_x_px=5,
        center_y_px=5,
        radius_px=2,
        **geometry,
    )


def test_rois_for_camera_uses_each_cameras_own_circle() -> None:
    roi = _roi(camera2_center_x_px=7.0, camera2_center_y_px=3.0, camera2_radius_px=1.5)

    assert rois_for_camera((roi,), 1) == (roi,)
    (placed,) = rois_for_camera((roi,), 2)
    assert (placed.center_x_px, placed.center_y_px, placed.radius_px) == (7.0, 3.0, 1.5)
    assert placed.fiber_id == roi.fiber_id

    with pytest.raises(ValueError, match="has no camera-2 geometry"):
        rois_for_camera((_roi(),), 2)
    with pytest.raises(ValueError, match="no ROI geometry for camera 3"):
        rois_for_camera((roi,), 3)


def test_labeled_roi_maps_name_each_circle_in_its_colour() -> None:
    from driftless_photometry.text_stamp import text_mask

    rois = tuple(
        ROIConfig(
            fiber_id=f"fiber_0{index + 1}",
            label=f"Fiber {index + 1}",
            animal_id="mouse",
            brain_region="VTA",
            sensor_type="GCaMP",
            center_x_px=x,
            center_y_px=y,
            radius_px=20,
        )
        for index, (x, y) in enumerate(((150, 120), (450, 15)))
    )
    frame = np.full((300, 600), 1000, dtype=np.uint16)
    plain = render_annotated_rois(frame, rois, bit_depth=12)
    labeled = render_annotated_rois(frame, rois, bit_depth=12, labeled=True)
    np.testing.assert_array_equal(frame, np.full((300, 600), 1000, dtype=np.uint16))

    changed = np.any(labeled != plain, axis=2)
    text_height = text_mask("ROI 1 - Fiber 1").shape[0]
    # ROI 1's name sits above its circle; ROI 2 is at the top edge, so below it.
    assert changed[: 120 - 20, 100:200].any()
    assert (labeled[:100, 100:200] == (67, 214, 223)).all(axis=2).any()
    assert (labeled[15 + 20 :, 350:550] == (183, 229, 50)).all(axis=2).any()
    assert not changed[: max(0, 15 - 20 - text_height), :].any()
