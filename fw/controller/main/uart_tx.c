#include <stdbool.h>
#include <stdlib.h>
#include <string.h>

#include "driver/uart.h"
#include "esp_err.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

#include "shared.h"
#include "uart_tx.h"

#define TX_UART_NUM   UART_NUM_1
#define TX_GPIO       25
#define TX_BAUD       1200

/*
 * CON->BASE frame (10 bytes, LEN=0x08): 68 08 <state> <flag> <hi> <lo> 00 14 <CS> 43.
 * See DESIGN.md "What we know" and root README's byte tables -- this is the
 * one frame shape this whole file ever builds.
 */
#define FRAME_LEN            10
#define FRAME_START_BYTE     0x68
#define FRAME_LEN_FIELD      0x08
#define FRAME_FIXED_BYTE6    0x00
#define FRAME_FIXED_BYTE7    0x14
#define FRAME_END_BYTE       0x43

/* state byte: 0x21 observed during active/holding running, 0x20 at idle
 * and during transitions -- still "Partial" in README's byte tables, so
 * this mimics the observed pattern rather than claiming to fully
 * understand it (see DESIGN.md Open Questions). */
#define STATE_IDLE_OR_TRANSITION  0x20
#define STATE_HOLDING_NONZERO     0x21

/* flag byte: 0x50 whenever any non-idle speed is engaged (ramping or
 * holding), 0x00 only at true idle -- matches every capture in README. */
#define FLAG_ENGAGED  0x50
#define FLAG_IDLE     0x00

/*
 * Speed calibration table: index i is (0.8 + 0.1*i) km/h -> raw CON->BASE
 * value. Straight from README's "Full-range calibration corrects /775"
 * (log8, 53 individually-confirmed points against the console's own
 * display) -- ground truth, not the /775 approximation that table
 * superseded. Covers the full confirmed range: 0.8 km/h (floor) to
 * 6.0 km/h (the raw-4444 cap).
 */
static const uint16_t SPEED_RAW_TABLE[53] = {
    620,  697,  775,  852,  930,  1003, 1080, 1157, 1234, 1305,  /* 0.8-1.7 */
    1382, 1459, 1536, 1612, 1681, 1758, 1834, 1911, 1978, 2054,  /* 1.8-2.7 */
    2130, 2207, 2272, 2348, 2424, 2500, 2575, 2651, 2714, 2790,  /* 2.8-3.7 */
    2865, 2941, 3016, 3092, 3153, 3228, 3303, 3378, 3453, 3528,  /* 3.8-4.7 */
    3587, 3662, 3736, 3811, 3886, 3961, 4017, 4092, 4166, 4241,  /* 4.8-5.7 */
    4315, 4389, 4444,                                            /* 5.8-6.0 */
};

/*
 * Ramp/transmit tuning. Step size (~74-78 units/step) is well-established
 * from README's captures; the real-time cadence between steps is NOT
 * (DESIGN.md Open Questions: "only step sizes are well-established, not
 * their timing" -- the BLE captures that generated most of the ramp
 * evidence carry no timestamps). These constants mimic the *shape*
 * faithfully and pick a deliberately unhurried cadence for the timing
 * that isn't pinned down -- safe to tune once real hardware bring-up
 * (DESIGN.md "Staged rollout") gives firmer data.
 */
#define RAMP_STEP_UNITS       76     /* midpoint of the observed ~74-78 range */

/*
 * Steady one-frame-every-200ms cadence, cursor-measured directly off a real
 * console's own CON->BASE traffic on a scope (200ms exactly, repeatable) --
 * replaces an earlier "burst of 6 frames, 15ms apart, then a ~1.15s silent
 * gap" model that turned out to be a misreading of older byte-level capture
 * data. That burst-then-silence shape bore no resemblance to this: real
 * CON->BASE traffic is one frame roughly every 200ms, continuously, much
 * like BASE->CON's own well-established 220ms heartbeat tick -- not long
 * silences that could plausibly read as "console went away" to whatever's
 * on the other end.
 */
#define FRAME_PERIOD_MS       200

/*
 * The real console never ramps up from a literal 0 in RAMP_STEP_UNITS
 * steps -- every real play-test capture on file (logs/log4-ble-play-stop.txt
 * through logs/log8-ble-play-step-inc-to-max-speed.txt, five independent
 * sessions) shows the very first non-idle CON->BASE speed value jumping
 * straight to a fixed 250 (0x00FA) before continuing in ordinary
 * RAMP_STEP_UNITS-sized steps from there. Mimicked here as the first step
 * whenever a ramp starts from a full stop.
 */
#define INITIAL_ENGAGE_JUMP_RAW  250

