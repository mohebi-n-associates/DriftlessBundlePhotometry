from pathlib import Path

import pytest
from pydantic import ValidationError

from driftless_photometry.config import (
    CUSTOM_PROFILE,
    RWD_PROFILE,
    CameraConfig,
    ChannelConfig,
    CustomSourceConfig,
    ROIConfig,
    RWDSourceConfig,
    RWDTTLOutputConfig,
    SessionConfig,
    TTLInputConfig,
    Wavelength,
    demo_config,
)


def test_demo_config_has_requested_fibers_and_channels(tmp_path: Path) -> None:
    config = demo_config(tmp_path, fiber_count=9, raw_capture=True)
    assert len(config.rois) == 9
    assert config.enabled_rois == config.rois
    assert [channel.wavelength_nm for channel in config.enabled_channels] == list(
        CUSTOM_PROFILE.wavelengths
    )
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
    rwd = demo_config(tmp_path, fiber_count=2, system="rwd")

    assert SessionConfig.model_validate(base.model_dump(mode="json")).source == CustomSourceConfig()
    restored = SessionConfig.model_validate(rwd.model_dump(mode="json"))
    assert restored.source == RWDSourceConfig()
    assert rwd.model_dump(mode="json")["source"] == {
        "kind": "rwd",
        "controller_port": None,
        "ttl_outputs": [],
    }

    with pytest.raises(ValidationError):
        SessionConfig.model_validate({**base.model_dump(mode="json"), "source": {"kind": "native"}})


def test_rwd_source_applies_camera_roi_and_channel_rules(tmp_path: Path) -> None:
    base = demo_config(tmp_path, fiber_count=1, system="rwd")
    outside_camera = base.rois[0].model_copy(update={"center_x_px": 10_000.0})
    with pytest.raises(ValidationError, match="exceeds camera width"):
        SessionConfig.model_validate({**base.model_dump(), "rois": (outside_camera,)})

    payload = base.model_dump()
    for channel in payload["channels"]:
        channel["enabled"] = False
    with pytest.raises(ValidationError, match="at least one excitation channel"):
        SessionConfig.model_validate(payload)


def test_system_profiles_describe_custom_and_rwd_hardware() -> None:
    assert CUSTOM_PROFILE.wavelengths == (
        Wavelength.CONTROL_405,
        Wavelength.GREEN_470,
        Wavelength.RED_565,
    )
    assert CUSTOM_PROFILE.intensity_unit == "V"
    assert CUSTOM_PROFILE.camera_count == 1
    assert CUSTOM_PROFILE.camera_for(Wavelength.RED_565) == 1
    assert CUSTOM_PROFILE.maximum_exposure_us(20.0) is None
    assert CUSTOM_PROFILE.hardware_failsafe

    assert RWD_PROFILE.wavelengths == (
        Wavelength.CONTROL_410,
        Wavelength.GREEN_470,
        Wavelength.RED_560,
    )
    assert RWD_PROFILE.intensity_unit == "%"
    assert RWD_PROFILE.camera_count == 2
    assert RWD_PROFILE.camera_for(Wavelength.CONTROL_410) == 1
    assert RWD_PROFILE.camera_for(Wavelength.GREEN_470) == 1
    assert RWD_PROFILE.camera_for(Wavelength.RED_560) == 2
    assert RWD_PROFILE.sensor_size_px == (728, 544)
    assert RWD_PROFILE.maximum_exposure_us(20.0) == 47_370
    assert RWD_PROFILE.maximum_exposure_us(30.0) == 30_370
    assert not RWD_PROFILE.hardware_failsafe
    allowed = RWD_PROFILE.allowed_wavelength_sets
    assert allowed is not None
    assert frozenset({Wavelength.CONTROL_410, Wavelength.RED_560}) not in allowed
    assert frozenset({Wavelength.CONTROL_410, Wavelength.GREEN_470}) in allowed


def test_rwd_demo_config_is_valid_and_uses_percent_power(tmp_path: Path) -> None:
    config = demo_config(tmp_path, fiber_count=2, system="rwd")

    assert config.source == RWDSourceConfig()
    assert [channel.wavelength_nm for channel in config.channels] == list(RWD_PROFILE.wavelengths)
    assert all(channel.voltage_v is None for channel in config.channels)
    assert all(channel.power_percent == 5.0 for channel in config.channels)
    assert (config.camera.width_px, config.camera.height_px) == (728, 544)
    assert all(roi.has_camera2_geometry for roi in config.rois)
    assert config.display.visible_wavelengths == (410, 470, 560)
    assert config.session_id.startswith("rwd-")


