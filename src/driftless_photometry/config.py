"""Validated, persisted acquisition configuration."""

from __future__ import annotations

import ipaddress
from datetime import UTC, datetime
from enum import IntEnum, StrEnum
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class Wavelength(IntEnum):
    """Supported excitation wavelengths in nanometers."""

    CONTROL_405 = 405
    GREEN_470 = 470
    RED_565 = 565


class SignalRole(StrEnum):
    ISOSBESTIC_CONTROL = "isosbestic_control"
    GREEN_SIGNAL = "green_signal"
    RED_SIGNAL = "red_signal"


DEFAULT_ROLES: dict[Wavelength, SignalRole] = {
    Wavelength.CONTROL_405: SignalRole.ISOSBESTIC_CONTROL,
    Wavelength.GREEN_470: SignalRole.GREEN_SIGNAL,
    Wavelength.RED_565: SignalRole.RED_SIGNAL,
}

DEFAULT_EMISSION_WAVELENGTHS_NM: dict[Wavelength, float] = {
    Wavelength.CONTROL_405: 525.0,
    Wavelength.GREEN_470: 525.0,
    Wavelength.RED_565: 600.0,
}


class EdgeSelection(StrEnum):
    BOTH = "both"
    RISING = "rising"
    FALLING = "falling"


class ROIConfig(BaseModel):
    """A circular camera-space fiber region and its subject metadata."""

    model_config = ConfigDict(frozen=True)

    fiber_id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.-]+$")
    enabled: bool = True
    label: str = Field(min_length=1, max_length=128)
    animal_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.-]+$")
    brain_region: str = Field(min_length=1, max_length=256)
    sensor_type: str = Field(min_length=1, max_length=128)
    subject_age: str | None = Field(
        default=None,
        min_length=2,
        description="Optional ISO 8601 duration from birth at session start, for example P90D.",
    )
    subject_sex: Literal["M", "F", "U", "O"] = "U"
    center_x_px: float = Field(ge=0)
    center_y_px: float = Field(ge=0)
    radius_px: float = Field(gt=0)


class ChannelConfig(BaseModel):
    """An excitation channel and its commanded intensity."""

    model_config = ConfigDict(frozen=True)

    wavelength_nm: Wavelength
    enabled: bool = True
    voltage_v: float = Field(default=1.0, ge=0.0, le=5.0)
    role: SignalRole | None = None
    emission_wavelength_nm: float | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def fill_default_role(self) -> ChannelConfig:
        if self.role is None:
            object.__setattr__(self, "role", DEFAULT_ROLES[self.wavelength_nm])
        if self.emission_wavelength_nm is None:
            object.__setattr__(
                self,
                "emission_wavelength_nm",
                DEFAULT_EMISSION_WAVELENGTHS_NM[self.wavelength_nm],
            )
        return self


class TTLInputConfig(BaseModel):
    """A behavior TTL input line."""

    model_config = ConfigDict(frozen=True)

    line: int = Field(ge=1, le=4)
    label: str = Field(min_length=1, max_length=64)
    edges: EdgeSelection = EdgeSelection.BOTH


class CameraConfig(BaseModel):
    """Camera settings used by both real and simulated backends."""

    model_config = ConfigDict(frozen=True)

    model_name: str = "Thorlabs CS505MU"
    serial_number: str | None = None
    width_px: int = Field(default=256, gt=0)
    height_px: int = Field(default=256, gt=0)
    bit_depth: int = Field(default=12, ge=8, le=16)
    exposure_us: int = Field(default=10_000, gt=0)
    gain: float = Field(default=0.0, ge=0)
    requested_fps: float = Field(default=30.0, gt=0)
    raw_capture: bool = False


class TraceDisplayConfig(BaseModel):
    """Restorable live-trace presentation preferences."""

    model_config = ConfigDict(frozen=True)

    horizon_s: Literal[15.0, 60.0, 600.0, 3600.0] | None = 15.0
    visible_wavelengths: tuple[Literal[405, 410, 470, 560, 565], ...] = (405, 470, 565)
    mode: Literal["absolute", "dff"] = "absolute"
    dff_baseline_s: float = Field(default=5.0, ge=0.1, le=3600.0)
    smoothing_window_s: float = Field(
        default=0.0,
        ge=0.0,
        le=60.0,
        description="Display-only trailing mean window; zero disables smoothing.",
    )

    @model_validator(mode="after")
    def validate_wavelengths(self) -> TraceDisplayConfig:
        if len(self.visible_wavelengths) != len(set(self.visible_wavelengths)):
            raise ValueError("visible display wavelengths must be unique")
        return self