static uint16_t speed_tenths_to_raw(uint8_t tenths_km_h)
{
    if (tenths_km_h < UART_TX_SPEED_MIN_TENTHS) {
        tenths_km_h = UART_TX_SPEED_MIN_TENTHS;
    } else if (tenths_km_h > UART_TX_SPEED_MAX_TENTHS) {
        tenths_km_h = UART_TX_SPEED_MAX_TENTHS;
    }
    return SPEED_RAW_TABLE[tenths_km_h - UART_TX_SPEED_MIN_TENTHS];
}

static SemaphoreHandle_t s_target_mutex;
static uint16_t s_target_raw = 0;    /* 0 means "stopped/idle" */
static uint16_t s_current_raw = 0;   /* owned solely by the TX task -- no lock needed */

static uint16_t get_target_raw(void)
{
    uint16_t v;
    xSemaphoreTake(s_target_mutex, portMAX_DELAY);
    v = s_target_raw;
    xSemaphoreGive(s_target_mutex);
    return v;
}

static void set_target_raw(uint16_t v)
{
    xSemaphoreTake(s_target_mutex, portMAX_DELAY);
    s_target_raw = v;
    xSemaphoreGive(s_target_mutex);
}

void uart_tx_cmd_play(void)
{
    console_log("TX: PLAY (target -> %u, 0.8 km/h)\n", SPEED_RAW_TABLE[0]);
    set_target_raw(SPEED_RAW_TABLE[0]);
}

void uart_tx_cmd_stop(void)
{
    console_log("TX: STOP (target -> 0)\n");
    set_target_raw(0);
}

void uart_tx_cmd_set_speed_tenths(uint8_t tenths_km_h)
{
    uint16_t raw = speed_tenths_to_raw(tenths_km_h);
    console_log("TX: SET_SPEED %u.%u km/h (target -> %u)\n",
                tenths_km_h / 10, tenths_km_h % 10, raw);
    set_target_raw(raw);
}

uint16_t uart_tx_current_raw(void)
{
    return s_current_raw; /* single aligned 16-bit read/write on ESP32; TX task is the sole writer */
}

uint8_t uart_tx_speed_raw_to_tenths(uint16_t raw)
{
    const int last = (int)(sizeof(SPEED_RAW_TABLE) / sizeof(SPEED_RAW_TABLE[0])) - 1;

    /* raw==0 is true idle/full stop (README: decrease walks all the way
     * down to a real 0, not a nonzero floor) -- must report 0.0 km/h, not
     * clamp to the table's 0.8 km/h floor like an out-of-range low value
     * would. */
    if (raw == 0) {
        return 0;
    }
    if (raw <= SPEED_RAW_TABLE[0]) {
        return UART_TX_SPEED_MIN_TENTHS;
    }
    if (raw >= SPEED_RAW_TABLE[last]) {
        return UART_TX_SPEED_MAX_TENTHS;
    }

    /* Table is monotonically increasing -- binary search for the first
     * entry >= raw, then snap to whichever neighbor raw is numerically
     * closer to. */
    int lo = 0, hi = last;
    while (lo < hi) {
        int mid = (lo + hi) / 2;
        if (SPEED_RAW_TABLE[mid] < raw) {
            lo = mid + 1;
        } else {
            hi = mid;
        }
    }
    if (lo > 0 && (raw - SPEED_RAW_TABLE[lo - 1]) < (SPEED_RAW_TABLE[lo] - raw)) {
        lo -= 1;
    }

    return (uint8_t)(UART_TX_SPEED_MIN_TENTHS + lo);
}

/* Builds the one CON->BASE frame shape this firmware ever sends. */
static void build_frame(uint8_t *out, uint8_t state, uint8_t flag, uint16_t speed_raw)
{
    out[0] = FRAME_START_BYTE;
    out[1] = FRAME_LEN_FIELD;
    out[2] = state;
    out[3] = flag;
    out[4] = (uint8_t)(speed_raw >> 8);
    out[5] = (uint8_t)(speed_raw & 0xFF);
    out[6] = FRAME_FIXED_BYTE6;
    out[7] = FRAME_FIXED_BYTE7;

    uint8_t cs = 0;
    for (int i = 1; i <= 7; i++) {
        cs = (uint8_t)(cs + out[i]);
    }
    out[8] = cs;
    out[9] = FRAME_END_BYTE;
}

/*
 * Single continuous loop that unifies play/stop/set-speed: it just keeps
 * stepping s_current_raw toward whatever s_target_raw currently is (one
 * INITIAL_ENGAGE_JUMP_RAW-sized first step off a full stop, RAMP_STEP_UNITS
 * increments thereafter), and sends one frame every FRAME_PERIOD_MS. Idle
 * is the degenerate case (target=current=0). This deliberately never jumps
 * straight to the full target -- see DESIGN.md's "Ramp behavior" note:
 * every real speed change observed on the wire was a smooth ramp, and
 * whether the baseboard accepts a direct jump is untested, so this mimics
 * the one behavior actually confirmed safe.
 */
