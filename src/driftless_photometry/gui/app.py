"""GUI application bootstrap."""

from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtGui import QIcon, QPixmap
from PySide6.QtWidgets import QApplication

from .branding import logo_path
from .main_window import MainWindow


def run_gui(
    *,
    output_directory: Path,
    default_duration_s: float = 5.0,
    default_fibers: int = 3,
    default_raw: bool = False,
    demo: bool = False,
) -> int:
    _set_windows_app_id()
    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName("Driftless Bundle Photometry")
    app.setApplicationDisplayName("Driftless Bundle Photometry")
    with logo_path() as icon_path:
        # Load the pixels now; the path may be temporary when running from a zip/bundle.
        icon = QIcon(QPixmap(str(icon_path)))
    app.setWindowIcon(icon)
    window = MainWindow(
        output_directory=output_directory,
        default_duration_s=default_duration_s,
        default_fibers=default_fibers,
        default_raw=default_raw,
        default_system="rwd",
        auto_check_devices=True,
        demo_only=demo,
    )
    window.show()
    return app.exec()


WINDOWS_APP_ID = "DriftlessBundlePhotometry.DBF"
"""Windows taskbar identity. Without one, Windows groups DBF under python.exe and shows
Python's icon instead of the window icon."""


def _set_windows_app_id() -> None:
    if sys.platform != "win32":
        return
    try:
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(WINDOWS_APP_ID)
    except (AttributeError, OSError):
        pass  # the taskbar falls back to the interpreter's icon; nothing else changes
