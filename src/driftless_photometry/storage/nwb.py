"""Build, validate, and atomically finalize canonical per-ROI NWB outputs."""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np
from hdmf.backends.hdf5.h5_utils import H5DataIO
from hdmf.common import DynamicTable
from hdmf.data_utils import DataChunkIterator
from ndx_fiber_photometry import (
    CommandedVoltageSeries,
    FiberPhotometry,
    FiberPhotometryIndicators,
    FiberPhotometryResponseSeries,
    FiberPhotometryTable,
)
from ndx_ophys_devices import (
    ExcitationSource,
    ExcitationSourceModel,
    FiberInsertion,
    Indicator,
    OpticalFiber,
    OpticalFiberModel,
    Photodetector,
    PhotodetectorModel,
)
from nwbinspector import Importance, inspect_nwbfile
from pynwb import NWBHDF5IO, NWBFile, TimeSeries, validate
from pynwb.base import Images
from pynwb.epoch import TimeIntervals
from pynwb.file import EventsTable, Subject
from pynwb.image import GrayscaleImage, ImageSeries, RGBImage

from driftless_photometry import __version__
from driftless_photometry.config import (
    RWD_PROFILE,
    ROIConfig,
    SessionConfig,
    SystemProfile,
    Wavelength,
    profile_for,
)
from driftless_photometry.diagnostics import FinalizationProgress, FinalizationStage
from driftless_photometry.domain import AcquisitionData
from driftless_photometry.provenance import (
    NWB_RUNTIME_PROVENANCE_SCRATCH_NAME,
    capture_runtime_provenance,
)
from driftless_photometry.roi import render_annotated_rois, rois_for_camera
from driftless_photometry.settings import NWB_SETTINGS_SCRATCH_NAME, settings_json

from .frames import FrameStream

if TYPE_CHECKING:
    from numpy.typing import NDArray


_WAVELENGTH_SOURCE_DESCRIPTION = (
    "How the wavelength is known: controller (reported by the timing controller for this "
    "exposure) or phase_derived (derived from camera identity and the trigger slot after "
    "a sequencer restart; RWD hardware only)."
)


def _control_codes(profile: SystemProfile) -> dict[Wavelength, int]:
    """ImageSeries control values: each wavelength's index in the system's wavelength list."""

    return {wavelength: index for index, wavelength in enumerate(profile.wavelengths)}


def _control_descriptions(profile: SystemProfile) -> list[str]:
    return [f"{int(wavelength)} nm" for wavelength in profile.wavelengths]


@dataclass(frozen=True, slots=True)
class _Intensity:
    """The commanded-intensity column for a system: volts (Custom) or percent (RWD)."""

    column: str
    unit: str
    description: str

    def values(self, data: AcquisitionData) -> list[float]:
        if self.unit == "percent":
            return data.frame_commanded_power_percent
        return data.frame_commanded_voltages_v


def _intensity(config: SessionConfig) -> _Intensity:
    if profile_for(config.source).intensity_unit == "%":
        return _Intensity(
            "commanded_power_percent",
            "percent",
            "Commanded RWD LED power ratio (0-100 %) for this exposure; not an optical "
            "power measurement.",
        )
    return _Intensity(
        "commanded_voltage_v", "volts", "Actual commanded LED intensity for this exposure."
    )


def _camera_device_names(config: SessionConfig) -> dict[int, str]:
    profile = profile_for(config.source)
    if profile is RWD_PROFILE:
        return {camera: f"rwd_camera_{camera}" for camera in range(1, profile.camera_count + 1)}
    return {1: "cs505mu_camera"}


def _tick_description(config: SessionConfig) -> str:
    if profile_for(config.source) is RWD_PROFILE:
        return (
            "Exposure time on the RWD board's trigger grid in microseconds since session "
            "zero; derived, because the RWD controller reports no per-exposure ticks."
        )
    return "Unwrapped raw controller tick in microseconds."


def _sequence_description(config: SessionConfig) -> str:
    if profile_for(config.source) is RWD_PROFILE:
        return "Monotonic exposure sequence counted from the RWD board's trigger slots."
    return "Monotonic exposure sequence reported by the controller."


@dataclass(frozen=True, slots=True)
class NWBWriteReport:
    path: Path
    pynwb_validation_errors: tuple[str, ...]
    inspector_messages: tuple[str, ...]
    frame_count: int
    trace_sample_count: int
    ttl_edge_count: int


def _safe_filename(value: str) -> str:
    safe = "".join(
        character if character.isalnum() or character in "-_." else "_" for character in value
    )
    safe = safe.strip("._")
    if not safe:
        raise ValueError("session_id does not contain filename-safe characters")
    return safe


def _make_events_table(
    name: str,
    description: str,
    source_description: str,
    columns: tuple[tuple[str, str], ...],
) -> EventsTable:
    table = EventsTable(
        name=name,
        description=description,
        source_description=source_description,
    )
    for column_name, column_description in columns:
        table.add_column(name=column_name, description=column_description)
    return table


