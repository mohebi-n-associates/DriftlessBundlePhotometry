"""Trace-first acquisition window styled consistently with DriftlessFLIP."""

from __future__ import annotations

import hashlib
import json
import time
from array import array
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pyqtgraph as pg
import shiboken6
from PySide6.QtCore import QObject, QSignalBlocker, QSize, Qt, QThread, QTimer
from PySide6.QtGui import QCloseEvent, QIcon, QKeySequence, QPixmap, QShortcut
from PySide6.QtWidgets import (
    QAbstractSpinBox,
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSlider,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from driftless_photometry import __version__
from driftless_photometry.acquisition import (
    LIVE_VIEW_LIMIT_S,
    PREVIEW_LIMIT_S,
    AcquisitionProgress,
    AcquisitionRunResult,
    LiveFrame,
    PreviewResult,
)
from driftless_photometry.config import (
    CUSTOM_PROFILE,
    EVENT_MARKER_KEYS,
    RWD_PROFILE,
    CameraConfig,
    ChannelConfig,
    CustomSourceConfig,
    EdgeSelection,
    EventMarkerConfig,
    RecordingControlConfig,
    ROIConfig,
    RWDSourceConfig,
    RWDTTLOutputConfig,
    SessionConfig,
    SystemProfile,
    TraceDisplayConfig,
    TTLEventRuleConfig,
    TTLInputConfig,
    Wavelength,
    demo_config,
    profile_for,
)
from driftless_photometry.diagnostics import FinalizationProgress, FinalizationStage
from driftless_photometry.domain import EventMark
from driftless_photometry.hardware import StartCondition
from driftless_photometry.live_trace import LiveTracePoint, camera_live_trace_points
from driftless_photometry.settings import (
    SETTINGS_SUFFIX,
    configuration_from_nwb,
    default_settings_path,
    export_settings,
    import_settings_with_warnings,
    load_default_settings_with_warnings,
    save_default_settings,
    settings_root,
)

from .branding import logo_path
from .output_program import OutputProgramDialog, describe_program
from .plot_interaction import ScrollableTraceWorkspace, TraceViewBox
from .trace_display import (
    HORIZONS,
    MAX_DISPLAY_POINTS,
    compact_duration,
    downsample_min_max,
    fitted_control_f0,
    trailing_mean,
)
from .warmup import WARMUP_COOLDOWN_S, LEDWarmup, format_minutes
from .worker import (
    AcquisitionWorker,
    BoardTaskWorker,
    DeviceChecker,
    DeviceCheckWorker,
    LiveViewWorker,
    PreviewWorker,
    RigFactory,
    default_rig_factory,
)

_TRACE_REFRESH_INTERVAL_S = 0.1

_FIBER_COLORS = (
    "#00d5e4",
    "#f2b134",
    "#4fd18b",
    "#e46c9f",
    "#7f9cf5",
    "#f58b4c",
    "#66c2ff",
    "#b48cf2",
    "#d5e45c",
)

_WAVELENGTH_COLORS = {
    405: "#66c2ff",
    410: "#66c2ff",
    470: "#4fd18b",
    560: "#e05fa0",
    565: "#e05fa0",
}

_WAVELENGTH_TITLES = {
    Wavelength.CONTROL_405: "405 nm — isosbestic control",
    Wavelength.CONTROL_410: "410 nm — isosbestic control",
    Wavelength.GREEN_470: "470 nm — green signal",
    Wavelength.RED_560: "560 nm — red signal",
    Wavelength.RED_565: "565 nm — red signal",
}

_CUSTOM_TRACE_WAVELENGTHS = (405, 470, 565)
_ALL_TRACE_WAVELENGTHS = (405, 410, 470, 560, 565)

_CUSTOM_MODE_HINT = (
    "Custom mode drives DBF's own camera and timing controller. This build uses the "
    "simulator until those adapters are validated."
)
_RWD_MODE_HINT = (
    "RWD mode drives the RWD controller board and its two photometry cameras directly, "
    "without RWD's software. Wavelengths are derived from camera identity and the LED "
    "cycle after each sequencer start, and every exposure is marked phase-derived."
)
_RWD_SAFETY_WARNING = (
    "The RWD unit has no fail-safe: if DBF stops unexpectedly or its USB cable is "
    "unplugged, the LEDs stay on. DBF switches them off whenever it can; otherwise use "
    "the unit's power switch. Keep the fiber on its target or capped and never look "
    "into it."
)
_RWD_AUTO_PORT = "Auto: the only FTDI port"
_MAX_GAIN_DB = 48.0
_RWD_ROI_DIAMETER_PX = (40.0, 544.0)
"""OFRS's ROI diameter range on the 728 x 544 RWD cameras."""
"""Gain range of the Alvium 1800 U-040m and the Thorlabs CS505MU."""


class SummaryCard(QWidget):
    """Compact acquisition-configuration summary."""

    def __init__(self, value: str, caption: str) -> None:
        super().__init__()
        self.setObjectName("summaryCard")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 8)
        layout.setSpacing(0)
        self.value_label = QLabel(value)
        self.value_label.setObjectName("summaryValue")
        caption_label = QLabel(caption)
        caption_label.setObjectName("summaryCaption")
        layout.addWidget(self.value_label)
        layout.addWidget(caption_label)


class ROIMetadataEditor(QWidget):
    """Editable subject and implant metadata for one camera ROI."""

    def __init__(self, index: int) -> None:
        super().__init__()
        form = QFormLayout(self)
        form.setContentsMargins(6, 8, 6, 8)
        self.enabled_check = QCheckBox("Include in acquisition and NWB output")
        self.enabled_check.setChecked(True)
        self.label_edit = QLineEdit(f"Fiber {index + 1}")
        self.animal_id_edit = QLineEdit(f"animal-{index + 1:02d}")
        self.brain_region_edit = QLineEdit("not specified")
        self.sensor_type_edit = QLineEdit("not specified")
        self.age_edit = QLineEdit("P90D")
        self.age_edit.setPlaceholderText("ISO 8601, e.g. P90D")
        self.sex_combo = QComboBox()
        self.sex_combo.addItems(["U", "F", "M", "O"])
        form.addRow("Enabled", self.enabled_check)
        form.addRow("ROI label", self.label_edit)
        form.addRow("Animal ID", self.animal_id_edit)
        form.addRow("Brain region", self.brain_region_edit)
        form.addRow("Sensor type", self.sensor_type_edit)
        form.addRow("Animal age", self.age_edit)
        form.addRow("Animal sex", self.sex_combo)
        self.diameter_spins: dict[int, QDoubleSpinBox] = {}
        for camera, label in ((1, "Diameter"), (2, "Diameter, camera 2")):
            spin = QDoubleSpinBox()
            spin.setDecimals(1)
            spin.setSuffix(" px")
            spin.setToolTip("Circle diameter; type a value or drag the circle's handle")
            form.addRow(label, spin)
            self.diameter_spins[camera] = spin
        self._form = form

    def set_camera2_visible(self, visible: bool) -> None:
        self._form.setRowVisible(self.diameter_spins[2], visible)
        self._form.labelForField(self.diameter_spins[1]).setText(
            "Diameter, camera 1" if visible else "Diameter"
        )

    def values(self) -> dict[str, str | bool]:
        return {
            "enabled": self.enabled_check.isChecked(),
            "label": self.label_edit.text(),
            "animal_id": self.animal_id_edit.text(),
            "brain_region": self.brain_region_edit.text(),
            "sensor_type": self.sensor_type_edit.text(),
            "subject_age": self.age_edit.text(),
            "subject_sex": self.sex_combo.currentText(),
        }

    def restore(self, values: Mapping[str, str | bool]) -> None:
        self.enabled_check.setChecked(bool(values["enabled"]))
        self.label_edit.setText(str(values["label"]))
        self.animal_id_edit.setText(str(values["animal_id"]))
        self.brain_region_edit.setText(str(values["brain_region"]))
        self.sensor_type_edit.setText(str(values["sensor_type"]))
        self.age_edit.setText(str(values["subject_age"]))
        self.sex_combo.setCurrentText(str(values["subject_sex"]))


