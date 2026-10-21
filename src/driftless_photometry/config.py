"""Validated, persisted acquisition configuration."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import IntEnum, StrEnum
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Wavelength(IntEnum):
    """Exact excitation wavelengths in nanometers; each system supports its own set."""

    CONTROL_405 = 405
    CONTROL_410 = 410
    GREEN_470 = 470
    RED_560 = 560
    RED_565 = 565


class SignalRole(StrEnum):
    ISOSBESTIC_CONTROL = "isosbestic_control"
    GREEN_SIGNAL = "green_signal"
    RED_SIGNAL = "red_signal"


DEFAULT_ROLES: dict[Wavelength, SignalRole] = {
    Wavelength.CONTROL_405: SignalRole.ISOSBESTIC_CONTROL,
    Wavelength.CONTROL_410: SignalRole.ISOSBESTIC_CONTROL,
    Wavelength.GREEN_470: SignalRole.GREEN_SIGNAL,
    Wavelength.RED_560: SignalRole.RED_SIGNAL,
    Wavelength.RED_565: SignalRole.RED_SIGNAL,
}

DEFAULT_EMISSION_WAVELENGTHS_NM: dict[Wavelength, float] = {
    Wavelength.CONTROL_405: 525.0,
    Wavelength.CONTROL_410: 525.0,
    Wavelength.GREEN_470: 525.0,
    Wavelength.RED_560: 600.0,
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
    camera2_center_x_px: float | None = Field(
        default=None,
        ge=0,
        description="Center on the second camera (RWD 560 nm), placed separately.",
    )
    camera2_center_y_px: float | None = Field(default=None, ge=0)
    camera2_radius_px: float | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def validate_camera2_geometry(self) -> ROIConfig:
        values = (self.camera2_center_x_px, self.camera2_center_y_px, self.camera2_radius_px)
        if any(v is None for v in values) and any(v is not None for v in values):
            raise ValueError(
                f"ROI {self.fiber_id} camera-2 geometry needs center x, center y, and radius"
            )
        return self

    @property
    def has_camera2_geometry(self) -> bool:
        return self.camera2_radius_px is not None


class ChannelConfig(BaseModel):
    """An excitation channel and its commanded intensity."""

    model_config = ConfigDict(frozen=True)

    wavelength_nm: Wavelength
    enabled: bool = True
    voltage_v: float | None = Field(
        default=1.0,
        ge=0.0,
        le=5.0,
        description="Commanded LED voltage; used by the Custom system only.",
    )
    power_percent: float | None = Field(
        default=None,
        ge=0.0,
        le=100.0,
        description="RWD LED power ratio in percent; not a measured optical power.",
    )
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


EVENT_MARKER_KEYS = tuple(str(key) for key in range(1, 10))


class EventMarkerConfig(BaseModel):
    """An operator event marker, set with its number key or button while recording."""

    model_config = ConfigDict(frozen=True)

    key: Literal["1", "2", "3", "4", "5", "6", "7", "8", "9"]
    name: str = Field(min_length=1, max_length=64)


class TTLEventRuleConfig(BaseModel):
    """Name the pulses on one TTL input whose width falls in [min, max) milliseconds."""

    model_config = ConfigDict(frozen=True)

    line: int = Field(ge=1, le=4)
    min_width_ms: float = Field(default=0.0, ge=0.0)
    max_width_ms: float = Field(default=100.0, gt=0.0, le=3_600_000.0)
    name: str = Field(min_length=1, max_length=64)

    @model_validator(mode="after")
    def validate_width_range(self) -> TTLEventRuleConfig:
        if self.max_width_ms <= self.min_width_ms:
            raise ValueError(f"TTL event {self.name!r} needs a maximum width above its minimum")
        return self


MANUAL_STOP_LIMIT_S = 24 * 60 * 60.0
"""A recording that stops only on Stop still ends after 24 hours."""


class RecordingControlConfig(BaseModel):
    """When a recording starts and stops, with the choices OFRS offers.

    ``SessionConfig.recording_duration_s`` is the recording's length when ``stop`` is
    "duration" and its maximum length when ``stop`` is "ttl"; a "manual" recording runs
    until Stop for at most ``MANUAL_STOP_LIMIT_S``. Previews and live view always start
    at once and ignore these settings.
    """

    model_config = ConfigDict(frozen=True)

    start: Literal["immediate", "delayed", "ttl"] = "immediate"
    start_delay_s: float = Field(default=10.0, gt=0.0, le=86_400.0)
    start_ttl_line: int = Field(default=1, ge=1, le=4)
    start_ttl_edge: Literal["rising", "falling"] = "rising"
    stop: Literal["duration", "manual", "ttl"] = "duration"
    stop_ttl_line: int = Field(default=1, ge=1, le=4)
    stop_ttl_edge: Literal["rising", "falling"] = "rising"


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


class CustomSourceConfig(BaseModel):
    """DBF's own camera and timing-controller rig, or its simulator."""

    model_config = ConfigDict(frozen=True)

    kind: Literal["custom"] = "custom"


