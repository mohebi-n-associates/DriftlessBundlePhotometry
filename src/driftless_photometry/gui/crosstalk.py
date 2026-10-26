"""Fiber crosstalk test: how much of one lit fiber's signal appears in the other ROIs.

With every fiber end capped, DBF averages each ROI's live means for a dark baseline.
Then a fluorescent target goes on one fiber at a time; the rise in every other ROI as a
percentage of the lit fiber's own rise is that pair's crosstalk. Live view supplies the
frames, so the test uses the live LED path and records nothing itself; later
recordings log the result.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

CAPTURE_S = 2.0
MIN_SAMPLES = 3
MIN_SOURCE_RISE_COUNTS = 20.0
"""A lit fiber must rise at least this far above dark for its row to be meaningful."""

Means = dict[int, dict[str, float]]
"""Per wavelength (nm), each fiber's mean live ROI value (counts)."""


def crosstalk_percent(
    dark: Means, lit: Mapping[str, Means]
) -> dict[int, dict[str, dict[str, float | None]]]:
    """Per wavelength: source fiber -> other fiber -> crosstalk %, or None if too dim."""

    result: dict[int, dict[str, dict[str, float | None]]] = {}
    for wavelength, dark_means in dark.items():
        rows: dict[str, dict[str, float | None]] = {}
        for source, captured in lit.items():
            means = captured.get(wavelength)
            if not means or source not in means:
                continue
            rise = means[source] - dark_means.get(source, 0.0)
            rows[source] = {
                other: (
                    None
                    if rise < MIN_SOURCE_RISE_COUNTS
                    else 100.0 * (value - dark_means.get(other, 0.0)) / rise
                )
                for other, value in means.items()
                if other != source
            }
        if rows:
            result[wavelength] = rows
    return result


@dataclass
class _Capture:
    target: str | None
    """The lit fiber, or None for the dark baseline."""
    first_s: float | None = None
    samples: dict[int, dict[str, list[float]]] = field(default_factory=dict)

    def add(self, wavelength: int, timestamp_s: float, values: Mapping[str, float]) -> bool:
        """Add one frame's ROI means; True once every wavelength has enough samples."""

        if self.first_s is None:
            self.first_s = timestamp_s
        per_fiber = self.samples.setdefault(wavelength, {})
        for fiber, value in values.items():
            per_fiber.setdefault(fiber, []).append(float(value))
        enough = all(
            min((len(v) for v in fibers.values()), default=0) >= MIN_SAMPLES
            for fibers in self.samples.values()
        )
        return enough and timestamp_s - self.first_s >= CAPTURE_S

    def means(self) -> Means:
        return {
            wavelength: {fiber: sum(v) / len(v) for fiber, v in fibers.items()}
            for wavelength, fibers in self.samples.items()
        }


