# Thorlabs Camera Python API Integration Notes

## Purpose and source

This document records what Driftless Bundle Photometry (DBF) learned from
`Thorlabs_Camera_Python_API_Reference.pdf`, supplied as a local reference by the
project owner. The source identifies itself as:

- Title: `thorlabs_tsi_sdk`
- Author: Thorlabs Scientific Imaging
- Document revision: D, ITN003996-D01
- Documented package version: 0.0.8
- Document date shown in the PDF: April 26, 2022

The PDF documents the general Thorlabs Scientific Imaging Python SDK. It is not a
CS505MU-specific electrical specification, current compatibility guarantee, or proof
that this project's camera path has been physically validated. Model-specific facts
must be read from the connected camera and confirmed against the current camera
manual, SDK release, and bench measurements.

Color conversion and polarization processing are documented by the source but are
outside DBF's current monochrome photometry acquisition path. DBF should preserve the
camera's raw monochrome `uint16` samples rather than route them through display/color
processing.

## Communication model

The Python package is a wrapper around native Thorlabs Camera SDK DLLs. Communication
is organized around two resource-owning objects:

1. `TLCameraSDK` initializes and holds the native SDK open. The reference says only
   one SDK instance may be active at a time.
2. `TLCamera`, created only by `TLCameraSDK.open_camera(serial_number)`, represents
   one opened camera and exposes configuration, arm/trigger/disarm, and frame polling.

Both objects support `with` statements and explicit `dispose()`. Context managers are
the preferred DBF integration because they make the reverse-order cleanup boundary
visible and deterministic.

The SDK discovers cameras by serial number:

```python
from thorlabs_tsi_sdk.tl_camera import TLCameraSDK

with TLCameraSDK() as sdk:
    serial_numbers = sdk.discover_available_cameras()
    with sdk.open_camera(serial_numbers[0]) as camera:
        ...
```

DBF must select and persist the intended serial number rather than silently choosing
the first camera when more than one is connected.

## Vendor-documented acquisition lifecycle

The documented lifecycle is:

1. Construct exactly one `TLCameraSDK`.
2. Discover connected camera serial numbers.
3. Open the selected camera and obtain a `TLCamera`.
4. Configure properties such as exposure, ROI, operation mode, trigger polarity, and
   frames per trigger while the camera is disarmed.
5. Call `camera.arm(frames_to_buffer)` to prepare for software or hardware triggers.
6. For software triggering, call `issue_software_trigger()`. For hardware triggering,
   send electrical trigger signals to the camera input.
7. Poll `camera.get_pending_frame_or_null()` for frames.
8. Stop issuing triggers and call `camera.disarm()`.
9. Dispose the camera.
10. Dispose the SDK.

Important lifecycle details from the reference:

- `roi` and `operation_mode` are examples of settings that cannot be changed while
  armed. Configuration changes therefore require an explicit disarm/reconfigure/arm
  transition.
- Calling `arm()` clears images already in the camera queue as stale.
- A normal `disarm()` does not clear the queued images; polling may continue until
  the queue is empty. Calling `disarm()` again clears the queue. DBF should avoid an
  accidental second disarm before its drain policy has completed.
- `dispose()` releases camera resources, but the reference does not define it as a
  substitute for DBF's stop/drain protocol. Deterministic normal stop should disarm
  explicitly before resource cleanup.

## Trigger modes and the DBF mapping

The SDK exposes three `OPERATION_MODE` values:

| SDK mode | Vendor-documented behavior | Intended DBF use |
| --- | --- | --- |
| `SOFTWARE_TRIGGERED` | Software triggers produce finite frames, or one trigger starts continuous acquisition when frames per trigger is zero. | Calibration/focus preview and simulator-like diagnostics only. |
| `HARDWARE_TRIGGERED` | A configured trigger edge begins an exposure whose duration comes from `exposure_time_us`. | Normal controller-timed photometry acquisition. |
| `BULB` | A hardware input controls triggering and exposure length. | Do not use unless the exact CS505MU signaling and controller timing are separately validated. |

