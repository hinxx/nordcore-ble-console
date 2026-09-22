# BLE telemetry delivery latency — bursty updates while the motor is engaged

**UNRESOLVED, but the trigger is now identified: ground-path impedance
between the ESP32 controller and the baseboard, aggravated by motor
current.** Not radiated EMI, not a software bug -- both ruled out by direct
experiment (see below). `tools/treadmill_app.py`'s live Speed/Steps display
updates smoothly (~200-225ms, matching the baseboard's own heartbeat)
whenever the belt is idle, but while the motor is actively engaged (ramping
*or* holding a nonzero commanded speed) it visibly jumps roughly once every
1.8-1.9 seconds instead. The moment the belt returns to true idle, delivery
snaps back to smooth.

Every software-side candidate has been individually tested and ruled out.
The firmware itself has been proven, via a purpose-built diagnostic, to
attempt a BLE notification every ~200ms without fail, with the host-side
NimBLE API reporting success (`rc=0`) every single time, all the way
through the bursty periods. A follow-up hardware test then ruled out
*radiated* EMI specifically (board isolated from the baseboard entirely,
run on external power right next to the running motor -- stayed perfectly
clean). What actually flips the symptom on and off, found by direct
experiment: an **extra ground wire** between the controller board and a
second point (even an unpowered external supply's ground terminal) fixes
it; removing that same wire brings the bursty delivery straight back,
regardless of which supply is actually powering the board. See "The actual
trigger" below for the full experiment and what it implies.

**Firmware-side debugging instrumentation from this investigation is left
in place on purpose** (`fw/controller/main/ble_gatt.c`) in case this gets
picked up again later. The `tools/treadmill_app.py` side has since been
removed -- it caused a real silent bug in normal use. See "What's left in
the code" at the end for both.

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

### 8. Radiated EMI via physical proximity alone

To test the leading hypothesis at the time (EMI from the motor disrupting
the ESP32's radio), added `TREADMILL_DEBUG_SYNTHETIC_HEARTBEAT` (still
present, see below) -- a compile-time flag that makes the board emit a
synthetic TELEMETRY notification every ~200ms entirely on its own,
independent of any real `BASE->CON` UART traffic. This let the board run
fully standalone: external power, **zero wiring to the baseboard at all**
(no UART, no shared ground), physically placed right on top of the running
motor's casing at 3 km/h.

Result, over a continuous ~95-second run: **zero bursts, zero sequence
gaps, max gap 228ms.** Perfectly clean the entire time, right next to the
running motor. This rules out pure radiated EMI/RF interference from
proximity alone -- whatever is disrupting delivery requires some kind of
electrical connection to the baseboard, not just nearness to the motor.

## The actual trigger: ground-path impedance to the baseboard

With radiated EMI ruled out, the natural next test was reconnecting the
UART/ground wiring (restoring normal operation, board driving the motor
for real) while still on external power -- the one remaining variable
being the electrical connection itself, not proximity.

This produced a real, if initially confusing, result. In order:

1. **External 12V supply + baseboard wired (UART + ground)**: bursty
   delivery returned, and separately the board briefly stopped being
   discoverable over BLE entirely (resolved by a power cycle -- see below
   for why this specific combination may have been marginal).
2. **Switched to power drawn from the baseboard itself, same UART/ground
   wiring**: worked cleanly. First read as "don't mix power sources," but
   that framing turned out to be wrong.
3. **The actual variable, found on closer inspection**: the external
   supply's ground wire had been left physically connected the whole time,
   even though the external supply itself was switched off. With that
   extra ground wire in place -- regardless of which supply was actually
   powering the board -- everything worked. Disconnecting *just that one
   wire* (nothing else changed) brought the jerky bursts straight back.

That's a specific, narrower claim than "don't mix power domains": **a
single ground wire back to the baseboard isn't a low-enough-impedance
ground reference for the ESP32 by itself, and a second ground path fixes
it even when that second path goes to something unpowered.** The most
likely explanation is that the baseboard's own ground reference gets noisy
under motor load (a PWM motor driver sharing a ground plane with the UART
circuitry is an ordinary source of this), and the ESP32's local ground --
and with it its RF section -- gets dragged around by that noise when the
single UART ground wire is its only reference. A second ground path either
gives that noise current another route to drain through, or ties the
ESP32's ground to a larger, quieter mass, reducing how much of the
baseboard's noise actually reaches it.

**Not yet done**: confirming this is about ground mass/impedance in
general rather than something specific to that one external supply --
i.e., trying a *different* second ground point (mains/earth ground, a PC
chassis, anything else) and checking it fixes things the same way. If it
does, the practical fix is a better/lower-impedance ground path between
the controller board and the baseboard (a proper star ground, a beefier
ground strap, or a controller PCB revision with a real ground plane)
rather than anything about avoiding external power.

## Where this leaves it

`rc=0` from `ble_gatts_notify_custom()` only means NimBLE's *host* layer
accepted the notification and queued it for the controller/radio -- not
that it was transmitted yet. Since the firmware side is proven blameless
down to that exact boundary, and radiated EMI is now ruled out too, the
stall is best explained as the ESP32's own ground reference (and with it
its RF section) being disturbed via the single UART ground wire whenever
the baseboard's own ground gets noisy under motor load -- consistent with
every observation in this document, including the original USB-disappearing
precedent (also a single-wire ground connection to the baseboard, also
only acting up under motor load). Practical impact in the meantime:
**this is a live-display refresh problem only, not a data-loss problem**
-- `tools/treadmill_app.py`'s logged history (daily/weekly totals,
distance estimates) is unaffected, since the underlying data has been
confirmed fresh and accurate throughout every stall.

## What's left in the code

**Update**: the `tools/treadmill_app.py` instrumentation described below
(`TREADMILL_DEBUG_SKIP_DB`, `[DBG]`/`[DBG-BLE]`/`[DBG-SEQ]`) has since been
**removed**. It caused a real bug in normal use: `TREADMILL_DEBUG_SKIP_DB`
got left set in a terminal session after this investigation, silently
disabling all history logging with no error, for long enough to look like a
genuine regression. Since the investigation this instrumentation was for is
now resolved (root cause found, see "Where this leaves it" above), it had
done its job and the risk of it causing this kind of silent, hard-to-spot
failure outweighed keeping it around. The original description is kept
below for the historical record of what was used to reach the conclusions
in this document.

The firmware-side diagnostic bytes (`fw/controller/main/ble_gatt.c`) are
still in place -- see the second bullet below -- since they're purely
additive to the wire format and don't carry the same "silently changes
behavior" risk.

Originally left in deliberately, in case this investigation resumed:

- **`tools/treadmill_app.py`** (removed as of the update above):
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
  - These printed unconditionally to stdout whenever they triggered -- not
    gated behind a flag, so they showed up in any console the app was run
    from.
- **`fw/controller/main/ble_gatt.c`** (still present):
  - `ble_gatt_notify_telemetry()` appends two bytes to the TELEMETRY record
    (now 7 bytes, up from 5): byte 5 is a rolling per-attempt sequence
    counter, byte 6 is the previous call's `ble_gatts_notify_custom()`
    return code. Both fields are additive -- any client only reading the
    original 5 bytes is unaffected.
  - `TREADMILL_DEBUG_SYNTHETIC_HEARTBEAT` -- a compile-time flag (default
    `0`, normal operation unaffected). Set to `1` and reflash to make the
    board emit a synthetic TELEMETRY notification every ~200ms on its own,
    independent of any real UART input, for standalone proximity/EMI
    testing without the baseboard connected at all.