def _validate_inputs(
    config: SessionConfig,
    data: AcquisitionData,
    frames: NDArray[np.uint16] | FrameStream | None,
    calibration_image: NDArray[np.uint16] | None,
    wavelength_images: Mapping[Wavelength, NDArray[np.uint16]] | None,
) -> None:
    profile = profile_for(config.source)
    frame_count = len(data.frame_ids)
    parallel_lengths = {
        len(data.frame_timestamps_s),
        len(data.frame_sequences),
        len(data.frame_ticks_us),
        len(data.frame_wavelengths_nm),
        len(data.frame_commanded_voltages_v),
        len(data.frame_commanded_power_percent),
        len(data.frame_camera_indices),
        len(data.frame_wavelength_sources),
        len(data.frame_camera_timestamps_ns),
        len(data.frame_host_received_s),
        len(data.frame_saturation_fractions),
        len(data.frame_saved_indices),
        frame_count,
    }
    if len(parallel_lengths) != 1:
        raise ValueError("camera frame metadata columns have different lengths")

    expected_roi_count = len(config.enabled_rois)
    for index in range(frame_count):
        host_received_s = data.frame_host_received_s[index]
        saturation = data.frame_saturation_fractions[index]
        if profile.intensity_unit == "%":
            power = data.frame_commanded_power_percent[index]
            if not np.isfinite(power) or not 0 <= power <= 100:
                raise ValueError("per-frame commanded power must be between 0 and 100 %")
            if np.isfinite(data.frame_commanded_voltages_v[index]):
                raise ValueError(f"{profile.display_name} frames do not command a voltage")
        else:
            voltage = data.frame_commanded_voltages_v[index]
            if not np.isfinite(voltage) or not 0 <= voltage <= 5:
                raise ValueError("per-frame commanded voltage must be finite and between 0 and 5 V")
        camera_index = data.frame_camera_indices[index]
        if not 1 <= camera_index <= profile.camera_count:
            raise ValueError(f"{profile.display_name} has no camera {camera_index}")
        wavelength = Wavelength(data.frame_wavelengths_nm[index])
        if profile.camera_for(wavelength) != camera_index:
            raise ValueError(f"{int(wavelength)} nm frames must come from camera {camera_index}")
        source = data.frame_wavelength_sources[index]
        if source not in {"controller", "phase_derived"}:
            raise ValueError(f"unknown wavelength source {source!r}")
        if source == "phase_derived" and profile is not RWD_PROFILE:
            raise ValueError("only RWD hardware may record phase-derived wavelengths")
        if not np.isfinite(host_received_s):
            raise ValueError("per-frame host receipt time must be finite")
        if saturation.shape != (expected_roi_count,):
            raise ValueError("per-frame saturation ROI count does not match configuration")
        if not np.all(np.isfinite(saturation)) or np.any((saturation < 0) | (saturation > 1)):
            raise ValueError("per-frame saturation fractions must be finite and within [0, 1]")

    if isinstance(frames, np.ndarray):
        if frames.dtype != np.uint16 or frames.ndim != 3:
            raise TypeError("raw frames must be a uint16 [time, y, x] array")
        expected_shape = (config.camera.height_px, config.camera.width_px)
        if tuple(frames.shape[1:]) != expected_shape:
            raise ValueError(f"raw frame shape {frames.shape[1:]} does not match {expected_shape}")
        if frames.shape[0] != frame_count:
            raise ValueError("raw frame count does not match camera frame event count")
    elif isinstance(frames, FrameStream):
        expected_shape = (config.camera.height_px, config.camera.width_px)
        if frames.frame_shape != expected_shape:
            raise ValueError(
                f"raw frame stream shape {frames.frame_shape} does not match {expected_shape}"
            )
        if frames.count != frame_count:
            raise ValueError("raw frame count does not match camera frame event count")
    if config.camera.raw_capture != (frames is not None):
        raise ValueError("raw frame payload does not match camera.raw_capture setting")

    if calibration_image is not None:
        if calibration_image.dtype != np.uint16 or calibration_image.ndim != 2:
            raise TypeError("calibration image must be a uint16 [y, x] array")
        if tuple(calibration_image.shape) != (config.camera.height_px, config.camera.width_px):
            raise ValueError("calibration image shape does not match configured camera shape")

    enabled_wavelengths = {channel.wavelength_nm for channel in config.enabled_channels}
    for wavelength, image in (wavelength_images or {}).items():
        if wavelength not in enabled_wavelengths:
            raise ValueError(f"{int(wavelength)} nm reference image is not an enabled channel")
        if image.dtype != np.uint16 or image.ndim != 2:
            raise TypeError("wavelength reference images must be uint16 [y, x] arrays")
        if tuple(image.shape) != (config.camera.height_px, config.camera.width_px):
            raise ValueError(
                f"{int(wavelength)} nm reference image shape does not match configured camera"
            )

    for wavelength, samples in data.traces.items():
        previous_timestamp = -np.inf
        for sample in samples:
            if sample.wavelength_nm != wavelength:
                raise ValueError("trace sample is stored under the wrong wavelength")
            if len(sample.values) != expected_roi_count:
                raise ValueError("trace sample ROI count does not match configuration")
            if sample.timestamp_s <= previous_timestamp:
                raise ValueError("trace timestamps must increase within each wavelength")
            previous_timestamp = sample.timestamp_s


def _create_nwbfile(config: SessionConfig, roi: ROIConfig, data: AcquisitionData) -> NWBFile:
    roi_session_id = f"{config.session_id}__{roi.fiber_id}"
    nwbfile = NWBFile(
        session_description=config.session_description,
        identifier=roi_session_id,
        session_start_time=config.session_start_time,
        session_id=roi_session_id,
        experimenter=[config.experimenter],
        lab=config.lab,
        institution=config.institution,
        keywords=["fiber photometry", "multichannel", "NWB"],
        data_collection=(
            "Interleaved excitation with one explicit wavelength per camera exposure; "
            "camera imagery represents the proximal fiber-bundle face."
            + (
                " Recorded on RWD hardware driven directly by DBF: wavelength identity is "
                "phase-derived from camera identity and the trigger slot after each "
                "sequencer restart (events/camera_frames.wavelength_source)."
                if profile_for(config.source) is RWD_PROFILE
                else ""
            )
        ),
        subject=Subject(
            subject_id=roi.animal_id,
            age=roi.subject_age,
            sex=roi.subject_sex,
            description=(
                f"Animal assigned to {roi.fiber_id}; brain region: {roi.brain_region}; "
                f"sensor type: {roi.sensor_type}."
            ),
        ),
        source_script=("NWB file generated by driftless_photometry.storage.nwb.write_session_nwbs"),
        source_script_file_name="driftless_photometry/storage/nwb.py",
        was_generated_by=[["driftless-bundle-photometry", __version__]],
    )
    nwbfile.add_scratch(
        settings_json(config),
        name=NWB_SETTINGS_SCRATCH_NAME,
        description=(
            "Versioned complete application settings used for this recording, including "
            "all configured ROIs and display preferences."
        ),
    )
    provenance = data.runtime_provenance or capture_runtime_provenance(
        adapter_name="not supplied",
        protocol_version="native_acquisition_v1",
    )
    nwbfile.add_scratch(
        json.dumps(provenance.to_document(), indent=2, sort_keys=True) + "\n",
        name=NWB_RUNTIME_PROVENANCE_SCRATCH_NAME,
        description=(
            "Runtime application, Python, operating-system, dependency, adapter, and "
            "protocol versions used to acquire or finalize this recording."
        ),
    )
    return nwbfile


