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

Direction was confirmed by capture, not assumed in advance — see "Wire direction" under Protocol observations below. The current default firmware's output labels frames by direction (`BASE->CON` / `CON->BASE`) rather than by raw GPIO name; see Output below.

## Firmware builds

This repo can hold more than one firmware at once, each in its own `fw/<name>/` directory as a fully independent ESP-IDF project (own `CMakeLists.txt`, own `main/` component, own `sdkconfig.defaults`, own `version.txt`). They share the one `.tooling/esp-idf` toolchain checkout but never share build output or app code. That's the going-forward convention here: a new firmware feature substantial enough to change *how the sniffer behaves*, not just extend it, becomes a new `fw/<name>/` directory rather than a rewrite of an existing one — so an older, still-useful behavior stays buildable and flashable on its own instead of being lost to history.

| Firmware | Version | What it does |
|---|---|---|
| `fw/frame-sniffer/` (default) | 1.0.0 | Reassembles both directions into complete `68/LEN/payload/CS/43` frames; prints one `OK`/`MANGLED` line per frame, labeled by direction (`BASE->CON`/`CON->BASE`), over USB serial. See Output below. |
| `fw/byte-sniffer/` | 1.0.0 | The earlier one-line-per-byte firmware this project started from, extracted here so it stays available — useful when you want the rawest possible view (e.g. debugging the framing/checksum logic itself, or a protocol variant the frame parser doesn't recognize). Labels by GPIO (`GPIO26`/`GPIO27`), not direction. |
| `fw/ble-sniffer/` | 1.0.0 | Same frame parser as `frame-sniffer`, but valid frames go out over a BLE GATT notify characteristic instead of USB serial — see "BLE (`fw/ble-sniffer/`)" below. |

Each firmware's version lives in two places kept in sync by hand: `fw/<name>/version.txt` (which ESP-IDF embeds into the compiled binary's app description — check it later with `esptool.py image_info` or over OTA) and an `FW_VERSION` macro in that firmware's `main.c` (which is what actually gets printed in the serial boot banner). Confirmed by building all three and checking the configure-step output: `App "treadmill_frame_sniffer" version: 1.0.0`, `App "treadmill_byte_sniffer" version: 1.0.0`, `App "treadmill_ble_sniffer" version: 1.0.0`, each independent of the others and of the repo's own git state. Bump a firmware's version in both places together when its behavior changes.

`./tools/idf.sh` defaults to `frame-sniffer`; pass `-f <name>` (or `--fw <name>`) before the `idf.py` arguments to target a different one, e.g. `./tools/idf.sh -f byte-sniffer build` or `./tools/idf.sh -f ble-sniffer build`.

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
./tools/idf.sh build                    # builds fw/frame-sniffer (default)
./tools/idf.sh -f byte-sniffer build     # builds fw/byte-sniffer instead
```

## Flash and monitor

Find the USB serial port, commonly `/dev/ttyUSB0`, then:

```bash
./tools/idf.sh -p /dev/ttyUSB0 flash monitor
./tools/idf.sh -f byte-sniffer -p /dev/ttyUSB0 flash monitor
./tools/idf.sh -f ble-sniffer -p /dev/ttyUSB0 flash monitor
```

Exit the ESP-IDF monitor with `Ctrl+]`.

## BLE (`fw/ble-sniffer/`)

`fw/frame-sniffer/` and `fw/byte-sniffer/` both stream continuously over USB serial, which means a wired USB connection to a laptop for the whole capture. During a real play/ramp-down test (`log3-play-stop.txt`) the serial monitor dropped out repeatedly for several seconds right as the motor engaged and drew current — `fw/ble-sniffer/` exists to get the sniffer off that USB link entirely: it reuses the exact same frame parser as `fw/frame-sniffer/` (same `68/LEN/payload/CS/43` framing and checksum validation, byte-for-byte identical logic), but valid frames go out over Bluetooth Low Energy instead of a `printf` line.

### Architecture

The ESP32 acts as a **BLE peripheral / GATT server** (NimBLE host, no classic Bluetooth, no Bluedroid), built directly on ESP-IDF's own `bleprph`/`blehr` NimBLE examples rather than written from scratch — `ble_gatts_notify_custom()` is called straight from each `sniffer_task` the moment a frame validates, no polling timer involved:

```text
ESP32 (peripheral)
  |
  +-- GATT service: Treadmill Sniffer   (128-bit UUID, private -- not a registered SIG profile)
        |
        +-- characteristic RX_LOG   NOTIFY   one BLE notification per valid treadmill frame
        |
        +-- characteristic CMD      WRITE    accepted and logged, not acted on yet