For normal DBF acquisition, the expected starting configuration is:

- verify `get_is_operation_mode_supported(OPERATION_MODE.HARDWARE_TRIGGERED)`;
- set `operation_mode = OPERATION_MODE.HARDWARE_TRIGGERED`;
- set `frames_per_trigger_zero_for_unlimited = 1` so one hardware trigger requests
  one frame;
- set and read back `exposure_time_us` within `exposure_time_range_us`;
- set `trigger_polarity` to `ACTIVE_HIGH` or `ACTIVE_LOW` only after the electrical
  edge has been selected and measured;
- arm the camera before the controller begins sending triggers.

The source says `ACTIVE_HIGH` begins acquisition on the rising edge and `ACTIVE_LOW`
on the falling edge. It does not establish the CS505MU input voltage, minimum pulse
width, propagation delay, or exposure timing accuracy; those remain bench questions.

`frames_to_buffer`, passed to `arm()`, controls the SDK/camera buffering used for
retrieval. It is distinct from `frames_per_trigger_zero_for_unlimited`. DBF must choose
a bounded buffer value as part of its queue/capacity design and surface any inability
to poll fast enough.

## Polling and frame ownership

`get_pending_frame_or_null()` blocks for at most `image_poll_timeout_ms` and returns
either a `Frame` or `None`.

A `Frame` contains:

- `image_buffer`: a NumPy array with `dtype=np.ushort`;
- `frame_count`: the camera-assigned frame number;
- `time_stamp_relative_ns_or_null`: an optional camera-relative timestamp in
  nanoseconds, recorded immediately after exposure according to the reference.

The most important ownership rule is that `Frame.image_buffer` is temporary. The
reference says it may become invalid after the next frame poll, camera rearm, or
camera close. DBF must make a deep copy before returning the frame from the camera
owner thread or placing it on a queue:

```python
frame = camera.get_pending_frame_or_null()
if frame is not None:
    pixels = np.copy(frame.image_buffer)
```

The copied array must remain `uint16` and retain the camera's original values. The
camera's `bit_depth` says how many lower bits of each 16-bit value are relevant; this
is metadata for interpretation and validation, not permission to normalize, rescale,
or truncate frames.

The camera frame count and optional timestamp are independent provenance signals:

- preserve `frame_count` as the raw camera frame ID and detect gaps, duplicates,
  resets, or unexpected ordering;
- preserve the camera timestamp when supported, including its raw/relative nature;
- do not treat the camera timestamp as the controller clock or NWB session time until
  a measured clock-mapping method exists;
- do not infer excitation wavelength from `frame_count`, timestamp, parity, or modulo.

Each frame must be matched to DBF's explicit controller exposure record, which owns
the wavelength, controller sequence, controller tick, and commanded intensity.

A `None` result means that no frame became available within the configured polling
timeout. It is not by itself proof of a dropped trigger. The camera worker must combine
timeouts with controller exposure records, expected timing, disconnect state, and
shutdown state to decide whether to continue, report a drop, or enter `ERROR`.

## Configuration and capability discovery

Camera capabilities vary by model. DBF should query ranges and support flags from the
opened camera rather than hard-code general SDK examples.

### Identity and transport provenance

Persist at least:

- `serial_number`, `model`, and `firmware_version`;
- `communication_interface` and, for USB, `usb_port_type`;
- installed Python SDK/package version, native DLL version when discoverable, and
  Windows/driver version;
- `camera_sensor_type`, `sensor_width_pixels`, `sensor_height_pixels`, sensor pixel
  size, `sensor_pixel_size_bytes`, and `bit_depth`.

The SDK provides connect and disconnect callbacks by camera serial number. They are
useful for observability, but disconnect must also be handled when polling or property
access raises `TLCameraError`. Callback code should remain minimal and forward a typed
notification to the camera owner rather than manipulating acquisition state directly.

### Exposure and rate

- Validate exposure against `exposure_time_range_us`.
- `frame_time_us` and `sensor_readout_time_ns` may describe readiness/readout on
  supported models, but the reference notes model limitations.
