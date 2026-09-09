#include <inttypes.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "driver/uart.h"
#include "esp_err.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"
#include "freertos/task.h"

#include "ble_gatt.h"
#include "shared.h"
#include "uart_rx.h"

#define RX_UART_NUM              UART_NUM_2
#define RX_GPIO                  27
#define RX_BAUD                  1200
#define RX_BUFFER_SIZE           2048
#define RX_EVENT_QUEUE_LEN       20

/*
 * Frame parser: identical logic to fw/frame-sniffer's (same 68/LEN/
 * payload/CS/43 framing, same checksum, same MANGLED-reason set) --
 * deliberately not reinvented, since that parser was independently
 * verified against thousands of real BASE->CON frames across log1-log11.
 * See root README.md "Protocol observations" for the evidence.
 */
#define FRAME_START_BYTE         0x68
#define FRAME_END_BYTE           0x43
#define FRAME_MAX_LEN_FIELD      62
#define FRAME_MAX_TOTAL          (2 + FRAME_MAX_LEN_FIELD)
#define FRAME_STRAY_MAX          16
#define FRAME_IDLE_TIMEOUT_MS    750

/* The one BASE->CON frame shape ever observed (LEN=0x0C, 14 bytes total).
 * Telemetry is only decoded from frames matching this exact shape. */
#define BASE_CON_FRAME_LEN       14
#define BASE_CON_SPEED_HI_IDX    3
#define BASE_CON_SPEED_LO_IDX    4
#define BASE_CON_STEPS_IDX       6

typedef struct {
    uint8_t buf[FRAME_MAX_TOTAL];
    size_t count;
    int64_t start_ts;

    uint8_t stray[FRAME_STRAY_MAX];
    size_t stray_count;
    int64_t stray_ts;
} frame_parser_t;

static volatile uint16_t s_last_speed_raw = 0;
static volatile uint8_t s_last_steps = 0;

uint16_t uart_rx_last_speed_raw(void)
{
    return s_last_speed_raw;
}

uint8_t uart_rx_last_steps(void)
{
    return s_last_steps;
}

static void print_frame_mangled(int64_t ts, const char *reason, const uint8_t *bytes, size_t len)
{
    char hex[FRAME_MAX_TOTAL * 3 + 1];
    size_t pos = 0;
    for (size_t i = 0; i < len; i++) {
        int n = snprintf(hex + pos, sizeof(hex) - pos, "%02X ", bytes[i]);
        if (n <= 0 || (size_t)n >= sizeof(hex) - pos) {
            break;
        }
        pos += (size_t)n;
    }
    if (pos > 0) {
        hex[pos - 1] = '\0';
    } else {
        hex[0] = '\0';
    }
    console_log("%012" PRId64 " BASE->CON MANGLED %s %s\n", ts, reason, hex);
}

static void flush_stray(frame_parser_t *p)
{
    if (p->stray_count == 0) {
        return;
    }
    print_frame_mangled(p->stray_ts, "stray_bytes", p->stray, p->stray_count);
    p->stray_count = 0;
}

static void abandon_in_progress(frame_parser_t *p, const char *reason)
{
    if (p->count > 0) {
        print_frame_mangled(p->start_ts, reason, p->buf, p->count);
        p->count = 0;
    }
    flush_stray(p);
}

/* Decodes and stores telemetry from a valid, checksum-passed frame, and
 * pushes it out over BLE. Only known-shape frames get decoded -- anything
 * else that still passes checksum is logged but not treated as telemetry,
 * since only one BASE->CON frame shape has ever been observed. */
static void handle_ok_frame(int64_t ts, const uint8_t *buf, size_t len)
{
    if (len != BASE_CON_FRAME_LEN) {
        console_log("%012" PRId64 " BASE->CON OK (unexpected length %u, not decoded)\n",
                    ts, (unsigned)len);
        return;
    }

    uint16_t speed_raw = (uint16_t)((buf[BASE_CON_SPEED_HI_IDX] << 8) | buf[BASE_CON_SPEED_LO_IDX]);
    uint8_t steps = buf[BASE_CON_STEPS_IDX];

    s_last_speed_raw = speed_raw;
    s_last_steps = steps;

    console_log("%012" PRId64 " BASE->CON OK speed_raw=%u steps=%u\n", ts, speed_raw, steps);

    ble_gatt_notify_telemetry(speed_raw, steps);
}