def _add_photometry_metadata_and_traces(
    nwbfile: NWBFile,
    config: SessionConfig,
    data: AcquisitionData,
    roi_index: int,
) -> None:
    roi = config.enabled_rois[roi_index]
    profile = profile_for(config.source)
    cameras = _add_photodetectors(nwbfile, config)

    fiber_model = OpticalFiberModel(
        name="optical_fiber_model_not_supplied",
        manufacturer="not supplied",
        description=(
            "Populate model, numerical aperture, core diameter, and ferrule metadata "
            "before hardware use."
        ),
        numerical_aperture=float("nan"),
    )
    nwbfile.add_device_model(fiber_model)
    optical_fiber = OpticalFiber(
        name=f"optical_{roi.fiber_id}",
        description=f"Optical fiber corresponding to camera ROI {roi.fiber_id}.",
        model=fiber_model,
        fiber_insertion=FiberInsertion(name="fiber_insertion"),
    )
    nwbfile.add_device(optical_fiber)
    indicator = Indicator(
        name="indicator_01",
        label=roi.sensor_type,
        description="Sensor/indicator metadata supplied for this ROI and animal.",
    )

    excitation_sources: dict[Wavelength, ExcitationSource] = {}
    voltage_series: dict[Wavelength, CommandedVoltageSeries] = {}
    rwd = profile is RWD_PROFILE
    for channel in config.enabled_channels:
        wavelength = channel.wavelength_nm
        source_model = ExcitationSourceModel(
            name=f"led_{int(wavelength)}_model",
            manufacturer="RWD Life Science" if rwd else "not supplied",
            description=(
                f"RWD {profile.display_name} {int(wavelength)} nm excitation LED."
                if rwd
                else f"Excitation LED model for {int(wavelength)} nm; populate before hardware use."
            ),
            source_type="LED",
            excitation_mode="one-photon",
            wavelength_range_in_nm=[float(wavelength), float(wavelength)],
        )
        nwbfile.add_device_model(source_model)
        source = ExcitationSource(
            name=f"led_{int(wavelength)}",
            description=f"{int(wavelength)} nm excitation source.",
            model=source_model,
            exposure_time_in_s=config.camera.exposure_us * 1e-6,
        )
        nwbfile.add_device(source)
        excitation_sources[wavelength] = source
        frame_indices = [
            index
            for index, observed in enumerate(data.frame_wavelengths_nm)
            if observed == int(wavelength)
        ]
        timestamps = np.asarray(
            [data.frame_timestamps_s[index] for index in frame_indices], dtype=np.float64
        )
        if rwd:
            nwbfile.add_stimulus(
                TimeSeries(
                    name=f"led_{int(wavelength)}_commanded_power",
                    description=(
                        "Commanded RWD LED power ratio (0-100 %, OFRS's power setting) for "
                        "every observed exposure; not an optical-power measurement. An empty "
                        "series means the wavelength was configured but not observed."
                    ),
                    data=np.asarray(
                        [data.frame_commanded_power_percent[index] for index in frame_indices],
                        dtype=np.float32,
                    ),
                    unit="percent",
                    timestamps=timestamps,
                )
            )
            continue
        command = CommandedVoltageSeries(
            name=f"led_{int(wavelength)}_commanded_voltage",
            description=(
                "Actual commanded analog intensity for every observed exposure; this is "
                "not an optical-power readback. An empty series means the wavelength was "
                "configured but not observed before an interrupted stop."
            ),
            data=np.asarray(
                [data.frame_commanded_voltages_v[index] for index in frame_indices],
                dtype=np.float32,
            ),
            unit="volts",
            timestamps=timestamps,
        )
        nwbfile.add_stimulus(command)
        voltage_series[wavelength] = command

    table = FiberPhotometryTable(
        name="fiber_photometry_table",
        description="One row per physical fiber and excitation/emission path.",
    )
    table.add_column(
        name="camera_roi_id", description="Stable ID in processing/photometry/camera_rois."
    )
    table.add_column(
        name="signal_role",
        description="Role of the excitation path, such as isosbestic_control or green_signal.",
    )
    row_indices: dict[Wavelength, list[int]] = {}
    for row_index, channel in enumerate(config.enabled_channels):
        row: dict[str, object] = {
            "location": roi.brain_region,
            "excitation_wavelength_in_nm": float(channel.wavelength_nm),
            "emission_wavelength_in_nm": float(channel.emission_wavelength_nm),
            "indicator": indicator,
            "optical_fiber": optical_fiber,
            "excitation_source": excitation_sources[channel.wavelength_nm],
            "photodetector": cameras[profile.camera_for(channel.wavelength_nm)],
            "camera_roi_id": roi.fiber_id,
            "signal_role": str(channel.role),
            "notes": (
                f"Animal {roi.animal_id}; camera-space geometry is stored in the camera_rois table."
            ),
        }
        if not rwd:
            row["commanded_voltage_series"] = voltage_series[channel.wavelength_nm]
        table.add_row(**row)
        row_indices[channel.wavelength_nm] = [row_index]

    photometry_metadata = FiberPhotometry(
        name="FiberPhotometry",
        fiber_photometry_table=table,
        fiber_photometry_indicators=FiberPhotometryIndicators(indicators=[indicator]),
    )
    nwbfile.add_lab_meta_data(photometry_metadata)

    for channel in config.enabled_channels:
        samples = data.traces[channel.wavelength_nm]
        if not samples:
            continue
        response_data = np.asarray(
            [[sample.values[roi_index]] for sample in samples],
            dtype=np.float32,
        )
        timestamps = np.asarray([sample.timestamp_s for sample in samples], dtype=np.float64)
        region = table.create_region(
            name="fiber_photometry_table_region",
            region=row_indices[channel.wavelength_nm],
            description=(
                f"Rows for {int(channel.wavelength_nm)} nm, ordered identically to "
                "response columns."
            ),
        )
        response = FiberPhotometryResponseSeries(
            name=f"raw_fluorescence_{int(channel.wavelength_nm)}",
            description=(
                "Mean raw camera ADC counts within each circular ROI; no baseline "
                "correction or normalization."
            ),
            data=H5DataIO(response_data, compression="gzip", compression_opts=1, shuffle=True),
            unit="counts",
            timestamps=timestamps,
            fiber_photometry_table_region=region,
        )
        nwbfile.add_acquisition(response)


