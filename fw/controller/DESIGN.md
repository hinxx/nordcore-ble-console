# Controller firmware — design notes

Not yet implemented. This captures the plan for `fw/controller/` before any code exists,
so the hardware work and the firmware work can proceed from the same understanding. It
follows on directly from the passive-sniffing work in `fw/frame-sniffer/` and
`fw/ble-sniffer/` — see the root `README.md`'s "Protocol observations" for the full
evidence behind every protocol fact restated here.

## Goal

Command the baseboard directly from the ESP32 — play/stop/set-speed — replacing the
stock console's *control* role, while still reading the baseboard's own telemetry
(speed, steps) the same way the passive sniffers already do. A BLE/PC "console" then
shows speed, distance, and steps, and issues commands. Distance is not on the wire (see
"What we know", below) — the new console computes it itself from speed over time.

This is a deliberate escalation past every previous firmware in this repo: it's the
first one that transmits onto a treadmill UART line at all. `REQUIREMENTS.md` §5's "not
transmit on UART1 or UART2" and §11's "active control... only after protocol validation
and with explicit safeguards separating passive sniff mode from transmit/control mode"
describe exactly this moment. Protocol validation is the whole point of `log1`
through `log11`; the safeguards are the staged rollout and physical fallback below.

## Hardware plan (per the user's clarification)

