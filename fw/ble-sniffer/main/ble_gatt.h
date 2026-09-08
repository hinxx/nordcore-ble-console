#pragma once

#include <stddef.h>
#include <stdint.h>

#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"

/*
 * Shared with main.c so BLE lifecycle log lines (connect/disconnect/
 * subscribe, all rare) never interleave with the sniffer tasks' own
 * MANGLED/ERROR console lines. Created in app_main() before either the
 * UART tasks or ble_sniffer_start() run.
 */
extern SemaphoreHandle_t g_console_mutex;

/* Direction byte prefixed onto each RX_LOG notification record. */
#define BLE_SNIFFER_DIR_BASE_TO_CON  0x01
#define BLE_SNIFFER_DIR_CON_TO_BASE  0x02

/*
 * Bring up NimBLE, register the "Treadmill Sniffer" GATT service (RX_LOG
 * notify + CMD write, see ble_gatt.c), and start advertising. Call once
 * from app_main(), after g_console_mutex exists.
 */
void ble_sniffer_start(void);

/*
 * Push one complete, checksum-valid treadmill frame out over the RX_LOG
 * notify characteristic, as a compact binary record:
 *
 *   byte 0    direction (BLE_SNIFFER_DIR_*)
 *   byte 1    frame_len
 *   byte 2..  the raw frame bytes, unmodified
 *
 * A no-op if no central is connected or subscribed -- the frame is simply
 * not delivered, the same best-effort semantics as any BLE notification
 * with no subscriber. Safe to call from any task: NimBLE's ble_gatts_*
 * calls are documented safe to invoke from any task context, not just the
 * host task.
 */
void ble_sniffer_notify_frame(uint8_t direction, const uint8_t *frame, size_t frame_len);