def _add_photodetectors(nwbfile: NWBFile, config: SessionConfig) -> dict[int, Photodetector]:
    profile = profile_for(config.source)
    if profile is RWD_PROFILE:
        model = PhotodetectorModel(
            name="alvium_1800_u_040m_photodetector_model",
            manufacturer="Allied Vision",
            model_number="Alvium 1800 U-040m",
            description=(
                "Monochrome CMOS camera (Sony IMX287) of an RWD photometry system, triggered "
                "by the RWD controller and imaging the proximal fiber-bundle face."
            ),
            detector_type="CMOS",
            wavelength_range_in_nm=[300.0, 1100.0],
            gain=config.camera.gain,
            gain_unit="dB",
        )
        nwbfile.add_device_model(model)
        cameras = {}
        for camera, name in _camera_device_names(config).items():
            wavelengths = ", ".join(
                f"{int(w)} nm" for w in profile.wavelengths if profile.camera_for(w) == camera
            )
            cameras[camera] = Photodetector(
                name=name,
                description=(
                    f"RWD camera {camera} (DeviceUserID {camera}); the controller triggers it "
                    f"only for {wavelengths} exposures."
                ),
                serial_number="not supplied",
                model=model,
            )
            nwbfile.add_device(cameras[camera])
        return cameras
    camera_model = PhotodetectorModel(
        name="cs505mu_photodetector_model",
        manufacturer="Thorlabs",
        model_number="CS505MU",
        description="Monochrome CMOS camera used to measure proximal fiber-face emission.",
        detector_type="CMOS",
        wavelength_range_in_nm=[350.0, 1000.0],
        gain=config.camera.gain,
        gain_unit="camera gain setting",
    )
    nwbfile.add_device_model(camera_model)
    camera = Photodetector(
        name="cs505mu_camera",
        description="Camera detector shared by every fiber and excitation path.",
        serial_number=config.camera.serial_number or "not supplied",
        model=camera_model,
    )
    nwbfile.add_device(camera)
    return {1: camera}


def _add_rois_and_calibration(
    nwbfile: NWBFile,
    config: SessionConfig,
    calibration_image: NDArray[np.uint16] | None,
    wavelength_images: Mapping[Wavelength, NDArray[np.uint16]] | None,
    roi_index: int,
    calibration_camera: int = 1,
) -> None:
    profile = profile_for(config.source)
    two_cameras = profile.camera_count > 1
    module = nwbfile.create_processing_module(
        name="photometry",
        description=(
            "Camera-space ROI geometry, calibration images, and future derived photometry data."
        ),
    )
    roi_table = DynamicTable(
        name="camera_rois",
        description="Authoritative circular ROIs in full-frame camera pixel coordinates [y, x].",
    )
    for name, description in (
        ("fiber_id", "Stable fiber identifier used by FiberPhotometryTable.camera_roi_id."),
        ("label", "User-facing ROI label."),
        ("center_x_px", "Circle center column in full-frame pixels."),
        ("center_y_px", "Circle center row in full-frame pixels."),
        ("radius_px", "Circle radius in pixels."),
    ):
        roi_table.add_column(
            name=name, description=description + (" On camera 1." if two_cameras else "")
        )
    if two_cameras:
        for name, description in (
            ("camera2_center_x_px", "Circle center column on camera 2 (560 nm), or NaN."),
            ("camera2_center_y_px", "Circle center row on camera 2 (560 nm), or NaN."),
            ("camera2_radius_px", "Circle radius on camera 2 (560 nm), or NaN."),
        ):
            roi_table.add_column(name=name, description=description)
    roi = config.enabled_rois[roi_index]
    row: dict[str, object] = {
        "fiber_id": roi.fiber_id,
        "label": roi.label,
        "center_x_px": roi.center_x_px,
        "center_y_px": roi.center_y_px,
        "radius_px": roi.radius_px,
    }
    if two_cameras:
        row["camera2_center_x_px"] = _or_nan(roi.camera2_center_x_px)
        row["camera2_center_y_px"] = _or_nan(roi.camera2_center_y_px)
        row["camera2_radius_px"] = _or_nan(roi.camera2_radius_px)
    roi_table.add_row(**row)
    module.add(roi_table)

    if calibration_image is not None:
        calibration_images = Images(
            name="calibration_images",
            description="Static images used to define and verify circular fiber ROIs.",
            images=[
                GrayscaleImage(
                    name="reference_frame",
                    data=calibration_image,
                    description=(
                        "Unnormalized uint16 reference frame in [y, x] orientation"
                        + (f", from camera {calibration_camera}." if two_cameras else ".")
                    ),
                )
            ],
        )
        module.add(calibration_images)

    if wavelength_images:
        diagnostic_images: list[GrayscaleImage | RGBImage] = []
        for wavelength in sorted(wavelength_images, key=int):
            image = wavelength_images[wavelength]
            camera = profile.camera_for(wavelength)
            placed = rois_for_camera((roi,), camera)
            diagnostic_images.extend(
                (
                    GrayscaleImage(
                        name=f"raw_reference_{int(wavelength)}nm",
                        data=image,
                        description=(
                            f"Original unnormalized uint16 camera frame captured during a "
                            f"{int(wavelength)} nm exposure."
                        ),
                    ),
                    RGBImage(
                        name=f"roi_overlay_{int(wavelength)}nm",
                        data=render_annotated_rois(
                            image,
                            placed,
                            bit_depth=config.camera.bit_depth,
                        ),
                        description=(
                            f"Derived fixed-scale RGB view of the {int(wavelength)} nm "
                            f"reference frame with ROI {roi.fiber_id} outlined."
                        ),
                    ),
                )
            )
        module.add(
            Images(
                name="wavelength_roi_images",
                description=(
                    "One original camera reference and one derived ROI-annotated view for "
                    "each excitation wavelength observed during this recording."
                ),
                images=diagnostic_images,
            )
        )


