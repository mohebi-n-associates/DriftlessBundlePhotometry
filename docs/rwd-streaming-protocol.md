# RWD read-only streaming contract

## Scope and evidence

This contract covers the fluorescence/event TCP stream exported by the RWD
Multichannel Fiber Photometry System software. Driftless Bundle Photometry (DBF) is
the TCP client and recorder. RWD remains responsible for hardware control,
acquisition timing, excitation, and its own connection page. DBF sends no hardware
commands and must never present an RWD session as a DBF-controlled camera/controller
session.

The evidence reviewed for this contract is:

1. `Matlab Operating Instruction.docx`, especially section 3, "MATLAB Data Message
   Format."
2. The vendor `Wave.m` demo, which shows byte offsets, Windows/MATLAB `typecast`
   behavior, and fluorescence scaling.
3. `Multichannel Fiber Photometry System User Manual[78].pdf`, pages 64-65, which
   confirms that RWD opens a fluorescence-data port and that MATLAB connects as a TCP
   client on the same computer or LAN.
4. The vendor `Video.m` demo, used only to bound future behavior-video work.

These sources are historical/vendor evidence, not a versioned protocol
specification. No packet capture or physical RWD system was available during this
implementation. Every inference and unknown below therefore remains visible in
configuration and provenance.

## Transport

- TCP byte stream; RWD software listens and DBF connects.
- Host and port are operator-configured from RWD **System Setting > Data Transfer**.
- TCP may fragment one record across reads or combine multiple records in one read.
  A socket `recv` boundary is never a record boundary.
- The fluorescence and event records share one port and are distinguished by byte 5
  (zero-based offset 4).
- There is no documented length prefix, delimiter, checksum, magic value, sequence
  number, or protocol version.
- Because there is no reliable synchronization marker, malformed bytes are a
  fail-closed stream fault. DBF must not silently scan ahead and invent a boundary.
- One TCP connection is one DBF recording. Connect failure, configured idle timeout,
  clean peer disconnect, truncated EOF, malformed payload, and operator stop are
  distinct outcomes. DBF never reconnects automatically because the protocol has no
  session/sequence handshake that could prove continuity across connections.

## Optional connection preamble

`Wave.m` performs one unbounded `read(server)` into a machine-name variable before it
starts fixed-record parsing, but the tabulated message format also places a four-byte
machine name at the beginning of every record. The sources do not state clearly
whether RWD sends a standalone four-byte banner on connection.

`RWDSourceConfig.preamble_mode` therefore persists one of:

- `none`: the stream begins with a normal 30- or 31-byte record;
- `machine_name_4`: consume exactly four banner bytes first;
- `auto` (default): inspect the fifth buffered byte. A value of `0x01` or `0x02`
  means a normal record begins at byte zero; otherwise consume the first four bytes
  as the candidate banner and validate the next record normally.

The detected mode and raw banner must be retained as run provenance. An expected
four-byte machine name may be configured; a mismatch is a surfaced fault.

## Byte order, numeric representation, and units

The vendor demos run on Windows and use MATLAB `typecast(uint8(...), 'uint32')`.
They therefore imply little-endian unsigned 32-bit timestamps and fluorescence
integers. `Wave.m` converts fluorescence with `raw_uint32 / 1000`.

The vendor documents do **not** state the timestamp unit. DBF persists
`timestamp_scale_s` (default `0.001` seconds per tick) in settings and runtime
provenance; that default is an explicit working assumption requiring replay or RWD
bench confirmation. Raw `uint32` ticks are always preserved even after conversion to
NWB seconds. Tick rollover, reset, and discontinuity handling must be tested before a
live system is described as validated.

The trace-only normalizer unwraps each channel/wavelength series independently so it
does not assume the three fixed slots inside one record are timestamp-sorted. New
series are aligned to the closest observed shared tick epoch, the earliest resolved
tick becomes NWB time zero, ordinary backward resets are rejected, and uint32
rollovers are retained explicitly. Equal named-event timestamps are allowed because
distinct event edges may share a device tick; trace timestamps for one mapped
channel/wavelength must remain strictly increasing.

## Fluorescence record (`0x01`, 31 bytes)

All offsets below are zero-based, half-open byte ranges. Every wavelength slot
occupies eight bytes even when its presence bit is clear; `Wave.m` advances over an
absent slot rather than shortening the record.

| Bytes | Width | Field | Interpretation |
| --- | ---: | --- | --- |
| `0:4` | 4 | machine name | Raw four-byte device/software identifier |
| `4` | 1 | record type | Must equal `0x01` |
| `5` | 1 | wavelength mask | bit 0 = 410, bit 1 = 470, bit 2 = 560 |
| `6` | 1 | device channel | Unsigned channel ID, `0..255` |
| `7:11` | 4 | 410 timestamp | Little-endian `uint32` tick |
| `11:15` | 4 | 410 value | Little-endian `uint32`, scaled by `value_scale` |
| `15:19` | 4 | 470 timestamp | Little-endian `uint32` tick |
| `19:23` | 4 | 470 value | Little-endian `uint32`, scaled by `value_scale` |
| `23:27` | 4 | 560 timestamp | Little-endian `uint32` tick |
| `27:31` | 4 | 560 value | Little-endian `uint32`, scaled by `value_scale` |

Parser invariants:

- at least one of mask bits 0-2 is set;
- reserved mask bits 3-7 are zero until vendor evidence defines them;
- only samples whose presence bits are set are emitted;
- each emitted sample retains exact device wavelength `410`, `470`, or `560`, raw
  tick, raw value, and scaled value;
