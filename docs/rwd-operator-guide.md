# RWD bridge operator and bench-validation guide

## What this workflow does

Driftless Bundle Photometry (DBF) connects as a read-only TCP client to the
fluorescence/event stream opened by the RWD software. RWD continues to configure and
control its hardware. DBF records the streamed values and named events, shows live
raw/smoothed/dF/F traces, and produces one independently validated NWB file for each
mapped fiber/animal.

This implementation has synthetic parser, TCP, recovery, GUI, and NWB validation.
It has not yet been exercised against a physical RWD system. The exact-wire capture
procedure below exists so that validation can be completed without relying on screen
observations or undocumented assumptions.

## Configure one RWD session

1. In RWD, configure the acquisition and open **System Setting > Data Transfer**.
   Record the fluorescence-data host/IP and port. Do not use a behavior-video port.
2. In DBF, choose **RWD read-only stream** at the top of the session setup.
3. Enter the same host and port. Leave the preamble on `auto` for the first bench
   capture unless the real stream has already established a different mode.
4. Enter the timestamp and value scales as explicit assumptions. The defaults are
   `0.001 s/tick` and `0.001 value units/raw count`; neither is physically confirmed
   merely because a connection succeeds.
5. Select the exact RWD wavelengths expected on the stream: 410, 470, and/or 560 nm.
6. For every enabled fiber, enter the RWD device-channel number, a readable channel
   label, and the animal metadata. Device channels must be unique. The stable DBF
   `fiber_id`, not the visible row order, determines the animal file.
7. Set a conservative maximum expected record rate and the output directory. DBF
   uses that rate only for disk-capacity preflight; it does not throttle RWD.
8. Save the settings JSON. Start recording in RWD first, then start DBF. One TCP
   connection is one DBF session.

DBF hides camera, LED-voltage, raw-image, calibration-image, and native TTL controls
in RWD mode because the RWD fluorescence stream does not substantiate those values.
It sends no hardware command and never reconnects automatically after a disconnect.

## Capture exact bench evidence

Use the saved settings with the headless bridge when validating a real RWD system:

```bash
dbf --rwd-settings path/to/rwd.settings.json \
    --duration 300 \
    --rwd-wire-capture path/to/bench-001.rwd-wire
```

This performs the normal bounded-spool and per-fiber NWB workflow while retaining
the exact non-empty byte chunks returned by the TCP socket. The single-file capture
contains:

- the parsing/remapping contract and application/runtime provenance;
- each exact TCP chunk, its monotonic host receipt time, and its SHA-256 checksum;
- a whole-stream SHA-256 digest, byte/chunk counts, duration, resolved preamble,
  machine identity, and terminal outcome.

The writer uses a `.partial` suffix until it writes and fsyncs the integrity footer,
then atomically promotes the requested path. An interrupted `.partial` is evidence of
an incomplete capture and is deliberately rejected for replay. Wire captures may
contain experimental data and must be handled with the same access controls as the
resulting NWB files; do not commit them to this repository.

Inspect a completed capture without writing an NWB:

```bash
dbf --inspect-rwd-capture path/to/bench-001.rwd-wire
```

The command validates every chunk and the whole-stream digest before returning JSON.

## Replay through the production path

Make a copy of the settings with a new session ID and an empty output directory. Do
not change the preamble policy, scales, wavelengths, channel mappings, labels, or
expected machine name: those fields form the replay contract and a mismatch is
rejected.

```bash
dbf --rwd-settings path/to/replay.settings.json \
    --rwd-replay path/to/bench-001.rwd-wire
```

Replay is deterministic and unpaced. It preserves captured host receipt times and
feeds the exact chunks through the same incremental decoder, bounded recovery spool,
tick normalization, per-fiber NWB finalizer, validation, reopen verification, and
atomic promotion used by the live client. The recorded transport terminal outcome
is inspection evidence; replay does not simulate a socket disconnect after the last
captured byte.

## Physical validation checklist

Keep the settings JSON, `.rwd-wire` capture, command JSON output, produced NWBs, RWD
software version, hardware model/serials, Windows version, test notes, and an
independent timing/value reference together. Record measured results for each item:

1. Confirm whether a standalone four-byte connection preamble exists and compare the
   captured machine identity with the RWD installation.
2. Enable one known device channel at a time, then multiple channels, to establish
   whether channel numbering is zero- or one-based and whether it remains stable.
3. Exercise every supported wavelength combination and confirm the observed mask and
   fixed 31-byte fluorescence layout.
4. Compare raw counts and configured scaling with RWD's displayed/exported values.
5. Measure device-tick intervals against an independent clock to determine the true
   seconds-per-tick scale and characterize drift or resets.
6. Generate named event ON and OFF edges, including maximum-length names, and confirm
   status values, ASCII padding, ordering, and timestamps.
7. Test normal duration stop, operator stop, RWD-side disconnect, cable/network loss,
   idle timeout, and DBF close. Confirm faults and invalid spans are visible and all
   committed records remain recoverable.
8. Run short high-rate and extended soak recordings. Compare RWD counts with capture,
   spool, and every NWB; record queue peak, write latency, disk use, and any gap.

Do not change a configured scale merely to make a graph look plausible. Preserve the
original capture, update the settings assumption only from measured evidence, and
replay into a new session/output so the resulting NWB provenance remains explicit.

## Recovery and troubleshooting

If DBF stops after records were committed, inspect and recover the RWD spool:

```bash
dbf --inspect-spools path/to/output-directory
dbf --recover-spool path/to/session.rwd-spool --keep-spool
```

Recovery is idempotent and will not overwrite a mismatched canonical NWB. Keep the
spool during bench work until counts and provenance have been independently checked.

- **Connect failure:** verify that RWD is already listening, use the fluorescence
  port, confirm firewall/LAN routing, and check the saved host/port.
- **Immediate malformed-stream fault:** inspect the capture, preamble setting,
  expected machine name, enabled wavelengths, and mapped device channels. Do not add
  byte scanning or silently skip the offending payload.
- **Idle timeout:** determine whether RWD sends no bytes when acquisition is paused;
  increase the explicit timeout only if that behavior is verified and acceptable.
- **Unknown channel or wavelength:** stop and correct the settings from independent
  RWD evidence. DBF will not create an animal or relabel a wavelength automatically.
- **Queue/storage fault:** retain the spool/capture, move future output to a suitable
  local disk, raise the conservative rate estimate, and repeat the capacity test.

Behavior video, RWD hardware control, camera calibration imagery, illumination
commands, and native numbered TTL inputs are outside this bridge. Live smoothing and
dF/F are presentation-only; raw values remain canonical, and derived datasets never
replace them.
