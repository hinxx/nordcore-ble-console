# ESP32 Treadmill UART Sniffer

Passive dual-UART sniffer for the treadmill console/baseboard link using an original ESP32-WROOM-32 development board.

## Electrical wiring

The treadmill serial signals are approximately 5 V logic. Use one divider per input:

```text
line A ---- 10k ----+---- GPIO26
                    |
                   15k
                    |
                   GND

line B ---- 10k ----+---- GPIO27
                    |
                   15k
                    |
                   GND

console GND -------------- ESP32 GND
```

Do not connect an ESP32 TX pin to either treadmill line in the passive-sniffer stage.

For installed operation the ESP32 board may be fed from the console's regulated +5 V rail via its `5V`/`VIN` pin. During USB development, disconnect the external +5 V feed unless the development board's USB/external-5-V power-path isolation has been verified.

## UART setup

- GPIO26 -> ESP32 UART1 RX — taps console/HC32L130 TX -> baseboard
- GPIO27 -> ESP32 UART2 RX — taps baseboard TX -> console/HC32L130 RX
- 1200 baud, 8 data bits, no parity, 2 stop bits
- UART0 remains the normal USB serial/programming console

Direction was confirmed by capture, not assumed in advance — see "Wire direction" under Protocol observations below. The firmware's output now labels frames by direction (`BASE->CON` / `CON->BASE`) rather than by raw GPIO name; see Output below.

## Bootstrap on Ubuntu / Mint / Debian

```bash
./tools/bootstrap.sh
```

This installs common host prerequisites, clones **ESP-IDF v5.5.5** recursively into `.tooling/esp-idf`, and runs Espressif's official `install.sh esp32` to fetch the target toolchain and Python tooling.

If host prerequisites are already installed:

```bash
./tools/bootstrap.sh --skip-host-deps
```

## Build

```bash
./tools/idf.sh build
```

## Flash and monitor

Find the USB serial port, commonly `/dev/ttyUSB0`, then:

```bash
./tools/idf.sh -p /dev/ttyUSB0 flash monitor
```

Exit the ESP-IDF monitor with `Ctrl+]`.

## Output

The firmware reassembles each direction's byte stream into complete `68 LEN ... CS 43` frames (frame shape and checksum are covered under "Protocol observations" below) and emits one line per complete, checksum-valid frame:

```text
000001234567 BASE->CON OK 68 0C A0 00 00 00 00 9F 00 00 00 00 4B 43
000001234787 CON->BASE OK 68 08 20 00 00 00 00 14 3C 43
```

Direction is printed directly — `BASE->CON` (baseboard -> console/HC32L130, tapped on GPIO27) and `CON->BASE` (console/HC32L130 -> baseboard, tapped on GPIO26) — rather than the raw `GPIO26`/`GPIO27` tag an earlier revision of this firmware used; see "Wire direction" below for how the mapping was confirmed.

Timestamps are software receive time (when the sniffer task pulled the frame's first byte out of the UART driver's ring buffer), not oscilloscope-grade wire-arrival time. At 1200 baud 8N2 (~9.17 ms per character) ordinary scheduler jitter is far smaller than the inter-byte spacing, so this is fine for protocol reverse engineering but should not be treated as precise bit-level timing.

### Mangled data

Anything that doesn't parse as a complete, checksum-valid frame is printed as its own line, clearly distinct from a real message, instead of being folded into a byte stream or silently dropped:

```text
000001235012 CON->BASE MANGLED bad_checksum computed=4C 68 0C A0 00 00 00 00 9F 00 00 00 00 4D 43
000001235300 BASE->CON MANGLED stray_bytes 0E 00 00 00 4B 43
000001236500 CON->BASE MANGLED timeout_incomplete 68 0C A0
```

- `bad_checksum` / `bad_end=XX` — a complete frame's worth of bytes was collected, but the checksum or the trailing `0x43` didn't match.
- `len_too_small` / `len_too_large` — the byte after `0x68` implied an implausible frame length and was rejected immediately rather than waiting on a frame that was never coming.
- `stray_bytes` — bytes that didn't start with `0x68` (capture noise, or the parser resynchronizing after a corrupt frame).
- `timeout_incomplete` — a frame (or a run of stray bytes) sat unfinished for 750 ms with nothing further arriving, so it was flushed rather than held forever. This is also what reports the sniffer's own startup/shutdown boundaries: it can start listening mid-frame, and a capture stopped mid-frame leaves a partial one.
- `interrupted_by_overflow` — an in-progress frame was abandoned because the UART driver reported `FIFO_OVF`/`BUFFER_FULL` (see Error events below); the lost bytes can't be recovered, so what was collected so far is reported instead of discarded silently.

