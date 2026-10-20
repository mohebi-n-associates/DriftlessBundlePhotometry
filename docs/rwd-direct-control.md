# RWD direct-control design record and controller protocol

**Status:** protocol codec and offline USB-capture decoder implemented and tested
against OFRS's own encoder. Bench tests on 2026-10-20 confirmed the framing, CRC,
escaping, acknowledgements, version handshake, model query, controller heartbeat, LED
power and enable, continuous and three-LED interleaved modes, and the sequencer
enable that starts and stops excitation (see [Bench evidence](#bench-evidence)). No DBF
product code sends commands to RWD hardware yet. Timing units and everything not
marked "bench" below remain unconfirmed.

## Decision

DBF's earlier read-only bridge left RWD's OFRS software in control of the hardware.
That kept OFRS's requirements (its Windows GUI and a Sentinel dongle for recording) and
exposed only OFRS's exported fluorescence values, not camera frames, exposures, or
controller events. The bridge was removed after 0.2.1.

In DBF's RWD mode, DBF drives the RWD controller board over its serial link and the
photometry cameras through their vendor SDK. Frames then go through the same camera
acquisition, ROI, recovery, and NWB path as the Custom system's `RigSource`. Setup
offers exactly these two systems.

Planned gated steps:

0. **Two-system setup — implemented.** RWD mode is selectable and persisted, but
   preview, acquisition, spool, and NWB services refuse it until step 5.
1. **Protocol codec and capture decoder — implemented (unvalidated).**
   `hardware/rwd_controller_protocol.py`, `hardware/rwd_usb_capture.py`, and
   `--decode-rwd-usb-capture`.
2. **Bench capture of OFRS traffic — hardware required.** Follow
   [the bench-capture procedure](rwd-direct-bench-capture.md), then promote captured
   frames to golden fixtures and resolve the open questions below.
3. **Serial transport — implemented; simulator-tested and bench-checked.**
   `hardware/rwd_transport.py` is a single-owner port. It handles:
   - handshake and identity check, automatic acks, and OFRS-style resends;
   - confirmation checks for each setting;
   - the host heartbeat, and faults on controller silence, link loss, or malformed
     frames.

   It sends the full off sequence on connect and on every close, because the board
   has no fail-safe (see [Bench evidence](#bench-evidence)).
   `hardware/rwd_board_simulator.py` reproduces the bench-measured firmware behavior
   for tests and simulation.
4. **Alvium camera adapter — implemented; simulator-tested and bench-checked.**
   `hardware/alvium.py` imports VmbPy lazily, finds the cameras by `DeviceUserID`, and
   applies OFRS's trigger setup in every session, restoring the previous settings on
   exit. It delivers `uint16` frames with hardware frame IDs and timestamps through a
   bounded queue, and counts incomplete frames and overflows.
5. **RWD rig source — in progress.** Pairs the transport and cameras, assigns
   wavelength identity as described below, and emits `RigPacket`s. Each fiber gets its
   own camera-2 ROI for 560 nm, placed separately as in OFRS (decided 2026-10-20).
   Built in five parts:
   1. **System profile and settings — implemented.** See
      [System profiles](#system-profiles).
   2. **Rig source — implemented; simulator-tested.** `hardware/rwd_rig.py` and
      `hardware/rwd_timing.py`; see [RWD rig source](#rwd-rig-source).
   3. **Camera-aware ROI extraction and NWB — implemented; simulator-tested.** The
      preview, acquisition, spool, recovery, and NWB services accept RWD sessions; see
      [RWD recordings](#rwd-recordings).
   4. **GUI controls — implemented; simulator-tested.** Preview and recording run on
      the RWD hardware. The RWD panel has the no-fail-safe warning, the COM port,
      and the TTL outputs. The calibration view has a camera selector, so each
      fiber's camera-2 circle is placed over camera 2's latest frame.
   5. **Bench validation and operator guide** — hardware required.
6. **Bench characterization — hardware required.** LED/trigger timing, the
   disconnect and watchdog LED state, TTL latency, and clock drift.

## Installing for RWD hardware

The RWD controller needs only the COM port that Windows creates for its FTDI bridge;
Windows Update installs that driver automatically. The cameras need Allied Vision
Vimba X with its USB transport layer, and DBF needs its Python binding:

```bash
python -m pip install "driftless-bundle-photometry[rwd]"
```

Start DBF after installing Vimba X, so it sees `GENICAM_GENTL64_PATH`.

## Approved exception: wavelength identity

The agent contract says never to infer wavelength solely from frame parity/modulo.
The RWD controller does not report per-exposure records, so this hardware cannot meet
that rule without modification. OFRS itself numbers frames from the moment the
sequencer is re-enabled and takes `frame number mod enabled-LED count` as the LED.

The approved method for the RWD direct track, recorded 2026-10-20:

1. **Camera identity.** The board triggers camera 2 only for 560 nm exposures and
   camera 1 only for 410 and 470 nm exposures (bench).
2. **Phase anchor.** Sequence position is counted per camera from the first exposure
   after the sequencer is re-enabled, mirroring OFRS's resync. In 50 sequencer
   restarts across all three interleaved modes, the first exposure was always the
   cycle's first LED (410 nm in modes 1 and 3, 470 nm in mode 2), and the cycle never
   skipped within a run (bench).
3. **Gap detection.** Every frame's hardware frame ID and hardware timestamp are checked
   against the commanded period. Any missing, duplicated, or late exposure ends the
   valid span, is recorded in `invalid_times`, and requires a resync before samples are
   attributed again. The exposure in flight when the sequencer is disabled can be cut
   short, so it is always marked invalid (bench).
4. **Independent check.** A per-wavelength brightness-pattern check runs as QC only. It
   never assigns wavelength.
5. **Provenance.** Every exposure from this backend records that its wavelength is
   phase-derived, so these data are never mistaken for controller-asserted identity.

`CLAUDE.md` and `AGENTS.md` record this scoped exception. `hardware/rwd_timing.py`
performs the assignment; [RWD rig source](#rwd-rig-source) describes how.

RWD hardware uses 410, 470, and 560 nm LEDs, while the Custom system's channel model is
405, 470, and 565 nm. `Wavelength` therefore has exact `CONTROL_410` and `RED_560`
members, and RWD exposures are never relabeled as Custom wavelengths.

## System profiles

The GUI is the same for both systems. A `SystemProfile` in `config.py` describes what
each system's hardware can do, and both validation and the GUI read from it. Custom
settings are unchanged. `profile_for(source)` picks the profile.

| Property | Custom | RWD R821 |
| --- | --- | --- |
| Excitation wavelengths | 405, 470, 565 nm | 410, 470, 560 nm |
| Intensity | `voltage_v`, 0–5 V | `power_percent`, 0–100 % |
| Cameras | 1 | 2 (410/470 nm on camera 1, 560 nm on camera 2) |
| Interleaved sets | any | one LED, 410+470, 470+560, or all three (no 410+560) |
| Frame rate | any | whole hertz, 10–250 Hz (total trigger rate) |
| Exposure | any | 500 µs to (1000 // fps) ms − 2.63 ms |
| Sensor | any | full 728 × 544 Alvium 1800 U-040m |
| TTL outputs | — | 4 |
| Hardware fail-safe | yes | **no** |

For RWD settings, validation also requires:

- every enabled ROI has camera-2 geometry whenever 560 nm is enabled;
- camera-2 circles fit the sensor;
- the trace display shows only the system's wavelengths.

Custom settings may not set camera-2 geometry. `RWDSourceConfig.controller_port`
optionally pins the controller's COM port.

Switching the GUI between systems swaps the channel group, the trace and image
wavelengths, and the camera template. Fiber circles are laid out again if they do not
fit the new sensor. A new camera-2 circle starts at its fiber's camera-1 position;
the calibration view's camera selector shows camera 2 so it can be placed.

`RWDSourceConfig.ttl_outputs` lists pulse trains (frequency in 0.01 Hz steps up to
655.35 Hz, pulse width in ms, shorter than the period) for any of the four BNC
outputs. The rig configures and enables them after the board settings and before
the sequencer starts. The transport's off sequence disables all four outputs at
the end. Whether outputs pause while the sequencer restarts is a bench item.

Settings v2 files for the removed TCP bridge migrate as follows:

- Session and fiber metadata are kept.
- Channels reset to the three RWD LEDs at 5 %. They stay enabled as the TCP stream
  listed them when the board can run that set; otherwise all three are enabled.
- The camera resets to the Alvium defaults.
- Circles are rescaled when they do not fit the 728 × 544 sensor, and camera 2 copies
  camera 1.
- The warning names each of these changes and every dropped TCP field.

## RWD rig source

`RWDRig` is the RWD `RigSource`. One `packets()` call owns the controller and the
cameras for a session. Its tests run against `SimulatedRWDBoard` and
`SimulatedRWDCameras` (`hardware/rwd_rig_simulator.py`), which reproduce the bench
behavior on a virtual clock. Nothing below has run on hardware yet; part 5 does that.

**Session lifecycle.**

1. Open the controller port: the configured `controller_port`, or the only FT232R
   port present. Several candidates are an error, so DBF never probes other FTDI
   devices. The transport handshakes and forces the board all-off. Any model other
   than the R821 (code 0) is refused.
2. Open only the cameras that an enabled LED triggers, then send every LED's power
   and enable, the camera timing, and the excitation mode.
3. Enable the sequencer. That starts run 0.
4. On a stop request, at the requested duration, or on any error, disable the
   sequencer. The transport then sends the full off sequence as it closes, including
   when the consumer abandons the generator.

**Wavelength and time.** `RWDExcitationPlan` lists the enabled LEDs in the board's
cycle order. `RWDSegmentTracker` gives each camera's first frame in a run that
camera's first slot in the cycle. Each later frame must have the next frame ID, and
its hardware timestamp must land on the camera's next slot. The tracker fits each
camera's clock period, so crystal differences of tens of ppm never accumulate.

Session time is the board's trigger grid. Slot `k` of run 0 is at `k / fps`, and
session zero is run 0's first trigger. A later run starts at the session time
measured on its first camera's hardware clock since that camera's last recorded
frame. Exposures carry:

- the slot time;
- a derived `controller_tick_us` equal to the slot time in microseconds;
- the commanded power in percent;
- the camera index;
- `wavelength_source = "phase_derived"`;
- the camera's own frame ID and timestamp.

Frames from both cameras are merged into slot order, and sequences stay contiguous
across runs.

**What breaks a run.** Any of these breaks a run:

- a missing, repeated, or reordered frame ID;
- a frame early, late, or after a missed slot (beyond a quarter period);
- a frame from a camera no enabled LED triggers;
- first frames of two cameras that do not line up within half a period;
- no frame within 2 s of a start, or 0.5 s past a slot's expected arrival;
- after a restart, an enable-to-first-frame delay that differs by more than half a
  period from the delay measured at session start. This catches a camera that missed
  its first trigger. Run 0 sets the baseline, so it cannot check itself.

**On a break,** the rig:

1. stops the sequencer;
2. discards the frames that can no longer be attributed and every frame still
   arriving, until frames have been quiet for 0.15 s;
3. starts a new run.

An invalid span then covers the time from the first unrecorded slot to the new
run's first trigger. A `rwd_sequencer_restart` event gives the reason and the number
of frames discarded. The board reports no TTL edges while its sequencer is stopped,
and that event says so.

Three consecutive restarts without a recorded frame raise `CAMERA_DISCONNECT`. A
fitted trigger period more than 0.2 % off the command raises `CLOCK_DISCONTINUITY`
rather than drifting. That would happen, for example, if firmware rounded 30 Hz to a
33 ms period.

**The exposure cut short by a stop is never recorded.** That is stricter than
marking it invalid:

- A session that reaches its duration stops only after a slot beyond the duration has
  arrived, so no recorded exposure was still in flight.
- After a stop request, frames that arrive later are discarded. A
  `rwd_stop_requested` event counts them.

**TTL edges.** Each edge keeps its raw board tick, unwrapped, as `controller_tick_us`
(1 ms resolution). The board's tick and its trigger grid come from one oscillator, so
a single offset places every edge on the session clock. `RWDTTLClock` estimates that
offset from the earliest frame and edge arrivals near each edge. It holds edges until
8 have arrived or 10 s have passed, then freezes the offset, so intervals between
edges stay exact. The offset's error is the difference between the camera readout
floor and the serial delivery floor: about 2 ms in simulation, and not yet measured
on hardware. Lowering the FTDI port's latency timer (Device Manager, port settings,
16 ms by default) should tighten it. Edges on lines with no configured input are
counted in a `rwd_ttl_edges_not_configured` event. A malformed controller frame
records an invalid span, because it may have been a lost edge.

**QC.** `RWDBrightnessCheck` flags a run whose first 410 and 470 nm frames look
swapped against earlier means (`rwd_brightness_qc_mismatch`). It never assigns or
changes a wavelength.

## RWD recordings

The shared engine records `RWDRig` packets like any other source:

- **ROIs.** `roi.rois_for_camera` gives the extraction each frame's own camera
  geometry, so 560 nm traces come from each fiber's camera-2 circle.
- **Preview.** It checks frame IDs per camera and compares commanded power, rather
  than voltage, against the settings.
- **Spool.** Spool schema v3 stores camera index, commanded power, wavelength source,
  and camera timestamps for every frame.

Each per-ROI NWB file then contains:

- `events/camera_frames` with `camera_index`, `camera_timestamp_ns`,
  `wavelength_source` (`phase_derived`), and `commanded_power_percent`;
  `excitation_events` carries the wavelength source and power too;
- `stimulus/led_<nm>_commanded_power`, a core `TimeSeries` in percent, one value per
  observed exposure. OFRS's power ratio is not a voltage, so RWD files have no
  commanded-voltage series;
- photodetectors `rwd_camera_1` and `rwd_camera_2` (Allied Vision Alvium 1800 U-040m),
  each linked from the FiberPhotometryTable rows it images;
- `processing/photometry/camera_rois` with the camera-2 circle columns, and each
  wavelength's reference overlay drawn on its own camera's circle;
- when raw frames are kept, one `camera_frames` series interleaving both cameras in
  exposure order. Its control codes are 0 = 410, 1 = 470, 2 = 560 nm;
- the rig's system events and invalid spans: restarts, the start delay, TTL alignment,
  and controller identity.

`SimulatedRig` refuses RWD settings, so an RWD recording can never be simulated by the
Custom simulator.

**Bench items for part 5:**

- the enable-to-first-trigger delay;
- the trigger period at frame rates that do not divide 1000 ms;
- the TTL offset error;
- the real camera readout latency against the stall and start checks at high frame
  rates.

## Recovered system architecture

Recovered from OFRS 2.0.0.38566 (decompiled .NET assemblies), its bundled camera
profiles, and the RWD user manual. No RWD code, keys, or binaries are in this
repository.

- **Controller board:** a microcontroller behind an FTDI USB-serial bridge. It is the
  timing master: it generates the camera triggers, gates the LEDs, reads four TTL
  inputs, and drives four TTL outputs.
- **Photometry cameras:** current units use Allied Vision Alvium 1800 U-040m (Sony
  IMX287, 728×544, Mono12); earlier units used Basler acA720-520um. OFRS sets
  `TriggerSelector=FrameStart`, `TriggerMode=On`, `TriggerSource=Line0` (`Line1` on
  Basler), `TriggerActivation=FallingEdge`, `ExposureMode=Timed`, and disables auto
  exposure and gain. `DeviceUserID` distinguishes camera 1 from camera 2, and camera 2
  is mirrored with `ReverseX`. The cameras power up untriggered, so these settings
  must be applied in every session (bench).
- **Camera I/O lines:** Lines 0–3 are inputs. Line0 carries the board's trigger and
  reads low at exposure start; Lines 1–3 read high throughout and carry no LED
  identity (bench).
- **Excitation:** a single LED runs continuously; LED combinations are interleaved
  with one LED per exposure. OFRS defines no 410+560 combination.
- **Signal:** OFRS averages pixels inside circular ROIs, the same model as DBF's `roi`.
- **Licensing:** OFRS checks its dongle on the PC before recording. The controller
  handshake contains no authentication; DBF never interacts with the dongle.

## Wire format

FTDI virtual COM port, 115200 baud, 8 data bits, no parity, 1 stop bit.

```text
7E | FF 03 00 21 | type | seq_hi seq_lo | function | data... | crc_hi crc_lo | 7E
```

- **Escaping:** between the flags, `7E` becomes `7D 5E` and `7D` becomes `7D 5D`.
- **CRC:** CRC-16/MODBUS (init `0xFFFF`, reflected polynomial `0xA001`) over
  `FF 03 00 21` through the last data byte, sent high byte first. OFRS does not verify
  the CRC on received frames; DBF does.
- **Types:** `0` error, `1` command, `2` ack, `3` reply, `4` info.
- **Acknowledgement:** OFRS acks every received command or reply by echoing its
  sequence, function, and data with type `2`. It resends its own unacknowledged
  commands up to three times.
- **Handshake:** OFRS sends `VERSION_QUERY` and `QUERY_MODEL`. The board's
  `VERSION_DETAIL_REPLY` must carry ASCII `OFRS` in data bytes 4–7. OFRS then enables
  the sequencer.
- **Keepalive:** the board sends a heartbeat about once per second, unprompted and
  even before any handshake (bench). OFRS disconnects after 5 s without one.
- **Board acknowledgements:** the board acks each host command with type `2`, echoing
  its sequence and function but carrying four zero data bytes (bench). It then confirms
  the new setting with a reply whose function ID is the command's minus `0x20`: `0x41`
  → `0x21` through `0x45` → `0x25` (bench). It does not ack host heartbeats. It numbers
  all of its own frames from one shared sequence counter (bench).

## Functions

"Code" means the meaning comes from OFRS source. "Bench" means a capture has confirmed
it. Rows marked "bench" were confirmed by the 2026-10-20 bench tests.

| ID | Name | Direction | Data | Confidence |
| --- | --- | --- | --- | --- |
| 0x41 | `LED_VALUE` | host → board | `[led, 0, value_hi, value_lo]`, value = percent × 100 | bench |
| 0x42 | `LED_ENABLE` | host → board | `[led, on, 0, 0]` | bench |
| 0x43 | `CAMERA_TIMING` | host → board | `[fps_hi, fps_lo, exp_hi, exp_lo]`: total trigger rate across all LEDs, and the LED pulse length per trigger in interleaved modes, in 10 µs units, read as sent | bench |
| 0x44 | `EXCITATION_MODE` | host → board | `[mode, 0, 0, 0]`: 0 continuous, 1 410+470, 2 470+560, 3 all | bench (0, 3) |
| 0x45 | `DEVICE_ENABLE` | host → board | `[on, 0, 0, 0]`: sequencer enable; light starts and stops with it | bench |
| 0x46 | `OUTPUT_ENABLE` | host → board | `[port 1–4, on]`; confirmed by `0x29` only when the state changes | bench |
| 0x48 | `OUTPUT_CONFIG` | host → board | `[port, freq_hi, freq_lo, width_hi, width_lo]`, frequency × 100 Hz, width in ms; ack only, no confirmation reply | bench |
| 0x49 | `VERSION_QUERY` | host → board | `[0, 0, 0, 0]` | bench |
| 0x4C | `TIME_CHECK` | host → board | `[0, 0]`; OFRS also queries LED limits with this ID as `[led, 0]` | code |
| 0x4E | `QUERY_MODEL` | host → board | `[0, 0, 0, 0]` | bench |
| 0x71 | `HOST_HEARTBEAT` | host → board | type info, `[0, 0, 0, 0]`; no ack or reply | accepted (bench) |
| 0x21 | `LED_VALUE_REPLY` | board → host | echo of the 4-byte `LED_VALUE` data | bench |
| 0x22 | `LED_ENABLE_REPLY` | board → host | `[led, on]`, 2 bytes | bench |
| 0x23 | `CAMERA_TIMING_REPLY` | board → host | frame rate in sent order, then the exposure bytes swapped (a reporting quirk only) | bench |
| 0x24 | `EXCITATION_MODE_REPLY` | board → host | echo of the 4-byte mode data | bench |
| 0x25 | `STATE_REPLY` | board → host | `[on, 0, 0, 0]`, the sequencer state | bench |
| 0x26 | `VERSION_REPLY` | board → host | firmware `data[0].data[1].data[2]`, 4 bytes | bench |
| 0x27 | `VERSION_DETAIL_REPLY` | board → host | 32-bit build, then `OFRS` | bench |
| 0x28 | `INPUT_EDGE` | board → host | `[line index 0–3, tick_ms (u32, high byte first), edge 0 rising / 1 falling]`; sent only while the sequencer runs | bench |
| 0x2B | `MODEL_REPLY` | board → host | one byte: 0 = R821 (bench); OFRS maps 1 = R820, 2 = R811, 3 = R810 (code) | bench (0) |
| 0x29 | `BNC_PORT_REPLY` | board → host | `[port, on]`: output state changed | bench |
| 0x2A, 0x2D | LED limits, time-check replies | board → host | layouts unknown | name only |
| 0x31 | `CONTROLLER_HEARTBEAT` | board → host | type info, `[0, 0, 0, 0]`, about 1 Hz | bench |

LED index: `0` = 410 nm, `1` = 470 nm, `2` = 560 nm. OFRS limits exposure to
`(1000 // fps) − 2.63` ms and to at least 0.5 ms.

**Never sent by DBF:** `0x4B` LED calibration limits, `0x4D` model, `0x4F` restore
defaults, and `0x52` firmware upgrade. The encoder refuses these commands unless a
caller passes `allow_unsafe=True`, which no DBF code does.

## Open questions for the bench capture

- Reply layouts for `0x2A` and `0x2D`, and the time-check exchange.

## Bench evidence

**2026-10-20 probe.**

- **Unit:** an RWD unit whose controller enumerates as FTDI VID `0403`, PID `6001`, an
  FT232R full-speed bridge with 64-byte USB packets. Two Allied Vision USB3 Vision
  cameras (VID `1AB2`) were attached but had no driver installed.
- **Probe:** the host opened the port at 115200 8N1 with DTR and RTS low and listened
  for 3 s. It then sent only `VERSION_QUERY` and `QUERY_MODEL`, acknowledged each board
  reply, and closed after 9 s. It never sent the sequencer enable or any LED, timing,
  mode, or output command.
- **Results:**
  - Every board frame passed the CRC check, and none were malformed.
  - The board acknowledged both queries.
  - It reported firmware 2.0.0, build 37619 with the `OFRS` magic, and model code 0. The
    unit's label reads R821.
  - It sent heartbeats about once per second throughout.
  - A repeat probe with a 20 s window produced no visible light from the fiber
    (operator observation), so the handshake alone does not start excitation.
  - The board's sequence counter advanced by about 180 between the two runs, three
    minutes apart. The board sends heartbeats continuously, whether or not a host is
    connected.
- **Regression test:** the exact board bytes are a golden fixture in
  `tests/test_rwd_controller_protocol.py`.

**2026-10-20 LED tests**, on the same unit with the fiber aimed at a card:

- **470 nm at 5 %, continuous mode, 20 Hz / 40 ms trigger:**
  - Sequencer enable started the light, and sequencer disable stopped it (operator
    observation).
  - Every command was acknowledged on the first attempt.
  - Several board CRCs contained `7E` and arrived correctly escaped as `7D 5E`.
- **Sequence test:** 410, 470, and 560 nm were each run alone at 5 % for 3 s, then all
  three interleaved (mode 3) at 10 Hz / 50 ms.
  - The operator reported that it worked as expected.
  - The board confirmed every LED, mode, timing, and sequencer change.
  - There were 94 acknowledgements, no retries, and no malformed frames.
- **Regression tests:** the board's exact confirmation frames are golden fixtures in
  `tests/test_rwd_controller_protocol.py`.
- **Still unmeasured:** the LED state after host loss. The tests stopped the light by
  command only.

**2026-10-20 camera test.** Both Alvium cameras were set to hardware trigger (Line0,
falling edge), Mono12, 10 ms exposure, gain 0, with chunk data enabled. The fiber was on
the kit's green fluorescent film. DBF then ran each LED alone at 5 % with a 20 Hz
setting, followed by all three interleaved with a 10 Hz setting.

| Step | Camera 1 (`DeviceUserID` 1) | Camera 2 (`DeviceUserID` 2) |
| --- | --- | --- |
| 410 nm alone | 62 frames, 50.0 ms apart, mean 8.2 counts | none |
| 470 nm alone | 63 frames, 50.0 ms apart, mean 2.9 counts | none |
| 560 nm alone | none | 63 frames, 50.0 ms apart, mean 13.6 counts |
| Interleaved, 10 Hz | 21 frames, intervals alternating 100 and 200 ms | 10 frames, 300 ms apart |

- **Trigger routing:** neither camera received triggers while the sequencer was
  disabled, and no frames were incomplete.
- **Interleave order:** in the interleaved run, frames arrived as 410 → 470 → 560
  (identified by the per-LED brightness levels), one exposure every 100 ms in total.
- **Line state:** `ChunkLineStatusAll` was `14` on every frame.
- **Not yet confirmed:** that the exposure field is read correctly. The camera's own
  10 ms exposure was set separately, so this test did not exercise the board's field.

**2026-10-20 phase and exposure tests**, with the same target, LEDs at 5 %, a 20 Hz
setting, and camera exposure 10 ms (phase) or 45 ms (exposure).

- **Phase:** 50 sequencer restarts in total, 10 or 20 per interleaved mode, each
  running about 1 s.
  - Every restart began on the cycle's first LED, with no frame-ID gaps and exact 50 or
    100 ms camera intervals.
  - The 410 and 470 brightness ranges never overlapped (about 7.1 versus 3.75 counts).
  - In two restarts the final frame arrived 13 ms after the disable command with
    reduced brightness: the stop truncated the exposure in flight.
- **Exposure field:** the camera exposed for 45 ms, longer than any LED pulse, while only
  the board's field changed. In 410+470 interleaved mode, brightness rose linearly
  with the field: 410 frames went 13.4, 22.1, 33.8 and 470 frames 11.8, 14.8, 19.0
  counts for 5, 20, and 40 ms. Both lines extrapolate to the same dark level, about
  10.6 counts.
  - So the field is the LED pulse length per trigger, read in the byte order sent; the
    swapped echo is cosmetic.
  - In continuous mode the 470 LED stayed on throughout, at 19.9–20.0 counts for every
    field value. That matches the interleaved 470 line extended to 45 ms.

**2026-10-20 host-loss tests**, with 470 nm at 5 % continuous and the operator watching the
fiber:

| Host failure | Light |
| --- | --- |
| Port open, no host traffic for 15 s | Stayed on |
| Port closed without any off command, 10 s | Stayed on |
| USB cable unplugged for 18 s | Stayed on |

The board has no host watchdog and no fail-safe on link loss; only an off command or
the unit's power switch stops excitation. After each test, reconnecting and sending the
off sequence turned the light off. The board kept sending heartbeats whether or not a
host was talking to it.

**2026-10-20 TTL input tests.** An external source drove Input 1 with a burst of pulses
every 31 s.

- **Idle board:** with the sequencer disabled, 75 s produced no input reports.
- **Sequencer on, all LEDs disabled at 0 %:** two 70 s runs reported 28 and 52 edges.
  - Every burst began rising and ended falling, and rises and falls strictly
    alternated.
  - Pulse widths measured 10–210 ms at 1 ms resolution.
- **Tick clock:** data bytes 1–4 are a 32-bit millisecond counter.
  - It ran at 1000.0–1000.1 ticks per host second, with 3–5 ms host-side scatter.
  - Bursts were exactly 30 991 ticks apart.
  - The counter continued across sequencer restarts and sessions, consistent with
    counting from power-on.
- **Implications for DBF:** TTL edges carry a board-clock timestamp. They are only
  reported while the sequencer runs, so DBF must keep it enabled for the whole
  recording.

**2026-10-20 TTL output loopback.** A BNC cable joined Output 1 to Input 2. The sequencer
ran with all LEDs disabled at 0 %.

| Output 1 configuration | Input 2 result |
| --- | --- |
| 10 Hz, 20 ms, enabled about 5 s | 51 pulses, period 100.0 ms, every width 20 ms |
| 5 Hz, 50 ms, enabled about 4 s | 21 pulses, period 200.0 ms, every width 50 ms |

All 144 Input 2 edges fell inside the enabled windows, and the output stopped on
disable. This also shows that input timestamps resolve the board's own output timing
to the millisecond.

**2026-10-20 DBF RWD path, round 1.** These runs used DBF's own `RWDRig` through
`run_preview` and `AcquisitionEngine`. The R821 was on COM3 with both Alvium cameras,
the fiber was on its target, and the operator watched. Settings: 410/470/560 nm at
5 %, 20 Hz, 10 ms exposure, one fiber at the demo position. The run was made twice;
the second run included the two fixes below.

- **Previews:** 3 of 3 passed each time, 10 frames each. 410 and 470 nm frames came
  only from camera 1 and 560 nm frames only from camera 2. Mean frame brightness per
  wavelength was steady across previews (about 6.0, 2.5, and 13.5 counts).
- **Recordings:** each 10 s recording held 200 frames. Every slot step was exactly
  50.000 ms on the trigger grid, with no camera breaks, restarts, drops, or invalid
  spans. The NWB file passed validation and round-trip checks.
- **LEDs:** the operator saw the LEDs flash for each preview, stay on through the
  recording, and go off at the end every time.
- **TTL input:** a burst on Input 1 during the second recording gave alternating rising
  and falling edges. The TTL clock froze its offset after 8 edges, and edge intervals
  equal the raw board-tick intervals. The absolute edge-to-exposure offset still has
  no reference measurement.
- **Start delay:** the first run reported −4.6 ms from the enable command to the
  first frame. That was impossible, because the host stamped the enable after the
  board's confirmation returned through the FTDI latency timer. The rig now stamps it
  before sending. The second run measured +10.6 ms, and the host-clock residual fell
  from 13.3 ms to 2.1 ms.
- **Startup time:** opening the controller and cameras took about 7 s of a preview's
  wall time. Vimba X's three simulated cameras added 1.7 s, so `AlviumTriggeredCameras`
  now skips cameras on transport layers other than USB3 Vision.