- DBF does not relabel 410 as 405 or 560 as 565;
- one RWD record may carry multiple wavelength samples and is not represented as a
  camera exposure or as proof that the wavelengths were illuminated simultaneously;
- the device channel must have an explicit persisted mapping to one stable
  `fiber_id`; unknown channels are faults, not automatically created animals.

## Event record (`0x02`, 30 bytes)

| Bytes | Width | Field | Interpretation |
| --- | ---: | --- | --- |
| `0:4` | 4 | machine name | Raw four-byte device/software identifier |
| `4` | 1 | record type | Must equal `0x02` |
| `5:9` | 4 | event timestamp | Little-endian `uint32` tick |
| `9:29` | 20 | event name | Fixed-width ASCII field |
| `29` | 1 | event status | `0x00` = ON, `0x01` = OFF |

The exact 20-byte name is retained. The presentation label removes only trailing NUL
and ASCII-space padding and then decodes strict ASCII. Both ON and OFF records are
stored. Empty names, invalid ASCII, other status values, and machine-name changes are
surfaced as malformed-stream faults.

RWD events are named edges, not the native controller's four numbered TTL inputs.
They require their own canonical event representation and must not be squeezed into
a fabricated TTL line number.

## Configuration boundary

Session settings v2 add a discriminated `source`:

- `{"kind": "native"}` preserves the DBF camera/controller workflow;
- `{"kind": "rwd", ...}` selects the read-only RWD bridge.

RWD configuration persists host, port, timeouts, preamble policy, timestamp/value
scales, expected machine name, exact enabled RWD wavelengths, and a unique mapping
from each device channel to every enabled `fiber_id`. Native camera, LED voltage,
camera ROI, and numbered TTL controls are inactive in RWD mode. Existing settings v1
files migrate explicitly to `native`; unknown versions remain rejected.

`maximum_expected_record_rate_hz` is a conservative operator-configurable bound used
only to estimate worst-case spool plus duplicated per-fiber NWB capacity before a
connection is armed. It never throttles or changes the RWD software's sampling rate.

The current transitional session model still carries native camera/channel/display
fields in its complete snapshot so old configurations remain reversible. RWD workers
must not read those inactive fields. The GUI refactor will hide them rather than
imply that DBF controls RWD hardware.

## Trace-only recovery and NWB boundary

RWD records use a dedicated `*.rwd-spool` rather than the native camera-frame spool.
Its bounded single-owner writer commits mixed fluorescence/event records in wire
order and checksums every record chunk plus configuration, runtime provenance,
resolved preamble/machine identity, system events, and invalid spans. Complete and
explicitly aborted spools are recoverable. A spool is removed only after all mapped
fiber outputs validate, reopen with exact counts/values, and complete atomic
promotion; valid canonical or partial files are reused when recovery resumes.

Each output is one self-contained NWB per mapped `fiber_id`/animal. It contains:

- one `FiberPhotometryResponseSeries` per observed exact RWD wavelength with the
  configured scaled vendor values, plus a core `TimeSeries` retaining each exact
  `uint32` wire value;
- event tables for every stream record, every fluorescence sample represented by
  that file, and every shared named ON/OFF event, including raw and unwrapped ticks,
  wire sequence, device channel, machine bytes, and host receipt time;
- the RWD timing/value scales, connection metadata, complete settings, runtime
  provenance, lifecycle/fault events, and any invalid intervals.

The stream does not substantiate camera pixels, camera ROIs, illumination commands,
optical power, exposure duration, emission wavelength, or native numbered TTL lines.
Those fields are recorded as unknown where an extension requires metadata and are
otherwise absent; DBF does not synthesize native camera/controller records for RWD.

## Behavior video is deferred

The RWD software exposes up to three additional behavior-video ports. `Video.m`
suggests a 12-byte little-endian header (timestamp, height, width) followed by
`height * width * 3` RGB bytes. The prose table contains an ambiguous height range,
and the demo fixes dimensions after the first frame. Video is out of scope for the
initial bridge because safe implementation still needs:

- maximum dimension/payload limits before allocation;
- confirmation that dimensions may or may not change;
- orientation/color-order and timestamp-unit fixtures;
- disconnect, truncation, and oversized-frame tests;
- an explicit decision about whether behavior video belongs in each animal NWB.

## Validation gates before a live claim

1. **Implemented with synthetic fixtures:** golden bytes for every wavelength-mask
   combination, mixed event/data records, fragmentation, coalescing, rollover
   endpoints, malformed types/masks/status/names, truncated EOF, preamble modes, and
   unknown channels. These fixtures test the documented interpretation but are not a
   substitute for a real capture.
2. **Implemented with synthetic records:** the same incremental parser feeds the
   bounded trace-only spool, recovery, rollover handling, exact per-fiber NWB
   finalization, PyNWB validation, NWB Inspector, and reopen/count/value checks.
3. **Partially implemented with a local TCP server:** fragmentation/coalescing,
   connection failure, idle timeout, exact-boundary disconnect, truncated EOF,
   malformed input, bounded queue pressure, prompt stop, fault journaling, and full
   headless finalization are covered. GUI close behavior remains in the GUI gate.
4. A real RWD packet capture confirming byte order, the connection preamble,
   timestamp unit, value scale, channel numbering, wavelength combinations, event
   padding, and tick behavior.

Until gate 4 passes, the feature is an evidence-based RWD bridge implementation, not
a physically validated RWD integration.
