# What's New

This document tracks user-visible changes as they are made. Add work to
**Unreleased** first, including changes that have not shipped yet. When publishing a
version, move its completed entries into a dated version section.

## Unreleased

### Review fixes — 2026-10-22

This work addresses the repository review of acquisition safety, scientific data
integrity, and operator workflow. Changes are uncommitted at the user's request.

- **Reject an ambiguous first RWD frame, including run zero.** The first frame must
  arrive before the next possible exposure from that camera could have completed.
  Previously a missing first 410 nm frame could make every following 410/470 label
  swap while the file still validated. The new causal arrival bound forces an
  invalid span and sequencer restart instead. It also rejects sufficiently late
  delivery of a valid first frame, particularly at high rates; reduce the rate or
  camera/USB latency if attribution fails. This conservative behavior is intentional.
  The new gate is simulator-tested and still needs hardware revalidation.
- **Close hardware before draining a failed acquisition.** Recording, preview, and
  live view explicitly close their packet generators on the owner thread. A retained
  exception traceback can no longer keep an RWD generator suspended with LEDs on
  while the recovery writer drains. Cleanup failures are attached to the error and
  retained in the acquisition fault record.
- **Attempt the entire off sequence after a failed acknowledgement.** Sequencer,
  all four TTL outputs, and every LED enable/power command are attempted independently
  with bounded acknowledgement waits. Closing releases the port and reports any
  unconfirmed shutdown instead of silently succeeding. Device checks also report
  shutdown failure. An unresponsive or unplugged RWD still requires the unit's power
  switch; this software cannot add a hardware watchdog.
- **Act on a TTL stop when it is received.** Recording stop no longer waits up to
  ten seconds for the TTL clock estimator to settle. The final drain retains the
  aligned edge and its original tick. Preview/live view do not use recording stop
  triggers. TTL alignment keeps a bounded recent frame history.
- **Verify scientific content before accepting recovered files.** Reopening checks
  every retained raw `uint16` frame, trace timestamps and values, commanded-intensity
  timestamps, excitation/TTL/drop/event-mark/system records, invalid spans, ROI
  geometry, calibration references, overlays, and raw-frame time/wavelength metadata.
  Frame and event checks use bounded batches instead of materializing a camera-event
  DataFrame. The old endpoint-only image check and missing-TTL-table loophole could
  accept altered files and then remove their only good recovery spool. Invalid
  canonical outputs are refused and the spool is kept. Original complete outputs
  from an interrupted promotion remain recoverable without a recovery marker.
- **Persist the actual session UTC origin after a delayed/TTL start.** A source
  reports session zero, the spool writer atomically saves it before committing frames,
  and finalization uses that saved configuration. RWD records the host UTC estimate
  and trigger/receipt uncertainty; it does not claim an independently calibrated UTC
  clock. The earlier setup timestamp remains a system event. Relative exposure and
  TTL timestamps retain their shared session clock.
- **Bound the GUI handoff and display history.** Workers retain latest images per
  camera/wavelength and queue at most one lightweight notification. The GUI drains
  at 10 Hz, retaining trace extrema when a backlog must be aggregated. Display trace
  histories and event graphics are capped; acquired samples/events still go directly
  to the spool without display decimation. Aggregated display history is identified,
  and live dF/F calculated from it is explicitly approximate. Rolling F0 evaluates
  requested bins in bounded batches, avoiding a dense allocation from session zero
  for a small baseline window. Full-session metadata loading/NWB table construction
  still grows with recording length; a streaming finalizer and measured long-run
  memory gate remain follow-up work in `roadmap.md`.
- **Preserve placement while changing fiber count.** Existing circles on both
  cameras and their subject metadata stay in place when fibers are added or removed.
  Only new fibers receive default circles. Recording and preview lock circle editing;
  live view continues to allow placement.
- **Make Stop reachable throughout the workflow.** A persistent header button and
  Escape shortcut stop acquisition, preview, live view, output tests, and power-meter
  measurements. Closing waits asynchronously for device cleanup and file validation,
  rather than blocking the GUI for 15 seconds and potentially destroying a running
  thread. Device discovery itself finishes its bounded calls before close.
- **Separate demonstration from physical operation.** `dbf --demo` always uses the
  Custom simulator, skips saved defaults, and refuses RWD imports/selection. Use
  `dbf --gui` or the installed desktop shortcut for hardware support. This prevents
  a demonstration command from opening the controller or lighting a real fiber.
