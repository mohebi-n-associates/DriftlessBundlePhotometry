# What's New

This document tracks user-visible changes as they are made. Add work to
**Unreleased** first, including changes that have not shipped yet. When publishing a
version, move its completed entries into a dated version section.

## Unreleased

No changes yet.

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
