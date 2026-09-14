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

**Revised architecture: two separate physical rigs, swapped at the baseboard connector
as needed — no jumper, and no stock console in the controller circuit at all.**

- **Sniffer rig (existing, unchanged)**: the passive dual RX-only divider circuit
  already documented in the root README's "Electrical wiring", with its own ESP32,
  flashed with whichever passive firmware is needed
  (`fw/byte-sniffer`/`fw/frame-sniffer`/`fw/ble-sniffer`). Kept around exactly as it is
  today — no changes for the controller work.
- **Controller rig (new)**: a single dedicated PCB built from the baseboard connector
  onward — power supply, RX divider, and TX level shifter all on one board (see below),
  with its own ESP32 running `fw/controller`. This board *is* the console replacement
  whenever it's connected; the stock console isn't part of this circuit at all.
- **Fallback, revised**: no jumper anymore — reverting means physically disconnecting
  whichever rig is currently on the baseboard connector and connecting a different one
  (stock console, sniffer rig, or controller rig) instead. Same "instant revert, no
  reflashing" property the jumper plan was aiming for, just done by swapping a
  connector/board rather than flipping a jumper.
- **`BASE->CON` (baseboard's own output)**: same passive, purely-listen divider tap as
  the sniffer rig already uses (10k/15k, per the root README) — reused on the
  controller PCB, not redesigned. Nothing should ever drive TX onto this line; it's the
  baseboard's own output.
- **`CON->BASE` (commands *to* the baseboard)**: the controller PCB's TX line (level
  shifter below) is the only thing ever connected here while the controller rig is in
  place — no contention to design around, since the stock console and any other driver
  are physically disconnected whenever this board is connected.
- **Power — decided: dedicated `7805` regulator (TO-220, 2A-rated part on hand), not a
  tap off the console's own 5V rail.** 12V → 5V, independent of whatever spare current
  capacity the stock console's own regulator actually has. Standard app-circuit notes:
  ~0.33µF input / ~0.1µF output decoupling caps close to the regulator's pins, per the
  classic 7805 datasheet circuit. Heatsinking: dissipation = (12V − 5V) x current: an
  ESP32 with BLE active typically draws ~80–260mA with bursts higher (WiFi/BT peaks
  toward ~300–500mA) — ~1.4W at 200mA is fine bare in free air, but a ~500mA burst is
  ~3W, which a bare TO-220 will run hot on despite the part being rated for 2A: worth a
  small heatsink or clip-on if those peaks show up regularly. Carry over the existing
  caution from the root README's "Electrical wiring" section regardless of where the 5V
  comes from: verify this supply and USB power aren't both driving the ESP32 at once
  without confirmed power-path isolation, same as during bench sniffing.
- **TX signal level — revised: `74HCT125` buffer IC, replacing the earlier
  single-transistor BC546 shifter.** The BC546 common-emitter approach (a saturated
  transistor pulling the collector near GND, a resistor pulling it up to 5V when off)
  produced electrically valid-looking TTL levels but the baseboard never responded to
  anything sent through it — see `RX_TX_LEVEL_INVESTIGATION.md` for the full
  investigation. The telling clue: the collector's idle-high level barely moved across
  three very different pull-up strengths (10k, 1.2k, and none at all) — consistent
  with the transistor never reaching a clean cutoff, or with a passive pull-up simply
  not being a firm enough "high" assertion, rather than anything a resistor value
  could fix. That's what motivates dropping the passive-pull-up approach entirely
  in favor of an IC that actively drives both rails.

  New circuit: ESP32 TX (GPIO25) into one channel of a `74HCT125` quad buffer,
  straight through to the baseboard's RX pin. No shifting needed on the input side —
  HCT-family inputs are TTL-threshold-compatible, so they read a 3.3V CMOS input
  correctly despite the IC itself running from 5V. The output side (from the 5V rail)
  actively drives a true 5V high and a true 0V low directly — no pull-up, no
  transistor, no base resistor network anywhere in this signal path.

  **Unlike the BC546 stage, a plain buffer doesn't invert the signal** — so
  `uart_set_line_inverse(uart_num, UART_SIGNAL_TXD_INV)` goes away entirely from
  `uart_tx_start()`. There's no inversion left anywhere in the path to cancel out.

  **Boot-safety, revised for this IC.** The BC546 circuit's base pull-down guaranteed
  a defined off-state (and so idle-high) for free during boot, before firmware ever
  runs. A CMOS/HCT buffer needs a different answer: a floating (Hi-Z) input into a
  CMOS gate is not safe on its own — it can sit at an indeterminate voltage, making
  the input stage draw excess current with an unpredictable, possibly-oscillating
  output. Fix: a pull-up resistor (10k) from GPIO25 — the `74HCT125`'s input pin — up
  to 3.3V.
  - At reset, before firmware runs: GPIO25 is Hi-Z (its normal power-on state; it's
    not a strapping pin), so the pull-up holds the buffer's input at a defined HIGH,
    and its output is HIGH — idle-high on the baseboard line from the very first
    instant, with no firmware involved at all.
  - Once firmware configures GPIO25 as UART1 TX, UART idle state is high by
    definition ("mark") — so the handoff from "pulled high by the resistor" to
    "driven high by the UART peripheral" is seamless, same logic level throughout,
    with no glitch window in between.
  - No OE-pin gating needed as a result — tie the buffer's enable pin permanently
    active per its datasheet polarity.

  Not yet bench-verified against the baseboard's actual RX input characteristics —
  the same caveat the BC546 circuit carried, which turned out to matter. See
  `RX_TX_LEVEL_INVESTIGATION.md`'s open questions for what's still unknown even after
  this change.

