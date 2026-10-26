"""Entry point for the installed Windows desktop application."""

from __future__ import annotations

import sys
from collections.abc import Sequence
from pathlib import Path

from driftless_photometry.cli import main as cli_main


def main(argv: Sequence[str] | None = None) -> int:
    """Open the GUI by default while retaining diagnostic CLI arguments."""

    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments == ["--desktop-smoke"]:
        return _desktop_smoke()
    if not arguments:
        arguments = ["--gui"]
    return cli_main(arguments)


def _desktop_smoke() -> int:
    """Load the frozen Qt entry point, hardware libraries, and icon without a window.

    The RWD cameras need ``vmbpy`` (with its bundled VmbC library), the controller
    ``pyserial``, and the power meter ``pyvisa``; a release without them would run only
    the simulator, so a missing one fails the smoke test.
    """

    import importlib

    from driftless_photometry.gui.app import run_gui  # noqa: F401
    from driftless_photometry.gui.branding import logo_path

    if getattr(sys, "frozen", False):  # the installer must bundle them; dev setups may not
        for module in ("vmbpy", "serial", "pyvisa"):
            importlib.import_module(module)

    with logo_path() as icon:
        if not Path(icon).is_file():
            raise RuntimeError("packaged application icon is missing")
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised by the frozen executable
    raise SystemExit(main())
