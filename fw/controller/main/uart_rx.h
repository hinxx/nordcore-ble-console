#pragma once

#include <stdint.h>

/*
 * BASE->CON receive side: the baseboard's own telemetry, read exactly the
 * way the passive sniffers already do (same frame/checksum logic as
 * fw/frame-sniffer) -- this link is unchanged by moving to a controller,
 * per DESIGN.md's Hardware plan ("needs no change... reused on the
 * controller PCB, not redesigned").
 */

/* Bring up UART2 RX-only on GPIO27 and start the parsing task. Call once
 * from app_main(), after g_console_mutex exists. */
void uart_rx_start(void);

/*
 * Latest decoded telemetry from a valid BASE->CON frame. Per README's byte
 * tables: speed_raw is the noisy mirror of the commanded setpoint (bytes
 * 3:4), steps is the confirmed 1:1 step counter (offset 6, wraps at
 * 0xFF and resets to 0 at a real stop -- see README's "The step counter,
 * confirmed"). Safe to call from any task.
 */
uint16_t uart_rx_last_speed_raw(void);
uint8_t uart_rx_last_steps(void);