class NativeSourceConfig(BaseModel):
    """Driftless-controlled camera/controller acquisition source."""

    model_config = ConfigDict(frozen=True)

    kind: Literal["native"] = "native"


class RWDPreambleMode(StrEnum):
    """Treatment of the undocumented optional four-byte connection banner."""

    AUTO = "auto"
    NONE = "none"
    MACHINE_NAME_4 = "machine_name_4"


class RWDChannelMapping(BaseModel):
    """Map one RWD device channel to a stable configured fiber/animal."""

    model_config = ConfigDict(frozen=True)

    device_channel: int = Field(ge=0, le=255)
    fiber_id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.-]+$")
    label: str = Field(min_length=1, max_length=128)


class RWDSourceConfig(BaseModel):
    """Read-only TCP fluorescence/event stream exported by RWD software."""

    model_config = ConfigDict(frozen=True)

    kind: Literal["rwd"] = "rwd"
    host: str = Field(default="127.0.0.1", min_length=1, max_length=253)
    port: int = Field(default=8080, ge=1, le=65535)
    connect_timeout_s: float = Field(default=3.0, gt=0, le=60)
    read_timeout_s: float = Field(default=1.0, gt=0, le=60)
    preamble_mode: RWDPreambleMode = RWDPreambleMode.AUTO
    timestamp_scale_s: float = Field(
        default=0.001,
        gt=0,
        description=(
            "Seconds per RWD uint32 timestamp tick. Vendor documents do not state the unit; "
            "the default millisecond scale requires replay or bench confirmation."
        ),
    )
    value_scale: float = Field(
        default=0.001,
        gt=0,
        description="Scale applied to the vendor uint32 fluorescence value, matching Wave.m.",
    )
    maximum_expected_record_rate_hz: float = Field(
        default=1000.0,
        gt=0,
        le=100_000,
        description=(
            "Conservative upper bound used only for queue/storage planning; it does not "
            "change the RWD acquisition rate."
        ),
    )
    enabled_wavelengths_nm: tuple[Literal[410, 470, 560], ...] = Field(
        default=(410, 470, 560),
        min_length=1,
        max_length=3,
    )
    channel_mappings: tuple[RWDChannelMapping, ...] = Field(min_length=1, max_length=9)
    expected_machine_name: str | None = Field(
        default=None,
        min_length=4,
        max_length=4,
        pattern=r"^[\x20-\x7E]{4}$",
    )

    @field_validator("host")
    @classmethod
    def validate_host(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized or any(character.isspace() for character in normalized):
            raise ValueError("RWD host must be a non-empty hostname or address without spaces")
        try:
            ipaddress.ip_address(normalized)
        except ValueError:
            labels = normalized.split(".")
            if any(
                not label
                or len(label) > 63
                or label.startswith("-")
                or label.endswith("-")
                or not all(
                    character.isascii() and (character.isalnum() or character == "-")
                    for character in label
                )
                for label in labels
            ):
                raise ValueError("RWD host must be a valid IP address or DNS hostname") from None
        return normalized

    @model_validator(mode="after")
    def validate_stream_mapping(self) -> RWDSourceConfig:
        if len(self.enabled_wavelengths_nm) != len(set(self.enabled_wavelengths_nm)):
            raise ValueError("RWD enabled wavelengths must be unique")
        device_channels = [mapping.device_channel for mapping in self.channel_mappings]
        if len(device_channels) != len(set(device_channels)):
            raise ValueError("RWD device channel mappings must be unique")
        fiber_ids = [mapping.fiber_id for mapping in self.channel_mappings]
        if len(fiber_ids) != len(set(fiber_ids)):
            raise ValueError("RWD fiber mappings must be unique")
        return self


AcquisitionSourceConfig = Annotated[
    NativeSourceConfig | RWDSourceConfig,
    Field(discriminator="kind"),
]


class SessionConfig(BaseModel):
    """Complete immutable configuration for one recording session."""

    model_config = ConfigDict(frozen=True)

    session_id: str = Field(min_length=1, max_length=128)
    session_description: str = Field(min_length=1)
    experimenter: str = Field(min_length=1)
    output_directory: Path
    recording_duration_s: float = Field(default=5.0, gt=0, le=24 * 60 * 60)
    session_start_time: datetime = Field(default_factory=lambda: datetime.now(UTC))
    source: AcquisitionSourceConfig = Field(default_factory=NativeSourceConfig)
    camera: CameraConfig
    rois: tuple[ROIConfig, ...] = Field(min_length=1, max_length=9)
    channels: tuple[ChannelConfig, ...] = Field(min_length=1, max_length=3)
    ttl_inputs: tuple[TTLInputConfig, ...] = Field(default=(), max_length=4)
    lab: str | None = None
    institution: str | None = None
    display: TraceDisplayConfig = Field(default_factory=TraceDisplayConfig)

    @model_validator(mode="after")
    def validate_contract(self) -> SessionConfig:
        fiber_ids = [roi.fiber_id for roi in self.rois]
        if len(fiber_ids) != len(set(fiber_ids)):
            raise ValueError("fiber_id values must be unique")

        animal_ids = [roi.animal_id for roi in self.rois]
        if len(animal_ids) != len(set(animal_ids)):
            raise ValueError("animal_id values must be unique across ROIs")
        if not any(roi.enabled for roi in self.rois):
            raise ValueError("at least one ROI must be enabled")

        wavelengths = [channel.wavelength_nm for channel in self.channels]
        if len(wavelengths) != len(set(wavelengths)):
            raise ValueError("channel wavelengths must be unique")
        if isinstance(self.source, NativeSourceConfig) and not any(
            channel.enabled for channel in self.channels
        ):
            raise ValueError("at least one excitation channel must be enabled")

        ttl_lines = [ttl.line for ttl in self.ttl_inputs]
        if len(ttl_lines) != len(set(ttl_lines)):
            raise ValueError("TTL input lines must be unique")

        if isinstance(self.source, NativeSourceConfig):
            for roi in self.rois:
                if roi.center_x_px + roi.radius_px > self.camera.width_px:
                    raise ValueError(f"ROI {roi.fiber_id} exceeds camera width")
                if roi.center_y_px + roi.radius_px > self.camera.height_px:
                    raise ValueError(f"ROI {roi.fiber_id} exceeds camera height")
                if roi.center_x_px - roi.radius_px < 0:
                    raise ValueError(f"ROI {roi.fiber_id} exceeds camera left edge")
                if roi.center_y_px - roi.radius_px < 0:
                    raise ValueError(f"ROI {roi.fiber_id} exceeds camera top edge")
        else:
            mapped_fibers = {mapping.fiber_id for mapping in self.source.channel_mappings}
            enabled_fibers = {roi.fiber_id for roi in self.enabled_rois}
            if mapped_fibers != enabled_fibers:
                raise ValueError(
                    "RWD channel mappings must map every enabled fiber exactly once and no "
                    "disabled fiber"
                )
        return self

    @property
    def enabled_channels(self) -> tuple[ChannelConfig, ...]:
        return tuple(channel for channel in self.channels if channel.enabled)

    @property
    def enabled_rois(self) -> tuple[ROIConfig, ...]:
        return tuple(roi for roi in self.rois if roi.enabled)


def demo_config(
    output_directory: Path,
    *,
    fiber_count: int = 3,
    raw_capture: bool = False,
    recording_duration_s: float = 5.0,
    width_px: int = 256,
    height_px: int = 256,
) -> SessionConfig:
    """Build a deterministic, valid configuration for demos and tests."""

    if not 1 <= fiber_count <= 9:
        raise ValueError("fiber_count must be between 1 and 9")
    grid_columns = min(3, fiber_count)
    grid_rows = (fiber_count + grid_columns - 1) // grid_columns
    spacing_x = width_px / (grid_columns + 1)
    spacing_y = height_px / (grid_rows + 1)
    radius = max(4.0, min(spacing_x, spacing_y) * 0.22)
    rois = tuple(
        ROIConfig(
            fiber_id=f"fiber_{index + 1:02d}",
            label=f"Fiber {index + 1}",
            animal_id=f"animal-{index + 1:02d}",
            brain_region="not specified",
            sensor_type="not specified",
            subject_age="P90D",
            subject_sex="U",
            center_x_px=(index % grid_columns + 1) * spacing_x,
            center_y_px=(index // grid_columns + 1) * spacing_y,
            radius_px=radius,
        )
        for index in range(fiber_count)
    )
    channels = tuple(
        ChannelConfig(wavelength_nm=wavelength, voltage_v=1.0) for wavelength in Wavelength
    )
    ttl_inputs = tuple(TTLInputConfig(line=line, label=f"behavior_{line}") for line in range(1, 5))
    started = datetime.now(UTC)
    return SessionConfig(
        session_id=started.strftime("demo-%Y%m%dT%H%M%S-%fZ"),
        session_description="Synthetic multichannel fiber-photometry demonstration",
        experimenter="Simulator",
        output_directory=output_directory,
        recording_duration_s=recording_duration_s,
        session_start_time=started,
        camera=CameraConfig(
            width_px=width_px,
            height_px=height_px,
            raw_capture=raw_capture,
        ),
        rois=rois,
        channels=channels,
        ttl_inputs=ttl_inputs,
        lab="Driftless Bundle Photometry",
    )
