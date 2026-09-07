#include <inttypes.h>
#include <stdio.h>
#include <string.h>

#include "driver/uart.h"
#include "esp_err.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"
#include "freertos/semphr.h"
#include "freertos/task.h"

#define SNIFF_BAUD              1200
#define SNIFF_RX_BUFFER_SIZE    2048
#define SNIFF_EVENT_QUEUE_LEN   20

#define LINE_A_UART             UART_NUM_1
#define LINE_A_GPIO             26
/* GPIO26 taps the console/HC32L130's TX pin: console/HC32L130 -> baseboard. */
#define LINE_A_NAME             "CON->BASE"

#define LINE_B_UART             UART_NUM_2
#define LINE_B_GPIO             27
/* GPIO27 taps the baseboard's TX pin: baseboard -> console/HC32L130. */
#define LINE_B_NAME             "BASE->CON"

#define UART_TASK_STACK         4096
#define UART_TASK_PRIORITY      10

/*
 * Frame shape observed on both directions (see README.md "Protocol
 * observations"):
 *
 *   68  LEN  [ ... LEN-2 payload bytes ... ]  CS  43
 *   ^                                         ^   ^
 *   start                                     |   fixed end byte
 *                                              8-bit sum of LEN+payload, mod 256
 *
 * LEN counts every byte after itself: payload + CS + the end byte. That
 * theory was verified against a full hardware capture (log1.txt): with
 * total = 2 + LEN, every real frame in that log (53/53 on GPIO27, 66/66 on
 * GPIO26) parses with a matching checksum and end byte; the only leftover,
 * unparsed bytes are the capture's own start/end boundaries (mid-frame when
 * the sniffer began/stopped listening), not framing failures.
 */
#define FRAME_START_BYTE         0x68
#define FRAME_END_BYTE           0x43
#define FRAME_MAX_LEN_FIELD      62               /* generous headroom over the 8/12 seen so far */
#define FRAME_MAX_TOTAL          (2 + FRAME_MAX_LEN_FIELD)
#define FRAME_STRAY_MAX          16
/* 750 ms comfortably exceeds FRAME_MAX_TOTAL's worst-case transmit time at
 * 1200 baud 8N2 (~9.17 ms/byte * 64 bytes =~ 587 ms), so it only fires on a
 * genuine stall, not a slow-but-legitimate long frame. */
#define FRAME_IDLE_TIMEOUT_MS    750

static SemaphoreHandle_t s_console_mutex;

typedef struct {
    uart_port_t uart_num;
    int rx_gpio;
    const char *name;
    QueueHandle_t event_queue;
} sniff_line_t;

static sniff_line_t s_line_a = {
    .uart_num = LINE_A_UART,
    .rx_gpio = LINE_A_GPIO,
    .name = LINE_A_NAME,
    .event_queue = NULL,
};

static sniff_line_t s_line_b = {
    .uart_num = LINE_B_UART,
    .rx_gpio = LINE_B_GPIO,
    .name = LINE_B_NAME,
    .event_queue = NULL,
};

/* Per-line frame reassembly state. One instance lives on each sniffer_task's
 * own stack, so the two lines never share state -- no locking needed here. */
typedef struct {
    uint8_t buf[FRAME_MAX_TOTAL];
    size_t count;                /* bytes accumulated for the in-progress frame */
    int64_t start_ts;            /* timestamp of buf[0] (the 0x68) */

    uint8_t stray[FRAME_STRAY_MAX];
    size_t stray_count;          /* bytes that didn't fit any frame (not yet flushed) */
    int64_t stray_ts;            /* timestamp of stray[0] */
} frame_parser_t;

static void console_print_error(const char *name, const char *what)
{
    const int64_t timestamp_us = esp_timer_get_time();
    xSemaphoreTake(s_console_mutex, portMAX_DELAY);
    printf("%012" PRId64 " %s ERROR %s\n", timestamp_us, name, what);
    fflush(stdout);
    xSemaphoreGive(s_console_mutex);
}

/*
 * Print one line for a complete, checksum-valid frame ("OK ...") or for
 * anything that didn't parse as one ("MANGLED <reason> ..."), so a mangled
 * capture is never mistaken for a real message downstream. `bytes` is
 * whatever was actually captured -- a full frame for OK, or a partial/stray
 * run for MANGLED.
 */
static void print_frame_line(const char *name, int64_t ts, const char *status,
                              const char *reason, const uint8_t *bytes, size_t len)
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
        hex[pos - 1] = '\0'; /* drop the trailing space */
    } else {
        hex[0] = '\0';
    }

    xSemaphoreTake(s_console_mutex, portMAX_DELAY);
    if (reason != NULL) {
        printf("%012" PRId64 " %s %s %s %s\n", ts, name, status, reason, hex);
    } else {
        printf("%012" PRId64 " %s %s %s\n", ts, name, status, hex);
    }
    fflush(stdout);
    xSemaphoreGive(s_console_mutex);
}

