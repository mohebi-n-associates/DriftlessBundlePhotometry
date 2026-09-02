# Architecture and implementation gates

## Purpose

This application acquires camera frames from the proximal face of a fiber bundle,
coordinates interleaved excitation, extracts circular-ROI fluorescence signals,
timestamps behavioral TTL edges, and produces one validated NWB file per ROI and
animal.

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
                              +--> per-wavelength reference image spool
                              |
                              +--> optional bounded raw-frame spool

Per-exposure command, host-receipt, and saturation QC -----> provenance stream

TTL interrupt records -------------------------------------> event stream

trace + event + optional frame streams --> per-ROI NWB finalizer --> validate --> ROI files
```

The controller provides sequence identity and deterministic timing. The acquisition
coordinator verifies rather than invents wavelength assignments. The simulator uses
the same contracts.

Session settings use a discriminated acquisition source. `native` selects the
camera/controller/simulator contracts in this document. `rwd` is a read-only TCP
trace source with separately validated connection assumptions and device-channel
mappings. RWD never enters the native camera-exposure path and never fabricates
camera frames, ROI geometry, LED commands, or numbered TTL lines. Its evidence and
unknowns are specified in [the RWD streaming contract](rwd-streaming-protocol.md).
The pure RWD decoder consumes a byte stream rather than socket-read packets, yields
valid records in order before surfacing a later malformed record, and then remains in
a failed state. It never scans ahead after an error because the vendor format has no
documented synchronization marker.

The RWD persistence path is intentionally separate from native frame acquisition:

```text
RWD TCP bytes -> incremental decoder -> typed mixed records -> bounded RWD spool
                                                           -> tick normalization
                                                           -> per-fiber trace-only NWBs
                                                              -> validate/reopen/promote
```

The spool retains raw records and connection provenance before normalization. Its
finalizer copies shared stream/event/fault context into every mapped animal file,
while fluorescence tables and response series contain only that file's mapped device
channel. It stores raw uint32 ticks/values beside scaled series and never creates
camera, stimulus, exposure, or numbered-TTL data that the source did not provide.
The headless RWD coordinator owns the socket and decoder on its run thread and feeds
the bounded single-writer spool directly, so there is no unbounded intermediate
queue. Another thread may request stop; short socket polls make an idle stop prompt,
while a partially received record gets a bounded drain window and then becomes an
explicit truncation fault. One connection always defines one session. Disconnects
are terminal and never trigger an automatic reconnect or silent session join.

Native and RWD progress are projected into immutable live points with four fields:
session-relative display time, stable `fiber_id`, exact source wavelength, and raw
source-level value (native camera ROI count or configured-scaled RWD value). The GUI
keeps one timestamp/value buffer per fiber and wavelength; it therefore does not
assume synchronous vectors or native 405/470/565 identities. This adapter is
presentation-only. Native acquisition records and RWD wire records continue directly
to their separate canonical spools without passing through the display projection.

The desktop source selector is part of the persisted settings workflow. Native mode
shows camera ROI, excitation-voltage, raw-frame, and wavelength-image controls and
retains its mandatory preview. RWD mode shows read-only connection fields and
fiber/subject metadata, hides unsupported native hardware controls, and starts the
RWD coordinator directly because it cannot perform a camera/LED preflight.

Each enabled RWD fiber exposes an explicit vendor device-channel byte and a separate
operator-facing channel label. Device-channel uniqueness is a configuration
invariant; stable `fiber_id` remains the scientific identity and the label is copied
into that fiber's NWB photometry metadata. Imported mappings restore these fields
exactly instead of being reassigned by row order.

Connection/decoder counters, spool pressure, committed-record counts, and named RWD
event state cross the worker boundary as presentation diagnostics. Live smoothing is
an elapsed-time trailing mean applied after horizon selection and before optional
dF/F display conversion. These transformations operate only on bounded GUI buffers;
the raw source records continue unchanged to the recovery spool and NWB finalizer.

Physical protocol characterization uses a separate exact-wire evidence path:

```text
TCP recv chunk -> production decoder -> RWD recovery/NWB path
              \-> checksummed *.rwd-wire capture -> unpaced production-decoder replay