- **`BASE->CON` (baseboard's own output) needs no change.** It's already a passive,
  purely-listen tap (10k/15k divider into a GPIO, per the root README's "Electrical
  wiring") — the ESP32 can keep listening on it in parallel with anything else on that
  line indefinitely, no contention, whether or not the stock console is present. No
  jumper needed here.
- **`CON->BASE` (commands *to* the baseboard) is the one that needs a physical
  break-and-jumper.** A UART TX pin is a single push-pull driver; the stock console and
  the ESP32 cannot both be connected to that line at the same time without contention or
  driver damage. Plan: break the TX/RX line at the original console, and wire in a
  jumper so exactly one of {stock console, ESP32} is connected to the baseboard's RX at
  any time — the other left fully open. This is the explicit fallback: if the ESP32
  side misbehaves, move the jumper back and the stock console works again immediately,
  no reflashing or rewiring beyond the jumper.
- **Power**: the stock console does its own 12V → 5V regulation today, and the ESP32
  needs 5V too. Plan is to tap the console board's existing 5V rail rather than build a
  separate supply right now. Carry over the existing caution from the root README's
  "Electrical wiring" section: verify this 5V tap and USB power aren't both driving the
  ESP32 at once without confirmed power-path isolation, same as during bench sniffing.
- **TX signal level — decided: discrete single-transistor inverting shifter, not an
  IC.** RX only ever needed a step-down divider (5V → 3.3V). TX is the reverse: the
  ESP32's 3.3V output driving into whatever the baseboard's RX pin actually requires.
  Chosen circuit (a **BC546**, NPN, on hand — interchangeable with the whole
  BC546/547/548/549/550 family for this purpose; none of their voltage ratings are
  remotely stressed switching 5V):

  ```
  ESP32 TX ---[1k-4.7k]--- base (BC546)
  GND -------------------- emitter
  5V ---[4.7k-10k]------- collector ---> to baseboard RX
  ```

  A saturated common-emitter transistor pulls its collector to within ~0.1–0.2V of
  GND, and the pull-up resistor takes it to a true, full 5V when off — near rail-to-
  rail, and simpler than any of the IC or multi-transistor alternatives considered
  first (a `74HCT125` IC, and a 3-transistor complementary push-pull using BC546 +
  BC556 + a possible D1616/2SD1616 were both considered and set aside — the push-pull
  in particular would have given *worse* logic levels here, losing ~0.6V on each rail
  to emitter-follower Vbe drops, for more parts). **This circuit inverts the signal** —
  firmware must call `uart_set_line_inverse(uart_num, UART_SIGNAL_TXD_INV)` on the TX
  UART to correct it. Not yet bench-verified against the baseboard's actual RX input
  characteristics.

### Line-by-line UART plan

Keeping both existing RX taps, not replacing either — the new TX capability is added
alongside them, sharing a UART peripheral rather than needing a fourth:

- **`UART2` — `BASE->CON`, unchanged.** Stays RX-only, exactly as in `fw/frame-sniffer/`
  and `fw/ble-sniffer/` today. This is the baseboard's own output; nothing should ever
  drive TX onto it — that would fight the baseboard's own transmitter.
- **`UART1` — `CON->BASE`, gains a TX pin alongside its existing RX pin.** The RX tap
  already on GPIO26 is kept, not dropped, because it's useful in both jumper positions:
  - Jumper on **ESP32**: UART1's own RX reads back exactly what UART1's TX actually put
    on the wire — a real hardware confirmation a transmitted frame landed correctly, not
    just "the code believes it sent it."
  - Jumper on **stock console**: UART1's RX keeps working exactly like the passive
    sniffer does today, watching the real console's traffic — useful through bring-up,
    and for later A/B-comparing the controller's own output against a real session.

  ESP-IDF assigns a UART's RX and TX pins independently via the GPIO matrix, so this is
  configuration on the existing UART1 peripheral (a new TX-capable GPIO, through the
  level shifter above), not a new hardware UART instance.

## What we know (from sniffing) that the controller needs

Condensed from the root README; see there for the full evidence and caveats.

**Frame shape** (both directions): `68 LEN [payload] CS 43`. `LEN` counts every byte
after itself (payload + `CS` + end byte). `CS` is an 8-bit sum of `LEN` + payload, mod
256 — no CRC, no carry-fold.

**`CON->BASE` frame** (10 bytes, `LEN` = `0x08`) — what the controller transmits:

```
68 08 <state> <flag> <speed_hi> <speed_lo> 00 14 <CS> 43
```

- Idle: `68 08 20 00 00 00 00 14 3C 43`
- Commanding a speed: `state` toggles `0x20`/`0x21` (not fully understood — mimic the
  pattern observed in captures rather than inventing a new one: `0x21` shows up during
  active running, `0x20` at idle and around some transitions), `flag` is `0x50` whenever
  any non-idle speed is commanded and `0x00` at true idle, `speed_hi:speed_lo` is the
  big-endian raw speed value, offset 6 stays `0x00` (no step-related field on this
  side — steps only ever appeared in `BASE->CON`), offset 7 is always the fixed `0x14`.

**Speed value**: not a closed-form formula — README's `log8` 53-point calibration
table (0.8–6.0 km/h) is the ground truth to use for known speeds; `~750 x speed_km/h`
(or the affine `736 x speed_km/h + 58`) is a fine approximation elsewhere, accurate to
about ±0.04 km/h except right at the top of the range. Two hard limits confirmed by
real hardware, not just inferred: a **minimum of 0.8 km/h** (raw `620`) and a **max cap
around raw `4444`** (~5.66–5.73 km/h depending which fit you use, ~0.27–0.34 km/h short
of the 6.0 km/h nameplate value — the cap's exact relationship to the nameplate number
is still unresolved). The controller should clamp to this empirically-observed range,
not extrapolate past it.

**Ramp behavior**: every real speed change observed on the wire was a smooth ramp in
~74–78-unit steps (matching a single button tap, a held button, or nothing else) never
a direct jump to target. Whether the baseboard would even accept a direct jump is
**untested** — see "Start by mimicking", below.

**Timing**: the idle heartbeat is bursts of ~6–7 identical frames roughly every
~1.07–1.25 s (timestamped evidence from the USB-serial captures, `log1`/`log2`). The
exact real-time cadence of ramp-step updates during an active ramp is *not* tightly
pinned down — the BLE captures that generated most of the ramp evidence carry no
timestamps (see README's BLE section), so only the step *sizes* are well-established,
not their timing. Mimicking the observed cadence as closely as practical is the safe
default for the first pass.

**`BASE->CON` telemetry** (what the controller reads back, already fully described in
README's byte tables): bytes 3:4 (mirrored/noisy version of the commanded speed,
usable to confirm the baseboard is actually tracking what was commanded) and offset 6
(the step counter — increments 1:1 per physical step, resets at a real stop, carries no
history of its own; any persistence across sessions is something the *new console* has
to do itself, same as the stock console apparently does). Offsets 5, 10, 11 remain
completely unexplained (always zero in every capture so far, including walking tests) —
leave them `0x00` when transmitting, and don't assume they're safe to ignore forever if
this ever gets extended to something like incline.

## Staged rollout

Agreed approach: **start by mimicking the original console as closely as possible**;
only diverge (different timing, direct setpoint jumps, etc.) later and deliberately,
once the faithful-replication baseline is proven solid.

1. **Hardware bring-up.** Install the break-and-jumper on `CON->BASE` only. Confirm the
   jumper genuinely isolates one driver at a time (no floating line, no contention) with
   a meter before connecting anything live. Resolve the TX level-shifting question.
   Confirm the 5V power tap is clean and sufficient before relying on it.
2. **Heartbeat only, belt unloaded, nobody on it.** Jumper set to ESP32. Transmit
   *only* the idle frame at the observed burst/gap cadence — no speed commands yet.
   Confirm `BASE->CON` keeps reporting normal idle telemetry (matches the idle baseline
   already documented) and nothing faults. This is the first time anything ESP32-
   originated has ever reached the baseboard — treat it as the highest-risk single step
   in the whole plan even though it's "just" the idle frame.
3. **Play and ramp to 0.8 km/h, belt unloaded.** Replicate the exact ramp shape and
   cadence already observed. Watch `BASE->CON` for the expected response (state flag,
   speed pair climbing to `620`, matching known-good patterns) and watch the belt
   physically.
4. **Full range and stop, belt unloaded.** Ramp up toward the confirmed max, back down
   through the confirmed 0.8 km/h floor, stop — still mimicking observed behavior, still
   unloaded.
5. Only after 1–4 are solid: walking tests, then whatever divergence from stock
   behavior is actually wanted (faster ramps, different min/max, BLE command surface
   driving it in real time, etc.).

Keep the jumper physically reachable at every stage — the point of the fallback is that
reverting to the stock console is a physical action taking seconds, not a firmware
question.

## BLE console architecture (sketch, not yet implemented)

Reuse rather than reinvent: `fw/ble-sniffer/` already has a `CMD` write characteristic
that today only logs what's written to it (see its `ble_gatt.c` and the root README's
BLE section). The natural evolution is to give it real meaning — a small app-level
command set (`PLAY`, `STOP`, `SET_SPEED(tenths_km/h)`) rather than making a BLE client
speak the raw wire protocol — with the controller firmware owning ramping, checksums,
and timing internally. `RX_LOG`-style notifications carry decoded telemetry (speed,
steps) rather than raw frames. The PC/BLE console computes distance client-side from
speed over time, since the baseboard never reports it.

## Open questions / not yet resolved

- TX level: single-transistor BC546 inverting shifter chosen (see Hardware plan above)
  but not yet bench-verified against this specific baseboard's actual RX input
  characteristics.
- Exact real-time cadence of ramp-step updates during an active ramp (only step *sizes*
  are well-established; timing is inferred, not timestamped).
- The `0x20`/`0x21` (`CON->BASE`) and `A0`/`A1` (`BASE->CON`) state-byte trigger
  semantics — still "Partial" in README's byte tables, not fully understood.
- Whether a direct setpoint jump (skipping the ramp) is accepted, rejected, or unsafe —
  deliberately untested; not part of the initial mimic-the-console phase.
- The exact relationship between the observed `4444` cap and the 6.0 km/h nameplate
  figure (see README's `log7`/`log8` sections) — affects how close to the nameplate max
  it's safe to command.
- Whether anything changes about `BASE->CON`'s behavior when commands originate from the
  ESP32 instead of the stock console — assumed not, since the baseboard shouldn't be
  able to tell the difference, but not something sniffing could ever test.