static void frame_feed_byte(frame_parser_t *p, uint8_t byte, int64_t ts)
{
    if (p->count == 0) {
        if (byte != FRAME_START_BYTE) {
            if (p->stray_count >= FRAME_STRAY_MAX) {
                flush_stray(p);
            }
            if (p->stray_count == 0) {
                p->stray_ts = ts;
            }
            p->stray[p->stray_count++] = byte;
            return;
        }
        flush_stray(p);
        p->start_ts = ts;
        p->buf[p->count++] = byte;
        return;
    }

    if (p->count == 1) {
        p->buf[p->count++] = byte;
        if (byte < 2) {
            print_frame_mangled(p->start_ts, "len_too_small", p->buf, p->count);
            p->count = 0;
            return;
        }
        if ((size_t)(2 + byte) > FRAME_MAX_TOTAL) {
            print_frame_mangled(p->start_ts, "len_too_large", p->buf, p->count);
            p->count = 0;
            return;
        }
        return;
    }

    p->buf[p->count++] = byte;
    const size_t expected_total = 2 + (size_t)p->buf[1];
    if (p->count < expected_total) {
        return;
    }

    const uint8_t end_byte = p->buf[p->count - 1];
    const uint8_t claimed_cs = p->buf[p->count - 2];
    uint8_t computed_cs = 0;
    for (size_t i = 1; i < p->count - 2; i++) {
        computed_cs = (uint8_t)(computed_cs + p->buf[i]);
    }

    if (end_byte != FRAME_END_BYTE) {
        char reason[24];
        snprintf(reason, sizeof(reason), "bad_end=%02X", end_byte);
        print_frame_mangled(p->start_ts, reason, p->buf, p->count);
    } else if (claimed_cs != computed_cs) {
        char reason[40];
        snprintf(reason, sizeof(reason), "bad_checksum computed=%02X", computed_cs);
        print_frame_mangled(p->start_ts, reason, p->buf, p->count);
    } else {
        handle_ok_frame(p->start_ts, p->buf, p->count);
    }
    p->count = 0;
}

static void drain_data_event(frame_parser_t *parser, size_t available)
{
    uint8_t byte;
    for (size_t i = 0; i < available; i++) {
        int n = uart_read_bytes(RX_UART_NUM, &byte, 1, portMAX_DELAY);
        if (n == 1) {
            frame_feed_byte(parser, byte, esp_timer_get_time());
        }
    }
}

static void rx_task(void *arg)
{
    (void)arg;
    QueueHandle_t event_queue = NULL;

    const uart_config_t config = {
        .baud_rate = RX_BAUD,
        .data_bits = UART_DATA_8_BITS,
        .parity = UART_PARITY_DISABLE,
        .stop_bits = UART_STOP_BITS_2,
        .flow_ctrl = UART_HW_FLOWCTRL_DISABLE,
        .rx_flow_ctrl_thresh = 0,
        .source_clk = UART_SCLK_DEFAULT,
    };
    ESP_ERROR_CHECK(uart_param_config(RX_UART_NUM, &config));
    ESP_ERROR_CHECK(uart_set_pin(RX_UART_NUM,
                                 UART_PIN_NO_CHANGE,
                                 RX_GPIO,
                                 UART_PIN_NO_CHANGE,
                                 UART_PIN_NO_CHANGE));
    ESP_ERROR_CHECK(uart_driver_install(RX_UART_NUM,
                                        RX_BUFFER_SIZE,
                                        0,
                                        RX_EVENT_QUEUE_LEN,
                                        &event_queue,
                                        0));

    uart_event_t event;
    frame_parser_t parser;
    memset(&parser, 0, sizeof(parser));

    for (;;) {
        const bool frame_pending = (parser.count > 0) || (parser.stray_count > 0);
        const TickType_t wait_ticks = frame_pending ? pdMS_TO_TICKS(FRAME_IDLE_TIMEOUT_MS) : portMAX_DELAY;

        if (xQueueReceive(event_queue, &event, wait_ticks) != pdTRUE) {
            abandon_in_progress(&parser, "timeout_incomplete");
            continue;
        }

        switch (event.type) {
        case UART_DATA:
            drain_data_event(&parser, event.size);
            break;

        case UART_FIFO_OVF:
            console_log("BASE->CON ERROR FIFO_OVF bytes_lost=unknown\n");
            abandon_in_progress(&parser, "interrupted_by_overflow");
            uart_flush_input(RX_UART_NUM);
            xQueueReset(event_queue);
            console_log("BASE->CON ERROR RX_FLUSH\n");
            break;

        case UART_BUFFER_FULL:
            console_log("BASE->CON ERROR BUFFER_FULL bytes_lost=unknown\n");
            abandon_in_progress(&parser, "interrupted_by_overflow");
            uart_flush_input(RX_UART_NUM);
            xQueueReset(event_queue);
            console_log("BASE->CON ERROR RX_FLUSH\n");
            break;

        case UART_FRAME_ERR:
            console_log("BASE->CON ERROR FRAME_ERR\n");
            break;

        case UART_PARITY_ERR:
            console_log("BASE->CON ERROR PARITY_ERR\n");
            break;

        case UART_BREAK:
            console_log("BASE->CON ERROR BREAK\n");
            break;

        default:
            break;
        }
    }
}

void uart_rx_start(void)
{
    BaseType_t ok = xTaskCreate(rx_task, "base_con_rx", 4096, NULL, 10, NULL);
    if (ok != pdPASS) {
        console_log("ERROR: failed to create BASE->CON RX task\n");
        abort();
    }
}