- **Expose recovery and recording review in the GUI.** Configuration has an
  interrupted-recording recovery dialog with background checksum inspection, counts,
  per-spool errors, validation, and a keep-spool option enabled by default. A corrupt
  spool does not hide other sessions. Acquisition errors show the actual recovery
  path only when one exists and put the traceback in expandable details. Review setup
  lists wavelengths, subjects, timing, output location, storage estimate, and any
  remaining demonstration metadata on RWD. Completion shows frame/TTL counts and
  output paths. Storage estimates now account for metadata duplicated into every ROI
  file, including trace-only recordings.
- **Fit ordinary laptop screens.** The initial window is 1280 × 720 with a 960 × 600
  minimum; settings, camera setup, and trace rows remain scrollable. Offscreen visual
  checks covered 1366 × 768 with 1, 3, and 9 fibers. Physical hardware and platform
  display scaling were not validated by those checks.

Validation: targeted timing, shutdown, sparse-TTL, corruption/recovery, delayed-start,
mailbox pressure, rolling-baseline, ROI-placement, simulator isolation, and GUI
tests were added. The final full run passed **364 tests**, with **1 skip** for a
non-Windows boundary test, in 274 seconds (`python -m pytest -q -p no:cacheprovider`).
`ruff check .` and `ruff format --check .` passed (106 files). `git diff --check`
passed, and `AGENTS.md` / `CLAUDE.md` still have identical SHA-256 hashes. CLI help
recognizes both GUI launch modes. No physical hardware, new installer, or
clean-machine test was run; the bench and finalization-memory gates above remain open.

### Review fixes, second pass — 2026-10-22

- Previews check commanded intensity again. On RWD, LED power may change during a
  preview only after each wavelength's first frame, which must match the settings;
  Custom previews check every frame.
- Pressing **Fire** while a TTL start is armed no longer drives the output early.
  Output triggers wait for the recording to start, and an output mark before run 0
  is stamped at session zero rather than at the raw host clock.
- A *When I press Stop* recording sizes its free-space check by the **Expected
  length** field instead of the 24 h limit, so raw-frame recordings are not refused.
- Settings with markers on keys 5–9 or more than four named TTL events keep them when
  the GUI rebuilds the settings; the panel says what it kept.
- Stopping a recording that was still waiting to start keeps the passed preview.
- Rolling-F0 and control-fit dF/F compute only over the visible span plus one window,
  and the control fit uses strided samples of both traces.
- GUI workers move back to the GUI thread before their run ends, so the window owns
  and deletes them; the retired-worker list and `deleteLater` wiring are gone.
- The delayed start, output test, and power measurement share one `poll_until`
  helper that keeps the controller link serviced while they wait.

- The live trace y axis reads *Mean pixel intensity (counts)*, without pyqtgraph's
  automatic SI prefix, which showed values above 1000 as "kcounts".

- Each RWD camera has its own gain: camera 1 (410/470 nm) and camera 2 (560 nm) get
  separate sliders, both adjustable live in Live view. Settings store camera 2's gain
  only when it differs (`camera.camera2_gain`), and NWB files carry one camera model
  per gain when they differ.

- The first-frame arrival limit now applies only where it is the sole evidence of a
  missed first trigger: camera 1 with 410 + 470 nm alone. With all three LEDs, camera
  1's 1-2-1-2 slot spacing exposes a shifted start on its next frame, and single-
  wavelength cameras cannot be mislabelled, so first frames that arrive a few
  milliseconds late no longer fail previews and recordings at 120 Hz. On the R821 the
  first frame took about 11 ms at 120 Hz against a 13.7 ms limit, so busy hosts failed.
  410 + 470 nm alone at high rates still fails closed.

- On Windows, DBF sets its own taskbar identity, so the taskbar shows the DBF logo
  rather than Python's icon when it runs from a Python environment.

- Live view also draws the live traces on *Acquire*, from the circles' current
  positions, so moving a circle or changing gain or LED power shows in the traces while
  you switch tabs. It stays display-only: nothing is recorded, and each camera and
  wavelength is drawn at most 15 times a second.
- Each camera panel on *Camera & fiber ROIs* shows every ROI's mean intensity (counts)
  on the latest frame in its top-right corner, with its wavelength and any saturated
  fraction, in live view, preview, and recording. Camera 1 follows 470 nm (410 nm when
  470 is off), so its readout does not alternate between wavelengths.

