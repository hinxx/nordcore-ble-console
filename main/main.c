#include <inttypes.h>
#include <stdio.h>

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
#define LINE_A_NAME             "GPIO26"

#define LINE_B_UART             UART_NUM_2
#define LINE_B_GPIO             27
#define LINE_B_NAME             "GPIO27"

#define UART_TASK_STACK         4096
#define UART_TASK_PRIORITY      10

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

static void console_print_byte(const char *name, int64_t timestamp_us, uint8_t byte)
{
    xSemaphoreTake(s_console_mutex, portMAX_DELAY);
    printf("%012" PRId64 " %s %02X\n", timestamp_us, name, byte);
    fflush(stdout);
    xSemaphoreGive(s_console_mutex);
}

static void console_print_error(const char *name, const char *what)
{
    const int64_t timestamp_us = esp_timer_get_time();
    xSemaphoreTake(s_console_mutex, portMAX_DELAY);
    printf("%012" PRId64 " %s ERROR %s\n", timestamp_us, name, what);
    fflush(stdout);
    xSemaphoreGive(s_console_mutex);
}

/*
 * Drain and timestamp the bytes reported by one UART_DATA event.
 *
 * Each byte gets its own esp_timer_get_time() call at the moment this task
 * pulls it out of the driver's RX ring buffer. That is software receive
 * time -- when the task retrieved the byte -- not the true wire-arrival
 * time of that byte's first or last UART bit; it also does not distinguish
 * bytes that were already buffered when the event fired from ones read a
 * loop iteration later. At 1200 baud 8N2 one character takes ~9.17 ms to
 * transmit, so ordinary scheduler jitter (tens to low hundreds of
 * microseconds) is far smaller than the inter-byte spacing and should not
 * affect protocol reverse engineering. These timestamps are NOT
 * oscilloscope-grade wire timing; do not use them as such.
 */
static void drain_data_event(const sniff_line_t *line, size_t available)
{
    uint8_t byte;

    for (size_t i = 0; i < available; i++) {
        int n = uart_read_bytes(line->uart_num, &byte, 1, portMAX_DELAY);
        if (n == 1) {
            console_print_byte(line->name, esp_timer_get_time(), byte);
        }
    }
}

static void sniffer_task(void *arg)
{
    const sniff_line_t *line = (const sniff_line_t *)arg;
    uart_event_t event;

    for (;;) {
        if (xQueueReceive(line->event_queue, &event, portMAX_DELAY) != pdTRUE) {
            continue;
        }

        switch (event.type) {
        case UART_DATA:
            drain_data_event(line, event.size);
            break;

        case UART_FIFO_OVF:
            /*
             * HW FIFO overflowed before the driver ISR could drain it.
             * Bytes are gone; make that loss explicit in the capture
             * rather than letting the stream look continuous.
             */
            console_print_error(line->name, "FIFO_OVF bytes_lost=unknown");
            uart_flush_input(line->uart_num);
            xQueueReset(line->event_queue);
            console_print_error(line->name, "RX_FLUSH");
            break;

        case UART_BUFFER_FULL:
            /* Driver's RX ring buffer overflowed; same recovery as above. */
            console_print_error(line->name, "BUFFER_FULL bytes_lost=unknown");
            uart_flush_input(line->uart_num);
            xQueueReset(line->event_queue);
            console_print_error(line->name, "RX_FLUSH");
            break;

        case UART_FRAME_ERR:
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
             * meaningful for this baseline raw-byte sniffer. */
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
    printf("UART1 RX: GPIO26, 1200 8N2\n");
    printf("UART2 RX: GPIO27, 1200 8N2\n");
    printf("TX: disabled / not routed\n");
    printf("Output: <timestamp_us> <source> <hex-byte>\n");
    printf("Errors: <timestamp_us> <source> ERROR <reason>\n\n");
    fflush(stdout);

    BaseType_t ok_a = xTaskCreate(sniffer_task,
                                  "sniff_gpio26",
                                  UART_TASK_STACK,
                                  &s_line_a,
                                  UART_TASK_PRIORITY,
                                  NULL);

    BaseType_t ok_b = xTaskCreate(sniffer_task,
                                  "sniff_gpio27",
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
