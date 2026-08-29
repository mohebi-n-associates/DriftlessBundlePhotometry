from pathlib import Path

import pytest
from pydantic import ValidationError

from driftless_photometry.config import (
    CameraConfig,
    ChannelConfig,
    ROIConfig,
    SessionConfig,
    TTLInputConfig,
    Wavelength,
    demo_config,
)


def test_demo_config_has_requested_fibers_and_channels(tmp_path: Path) -> None:
    config = demo_config(tmp_path, fiber_count=9, raw_capture=True)
    assert len(config.rois) == 9
    assert [channel.wavelength_nm for channel in config.enabled_channels] == list(Wavelength)
    assert config.camera.raw_capture is True


def test_rejects_duplicate_fibers(tmp_path: Path) -> None:
    roi = ROIConfig(fiber_id="fiber", label="Fiber", center_x_px=20, center_y_px=20, radius_px=5)
    with pytest.raises(ValidationError, match="fiber_id values must be unique"):
        SessionConfig(
            subject_id="subject",
            subject_age="P90D",
            subject_sex="U",
            session_id="session",
            session_description="test",
            experimenter="test",
            output_directory=tmp_path,
            camera=CameraConfig(width_px=50, height_px=50),
            rois=(roi, roi),
            channels=(ChannelConfig(wavelength_nm=Wavelength.GREEN_470),),
        )


def test_rejects_out_of_bounds_roi(tmp_path: Path) -> None:
    roi = ROIConfig(fiber_id="fiber", label="Fiber", center_x_px=2, center_y_px=20, radius_px=5)
    with pytest.raises(ValidationError, match="left edge"):
        SessionConfig(
            subject_id="subject",
            subject_age="P90D",
            subject_sex="U",
            session_id="session",
            session_description="test",
            experimenter="test",
            output_directory=tmp_path,
            camera=CameraConfig(width_px=50, height_px=50),
            rois=(roi,),
            channels=(ChannelConfig(wavelength_nm=Wavelength.GREEN_470),),
        )


def test_rejects_duplicate_ttl_lines(tmp_path: Path) -> None:
    config = demo_config(tmp_path, fiber_count=1)
    payload = config.model_dump()
    payload["ttl_inputs"] = [
        TTLInputConfig(line=1, label="a"),
        TTLInputConfig(line=1, label="b"),
    ]
    with pytest.raises(ValidationError, match="TTL input lines must be unique"):
        SessionConfig.model_validate(payload)
