from pathlib import Path

import pytest
from pydantic import ValidationError

from driftless_photometry.config import (
    CameraConfig,
    ChannelConfig,
    NativeSourceConfig,
    ROIConfig,
    RWDChannelMapping,
    RWDPreambleMode,
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
    assert config.source == NativeSourceConfig()


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


def test_rwd_source_round_trips_discriminated_settings_and_maps_enabled_fibers(
    tmp_path: Path,
) -> None:
    base = demo_config(tmp_path, fiber_count=2)
    source = RWDSourceConfig(
        host=" 192.168.65.9 ",
        port=8080,
        preamble_mode=RWDPreambleMode.AUTO,
        channel_mappings=(
            RWDChannelMapping(device_channel=0, fiber_id="fiber_01", label="Mouse A"),
            RWDChannelMapping(device_channel=7, fiber_id="fiber_02", label="Mouse B"),
        ),
    )
    config = base.model_copy(update={"source": source})

    restored = SessionConfig.model_validate(config.model_dump(mode="json"))

    assert isinstance(restored.source, RWDSourceConfig)
    assert restored.source.host == "192.168.65.9"
    assert restored.source.enabled_wavelengths_nm == (410, 470, 560)
    assert restored.source.timestamp_scale_s == 0.001
    assert restored.source.value_scale == 0.001


def test_rwd_source_rejects_duplicate_or_incomplete_channel_mapping(tmp_path: Path) -> None:
    base = demo_config(tmp_path, fiber_count=2)
    with pytest.raises(ValidationError, match="device channel mappings must be unique"):
        RWDSourceConfig(
            channel_mappings=(
                RWDChannelMapping(device_channel=1, fiber_id="fiber_01", label="A"),
                RWDChannelMapping(device_channel=1, fiber_id="fiber_02", label="B"),
            )
        )

    incomplete = RWDSourceConfig(
        channel_mappings=(RWDChannelMapping(device_channel=1, fiber_id="fiber_01", label="A"),)
    )
    with pytest.raises(ValidationError, match="map every enabled fiber"):
        SessionConfig.model_validate({**base.model_dump(), "source": incomplete})

    with pytest.raises(ValidationError, match="valid IP address or DNS hostname"):
        RWDSourceConfig(
            host="!!!",
            channel_mappings=(RWDChannelMapping(device_channel=1, fiber_id="fiber_01", label="A"),),
        )


def test_rwd_source_does_not_treat_camera_geometry_as_stream_provenance(tmp_path: Path) -> None:
    base = demo_config(tmp_path, fiber_count=1)
    outside_camera = base.rois[0].model_copy(update={"center_x_px": 10_000.0})
    source = RWDSourceConfig(
        channel_mappings=(
            RWDChannelMapping(device_channel=3, fiber_id="fiber_01", label="RWD channel 3"),
        )
    )

    config = SessionConfig.model_validate(
        {
            **base.model_dump(),
            "source": source,
            "rois": (outside_camera,),
        }
    )

    assert config.rois[0].center_x_px == 10_000.0


def test_rwd_source_does_not_require_inactive_native_excitation_channels(tmp_path: Path) -> None:
    base = demo_config(tmp_path, fiber_count=1)
    payload = base.model_dump()
    for channel in payload["channels"]:
        channel["enabled"] = False
    payload["source"] = RWDSourceConfig(
        channel_mappings=(
            RWDChannelMapping(device_channel=2, fiber_id="fiber_01", label="RWD channel 2"),
        )
    )

    config = SessionConfig.model_validate(payload)

    assert config.enabled_channels == ()
