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
    Tracks two step counts side by side: the hardware's own (unreliable at
    some speeds) and a speed-derived estimate from a configurable height,
    since "did a footfall happen" is a much harder sensing problem than
    "how fast is the belt moving."

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

Closing the window minimizes to an XFCE4/AppIndicator system tray icon
instead of quitting -- the BLE connection and history logging keep running
in the background. The icon's color and hover tooltip reflect live state
(disconnected / connected-idle / running, plus current speed and this
session's step count); the tray menu has "Show Treadmill" (also the
default/click action) and "Quit" (the only way to actually exit).

Requires: pip install bleak matplotlib pystray pillow
"""

import asyncio
import queue
import sqlite3
import subprocess
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

import pystray
from PIL import Image, ImageDraw

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

# HistoryDB.settings keys for ControlTab's/HistoryTab's persisted preferences.
SETTING_PLAY_SPEED_KM_H = "play_speed_km_h"
SETTING_AUTO_STOP_S = "auto_stop_s"
SETTING_HEIGHT_CM = "height_cm"

# When integrating speed over time to estimate distance, skip any gap
# between consecutive samples bigger than this -- a pause, a disconnect, a
# day boundary -- not actual walking. Comfortably above the real ~200-220ms
# BASE->CON heartbeat cadence (README/DESIGN.md), even with some BLE jitter.
MAX_INTEGRATION_GAP_S = 3.0

# Tray icon dot colors -- this app has no distinct "paused" state (only
# connected/idle and running), so the tray simplifies to three states.
TRAY_COLOR_DISCONNECTED = "#9e9e9e"
TRAY_COLOR_IDLE = "#4c72b0"
TRAY_COLOR_RUNNING = "#2e8b57"


def make_tray_image(color: str) -> "Image.Image":
    """A simple filled dot on a transparent square, crisp at panel sizes."""
    size = 64
    margin = 6
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.ellipse((margin, margin, size - margin, size - margin), fill=color, outline="white", width=3)
    return img


# The hardware step count can be unreliable (missed/duplicate footfalls at
# certain speeds) -- see the discussion that prompted this. STEP_LENGTH_FACTOR
# is the standard pedometer-calibration constant relating a person's height
# to their walking step length (one footfall, not a full 2-step stride):
# step_length_cm ~= height_cm * 0.415. It's an approximation (real step
# length isn't perfectly speed-independent -- people lengthen their stride
# somewhat at faster paces too), good enough for a secondary/comparison
# estimate, not a replacement for the real sensor.
STEP_LENGTH_FACTOR = 0.415
DEFAULT_HEIGHT_CM = 170.0


def height_to_step_length_m(height_cm: float) -> float:
    return height_cm * STEP_LENGTH_FACTOR / 100.0


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
    cycles, app restarts, and reconnects.

    Each row carries two independent step counts: step_delta (the
    hardware's own reported count, reset-aware per BLEWorker) and
    est_step_delta (a speed-derived estimate, see height_to_step_length_m
    -- a comparison metric for when the hardware sensor looks wrong, not a
    replacement for it)."""

    _METRIC_COLUMNS = {"hardware": "step_delta", "estimated": "est_step_delta"}

    def __init__(self, path: Path):
        self.conn = sqlite3.connect(path)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS samples (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts REAL NOT NULL,
                speed_tenths INTEGER NOT NULL,
                step_delta INTEGER NOT NULL,
                est_step_delta REAL NOT NULL DEFAULT 0
            )
            """
        )
        # Migration for a samples table created before est_step_delta existed.
        existing_cols = {row[1] for row in self.conn.execute("PRAGMA table_info(samples)")}
        if "est_step_delta" not in existing_cols:
            self.conn.execute("ALTER TABLE samples ADD COLUMN est_step_delta REAL NOT NULL DEFAULT 0")
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_samples_ts ON samples(ts)")
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """
        )
        self.conn.commit()
        # Seed from the table's own last row, not just None -- otherwise every
        # app restart would silently lose one interval's worth of estimated
        # steps on the very first sample (nothing to integrate against yet
        # in a fresh process, even though the previous session's last sample
        # is sitting right there in the table).
        self._last_sample: tuple[float, int] | None = self.conn.execute(
            "SELECT ts, speed_tenths FROM samples ORDER BY id DESC LIMIT 1"
        ).fetchone()

    def add_sample(self, ts: float, speed_tenths: int, step_delta: int, step_length_m: float) -> None:
        est_step_delta = 0.0
        if self._last_sample is not None:
            prev_ts, prev_speed_tenths = self._last_sample
            dt = ts - prev_ts
            if 0 < dt <= MAX_INTEGRATION_GAP_S and step_length_m > 0:
                avg_kmh = (prev_speed_tenths + speed_tenths) / 2.0 / 10.0
                distance_m = avg_kmh * 1000.0 * dt / 3600.0
                est_step_delta = distance_m / step_length_m
        self._last_sample = (ts, speed_tenths)

        self.conn.execute(
            "INSERT INTO samples (ts, speed_tenths, step_delta, est_step_delta) VALUES (?, ?, ?, ?)",
            (ts, speed_tenths, step_delta, est_step_delta),
        )
        self.conn.commit()

    def get_setting(self, key: str, default: str) -> str:
        row = self.conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row[0] if row is not None else default

    def set_setting(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        self.conn.commit()

    def daily_totals(self, days: int, metric: str = "hardware"):
        column = self._METRIC_COLUMNS[metric]
        rows = self.conn.execute(
            f"""
            SELECT date(ts, 'unixepoch', 'localtime') AS day, SUM({column}) AS steps
            FROM samples GROUP BY day ORDER BY day DESC LIMIT ?
            """,
            (days,),
        ).fetchall()
        return list(reversed(rows))  # oldest -> newest, for left-to-right charting

    def weekly_totals(self, weeks: int, metric: str = "hardware"):
        column = self._METRIC_COLUMNS[metric]
        rows = self.conn.execute(
            f"""
            SELECT strftime('%Y-W%W', ts, 'unixepoch', 'localtime') AS week, SUM({column}) AS steps
            FROM samples GROUP BY week ORDER BY week DESC LIMIT ?
            """,
            (weeks,),
        ).fetchall()
        return list(reversed(rows))

    def today_steps(self, metric: str = "hardware") -> float:
        column = self._METRIC_COLUMNS[metric]
        row = self.conn.execute(
            f"""
            SELECT COALESCE(SUM({column}), 0) FROM samples
            WHERE date(ts, 'unixepoch', 'localtime') = date('now', 'localtime')
            """
        ).fetchone()
        return row[0]

    def current_week_steps(self, metric: str = "hardware") -> float:
        column = self._METRIC_COLUMNS[metric]
        row = self.conn.execute(
            f"""
            SELECT COALESCE(SUM({column}), 0) FROM samples
            WHERE strftime('%Y-W%W', ts, 'unixepoch', 'localtime')
                = strftime('%Y-W%W', 'now', 'localtime')
            """
        ).fetchone()
        return row[0]

    def total_steps(self, metric: str = "hardware") -> float:
        column = self._METRIC_COLUMNS[metric]
        row = self.conn.execute(f"SELECT COALESCE(SUM({column}), 0) FROM samples").fetchone()
        return row[0]

    def hourly_totals_today(self, metric: str = "hardware"):
        """24 (hour label, steps) pairs for today, 00-23, zero-filled for
        hours with no activity -- a day-total bar loses when the walking
        actually happened; this shows it."""
        column = self._METRIC_COLUMNS[metric]
        rows = self.conn.execute(
            f"""
            SELECT CAST(strftime('%H', ts, 'unixepoch', 'localtime') AS INTEGER) AS hour,
                   SUM({column}) AS steps
            FROM samples
            WHERE date(ts, 'unixepoch', 'localtime') = date('now', 'localtime')
            GROUP BY hour
            """
        ).fetchall()
        by_hour = dict(rows)
        return [(f"{h:02d}", by_hour.get(h, 0)) for h in range(24)]

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


def _clear_stale_connection(name: str) -> bool:
    """Best-effort self-heal for tools/BLE_CONNECTION_RELIABILITY.md's cause
    #1: blueman-manager (or anything else on this host) grabbing a GATT
    connection to the device in the background. The board won't
    re-advertise while it thinks it's connected
    (fw/controller/main/ble_gatt.c's ADV_COMPLETE handling), so a stale
    OS-side connection makes BleakScanner fail to find the device at all --
    this has to run *before* scanning, not in response to a failed scan.
    Mirrors the exact manual fix from that doc: find the MAC by name, check
    if BlueZ already thinks it's connected, and if so disconnect it.
    Returns True only if it actually cleared something, so callers can stay
    quiet in the common case. Silently does nothing if bluetoothctl isn't
    available -- this is a convenience, not a requirement to run the app."""
    try:
        listing = subprocess.run(
            ["bluetoothctl", "devices"], capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    mac = None
    for line in listing.stdout.splitlines():
        parts = line.split(maxsplit=2)
        if len(parts) == 3 and parts[0] == "Device" and parts[2] == name:
            mac = parts[1]
            break
    if mac is None:
        return False
    try:
        info = subprocess.run(
            ["bluetoothctl", "info", mac], capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    if "Connected: yes" not in info.stdout:
        return False
    try:
        subprocess.run(
            ["bluetoothctl", "disconnect", mac], capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return True


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
        elif tenths_est == 0:
            delta = steps  # genuine reset: firmware only zeroes its total at a real stop
        else:
            # steps decreased while still moving -- the firmware's own step
            # count is an 8-bit value that wraps at 256 (fw/controller's
            # uart_rx.c only zeroes it at speed_raw==0, so this can't be a
            # reset), not a new segment. Credit the full wrap so a long
            # continuous walk doesn't lose ~a whole 256-step cycle every
            # time this rolls over.
            delta = (256 - self._last_steps) + steps
        self._last_steps = steps

        self.events.put(("telemetry", tenths_est, steps, delta, time.time()))

    async def _connect(self) -> None:
        if await asyncio.to_thread(_clear_stale_connection, DEVICE_NAME):
            self.events.put(("status", "Cleared a stale connection (e.g. blueman-manager) before scanning..."))
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

    async def _disconnect_current(self) -> None:
        # Reconnecting (or exiting) while still holding a live client used
        # to just drop the Python reference -- but that never told BlueZ to
        # actually close the old connection, leaving it open at the OS
        # level. The board correctly won't advertise while it still thinks
        # something's connected (see fw/controller/main/ble_gatt.c's
        # ADV_COMPLETE fix), so a rescan would silently fail to find it,
        # forever -- the exact same stale-connection symptom a leftover
        # bluetoothctl/GNOME Bluetooth-panel session caused, just
        # self-inflicted, whether by clicking Reconnect a second time or by
        # just closing the app without disconnecting first.
        if self.client is not None:
            try:
                await self.client.disconnect()
            except Exception:  # noqa: BLE001 -- best-effort; proceed regardless
                pass
            self.client = None

    def reconnect(self) -> None:
        asyncio.run_coroutine_threadsafe(self._reconnect(), self.loop)

    async def _reconnect(self) -> None:
        await self._disconnect_current()
        await self._connect()

    def disconnect_blocking(self, timeout: float = 3.0) -> None:
        """Best-effort synchronous disconnect for a clean app shutdown --
        called from Tkinter's main thread right before the window closes,
        so it blocks briefly (up to timeout) rather than letting the
        process exit mid-disconnect and leave the connection dangling."""
        if self.client is None:
            return
        future = asyncio.run_coroutine_threadsafe(self._disconnect_current(), self.loop)
        try:
            future.result(timeout=timeout)
        except Exception:  # noqa: BLE001 -- don't block shutdown on a slow/failed disconnect
            pass

    async def _send(self, *payload: int) -> None:
        if self.client is None or not self.client.is_connected:
            self.events.put(("status", "Not connected -- command dropped."))
            return
        try:
            await self.client.write_gatt_char(CMD_CHAR_UUID, bytes(payload), response=True)
        except Exception as exc:  # noqa: BLE001 -- report write failures, don't crash the loop
            self.events.put(("status", f"Write failed: {exc}"))

    def send_stop(self) -> None:
        asyncio.run_coroutine_threadsafe(self._send(CMD_STOP), self.loop)

    def send_speed(self, tenths: int) -> None:
        asyncio.run_coroutine_threadsafe(self._send(CMD_SET_SPEED, tenths), self.loop)


class ControlTab:
    """Play/Stop/Speed Up/Down + live telemetry -- same layout and logic as
    controller_ui.py's App, just living in a Notebook tab instead of owning
    the whole window. Adds an auto-stop safety watchdog controller_ui.py
    doesn't have: PLAY starts the belt rolling even if nobody steps on it,
    so if the reported step count hasn't changed for AUTO_STOP_CHECK_MS-
    granularity intervals covering the configured timeout, this sends STOP
    on its own."""

    AUTO_STOP_CHECK_MS = 500
    DEFAULT_AUTO_STOP_S = 10.0
    AUTO_STOP_WARNING_S = 2.0  # show the red countdown once no step for this long

    def __init__(
        self, notebook: ttk.Notebook, worker: BLEWorker,
        play_speed_var: tk.StringVar, auto_stop_var: tk.StringVar,
    ):
        self.worker = worker
        self.play_speed_var = play_speed_var
        self.auto_stop_var = auto_stop_var
        self.connected = False
        self.running = False
        self.target_tenths = SPEED_MIN_TENTHS
        self._last_seen_steps: int | None = None
        self._last_step_change_time = time.monotonic()

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

        self.countdown_var = tk.StringVar(value="")
        ttk.Label(
            self.frame, textvariable=self.countdown_var, font=("", 13, "bold"), foreground="red"
        ).grid(row=3, column=0, columnspan=3, pady=(0, 6))

        self.down_btn = ttk.Button(self.frame, text="▼ Speed Down", command=self._on_speed_down)
        self.down_btn.grid(row=4, column=0, **pad)

        self.up_btn = ttk.Button(self.frame, text="▲ Speed Up", command=self._on_speed_up)
        self.up_btn.grid(row=4, column=2, **pad)

        self.play_btn = ttk.Button(self.frame, text="Play", command=self._on_play)
        self.play_btn.grid(row=5, column=0, sticky="ew", **pad)

        self.stop_btn = ttk.Button(self.frame, text="Stop", command=self._on_stop)
        self.stop_btn.grid(row=5, column=2, sticky="ew", **pad)

        self.reconnect_btn = ttk.Button(self.frame, text="Reconnect", command=self._on_reconnect)
        self.reconnect_btn.grid(row=6, column=0, columnspan=3, sticky="ew", **pad)

        # Play speed and auto-stop timeout live on the Settings tab (owned
        # and persisted by SettingsTab) -- play_speed_var/auto_stop_var
        # here are that tab's own StringVars, shared by reference, not
        # copies, so editing them there is immediately live here too.

        self._set_controls_enabled(connected=False, running=False)
        self.frame.after(self.AUTO_STOP_CHECK_MS, self._check_auto_stop)

    def _target_text(self) -> str:
        return f"Target: {self.target_tenths / 10:.1f} km/h"

    def _set_controls_enabled(self, *, connected: bool, running: bool) -> None:
        self.play_btn.state(["!disabled"] if connected else ["disabled"])
        self.stop_btn.state(["!disabled"] if (connected and running) else ["disabled"])
        speed_ok = connected and running
        self.up_btn.state(["!disabled"] if speed_ok else ["disabled"])
        self.down_btn.state(["!disabled"] if speed_ok else ["disabled"])

    def _play_speed_tenths(self) -> int:
        try:
            km_h = float(self.play_speed_var.get())
        except ValueError:
            km_h = SPEED_MIN_TENTHS / 10
        return min(max(round(km_h * 10), SPEED_MIN_TENTHS), SPEED_MAX_TENTHS)

    def _on_play(self) -> None:
        # SET_SPEED sent from a full stop engages the ramp exactly like
        # PLAY does (uart_tx.c's ramp task only compares current vs.
        # target, regardless of which command set it) -- so this reaches
        # the configured preset directly instead of always landing on the
        # firmware's fixed 0.8 km/h PLAY floor and needing manual Speed Up
        # clicks afterward.
        self.target_tenths = self._play_speed_tenths()
        self.target_var.set(self._target_text())
        self.worker.send_speed(self.target_tenths)
        self.running = True
        self._last_seen_steps = None
        self._last_step_change_time = time.monotonic()
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
        if self.running and steps != self._last_seen_steps:
            self._last_step_change_time = time.monotonic()
        self._last_seen_steps = steps

    def _auto_stop_timeout_s(self) -> float:
        try:
            value = float(self.auto_stop_var.get())
        except ValueError:
            value = -1
        # A blank/garbage entry falls back to the default rather than
        # silently disabling the safety check -- this is a safety feature,
        # so a bad typo in the box shouldn't be able to turn it off.
        return value if value > 0 else self.DEFAULT_AUTO_STOP_S

    def _check_auto_stop(self) -> None:
        if self.running:
            elapsed = time.monotonic() - self._last_step_change_time
            timeout = self._auto_stop_timeout_s()
            if elapsed >= timeout:
                self.worker.send_stop()
                self.running = False
                self._set_controls_enabled(connected=self.connected, running=self.running)
                self.status_var.set(f"Auto-stopped: no steps detected for {timeout:.0f}s.")
                self.countdown_var.set("")
            elif elapsed >= self.AUTO_STOP_WARNING_S:
                self.countdown_var.set(f"Stopping in {timeout - elapsed:.1f}s -- step on the belt!")
            else:
                self.countdown_var.set("")
        else:
            self.countdown_var.set("")
        self.frame.after(self.AUTO_STOP_CHECK_MS, self._check_auto_stop)


class HistoryTab:
    """Daily/weekly step totals (hardware and speed-estimated side by
    side), an estimated distance, and a bar chart over the SQLite log
    HistoryDB maintains."""

    def __init__(self, notebook: ttk.Notebook, db: HistoryDB):
        self.db = db
        self.view = tk.StringVar(value="today")
        self.metric = tk.StringVar(value="hardware")

        self.frame = ttk.Frame(notebook)
        pad = {"padx": 10, "pady": 6}

        self.summary_var = tk.StringVar(value="Loading...")
        ttk.Label(self.frame, textvariable=self.summary_var, justify="left", font=("", 11)).grid(
            row=0, column=0, columnspan=3, sticky="w", **pad
        )

        # Height (for the estimated-steps metric) lives on the Settings tab.

        toggle_frame = ttk.Frame(self.frame)
        toggle_frame.grid(row=1, column=0, sticky="w", **pad)
        ttk.Radiobutton(toggle_frame, text="Today (by hour)", variable=self.view, value="today",
                        command=self._refresh_chart).pack(side="left")
        ttk.Radiobutton(toggle_frame, text="Daily", variable=self.view, value="daily",
                        command=self._refresh_chart).pack(side="left")
        ttk.Radiobutton(toggle_frame, text="Weekly", variable=self.view, value="weekly",
                        command=self._refresh_chart).pack(side="left")
        ttk.Radiobutton(toggle_frame, text="Hardware steps", variable=self.metric, value="hardware",
                        command=self._refresh_chart).pack(side="left", padx=(16, 0))
        ttk.Radiobutton(toggle_frame, text="Estimated steps", variable=self.metric, value="estimated",
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

        since_ts = self.db.tracking_since()
        since_str = "no data yet" if since_ts is None else datetime.fromtimestamp(since_ts).strftime("%Y-%m-%d")

        def both(fn) -> str:
            hw = fn(metric="hardware")
            est = fn(metric="estimated")
            return f"{hw:.0f} hardware / {est:.0f} estimated"

        self.summary_var.set(
            f"Today: {both(self.db.today_steps)} steps, {today_km:.2f} km\n"
            f"This week: {both(self.db.current_week_steps)} steps\n"
            f"All-time: {both(self.db.total_steps)} steps (tracking since {since_str})"
        )

    def _refresh_chart(self) -> None:
        self.ax.clear()
        metric = self.metric.get()
        metric_label = "hardware" if metric == "hardware" else "estimated"
        view = self.view.get()
        if view == "today":
            rows = self.db.hourly_totals_today(metric=metric)
            labels = [hour for hour, _steps in rows]
            title = f"{metric_label.capitalize()} steps by hour (today)"
            tick_step = 2  # 24 bars is crowded -- label every other hour
        elif view == "daily":
            rows = self.db.daily_totals(days=14, metric=metric)
            labels = [day[5:] for day, _steps in rows]  # MM-DD
            title = f"{metric_label.capitalize()} steps per day (last 14 days)"
            tick_step = 1
        else:
            rows = self.db.weekly_totals(weeks=12, metric=metric)
            labels = [week for week, _steps in rows]
            title = f"{metric_label.capitalize()} steps per week (last 12 weeks)"
            tick_step = 1

        values = [steps or 0 for _label, steps in rows]
        self.ax.bar(labels, values, color="#4C72B0")
        self.ax.set_title(title)
        self.ax.set_ylabel("steps")
        self.ax.set_xticks(range(0, len(labels), tick_step))
        self.ax.set_xticklabels(labels[::tick_step])
        self.ax.tick_params(axis="x", rotation=45, labelsize=8)
        self.figure.tight_layout()
        self.canvas.draw()


class SettingsTab:
    """All persisted preferences (play speed, auto-stop timeout, height) in
    one place: owns the StringVars, loads them from HistoryDB.settings at
    startup, and persists on every edit via a trace -- ControlTab and
    HistoryTab just receive the vars by reference (not copies), so editing
    a value here is immediately live wherever else it's used."""

    def __init__(self, notebook: ttk.Notebook, db: HistoryDB):
        self.db = db
        self.frame = ttk.Frame(notebook)
        pad = {"padx": 10, "pady": 8}

        default_play_speed = f"{SPEED_MIN_TENTHS / 10:.1f}"
        self.play_speed_var = tk.StringVar(
            value=db.get_setting(SETTING_PLAY_SPEED_KM_H, default_play_speed)
        )
        self._persist_on_write(self.play_speed_var, SETTING_PLAY_SPEED_KM_H)

        default_auto_stop = str(int(ControlTab.DEFAULT_AUTO_STOP_S))
        self.auto_stop_var = tk.StringVar(
            value=db.get_setting(SETTING_AUTO_STOP_S, default_auto_stop)
        )
        self._persist_on_write(self.auto_stop_var, SETTING_AUTO_STOP_S)

        self.height_var = tk.StringVar(
            value=db.get_setting(SETTING_HEIGHT_CM, str(int(DEFAULT_HEIGHT_CM)))
        )
        self._persist_on_write(self.height_var, SETTING_HEIGHT_CM)

        row = 0
        ttk.Label(self.frame, text="Play ramps up to:").grid(row=row, column=0, sticky="w", **pad)
        ttk.Spinbox(
            self.frame, from_=SPEED_MIN_TENTHS / 10, to=SPEED_MAX_TENTHS / 10,
            increment=0.1, width=6, format="%.1f", textvariable=self.play_speed_var,
        ).grid(row=row, column=1, sticky="w", **pad)
        ttk.Label(self.frame, text="km/h").grid(row=row, column=2, sticky="w", **pad)

        row += 1
        ttk.Label(self.frame, text="Auto-stop if no steps for:").grid(row=row, column=0, sticky="w", **pad)
        ttk.Spinbox(
            self.frame, from_=1, to=300, increment=1, width=6, textvariable=self.auto_stop_var,
        ).grid(row=row, column=1, sticky="w", **pad)
        ttk.Label(self.frame, text="seconds").grid(row=row, column=2, sticky="w", **pad)

        row += 1
        ttk.Label(self.frame, text="Height (for estimated steps):").grid(row=row, column=0, sticky="w", **pad)
        ttk.Spinbox(
            self.frame, from_=100, to=220, increment=1, width=6, textvariable=self.height_var,
        ).grid(row=row, column=1, sticky="w", **pad)
        ttk.Label(self.frame, text="cm").grid(row=row, column=2, sticky="w", **pad)

        row += 1
        self.step_length_var = tk.StringVar()
        ttk.Label(self.frame, textvariable=self.step_length_var, foreground="#666666").grid(
            row=row, column=0, columnspan=3, sticky="w", padx=10
        )
        self.height_var.trace_add("write", lambda *_: self._refresh_step_length_label())
        self._refresh_step_length_label()

        self.frame.columnconfigure(3, weight=1)

    def _persist_on_write(self, var: tk.StringVar, key: str) -> None:
        # Persisted immediately on every edit (typed or via the spin
        # arrows) so a value set here is already loaded next time the app
        # starts -- no separate Save action to remember.
        var.trace_add("write", lambda *_: self.db.set_setting(key, var.get()))

    def _refresh_step_length_label(self) -> None:
        try:
            height_cm = float(self.height_var.get())
        except ValueError:
            height_cm = DEFAULT_HEIGHT_CM
        step_length_cm = height_to_step_length_m(height_cm) * 100
        self.step_length_var.set(f"-> ~{step_length_cm:.0f} cm/step")


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

        self.settings_tab = SettingsTab(notebook, self.db)
        self.control_tab = ControlTab(
            notebook, self.worker, self.settings_tab.play_speed_var, self.settings_tab.auto_stop_var
        )
        self.history_tab = HistoryTab(notebook, self.db)
        notebook.add(self.control_tab.frame, text="Control")
        notebook.add(self.history_tab.frame, text="History")
        notebook.add(self.settings_tab.frame, text="Settings")

        # Session step tracking for the tray tooltip's "Last session" line --
        # reset to 0 when ControlTab.running goes False->True, frozen into
        # _last_session_steps on the True->False edge. Detected in
        # _poll_events since running can change (Play/Stop/auto-stop)
        # without a telemetry event in the same tick.
        self._was_running = False
        self._session_steps = 0
        self._last_session_steps = 0
        self._last_tenths_est = 0

        self._tray_last_color: str | None = None
        self._tray_last_title: str | None = None
        self.tray_icon = self._build_tray_icon()
        self.tray_icon.run_detached()

        # Closing the window minimizes to tray instead of quitting; only the
        # tray's own "Quit" item calls _quit().
        root.protocol("WM_DELETE_WINDOW", self._hide_to_tray)

        self.worker.start()
        self.root.after(100, self._poll_events)
        self.root.after(self.HISTORY_REFRESH_MS, self._refresh_history_periodically)

    def _tray_color(self) -> str:
        if not self.control_tab.connected:
            return TRAY_COLOR_DISCONNECTED
        return TRAY_COLOR_RUNNING if self.control_tab.running else TRAY_COLOR_IDLE

    def _tray_title(self) -> str:
        if not self.control_tab.connected:
            return "TreadmillController\nDisconnected"
        if self.control_tab.running:
            speed = self._last_tenths_est / 10
            return (
                f"TreadmillController\nRunning at {speed:.1f} km/h\n"
                f"This session: {self._session_steps} steps"
            )
        last = f"\nLast session: {self._last_session_steps} steps" if self._last_session_steps else ""
        return f"TreadmillController\nIdle (connected){last}"

    def _build_tray_icon(self) -> pystray.Icon:
        menu = pystray.Menu(
            pystray.MenuItem("Show Treadmill", self._on_tray_show, default=True),
            pystray.MenuItem("Quit", self._on_tray_quit),
        )
        return pystray.Icon("treadmill", make_tray_image(self._tray_color()), self._tray_title(), menu)

    def _on_tray_show(self, icon, item) -> None:
        # Runs on pystray's own icon thread -- marshal onto the Tk thread via
        # the same events queue BLEWorker already uses, rather than touching
        # Tk widgets directly from here.
        self.events.put(("tray_show",))

    def _on_tray_quit(self, icon, item) -> None:
        self.events.put(("tray_quit",))

    def _show_window(self) -> None:
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    def _hide_to_tray(self) -> None:
        self.root.withdraw()

    def _update_tray(self) -> None:
        color = self._tray_color()
        if color != self._tray_last_color:
            self.tray_icon.icon = make_tray_image(color)
            self._tray_last_color = color
        title = self._tray_title()
        if title != self._tray_last_title:
            self.tray_icon.title = title
            self._tray_last_title = title

    def _step_length_m(self) -> float:
        try:
            height_cm = float(self.settings_tab.height_var.get())
        except ValueError:
            height_cm = DEFAULT_HEIGHT_CM
        if height_cm <= 0:
            height_cm = DEFAULT_HEIGHT_CM
        return height_to_step_length_m(height_cm)

    def _poll_events(self) -> None:
        running_now = self.control_tab.running
        if running_now and not self._was_running:
            self._session_steps = 0
        elif not running_now and self._was_running:
            self._last_session_steps = self._session_steps
        self._was_running = running_now

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
                    self.db.add_sample(ts, tenths_est, delta, self._step_length_m())
                    self._last_tenths_est = tenths_est
                    if self.control_tab.running:
                        self._session_steps += delta
                elif kind == "tray_show":
                    self._show_window()
                elif kind == "tray_quit":
                    self._quit()
                    return
        except queue.Empty:
            pass
        self._update_tray()
        self.root.after(100, self._poll_events)

    def _refresh_history_periodically(self) -> None:
        self.history_tab.refresh()
        self.root.after(self.HISTORY_REFRESH_MS, self._refresh_history_periodically)

    def _quit(self) -> None:
        self.tray_icon.stop()
        self.worker.disconnect_blocking()
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
