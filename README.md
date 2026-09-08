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

**`BASE->CON` byte 8** (the field flagged as the best speed candidate from `log3`'s 3 sparse samples, `0x13/0x15/0x0F` = 19/21/15) now has a full curve behind it in `log4`: `0 -> 10 -> 13 -> 15 -> 18 -> 21 -> 22` rising in lockstep with the ramp above, holding at **21–23** through the plateau, then back down to `0` — roughly proportional to the fine ramp value (ratio ≈ 28), i.e. plausibly a coarser/rounded report of the same quantity. See below (`log5`) for the exact conversion this ramp field turned out to have.

Other bytes during the same window:

- The status byte at frame offset 2 (`A0`/`A1` on `BASE->CON`, `0x20`/`0x21` on `CON->BASE`) toggles during the run/plateau but isn't cleanly pinned to a specific ramp edge yet — clearly "active vs. idle" rather than noise, not yet a validated flag meaning.
- `BASE->CON` byte 9 fluctuates in a small 0–5 range roughly tracking the ramp phase, but far noisier than the bytes-3:4 ramp — a weaker lead than the ramp field, not yet explained.
- The `9E`/`9D` "live" byte (frame offset 7, see "Live-looking fields" above) drifts on its own schedule throughout `log4` too, independent of the run/stop cycle — reconfirms it's unrelated background drift, now across three separate sessions.

### Speed field calibrated exactly (`log5-ble-play-2x-inc-2x-dec-stop.txt`)

A follow-up capture did exactly what the section above called for: idle -> play (0.8 km/h startup) -> **+0.1 km/h twice** -> wait -> **-0.1 km/h twice** -> stop -> idle, three distinct known target speeds instead of one. 329 frames, independently re-verified, zero checksum mismatches. `CON->BASE` bytes 4:5 (the same big-endian-pair ramp field from `log3`/`log4`) settled into clean, sustained plateaus that line up with the button presses exactly in order:

| Action | `CON->BASE` bytes 4:5 | Frames held | value ÷ 775 |
|---|---:|---:|---:|
| play (startup) | `620` (`0x026C`) | 42 | **0.800** |
| +0.1 | `697` (`0x02B9`) | 13 | **0.899** |
| +0.1 | `775` (`0x0307`) | 32 | **1.000** |
| −0.1 | `697` (`0x02B9`) | 18 | **0.899** |
| −0.1 | `620` (`0x026C`) | 13 | **0.800** |
| stop | ramps `620 -> 0` | — | **0.000** |

That is exact (to rounding) at all three calibration points against the actual km/h values pressed on the console:

```
speed_km/h = CON->BASE bytes[4:5] (big-endian uint16, frame offset 5:6) / 775
```

`BASE->CON`'s mirrored field (bytes 3:4) tracks the same plateaus but with small real jitter — averaged over each plateau: `620.0` / `696.4` / `775.2` (min/max spread ±1–3 units) versus `CON->BASE`'s dead-steady exact values. Read together with `CON->BASE`'s side being rock-solid at each plateau, this looks like a **commanded setpoint** (`CON->BASE`, exact) versus a **measured/actual value** (`BASE->CON`, small real fluctuation) — a sensible split for a motor control loop. `BASE->CON` byte 8 correlates with the same plateaus too but stays coarser and less exact (averages `21.98` / `24.00` / `26.73` across the three speeds, roughly 27–28 units per km/h) — a real but weaker, not-yet-pinned-down secondary field.

This is now the first payload field in this project with a validated formula, not just an observed pattern — though only confirmed across the narrow 0.8–1.0 km/h range tested here. **Correction, see `log8` below: `/775` turns out to be only a good approximation in this narrow range, not the true relationship across the treadmill's full speed range.**

### Confirmed across the full speed range, and a lagged "actual speed" byte (`log6-ble-play-hold-inc-max-speed-hold-dec-stop.txt`)

A capture that held increase all the way to this treadmill's top speed, then held decrease back down, instead of single 0.1 km/h taps: 395 frames, zero checksum mismatches. Two things confirmed, one refined:

- **The setpoint/measured split holds across the entire dynamic range**, not just the narrow 0.8–1.0 km/h window `log5` tested — `CON->BASE` stays exact all the way up to a peak of `4389` (`/775` = 5.663 km/h estimate), `BASE->CON`'s mirrored field tracks it within ~1 unit even near that peak.
- **The ramp mechanism is the same whether tapped or held** — step size during this continuous hold is ~77–78 early on, same as `log5`'s single-tap step size, drifting to ~73–75 later in the ramp. (One anomalous ~56-sized step is most likely a dropped BLE notification — best-effort delivery, no retry — not a protocol quirk.)
- **`BASE->CON` byte 8 is *not* a constant multiple of the ramp value**, refining the `log4`/`log5` guess of "roughly proportional, ratio ≈ 27–28": the ratio actually *rises* through a ramp — `9.6 -> 20.8 -> 28 ->` stabilizing around **~31** only once the ramp value has been sustained above ~1000 for a while. That's consistent with byte 8 being a **lagged/filtered actual-speed reading** (the motor physically catching up to the commanded setpoint, so early in acceleration the real speed reads lower than where the ramp already is) rather than a simple scaled copy of the setpoint.

One open question this capture couldn't resolve: holding decrease ran smoothly all the way down to true `0`/idle with no distinct floor plateau before the final "stop" — so whether decrease has a nonzero minimum-speed floor, or just walks straight down to a full stop, was still unclear. See `log7` below.

### Max-speed cap and the 0.8 km/h minimum floor (`log7-ble-play-hold-inc-to-max-speed-wait-hold-dec-to-min-speed-wait-stop.txt`)

Same idea as `log6` but with a deliberate pause held at both the top and bottom of the range, and a known nameplate max speed (**6 km/h**) to check the formula against. 630 frames, zero checksum mismatches.

**Confirmed: 0.8 km/h is a real floor.** Holding decrease produces a clean, sustained plateau at `620` (0.8 km/h — the same value as the startup speed) held for 38–40 frames, matching the deliberate wait — *before* a further, separate ramp down to true `0`. `log6` didn't show this because that capture had no equivalent pause. So decrease cannot go below 0.8 km/h on its own; `stop` is a distinct action that finishes the job.

**The max-speed cap doesn't sit exactly on the `/775` line.** The sustained max plateau is `4444` (`0x115C`), held for 58 frames — clearly the intended "wait at max speed." `4444 / 775 = 5.734 km/h`, about 0.27 km/h (4.4%) short of the actual 6.0 km/h nameplate value — too large to be rounding. The likely reason: every ramp step elsewhere in this capture is a consistent ~74–78 units, but the *final* step into the `4444` plateau (from `4389`) is only **55** — noticeably smaller than every other step. That's evidence `4444` is a firmware-enforced ceiling the ramp gets clamped to, not a value that fell out naturally from the same linear stepping the rest of the ramp uses. **This turned out to be only half the story — see `log8` below: `/775` isn't exact even in the smooth interior of the range, so the gap here is partly the cap and partly `/775` itself already being an approximation by 6.0 km/h.**

`BASE->CON`'s mirrored field averaged `4443.3` against `CON->BASE`'s exact `4444` at the max plateau — the setpoint/measured split holds right up to the cap. `byte 8` averaged `141.06` there, a ratio of `31.50` — matching `log6`'s settled ~31 ratio once speed has been sustained, reconfirming the lagged-actual-speed hypothesis rather than a fixed proportional field.

### Full-range calibration corrects `/775` (`log8-ble-play-step-inc-to-max-speed.txt`)

The definitive calibration capture: 53 individual `+0.1 km/h` taps from 0.8 up to 6.0 km/h, each held for the console's own ~2-second settling time and matched to what the console display actually showed at every step. 2169 frames, zero checksum mismatches, and every one of the 53 expected speeds produced its own clean, sustained `CON->BASE` plateau (19–23 frames each, consistent with the ~2 s dwell).

**Correction: `/775` is not the true relationship — it was only a good approximation in the narrow range it happened to be calibrated in.** The error against the known speed grows steadily rather than staying flat:

| Speed | `CON->BASE` value | `value / 775` | Error |
|---:|---:|---:|---:|
| 0.8 | 620 | 0.800 | 0 |
| 2.0 | 1536 | 2.000 | ~0 |
| 4.0 | 3016 | 3.892 | −0.11 km/h |
| 6.0 | 4444 | 5.734 | **−0.27 km/h** |

That rules out a simple proportional formula. The reason shows up in the step sizes: most `+0.1 km/h` taps step the value up by **74–78 units**, but roughly every 4–6 steps there's a noticeably smaller step, and those smaller steps themselves shrink as speed increases — `73, 71, 69, 67, 65, 63, 61, 59, 56, 55` (that last `55` is the same anomalous final step into the max cap already flagged in `log7`). That's a real, repeating structural pattern, not noise — consistent with this value being a **non-linear transform of speed** (plausibly something tied to motor step *period*, which relates to speed as `1/v` rather than `v`) rather than a straightforwardly linear one. A least-squares line fits much better than `/775` (residuals shrink to within ~0.04 km/h) but still isn't exact, for the same reason.

`BASE->CON` byte 8 behaves better once given time to fully settle (2 s per step here, versus the continuous ramps in `log6`/`log7` where it was still catching up): its ratio to the `CON->BASE` value climbs from ~28 at 0.8 km/h and **stabilizes tightly around 31.3–31.5 for every speed above ~1.5 km/h**. Against speed directly it fits a decent line, `byte8 ≈ 23.0 x speed_km/h + 3.4`.

**Bottom line: there's no clean single formula for this field.** The full 53-point table is now the ground-truth reference for this treadmill's speed range, superseding the `/775` claim above:

| Speed | `CON->BASE` bytes 4:5 | `BASE->CON` byte 8 (avg) | Speed | `CON->BASE` bytes 4:5 | `BASE->CON` byte 8 (avg) |
|---:|---:|---:|---:|---:|---:|
| 0.8 | 620 | 21.9 | 3.5 | 2651 | 84.2 |
| 0.9 | 697 | 23.9 | 3.6 | 2714 | 86.5 |
| 1.0 | 775 | 26.4 | 3.7 | 2790 | 89.7 |
| 1.1 | 852 | 28.7 | 3.8 | 2865 | 90.6 |
| 1.2 | 930 | 31.1 | 3.9 | 2941 | 94.5 |
| 1.3 | 1003 | 33.4 | 4.0 | 3016 | 95.4 |
| 1.4 | 1080 | 35.5 | 4.1 | 3092 | 99.1 |
| 1.5 | 1157 | 38.2 | 4.2 | 3153 | 100.1 |
| 1.6 | 1234 | 40.4 | 4.3 | 3228 | 103.8 |
| 1.7 | 1305 | 42.8 | 4.4 | 3303 | 104.8 |
| 1.8 | 1382 | 44.9 | 4.5 | 3378 | 108.0 |
| 1.9 | 1459 | 47.5 | 4.6 | 3453 | 110.3 |
| 2.0 | 1536 | 49.6 | 4.7 | 3528 | 112.4 |
| 2.1 | 1612 | 52.3 | 4.8 | 3587 | 114.3 |
| 2.2 | 1681 | 54.0 | 4.9 | 3662 | 116.7 |
| 2.3 | 1758 | 56.5 | 5.0 | 3736 | 119.2 |
| 2.4 | 1834 | 59.1 | 5.1 | 3811 | 121.2 |
| 2.5 | 1911 | 61.2 | 5.2 | 3886 | 124.0 |
| 2.6 | 1978 | 64.1 | 5.3 | 3961 | 125.9 |
| 2.7 | 2054 | 65.3 | 5.4 | 4017 | 128.1 |
| 2.8 | 2130 | 67.9 | 5.5 | 4092 | 130.2 |
| 2.9 | 2207 | 70.7 | 5.6 | 4166 | 132.8 |
| 3.0 | 2272 | 72.9 | 5.7 | 4241 | 135.1 |
| 3.1 | 2348 | 75.6 | 5.8 | 4315 | 137.1 |
| 3.2 | 2424 | 76.8 | 5.9 | 4389 | 139.5 |
| 3.3 | 2500 | 79.5 | 6.0 | 4444 (capped) | 141.5 |
| 3.4 | 2575 | 82.2 | | | |

For a quick estimate rather than a table lookup, `CON->BASE`'s value is roughly `750 x speed_km/h` (proportional fit) or `736 x speed_km/h + 58` (affine fit) — both within about ±0.04 km/h through most of the range, worse only right at the very top near the cap.

### The step counter, confirmed (`log9`, `log10`, `log11-ble-play-walk-stop.txt`)

Every capture up to this point had nobody actually walking on the belt — buttons only. Three follow-up captures with real walking confirmed the prediction from the byte-interpretation table below: **`BASE->CON` offset 6 is a step counter**, incrementing by exactly `1` per event while walking and holding at `0` otherwise.

- **`log9`** (walking at 0.8 km/h): offset 6 went `0 -> 1 -> 2 -> ... -> 10`, then reset cleanly to `0` the instant `stop` was pressed. Offsets 5, 10, 11 stayed `0` throughout — confirms it's specifically offset 6, not the whole group. This also explained a stray `0x01` flagged back in `log4`: a single real step registering right as that earlier test began, not noise.
- **`log10`** (a longer walk): offset 6 reached `25` before reset, this time with a brief false-start blip (`0 -> 1 -> 0 -> 1 -> 2 -> ...`) right at motor startup, before the belt was even up to speed — consistent with a genuine physical sensor glitching once during motor start and then settling, which is exactly the kind of noise a real footfall/pulse sensor produces and a purely time- or distance-derived counter would not.
- **`log11`** (deliberate: power-cycled the device first, idled a while, then walked, counting steps by hand): offset 6 started at exactly `0` right after power-on — not resumed from any prior value — then counted `0 -> 1 -> ... -> 30`, matching the walker's own hand-count of 30 steps **exactly, 1:1**. The capture ended mid-walk (no `stop` captured this time), but the clean start at `0` is the important result.

That last point resolves an open question about how this console's on-screen step count relates to the raw baseboard counter, and revises an earlier guess from `log10` alone: with only that log, raw `25` against a *recalled* LCD reading of "~55" looked like it might be roughly a 2x relationship (one sensor pulse per full two-footed stride, perhaps). `log11`'s deliberate 1:1 match (30 raw = 30 actual steps) rules that out — **the raw counter is a direct 1:1 physical step count**, not scaled. `log10`'s gap between `25` (raw) and `~55` (recalled LCD) is much better explained by the console carrying over an **accumulated historical total from earlier sessions** and adding this session's raw delta on top (`25 + ~30` carried-over history lands almost exactly on "~55") — which is exactly the "console remembers previous count after sleep/wake" behavior described alongside `log11`.

That also settles where persistence lives: **the wire protocol itself carries no history.** The raw counter resets to `0` at the start of every session (confirmed fresh after a real power cycle in `log11`, and at every `stop` in `log9`/`log10`) — so whatever accumulation the console displays across sleep/wake cycles is being tracked entirely in the **console's own memory**, not transmitted by or stored on the baseboard. Consistent with the console doing its own math for distance/calories/time too (see "Byte interpretation summary" below) — the baseboard's role stays limited to motor control plus reporting raw physical events (speed, steps) as they happen, with no memory of its own.

Two things this doesn't confirm, both stated as your own hypothesis rather than something verified at the wire level: whether the console actually resets everything at 9999 steps / 99:99 elapsed time, and the exact accumulation arithmetic across sleep/wake. Neither would show up in what the baseboard transmits — confirming them would mean reading the console's own display state directly, not the UART traffic.

### Byte interpretation summary, both directions

Everything above, consolidated. Offsets are 0-indexed from the frame's leading `0x68`. "Known" means a validated field (formula or lookup table); "Partial" means a real, evidenced correlation without a full explanation; "Unknown" means no evidence beyond "this is what's been observed so far."

**`BASE->CON`** (14 bytes when `LEN` = `0x0C`, the only length seen so far):

| Offset | Observed values | Status | What we know |
|---|---|---|---|
| 0 | `0x68` | Known | Start byte, fixed |
| 1 | `0x0C` | Known | Length field: bytes after itself (payload + CS + end) |
| 2 | `A0` / `A1` | Partial | Status/state flag — toggles active vs. idle-ish, but the exact trigger edge isn't pinned down |
| 3:4 | big-endian uint16 | **Known** (lookup table, not a closed-form formula) | Measured/actual speed; mirrors `CON->BASE` bytes 4:5 with small real jitter (±1–3 units). See "Full-range calibration" above for the 53-point speed table |
| 5 | `0x00` always | Unknown | Ruled out as the step counter (that's offset 6 — see "The step counter, confirmed" above) — still always zero across every session including three real walking tests, so still unexplained |
| 6 | increments while walking | **Known** | **Step counter.** Increments by exactly `1` per physical step (confirmed 1:1 against a hand-count of 30 in `log11`), holds at `0` otherwise, resets to `0` at `stop`, and starts fresh at `0` after a power cycle — the wire protocol itself carries no history across sessions. See "The step counter, confirmed" above |
| 7 | `9D` / `9E` / `9F` / `A0` | Partial | Confirmed *unrelated* to run/speed state (drifts on its own schedule across every session) — what it actually represents is still unknown |
| 8 | correlates with speed | Partial (correlated, no exact formula) | Lagged/filtered actual-speed reading — catches up to the setpoint during a ramp; ratio to bytes 3:4 settles to ~31.3–31.5 once held above ~1.5 km/h; roughly `23.0 x speed_km/h + 3.4` |
| 9 | `0x00`–`0x06` | Unknown | Weak, noisy correlation with ramp phase — also the source of the rare idle `...00 01 00 00...` variant seen in `log1` |
| 10 | `0x00` always | Unknown | Ruled out as the step counter — still always zero across every session including three real walking tests, so still unexplained |
| 11 | `0x00` always | Unknown | Ruled out as the step counter — still always zero across every session including three real walking tests, so still unexplained |
| 12 | derived | Known | Checksum — 8-bit sum of bytes 1–11, mod 256 |
| 13 | `0x43` | Known | End byte, fixed |

**`CON->BASE`** (10 bytes when `LEN` = `0x08`, the only length seen so far):

| Offset | Observed values | Status | What we know |
|---|---|---|---|
| 0 | `0x68` | Known | Start byte, fixed |
| 1 | `0x08` | Known | Length field |
| 2 | `0x20` / `0x21` | Partial | Status/state flag, same pattern as `BASE->CON` offset 2 |
| 3 | `0x00` / `0x50` | **Known-ish** | "Motor commanded on" flag — `0x50` whenever any non-idle speed is commanded (from play through the stop ramp), `0x00` at true idle |
| 4:5 | big-endian uint16 | **Known** (lookup table, not a closed-form formula) | Commanded speed setpoint — exact, essentially zero jitter once held. Confirmed 0.8 km/h minimum floor and a hard cap at the 6.0 km/h nameplate max (raw value clamps below where the smooth ramp would otherwise land). See "Full-range calibration" above |
| 6 | `0x00` always | Unknown | Never seen anything but zero |
| 7 | `0x14` always | Known constant | Fixed in every capture regardless of state — a reserved/marker byte, purpose unknown |
| 8 | derived | Known | Checksum — 8-bit sum of bytes 1–7, mod 256 |
| 9 | `0x43` | Known | End byte, fixed |

Every test run so far has only exercised **speed** (play/stop/±0.1 km/h/hold-to-limits) via the console's buttons, with the belt spinning freely and nobody actually walking on it. Nothing has touched incline (if this treadmill has it) or any error/fault condition — the always-zero bytes are prime candidates for fields that simply haven't been triggered yet, not necessarily unused.

The console tracks five quantities: distance, calories, steps, time, and speed. Steps is now a confirmed field (offset 6, see "The step counter, confirmed" above); speed is confirmed (bytes 3:4/4:5, see "Full-range calibration" above). Distance, calories, and time are still unconfirmed at the wire level, but the working hypothesis remains that they're computed from speed x elapsed time on the **console** side rather than transmitted by the baseboard — consistent with the step counter turning out to carry no history either (all persistence/accumulation lives in the console, not the wire protocol). Offsets 5, 10, and 11 remain genuinely unexplained: always zero across every session so far, including three real walking tests, so they weren't the step counter after all — candidates now include incline (if this treadmill has it) or an error/fault state, neither of which any test so far has triggered.

Frame reassembly and checksum validation are now implemented in firmware, as described under Output above — this goes beyond `REQUIREMENTS.md`'s originally frozen baseline (§5's "not interpret or modify received bytes", §11's "packet framing and checksum/CRC identification" as a deferred future stage), a deliberate escalation once the frame shape and checksum were confirmed against real hardware capture rather than something assumed upfront. What's still preliminary reverse engineering, not a validated contract, is the *meaning* of bytes not covered above — see "Byte interpretation summary" just above, and `REQUIREMENTS.md` for the rest of the frozen baseline intent and future stages.