```

The capture retains each socket chunk and host receipt time, a parsing/remapping
contract, runtime provenance, resolved stream identity, terminal outcome, per-chunk
checksums, and a whole-stream digest. It is not a canonical scientific artifact and
never replaces the trace spool or per-fiber NWBs. Completed captures are atomically
promoted; incomplete `.partial` captures are retained but rejected for replay.

## Initial delivery gates

### Gate 0 — contracts and golden file

- Validated configuration models enforce 1–9 circular ROIs, unique TTL lines, and
  1–3 enabled wavelengths.
- Tick rollover and wavelength scheduling have unit tests.
- ROI extraction has numerical tests and never mutates source frames.
- A two-fiber, three-wavelength NWB fixture round-trips frames, traces, frame events,
  excitation events, actual commanded voltages, host receipt times, saturation QC,
  invalid intervals, TTL edges, ROI geometry, runtime provenance, and metadata.
- PyNWB validation and NWB Inspector pass at the pinned dependency versions.

### Gate 1 — headless acquisition

- Deterministic simulator emits explicit exposure records and synthetic TTL edges.
- State transitions are enforced.
- Optional frames are written through a bounded, committed chunk spool.
- Before arming, a conservative volume check estimates spool plus per-ROI NWB growth,
  including copied raw frames/reference images and a fixed free-space reserve.
- Progress snapshots expose queue depth/capacity, peak queue pressure, committed
  chunks/frames, last/maximum chunk latency, sequence drops, and host/controller
  transport-offset residuals. Finalization reports preparing, writing, validating,
  promotion, and completion as typed stages.
- Stop drains committed data and finalizes the NWB file.
- Typed source/coordinator faults stop the source first, then drain a machine-readable
  system event and zero-duration invalid marker before closing an incomplete spool.
  A writer failure remains a surfaced error even when the failed writer cannot commit
  its own fault record.
- Interrupted spools are discoverable and recoverable. Discovery examines only direct
  `*.photometry-spool` children. Inspection checksum-validates records before showing
  counts or completeness.

### Gate 2 — desktop demo

- PySide6 GUI configures a session and runs the same headless engine in a worker.
- pyqtgraph renders live traces and a limited-rate calibration image.
- Live trace horizons use a screen-width point budget and anchored min/max
  decimation; the display path never changes acquisition or stored samples.
- Live traces use one independently scaled row per ROI and overlay the enabled
  wavelengths. Visibility and Absolute/dF/F controls are display state; each dF/F
  baseline is calculated independently per ROI and wavelength and is not persisted
  as raw data.
- ROI rows receive one explicit shared numeric x-range rather than relying on
  geometry-dependent plot linking. The trace canvas routes wheel input to the parent
  page; dominant-axis drag boxes zoom x or y only, and double-click restores the
  selected horizon plus automatic y-ranges.
- A versioned JSON settings model round-trips every validated configuration field and
  is applied to the GUI as one unit. Startup defaults use the operator's visible
  Documents folder rather than a repository or hidden application directory.
- UI remains responsive during recording and finalization.
- macOS offscreen smoke test and manual demo pass.

### Gate 3 — physical hardware

- Thorlabs SDK adapter is tested on Windows with the CS505MU.
- Camera orientation, trigger semantics, frame IDs, ROI constraints, and sustained
  throughput are measured.
- Controller protocol and 0–5 V intensity hardware pass oscilloscope/photodiode tests.
- TTL edges, camera exposure, and excitation gates are demonstrated on one clock.
- Disconnect, watchdog, disk-full, stop, and application-close tests leave LEDs off.

## Per-ROI NWB organization

Every configured ROI maps to one animal and one self-contained NWB file. The file
contains that ROI's single-column fluorescence series and subject metadata. Shared
session timing, TTL edges, exposure provenance, calibration imagery, and optional
raw frames are copied into each file so it remains independently interpretable.
For every wavelength observed, it also contains the original unnormalized uint16
reference frame and a derived fixed-scale RGB view with that file's ROI outlined.

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
│   ├── excitation_events
│   ├── ttl_edges
│   └── system_events
├── intervals
│   └── invalid_times
└── processing/photometry
    ├── camera_rois
    ├── calibration_images
    ├── wavelength_roi_images      # raw reference + annotated view per wavelength
    └── future derived signals/QC
```

All time-series timestamps are explicit seconds from the NWB session reference time.
Raw controller ticks, camera frame IDs, actual commanded voltages, host receipt
times, and the represented ROI's saturation fraction are retained as separate event
columns. Commanded-voltage stimulus series contain one value for every observed
exposure. Derived signals never replace raw response series.

Every per-ROI NWB also carries a
`scratch/driftless_bundle_photometry_settings_json` value. This is a versioned JSON
provenance snapshot, not a competing scientific data representation. It includes all
configured ROIs even though each canonical output contains one animal's traces, so
any current-format file restores the whole setup exactly. The legacy importer can
assemble sibling DBF files written before this snapshot existed, but it reports
unrecoverable legacy fields instead of presenting inferred defaults as exact
provenance.

The separate `scratch/driftless_runtime_provenance_json` snapshot records the exact
application, Python, operating-system, dependency, adapter, and protocol versions.
Recovery spool schema v2 persists the per-exposure provenance needed to reconstruct
these fields. Schema-v1 spools remain readable, but recovery adds an explicit invalid
interval over the affected recording because actual host receipt times and dynamic
voltage commands were not historically available.

Recovery treats the per-ROI outputs as a validated set. A retry verifies session,
subject, full settings, runtime provenance, exposure identities/timestamps, trace
values, images, and event counts before reusing an existing final or partial file.
This allows safe resumption after interruption between validation and promotion.
Invalid partials may be regenerated from the unchanged committed spool; a mismatched
canonical `.nwb` is surfaced and never replaced automatically.

Software capacity gates use small deterministic fixtures: 300 frames/s trace-only
and 60 frames/s with retained 32 × 24 `uint16` frames, each for one ROI and one
non-realtime simulated second. They prove bounded-queue correctness at those fixture
sizes, not sustained physical-camera performance. Windows CS505MU rates, full-frame
raw retention, disk headroom, and long-run latency remain bench measurements.

## Known hardware boundary

The current repository does not contain a validated Python CS505MU driver or a
validated controller design. Simulator success proves software behavior only. The
hardware gate requires the actual Windows system, camera, LED driver/DAC, controller,
and timing measurement equipment.
