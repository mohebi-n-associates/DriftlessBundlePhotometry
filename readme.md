# Driftless Bundle Photometry

**Version 0.1.0** · [What's new](WHATS_NEW.md)

A Python-based, trace-first multichannel fiber-photometry acquisition system.

The application supports up to nine circular fiber ROIs, interleaved 405/470/565 nm
excitation, four edge-recording TTL inputs, optional lossless raw frames, and a
self-contained NWB-HDF5 session output. Windows is the physical-hardware target;
the simulator and GUI demo are cross-platform.

## Current implementation

- Immutable validation for sessions, channels, circular ROIs, camera settings, and
  TTL inputs.
- Deterministic simulator and a shared headless acquisition engine with explicit
  lifecycle states.
- Bounded, checksummed, chunked session spools that survive interruption and disk or
  finalizer errors.
- Canonical NWB finalization using PyNWB, `ndx-fiber-photometry`, and
  `ndx-ophys-devices`, including strict validation and round-trip verification.
- Trace-first PySide6/pyqtgraph desktop demo with a separate calibration image and
  draggable circular fiber ROIs.
- Versioned/checksummed controller protocol, fail-safe lifecycle guard, and
  single-owner serial transport that sends a best-effort `STOP` on close.
- Lazy Thorlabs SDK detection that does not break simulator mode on macOS.

The CS505MU camera transport, serial controller transport, controller firmware, and
LED intensity electronics are not yet physically validated. Those require the real
Windows rig, vendor SDK, trigger cabling, DAC/driver hardware, and bench timing and
safety measurements. Simulator success is not evidence of physical validation.

The historical MATLAB, Arduino, and Bonsai sources remain under `old mescoscope/`
for reference and are not runtime dependencies.

## Installation with Conda

Use `dbf` as the short environment and command name. The distribution keeps the
full `driftless-bundle-photometry` name for PyPI, and Python imports remain under
`driftless_photometry`.

Create and activate a clean Conda environment:

```bash
conda create --name dbf python=3.13 pip -y
conda activate dbf
python -m pip install --upgrade pip
```

Until the first PyPI release is published, install the current version directly
from GitHub, including the desktop GUI dependencies:

```bash
python -m pip install "driftless-bundle-photometry[gui] @ git+https://github.com/mohebi-n-associates/DriftlessBundlePhotometry.git@main"
```

After the package is published on PyPI, the equivalent installation will be:

```bash
python -m pip install "driftless-bundle-photometry[gui]"
```

Confirm the installation and launch the GUI:

```bash
dbf --version
dbf --demo
```

The long command remains available as `driftless-photometry`. Run a headless
simulated acquisition with:

```bash
dbf --headless --duration 5 --raw
```

On Windows and macOS, the Conda commands are the same. Physical camera operation is
supported on Windows; macOS supports the simulator and GUI demo.

## Development installation

Clone the repository, create the same short Conda environment, and install it in
editable mode with the development tools:

```bash
git clone https://github.com/mohebi-n-associates/DriftlessBundlePhotometry.git
cd DriftlessBundlePhotometry
conda env create --file environment.yml
conda activate dbf
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
python -m pytest
```

Run a headless simulated acquisition:

```bash
dbf --headless --duration 5 --raw
```

Run the GUI demo:

```bash
dbf --demo
```

Recover all committed data from a complete or interrupted spool:

```bash
dbf --recover-spool path/to/session.photometry-spool
```

Successful recovery validates the NWB before removing the spool. Add `--keep-spool`
to retain it for forensic inspection. Incomplete acquisitions are explicitly marked
in the NWB system event table.

Run the release gates:

```bash
python -m pytest --cov=driftless_photometry --cov-report=term-missing
python -m ruff check .
python -m ruff format --check .
python -m pip wheel . --no-deps --wheel-dir dist
```

See [the architecture](docs/architecture.md) and the synchronized agent contracts
in `AGENTS.md` and `CLAUDE.md`. The planned hardware wire contract is documented in
[the controller protocol](docs/controller-protocol.md).
