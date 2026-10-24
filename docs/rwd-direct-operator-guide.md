# Recording with RWD hardware

This guide covers recording fiber photometry with an RWD R821 multichannel system,
driven directly by DBF without RWD's OFRS software. The design and its evidence are in
[the RWD direct-control design record](rwd-direct-control.md).

DBF has been bench-tested on one R821 (firmware 2.0.0, build 37619) with two Allied
Vision Alvium 1800 U-040m cameras. The tests covered previews and recordings at 15, 20,
30, and 50 Hz with all three LEDs at 5 %. DBF refuses other RWD models.

## Safety first

**The RWD unit has no fail-safe.** Bench tests showed that its LEDs stay on if the
computer stops talking to it, if DBF closes without switching them off, or if the USB
cable is unplugged.

- DBF switches every LED, the sequencer, and every TTL output off when it connects,
  when a preview or recording ends, when it stops on an error, and when you close the
  window during a recording.
- If DBF crashes, the computer freezes, or the cable comes out, the LEDs can stay on.
  **Use the unit's power switch to turn them off.**
- During setup and previews, keep the fiber on its target or capped. Never look into
  the fiber or point it at anyone.

## Install once

1. **Vimba X.** Install Allied Vision Vimba X with its USB transport layer. Vimba X's
   bundled camera simulators can stay installed; DBF ignores them.
2. **DBF with RWD support:**

   ```bash
   python -m pip install "driftless-bundle-photometry[rwd]"
   ```

3. **Controller driver.** Windows Update installs the FTDI driver for the
   controller's USB cable, which then appears as a COM port.
4. **Optional:** in Device Manager, open the controller's USB Serial Port, then
   Port Settings → Advanced, and set the Latency Timer to 1 ms. The default is 16 ms.
   This should tighten how precisely TTL edges are placed against the exposures.
5. **Optional, for Measure power:** a Thorlabs PM100 or PM400 meter needs a VISA
   runtime, such as NI-VISA or Thorlabs' Optical Power Monitor driver, and
   `python -m pip install "driftless-bundle-photometry[power-meter]"`.

Start DBF after installing Vimba X, from a new window or the installed shortcut, so it
can find the camera drivers. OFRS and its licence dongle are not needed. Close OFRS
before starting DBF, because only one program can use the controller's port.

## Before each session

- Switch the RWD unit on and connect both USB cables.
- Close Vimba X Viewer or any other program that has the cameras open.
- The cameras must keep RWD's `DeviceUserID` settings: `1` for the camera imaging
  410/470 nm and `2` for the 560 nm camera. Vimba X Viewer shows them.
- After you choose **RWD**, the **Devices** list in the RWD hardware panel shows a
  tick for the controller (with its COM port and firmware) and for each camera (with
  its serial number). A cross names what is missing. Fix it and press **Check
  devices**. The check switches no LED on.

## Set up a recording

1. Start DBF (`dbf --gui` or the installed shortcut) and choose **RWD** under
   *Acquisition system*. `dbf --demo` is restricted to the Custom simulator.
2. **Excitation:** enable the LEDs you need and set each one's power in percent. The
   board can run one LED, 410+470, 470+560, or all three; it has no 410+560 mode.
3. **Camera settings** (on *Camera & fiber ROIs*):
   - **Frame rate:** the total rate shared by the enabled LEDs. It must be a whole
     number between 10 and 250 Hz. With three LEDs at 30 Hz, each LED gets 10 frames
     per second.
   - **Exposure:** at most (1000 ÷ frame rate, rounded down) ms minus 2.63 ms, and at
     least 0.5 ms. **Max** sets the longest exposure, which RWD recommends.
   - **Gain, camera 1 / camera 2:** each camera's gain in dB (0–48). Camera 2 images
     560 nm, which can be much brighter, so set it separately. Raise gain to find and
     focus the fiber face, then record at 0 dB; gain does not improve the
     signal-to-noise ratio.