def _add_events(
    nwbfile: NWBFile,
    config: SessionConfig,
    data: AcquisitionData,
    roi_index: int,
    additional_system_events: tuple[str, ...],
) -> None:
    intensity = _intensity(config)
    intensities = intensity.values(data)
    frames = _make_events_table(
        name="camera_frames",
        description="One event for every camera exposure received by the acquisition pipeline.",
        source_description="Camera and hardware-controller matcher.",
        columns=(
            ("camera_frame_id", "Raw camera frame identifier, counted per camera."),
            ("camera_index", "Camera that recorded the exposure (1, or 2 for RWD 560 nm)."),
            (
                "camera_timestamp_ns",
                "The camera's own hardware timestamp in nanoseconds, or -1 if none.",
            ),
            ("controller_sequence", _sequence_description(config)),
            ("wavelength_nm", "Explicit excitation wavelength active for this exposure."),
            ("wavelength_source", _WAVELENGTH_SOURCE_DESCRIPTION),
            ("controller_tick_us", _tick_description(config)),
            (intensity.column, intensity.description),
            ("host_received_s", "Host receipt time on the session-relative monotonic clock."),
            (
                "roi_saturation_fraction",
                "Fraction of pixels saturated inside the ROI represented by this file.",
            ),
            ("raw_saved", "Whether the original frame is embedded in camera_frames ImageSeries."),
            ("raw_image_index", "Index into camera_frames ImageSeries, or -1 when not saved."),
        ),
    )
    for index in range(len(data.frame_ids)):
        saved_index = data.frame_saved_indices[index]
        frames.add_row(
            timestamp=data.frame_timestamps_s[index],
            camera_frame_id=data.frame_ids[index],
            camera_index=data.frame_camera_indices[index],
            camera_timestamp_ns=data.frame_camera_timestamps_ns[index],
            controller_sequence=data.frame_sequences[index],
            wavelength_nm=data.frame_wavelengths_nm[index],
            wavelength_source=data.frame_wavelength_sources[index],
            controller_tick_us=data.frame_ticks_us[index],
            **{intensity.column: intensities[index]},
            host_received_s=data.frame_host_received_s[index],
            roi_saturation_fraction=float(data.frame_saturation_fractions[index][roi_index]),
            raw_saved=saved_index >= 0,
            raw_image_index=saved_index,
        )
    nwbfile.add_events_table(frames)

    excitation = _make_events_table(
        name="excitation_events",
        description="One explicit excitation command record for every observed exposure.",
        source_description="Deterministic hardware controller exposure records.",
        columns=(
            ("controller_sequence", _sequence_description(config)),
            ("wavelength_nm", "Explicit excitation wavelength commanded for the exposure."),
            ("wavelength_source", _WAVELENGTH_SOURCE_DESCRIPTION),
            ("controller_tick_us", _tick_description(config)),
            (intensity.column, intensity.description),
        ),
    )
    for index in range(len(data.frame_ids)):
        excitation.add_row(
            timestamp=data.frame_timestamps_s[index],
            controller_sequence=data.frame_sequences[index],
            wavelength_nm=data.frame_wavelengths_nm[index],
            wavelength_source=data.frame_wavelength_sources[index],
            controller_tick_us=data.frame_ticks_us[index],
            **{intensity.column: intensities[index]},
        )
    nwbfile.add_events_table(excitation)

    if data.ttl_edges:
        ttl = _make_events_table(
            name="ttl_edges",
            description="Observed rising and falling edges from up to four behavior TTL inputs.",
            source_description="Hardware controller digital inputs.",
            columns=(
                ("line", "Controller TTL input number, 1 through 4."),
                ("label", "Configured semantic label for the input."),
                ("edge", "Observed edge: rising or falling."),
                ("new_level", "Digital level immediately after the edge."),
                ("controller_tick_us", "Unwrapped raw controller tick in microseconds."),
                ("controller_sequence", "Controller event sequence number."),
            ),
        )
        for edge in data.ttl_edges:
            ttl.add_row(
                timestamp=edge.timestamp_s,
                line=edge.line,
                label=edge.label,
                edge=edge.edge,
                new_level=edge.new_level,
                controller_tick_us=edge.controller_tick_us,
                controller_sequence=edge.sequence,
            )
        nwbfile.add_events_table(ttl)

    if data.dropped_frames:
        dropped = _make_events_table(
            name="dropped_frame_events",
            description="Detected gaps in the authoritative controller exposure sequence.",
            source_description="Driftless acquisition integrity checks.",
            columns=(
                ("expected_sequence", "First controller exposure sequence expected."),
                ("observed_sequence", "Next controller exposure sequence actually received."),
                ("missing_count", "Number of missing exposure records."),
                ("reason", "Machine-readable drop classification."),
            ),
        )
        for event in data.dropped_frames:
            dropped.add_row(
                timestamp=event.timestamp_s,
                expected_sequence=event.expected_sequence,
                observed_sequence=event.observed_sequence,
                missing_count=event.missing_count,
                reason=event.reason,
            )
        nwbfile.add_events_table(dropped)

    system = _make_events_table(
        name="system_events",
        description="Acquisition lifecycle events.",
        source_description="Driftless acquisition coordinator.",
        columns=(
            ("event", "Lifecycle or machine-readable fault classification."),
            ("detail", "Human-readable detail; empty for ordinary lifecycle events."),
        ),
    )
    system.add_row(timestamp=0.0, event="recording_started", detail="")
    if data.frame_timestamps_s:
        system.add_row(
            timestamp=data.frame_timestamps_s[-1],
            event="recording_stopped",
            detail="",
        )
        for event in data.system_events:
            system.add_row(
                timestamp=event.timestamp_s,
                event=event.event,
                detail=event.detail,
            )
        for event in additional_system_events:
            system.add_row(
                timestamp=data.frame_timestamps_s[-1],
                event=event,
                detail="",
            )
    nwbfile.add_events_table(system)

    if data.invalid_times:
        invalid_times = TimeIntervals(
            name="invalid_times",
            description=(
                "Continuous session spans whose scientific validity is compromised; the "
                "reason and originating event are retained explicitly."
            ),
        )
        invalid_times.add_column(name="reason", description="Human-readable invalidity reason.")
        invalid_times.add_column(
            name="source_event",
            description="Machine-readable event or fault that created this invalid span.",
        )
        for interval in data.invalid_times:
            invalid_times.add_row(
                start_time=interval.start_time_s,
                stop_time=interval.stop_time_s,
                tags=(interval.source_event,),
                reason=interval.reason,
                source_event=interval.source_event,
            )
        nwbfile.add_time_intervals(invalid_times)


