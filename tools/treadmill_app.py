#!/usr/bin/env python3
"""
Elaborate Treadmill Controller + History app for fw/controller's BLE GATT
service. A separate, bigger sibling of tools/controller_ui.py (that one
stays exactly as it is -- simple, no persistence) with two tabs:

  - Control: same Play / Stop / Speed Up / Speed Down + live telemetry as
    controller_ui.py.
  - History: a local SQLite log of every TELEMETRY sample received while
    this app is connected, with daily/weekly step totals, an estimated
    distance, and a bar chart. Logging only happens while this app is open
    and connected -- there's no separate background service, by design.

Same protocol as tools/controller.py/controller_ui.py -- see those and
fw/controller/main/ble_gatt.h / DESIGN.md's "BLE console architecture" for
the wire formats.

Step history survives power cycles and app restarts even though the
firmware's own TELEMETRY step count resets to 0 at a real stop or an
ESP32 reboot (see fw/controller/main/uart_rx.c): BLEWorker computes a
reset-aware per-sample delta the same way uart_rx.c does, one level up,
and HistoryDB stores those deltas as an append-only log -- daily/weekly
totals are then just SUM(step_delta) over a date range, so nothing is
ever "reset" in the database itself.

SAFETY: fw/controller/DESIGN.md's staged rollout calls for testing with the
belt unloaded before anything else. This app has no opinion on that -- it
sends exactly what you click, whenever you click it.

Requires: pip install bleak matplotlib
"""

import asyncio
import queue
import sqlite3
import sys
import threading
import time
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import ttk

import matplotlib
matplotlib.use("TkAgg")
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.figure import Figure

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

DB_PATH = Path(__file__).with_name("treadmill_history.db")

# When integrating speed over time to estimate distance, skip any gap
# between consecutive samples bigger than this -- a pause, a disconnect, a
# day boundary -- not actual walking. Comfortably above the real ~200-220ms
# BASE->CON heartbeat cadence (README/DESIGN.md), even with some BLE jitter.
MAX_INTEGRATION_GAP_S = 3.0


def decode_telemetry(data: bytes):
    """Returns (tenths_km_h, steps) from a TELEMETRY record, or None if
    short. See ble_gatt.h: byte 3 is a nearest-entry lookup against the
    firmware's 53-point calibration table; byte 4 is the accumulated
    (cross-speed-change) step count, reset only at a real stop or reboot."""
    if len(data) < 5:
        return None
    _version, _speed_hi, _speed_lo, tenths_est, steps = data[:5]
    return tenths_est, steps


