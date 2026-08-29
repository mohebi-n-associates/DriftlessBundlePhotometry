# Controller protocol contract

The planned timing controller is the authority for exposure identity, excitation
wavelength, commanded intensity, and all four behavior TTL inputs. The Python host
must never use GUI or operating-system timing to schedule exposure-critical signals.

Wire messages are newline-delimited `DBP1|JSON|CRC32` records. JSON is canonical,
UTF-8 encoded, versioned, sequenced, and protected by an eight-digit hexadecimal
CRC32. The streaming decoder accepts serial fragmentation, bounds its input buffer,
and rejects malformed, corrupt, unknown-version, and unknown-type messages.

Required lifecycle:

```text
DISCONNECTED --READY--> READY --ARM--> ARMED --START--> RUNNING
                                  \--STOP--> READY <--STOP--/
any state --ERROR--> ERROR --physical safe/reset--> DISCONNECTED
```

`HELLO` negotiates the protocol and firmware identity. `CONFIG` carries channel
voltages, schedule, TTL edge selection, trigger timing, and watchdog interval;
`ACK` must refer to the accepted configuration. `EXPOSURE` and `TTL` are immutable
records with raw controller ticks and independent monotonic sequences. `HEARTBEAT`
maintains the watchdog. `STOP`, disconnect, parse failure, watchdog expiry, reset,
and firmware error must disable every LED gate and drive intensity outputs to their
electrically characterized safe state.

The codec, bounded streaming decoder, host lifecycle guard, and single-owner serial
transport are implemented and simulator-tested. The transport sends `HELLO` when it
opens and makes a best-effort `STOP` before closing. Firmware, integration of the
transport with the matched camera stream, DAC selection, electrical levels, trigger
pulse widths, watchdog latency, and clock accuracy still require hardware
implementation and bench characterization before animal experiments.
