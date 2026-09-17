#!/usr/bin/env python3
"""
BLE client for fw/controller's Treadmill Controller GATT service.

Scans for the device by name (advertised as "TreadmillController") rather
than a hardcoded address, same reasoning as tools/ble_monitor.py: macOS
hides real BLE hardware addresses from apps, so scanning by name is the
only approach that works the same way on Linux/macOS/Windows.

See fw/controller/main/ble_gatt.h and fw/controller/DESIGN.md's "BLE
console architecture" for the GATT layout and wire formats this script
speaks:

  - TELEMETRY (notify): decoded speed + steps, pushed once per valid
    BASE->CON frame.
  - CMD (write): PLAY / STOP / SET_SPEED, applied to uart_tx's ramp
    target -- the firmware itself owns ramping, checksums, and timing.

SAFETY: fw/controller/DESIGN.md's staged rollout calls for testing with
the belt unloaded before anything else. This script has no opinion on
that -- it sends exactly what you tell it to, whenever you tell it to.

Requires: pip install bleak
"""

import argparse
import asyncio
import sys
import threading

from bleak import BleakClient, BleakScanner

DEVICE_NAME = "TreadmillController"
TELEMETRY_CHAR_UUID = "7cee917e-4e4e-4250-bbe9-35d678c2d721"
CMD_CHAR_UUID = "239f8516-6fdb-4b12-b127-f601c7043f16"

CMD_PLAY = 0x01
CMD_STOP = 0x02
CMD_SET_SPEED = 0x03

# Matches fw/controller/main/uart_tx.h's UART_TX_SPEED_MIN_TENTHS/_MAX_TENTHS
# -- the confirmed floor/cap from DESIGN.md, not extrapolated past it. The
# firmware clamps too; this just avoids sending an obviously-wrong value.
SPEED_MIN_KMH = 0.8
SPEED_MAX_KMH = 6.0


def decode_telemetry(data: bytearray) -> str:
    """
    TELEMETRY record (see ble_gatt.h): byte 0 = format version,
    byte 1:2 = raw CON->BASE speed (big-endian), byte 3 = tenths-km/h
    estimate (nearest-entry lookup against the firmware's 53-point
    calibration table), byte 4 = step count (wraps at 256).
    """
    if len(data) < 5:
        return f"[short record, {len(data)} byte(s)]: {data.hex(' ')}"

    version, speed_hi, speed_lo, tenths_est, steps = data[:5]
    raw = (speed_hi << 8) | speed_lo
    note = "" if version == 0x01 else f"  (unknown format version 0x{version:02X})"
    return f"speed~{tenths_est / 10:.1f} km/h  raw={raw:5d}  steps={steps:3d}{note}"


def speed_to_tenths(km_h: float) -> int:
    clamped = min(max(km_h, SPEED_MIN_KMH), SPEED_MAX_KMH)
    if clamped != km_h:
        print(f"speed {km_h:.1f} km/h out of [{SPEED_MIN_KMH}, {SPEED_MAX_KMH}] range, "
              f"clamping to {clamped:.1f} (firmware would clamp anyway)", file=sys.stderr)
    return round(clamped * 10)


async def find_device(timeout: float):
    print(f"Scanning for '{DEVICE_NAME}' (timeout {timeout:.0f}s)...")
    device = await BleakScanner.find_device_by_name(DEVICE_NAME, timeout=timeout)
    if device is None:
        print(f"'{DEVICE_NAME}' not found -- is the ESP32 powered on and advertising?", file=sys.stderr)
    return device


async def send_cmd(client: BleakClient, *payload: int) -> None:
    await client.write_gatt_char(CMD_CHAR_UUID, bytes(payload), response=True)


