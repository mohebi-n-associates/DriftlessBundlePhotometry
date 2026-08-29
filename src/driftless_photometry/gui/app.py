"""GUI application bootstrap."""

from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication

from .branding import logo_path
from .main_window import MainWindow


def run_gui(
    *,
    output_directory: Path,
    default_duration_s: float = 5.0,
    default_fibers: int = 3,
    default_raw: bool = False,
) -> int:
    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName("Driftless Bundle Photometry")
    app.setApplicationDisplayName("Driftless Bundle Photometry")
    with logo_path() as icon_path:
        app.setWindowIcon(QIcon(str(icon_path)))
    window = MainWindow(
        output_directory=output_directory,
        default_duration_s=default_duration_s,
        default_fibers=default_fibers,
        default_raw=default_raw,
    )
    window.show()
    return app.exec()
