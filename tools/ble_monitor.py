#!/usr/bin/env python3
"""
BLE monitor for fw/ble-sniffer's RX_LOG notify characteristic.

Scans for the device by name (advertised as "TreadmillSniffer") rather than
a hardcoded address, since macOS hides real BLE hardware addresses from
apps -- Bleak's ADDRESS on macOS is a locally-assigned identifier, not the
XX:XX:XX:XX:XX:XX MAC a BLE scanner app shows you, and it isn't guaranteed
stable across scans on every macOS version. Scanning by name sidesteps that
entirely, and works the same way on Linux/Windows too.

See README.md "BLE (fw/ble-sniffer/)" for the GATT layout, both UUID forms,
and the RX_LOG record format this script decodes.

Requires: pip install bleak
"""

import argparse
import asyncio
import sys

from bleak import BleakClient, BleakScanner

DEVICE_NAME = "TreadmillSniffer"
RX_LOG_CHAR_UUID = "f02dc604-61e1-4a7c-9413-3f4c5d97c47f"

DIRECTION_NAMES = {
    0x01: "BASE->CON",
    0x02: "CON->BASE",
}


def decode_notification(sender, data: bytearray) -> None:
    """
    RX_LOG record: byte 0 = direction, byte 1 = length, byte 2.. = raw frame.
    """
    if len(data) < 2:
        print(f"[short record, {len(data)} byte(s)]: {data.hex(' ')}")
        return

    direction_code = data[0]
    length = data[1]
    frame = data[2:]

    direction = DIRECTION_NAMES.get(direction_code, f"0x{direction_code:02X}")
    note = "" if length == len(frame) else f"  (length byte says {length}, got {len(frame)})"
    print(f"{direction:10s} {frame.hex(' ').upper()}{note}")


async def run(timeout: float) -> int:
    print(f"Scanning for '{DEVICE_NAME}' (timeout {timeout:.0f}s)...")
    device = await BleakScanner.find_device_by_name(DEVICE_NAME, timeout=timeout)
    if device is None:
        print(f"'{DEVICE_NAME}' not found -- is the ESP32 powered on and advertising?", file=sys.stderr)
        return 1

    async with BleakClient(device) as client:
        print(f"Connected: {client.is_connected}")
        await client.start_notify(RX_LOG_CHAR_UUID, decode_notification)
        print("Subscribed to RX_LOG. Ctrl+C to stop.\n")
        try:
            while True:
                await asyncio.sleep(1)
        except asyncio.CancelledError:
            pass

    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--timeout", type=float, default=15.0,
        help="seconds to scan for the device before giving up (default: 15)",
    )
    args = parser.parse_args()

    try:
        sys.exit(asyncio.run(run(args.timeout)))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