async def run_one_shot(args, timeout: float) -> int:
    device = await find_device(timeout)
    if device is None:
        return 1

    async with BleakClient(device) as client:
        if args.action == "play":
            await send_cmd(client, CMD_PLAY)
            print("Sent PLAY.")
        elif args.action == "stop":
            await send_cmd(client, CMD_STOP)
            print("Sent STOP.")
        elif args.action == "speed":
            tenths = speed_to_tenths(args.km_h)
            await send_cmd(client, CMD_SET_SPEED, tenths)
            print(f"Sent SET_SPEED {tenths / 10:.1f} km/h (tenths={tenths}).")

    return 0


async def run_monitor(timeout: float) -> int:
    device = await find_device(timeout)
    if device is None:
        return 1

    def on_telemetry(_sender, data: bytearray) -> None:
        print(decode_telemetry(data))

    async with BleakClient(device) as client:
        print(f"Connected: {client.is_connected}")
        await client.start_notify(TELEMETRY_CHAR_UUID, on_telemetry)
        print("Subscribed to TELEMETRY. Ctrl+C to stop.\n")
        try:
            while True:
                await asyncio.sleep(1)
        except asyncio.CancelledError:
            pass

    return 0


async def run_shell(timeout: float) -> int:
    device = await find_device(timeout)
    if device is None:
        return 1

    def on_telemetry(_sender, data: bytearray) -> None:
        print(f"\r{decode_telemetry(data)}\n> ", end="", flush=True)

    async with BleakClient(device) as client:
        print(f"Connected: {client.is_connected}")
        await client.start_notify(TELEMETRY_CHAR_UUID, on_telemetry)

        loop = asyncio.get_running_loop()
        line_queue: asyncio.Queue = asyncio.Queue()

        def stdin_reader() -> None:
            try:
                while True:
                    line = input()
                    loop.call_soon_threadsafe(line_queue.put_nowait, line)
            except EOFError:
                loop.call_soon_threadsafe(line_queue.put_nowait, None)

        threading.Thread(target=stdin_reader, daemon=True).start()

        print(
            "Commands: play | stop | speed <km/h> | quit\n"
            "Live TELEMETRY prints above the prompt as it arrives.\n> ",
            end="", flush=True,
        )

        while True:
            line = await line_queue.get()
            if line is None:
                break

            parts = line.strip().split()
            if not parts:
                pass
            elif parts[0] == "play":
                await send_cmd(client, CMD_PLAY)
                print("Sent PLAY.")
            elif parts[0] == "stop":
                await send_cmd(client, CMD_STOP)
                print("Sent STOP.")
            elif parts[0] == "speed" and len(parts) == 2:
                try:
                    km_h = float(parts[1])
                except ValueError:
                    print(f"not a number: {parts[1]!r}")
                else:
                    tenths = speed_to_tenths(km_h)
                    await send_cmd(client, CMD_SET_SPEED, tenths)
                    print(f"Sent SET_SPEED {tenths / 10:.1f} km/h (tenths={tenths}).")
            elif parts[0] in ("quit", "exit"):
                break
            else:
                print(f"unrecognized command: {line!r} (try: play, stop, speed <km/h>, quit)")

            print("> ", end="", flush=True)

    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--timeout", type=float, default=15.0,
        help="seconds to scan for the device before giving up (default: 15)",
    )
    sub = parser.add_subparsers(dest="action")

    sub.add_parser("play", help="send PLAY (ramp to the 0.8 km/h startup speed)")
    sub.add_parser("stop", help="send STOP (ramp down to a full stop)")

    speed_parser = sub.add_parser("speed", help="send SET_SPEED, in km/h")
    speed_parser.add_argument("km_h", type=float, help="target speed in km/h (0.8-6.0)")

    sub.add_parser("monitor", help="just print TELEMETRY notifications, send no commands")
    sub.add_parser("shell", help="interactive: live TELEMETRY plus a play/stop/speed prompt (default)")

    args = parser.parse_args()
    action = args.action or "shell"

    try:
        if action == "monitor":
            sys.exit(asyncio.run(run_monitor(args.timeout)))
        elif action == "shell":
            sys.exit(asyncio.run(run_shell(args.timeout)))
        else:
            sys.exit(asyncio.run(run_one_shot(args, args.timeout)))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
