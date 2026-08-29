"""Trace-first acquisition window styled consistently with DriftlessFLIP."""

from __future__ import annotations

from collections import deque
from pathlib import Path

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import QSize, Qt, QThread, QTimer
from PySide6.QtGui import QCloseEvent, QIcon, QPixmap
from PySide6.QtWidgets import (
    QCheckBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from driftless_photometry import __version__
from driftless_photometry.acquisition import AcquisitionProgress, AcquisitionRunResult
from driftless_photometry.config import (
    ChannelConfig,
    ROIConfig,
    SessionConfig,
    Wavelength,
    demo_config,
)

from .branding import logo_path
from .worker import AcquisitionWorker

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
        self._trace_times: dict[Wavelength, deque[float]] = {}
        self._trace_values: dict[tuple[Wavelength, int], deque[float]] = {}
        self._image_item = pg.ImageItem()
        self.last_result: AcquisitionRunResult | None = None
        self.last_error: str | None = None

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
        root_layout.addWidget(self.tabs)
        self.setCentralWidget(root)
        self._apply_theme()
        self.statusBar().showMessage(
            "Ready — simulator mode; no physical camera or controller connected"
        )
        self._rebuild_roi_items()

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
        setup_copy = QLabel("Define the subject, excitation schedule, and retained data.")
        setup_copy.setObjectName("hint")
        setup_copy.setWordWrap(True)
        controls_layout.addWidget(setup_eyebrow)
        controls_layout.addWidget(setup_title)
        controls_layout.addWidget(setup_copy)

        session_group = QGroupBox("Recording")
        session_form = QFormLayout(session_group)
        self.subject_edit = QLineEdit("demo-subject")
        self.experimenter_edit = QLineEdit("Simulator")
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
        session_form.addRow("Subject", self.subject_edit)
        session_form.addRow("Experimenter", self.experimenter_edit)
        session_form.addRow("Output root", output_row)
        session_form.addRow("Duration", self.duration_spin)
        controls_layout.addWidget(session_group)

        channels_group = QGroupBox("Excitation schedule")
        channels_form = QFormLayout(channels_group)
        self.channel_checks: dict[Wavelength, QCheckBox] = {}
        self.channel_voltages: dict[Wavelength, QDoubleSpinBox] = {}
        for wavelength in Wavelength:
            check = QCheckBox(_WAVELENGTH_TITLES[wavelength])
            check.setChecked(True)
            voltage = QDoubleSpinBox()
            voltage.setRange(0.0, 5.0)
            voltage.setDecimals(2)
            voltage.setSingleStep(0.05)
            voltage.setSuffix(" V")
            voltage.setValue(1.0)
            row = QWidget()
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(0, 0, 0, 0)
            row_layout.addWidget(check, stretch=1)
            row_layout.addWidget(voltage)
            channels_form.addRow(row)
            self.channel_checks[wavelength] = check
            self.channel_voltages[wavelength] = voltage
        controls_layout.addWidget(channels_group)

        retention_group = QGroupBox("Data retention")
        retention_layout = QVBoxLayout(retention_group)
        self.raw_checkbox = QCheckBox("Embed lossless raw camera frames")
        self.raw_checkbox.setChecked(raw_capture)
        retention_layout.addWidget(self.raw_checkbox)
        raw_hint = QLabel(
            "ROI traces, exposure identity, TTL edges, and a calibration frame are "
            "always retained in NWB. Raw frame retention is optional."
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
        self.format_summary = SummaryCard("NWB", "SESSION OUTPUT")
        overview.addWidget(self.channels_summary)
        overview.addWidget(self.fibers_summary)
        overview.addWidget(self.format_summary)
        live_layout.addLayout(overview)
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

        self._settings_widgets = [session_group, channels_group, retention_group, browse]
        for check in self.channel_checks.values():
            check.toggled.connect(self._update_overview)
        return page

    def _update_overview(self) -> None:
        enabled = sum(check.isChecked() for check in self.channel_checks.values())
        self.channels_summary.value_label.setText(str(enabled))
        if hasattr(self, "fiber_count_spin"):
            self.fibers_summary.value_label.setText(str(self.fiber_count_spin.value()))

    def _build_trace_workspace(self) -> pg.GraphicsLayoutWidget:
        traces_widget = pg.GraphicsLayoutWidget()
        self.trace_plots: dict[Wavelength, pg.PlotItem] = {}
        for row, wavelength in enumerate(Wavelength):
            plot = traces_widget.addPlot(row=row, col=0)
            plot.setTitle(_WAVELENGTH_TITLES[wavelength], color="#e8f7fa", size="10pt")
            plot.showGrid(x=True, y=True, alpha=0.22)
            plot.setLabel("left", "Fluorescence", units="counts")
            if row == len(Wavelength) - 1:
                plot.setLabel("bottom", "Session time", units="s")
            if row == 0:
                plot.addLegend(offset=(10, 5), colCount=3)
            self.trace_plots[wavelength] = plot
        return traces_widget

    def _build_calibration_tab(self, fiber_count: int) -> QWidget:
        page = QWidget()
        layout = QHBoxLayout(page)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(10)

        fibers_group = QGroupBox("Fiber array")
        fibers_group.setMinimumWidth(320)
        fibers_group.setMaximumWidth(390)
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

    def _rebuild_roi_items(self) -> None:
        if not hasattr(self, "calibration_plot"):
            return
        for item in self._roi_items:
            self.calibration_plot.removeItem(item)
        self._roi_items.clear()
        preview = demo_config(
            Path(self.output_edit.text() or "."),
            fiber_count=self.fiber_count_spin.value(),
        )
        blank = np.zeros((preview.camera.height_px, preview.camera.width_px), dtype=np.uint16)
        self._image_item.setImage(blank, autoLevels=False, levels=(0, 4095))
        for index, roi in enumerate(preview.rois):
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
        self.calibration_plot.setXRange(0, preview.camera.width_px, padding=0.02)
        self.calibration_plot.setYRange(0, preview.camera.height_px, padding=0.02)
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
        base = demo_config(
            output,
            fiber_count=self.fiber_count_spin.value(),
            raw_capture=self.raw_checkbox.isChecked(),
        )
        channels = tuple(
            ChannelConfig(
                wavelength_nm=wavelength,
                enabled=self.channel_checks[wavelength].isChecked(),
                voltage_v=self.channel_voltages[wavelength].value(),
            )
            for wavelength in Wavelength
        )
        rois: list[ROIConfig] = []
        for index, item in enumerate(self._roi_items):
            position = item.pos()
            size = item.size()
            radius = min(float(size.x()), float(size.y())) / 2.0
            rois.append(
                ROIConfig(
                    fiber_id=f"fiber_{index + 1:02d}",
                    label=f"Fiber {index + 1}",
                    center_x_px=float(position.x()) + radius,
                    center_y_px=float(position.y()) + radius,
                    radius_px=radius,
                )
            )
        payload = base.model_dump()
        payload.update(
            subject_id=self.subject_edit.text().strip(),
            experimenter=self.experimenter_edit.text().strip(),
            channels=channels,
            rois=tuple(rois),
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
        self.progress_bar.setValue(0)
        self.progress_bar.setFormat("0%")
        self.frame_counter.setText("0 frames")
        self.live_status.setText("Arming simulator…")
        self._set_system_status("STARTING…", "recording")
        self._set_recording_controls(True)
        self.statusBar().showMessage("Arming simulator…")

        thread = QThread(self)
        worker = AcquisitionWorker(config, self.duration_spin.value())
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
        for plot in self.trace_plots.values():
            plot.clear()
            if plot.legend is not None:
                plot.legend.clear()
        self._curves.clear()
        self._trace_times = {wavelength: deque(maxlen=2000) for wavelength in Wavelength}
        self._trace_values.clear()
        for wavelength in Wavelength:
            for index, roi in enumerate(config.rois):
                curve = self.trace_plots[wavelength].plot(
                    name=roi.label if wavelength is Wavelength.CONTROL_405 else None,
                    pen=pg.mkPen(_FIBER_COLORS[index], width=1.7),
                )
                self._curves[(wavelength, index)] = curve
                self._trace_values[(wavelength, index)] = deque(maxlen=2000)

    def _on_progress(self, progress: AcquisitionProgress) -> None:
        sample = progress.sample
        times = self._trace_times[sample.wavelength_nm]
        times.append(sample.timestamp_s)
        for index, value in enumerate(sample.values):
            values = self._trace_values[(sample.wavelength_nm, index)]
            values.append(float(value))
            self._curves[(sample.wavelength_nm, index)].setData(list(times), list(values))
        if progress.frame_count == 1 or progress.frame_count % 5 == 0:
            self._image_item.setImage(
                progress.image,
                autoLevels=False,
                levels=(0, (1 << 12) - 1),
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
        self.progress_bar.setValue(1000)
        self.progress_bar.setFormat("100%")
        self.live_status.setText("Saved and validated")
        self._set_system_status("COMPLETE", "complete")
        self.statusBar().showMessage(f"Saved and validated: {result.report.path}")

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