static inline void print_frame_ok(const char *name, int64_t ts, const uint8_t *bytes, size_t len)
{
    print_frame_line(name, ts, "OK", NULL, bytes, len);
}

static inline void print_frame_mangled(const char *name, int64_t ts, const char *reason,
                                        const uint8_t *bytes, size_t len)
{
    print_frame_line(name, ts, "MANGLED", reason, bytes, len);
}

static void flush_stray(frame_parser_t *p, const sniff_line_t *line)
{
    if (p->stray_count == 0) {
        return;
    }
    print_frame_mangled(line->name, p->stray_ts, "stray_bytes", p->stray, p->stray_count);
    p->stray_count = 0;
}

/* Drop any in-progress frame and unflushed stray bytes as MANGLED, tagged
 * with `reason`. Used when the driver reports data loss or when the line
 * has gone idle mid-frame -- either way, what's buffered can no longer be
 * trusted as a real message. */
static void abandon_in_progress(frame_parser_t *p, const sniff_line_t *line, const char *reason)
{
    if (p->count > 0) {
        print_frame_mangled(line->name, p->start_ts, reason, p->buf, p->count);
        p->count = 0;
    }
    flush_stray(p, line);
}

static void frame_feed_byte(frame_parser_t *p, const sniff_line_t *line, uint8_t byte, int64_t ts)
{
    if (p->count == 0) {
        if (byte != FRAME_START_BYTE) {
            if (p->stray_count >= FRAME_STRAY_MAX) {
                flush_stray(p, line);
            }
            if (p->stray_count == 0) {
                p->stray_ts = ts;
            }
            p->stray[p->stray_count++] = byte;
            return;
        }
        flush_stray(p, line);
        p->start_ts = ts;
        p->buf[p->count++] = byte;
        return;
    }

    if (p->count == 1) {
        /* LEN byte: counts everything after itself (payload + CS + end byte).
         * A valid frame needs at least a CS and an end byte after LEN, so
         * LEN must be >= 2. */
        p->buf[p->count++] = byte;
        if (byte < 2) {
            print_frame_mangled(line->name, p->start_ts, "len_too_small", p->buf, p->count);
            p->count = 0;
            return;
        }
        if ((size_t)(2 + byte) > FRAME_MAX_TOTAL) {
            print_frame_mangled(line->name, p->start_ts, "len_too_large", p->buf, p->count);
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
        print_frame_mangled(line->name, p->start_ts, reason, p->buf, p->count);
    } else if (claimed_cs != computed_cs) {
        char reason[40];
        snprintf(reason, sizeof(reason), "bad_checksum computed=%02X", computed_cs);
        print_frame_mangled(line->name, p->start_ts, reason, p->buf, p->count);
    } else {
        print_frame_ok(line->name, p->start_ts, p->buf, p->count);
    }
    p->count = 0;
}

/*
 * Feed every byte reported by one UART_DATA event into the frame parser.
 * Each byte's esp_timer_get_time() is software receive time -- when this
 * task pulled it from the driver's ring buffer, not true wire-arrival time
 * (see README.md "Message timing"); only buf[0]'s timestamp is kept, as the
 * frame's timestamp.
 */
static void drain_data_event(const sniff_line_t *line, frame_parser_t *parser, size_t available)
{
    uint8_t byte;

    for (size_t i = 0; i < available; i++) {
        int n = uart_read_bytes(line->uart_num, &byte, 1, portMAX_DELAY);
        if (n == 1) {
            frame_feed_byte(parser, line, byte, esp_timer_get_time());
        }
    }
}

static void sniffer_task(void *arg)
{
    const sniff_line_t *line = (const sniff_line_t *)arg;
    uart_event_t event;
    frame_parser_t parser;
    memset(&parser, 0, sizeof(parser));

    for (;;) {
        const bool frame_pending = (parser.count > 0) || (parser.stray_count > 0);
        const TickType_t wait_ticks = frame_pending ? pdMS_TO_TICKS(FRAME_IDLE_TIMEOUT_MS) : portMAX_DELAY;

        if (xQueueReceive(line->event_queue, &event, wait_ticks) != pdTRUE) {
            /* Nothing arrived within the idle timeout: whatever is
             * buffered is stale (the far side went quiet mid-frame, or a
             * byte was simply lost). Report it and resync. */
            abandon_in_progress(&parser, line, "timeout_incomplete");
            continue;
        }

        switch (event.type) {
        case UART_DATA:
            drain_data_event(line, &parser, event.size);
            break;

        case UART_FIFO_OVF:
            /*
             * HW FIFO overflowed before the driver ISR could drain it.
             * Bytes are gone; make that loss explicit in the capture
             * rather than letting the stream look continuous.
             */
            console_print_error(line->name, "FIFO_OVF bytes_lost=unknown");
            abandon_in_progress(&parser, line, "interrupted_by_overflow");
            uart_flush_input(line->uart_num);
            xQueueReset(line->event_queue);
            console_print_error(line->name, "RX_FLUSH");
            break;

        case UART_BUFFER_FULL:
            /* Driver's RX ring buffer overflowed; same recovery as above. */
            console_print_error(line->name, "BUFFER_FULL bytes_lost=unknown");
            abandon_in_progress(&parser, line, "interrupted_by_overflow");
            uart_flush_input(line->uart_num);
            xQueueReset(line->event_queue);
            console_print_error(line->name, "RX_FLUSH");
            break;

        case UART_FRAME_ERR:
            /* The offending byte still lands in the ring buffer; if it
             * corrupted an in-progress frame, checksum validation will
             * catch that and report MANGLED on its own. */
            console_print_error(line->name, "FRAME_ERR");
            break;

        case UART_PARITY_ERR:
            console_print_error(line->name, "PARITY_ERR");
            break;

        case UART_BREAK:
            console_print_error(line->name, "BREAK");
            break;

        default:
            /* Other event types (pattern match, wakeup, ...) are not
             * meaningful for this sniffer. */
            break;
        }
    }
}

static void init_sniff_uart(sniff_line_t *line)
{
    const uart_config_t config = {
        .baud_rate = SNIFF_BAUD,
        .data_bits = UART_DATA_8_BITS,
        .parity = UART_PARITY_DISABLE,
        .stop_bits = UART_STOP_BITS_2,
        .flow_ctrl = UART_HW_FLOWCTRL_DISABLE,
        .rx_flow_ctrl_thresh = 0,
        .source_clk = UART_SCLK_DEFAULT,
    };

    ESP_ERROR_CHECK(uart_param_config(line->uart_num, &config));

    /*
     * RX-only. UART_PIN_NO_CHANGE for TX means the sniffer firmware does not
     * route a UART transmitter onto any external GPIO.
     */
    ESP_ERROR_CHECK(uart_set_pin(line->uart_num,
                                 UART_PIN_NO_CHANGE,
                                 line->rx_gpio,
                                 UART_PIN_NO_CHANGE,
                                 UART_PIN_NO_CHANGE));

    /*
     * queue_size > 0 with a non-NULL uart_queue out-param is what makes
     * the driver report UART_FRAME_ERR / UART_PARITY_ERR / UART_FIFO_OVF /
     * UART_BUFFER_FULL to this task at all; with queue_size 0 those events
     * are silently dropped inside the driver.
     */
    ESP_ERROR_CHECK(uart_driver_install(line->uart_num,
                                        SNIFF_RX_BUFFER_SIZE,
                                        0,
                                        SNIFF_EVENT_QUEUE_LEN,
                                        &line->event_queue,
                                        0));
}

void app_main(void)
{
    s_console_mutex = xSemaphoreCreateMutex();
    if (s_console_mutex == NULL) {
        abort();
    }

    init_sniff_uart(&s_line_a);
    init_sniff_uart(&s_line_b);

    printf("\nESP32 treadmill UART sniffer\n");
    printf("ESP-IDF target: ESP32\n");
    printf("UART1 RX: GPIO26 (%s), 1200 8N2\n", LINE_A_NAME);
    printf("UART2 RX: GPIO27 (%s), 1200 8N2\n", LINE_B_NAME);
    printf("TX: disabled / not routed\n");
    printf("Output: <timestamp_us> <direction> OK <hex bytes...>\n");
    printf("        <timestamp_us> <direction> MANGLED <reason> <hex bytes...>\n");
    printf("Errors: <timestamp_us> <direction> ERROR <reason>\n\n");
    fflush(stdout);

    BaseType_t ok_a = xTaskCreate(sniffer_task,
                                  "sniff_con_base",
                                  UART_TASK_STACK,
                                  &s_line_a,
                                  UART_TASK_PRIORITY,
                                  NULL);

    BaseType_t ok_b = xTaskCreate(sniffer_task,
                                  "sniff_base_con",
                                  UART_TASK_STACK,
                                  &s_line_b,
                                  UART_TASK_PRIORITY,
                                  NULL);

    if (ok_a != pdPASS || ok_b != pdPASS) {
        printf("ERROR: failed to create sniffer task(s)\n");
        fflush(stdout);
        abort();
    }
}