4. **RWD hardware panel:**
   - **Controller port:** *Auto* works when the controller is the only FTDI device.
     Otherwise choose its COM port. Use *Refresh* after plugging it in.
   - **TTL outputs:** tick an output to drive it during the recording. **Edit…**
     sets its program:
     - the pulse frequency and width;
     - what starts it: the recording's start, its **Fire** button (shown next to the
       markers while recording), or a live dF/F threshold on one ROI and wavelength
       (F0 is the median of the first seconds, with a wait between triggers);
     - a delay, and either one train until the recording ends or timed trains, each
       followed by a gap, repeated for a number of cycles;
     - further steps with their own pulses, and repeats of the whole program.

     Each train is recorded as an *output* event mark with its length, for example
     to mark optogenetic stimulation, and is shaded on the live traces. DBF times
     trains from the computer, to within a few milliseconds. For board-clock times,
     loop the output back to a TTL input. **Test** drives the output for 1 s with the
     LEDs off, after asking, so a connected laser fires too.
5. **TTL inputs and events** (left panel):
   - **TTL inputs:** tick the BNC inputs you use, label them, and choose which edges
     to record. All four record both edges by default.
   - **Markers:** give keys 1–4 a name, for example *Tone*. While recording, press
     the key or click its button below the traces to mark that event. Markers are
     timed on the computer's clock through the latest camera frame, so they can lag
     the true moment by one exposure plus readout. A key does nothing while you type
     in a text box.
   - **Named TTL events:** name the pulses on an input by their width, from the
     rising to the falling edge, for example *Lever* for 50–150 ms. The input must
     record both edges. The event is timed at the pulse's rising edge on the board
     clock.

   Markers and named events appear as dashed lines on the traces while recording.
   None of these settings needs a new preview.

## Place the fiber circles on both cameras

The 560 nm light is imaged by the second camera, so every fiber needs a circle on each
camera.

1. On *Camera & fiber ROIs*, press **Live view**. DBF connects, switches the LEDs on
   with the current excitation settings, and streams both cameras. Nothing is
   recorded. The LEDs stay on until you press the same button, now **Stop live
   view**, the header **Stop**, or **Escape**); live view stops by itself after 10 minutes.
2. Keep **Auto contrast** ticked while focusing. It stretches the display between
   the image's dim and bright pixels, which matters at low LED power. It changes only
   the display, never the recorded data.
3. The two cameras are shown side by side. Drag each fiber's solid circle over its
   fiber on **Camera 1 — 410/470 nm**, and drag its handle to resize it, or type its
   **Diameter** in the fiber's ROI tab. Diameters are 40–544 px, as in OFRS.
4. On **Camera 2 — 560 nm**, place each fiber's dashed circle over the same fiber.
   Camera 2 is mirrored to match camera 1, but the fibers will not sit at exactly the
   same pixels.

   Each circle is named beside it. **Apply diameter to all ROIs** in a fiber's tab
   gives every circle that fiber's size. Once placed, tick **Lock ROI circles** so they
   cannot be dragged by accident.

   During live view, gain changes take effect at once. Frame-rate or exposure changes
   restart live view, because the controller takes them only at the start.
5. Press **Stop**, then fill in each ROI's animal and region details. Each ROI becomes
   its own NWB file.
6. Press **Run preview**. It runs the full acquisition path, live traces included,
   until you press **Stop** (10 minutes at most). Nothing is saved. While it
   runs, move the LED power sliders to set each LED. The board restarts its LED cycle
   for each change, and the short gap is marked. Which LEDs are on cannot change
   during a preview. When you stop it, the preview counts for recording only if it
   ended on the current powers and nothing else changed. Any later change to the
   settings or circles needs a new preview before **Record** is enabled.

## Markers and notes while recording

- TTL input edges appear on the traces as thin dashed lines, one colour per input.
  The **TTL** switch above the traces hides them all; the numbered switches hide one
  input each.
