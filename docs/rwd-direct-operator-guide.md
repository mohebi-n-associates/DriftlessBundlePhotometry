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

Start DBF after installing Vimba X, from a new window or the installed shortcut, so it
can find the camera drivers. OFRS and its licence dongle are not needed. Close OFRS
before starting DBF, because only one program can use the controller's port.

## Before each session

- Switch the RWD unit on and connect both USB cables.
- Close Vimba X Viewer or any other program that has the cameras open.
- The cameras must keep RWD's `DeviceUserID` settings: `1` for the camera imaging
  410/470 nm and `2` for the 560 nm camera. Vimba X Viewer shows them.

## Set up a recording

1. Start DBF (`dbf --demo`) and choose **RWD** under *Acquisition system*.
2. **Excitation:** enable the LEDs you need and set each one's power in percent. The
   board can run one LED, 410+470, 470+560, or all three; it has no 410+560 mode.
3. **Camera settings** (on *Camera & fiber ROIs*):
   - **Frame rate:** the total rate shared by the enabled LEDs. It must be a whole
     number between 10 and 250 Hz. With three LEDs at 30 Hz, each LED gets 10 frames
     per second.
   - **Exposure:** at most (1000 ÷ frame rate, rounded down) ms minus 2.63 ms, and at
     least 0.5 ms. **Max** sets the longest exposure, which RWD recommends.
   - **Gain:** the cameras' gain in dB (0–48). Raise it to find and focus the fiber
     face, then record at 0 dB; gain does not improve the signal-to-noise ratio.
4. **RWD hardware panel:**
   - **Controller port:** *Auto* works when the controller is the only FTDI device.
     Otherwise choose its COM port. Use *Refresh* after plugging it in.
   - **TTL outputs:** tick an output to drive a pulse train on it during the
     recording, with its frequency and pulse width.
5. **TTL inputs:** label the BNC inputs you use. Both edges are recorded by default.

## Place the fiber circles on both cameras

The 560 nm light is imaged by the second camera, so every fiber needs a circle on each
camera.

1. Press **Live view**. DBF connects, switches the LEDs on with the current excitation
   settings, and opens *Camera & fiber ROIs* with the cameras streaming. Nothing is
   recorded. The LEDs stay on until you press **Stop**; live view stops by itself
   after 10 minutes.
2. Keep **Auto contrast** ticked while focusing. It stretches the display between
   the image's dim and bright pixels, which matters at low LED power. It changes only
   the display, never the recorded data.
3. The two cameras are shown side by side. Drag each fiber's solid circle over its
   fiber on **Camera 1 — 410/470 nm**, and drag its handle to resize it.
4. On **Camera 2 — 560 nm**, place each fiber's dashed circle over the same fiber.
   Camera 2 is mirrored to match camera 1, but the fibers will not sit at exactly the
   same pixels.

   During live view, gain changes take effect at once. Frame-rate or exposure changes
   restart live view, because the controller takes them only at the start.
5. Press **Stop**, then fill in each ROI's animal and region details. Each ROI becomes
   its own NWB file.
6. Run **Preview**. Any change to the settings or circles needs a new preview before
   **Record** is enabled.

## Record

Press **Record**. DBF connects, sends the settings, starts the LED sequence, and
records until the duration ends or you press **Stop**. It then saves and validates one
NWB file per ROI. Connecting and opening the cameras takes about 7 seconds.

If something interrupts the frame stream, DBF stops the sequencer, marks the gap as
invalid, and restarts it. For example, a camera might miss a frame. The restart keeps
every recorded wavelength correct.

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
- System events, including the controller's identity, each sequencer start and
  restart, and the TTL clock alignment.
- Invalid time spans around any restart.

Every file embeds the complete settings, so *Load NWB…* restores the whole setup.

## When something goes wrong

| Message or symptom | What it means | What to do |
| --- | --- | --- |
| LEDs still on after DBF stopped | DBF could not send the off sequence | Switch the unit off with its power switch |
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

- The offset between TTL edges and exposures is estimated from host arrival times.
  It was about 2 ms in simulation and has not been measured on hardware. Intervals
  between TTL edges are exact to the board's 1 ms tick.
- The restart path has only been tested in simulation, because no bench run needed a
  restart.
- Whether TTL outputs pause during a restart has not been measured.
