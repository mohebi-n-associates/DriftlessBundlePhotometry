# RWD direct-control design record and controller protocol

**Status:** protocol codec and offline USB-capture decoder implemented and tested
against OFRS's own encoder. Nothing here has been exercised against a physical RWD
controller. No DBF code sends commands to RWD hardware yet.

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

1. **Camera identity.** The 560 nm channel is imaged by the second photometry camera.
   Whether that camera is triggered only on 560 nm exposures must be confirmed on the
   bench.
2. **Phase anchor.** Sequence position is counted per camera from the first exposure
   after the sequencer is re-enabled, mirroring OFRS's resync.
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
  is mirrored with `ReverseX`.
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
- **Keepalive:** OFRS disconnects after 5 s without a controller heartbeat.

## Functions

"Code" means the meaning comes from OFRS source. "Bench" means a capture has confirmed
it. No row is bench-confirmed yet.

| ID | Name | Direction | Data | Confidence |
| --- | --- | --- | --- | --- |
| 0x41 | `LED_VALUE` | host → board | `[led, 0, value_hi, value_lo]`, value = percent × 100 | code |
| 0x42 | `LED_ENABLE` | host → board | `[led, on, 0, 0]` | code |
| 0x43 | `CAMERA_TIMING` | host → board | `[fps_hi, fps_lo, exp_hi, exp_lo]`, exposure in 10 µs units | code |
| 0x44 | `EXCITATION_MODE` | host → board | `[mode, 0, 0, 0]`: 0 continuous, 1 410+470, 2 470+560, 3 all | code |
| 0x45 | `DEVICE_ENABLE` | host → board | `[on, 0, 0, 0]`: sequencer enable | code |
| 0x46 | `OUTPUT_ENABLE` | host → board | `[port 1–4, on]` | code |
| 0x48 | `OUTPUT_CONFIG` | host → board | `[port, freq_hi, freq_lo, width_hi, width_lo]`, frequency × 100 Hz, width in ms | code |
| 0x49 | `VERSION_QUERY` | host → board | `[0, 0, 0, 0]` | code |
| 0x4C | `TIME_CHECK` | host → board | `[0, 0]`; OFRS also queries LED limits with this ID as `[led, 0]` | code |
| 0x4E | `QUERY_MODEL` | host → board | `[0, 0, 0, 0]` | code |
| 0x71 | `HOST_HEARTBEAT` | host → board | type info, `[0, 0, 0, 0]` | code |
| 0x21 / 0x22 | LED value / enable replies | board → host | `data[0]` = LED | code |
| 0x25 | `STATE_REPLY` | board → host | sequencer state acknowledged | code |
| 0x26 | `VERSION_REPLY` | board → host | firmware `data[0].data[1].data[2]` | code |
| 0x27 | `VERSION_DETAIL_REPLY` | board → host | 32-bit build, then `OFRS` | code |
| 0x28 | `INPUT_EDGE` | board → host | `data[0]` line index 0–3, `data[5]` 0 rising / 1 falling; bytes 1–4 unknown | partial |
| 0x29, 0x2A, 0x2B, 0x2D | BNC, LED limits, model, time-check replies | board → host | layouts unknown | name only |
| 0x31 | `CONTROLLER_HEARTBEAT` | board → host | type info | code |

LED index: `0` = 410 nm, `1` = 470 nm, `2` = 560 nm. OFRS limits exposure to
`(1000 // fps) − 2.63` ms and to at least 0.5 ms.

**Never sent by DBF:** `0x4B` LED calibration limits, `0x4D` model, `0x4F` restore
defaults, and `0x52` firmware upgrade. The encoder refuses these commands unless a
caller passes `allow_unsafe=True`, which no DBF code does.

## Open questions for the bench capture

- Exact `CAMERA_TIMING` semantics: whether the frame rate is per exposure or per LED
  cycle, and how the board splits triggers between the two cameras.
- Whether camera 2 receives triggers only for 560 nm exposures.
- The meaning of `INPUT_EDGE` bytes 1–4 (a controller tick would allow one TTL clock).
- What the board does with its LEDs when heartbeats stop or the USB link drops.
- Reply layouts for `0x29`, `0x2A`, `0x2B`, and `0x2D`, and the time-check exchange.
- Whether the board resets its sequence phase on every `DEVICE_ENABLE` off→on.
