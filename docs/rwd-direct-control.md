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
3. **Serial transport — planned.** Single-owner port: handshake, automatic ACKs,
   resend, heartbeat, malformed-frame faults, and a best-effort sequencer disable on close.
4. **Alvium camera adapter — planned.** Lazy vendor SDK import, hardware trigger,
   `uint16` frames with hardware frame IDs and timestamps.
5. **RWD rig source — planned.** Pairs the transport and cameras, assigns wavelength
   identity as described below, and emits `RigPacket`s.
6. **Bench characterization — hardware required.** LED/trigger timing, the
   disconnect and watchdog LED state, TTL latency, and clock drift.

## Approved exception: wavelength identity

The agent contract says never to infer wavelength solely from frame parity/modulo.
The RWD controller does not report per-exposure records, so this hardware cannot meet
that rule without modification. OFRS itself numbers frames from the moment the
sequencer is re-enabled and takes `frame number mod enabled-LED count` as the LED.

The approved method for the RWD direct track, recorded 2026-10-20:

1. **Camera identity.** The board triggers camera 2 only for 560 nm exposures and
   camera 1 only for 410 and 470 nm exposures (bench).
2. **Phase anchor.** Sequence position is counted per camera from the first exposure
   after the sequencer is re-enabled, mirroring OFRS's resync. In one three-LED
   interleaved run the first exposure after enable was 410 nm, followed by 470 and
   560 nm, repeating (bench). Determinism across runs and modes 1 and 2 is not yet
   shown.
3. **Gap detection.** Every frame's hardware frame ID and hardware timestamp are checked
   against the commanded period. Any missing, duplicated, or late exposure ends the
   valid span, is recorded in `invalid_times`, and requires a resync before samples are
   attributed again.
4. **Independent check.** A per-wavelength brightness-pattern check runs as QC only. It
   never assigns wavelength.
5. **Provenance.** Every exposure from this backend records that its wavelength is
   phase-derived, so these data are never mistaken for controller-asserted identity.

`CLAUDE.md` and `AGENTS.md` record this scoped exception. No code performs the
assignment until the rig source (step 5) exists.

RWD hardware uses 410, 470, and 560 nm LEDs, while the Custom system's channel model is
405, 470, and 565 nm. Step 5 must add exact RWD channel identities rather than relabel
RWD exposures as Custom wavelengths.

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
| 0x43 | `CAMERA_TIMING` | host → board | `[fps_hi, fps_lo, exp_hi, exp_lo]`: total trigger rate across all LEDs (bench), exposure in 10 µs units (code) | bench (rate) |
| 0x44 | `EXCITATION_MODE` | host → board | `[mode, 0, 0, 0]`: 0 continuous, 1 410+470, 2 470+560, 3 all | bench (0, 3) |
| 0x45 | `DEVICE_ENABLE` | host → board | `[on, 0, 0, 0]`: sequencer enable; light starts and stops with it | bench |
| 0x46 | `OUTPUT_ENABLE` | host → board | `[port 1–4, on]` | code |
| 0x48 | `OUTPUT_CONFIG` | host → board | `[port, freq_hi, freq_lo, width_hi, width_lo]`, frequency × 100 Hz, width in ms | code |
| 0x49 | `VERSION_QUERY` | host → board | `[0, 0, 0, 0]` | bench |
| 0x4C | `TIME_CHECK` | host → board | `[0, 0]`; OFRS also queries LED limits with this ID as `[led, 0]` | code |
| 0x4E | `QUERY_MODEL` | host → board | `[0, 0, 0, 0]` | bench |
| 0x71 | `HOST_HEARTBEAT` | host → board | type info, `[0, 0, 0, 0]`; no ack or reply | accepted (bench) |
| 0x21 | `LED_VALUE_REPLY` | board → host | echo of the 4-byte `LED_VALUE` data | bench |
| 0x22 | `LED_ENABLE_REPLY` | board → host | `[led, on]`, 2 bytes | bench |
| 0x23 | `CAMERA_TIMING_REPLY` | board → host | frame rate in sent order, then the exposure bytes swapped | bench |
| 0x24 | `EXCITATION_MODE_REPLY` | board → host | echo of the 4-byte mode data | bench |
| 0x25 | `STATE_REPLY` | board → host | `[on, 0, 0, 0]`, the sequencer state | bench |
| 0x26 | `VERSION_REPLY` | board → host | firmware `data[0].data[1].data[2]`, 4 bytes | bench |
| 0x27 | `VERSION_DETAIL_REPLY` | board → host | 32-bit build, then `OFRS` | bench |
| 0x28 | `INPUT_EDGE` | board → host | `data[0]` line index 0–3, `data[5]` 0 rising / 1 falling; bytes 1–4 unknown | partial |
| 0x2B | `MODEL_REPLY` | board → host | one byte, the model code; its mapping to R810–R821 is unconfirmed | bench (layout) |
| 0x29, 0x2A, 0x2D | BNC, LED limits, time-check replies | board → host | layouts unknown | name only |
| 0x31 | `CONTROLLER_HEARTBEAT` | board → host | type info, `[0, 0, 0, 0]`, about 1 Hz | bench |

LED index: `0` = 410 nm, `1` = 470 nm, `2` = 560 nm. OFRS limits exposure to
`(1000 // fps) − 2.63` ms and to at least 0.5 ms.

**Never sent by DBF:** `0x4B` LED calibration limits, `0x4D` model, `0x4F` restore
defaults, and `0x52` firmware upgrade. The encoder refuses these commands unless a
caller passes `allow_unsafe=True`, which no DBF code does.

## Open questions for the bench capture

- What the `CAMERA_TIMING` exposure field controls (probably the LED on-time per
  trigger), and why its confirmation returns the exposure bytes swapped (`0F A0` comes
  back as `A0 0F`). Either the board only reports them swapped, or it reads the
  exposure byte-swapped.
- Whether the interleave always starts at 410 nm after enable, and the order in modes
  1 and 2.
- Whether the board needs host heartbeats at all.
- The meaning of `INPUT_EDGE` bytes 1–4 (a controller tick would allow one TTL clock).
- What the board does with its LEDs when heartbeats stop or the USB link drops.
- Reply layouts for `0x29`, `0x2A`, and `0x2D`, the time-check exchange, and which
  model each `MODEL_REPLY` code means.
- Whether the board resets its sequence phase on every `DEVICE_ENABLE` off→on.

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
  - It reported firmware 2.0.0, build 37619 with the `OFRS` magic, and model code 0.
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