- The **Rows** menu above the traces picks which ROIs' traces are shown. Untick the
  others (or *Select none*, then tick one) to give one ROI the whole trace area;
  *Select all* brings every row back.
- To note something, tick the ROIs it is about under the traces (all are ticked by
  default), type the note and press Return. It is timed when you press Return, drawn
  on those ROIs' rows, listed under **Show notes**, and saved in each ticked ROI's
  NWB file.
- If animals have their own behavior inputs, assign each TTL input to its ROIs with
  the button beside the input's label. Each ROI's NWB file then keeps only its own
  inputs' edges.

## Measure LED power

With a Thorlabs PM100 or PM400 connected, point the fiber tip at the meter's sensor
and press **Measure power** in the RWD panel. No animal may be connected, and nobody
may look into the fiber. After you confirm, DBF lights each enabled LED alone, on
continuously at its set power, for about 2 s. It sets the meter's wavelength
correction to that LED, averages 10 readings, and switches the LED off. The panel lists
each LED's power in mW. In an interleaved recording each LED is lit only during its
own exposures, so its average power is lower. Close Thorlabs' Optical Power Monitor
first, because it holds the meter open. This has not yet been run with a physical
meter.

## Record

RWD recommends running the LEDs for 5 minutes before recording so their output
settles. Live view counts, so keep it running while you place the circles until
**LED warm-up** in the RWD panel shows a tick. The preview counts too, and a gap of
up to 3 minutes between Live view and **Record** keeps the warm-up. With less warm-up,
**Record** asks before it starts.

To bleach the patch cords' autofluorescence first, cap the fiber ends (or put them on
a non-fluorescent target, with no animal attached) and press **Bleach patch cords…**
beside Live view. Choose the time and LED power; the LEDs switch off by themselves when
it ends, or press **Stop bleaching**. The LEDs pulse with the camera exposures, so set
the exposure to Max for the strongest bleach. A bleach also warms the LEDs up, and the
next recordings log it.

Choose when the recording starts and stops in the *Recording* group:

- **Start:** *Immediately*, *After a delay* (0.1 s to 24 h), or *On a TTL input*
  (an input and its rising or falling edge). DBF connects and opens the cameras
  first, then waits with the LEDs off, so the recording begins within a fraction of
  a second. While it waits, the status reads *WAITING*. **Stop** cancels the wait
  and nothing is recorded.
- **Stop:** *After the duration*, *When I press Stop* (24 hours at most), or *On a
  TTL input*. A TTL stop still ends at the **Maximum length**. Its input must be a
  configured TTL input recording that edge.

- **Excitation:** *Continuous*, or *Sequential* with an on and an off time (1 s to
  24 h each). Sequential lights the LEDs for the on time, then keeps them dark for the
  off time, repeating from the start of the recording, to limit photobleaching. No
  frames are recorded while dark, but TTL inputs still are, and a TTL stop still
  works. The live traces and progress bar pause with the LEDs.

None of these choices needs a new preview.

While recording, the trace display's **Y axis** can show *Absolute* counts (the mean
12-bit pixel value in each circle), *dF/F* against a median baseline, *dF/F, control
fit*, or *dF/F, rolling F0*. The control fit takes each 470 and
560 nm trace's F0 from the 410 nm trace, fitted to it by least squares over the
recording so far, which removes shared artifacts such as bleaching and motion. The
410 nm trace is hidden in that mode. *dF/F, rolling F0* uses the median of the
preceding *Baseline* seconds, which follows slow drift. All of them change only the
display.

If the display history is aggregated, its hint identifies this and live dF/F is an
approximation. Saved raw samples and events remain complete. Only the most recent
512 event marks are drawn. Use **Review setup…** to check subjects, wavelengths,
recording conditions, output location, and the estimate for the complete ROI file
set before pressing Record.

Press **Record**. DBF connects, sends the settings, starts the LED sequence, and
records until the stop condition or **Stop**. It then saves and validates one NWB
file per ROI. Connecting and opening the cameras takes about 7 seconds. The NWB
system events record what started and stopped the recording.

