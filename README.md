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

Direction was confirmed by capture, not assumed in advance — see "Wire direction" under Protocol observations below. The firmware's raw output still labels bytes by GPIO rather than by direction; see Output below.

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

The baseline firmware emits one line per byte:

```text
000001234567 GPIO26 68
000001243729 GPIO26 0C
000001252891 GPIO26 A0
000001272201 GPIO27 68
```

The source is named by GPIO, not by direction, in this raw output — that's a deliberate baseline choice so the capture path never assumes semantics. Wiring direction has since been confirmed by capture analysis (GPIO26 = console/HC32L130 -> baseboard, GPIO27 = baseboard -> console/HC32L130; see "Wire direction" under Protocol observations below), but the firmware itself is unchanged and still emits `GPIO26`/`GPIO27`, not direction labels.

Timestamps are software receive time (when the sniffer task pulled the byte out of the UART driver's ring buffer), not oscilloscope-grade wire-arrival time. At 1200 baud 8N2 (~9.17 ms per character) ordinary scheduler jitter is far smaller than the inter-byte spacing, so this is fine for protocol reverse engineering but should not be treated as precise bit-level timing.

### Error events

UART framing/parity errors and RX overflow are reported inline as their own lines rather than being silently dropped, so a later analysis doesn't mistake a lossy capture for a complete one:

```text
000001234567 GPIO26 ERROR FRAME_ERR
000001235012 GPIO27 ERROR FIFO_OVF bytes_lost=unknown
000001235014 GPIO27 ERROR RX_FLUSH
```

`FIFO_OVF` and `BUFFER_FULL` are followed by an `RX_FLUSH` line once the driver's input buffer has been flushed to recover; any bytes lost to the overflow are not recoverable and are not counted.

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

This section documents in-progress reverse engineering, not firmware behavior; the baseline sniffer still emits raw, uninterpreted bytes exactly as described above and does not validate or act on this checksum. See `REQUIREMENTS.md` for the frozen baseline intent and future stages.
