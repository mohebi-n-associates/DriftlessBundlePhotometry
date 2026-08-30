"""Install a built wheel in isolation and exercise its public entry points."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import venv
from pathlib import Path


def _environment_python(environment: Path) -> Path:
    executable = "python.exe" if os.name == "nt" else "python"
    scripts = "Scripts" if os.name == "nt" else "bin"
    return environment / scripts / executable


def _run(
    command: list[str],
    *,
    cwd: Path,
    environment: dict[str, str],
) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        command,
        cwd=cwd,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )
    if completed.stdout:
        print(completed.stdout, end="")
    if completed.stderr:
        print(completed.stderr, end="", file=sys.stderr)
    return completed


def _isolated_environment() -> dict[str, str]:
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    environment["PYTHONNOUSERSITE"] = "1"
    environment.setdefault("QT_QPA_PLATFORM", "offscreen")
    return environment


def smoke_wheel(wheel: Path, *, mode: str, expected_version: str) -> None:
    wheel = wheel.resolve(strict=True)
    with tempfile.TemporaryDirectory(prefix="dbf-wheel-smoke-") as temporary:
        root = Path(temporary)
        environment_path = root / "environment"
        work = root / "outside-checkout"
        work.mkdir()
        venv.EnvBuilder(with_pip=True, clear=True).create(environment_path)
        python = _environment_python(environment_path)
        environment = _isolated_environment()
        requirement = f"{wheel}[gui]" if mode == "gui" else str(wheel)
        _run(
            [str(python), "-m", "pip", "install", "--disable-pip-version-check", requirement],
            cwd=work,
            environment=environment,
        )
        version = _run(
            [str(python), "-m", "driftless_photometry", "--version"],
            cwd=work,
            environment=environment,
        ).stdout.strip()
        if version != f"DBF {expected_version}":
            raise RuntimeError(f"unexpected installed version: {version!r}")
        if mode == "core":
            output = root / "recordings"
            completed = _run(
                [
                    str(python),
                    "-m",
                    "driftless_photometry",
                    "--headless",
                    "--duration",
                    "0.04",
                    "--fibers",
                    "1",
                    "--output",
                    str(output),
                ],
                cwd=work,
                environment=environment,
            )
            result = json.loads(completed.stdout)
            paths = [Path(path) for path in result["nwb_paths"]]
            if result["roi_files"] != 1 or not all(path.is_file() for path in paths):
                raise RuntimeError("isolated headless wheel smoke did not create its NWB output")
        else:
            probe = """
import json
from pathlib import Path
import driftless_photometry
from driftless_photometry import __version__
from driftless_photometry.gui.branding import logo_path
from driftless_photometry.gui.main_window import MainWindow
with logo_path() as logo:
    payload = {
        "version": __version__,
        "package_path": driftless_photometry.__file__,
        "window": MainWindow.__name__,
        "logo_exists": Path(logo).is_file(),
        "logo_size": Path(logo).stat().st_size,
    }
print(json.dumps(payload))
"""
            completed = _run(
                [str(python), "-c", probe],
                cwd=work,
                environment=environment,
            )
            result = json.loads(completed.stdout)
            package_path = Path(result["package_path"]).resolve()
            if (
                result["version"] != expected_version
                or result["window"] != "MainWindow"
                or not result["logo_exists"]
                or result["logo_size"] <= 0
                or not package_path.is_relative_to(environment_path.resolve())
            ):
                raise RuntimeError(f"isolated GUI wheel smoke failed: {result!r}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wheel-dir", type=Path, required=True)
    parser.add_argument("--mode", choices=("core", "gui"), required=True)
    parser.add_argument("--expected-version", required=True)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    wheels = sorted(args.wheel_dir.glob("*.whl"))
    if len(wheels) != 1:
        raise RuntimeError(f"expected exactly one wheel in {args.wheel_dir}, found {wheels}")
    smoke_wheel(wheels[0], mode=args.mode, expected_version=args.expected_version)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
