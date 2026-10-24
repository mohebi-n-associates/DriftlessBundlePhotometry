"""A preview of the RWD LED and camera timing for the current settings.

Each board trigger lights the cycle's next LED for the exposure field's length and
exposes that LED's camera (camera 2 only for 560 nm); one LED alone is driven
continuously (bench, 2026-10-20). Display only: drawn from the settings, not measured.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import QSizePolicy, QWidget

_CYCLES = 2


@dataclass(frozen=True, slots=True)
class TimingPlan:
    order: tuple[int, ...]
    """Enabled LEDs (nm) in the order the board cycles them."""
    period_s: float
    exposure_s: float
    camera_for: Mapping[int, int]

    @property
    def continuous(self) -> bool:
        return len(self.order) == 1

    @property
    def cycle_s(self) -> float:
        return self.period_s * len(self.order)

    def summary(self) -> str:
        if not self.order:
            return "No LED is enabled."
        per_led_hz = 1.0 / self.cycle_s
        cameras = sorted(set(self.camera_for[w] for w in self.order))
        exposure_ms, period_ms = self.exposure_s * 1000, self.period_s * 1000
        if self.continuous:
            lit = f"{self.order[0]} nm is lit continuously"
        else:
            duty = self.exposure_s / self.cycle_s
            lit = (
                f"each LED is lit {exposure_ms:.2f} ms every {self.cycle_s * 1000:.2f} ms "
                f"({duty:.0%} of the time)"
            )
        return (
            f"A trigger every {period_ms:.3f} ms, {exposure_ms:.2f} ms exposures "
            f"({period_ms - exposure_ms:.2f} ms gap for readout); each wavelength is sampled "
            f"at {per_led_hz:.2f} Hz; {lit}; camera"
            + ("s " if len(cameras) > 1 else " ")
            + " and ".join(str(c) for c in cameras)
            + "."
        )


class TimingDiagram(QWidget):
    """Two LED cycles: one row per LED, then one per camera's exposures."""

    def __init__(self, colors: Mapping[int, str], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._colors = dict(colors)
        self._plan: TimingPlan | None = None
        self.setMinimumHeight(120)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setToolTip(
            "LED pulses and camera exposures for two LED cycles, drawn from the current settings; "
            "dotted lines are the board's triggers"
        )

    def set_plan(self, plan: TimingPlan | None) -> None:
        self._plan = plan
        rows = (
            0
            if plan is None
            else len(plan.order) + len(set(plan.camera_for[w] for w in plan.order))
        )
        self.setFixedHeight(28 + 18 * max(1, rows))
        self.update()

    def rows(self) -> list[tuple[str, list[tuple[float, float, int]]]]:
        """(label, [(start_s, stop_s, nm)]) per row, for drawing and tests."""

        plan = self._plan
        if plan is None or not plan.order:
            return []
        span = plan.cycle_s * _CYCLES
        slots = [
            (k * plan.period_s, plan.order[k % len(plan.order)])
            for k in range(len(plan.order) * _CYCLES)
        ]
        rows: list[tuple[str, list[tuple[float, float, int]]]] = []
        for wavelength in plan.order:
            if plan.continuous:
                pulses = [(0.0, span, wavelength)]
            else:
                pulses = [(t, t + plan.exposure_s, w) for t, w in slots if w == wavelength]
            rows.append((f"{wavelength} nm", pulses))
        for camera in sorted(set(plan.camera_for[w] for w in plan.order)):
            exposures = [
                (t, t + plan.exposure_s, w) for t, w in slots if plan.camera_for[w] == camera
            ]
            rows.append((f"Camera {camera}", exposures))
        return rows

    def paintEvent(self, _event) -> None:
        plan = self._plan
        rows = self.rows()
        if plan is None or not rows:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        font = QFont(self.font())
        font.setPointSizeF(max(7.0, font.pointSizeF() - 1))
        painter.setFont(font)
        label_w, top, row_h = 70.0, 6.0, 18.0
        width = max(10.0, self.width() - label_w - 8)
        span = plan.cycle_s * _CYCLES
        text_color = self.palette().color(self.foregroundRole())
        grid = QColor(text_color)
        grid.setAlpha(60)
        for index, (label, pulses) in enumerate(rows):
            y = top + index * row_h
            painter.setPen(text_color)
            painter.drawText(
                QRectF(0, y, label_w - 6, row_h),
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                label,
            )
            painter.setPen(QPen(grid, 1))
            painter.drawLine(label_w, y + row_h - 3, label_w + width, y + row_h - 3)
            for start, stop, wavelength in pulses:
                color = QColor(self._colors.get(wavelength, "#8aa"))
                if label.startswith("Camera"):
                    color.setAlpha(150)
                x0 = label_w + width * start / span
                x1 = label_w + width * min(stop, span) / span
                painter.fillRect(QRectF(x0, y + 3, max(1.5, x1 - x0), row_h - 7), color)
        y = top + len(rows) * row_h
        painter.setPen(text_color)
        for k in range(len(plan.order) * _CYCLES + 1):
            x = label_w + width * (k * plan.period_s) / span
            painter.setPen(QPen(grid, 1, Qt.PenStyle.DotLine))
            painter.drawLine(x, top, x, y)
        painter.setPen(text_color)
        painter.drawText(
            QRectF(label_w, y, width, 16),
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            "0 ms",
        )
        painter.drawText(
            QRectF(label_w, y, width, 16),
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
            f"{span * 1000:.1f} ms",
        )
        painter.end()


def plan_for(
    wavelengths: Sequence[int], period_s: float, exposure_s: float, camera_for: Mapping[int, int]
) -> TimingPlan:
    return TimingPlan(tuple(sorted(wavelengths)), period_s, exposure_s, dict(camera_for))
