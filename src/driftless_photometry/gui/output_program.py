"""Edit one RWD TTL output's pulse-train program."""

from __future__ import annotations

from collections.abc import Sequence

from pydantic import ValidationError
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from driftless_photometry.config import (
    DffTriggerConfig,
    RWDTTLOutputConfig,
    RWDTTLOutputStepConfig,
    Wavelength,
)

TRIGGERS = (
    ("At the recording's start", "recording_start"),
    ("Manually, with its button", "manual"),
    ("When a dF/F crosses a threshold", "dff_threshold"),
)
_STEP_COLUMNS = ("Frequency (Hz)", "Width (ms)", "Train (s)", "Gap (s)", "Cycles")


def describe_program(output: RWDTTLOutputConfig) -> str:
    """One line for the panel, such as '20 Hz, 10 ms · manual · 5 s program'."""

    trigger = {
        "recording_start": "at start",
        "manual": "manual",
        "dff_threshold": "dF/F trigger",
    }[output.trigger]
    length = "continuous" if output.program_s is None else f"{output.program_s:g} s program"
    return f"{output.frequency_hz:g} Hz, {output.pulse_width_ms} ms · {trigger} · {length}"


def _spin(low: float, high: float, value: float, suffix: str, decimals: int = 2):
    spin = QDoubleSpinBox()
    spin.setRange(low, high)
    spin.setDecimals(decimals)
    spin.setSuffix(suffix)
    spin.setValue(value)
    return spin


