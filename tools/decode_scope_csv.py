#!/usr/bin/env python3
"""
Software UART decoder for raw analog samples pulled from the scope over
SCPI (:CHANnelN:DATA?), used to cross-check/replace the scope's own
built-in protocol decoder when its display/zoom is too coarse or a noisy
region confuses it.

Usage: decode_scope_csv.py <csv_file> <x_start> <x_stop> [--label NAME]

<csv_file> is a single line of comma-separated voltage samples (as
returned by :CHANnelN:DATA?). <x_start>/<x_stop> come from the matching
:CHANnelN:DATA:HEADer? query (first two fields) and set the absolute
timestamp of each printed byte.

Protocol: 1200 baud, 8 data bits, no parity, 2 stop bits, idle-high --
matching the treadmill CON->BASE/BASE->CON UART settings used throughout
this project (see README.md "UART setup").
"""

import argparse
import sys

BAUD = 1200
BIT_PERIOD = 1.0 / BAUD
DATA_BITS = 8
STOP_BITS = 2
THRESHOLD_V = 2.5  # midpoint for a 0-5V idle-high UART line


def load_samples(path):
    with open(path) as f:
        text = f.read().strip()
    return [float(v) for v in text.split(",")]


def decode(samples, x_start, x_stop, label):
    n = len(samples)
    dt = (x_stop - x_start) / n
    bits = [1 if v > THRESHOLD_V else 0 for v in samples]

    samples_per_bit = BIT_PERIOD / dt
    if samples_per_bit < 4:
        print(f"WARNING: only {samples_per_bit:.1f} samples/bit -- "
              f"decode will be unreliable", file=sys.stderr)

    i = 1
    results = []
    while i < n:
        # look for a falling edge (idle-high 1 -> 0) as a candidate start bit
        if bits[i - 1] == 1 and bits[i] == 0:
            start_idx = i
            start_time = x_start + start_idx * dt

            # verify start bit: sample at its midpoint should be 0
            mid = start_idx + int(0.5 * samples_per_bit)
            if mid >= n or bits[mid] != 0:
                i += 1
                continue

            value = 0
            ok = True
            for b in range(DATA_BITS):
                sample_idx = start_idx + int((1.5 + b) * samples_per_bit)
                if sample_idx >= n:
                    ok = False
                    break
                if bits[sample_idx]:
                    value |= (1 << b)  # LSB first

            stop_ok = True
            last_bit_idx = start_idx
            for sbit in range(STOP_BITS):
                sample_idx = start_idx + int((1.5 + DATA_BITS + sbit) * samples_per_bit)
                last_bit_idx = sample_idx
                if sample_idx >= n:
                    ok = False
                    break
                if not bits[sample_idx]:
                    stop_ok = False

            if not ok:
                break

            results.append((start_time, value, stop_ok))
            i = last_bit_idx + 1
        else:
            i += 1

    print(f"=== {label}: {len(results)} byte(s) decoded, "
          f"window [{x_start:.6f}s .. {x_stop:.6f}s], "
          f"{samples_per_bit:.1f} samples/bit ===")
    for t, value, stop_ok in results:
        flag = "" if stop_ok else "  <-- BAD STOP BIT (framing error / noise)"
        print(f"  t={t:10.6f}s  0x{value:02X}{flag}")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("csv_file")
    ap.add_argument("x_start", type=float)
    ap.add_argument("x_stop", type=float)
    ap.add_argument("--label", default=None)
    args = ap.parse_args()

    samples = load_samples(args.csv_file)
    decode(samples, args.x_start, args.x_stop, args.label or args.csv_file)


if __name__ == "__main__":
    main()
