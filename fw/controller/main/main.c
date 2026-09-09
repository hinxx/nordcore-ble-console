#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>

#include "ble_gatt.h"
#include "shared.h"
#include "uart_rx.h"
#include "uart_tx.h"

/* Keep in sync with version.txt in this firmware's project directory --
 * that file sets the version embedded in the compiled binary's app
 * description (queryable via `esptool.py image_info` / OTA); this constant
 * is what actually gets printed in the serial boot banner below. See
 * fw/frame-sniffer, fw/byte-sniffer, fw/ble-sniffer for the same pattern. */
#define FW_NAME     "controller"
#define FW_VERSION  "1.0.0"

SemaphoreHandle_t g_console_mutex;

void console_log(const char *fmt, ...)
{
    xSemaphoreTake(g_console_mutex, portMAX_DELAY);
    va_list ap;
    va_start(ap, fmt);
    vprintf(fmt, ap);
    va_end(ap);
    fflush(stdout);
    xSemaphoreGive(g_console_mutex);
}

void app_main(void)
{
    g_console_mutex = xSemaphoreCreateMutex();
    if (g_console_mutex == NULL) {
        abort();
    }

    ble_gatt_start();
    uart_rx_start();
    uart_tx_start();

    console_log("\nESP32 treadmill controller -- %s v%s\n", FW_NAME, FW_VERSION);
    console_log("UART1 TX: GPIO25 (CON->BASE, via BC546 shifter -- see DESIGN.md)\n");
    console_log("UART2 RX: GPIO27 (BASE->CON telemetry)\n");
    console_log("BLE: advertising as \"TreadmillController\"\n");
    console_log("  TELEMETRY notify: <version> <speed_raw hi> <speed_raw lo> <speed_tenths_est> <steps>\n");
    console_log("  CMD write: 0x01=PLAY  0x02=STOP  0x03 <tenths>=SET_SPEED\n");
    console_log("Boots directly into idle heartbeat (DESIGN.md staged rollout step 2) --\n");
    console_log("no speed is commanded until a PLAY or SET_SPEED write arrives.\n\n");
}