static void tx_task(void *arg)
{
    (void)arg;

    for (;;) {
        uint16_t target = get_target_raw();

        if (s_current_raw < target) {
            /* See INITIAL_ENGAGE_JUMP_RAW above: a ramp starting from a
             * full stop takes one big first step, not a RAMP_STEP_UNITS
             * one, matching every real capture of the console's own
             * play behavior. */
            uint16_t next = (s_current_raw == 0)
                                 ? INITIAL_ENGAGE_JUMP_RAW
                                 : (uint16_t)(s_current_raw + RAMP_STEP_UNITS);
            s_current_raw = (next > target) ? target : next;
        } else if (s_current_raw > target) {
            s_current_raw = (s_current_raw > RAMP_STEP_UNITS)
                                 ? (uint16_t)(s_current_raw - RAMP_STEP_UNITS)
                                 : 0;
        }

        const bool engaged = (s_current_raw > 0) || (target > 0);
        const bool holding = (s_current_raw > 0) && (s_current_raw == target);

        uint8_t frame[FRAME_LEN];
        build_frame(frame,
                    holding ? STATE_HOLDING_NONZERO : STATE_IDLE_OR_TRANSITION,
                    engaged ? FLAG_ENGAGED : FLAG_IDLE,
                    s_current_raw);
        uart_write_bytes(TX_UART_NUM, (const char *)frame, FRAME_LEN);

        vTaskDelay(pdMS_TO_TICKS(FRAME_PERIOD_MS));
    }
}

void uart_tx_start(void)
{
    s_target_mutex = xSemaphoreCreateMutex();
    if (s_target_mutex == NULL) {
        abort();
    }

    /*
     * 8O1, not 8N2 -- both give 11 bits/character (1 start + 8 data + 1
     * parity + 1 stop, vs. 1 start + 8 data + 0 parity + 2 stop), which is
     * why this went unnoticed for so long: content, cadence, and even the
     * fixed "stop" bit right before the next start bit all looked identical
     * either way. Confirmed by measuring the bit immediately after the 8
     * data bits directly off real captures: on the stock console it varies
     * exactly with odd parity of that byte's data (0 for 68/08/20/43, which
     * have an odd number of 1-bits; 1 for 00/14/3C, which have an even
     * number) -- not a fixed stop bit at all, the way our own then-8N2 TX
     * always showed 1 there regardless of data. See
     * RX_TX_LEVEL_INVESTIGATION.md's "UART framing" section. The ESP32's
     * hardware UART generates the parity bit automatically from this config
     * alone; no other code here needs to change.
     */
    const uart_config_t config = {
        .baud_rate = TX_BAUD,
        .data_bits = UART_DATA_8_BITS,
        .parity = UART_PARITY_ODD,
        .stop_bits = UART_STOP_BITS_1,
        .flow_ctrl = UART_HW_FLOWCTRL_DISABLE,
        .rx_flow_ctrl_thresh = 0,
        .source_clk = UART_SCLK_DEFAULT,
    };
    ESP_ERROR_CHECK(uart_param_config(TX_UART_NUM, &config));

    /* TX-only: RX pin left unrouted (DESIGN.md "Line-by-line UART plan" --
     * simplified to one plain RX line and one plain TX line, no loopback). */
    ESP_ERROR_CHECK(uart_set_pin(TX_UART_NUM,
                                 TX_GPIO,
                                 UART_PIN_NO_CHANGE,
                                 UART_PIN_NO_CHANGE,
                                 UART_PIN_NO_CHANGE));

    /* tx_buffer_size=0 -> uart_write_bytes() blocks until the HW FIFO can
     * take it, which is fine here: frames are tiny (10 bytes) and bursts
     * already have their own inter-frame delay. rx_buffer_size is still
     * nonzero because uart_driver_install() requires it even though this
     * UART's RX pin is never routed to a GPIO -- the buffer is simply
     * never used. */
    ESP_ERROR_CHECK(uart_driver_install(TX_UART_NUM, 256, 0, 0, NULL, 0));

    /*
     * DESIGN.md "TX signal level": TX now goes through a TXB0104 bidirectional
     * level translator (3.3V ESP32 side -> 5V baseboard side), replacing the
     * earlier BC546 common-emitter shifter. A translator doesn't invert, so
     * unlike that BC546 stage, no uart_set_line_inverse() call is needed here
     * -- the UART's own idle-high output reaches the baseboard unchanged.
     */

    BaseType_t ok = xTaskCreate(tx_task, "con_base_tx", 4096, NULL, 10, NULL);
    if (ok != pdPASS) {
        console_log("ERROR: failed to create CON->BASE TX task\n");
        abort();
    }
}
