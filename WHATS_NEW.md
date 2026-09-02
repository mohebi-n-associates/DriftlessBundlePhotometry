# What's New

This document tracks user-visible changes as they are made. Add work to
**Unreleased** first, including changes that have not shipped yet. When publishing a
version, move its completed entries into a dated version section.

## Unreleased

### Added

- Added the evidence-based RWD read-only streaming contract, including fixed
  fluorescence/event layouts, exact 410/470/560 labels, TCP framing rules, event
  semantics, known ambiguities, and the replay/bench gates required before claiming
  live validation.
- Added a discriminated native/RWD session source. RWD settings persist host/port,
  timeouts, preamble policy, explicit timestamp and value scales, expected machine
  name, device wavelengths, and unique device-channel-to-fiber mappings.
- Added typed RWD fluorescence and event domain records that retain raw machine-name
  bytes, device channel, raw uint32 timestamps/values, exact wavelength identity,
  fixed event-name bytes, and ON/OFF status.
- Added a pure incremental RWD decoder for arbitrarily fragmented or coalesced TCP
  input, explicit/automatic four-byte preambles, fixed record validation, stable
  machine identity, configured channels/wavelengths, EOF truncation, and fail-closed
  malformed streams. Synthetic golden hex fixtures cover every documented wavelength
  mask, mixed data/events, rollover endpoints, and faults without containing animal
  data.
- Added a separate trace-only RWD recovery spool and canonical NWB finalizer. The
  bounded background writer checksums configuration, runtime provenance, connection
  metadata, mixed record chunks, system events, and invalid spans; recovery validates
  exact counts and resumes interrupted multi-fiber promotion before removing a spool.
- Each mapped RWD fiber/animal now gets an independently validated NWB containing
  exact 410/470/560 identities, raw uint32 ticks and vendor values, configured scales,
  rollover-resolved timestamps, scaled response series, named ON/OFF events, and
  complete settings/provenance. RWD files explicitly omit unsupported camera frames,
  native exposure/TTL tables, LED commands, and inferred signal roles.
- Added the headless read-only RWD TCP client and acquisition coordinator. One owner
  thread connects, incrementally decodes and spools records with short stop polling;
  connect failure, idle timeout, clean disconnect, truncation, malformed bytes, queue
  pressure, and storage failure remain distinct surfaced faults. Automatic reconnect
  is intentionally disabled so a gap or changed RWD state cannot be hidden.
- Added `dbf --rwd-settings FILE --duration SECONDS`, combined native/RWD spool
  inspection, and suffix-aware recovery. RWD capacity preflight uses a persisted
  maximum expected record-rate bound without changing the vendor acquisition rate.
- Added source selection at the top of desktop session setup. Choosing RWD switches
  to read-only connection settings, exact 410/470/560 visibility, and fiber/subject
  metadata while hiding camera calibration, raw-frame retention, LED voltage, and
  wavelength-image controls that the RWD stream cannot support.
- Native ROI-vector samples and asynchronous RWD device samples now feed one
  source-neutral immutable live-trace contract keyed by stable `fiber_id` and exact
  wavelength. Each fiber/wavelength curve owns its timestamps, so display-only
  windowing, extrema-preserving decimation, and dF/F no longer assume synchronized
  vector samples and never alter stored raw data.
- Added explicit RWD device-channel and user-facing channel-label fields for every
  enabled fiber/animal. Duplicate device channels fail validation, imported mappings
  restore exactly, and each label is embedded in the per-fiber photometry metadata.
- Added live RWD connection, byte/record decoder, bounded spool queue, committed
  record, and named event ON/OFF status. Existing finalization progress now applies
  to both native ROI files and RWD fiber files.
- Added a persisted display-only trailing-mean window based on elapsed seconds. Raw,
  smoothed, and dF/F views share independently timed fiber/wavelength buffers; the
  smoothing and dF/F pipeline never mutates acquired or stored values.
- Added a versioned exact-wire RWD bench artifact with socket-chunk receipt times,
  parsing/remapping contract, runtime provenance, resolved stream identity, terminal
  outcome, per-chunk checksums, and a whole-stream SHA-256 digest. Completed captures
  are atomically promoted; interrupted partials are retained and rejected for replay.