class CrosstalkDialog(QDialog):
    """Guide the operator through dark and per-fiber captures and show the result."""

    def __init__(
        self,
        parent: QWidget | None,
        *,
        fibers: Sequence[tuple[str, str]],
        on_save: Callable[[str], None],
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Fiber crosstalk test")
        self.setModal(False)
        self.resize(620, 460)
        self._fibers = list(fibers)
        self._names = dict(fibers)
        self._on_save = on_save
        self._capture: _Capture | None = None
        self.dark: Means | None = None
        self.lit: dict[str, Means] = {}
        layout = QVBoxLayout(self)
        intro = QLabel(
            "1. Start Live view. 2. Cap every fiber end and press Capture dark. 3. Put a "
            "fluorescent target on one fiber (others stay capped), choose it, and press "
            "Capture fiber; repeat for each fiber. The table shows how much of the lit "
            "fiber's rise above dark appears in each other ROI. Keep LED power and gain "
            "unchanged during the test."
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)
        row = QHBoxLayout()
        self.dark_button = QPushButton("Capture dark")
        self.dark_button.clicked.connect(self._capture_dark)
        row.addWidget(self.dark_button)
        self.fiber_combo = QComboBox()
        for fiber, name in self._fibers:
            self.fiber_combo.addItem(name, fiber)
        row.addWidget(self.fiber_combo)
        self.fiber_button = QPushButton("Capture fiber")
        self.fiber_button.clicked.connect(self._capture_fiber)
        row.addWidget(self.fiber_button)
        row.addStretch(1)
        row.addWidget(QLabel("Wavelength"))
        self.wavelength_combo = QComboBox()
        self.wavelength_combo.currentIndexChanged.connect(self._show_table)
        row.addWidget(self.wavelength_combo)
        layout.addLayout(row)
        self.status = QLabel()
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.table = QTableWidget(len(self._fibers), len(self._fibers))
        names = [name for _fiber, name in self._fibers]
        self.table.setHorizontalHeaderLabels(names)
        self.table.setVerticalHeaderLabels([f"lit: {name}" for name in names])
        layout.addWidget(self.table, stretch=1)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self.save_button = QPushButton("Log in next recordings")
        self.save_button.setEnabled(False)
        self.save_button.clicked.connect(self._save)
        buttons.addWidget(self.save_button)
        close = QPushButton("Close")
        close.clicked.connect(self.close)
        buttons.addWidget(close)
        layout.addLayout(buttons)
        self.set_live(False)

    @property
    def capturing(self) -> bool:
        return self._capture is not None

    def set_live(self, live: bool) -> None:
        self._live = live
        idle = live and self._capture is None
        self.dark_button.setEnabled(idle)
        self.fiber_button.setEnabled(idle and self.dark is not None)
        if not live:
            self._capture = None
            self.status.setText("Start Live view to capture.")
        elif self._capture is None and self.dark is None:
            self.status.setText("Cap every fiber end, then press Capture dark.")

    def _capture_dark(self, *_args: object) -> None:
        self._start(None)

    def _capture_fiber(self, *_args: object) -> None:
        self._start(self.fiber_combo.currentData())

    def _start(self, target: str | None) -> None:
        self._capture = _Capture(target)
        what = "dark baseline" if target is None else self._names[target]
        self.status.setText(f"Capturing {what} for {CAPTURE_S:g} s…")
        self.set_live(self._live)

    def add_frame(self, wavelength: int, timestamp_s: float, values: Mapping[str, float]) -> None:
        """One live frame's ROI means for one wavelength (called by the main window)."""

        capture = self._capture
        if capture is None or not capture.add(wavelength, timestamp_s, values):
            return
        self._capture = None
        if capture.target is None:
            self.dark = capture.means()
            self.lit.clear()
            self.status.setText(
                "Dark captured. Put the target on one fiber, choose it, and press Capture fiber."
            )
        else:
            self.lit[capture.target] = capture.means()
            self.status.setText(f"{self._names[capture.target]} captured.")
        self._refresh_wavelengths()
        self._show_table()
        self.save_button.setEnabled(bool(self.lit))
        self.set_live(self._live)

    def result(self) -> dict[int, dict[str, dict[str, float | None]]]:
        return {} if self.dark is None else crosstalk_percent(self.dark, self.lit)

    def _refresh_wavelengths(self) -> None:
        current = self.wavelength_combo.currentData()
        wavelengths = sorted(self.dark or {})
        if [self.wavelength_combo.itemData(i) for i in range(self.wavelength_combo.count())] == (
            wavelengths
        ):
            return
        self.wavelength_combo.blockSignals(True)
        self.wavelength_combo.clear()
        for wavelength in wavelengths:
            self.wavelength_combo.addItem(f"{wavelength} nm", wavelength)
        index = self.wavelength_combo.findData(current)
        self.wavelength_combo.setCurrentIndex(max(0, index))
        self.wavelength_combo.blockSignals(False)

    def _show_table(self, *_args: object) -> None:
        rows = self.result().get(self.wavelength_combo.currentData(), {})
        for r, (source, _name) in enumerate(self._fibers):
            for c, (other, _other_name) in enumerate(self._fibers):
                if source == other:
                    text = "—"
                elif source not in rows:
                    text = ""
                else:
                    value = rows[source].get(other)
                    text = "too dim" if value is None else f"{value:.1f} %"
                item = QTableWidgetItem(text)
                item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                self.table.setItem(r, c, item)

    def summary(self) -> str:
        """One line for the session log: worst crosstalk per wavelength, plus all values."""

        result = self.result()
        worst = []
        for wavelength, rows in sorted(result.items()):
            values = [(v, s, o) for s, row in rows.items() for o, v in row.items() if v is not None]
            if values:
                value, source, other = max(values)
                worst.append(f"{wavelength} nm max {value:.1f} % ({source} -> {other})")
        matrix = {
            str(w): {
                s: {o: None if v is None else round(v, 2) for o, v in row.items()}
                for s, row in rows.items()
            }
            for w, rows in result.items()
        }
        return (
            "; ".join(worst or ["no lit fiber was bright enough"])
            + "; percent "
            + json.dumps(matrix, sort_keys=True)
        )

    def _save(self) -> None:
        self._on_save(self.summary())
        self.status.setText("The result will be logged in the next recordings.")