- TTL input edges are drawn on the live traces as thin dashed lines, one colour per
  input, as in DriftlessFLIP. A **TTL** switch and one switch per input above the
  traces hide or show them; notes and named events always stay visible. The display
  keeps the latest 2000 edges; the NWB keeps every edge.
- Timestamped operator notes: while recording, tick the ROIs a note is about (all
  are ticked by default), type it and press Return or **Add timestamped note**. Notes
  are timed at submission through the latest camera frame, drawn on those ROIs' trace
  rows, and listed under **Show notes**. Each ticked ROI's NWB file stores its own
  entry in the `event_marks` table (source `note`, new `fiber_id` column).
- TTL inputs can be assigned to ROIs (the button beside each input's label, *All ROIs*
  by default). Each ROI's NWB file then keeps only its own inputs' edges and named
  TTL events, so animals in separate boxes keep their own behavior signals.

- Each NWB file now also holds, per wavelength, a mean reference image: the mean of
  10 consecutive frames from 5 s into the recording (`mean_reference_{nm}nm`), and the
  ROI drawn on it (`mean_roi_overlay_{nm}nm`). Recordings shorter than that average
  their latest frames. The original single raw frame stays as `raw_reference_{nm}nm`.

- Each recording now gets its own folder, `<output root>/<session id>/`, holding:
  - every ROI's NWB file, plus the recovery spool while recording;
  - `session_log.txt`, a detailed log (settings, ROIs, TTL inputs, frame and TTL
    counts, and a timeline of system events, markers, notes, TTL events, stimulation,
    invalid spans, and dropped frames) with the NWB file list;
  - `settings.json`;
  - `roi_map_{nm}nm.png`: the mean reference with every ROI on that camera outlined
    and named ("ROI 1 - Fiber 1") in its colour, auto-contrasted like the live view (0.5-99.9th percentile; the log gives the levels).

  Each NWB file carries the same log (scratch `driftless_session_log`) and the
  all-ROI maps: fixed-scale `roi_map_{nm}nm`, as the data contract requires for
  annotated copies, and `roi_map_autocontrast_{nm}nm`, identical to the PNG. Settings saved in the NWB keep the output root, so
  reloading them does not nest recordings. Recovery finds spools in recording folders
  (`<id>/<id>.photometry-spool`) as well as directly under the root.

- Every ROI circle in calibration and live view (both cameras) is named beside it
  ("ROI 2 - NAc left") in its colour; the name follows the circle as it is dragged and
  the ROI label as it is edited. The Live wavelength images tab outlines and names the
  ROIs too. Display only.

- A **Rows** menu above the live traces has a checkbox per ROI, plus *Select all* and
  *Select none*; only checked ROIs' traces are shown, sharing the whole trace area, so
  one checked ROI fills it for a closer look. The menu stays open while you tick. The time span, zoom,
  Y-axis mode, smoothing, TTL switches, and marks all keep working, and the choice
  carries over to the next preview or recording. Display only. Rows ticked back after
  *Select none* fill the trace area again instead of staying squashed at the top.

### Added

- Started the RWD direct-control track, which will let DBF drive the RWD controller
  and cameras without RWD's software. Added a codec for the recovered controller serial
  protocol whose frames match those produced by RWD's own encoder, and
  `--decode-rwd-usb-capture` (with optional `--rwd-usb-csv`) to decode USBPcap
  captures of RWD software sessions. The track also adds a design record and a
  bench-capture procedure. Bench tests on a physical RWD controller confirmed the
  framing, CRC, escaping, acknowledgements, version handshake, heartbeat, LED power and
  enable, continuous and interleaved modes, and starting and stopping excitation.
  Camera tests confirmed that the board triggers only the camera for the active LED,
  that the frame rate is shared across interleaved LEDs, that every sequencer restart
  begins on the cycle's first LED, and that the exposure setting is the per-trigger LED
  pulse length.
  Host-loss tests showed that the RWD board keeps its LEDs on when the host goes silent,
  closes the port, or is unplugged, so any future RWD transport must always send the off
  sequence itself. TTL input edges carry a millisecond board-clock timestamp. Nothing in
  the application sends commands to RWD hardware yet. A loopback test confirmed that the
  TTL output pulse trains match their configured frequency and width.

- Added the RWD controller transport, a simulated RWD board, and an Alvium camera
  adapter (VmbPy, via the new `rwd` extra) as the building blocks for RWD acquisition.
  Both were checked on an R821, and the RWD rig source below uses them.