```

`CMD` exists so the GATT structure is already in place for active control once the protocol is understood (`REQUIREMENTS.md`'s deferred future stages) — writes to it are logged over USB serial and otherwise ignored; nothing is ever transmitted onto either treadmill UART line from this firmware, same guarantee as the other two.

UUIDs (generated once, fixed from here on so a client app doesn't need reconfiguring after a rebuild):

| | Standard UUID string | `BLE_UUID128_INIT` bytes (NimBLE's own byte order — reverse of the string) |
|---|---|---|
| Service | `3AAC01E9-30A5-4B5F-9FDE-DC1CC874E6D6` | `d6,e6,74,c8,1c,dc,de,9f,5f,4b,a5,30,e9,01,ac,3a` |
| RX_LOG | `F02DC604-61E1-4A7C-9413-3F4C5D97C47F` | `7f,c4,97,5d,4c,3f,13,94,7c,4a,e1,61,04,c6,2d,f0` |
| CMD | `842DEBE9-3D97-49EC-BCD1-590733A4A4D2` | `d2,a4,a4,33,07,59,d1,bc,ec,49,97,3d,e9,eb,2d,84` |

The device advertises as **`TreadmillSniffer`** (flags + name only — a 128-bit UUID wouldn't leave room for a readable name in a 31-byte legacy advertisement; the service is still fully visible via normal GATT discovery once connected).

### RX_LOG notification format

One notification per valid frame, exactly as the original request specified:

```text
byte 0    direction   (0x01 = BASE->CON, 0x02 = CON->BASE)
byte 1    length      (total raw frame length)
byte 2..  the raw frame bytes, unmodified
```

Example — the idle `BASE->CON` heartbeat frame:

```text
01 0E 68 0C A0 00 00 00 00 9E 00 00 00 00 4A 43
```

Currently observed frames (9–14 bytes) fit comfortably inside the default BLE ATT MTU (23 bytes total, 20 usable) without any MTU negotiation. If a future, larger frame variant shows up, a notification carrying it would be truncated to whatever MTU is in effect — not handled yet, since nothing bigger has been observed so far.

Only complete, checksum-valid (`OK`) frames go over BLE. `MANGLED` and `ERROR` (framing/parity/overflow) reporting is unchanged from `frame-sniffer` but stays on USB serial only — those are rare (a handful of lines across a multi-minute capture in `log1.txt`/`log2.txt`), so keeping them on the wired console doesn't reintroduce the continuous-traffic problem BLE is solving, and it means a bench debugging session over USB still sees them. USB serial in this firmware carries only the boot banner, BLE connect/disconnect/subscribe lifecycle lines, and those `MANGLED`/`ERROR` lines — never the continuous per-frame stream.

Like any BLE notification, delivery is best-effort: a frame that arrives with no client connected, or not subscribed, is simply not delivered (not queued, not retried).

### Testing it

Any BLE central can connect, discover the `Treadmill Sniffer` service, and subscribe to `RX_LOG`'s notifications (write `0x01 0x00` to its CCCD, or use your app's "enable notifications" toggle):

- **nRF Connect** (Android/iOS) or **LightBlue** (iOS/macOS) — connect to `TreadmillSniffer`, open the service, tap the notify icon on `RX_LOG`.
- `tools/ble_monitor.py` — a small [`bleak`](https://github.com/hbldh/bleak)-based Python client included in this repo. Scans for the device by name (`TreadmillSniffer`) rather than a hardcoded address — necessary on macOS, which hides real BLE hardware addresses from apps — and decodes each `RX_LOG` record into `<direction> <hex frame bytes>`. `pip install bleak && python3 tools/ble_monitor.py`.

**Validated on real hardware**: a full play → run → ramp-down → idle test captured with `tools/ble_monitor.py` (`log4-ble-play-stop.txt`, 214 frames) came back with **zero disconnects, zero `MANGLED`, zero `ERROR`** for the entire run — every frame checksum-valid. `log3-play-stop.txt`, the equivalent test over USB serial, lost most of that same window to repeated serial disconnects right as the motor engaged; this run had none of that. See "Protocol observations" below for what the clean, gap-free capture revealed about the speed ramp itself.

## Output

The firmware reassembles each direction's byte stream into complete `68 LEN ... CS 43` frames (frame shape and checksum are covered under "Protocol observations" below) and emits one line per complete, checksum-valid frame:

```text
000001234567 BASE->CON OK 68 0C A0 00 00 00 00 9F 00 00 00 00 4B 43
000001234787 CON->BASE OK 68 08 20 00 00 00 00 14 3C 43
```

Direction is printed directly — `BASE->CON` (baseboard -> console/HC32L130, tapped on GPIO27) and `CON->BASE` (console/HC32L130 -> baseboard, tapped on GPIO26) — rather than the raw `GPIO26`/`GPIO27` tag `fw/byte-sniffer/` still uses (see "Firmware builds" above); see "Wire direction" below for how the mapping was confirmed.

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

It has also now been confirmed on a real 118-second run of the actual firmware (`log2.txt`, `frame-sniffer v1.0.0`): 1195 `OK` frames (658 `CON->BASE` + 537 `BASE->CON`), every one independently re-checked byte-for-byte against the checksum formula with zero mismatches. Only 2 `ERROR BREAK` lines and 2 `MANGLED stray_bytes` lines appear in the whole capture, all four clustered in the first ~5 seconds while the parser was still syncing to the two lines at boot; the remaining ~113 seconds are 100% clean.

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

### Second session confirms the pattern, and narrows the "live" byte theory (`log2.txt`)

A later, much longer 118-second capture with the current `frame-sniffer` firmware (1195 total frames: 658 `CON->BASE`, 537 `BASE->CON`) reconfirms everything above at higher statistical confidence, and adds one new data point:

- The 220 ms `BASE->CON` tick holds essentially exactly: 527 of 536 gaps are 220 ms on the nose, the rest 213/227 ms.
- `CON->BASE`'s frame (`68 08 20 00 00 00 00 14 3C 43`) is again **100% invariant** across all 658 occurrences — reinforcing it as a fixed command/ack, not a carrier of live state.
- `BASE->CON`'s baseline payload value shifted session-to-session: `log1.txt` had `9F` as the dominant value; this session's dominant value is **`9E`** (529 of 537 frames), with `9F` reappearing 5 times and a `9E...01...` variant (analogous to `log1.txt`'s rare `9F...01...` frame) appearing 3 times. Between the two sessions, that byte (frame offset 7) has now taken three *consecutive* values — `0x9E` (158), `0x9F` (159), `0xA0` (160) — which is a stronger hint than a single session gave that this is a drifting counter or live analog/sensor reading rather than a fixed constant; its idle baseline isn't stable across power-on sessions. Not yet a validated field, still just the best current lead (see "Live-looking fields" below).

The only imperfect lines in the whole 118-second capture — 2 `ERROR BREAK` and 2 `MANGLED stray_bytes` — are all within the first ~5 seconds while the parser was still syncing to the lines at boot; see Output above for the full validation result.

### Timestamps compress during catch-up bursts (log2.txt evidence)

`log2.txt` also gives a sharper, more frequent example of the caveat already noted under Output ("software receive time, not wire-arrival time"): dozens of times through the capture — roughly every ~1.3 s, in pairs — two **complete, distinct** `CON->BASE` frames show only **~0.37–0.42 ms** between their timestamps. That's physically impossible on the wire: a 10-byte frame takes ~91.7 ms to transmit at 1200 baud 8N2, so two full frames cannot really start 0.4 ms apart. This is the sniffer task falling behind (e.g. while busy printing the previous line) and then, once scheduled again, draining several frames' worth of already-buffered bytes in a tight loop — each `uart_read_bytes()` call returns near-instantly because the data is already sitting in the ring buffer, so the whole run of already-arrived frames gets timestamped within a fraction of a millisecond of each other, even though their true wire arrival was spread over a much longer real span. Treat any run of unusually tight-packed frame timestamps as this artifact, not as evidence the console actually transmits that fast.

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

Across the `log1.txt` samples above, only two payload positions changed:

- payload byte 5 (frame offset 7, right after the four `00`s): `9F` / `A0` / `9F`
- payload byte 7 (frame offset 9): `00` / `00` / `01`

Everything else (`A0 00 00 00 00 ... 00 00`) held constant. `log2.txt`'s much longer session (see above) varies the same two positions independently of each other — offset 7's baseline moved to `9E` this session while offset 9's rare `01` variant recurred regardless — and offset 7 has now taken three consecutive values (`0x9E`, `0x9F`, `0xA0`) across the two sessions. These two positions remain the best current lead on which fields carry live console/baseboard state, but this is still an observation from two capture sessions, not a validated field map — treat as a starting point for further capture and correlation (ideally the single-action-at-a-time captures suggested in `NOTES.md`), not as ground truth.

### A linear speed ramp during play -> stop (`log3-play-stop.txt`, `log4-ble-play-stop.txt`)

Two captures of the same real action — press play (startup speed 0.8 km/h), let it run a few seconds, press stop (which ramps down in 0.1 km/h steps) — give the clearest field semantics found so far. `log3-play-stop.txt` (over USB serial) caught only 5 non-idle frames because the serial connection kept dropping out as the motor drew current; `log4-ble-play-stop.txt` (over BLE, see "BLE" above) caught the entire cycle cleanly — 214 frames, zero drops, zero mangled — and both agree with each other everywhere they overlap.

**`CON->BASE` bytes 4:5, read as one big-endian 16-bit value, trace a near-perfect linear ramp:**

```
0 -> 250 -> 310 -> 387 -> 465 -> 542 -> 620   [held steady for ~33 frames]   -> 542 -> 465 -> 387 -> 310 -> 232 -> 155 -> 77 -> 0
```

Step size is almost exactly constant both up and down — **~77–78** per step, an 8-step ramp from 0 to 620 and back. That's a ramp generator, not noise.

**`BASE->CON` bytes 3:4 (same big-endian-pair position, one direction over) mirror the identical shape**, plateauing in the same 618–623 range while `CON->BASE`'s sits at 620 — both sides reporting essentially the same underlying setpoint.

**`BASE->CON` byte 8** (the field flagged as the best speed candidate from `log3`'s 3 sparse samples, `0x13/0x15/0x0F` = 19/21/15) now has a full curve behind it in `log4`: `0 -> 10 -> 13 -> 15 -> 18 -> 21 -> 22` rising in lockstep with the ramp above, holding at **21–23** through the plateau, then back down to `0` — roughly proportional to the fine ramp value (ratio ≈ 28), i.e. plausibly a coarser/rounded report of the same quantity. The exact km/h-per-unit conversion isn't pinned down yet — that needs a second capture at a different, precisely-known target speed to compare ratios against this one — but the shape is now unambiguous.

Other bytes during the same window:

- The status byte at frame offset 2 (`A0`/`A1` on `BASE->CON`, `0x20`/`0x21` on `CON->BASE`) toggles during the run/plateau but isn't cleanly pinned to a specific ramp edge yet — clearly "active vs. idle" rather than noise, not yet a validated flag meaning.
- `BASE->CON` byte 9 fluctuates in a small 0–5 range roughly tracking the ramp phase, but far noisier than the bytes-3:4 ramp — a weaker lead than the ramp field, not yet explained.
- The `9E`/`9D` "live" byte (frame offset 7, see "Live-looking fields" above) drifts on its own schedule throughout `log4` too, independent of the run/stop cycle — reconfirms it's unrelated background drift, now across three separate sessions.

As with everything in this section: real observations from real captures, not yet a validated field map. The next useful capture would be a second, different target speed to calibrate the ramp's units.

Frame reassembly and checksum validation are now implemented in firmware, as described under Output above — this goes beyond `REQUIREMENTS.md`'s originally frozen baseline (§5's "not interpret or modify received bytes", §11's "packet framing and checksum/CRC identification" as a deferred future stage), a deliberate escalation once the frame shape and checksum were confirmed against real hardware capture rather than something assumed upfront. What's still preliminary reverse engineering, not a validated contract, is the *meaning* of the payload bytes — which fields carry speed, incline, state, etc. See "Live-looking fields" above and `REQUIREMENTS.md` for the rest of the frozen baseline intent and future stages.