class HistoryDB:
    """Append-only sample log, one row per TELEMETRY notification. Daily/
    weekly totals are plain SUM(step_delta) queries -- no counter lives in
    the database that ever needs resetting, so history survives power
    cycles, app restarts, and reconnects."""

    def __init__(self, path: Path):
        self.conn = sqlite3.connect(path)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS samples (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts REAL NOT NULL,
                speed_tenths INTEGER NOT NULL,
                step_delta INTEGER NOT NULL
            )
            """
        )
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_samples_ts ON samples(ts)")
        self.conn.commit()

    def add_sample(self, ts: float, speed_tenths: int, step_delta: int) -> None:
        self.conn.execute(
            "INSERT INTO samples (ts, speed_tenths, step_delta) VALUES (?, ?, ?)",
            (ts, speed_tenths, step_delta),
        )
        self.conn.commit()

    def daily_totals(self, days: int):
        rows = self.conn.execute(
            """
            SELECT date(ts, 'unixepoch', 'localtime') AS day, SUM(step_delta) AS steps
            FROM samples GROUP BY day ORDER BY day DESC LIMIT ?
            """,
            (days,),
        ).fetchall()
        return list(reversed(rows))  # oldest -> newest, for left-to-right charting

    def weekly_totals(self, weeks: int):
        rows = self.conn.execute(
            """
            SELECT strftime('%Y-W%W', ts, 'unixepoch', 'localtime') AS week, SUM(step_delta) AS steps
            FROM samples GROUP BY week ORDER BY week DESC LIMIT ?
            """,
            (weeks,),
        ).fetchall()
        return list(reversed(rows))

    def today_steps(self) -> int:
        row = self.conn.execute(
            """
            SELECT COALESCE(SUM(step_delta), 0) FROM samples
            WHERE date(ts, 'unixepoch', 'localtime') = date('now', 'localtime')
            """
        ).fetchone()
        return row[0]

    def current_week_steps(self) -> int:
        row = self.conn.execute(
            """
            SELECT COALESCE(SUM(step_delta), 0) FROM samples
            WHERE strftime('%Y-W%W', ts, 'unixepoch', 'localtime')
                = strftime('%Y-W%W', 'now', 'localtime')
            """
        ).fetchone()
        return row[0]

    def total_steps(self) -> int:
        row = self.conn.execute("SELECT COALESCE(SUM(step_delta), 0) FROM samples").fetchone()
        return row[0]

    def tracking_since(self):
        row = self.conn.execute("SELECT MIN(ts) FROM samples").fetchone()
        return row[0]

    def samples_since(self, ts_start: float):
        """(ts, speed_tenths) pairs, ordered, for distance estimation."""
        return self.conn.execute(
            "SELECT ts, speed_tenths FROM samples WHERE ts >= ? ORDER BY ts",
            (ts_start,),
        ).fetchall()

    def close(self) -> None:
        self.conn.close()


def estimate_distance_km(rows) -> float:
    """rows: [(ts, speed_tenths), ...] ordered by ts. Integrates speed over
    time between consecutive samples (trapezoidal), skipping anything wider
    than MAX_INTEGRATION_GAP_S so a pause or reconnect doesn't get counted
    as distance covered."""
    total_km = 0.0
    for (t0, s0), (t1, s1) in zip(rows, rows[1:]):
        dt = t1 - t0
        if dt <= 0 or dt > MAX_INTEGRATION_GAP_S:
            continue
        avg_kmh = (s0 + s1) / 2.0 / 10.0
        total_km += avg_kmh * dt / 3600.0
    return total_km


class BLEWorker:
    """Owns the asyncio loop + BleakClient on a background thread, same
    approach as controller_ui.py. Additionally computes a reset-aware step
    delta per sample: any decrease in the firmware's own (already
    cross-speed-change-accumulated, see uart_rx.c) step count means a real
    stop or an ESP32 reboot happened, so that sample starts a fresh local
    segment instead of going negative or double-counting."""

    def __init__(self, events: "queue.Queue[tuple]"):
        self.events = events
        self.loop = asyncio.new_event_loop()
        self.client: BleakClient | None = None
        self.thread = threading.Thread(target=self._run_loop, daemon=True)
        self._last_steps: int | None = None

    def start(self) -> None:
        self.thread.start()
        asyncio.run_coroutine_threadsafe(self._connect(), self.loop)

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def _on_disconnect(self, _client: BleakClient) -> None:
        self._last_steps = None  # next reconnect starts a fresh local segment
        self.events.put(("status", "Disconnected."))
        self.events.put(("connected", False))

    def _on_telemetry(self, _sender, data: bytearray) -> None:
        decoded = decode_telemetry(bytes(data))
        if decoded is None:
            return
        tenths_est, steps = decoded

        if self._last_steps is None:
            delta = 0  # first sample this connection: establish baseline only
        elif steps >= self._last_steps:
            delta = steps - self._last_steps
        else:
            delta = steps  # firmware-side reset (real stop or reboot) -- new segment
        self._last_steps = steps

        self.events.put(("telemetry", tenths_est, steps, delta, time.time()))

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
        self._last_steps = None
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


class ControlTab:
    """Play/Stop/Speed Up/Down + live telemetry -- same layout and logic as
    controller_ui.py's App, just living in a Notebook tab instead of owning
    the whole window."""

    def __init__(self, notebook: ttk.Notebook, worker: BLEWorker):
        self.worker = worker
        self.connected = False
        self.running = False
        self.target_tenths = SPEED_MIN_TENTHS

        self.frame = ttk.Frame(notebook)
        pad = {"padx": 10, "pady": 6}

        self.status_var = tk.StringVar(value="Starting...")
        ttk.Label(self.frame, textvariable=self.status_var, wraplength=360).grid(
            row=0, column=0, columnspan=3, sticky="w", **pad
        )

        self.telemetry_var = tk.StringVar(value="Speed: -- km/h   Steps: --")
        ttk.Label(self.frame, textvariable=self.telemetry_var, font=("", 11)).grid(
            row=1, column=0, columnspan=3, sticky="w", **pad
        )

        self.target_var = tk.StringVar(value=self._target_text())
        ttk.Label(self.frame, textvariable=self.target_var, font=("", 20, "bold")).grid(
            row=2, column=0, columnspan=3, pady=(4, 10)
        )

        self.down_btn = ttk.Button(self.frame, text="▼ Speed Down", command=self._on_speed_down)
        self.down_btn.grid(row=3, column=0, **pad)

        self.up_btn = ttk.Button(self.frame, text="▲ Speed Up", command=self._on_speed_up)
        self.up_btn.grid(row=3, column=2, **pad)

        self.play_btn = ttk.Button(self.frame, text="Play", command=self._on_play)
        self.play_btn.grid(row=4, column=0, sticky="ew", **pad)

        self.stop_btn = ttk.Button(self.frame, text="Stop", command=self._on_stop)
        self.stop_btn.grid(row=4, column=2, sticky="ew", **pad)

        self.reconnect_btn = ttk.Button(self.frame, text="Reconnect", command=self._on_reconnect)
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

    def on_status(self, text: str) -> None:
        self.status_var.set(text)

    def on_connected(self, connected: bool) -> None:
        self.connected = connected
        if not connected:
            self.running = False
        self._set_controls_enabled(connected=self.connected, running=self.running)

    def on_telemetry(self, tenths_est: int, steps: int) -> None:
        self.telemetry_var.set(f"Speed: {tenths_est / 10:.1f} km/h   Steps: {steps}")


class HistoryTab:
    """Daily/weekly step totals, an estimated distance, and a bar chart
    over the SQLite log HistoryDB maintains."""

    def __init__(self, notebook: ttk.Notebook, db: HistoryDB):
        self.db = db
        self.view = tk.StringVar(value="daily")

        self.frame = ttk.Frame(notebook)
        pad = {"padx": 10, "pady": 6}

        self.summary_var = tk.StringVar(value="Loading...")
        ttk.Label(self.frame, textvariable=self.summary_var, justify="left", font=("", 11)).grid(
            row=0, column=0, columnspan=3, sticky="w", **pad
        )

        toggle_frame = ttk.Frame(self.frame)
        toggle_frame.grid(row=1, column=0, sticky="w", **pad)
        ttk.Radiobutton(toggle_frame, text="Daily", variable=self.view, value="daily",
                        command=self._refresh_chart).pack(side="left")
        ttk.Radiobutton(toggle_frame, text="Weekly", variable=self.view, value="weekly",
                        command=self._refresh_chart).pack(side="left")

        ttk.Button(self.frame, text="Refresh", command=self.refresh).grid(row=1, column=2, sticky="e", **pad)

        self.figure = Figure(figsize=(6.4, 3.4), dpi=100)
        self.ax = self.figure.add_subplot(111)
        self.canvas = FigureCanvasTkAgg(self.figure, master=self.frame)
        self.canvas.get_tk_widget().grid(row=2, column=0, columnspan=3, sticky="nsew", padx=10, pady=(0, 10))

        self.frame.columnconfigure(0, weight=1)
        self.frame.rowconfigure(2, weight=1)

        self.refresh()

    def refresh(self) -> None:
        self._refresh_summary()
        self._refresh_chart()

    def _refresh_summary(self) -> None:
        now = datetime.now()
        today_start = datetime(now.year, now.month, now.day).timestamp()
        today_km = estimate_distance_km(self.db.samples_since(today_start))

        total_steps = self.db.total_steps()
        since_ts = self.db.tracking_since()
        since_str = "no data yet" if since_ts is None else datetime.fromtimestamp(since_ts).strftime("%Y-%m-%d")

        self.summary_var.set(
            f"Today: {self.db.today_steps()} steps, {today_km:.2f} km\n"
            f"This week: {self.db.current_week_steps()} steps\n"
            f"All-time: {total_steps} steps (tracking since {since_str})"
        )

    def _refresh_chart(self) -> None:
        self.ax.clear()
        if self.view.get() == "daily":
            rows = self.db.daily_totals(days=14)
            labels = [day[5:] for day, _steps in rows]  # MM-DD
            title = "Steps per day (last 14 days)"
        else:
            rows = self.db.weekly_totals(weeks=12)
            labels = [week for week, _steps in rows]
            title = "Steps per week (last 12 weeks)"

        values = [steps or 0 for _label, steps in rows]
        self.ax.bar(labels, values, color="#4C72B0")
        self.ax.set_title(title)
        self.ax.set_ylabel("steps")
        self.ax.tick_params(axis="x", rotation=45, labelsize=8)
        self.figure.tight_layout()
        self.canvas.draw()


class App:
    HISTORY_REFRESH_MS = 30_000

    def __init__(self, root: tk.Tk):
        self.root = root
        root.title("Treadmill")
        root.geometry("640x560")

        self.db = HistoryDB(DB_PATH)
        self.events: "queue.Queue[tuple]" = queue.Queue()
        self.worker = BLEWorker(self.events)

        notebook = ttk.Notebook(root)
        notebook.pack(fill="both", expand=True)

        self.control_tab = ControlTab(notebook, self.worker)
        self.history_tab = HistoryTab(notebook, self.db)
        notebook.add(self.control_tab.frame, text="Control")
        notebook.add(self.history_tab.frame, text="History")

        root.protocol("WM_DELETE_WINDOW", self._on_close)

        self.worker.start()
        self.root.after(100, self._poll_events)
        self.root.after(self.HISTORY_REFRESH_MS, self._refresh_history_periodically)

    def _poll_events(self) -> None:
        try:
            while True:
                kind, *rest = self.events.get_nowait()
                if kind == "status":
                    self.control_tab.on_status(rest[0])
                elif kind == "connected":
                    self.control_tab.on_connected(rest[0])
                elif kind == "telemetry":
                    tenths_est, steps, delta, ts = rest
                    self.control_tab.on_telemetry(tenths_est, steps)
                    self.db.add_sample(ts, tenths_est, delta)
        except queue.Empty:
            pass
        self.root.after(100, self._poll_events)

    def _refresh_history_periodically(self) -> None:
        self.history_tab.refresh()
        self.root.after(self.HISTORY_REFRESH_MS, self._refresh_history_periodically)

    def _on_close(self) -> None:
        self.db.close()
        self.root.destroy()


def main() -> None:
    root = tk.Tk()
    App(root)
    try:
        root.mainloop()
    except KeyboardInterrupt:
        sys.exit(0)


if __name__ == "__main__":
    main()
