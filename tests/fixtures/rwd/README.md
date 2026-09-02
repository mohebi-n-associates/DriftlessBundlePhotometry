# Synthetic RWD protocol fixtures

These hex fixtures encode the byte layouts documented in
`docs/rwd-streaming-protocol.md`. They contain no animal data and were not captured
from a physical RWD system.

- `fluorescence_all.hex`: one `RWD1` fluorescence record, mask `0x07`, device
  channel 2, with 410/470/560 samples.
- `event_on.hex`: one `RWD1` event record at tick 42, name `lever press`, status ON.
- `mixed_with_banner.hex`: a standalone `RWD1` preamble followed by those two
  records in wire order.

Real packet captures are still required to confirm preamble behavior, timestamp
units, scaling, channel numbering, and padding before live validation.
