#!/usr/bin/env python3
"""
One-shot PLAY -> 2.0 km/h -> STOP sequence test for fw/controller.

Runs immediately on startup, no arguments: sends PLAY, waits 3s, sends
SET_SPEED 2.0 km/h, waits 3s, sends STOP. Every CMD write and every
TELEMETRY notification is printed with a monotonic timestamp and its raw
bytes, so a run can be lined up against a scope capture on the CON->BASE
(TX) and BASE->CON (RX) wires to see whether the transmitted frame and the
baseboard's own reported speed actually move together.

See tools/controller.py for the full interactive client and
fw/controller/main/ble_gatt.h for the wire formats decoded/built here.

Requires: pip install bleak
"""

import asyncio
import sys
import time

from bleak import BleakClient, BleakScanner

DEVICE_NAME = "TreadmillController"
TELEMETRY_CHAR_UUID = "7cee917e-4e4e-4250-bbe9-35d678c2d721"
CMD_CHAR_UUID = "239f8516-6fdb-4b12-b127-f601c7043f16"

CMD_PLAY = 0x01
CMD_STOP = 0x02
CMD_SET_SPEED = 0x03

STEP_DELAY_S = 3.0
SPEED_TENTHS = 20  # 2.0 km/h

_t0 = time.monotonic()


def log(label: str, data: bytes) -> None:
    elapsed = time.monotonic() - _t0
    print(f"[{elapsed:7.3f}s] {label:<10s} {data.hex(' ').upper()}", flush=True)


async def send_cmd(client: BleakClient, label: str, *payload: int) -> None:
    data = bytes(payload)
    log(f"TX {label}", data)
    await client.write_gatt_char(CMD_CHAR_UUID, data, response=True)


def on_telemetry(_sender, data: bytearray) -> None:
    log("RX TELEM", bytes(data))


async def main() -> int:
    print(f"Scanning for '{DEVICE_NAME}'...")
    device = await BleakScanner.find_device_by_name(DEVICE_NAME, timeout=15.0)
    if device is None:
        print(f"'{DEVICE_NAME}' not found -- is the ESP32 powered on and advertising?", file=sys.stderr)
        return 1

    async with BleakClient(device) as client:
        print(f"Connected: {client.is_connected}")
        await client.start_notify(TELEMETRY_CHAR_UUID, on_telemetry)

        await send_cmd(client, "PLAY", CMD_PLAY)
        await asyncio.sleep(STEP_DELAY_S)

        await send_cmd(client, "SPEED", CMD_SET_SPEED, SPEED_TENTHS)
        await asyncio.sleep(STEP_DELAY_S)

        await send_cmd(client, "STOP", CMD_STOP)
        await asyncio.sleep(1.0)  # catch any trailing notifications before disconnect

    print("Done.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        pass