- RWD settings now describe the RWD hardware as it is:
  - exact 410, 470, and 560 nm LEDs, with power in percent;
  - a separate camera-2 circle per fiber for 560 nm;
  - an optional controller COM port.

  A system profile drives both validation and the excitation panel. Validation rejects
  RWD settings the board cannot run: the 410+560 nm pair, fractional or out-of-range
  frame rates, exposures past the per-frame-rate ceiling, and a cropped sensor. The
  setup window is the same for both systems. Choosing RWD swaps in the RWD
  wavelengths, a percent power slider, and the Alvium camera; switching back restores
  the Custom camera.

- Added the RWD rig source, which records an RWD session as DBF frames. It:
  - configures the controller and cameras and runs the sequencer;
  - derives each exposure's wavelength and time from camera identity and the board's
    trigger grid, marking the identity as phase-derived;
  - restarts the sequencer and records an invalid span whenever a frame is missing,
    repeated, or off-grid;
  - places TTL edges on the session clock from their board ticks.

  The board is switched off on every exit path. It is tested against a simulated R821
  and its cameras but has not yet recorded on hardware.
- Acquisition now records invalid spans and system events reported by the
  acquisition source. After a stop request it reads until the source ends, so the
  source can hand over its final events.
- RWD sessions can now be previewed, recorded, spooled, recovered, and written to NWB
  by the shared engine.
  - The 560 nm traces use each fiber's camera-2 circle.
  - Each exposure's camera, commanded power in percent, camera timestamp, and
    phase-derived wavelength source are kept through the spool (schema v3) into
    every NWB file.
  - RWD files name both Alvium cameras as photodetectors, store a commanded-power
    series in percent per LED, and draw each wavelength's ROI overlay on its own
    camera.
