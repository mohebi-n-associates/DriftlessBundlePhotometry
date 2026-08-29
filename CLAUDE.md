# Driftless Bundle Photometry — Agent Contract

`AGENTS.md` and `CLAUDE.md` must remain byte-for-byte identical. Any change to one
must be copied to the other in the same commit. The test suite enforces this.

## Mission

Build a reliable, Python-based multichannel fiber-photometry acquisition system.
The supported hardware target is Windows. macOS must support the simulator,
replay, NWB writing/reading, analysis, and GUI demo; physical camera operation on
macOS is best-effort only.

The MATLAB applications, Arduino firmware, Bonsai files, and manual are historical
design evidence. They are not specifications to port verbatim.

## Scientific and product invariants

- The primary workflow is photometry traces, not an imaging dashboard.
- Camera imagery is used for calibration, focus, ROI placement, and diagnostics.
- Support 1–9 circular fiber ROIs with stable IDs, labels, positions, and radii.
- Support up to three configured excitation wavelengths: 405, 470, and 565 nm.
- Normal acquisition interleaves exactly one excitation wavelength per exposure.
  Do not combine wavelengths in one monochrome exposure unless a future hardware
  design provides a validated unmixing mechanism.
- The total camera frame rate is shared across enabled wavelengths.
- Raw camera frames are optional. When retained, preserve original `uint16` values;
  never normalize them per frame.
- Support four behavior TTL inputs and retain both rising and falling edges.
- Never infer wavelength solely from frame parity/modulo. Persist an explicit
  controller sequence and excitation identity for every camera exposure.
- Preserve raw data. Corrections, fitted controls, dF/F, z-scores, and QC outputs
  are separate derived datasets.
- Never silently drop data. Queue pressure, dropped frames, malformed device
  messages, clock discontinuities, and writer failures must be observable.

## Timing and controller contract

The hardware controller is the deterministic timing authority. The intended design
uses one clock for camera trigger/exposure records, excitation gates, and all TTL
edges. Each exposure record includes a monotonic sequence number, wavelength,
controller tick, and commanded intensity.

The GUI must not directly bit-bang LEDs or use host scheduling for exposure-critical
timing. The legacy Arduino Uno and firmware may be reused only after electrical and
timing characterization. Prefer true DAC outputs (or a validated external DAC) for
0–5 V intensity commands and separate digital LED gates.

Every hardware command protocol must be versioned and include explicit HELLO/READY,
configuration acknowledgement, ARM/START/STOP, error reporting, and safe LED-off
behavior on disconnect or watchdog timeout.

## Canonical data contract

The final scientific artifact is one self-contained NWB-HDF5 file per session.
Temporary recovery journals/spools may be used during acquisition, but they are not
the canonical deliverable.

Use a hybrid of core NWB and `ndx-fiber-photometry`:

- `NWBFile`, `Subject`, `DeviceModel`, and `Device`: session and hardware provenance.
- `ndx-fiber-photometry`/`ndx-ophys-devices`: fibers, indicators, excitation
  sources, photodetector, filters, commanded voltage, and per-wavelength response
  series.
- One raw `FiberPhotometryResponseSeries` per enabled wavelength, shaped
  `[time, fiber]`, with explicit timestamps.
- Core ROI table and calibration image: authoritative circular camera-space ROIs.
- Core `ImageSeries`: optional embedded lossless chronological `uint16` frames.
- Core `EventsTable`: TTL edges, camera exposures, excitation events, dropped frames,
  and system events.
- `invalid_times`/`TimeIntervals`: bad continuous spans.
- Processing modules: immutable derived traces and QC.

Use NWB seconds on a shared session time base and preserve raw camera frame IDs and
controller ticks for forensic reconstruction. Use stable `fiber_id` values to map
camera ROIs to fiber-photometry table rows.

Write to a partial path, drain and close all writers, validate, reopen and verify
counts, then atomically promote to `.nwb`. Never label an unvalidated partial file
as complete.

## Architecture boundaries

- `config`: validated immutable session/camera/channel/ROI/TTL configuration.
- `domain`: typed frames, events, samples, and result records.
- `timing`: wavelength schedules, tick unwrapping, and clock mapping.
- `roi`: circular masks, hardware crop calculation, and signal extraction.
- `hardware.camera`: camera interface; simulator and Thorlabs implementations.
- `hardware.controller`: controller interface, protocol, and serial implementation.
- `acquisition`: explicit state machine and coordination; no GUI dependencies.
- `storage`: append-safe spool/recovery and NWB finalization/validation.
- `gui`: PySide6/pyqtgraph presentation; communicates with workers through signals.

Device-owning objects have one owner thread. HDF5/NWB writes have one writer owner.
Use bounded queues. The GUI thread must never poll hardware, write frames, or block on
acquisition loops.

State transitions are explicit:

`DISCONNECTED -> READY -> CALIBRATING -> ARMED -> RECORDING -> DRAINING -> READY`

`ERROR` is reachable from every active state, with deterministic cleanup and LEDs
off. Invalid transitions raise errors rather than being ignored.

## Development gates

Work in order and do not advance past a failing gate:

1. Domain/config/timing/ROI unit tests.
2. Tiny golden NWB fixture: round-trip, PyNWB validation, NWB Inspector, and exact
   frame/trace/event count checks.
3. Headless simulator and acquisition pipeline with bounded queues and recovery.
4. Fault tests: stop, queue full, disk error, malformed controller data, dropped
   frame, camera loss, and clock rollover.
5. GUI demo backed by the same headless services.
6. Windows camera/controller integration and bench characterization.
7. Packaging and clean-machine smoke tests.

Hardware-dependent work must have a simulator/replay test path. Do not claim camera
or electrical validation without the physical hardware and measurements.

## Code and repository rules

- Target Python 3.11–3.13 and use type annotations throughout.
- Prefer small pure functions and dependency-injected interfaces.
- Use Pydantic for persisted user/session configuration and dataclasses for hot-path
  runtime records.
- Use NumPy arrays with explicit dtype and documented axis order `[y, x]` or
  `[time, y, x]`.
- Use `pathlib.Path`; never add machine-specific absolute paths.
- Public behavior changes require tests. Bugs receive regression tests.
- Keep optional hardware imports lazy so simulator mode works without vendor SDKs.
- Before implementing or changing the Thorlabs camera adapter, read
  `docs/thorlabs-camera-python-api.md`; it records the vendor API lifecycle, DBF
  integration decisions, and the boundary between documented behavior and required
  CS505MU bench validation.
- Do not edit generated/vendor/legacy files unless the task explicitly targets them.
- Do not commit recordings, build outputs, virtual environments, or secrets.
- Keep `WHATS_NEW.md` current for every user-visible change. Record work under
  `Unreleased` as it lands, including changes that have not shipped, and move those
  entries into the matching version section when a version is released.
- Use `roadmap.md` as the living development sequence. Update it when phase scope,
  dependencies, status, or exit criteria change, and never mark a phase complete
  without the evidence required by its exit criteria.
- Use `ruff` for lint/format and `pytest` for tests.
- Keep dependency versions bounded and test the exact NWB/PyNWB/NDX combination.

## Standard commands

```bash
python -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/python -m pytest
.venv/bin/python -m ruff check .
.venv/bin/python -m ruff format --check .
.venv/bin/python -m driftless_photometry --demo
```

On Windows, replace `.venv/bin/python` with `.venv\Scripts\python.exe`.

## Definition of done

A step is complete only when its automated tests pass, relevant validation succeeds,
failure behavior is surfaced, documentation reflects the behavior, and no unrelated
user files were changed. A simulator passing is not proof that physical hardware has
been validated.
