# Driftless Bundle Photometry

**Version 0.1.6** · [What's new](WHATS_NEW.md)

A Python-based, trace-first multichannel fiber-photometry acquisition system.

The application supports up to nine circular fiber ROIs, interleaved 405/470/565 nm
excitation, four edge-recording TTL inputs, optional lossless raw frames, and one
self-contained NWB-HDF5 output per ROI and animal. Windows is the physical-hardware
target; the simulator and GUI demo are cross-platform.

## Current implementation

- Immutable validation for sessions, channels, circular ROIs, camera settings, and
  TTL inputs.
- Deterministic simulator and a shared headless acquisition engine with explicit
  lifecycle states.
- Bounded, checksummed, chunked session spools that survive interruption and disk or
  finalizer errors.
- Canonical NWB finalization using PyNWB, `ndx-fiber-photometry`, and
  `ndx-ophys-devices`, including a separate subject, brain region, sensor type, and
  validated file for every ROI.
- Trace-first PySide6/pyqtgraph desktop demo with a separate calibration image and
  draggable circular fiber ROIs, plus separate live camera panels for 405, 470, and
  565 nm exposures.
- Selectable 15 s, 1 min, 10 min, 1 h, and Full trace horizons with display-only
  extrema-preserving decimation, keeping long recordings responsive without changing
  acquired or stored samples.
- One live plot row per ROI with overlaid 405, 470, and 565 nm traces, independent
  wavelength visibility controls, and Absolute or display-only dF/F views with a
  configurable per-ROI/per-wavelength median baseline window.
- Independent enable/disable controls for each ROI; disabled definitions remain in
  saved settings but are excluded from extraction, live traces, and NWB output.
- A required non-writing acquisition preview that validates timing, explicit
  wavelength coverage, commanded voltages, camera frames, ROI extraction, and
  saturation before enabling each recording.
- Synchronized numeric time axes across all ROI rows, horizontal/vertical drag-box
  zoom, double-click zoom reset, and page scrolling—not graph zoom—under the mouse
  wheel.
- Clean numeric entry fields without embedded increment/decrement stepper buttons.
- Versioned, human-readable JSON settings with GUI actions to save, load, set startup
  defaults, and restore the complete setup from a previous DBF NWB recording.
- A raw uint16 reference frame and fixed-scale ROI-annotated diagnostic view for
  every wavelength observed in a recording are saved inside each ROI NWB file.
- Versioned/checksummed controller protocol, fail-safe lifecycle guard, and
  single-owner serial transport that sends a best-effort `STOP` on close.
- Lazy Thorlabs SDK detection that does not break simulator mode on macOS.

The CS505MU camera transport, serial controller transport, controller firmware, and
LED intensity electronics are not yet physically validated. Those require the real
Windows rig, vendor SDK, trigger cabling, DAC/driver hardware, and bench timing and
safety measurements. Simulator success is not evidence of physical validation.

The historical MATLAB, Arduino, and Bonsai sources remain under `old mescoscope/`
for reference and are not runtime dependencies.

## Saving and restoring settings

The Configuration panel provides **Save JSON**, **Load JSON**, **Load NWB**, and
**Set as default**. A settings file includes all session metadata, recording duration,
output location, camera and retention settings, excitation states and voltages, TTL
configuration, live-display preferences, and every ROI's animal metadata and camera
geometry.

On Windows, DBF resolves the same Documents folder used by Explorer, including
OneDrive or other redirected locations, and uses its `Driftless Bundle Photometry`
folder for settings. **Set as default** writes `default_settings.json` there; DBF
loads that file automatically at startup. Save and Load dialogs open in the same
folder by default.

Every newly recorded per-ROI NWB file embeds the complete settings snapshot, so any
one of those files can restore the whole multi-ROI setup. DBF can also import older
per-ROI DBF NWBs: it gathers matching sibling files to recover all available ROIs and
warns when an old file never recorded a setting that must use a safe default.

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
[the controller protocol](docs/controller-protocol.md). Development priorities,
dependencies, and phase exit criteria are maintained in [the roadmap](roadmap.md).