- Added [the RWD operator guide](docs/rwd-direct-operator-guide.md). It covers safety
  (the unit's power switch is its only fail-safe), installation, placing fiber circles
  on both cameras, recording, what the NWB files contain, and troubleshooting.
- Added **Live view**, which streams the cameras with the current excitation
  settings. Use it to focus the fiber bundle and place each fiber's circles,
  including the camera-2 circles on RWD. Its button sits on *Camera & fiber ROIs* and
  becomes **Stop live view** while it runs. It records and validates nothing, stops
  on that button, on Stop, or by itself after 10 minutes, and on RWD switches the board
  off afterwards.
  Camera views gain an **Auto contrast** option, on by default, which stretches only
  the display. Recorded frames and NWB reference images keep their fixed scaling.
- *Camera & fiber ROIs* now has camera settings:
  - frame rate, limited to the RWD board's 10–250 Hz;
  - exposure, with **Max** for the longest the frame rate allows;
  - camera gain in dB.

  On RWD it shows camera 1 (410/470 nm) and camera 2 (560 nm) side by side. Gain
  changes apply at once during live view. Frame-rate and exposure changes restart
  it. The settings are saved and recorded like every other setting.
- The RWD hardware panel has a **Devices** list. It shows whether the controller
  answers on its COM port, with model and firmware, and whether cameras `1` and `2`
  are connected, with model and serial number. DBF checks when RWD mode is chosen and
  again on **Check devices**. The check switches no LED on and leaves the board off.
- Each ROI tab has a typed **Diameter** for its circle, and on RWD one per camera, as
  well as dragging. RWD circles are kept between OFRS's 40 and 544 px.
- DBF now tracks how long the RWD LEDs have been on and shows it under **LED warm-up**.
  RWD recommends 5 minutes of warm-up before recording, and Live view, preview and
  recording all count. **Record** asks before starting a recording with less than
  5 minutes of warm-up. A gap of up to 3 minutes, such as the preview, keeps the
  warm-up. The reminder can be switched off in the panel.
- Recordings have start and stop options, as in OFRS:
  - **Start:** immediately, after a delay of up to 24 h, or on a chosen TTL input
    edge;
  - **Stop:** after the duration, on **Stop** (at most 24 h), or on a chosen TTL
    input edge, with the duration as the maximum length.

  DBF connects and opens the cameras before it waits, with the LEDs off. For a TTL
  start on RWD, it runs the sequencer with every LED disabled at 0 % so the board
  reports inputs. **Stop** while waiting records nothing. The NWB system events record
  the trigger (`recording_start_trigger`, `recording_stop_trigger`), including the
  start edge's board tick. The options are saved with the settings and need no new
  preview.
- Event marking:
  - The new *TTL inputs and events* panel sets which TTL inputs are recorded, their
    labels and edges.
  - Name keys 1–4 as markers. While recording, press the key or its button to mark
    the event.
  - Name TTL pulses on an input by their width, for example *Lever* for 50–150 ms.

  Marks show as dashed lines on the live traces. Each NWB file stores them in a new
  `event_marks` table with their source (key, button, or ttl). Operator marks are
  timed on the host clock through the latest camera frame. TTL marks take the
  pulse's rising edge on the board clock. Settings files keep them.
- Sequential excitation: recordings can light the LEDs for an on time and then keep
  them dark for an off time (1 s to 24 h each), repeating, to limit photobleaching.
  - **On RWD:** the board runs dark between windows, so TTL inputs and a TTL stop
    still work. Each window relights on schedule, within a frame period in
    simulation.
  - **Records:** `excitation_paused` and `excitation_resumed` events mark the windows.
    The gaps are intentional, so they are not marked invalid.
- The live traces have a third Y axis mode, **dF/F, control fit**. Each signal's F0
  is the isosbestic control (410 nm on RWD, 405 nm on Custom) fitted to that signal
  by least squares over the recording so far. It changes only the display, and the
  settings keep it.
- RWD TTL outputs run programs:
  - **Start:** at the recording's start, on a **Fire** button, or when one ROI's live
    dF/F crosses a threshold;
  - **Timing:** a delay; timed trains with gaps and cycles; further steps with their
    own pulses; repeats of the whole program.

  Each train is recorded as an `output` event mark with its length, for example for
  optogenetic stimulation, and is shaded on the live traces. **Test** drives an output
  for 1 s with the LEDs off, after asking. Existing settings keep their continuous
  trains. Trains are timed from the host to within a few milliseconds.
- **Measure power** in the RWD panel reads a Thorlabs PM100 or PM400 power meter
  over VISA (new `power-meter` extra, `pyvisa`). After asking, it lights each enabled
  LED alone, continuously, at its set power and sets the meter's wavelength
  correction. It lists each LED's power in mW and switches every LED off again. It
  has not yet been run with a physical meter.
- RWD recordings end with a camera-diagnostics event: frames delivered, incomplete
  frames, and frames lost to a full host queue.

### Changed

- **Stop** ends a running preview; the preview button no longer doubles as a stop
  button.
- Every failure dialog is also appended to `logs/dbf-errors.log` in the settings
  folder. When an RWD camera delivers no frame after a sequencer start, the message
  now gives the frame rate, exposure, trigger period, LED powers, and the cameras'
  delivered and incomplete frame counts.
- The live traces add a **dF/F, rolling F0** mode. F0 at each moment is the median
  of the preceding *Baseline* seconds, taken over short bin means, so slow drift such
  as bleaching or changing room light drops out and brief transients remain.
  **dF/F, control fit** now hides the 405/410 nm trace, which is already divided out
  of every signal.
- **Run preview** now runs until **Stop** (10 minutes at most), so it can be
  used to set LED powers and inspect the signal. On RWD the power sliders stay live
  during preview and live view: each change restarts the board's LED cycle with the
  new power, records an `rwd_led_power_changed` event, and marks the short gap invalid.
  A preview validates recording only if it ended on the current settings.
- With no saved settings, the application now starts on the RWD system.
- The *Acquire* tab scrolls its settings column and its live traces separately, so
  the window keeps a fixed height. Each ROI's trace row is a fixed 230 px; more ROIs
  scroll within the trace area. LED excitation now sits above *TTL inputs and
  events*, and camera gain is a slider with a typed value beside it.
- Recovery spools moved to schema v5, which adds the event-mark log. Spools from
  schemas 1–4 still load.
- The setup window keeps each finished worker until Qt has deleted it. Dropping it at
  once could delete it from two threads at the same moment and crash DBF after a
  device check.
- The Alvium cameras now stream into 64 buffers instead of 16 (about half a second
  each at 120 frames/s). With 16, camera 1 occasionally delivered an incomplete frame
  at 120 frames/s, which cost a sequencer restart.

- The recovery spool's queue now holds 30 s of trace-only frames (raw-frame recordings
  are capped at about 1 GB). A multi-second disk stall no longer ends a high-rate
  recording; the old 64-frame queue lasted half a second at 120 frames/s.
