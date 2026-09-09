#pragma once

#include <stddef.h>
#include <stdint.h>

/*
 * GATT layout for this firmware (see DESIGN.md "BLE console architecture"):
 *
 *   Service: Treadmill Controller          (128-bit UUID, private/testing)
 *     +-- characteristic TELEMETRY  NOTIFY  decoded speed + steps, see below
 *     +-- characteristic CMD        WRITE   PLAY / STOP / SET_SPEED, see below
 *
 * Unlike fw/ble-sniffer's CMD stub (which only logged writes), this CMD
 * characteristic has real effect: it drives uart_tx's command API, which
 * drives what actually gets transmitted onto CON->BASE.
 */

/* Bring up NimBLE, register the GATT service, and start advertising as
 * "TreadmillController". Call once from app_main(), after g_console_mutex
 * exists. */
void ble_gatt_start(void);

/*
 * Push one telemetry update out over the TELEMETRY notify characteristic.
 * Called from uart_rx each time a valid BASE->CON frame is decoded.
 * Record format (5 bytes):
 *
 *   byte 0    0x01 (format version, for future extensibility)
 *   byte 1:2  raw CON->BASE speed value, big-endian (the wire's own units --
 *             see root README's speed calibration table for exact km/h)
 *   byte 3    speed estimate, tenths of km/h (raw/750 approximation --
 *             see README's "Full-range calibration"; a convenience for a
 *             simple display, not as exact as a full table lookup)
 *   byte 4    step count (0-255, wraps; resets to 0 at a real stop --
 *             see README's "The step counter, confirmed")
 *
 * A no-op if no central is connected or subscribed, same best-effort
 * semantics as fw/ble-sniffer's RX_LOG. Safe to call from any task.
 */
void ble_gatt_notify_telemetry(uint16_t speed_raw, uint8_t steps);
