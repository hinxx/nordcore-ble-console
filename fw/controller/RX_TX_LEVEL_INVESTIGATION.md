# RX/TX electrical investigation — clone controller vs. stock console

**RESOLVED.** Root cause: `fw/controller`'s UART was configured 8N2 (no
parity, 2 stop bits) while the stock console actually transmits **8O1** (odd
parity, 1 stop bit) — both 11 bits/character, which is exactly why content,
cadence, and every voltage/level measurement in this document looked
identical the whole time. Confirmed directly by measuring the bit
immediately after the 8 data bits on real captures: on the stock console it
varies exactly with odd parity of that byte's data; on the clone (then 8N2)
it was fixed at 1 regardless of data, every single capture in this
investigation. Fixed in `uart_tx.c` by changing `.parity` to
`UART_PARITY_ODD` and `.stop_bits` to `UART_STOP_BITS_1` (v1.0.4) — a
config-only change, no other code affected. Confirmed working end to end:
`BASE->CON` telemetry now tracks a commanded ramp exactly like the real
console (PLAY → climbs to raw 620/0.8km/h and holds; SET_SPEED 2.0km/h →
climbs to raw ~1534/2.0km/h and holds; STOP → ramps back down), reproduced
twice, and **the belt physically moved** on both runs. See "UART framing:
8N2 vs 8O1 — the actual root cause" near the end of this document for the
full discovery, and `first_successful_play_stop.png` for the scope capture
of the second, belt-confirmed run.

Everything below is kept as the historical record of how this was found —
every dead end is real work that narrowed the search, not wasted effort, and
the methodology (skepticism toward screenshot-reading, insistence on direct
measurement, retracting wrong theories in writing rather than quietly
dropping them) is probably as useful to whoever reads this next as the
answer itself.

---

**Original framing of the problem, kept for context**: `fw/controller` (the
"clone" board below) transmits byte-for-byte, cycle-for-cycle identical
`CON->BASE` traffic to the stock console, confirmed against live captures of
the real console (see "Protocol verification" below), yet the baseboard
never responds to it — `BASE->CON` telemetry stays at raw speed 0 / steps 0
through PLAY, SET_SPEED, and STOP, indefinitely. As of the `74AHCT125`
attempt (later in this document), the TX signal reaching the baseboard had
been directly confirmed — not just inferred to be within some threshold —
as clean and correct as the real stock console's own signal: content,
cadence, ground reference, and voltage levels all checked out, and the
baseboard still didn't react at all.

The baseboard also has an audible relay that engages/disengages based on
detecting active `CON->BASE` (TX) traffic — nothing to do with RX, power
source, or protocol content in any way this document was able to test with
a scope. The clone's TX signal, even in its fully voltage/content/cadence-
verified 8N2 form, never triggered this relay. In hindsight, this is fully
explained by the framing mismatch too: a baseboard UART peripheral genuinely
configured for 8O1 would flag a hardware parity error on every 8N2 byte
whose extra bit didn't happen to match what odd parity required, and very
plausibly never refreshes the relay's watchdog on a parity-errored frame —
see "UART framing" below for exactly how this was found.

## The symptom

- Clone board (ESP32 + BC546 level shifter, `fw/controller` firmware) replaces
  the stock console at the baseboard connector.
- Clone transmits the identical `CON->BASE` frame sequence a real button press
  on the stock console produces (verified live, see below).
- Baseboard's own `BASE->CON` telemetry never changes: stays at the idle
  values (status `0xA0`, speed-mirror bytes `00 00`) forever, regardless of
  PLAY / SET_SPEED / STOP commands from the clone.
- Swapping the stock console back onto the same connector, same baseboard,
  same power — it responds normally (belt runs, telemetry tracks the
  commanded speed).
- The belt has never moved once under the clone's control, across many
  attempts.

## Ruled out so far

- **No safety key on this model.** Confirmed directly.
- **Power-sequencing / pairing at boot.** The connector has been swapped and
  the whole system power-cycled well over a hundred times across this
  investigation (every time a wire was touched or a console swapped). If the
  baseboard needed to see the clone attached from cold power-on to "pair," it
  has had ample opportunity to. Separately, hot-swapping the clone out for the
  stock console on an *already-running* baseboard (no power cycle at all)
  worked immediately — ruling out any baseboard-side cold-boot requirement
  specifically. A dedicated capture of the stock console's own power-cycle
  (see "Power-cycle / handshake hypothesis" below) found no special
  introduction packet on either wire either — see that section for the full
  story.
- **A separate "console present" wire.** Confirmed directly: exactly four
  wires run between the console and the baseboard — 12V, GND, RX, TX. No
  additional pins, so there's no separate discrete presence-detection line
  to account for; whatever the baseboard needs, it has to come through one of
  these four.
- **TX/RX synchronization.** See "TX/RX synchronization" below: rigorously
  re-tested with precise simultaneous timestamps (not the older, less
  reliable byte-sniffer logs) — the two directions run on independent,
  freely-drifting clocks with no phase relationship at all, on the real
  console. The clone's own firmware already behaves the same way.
