# What's New

This document tracks user-visible changes as they are made. Add work to
**Unreleased** first, including changes that have not shipped yet. When publishing a
version, move its completed entries into a dated version section.

## Unreleased

- No changes yet.

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
