"""Brand asset helpers shared by the GUI bootstrap and main window."""

from __future__ import annotations

from contextlib import AbstractContextManager
from importlib.resources import as_file, files
from pathlib import Path


def logo_path() -> AbstractContextManager[Path]:
    """Provide a filesystem path for the packaged transparent logo image."""

    asset = files("driftless_photometry.gui.assets").joinpath("driftless-photometry.png")
    return as_file(asset)