class RWDTTLOutputConfig(BaseModel):
    """A pulse train the RWD board drives on one BNC TTL output during a session."""

    model_config = ConfigDict(frozen=True)

    port: int = Field(ge=1, le=4)
    frequency_hz: float = Field(gt=0.0, le=655.35)
    """Board resolution is 0.01 Hz."""
    pulse_width_ms: int = Field(ge=1, le=65_535)
    label: str = Field(default="", max_length=64)

    @model_validator(mode="after")
    def validate_pulse_fits_period(self) -> RWDTTLOutputConfig:
        if self.pulse_width_ms >= 1000.0 / self.frequency_hz:
            raise ValueError(
                f"TTL output {self.port} pulse of {self.pulse_width_ms} ms does not fit its "
                f"{1000.0 / self.frequency_hz:g} ms period"
            )
        return self


class RWDSourceConfig(BaseModel):
    """RWD photometry hardware driven directly by DBF; see ``docs/rwd-direct-control.md``."""

    model_config = ConfigDict(frozen=True)

    kind: Literal["rwd"] = "rwd"
    controller_port: str | None = Field(
        default=None,
        min_length=1,
        max_length=64,
        description="Controller COM port; None uses the only FT232R port present.",
    )
    ttl_outputs: tuple[RWDTTLOutputConfig, ...] = Field(
        default=(),
        max_length=4,
        description="Pulse trains to drive while recording; outputs not listed stay off.",
    )

    @model_validator(mode="after")
    def validate_output_ports(self) -> RWDSourceConfig:
        ports = [output.port for output in self.ttl_outputs]
        if len(ports) != len(set(ports)):
            raise ValueError("each RWD TTL output may be configured once")
        return self


AcquisitionSourceConfig = Annotated[
    CustomSourceConfig | RWDSourceConfig,
    Field(discriminator="kind"),
]


