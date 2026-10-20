# RWD controller bench capture

This procedure records the serial traffic between RWD's OFRS software and the RWD
controller board while OFRS runs a normal session. The capture confirms the
[controller protocol](rwd-direct-control.md) and answers its open questions. It is
passive: it changes neither OFRS nor the device, and DBF sends nothing to the hardware.

## Before you start

- Use the rig PC that already runs OFRS with its dongle and the RWD unit attached. You
  need administrator rights once, to install Wireshark.
- Do not connect an animal. The LEDs will turn on: never look into a fiber tip. Cap the
  fiber or point it at a power meter.
- Do not open **Help > Maintenance setting** or update firmware during the capture.

## 1. Install the capture tool (once)

Install Wireshark for Windows (x64) from wireshark.org and select the **USBPcap**
component in the installer. Reboot if the installer asks.

## 2. Capture only the controller

1. Power on the RWD unit. In Device Manager, **Ports (COM & LPT)** should list a
   **USB Serial Port (COMn)**. That is the controller.
2. Start Wireshark. Each `USBPcapN` interface has a gear icon that lists its attached
   devices. Find the interface that contains the FTDI serial device.
3. On that interface, clear **Capture from all devices connected** and
   **Capture from newly connected devices**, then select only the FTDI device.

Capturing everything would also record the photometry cameras' video stream, which
fills a disk within minutes and is not needed.

## 3. Record a scripted session

Start the capture, then work through these steps in OFRS. Pause about 10 seconds
between steps so they separate in the decoded timeline, and note the clock time of each
step.

| Step | In OFRS | Confirms |
| --- | --- | --- |
| 1 | Launch, choose **Photometry**, and wait until connected. | Handshake, version, heartbeat. |
| 2 | Select 470 only. Set power to 10 %, then 20 %, then 50 %. | LED power encoding. |
| 3 | Select 410 only, then 560 only (if fitted), then 410+470, 470+560, and 410+470+560. | LED enable and excitation modes. |
| 4 | In **Channel Setting**, choose 20, 60, then 100 fps. At 60 fps, change the exposure twice. | Frame-rate and exposure units. |
| 5 | Start a preview, wait 20 s, stop it. | What starts and stops the sequencer. |
| 6 | With 410+470+560 at 60 fps, record for 30 s, then stop. | The recording resync sequence. |
| 7 | In **DI/O**, set Output1 to 20 Hz / 5 ms and start its output preview, then stop. Repeat at 10 Hz / 10 ms. | Output configuration. |
| 8 | If you have a TTL source, send a few pulses into **Input 1** during a short recording and note how many. | Input-edge payload, including bytes 1–4. |
| 9 | Exit OFRS normally. | Shutdown sequence. |

Optional last step, if you accept that OFRS may need restarting: start a recording and
unplug the controller's USB cable. Note whether the LEDs turn off and how quickly. This
answers what the board does when the host disappears.

## 4. Save the evidence

1. Stop the capture and use **File > Save As** to save a `.pcapng` file, for example
   `rwd-ofrs-2026-10-21.pcapng`.
2. Keep these with it: the OFRS output folder from step 6, the OFRS log for the day
   (`Log\Info\<date>.txt` under the OFRS install folder), and your step times.
3. Store captures on lab storage, not in this repository.

## 5. Decode

On any machine with DBF installed:

```bash
dbf --decode-rwd-usb-capture rwd-ofrs-2026-10-21.pcapng --rwd-usb-csv rwd-ofrs-2026-10-21.csv
```

The command prints a JSON summary: the selected USB device, the detected FTDI packet
size, frame counts per direction, and a timeline of every frame except heartbeats and
acknowledgements, with decoded descriptions. The CSV lists every frame. Malformed
frames are reported in both, never dropped. If the decoder reports that no device
carried controller frames, the FTDI device was probably not selected in step 2.

## 6. After decoding

- Promote short excerpts of real frames into golden test fixtures.
- Update the confidence column in [the controller protocol](rwd-direct-control.md) and
  resolve its open questions.
- Wireshark and USBPcap can be uninstalled from the rig PC afterwards.