- Added capture inspection and deterministic unpaced replay through the production
  decoder, recovery spool, and per-fiber NWB finalizer. Replay rejects changed
  scientific parsing/mapping settings and preserves captured host receipt times.
- Added an RWD operator and physical-validation guide covering connection order,
  channel/animal mapping, recovery, exact-wire evidence, replay, failure handling,
  and the measurements required before claiming live hardware validation.
- Added typed acquisition-fault and system-event records for camera/controller
  disconnects, malformed controller streams, clock discontinuities, queue pressure,
  and storage failures. When committed samples exist, the fault classification and
  invalid-time marker are drained into the recoverable spool before it closes.
- Added structured diagnostics for bounded spool queue depth/capacity, peak pressure,
  committed chunks/frames, write latency, dropped frames, clock residuals, and each
  multi-ROI NWB finalization stage.
- Added conservative storage preflight before arming. Estimates account for the
  recovery spool, one independently useful NWB per ROI, optional duplicated raw
  frames, reference images, overhead, and a free-space reserve.
- Added read-only direct-child spool discovery and inspection with checksum-validated
  frame, trace, TTL, invalid-time, system-event, completeness, schema, and ROI counts.
  The CLI exposes this as `dbf --inspect-spools DIRECTORY`.
- Every native camera exposure now retains its actual commanded LED voltage, host
  receipt time, and per-ROI saturation fraction through the recovery spool and into
  each independently validated animal NWB file.
- Added explicit excitation event records and `invalid_times` intervals with both a
  human-readable reason and the originating fault/event.
- Added an embedded runtime provenance snapshot containing the application, Python,
  operating system, dependency, acquisition-adapter, and protocol versions.

### Changed

- Settings snapshots now use format v2. Version-1 settings migrate explicitly to the
  native camera/controller source with a visible warning; malformed or unknown
  versions remain rejected.
- Recovery is now idempotent. It validates and reuses matching canonical or partial
  ROI outputs, resumes an interrupted all-ROI promotion, and rewrites only a corrupt
  unvalidated partial. Existing canonical files are never silently replaced.
- Closing the GUI during recording or draining requests a safe stop and defers window
  destruction until the acquisition worker has drained and finished.
- Commanded-voltage series now contain one value per observed exposure instead of a
  single value reconstructed from the initial configuration. Interrupted recordings
  retain empty series for configured wavelengths that were not observed.
- Recovery spools now use schema version 2 while retaining explicit, marked recovery
  support for schema version 1 spools that did not store per-frame host receipt and
  intensity provenance.

## 0.1.6 - 2026-08-29

### Added

- Added read-only GitHub Actions CI across Windows and macOS on Python 3.11, 3.12,
  and 3.13, with lint, formatting, synchronized-contract, and full-suite gates.
- Added isolated core and GUI wheel smoke tests on Windows and macOS. They install
  the built artifact in a fresh environment outside the checkout, then verify the
  CLI version, headless NWB acquisition, GUI import, and packaged logo.
- Recording now requires a successful non-writing preview for the current settings.
  Preview displays live frames and ROI traces while checking frame/exposure ordering,
  explicit wavelength coverage and voltage commands, camera dtype/shape, finite ROI
  extraction, and saturation bounds; relevant setting changes require a new preview.
- Every ROI can now be enabled or disabled independently. Disabled definitions remain
  available in JSON/default settings and embedded recording settings, while only
  enabled ROIs are extracted, plotted, and written as per-animal NWB files.

### Fixed

- Made display-horizon button signals reliable across the supported Python and macOS
  combinations, and made the synchronized-agent-contract CI gate independent of Qt.

## 0.1.5 - 2026-08-29

### Added

- Added complete, versioned JSON configuration files covering session metadata,
  recording and camera settings, excitation channels, TTL inputs, display choices,
  and every subject-specific ROI and its exact geometry.
- Added GUI actions to save settings, load settings, load settings from a previous
  DBF NWB file, and make the current setup the startup default. The default and file
  picker location is the operator's real Windows Documents folder, including
  redirected Documents folders.