This parser (framing, length handling, and checksum validation) was verified by replaying the full `log1.txt` capture byte-for-byte through the same logic: every one of the 53 `BASE->CON` and 66 `CON->BASE` real frames comes back `OK`, and the only `MANGLED` output is the capture's own start/end boundaries — 6 bytes of a frame the capture began mid-way through, and a trailing frame still in progress when the capture ended.

### Error events

UART framing/parity errors and RX overflow are reported inline as their own lines rather than being silently dropped, so a later analysis doesn't mistake a lossy capture for a complete one:

```text
000001234567 CON->BASE ERROR FRAME_ERR
000001235012 BASE->CON ERROR FIFO_OVF bytes_lost=unknown
000001235014 BASE->CON ERROR RX_FLUSH
```

`FIFO_OVF` and `BUFFER_FULL` are followed by an `RX_FLUSH` line once the driver's input buffer has been flushed to recover, and by a `MANGLED interrupted_by_overflow` line for any frame that was in progress at the time (see Mangled data above); any bytes lost to the overflow are not recoverable and are not counted.

## Protocol observations (preliminary, not yet a contract)

### Wire direction

Confirmed from a captured log (53 frames on GPIO27, 66 frames on GPIO26):

| GPIO | Chip role | Direction | Dominant frame |
|---|---|---|---|
| GPIO27 | HC32L130 RX | Baseboard -> console/HC32L130 | `68 0C A0 00 00 00 00 9F 00 00 00 00 4B 43` |
| GPIO26 | HC32L130 TX | Console/HC32L130 -> baseboard | `68 08 20 00 00 00 00 14 3C 43` |

The GPIO27 frame is the periodic baseboard status/heartbeat — frame starts land almost exactly **220 ms apart** for essentially the whole capture. The GPIO26 frame is invariant byte-for-byte across all 66 occurrences in this capture; it sometimes appears as isolated frames and sometimes as bursts about **20.8 ms apart**, consistent with a console-originated command/acknowledgement rather than a periodic heartbeat.

### Message timing (measured from `log1.txt`)

GPIO27's 53 frames, by payload variant:

| Count | Frame | Payload difference |
|---|---|---|
| 48 | `68 0C A0 00 00 00 00 9F 00 00 00 00 4B 43` | steady-state (baseline) |
| 4 | `68 0C A0 00 00 00 00 A0 00 00 00 00 4C 43` | payload byte 7: `9F` -> `A0` |
| 1 | `68 0C A0 00 00 00 00 9F 00 01 00 00 4C 43` | payload byte 9: `00` -> `01` |

Reconstructing all 53 frames' start timestamps from the raw per-byte log and diffing them: every GPIO27 frame — regardless of which of the three payloads it carries — lands on the same fixed **220 ms tick** (measured range 219.90–226.67 ms, average 220.13 ms across all 52 gaps). The three payloads are not separately-timed message types; they're mutually exclusive states reported on one fixed heartbeat slot. A given payload can occupy consecutive ticks — the `A0` variant appears three ticks in a row once in this capture.

Gap between repeats of the *same* payload (i.e. skipping ticks occupied by a different payload):

| Payload | Occurrences | Gap between repeats |
|---|---|---|
| `9F...00...00` (baseline) | 48 | 220 ms typical, up to 880 ms (4 ticks) when another payload pre-empts a slot |
| `A0...00...00` | 4 | 220 ms when back-to-back, up to 5.5 s (25 ticks) between separated occurrences |
| `9F...01...00` | 1 | only one occurrence in this capture — no repeat interval yet |

For contrast, GPIO26 does **not** run on a fixed period: the same invariant frame (`68 08 20 00 00 00 00 14 3C 43`) either appears in isolation or as a burst of ~6–7 repeats spaced **~9.8–20.8 ms apart** (drifting up to ~40–90 ms toward the end of a burst), with **~1.07–1.25 s** gaps between bursts — consistent with a command being retransmitted by the console rather than a periodic status tick.

### The two streams are not synchronized