def _add_raw_frames(
    nwbfile: NWBFile,
    config: SessionConfig,
    data: AcquisitionData,
    frames: NDArray[np.uint16] | FrameStream | None,
) -> None:
    if frames is None:
        return
    profile = profile_for(config.source)
    codes = _control_codes(profile)
    controls = np.asarray(
        [codes[Wavelength(value)] for value in data.frame_wavelengths_nm],
        dtype=np.uint8,
    )
    devices = _camera_device_names(config)
    used_cameras = set(data.frame_camera_indices)
    device = nwbfile.devices[devices[next(iter(used_cameras))]] if len(used_cameras) == 1 else None
    if isinstance(frames, FrameStream):
        frame_count = frames.count
        height, width = frames.frame_shape
        frame_payload = DataChunkIterator(
            data=frames.iter_frames(),
            maxshape=(frame_count, height, width),
            dtype=np.dtype(np.uint16),
            buffer_size=1,
        )
    else:
        frame_count = len(frames)
        height, width = frames.shape[1:]
        frame_payload = frames
    series = ImageSeries(
        name="camera_frames",
        description=(
            "Original unnormalized monochrome camera frames in [time, y, x] orientation. "
            "Control values encode excitation wavelength."
            + (
                " Frames from both cameras are interleaved in exposure order; "
                "events/camera_frames.camera_index names each frame's camera."
                if device is None
                else ""
            )
        ),
        data=H5DataIO(
            frame_payload,
            chunks=(1, height, width),
            compression="gzip",
            compression_opts=1,
            shuffle=True,
        ),
        unit="counts",
        format="raw",
        timestamps=np.asarray(data.frame_timestamps_s, dtype=np.float64),
        control=controls,
        control_description=_control_descriptions(profile),
        num_samples=np.uint64(frame_count),
        device=device,
    )
    nwbfile.add_acquisition(series)


def _round_trip_verify(
    path: Path,
    config: SessionConfig,
    data: AcquisitionData,
    frames: NDArray[np.uint16] | FrameStream | None,
    wavelength_images: Mapping[Wavelength, NDArray[np.uint16]] | None,
    roi_index: int,
) -> None:
    with NWBHDF5IO(path, mode="r", load_namespaces=True) as io:
        nwbfile = io.read()
        roi = config.enabled_rois[roi_index]
        if nwbfile.session_id != f"{config.session_id}__{roi.fiber_id}":
            raise RuntimeError("NWB round-trip ROI session identity mismatch")
        if nwbfile.subject is None or nwbfile.subject.subject_id != roi.animal_id:
            raise RuntimeError("NWB round-trip subject identity mismatch")
        if NWB_SETTINGS_SCRATCH_NAME not in nwbfile.scratch:
            raise RuntimeError("NWB round-trip settings snapshot is missing")
        stored_settings = json.loads(str(nwbfile.get_scratch(NWB_SETTINGS_SCRATCH_NAME)))
        expected_settings = json.loads(settings_json(config))
        if (
            stored_settings.get("format") != expected_settings["format"]
            or stored_settings.get("settings") != expected_settings["settings"]
        ):
            raise RuntimeError("NWB round-trip settings snapshot mismatch")
        if NWB_RUNTIME_PROVENANCE_SCRATCH_NAME not in nwbfile.scratch:
            raise RuntimeError("NWB round-trip runtime provenance is missing")
        expected_provenance = data.runtime_provenance or capture_runtime_provenance(
            adapter_name="not supplied",
            protocol_version="native_acquisition_v1",
        )
        stored_provenance = json.loads(
            str(nwbfile.get_scratch(NWB_RUNTIME_PROVENANCE_SCRATCH_NAME))
        )
        if stored_provenance != expected_provenance.to_document():
            raise RuntimeError("NWB round-trip runtime provenance mismatch")
        frame_events = nwbfile.events["camera_frames"]
        if len(frame_events) != len(data.frame_ids):
            raise RuntimeError("NWB round-trip camera event count mismatch")
        excitation_events = nwbfile.events["excitation_events"]
        if len(excitation_events) != len(data.frame_ids):
            raise RuntimeError("NWB round-trip excitation event count mismatch")
        frame_table = frame_events.to_dataframe()
        exact_columns = {
            "camera_frame_id": data.frame_ids,
            "camera_index": data.frame_camera_indices,
            "camera_timestamp_ns": data.frame_camera_timestamps_ns,
            "controller_sequence": data.frame_sequences,
            "wavelength_nm": data.frame_wavelengths_nm,
            "wavelength_source": data.frame_wavelength_sources,
            "controller_tick_us": data.frame_ticks_us,
        }
        for column, expected in exact_columns.items():
            if not np.array_equal(frame_table[column].to_numpy(), np.asarray(expected)):
                raise RuntimeError(f"NWB round-trip {column} provenance mismatch")
        if not np.array_equal(
            frame_table["timestamp"].to_numpy(),
            np.asarray(data.frame_timestamps_s),
        ):
            raise RuntimeError("NWB round-trip frame timestamp mismatch")
        intensity = _intensity(config)
        if not np.array_equal(
            frame_table[intensity.column].to_numpy(dtype=np.float64),
            np.asarray(intensity.values(data), dtype=np.float64),
            equal_nan=True,
        ):
            raise RuntimeError("NWB round-trip commanded-intensity provenance mismatch")
        if not np.array_equal(
            frame_table["host_received_s"].to_numpy(),
            np.asarray(data.frame_host_received_s),
        ):
            raise RuntimeError("NWB round-trip host-receipt provenance mismatch")
        expected_saturation = np.asarray(
            [values[roi_index] for values in data.frame_saturation_fractions],
            dtype=np.float32,
        )
        if not np.array_equal(
            frame_table["roi_saturation_fraction"].to_numpy(),
            expected_saturation,
        ):
            raise RuntimeError("NWB round-trip saturation provenance mismatch")
        if "ttl_edges" in nwbfile.events and len(nwbfile.events["ttl_edges"]) != len(
            data.ttl_edges
        ):
            raise RuntimeError("NWB round-trip TTL event count mismatch")
        if data.invalid_times:
            if "invalid_times" not in nwbfile.intervals:
                raise RuntimeError("NWB round-trip invalid-time table is missing")
            if len(nwbfile.intervals["invalid_times"]) != len(data.invalid_times):
                raise RuntimeError("NWB round-trip invalid-time count mismatch")
        expected_system_count = 2 + len(data.system_events)
        if len(nwbfile.events["system_events"]) < expected_system_count:
            raise RuntimeError("NWB round-trip system-event count mismatch")
        if wavelength_images:
            stored_images = nwbfile.processing["photometry"]["wavelength_roi_images"].images
            for wavelength, expected in wavelength_images.items():
                raw_name = f"raw_reference_{int(wavelength)}nm"
                overlay_name = f"roi_overlay_{int(wavelength)}nm"
                if not np.array_equal(stored_images[raw_name].data, expected):
                    raise RuntimeError(f"NWB round-trip reference mismatch for {raw_name}")
                if stored_images[overlay_name].data.shape != (*expected.shape, 3):
                    raise RuntimeError(f"NWB round-trip overlay shape mismatch for {overlay_name}")
        if frames is not None:
            stored = nwbfile.acquisition["camera_frames"].data
            if isinstance(frames, FrameStream):
                expected_shape = (frames.count, *frames.frame_shape)
                first_frame = frames.first_frame
                last_frame = frames.last_frame
            else:
                expected_shape = frames.shape
                first_frame = frames[0]
                last_frame = frames[-1]
            if tuple(stored.shape) != tuple(expected_shape):
                raise RuntimeError("NWB round-trip raw frame shape mismatch")
            if not np.array_equal(stored[0], first_frame) or not np.array_equal(
                stored[-1], last_frame
            ):
                raise RuntimeError("NWB round-trip raw frame values mismatch")
        table = nwbfile.lab_meta_data["FiberPhotometry"].fiber_photometry_table
        expected_rows = len(config.enabled_channels)
        if len(table) != expected_rows:
            raise RuntimeError("NWB round-trip FiberPhotometryTable row count mismatch")
        for channel in config.enabled_channels:
            name = f"raw_fluorescence_{int(channel.wavelength_nm)}"
            expected_samples = len(data.traces[channel.wavelength_nm])
            if expected_samples and nwbfile.acquisition[name].data.shape != (
                expected_samples,
                1,
            ):
                raise RuntimeError(f"NWB round-trip trace shape mismatch for {name}")
            if expected_samples:
                expected_values = np.asarray(
                    [sample.values[roi_index] for sample in data.traces[channel.wavelength_nm]],
                    dtype=np.float32,
                )
                if not np.array_equal(
                    nwbfile.acquisition[name].data[:, 0],
                    expected_values,
                ):
                    raise RuntimeError(f"NWB round-trip trace values mismatch for {name}")
            suffix = "commanded_power" if intensity.unit == "percent" else "commanded_voltage"
            command_name = f"led_{int(channel.wavelength_nm)}_{suffix}"
            frame_indices = [
                index
                for index, wavelength in enumerate(data.frame_wavelengths_nm)
                if wavelength == int(channel.wavelength_nm)
            ]
            if frame_indices:
                stored_commands = nwbfile.stimulus[command_name]
                values = intensity.values(data)
                expected_commands = np.asarray(
                    [values[index] for index in frame_indices], dtype=np.float32
                )
                if not np.array_equal(stored_commands.data[:], expected_commands):
                    raise RuntimeError(f"NWB round-trip intensity mismatch for {command_name}")