- Every new per-ROI NWB file now embeds the same complete configuration snapshot for
  exact restoration. Legacy DBF NWBs restore all recoverable settings and sibling
  ROIs while explicitly warning about fields those older files never stored.

### Changed

- The main window now opens at the same 2300 × 1300 size as DriftlessFLIP, with the
  same 1408 × 792 minimum size.
- Removed the small increment/decrement stepper buttons from every numeric input in
  the GUI. Values remain directly editable, and excitation voltages retain their
  dedicated sliders.
- The mouse wheel over the main trace display now scrolls the Acquire page instead
  of zooming a graph. Page scrollbars use a slimmer dark style without arrow buttons.
- Every ROI row now uses exactly the same numeric x-axis range. Drag a horizontal box
  for time-only zoom, drag a vertical box for y-only zoom, and double-click any row to
  reset all trace zoom to the selected time horizon and automatic y-ranges.

## 0.1.4 - 2026-08-29

### Added

- Live fluorescence is now arranged as one plot row per ROI, with the 405, 470,
  and 565 nm traces overlaid so fibers with very different signal ranges no longer
  distort one another's y-axis.
- Added independent 405, 470, and 565 nm visibility controls and an Absolute/dF/F
  display selector. dF/F uses a configurable initial-window median baseline computed
  separately for every ROI and wavelength; the transformation is display-only and
  never changes acquired or stored samples.

## 0.1.3 - 2026-08-29

### Added

- Added 15 s, 1 min, 10 min, 1 h, and Full live-trace display horizons modeled on
  DriftlessFLIP, with clear availability state during a recording.
- Live traces now use display-only, power-of-two anchored min/max decimation with a
  point budget tied to plot width. Every acquired value remains available to storage;
  only the graphics payload is reduced for long views.

## 0.1.2 - 2026-08-29

### Added

- Added a dedicated live camera tab with independent, continuously updated panels
  for 405, 470, and 565 nm exposures.
- Acquisition spools now preserve a representative original uint16 frame for every
  wavelength observed, including when full raw-frame retention is disabled.
- Every ROI/animal NWB file now includes the original per-wavelength reference
  frames and derived fixed-scale RGB copies with that file's ROI outlined.

## 0.1.1 - 2026-08-29

### Added

- Each ROI now has its own animal ID, brain region, sensor type, age, and sex.
- Every acquisition writes one validated NWB file per ROI and animal, with a
  single-column fluorescence series and shared timing, TTL, and frame provenance.
- Excitation voltage controls are now 0–5 V slider bars with live numeric readouts.
- The calibration workspace now provides a separate subject metadata tab for every
  ROI while keeping the experimenter as session-level metadata.
- Added the short `dbf` command for launching the GUI, running simulations,
  recovering spools, and checking the installed version.
- Added Conda installation instructions using a short `dbf` environment name and a
  working GitHub-based pip install while the first PyPI release is pending.
- Added a reusable `environment.yml` for creating the supported Python 3.13 Conda
  environment.
- Added a living development roadmap covering reliability, controller and camera
  integration, one-clock rig validation, operator workflows, replay and analysis,
  quality control, and release engineering.
- Added Thorlabs Python camera API integration notes covering SDK ownership,
  discovery, configuration, hardware triggering, frame polling and buffer lifetime,
  cleanup, error handling, and the remaining CS505MU bench-validation questions.

## 0.1.0 - 2026-08-29

### Added

- A new Driftless Bundle Photometry logo and application icon.
- A refreshed trace-first desktop interface with clearer session setup, live status,
  configuration summaries, and camera/ROI calibration workspace.
- The application version is now shown on the main page and in the window title.
- The PySide6 dependency is bounded to the tested 6.8 release line for reliable
  Windows and Python 3.13 startup.
- Simulator-backed acquisition for 1-9 circular fiber ROIs and interleaved 405, 470,
  and 565 nm channels.
- Validated, self-contained NWB output, optional lossless raw frames, TTL edge events,
  recovery spools, and explicit acquisition lifecycle states.
- A versioned controller protocol and lazy physical-camera integration boundary.

### Known limitations

- Physical camera, controller, firmware, trigger wiring, and LED electronics still
  require validation on the Windows hardware rig. Simulator success is not physical
  hardware validation.
