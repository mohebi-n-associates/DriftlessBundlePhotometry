# Architecture and implementation gates

## Purpose

This application acquires camera frames from the proximal face of a fiber bundle,
coordinates interleaved excitation, extracts circular-ROI fluorescence signals,
timestamps behavioral TTL edges, and produces a validated NWB session.

The main display is a live trace workspace. The image view is a calibration and
diagnostic tool rather than the primary acquisition interface.

## Runtime data flow

```text
Controller scheduler ---> exposure/wavelength records ----+
                                                        matcher
Camera worker ----------> uint16 frames -------------------+
                                                          |
                              +---------------------------+
                              |
                              +--> circular ROI extractor --> trace/QC stream
                              |
                              +--> optional bounded raw-frame spool

TTL interrupt records -------------------------------------> event stream

trace + event + optional frame streams --> NWB finalizer --> validate --> session.nwb
```

The controller provides sequence identity and deterministic timing. The acquisition
coordinator verifies rather than invents wavelength assignments. The simulator uses
the same contracts.

## Initial delivery gates

### Gate 0 — contracts and golden file

- Validated configuration models enforce 1–9 circular ROIs, unique TTL lines, and
  1–3 enabled wavelengths.
- Tick rollover and wavelength scheduling have unit tests.
- ROI extraction has numerical tests and never mutates source frames.
- A two-fiber, three-wavelength NWB fixture round-trips frames, traces, frame events,
  TTL edges, ROI geometry, and metadata.
- PyNWB validation and NWB Inspector pass at the pinned dependency versions.

### Gate 1 — headless acquisition

- Deterministic simulator emits explicit exposure records and synthetic TTL edges.
- State transitions are enforced.
- Optional frames are written through a bounded, committed chunk spool.
- Stop drains committed data and finalizes the NWB file.
- Interrupted spools are discoverable and recoverable.

### Gate 2 — desktop demo

- PySide6 GUI configures a session and runs the same headless engine in a worker.
- pyqtgraph renders live traces and a limited-rate calibration image.
- UI remains responsive during recording and finalization.
- macOS offscreen smoke test and manual demo pass.

### Gate 3 — physical hardware

- Thorlabs SDK adapter is tested on Windows with the CS505MU.
- Camera orientation, trigger semantics, frame IDs, ROI constraints, and sustained
  throughput are measured.
- Controller protocol and 0–5 V intensity hardware pass oscilloscope/photodiode tests.
- TTL edges, camera exposure, and excitation gates are demonstrated on one clock.
- Disconnect, watchdog, disk-full, stop, and application-close tests leave LEDs off.

## NWB organization

```text
NWBFile
├── general/devices
├── general/lab_meta_data/FiberPhotometry
├── acquisition
│   ├── raw_fluorescence_405
│   ├── raw_fluorescence_470
│   ├── raw_fluorescence_565
│   └── camera_frames              # optional
├── events
│   ├── camera_frames
│   ├── ttl_edges
│   └── system_events
└── processing/photometry
    ├── camera_rois
    ├── calibration_images
    └── future derived signals/QC
```

All time-series timestamps are explicit seconds from the NWB session reference time.
Raw controller ticks and camera frame IDs are retained as separate columns. Derived
signals never replace raw response series.

## Known hardware boundary

The current repository does not contain a validated Python CS505MU driver or a
validated controller design. Simulator success proves software behavior only. The
hardware gate requires the actual Windows system, camera, LED driver/DAC, controller,
and timing measurement equipment.
