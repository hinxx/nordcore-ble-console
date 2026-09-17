#pragma once

#include <stdint.h>

/*
 * CON->BASE transmit side: commands the baseboard, replacing the stock
 * console's control role. See fw/controller/DESIGN.md for the full plan
 * this implements ("start by mimicking the original console as closely
 * as possible").
 */

/* Minimum and maximum commandable speed, in tenths of km/h (0.8-6.0 km/h),
 * matching the confirmed floor/cap in DESIGN.md's "What we know" section. */
#define UART_TX_SPEED_MIN_TENTHS 8
#define UART_TX_SPEED_MAX_TENTHS 60

/* Bring up UART1 TX-only on GPIO25 and start the ramp/transmit task. Call
 * once from app_main(), after g_console_mutex exists. */
void uart_tx_start(void);

/*
 * Command API, safe to call from any task (BLE callbacks included) --
 * these only ever update the target the TX task is ramping toward; the TX
 * task itself owns all actual transmission.
 */

/* Start moving toward the default startup speed (0.8 km/h), same as
 * pressing play on the stock console. */
void uart_tx_cmd_play(void);

/* Start ramping down to a full stop (target speed 0). */
void uart_tx_cmd_stop(void);

/* Command a specific speed, in tenths of km/h. Out-of-range values are
 * clamped to [UART_TX_SPEED_MIN_TENTHS, UART_TX_SPEED_MAX_TENTHS] --
 * matching the confirmed floor/cap, not extrapolating past what's been
 * seen accepted on real hardware. Works whether currently idle, holding,
 * or mid-ramp: the TX task just retargets from wherever it currently is. */
void uart_tx_cmd_set_speed_tenths(uint8_t tenths_km_h);

/* Current raw CON->BASE speed value actually being transmitted right now
 * (not the target -- the value the ramp has reached so far). For
 * diagnostics/telemetry cross-checking only. */
uint16_t uart_tx_current_raw(void);

/* Convert a raw speed value (CON->BASE or BASE->CON -- README confirms the
 * two share the same scale, BASE->CON tracking within ~1-3 units of jitter)
 * to a tenths-km/h estimate by nearest-entry lookup against the same
 * 53-point calibration table SET_SPEED uses, run in reverse. Accurate to
 * that calibration, unlike the coarser raw/750 proportional approximation
 * it replaces. raw==0 (true idle/full stop) reports 0; any other raw
 * outside the table's range clamps to UART_TX_SPEED_MIN_TENTHS/_MAX_TENTHS. */
uint8_t uart_tx_speed_raw_to_tenths(uint16_t raw);
