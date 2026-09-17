#!/usr/bin/env python3
"""
Simple desktop GUI for fw/controller's Treadmill Controller GATT service.

Same protocol as tools/controller.py (see that file and
fw/controller/main/ble_gatt.h / DESIGN.md's "BLE console architecture" for
the wire formats) but as a Tkinter window instead of a text shell: auto-scans
for the device by name, then gives Play / Stop / Speed Up / Speed Down
buttons and a live telemetry readout.

BLE I/O (bleak, asyncio) runs on a background thread with its own event
loop; the Tkinter mainloop stays on the main thread and polls a queue for
status/telemetry updates. All GATT calls are marshalled onto the BLE
thread's loop via asyncio.run_coroutine_threadsafe.

SAFETY: fw/controller/DESIGN.md's staged rollout calls for testing with the
belt unloaded before anything else. This UI has no opinion on that -- it
sends exactly what you click, whenever you click it.

Requires: pip install bleak
"""

import asyncio
import queue
import sys
import threading
import tkinter as tk
from tkinter import ttk

from bleak import BleakClient, BleakScanner

DEVICE_NAME = "TreadmillController"
TELEMETRY_CHAR_UUID = "7cee917e-4e4e-4250-bbe9-35d678c2d721"
CMD_CHAR_UUID = "239f8516-6fdb-4b12-b127-f601c7043f16"

CMD_PLAY = 0x01
CMD_STOP = 0x02
CMD_SET_SPEED = 0x03

# Matches fw/controller/main/uart_tx.h's UART_TX_SPEED_MIN_TENTHS/_MAX_TENTHS.
SPEED_MIN_TENTHS = 8   # 0.8 km/h
SPEED_MAX_TENTHS = 60  # 6.0 km/h
SPEED_STEP_TENTHS = 1  # 0.1 km/h per Speed Up/Down click

SCAN_TIMEOUT_S = 15.0


def decode_telemetry(data: bytes) -> tuple[float, int] | None:
    """
    Returns (speed_km_h, steps) from a TELEMETRY record, or None if short.
    byte 3's tenths-km/h estimate is the firmware's nearest-entry lookup
    against its 53-point calibration table, not a raw approximation.
    """
    if len(data) < 5:
        return None
    _version, _speed_hi, _speed_lo, tenths_est, steps = data[:5]
    return tenths_est / 10, steps