class OutputProgramDialog(QDialog):
    """Edit the program of one output; ``result_output`` holds it after OK."""

    def __init__(
        self,
        output: RWDTTLOutputConfig,
        *,
        fibers: Sequence[tuple[str, str]],
        wavelengths: Sequence[Wavelength],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"TTL output {output.port} program")
        self._port = output.port
        self.result_output: RWDTTLOutputConfig | None = None
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.label_edit = QLineEdit(output.label)
        self.label_edit.setMaxLength(64)
        self.label_edit.setPlaceholderText(f"Output {output.port}")
        self.frequency_spin = _spin(0.01, 655.35, output.frequency_hz, " Hz")
        self.width_spin = QSpinBox()
        self.width_spin.setRange(1, 65_535)
        self.width_spin.setSuffix(" ms")
        self.width_spin.setValue(output.pulse_width_ms)
        self.trigger_combo = QComboBox()
        for text, value in TRIGGERS:
            self.trigger_combo.addItem(text, value)
        self.trigger_combo.setCurrentIndex(self.trigger_combo.findData(output.trigger))
        self.delay_spin = _spin(0.0, 86_400.0, output.delay_s, " s")
        self.continuous_check = QCheckBox("One train until the recording ends")
        self.continuous_check.setChecked(output.train_s is None)
        self.train_spin = _spin(0.01, 86_400.0, output.train_s or 1.0, " s")
        self.gap_spin = _spin(0.0, 86_400.0, output.gap_s, " s")
        self.cycles_spin = QSpinBox()
        self.cycles_spin.setRange(1, 10_000)
        self.cycles_spin.setValue(output.cycles)
        self.repeats_spin = QSpinBox()
        self.repeats_spin.setRange(1, 10_000)
        self.repeats_spin.setValue(output.repeats)
        self.mark_check = QCheckBox("Record each train as an event (stimulation marking)")
        self.mark_check.setChecked(output.mark_stimulation)
        form.addRow("Name", self.label_edit)
        form.addRow("Frequency", self.frequency_spin)
        form.addRow("Pulse width", self.width_spin)
        form.addRow("Start", self.trigger_combo)
        form.addRow("Delay", self.delay_spin)
        form.addRow("", self.continuous_check)
        form.addRow("Train length", self.train_spin)
        form.addRow("Gap after each train", self.gap_spin)
        form.addRow("Cycles", self.cycles_spin)
        layout.addLayout(form)

        steps_group = QGroupBox("Further steps, after the first")
        steps_layout = QVBoxLayout(steps_group)
        self.steps_table = QTableWidget(0, len(_STEP_COLUMNS))
        self.steps_table.setHorizontalHeaderLabels(_STEP_COLUMNS)
        self.steps_table.verticalHeader().setVisible(False)
        for step in output.steps:
            self._add_step(step)
        buttons = QHBoxLayout()
        add = QPushButton("Add step")
        add.clicked.connect(lambda: self._add_step(None))
        remove = QPushButton("Remove step")
        remove.clicked.connect(self._remove_step)
        buttons.addWidget(add)
        buttons.addWidget(remove)
        buttons.addStretch(1)
        steps_layout.addWidget(self.steps_table)
        steps_layout.addLayout(buttons)
        layout.addWidget(steps_group)
        repeat_form = QFormLayout()
        repeat_form.addRow("Repeat the whole program", self.repeats_spin)
        layout.addLayout(repeat_form)

        self.dff_group = QGroupBox("dF/F threshold")
        dff_form = QFormLayout(self.dff_group)
        trigger = output.dff_trigger or DffTriggerConfig(
            fiber_id=fibers[0][0] if fibers else "fiber_01"
        )
        self.dff_fiber_combo = QComboBox()
        for fiber_id, label in fibers:
            self.dff_fiber_combo.addItem(label, fiber_id)
        self.dff_fiber_combo.setCurrentIndex(
            max(0, self.dff_fiber_combo.findData(trigger.fiber_id))
        )
        self.dff_wavelength_combo = QComboBox()
        for wavelength in wavelengths:
            self.dff_wavelength_combo.addItem(f"{int(wavelength)} nm", int(wavelength))
        self.dff_wavelength_combo.setCurrentIndex(
            max(0, self.dff_wavelength_combo.findData(int(trigger.wavelength_nm)))
        )
        self.dff_threshold_spin = _spin(-1000.0, 1000.0, trigger.threshold_percent, " %", 1)
        self.dff_direction_combo = QComboBox()
        self.dff_direction_combo.addItem("rises to or above", "above")
        self.dff_direction_combo.addItem("falls to or below", "below")
        self.dff_direction_combo.setCurrentIndex(
            self.dff_direction_combo.findData(trigger.direction)
        )
        self.dff_baseline_spin = _spin(0.1, 3600.0, trigger.baseline_s, " s", 1)
        self.dff_refractory_spin = _spin(0.0, 86_400.0, trigger.refractory_s, " s", 1)
        dff_form.addRow("ROI", self.dff_fiber_combo)
        dff_form.addRow("Wavelength", self.dff_wavelength_combo)
        dff_form.addRow("When dF/F", self.dff_direction_combo)
        dff_form.addRow("Threshold", self.dff_threshold_spin)
        dff_form.addRow("F0: median of the first", self.dff_baseline_spin)
        dff_form.addRow("Wait between triggers", self.dff_refractory_spin)
        layout.addWidget(self.dff_group)
        layout.addWidget(self.mark_check)
        self.error_label = QLabel()
        self.error_label.setObjectName("hint")
        self.error_label.setWordWrap(True)
        layout.addWidget(self.error_label)
        box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        box.accepted.connect(self._accept)
        box.rejected.connect(self.reject)
        layout.addWidget(box)
        self.trigger_combo.currentIndexChanged.connect(self._sync)
        self.continuous_check.toggled.connect(self._sync)
        self._sync()

    def _add_step(self, step: RWDTTLOutputStepConfig | None) -> None:
        row = self.steps_table.rowCount()
        self.steps_table.insertRow(row)
        values = (
            (step.frequency_hz, step.pulse_width_ms, step.train_s, step.gap_s, step.cycles)
            if step is not None
            else (self.frequency_spin.value(), self.width_spin.value(), 1.0, 0.0, 1)
        )
        for column, value in enumerate(values):
            self.steps_table.setItem(row, column, QTableWidgetItem(f"{value:g}"))
        self._sync()

    def _remove_step(self) -> None:
        row = self.steps_table.currentRow()
        if row < 0:
            row = self.steps_table.rowCount() - 1
        if row >= 0:
            self.steps_table.removeRow(row)
        self._sync()

    def _sync(self, *_args: object) -> None:
        trigger = self.trigger_combo.currentData()
        can_run_open = trigger == "recording_start"
        if not can_run_open:
            self.continuous_check.setChecked(False)
        self.continuous_check.setEnabled(can_run_open)
        timed = not self.continuous_check.isChecked()
        for widget in (self.train_spin, self.gap_spin, self.cycles_spin, self.repeats_spin):
            widget.setEnabled(timed)
        self.steps_table.setEnabled(timed)
        self.dff_group.setVisible(trigger == "dff_threshold")

    def _steps(self) -> tuple[RWDTTLOutputStepConfig, ...]:
        steps = []
        for row in range(self.steps_table.rowCount()):
            cells = [self.steps_table.item(row, column) for column in range(len(_STEP_COLUMNS))]
            texts = [cell.text() if cell is not None else "" for cell in cells]
            try:
                frequency, width, train, gap, cycles = (float(text) for text in texts)
            except ValueError as error:
                raise ValueError(f"step {row + 2} needs a number in every column") from error
            steps.append(
                RWDTTLOutputStepConfig(
                    frequency_hz=frequency,
                    pulse_width_ms=int(width),
                    train_s=train,
                    gap_s=gap,
                    cycles=int(cycles),
                )
            )
        return tuple(steps)

    def output(self) -> RWDTTLOutputConfig:
        """The program as edited; raises ValueError when it is not valid."""

        trigger = self.trigger_combo.currentData()
        timed = not self.continuous_check.isChecked()
        try:
            return RWDTTLOutputConfig(
                port=self._port,
                frequency_hz=self.frequency_spin.value(),
                pulse_width_ms=self.width_spin.value(),
                label=self.label_edit.text().strip(),
                trigger=trigger,
                delay_s=self.delay_spin.value(),
                train_s=self.train_spin.value() if timed else None,
                gap_s=self.gap_spin.value() if timed else 0.0,
                cycles=self.cycles_spin.value() if timed else 1,
                steps=self._steps() if timed else (),
                repeats=self.repeats_spin.value() if timed else 1,
                dff_trigger=(
                    DffTriggerConfig(
                        fiber_id=self.dff_fiber_combo.currentData() or "fiber_01",
                        wavelength_nm=Wavelength(self.dff_wavelength_combo.currentData()),
                        threshold_percent=self.dff_threshold_spin.value(),
                        direction=self.dff_direction_combo.currentData(),
                        baseline_s=self.dff_baseline_spin.value(),
                        refractory_s=self.dff_refractory_spin.value(),
                    )
                    if trigger == "dff_threshold"
                    else None
                ),
                mark_stimulation=self.mark_check.isChecked(),
            )
        except ValidationError as error:
            raise ValueError("; ".join(item["msg"] for item in error.errors())) from error

    def _accept(self) -> None:
        try:
            self.result_output = self.output()
        except ValueError as error:
            self.error_label.setText(str(error))
            return
        self.accept()
