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
        arguments = ["--demo"]
    return cli_main(arguments)


def _desktop_smoke() -> int:
    """Load the frozen Qt entry point and packaged icon without opening a window."""

    from driftless_photometry.gui.app import run_gui  # noqa: F401
    from driftless_photometry.gui.branding import logo_path

    with logo_path() as icon:
        if not Path(icon).is_file():
            raise RuntimeError("packaged application icon is missing")
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised by the frozen executable
    raise SystemExit(main())
