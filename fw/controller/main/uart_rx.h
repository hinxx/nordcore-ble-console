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
 * 3:4). steps is an accumulated total across the whole run since the last
 * real stop (or power-up) -- built from the baseboard's own per-segment
 * counter (offset 6), which real-hardware testing showed resets to 0 not
 * just at a real stop but on every mid-run speed change too; this getter
 * reconstructs the run-wide total the stock console must show, banking
 * each finished segment instead of losing it. Still an 8-bit wraparound
 * value, same as the raw field it's built from. Safe to call from any
 * task.
 */
uint16_t uart_rx_last_speed_raw(void);
uint8_t uart_rx_last_steps(void);