@dataclass(frozen=True)
class SystemProfile:
    """Hardware capabilities that drive validation, the GUI, and backend selection."""

    kind: Literal["custom", "rwd"]
    display_name: str
    wavelengths: tuple[Wavelength, ...]
    intensity_unit: Literal["V", "%"]
    intensity_maximum: float
    camera_count: int
    wavelength_cameras: Mapping[Wavelength, int]
    allowed_wavelength_sets: frozenset[frozenset[Wavelength]] | None
    frame_rate_range_hz: tuple[float, float] | None
    integer_frame_rate: bool
    minimum_exposure_us: float | None
    exposure_margin_us: float | None
    sensor_size_px: tuple[int, int] | None
    ttl_output_count: int
    hardware_failsafe: bool
    default_camera: CameraConfig

    def camera_for(self, wavelength: Wavelength) -> int:
        return self.wavelength_cameras[wavelength]

    def maximum_exposure_us(self, frame_rate_hz: float) -> float | None:
        """RWD rule from OFRS: whole-millisecond frame period minus the board dead time."""

        if self.exposure_margin_us is None:
            return None
        return (1000 // int(frame_rate_hz)) * 1000.0 - self.exposure_margin_us


_CUSTOM_WAVELENGTHS = (Wavelength.CONTROL_405, Wavelength.GREEN_470, Wavelength.RED_565)
_RWD_WAVELENGTHS = (Wavelength.CONTROL_410, Wavelength.GREEN_470, Wavelength.RED_560)

CUSTOM_PROFILE = SystemProfile(
    kind="custom",
    display_name="Custom",
    wavelengths=_CUSTOM_WAVELENGTHS,
    intensity_unit="V",
    intensity_maximum=5.0,
    camera_count=1,
    wavelength_cameras=dict.fromkeys(_CUSTOM_WAVELENGTHS, 1),
    allowed_wavelength_sets=None,
    frame_rate_range_hz=None,
    integer_frame_rate=False,
    minimum_exposure_us=None,
    exposure_margin_us=None,
    sensor_size_px=None,
    ttl_output_count=0,
    hardware_failsafe=True,
    default_camera=CameraConfig(),
)

RWD_PROFILE = SystemProfile(
    kind="rwd",
    display_name="RWD R821",
    wavelengths=_RWD_WAVELENGTHS,
    intensity_unit="%",
    intensity_maximum=100.0,
    camera_count=2,
    wavelength_cameras={Wavelength.CONTROL_410: 1, Wavelength.GREEN_470: 1, Wavelength.RED_560: 2},
    allowed_wavelength_sets=frozenset(
        frozenset(selection)
        for selection in (
            {Wavelength.CONTROL_410},
            {Wavelength.GREEN_470},
            {Wavelength.RED_560},
            {Wavelength.CONTROL_410, Wavelength.GREEN_470},
            {Wavelength.GREEN_470, Wavelength.RED_560},
            set(_RWD_WAVELENGTHS),
        )
    ),
    frame_rate_range_hz=(10.0, 250.0),
    integer_frame_rate=True,
    minimum_exposure_us=500.0,
    exposure_margin_us=2630.0,
    sensor_size_px=(728, 544),
    ttl_output_count=4,
    hardware_failsafe=False,
    default_camera=CameraConfig(
        model_name="Allied Vision Alvium 1800 U-040m",
        width_px=728,
        height_px=544,
        bit_depth=12,
        exposure_us=10_000,
        requested_fps=20.0,
    ),
)


def profile_for(source: CustomSourceConfig | RWDSourceConfig) -> SystemProfile:
    return RWD_PROFILE if isinstance(source, RWDSourceConfig) else CUSTOM_PROFILE


class SessionConfig(BaseModel):
    """Complete immutable configuration for one recording session."""

    model_config = ConfigDict(frozen=True)

    session_id: str = Field(min_length=1, max_length=128)
    session_description: str = Field(min_length=1)
    experimenter: str = Field(min_length=1)
    output_directory: Path
    recording_duration_s: float = Field(default=5.0, gt=0, le=24 * 60 * 60)
    session_start_time: datetime = Field(default_factory=lambda: datetime.now(UTC))
    source: AcquisitionSourceConfig = Field(default_factory=CustomSourceConfig)
    camera: CameraConfig
    rois: tuple[ROIConfig, ...] = Field(min_length=1, max_length=9)
    channels: tuple[ChannelConfig, ...] = Field(min_length=1, max_length=3)
    ttl_inputs: tuple[TTLInputConfig, ...] = Field(default=(), max_length=4)
    lab: str | None = None
    institution: str | None = None
    display: TraceDisplayConfig = Field(default_factory=TraceDisplayConfig)
    recording: RecordingControlConfig = Field(default_factory=RecordingControlConfig)
    event_markers: tuple[EventMarkerConfig, ...] = Field(default=(), max_length=9)
    ttl_events: tuple[TTLEventRuleConfig, ...] = Field(default=(), max_length=8)

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
        if not any(channel.enabled for channel in self.channels):
            raise ValueError("at least one excitation channel must be enabled")

        ttl_lines = [ttl.line for ttl in self.ttl_inputs]
        if len(ttl_lines) != len(set(ttl_lines)):
            raise ValueError("TTL input lines must be unique")
        keys = [marker.key for marker in self.event_markers]
        if len(keys) != len(set(keys)):
            raise ValueError("each event-marker key may be used once")
        for rule in self.ttl_events:
            ttl = next((item for item in self.ttl_inputs if item.line == rule.line), None)
            if ttl is None or ttl.edges is not EdgeSelection.BOTH:
                raise ValueError(
                    f"TTL event {rule.name!r} measures pulse widths, so TTL input {rule.line} "
                    "must be configured to record both edges"
                )
        recording = self.recording
        if recording.stop == "ttl":
            stop_input = next(
                (ttl for ttl in self.ttl_inputs if ttl.line == recording.stop_ttl_line), None
            )
            if stop_input is None or stop_input.edges.value not in {
                "both",
                recording.stop_ttl_edge,
            }:
                raise ValueError(
                    f"stopping on TTL input {recording.stop_ttl_line} needs that input "
                    f"configured to record its {recording.stop_ttl_edge} edges"
                )

        for roi in self.rois:
            self._check_circle(roi.fiber_id, roi.center_x_px, roi.center_y_px, roi.radius_px, "")
        self._validate_profile(profile_for(self.source))
        return self

    def _check_circle(self, fiber_id: str, x: float, y: float, radius: float, where: str) -> None:
        if x + radius > self.camera.width_px:
            raise ValueError(f"ROI {fiber_id}{where} exceeds camera width")
        if y + radius > self.camera.height_px:
            raise ValueError(f"ROI {fiber_id}{where} exceeds camera height")
        if x - radius < 0:
            raise ValueError(f"ROI {fiber_id}{where} exceeds camera left edge")
        if y - radius < 0:
            raise ValueError(f"ROI {fiber_id}{where} exceeds camera top edge")

    def _validate_profile(self, profile: SystemProfile) -> None:
        name = profile.display_name
        for channel in self.channels:
            if channel.wavelength_nm not in profile.wavelengths:
                raise ValueError(f"{name} has no {int(channel.wavelength_nm)} nm excitation")
            uses_voltage = channel.voltage_v is not None and channel.power_percent is None
            uses_percent = channel.power_percent is not None and channel.voltage_v is None
            if profile.intensity_unit == "V" and not uses_voltage:
                raise ValueError(f"{name} channels set voltage_v and leave power_percent empty")
            if profile.intensity_unit == "%" and not uses_percent:
                raise ValueError(f"{name} channels set power_percent and leave voltage_v empty")
        enabled = frozenset(channel.wavelength_nm for channel in self.enabled_channels)
        allowed = profile.allowed_wavelength_sets
        if allowed is not None and enabled not in allowed:
            names = "+".join(str(int(w)) for w in sorted(enabled))
            raise ValueError(f"{name} cannot interleave {names} nm")
        shown = set(self.display.visible_wavelengths) - {int(w) for w in profile.wavelengths}
        if shown:
            names = ", ".join(str(w) for w in sorted(shown))
            raise ValueError(f"{name} cannot display {names} nm traces")
        camera = self.camera
        if profile.sensor_size_px is not None and (
            (camera.width_px, camera.height_px) != profile.sensor_size_px
        ):
            width, height = profile.sensor_size_px
            raise ValueError(f"{name} cameras record the full {width}x{height} sensor")
        fps = camera.requested_fps
        if profile.integer_frame_rate and fps != int(fps):
            raise ValueError(f"{name} frame rate must be a whole number of hertz")
        if profile.frame_rate_range_hz is not None:
            low, high = profile.frame_rate_range_hz
            if not low <= fps <= high:
                raise ValueError(f"{name} frame rate must be between {low:g} and {high:g} Hz")
        ceiling = profile.maximum_exposure_us(fps)
        if ceiling is not None and camera.exposure_us > ceiling + 1e-6:
            raise ValueError(f"{name} exposure at {fps:g} Hz must not exceed {ceiling:g} us")
        floor = profile.minimum_exposure_us
        if floor is not None and camera.exposure_us < floor:
            raise ValueError(f"{name} exposure must be at least {floor:g} us")
        needs_camera2 = any(
            profile.camera_for(channel.wavelength_nm) == 2 for channel in self.enabled_channels
        )
        for roi in self.rois:
            if roi.has_camera2_geometry:
                if profile.camera_count < 2:
                    raise ValueError(f"{name} has one camera; ROI {roi.fiber_id} sets camera 2")
                self._check_circle(
                    roi.fiber_id,
                    float(roi.camera2_center_x_px or 0.0),
                    float(roi.camera2_center_y_px or 0.0),
                    float(roi.camera2_radius_px or 0.0),
                    " on camera 2",
                )
            elif needs_camera2 and roi.enabled:
                raise ValueError(f"ROI {roi.fiber_id} needs camera-2 geometry for 560 nm")

    @property
    def recording_limit_s(self) -> float:
        """How long a recording may run: its duration, or 24 h when it stops on Stop."""

        if self.recording.stop == "manual":
            return MANUAL_STOP_LIMIT_S
        return self.recording_duration_s

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
    system: Literal["custom", "rwd"] = "custom",
) -> SessionConfig:
    """Build a deterministic, valid configuration for demos and tests.

    For ``system="rwd"`` the camera is the profile's full sensor (``width_px`` and
    ``height_px`` are ignored), channels use 5 % power, and each fiber's camera-2 circle
    starts at its camera-1 position until it is calibrated.
    """

    if not 1 <= fiber_count <= 9:
        raise ValueError("fiber_count must be between 1 and 9")
    profile = RWD_PROFILE if system == "rwd" else CUSTOM_PROFILE
    if profile.sensor_size_px is not None:
        width_px, height_px = profile.sensor_size_px
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
            **(
                {
                    "camera2_center_x_px": (index % grid_columns + 1) * spacing_x,
                    "camera2_center_y_px": (index // grid_columns + 1) * spacing_y,
                    "camera2_radius_px": radius,
                }
                if profile.camera_count > 1
                else {}
            ),
        )
        for index in range(fiber_count)
    )
    if profile.intensity_unit == "%":
        channels = tuple(
            ChannelConfig(wavelength_nm=wavelength, voltage_v=None, power_percent=5.0)
            for wavelength in profile.wavelengths
        )
    else:
        channels = tuple(
            ChannelConfig(wavelength_nm=wavelength, voltage_v=1.0)
            for wavelength in profile.wavelengths
        )
    ttl_inputs = tuple(TTLInputConfig(line=line, label=f"behavior_{line}") for line in range(1, 5))
    started = datetime.now(UTC)
    camera = profile.default_camera.model_copy(
        update={"width_px": width_px, "height_px": height_px, "raw_capture": raw_capture}
    )
    return SessionConfig(
        session_id=started.strftime(f"{system if system == 'rwd' else 'demo'}-%Y%m%dT%H%M%S-%fZ"),
        session_description="Synthetic multichannel fiber-photometry demonstration",
        experimenter="Simulator",
        output_directory=output_directory,
        recording_duration_s=recording_duration_s,
        session_start_time=started,
        source=RWDSourceConfig() if system == "rwd" else CustomSourceConfig(),
        camera=camera,
        rois=rois,
        channels=channels,
        ttl_inputs=ttl_inputs,
        lab="Driftless Bundle Photometry",
        display=TraceDisplayConfig(visible_wavelengths=tuple(int(w) for w in profile.wavelengths)),
    )
