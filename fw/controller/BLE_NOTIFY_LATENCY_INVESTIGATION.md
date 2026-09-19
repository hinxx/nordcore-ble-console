# BLE telemetry delivery latency — bursty updates while the motor is engaged

**UNRESOLVED — narrowed to a hardware/electrical hypothesis, software side
exhausted.** `tools/treadmill_app.py`'s live Speed/Steps display updates
smoothly (~200-225ms, matching the baseboard's own heartbeat) whenever the
belt is idle, but while the motor is actively engaged (ramping *or* holding
a nonzero commanded speed) it visibly jumps roughly once every 1.8-1.9
seconds instead. The moment the belt returns to true idle, delivery snaps
back to smooth. This has been reproduced on every single test run, with no
exceptions, across many hours of investigation.

Every software-side candidate has been individually tested and ruled out.
The firmware itself has been proven, via a purpose-built diagnostic, to
attempt a BLE notification every ~200ms without fail, with the host-side
NimBLE API reporting success (`rc=0`) every single time, all the way
through the bursty periods. That leaves the delay confined to somewhere
below `ble_gatts_notify_custom()`'s return -- inside the ESP32's own BLE
controller/radio scheduling, a layer no application source code touches.

**Leading hypothesis, not yet confirmed:** electrical, not logical. The
only thing that's physically different between "idle" and "engaged" is
that the baseboard is now driving real motor current. The user
independently recalled seeing the *exact same symptom* on this same board
over a **wired USB serial** connection: as soon as the motor engaged, the
PC would report the USB device disappearing and reappearing, with kernel
log messages suggesting EMI. Two completely different transports (BLE
radio, USB serial) both degrading in the same way at the same trigger
(motor engaging) is a strong signal this is EMI/ground-disturbance from
the motor's own current draw affecting the controller board generally, not
something specific to Bluetooth. Next step is hardware-level: decoupling
capacitors, a cleaner/separate power supply, physically separating the
Bluetooth dongle (or, if relevant, the ESP32 itself) from the baseboard --
none of which this document attempts, since it's outside what's
diagnosable from software.

**Debugging instrumentation from this investigation is left in place on
purpose** (both `tools/treadmill_app.py` and
`fw/controller/main/ble_gatt.c`) in case this gets picked up again later --
see "What's left in the code" at the end.

---

## The symptom

Connect `tools/treadmill_app.py` to the board and watch the Control tab's
Speed/Steps line:

- **Idle** (belt not commanded to move): updates smoothly, about every
  200-225ms, matching `fw/controller`'s documented `BASE->CON` heartbeat
  cadence.
- **Engaged** (PLAY sent, ramping up/down, or holding a nonzero speed):
  updates visibly jump, roughly once every 1.8-1.9 seconds, with several
  steps' worth of change appearing all at once.
- The transition is immediate and reliable in both directions: it starts
  the instant PLAY is sent and stops the instant the belt actually reaches
  a real idle (speed reported as 0), reproduced identically across many
  separate test runs.

## Ruled out, in the order they were tested

Each of these was eliminated by a specific, repeatable experiment, not by
inference alone.

### 1. The database write (`HistoryDB.add_sample`)

Added `TREADMILL_DEBUG_SKIP_DB=1` (still present, see below) to skip the
SQLite commit entirely while keeping everything else identical. The exact
same bursty pattern persisted, drained in the exact same batch sizes. Ruled
out.

### 2. Tkinter `_poll_events` scheduling / Python's GIL

Instrumented `_poll_events` to log its own actual call-to-call gap.
Overwhelmingly it fired close to its intended 100ms cadence -- the
occasional backlog it drained (4-8 queued events at once) was *already
sitting in the queue* when it ran, not caused by its own tardiness. Tried
`sys.setswitchinterval(0.001)` (forcing much more frequent GIL handoff, a
standard mitigation for a background asyncio thread being starved by a
busy Tkinter main thread) -- made no measurable difference, same magnitude
gaps before and after. This specific mitigation attempt was reverted since
it didn't help; the observational instrumentation was kept.

### 3. The BLE background thread's own asyncio loop

Instrumented `BLEWorker._on_telemetry` (the `bleak` notification callback
itself) to log its own invocation gap, timestamped at the very top of the
function, before any of the app's own code runs. This showed the *callback
itself* was only being invoked once every ~1.8-1.9s during bursts -- not a
case of prompt delivery with slow processing afterward. This ruled out
everything downstream of the callback (GUI updates, DB writes, the app's
own `queue.Queue`) as the primary cause, and pointed at something upstream
of our own code.

### 4. `blueman-manager`'s background polling

`btmon` (kernel-level HCI capture, run manually by the user with `sudo`
since this session has no root) revealed `blueman-manager` continuously
opening and closing a raw HCI socket roughly once per second, the entire
time it runs -- traced to its own `_monitor_power_levels()` background
task, which logs `Failed to get power levels, probably a LE device` and
retries on a timer regardless. Investigated as a candidate source of
interference on the shared adapter.

Killing the live process once and retesting still showed bursting, which
looked like a clean ruleout -- but `blueman-manager` turned out to
auto-respawn via D-Bus service activation (`org.blueman.Manager.service`),
and a later capture caught a *new* PID doing the same once-per-second
polling. To close this properly, the systemd user service was masked
(`systemctl --user mask blueman-manager.service`, confirmed to stay dead
this time) and the full idle -> Play -> walk -> Stop test was rerun with it
genuinely absent for the entire run. Identical bursty pattern. **Ruled out
for real this time.**

### 5. BLE connection interval / BlueZ negotiation

A `btmon` capture run simultaneously with the app's own timestamped log
showed an `LE Connection Update Complete` event confirming a fast 37.5ms
connection interval was already active (matching `fw/controller/main/
ble_gatt.c`'s own `ble_gap_update_params()` request on connect) well before
and throughout several of the observed ~1.8-1.9s stalls. With a 37.5ms
interval, there are roughly 50 connection events available during each
stall window -- more than enough to drain any reasonable backlog quickly if
connection interval itself were the bottleneck. Ruled out.

### 6. USB autosuspend on the Bluetooth adapter

The adapter (`/sys/bus/usb/devices/1-2.1`, a Realtek USB dongle) has
`power/control=auto` and `autosuspend_delay_ms=2000` -- suspiciously close
to the observed gap size, and a well-documented class of Linux USB
Bluetooth quirk. Ruled out by the clean idle/Play/Stop correlation test:
autosuspend is an *idle*-triggered mechanism, but the bursting is
*activity*-triggered -- it starts when the motor engages (more BLE
traffic, if anything) and stops when it goes idle, the opposite of what an
idle-timeout theory predicts.

### 7. Whether the firmware itself is skipping notify attempts

The decisive test. Added a diagnostic sequence counter to the firmware
(see below): `ble_gatt_notify_telemetry()` now increments a rolling
per-attempt counter and records the previous call's
`ble_gatts_notify_custom()` return code, both piggybacked onto the
TELEMETRY payload (bytes 5 and 6, on top of the existing 5-byte record).
The app checks every incoming notification's sequence number against what
it expected and flags any gap.

Result, across a full idle -> Play -> walk -> Stop test with six separate
~1.8-1.9s stalls: **zero gaps in the sequence, `rc=0` on every single
attempt.** The firmware calls the notify function every ~200ms without
fail, on schedule, straight through every stall, and NimBLE's host-layer
API reports success every time. This conclusively rules out any
application-level bug in `fw/controller` -- the code does the identical
thing whether the motor is idle or engaged, exactly as expected, since (as
raised directly by the user) the BLE stack has no semantic awareness of
motor state at all. It also independently confirmed data freshness: earlier
in the investigation, the `speed_tenths` values *within* a single burst
(e.g. `20, 19, 18, 16, 15, 14, 13`) were checked and found to be small,
correctly-incrementing per-~200ms steps, not the large jumps that would
indicate the baseboard's own heartbeat had actually slowed -- the data is
real and fresh throughout, only its BLE delivery is delayed.

## Where this leaves it

`rc=0` from `ble_gatts_notify_custom()` only means NimBLE's *host* layer
accepted the notification and queued it for the controller/radio -- not
that it was transmitted yet. Since the firmware side is now proven
blameless down to that exact boundary, the stall is confined to whatever
happens after that point: the ESP32's own BLE controller/link-layer
scheduling, entirely below anything firmware source code controls or can
observe without lower-level tooling (a radio-aware logic analyzer, or
NimBLE/controller-internal instrumentation this project doesn't have).

Given the software is proven byte-for-byte identical in both states, and
the one genuine physical difference is the baseboard actively driving motor
current, plus the independent USB-EMI precedent on this same hardware, the
electrical/EMI hypothesis is where this stands. Practical impact in the
meantime: **this is a live-display refresh problem only, not a data-loss
problem** -- `tools/treadmill_app.py`'s logged history (daily/weekly
totals, distance estimates) is unaffected, since the underlying data has
been confirmed fresh and accurate throughout every stall.

## What's left in the code

Left in deliberately, in case this investigation resumes:

- **`tools/treadmill_app.py`**:
  - `TREADMILL_DEBUG_SKIP_DB` environment variable -- set to any truthy
    value to skip the SQLite write in `_poll_events` (isolates DB-write
    cost from delivery timing).
  - `[DBG]` -- logs when `_poll_events` itself runs later than expected,
    and how many queued events it drains in one pass.
  - `[DBG-BLE]` -- logs `BLEWorker._on_telemetry`'s own invocation gap
    whenever it exceeds 150ms, plus the diagnostic sequence number and
    last return code from the firmware payload if present.
  - `[DBG-SEQ]` -- fires only on an actual gap in the firmware's notify
    sequence counter (real loss, not just delay). Never fired in the
    decisive test.
  - These print unconditionally to stdout whenever they trigger -- not
    gated behind a flag, so they'll show up in any console the app is run
    from. Harmless if not watched; pipe to a file to capture them
    (`python3 tools/treadmill_app.py 2>&1 | tee /tmp/app_debug.log`, the
    pattern used throughout this investigation).
- **`fw/controller/main/ble_gatt.c`**: `ble_gatt_notify_telemetry()`
  appends two bytes to the TELEMETRY record (now 7 bytes, up from 5):
  byte 5 is a rolling per-attempt sequence counter, byte 6 is the previous
  call's `ble_gatts_notify_custom()` return code. Both fields are additive
  -- any client only reading the original 5 bytes is unaffected.