def _rwd_payload(tmp_path: Path) -> dict:
    return demo_config(tmp_path, fiber_count=1, system="rwd").model_dump()


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (lambda p: p["channels"][0].update(wavelength_nm=405), "has no 405 nm excitation"),
        (
            lambda p: p["channels"][0].update(voltage_v=1.0),
            "set power_percent and leave voltage_v empty",
        ),
        (lambda p: p["channels"][1].update(enabled=False), "cannot interleave 410\\+560 nm"),
        (lambda p: p["camera"].update(width_px=640), "full 728x544 sensor"),
        (lambda p: p["camera"].update(requested_fps=20.5), "whole number of hertz"),
        (lambda p: p["camera"].update(requested_fps=5.0), "between 10 and 250 Hz"),
        (lambda p: p["camera"].update(exposure_us=47_371.0), "must not exceed 47370 us"),
        (lambda p: p["camera"].update(exposure_us=400.0), "at least 500 us"),
        (
            lambda p: p["rois"][0].update(
                camera2_center_x_px=None, camera2_center_y_px=None, camera2_radius_px=None
            ),
            "needs camera-2 geometry for 560 nm",
        ),
        (lambda p: p["rois"][0].update(camera2_center_x_px=10_000.0), "on camera 2 exceeds"),
        (lambda p: p["display"].update(visible_wavelengths=(405,)), "cannot display 405 nm"),
    ],
)
def test_rwd_profile_rejects_settings_the_hardware_cannot_run(
    tmp_path: Path, change, message: str
) -> None:
    payload = _rwd_payload(tmp_path)
    change(payload)

    with pytest.raises(ValidationError, match=message):
        SessionConfig.model_validate(payload)


def test_rwd_profile_allows_camera2_geometry_to_be_omitted_without_560(tmp_path: Path) -> None:
    payload = _rwd_payload(tmp_path)
    payload["channels"][2]["enabled"] = False
    payload["rois"][0].update(
        camera2_center_x_px=None, camera2_center_y_px=None, camera2_radius_px=None
    )

    config = SessionConfig.model_validate(payload)

    assert not config.rois[0].has_camera2_geometry


def test_custom_profile_rejects_rwd_only_settings(tmp_path: Path) -> None:
    payload = demo_config(tmp_path, fiber_count=1).model_dump()
    payload["channels"][0]["wavelength_nm"] = 410
    with pytest.raises(ValidationError, match="has no 410 nm excitation"):
        SessionConfig.model_validate(payload)

    payload = demo_config(tmp_path, fiber_count=1).model_dump()
    payload["channels"][0].update(voltage_v=None, power_percent=5.0)
    with pytest.raises(ValidationError, match="set voltage_v and leave power_percent empty"):
        SessionConfig.model_validate(payload)

    payload = demo_config(tmp_path, fiber_count=1).model_dump()
    payload["rois"][0].update(
        camera2_center_x_px=50.0, camera2_center_y_px=50.0, camera2_radius_px=10.0
    )
    with pytest.raises(ValidationError, match="has one camera"):
        SessionConfig.model_validate(payload)


def test_roi_camera2_geometry_is_all_or_none() -> None:
    with pytest.raises(ValidationError, match="camera-2"):
        ROIConfig(
            fiber_id="fiber_01",
            label="Fiber",
            animal_id="animal-01",
            brain_region="NAc",
            sensor_type="dLight1.3b",
            center_x_px=50.0,
            center_y_px=50.0,
            radius_px=10.0,
            camera2_center_x_px=60.0,
        )


def test_channel_power_percent_is_bounded() -> None:
    with pytest.raises(ValidationError):
        ChannelConfig(wavelength_nm=Wavelength.GREEN_470, voltage_v=None, power_percent=100.5)
    with pytest.raises(ValidationError):
        ChannelConfig(wavelength_nm=Wavelength.GREEN_470, voltage_v=None, power_percent=-1.0)


def test_rwd_ttl_outputs_must_fit_their_period_and_use_each_port_once() -> None:
    output = RWDTTLOutputConfig(port=2, frequency_hz=10.0, pulse_width_ms=20, label="laser")
    assert RWDSourceConfig(ttl_outputs=(output,)).ttl_outputs == (output,)

    with pytest.raises(ValidationError, match="does not fit its 100 ms period"):
        RWDTTLOutputConfig(port=1, frequency_hz=10.0, pulse_width_ms=100)
    with pytest.raises(ValidationError):
        RWDTTLOutputConfig(port=5, frequency_hz=1.0, pulse_width_ms=10)
    with pytest.raises(ValidationError):
        RWDTTLOutputConfig(port=1, frequency_hz=700.0, pulse_width_ms=1)
    with pytest.raises(ValidationError, match="configured once"):
        RWDSourceConfig(ttl_outputs=(output, output))
