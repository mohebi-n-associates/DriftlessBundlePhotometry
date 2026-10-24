"""Dark and light themes: the window stylesheet and the plot colours.

The dark theme is DBF's original look. The light theme maps its neutral colours
(backgrounds, borders, text) to light ones and keeps the accents: the teal action
colour, status colours, and wavelength colours, with a few rules overridden where an
accent would be hard to read on white.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from driftless_photometry.settings import settings_root

THEMES = ("dark", "light")
_PREFERENCES_NAME = "ui_preferences.json"

DARK_STYLESHEET = """QMainWindow, QWidget { background: #081521; color: #e8f7fa; }
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
QLabel#roiReadout { color: #e8f7fa; font-family: Consolas, monospace; }
QLabel#roiReadout[state="saturated"] { color: #ff8791; font-weight: 700; }
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

_LIGHT_COLORS = {
    "#081521": "#f3f6f9",
    "#0d2132": "#e6edf3",
    "#203e52": "#c9d6e0",
    "#f4fcfd": "#0f2233",
    "#e8f7fa": "#16293a",
    "#6f91a6": "#5a7184",
    "#43d6df": "#00838d",
    "#102a3e": "#e3ecf2",
    "#2b5167": "#b9cad6",
    "#8cabbc": "#4d6a7f",
    "#0c1d2b": "#ffffff",
    "#203c50": "#cfdbe4",
    "#10283a": "#ffffff",
    "#31566d": "#b3c4d1",
    "#0d1f31": "#edf1f5",
    "#22394d": "#d6dfe6",
    "#5f7387": "#8a9aa8",
    "#15374d": "#dde7ee",
    "#315b71": "#b3c4d1",
    "#205170": "#cddbe5",
    "#61778a": "#8a9aa8",
    "#13283a": "#e3e9ee",
    "#7694aa": "#5b7385",
    "#ff8791": "#c62f3c",
    "#3a1820": "#fde4e6",
    "#ffb3b9": "#9b1c28",
    "#f2b134": "#a86a00",
    "#2b2316": "#fdf1dc",
    "#315570": "#b3c4d1",
    "#102b43": "#e0eefb",
    "#66c2ff": "#1f6fb2",
    "#103226": "#def5e8",
    "#102638": "#ffffff",
    "#23465b": "#cfdbe4",
    "#17384d": "#d6e0e8",
    "#4c7893": "#8fa6b7",
}

# Accents kept from the dark theme that need a darker shade as text on white.
_LIGHT_OVERRIDES = """
QLabel#previewStatus[state="passed"] { color: #1d7a47; }
QLabel#deviceStatus[state="ok"] { color: #1d7a47; }
QLabel#voltageReadout { color: #007780; }
QSlider::handle:horizontal { background: #ffffff; }
QTabBar::tab:selected { color: #04151e; }
QGroupBox QLabel { background: transparent; }
"""


@dataclass(frozen=True, slots=True)
class PlotColors:
    background: str
    foreground: str
    title: str
    label_fill: tuple[int, int, int, int]
    """Behind ROI names, which sit on camera images: dark in both themes."""


PLOT_COLORS = {
    "dark": PlotColors("#081a2b", "#9fc4dc", "#e8f7fa", (4, 14, 22, 170)),
    "light": PlotColors("#ffffff", "#3d5566", "#13283a", (4, 14, 22, 170)),
}


def stylesheet(theme: str) -> str:
    if theme == "dark":
        return DARK_STYLESHEET
    if theme != "light":
        raise ValueError(f"unknown theme {theme!r}")
    mapped = re.sub(
        r"#[0-9a-fA-F]{6}",
        lambda match: _LIGHT_COLORS.get(match.group(0).lower(), match.group(0)),
        DARK_STYLESHEET,
    )
    return mapped + _LIGHT_OVERRIDES


def load_theme() -> str:
    """The operator's saved theme, or dark."""

    try:
        saved = json.loads((settings_root() / _PREFERENCES_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "dark"
    theme = saved.get("theme") if isinstance(saved, dict) else None
    return theme if theme in THEMES else "dark"


def save_theme(theme: str) -> None:
    if theme not in THEMES:
        raise ValueError(f"unknown theme {theme!r}")
    path = settings_root() / _PREFERENCES_NAME
    try:
        saved = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(saved, dict):
            saved = {}
    except (OSError, ValueError):
        saved = {}
    saved["theme"] = theme
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(saved, indent=2) + "\n", encoding="utf-8")
    except OSError:
        pass  # a display preference; the theme still applies for this session
