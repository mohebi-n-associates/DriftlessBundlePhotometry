"""Patch-cord pre-bleach: run the LEDs at high power for a set time, then switch them off.

Fiber bundles and patch cords autofluoresce; a bleach before recording lowers and
steadies that background. The bleach is a timed live view with every enabled LED at
one power, so it uses the same LED path, and the same off sequence, as live view.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

BLEACH_DEFAULT_MINUTES = 10
BLEACH_MAX_MINUTES = 60
BLEACH_DEFAULT_PERCENT = 80


def led_duty_fraction(exposure_us: float, frame_rate_hz: float, led_count: int) -> float:
    """Fraction of time each LED is lit: it fires once per cycle, for one exposure.

    One LED alone is driven continuously by the RWD board (bench, 2026-10-20).
    """

    if led_count <= 0 or frame_rate_hz <= 0:
        return 0.0
    if led_count == 1:
        return 1.0
    return min(1.0, exposure_us * 1e-6 * frame_rate_hz / led_count)


@dataclass(frozen=True, slots=True)
class BleachPlan:
    duration_s: float
    percent: float


@dataclass(frozen=True, slots=True)
class BleachRecord:
    """A finished bleach, logged into the next recordings' session log and NWB files."""

    planned_s: float
    lit_s: float
    percent: float
    wavelengths_nm: tuple[int, ...]
    duty_fraction: float
    ended: datetime

    def describe(self) -> str:
        leds = ", ".join(f"{nm} nm" for nm in self.wavelengths_nm)
        return (
            f"{self.lit_s / 60:.1f} of {self.planned_s / 60:g} min at {self.percent:g} % "
            f"LED power ({leds}), ended {self.ended:%Y-%m-%d %H:%M:%S %Z}; each LED pulsed "
            f"with the camera exposures, lit about {self.duty_fraction:.0%} of the time"
        )


class BleachDialog(QDialog):
    """Choose the bleach time and power; confirm the fiber ends are safe first."""

    def __init__(
        self,
        parent: QWidget | None,
        *,
        wavelengths_nm: tuple[int, ...],
        duty_fraction: float,
        uses_percent: bool,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Bleach patch cords")
        layout = QVBoxLayout(self)
        intro = QLabel(
            "Runs the enabled LEDs ("
            + ", ".join(f"{nm} nm" for nm in wavelengths_nm)
            + ") at one power for a set time, then switches them off, to bleach the "
            "patch cords' autofluorescence before recording. Nothing is recorded; the "
            "next recordings log the bleach."
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)
        form = QFormLayout()
        self.minutes_spin = QSpinBox()
        self.minutes_spin.setRange(1, BLEACH_MAX_MINUTES)
        self.minutes_spin.setValue(BLEACH_DEFAULT_MINUTES)
        self.minutes_spin.setSuffix(" min")
        form.addRow("Duration", self.minutes_spin)
        self.percent_spin = QSpinBox()
        self.percent_spin.setRange(5, 100)
        self.percent_spin.setValue(BLEACH_DEFAULT_PERCENT)
        self.percent_spin.setSuffix(" %")
        self.percent_spin.setEnabled(uses_percent)
        form.addRow("LED power", self.percent_spin)
        layout.addLayout(form)
        duty = QLabel(
            f"The LEDs pulse with the camera exposures: at the current frame rate and "
            f"exposure each is lit about {duty_fraction:.0%} of the time. Set the exposure "
            "to Max for the strongest bleach."
            + ("" if uses_percent else " This system sets LED voltages, so they stay as set.")
        )
        duty.setObjectName("hint")
        duty.setWordWrap(True)
        layout.addWidget(duty)
        self.safety_check = QCheckBox(
            "The fiber ends are capped or on a non-fluorescent target, no animal is "
            "attached, and nobody is looking into them"
        )
        layout.addWidget(self.safety_check)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        self.start_button = buttons.button(QDialogButtonBox.StandardButton.Ok)
        self.start_button.setText("Start bleaching")
        self.start_button.setEnabled(False)
        self.safety_check.toggled.connect(self.start_button.setEnabled)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def plan(self) -> BleachPlan:
        return BleachPlan(self.minutes_spin.value() * 60.0, float(self.percent_spin.value()))
