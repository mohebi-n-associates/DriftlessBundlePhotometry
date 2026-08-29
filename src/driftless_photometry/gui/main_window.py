"""Trace-first acquisition window styled consistently with DriftlessFLIP."""

from __future__ import annotations

from array import array
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import QSignalBlocker, QSize, Qt, QThread, QTimer
from PySide6.QtGui import QCloseEvent, QIcon, QPixmap
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
from driftless_photometry.acquisition import AcquisitionProgress, AcquisitionRunResult
from driftless_photometry.config import (
    CameraConfig,
    ChannelConfig,
    ROIConfig,
    SessionConfig,
    TraceDisplayConfig,
    Wavelength,
    demo_config,
)
from driftless_photometry.settings import (
    SETTINGS_SUFFIX,
    configuration_from_nwb,
    default_settings_path,
    export_settings,
    import_settings,
    load_default_settings,
    save_default_settings,
    settings_root,
)

from .branding import logo_path
from .trace_display import (
    HORIZONS,
    MAX_DISPLAY_POINTS,
    compact_duration,
    downsample_min_max,
)
from .worker import AcquisitionWorker

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
    Wavelength.CONTROL_405: "#66c2ff",
    Wavelength.GREEN_470: "#4fd18b",
    Wavelength.RED_565: "#e05fa0",
}

_WAVELENGTH_TITLES = {
    Wavelength.CONTROL_405: "405 nm — isosbestic control",
    Wavelength.GREEN_470: "470 nm — green signal",
    Wavelength.RED_565: "565 nm — red signal",
}


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
        self.label_edit = QLineEdit(f"Fiber {index + 1}")
        self.animal_id_edit = QLineEdit(f"animal-{index + 1:02d}")
        self.brain_region_edit = QLineEdit("not specified")
        self.sensor_type_edit = QLineEdit("not specified")
        self.age_edit = QLineEdit("P90D")
        self.age_edit.setPlaceholderText("ISO 8601, e.g. P90D")
        self.sex_combo = QComboBox()
        self.sex_combo.addItems(["U", "F", "M", "O"])
        form.addRow("ROI label", self.label_edit)
        form.addRow("Animal ID", self.animal_id_edit)
        form.addRow("Brain region", self.brain_region_edit)
        form.addRow("Sensor type", self.sensor_type_edit)
        form.addRow("Animal age", self.age_edit)
        form.addRow("Animal sex", self.sex_combo)

    def values(self) -> dict[str, str]:
        return {
            "label": self.label_edit.text(),
            "animal_id": self.animal_id_edit.text(),
            "brain_region": self.brain_region_edit.text(),
            "sensor_type": self.sensor_type_edit.text(),
            "subject_age": self.age_edit.text(),
            "subject_sex": self.sex_combo.currentText(),
        }

    def restore(self, values: dict[str, str]) -> None:
        self.label_edit.setText(values["label"])
        self.animal_id_edit.setText(values["animal_id"])
        self.brain_region_edit.setText(values["brain_region"])
        self.sensor_type_edit.setText(values["sensor_type"])
        self.age_edit.setText(values["subject_age"])
        self.sex_combo.setCurrentText(values["subject_sex"])