Cross-referencing frame timestamps between GPIO26 and GPIO27 directly (not just each stream's own stats) shows the two run on independent, free-running clocks:

- **Burst period is not an integer multiple of the heartbeat tick.** GPIO26's burst-to-burst gaps are highly consistent (`1073.333`/`1073.334 ms` or `1253.334 ms`, repeatable to the microsecond across the capture) but land at **4.879×** or **5.697×** the 220 ms GPIO27 tick — not a clean 5× or 6×, so the burst cadence isn't derived by counting GPIO27 ticks. The two gap values differ by exactly `180.00 ms`, suggesting the console's own loop occasionally takes one extra step of that size — internal to GPIO26's side, unrelated to GPIO27.
- **The phase relationship drifts freely.** The offset from each GPIO26 burst start to the nearest surrounding GPIO27 frame, across all 9 bursts in this capture: `179, 72, 146, 39, 152, 46, 159, 46 ms` (plus edge cases at the very start/end of the log). That spans nearly the entire 0–220 ms tick window with no repeating pattern — a phase-locked or triggered relationship would hold that offset roughly constant instead.
- **They are not paired 1:1.** Early in the capture there are four consecutive GPIO27 heartbeat ticks (`t = 594, 814, 1034, 1254 ms`) with zero GPIO26 activity interleaved between them.

In short: GPIO27 (baseboard) ticks on its own fixed 220 ms clock and GPIO26 (console) bursts on its own ~1073/1253 ms clock; they interleave in the capture because both run continuously, not because either one triggers or paces the other.

No `FRAME_ERR`, `PARITY_ERR`, `FIFO_OVF`, or `BUFFER_FULL` events were seen in this capture, so the divider + UART setup reads as electrically clean at this baud rate.

Captured frames observed so far all share this shape:

```
68  0C  A0 00 00 00 00 9F 00 00 00 00  4B  43
68  LEN [------ 10 bytes payload ----] CS  END
```

- `0x68` — fixed start byte.
- `LEN` (`0x0C` = 12 in every sample so far) — counts everything *after itself*: 10 payload bytes + 1 checksum byte + 1 end byte.
- 10 payload bytes.
- `CS` — an 8-bit checksum (see below).
- `0x43` — fixed end/tail byte, constant in every sample so far, not part of the checksum.

### Checksum

`CS` is a plain running sum, mod 256, over the `LEN` byte plus all 10 payload bytes (the start byte `0x68` and the end byte `0x43` are excluded):

```
checksum = (LEN + payload[0] + payload[1] + ... + payload[9]) & 0xFF
```

Verified against three distinct captured frames:

| Frame | Bytes summed (`LEN` .. last payload byte) | Sum | `& 0xFF` | Captured `CS` |
|---|---|---|---|---|
| `68 0C A0 00 00 00 00 9F 00 00 00 00 4B 43` | `0C A0 00 00 00 00 9F 00 00 00 00` | `0x14B` | `0x4B` | `4B` ✅ |
| `68 0C A0 00 00 00 00 A0 00 00 00 00 4C 43` | `0C A0 00 00 00 00 A0 00 00 00 00` | `0x14C` | `0x4C` | `4C` ✅ |
| `68 0C A0 00 00 00 00 9F 00 01 00 00 4C 43` | `0C A0 00 00 00 00 9F 00 01 00 00` | `0x14C` | `0x4C` | `4C` ✅ |

No CRC, no carry-fold, no two's-complement — a straightforward 8-bit additive checksum. It also holds on the GPIO26 (console/HC32L130 -> baseboard) frame: `08 + 20 + 00 + 00 + 00 + 00 + 14 = 0x3C`, matching its checksum byte, so the formula is not specific to one direction.

### Live-looking fields

Across the three samples above, only two payload positions changed:

- payload byte 5 (frame offset 7, right after the four `00`s): `9F` / `A0` / `9F`
- payload byte 7 (frame offset 9): `00` / `00` / `01`

Everything else (`A0 00 00 00 00 ... 00 00`) held constant. These two positions are the best current lead on which fields carry live console/baseboard state, but this is still an observation from a handful of frames, not a validated field map — treat as a starting point for further capture and correlation, not as ground truth.

Frame reassembly and checksum validation are now implemented in firmware, as described under Output above — this goes beyond `REQUIREMENTS.md`'s originally frozen baseline (§5's "not interpret or modify received bytes", §11's "packet framing and checksum/CRC identification" as a deferred future stage), a deliberate escalation once the frame shape and checksum were confirmed against real hardware capture rather than something assumed upfront. What's still preliminary reverse engineering, not a validated contract, is the *meaning* of the payload bytes — which fields carry speed, incline, state, etc. See "Live-looking fields" above and `REQUIREMENTS.md` for the rest of the frozen baseline intent and future stages.