- `get_measured_frame_rate_fps()` reports the delivery rate to the host and may be
  limited by the computer or communication interface. It is a diagnostic, not the
  deterministic exposure schedule.
- `data_rate` values and support vary by camera. Query
  `get_is_data_rate_supported()` before selecting one.
- Frame-rate control also varies by model and is constrained by exposure and readout.
  DBF's normal externally triggered rate remains the controller's responsibility.

DBF must verify that the requested total trigger rate is possible for the configured
exposure, readout, ROI, binning, data rate, host, and raw-frame retention. The total
camera rate is shared across enabled wavelengths.

### ROI and binning

The SDK camera ROI is one rectangular hardware crop represented by upper-left and
lower-right coordinates. `roi_range` describes model-specific limits. DBF's 1–9
circular fiber ROIs remain software extraction regions inside that camera-space
rectangle; they are not separate SDK ROIs.

Implementation rules:

- derive one enclosing hardware crop from the circular fiber ROIs plus any required
  margin/alignment;
- validate the proposed rectangle against `roi_range`;
- set the hardware `roi` only while disarmed;
- read the accepted ROI back from the camera and use that value as the authoritative
  crop, because SDK documentation elsewhere warns that some models may adjust ROI
  coordinates;
- map circular ROI coordinates explicitly between full-sensor and cropped-image
  spaces and test orientation/mirroring on the physical camera;
- query `binx_range` and `biny_range`; default to 1×1 unless binning is deliberately
  validated because binning changes spatial coordinates and sums adjacent pixels.

### Gain, black level, and pixel correction

- `gain` is an integer/index-like camera setting whose supported range and units vary
  by model. The SDK provides gain↔decibel conversion helpers, but the model data sheet
  still controls interpretation.
- `black_level` is a camera-applied offset when supported.
- Hot-pixel correction replaces detected pixel values using neighbors when enabled.

Because DBF treats camera output as raw scientific input, the application should
record all three settings. The initial validation configuration should disable
hot-pixel correction and avoid implicit black-level processing unless a documented
scientific requirement says otherwise. This is a DBF design recommendation, not a
vendor requirement. Any correction must remain explicit and must not be mistaken for
unchanged sensor samples.

### Equal Exposure Pulse

Some models expose Equal Exposure Pulse (EEP), an LVTTL-level output associated with
the interval after rolling reset when all rows have equal exposure. The SDK provides
`is_eep_supported`, `is_eep_enabled`, and `eep_status`.

EEP may be useful as a timing diagnostic, but the reference does not prove that the
CS505MU supports it or that it should control DBF excitation. The DBF controller
remains the timing authority unless a future electrical design explicitly validates
EEP behavior, polarity, latency, exposure limits, and its relationship to controller
records.

## DLL loading and optional imports

The native Camera SDK DLL directory must be visible to the Python process. The
reference lists three mechanisms:

- copy DLLs into the script working directory;
- adjust the process DLL search path/current directory at runtime;
- add the DLL directory to the system `PATH`.

DBF should not copy vendor DLLs into the repository or depend on a machine-specific
absolute path. The physical adapter should load the optional SDK lazily, accept a
configured/installed SDK location where necessary, and produce an actionable error
that names the missing SDK/DLL requirement. Simulator, replay, NWB, analysis, and GUI
demo paths must continue working without the vendor package or DLLs.

On modern Windows Python, a process-local DLL directory mechanism is preferable to
changing the global working directory or permanently editing `PATH`, but the exact
supported mechanism must be verified against the installed Thorlabs SDK release.

## Errors and logging

The SDK raises `TLCameraError` for camera/operation failures, including examples such
as unplugging a camera during acquisition or trying to change ROI while armed. DBF
must not catch and discard these exceptions.

The reference identifies the logger `thorlabs_tsi_sdk.tl_camera`. The camera adapter
should bridge relevant SDK diagnostics into DBF's structured system events while
avoiding duplicate or unbounded logs.

Error handling must preserve these DBF guarantees:

- signal the acquisition coordinator and enter `ERROR` when camera ownership or frame
  integrity can no longer be trusted;
- request controller STOP so excitation is disabled;
- stop issuing triggers before disarming during normal shutdown;
- preserve queued/committed records according to the defined drain policy;
- make timeouts, disconnects, SDK exceptions, frame-count discontinuities, and queue
  pressure observable;
- never promote an incomplete or unvalidated partial file to `.nwb`.

The camera driver is not the electrical safety authority. Controller watchdog and
safe hardware defaults must disable LEDs even if the SDK, USB link, owner thread, or
host process fails.

## Proposed DBF owner-thread sequence

This is the integration sequence inferred from the vendor API and DBF architecture:

1. Start one camera owner thread.
2. Create one `TLCameraSDK` inside that thread.
3. Register lightweight connect/disconnect callbacks.
4. Discover serial numbers and open the configured camera.
5. Read identity, firmware, transport, sensor, capability, and range properties.
6. Validate the immutable DBF configuration against actual capabilities.
7. While disarmed, apply data rate, binning, accepted hardware crop, gain, black
   level/correction policy, exposure, hardware-trigger mode, polarity, and one frame
   per trigger.
8. Read back every setting that affects pixels, geometry, or timing.
9. Set a bounded polling timeout and call `arm(frames_to_buffer)`.
10. Tell the acquisition coordinator that the camera is armed; do not start the
    controller from the camera thread.
11. Poll frames, immediately deep-copy the temporary buffer, attach camera frame ID
    and optional timestamp, and submit to a bounded matcher/queue.
12. On normal stop, stop controller triggers first, disarm once, drain the remaining
    camera queue according to policy, and reconcile frames with exposure records.
13. On fatal error, notify the coordinator immediately so it can command safe STOP,
    then perform deterministic camera cleanup without hiding the original error.
14. Dispose camera and SDK in reverse order and terminate the owner thread.

The exact ordering around disarm and final queue drain needs a small hardware test,
because the source states the queue survives one disarm but does not specify every
model's delivery latency around the final trigger.

## Required CS505MU bench questions

The API reference does not answer the following. They must be resolved before this
project claims physical camera support:

- Which Thorlabs Python SDK, native DLL, driver, firmware, and Windows versions support
  the exact CS505MU serial number?
- Does the camera report `HARDWARE_TRIGGERED` support, and what trigger voltage,
  polarity, minimum pulse width, dead time, and maximum rate are valid?
- What is the measured delay/jitter from controller trigger to exposure start/end?
- Does the camera expose EEP or another exposure-active signal, and how does it behave?
- Does `frame_count` start/reset/wrap predictably, and what happens after a missed
  trigger, USB stall, disarm/rearm, disconnect, or reconnect?
- Is the relative timestamp supported? What clock, resolution, wrap, drift, and reset
  behavior does it have on this model?
- What is the exact pixel orientation, row order, mirroring, ROI coordinate convention,
  accepted crop alignment, and read-back behavior?
- What effective bit depth and lower-bit packing are delivered at each supported data
  rate?
- What are the sustained full-frame/cropped rates with 1–3 wavelength triggering,
  1–9 software fiber ROIs, and raw-frame retention on/off?
- What are the defaults and measured effects of gain, black level, hot-pixel correction,
  frame-rate control, and binning?
- How do polling timeout, SDK buffer depth, host load, and USB topology affect frame
  delivery and loss reporting?
- Do disconnect callbacks and `TLCameraError` arrive reliably for cable removal,
  power loss, and camera reset?

Results should be recorded in a versioned validation report with camera serial,
computer/USB topology, software and firmware versions, test duration, instruments,
raw measurements, and pass/fail thresholds chosen before measurement.

## Implementation boundary

This note is sufficient to design the Python adapter and simulator/replay tests. It is
not authorization to claim the existing `hardware.thorlabs` module operates the
camera. That module remains an SDK-availability boundary until the adapter, frame
matcher, fault tests, and named CS505MU bench characterization are implemented and
reviewed.