class BLEWorker:
    """Owns the asyncio loop + BleakClient on a background thread."""

    def __init__(self, events: "queue.Queue[tuple]"):
        self.events = events
        self.loop = asyncio.new_event_loop()
        self.client: BleakClient | None = None
        self.thread = threading.Thread(target=self._run_loop, daemon=True)

    def start(self) -> None:
        self.thread.start()
        asyncio.run_coroutine_threadsafe(self._connect(), self.loop)

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def _on_disconnect(self, _client: BleakClient) -> None:
        self.events.put(("status", "Disconnected."))
        self.events.put(("connected", False))

    def _on_telemetry(self, _sender, data: bytearray) -> None:
        decoded = decode_telemetry(bytes(data))
        if decoded is not None:
            self.events.put(("telemetry", *decoded))

    async def _connect(self) -> None:
        self.events.put(("status", f"Scanning for '{DEVICE_NAME}'..."))
        device = await BleakScanner.find_device_by_name(DEVICE_NAME, timeout=SCAN_TIMEOUT_S)
        if device is None:
            self.events.put(("status", f"'{DEVICE_NAME}' not found -- is it powered on and advertising?"))
            self.events.put(("connected", False))
            return

        self.events.put(("status", "Connecting..."))
        client = BleakClient(device, disconnected_callback=self._on_disconnect)
        try:
            await client.connect()
            await client.start_notify(TELEMETRY_CHAR_UUID, self._on_telemetry)
        except Exception as exc:  # noqa: BLE001 -- report any connect failure to the UI
            self.events.put(("status", f"Connect failed: {exc}"))
            self.events.put(("connected", False))
            return

        self.client = client
        self.events.put(("status", f"Connected to {DEVICE_NAME}."))
        self.events.put(("connected", True))

    def reconnect(self) -> None:
        self.client = None
        asyncio.run_coroutine_threadsafe(self._connect(), self.loop)

    async def _send(self, *payload: int) -> None:
        if self.client is None or not self.client.is_connected:
            self.events.put(("status", "Not connected -- command dropped."))
            return
        try:
            await self.client.write_gatt_char(CMD_CHAR_UUID, bytes(payload), response=True)
        except Exception as exc:  # noqa: BLE001 -- report write failures, don't crash the loop
            self.events.put(("status", f"Write failed: {exc}"))

    def send_play(self) -> None:
        asyncio.run_coroutine_threadsafe(self._send(CMD_PLAY), self.loop)

    def send_stop(self) -> None:
        asyncio.run_coroutine_threadsafe(self._send(CMD_STOP), self.loop)

    def send_speed(self, tenths: int) -> None:
        asyncio.run_coroutine_threadsafe(self._send(CMD_SET_SPEED, tenths), self.loop)


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        root.title("Treadmill Controller")
        root.resizable(False, False)

        self.events: "queue.Queue[tuple]" = queue.Queue()
        self.worker = BLEWorker(self.events)

        self.connected = False
        self.running = False  # True once PLAY has been sent and STOP hasn't followed
        self.target_tenths = SPEED_MIN_TENTHS

        self._build_ui()
        self.worker.start()
        self.root.after(100, self._poll_events)

    def _build_ui(self) -> None:
        pad = {"padx": 10, "pady": 6}

        self.status_var = tk.StringVar(value="Starting...")
        ttk.Label(self.root, textvariable=self.status_var, wraplength=320).grid(
            row=0, column=0, columnspan=3, sticky="w", **pad
        )

        self.telemetry_var = tk.StringVar(value="Speed: -- km/h   Steps: --")
        ttk.Label(self.root, textvariable=self.telemetry_var, font=("", 11)).grid(
            row=1, column=0, columnspan=3, sticky="w", **pad
        )

        self.target_var = tk.StringVar(value=self._target_text())
        ttk.Label(self.root, textvariable=self.target_var, font=("", 20, "bold")).grid(
            row=2, column=0, columnspan=3, pady=(4, 10)
        )

        self.down_btn = ttk.Button(self.root, text="▼ Speed Down", command=self._on_speed_down)
        self.down_btn.grid(row=3, column=0, **pad)

        self.up_btn = ttk.Button(self.root, text="▲ Speed Up", command=self._on_speed_up)
        self.up_btn.grid(row=3, column=2, **pad)

        self.play_btn = ttk.Button(self.root, text="Play", command=self._on_play)
        self.play_btn.grid(row=4, column=0, sticky="ew", **pad)

        self.stop_btn = ttk.Button(self.root, text="Stop", command=self._on_stop)
        self.stop_btn.grid(row=4, column=2, sticky="ew", **pad)

        self.reconnect_btn = ttk.Button(self.root, text="Reconnect", command=self._on_reconnect)
        self.reconnect_btn.grid(row=5, column=0, columnspan=3, sticky="ew", **pad)

        self._set_controls_enabled(connected=False, running=False)

    def _target_text(self) -> str:
        return f"Target: {self.target_tenths / 10:.1f} km/h"

    def _set_controls_enabled(self, *, connected: bool, running: bool) -> None:
        self.play_btn.state(["!disabled"] if connected else ["disabled"])
        self.stop_btn.state(["!disabled"] if (connected and running) else ["disabled"])
        speed_ok = connected and running
        self.up_btn.state(["!disabled"] if speed_ok else ["disabled"])
        self.down_btn.state(["!disabled"] if speed_ok else ["disabled"])

    def _on_play(self) -> None:
        self.target_tenths = SPEED_MIN_TENTHS
        self.target_var.set(self._target_text())
        self.worker.send_play()
        self.running = True
        self._set_controls_enabled(connected=self.connected, running=self.running)

    def _on_stop(self) -> None:
        self.worker.send_stop()
        self.running = False
        self._set_controls_enabled(connected=self.connected, running=self.running)

    def _on_speed_up(self) -> None:
        self.target_tenths = min(self.target_tenths + SPEED_STEP_TENTHS, SPEED_MAX_TENTHS)
        self.target_var.set(self._target_text())
        self.worker.send_speed(self.target_tenths)

    def _on_speed_down(self) -> None:
        self.target_tenths = max(self.target_tenths - SPEED_STEP_TENTHS, SPEED_MIN_TENTHS)
        self.target_var.set(self._target_text())
        self.worker.send_speed(self.target_tenths)

    def _on_reconnect(self) -> None:
        self.running = False
        self._set_controls_enabled(connected=False, running=False)
        self.worker.reconnect()

    def _poll_events(self) -> None:
        try:
            while True:
                kind, *rest = self.events.get_nowait()
                if kind == "status":
                    self.status_var.set(rest[0])
                elif kind == "connected":
                    self.connected = rest[0]
                    if not self.connected:
                        self.running = False
                    self._set_controls_enabled(connected=self.connected, running=self.running)
                elif kind == "telemetry":
                    speed_km_h, steps = rest
                    self.telemetry_var.set(f"Speed: {speed_km_h:.1f} km/h   Steps: {steps}")
        except queue.Empty:
            pass
        self.root.after(100, self._poll_events)


def main() -> None:
    root = tk.Tk()
    App(root)
    try:
        root.mainloop()
    except KeyboardInterrupt:
        sys.exit(0)


if __name__ == "__main__":
    main()
