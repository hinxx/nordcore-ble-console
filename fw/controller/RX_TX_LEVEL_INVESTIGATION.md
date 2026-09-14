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
3. Does the BC546 stage need to actively drive the high side (e.g. a
   complementary push-pull, or a buffer IC like a 74HCT125) rather than rely
   on any passive pull-up, given the collector voltage barely moved across
   three very different pull-up conditions (10kΩ, 1.2kΩ, none)? Is that
   pattern itself evidence of incomplete transistor cutoff / leakage, or
   something else?
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
