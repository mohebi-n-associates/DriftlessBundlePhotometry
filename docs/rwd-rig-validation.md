# RWD rig validation (the 0.4 line)

The 0.4 versions work toward one thing: **the RWD rig validated for animal
recordings.** The rig counts as validated only once every check below has passed on the
physical rig and its results are logged at the end of this document, naming the DBF
version that passed. Simulator runs do not count. DBF's own Custom controller and the
Thorlabs camera are not part of this line (see `roadmap.md`).

The software for these checks is in place and simulator-tested. What remains is
running them on the R821 and recording what they show.

## Acceptance thresholds

Proposed 2026-11-15, to be confirmed by the owner before the first validation run.
They are the defaults of `AcceptanceThresholds` in `rig_validation.py`.

| Check | Limit | Bench so far |
| --- | --- | --- |
| Invalid time, excluding requested restarts | ≤ 0.1 % of the recording | 0 % at 20 Hz for 4 h; 0.24 % at 120 Hz for 2.5 h with a loaded host |
| Step between exposures within a run | one trigger period ± 1 µs | exact |
| Camera clock against the trigger grid | within ±100 ppm | −24 ppm on both cameras |
| Camera timestamp residual from its fit | ≤ 500 µs | 83 and 159 µs |
| Lost or incomplete camera frames | 0 | 100 skips at 120 Hz with a loaded host |
| TTL edge times against board ticks | ± 1 µs | exact |
| TTL-to-exposure offset, median | ≤ 2 ms | not measured; about 2 ms in simulation |
| Brightness-QC mismatches, malformed controller frames, dropped-frame events | 0 | 0 |
| LEDs after every stop, fault, and window close | off (observed) | off in every run so far |

Every gap between runs must be explained by an invalid span or an excitation pause,
and every run must restart the LED cycle at its first wavelength on the right camera.

## Tools

All of these run from a terminal in the `dbf` environment.

- `dbf --check-devices` finds the controller and both cameras with every LED off.
- `dbf --bench-record SETTINGS.json` records with saved RWD settings and no window,
  then audits the files. Options:
  - `--duration S` overrides the settings' length.
  - `--output DIR` overrides the settings' output folder.
  - `--restart-every S` restarts the sequencer every S seconds.
  - `--flash-line N` adds the TTL offset measurement.
  - `--report PATH` writes the result to PATH as JSON.

  Ctrl+C stops the recording and saves it. Windows is kept awake while it runs. The
  audit is also saved as `rig-audit_<YYYY-MM-DD>_<HHMMSS>.json` in the output root,
  with the recording and finalization times, the peak memory, and the DBF version.
