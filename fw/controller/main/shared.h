#pragma once

#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"

/*
 * Shared across main.c / uart_tx.c / uart_rx.c / ble_gatt.c so console
 * output from all of them (telemetry, TX activity, BLE lifecycle,
 * command logging) never interleaves -- same reasoning as
 * fw/ble-sniffer's g_console_mutex.
 */
extern SemaphoreHandle_t g_console_mutex;

/* printf-style helper that takes g_console_mutex around the call and
 * flushes stdout, so every module logs through one funnel instead of
 * duplicating the take/print/flush/give dance in each file. */
void console_log(const char *fmt, ...);
