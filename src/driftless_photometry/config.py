"""Validated, persisted acquisition configuration."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import IntEnum, StrEnum
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


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
    """A circular camera-space fiber region."""

    model_config = ConfigDict(frozen=True)

    fiber_id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.-]+$")
    label: str = Field(min_length=1, max_length=128)
    center_x_px: float = Field(ge=0)
    center_y_px: float = Field(ge=0)
    radius_px: float = Field(gt=0)
    location: str = Field(default="not specified", min_length=1, max_length=256)
    indicator_label: str = Field(default="not specified", min_length=1, max_length=128)


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


class SessionConfig(BaseModel):
    """Complete immutable configuration for one recording session."""

    model_config = ConfigDict(frozen=True)

    subject_id: str = Field(min_length=1, max_length=128)
    subject_age: str = Field(
        min_length=2,
        description="ISO 8601 duration from birth at session start, for example P90D.",
    )
    subject_sex: Literal["M", "F", "U", "O"]
    session_id: str = Field(min_length=1, max_length=128)
    session_description: str = Field(min_length=1)
    experimenter: str = Field(min_length=1)
    output_directory: Path
    session_start_time: datetime = Field(default_factory=lambda: datetime.now(UTC))
    camera: CameraConfig
    rois: tuple[ROIConfig, ...] = Field(min_length=1, max_length=9)
    channels: tuple[ChannelConfig, ...] = Field(min_length=1, max_length=3)
    ttl_inputs: tuple[TTLInputConfig, ...] = Field(default=(), max_length=4)
    lab: str | None = None
    institution: str | None = None

    @model_validator(mode="after")
    def validate_contract(self) -> SessionConfig:
        fiber_ids = [roi.fiber_id for roi in self.rois]
        if len(fiber_ids) != len(set(fiber_ids)):
            raise ValueError("fiber_id values must be unique")

        wavelengths = [channel.wavelength_nm for channel in self.channels]
        if len(wavelengths) != len(set(wavelengths)):
            raise ValueError("channel wavelengths must be unique")
        if not any(channel.enabled for channel in self.channels):
            raise ValueError("at least one excitation channel must be enabled")

        ttl_lines = [ttl.line for ttl in self.ttl_inputs]
        if len(ttl_lines) != len(set(ttl_lines)):
            raise ValueError("TTL input lines must be unique")

        for roi in self.rois:
            if roi.center_x_px + roi.radius_px > self.camera.width_px:
                raise ValueError(f"ROI {roi.fiber_id} exceeds camera width")
            if roi.center_y_px + roi.radius_px > self.camera.height_px:
                raise ValueError(f"ROI {roi.fiber_id} exceeds camera height")
            if roi.center_x_px - roi.radius_px < 0:
                raise ValueError(f"ROI {roi.fiber_id} exceeds camera left edge")
            if roi.center_y_px - roi.radius_px < 0:
                raise ValueError(f"ROI {roi.fiber_id} exceeds camera top edge")
        return self

    @property
    def enabled_channels(self) -> tuple[ChannelConfig, ...]:
        return tuple(channel for channel in self.channels if channel.enabled)


def demo_config(
    output_directory: Path,
    *,
    fiber_count: int = 3,
    raw_capture: bool = False,
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
        subject_id="demo-subject",
        subject_age="P90D",
        subject_sex="U",
        session_id=started.strftime("demo-%Y%m%dT%H%M%S-%fZ"),
        session_description="Synthetic multichannel fiber-photometry demonstration",
        experimenter="Simulator",
        output_directory=output_directory,
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