- `dbf --audit-recording PATH` audits an existing NWB file or every NWB file under a
  folder (an animal's day, or the whole output root), read-only. It exits with code 1
  if any check fails. It also checks that the ROI files of each session hold identical
  exposure records.

Save the settings from the GUI (**Save JSON…**) after setting up the fibers, so
bench runs use the real ROI layout.

## Checks

Each check lists what to set up, the command, and what must hold. Record every run in
the results log at the end.

### V1. Clean long recording at the operating rate

- **Setup:**
  - 120 Hz, all three LEDs at their working power, the real ROI layout.
  - The fibers on a target and shielded from room light.
  - A TTL source on Input 1, such as a 1 Hz pulse train.
  - The host idle: no test suites, no other camera software.
- **Command:** `dbf --bench-record rig.json --duration 10800`, which is 3 h. Use
  the longest planned session if that is longer.
- **Pass:** the audit passes. Note `finalization_s` and `peak_memory_mb` for V7.

### V2. Frame-rate sweep

The board's timer step is 10 µs at 30 Hz and 1 µs at 120 Hz. Where it changes is not
known, and DBF assumes 10 ms. A wrong step shows up as a camera clock far outside
±100 ppm.

- **Setup:** all three LEDs.
- **Command:** a 2-minute `--bench-record` at each of 15, 20, 30, 40, 50, 60, 75, 90,
  100, and 120 Hz.
- **Pass:** every audit passes, and `camera_clocks` stays near −24 ppm at every rate.
  If a rate fails, the step rule in `rwd_timing.trigger_period_s` must change at that
  rate.

### V3. Restart path on hardware

No restart has happened naturally on the bench, so this path has only run in
simulation.

- **Setup:**
  - Output 1 runs one continuous train (for example 5 Hz, 50 ms, started with the
    recording) and is looped back to Input 2 with a BNC cable.
  - A TTL source stays on Input 1.
- **Command:** `dbf --bench-record rig.json --duration 900 --restart-every 30` at 120
  Hz, then once more at 20 Hz.
- **Pass:**
  - The audit passes.
  - About 30 requested restarts are listed, with no unrequested ones.
  - Every run restarts at 410 nm.
- **Answers:** the `ttl_train_phase` line shows whether outputs pause during a
  restart.
  - If line 2's train kept its phase across the restarts, the output ran on while the
    sequencer was stopped.
  - If not, it paused, and output programs need to account for that.

  Edges are not reported while the sequencer is stopped, so the train's phase is the
  only evidence.

### V4. TTL-to-exposure offset

DBF places TTL edges on the session clock with one offset, estimated from host
arrival times. This check measures that offset's error against light the cameras
actually saw.

- **Setup:**
  - Split Output 1 with a BNC T. One branch goes to Input 2. The other drives an LED
    through a 330 Ω resistor, taped where a fiber sees it, or shining into a patch
    cord's free end.
  - Output 1: 0.4 Hz with 1000 ms pulses.
  - Enable 470 nm only, at 20 Hz with a 45 ms exposure. Nearly every flash then starts
    inside an exposure.
- **Command:** `dbf --bench-record rig.json --duration 600 --flash-line 2`.
- **How it measures:** each flash starts inside one exposure and lights only part of
  it. That fraction gives the onset time to well under a millisecond. The result lists
  the median, spread, and range of edge time minus onset over all flashes. A flash that
  starts between exposures only bounds the offset; those are listed separately.
- **Pass:** median within ±2 ms. Then repeat at 120 Hz with all three LEDs, to measure
  at the operating rate (more flashes will only be bounded).
- **If it fails:** the offset is systematic and can be corrected in `RWDTTLClock`.
  Lowering the FTDI latency timer (Device Manager, port settings, from 16 ms to 1 ms)
  should also tighten it.

### V5. TTL start

- **Setup:** Recording **Start: TTL** on Input 1, LEDs dark while armed.
- **Command:** `--bench-record` with those settings; trigger the start after about
  10 s.
- **Pass:**
  - The recording starts and the audit passes.
  - The `recording_start_trigger` line shows the start latency.
  - It also shows how many frames were seen while armed, which tells whether the
    cameras are triggered while the LEDs are dark.

### V6. Failure paths

Watch the fiber in each case. After each one, recover the spool if needed
(`dbf --recover-spool`) and audit what was saved.

| Failure, during a recording | Must happen |
| --- | --- |
| Stop pressed | LEDs off; files complete and the audit passes |
| Window closed | LEDs off; the recording is saved or its spool recovers |
| Camera USB cable pulled | Error shown; LEDs off; the spool recovers |
| Output disk full (a small USB stick as the output folder) | Error shown; LEDs off; the spool recovers |
| Controller USB cable pulled | Error shown; **LEDs stay on**, which is expected; the power switch turns them off |
| DBF killed in Task Manager | **LEDs stay on**, which is expected; the power switch turns them off; the spool recovers |

The board has no watchdog, so the last two are its known limits. The pass criterion
is that DBF reports them and the operator guide names the power switch.

### V7. Saving long sessions

- **Setup:** the longest planned session, with the planned number of fibers. Up to 9
  fibers give up to 9 files.
- **Command:** V1's run, or a separate `--bench-record`.
- **Pass:**
  - `finalization_s` and `peak_memory_mb` fit the rig PC. Agree a budget, for example
    under 30 minutes and under half the PC's memory.
  - On the rig, 0.3.18 took 6.5 minutes to save a 70-minute, 8-fiber session, with
    74 MB per file. 0.4.1 saves the same simulated session in 34 s instead of 316 s,
    with 2.4 MB files, and a simulated 4.6-hour session in 104 s with a 1.35 GB peak.
    It still holds the whole session's exposure records in memory, now as compact
    columns (about 6 GB expected for 22 hours); if the budget fails, the streaming
    finalizer in the roadmap's storage-scale gate is needed before the rig counts as
    validated.

### V8. Display scaling

Check the GUI at 100, 125, and 150 % Windows scaling on the rig's monitor, in both
themes. Every recording control must be reachable.

## Results log

| Date | DBF | Check | Settings | Result | Notes |
| --- | --- | --- | --- | --- | --- |
| | | | | | |