### Line-by-line UART plan

Decided: simplify to one plain RX line and one plain TX line — dropping the earlier plan
to also keep an RX tap on `CON->BASE` (which would have doubled as a self-check loopback
and, back when a jumper/shared-line design was still in play, a live-console monitor —
moot now that the controller rig has no stock-console connection to monitor at all).
Traded away deliberately: no hardware confirmation that a transmitted frame actually
landed on the wire as sent — the controller firmware becomes control-only on that line,
not dual-purpose.

- **`UART2` — `BASE->CON`, unchanged, GPIO27.** Stays RX-only, exactly as in
  `fw/frame-sniffer/` and `fw/ble-sniffer/` today. This is the baseboard's own output;
  nothing should ever drive TX onto it — that would fight the baseboard's own
  transmitter.
- **`UART1` — `CON->BASE`, TX-only, GPIO25.** No RX pin assigned on this UART at all —
  just the new TX line, through the level shifter above, straight to the baseboard's RX
  (no jumper — see Hardware plan above). GPIO25 chosen as a general-purpose,
  output-capable pin: not a strapping pin (GPIO0/2/5/12/15, which affect boot mode), not
  input-only (GPIO34-39, can't do TX at all), and not one of the WROOM-32's reserved
  integrated-flash pins (GPIO6-11). Sits next to the existing GPIO26/27 pair for a
  clustered, easy-to-remember pin layout; GPIO26 itself is intentionally *not* reused
  here, to avoid confusion with its different role (`CON->BASE` RX) on the sniffer rig.

#### Pin table

**Controller rig** (`fw/controller`, this document):

| GPIO | UART | Direction | Signal | Through |
|---|---|---|---|---|
| GPIO25 | UART1 | TX (out) | `CON->BASE` (commands to baseboard) | 74HCT125 buffer (see Hardware plan above) |
| GPIO27 | UART2 | RX (in) | `BASE->CON` (telemetry from baseboard) | 10k/15k divider (same as sniffer rig) |
| GPIO1 / GPIO3 | UART0 | board default | USB serial console | — |

**Sniffer rig** (`fw/byte-sniffer`, `fw/frame-sniffer`, `fw/ble-sniffer` — unchanged,
shown here for contrast since both rigs get referenced throughout this doc):

| GPIO | UART | Direction | Signal | Through |
|---|---|---|---|---|
| GPIO26 | UART1 | RX (in) | `CON->BASE` (console → baseboard, tapped) | 10k/15k divider |
| GPIO27 | UART2 | RX (in) | `BASE->CON` (baseboard → console, tapped) | 10k/15k divider |
| GPIO1 / GPIO3 | UART0 | board default | USB serial console | — |

Note GPIO27's role is identical on both rigs (same signal, same divider) — only GPIO26
(sniffer RX tap, unused on the controller) and GPIO25 (controller TX, doesn't exist on
the sniffer) differ.

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

1. **Hardware bring-up.** Build the controller PCB (power supply, RX divider, TX level
   shifter). Bench-test the board on its own — power rail clean, TX shifter's idle-high
   output correct — before it's ever connected to the real baseboard.
2. **Heartbeat only, belt unloaded, nobody on it.** Controller rig connected in place of
   the stock console. Transmit *only* the idle frame at the observed burst/gap cadence —
   no speed commands yet.
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

Keep the stock console (and/or the sniffer rig) physically at hand at every stage — the
point of the fallback is that reverting is a physical connector swap taking seconds, not
a firmware question.

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

- TX level: switched from a single-transistor BC546 inverting shifter to a `74HCT125`
  buffer (see Hardware plan above and `RX_TX_LEVEL_INVESTIGATION.md`) after the BC546
  circuit reached valid-looking TTL levels but the baseboard never responded to it.
  Not yet bench-verified against this baseboard's actual RX input characteristics —
  whether the new circuit actually resolves the non-response is still open.
- RX divider: the 10k/15k ratio was carried over from the original passive-sniffer
  rig's 5V-bus assumption, but the baseboard's real `BASE->CON` output was measured at
  ~13.5V — after this divider, GPIO27 sees ~8.3–8.7V, above the ESP32's rated 3.3V
  input (apparently survived so far via the GPIO's own clamp diodes, current-limited
  by the divider's series leg). Not the cause of the current non-response, but a
  separate thing worth fixing — see `RX_TX_LEVEL_INVESTIGATION.md`.
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