- Recovery spools moved to schema v4. Committed chunks are listed in an append-only
  index, so each commit costs the same however long the recording runs. Under the
  old manifest list a 120 frames/s recording fell behind after about 20 minutes.
  Chunks hold about a quarter second of frames at high rates. An interrupted spool now
  recovers even when its event logs end with lines written after its last manifest
  update. Spools from schemas 1–3 still load.
- RWD timing at high frame rates:
  - the board's 120 Hz period (8.333 ms) is modelled correctly;
  - the trigger-period check is tighter;
  - the restart start-delay check is skipped above 50 Hz, where host timing scatter
    made it fire falsely.
- RWD timing and robustness fixes from DBF's own runs on an R821:
  - Exposures at frame rates that do not divide 100 000 µs (for example 15 or 30 Hz)
    are now timed on the board's real trigger period, 1/fps truncated to 10 µs.
    Timing them at exactly 1/fps would have drifted against TTL edges by up to
    0.25 %.
  - Repeated controller messages that the board resends when an acknowledgement is
    late are ignored, so they no longer stop a recording.
  - TTL edges from before the first exposure are counted instead of recorded.
  - The acquisition loop no longer waits on an idle serial port, and ROI masks are
    cached, so 50 Hz recordings keep up.
  - The stall check judges missing frames by arrival time, not by how far the loop
    has got.
  - Camera discovery re-scans briefly when a camera is not listed yet.
- RWD sessions now time each sequencer start from before the enable command is sent,
  and the Alvium adapter skips non-USB3 cameras such as Vimba X's simulators, which
  shortens RWD startup. Both changes came from the first DBF-driven runs on an R821.
- NWB frame events now record each exposure's camera index, camera hardware
  timestamp, and wavelength source for both systems, and the recovery spool moved to
  schema v3. Earlier spools still load and recover. The Custom simulator now refuses
  RWD settings.
- The setup window's RWD mode now previews and records on the RWD hardware. Its
  RWD panel:
  - warns that the unit has no fail-safe and names the power switch;
  - picks the controller's COM port;
  - configures pulse trains on the four TTL outputs.

  The camera calibration view switches between camera 1 (410/470 nm) and camera 2
  (560 nm). There each fiber's camera-2 circle is placed separately, over that
  camera's latest frame.
- Setup now offers exactly two acquisition systems: **Custom** (DBF's own camera and
  timing controller, simulator-backed today) and **RWD** (direct control of RWD
  hardware).
- Settings files are now format v3 with source kinds `custom` and `rwd`. Settings v1
  files and v2 `native` sources migrate to Custom. v2 RWD TCP-bridge sources migrate to
  RWD mode. Their session and fiber metadata are kept. Channels reset to the RWD LEDs
  at 5 % and the camera to the Alvium defaults. Circles are rescaled to fit the RWD
  sensor if needed. A warning names each change and every dropped connection field.
  The same migration applies to settings embedded in earlier NWB recordings.

### Removed

- Removed the read-only bridge to RWD's exported TCP stream, including `--rwd-settings`,
  `--rwd-wire-capture`, `--rwd-replay`, `--inspect-rwd-capture`, the trace-only RWD
  spool and NWB writer, and the RWD TCP GUI fields. It had only synthetic validation
  and is superseded by RWD direct control. `--inspect-spools` lists any `*.rwd-spool`
  as unsupported and `--recover-spool` refuses it; recover such spools with DBF 0.2.1.

## 0.2.1 - 2026-09-02

### Added

- Added a versioned 64-bit Windows installer build. Each published GitHub release now
  freezes the GUI and scientific stack, compiles a per-user Setup executable, tests
  silent installation, launch, and uninstall, and attaches it with SHA-256 checksums
  while attempting GitHub build provenance when the repository plan supports it.
- Added synchronized PyPI and GitHub Release automation using Trusted
  Publishing. Exact tag/version/changelog checks prevent mismatched releases, and
  the wheel and source distribution must pass tests and strict metadata validation
  before production publication.
- Added separate end-user installer, developer Conda, and PyPI installation paths,
  plus a release guide covering protected environments and one-time publisher setup.

### Changed

- The Conda environment now installs the project editably with GUI, development, and
  release dependencies from `environment.yml`.

## 0.2.0 - 2026-09-02

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

### Fixed

- Made trace-horizon button selection use the button group's stable integer signal,
  avoiding a PySide6 6.8/macOS callback-binding failure seen in the release matrix.

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