If something interrupts the frame stream, DBF stops the sequencer, marks the gap as
invalid, and restarts it. For example, a camera might miss a frame. The restart keeps
every recorded wavelength correct.

## Where a recording is saved

Each recording gets its own folder inside the output root, named after the session:
its ROI NWB files, `session_log.txt` (a readable account of the session, with every
event, marker, note, and stimulation in time order), `settings.json`, and one
`roi_map_{nm}nm.png` per wavelength with every ROI outlined and named in its colour. The NWB files contain the
same log and maps.

## What each NWB file records

- Raw fluorescence per wavelength (410, 470, and 560 nm) for that ROI's fiber, with 560
  nm measured on camera 2.
- For every exposure:
  - its camera, the camera's own frame number and timestamp, and the commanded LED
    power in percent;
  - `wavelength_source = phase_derived`. The RWD controller does not report which LED
    fired, so DBF works it out from the camera and the position in the LED cycle after
    each sequencer start.
- TTL input edges with the board's raw millisecond ticks.
- Event marks: each marker and named TTL event, with its source (key, button, or ttl)
  and, for TTL events, the pulse width.
- System events, including the controller's identity, each sequencer start and
  restart, and the TTL clock alignment.
- Invalid time spans around any restart.

Every file embeds the complete settings, so *Load NWB…* restores the whole setup.

## When something goes wrong

| Message or symptom | What it means | What to do |
| --- | --- | --- |
| LEDs still on after DBF stopped | DBF could not send the off sequence | Switch the unit off with its power switch |
| "shutdown unconfirmed" | One or more off commands could not be confirmed | Use the unit's power switch; do not assume a closed window means LEDs or outputs are off |
| "first frame arrived too late to establish wavelength phase" | Initial wavelength identity is ambiguous | Reduce the frame rate or camera/USB latency; DBF refuses to guess |
| "no FTDI serial port for an RWD controller was found" | The controller's USB cable or power is missing | Connect it, switch the unit on, press *Refresh* |
| "several FTDI serial ports were found" | Another FTDI device is attached | Choose the controller's port in the RWD panel |
| "did not acknowledge" or "sent nothing for more than 5 s" | The controller link was lost | The LEDs may still be on: use the power switch, reconnect, and try again |
| "reports model …; DBF drives only the bench-validated R821" | A different RWD model | Not supported yet |
| "camera(s) with DeviceUserID … not found" | A camera is unplugged, in use by another program, or lost its ID | Close Vimba X Viewer, check the cables, check the IDs |
| "Vimba X could not load its camera transport layers" | Vimba X is missing, or DBF started before it was installed | Install Vimba X, then restart DBF |
| "could not attribute frames after 3 sequencer restarts" | A camera stopped delivering frames | Check the camera cables and USB ports; avoid sharing a USB controller with other heavy devices |
| "RWD trigger period … not the commanded …" | The board's timing does not match what DBF expects at this frame rate | Choose another frame rate and report it |
| `rwd_sequencer_restart` events in a file | A frame was missing or off its timing, so DBF restarted | The gap is marked invalid; frequent restarts point to a camera or USB problem |

## Known limits

For interrupted recordings, open **Configuration → Recover an interrupted
recording…**. Select the output folder and session; the dialog verifies checksums
and shows counts before recovery. It keeps the spool by default. A failed canonical
file comparison leaves the source spool intact. Stop/Escape also cancel output
tests and power measurement; closing stays responsive while hardware cleanup or
NWB validation finishes.

- The offset between TTL edges and exposures is estimated from host arrival times.
  It was about 2 ms in simulation and has not been measured on hardware. Intervals
  between TTL edges are exact to the board's 1 ms tick.
- The new first-arrival gate and failure cleanup changes have simulator tests and
  require physical fault/latency revalidation, including high frame rates.
- Whether TTL outputs pause during a restart has not been measured.