- **Power-source-based load detection.** See "Relay-click discovery" below:
  the stock console still engages the baseboard's relay and works normally
  when powered from a completely external 12V supply (shared ground only,
  not drawing through the baseboard's own 12V pin). Whatever gates the
  relay, it isn't about the baseboard sensing current on its own supply.
- **Frame content.** `fw/controller`'s `uart_tx.c` transmits
  `68 08 21 50 00 FA 00 14 87 43` as the first non-idle frame after PLAY —
  confirmed against a live scope capture of the clone (not just the historical
  sniffer logs) to be an exact byte-for-byte match to what the stock console
  transmits for the same action.
- **Cadence.** The stock console sends one `CON->BASE` frame every ~200ms,
  continuously (no bursts, no multi-hundred-ms gaps — an earlier assumption
  in `DESIGN.md`, based on older byte-level sniffing, turned out to be wrong).
  `fw/controller`'s `uart_tx.c` was corrected to match this and does.
- **Physical pin mapping.** Visually confirmed the clone's TX/RX wires land on
  the same physical connector pins the stock console's did.
- **Ground reference offset.** See voltage data below — both TX and RX idle-low
  levels sit within ~0.1-0.2V of true 0V, on both the clone and (where
  measured) the stock console. No evidence of a ground offset between boards.

## Circuit comparison

### Stock console (known-good reference)

Traced directly off the console's own PCB:

- **TX** (console's own CPU output → baseboard, i.e. `CON->BASE`): a single
  ~220Ω SMD resistor ("221" marking) in series between the CPU pin and the
  baseboard connector. **No pull-up, no pull-down, no other components.**
- **RX** (baseboard output → console's own CPU input, i.e. `BASE->CON`): a
  ~220Ω SMD resistor in series, plus a small capacitor from that node to
  ground (simple RC low-pass). No voltage divider.

Notably, the console's own CPU receives the baseboard's raw signal directly
through just an RC filter — no divider — and drives its own output through
nothing but a series resistor. Whatever this CPU is, it appears to tolerate
the baseboard's native voltage swing on both pins without additional
level-shifting.

### Clone (`fw/controller`)

- **TX**: ESP32 GPIO25 → UART1 (software-inverted via
  `uart_set_line_inverse(..., UART_SIGNAL_TXD_INV)`) → BC546 common-emitter
  stage (base resistor, base pull-down per `DESIGN.md`, collector pull-up to
  the board's 5V rail) → baseboard connector. Collector pull-up value has
  been tried at 10kΩ, 1.2kΩ, and removed entirely (see below) — none changed
  the outcome.
- **RX**: baseboard connector → 10k/15k resistor divider (same ratio as the
  original passive sniffer rig, per `DESIGN.md`) → ESP32 GPIO27.

## Voltage measurements

All measurements via a Rohde & Schwarz RTA4004, raw sample export over SCPI,
analyzed as min/max/median/p5/p95 split by logic state (threshold 2.5V).
"TX" = `CON->BASE` (commands into the baseboard). "RX" = `BASE->CON`
(baseboard's own telemetry output). Direction/channel identity in each row
was confirmed by decoding actual bit content, not by assumed probe labeling
(an earlier session mislabeled one capture — see caveat at the end).

| Condition | Line | State | median | p95 | min/max |
|---|---|---|---:|---:|---:|
| Stock console, connected | TX | idle-high | 4.66V | **5.00V** | −7.89 / 13.74* |
| Clone, 10kΩ pull-up | TX | idle-high | 3.98V | 4.27V | −0.86 / 4.71 |
| Clone, 10kΩ pull-up | TX | idle-low | 0.12V | 0.37V | −0.90 / 2.08 |
| Clone, 1.2kΩ pull-up | TX | idle-high | 4.27V | 4.52V | −0.81 / 4.96 |
| Clone, 1.2kΩ pull-up | TX | idle-low | 0.12V | 0.37V | (same as above) |
| Clone, pull-up **removed** | TX | idle-high | 4.03V | 4.27V | −0.81 / 4.66 |
| Clone, pull-up **removed** | TX | idle-low | 0.12V | 0.32V | — |
| Clone disconnected, connector floating | TX pin (baseboard's RX input, undriven) | — | 4.22V | 4.47V | 3.54 / 4.86, **no valid UART framing at all** |
| Clone, 10kΩ or 1.2kΩ pull-up | RX | idle-high | 8.26–8.46V | 8.6–8.7V | up to 10.8 |
| Clone, 10kΩ or 1.2kΩ pull-up | RX | idle-low | 0.06V | 0.35V | — |
| Clone disconnected, connector floating | RX (baseboard's real telemetry, decodes correctly) | idle-high | 13.54V | 13.78V | up to 15.5 |
| Clone disconnected, connector floating | RX | idle-low | 0.06V | 0.31V | — |

\* the stock console's max/min spikes are overshoot/ringing on a fast edge,
not sustained levels — p95 is the representative "steady high."

**Key points from this table:**

- Stock console's TX idle-high is a clean, solid 5.00V. The clone's TX
  idle-high has never exceeded ~4.3–5.0V range and settles around 4.0–4.3V
  regardless of collector pull-up value (10kΩ → 1.2kΩ → none). Removing the
  pull-up entirely made it *slightly worse*, not better — the level is
  essentially insensitive to the external pull-up, which is itself odd if a
  passive resistor divider were the limiting factor.
- Both TX idle-high (~4.0–5.0V) and idle-low (~0.1V) are comfortably within
  standard TTL thresholds (VIL max 0.8V, VIH min 2.0V) on the clone, the same
  as the stock console. **By that standard there is no meaningful difference
  in TX signal quality between the clone and the stock console** — this was
  raised directly in review and we don't have a concrete measured difference
  (rise time, jitter, etc.) beyond the ~0.5–1V idle-high gap, itself within
  valid range either way.
- RX (baseboard's own output, undriven by anything on our side) genuinely
  operates at ~13.5V high when measured directly at the connector with
  nothing else attached — confirmed by decoding real, checksum-valid baseboard
  telemetry frames on that same capture, so this is a real signal property,
  not a floating/undriven artifact. After the clone's 10k/15k divider this
  becomes ~8.3–8.7V at the ESP32's GPIO27 — above the ESP32's rated 3.3V
  input, survived so far apparently via the GPIO's own internal clamp diodes
  (current-limited by the divider's 10k series leg). This divider ratio was
  carried over from the original passive-sniffer rig's design, which assumed
  a ~5V bus.
- TX (baseboard's own RX input, what the clone/console drive into) floats at
  only ~4.2–4.5V when nothing drives it — not a strong voltage, and not in
  the same domain as RX's ~13.5V. Whether this is a weak native pull-up on
  the baseboard's input or coupling from the physically adjacent, actively-
  switching RX line is not established.
- A 5V-rail check during active TX switching (scope on the 5V rail itself,
  correlated against TX high/low state) showed **no measurable sag**: median
  5.042V identical whether TX was high or low. Rules out regulator/decoupling
  sag as a contributor to the TX level.

**Rail sag check** (5V rail vs. TX switching state, 1.2kΩ pull-up):

| | median |
|---|---:|
| Rail while TX high | 5.042V |
| Rail while TX low | 5.042V |

## Caveat on the data above

One capture session had CH1/CH2 (TX/RX) swapped by mistake at the probe —
identified and corrected by re-verifying channel identity against actual
decoded frame content rather than trusting the physical probe label. The
table above uses the corrected assignments throughout. The original
(uncorrected) reading momentarily suggested the baseboard's TX **input** had
a ~13.7V pull-up of its own; that was wrong — it was actually looking at the
RX/telemetry **output** signal. That specific theory (and the resulting
"remove the external pull-up" experiment) should be disregarded; it's
included in the pull-up-removed row above for completeness, but the reasoning
behind trying it was based on the mislabeled data.

## TXB0104 attempt (tried, and reverted)

Tried as a substitute for the (unavailable at the time) `74HCT125`, since a
`TXB0104` breakout was already on hand. Wiring was confirmed correct at every
point checked: `VCCA` on the ESP32's 3.3V rail, `VCCB` measured at 5.01V, `OE`
tied to `VCCA` (correct — active-high, and this specific breakout also has an
onboard pull-up to `VCCA` by default), and GND continuity confirmed between
the `TXB0104` board and the baseboard connector. Ultimately not usable for
this signal anyway, for a specific, well-understood reason — not a wiring
mistake.

### What was measured

1. **At the baseboard connector** (the same probe point used for every BC546
   measurement above): no valid UART framing at all. 130,965 of 131,032
   samples sat flat "high" (median 4.08V); the rare "low" excursions only
   reached ~2.2–2.5V, never near 0V.
2. **Straight off GPIO25** (the `TXB0104`'s A1 input): perfectly clean —
   `68 08 20 00 00 00 00 14 3C 43` decoding correctly every ~200ms, HIGH
   median 3.34V, LOW median 0.48V, essentially zero framing errors. This
   confirmed the firmware/ESP32 side was never the problem.
3. **Directly at the `TXB0104`'s own B1 pin** (upstream of the board's 220Ω
   series resistor toward the connector): real toggling activity was present
   — but compressed to LOW ≈2.09V / HIGH ≈4.16V, and the decoded content was
   garbage (random bytes, no resemblance to the known frame).

### Diagnosis (corrected after outside review)

The first-pass read on this — "a `TXB0104` can't handle continuous 1200-baud
UART" — was wrong, and worth retracting explicitly: TI's own materials list
UART as a supported `TXB0104` application. The actual mechanism, per outside
review of this document:

- A `TXB0104` drives strongly only briefly around a detected edge, then
  settles into a deliberately weak (~4kΩ-class) steady-state "keeper" drive —
  correct behavior for a bidirectional bus like I2C, where nothing should
  actively fight a driver at rest. TI's own guidance is that any external
  pull-up/pull-down sharing a line with a `TXB0104` should be well above 50kΩ.
- The baseboard's `CON->BASE` input turns out to have a real bias of its own —
  inferred to be in the same few-kΩ range as the `TXB0104`'s weak
  steady-state drive, given the ~2.1V the two settled at while fighting each
  other. That's consistent with (not a separate fact from) the ~4.2–4.5V this
  same input floats to with nothing connected at all (see "Voltage
  measurements" above) — a moderate, real bias, not a strong pull-up and not
  genuinely floating either.
- When the `TXB0104` tries to hold LOW, the baseboard's own bias fights it,
  and the node settles at a divided ~2.1V instead of a clean low — exactly
  what was measured. When it holds HIGH, the `TXB0104`'s weak high-drive and
  the baseboard's bias agree, so HIGH looks comparatively clean (~4.16V).
- This explains every measurement above at once: edges get through (the
  `TXB0104`'s brief strong drive does move the line), but the sustained level
  in between decays back toward wherever the fight between the two biases
  settles, corrupting bit sampling — real toggling at the chip but garbage
  content, and an even flatter, more fully-lost signal by the time it reaches
  the connector through the added 220Ω series resistor (one more attenuating
  element in the same fight).

**This is a real, useful finding about the baseboard, not just about the
`TXB0104`**: its RX input carries a moderate bias of its own, likely in the
low-single-digit-kΩ range — not the near-floating condition a bare
series-resistor drive from a true push-pull source wouldn't need to fight at
all. It also retroactively explains why the earlier BC546 pull-up experiments
(10k → 1.2k → none) never moved the needle much: all of them were fighting
this same baseboard-side bias with a similarly-weak, comparable-order-of-
magnitude resistor, so none of them decisively won either.

### Conclusion

Not a wiring mistake — `VCCA`, `VCCB`, `OE`, GND, and the A-side signal were
all confirmed correct. It's a device-mechanism mismatch: a bidirectional,
edge-triggered translator built for weakly-biased/high-impedance bus lines
doesn't hold up against a baseboard input that turns out to have a real (if
modest) bias of its own. Reverted to the `74HCT125` plan (see `DESIGN.md`'s
Hardware plan, now including a 220Ω series resistor mirroring the stock
console's own TX circuit) — a buffer that continuously and unconditionally
drives both rails doesn't leave anything for the far end to out-fight.

### Confirming experiment — run, and conclusive

The suggested experiment above was run: with B1 floating (disconnected from
the baseboard, driving only the scope probe), the signal was a clean, proper
0V–5V rail-to-rail swing, as predicted. Confirms the `TXB0104` itself drives
cleanly with nothing to fight.

Follow-up series-resistor sweep between B1 and the baseboard connector (real
functional test each time — actual PLAY/SET_SPEED/STOP commands over BLE,
`BASE->CON` telemetry watched live, belt watched directly):

| Series resistance (B1 → connector) | TX LOW | TX HIGH | Content | Belt moved? |
|---|---:|---:|---|---|
| B1 floating (no baseboard connected) | ~0V | ~5V | clean | n/a |
| Direct ESP32 GPIO (no `TXB0104`), 1kΩ series | 2.47V | 3.72V | garbled | No |
| `TXB0104` B1, **0Ω** (direct wire, no added resistance) | 2.39V | 4.10V | garbled | No |

**The 0Ω result is the conclusive one.** With B1 wired directly to the
connector, the only series impedance in the circuit is the `TXB0104`'s own
internal output impedance — and it still lost the fight against the
baseboard's bias, landing at the same compressed, garbled signal as every
higher-resistance attempt. Since 0Ω is the floor for reducing series
resistance, no resistor value between B1 and the connector can fix this: any
added resistance only makes the fight worse, never better. This closes out
the `TXB0104` as a viable option for this line, definitively rather than
provisionally — it isn't a matter of finding the right series resistor, the
chip's steady-state drive itself is too weak for this specific baseboard,
full stop. The `74HCT125` plan (continuous, unconditional low-impedance drive
on both rails, not an edge-triggered weak keeper) is the fix; series-resistor
tuning is only meaningful once paired with a driver strong enough to be worth
tuning against in the first place.

## 74AHCT125 attempt — signal confirmed clean, symptom persists

The `74HCT125`-class plan, in practice: a `74AHCT125` (TTL-compatible inputs,
5V push-pull output, chosen over plain `74HC125` for the input-threshold
reason discussed earlier) plus a 220Ω series resistor toward the baseboard
connector, mirroring the stock console's own traced TX circuit exactly.

**One wiring gotcha along the way, worth flagging for next time**: OE on the
`74HCT125`/`74AHCT125` family is **active-LOW** — the opposite polarity from
the `TXB0104`'s active-HIGH OE used earlier. Tying it high (matching the
`TXB0104` convention) disables the output entirely (high-Z). First bring-up
showed exactly that symptom — TX pinned flat at ~0V, zero toggling at all,
no framing to even call "bad." Re-tied OE to GND and the chip started
driving immediately.

### Series resistor still mattered, but for the opposite reason as the `TXB0104`

| Series resistance | TX LOW | TX HIGH | Content |
|---|---:|---:|---|
| 1kΩ | 2.45V | 4.37V | garbled |
| **220Ω** | **1.12V** | **4.76V** | **clean, correct** |

The 1kΩ result looked superficially like the `TXB0104`'s failure mode
(compressed, garbled), but the underlying cause is different this time: the
`74AHCT125`'s own output impedance is genuinely low (a real push-pull driver,
not an edge-triggered keeper), so at 1kΩ the *resistor itself* — not a weak
chip — was the dominant impedance fighting the baseboard's bias. Dropping to
220Ω (matching the stock console's own value) was enough for this driver to
win that fight.

### Full functional test at 220Ω — the clearest result in this whole investigation

A complete, scope-correlated PLAY → SET_SPEED(2.0 km/h) → STOP cycle over
BLE, with the TX line captured throughout:

- **467 bytes decoded, only 2 framing errors** — both in the first two bytes
  of the entire capture window (almost certainly the window boundary cutting
  into a frame already in flight, not an ongoing issue). This is
  indistinguishable in quality from the real stock console's own signal,
  measured under the identical methodology throughout this document.
- The capture caught real, live ramp content: `320 → 244 → 168 → 92 → 16 → 0`
  in the CON->BASE speed field, decrementing by exactly 76 each ~200ms cycle
  — `uart_tx.c`'s own `RAMP_STEP_UNITS`, transmitted correctly, mid-STOP-ramp.
  It settles cleanly into repeating `68 08 20 00 00 00 00 14 3C 43` idle
  afterward, exactly as expected.
- **`BASE->CON` telemetry never moved.** `01 00 00 00 00` (speed 0, steps 0)
  for the entire ~11-second test — PLAY, SET_SPEED, and STOP all sent, all
  correctly transmitted, zero reaction.

### What this settles

Content, cadence, ground reference, and now voltage levels have all been
independently confirmed correct and clean — not merely "within some
threshold," but empirically as good as the real console's own signal,
verified with the same instruments and methodology used throughout this
document. **This rules out signal quality as the explanation for the
non-response, with much higher confidence than any earlier attempt got to.**
Every hypothesis this document has chased on the electrical side — BC546
pull-up sizing, `TXB0104` device mechanism, series resistor value, absolute
voltage levels — has now been tested to a clean, working signal, and the
baseboard still does not react at all. Whatever is actually blocking this has
to be something the electrical layer alone can't reveal.

## Power-cycle / handshake hypothesis (tested, not confirmed)

With clean TX signal quality established (previous section), one live
observation reopened the question of *when* the baseboard listens rather than
*what* it's sent: hot-swapping the clone out for the stock console on an
already-running, powered baseboard worked immediately — no baseboard cold
boot required. That ruled out a baseboard-side "needs to see a console from
its own power-on" requirement, but raised a narrower one: maybe the
**console's own** power-up moment is what matters — some introduction/
handshake frame it sends once, right as it boots, that the baseboard uses to
recognize a session before trusting anything else from it. Every capture in
this document up to this point had only ever recorded steady-state traffic,
never a console's actual first instant of being powered.

### Experiment

Scope armed continuously (`:RUN`, wide window) before a full power cycle of
the whole system with the stock console attached throughout, so its own
boot-up would be captured start to finish. First attempt used a 30s window
and missed the event entirely — both channels showed nothing but ordinary
steady-state idle traffic from the start of the window, meaning the real-world
delay between powering on, observing it, and confirming "done" exceeded the
window. Re-armed with 90s and re-ran the power cycle; this time the actual
power-off/on transition landed inside the capture (confirmed independently by
a per-second standard-deviation scan across the acquired memory, which found
a sharp drop from ~1.7 to ~0.05 right where the gap was) rather than by luck
of the window size.

### Result

Both wires show a genuine ~2.2 second gap (TX: no decodable start bit at all;
RX: noisy/garbled samples consistent with real electrical transient during
the power interruption, not valid data) — confirming this really is the
power-off/on moment, not a decode artifact. Immediately afterward, **both
resume with completely ordinary idle content**:

- TX: `68 08 20 00 00 00 00 14 3C 43` — the same idle frame as every other
  capture in this document.
- RX: `68 0C A0 00 00 00 00 ...` — the same idle heartbeat shape as always.

No distinct handshake frame, no unusual byte pattern, no content difference
from steady-state idle traffic at all.

### What this does and doesn't settle

Doesn't confirm the "special packet at boot" hypothesis — there isn't one, at
least not visible on the two wires this document has instrumented throughout.
It also doesn't rule out some other console-side presence signal, except that
a separate physical wire has now been ruled out directly: the console-to-
baseboard connector carries exactly four wires (12V, GND, RX, TX), confirmed
directly, so there's no discrete "presence" line to account for either. If
the baseboard does have some way of distinguishing "a real console is here"
from "nothing/something else is here," it isn't visible in the UART content,
its timing, or a fifth wire — which are the three things this document knows
how to check from the outside.

### Side-by-side comparison against the clone's own power cycle

The same experiment was repeated with the clone board (`74AHCT125` + 220Ω,
v1.0.3) in place instead of the stock console, using the identical
methodology (90s window, per-second stdev scan to locate the transition).
Reference screenshots for both, at matching zoom levels showing the same
flat-then-active transition shape: `original_console_boot.png` and
`clone_board_boot.png` (both in this directory).

Result: **no discrepancy found.** The clone's own power-up produces the same
ordinary idle content on both wires as the stock console's did —
`68 08 20 00 00 00 00 14 3C 43` on TX, `68 0C A0 00 00 00 00 ...` on RX —
and the two screenshots are visually indistinguishable in shape (a clean
flat-to-toggling transition at the power-on moment, sustained afterward).
This was expected, since the clone's own firmware behavior was already known
(`uart_tx.c` starts transmitting idle immediately in `app_main`), but it
closes off the possibility that the clone's *own* boot sequence looks
different from the console's in some way this document hadn't checked yet.

## TX/RX synchronization (tested, no correlation found)

Raised directly: does the baseboard expect `CON->BASE` commands at some
fixed timing relationship to its own `BASE->CON` heartbeat — a
request/response or phase-locked pattern — rather than each side just
running on its own clock? The root README already claimed the two streams
are independent, but that claim came from the same older byte-sniffer logs
that also got the burst-timing wrong elsewhere in this project (corrected in
`uart_tx.c`'s cadence fix), so it was worth re-testing rigorously rather than
trusting it by inheritance.

### Method

Used an existing simultaneous TX+RX capture of the real stock console
(precise, scope-timestamped samples, not the older byte-level logs) to
extract every frame's start time on both wires, then computed each TX frame
start's offset from the nearest preceding RX frame start, across 27 TX
frames and 22 RX frames.

### Result

- TX interval: a rock-steady **179.2ms**, every single cycle.
- RX interval: a rock-steady **220.2ms**, every single cycle.
- The TX-to-nearest-preceding-RX offset does **not** hold constant — it
  drifts steadily downward (~41ms per cycle, exactly `220.2ms - 179.2ms`)
  and wraps around by a full RX period each time it would go negative:
  `0.066 → 0.025 → (wrap) → 0.205 → 0.164 → 0.123 → 0.082 → 0.041 → (wrap) →
  0.220 → 0.179 → ...`

This is the textbook beat pattern of two clocks running independently at
different, stable rates — not a request/response or phase-locked
relationship. If TX were triggered by, or timed relative to, incoming RX
frames, this offset would stay fixed instead of walking continuously through
the full cycle.

### What this means for the clone

`fw/controller`'s own `uart_tx.c` already behaves the same way: `tx_task`
runs on its own independent `vTaskDelay(FRAME_PERIOD_MS)` loop and never
references incoming `BASE->CON` frames at all — free-running, exactly like
the real console. So this isn't a difference between the two: both are
genuinely independent, and the clone already matches that property
correctly. Ruled out as an explanation for the non-response, with much
stronger confidence than the inherited README claim carried on its own.

## Relay-click discovery — TX presence detection, not signal correctness

The single most important finding in this document. Discovered by ear, not
by instrument: the baseboard has an audible relay (almost certainly gating
power to the motor driver or some other critical circuit) that clicks on
engage and again on disengage. This gave a completely new, previously
untested signal to investigate against, independent of everything measured
so far.

### Baseline observations

- **Stock console, power on/off**: a click each time — relay engaging on
  power-up, disengaging on power-down.
- **Clone, power on/off**: no click, ever, at any point in this
  investigation, including with the fully verified 74AHCT125+220Ω circuit.
- **Nothing attached at all** (baseboard powered, no console/clone): silent.
  Confirms the relay isn't triggered by the baseboard's own power-up — it's
  specifically detecting something about a console's *presence*.

### Isolating what triggers it

**Power source is not it.** The stock console, powered from a completely
external 12V supply (sharing only ground with the baseboard, not drawing
through the baseboard's own 12V pin at all) — relay still clicks, console
still works normally. Rules out a "baseboard senses current on its own
supply pin" load-detection mechanism entirely; whatever this is, it isn't
about power at all.

**TX presence is it — RX is irrelevant.** Systematically connecting/
disconnecting the stock console's own RX and TX wires (independent of the
clone, using the real console to map the baseboard's requirements):

| Wires connected | Result |
|---|---|
| RX + TX | Click immediately on power-up |
| RX only | No click, ever |
| TX only (no RX at all) | Click |
| Both, then TX removed while running | Click again ~5 seconds later (disengage) |
| TX reconnected | Click immediately (re-engage) |

This maps out an asymmetric watchdog gated purely on `CON->BASE` (TX)
traffic: engage is fast (near-immediate once valid TX activity is present),
disengage takes ~5 seconds of TX absence. RX/telemetry plays no role in the
relay at all — a real console with only its TX wire connected engages the
relay exactly like a fully-wired one.

### The critical negative result

With this mapped out, the obvious next test: does the clone's TX — the fully
voltage/content/cadence-verified 74AHCT125+220Ω signal from the `74AHCT125`
section above — trigger the same relay the same way? **It does not.** No
click, ever, with the clone connected, under the same conditions that make
the real console's TX-only connection click immediately.

This is the sharpest result in the whole investigation: every check this
document knows how to perform on the TX signal — byte content, checksum,
cadence, idle-high/low voltage levels by both TTL standard and direct
comparison to the real console, ground reference — passes, and this
detector still doesn't fire. That means it's very likely sensing something
below the level anything in this document has measured so far. Two
candidates, both untested:

- **Actual current the driver sinks/sources**, not the resulting voltage.
  A detector built around injecting or monitoring current on the line (a
  common way to distinguish "something is actively driving this" from "it's
  floating or weakly biased") wouldn't show up in any voltage measurement
  this document has taken, however clean.
- **Edge speed / slew rate.** Every measurement so far has looked at
  steady-state levels via percentiles; nothing has measured rise/fall time at
  the microsecond scale. A detector keyed on edge rate rather than logic
  level would be invisible to everything checked up to this point.

### Suggested next step (not yet run)

Measure and compare the actual current the TX line sinks while driving LOW —
stock console vs. clone — not just the resulting voltage. If there's a
meaningful difference in current capability despite matching voltage levels,
that would point straight at a current-based detector as the actual gate on
this whole system's response, reframing the fix away from "get the voltage
right" (already done) toward "match the real driver's current/impedance
characteristics."

### Voltage match confirmed hands-on, current/slew-rate tests begun

Since the section above was written, two more data points came in:

**The clone's loaded voltage levels now match the stock console's exactly.**
Measured directly, side by side: clone unloaded reads a clean 4.8V high /
0V low; connected to the baseboard, the same clone reads 4.8V high / **1.2V**
low (the baseboard's own bias pulling the LOW state up, same effect
documented earlier in this file). The stock console, measured the same way
under the same load, reads the identical 4.8V / 1.2V. This is about as
direct a confirmation as possible that steady-state voltage — high or low —
is not the differentiator; the two are now indistinguishable by that measure.
A 100Ω series resistor was also tried (LOW moves further toward 0V) with no
change in relay behavior, reinforcing that voltage level alone isn't the
lever to pull here.

**First edge slew-rate measurement: close, not dramatic.** Using the scope's
own already-acquired memory (zooming a previously-captured wide window down
to a narrow slice reveals much finer native sample spacing than the wide
export provides — 60ns/sample here, vs. 4.62µs/sample for the same
acquisition exported at 0.6s width) precise 90%→10% fall times were measured
on one frame-start (idle-high → start-bit) edge each, stock console vs.
clone:

| | Fall time (90%→10%) |
|---|---:|
| Stock console | 109.1ns |
| Clone | 125.1ns |

A ~16ns difference, on the order of a single sample period at this
resolution (60ns) — not the dramatic, order-of-magnitude gap that would
obviously explain a hard pass/fail detector. This is a single edge measured
once on each side, not an average, so it's inconclusive rather than a clean
ruling-out: worth repeating across several edges (and the rising edge too,
since a driver's rise and fall characteristics aren't always symmetric)
before treating slew rate as settled either way.

### Still open (as of the electrical investigation — see below for the actual answer)

- Actual current sunk/sourced by the TX line while driving LOW, not just the
  resulting voltage — not measured, and turned out not to matter.
- Rising-edge (LOW→HIGH) slew rate — not measured, and turned out not to
  matter.
- Averaged/statistical edge-timing comparison across many edges — not done,
  and turned out not to matter.

None of these needed answering — see below.

## UART framing: 8N2 vs 8O1 — the actual root cause

Prompted by a direct, careful visual comparison of two scope screenshots (an
idle-frame capture from the clone and one from the stock console) that, on
close inspection, showed different-looking pulse-width patterns despite
identical decoded byte content and matching voltage levels. That observation
led to checking something no earlier step in this document had checked:
not just *what byte value* each frame decodes to, but the *exact bit* sitting
between the 8 data bits and the byte boundary — the assumption throughout
this entire investigation (baked into `tools/decode_scope_csv.py` and every
manual bit-count in this document) had been 8N2 (8 data bits, no parity, 2
stop bits). That assumption was never itself tested.

### The test

Both 8N2 (1 start + 8 data + 0 parity + 2 stop) and 8O1 (1 start + 8 data + 1
parity + 1 stop) are 11 bits per character — identical frame duration,
identical cadence, identical decoded byte value under either interpretation
(a naive decoder that just reads 8 data bits and checks for "stop bits ≈ 1"
would report the same byte value regardless of which framing is real, and
would only flag an error if the assumed-stop-bit position happened to read
0). The only way to tell them apart is to look at the specific bit
immediately after the 8 data bits and check whether it's a fixed 1 (real
8N2's first stop bit) or whether it varies with the data (8O1's parity bit).

Measured directly off real, precisely-timestamped raw samples (not a
screenshot) for the known idle frame `68 08 20 00 00 00 00 14 3C 43`, both
boards, same methodology used throughout this document:

| Byte | 1-bits | Odd parity would need | Clone's bit9 | Console's bit9 |
|---|---:|---:|---:|---:|
| `68` | 3 (odd) | 0 | 1 | **0** |
| `08` | 1 (odd) | 0 | 1 | **0** |
| `20` | 1 (odd) | 0 | 1 | **0** |
| `00` | 0 (even) | 1 | 1 | 1 |
| `00` | 0 (even) | 1 | 1 | 1 |
| `00` | 0 (even) | 1 | 1 | 1 |
| `00` | 0 (even) | 1 | 1 | 1 |
| `14` | 2 (even) | 1 | 1 | 1 |
| `3C` | 4 (even) | 1 | 1 | 1 |
| `43` | 3 (odd) | 0 | 1 | **0** |

The clone's bit9 is fixed at 1 for every byte, regardless of data — genuine
8N2, exactly matching its then-current firmware config. The console's bit9
tracks odd parity exactly, for every byte, in both of two independent frames
checked — genuine 8O1. Bit10 (the true stop bit) was 1 on both boards in
every case. The bytes where the two framings happen to agree (`00`, `14`,
`3C` — all with an even number of data 1-bits, where odd parity also
requires a 1) are exactly why the mismatch went unnoticed for so long:
enough of the idle frame's own byte values coincidentally produce the same
extra bit under either interpretation that a difference was never obvious
without isolating this one bit's data-dependence specifically.

### The fix

`fw/controller/main/uart_tx.c`, in the `uart_config_t` passed to
`uart_param_config()`:

```c
.parity    = UART_PARITY_ODD,   // was UART_PARITY_DISABLE
.stop_bits = UART_STOP_BITS_1,  // was UART_STOP_BITS_2
```

The ESP32's hardware UART peripheral generates the correct parity bit for
each transmitted byte automatically from this config; nothing else in the
firmware needed to change. Shipped as v1.0.4.

### Confirmed working, twice, with the belt moving

Ran the real functional test (`tools/controller_play_stop_test.py`: PLAY →
wait → SET_SPEED 2.0 km/h → wait → STOP, `BASE->CON` telemetry logged live)
against the real baseboard with v1.0.4. Both times:

- PLAY: raw speed climbs cleanly from 0 to **620** (0.8 km/h, the documented
  floor) and holds.
- SET_SPEED 2.0 km/h: climbs further to **~1534** (≈2.0 km/h) and holds.
- STOP: ramps back down toward 0.

This is the baseboard genuinely tracking a commanded setpoint for the first
time in this entire investigation — not a decode artifact, not a partial
response, the same shape of behavior documented from the real console
throughout this whole project. **The belt physically moved on both runs**,
confirmed directly. Scope capture of the second run (continuous, healthy TX
and RX activity throughout, no dropouts) saved as
`first_successful_play_stop.png`.

## Open questions for review

1. **Resolved — see "UART framing: 8N2 vs 8O1" above.** The relay (and the
   baseboard's non-response generally) was gated on receiving hardware-valid
   UART frames; the clone was transmitting 8N2 while the console transmits
   8O1, so a real baseboard UART peripheral configured for 8O1 would flag a
   parity error on roughly a third of the idle frame's own bytes (every one
   with an odd number of 1-bits), never refreshing whatever watchdog gates
   the relay. Every earlier hypothesis in this section (current sink/source
   capability, edge slew rate) was a reasonable next step given what was
   known at the time, but none of them needed to be true — the frames were
   being electrically received and then rejected by hardware framing logic
   underneath anything a voltage or timing measurement could see.
2. **Resolved** — see the `74AHCT125` section: the clone's TX levels are
   adequate once actively driven at low impedance through a small enough
   series resistor (220Ω, matching the stock console). The earlier ~4.0–4.3V
   "TTL-valid but did nothing" result was real but not sufficient on its own;
   a properly low-impedance drive plus the right series resistance produced a
   signal indistinguishable from the real console's, and the baseboard still
   didn't respond — so voltage level was never actually the blocker.
3. **Resolved**: yes, the TX stage needed to actively drive both rails at low
   impedance through a small series resistance. The BC546's near-constant
   collector voltage across three pull-up values, the `TXB0104`'s LOW
   settling at ~2.1V even at 0Ω added resistance, and the `74AHCT125`'s own
   1kΩ-vs-220Ω contrast are all explained by the same fact — the baseboard's
   `CON->BASE` input has a real bias of its own, and a driver needs both low
   output impedance *and* a small enough series resistance to firmly win
   against it. 220Ω (matching the stock console exactly) does.
4. Is the RX-side divider's resulting ~8.3–8.7V at GPIO27 something to fix
   independently (it's above the ESP32's rated input range) even though it
   isn't the cause of the current symptom?
5. Any other diagnostic that would help isolate "reaches the baseboard's
   input pin correctly, in every measurable respect, but the baseboard still
   doesn't act on it" — e.g. does this baseboard's UART peripheral have any
   documented quirk (framing, minimum pulse width, glitch filtering) that
   would reject an otherwise-valid signal from a different driving impedance
   than the stock console's push-pull output?

## Reference material

- `fw/controller/DESIGN.md` — full hardware plan and protocol notes this
  investigation is built on.
- `README.md` (repo root) — original passive-sniffer protocol findings,
  including the checksum formula and frame format referenced throughout.
- `tools/decode_scope_csv.py` — the software UART decoder used to produce
  the byte-content confirmations in this document, independent of the
  oscilloscope's own built-in protocol decoder (which was at one point found
  misconfigured as SPI on one channel, and separately gave misleading
  content at some display zoom levels).
