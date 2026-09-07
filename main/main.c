#include <inttypes.h>
#include <stdio.h>

#include "driver/uart.h"
#include "esp_err.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "freertos/task.h"

#define SNIFF_BAUD              1200
#define SNIFF_RX_BUFFER_SIZE    2048

#define LINE_A_UART             UART_NUM_1
#define LINE_A_GPIO             26
#define LINE_A_NAME             "GPIO26"

#define LINE_B_UART             UART_NUM_2
#define LINE_B_GPIO             27
#define LINE_B_NAME             "GPIO27"

#define UART_TASK_STACK         3072
#define UART_TASK_PRIORITY      10

static SemaphoreHandle_t s_console_mutex;

typedef struct {
    uart_port_t uart_num;
    int rx_gpio;
    const char *name;
} sniff_line_t;

static const sniff_line_t s_line_a = {
    .uart_num = LINE_A_UART,
    .rx_gpio = LINE_A_GPIO,
    .name = LINE_A_NAME,
};

static const sniff_line_t s_line_b = {
    .uart_num = LINE_B_UART,
    .rx_gpio = LINE_B_GPIO,
    .name = LINE_B_NAME,
};

static void console_printf(const char *name, int64_t timestamp_us, uint8_t byte)
{
    xSemaphoreTake(s_console_mutex, portMAX_DELAY);
    printf("%012" PRId64 " %s %02X\n", timestamp_us, name, byte);
    fflush(stdout);
    xSemaphoreGive(s_console_mutex);
}

static void sniffer_task(void *arg)
{
    const sniff_line_t *line = (const sniff_line_t *)arg;
    uint8_t byte;

    for (;;) {
        int n = uart_read_bytes(line->uart_num, &byte, 1, portMAX_DELAY);
        if (n == 1) {
            const int64_t now_us = esp_timer_get_time();
            console_printf(line->name, now_us, byte);
        }
    }
}

static void init_sniff_uart(const sniff_line_t *line)
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

    ESP_ERROR_CHECK(uart_driver_install(line->uart_num,
                                        SNIFF_RX_BUFFER_SIZE,
                                        0,
                                        0,
                                        NULL,
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
    printf("Output: <timestamp_us> <source> <hex-byte>\n\n");
    fflush(stdout);

    BaseType_t ok_a = xTaskCreate(sniffer_task,
                                  "sniff_gpio26",
                                  UART_TASK_STACK,
                                  (void *)&s_line_a,
                                  UART_TASK_PRIORITY,
                                  NULL);

    BaseType_t ok_b = xTaskCreate(sniffer_task,
                                  "sniff_gpio27",
                                  UART_TASK_STACK,
                                  (void *)&s_line_b,
                                  UART_TASK_PRIORITY,
                                  NULL);

    if (ok_a != pdPASS || ok_b != pdPASS) {
        printf("ERROR: failed to create sniffer task(s)\n");
        fflush(stdout);
        abort();
    }
}
