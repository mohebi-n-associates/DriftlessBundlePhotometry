from pathlib import Path

import pytest
from pydantic import ValidationError

from driftless_photometry.config import (
    CameraConfig,
    ChannelConfig,
    CustomSourceConfig,
    ROIConfig,
    RWDSourceConfig,
    SessionConfig,
    TTLInputConfig,
    Wavelength,
    demo_config,
)


def test_demo_config_has_requested_fibers_and_channels(tmp_path: Path) -> None:
    config = demo_config(tmp_path, fiber_count=9, raw_capture=True)
    assert len(config.rois) == 9
    assert config.enabled_rois == config.rois
    assert [channel.wavelength_nm for channel in config.enabled_channels] == list(Wavelength)
    assert config.camera.raw_capture is True
    assert config.source == CustomSourceConfig()


def test_requires_at_least_one_enabled_roi(tmp_path: Path) -> None:
    config = demo_config(tmp_path, fiber_count=2)
    payload = config.model_dump()
    for roi in payload["rois"]:
        roi["enabled"] = False

    with pytest.raises(ValidationError, match="at least one ROI must be enabled"):
        SessionConfig.model_validate(payload)


def test_rejects_duplicate_fibers(tmp_path: Path) -> None:
    roi = ROIConfig(
        fiber_id="fiber",
        label="Fiber",
        animal_id="animal-01",
        brain_region="NAc",
        sensor_type="dLight1.3b",
        center_x_px=20,
        center_y_px=20,
        radius_px=5,
    )
    with pytest.raises(ValidationError, match="fiber_id values must be unique"):
        SessionConfig(
            session_id="session",
            session_description="test",
            experimenter="test",
            output_directory=tmp_path,
            camera=CameraConfig(width_px=50, height_px=50),
            rois=(roi, roi),
            channels=(ChannelConfig(wavelength_nm=Wavelength.GREEN_470),),
        )


def test_rejects_out_of_bounds_roi(tmp_path: Path) -> None:
    roi = ROIConfig(
        fiber_id="fiber",
        label="Fiber",
        animal_id="animal-01",
        brain_region="NAc",
        sensor_type="dLight1.3b",
        center_x_px=2,
        center_y_px=20,
        radius_px=5,
    )
    with pytest.raises(ValidationError, match="left edge"):
        SessionConfig(
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


def test_rejects_duplicate_animals_across_rois(tmp_path: Path) -> None:
    config = demo_config(tmp_path, fiber_count=2)
    payload = config.model_dump()
    payload["rois"][1]["animal_id"] = payload["rois"][0]["animal_id"]

    with pytest.raises(ValidationError, match="animal_id values must be unique"):
        SessionConfig.model_validate(payload)


def test_sources_round_trip_as_discriminated_custom_or_rwd_kinds(tmp_path: Path) -> None:
    base = demo_config(tmp_path, fiber_count=2)
    rwd = base.model_copy(update={"source": RWDSourceConfig()})

    assert SessionConfig.model_validate(base.model_dump(mode="json")).source == CustomSourceConfig()
    restored = SessionConfig.model_validate(rwd.model_dump(mode="json"))
    assert restored.source == RWDSourceConfig()
    assert rwd.model_dump(mode="json")["source"] == {"kind": "rwd"}

    with pytest.raises(ValidationError):
        SessionConfig.model_validate({**base.model_dump(mode="json"), "source": {"kind": "native"}})


def test_rwd_source_applies_camera_roi_and_channel_rules(tmp_path: Path) -> None:
    base = demo_config(tmp_path, fiber_count=1)
    outside_camera = base.rois[0].model_copy(update={"center_x_px": 10_000.0})
    with pytest.raises(ValidationError, match="exceeds camera width"):
        SessionConfig.model_validate(
            {**base.model_dump(), "source": RWDSourceConfig(), "rois": (outside_camera,)}
        )

    payload = base.model_dump()
    for channel in payload["channels"]:
        channel["enabled"] = False
    payload["source"] = RWDSourceConfig()
    with pytest.raises(ValidationError, match="at least one excitation channel"):
        SessionConfig.model_validate(payload)