def _or_nan(value: float | None) -> float:
    return float("nan") if value is None else float(value)


def _roi_output_paths(config: SessionConfig, roi: ROIConfig) -> tuple[Path, Path]:
    output_directory = config.output_directory.expanduser().resolve()
    stem = "__".join(
        (
            _safe_filename(config.session_id),
            _safe_filename(roi.animal_id),
            _safe_filename(roi.fiber_id),
        )
    )
    return (
        output_directory / f"{stem}.nwb",
        output_directory / f"{stem}.partial.nwb",
    )


def _write_roi_partial_nwb(
    config: SessionConfig,
    data: AcquisitionData,
    roi_index: int,
    *,
    frames: NDArray[np.uint16] | FrameStream | None = None,
    calibration_image: NDArray[np.uint16] | None = None,
    wavelength_images: Mapping[Wavelength, NDArray[np.uint16]] | None = None,
    additional_system_events: tuple[str, ...] = (),
    on_progress: Callable[[FinalizationProgress], None] | None = None,
    completed_roi_files: int = 0,
) -> NWBWriteReport:
    """Write and validate one ROI's partial NWB without promoting it."""

    roi = config.enabled_rois[roi_index]
    output_directory = config.output_directory.expanduser().resolve()
    output_directory.mkdir(parents=True, exist_ok=True)
    final_path, partial_path = _roi_output_paths(config, roi)
    if on_progress is not None:
        on_progress(
            FinalizationProgress(
                stage=FinalizationStage.WRITING,
                completed_roi_files=completed_roi_files,
                total_roi_files=len(config.enabled_rois),
                fiber_id=roi.fiber_id,
            )
        )

    nwbfile = _create_nwbfile(config, roi, data)
    _add_photometry_metadata_and_traces(nwbfile, config, data, roi_index)
    _add_rois_and_calibration(
        nwbfile,
        config,
        calibration_image,
        wavelength_images,
        roi_index,
        calibration_camera=data.frame_camera_indices[0] if data.frame_camera_indices else 1,
    )
    _add_events(nwbfile, config, data, roi_index, additional_system_events)
    _add_raw_frames(nwbfile, config, data, frames)

    with NWBHDF5IO(partial_path, mode="w") as io:
        io.write(nwbfile, cache_spec=True)

    if on_progress is not None:
        on_progress(
            FinalizationProgress(
                stage=FinalizationStage.VALIDATING,
                completed_roi_files=completed_roi_files,
                total_roi_files=len(config.enabled_rois),
                fiber_id=roi.fiber_id,
            )
        )
    return _validate_roi_nwb(
        partial_path,
        final_path,
        config,
        data,
        frames,
        wavelength_images,
        roi_index,
    )


def _validate_roi_nwb(
    candidate_path: Path,
    final_path: Path,
    config: SessionConfig,
    data: AcquisitionData,
    frames: NDArray[np.uint16] | FrameStream | None,
    wavelength_images: Mapping[Wavelength, NDArray[np.uint16]] | None,
    roi_index: int,
) -> NWBWriteReport:
    """Run the complete validation and exact round-trip gate on one candidate."""

    validation_errors = tuple(str(error) for error in validate(path=candidate_path))
    if validation_errors:
        raise RuntimeError("PyNWB validation failed: " + "; ".join(validation_errors))

    inspector_messages = tuple(
        str(message)
        for message in inspect_nwbfile(
            candidate_path,
            skip_validate=True,
            importance_threshold=Importance.CRITICAL,
            ignore=["check_data_orientation"],
        )
        if message is not None
    )
    if inspector_messages:
        raise RuntimeError("NWB Inspector found critical issues: " + "; ".join(inspector_messages))

    _round_trip_verify(candidate_path, config, data, frames, wavelength_images, roi_index)
    trace_sample_count = sum(len(samples) for samples in data.traces.values())
    return NWBWriteReport(
        path=final_path,
        pynwb_validation_errors=validation_errors,
        inspector_messages=inspector_messages,
        frame_count=len(data.frame_ids),
        trace_sample_count=trace_sample_count,
        ttl_edge_count=len(data.ttl_edges),
    )