class MainWindow(QMainWindow):
    """Simulator-backed GUI; physical adapters plug into the same acquisition engine."""

    def __init__(
        self,
        *,
        output_directory: Path,
        default_duration_s: float = 5.0,
        default_fibers: int = 3,
        default_raw: bool = False,
        rig_factory: RigFactory = default_rig_factory,
        device_checker: DeviceChecker | None = None,
        auto_check_devices: bool = False,
        clock: Callable[[], float] = time.monotonic,
        output_tester: Callable[[SessionConfig, int], str] | None = None,
        power_meter_factory: Callable[[], object] | None = None,
        power_measurer: Callable[[SessionConfig, object], tuple] | None = None,
        default_system: str = "custom",
    ) -> None:
        super().__init__()
        self._rig_factory = rig_factory
        if output_tester is None:
            from driftless_photometry.hardware.rwd_devices import drive_rwd_output_test

            output_tester = drive_rwd_output_test
        self._output_tester = output_tester
        if power_meter_factory is None:
            from driftless_photometry.hardware.power_meter import ThorlabsPowerMeter

            power_meter_factory = ThorlabsPowerMeter
        if power_measurer is None:
            from driftless_photometry.hardware.rwd_devices import measure_rwd_led_power

            power_measurer = measure_rwd_led_power
        self._power_meter_factory = power_meter_factory
        self._power_measurer = power_measurer
        self.last_power_readings: tuple = ()
        self.last_output_test: str | None = None
        self._rwd_output_programs = {
            port: RWDTTLOutputConfig(port=port, frequency_hz=10.0, pulse_width_ms=10)
            for port in range(1, 5)
        }
        self._warmup = LEDWarmup(clock=clock)
        self._warmup_announced = False
        self._operation_uses_leds = False
        if device_checker is None:
            from driftless_photometry.hardware.rwd_devices import check_rwd_devices

            device_checker = check_rwd_devices
        self._device_checker = device_checker
        # Only the real application checks automatically: a check talks to the board.
        self._auto_check_devices = auto_check_devices
        self._device_thread: QThread | None = None
        self._device_worker: DeviceCheckWorker | None = None
        self.last_device_report = None
        self._devices_checked_for_mode = False
        pg.setConfigOptions(
            imageAxisOrder="row-major",
            antialias=True,
            background="#081a2b",
            foreground="#9fc4dc",
        )
        self.setWindowTitle(f"Driftless Bundle Photometry {__version__}")
        with logo_path() as icon_path:
            self._logo_pixmap = QPixmap(str(icon_path))
            self.setWindowIcon(QIcon(self._logo_pixmap))
        self.resize(2300, 1300)
        self.setMinimumSize(1408, 792)
        self._thread: QThread | None = None
        self._worker: AcquisitionWorker | PreviewWorker | None = None
        self._retired_workers: list[QObject] = []
        self._operation_kind: str | None = None
        self._close_pending = False
        self._preview_signature: str | None = None
        self._pending_preview_signature: str | None = None
        self._progress_duration_s = default_duration_s
        self._roi_items: list[pg.CircleROI] = []
        self._roi_items_cam2: list[pg.CircleROI] = []
        self._restart_live_view = False
        self._camera_controls_ready = False
        self._camera_frame_counts: dict[int, int] = {}
        self._last_camera_images: dict[int, np.ndarray] = {}
        self._curves: dict[tuple[int, int], pg.PlotDataItem] = {}
        self._trace_times: dict[tuple[int, int], array] = {}
        self._trace_values: dict[tuple[int, int], array] = {}
        self._trace_roi_indices: dict[str, int] = {}
        self._active_trace_wavelengths: tuple[int, ...] = _CUSTOM_TRACE_WAVELENGTHS
        self._active_config: SessionConfig | None = None
        self._trace_roi_count = 0
        self._trace_horizon_s: float | None = HORIZONS[0][1]
        self._trace_manual_x_range: tuple[float, float] | None = None
        self._trace_first_s: float | None = None
        self._trace_latest_s: float | None = None
        self._last_trace_render_s: dict[int, float] = {}
        self._image_item = pg.ImageItem()
        self._image_item_cam2 = pg.ImageItem()
        self._calibration_images = {1: self._image_item, 2: self._image_item_cam2}
        self._image_display_maximum = (1 << 12) - 1
        self.last_result: AcquisitionRunResult | None = None
        self.last_preview: PreviewResult | None = None
        self.last_error: str | None = None
        self._session_template = demo_config(
            output_directory,
            fiber_count=default_fibers,
            raw_capture=default_raw,
            recording_duration_s=default_duration_s,
        )
        self._custom_camera = self._session_template.camera
        self._default_system = default_system
        self._preview_started_config: SessionConfig | None = None

        root = QWidget()
        root_layout = QVBoxLayout(root)
        root_layout.setContentsMargins(18, 14, 18, 14)
        root_layout.setSpacing(12)
        root_layout.addWidget(self._build_header())
        self.tabs = QTabWidget()
        self.tabs.addTab(
            self._build_acquire_tab(
                output_directory,
                default_duration_s,
                default_raw,
                default_fibers,
            ),
            "Acquire",
        )
        self.tabs.addTab(
            self._scrollable(self._build_calibration_tab(default_fibers)),
            "Camera & fiber ROIs",
        )
        self.tabs.addTab(self._build_wavelength_images_tab(), "Live wavelength images")
        root_layout.addWidget(self.tabs)
        self.setCentralWidget(root)
        self._apply_theme()
        self._remove_spin_box_buttons()
        self.statusBar().showMessage(
            "Ready — simulator mode; no physical camera or controller connected"
        )
        self._configure_camera_controls(CUSTOM_PROFILE)
        self._load_camera_controls(self._session_template.camera)
        self._camera_controls_ready = True
        self._rebuild_roi_items()
        self._load_default_settings_at_startup()
        self._apply_source_mode()

    def _remove_spin_box_buttons(self) -> None:
        for spin_box in self.findChildren(QAbstractSpinBox):
            spin_box.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.NoButtons)

    def _build_header(self) -> QWidget:
        header_widget = QWidget()
        header_widget.setObjectName("appHeader")
        header = QHBoxLayout(header_widget)
        header.setContentsMargins(14, 10, 14, 10)
        header.setSpacing(11)
        mark = QLabel()
        mark.setObjectName("brandMark")
        mark.setFixedSize(52, 52)
        mark.setAlignment(Qt.AlignmentFlag.AlignCenter)
        mark.setPixmap(
            self._logo_pixmap.scaled(
                QSize(48, 48),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        )
        identity = QWidget()
        identity_layout = QVBoxLayout(identity)
        identity_layout.setContentsMargins(0, 0, 0, 0)
        identity_layout.setSpacing(1)
        wordmark = QLabel("DRIFTLESS BUNDLE PHOTOMETRY")
        wordmark.setObjectName("wordmark")
        strapline = QLabel("Trace-first acquisition  /  explicit timing  /  validated NWB")
        strapline.setObjectName("strapline")
        identity_layout.addWidget(wordmark)
        identity_layout.addWidget(strapline)
        header.addWidget(mark)
        header.addWidget(identity)
        header.addStretch(1)
        self.version_badge = QLabel(f"v{__version__}")
        self.version_badge.setObjectName("versionBadge")
        self.version_badge.setToolTip("Application version")
        header.addWidget(self.version_badge)
        self.system_status = QLabel("READY")
        self.system_status.setObjectName("systemStatus")
        self.system_status.setProperty("state", "ready")
        header.addWidget(self.system_status)
        self.backend_badge = QLabel("Simulator")
        self.backend_badge.setObjectName("badge")
        self.backend_badge.setProperty("hardware", False)
        header.addWidget(self.backend_badge)
        return header_widget

    @staticmethod
    def _scrollable(widget: QWidget) -> QScrollArea:
        area = QScrollArea()
        area.setWidgetResizable(True)
        area.setFrameShape(QScrollArea.Shape.NoFrame)
        area.setWidget(widget)
        return area

    def _build_acquire_tab(
        self,
        output_directory: Path,
        duration_s: float,
        raw_capture: bool,
        fiber_count: int,
    ) -> QWidget:
        page = QWidget()
        layout = QHBoxLayout(page)
        layout.setContentsMargins(12, 14, 12, 12)
        layout.setSpacing(14)

        controls = QWidget()
        controls.setMinimumWidth(350)
        controls.setMaximumWidth(440)
        controls_layout = QVBoxLayout(controls)
        controls_layout.setContentsMargins(0, 0, 0, 0)
        controls_layout.setSpacing(12)

        setup_eyebrow = QLabel("SESSION SETUP")
        setup_eyebrow.setObjectName("eyebrow")
        setup_title = QLabel("Configure a recording")
        setup_title.setObjectName("sectionTitle")
        setup_copy = QLabel(
            "Define shared recording settings here; assign each ROI to its animal "
            "under Camera & fiber ROIs."
        )
        setup_copy.setObjectName("hint")
        setup_copy.setWordWrap(True)
        controls_layout.addWidget(setup_eyebrow)
        controls_layout.addWidget(setup_title)
        controls_layout.addWidget(setup_copy)

        source_group = QGroupBox("Acquisition system — choose first")
        source_form = QFormLayout(source_group)
        self.source_combo = QComboBox()
        self.source_combo.setObjectName("sourceSelector")
        self.source_combo.addItem("Custom", "custom")
        self.source_combo.addItem("RWD", "rwd")
        self.source_combo.currentIndexChanged.connect(self._apply_source_mode)
        self.source_mode_hint = QLabel(_CUSTOM_MODE_HINT)
        self.source_mode_hint.setObjectName("hint")
        self.source_mode_hint.setWordWrap(True)
        source_form.addRow("System", self.source_combo)
        source_form.addRow(self.source_mode_hint)
        controls_layout.addWidget(source_group)

        settings_group = QGroupBox("Configuration")
        settings_layout = QGridLayout(settings_group)
        self.save_settings_button = QPushButton("Save JSON…")
        self.save_settings_button.setToolTip(
            "Save all metadata, acquisition settings, display preferences, and ROIs"
        )
        self.save_settings_button.clicked.connect(self._save_settings_json)
        self.load_settings_button = QPushButton("Load JSON…")
        self.load_settings_button.clicked.connect(self._load_settings_json)
        self.load_nwb_settings_button = QPushButton("Load NWB…")
        self.load_nwb_settings_button.setToolTip(
            "Restore the settings embedded in a previous DBF recording"
        )
        self.load_nwb_settings_button.clicked.connect(self._load_settings_nwb)
        self.default_settings_button = QPushButton("Set as default")
        self.default_settings_button.setToolTip(
            "Save the current setup in Documents and load it whenever DBF starts"
        )
        self.default_settings_button.clicked.connect(self._save_as_default_settings)
        settings_layout.addWidget(self.save_settings_button, 0, 0)
        settings_layout.addWidget(self.load_settings_button, 0, 1)
        settings_layout.addWidget(self.load_nwb_settings_button, 1, 0)
        settings_layout.addWidget(self.default_settings_button, 1, 1)
        self.settings_path_hint = QLabel(f"Defaults: {default_settings_path()}")
        self.settings_path_hint.setObjectName("hint")
        self.settings_path_hint.setWordWrap(True)
        settings_layout.addWidget(self.settings_path_hint, 2, 0, 1, 2)
        controls_layout.addWidget(settings_group)

        session_group = QGroupBox("Recording")
        session_form = QFormLayout(session_group)
        self.experimenter_edit = QLineEdit("Simulator")
        self.session_description_edit = QLineEdit(
            "Synthetic multichannel fiber-photometry demonstration"
        )
        self.lab_edit = QLineEdit("Driftless Bundle Photometry")
        self.institution_edit = QLineEdit()
        self.output_edit = QLineEdit(str(output_directory))
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse_output)
        output_row = QWidget()
        output_layout = QHBoxLayout(output_row)
        output_layout.setContentsMargins(0, 0, 0, 0)
        output_layout.addWidget(self.output_edit, stretch=1)
        output_layout.addWidget(browse)
        self.duration_spin = QDoubleSpinBox()
        self.duration_spin.setRange(0.1, 24 * 60 * 60)
        self.duration_spin.setDecimals(1)
        self.duration_spin.setSuffix(" s")
        self.duration_spin.setValue(duration_s)
        session_form.addRow("Experimenter", self.experimenter_edit)
        session_form.addRow("Description", self.session_description_edit)
        session_form.addRow("Lab", self.lab_edit)
        session_form.addRow("Institution", self.institution_edit)
        session_form.addRow("Output root", output_row)
        session_form.addRow("Start", self._build_start_row())
        session_form.addRow("Stop", self._build_stop_row())
        session_form.addRow("Excitation", self._build_excitation_row())
        session_form.addRow("Duration", self.duration_spin)
        self._duration_label = session_form.labelForField(self.duration_spin)
        controls_layout.addWidget(session_group)
        self._apply_recording_modes()
        self._channel_controls: dict[
            str, tuple[dict[Wavelength, QCheckBox], dict[Wavelength, QSlider], QGroupBox]
        ] = {}
        self._channel_readouts: dict[str, dict[Wavelength, QLabel]] = {}
        for profile in (CUSTOM_PROFILE, RWD_PROFILE):
            controls_layout.addWidget(self._build_channel_group(profile))
        self.events_group = self._build_events_group()
        controls_layout.addWidget(self.events_group)

        retention_group = QGroupBox("Data retention")
        retention_layout = QVBoxLayout(retention_group)
        self.raw_checkbox = QCheckBox("Embed lossless raw camera frames")
        self.raw_checkbox.setChecked(raw_capture)
        retention_layout.addWidget(self.raw_checkbox)
        raw_hint = QLabel(
            "Each enabled ROI produces a separate animal NWB file. Exposure identity, TTL "
            "edges, per-wavelength camera references, and ROI-annotated views are "
            "copied into every file. Full raw-frame retention is optional and "
            "duplicates frames across those files."
        )
        raw_hint.setObjectName("hint")
        raw_hint.setWordWrap(True)
        retention_layout.addWidget(raw_hint)
        controls_layout.addWidget(retention_group)

        self.rwd_group = self._build_rwd_group()
        self.rwd_group.setVisible(False)
        controls_layout.addWidget(self.rwd_group)
        controls_layout.addStretch(1)
        # Settings scroll on their own; the live display keeps the window's height.
        self.controls_scroll = self._scrollable(controls)
        self.controls_scroll.setMinimumWidth(370)
        self.controls_scroll.setMaximumWidth(470)
        self.controls_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        layout.addWidget(self.controls_scroll)

        live_group = QGroupBox("Live acquisition")
        live_layout = QVBoxLayout(live_group)
        live_layout.setSpacing(10)
        overview = QHBoxLayout()
        self.channels_summary = SummaryCard("3", "ACTIVE CHANNELS")
        self.fibers_summary = SummaryCard(str(fiber_count), "FIBER ROIS")
        self.format_summary = SummaryCard(str(fiber_count), "NWB FILES")
        overview.addWidget(self.channels_summary)
        overview.addWidget(self.fibers_summary)
        overview.addWidget(self.format_summary)
        live_layout.addLayout(overview)
        horizon_row = QHBoxLayout()
        horizon_row.addWidget(QLabel("Display"))
        self.horizon_group = QButtonGroup(self)
        self.horizon_group.setExclusive(True)
        self.horizon_buttons: dict[float | None, QPushButton] = {}
        for index, (label, seconds) in enumerate(HORIZONS):
            button = QPushButton(label)
            button.setObjectName("horizon")
            button.setCheckable(True)
            button.setChecked(seconds == self._trace_horizon_s)
            self.horizon_group.addButton(button, index)
            self.horizon_buttons[seconds] = button
            horizon_row.addWidget(button)
        self.horizon_group.idClicked.connect(self._set_trace_horizon_from_button_id)
        self.trace_span_hint = QLabel("No trace data yet")
        self.trace_span_hint.setObjectName("hint")
        horizon_row.addSpacing(8)
        horizon_row.addWidget(self.trace_span_hint)
        self.trace_interaction_hint = QLabel(
            "Drag horizontally or vertically to zoom; double-click to reset"
        )
        self.trace_interaction_hint.setObjectName("hint")
        horizon_row.addSpacing(8)
        horizon_row.addWidget(self.trace_interaction_hint)
        horizon_row.addStretch(1)
        live_layout.addLayout(horizon_row)
        trace_options = QHBoxLayout()
        trace_options.addWidget(QLabel("Show"))
        self.trace_wavelength_checks: dict[int, QCheckBox] = {}
        for wavelength in _ALL_TRACE_WAVELENGTHS:
            check = QCheckBox(f"{wavelength} nm")
            check.setChecked(True)
            check.setStyleSheet(f"color: {_WAVELENGTH_COLORS[wavelength]}; font-weight: 700;")
            check.toggled.connect(self._apply_trace_visibility)
            self.trace_wavelength_checks[wavelength] = check
            trace_options.addWidget(check)
        trace_options.addSpacing(12)
        trace_options.addWidget(QLabel("Smooth"))
        self.trace_smoothing_spin = QDoubleSpinBox()
        self.trace_smoothing_spin.setRange(0.0, 60.0)
        self.trace_smoothing_spin.setDecimals(2)
        self.trace_smoothing_spin.setSingleStep(0.1)
        self.trace_smoothing_spin.setSuffix(" s")
        self.trace_smoothing_spin.setSpecialValueText("Off")
        self.trace_smoothing_spin.setValue(0.0)
        self.trace_smoothing_spin.setToolTip(
            "Display-only trailing mean in elapsed seconds. Raw acquired and stored "
            "values are never changed."
        )
        self.trace_smoothing_spin.valueChanged.connect(self._refresh_all_trace_curves)
        trace_options.addWidget(self.trace_smoothing_spin)
        trace_options.addSpacing(12)
        trace_options.addWidget(QLabel("Y axis"))
        self.trace_mode_combo = QComboBox()
        self.trace_mode_combo.addItem("Absolute", "absolute")
        self.trace_mode_combo.addItem("dF/F", "dff")
        self.trace_mode_combo.addItem("dF/F, control fit", "dff_fit")
        self.trace_mode_combo.setItemData(
            2,
            "F0 is the 410 nm (RWD) or 405 nm (Custom) control trace, fitted to each "
            "signal by least squares over the whole recording so far. The control "
            "itself uses the median baseline. Display only.",
            Qt.ItemDataRole.ToolTipRole,
        )
        self.trace_mode_combo.currentIndexChanged.connect(self._apply_trace_mode)
        trace_options.addWidget(self.trace_mode_combo)
        trace_options.addWidget(QLabel("Baseline"))
        self.trace_baseline_spin = QDoubleSpinBox()
        self.trace_baseline_spin.setRange(0.1, 3600.0)
        self.trace_baseline_spin.setDecimals(1)
        self.trace_baseline_spin.setSingleStep(1.0)
        self.trace_baseline_spin.setSuffix(" s")
        self.trace_baseline_spin.setValue(5.0)
        self.trace_baseline_spin.setToolTip(
            "dF/F uses the median of the first N seconds separately for every "
            "ROI and wavelength. This changes display only."
        )
        self.trace_baseline_spin.valueChanged.connect(self._apply_trace_mode)
        trace_options.addWidget(self.trace_baseline_spin)
        self.trace_baseline_hint = QLabel("median F0 per ROI and wavelength")
        self.trace_baseline_hint.setObjectName("hint")
        self.trace_baseline_spin.setEnabled(False)
        self.trace_baseline_hint.setEnabled(False)
        trace_options.addWidget(self.trace_baseline_hint)
        trace_options.addStretch(1)
        live_layout.addLayout(trace_options)
        status_row = QHBoxLayout()
        self.live_status = QLabel("Ready — configure the session, then start recording")
        self.live_status.setObjectName("liveStatus")
        self.frame_counter = QLabel("0 frames")
        self.frame_counter.setObjectName("hint")
        status_row.addWidget(self.live_status)
        status_row.addStretch(1)
        status_row.addWidget(self.frame_counter)
        live_layout.addLayout(status_row)
        self.connection_diagnostics_label = QLabel("Queue and connection diagnostics appear here")
        self.connection_diagnostics_label.setObjectName("hint")
        live_layout.addWidget(self.connection_diagnostics_label)
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 1000)
        self.progress_bar.setValue(0)
        self.progress_bar.setFormat("0%")
        live_layout.addWidget(self.progress_bar)
        self.trace_scroll = self._scrollable(self._build_trace_workspace())
        self.trace_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        live_layout.addWidget(self.trace_scroll, stretch=1)

        self.preview_status = QLabel(
            "Preview required - validate the current acquisition settings before recording"
        )
        self.preview_status.setObjectName("previewStatus")
        self.preview_status.setProperty("state", "required")
        self.preview_status.setWordWrap(True)
        live_layout.addWidget(self.preview_status)

        self.marker_row = QHBoxLayout()
        self.marker_row_label = QLabel("Mark")
        self.marker_row_label.setObjectName("hint")
        self.marker_row.addWidget(self.marker_row_label)
        self.marker_buttons: dict[str, QPushButton] = {}
        self.marker_row.addStretch(1)
        live_layout.addLayout(self.marker_row)
        self._marker_shortcuts: list[QShortcut] = []
        for key in EVENT_MARKER_KEYS:
            shortcut = QShortcut(QKeySequence(key), self)
            shortcut.activated.connect(lambda key=key: self.mark_event(key, "key"))
            self._marker_shortcuts.append(shortcut)
        self._event_marks_shown: list[EventMark] = []

        action_row = QHBoxLayout()
        self.preview_button = QPushButton("Run preview")
        self.preview_button.setObjectName("preview")
        self.preview_button.setMinimumHeight(38)
        self.preview_button.clicked.connect(self._toggle_preview)
        self.start_button = QPushButton("Start recording")
        self.start_button.setObjectName("record")
        self.start_button.setMinimumHeight(38)
        self.start_button.setEnabled(False)
        self.start_button.clicked.connect(self.start_recording)
        self.stop_button = QPushButton("Stop")
        self.stop_button.setObjectName("attention")
        self.stop_button.setMinimumHeight(38)
        self.stop_button.setEnabled(False)
        self.stop_button.clicked.connect(self.stop_recording)
        action_row.addWidget(self.preview_button, stretch=1)
        action_row.addWidget(self.start_button, stretch=1)
        action_row.addWidget(self.stop_button, stretch=1)
        live_layout.addLayout(action_row)
        layout.addWidget(live_group, stretch=1)

        self._settings_widgets = [
            source_group,
            settings_group,
            session_group,
            self.events_group,
            *(group for _checks, _sliders, group in self._channel_controls.values()),
            retention_group,
            self.rwd_group,
            browse,
        ]
        self._power_timer = QTimer(self)
        self._power_timer.setSingleShot(True)
        self._power_timer.setInterval(250)
        self._power_timer.timeout.connect(self._send_led_powers)
        for checks, sliders, _group in self._channel_controls.values():
            for check in checks.values():
                check.toggled.connect(self._update_overview)
                check.toggled.connect(self._invalidate_preview)
            for slider in sliders.values():
                slider.valueChanged.connect(self._invalidate_preview)
                slider.valueChanged.connect(self._on_power_slider_changed)
        for edit in (
            self.experimenter_edit,
            self.session_description_edit,
            self.lab_edit,
            self.institution_edit,
            self.output_edit,
        ):
            edit.textChanged.connect(self._invalidate_preview)
        self.duration_spin.valueChanged.connect(self._invalidate_preview)
        self.raw_checkbox.toggled.connect(self._invalidate_preview)
        return page

    def _build_rwd_group(self) -> QGroupBox:
        group = QGroupBox("RWD hardware")
        layout = QVBoxLayout(group)
        self.rwd_safety_label = QLabel(_RWD_SAFETY_WARNING)
        self.rwd_safety_label.setObjectName("safetyWarning")
        self.rwd_safety_label.setWordWrap(True)
        layout.addWidget(self.rwd_safety_label)
        devices_title = QLabel("DEVICES")
        devices_title.setObjectName("eyebrow")
        layout.addWidget(devices_title)
        self.device_labels: dict[str, QLabel] = {}
        for key, name in (
            ("controller", "Controller"),
            ("camera1", "Camera 1 (410/470 nm)"),
            ("camera2", "Camera 2 (560 nm)"),
        ):
            label = QLabel(f"• {name}: not checked yet")
            label.setObjectName("deviceStatus")
            label.setProperty("state", "unknown")
            label.setWordWrap(True)
            layout.addWidget(label)
            self.device_labels[key] = label
        self.check_devices_button = QPushButton("Check devices")
        self.check_devices_button.setToolTip(
            "Connect to the controller and list the cameras; no LED is switched on"
        )
        self.check_devices_button.clicked.connect(self.start_device_check)
        layout.addWidget(self.check_devices_button)
        power_title = QLabel("LED POWER")
        power_title.setObjectName("eyebrow")
        layout.addWidget(power_title)
        self.measure_power_button = QPushButton("Measure power")
        self.measure_power_button.setToolTip(
            "Light each enabled LED alone at its set power and read a Thorlabs PM100 or "
            "PM400 meter; point the fiber at the meter's sensor first"
        )
        self.measure_power_button.clicked.connect(self.start_power_measurement)
        layout.addWidget(self.measure_power_button)
        self.power_label = QLabel("Not measured yet")
        self.power_label.setObjectName("hint")
        self.power_label.setWordWrap(True)
        layout.addWidget(self.power_label)
        warmup_title = QLabel("LED WARM-UP")
        warmup_title.setObjectName("eyebrow")
        layout.addWidget(warmup_title)
        self.warmup_label = QLabel()
        self.warmup_label.setObjectName("deviceStatus")
        self.warmup_label.setWordWrap(True)
        layout.addWidget(self.warmup_label)
        self.warmup_check = QCheckBox(
            f"Remind me before recording with less than "
            f"{format_minutes(self._warmup.required_s)} of warm-up"
        )
        self.warmup_check.setChecked(True)
        self.warmup_check.setToolTip(
            "RWD recommends running the LEDs for 5 minutes before recording so their "
            "output settles; Live view counts as warm-up"
        )
        layout.addWidget(self.warmup_check)
        self._warmup_timer = QTimer(self)
        self._warmup_timer.setInterval(1000)
        self._warmup_timer.timeout.connect(self._update_warmup_label)
        self._warmup_timer.start()
        self._update_warmup_label()
        form = QFormLayout()
        port_row = QWidget()
        port_layout = QHBoxLayout(port_row)
        port_layout.setContentsMargins(0, 0, 0, 0)
        self.rwd_port_combo = QComboBox()
        self.rwd_port_combo.setEditable(True)
        self.rwd_port_combo.setToolTip(
            "The COM port Windows created for the controller's FTDI USB bridge"
        )
        self.rwd_port_refresh = QPushButton("Refresh")
        self.rwd_port_refresh.clicked.connect(self._refresh_rwd_ports)
        port_layout.addWidget(self.rwd_port_combo, stretch=1)
        port_layout.addWidget(self.rwd_port_refresh)
        form.addRow("Controller port", port_row)
        layout.addLayout(form)
        outputs_title = QLabel("TTL OUTPUT PULSE TRAINS")
        outputs_title.setObjectName("eyebrow")
        layout.addWidget(outputs_title)
        outputs = QGridLayout()
        outputs.setColumnStretch(1, 1)
        self.rwd_output_checks: dict[int, QCheckBox] = {}
        self.rwd_output_summaries: dict[int, QLabel] = {}
        self.rwd_output_test_buttons: dict[int, QPushButton] = {}
        for row, port in enumerate(range(1, 5)):
            check = QCheckBox(f"Output {port}")
            summary = QLabel()
            summary.setObjectName("hint")
            summary.setWordWrap(True)
            edit_button = QPushButton("Edit…")
            edit_button.setToolTip("Set the pulse train, its trigger, timing, steps and repeats")
            edit_button.clicked.connect(lambda _checked=False, p=port: self.edit_output_program(p))
            test_button = QPushButton("Test")
            test_button.setToolTip(
                "Drive this output's pulse train for 1 s with the LEDs off; anything "
                "connected to it, such as a laser, is driven too"
            )
            test_button.clicked.connect(lambda _checked=False, p=port: self.start_output_test(p))
            for column, widget in enumerate((check, summary, edit_button, test_button)):
                outputs.addWidget(widget, row, column)
            check.toggled.connect(self._invalidate_preview)
            check.toggled.connect(self._rebuild_marker_buttons)
            self.rwd_output_checks[port] = check
            self.rwd_output_summaries[port] = summary
            self.rwd_output_test_buttons[port] = test_button
        layout.addLayout(outputs)
        self._refresh_output_summaries()
        self.rwd_status_label = QLabel(
            "Preview connects to the controller and cameras, switches the LEDs on for a "
            "few frames, and switches everything off again. Run it with the fiber on its "
            "target."
        )
        self.rwd_status_label.setObjectName("hint")
        self.rwd_status_label.setWordWrap(True)
        layout.addWidget(self.rwd_status_label)
        self._refresh_rwd_ports()
        self.rwd_port_combo.currentTextChanged.connect(self._invalidate_preview)
        return group

    def start_device_check(self) -> None:
        if self._device_thread is not None or self._thread is not None:
            return
        try:
            config = self._build_config()
        except Exception as error:
            QMessageBox.critical(self, "Invalid configuration", str(error))
            return
        if not isinstance(config.source, RWDSourceConfig):
            return
        for label in self.device_labels.values():
            label.setText(label.text().split(":")[0] + ": checking…")
            self._set_device_state(label, "unknown")
        thread = QThread(self)
        worker = DeviceCheckWorker(config, self._device_checker)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.finished.connect(self._on_devices_checked)
        worker.failed.connect(self._on_device_check_failed)
        worker.finished.connect(thread.quit)
        worker.failed.connect(thread.quit)
        thread.finished.connect(self._on_device_thread_finished)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        self._device_thread = thread
        self._device_worker = worker
        self._set_busy_controls(False)
        self.statusBar().showMessage("Checking the RWD controller and cameras…")
        thread.start()

    def _on_devices_checked(self, report) -> None:
        self.last_device_report = report
        keys = ("controller", "camera1", "camera2")
        for key, device in zip(keys, report.devices, strict=True):
            mark = "✓" if device.ok else ("✗" if device.needed else "·")
            self.device_labels[key].setText(f"{mark} {device.name}: {device.detail}")
            self._set_device_state(
                self.device_labels[key],
                "ok" if device.ok else ("failed" if device.needed else "unknown"),
            )
        self.statusBar().showMessage(
            "RWD controller and cameras found"
            if report.ok
            else "RWD device check found a problem; see the RWD hardware panel"
        )

    def _on_device_check_failed(self, traceback_text: str) -> None:
        for label in self.device_labels.values():
            label.setText(label.text().split(":")[0] + ": check failed")
            self._set_device_state(label, "failed")
        if not self._close_pending:
            QMessageBox.critical(self, "Device check failed", traceback_text)

    def _on_device_thread_finished(self) -> None:
        self._device_thread = None
        self._retire_worker(self._device_worker)
        self._device_worker = None
        self._set_busy_controls(self._thread is not None)

    @staticmethod
    def _set_device_state(label: QLabel, state: str) -> None:
        label.setProperty("state", state)
        label.style().unpolish(label)
        label.style().polish(label)

    def _refresh_rwd_ports(self) -> None:
        current = self._rwd_port()
        try:
            from driftless_photometry.hardware import candidate_rwd_ports

            ports = candidate_rwd_ports()
        except Exception:
            ports = ()
        blocker = QSignalBlocker(self.rwd_port_combo)
        self.rwd_port_combo.clear()
        self.rwd_port_combo.addItem(_RWD_AUTO_PORT)
        for port in ports:
            self.rwd_port_combo.addItem(port)
        self._set_rwd_port(current)
        del blocker

    def _rwd_port(self) -> str | None:
        if not hasattr(self, "rwd_port_combo"):
            return None
        text = self.rwd_port_combo.currentText().strip()
        return None if not text or text == _RWD_AUTO_PORT else text

    def _set_rwd_port(self, port: str | None) -> None:
        if port is None:
            self.rwd_port_combo.setCurrentIndex(0)
            return
        index = self.rwd_port_combo.findText(port)
        if index < 0:
            self.rwd_port_combo.addItem(port)
            index = self.rwd_port_combo.count() - 1
        self.rwd_port_combo.setCurrentIndex(index)

    def _rwd_source_from_controls(self) -> RWDSourceConfig:
        outputs = tuple(
            self._rwd_output_programs[port]
            for port, check in self.rwd_output_checks.items()
            if check.isChecked()
        )
        return RWDSourceConfig(controller_port=self._rwd_port(), ttl_outputs=outputs)

    def _apply_rwd_source(self, source: RWDSourceConfig) -> None:
        self._set_rwd_port(source.controller_port)
        configured = {output.port: output for output in source.ttl_outputs}
        for port, check in self.rwd_output_checks.items():
            output = configured.get(port)
            check.setChecked(output is not None)
            if output is not None:
                self._rwd_output_programs[port] = output
        self._refresh_output_summaries()
        self._rebuild_marker_buttons()

    def _refresh_output_summaries(self) -> None:
        for port, summary in self.rwd_output_summaries.items():
            output = self._rwd_output_programs[port]
            name = f"{output.label}: " if output.label else ""
            summary.setText(name + describe_program(output))

    def edit_output_program(self, port: int) -> None:
        rois = self._roi_configs_from_controls() if self._roi_items else ()
        fibers = [(roi.fiber_id, f"{roi.label} ({roi.fiber_id})") for roi in rois if roi.enabled]
        wavelengths = [
            wavelength for wavelength, check in self.channel_checks.items() if check.isChecked()
        ]
        dialog = OutputProgramDialog(
            self._rwd_output_programs[port], fibers=fibers, wavelengths=wavelengths, parent=self
        )
        if dialog.exec() and dialog.result_output is not None:
            self.apply_output_program(dialog.result_output)

    def apply_output_program(self, output: RWDTTLOutputConfig) -> None:
        self._rwd_output_programs[output.port] = output
        self.rwd_output_checks[output.port].setChecked(True)
        self._refresh_output_summaries()
        self._rebuild_marker_buttons()
        self._invalidate_preview()

    def start_output_test(self, port: int) -> None:
        if self._thread is not None or self._device_thread is not None:
            return
        try:
            config = self._build_config()
        except Exception as error:
            QMessageBox.critical(self, "Invalid configuration", str(error))
            return
        if not isinstance(config.source, RWDSourceConfig):
            return
        output = self._rwd_output_programs[port]
        answer = QMessageBox.question(
            self,
            f"Test TTL output {port}",
            f"Drive output {port} with {output.frequency_hz:g} Hz, {output.pulse_width_ms} ms "
            "pulses for 1 s? Anything connected to it, such as a laser, is driven too. "
            "The LEDs stay off.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        source = config.source.model_copy(
            update={
                "ttl_outputs": (
                    *(o for o in config.source.ttl_outputs if o.port != port),
                    output,
                )
            }
        )
        config = config.model_copy(update={"source": source})
        thread = QThread(self)
        worker = BoardTaskWorker(lambda: self._output_tester(config, port))
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.finished.connect(self._on_output_tested)
        worker.failed.connect(self._on_output_test_failed)
        worker.finished.connect(thread.quit)
        worker.failed.connect(thread.quit)
        thread.finished.connect(self._on_device_thread_finished)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        self._device_thread = thread
        self._device_worker = worker
        self._set_busy_controls(False)
        self.statusBar().showMessage(f"Testing TTL output {port}…")
        thread.start()

    def start_power_measurement(self) -> None:
        if self._thread is not None or self._device_thread is not None:
            return
        try:
            config = self._build_config()
        except Exception as error:
            QMessageBox.critical(self, "Invalid configuration", str(error))
            return
        if not isinstance(config.source, RWDSourceConfig):
            return
        leds = ", ".join(f"{int(channel.wavelength_nm)} nm" for channel in config.enabled_channels)
        answer = QMessageBox.question(
            self,
            "Measure LED power",
            f"DBF will light {leds} one at a time, for about 2 s each, at their set power, "
            "and read the power meter.\n\nPoint the fiber tip at the meter's sensor. No "
            "animal may be connected, and nobody may look into the fiber.\n\nContinue?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return

        def measure() -> tuple:
            meter = self._power_meter_factory()
            try:
                return self._power_measurer(config, meter)
            finally:
                meter.close()

        thread = QThread(self)
        worker = BoardTaskWorker(measure)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.finished.connect(self._on_power_measured)
        worker.failed.connect(self._on_power_measurement_failed)
        worker.finished.connect(thread.quit)
        worker.failed.connect(thread.quit)
        thread.finished.connect(self._on_device_thread_finished)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        self._device_thread = thread
        self._device_worker = worker
        self._set_busy_controls(False)
        self._warmup.leds_on()
        self.power_label.setText("Measuring…")
        self.statusBar().showMessage("Measuring LED power - the LEDs light one at a time")
        thread.start()

    def _on_power_measured(self, readings: tuple) -> None:
        self._warmup.leds_off()
        self.last_power_readings = readings
        self.power_label.setText(
            "\n".join(
                f"{reading.wavelength_nm} nm at {reading.set_percent:g} %: "
                f"{reading.power_mw:.3f} mW (± {reading.spread_mw:.3f})"
                for reading in readings
            )
            + "\nContinuous power at the fiber tip; interleaved recordings average less."
        )
        self.statusBar().showMessage("LED power measured; every LED is off again")

    def _on_power_measurement_failed(self, traceback_text: str) -> None:
        self._warmup.leds_off()
        self.power_label.setText("Measurement failed")
        if not self._close_pending:
            QMessageBox.critical(self, "Power measurement failed", traceback_text)

    def _on_output_tested(self, summary: str) -> None:
        self.last_output_test = summary
        self.statusBar().showMessage(summary)

    def _on_output_test_failed(self, traceback_text: str) -> None:
        self.last_output_test = None
        self.statusBar().showMessage("TTL output test failed")
        if not self._close_pending:
            QMessageBox.critical(self, "TTL output test failed", traceback_text)

    def fire_output(self, port: int) -> None:
        """Start a manual output program in the running recording."""

        if self._operation_kind != "recording" or self._worker is None:
            return
        self._worker.trigger_output(port)
        self.statusBar().showMessage(f"Triggered TTL output {port}")

    def _update_overview(self) -> None:
        enabled = sum(check.isChecked() for check in self.channel_checks.values())
        self.channels_summary.value_label.setText(str(enabled))
        if hasattr(self, "fiber_count_spin"):
            fiber_count = self.fiber_count_spin.value()
            active_count = sum(
                editor.enabled_check.isChecked()
                for editor in getattr(self, "_roi_metadata_editors", ())
            )
            self.fibers_summary.value_label.setText(f"{active_count}/{fiber_count}")
            self.format_summary.value_label.setText(str(active_count))

    def _build_channel_group(self, profile: SystemProfile) -> QGroupBox:
        group = QGroupBox("Excitation schedule")
        layout = QVBoxLayout(group)
        layout.setSpacing(8)
        percent = profile.intensity_unit == "%"
        scale = self._intensity_scale(profile)
        default = round((5.0 if percent else 1.0) * scale)
        checks: dict[Wavelength, QCheckBox] = {}
        sliders: dict[Wavelength, QSlider] = {}
        readouts: dict[Wavelength, QLabel] = {}
        for wavelength in profile.wavelengths:
            check = QCheckBox(_WAVELENGTH_TITLES[wavelength])
            check.setChecked(True)
            slider = QSlider(Qt.Orientation.Horizontal)
            slider.setRange(0, round(profile.intensity_maximum * scale))
            slider.setSingleStep(1 if percent else 5)
            slider.setPageStep(10 if percent else 25)
            slider.setTickInterval(100 if percent else 50)
            slider.setValue(default)
            noun = "power" if percent else "voltage"
            slider.setAccessibleName(f"{int(wavelength)} nanometer excitation {noun}")
            readout = QLabel(self._format_intensity(default, profile))
            readout.setObjectName("voltageReadout")
            readout.setMinimumWidth(54)
            slider.valueChanged.connect(
                lambda value, label=readout, prof=profile: label.setText(
                    self._format_intensity(value, prof)
                )
            )
            row = QWidget()
            row_layout = QVBoxLayout(row)
            row_layout.setContentsMargins(0, 0, 0, 0)
            row_layout.setSpacing(3)
            heading = QHBoxLayout()
            heading.addWidget(check, stretch=1)
            heading.addWidget(readout)
            row_layout.addLayout(heading)
            row_layout.addWidget(slider)
            layout.addWidget(row)
            checks[wavelength] = check
            sliders[wavelength] = slider
            readouts[wavelength] = readout
        group.setVisible(profile is CUSTOM_PROFILE)
        self._channel_controls[profile.kind] = (checks, sliders, group)
        self._channel_readouts[profile.kind] = readouts
        return group

    @staticmethod
    def _intensity_scale(profile: SystemProfile) -> int:
        """Slider steps per unit: 0.1 % for RWD power, 0.01 V for Custom voltage."""

        return 10 if profile.intensity_unit == "%" else 100

    @classmethod
    def _format_intensity(cls, value: int, profile: SystemProfile) -> str:
        if profile.intensity_unit == "%":
            return f"{value / cls._intensity_scale(profile):.1f} %"
        return f"{value / cls._intensity_scale(profile):.2f} V"

    @property
    def _active_profile(self) -> SystemProfile:
        return RWD_PROFILE if self._is_rwd_mode() else CUSTOM_PROFILE

    @property
    def channel_checks(self) -> dict[Wavelength, QCheckBox]:
        return self._channel_controls[self._active_profile.kind][0]

    @property
    def channel_voltages(self) -> dict[Wavelength, QSlider]:
        """Intensity sliders of the active system (volts for Custom, percent for RWD)."""

        return self._channel_controls[self._active_profile.kind][1]

    @property
    def channel_voltage_labels(self) -> dict[Wavelength, QLabel]:
        return self._channel_readouts[self._active_profile.kind]

    def _use_profile_camera(self, profile: SystemProfile) -> None:
        # Keep the edited values before the new system's ranges can clamp them.
        camera = self._camera_from_controls()
        self._session_template = self._session_template.model_copy(update={"camera": camera})
        self._configure_camera_controls(profile)
        if profile.sensor_size_px is not None:
            if (camera.width_px, camera.height_px) == profile.sensor_size_px:
                return
            self._custom_camera = camera
            replacement = profile.default_camera
        else:
            if camera.model_name != RWD_PROFILE.default_camera.model_name:
                return
            replacement = self._custom_camera
        replacement = replacement.model_copy(update={"raw_capture": camera.raw_capture})
        rois = self._roi_configs_from_controls() if self._roi_items else ()
        self._session_template = self._session_template.model_copy(update={"camera": replacement})
        fits = all(
            roi.radius_px <= roi.center_x_px <= replacement.width_px - roi.radius_px
            and roi.radius_px <= roi.center_y_px <= replacement.height_px - roi.radius_px
            for roi in rois
        )
        self._load_camera_controls(replacement)
        if rois and fits:
            self._set_roi_items(rois, replacement)
        elif rois:
            # Circles from the other sensor do not fit; lay fibers out again for this one.
            self._rebuild_roi_items()

    def _build_camera_settings_group(self) -> QGroupBox:
        group = QGroupBox("Camera settings")
        self.camera_settings_group = group
        form = QFormLayout(group)
        self.fps_spin = QSpinBox()
        self.fps_spin.setSuffix(" Hz")
        self.fps_spin.setToolTip("Total trigger rate, shared by the enabled LEDs")
        self.exposure_spin = QDoubleSpinBox()
        self.exposure_spin.setDecimals(2)
        self.exposure_spin.setSingleStep(0.5)
        self.exposure_spin.setSuffix(" ms")
        self.exposure_max_button = QPushButton("Max")
        self.exposure_max_button.setToolTip("Use the longest exposure this frame rate allows")
        self.exposure_max_button.clicked.connect(
            lambda: self.exposure_spin.setValue(self.exposure_spin.maximum())
        )
        exposure_row = QWidget()
        exposure_layout = QHBoxLayout(exposure_row)
        exposure_layout.setContentsMargins(0, 0, 0, 0)
        exposure_layout.addWidget(self.exposure_spin, stretch=1)
        exposure_layout.addWidget(self.exposure_max_button)
        self.gain_spin = QDoubleSpinBox()
        self.gain_spin.setRange(0.0, _MAX_GAIN_DB)
        self.gain_spin.setDecimals(1)
        self.gain_spin.setSingleStep(1.0)
        self.gain_spin.setSuffix(" dB")
        self.gain_spin.setMaximumWidth(90)
        gain_tip = (
            "Camera gain. Raise it to find and focus the fiber face; record at 0 dB, "
            "since gain does not improve the signal-to-noise ratio."
        )
        self.gain_spin.setToolTip(gain_tip)
        self.gain_slider = QSlider(Qt.Orientation.Horizontal)
        self.gain_slider.setRange(0, round(_MAX_GAIN_DB * 10))
        self.gain_slider.setSingleStep(1)
        self.gain_slider.setPageStep(10)
        self.gain_slider.setToolTip(gain_tip)
        self.gain_slider.valueChanged.connect(lambda tenths: self.gain_spin.setValue(tenths / 10))
        self.gain_spin.valueChanged.connect(lambda db: self.gain_slider.setValue(round(db * 10)))
        gain_row = QWidget()
        gain_layout = QHBoxLayout(gain_row)
        gain_layout.setContentsMargins(0, 0, 0, 0)
        gain_layout.addWidget(self.gain_slider, stretch=1)
        gain_layout.addWidget(self.gain_spin)
        form.addRow("Frame rate", self.fps_spin)
        form.addRow("Exposure", exposure_row)
        form.addRow("Gain", gain_row)
        self.camera_settings_hint = QLabel()
        self.camera_settings_hint.setObjectName("hint")
        self.camera_settings_hint.setWordWrap(True)
        form.addRow(self.camera_settings_hint)
        self.fps_spin.valueChanged.connect(self._on_camera_timing_changed)
        self.exposure_spin.valueChanged.connect(self._on_camera_timing_changed)
        self.gain_spin.valueChanged.connect(self._on_gain_changed)
        return group

    def _configure_camera_controls(self, profile: SystemProfile) -> None:
        if not hasattr(self, "fps_spin"):
            return
        blockers = [QSignalBlocker(w) for w in (self.fps_spin, self.exposure_spin)]
        low, high = profile.frame_rate_range_hz or (1.0, 500.0)
        self.fps_spin.setRange(int(low), int(high))
        minimum_us = profile.minimum_exposure_us or 10.0
        self.exposure_spin.setMinimum(minimum_us / 1000)
        del blockers
        self._update_exposure_limits()

    def _load_camera_controls(self, camera: CameraConfig) -> None:
        if not hasattr(self, "fps_spin"):
            return
        blockers = [QSignalBlocker(w) for w in (self.fps_spin, self.exposure_spin, self.gain_spin)]
        self.fps_spin.setValue(round(camera.requested_fps))
        self._update_exposure_limits()
        self.exposure_spin.setValue(camera.exposure_us / 1000)
        self.gain_spin.setValue(camera.gain)
        del blockers
        self._update_exposure_limits()

    def _update_exposure_limits(self) -> None:
        profile = self._active_profile
        fps = self.fps_spin.value()
        ceiling_us = profile.maximum_exposure_us(fps)
        maximum_ms = 1000.0 if ceiling_us is None else ceiling_us / 1000
        self.exposure_spin.setMaximum(maximum_ms)
        if ceiling_us is None:
            text = f"One frame every {1000 / fps:.2f} ms."
        else:
            text = (
                f"At {fps} Hz the exposure can be {self.exposure_spin.minimum():g} to "
                f"{maximum_ms:.2f} ms; the RWD manual recommends the maximum."
            )
        self.camera_settings_hint.setText(
            text + " Raise the gain to find and focus the fiber face, then record at 0 dB."
        )

    def _camera_from_controls(self) -> CameraConfig:
        camera = self._session_template.camera
        update: dict[str, object] = {}
        if hasattr(self, "raw_checkbox"):
            update["raw_capture"] = self.raw_checkbox.isChecked()
        if self._camera_controls_ready:
            update.update(
                requested_fps=float(self.fps_spin.value()),
                exposure_us=max(1, round(self.exposure_spin.value() * 1000)),
                gain=float(self.gain_spin.value()),
            )
        return camera.model_copy(update=update)

    def _on_camera_timing_changed(self, *_args: object) -> None:
        self._update_exposure_limits()
        self._invalidate_preview()
        if self._operation_kind == "live" and self._worker is not None:
            # The board and cameras take timing only at the start of a session.
            self._restart_live_view = True
            self.stop_recording()

    def _on_gain_changed(self, value: float) -> None:
        self._invalidate_preview()
        if self._operation_kind == "live" and isinstance(self._worker, LiveViewWorker):
            self._worker.request_gain(value)

    def _is_rwd_mode(self) -> bool:
        return hasattr(self, "source_combo") and self.source_combo.currentData() == "rwd"

    def _apply_source_mode(self, *_args: object) -> None:
        if not hasattr(self, "rwd_group"):
            return
        is_rwd = self._is_rwd_mode()
        profile = self._active_profile
        self.rwd_group.setVisible(is_rwd)
        for kind, (_checks, _sliders, group) in self._channel_controls.items():
            group.setVisible(kind == profile.kind)
        for wavelength, image_group in self.wavelength_image_groups.items():
            image_group.setVisible(wavelength in profile.wavelengths)
        self._use_profile_camera(profile)
        self._sync_camera2_items()
        self._rebuild_marker_buttons()
        self._enforce_diameter_limits()
        self._sync_diameter_controls()
        two_cameras = profile.camera_count > 1
        self.calibration_panel_cam2.setVisible(two_cameras)
        self.calibration_panel_titles[1].setText(
            "CAMERA 1 — 410/470 NM" if two_cameras else "CAMERA"
        )
        self._active_trace_wavelengths = tuple(int(w) for w in profile.wavelengths)
        for wavelength_nm, check in self.trace_wavelength_checks.items():
            check.setVisible(wavelength_nm in self._active_trace_wavelengths)
        if not is_rwd:
            self._devices_checked_for_mode = False
        elif self._auto_check_devices and not self._devices_checked_for_mode:
            self._devices_checked_for_mode = True
            QTimer.singleShot(0, self.start_device_check)
        if is_rwd:
            self.source_mode_hint.setText(_RWD_MODE_HINT)
            self.backend_badge.setText("RWD hardware")
            self.statusBar().showMessage(
                "RWD mode - DBF drives the RWD controller and cameras; the unit's power "
                "switch is the only hardware fail-safe"
            )
        else:
            self.source_mode_hint.setText(_CUSTOM_MODE_HINT)
            self.backend_badge.setText("Simulator")
            self.statusBar().showMessage(
                "Ready - simulator mode; no physical camera or controller connected"
            )
        self.preview_button.setEnabled(self._thread is None)
        self.backend_badge.setProperty("hardware", is_rwd)
        self.backend_badge.style().unpolish(self.backend_badge)
        self.backend_badge.style().polish(self.backend_badge)
        self._update_overview()
        self._invalidate_preview()

    def _build_trace_workspace(self) -> ScrollableTraceWorkspace:
        self.trace_workspace = ScrollableTraceWorkspace()
        self.trace_plots: dict[int, pg.PlotItem] = {}
        self.trace_view_boxes: dict[int, TraceViewBox] = {}
        return self.trace_workspace

    def _configure_trace_plots(self, rois: tuple[ROIConfig, ...]) -> None:
        self.trace_workspace.clear()
        # Each ROI keeps a fixed-height row; more ROIs scroll inside the trace area.
        self.trace_workspace.setFixedHeight(_TRACE_ROW_HEIGHT_PX * max(1, len(rois)))
        self.trace_plots = {}
        self.trace_view_boxes = {}
        self._trace_manual_x_range = None
        self._trace_roi_count = len(rois)
        self._curves.clear()
        display_mode = self.trace_mode_combo.currentData()
        for row, roi in enumerate(rois):
            view_box = TraceViewBox()
            view_box.range_selected.connect(self._on_trace_range_selected)
            view_box.reset_requested.connect(self._reset_trace_zoom)
            view_box.sigRangeChangedManually.connect(
                lambda mask, index=row: self._on_trace_range_changed_manually(index, mask)
            )
            plot = self.trace_workspace.addPlot(row=row, col=0, viewBox=view_box)
            title = f"ROI {row + 1} - {roi.label} / {roi.animal_id}"
            plot.setTitle(title, color="#e8f7fa", size="10pt")
            plot.showGrid(x=True, y=True, alpha=0.22)
            if display_mode in _DFF_MODES:
                plot.setLabel("left", "dF/F", units="%")
            else:
                plot.setLabel("left", "Fluorescence", units="counts")
            if row == len(rois) - 1:
                plot.setLabel("bottom", "Session time", units="s")
            if row == 0:
                plot.addLegend(offset=(10, 5), colCount=3)
            plot.setClipToView(True)
            self.trace_plots[row] = plot
            self.trace_view_boxes[row] = view_box

    def _build_wavelength_images_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(12, 14, 12, 12)
        layout.setSpacing(10)
        title = QLabel("Live camera frames by excitation wavelength")
        title.setObjectName("sectionTitle")
        copy = QLabel(
            "Each panel holds the latest frame received for that explicit wavelength. "
            "The acquisition trace and saved frame identity use the same controller record."
        )
        copy.setObjectName("hint")
        copy.setWordWrap(True)
        layout.addWidget(title)
        layout.addWidget(copy)

        panels = QHBoxLayout()
        panels.setSpacing(10)
        self.wavelength_image_items: dict[Wavelength, pg.ImageItem] = {}
        self.wavelength_image_plots: dict[Wavelength, pg.PlotWidget] = {}
        self.wavelength_frame_labels: dict[Wavelength, QLabel] = {}
        self.wavelength_image_groups: dict[Wavelength, QGroupBox] = {}
        for wavelength in Wavelength:
            group = QGroupBox(_WAVELENGTH_TITLES[wavelength])
            group.setVisible(wavelength in CUSTOM_PROFILE.wavelengths)
            self.wavelength_image_groups[wavelength] = group
            group_layout = QVBoxLayout(group)
            plot = pg.PlotWidget()
            plot.setAspectLocked(True)
            plot.invertY(True)
            plot.showGrid(x=True, y=True, alpha=0.15)
            plot.setLabel("bottom", "Camera x", units="px")
            plot.setLabel("left", "Camera y", units="px")
            image_item = pg.ImageItem()
            image_item.setImage(
                np.zeros((256, 256), dtype=np.uint16),
                autoLevels=False,
                levels=(0, (1 << 12) - 1),
            )
            plot.addItem(image_item)
            status = QLabel("Waiting for recording")
            status.setObjectName("hint")
            status.setAlignment(Qt.AlignmentFlag.AlignCenter)
            group_layout.addWidget(plot, stretch=1)
            group_layout.addWidget(status)
            panels.addWidget(group, stretch=1)
            self.wavelength_image_items[wavelength] = image_item
            self.wavelength_image_plots[wavelength] = plot
            self.wavelength_frame_labels[wavelength] = status
        layout.addLayout(panels, stretch=1)
        return page

    def _build_calibration_tab(self, fiber_count: int) -> QWidget:
        page = QWidget()
        layout = QHBoxLayout(page)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(10)

        left = QWidget()
        left.setMinimumWidth(390)
        left.setMaximumWidth(470)
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.addWidget(self._build_camera_settings_group())
        fibers_group = QGroupBox("Fiber array")
        left_layout.addWidget(fibers_group, stretch=1)
        fibers_layout = QVBoxLayout(fibers_group)
        fibers_form = QFormLayout()
        self.fiber_count_spin = QSpinBox()
        self.fiber_count_spin.setRange(1, 9)
        self.fiber_count_spin.setValue(fiber_count)
        self.fiber_count_spin.valueChanged.connect(self._rebuild_roi_items)
        self.fiber_count_spin.valueChanged.connect(self._update_overview)
        fibers_form.addRow("Circular ROIs", self.fiber_count_spin)
        fibers_layout.addLayout(fibers_form)
        instructions = QLabel(
            "Drag a circle to move its fiber ROI. Drag its handle to resize it. "
            "Each ROI has its own live trace row; wavelength colors remain consistent "
            "across every row."
        )
        instructions.setObjectName("hint")
        instructions.setWordWrap(True)
        self.fiber_instructions = instructions
        fibers_layout.addWidget(instructions)
        metadata_title = QLabel("ONE ANIMAL AND NWB FILE PER ROI")
        metadata_title.setObjectName("eyebrow")
        fibers_layout.addWidget(metadata_title)
        self.roi_metadata_tabs = QTabWidget()
        self.roi_metadata_tabs.setMinimumHeight(280)
        self._roi_metadata_editors: list[ROIMetadataEditor] = []
        fibers_layout.addWidget(self.roi_metadata_tabs)
        self.roi_summary = QLabel()
        self.roi_summary.setWordWrap(True)
        fibers_layout.addWidget(self.roi_summary)
        fibers_layout.addStretch(1)
        layout.addWidget(left)

        camera_group = QGroupBox("Camera calibration — full frame")
        self.camera_calibration_group = camera_group
        camera_layout = QVBoxLayout(camera_group)
        camera_row = QHBoxLayout()
        self.live_button = QPushButton("Live view")
        self.live_button.setObjectName("preview")
        self.live_button.setMinimumHeight(38)
        self.live_button.setMinimumWidth(150)
        self.live_button.setToolTip(
            "Stream the cameras with the current excitation settings to focus and place "
            "fiber circles; nothing is recorded. The LEDs stay on until you press Stop "
            "live view, and it stops by itself after 10 minutes."
        )
        self.live_button.clicked.connect(self._toggle_live_view)
        camera_row.addWidget(self.live_button)
        self.auto_contrast_check = QCheckBox("Auto contrast")
        self.auto_contrast_check.setChecked(True)
        self.auto_contrast_check.setToolTip(
            "Stretch the display between the image's dim and bright pixels. Display only: "
            "recorded frames and NWB reference images keep their fixed scaling."
        )
        self.auto_contrast_check.toggled.connect(self._refresh_calibration_images)
        self.live_view_hint = QLabel(
            "Press Live view to stream the cameras while you focus and place each fiber's "
            "circle. On RWD, the solid circles are on camera 1 and the dashed circles on "
            "camera 2."
        )
        self.live_view_hint.setObjectName("hint")
        self.live_view_hint.setWordWrap(True)
        camera_row.addWidget(self.live_view_hint, stretch=1)
        camera_row.addWidget(self.auto_contrast_check)
        camera_layout.addLayout(camera_row)
        panels = QHBoxLayout()
        self.calibration_panel_titles: dict[int, QLabel] = {}
        self.calibration_panels: dict[int, QWidget] = {}
        plots: dict[int, pg.PlotWidget] = {}
        for camera, title in ((1, "CAMERA"), (2, "CAMERA 2 — 560 NM")):
            panel = QWidget()
            panel_layout = QVBoxLayout(panel)
            panel_layout.setContentsMargins(0, 0, 0, 0)
            heading = QLabel(title)
            heading.setObjectName("eyebrow")
            panel_layout.addWidget(heading)
            plot = pg.PlotWidget()
            plot.setAspectLocked(True)
            plot.invertY(True)
            plot.showGrid(x=True, y=True, alpha=0.18)
            plot.setLabel("bottom", "Camera x", units="px")
            plot.setLabel("left", "Camera y", units="px")
            plot.addItem(self._calibration_images[camera])
            panel_layout.addWidget(plot, stretch=1)
            panels.addWidget(panel, stretch=1)
            self.calibration_panel_titles[camera] = heading
            self.calibration_panels[camera] = panel
            plots[camera] = plot
        self.calibration_plot = plots[1]
        self.calibration_plot_cam2 = plots[2]
        self.calibration_panel_cam2 = self.calibration_panels[2]
        self.calibration_panel_cam2.setVisible(False)
        camera_layout.addLayout(panels, stretch=1)
        layout.addWidget(camera_group, stretch=1)
        self._settings_widgets.append(fibers_group)
        self._rebuild_roi_metadata(fiber_count)
        return page

    def _apply_theme(self) -> None:
        self.setStyleSheet(
            """
            QMainWindow, QWidget { background: #081521; color: #e8f7fa; }
            QWidget#appHeader {
                background: #0d2132; border: 1px solid #203e52; border-radius: 12px;
            }
            QWidget#appHeader QWidget { background: transparent; border: none; }
            QLabel#brandMark { background: transparent; }
            QLabel#wordmark {
                color: #f4fcfd; font-weight: 800; font-size: 12pt; letter-spacing: 1px;
            }
            QLabel#strapline { color: #6f91a6; font-size: 9pt; }
            QLabel#eyebrow {
                color: #43d6df; font-size: 8pt; font-weight: 800; letter-spacing: 2px;
            }
            QLabel#sectionTitle { color: #f4fcfd; font-size: 18pt; font-weight: 700; }
            QLabel#versionBadge {
                background: #102a3e; border: 1px solid #2b5167; border-radius: 10px;
                color: #8cabbc; padding: 5px 10px; font-weight: 700;
            }
            QGroupBox {
                background: #0c1d2b; border: 1px solid #203c50; border-radius: 10px;
                margin-top: 13px; padding: 12px; font-weight: 700;
            }
            QGroupBox::title { subcontrol-origin: margin; left: 12px; padding: 0 5px; }
            QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox {
                background: #10283a; border: 1px solid #31566d; border-radius: 6px;
                padding: 7px; color: #f4fcfd; selection-background-color: #00aeba;
            }
            QLineEdit:disabled, QComboBox:disabled, QSpinBox:disabled,
            QDoubleSpinBox:disabled {
                background: #0d1f31; border-color: #22394d; color: #5f7387;
            }
            QLabel:disabled, QCheckBox:disabled, QGroupBox:disabled { color: #5f7387; }
            QPushButton {
                background: #15374d; border: 1px solid #315b71; border-radius: 7px;
                padding: 8px 14px;
            }
            QPushButton:hover { background: #205170; }
            QPushButton#preview { background: #00aeba; color: #04151e; font-weight: 700; }
            QPushButton#preview:hover { background: #20c3ce; }
            QPushButton#record { background: #4fd18b; color: #05261a; font-weight: 700; }
            QPushButton#record:hover { background: #66dd9c; }
            QPushButton#horizon { padding: 6px 12px; }
            QPushButton#horizon:checked {
                background: #00aeba; color: #071628; border-color: #43d6df;
                font-weight: 800;
            }
            QPushButton#attention { background: #d98324; color: #1a0f02; font-weight: 700; }
            QPushButton#attention:hover { background: #eb9438; }
            QPushButton:disabled { color: #61778a; background: #13283a; }
            QPushButton#preview:disabled, QPushButton#record:disabled,
            QPushButton#attention:disabled {
                color: #61778a; background: #13283a; border-color: #22394d;
            }
            QLabel#hint { color: #7694aa; }
            QLabel#deviceStatus[state="ok"] { color: #4fd18b; }
            QLabel#deviceStatus[state="failed"] { color: #ff8791; font-weight: 700; }
            QLabel#deviceStatus[state="unknown"] { color: #7694aa; }
            QLabel#safetyWarning {
                background: #3a1820; color: #ffb3b9; border-radius: 6px; padding: 8px;
                font-weight: 700;
            }
            QLabel#liveStatus { font-weight: 700; color: #f2b134; }
            QLabel#previewStatus {
                border: 1px solid #315570; border-radius: 6px; padding: 7px 10px;
                font-weight: 700;
            }
            QLabel#previewStatus[state="required"] { background: #2b2316; color: #f2b134; }
            QLabel#previewStatus[state="running"] { background: #102b43; color: #66c2ff; }
            QLabel#previewStatus[state="passed"] { background: #103226; color: #4fd18b; }
            QLabel#previewStatus[state="failed"] { background: #3a1820; color: #ff8791; }
            QWidget#summaryCard {
                background: #102638; border: 1px solid #23465b; border-radius: 8px;
            }
            QLabel#summaryValue { color: #f4fcfd; font-size: 15pt; font-weight: 800; }
            QLabel#summaryCaption { color: #6f91a6; font-size: 7pt; font-weight: 700; }
            QLabel#voltageReadout { color: #43d6df; font-weight: 800; }
            QSlider::groove:horizontal {
                height: 6px; background: #17384d; border: 1px solid #31566d;
                border-radius: 3px;
            }
            QSlider::sub-page:horizontal { background: #00aeba; border-radius: 3px; }
            QSlider::handle:horizontal {
                background: #e8f7fa; border: 2px solid #00aeba; width: 16px;
                margin: -6px 0; border-radius: 8px;
            }
            QLabel#badge { border-radius: 10px; padding: 5px 12px; font-weight: 800; }
            QLabel#badge[hardware="false"] { background: #b7e532; color: #0b1d31; }
            QLabel#systemStatus {
                border-radius: 10px; padding: 5px 14px; font-weight: 800; font-size: 13pt;
            }
            QLabel#systemStatus[state="ready"] { background: #13283a; color: #7694aa; }
            QLabel#systemStatus[state="preview"] { background: #102b43; color: #66c2ff; }
            QLabel#systemStatus[state="recording"] { background: #e04450; color: #fff5f5; }
            QLabel#systemStatus[state="saving"] { background: #d98324; color: #1a0f02; }
            QLabel#systemStatus[state="complete"] { background: #00aeba; color: #04151e; }
            QLabel#systemStatus[state="error"] { background: #e04450; color: #fff5f5; }
            QTabWidget::pane {
                border: 1px solid #203c50; border-radius: 9px; top: -1px;
            }
            QTabBar::tab {
                background: #0d2132; border: 1px solid #203c50; padding: 10px 20px;
                margin-right: 4px; border-top-left-radius: 7px; border-top-right-radius: 7px;
            }
            QTabBar::tab:selected {
                background: #00aeba; color: #071628; font-weight: 800;
            }
            QProgressBar {
                border: 1px solid #315570; border-radius: 5px; text-align: center;
                background: #102b43;
            }
            QProgressBar::chunk { background: #00bfd1; }
            QStatusBar { color: #7694aa; border-top: 1px solid #203c50; }
            QScrollArea { border: none; }
            QScrollBar:vertical {
                background: #0d1f31; width: 12px; margin: 0; border-radius: 6px;
            }
            QScrollBar::handle:vertical {
                background: #315570; min-height: 28px; border-radius: 5px; margin: 2px;
            }
            QScrollBar::handle:vertical:hover { background: #4c7893; }
            QScrollBar:horizontal {
                background: #0d1f31; height: 12px; margin: 0; border-radius: 6px;
            }
            QScrollBar::handle:horizontal {
                background: #315570; min-width: 28px; border-radius: 5px; margin: 2px;
            }
            QScrollBar::handle:horizontal:hover { background: #4c7893; }
            QScrollBar::add-line, QScrollBar::sub-line { width: 0; height: 0; }
            QScrollBar::add-page, QScrollBar::sub-page { background: transparent; }
            """
        )

    def _set_system_status(self, text: str, state: str) -> None:
        self.system_status.setText(text)
        self.system_status.setProperty("state", state)
        self.system_status.style().unpolish(self.system_status)
        self.system_status.style().polish(self.system_status)

    def _set_preview_status(self, text: str, state: str) -> None:
        self.preview_status.setText(text)
        self.preview_status.setProperty("state", state)
        self.preview_status.style().unpolish(self.preview_status)
        self.preview_status.style().polish(self.preview_status)

    @staticmethod
    def _configuration_signature(config: SessionConfig) -> str:
        # A preview exercises neither TTL inputs nor the start/stop options.
        payload = config.model_dump(
            mode="json",
            exclude={
                "session_id",
                "session_start_time",
                "display",
                "recording",
                "ttl_inputs",
                "event_markers",
                "ttl_events",
            },
        )
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(encoded).hexdigest()

    def _invalidate_preview(self, *_args: object) -> None:
        self._preview_signature = None
        self._pending_preview_signature = None
        if hasattr(self, "start_button"):
            self.start_button.setEnabled(False)
        if hasattr(self, "preview_status") and self._thread is None:
            self._set_preview_status(
                "Preview required - settings changed; run preview before recording",
                "required",
            )

    def _build_events_group(self) -> QGroupBox:
        group = QGroupBox("TTL inputs and events")
        layout = QGridLayout(group)
        layout.setColumnStretch(1, 1)
        row = 0

        def heading(text: str, hint: str) -> None:
            nonlocal row
            title = QLabel(text)
            title.setObjectName("eyebrow")
            title.setToolTip(hint)
            layout.addWidget(title, row, 0, 1, 4)
            row += 1

        heading("TTL INPUTS", "Inputs DBF records, with the label stored in the NWB files")
        self.ttl_input_rows: dict[int, tuple[QCheckBox, QLineEdit, QComboBox]] = {}
        for line in range(1, 5):
            check = QCheckBox(f"Input {line}")
            label = QLineEdit(f"behavior_{line}")
            label.setMaxLength(64)
            edges = QComboBox()
            for text, value in (
                ("both edges", "both"),
                ("rising only", "rising"),
                ("falling only", "falling"),
            ):
                edges.addItem(text, value)
            layout.addWidget(check, row, 0)
            layout.addWidget(label, row, 1, 1, 2)
            layout.addWidget(edges, row, 3)
            self.ttl_input_rows[line] = (check, label, edges)
            row += 1

        heading(
            "MARKERS",
            "Press the number key or the marker's button while recording; blank names are off",
        )
        self.marker_name_edits: dict[str, QLineEdit] = {}
        for key in EVENT_MARKER_KEYS[:_MARKER_ROWS]:
            name = QLineEdit()
            name.setMaxLength(64)
            name.setPlaceholderText("type a name to use this key")
            layout.addWidget(QLabel(f"Key {key}"), row, 0)
            layout.addWidget(name, row, 1, 1, 3)
            name.textChanged.connect(self._rebuild_marker_buttons)
            self.marker_name_edits[key] = name
            row += 1

        heading(
            "NAMED TTL EVENTS",
            "Name pulses on an input by their width, from rising to falling edge; the "
            "input must record both edges",
        )
        self.ttl_event_rows: list[
            tuple[QCheckBox, QComboBox, QDoubleSpinBox, QDoubleSpinBox, QLineEdit]
        ] = []
        for index in range(_TTL_EVENT_ROWS):
            check = QCheckBox()
            line = _ttl_line_combo()
            low = QDoubleSpinBox()
            high = QDoubleSpinBox()
            for spin, value in ((low, 0.0), (high, 100.0)):
                spin.setRange(0.0, 3_600_000.0)
                spin.setDecimals(0)
                spin.setSuffix(" ms")
                spin.setValue(value)
            low.setToolTip("Shortest pulse width that gets this name")
            high.setToolTip("Pulses at least this wide do not get this name")
            name = QLineEdit(f"TTL event {index + 1}")
            name.setMaxLength(64)
            widths = QWidget()
            widths_layout = QHBoxLayout(widths)
            widths_layout.setContentsMargins(0, 0, 0, 0)
            widths_layout.addWidget(low)
            widths_layout.addWidget(QLabel("to"))
            widths_layout.addWidget(high)
            layout.addWidget(check, row, 0)
            layout.addWidget(name, row, 1)
            layout.addWidget(line, row, 2)
            layout.addWidget(widths, row, 3)
            self.ttl_event_rows.append((check, line, low, high, name))
            row += 1
        self._load_event_controls(self._session_template)
        return group

    def _ttl_inputs_from_controls(self) -> tuple[TTLInputConfig, ...]:
        return tuple(
            TTLInputConfig(
                line=line,
                label=label.text().strip() or f"input_{line}",
                edges=EdgeSelection(edges.currentData()),
            )
            for line, (check, label, edges) in self.ttl_input_rows.items()
            if check.isChecked()
        )

    def _markers_from_controls(self) -> tuple[EventMarkerConfig, ...]:
        return tuple(
            EventMarkerConfig(key=key, name=edit.text().strip())
            for key, edit in self.marker_name_edits.items()
            if edit.text().strip()
        )

    def _ttl_events_from_controls(self) -> tuple[TTLEventRuleConfig, ...]:
        return tuple(
            TTLEventRuleConfig(
                line=line.currentData(),
                min_width_ms=low.value(),
                max_width_ms=high.value(),
                name=name.text().strip() or f"TTL event {index + 1}",
            )
            for index, (check, line, low, high, name) in enumerate(self.ttl_event_rows)
            if check.isChecked()
        )

    def _load_event_controls(self, config: SessionConfig) -> None:
        inputs = {ttl.line: ttl for ttl in config.ttl_inputs}
        for line, (check, label, edges) in self.ttl_input_rows.items():
            ttl = inputs.get(line)
            check.setChecked(ttl is not None)
            if ttl is not None:
                label.setText(ttl.label)
                edges.setCurrentIndex(edges.findData(ttl.edges.value))
        markers = {marker.key: marker.name for marker in config.event_markers}
        for key, edit in self.marker_name_edits.items():
            edit.setText(markers.get(key, ""))
        for index, (check, line, low, high, name) in enumerate(self.ttl_event_rows):
            rule = config.ttl_events[index] if index < len(config.ttl_events) else None
            check.setChecked(rule is not None)
            if rule is not None:
                line.setCurrentIndex(line.findData(rule.line))
                low.setValue(rule.min_width_ms)
                high.setValue(rule.max_width_ms)
                name.setText(rule.name)
        self._rebuild_marker_buttons()

    def _rebuild_marker_buttons(self, *_args: object) -> None:
        if not hasattr(self, "marker_row"):
            return
        for button in self.marker_buttons.values():
            self.marker_row.removeWidget(button)
            button.deleteLater()
        self.marker_buttons = {}
        for button in getattr(self, "output_fire_buttons", {}).values():
            self.marker_row.removeWidget(button)
            button.deleteLater()
        self.output_fire_buttons: dict[int, QPushButton] = {}
        if hasattr(self, "rwd_output_checks") and self._is_rwd_mode():
            for port, check in self.rwd_output_checks.items():
                output = self._rwd_output_programs[port]
                if not check.isChecked() or output.trigger != "manual":
                    continue
                button = QPushButton(f"Fire {output.label or f'output {port}'}")
                button.setObjectName("attention")
                button.setToolTip(f"Start TTL output {port}'s program now")
                button.clicked.connect(lambda _checked=False, p=port: self.fire_output(p))
                self.marker_row.insertWidget(self.marker_row.count() - 1, button)
                self.output_fire_buttons[port] = button
        for marker in self._markers_from_controls():
            button = QPushButton(f"{marker.key} · {marker.name}")
            button.setObjectName("horizon")
            button.setToolTip(f"Mark {marker.name!r} now (key {marker.key})")
            button.clicked.connect(
                lambda _checked=False, key=marker.key: self.mark_event(key, "button")
            )
            self.marker_row.insertWidget(self.marker_row.count() - 1, button)
            self.marker_buttons[marker.key] = button
        self._update_marker_buttons()

    def _update_marker_buttons(self) -> None:
        recording = self._operation_kind == "recording" and self._worker is not None
        fire_buttons = getattr(self, "output_fire_buttons", {})
        for button in (*self.marker_buttons.values(), *fire_buttons.values()):
            button.setEnabled(recording)
        self.marker_row_label.setVisible(bool(self.marker_buttons) or bool(fire_buttons))

    def mark_event(self, key: str, source: str) -> None:
        """Mark the named event for ``key`` in the running recording."""

        if self._operation_kind != "recording" or self._worker is None:
            return
        markers = {marker.key: marker.name for marker in self._active_config.event_markers}
        name = markers.get(key)
        if name is None:
            return
        self._worker.mark_event(name, source)
        self.statusBar().showMessage(f"Marked {name!r}")

    def _show_event_marks(self, marks: Sequence[EventMark]) -> None:
        for mark in marks:
            self._event_marks_shown.append(mark)
            self._draw_event_mark(mark)

    def _draw_event_mark(self, mark: EventMark) -> None:
        color = {"ttl": "#ffd166", "output": "#7bdff2"}.get(mark.source, "#f78fb3")
        for row, plot in self.trace_plots.items():
            if mark.duration_s > 0:
                region = pg.LinearRegionItem(
                    values=(mark.timestamp_s, mark.timestamp_s + mark.duration_s),
                    movable=False,
                    brush=pg.mkBrush(123, 223, 242, 40),
                    pen=pg.mkPen(None),
                )
                region.setZValue(-5)
                plot.addItem(region)
            line = pg.InfiniteLine(
                pos=mark.timestamp_s,
                angle=90,
                movable=False,
                pen=pg.mkPen(color, width=1.2, style=Qt.PenStyle.DashLine),
                label=mark.name if row == 0 else None,
                labelOpts={"position": 0.92, "color": color, "movable": False},
            )
            line.setZValue(20)
            plot.addItem(line)

    def _build_start_row(self) -> QWidget:
        self.start_mode_combo = QComboBox()
        for label, mode in (
            ("Immediately", "immediate"),
            ("After a delay", "delayed"),
            ("On a TTL input", "ttl"),
        ):
            self.start_mode_combo.addItem(label, mode)
        self.start_mode_combo.setToolTip(
            "When Record starts the recording. DBF connects first and keeps the LEDs off "
            "while it waits; Stop cancels the wait"
        )
        self.start_delay_spin = QDoubleSpinBox()
        self.start_delay_spin.setRange(0.1, 86_400.0)
        self.start_delay_spin.setDecimals(1)
        self.start_delay_spin.setSuffix(" s")
        self.start_delay_spin.setValue(10.0)
        self.start_ttl_line_combo = _ttl_line_combo()
        self.start_ttl_edge_combo = _ttl_edge_combo()
        return self._mode_row(
            self.start_mode_combo,
            self.start_delay_spin,
            self.start_ttl_line_combo,
            self.start_ttl_edge_combo,
        )

    def _build_stop_row(self) -> QWidget:
        self.stop_mode_combo = QComboBox()
        for label, mode in (
            ("After the duration", "duration"),
            ("When I press Stop", "manual"),
            ("On a TTL input", "ttl"),
        ):
            self.stop_mode_combo.addItem(label, mode)
        self.stop_mode_combo.setToolTip(
            "When the recording ends. Stop always works; a TTL stop still ends at the "
            "maximum length, and a manual one after 24 hours"
        )
        self.stop_ttl_line_combo = _ttl_line_combo()
        self.stop_ttl_edge_combo = _ttl_edge_combo()
        return self._mode_row(
            self.stop_mode_combo, self.stop_ttl_line_combo, self.stop_ttl_edge_combo
        )

    def _build_excitation_row(self) -> QWidget:
        self.excitation_mode_combo = QComboBox()
        self.excitation_mode_combo.addItem("Continuous", "continuous")
        self.excitation_mode_combo.addItem("Sequential", "sequential")
        self.excitation_mode_combo.setToolTip(
            "Sequential lights the LEDs for the on time, then keeps them off for the off "
            "time, repeating, to limit photobleaching. TTL inputs are still recorded while "
            "they are off."
        )
        self.excitation_on_spin = QDoubleSpinBox()
        self.excitation_off_spin = QDoubleSpinBox()
        for spin, prefix, value in (
            (self.excitation_on_spin, "on ", 60.0),
            (self.excitation_off_spin, "off ", 240.0),
        ):
            spin.setRange(1.0, 86_400.0)
            spin.setDecimals(0)
            spin.setPrefix(prefix)
            spin.setSuffix(" s")
            spin.setValue(value)
        return self._mode_row(
            self.excitation_mode_combo, self.excitation_on_spin, self.excitation_off_spin
        )

    def _mode_row(self, mode_combo: QComboBox, *details: QWidget) -> QWidget:
        row = QWidget()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(mode_combo, stretch=1)
        for widget in details:
            layout.addWidget(widget)
        mode_combo.currentIndexChanged.connect(self._apply_recording_modes)
        return row

    def _apply_recording_modes(self, *_args: object) -> None:
        start = self.start_mode_combo.currentData()
        self.start_delay_spin.setVisible(start == "delayed")
        self.start_ttl_line_combo.setVisible(start == "ttl")
        self.start_ttl_edge_combo.setVisible(start == "ttl")
        sequential = self.excitation_mode_combo.currentData() == "sequential"
        self.excitation_on_spin.setVisible(sequential)
        self.excitation_off_spin.setVisible(sequential)
        stop = self.stop_mode_combo.currentData()
        self.stop_ttl_line_combo.setVisible(stop == "ttl")
        self.stop_ttl_edge_combo.setVisible(stop == "ttl")
        self.duration_spin.setEnabled(stop != "manual")
        self._duration_label.setText("Maximum length" if stop == "ttl" else "Duration")
        self.duration_spin.setToolTip(
            "Runs until you press Stop, for at most 24 hours"
            if stop == "manual"
            else "The recording ends here even if the stop TTL never arrives"
            if stop == "ttl"
            else "How long the recording runs"
        )

    def _recording_from_controls(self) -> RecordingControlConfig:
        return RecordingControlConfig(
            start=self.start_mode_combo.currentData(),
            start_delay_s=self.start_delay_spin.value(),
            start_ttl_line=self.start_ttl_line_combo.currentData(),
            start_ttl_edge=self.start_ttl_edge_combo.currentData(),
            stop=self.stop_mode_combo.currentData(),
            stop_ttl_line=self.stop_ttl_line_combo.currentData(),
            stop_ttl_edge=self.stop_ttl_edge_combo.currentData(),
            excitation=self.excitation_mode_combo.currentData(),
            excitation_on_s=self.excitation_on_spin.value(),
            excitation_off_s=self.excitation_off_spin.value(),
        )

    def _load_recording_controls(self, recording: RecordingControlConfig) -> None:
        for combo, value in (
            (self.start_mode_combo, recording.start),
            (self.start_ttl_line_combo, recording.start_ttl_line),
            (self.start_ttl_edge_combo, recording.start_ttl_edge),
            (self.stop_mode_combo, recording.stop),
            (self.stop_ttl_line_combo, recording.stop_ttl_line),
            (self.stop_ttl_edge_combo, recording.stop_ttl_edge),
            (self.excitation_mode_combo, recording.excitation),
        ):
            combo.setCurrentIndex(combo.findData(value))
        self.start_delay_spin.setValue(recording.start_delay_s)
        self.excitation_on_spin.setValue(recording.excitation_on_s)
        self.excitation_off_spin.setValue(recording.excitation_off_s)
        self._apply_recording_modes()

    def _browse_output(self) -> None:
        selected = QFileDialog.getExistingDirectory(
            self, "Select output directory", self.output_edit.text()
        )
        if selected:
            self.output_edit.setText(selected)

    def _save_settings_json(self) -> None:
        try:
            config = self._build_config()
            root = settings_root()
            root.mkdir(parents=True, exist_ok=True)
        except (OSError, ValueError) as error:
            QMessageBox.warning(self, "Could not save settings", str(error))
            return
        suggested = root / f"dbf{SETTINGS_SUFFIX}"
        path, _ = QFileDialog.getSaveFileName(
            self,
            "Save all settings",
            str(suggested),
            "DBF settings (*.json)",
        )
        if not path:
            return
        try:
            written = export_settings(path, config)
        except (OSError, ValueError) as error:
            QMessageBox.warning(self, "Could not save settings", str(error))
            return
        self.statusBar().showMessage(f"Saved all settings to {written}")

    def _load_settings_json(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Load all settings",
            str(settings_root()),
            "DBF settings (*.json)",
        )
        if not path:
            return
        try:
            config, warnings = import_settings_with_warnings(path)
            self._apply_configuration(config)
        except (OSError, ValueError) as error:
            QMessageBox.warning(self, "Could not load settings", str(error))
            return
        self.statusBar().showMessage(f"Loaded all settings from {path}")
        if warnings:
            QMessageBox.warning(
                self,
                "Older settings migrated",
                "\n\n".join(warnings),
            )

    def _load_settings_nwb(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Load settings from a DBF recording",
            self.output_edit.text(),
            "NWB files (*.nwb)",
        )
        if not path:
            return
        try:
            config, warnings = configuration_from_nwb(path)
            self._apply_configuration(config)
        except (OSError, ValueError, KeyError) as error:
            QMessageBox.warning(self, "Could not load settings from NWB", str(error))
            return
        self.statusBar().showMessage(f"Loaded settings from {path}")
        if warnings:
            QMessageBox.warning(
                self,
                "Legacy settings restored with defaults",
                "\n\n".join(warnings),
            )

    def _save_as_default_settings(self) -> None:
        path = default_settings_path()
        answer = QMessageBox.question(
            self,
            "Set as default configuration",
            f"Save every current setting to\n{path}\n\nand load it whenever DBF starts?",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            written = save_default_settings(self._build_config())
        except (OSError, ValueError) as error:
            QMessageBox.warning(self, "Could not save default settings", str(error))
            return
        self.statusBar().showMessage(f"Saved default settings to {written}")

    def _load_default_settings_at_startup(self) -> None:
        try:
            config, warnings = load_default_settings_with_warnings()
            if config is None:
                # No saved settings: start on the application's default system.
                index = self.source_combo.findData(self._default_system)
                if index >= 0:
                    self.source_combo.setCurrentIndex(index)
                return
            self._apply_configuration(config)
        except (OSError, ValueError) as error:
            self.statusBar().showMessage(
                f"Default settings could not be loaded; using built-ins: {error}"
            )
            return
        if warnings:
            self.statusBar().showMessage(warnings[0])
        else:
            self.statusBar().showMessage(f"Loaded defaults from {default_settings_path()}")

    def _apply_configuration(self, config: SessionConfig) -> None:
        """Push one validated settings snapshot into every corresponding GUI control."""

        self._session_template = config
        self._active_config = config
        source_blocker = QSignalBlocker(self.source_combo)
        source_kind = "rwd" if isinstance(config.source, RWDSourceConfig) else "custom"
        self.source_combo.setCurrentIndex(self.source_combo.findData(source_kind))
        del source_blocker
        self.duration_spin.setValue(config.recording_duration_s)
        self._load_recording_controls(config.recording)
        self._load_event_controls(config)
        self.experimenter_edit.setText(config.experimenter)
        self.session_description_edit.setText(config.session_description)
        self.lab_edit.setText(config.lab or "")
        self.institution_edit.setText(config.institution or "")
        self.output_edit.setText(str(config.output_directory))
        self.raw_checkbox.setChecked(config.camera.raw_capture)

        profile = profile_for(config.source)
        if profile is CUSTOM_PROFILE:
            self._custom_camera = config.camera
        if isinstance(config.source, RWDSourceConfig):
            self._apply_rwd_source(config.source)
        self._configure_camera_controls(profile)
        self._load_camera_controls(config.camera)
        checks, sliders, _group = self._channel_controls[profile.kind]
        scale = self._intensity_scale(profile)
        configured_channels = {channel.wavelength_nm: channel for channel in config.channels}
        for wavelength in profile.wavelengths:
            channel = configured_channels.get(wavelength)
            checks[wavelength].setChecked(channel.enabled if channel else False)
            if channel is None:
                value = 5.0 if profile.intensity_unit == "%" else 1.0
            elif profile.intensity_unit == "%":
                value = channel.power_percent or 0.0
            else:
                value = channel.voltage_v or 0.0
            sliders[wavelength].setValue(round(value * scale))

        blocker = QSignalBlocker(self.fiber_count_spin)
        self.fiber_count_spin.setValue(len(config.rois))
        del blocker
        self._rebuild_roi_metadata(len(config.rois))
        for editor, roi in zip(self._roi_metadata_editors, config.rois, strict=True):
            editor.restore(
                {
                    "enabled": roi.enabled,
                    "label": roi.label,
                    "animal_id": roi.animal_id,
                    "brain_region": roi.brain_region,
                    "sensor_type": roi.sensor_type,
                    "subject_age": roi.subject_age or "",
                    "subject_sex": roi.subject_sex,
                }
            )
        self._set_roi_items(config.rois, config.camera)

        display = config.display
        self._set_trace_horizon(display.horizon_s)
        visible = set(display.visible_wavelengths)
        for wavelength, check in self.trace_wavelength_checks.items():
            check.setChecked(wavelength in visible)
        mode_index = self.trace_mode_combo.findData(display.mode)
        self.trace_mode_combo.setCurrentIndex(max(0, mode_index))
        self.trace_baseline_spin.setValue(display.dff_baseline_s)
        self.trace_smoothing_spin.setValue(display.smoothing_window_s)
        self._apply_trace_mode()
        self._apply_source_mode()
        self._update_overview()
        self._invalidate_preview()

    def _rebuild_roi_metadata(self, fiber_count: int) -> None:
        if not hasattr(self, "roi_metadata_tabs"):
            return
        existing = [editor.values() for editor in self._roi_metadata_editors]
        selected = min(self.roi_metadata_tabs.currentIndex(), fiber_count - 1)
        while self.roi_metadata_tabs.count():
            page = self.roi_metadata_tabs.widget(0)
            self.roi_metadata_tabs.removeTab(0)
            page.deleteLater()
        self._roi_metadata_editors = []
        for index in range(fiber_count):
            editor = ROIMetadataEditor(index)
            if index < len(existing):
                editor.restore(existing[index])
            self._roi_metadata_editors.append(editor)
            editor.enabled_check.toggled.connect(
                lambda checked, roi_index=index: self._on_roi_enabled_changed(roi_index, checked)
            )
            for edit in (
                editor.label_edit,
                editor.animal_id_edit,
                editor.brain_region_edit,
                editor.sensor_type_edit,
                editor.age_edit,
            ):
                edit.textChanged.connect(self._invalidate_preview)
            editor.sex_combo.currentIndexChanged.connect(self._invalidate_preview)
            for camera, spin in editor.diameter_spins.items():
                spin.valueChanged.connect(
                    lambda value, roi_index=index, cam=camera: self._on_diameter_entered(
                        roi_index, cam, value
                    )
                )
            suffix = "" if editor.enabled_check.isChecked() else " (off)"
            self.roi_metadata_tabs.addTab(editor, f"ROI {index + 1}{suffix}")
        self.roi_metadata_tabs.setCurrentIndex(max(0, selected))
        self._remove_spin_box_buttons()

    def _on_roi_enabled_changed(self, index: int, checked: bool) -> None:
        self._invalidate_preview()
        if index < self.roi_metadata_tabs.count():
            suffix = "" if checked else " (off)"
            self.roi_metadata_tabs.setTabText(index, f"ROI {index + 1}{suffix}")
        for items in (self._roi_items, self._roi_items_cam2):
            if index < len(items):
                items[index].setOpacity(1.0 if checked else 0.25)
        try:
            rois = self._roi_configs_from_controls()
        except ValueError:
            pass
        else:
            self._configure_trace_plots(tuple(roi for roi in rois if roi.enabled))
        self._update_roi_summary()
        self._update_overview()

    def _rebuild_roi_items(self) -> None:
        if not hasattr(self, "calibration_plot"):
            return
        self._invalidate_preview()
        self._rebuild_roi_metadata(self.fiber_count_spin.value())
        camera = self._session_template.camera
        preview = demo_config(
            Path(self.output_edit.text() or "."),
            fiber_count=self.fiber_count_spin.value(),
            width_px=camera.width_px,
            height_px=camera.height_px,
        )
        preview_rois = tuple(
            roi.model_copy(update={"enabled": editor.enabled_check.isChecked()})
            for roi, editor in zip(preview.rois, self._roi_metadata_editors, strict=True)
        )
        self._set_roi_items(preview_rois, camera)
        self._update_overview()

    def _set_roi_items(
        self,
        rois: tuple[ROIConfig, ...],
        camera: CameraConfig,
    ) -> None:
        for item in self._roi_items:
            self.calibration_plot.removeItem(item)
        for item in self._roi_items_cam2:
            self.calibration_plot_cam2.removeItem(item)
        self._roi_items.clear()
        self._roi_items_cam2.clear()
        self._last_camera_images.clear()
        self._configure_trace_plots(tuple(roi for roi in rois if roi.enabled))
        blank = np.zeros((camera.height_px, camera.width_px), dtype=np.uint16)
        for image_item in self._calibration_images.values():
            image_item.setImage(
                blank,
                autoLevels=False,
                levels=(0, (1 << camera.bit_depth) - 1),
            )
        for index, roi in enumerate(rois):
            circle = pg.CircleROI(
                [roi.center_x_px - roi.radius_px, roi.center_y_px - roi.radius_px],
                [2 * roi.radius_px, 2 * roi.radius_px],
                pen=pg.mkPen(_FIBER_COLORS[index], width=2),
                hoverPen=pg.mkPen("#ffffff", width=2),
                movable=True,
                rotatable=False,
                resizable=True,
            )
            circle.setZValue(10)
            circle.setOpacity(1.0 if roi.enabled else 0.25)
            circle.sigRegionChangeFinished.connect(self._on_roi_geometry_changed)
            self.calibration_plot.addItem(circle)
            self._roi_items.append(circle)
        for plot in (self.calibration_plot, self.calibration_plot_cam2):
            plot.setXRange(0, camera.width_px, padding=0.02)
            plot.setYRange(0, camera.height_px, padding=0.02)
        if self._active_profile.camera_count > 1:
            self._add_camera2_items(rois)
        self._enforce_diameter_limits()
        self._sync_diameter_controls()
        self._update_roi_summary()

    def _add_camera2_items(self, rois: tuple[ROIConfig, ...]) -> None:
        for index, roi in enumerate(rois):
            if roi.has_camera2_geometry:
                x = float(roi.camera2_center_x_px or 0.0)
                y = float(roi.camera2_center_y_px or 0.0)
                radius = float(roi.camera2_radius_px or 0.0)
            else:
                x, y, radius = roi.center_x_px, roi.center_y_px, roi.radius_px
            circle = pg.CircleROI(
                [x - radius, y - radius],
                [2 * radius, 2 * radius],
                pen=pg.mkPen(_FIBER_COLORS[index], width=2, style=Qt.PenStyle.DashLine),
                hoverPen=pg.mkPen("#ffffff", width=2),
                movable=True,
                rotatable=False,
                resizable=True,
            )
            circle.setZValue(10)
            circle.setOpacity(1.0 if roi.enabled else 0.25)
            circle.sigRegionChangeFinished.connect(self._on_roi_geometry_changed)
            self.calibration_plot_cam2.addItem(circle)
            self._roi_items_cam2.append(circle)

    def _sync_camera2_items(self) -> None:
        """Keep camera-2 circles exactly when the active system has a second camera."""

        if not hasattr(self, "calibration_plot") or not self._roi_items:
            return
        wanted = self._active_profile.camera_count > 1
        if wanted == bool(self._roi_items_cam2):
            return
        rois = self._roi_configs_from_controls()
        self._set_roi_items(rois, self._session_template.camera)

    def _refresh_calibration_images(self, *_args: object) -> None:
        for camera, image_item in self._calibration_images.items():
            image = self._last_camera_images.get(camera)
            if image is not None:
                image_item.setImage(image, autoLevels=False, levels=self._display_levels(image))

    def _show_calibration_frame(self, camera: int, image: np.ndarray) -> None:
        image_item = self._calibration_images.get(camera)
        if image_item is not None:
            image_item.setImage(image, autoLevels=False, levels=self._display_levels(image))

    def _display_levels(self, image: np.ndarray) -> tuple[float, float]:
        """Display levels: fixed full scale, or a robust stretch when auto contrast is on."""

        if (
            not getattr(self, "auto_contrast_check", None)
            or not self.auto_contrast_check.isChecked()
        ):
            return (0.0, float(self._image_display_maximum))
        sample = image[::4, ::4]
        low, high = (float(value) for value in np.percentile(sample, (0.5, 99.9)))
        if high <= low:
            high = low + 1.0
        return (low, high)

    def _on_roi_geometry_changed(self) -> None:
        self._enforce_diameter_limits()
        self._sync_diameter_controls()
        self._update_roi_summary()
        self._invalidate_preview()

    def _diameter_limits(self) -> tuple[float, float]:
        if self._active_profile is RWD_PROFILE:
            return _RWD_ROI_DIAMETER_PX
        camera = self._session_template.camera
        return 4.0, float(min(camera.width_px, camera.height_px))

    def _circles_for(self, camera: int) -> list[pg.CircleROI]:
        return self._roi_items if camera == 1 else self._roi_items_cam2

    def _sync_diameter_controls(self) -> None:
        """Show each circle's diameter in its ROI tab (display only, no feedback loop)."""

        low, high = self._diameter_limits()
        two_cameras = bool(self._roi_items_cam2)
        for index, editor in enumerate(getattr(self, "_roi_metadata_editors", ())):
            editor.set_camera2_visible(two_cameras)
            for camera, spin in editor.diameter_spins.items():
                circles = self._circles_for(camera)
                blocker = QSignalBlocker(spin)
                spin.setRange(low, high)
                if index < len(circles):
                    spin.setValue(2 * self._circle_geometry(circles[index])[2])
                del blocker

    def _on_diameter_entered(self, index: int, camera: int, diameter: float) -> None:
        circles = self._circles_for(camera)
        if index >= len(circles):
            return
        circle = circles[index]
        center_x, center_y, _radius = self._circle_geometry(circle)
        radius = diameter / 2
        circle.setPos([center_x - radius, center_y - radius], update=False, finish=False)
        circle.setSize([2 * radius, 2 * radius], finish=True)

    def _enforce_diameter_limits(self) -> None:
        low, high = self._diameter_limits()
        for camera in (1, 2):
            for circle in self._circles_for(camera):
                center_x, center_y, radius = self._circle_geometry(circle)
                diameter = min(max(2 * radius, low), high)
                if abs(diameter - 2 * radius) > 1e-6:
                    radius = diameter / 2
                    circle.setPos(
                        [center_x - radius, center_y - radius], update=False, finish=False
                    )
                    circle.setSize([2 * radius, 2 * radius], finish=False)

    def _update_roi_summary(self) -> None:
        rows = []
        for index, item in enumerate(self._roi_items):
            position = item.pos()
            size = item.size()
            radius = min(float(size.x()), float(size.y())) / 2.0
            center_x = float(position.x()) + radius
            center_y = float(position.y()) + radius
            enabled = self._roi_metadata_editors[index].enabled_check.isChecked()
            state = "enabled" if enabled else "disabled"
            row = (
                f'<span style="color:{_FIBER_COLORS[index]}">●</span> '
                f"Fiber {index + 1} ({state}): x {center_x:.1f}, y {center_y:.1f}, "
                f"r {radius:.1f} px"
            )
            if index < len(self._roi_items_cam2):
                x2, y2, r2 = self._circle_geometry(self._roi_items_cam2[index])
                row += f"; camera 2: x {x2:.1f}, y {y2:.1f}, r {r2:.1f} px"
            rows.append(row)
        self.roi_summary.setText("<br>".join(rows))

    @staticmethod
    def _circle_geometry(item: pg.CircleROI) -> tuple[float, float, float]:
        position = item.pos()
        size = item.size()
        radius = min(float(size.x()), float(size.y())) / 2.0
        return float(position.x()) + radius, float(position.y()) + radius, radius

    def _roi_configs_from_controls(self) -> tuple[ROIConfig, ...]:
        rois: list[ROIConfig] = []
        used_fiber_ids: set[str] = set()
        template_rois = {roi.fiber_id: roi for roi in self._session_template.rois}
        two_cameras = self._active_profile.camera_count > 1
        for index, item in enumerate(self._roi_items):
            metadata = self._roi_metadata_editors[index]
            position = item.pos()
            size = item.size()
            radius = min(float(size.x()), float(size.y())) / 2.0
            fiber_id = (
                self._session_template.rois[index].fiber_id
                if index < len(self._session_template.rois)
                else f"fiber_{index + 1:02d}"
            )
            if fiber_id in used_fiber_ids:
                suffix = 2
                base = f"fiber_{index + 1:02d}"
                fiber_id = base
                while fiber_id in used_fiber_ids:
                    fiber_id = f"{base}_{suffix}"
                    suffix += 1
            used_fiber_ids.add(fiber_id)
            center_x = float(position.x()) + radius
            center_y = float(position.y()) + radius
            camera2: dict[str, float] = {}
            template_roi = template_rois.get(fiber_id)
            if two_cameras and index < len(self._roi_items_cam2):
                x2, y2, r2 = self._circle_geometry(self._roi_items_cam2[index])
                camera2 = {
                    "camera2_center_x_px": x2,
                    "camera2_center_y_px": y2,
                    "camera2_radius_px": r2,
                }
            elif two_cameras and template_roi is not None and template_roi.has_camera2_geometry:
                camera2 = {
                    "camera2_center_x_px": float(template_roi.camera2_center_x_px or 0.0),
                    "camera2_center_y_px": float(template_roi.camera2_center_y_px or 0.0),
                    "camera2_radius_px": float(template_roi.camera2_radius_px or 0.0),
                }
            elif two_cameras:
                camera2 = {
                    "camera2_center_x_px": center_x,
                    "camera2_center_y_px": center_y,
                    "camera2_radius_px": radius,
                }
            rois.append(
                ROIConfig(
                    fiber_id=fiber_id,
                    enabled=metadata.enabled_check.isChecked(),
                    label=metadata.label_edit.text().strip(),
                    animal_id=metadata.animal_id_edit.text().strip(),
                    brain_region=metadata.brain_region_edit.text().strip(),
                    sensor_type=metadata.sensor_type_edit.text().strip(),
                    subject_age=metadata.age_edit.text().strip() or None,
                    subject_sex=metadata.sex_combo.currentText(),
                    center_x_px=center_x,
                    center_y_px=center_y,
                    radius_px=radius,
                    **camera2,
                )
            )
        return tuple(rois)

    def _build_config(self) -> SessionConfig:
        output = Path(self.output_edit.text()).expanduser()
        profile = self._active_profile
        checks, sliders, _group = self._channel_controls[profile.kind]
        scale = self._intensity_scale(profile)
        percent = profile.intensity_unit == "%"
        template_channels = {
            channel.wavelength_nm: channel for channel in self._session_template.channels
        }
        channels = tuple(
            ChannelConfig(
                wavelength_nm=wavelength,
                enabled=checks[wavelength].isChecked(),
                voltage_v=None if percent else sliders[wavelength].value() / scale,
                power_percent=sliders[wavelength].value() / scale if percent else None,
                role=template_channels[wavelength].role
                if wavelength in template_channels
                else None,
                emission_wavelength_nm=template_channels[wavelength].emission_wavelength_nm
                if wavelength in template_channels
                else None,
            )
            for wavelength in profile.wavelengths
        )
        rois = self._roi_configs_from_controls()
        visible_wavelengths = tuple(
            wavelength
            for wavelength, check in self.trace_wavelength_checks.items()
            if wavelength in self._active_trace_wavelengths and check.isChecked()
        )
        display = TraceDisplayConfig(
            horizon_s=self._trace_horizon_s,
            visible_wavelengths=visible_wavelengths,
            mode=self.trace_mode_combo.currentData(),
            dff_baseline_s=self.trace_baseline_spin.value(),
            smoothing_window_s=self.trace_smoothing_spin.value(),
        )
        source: CustomSourceConfig | RWDSourceConfig
        if self._is_rwd_mode():
            source = self._rwd_source_from_controls()
            session_prefix = "rwd"
        else:
            source = CustomSourceConfig()
            session_prefix = "demo"
        started = datetime.now(UTC)
        payload = self._session_template.model_dump()
        payload.update(
            session_id=started.strftime(f"{session_prefix}-%Y%m%dT%H%M%S-%fZ"),
            session_description=self.session_description_edit.text().strip(),
            experimenter=self.experimenter_edit.text().strip(),
            output_directory=output,
            recording_duration_s=self.duration_spin.value(),
            session_start_time=started,
            source=source,
            camera=self._camera_from_controls(),
            channels=channels,
            rois=rois,
            lab=self.lab_edit.text().strip() or None,
            institution=self.institution_edit.text().strip() or None,
            display=display,
            recording=self._recording_from_controls(),
            ttl_inputs=self._ttl_inputs_from_controls(),
            event_markers=self._markers_from_controls(),
            ttl_events=self._ttl_events_from_controls(),
        )
        return SessionConfig.model_validate(payload)

    def start_preview(self) -> None:
        if self._thread is not None:
            return
        try:
            config = self._build_config()
        except Exception as error:
            QMessageBox.critical(self, "Invalid configuration", str(error))
            return
        self.last_preview = None
        self.last_error = None
        self._operation_kind = "preview"
        self._operation_uses_leds = isinstance(config.source, RWDSourceConfig)
        self._pending_preview_signature = self._configuration_signature(config)
        self._prepare_trace_curves(config)
        self._prepare_wavelength_images(config)
        self.progress_bar.setValue(0)
        self.progress_bar.setFormat("0%")
        self.frame_counter.setText("0 preview frames")
        self.live_status.setText("Running acquisition preflight...")
        self._set_preview_status(
            "Preview running - set LED powers and inspect the signal, then press Stop "
            f"preview; it stops by itself after {PREVIEW_LIMIT_S / 60:g} minutes",
            "running",
        )
        self._set_system_status("PREVIEW", "preview")
        self.statusBar().showMessage("Running non-recording acquisition preview...")

        self._camera_frame_counts = {}
        thread = QThread(self)
        try:
            worker = PreviewWorker(
                config,
                self._rig_factory,
                duration_s=PREVIEW_LIMIT_S,
                allow_power_changes=True,
            )
        except Exception as error:
            self._on_preview_failed(str(error))
            return
        self._progress_duration_s = worker.duration_s
        self._preview_started_config = config
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.progress.connect(self._on_progress)
        worker.completed.connect(self._on_preview_completed)
        worker.failed.connect(self._on_preview_failed)
        worker.completed.connect(thread.quit)
        worker.failed.connect(thread.quit)
        thread.finished.connect(self._on_thread_finished)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        self._thread = thread
        self._worker = worker
        self._set_busy_controls(True)
        thread.start()

    def start_recording(self) -> None:
        if self._thread is not None:
            return
        try:
            config = self._build_config()
        except Exception as error:
            QMessageBox.critical(self, "Invalid configuration", str(error))
            return
        if self._preview_signature != self._configuration_signature(config):
            self._invalidate_preview()
            QMessageBox.warning(
                self,
                "Preview required",
                "Run a successful preview for the current settings before recording.",
            )
            return
        if isinstance(config.source, RWDSourceConfig) and not self._confirm_warmup():
            return
        self._preview_signature = None
        self._pending_preview_signature = None
        self._operation_kind = "recording"
        self._operation_uses_leds = isinstance(config.source, RWDSourceConfig)
        self.last_result = None
        self.last_error = None
        self.connection_diagnostics_label.setText(
            "Waiting for the first connection/source diagnostic"
        )
        self._prepare_trace_curves(config)
        self._prepare_wavelength_images(config)
        self.progress_bar.setValue(0)
        self.progress_bar.setFormat("0%")
        self.frame_counter.setText("0 frames")
        arming = (
            "Connecting to the RWD controller and cameras..."
            if isinstance(config.source, RWDSourceConfig)
            else "Arming simulator..."
        )
        self.live_status.setText(arming)
        self._set_preview_status(
            "Preview passed for these settings - recording in progress",
            "passed",
        )
        self._set_system_status("STARTING...", "recording")
        self.statusBar().showMessage(arming)

        self._camera_frame_counts = {}
        thread = QThread(self)
        worker = AcquisitionWorker(config, config.recording_limit_s, self._rig_factory)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.progress.connect(self._on_progress)
        worker.finalization_progress.connect(self._on_finalization_progress)
        worker.state_changed.connect(self._on_state)
        worker.completed.connect(self._on_completed)
        worker.failed.connect(self._on_failed)
        worker.cancelled.connect(self._on_cancelled)
        worker.completed.connect(thread.quit)
        worker.failed.connect(thread.quit)
        worker.cancelled.connect(thread.quit)
        thread.finished.connect(self._on_thread_finished)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        self._thread = thread
        self._worker = worker
        self._progress_duration_s = config.recording_limit_s
        self._set_busy_controls(True)
        thread.start()

    def _toggle_preview(self) -> None:
        if self._operation_kind == "preview" and self._worker is not None:
            self.stop_recording()
        else:
            self.start_preview()

    def _toggle_live_view(self) -> None:
        if self._operation_kind == "live" and self._worker is not None:
            self.stop_recording()
        else:
            self.start_live_view()

    def start_live_view(self) -> None:
        if self._thread is not None:
            return
        try:
            config = self._build_config()
        except Exception as error:
            QMessageBox.critical(self, "Invalid configuration", str(error))
            return
        self.last_error = None
        self._operation_kind = "live"
        self._operation_uses_leds = isinstance(config.source, RWDSourceConfig)
        self._camera_frame_counts = {}
        self._prepare_wavelength_images(config)
        self.frame_counter.setText("0 live frames")
        self.live_status.setText(
            "Live view - connecting to the RWD controller and cameras..."
            if isinstance(config.source, RWDSourceConfig)
            else "Live view - starting the simulator..."
        )
        self._set_system_status("LIVE VIEW", "preview")
        self.statusBar().showMessage(
            f"Live view - nothing is recorded; it stops by itself after "
            f"{LIVE_VIEW_LIMIT_S / 60:g} minutes"
        )
        thread = QThread(self)
        try:
            worker = LiveViewWorker(config, self._rig_factory)
        except Exception as error:
            self._on_live_failed(str(error))
            return
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.frame.connect(self._on_live_frame)
        worker.completed.connect(self._on_live_completed)
        worker.failed.connect(self._on_live_failed)
        worker.completed.connect(thread.quit)
        worker.failed.connect(thread.quit)
        thread.finished.connect(self._on_thread_finished)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        self._thread = thread
        self._worker = worker
        self._set_busy_controls(True)
        self.tabs.setCurrentIndex(1)
        thread.start()

    def _confirm_warmup(self) -> bool:
        """Ask before recording with cold LEDs; Live view is the way to warm them up."""

        if not self.warmup_check.isChecked() or self._warmup.is_warm:
            return True
        answer = QMessageBox.question(
            self,
            "LEDs not warmed up",
            f"RWD recommends running the LEDs for "
            f"{format_minutes(self._warmup.required_s)} before recording so their output "
            f"settles; they have been on for {format_minutes(self._warmup.warm_s())}.\n\n"
            "Run Live view to warm them up, with the fiber on its target or capped.\n\n"
            "Record anyway?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        return answer == QMessageBox.StandardButton.Yes

    def _leds_lit(self) -> None:
        if self._operation_uses_leds and not self._warmup.leds_are_on:
            self._warmup.leds_on()
            self._update_warmup_label()

    def _update_warmup_label(self) -> None:
        warm = self._warmup.warm_s()
        required = format_minutes(self._warmup.required_s)
        if self._warmup.is_warm:
            text, state = f"✓ LEDs warmed up (on for {format_minutes(warm)})", "ok"
            if self._warmup.leds_are_on and not self._warmup_announced:
                self._warmup_announced = True
                self.statusBar().showMessage(
                    "The LEDs have warmed up; run the preview, then record"
                )
        elif self._warmup.leds_are_on:
            text, state = f"• Warming up: {format_minutes(warm)} of {required}", "unknown"
            self._warmup_announced = False
        elif warm > 0:
            text, state = (
                f"• Warm-up paused at {format_minutes(warm)} of {required}; it restarts "
                f"after {format_minutes(WARMUP_COOLDOWN_S)} off",
                "unknown",
            )
        else:
            text, state = f"• LEDs cold; run Live view for {required} before recording", ("unknown")
        if self.warmup_label.text() != text:
            self.warmup_label.setText(text)
            self._set_device_state(self.warmup_label, state)

    def _on_live_frame(self, frame: LiveFrame) -> None:
        self._leds_lit()
        self._last_camera_images[frame.camera_index] = frame.image
        self.wavelength_image_items[frame.wavelength_nm].setImage(
            frame.image, autoLevels=False, levels=self._display_levels(frame.image)
        )
        self.wavelength_frame_labels[frame.wavelength_nm].setText(
            f"Live · camera {frame.camera_index} · {frame.timestamp_s:.1f} s"
        )
        self._show_calibration_frame(frame.camera_index, frame.image)
        self.frame_counter.setText(f"{frame.frame_count:,} live frames")
        self.live_status.setText(
            f"Live view - camera {frame.camera_index}, {int(frame.wavelength_nm)} nm"
        )

    def _on_live_completed(self, frame_count: int) -> None:
        self.live_status.setText(f"Live view stopped after {frame_count:,} frames")
        self._set_system_status("READY", "ready")
        self.statusBar().showMessage("Live view stopped; nothing was recorded")

    def _on_live_failed(self, traceback_text: str) -> None:
        self.last_error = traceback_text
        self.live_status.setText("Live view failed")
        self._set_system_status("ERROR", "error")
        self.statusBar().showMessage("Live view failed; nothing was recorded")
        if not self._close_pending:
            QMessageBox.critical(self, "Live view failed", traceback_text)

    def stop_recording(self) -> None:
        if self._worker is not None:
            operation = {"preview": "preview", "live": "live view"}.get(
                self._operation_kind or "", "recording"
            )
            self.live_status.setText(f"Stopping {operation} after the current frame...")
            self.statusBar().showMessage(f"Stopping {operation} after the current frame...")
            self.stop_button.setEnabled(False)
            self._worker.request_stop()

    def _prepare_trace_curves(self, config: SessionConfig) -> None:
        self._active_config = config
        self._event_marks_shown = []
        self._configure_trace_plots(config.enabled_rois)
        self._active_trace_wavelengths = tuple(
            int(channel.wavelength_nm) for channel in config.enabled_channels
        )
        self._trace_roi_indices = {
            roi.fiber_id: index for index, roi in enumerate(config.enabled_rois)
        }
        self._trace_times.clear()
        self._trace_values.clear()
        self._trace_horizon_s = HORIZONS[0][1]
        self._trace_manual_x_range = None
        for seconds, button in self.horizon_buttons.items():
            button.setChecked(seconds == self._trace_horizon_s)
        self._trace_first_s = None
        self._trace_latest_s = None
        self._last_trace_render_s = {
            wavelength: -np.inf for wavelength in self._active_trace_wavelengths
        }
        for index, _roi in enumerate(config.enabled_rois):
            for wavelength in self._active_trace_wavelengths:
                curve = self.trace_plots[index].plot(
                    name=f"{wavelength} nm" if index == 0 else None,
                    pen=pg.mkPen(_WAVELENGTH_COLORS[wavelength], width=1.8),
                )
                curve.setVisible(self.trace_wavelength_checks[wavelength].isChecked())
                self._curves[(wavelength, index)] = curve
                self._trace_times[(wavelength, index)] = array("d")
                self._trace_values[(wavelength, index)] = array("f")
        self._update_trace_horizon_availability(0.0)

    def _set_trace_horizon(self, seconds: float | None) -> None:
        self._trace_horizon_s = seconds
        self._trace_manual_x_range = None
        for value, button in self.horizon_buttons.items():
            button.setChecked(value == seconds)
        for plot in self.trace_plots.values():
            plot.enableAutoRange(axis="y", enable=True)
        self._refresh_all_trace_curves()

    def _set_trace_horizon_from_button_id(self, button_id: int) -> None:
        if not 0 <= button_id < len(HORIZONS):
            raise ValueError(f"unknown trace horizon button ID {button_id}")
        _label, seconds = HORIZONS[button_id]
        self._set_trace_horizon(seconds)

    def _on_trace_range_selected(
        self,
        axis: str,
        low: float,
        high: float,
    ) -> None:
        if axis != "x" or high <= low:
            return
        self._trace_manual_x_range = (low, high)
        self._set_all_trace_x_ranges(low, high)
        self._refresh_all_trace_curves()

    def _on_trace_range_changed_manually(self, row: int, mask: Sequence[bool]) -> None:
        if not bool(mask[0]):
            return
        low, high = self.trace_plots[row].viewRange()[0]
        self._trace_manual_x_range = (low, high)
        self._set_all_trace_x_ranges(low, high)

    def _set_all_trace_x_ranges(self, low: float, high: float) -> None:
        for plot in self.trace_plots.values():
            plot.setXRange(low, high, padding=0.0)

    def _reset_trace_zoom(self) -> None:
        self._trace_manual_x_range = None
        for plot in self.trace_plots.values():
            plot.enableAutoRange(axis="y", enable=True)
        self._refresh_all_trace_curves()

    def _apply_trace_visibility(self, *_args: object) -> None:
        for (wavelength, _index), curve in self._curves.items():
            curve.setVisible(self.trace_wavelength_checks[wavelength].isChecked())

    def _apply_trace_mode(self, *_args: object) -> None:
        mode = self.trace_mode_combo.currentData()
        is_dff = mode in _DFF_MODES
        self.trace_baseline_spin.setEnabled(is_dff)
        self.trace_baseline_hint.setEnabled(is_dff)
        self.trace_baseline_hint.setText(
            "F0: control fitted to each signal; control: median"
            if mode == "dff_fit"
            else "median F0 per ROI and wavelength"
        )
        for plot in self.trace_plots.values():
            if is_dff:
                plot.setLabel("left", "dF/F", units="%")
            else:
                plot.setLabel("left", "Fluorescence", units="counts")
            plot.enableAutoRange(axis="y", enable=True)
        self._refresh_all_trace_curves()

    def _update_trace_horizon_availability(self, duration_s: float) -> None:
        operation = "preview" if self._operation_kind == "preview" else "recording"
        self.trace_span_hint.setText(f"{compact_duration(duration_s)} available (this {operation})")
        for label, seconds in HORIZONS:
            button = self.horizon_buttons[seconds]
            available = seconds is None or seconds <= duration_s or seconds == self._trace_horizon_s
            button.setEnabled(available)
            button.setToolTip(
                ""
                if available
                else f"{label} needs {seconds:g} s of data; {duration_s:g} s is available."
            )

    def _trace_point_budget(self) -> int:
        plot = next(iter(self.trace_plots.values()), None)
        if plot is None:
            return 256
        ratio = self.devicePixelRatioF() or 1.0
        return min(MAX_DISPLAY_POINTS, max(256, int(plot.width() * ratio * 2)))

    def _visible_trace_range(self) -> tuple[float, float] | None:
        if self._trace_first_s is None or self._trace_latest_s is None:
            return None
        stop = max(self._trace_latest_s, self._trace_first_s + 1 / 30)
        start = self._trace_first_s
        if self._trace_horizon_s is not None:
            start = max(start, stop - self._trace_horizon_s)
        return start, stop

    def _refresh_trace_wavelength(self, wavelength: int) -> None:
        visible = self._trace_manual_x_range or self._visible_trace_range()
        if visible is None:
            return
        start_s, stop_s = visible
        budget = self._trace_point_budget()
        for index in range(self._trace_roi_count):
            key = (wavelength, index)
            time_buffer = self._trace_times[key]
            if not time_buffer:
                continue
            times = np.frombuffer(time_buffer, dtype=np.float64)
            first_index = int(np.searchsorted(times, start_s, side="left"))
            stop_index = int(np.searchsorted(times, stop_s, side="right"))
            selected_times = times[first_index:stop_index]
            if first_index >= stop_index:
                self._curves[key].setData([], [])
                continue
            all_values = np.frombuffer(
                self._trace_values[key],
                dtype=np.float32,
            )
            smoothing_window_s = self.trace_smoothing_spin.value()
            if smoothing_window_s > 0:
                smoothing_start_s = times[first_index] - smoothing_window_s
                smoothing_start = int(np.searchsorted(times, smoothing_start_s, side="left"))
                smoothed = trailing_mean(
                    times[smoothing_start:stop_index],
                    all_values[smoothing_start:stop_index],
                    window_s=smoothing_window_s,
                )
                values = smoothed[first_index - smoothing_start :]
            else:
                values = all_values[first_index:stop_index]
            mode = self.trace_mode_combo.currentData()
            control = self._trace_control_wavelength()
            fitted_f0 = None
            if mode == "dff_fit" and control is not None and wavelength != control:
                fitted_f0 = self._fitted_f0(control, index, times, all_values, selected_times)
                if fitted_f0 is None:
                    values = np.full(len(values), np.nan, dtype=np.float64)
                else:
                    with np.errstate(divide="ignore", invalid="ignore"):
                        values = (values.astype(np.float64) - fitted_f0) / fitted_f0 * 100.0
                    values[~np.isfinite(values)] = np.nan
            elif mode in _DFF_MODES:
                baseline_stop_s = times[0] + self.trace_baseline_spin.value()
                baseline_stop = max(1, int(np.searchsorted(times, baseline_stop_s, side="right")))
                baseline_values = (
                    trailing_mean(
                        times[:baseline_stop],
                        all_values[:baseline_stop],
                        window_s=smoothing_window_s,
                    )
                    if smoothing_window_s > 0
                    else all_values[:baseline_stop]
                )
                finite = baseline_values[np.isfinite(baseline_values)]
                baseline = float(np.median(finite)) if len(finite) else np.nan
                if np.isfinite(baseline) and abs(baseline) > np.finfo(np.float32).eps:
                    values = (values.astype(np.float64) - baseline) / baseline * 100.0
                else:
                    values = np.full(len(values), np.nan, dtype=np.float64)
            display_times, display_values = downsample_min_max(
                selected_times,
                values,
                first_index=first_index,
                max_points=budget,
            )
            self._curves[key].setData(
                display_times,
                display_values,
                connect="finite",
            )
        self._set_all_trace_x_ranges(start_s, stop_s)

    def _trace_control_wavelength(self) -> int | None:
        """The isosbestic control being displayed: 410 nm on RWD, 405 nm on Custom."""

        for control in (int(Wavelength.CONTROL_410), int(Wavelength.CONTROL_405)):
            if control in self._active_trace_wavelengths:
                return control
        return None

    def _fitted_f0(
        self,
        control: int,
        index: int,
        times: np.ndarray,
        values: np.ndarray,
        at_times: np.ndarray,
    ) -> np.ndarray | None:
        key = (control, index)
        control_times = self._trace_times.get(key)
        if not control_times:
            return None
        return fitted_control_f0(
            times,
            values,
            np.frombuffer(control_times, dtype=np.float64),
            np.frombuffer(self._trace_values[key], dtype=np.float32),
            at_times,
        )

    def _refresh_all_trace_curves(self) -> None:
        for wavelength in self._active_trace_wavelengths:
            self._refresh_trace_wavelength(wavelength)

    def _prepare_wavelength_images(self, config: SessionConfig) -> None:
        enabled = {channel.wavelength_nm for channel in config.enabled_channels}
        self._image_display_maximum = (1 << config.camera.bit_depth) - 1
        blank = np.zeros((config.camera.height_px, config.camera.width_px), dtype=np.uint16)
        for wavelength in Wavelength:
            self.wavelength_image_items[wavelength].setImage(
                blank,
                autoLevels=False,
                levels=(0, self._image_display_maximum),
            )
            plot = self.wavelength_image_plots[wavelength]
            plot.setXRange(0, config.camera.width_px, padding=0.02)
            plot.setYRange(0, config.camera.height_px, padding=0.02)
            self.wavelength_frame_labels[wavelength].setText(
                "Waiting for first frame" if wavelength in enabled else "Channel disabled"
            )

    def _append_live_trace_points(self, points: Sequence[LiveTracePoint]) -> None:
        affected_wavelengths: set[int] = set()
        for point in points:
            roi_index = self._trace_roi_indices.get(point.fiber_id)
            if roi_index is None:
                raise ValueError(f"live trace references unknown fiber_id {point.fiber_id!r}")
            key = (point.wavelength_nm, roi_index)
            if key not in self._trace_times:
                raise ValueError(
                    f"live trace references inactive wavelength {point.wavelength_nm} nm"
                )
            times = self._trace_times[key]
            if times and point.timestamp_s <= times[-1]:
                raise ValueError("live trace timestamps must increase per fiber/wavelength")
            times.append(point.timestamp_s)
            self._trace_values[key].append(point.value)
            self._trace_first_s = (
                point.timestamp_s
                if self._trace_first_s is None
                else min(self._trace_first_s, point.timestamp_s)
            )
            self._trace_latest_s = (
                point.timestamp_s
                if self._trace_latest_s is None
                else max(self._trace_latest_s, point.timestamp_s)
            )
            affected_wavelengths.add(point.wavelength_nm)
        if self._trace_first_s is not None and self._trace_latest_s is not None:
            available_span = self._trace_latest_s - self._trace_first_s + 1 / 30
            self._update_trace_horizon_availability(available_span)
        visible = self._trace_manual_x_range or self._visible_trace_range()
        if visible is not None:
            self._set_all_trace_x_ranges(*visible)
        for wavelength_nm in affected_wavelengths:
            latest = max(
                self._trace_times[(wavelength_nm, index)][-1]
                for index in range(self._trace_roi_count)
                if self._trace_times[(wavelength_nm, index)]
            )
            if latest - self._last_trace_render_s[wavelength_nm] >= _TRACE_REFRESH_INTERVAL_S:
                self._refresh_trace_wavelength(wavelength_nm)
                self._last_trace_render_s[wavelength_nm] = latest

    def _on_progress(self, progress: AcquisitionProgress) -> None:
        self._leds_lit()
        if progress.event_marks:
            self._show_event_marks(progress.event_marks)
        sample = progress.sample
        wavelength = int(sample.wavelength_nm)
        self.wavelength_image_items[sample.wavelength_nm].setImage(
            progress.image,
            autoLevels=False,
            levels=self._display_levels(progress.image),
        )
        self.wavelength_frame_labels[sample.wavelength_nm].setText(
            f"Frame {progress.frame_count:,} · {sample.timestamp_s:.3f} s"
        )
        if self._active_config is None:
            raise RuntimeError("camera progress arrived without an active configuration")
        self._append_live_trace_points(camera_live_trace_points(self._active_config, progress))
        camera_count = self._camera_frame_counts.get(progress.camera_index, 0) + 1
        self._camera_frame_counts[progress.camera_index] = camera_count
        self._last_camera_images[progress.camera_index] = progress.image
        if camera_count == 1 or camera_count % 5 == 0:
            self._show_calibration_frame(progress.camera_index, progress.image)
        fraction = min(1.0, (sample.timestamp_s + 1 / 30) / self._progress_duration_s)
        open_ended = self._operation_kind == "preview" or (
            self._operation_kind == "recording" and self._active_config.recording.stop != "duration"
        )
        operation = "Preview" if self._operation_kind == "preview" else "Recording"
        self.frame_counter.setText(f"{progress.frame_count:,} {operation.lower()} frames")
        self.live_status.setText(f"{operation} - {wavelength} nm exposure")
        self.statusBar().showMessage(f"{operation} - frame {progress.frame_count}, {wavelength} nm")
        if progress.diagnostics is not None:
            spool = progress.diagnostics.spool
            self.connection_diagnostics_label.setText(
                f"Spool queue {spool.queue_depth}/{spool.queue_capacity} · "
                f"{spool.committed_frames:,} committed · "
                f"{progress.diagnostics.dropped_frames:,} dropped"
            )
        progress_value = round(fraction * 1000)
        self.progress_bar.setValue(progress_value)
        if open_ended:
            verb = "previewed" if self._operation_kind == "preview" else "recorded"
            self.progress_bar.setFormat(f"{format_minutes(sample.timestamp_s)} {verb}")
        else:
            self.progress_bar.setFormat(f"{fraction:.0%}")

    def _on_preview_completed(self, result: PreviewResult) -> None:
        self.last_preview = result
        self._pending_preview_signature = None
        # Powers may have changed during the preview; it validates the current settings
        # only if it ended on exactly their powers.
        try:
            current = self._build_config()
        except Exception:
            current = None
        powers_match = current is not None and all(
            abs(
                dict(result.final_power_percent).get(channel.wavelength_nm, -1.0)
                - float(channel.power_percent)
            )
            < 1e-9
            for channel in current.enabled_channels
            if channel.power_percent is not None
        )
        started = self._preview_started_config
        unchanged_otherwise = (
            current is not None
            and started is not None
            and self._signature_without_power(current) == self._signature_without_power(started)
        )
        self._preview_signature = (
            self._configuration_signature(current) if powers_match and unchanged_otherwise else None
        )
        if self._preview_signature is None:
            self._on_preview_superseded(result)
            return
        self._refresh_all_trace_curves()
        self.progress_bar.setValue(1000)
        self.progress_bar.setFormat("100%")
        wavelengths = ", ".join(
            f"{int(wavelength)} nm: {count}" for wavelength, count in result.wavelength_frame_counts
        )
        saturation = result.maximum_saturation_fraction * 100
        self.live_status.setText("Preview passed - recording is enabled")
        self._set_preview_status(
            f"Preview passed: {result.frame_count} frames; {result.roi_count} ROIs; "
            f"{wavelengths}; maximum saturation {saturation:.1f}%",
            "passed",
        )
        self._set_system_status("PREVIEW PASSED", "complete")
        self.statusBar().showMessage(
            "Preview passed for the current settings; recording is now enabled"
        )

    def _on_preview_superseded(self, result: PreviewResult) -> None:
        self._refresh_all_trace_curves()
        self.live_status.setText("Preview stopped - settings changed, so run it again")
        self._set_preview_status(
            f"Preview stopped after {result.frame_count} frames, but settings changed "
            "since it started; run it again before recording",
            "required",
        )
        self._set_system_status("READY", "ready")

    def _signature_without_power(self, config: SessionConfig) -> str:
        channels = tuple(
            channel.model_copy(update={"power_percent": None, "voltage_v": None})
            for channel in config.channels
        )
        return self._configuration_signature(config.model_copy(update={"channels": channels}))

    def _on_power_slider_changed(self, *_args: object) -> None:
        """During preview or live view on RWD, send the new LED powers to the board."""

        if self._operation_kind not in {"preview", "live"} or self._worker is None:
            return
        if not self._is_rwd_mode():
            return
        self._power_timer.start()

    def _send_led_powers(self) -> None:
        worker = self._worker
        request = getattr(worker, "request_led_power", None)
        if request is None or self._operation_kind not in {"preview", "live"}:
            return
        checks, sliders, _group = self._channel_controls[RWD_PROFILE.kind]
        for wavelength, slider in sliders.items():
            if checks[wavelength].isChecked():
                request(wavelength, slider.value() / 10)
        self.statusBar().showMessage("LED power sent; the board restarts its LED cycle")

    def _on_preview_failed(self, traceback_text: str) -> None:
        self.last_error = traceback_text
        self._preview_signature = None
        self._pending_preview_signature = None
        self.live_status.setText("Preview failed - recording remains disabled")
        self._set_preview_status(
            "Preview failed - correct the reported problem and run preview again",
            "failed",
        )
        self._set_system_status("PREVIEW FAILED", "error")
        self.statusBar().showMessage("Preview failed; no recording files were created")
        if not self._close_pending:
            QMessageBox.critical(self, "Preview failed", traceback_text)

    def _on_state(self, state: str) -> None:
        config = self._active_config
        start = StartCondition.for_recording(config) if config is not None else None
        if state == "armed" and start is not None:
            self._set_system_status("WAITING", "preview")
            lights = " with the LEDs off" if isinstance(config.source, RWDSourceConfig) else ""
            waiting = f"Waiting for {start.describe()}{lights}; press Stop to cancel"
            self.live_status.setText(waiting)
            self.statusBar().showMessage(waiting)
        elif state == "recording":
            self._set_system_status("RECORDING", "recording")
        elif state == "draining":
            self._set_system_status("SAVING…", "saving")
            self.live_status.setText("Finalizing and validating NWB…")
            self.statusBar().showMessage("Finalizing and validating NWB…")

    def _on_finalization_progress(self, progress: FinalizationProgress) -> None:
        stage_labels = {
            FinalizationStage.PREPARING: "Preparing NWB file set",
            FinalizationStage.WRITING: "Writing partial NWB",
            FinalizationStage.VALIDATING: "Validating partial NWB",
            FinalizationStage.PROMOTING: "Promoting validated NWB file set",
            FinalizationStage.COMPLETE: "NWB file set complete",
        }
        message = stage_labels[progress.stage]
        if progress.fiber_id is not None:
            message += f" for {progress.fiber_id}"
        message += (
            f" ({progress.completed_roi_files}/{progress.total_roi_files} ROI files complete)"
        )
        self.live_status.setText(message)
        self.statusBar().showMessage(message)

    def _on_completed(self, result: AcquisitionRunResult) -> None:
        self.last_result = result
        self._refresh_all_trace_curves()
        self.progress_bar.setValue(1000)
        self.progress_bar.setFormat("100%")
        file_count = len(result.reports)
        self.live_status.setText(f"Saved and validated {file_count} ROI NWB files")
        self._set_preview_status("Preview required before the next recording", "required")
        self._set_system_status("COMPLETE", "complete")
        self.statusBar().showMessage(
            f"Saved {file_count} validated ROI NWB files in {result.report.path.parent}"
        )

    def _on_cancelled(self, message: str) -> None:
        self.last_error = None
        self.live_status.setText("Recording cancelled before it started; nothing was recorded")
        self._set_system_status("READY", "ready")
        self.statusBar().showMessage(message[:1].upper() + message[1:])

    def _on_failed(self, traceback_text: str) -> None:
        self.last_error = traceback_text
        self.live_status.setText("Acquisition failed — recovery spool preserved")
        self._set_preview_status(
            "Preview required before another recording attempt",
            "required",
        )
        self._set_system_status("ERROR", "error")
        self.statusBar().showMessage("Acquisition failed; recovery spool was preserved")
        QMessageBox.critical(self, "Acquisition failed", traceback_text)

    def _on_thread_finished(self) -> None:
        self._warmup.leds_off()
        self._operation_uses_leds = False
        self._update_warmup_label()
        self._thread = None
        self._retire_worker(self._worker)
        self._worker = None
        self._operation_kind = None
        self._set_busy_controls(False)
        if self._restart_live_view and not self._close_pending:
            self._restart_live_view = False
            QTimer.singleShot(0, self.start_live_view)
        if self._close_pending:
            self._close_pending = False
            QTimer.singleShot(0, self.close)

    def _retire_worker(self, worker: QObject | None) -> None:
        """Hold a finished worker until Qt deletes it.

        ``thread.finished`` runs the worker's ``deleteLater`` in the worker's own thread.
        Dropping the last Python reference here as well made Python delete it from the
        GUI thread at the same moment, which crashed the device-check test.
        """

        self._retired_workers = [item for item in self._retired_workers if shiboken6.isValid(item)]
        if worker is not None:
            self._retired_workers.append(worker)

    def _set_busy_controls(self, busy: bool) -> None:
        if hasattr(self, "marker_buttons"):
            self._update_marker_buttons()
        checking = getattr(self, "_device_thread", None) is not None
        for button in getattr(self, "rwd_output_test_buttons", {}).values():
            button.setEnabled(not busy and not checking)
        if hasattr(self, "measure_power_button"):
            self.measure_power_button.setEnabled(not busy and not checking)
        if hasattr(self, "check_devices_button"):
            self.check_devices_button.setEnabled(not busy and not checking)
        busy = busy or checking
        live = busy and self._operation_kind == "live"
        previewing = busy and self._operation_kind == "preview"
        if hasattr(self, "live_button"):
            # While live view runs, the same button stops it.
            self.live_button.setEnabled(not busy or live)
            self.live_button.setText("Stop live view" if live else "Live view")
        self.preview_button.setEnabled(not busy or previewing)
        self.preview_button.setText("Stop preview" if previewing else "Run preview")
        self.start_button.setEnabled(not busy and self._preview_signature is not None)
        if hasattr(self, "camera_settings_group"):
            self.camera_settings_group.setEnabled(not busy or live)
        self.stop_button.setEnabled(busy)
        for widget in self._settings_widgets:
            widget.setEnabled(not busy)
        if (live or previewing) and self._is_rwd_mode():
            # LED powers stay adjustable; which LEDs are on needs a new session.
            checks, sliders, group = self._channel_controls[RWD_PROFILE.kind]
            group.setEnabled(True)
            for check in checks.values():
                check.setEnabled(False)
            for slider in sliders.values():
                slider.setEnabled(True)
        else:
            for checks, _sliders, _group in self._channel_controls.values():
                for check in checks.values():
                    check.setEnabled(True)

    def closeEvent(self, event: QCloseEvent) -> None:
        if self._device_thread is not None:
            self._device_thread.wait(15_000)
        if self._thread is not None and self._thread.isRunning():
            self._close_pending = True
            self.stop_recording()
            event.ignore()
            return
        event.accept()


def _ttl_line_combo() -> QComboBox:
    combo = QComboBox()
    for line in range(1, 5):
        combo.addItem(f"Input {line}", line)
    return combo


def _ttl_edge_combo() -> QComboBox:
    combo = QComboBox()
    combo.addItem("rising edge", "rising")
    combo.addItem("falling edge", "falling")
    return combo


_MARKER_ROWS = 4
"""Marker rows shown in setup; settings files may define markers for keys up to 9."""
_TTL_EVENT_ROWS = 4

_DFF_MODES = frozenset({"dff", "dff_fit"})

_TRACE_ROW_HEIGHT_PX = 230
"""Height of one ROI's live trace row; rows beyond the visible area scroll."""
