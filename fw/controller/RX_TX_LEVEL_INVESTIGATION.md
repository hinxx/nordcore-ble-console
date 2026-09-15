# RX/TX electrical investigation — clone controller vs. stock console

Written up for outside review. `fw/controller` (the "clone" board below) transmits
byte-for-byte, cycle-for-cycle identical `CON->BASE` traffic to the stock console,
confirmed against live captures of the real console (see "Protocol verification"
below), yet the baseboard never responds to it — `BASE->CON` telemetry stays at
raw speed 0 / steps 0 through PLAY, SET_SPEED, and STOP, indefinitely. Every
electrical property we know how to check from the outside (levels, ground
reference, timing) reads as valid, and the actual root cause is still unknown.
This document is the accumulated findings, not a conclusion.

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
  has had ample opportunity to.
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

## Open questions for review

1. **Why does the baseboard never respond**, given content, cadence, ground
   reference, and TX voltage levels (by TTL standard) all check out against a
   known-working reference? What test would distinguish "signal is
   electrically fine but not being sampled correctly" from "something
   non-electrical is different"?
2. Is the clone's TX idle-high (~4.0–4.3V, insensitive to pull-up strength)
   actually adequate for this baseboard's real input threshold, or is there a
   plausible failure mode (input capacitance, threshold near VIH_min under
   real loading, slew rate) that a static DC-level analysis wouldn't catch?
   Partially explained now — see the `TXB0104` section below: the baseboard's
   input has a real bias of its own in the low-single-digit-kΩ range, which
   any passive or weakly-driven "high" (the BC546's pull-up included) has to
   fight rather than simply needing to clear a static threshold.
3. **Resolved** (see the `TXB0104` section below): yes, the TX stage needs to
   actively drive both rails at low impedance. The BC546's near-constant
   collector voltage across three very different pull-up values, and the
   `TXB0104`'s LOW settling at ~2.1V instead of near 0V, are both explained by
   the same fact — the baseboard's `CON->BASE` input has a real bias of its
   own, comparable in magnitude to a weak resistive pull-up or a translator's
   weak steady-state drive, so neither ever decisively won. Not incomplete
   BC546 cutoff after all; a genuine fight between two comparably-weak drives.
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