class MainWindow(QMainWindow):
    """Simulator-backed GUI; physical adapters plug into the same acquisition engine."""

    def __init__(
        self,
        *,
        output_directory: Path,
        default_duration_s: float = 5.0,
        default_fibers: int = 3,
        default_raw: bool = False,
    ) -> None:
        super().__init__()
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
        self.resize(1500, 900)
        self.setMinimumSize(1180, 700)
        self._thread: QThread | None = None
        self._worker: AcquisitionWorker | None = None
        self._close_pending = False
        self._roi_items: list[pg.CircleROI] = []
        self._curves: dict[tuple[Wavelength, int], pg.PlotDataItem] = {}
        self._trace_times: dict[Wavelength, array] = {}
        self._trace_values: dict[tuple[Wavelength, int], array] = {}
        self._trace_roi_count = 0
        self._trace_horizon_s: float | None = HORIZONS[0][1]
        self._trace_first_s: float | None = None
        self._trace_latest_s: float | None = None
        self._last_trace_render_s: dict[Wavelength, float] = {}
        self._image_item = pg.ImageItem()
        self._image_display_maximum = (1 << 12) - 1
        self.last_result: AcquisitionRunResult | None = None
        self.last_error: str | None = None
        self._session_template = demo_config(
            output_directory,
            fiber_count=default_fibers,
            raw_capture=default_raw,
            recording_duration_s=default_duration_s,
        )

        root = QWidget()
        root_layout = QVBoxLayout(root)
        root_layout.setContentsMargins(18, 14, 18, 14)
        root_layout.setSpacing(12)
        root_layout.addWidget(self._build_header())
        self.tabs = QTabWidget()
        self.tabs.addTab(
            self._scrollable(
                self._build_acquire_tab(
                    output_directory,
                    default_duration_s,
                    default_raw,
                    default_fibers,
                )
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
        self._rebuild_roi_items()
        self._load_default_settings_at_startup()

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
        session_form.addRow("Duration", self.duration_spin)
        controls_layout.addWidget(session_group)

        channels_group = QGroupBox("Excitation schedule")
        channels_layout = QVBoxLayout(channels_group)
        channels_layout.setSpacing(8)
        self.channel_checks: dict[Wavelength, QCheckBox] = {}
        self.channel_voltages: dict[Wavelength, QSlider] = {}
        self.channel_voltage_labels: dict[Wavelength, QLabel] = {}
        for wavelength in Wavelength:
            check = QCheckBox(_WAVELENGTH_TITLES[wavelength])
            check.setChecked(True)
            voltage = QSlider(Qt.Orientation.Horizontal)
            voltage.setRange(0, 500)
            voltage.setSingleStep(5)
            voltage.setPageStep(25)
            voltage.setTickInterval(50)
            voltage.setValue(100)
            voltage.setAccessibleName(f"{int(wavelength)} nanometer excitation voltage")
            voltage_label = QLabel("1.00 V")
            voltage_label.setObjectName("voltageReadout")
            voltage_label.setMinimumWidth(54)
            voltage.valueChanged.connect(
                lambda value, label=voltage_label: label.setText(f"{value / 100:.2f} V")
            )
            row = QWidget()
            row_layout = QVBoxLayout(row)
            row_layout.setContentsMargins(0, 0, 0, 0)
            row_layout.setSpacing(3)
            heading = QHBoxLayout()
            heading.addWidget(check, stretch=1)
            heading.addWidget(voltage_label)
            row_layout.addLayout(heading)
            row_layout.addWidget(voltage)
            channels_layout.addWidget(row)
            self.channel_checks[wavelength] = check
            self.channel_voltages[wavelength] = voltage
            self.channel_voltage_labels[wavelength] = voltage_label
        controls_layout.addWidget(channels_group)

        retention_group = QGroupBox("Data retention")
        retention_layout = QVBoxLayout(retention_group)
        self.raw_checkbox = QCheckBox("Embed lossless raw camera frames")
        self.raw_checkbox.setChecked(raw_capture)
        retention_layout.addWidget(self.raw_checkbox)
        raw_hint = QLabel(
            "Each ROI produces a separate animal NWB file. Exposure identity, TTL "
            "edges, per-wavelength camera references, and ROI-annotated views are "
            "copied into every file. Full raw-frame retention is optional and "
            "duplicates frames across those files."
        )
        raw_hint.setObjectName("hint")
        raw_hint.setWordWrap(True)
        retention_layout.addWidget(raw_hint)
        controls_layout.addWidget(retention_group)
        controls_layout.addStretch(1)
        layout.addWidget(controls)

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
            button.clicked.connect(lambda _checked, value=seconds: self._set_trace_horizon(value))
            self.horizon_group.addButton(button, index)
            self.horizon_buttons[seconds] = button
            horizon_row.addWidget(button)
        self.trace_span_hint = QLabel("No trace data yet")
        self.trace_span_hint.setObjectName("hint")
        horizon_row.addSpacing(8)
        horizon_row.addWidget(self.trace_span_hint)
        horizon_row.addStretch(1)
        live_layout.addLayout(horizon_row)
        trace_options = QHBoxLayout()
        trace_options.addWidget(QLabel("Show"))
        self.trace_wavelength_checks: dict[Wavelength, QCheckBox] = {}
        for wavelength in Wavelength:
            check = QCheckBox(f"{int(wavelength)} nm")
            check.setChecked(True)
            check.setStyleSheet(f"color: {_WAVELENGTH_COLORS[wavelength]}; font-weight: 700;")
            check.toggled.connect(self._apply_trace_visibility)
            self.trace_wavelength_checks[wavelength] = check
            trace_options.addWidget(check)
        trace_options.addSpacing(12)
        trace_options.addWidget(QLabel("Y axis"))
        self.trace_mode_combo = QComboBox()
        self.trace_mode_combo.addItem("Absolute", "absolute")
        self.trace_mode_combo.addItem("dF/F", "dff")
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
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 1000)
        self.progress_bar.setValue(0)
        self.progress_bar.setFormat("0%")
        live_layout.addWidget(self.progress_bar)
        live_layout.addWidget(self._build_trace_workspace(), stretch=1)

        action_row = QHBoxLayout()
        self.start_button = QPushButton("Start recording")
        self.start_button.setObjectName("record")
        self.start_button.setMinimumHeight(38)
        self.start_button.clicked.connect(self.start_recording)
        self.stop_button = QPushButton("Stop")
        self.stop_button.setObjectName("attention")
        self.stop_button.setMinimumHeight(38)
        self.stop_button.setEnabled(False)
        self.stop_button.clicked.connect(self.stop_recording)
        action_row.addWidget(self.start_button, stretch=1)
        action_row.addWidget(self.stop_button, stretch=1)
        live_layout.addLayout(action_row)
        layout.addWidget(live_group, stretch=1)

        self._settings_widgets = [
            settings_group,
            session_group,
            channels_group,
            retention_group,
            browse,
        ]
        for check in self.channel_checks.values():
            check.toggled.connect(self._update_overview)
        return page

    def _update_overview(self) -> None:
        enabled = sum(check.isChecked() for check in self.channel_checks.values())
        self.channels_summary.value_label.setText(str(enabled))
        if hasattr(self, "fiber_count_spin"):
            fiber_count = self.fiber_count_spin.value()
            self.fibers_summary.value_label.setText(str(fiber_count))
            self.format_summary.value_label.setText(str(fiber_count))

    def _build_trace_workspace(self) -> pg.GraphicsLayoutWidget:
        self.trace_workspace = pg.GraphicsLayoutWidget()
        self.trace_plots: dict[int, pg.PlotItem] = {}
        return self.trace_workspace

    def _configure_trace_plots(self, rois: tuple[ROIConfig, ...]) -> None:
        self.trace_workspace.clear()
        self.trace_workspace.setMinimumHeight(max(380, 150 * len(rois)))
        self.trace_plots = {}
        self._trace_roi_count = len(rois)
        self._curves.clear()
        display_mode = self.trace_mode_combo.currentData()
        for row, roi in enumerate(rois):
            plot = self.trace_workspace.addPlot(row=row, col=0)
            plot.setTitle(
                f"ROI {row + 1} - {roi.label} / {roi.animal_id}",
                color="#e8f7fa",
                size="10pt",
            )
            plot.showGrid(x=True, y=True, alpha=0.22)
            if display_mode == "dff":
                plot.setLabel("left", "dF/F", units="%")
            else:
                plot.setLabel("left", "Fluorescence", units="counts")
            if row == len(rois) - 1:
                plot.setLabel("bottom", "Session time", units="s")
            if row == 0:
                plot.addLegend(offset=(10, 5), colCount=3)
            plot.setClipToView(True)
            self.trace_plots[row] = plot

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
        for wavelength in Wavelength:
            group = QGroupBox(_WAVELENGTH_TITLES[wavelength])
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

        fibers_group = QGroupBox("Fiber array")
        fibers_group.setMinimumWidth(390)
        fibers_group.setMaximumWidth(470)
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
            "Fiber colors are reused in every live trace so identity remains consistent "
            "across excitation wavelengths."
        )
        instructions.setObjectName("hint")
        instructions.setWordWrap(True)
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
        layout.addWidget(fibers_group)

        camera_group = QGroupBox("Camera calibration — full frame")
        camera_layout = QVBoxLayout(camera_group)
        self.calibration_plot = pg.PlotWidget()
        self.calibration_plot.setAspectLocked(True)
        self.calibration_plot.invertY(True)
        self.calibration_plot.showGrid(x=True, y=True, alpha=0.18)
        self.calibration_plot.setLabel("bottom", "Camera x", units="px")
        self.calibration_plot.setLabel("left", "Camera y", units="px")
        self.calibration_plot.addItem(self._image_item)
        camera_layout.addWidget(self.calibration_plot)
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
            QPushButton#record:disabled, QPushButton#attention:disabled {
                color: #61778a; background: #13283a; border-color: #22394d;
            }
            QLabel#hint { color: #7694aa; }
            QLabel#liveStatus { font-weight: 700; color: #f2b134; }
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
            """
        )

    def _set_system_status(self, text: str, state: str) -> None:
        self.system_status.setText(text)
        self.system_status.setProperty("state", state)
        self.system_status.style().unpolish(self.system_status)
        self.system_status.style().polish(self.system_status)

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
            config = import_settings(path)
            self._apply_configuration(config)
        except (OSError, ValueError) as error:
            QMessageBox.warning(self, "Could not load settings", str(error))
            return
        self.statusBar().showMessage(f"Loaded all settings from {path}")

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
            config = load_default_settings()
            if config is None:
                return
            self._apply_configuration(config)
        except (OSError, ValueError) as error:
            self.statusBar().showMessage(
                f"Default settings could not be loaded; using built-ins: {error}"
            )
            return
        self.statusBar().showMessage(f"Loaded defaults from {default_settings_path()}")

    def _apply_configuration(self, config: SessionConfig) -> None:
        """Push one validated settings snapshot into every corresponding GUI control."""

        self._session_template = config
        self.duration_spin.setValue(config.recording_duration_s)
        self.experimenter_edit.setText(config.experimenter)
        self.session_description_edit.setText(config.session_description)
        self.lab_edit.setText(config.lab or "")
        self.institution_edit.setText(config.institution or "")
        self.output_edit.setText(str(config.output_directory))
        self.raw_checkbox.setChecked(config.camera.raw_capture)

        configured_channels = {channel.wavelength_nm: channel for channel in config.channels}
        for wavelength in Wavelength:
            channel = configured_channels.get(wavelength)
            self.channel_checks[wavelength].setChecked(channel.enabled if channel else False)
            self.channel_voltages[wavelength].setValue(
                round((channel.voltage_v if channel else 1.0) * 100)
            )

        blocker = QSignalBlocker(self.fiber_count_spin)
        self.fiber_count_spin.setValue(len(config.rois))
        del blocker
        self._rebuild_roi_metadata(len(config.rois))
        for editor, roi in zip(self._roi_metadata_editors, config.rois, strict=True):
            editor.restore(
                {
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
        self._apply_trace_mode()
        self._update_overview()

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
            self.roi_metadata_tabs.addTab(editor, f"ROI {index + 1}")
        self.roi_metadata_tabs.setCurrentIndex(max(0, selected))

    def _rebuild_roi_items(self) -> None:
        if not hasattr(self, "calibration_plot"):
            return
        self._rebuild_roi_metadata(self.fiber_count_spin.value())
        camera = self._session_template.camera
        preview = demo_config(
            Path(self.output_edit.text() or "."),
            fiber_count=self.fiber_count_spin.value(),
            width_px=camera.width_px,
            height_px=camera.height_px,
        )
        self._set_roi_items(preview.rois, camera)

    def _set_roi_items(
        self,
        rois: tuple[ROIConfig, ...],
        camera: CameraConfig,
    ) -> None:
        for item in self._roi_items:
            self.calibration_plot.removeItem(item)
        self._roi_items.clear()
        self._configure_trace_plots(rois)
        blank = np.zeros((camera.height_px, camera.width_px), dtype=np.uint16)
        self._image_item.setImage(
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
            circle.sigRegionChangeFinished.connect(self._update_roi_summary)
            self.calibration_plot.addItem(circle)
            self._roi_items.append(circle)
        self.calibration_plot.setXRange(0, camera.width_px, padding=0.02)
        self.calibration_plot.setYRange(0, camera.height_px, padding=0.02)
        self._update_roi_summary()

    def _update_roi_summary(self) -> None:
        rows = []
        for index, item in enumerate(self._roi_items):
            position = item.pos()
            size = item.size()
            radius = min(float(size.x()), float(size.y())) / 2.0
            center_x = float(position.x()) + radius
            center_y = float(position.y()) + radius
            rows.append(
                f'<span style="color:{_FIBER_COLORS[index]}">●</span> '
                f"Fiber {index + 1}: x {center_x:.1f}, y {center_y:.1f}, r {radius:.1f} px"
            )
        self.roi_summary.setText("<br>".join(rows))

    def _build_config(self) -> SessionConfig:
        output = Path(self.output_edit.text()).expanduser()
        configured_channels = {
            wavelength: ChannelConfig(wavelength_nm=wavelength) for wavelength in Wavelength
        }
        configured_channels.update(
            {channel.wavelength_nm: channel for channel in self._session_template.channels}
        )
        channels = tuple(
            ChannelConfig(
                wavelength_nm=wavelength,
                enabled=self.channel_checks[wavelength].isChecked(),
                voltage_v=self.channel_voltages[wavelength].value() / 100.0,
                role=configured_channels[wavelength].role,
                emission_wavelength_nm=configured_channels[wavelength].emission_wavelength_nm,
            )
            for wavelength in Wavelength
        )
        rois: list[ROIConfig] = []
        used_fiber_ids: set[str] = set()
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
            rois.append(
                ROIConfig(
                    fiber_id=fiber_id,
                    label=metadata.label_edit.text().strip(),
                    animal_id=metadata.animal_id_edit.text().strip(),
                    brain_region=metadata.brain_region_edit.text().strip(),
                    sensor_type=metadata.sensor_type_edit.text().strip(),
                    subject_age=metadata.age_edit.text().strip() or None,
                    subject_sex=metadata.sex_combo.currentText(),
                    center_x_px=float(position.x()) + radius,
                    center_y_px=float(position.y()) + radius,
                    radius_px=radius,
                )
            )
        visible_wavelengths = tuple(
            wavelength
            for wavelength, check in self.trace_wavelength_checks.items()
            if check.isChecked()
        )
        display = TraceDisplayConfig(
            horizon_s=self._trace_horizon_s,
            visible_wavelengths=visible_wavelengths,
            mode=self.trace_mode_combo.currentData(),
            dff_baseline_s=self.trace_baseline_spin.value(),
        )
        started = datetime.now(UTC)
        payload = self._session_template.model_dump()
        payload.update(
            session_id=started.strftime("demo-%Y%m%dT%H%M%S-%fZ"),
            session_description=self.session_description_edit.text().strip(),
            experimenter=self.experimenter_edit.text().strip(),
            output_directory=output,
            recording_duration_s=self.duration_spin.value(),
            session_start_time=started,
            camera=self._session_template.camera.model_copy(
                update={"raw_capture": self.raw_checkbox.isChecked()}
            ),
            channels=channels,
            rois=tuple(rois),
            lab=self.lab_edit.text().strip() or None,
            institution=self.institution_edit.text().strip() or None,
            display=display,
        )
        return SessionConfig.model_validate(payload)

    def start_recording(self) -> None:
        if self._thread is not None:
            return
        try:
            config = self._build_config()
        except Exception as error:
            QMessageBox.critical(self, "Invalid configuration", str(error))
            return
        self.last_result = None
        self.last_error = None
        self._prepare_trace_curves(config)
        self._prepare_wavelength_images(config)
        self.progress_bar.setValue(0)
        self.progress_bar.setFormat("0%")
        self.frame_counter.setText("0 frames")
        self.live_status.setText("Arming simulator…")
        self._set_system_status("STARTING…", "recording")
        self._set_recording_controls(True)
        self.statusBar().showMessage("Arming simulator…")

        thread = QThread(self)
        worker = AcquisitionWorker(config, config.recording_duration_s)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.progress.connect(self._on_progress)
        worker.state_changed.connect(self._on_state)
        worker.completed.connect(self._on_completed)
        worker.failed.connect(self._on_failed)
        worker.completed.connect(thread.quit)
        worker.failed.connect(thread.quit)
        thread.finished.connect(self._on_thread_finished)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        self._thread = thread
        self._worker = worker
        thread.start()

    def stop_recording(self) -> None:
        if self._worker is not None:
            self.live_status.setText("Stopping after the current frame…")
            self.statusBar().showMessage("Stopping after the current frame…")
            self.stop_button.setEnabled(False)
            self._worker.request_stop()

    def _prepare_trace_curves(self, config: SessionConfig) -> None:
        self._configure_trace_plots(config.rois)
        self._trace_times = {wavelength: array("d") for wavelength in Wavelength}
        self._trace_values.clear()
        self._trace_horizon_s = HORIZONS[0][1]
        for seconds, button in self.horizon_buttons.items():
            button.setChecked(seconds == self._trace_horizon_s)
        self._trace_first_s = None
        self._trace_latest_s = None
        self._last_trace_render_s = {wavelength: -np.inf for wavelength in Wavelength}
        for index, _roi in enumerate(config.rois):
            for wavelength in Wavelength:
                curve = self.trace_plots[index].plot(
                    name=f"{int(wavelength)} nm" if index == 0 else None,
                    pen=pg.mkPen(_WAVELENGTH_COLORS[wavelength], width=1.8),
                )
                curve.setVisible(self.trace_wavelength_checks[wavelength].isChecked())
                self._curves[(wavelength, index)] = curve
                self._trace_values[(wavelength, index)] = array("f")
        self._update_trace_horizon_availability(0.0)

    def _set_trace_horizon(self, seconds: float | None) -> None:
        self._trace_horizon_s = seconds
        for value, button in self.horizon_buttons.items():
            button.setChecked(value == seconds)
        for plot in self.trace_plots.values():
            plot.enableAutoRange(axis="y", enable=True)
        self._refresh_all_trace_curves()

    def _apply_trace_visibility(self, *_args: object) -> None:
        for (wavelength, _index), curve in self._curves.items():
            curve.setVisible(self.trace_wavelength_checks[wavelength].isChecked())

    def _apply_trace_mode(self, *_args: object) -> None:
        is_dff = self.trace_mode_combo.currentData() == "dff"
        self.trace_baseline_spin.setEnabled(is_dff)
        self.trace_baseline_hint.setEnabled(is_dff)
        for plot in self.trace_plots.values():
            if is_dff:
                plot.setLabel("left", "dF/F", units="%")
            else:
                plot.setLabel("left", "Fluorescence", units="counts")
            plot.enableAutoRange(axis="y", enable=True)
        self._refresh_all_trace_curves()

    def _update_trace_horizon_availability(self, duration_s: float) -> None:
        self.trace_span_hint.setText(f"{compact_duration(duration_s)} available (this recording)")
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

    def _refresh_trace_wavelength(self, wavelength: Wavelength) -> None:
        visible = self._visible_trace_range()
        if visible is None:
            return
        start_s, stop_s = visible
        time_buffer = self._trace_times[wavelength]
        if not time_buffer:
            return
        times = np.frombuffer(time_buffer, dtype=np.float64)
        first_index = int(np.searchsorted(times, start_s, side="left"))
        selected_times = times[first_index:]
        budget = self._trace_point_budget()
        for index in range(self._trace_roi_count):
            all_values = np.frombuffer(
                self._trace_values[(wavelength, index)],
                dtype=np.float32,
            )
            values = all_values[first_index:]
            if self.trace_mode_combo.currentData() == "dff":
                baseline_stop_s = times[0] + self.trace_baseline_spin.value()
                baseline_stop = max(1, int(np.searchsorted(times, baseline_stop_s, side="right")))
                baseline_values = all_values[:baseline_stop]
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
            self._curves[(wavelength, index)].setData(
                display_times,
                display_values,
                connect="finite",
            )
        for plot in self.trace_plots.values():
            plot.setXRange(start_s, stop_s, padding=0.0)

    def _refresh_all_trace_curves(self) -> None:
        for wavelength in Wavelength:
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

    def _on_progress(self, progress: AcquisitionProgress) -> None:
        sample = progress.sample
        self.wavelength_image_items[sample.wavelength_nm].setImage(
            progress.image,
            autoLevels=False,
            levels=(0, self._image_display_maximum),
        )
        self.wavelength_frame_labels[sample.wavelength_nm].setText(
            f"Frame {progress.frame_count:,} · {sample.timestamp_s:.3f} s"
        )
        times = self._trace_times[sample.wavelength_nm]
        times.append(sample.timestamp_s)
        for index, value in enumerate(sample.values):
            values = self._trace_values[(sample.wavelength_nm, index)]
            values.append(float(value))
        if self._trace_first_s is None:
            self._trace_first_s = sample.timestamp_s
        self._trace_latest_s = sample.timestamp_s
        available_span = sample.timestamp_s - self._trace_first_s + 1 / 30
        self._update_trace_horizon_availability(available_span)
        visible = self._visible_trace_range()
        if visible is not None:
            for plot in self.trace_plots.values():
                plot.setXRange(*visible, padding=0.0)
        if (
            sample.timestamp_s - self._last_trace_render_s[sample.wavelength_nm]
            >= _TRACE_REFRESH_INTERVAL_S
        ):
            self._refresh_trace_wavelength(sample.wavelength_nm)
            self._last_trace_render_s[sample.wavelength_nm] = sample.timestamp_s
        if progress.frame_count == 1 or progress.frame_count % 5 == 0:
            self._image_item.setImage(
                progress.image,
                autoLevels=False,
                levels=(0, self._image_display_maximum),
            )
        fraction = min(1.0, (sample.timestamp_s + 1 / 30) / self.duration_spin.value())
        progress_value = round(fraction * 1000)
        self.progress_bar.setValue(progress_value)
        self.progress_bar.setFormat(f"{fraction:.0%}")
        self.frame_counter.setText(f"{progress.frame_count:,} frames")
        self.live_status.setText(f"Recording — {int(sample.wavelength_nm)} nm exposure")
        self.statusBar().showMessage(
            f"Recording — frame {progress.frame_count}, {int(sample.wavelength_nm)} nm"
        )

    def _on_state(self, state: str) -> None:
        if state == "recording":
            self._set_system_status("RECORDING", "recording")
        elif state == "draining":
            self._set_system_status("SAVING…", "saving")
            self.live_status.setText("Finalizing and validating NWB…")
            self.statusBar().showMessage("Finalizing and validating NWB…")

    def _on_completed(self, result: AcquisitionRunResult) -> None:
        self.last_result = result
        self._refresh_all_trace_curves()
        self.progress_bar.setValue(1000)
        self.progress_bar.setFormat("100%")
        file_count = len(result.reports)
        self.live_status.setText(f"Saved and validated {file_count} ROI NWB files")
        self._set_system_status("COMPLETE", "complete")
        self.statusBar().showMessage(
            f"Saved {file_count} validated ROI NWB files in {result.report.path.parent}"
        )

    def _on_failed(self, traceback_text: str) -> None:
        self.last_error = traceback_text
        self.live_status.setText("Acquisition failed — recovery spool preserved")
        self._set_system_status("ERROR", "error")
        self.statusBar().showMessage("Acquisition failed; recovery spool was preserved")
        QMessageBox.critical(self, "Acquisition failed", traceback_text)

    def _on_thread_finished(self) -> None:
        self._thread = None
        self._worker = None
        self._set_recording_controls(False)
        if self._close_pending:
            self._close_pending = False
            QTimer.singleShot(0, self.close)

    def _set_recording_controls(self, recording: bool) -> None:
        self.start_button.setEnabled(not recording)
        self.stop_button.setEnabled(recording)
        for widget in self._settings_widgets:
            widget.setEnabled(not recording)

    def closeEvent(self, event: QCloseEvent) -> None:
        if self._thread is not None and self._thread.isRunning():
            self._close_pending = True
            self.stop_recording()
            event.ignore()
            return
        event.accept()
