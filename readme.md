# Driftless Bundle Photometry

**Version 0.3.13** · [What's new](WHATS_NEW.md)

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
- Pre-arm storage-capacity checks and structured live diagnostics for queue pressure,
  write latency, dropped frames, clock residuals, and NWB finalization stages.
- Typed acquisition faults are committed as system events and invalid-time markers
  before an incomplete spool closes whenever committed samples can be recovered.
- Canonical NWB finalization using PyNWB, `ndx-fiber-photometry`, and
  `ndx-ophys-devices`, including a separate subject, brain region, sensor type, and
  validated file for every ROI.
- Exact per-exposure commanded voltage, controller sequence/tick, camera frame ID,
  host receipt time, per-ROI saturation QC, explicit excitation events, and marked
  invalid continuous spans in every independently interpretable animal file.
- Trace-first PySide6/pyqtgraph desktop demo with a separate calibration image and
  draggable circular fiber ROIs, plus separate live camera panels for each excitation
  wavelength (405, 470, and 565 nm on Custom; 410, 470, and 560 nm on RWD).
- Selectable 15 s, 1 min, 10 min, 1 h, and Full trace horizons with display-only
  extrema-preserving decimation, keeping long recordings responsive without changing
  acquired or stored samples.
- One live plot row per ROI with overlaid per-wavelength traces, independent
  wavelength visibility controls, and Absolute or display-only dF/F views with a
  configurable per-ROI/per-wavelength median baseline window or an F0 fitted from
  the 405/410 nm control.
- Optional display-only trailing-mean smoothing uses elapsed seconds, works with
  asynchronously sampled channels, and never changes acquired or stored values.
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
- Two acquisition systems, chosen first in setup and persisted in settings v3:
  **Custom** (DBF's own camera and timing controller, simulator-backed today) and
  **RWD** (direct control of an RWD R821 and its two cameras). Settings
  v1 and v2 files migrate explicitly with a visible warning.
- A raw uint16 reference frame and fixed-scale ROI-annotated diagnostic view for
  every wavelength observed in a recording are saved inside each ROI NWB file.
- Versioned/checksummed controller protocol, fail-safe lifecycle guard, and
  single-owner serial transport that sends a best-effort `STOP` on close.
- Lazy Thorlabs SDK detection that does not break simulator mode on macOS.

The CS505MU camera transport, serial controller transport, controller firmware, and
LED intensity electronics are not yet physically validated. Those require the real
Windows rig, vendor SDK, trigger cabling, DAC/driver hardware, and bench timing and
safety measurements. Simulator success is not evidence of physical validation.

RWD mode drives an RWD R821 controller board and its two Alvium cameras directly,
without RWD's software. It previews and records into the same per-ROI NWB files as the
Custom system. Each exposure's wavelength is derived from the camera and the LED cycle
and is marked as phase-derived. The RWD unit has no fail-safe: if DBF cannot switch
its LEDs off, the unit's power switch must. Read
[the RWD operator guide](docs/rwd-direct-operator-guide.md) before using it. The design and
bench evidence are in [the design record](docs/rwd-direct-control.md). An offline
decoder for USB captures of RWD software sessions is also included
(`dbf --decode-rwd-usb-capture`). The earlier read-only bridge to RWD's TCP data
stream was removed after 0.2.1.

The historical MATLAB, Arduino, and Bonsai sources remain under `old mescoscope/`
for reference and are not runtime dependencies.

## Saving and restoring settings

The Configuration panel provides **Save JSON**, **Load JSON**, **Load NWB**, and
**Set as default**. A settings file includes all session metadata, recording duration,
output location, Custom/RWD system selection, camera and retention settings,
excitation states and voltages, TTL configuration, live-display preferences, and every
fiber's animal metadata. Settings v1 files migrate to the Custom system because they
predate system selection. Settings v2 files migrate with a warning: their `native`
source becomes Custom, and their RWD TCP-bridge source becomes RWD with every dropped
connection field named.

On Windows, DBF resolves the same Documents folder used by Explorer, including
OneDrive or other redirected locations, and uses its `Driftless Bundle Photometry`
folder for settings. **Set as default** writes `default_settings.json` there; DBF
loads that file automatically at startup. Save and Load dialogs open in the same
folder by default.

Every newly recorded per-ROI NWB file embeds the complete settings snapshot, so any
one of those files can restore the whole multi-ROI setup. DBF can also import older
per-ROI DBF NWBs: it gathers matching sibling files to recover all available ROIs and
warns when an old file never recorded a setting that must use a safe default.
Each file also embeds a separate runtime provenance snapshot with the application,
Python, operating-system, dependency, adapter, and protocol versions that governed
acquisition or recovery.

## Installation choices

End users on 64-bit Windows should use the standalone installer. Developers should
use the repository's Conda environment. PyPI is also available for users who already
manage Python environments. The standalone installer includes Python and all normal
application dependencies; it does not require Conda or a separate Python install.

### Windows installer for end users

1. Open the [latest GitHub release](https://github.com/mohebi-n-associates/DriftlessBundlePhotometry/releases/latest).
2. Download `Driftless-Bundle-Photometry-<version>-Windows-x64-Setup.exe`.
3. Optionally compare its SHA-256 digest with `SHA256SUMS.txt` from the same release.
4. Run the installer, then launch **Driftless Bundle Photometry** from the Start menu.

The installer is per-user, supports clean uninstall through Windows Settings, and
does not require administrator access. Current installers are not code-signed, so
Windows SmartScreen may show an unrecognized-publisher warning. The application can
run the simulator, but the physical Thorlabs/controller path must not be treated as
validated until the documented Windows bench gates pass.

### Conda environment for developers

Clone the repository and let `environment.yml` create the `dbf` environment plus an
editable install with GUI, test, lint, build, and release tools:

```bash
git clone https://github.com/mohebi-n-associates/DriftlessBundlePhotometry.git
cd DriftlessBundlePhotometry
conda env create --file environment.yml
conda activate dbf
python -m pytest
```

After pulling dependency changes, synchronize the existing environment with:

```bash
conda env update --file environment.yml --prune
```

### PyPI installation

Inside an existing Python 3.11–3.13 environment, install the desktop application
from PyPI and launch it with:

```bash
python -m pip install "driftless-bundle-photometry[gui]"
dbf --version
dbf --demo
```

The distribution name is `driftless-bundle-photometry`, the short command and Conda
environment are `dbf`, and Python imports use `driftless_photometry`. The long
`driftless-photometry` command remains available. On macOS, use Conda or PyPI for the
simulator, replay, NWB, analysis, and GUI-demo paths.

## Running developer workflows

Run a headless simulated acquisition:

```bash
dbf --headless --duration 5 --raw
```

Decode the RWD controller traffic in a USB capture of an RWD software session:

```bash
dbf --decode-rwd-usb-capture path/to/session.pcapng --rwd-usb-csv path/to/session.csv
```

Captures can contain experimental data and must not be committed to the repository.

Run the GUI demo:

```bash
dbf --demo
```

Demo mode always runs the Custom simulator and ignores hardware defaults. Open
`dbf --gui` or the installed desktop shortcut to use physical RWD hardware.
The GUI also offers **Review setup…**, a Stop button available on every tab
(Escape), and **Configuration → Recover an interrupted recording…**.

Recover all committed data from a complete or interrupted spool:

```bash
dbf --recover-spool path/to/session.photometry-spool
```

Inspect direct child spools without creating or removing files:

```bash
dbf --inspect-spools path/to/output-directory
```

Inspection recognizes `*.photometry-spool` children, verifies checksums, and reports
counts plus completeness, invalid times, system events, schema, and output-file count.
Spools from the removed RWD TCP bridge (`*.rwd-spool`) are listed as unsupported and
must be recovered with DBF 0.2.1. It deliberately does not recurse
into unrelated directories. Successful recovery validates the complete
NWB set before removing the spool; add `--keep-spool` for forensic retention.
Recovery is idempotent and can resume if a process stopped after validating partials
or while promoting the multi-ROI set. Existing canonical files must match the spool
exactly and are never overwritten silently. Incomplete acquisitions are explicitly
marked in the NWB system event table. Legacy schema-v1 spools remain recoverable, but
the resulting invalid-time record identifies the per-frame host receipt and commanded
intensity provenance that those older spools never persisted.

The storage estimate uses configured duration, total frame rate, image dimensions,
raw-retention policy, enabled ROI count, and a 64 MiB reserve. CI correctness tests
exercise non-realtime simulator sessions at 300 frames/s trace-only and 60 frames/s
with raw 32 × 24 frames for one ROI. These small-fixture bounds test queue/data
integrity; they are not throughput claims for the CS505MU or any physical rig.

Run the release gates:

```bash
python -m pytest --cov=driftless_photometry --cov-report=term-missing
python -m ruff check .
python -m ruff format --check .
python -m build
python -m twine check --strict dist/*
```

The complete release procedure and one-time Trusted Publisher setup are in
[the release guide](docs/releasing.md).

See [the architecture](docs/architecture.md) and the synchronized agent contracts
in `AGENTS.md` and `CLAUDE.md`. The planned hardware wire contract is documented in
[the controller protocol](docs/controller-protocol.md). Development priorities,
dependencies, and phase exit criteria are maintained in [the roadmap](roadmap.md).