def write_session_nwbs(
    config: SessionConfig,
    data: AcquisitionData,
    *,
    frames: NDArray[np.uint16] | FrameStream | None = None,
    calibration_image: NDArray[np.uint16] | None = None,
    wavelength_images: Mapping[Wavelength, NDArray[np.uint16]] | None = None,
    additional_system_events: tuple[str, ...] = (),
    on_progress: Callable[[FinalizationProgress], None] | None = None,
) -> tuple[NWBWriteReport, ...]:
    """Write one self-contained, validated NWB file per enabled ROI and animal."""

    _validate_inputs(config, data, frames, calibration_image, wavelength_images)
    output_directory = config.output_directory.expanduser().resolve()
    output_directory.mkdir(parents=True, exist_ok=True)
    paths = [_roi_output_paths(config, roi) for roi in config.enabled_rois]
    collisions = [path for pair in paths for path in pair if path.exists()]
    if collisions:
        rendered = ", ".join(str(path) for path in collisions)
        raise FileExistsError(f"ROI output already exists: {rendered}")

    if on_progress is not None:
        on_progress(
            FinalizationProgress(
                stage=FinalizationStage.PREPARING,
                completed_roi_files=0,
                total_roi_files=len(config.enabled_rois),
            )
        )
    reports = []
    for roi_index in range(len(config.enabled_rois)):
        reports.append(
            _write_roi_partial_nwb(
                config,
                data,
                roi_index,
                frames=frames,
                calibration_image=calibration_image,
                wavelength_images=wavelength_images,
                additional_system_events=additional_system_events,
                on_progress=on_progress,
                completed_roi_files=roi_index,
            )
        )
    if on_progress is not None:
        on_progress(
            FinalizationProgress(
                stage=FinalizationStage.PROMOTING,
                completed_roi_files=0,
                total_roi_files=len(config.enabled_rois),
            )
        )
    for report, (_, partial_path) in zip(reports, paths, strict=True):
        os.replace(partial_path, report.path)
    if on_progress is not None:
        on_progress(
            FinalizationProgress(
                stage=FinalizationStage.COMPLETE,
                completed_roi_files=len(config.enabled_rois),
                total_roi_files=len(config.enabled_rois),
            )
        )
    return tuple(reports)


def recover_or_resume_session_nwbs(
    config: SessionConfig,
    data: AcquisitionData,
    *,
    frames: NDArray[np.uint16] | FrameStream | None = None,
    calibration_image: NDArray[np.uint16] | None = None,
    wavelength_images: Mapping[Wavelength, NDArray[np.uint16]] | None = None,
    additional_system_events: tuple[str, ...] = (),
    on_progress: Callable[[FinalizationProgress], None] | None = None,
) -> tuple[tuple[NWBWriteReport, ...], bool]:
    """Validate/reuse prior candidates and finish an interrupted file-set promotion."""

    _validate_inputs(config, data, frames, calibration_image, wavelength_images)
    output_directory = config.output_directory.expanduser().resolve()
    output_directory.mkdir(parents=True, exist_ok=True)
    paths = [_roi_output_paths(config, roi) for roi in config.enabled_rois]
    if on_progress is not None:
        on_progress(
            FinalizationProgress(
                stage=FinalizationStage.PREPARING,
                completed_roi_files=0,
                total_roi_files=len(paths),
            )
        )
    reports: list[NWBWriteReport] = []
    reused_existing = False
    for roi_index, (final_path, partial_path) in enumerate(paths):
        if final_path.exists() and partial_path.exists():
            raise FileExistsError(
                f"both canonical and partial ROI outputs exist: {final_path}, {partial_path}"
            )
        candidate = final_path if final_path.exists() else partial_path
        if candidate.exists():
            reused_existing = True
            if on_progress is not None:
                on_progress(
                    FinalizationProgress(
                        stage=FinalizationStage.VALIDATING,
                        completed_roi_files=roi_index,
                        total_roi_files=len(paths),
                        fiber_id=config.enabled_rois[roi_index].fiber_id,
                    )
                )
            try:
                report = _validate_roi_nwb(
                    candidate,
                    final_path,
                    config,
                    data,
                    frames,
                    wavelength_images,
                    roi_index,
                )
            except Exception:
                if candidate == final_path:
                    raise
                candidate.unlink()
                report = _write_roi_partial_nwb(
                    config,
                    data,
                    roi_index,
                    frames=frames,
                    calibration_image=calibration_image,
                    wavelength_images=wavelength_images,
                    additional_system_events=additional_system_events,
                    on_progress=on_progress,
                    completed_roi_files=roi_index,
                )
        else:
            report = _write_roi_partial_nwb(
                config,
                data,
                roi_index,
                frames=frames,
                calibration_image=calibration_image,
                wavelength_images=wavelength_images,
                additional_system_events=additional_system_events,
                on_progress=on_progress,
                completed_roi_files=roi_index,
            )
        reports.append(report)
    if on_progress is not None:
        on_progress(
            FinalizationProgress(
                stage=FinalizationStage.PROMOTING,
                completed_roi_files=sum(final.exists() for final, _partial in paths),
                total_roi_files=len(paths),
            )
        )
    for report, (final_path, partial_path) in zip(reports, paths, strict=True):
        if not final_path.exists():
            os.replace(partial_path, report.path)
    if on_progress is not None:
        on_progress(
            FinalizationProgress(
                stage=FinalizationStage.COMPLETE,
                completed_roi_files=len(paths),
                total_roi_files=len(paths),
            )
        )
    return tuple(reports), reused_existing


def write_session_nwb(
    config: SessionConfig,
    data: AcquisitionData,
    *,
    frames: NDArray[np.uint16] | FrameStream | None = None,
    calibration_image: NDArray[np.uint16] | None = None,
    wavelength_images: Mapping[Wavelength, NDArray[np.uint16]] | None = None,
    additional_system_events: tuple[str, ...] = (),
) -> NWBWriteReport:
    """Compatibility helper for callers that configure exactly one ROI."""

    if len(config.enabled_rois) != 1:
        raise ValueError("write_session_nwb requires exactly one ROI; use write_session_nwbs")
    return write_session_nwbs(
        config,
        data,
        frames=frames,
        calibration_image=calibration_image,
        wavelength_images=wavelength_images,
        additional_system_events=additional_system_events,
    )[0]
