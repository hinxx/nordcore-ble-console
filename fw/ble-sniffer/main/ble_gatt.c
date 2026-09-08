#include <stdarg.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "esp_err.h"
#include "nvs_flash.h"

#include "nimble/nimble_port.h"
#include "nimble/nimble_port_freertos.h"
#include "host/ble_hs.h"
#include "host/ble_uuid.h"
#include "host/util/util.h"
#include "services/gap/ble_svc_gap.h"
#include "services/gatt/ble_svc_gatt.h"

#include "ble_gatt.h"

/*
 * GATT layout:
 *
 *   Service: Treadmill Sniffer            (128-bit UUID, private/testing --
 *                                           not a registered SIG profile)
 *     +-- characteristic RX_LOG  NOTIFY    sniffed frames, see ble_gatt.h
 *     +-- characteristic CMD     WRITE     accepted and logged, not acted
 *                                          on yet -- present so the GATT
 *                                          structure is ready for active
 *                                          control once the protocol is
 *                                          understood (REQUIREMENTS.md
 *                                          future stages); nothing is ever
 *                                          transmitted onto either
 *                                          treadmill UART from here.
 *
 * UUIDs were generated once (uuid4) and are fixed from here on -- a client
 * app configured against one shouldn't need reconfiguring after a rebuild.
 * NimBLE's BLE_UUID128_INIT takes the bytes least-significant-first, the
 * reverse of the standard UUID string form; see README.md for both forms
 * of each UUID used here.
 */
static const ble_uuid128_t s_svc_uuid =
    BLE_UUID128_INIT(0xd6, 0xe6, 0x74, 0xc8, 0x1c, 0xdc, 0xde, 0x9f,
                      0x5f, 0x4b, 0xa5, 0x30, 0xe9, 0x01, 0xac, 0x3a);

static const ble_uuid128_t s_chr_rx_log_uuid =
    BLE_UUID128_INIT(0x7f, 0xc4, 0x97, 0x5d, 0x4c, 0x3f, 0x13, 0x94,
                      0x7c, 0x4a, 0xe1, 0x61, 0x04, 0xc6, 0x2d, 0xf0);

static const ble_uuid128_t s_chr_cmd_uuid =
    BLE_UUID128_INIT(0xd2, 0xa4, 0xa4, 0x33, 0x07, 0x59, 0xd1, 0xbc,
                      0xec, 0x49, 0x97, 0x3d, 0xe9, 0xeb, 0x2d, 0x84);

static const char *s_device_name = "TreadmillSniffer";

static uint16_t s_rx_log_val_handle;
static uint16_t s_conn_handle = BLE_HS_CONN_HANDLE_NONE;
static bool s_notify_enabled = false;
static uint8_t s_own_addr_type;

static void console_log(const char *fmt, ...)
{
    xSemaphoreTake(g_console_mutex, portMAX_DELAY);
    va_list ap;
    va_start(ap, fmt);
    vprintf(fmt, ap);
    va_end(ap);
    fflush(stdout);
    xSemaphoreGive(g_console_mutex);
}

static int gatt_access_rx_log(uint16_t conn_handle, uint16_t attr_handle,
                               struct ble_gatt_access_ctxt *ctxt, void *arg)
{
    /* NOTIFY-only (see s_gatt_svcs below): no READ/WRITE flag is set, so
     * the stack should never route an access here. */
    (void)conn_handle;
    (void)attr_handle;
    (void)ctxt;
    (void)arg;
    return BLE_ATT_ERR_UNLIKELY;
}

static int gatt_access_cmd(uint16_t conn_handle, uint16_t attr_handle,
                            struct ble_gatt_access_ctxt *ctxt, void *arg)
{
    (void)conn_handle;
    (void)attr_handle;
    (void)arg;

    if (ctxt->op != BLE_GATT_ACCESS_OP_WRITE_CHR) {
        return BLE_ATT_ERR_UNLIKELY;
    }

    uint8_t buf[32];
    uint16_t om_len = OS_MBUF_PKTLEN(ctxt->om);
    if (om_len > sizeof(buf)) {
        return BLE_ATT_ERR_INVALID_ATTR_VALUE_LEN;
    }

    uint16_t len = 0;
    if (ble_hs_mbuf_to_flat(ctxt->om, buf, sizeof(buf), &len) != 0) {
        return BLE_ATT_ERR_UNLIKELY;
    }

    char hex[sizeof(buf) * 3 + 1];
    size_t pos = 0;
    for (uint16_t i = 0; i < len; i++) {
        int n = snprintf(hex + pos, sizeof(hex) - pos, "%02X ", buf[i]);
        if (n <= 0 || (size_t)n >= sizeof(hex) - pos) {
            break;
        }
        pos += (size_t)n;
    }
    hex[pos > 0 ? pos - 1 : 0] = '\0';

    console_log("BLE CMD write (not yet implemented, ignored): %s\n", hex);
    return 0;
}

static const struct ble_gatt_svc_def s_gatt_svcs[] = {
    {
        .type = BLE_GATT_SVC_TYPE_PRIMARY,
        .uuid = &s_svc_uuid.u,
        .characteristics = (struct ble_gatt_chr_def[]) {
            {
                .uuid = &s_chr_rx_log_uuid.u,
                .access_cb = gatt_access_rx_log,
                .val_handle = &s_rx_log_val_handle,
                .flags = BLE_GATT_CHR_F_NOTIFY,
            },
            {
                .uuid = &s_chr_cmd_uuid.u,
                .access_cb = gatt_access_cmd,
                .flags = BLE_GATT_CHR_F_WRITE,
            },
            {
                0, /* No more characteristics in this service. */
            },
        },
    },
    {
        0, /* No more services. */
    },
};

static int ble_gap_event_cb(struct ble_gap_event *event, void *arg);

static void ble_sniffer_advertise(void)
{
    struct ble_gap_adv_params adv_params;
    struct ble_hs_adv_fields fields;
    int rc;

    /*
     * Keep the advertisement small: flags + device name only. A legacy
     * adv packet is capped at 31 bytes, and the 128-bit service UUID
     * alone would take 18 of those, leaving little room for a readable
     * name. The service is still fully discoverable after connecting via
     * normal GATT service discovery -- nRF Connect/LightBlue will show it
     * once connected, it's just not advertised up front.
     */
    memset(&fields, 0, sizeof(fields));
    fields.flags = BLE_HS_ADV_F_DISC_GEN | BLE_HS_ADV_F_BREDR_UNSUP;
    fields.name = (uint8_t *)s_device_name;
    fields.name_len = strlen(s_device_name);
    fields.name_is_complete = 1;

    rc = ble_gap_adv_set_fields(&fields);
    if (rc != 0) {
        console_log("BLE: error setting advertisement fields; rc=%d\n", rc);
        return;
    }

    memset(&adv_params, 0, sizeof(adv_params));
    adv_params.conn_mode = BLE_GAP_CONN_MODE_UND;
    adv_params.disc_mode = BLE_GAP_DISC_MODE_GEN;
    rc = ble_gap_adv_start(s_own_addr_type, NULL, BLE_HS_FOREVER,
                           &adv_params, ble_gap_event_cb, NULL);
    if (rc != 0) {
        console_log("BLE: error starting advertisement; rc=%d\n", rc);
    }
}

static int ble_gap_event_cb(struct ble_gap_event *event, void *arg)
{
    (void)arg;

    switch (event->type) {
    case BLE_GAP_EVENT_CONNECT:
        console_log("BLE: connection %s; status=%d\n",
                    event->connect.status == 0 ? "established" : "failed",
                    event->connect.status);
        if (event->connect.status == 0) {
            s_conn_handle = event->connect.conn_handle;
        } else {
            /* Failed connection attempt; resume advertising. */
            ble_sniffer_advertise();
        }
        return 0;

    case BLE_GAP_EVENT_DISCONNECT:
        console_log("BLE: disconnected; reason=%d\n", event->disconnect.reason);
        s_conn_handle = BLE_HS_CONN_HANDLE_NONE;
        s_notify_enabled = false;
        ble_sniffer_advertise();
        return 0;

    case BLE_GAP_EVENT_ADV_COMPLETE:
        ble_sniffer_advertise();
        return 0;

    case BLE_GAP_EVENT_SUBSCRIBE:
        if (event->subscribe.attr_handle == s_rx_log_val_handle) {
            s_notify_enabled = event->subscribe.cur_notify;
            console_log("BLE: RX_LOG notify %s\n", s_notify_enabled ? "enabled" : "disabled");
        }
        return 0;

    case BLE_GAP_EVENT_MTU:
        console_log("BLE: MTU negotiated; conn_handle=%d mtu=%d\n",
                    event->mtu.conn_handle, event->mtu.value);
        return 0;

    default:
        return 0;
    }
}

static void ble_sniffer_on_sync(void)
{
    int rc = ble_hs_id_infer_auto(0, &s_own_addr_type);
    if (rc != 0) {
        console_log("BLE: error determining address type; rc=%d\n", rc);
        return;
    }
    ble_sniffer_advertise();
}

static void ble_sniffer_on_reset(int reason)
{
    console_log("BLE: host reset; reason=%d\n", reason);
}

static void ble_sniffer_host_task(void *param)
{
    (void)param;
    /* Returns only once nimble_port_stop() runs, which this firmware never
     * calls -- the BLE host runs for the lifetime of the device. */
    nimble_port_run();
    nimble_port_freertos_deinit();
}

static int gatt_svr_init(void)
{
    int rc;

    ble_svc_gap_init();
    ble_svc_gatt_init();

    rc = ble_gatts_count_cfg(s_gatt_svcs);
    if (rc != 0) {
        return rc;
    }

    rc = ble_gatts_add_svcs(s_gatt_svcs);
    if (rc != 0) {
        return rc;
    }

    return 0;
}

void ble_sniffer_start(void)
{
    /* NVS is required by the NimBLE host (PHY calibration data storage). */
    esp_err_t ret = nvs_flash_init();
    if (ret == ESP_ERR_NVS_NO_FREE_PAGES || ret == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        ret = nvs_flash_init();
    }
    ESP_ERROR_CHECK(ret);

    ret = nimble_port_init();
    if (ret != ESP_OK) {
        console_log("BLE: nimble_port_init failed; ret=%d\n", ret);
        abort();
    }

    ble_hs_cfg.reset_cb = ble_sniffer_on_reset;
    ble_hs_cfg.sync_cb = ble_sniffer_on_sync;

    int rc = gatt_svr_init();
    if (rc != 0) {
        console_log("BLE: gatt_svr_init failed; rc=%d\n", rc);
        abort();
    }

    rc = ble_svc_gap_device_name_set(s_device_name);
    if (rc != 0) {
        console_log("BLE: ble_svc_gap_device_name_set failed; rc=%d\n", rc);
    }

    nimble_port_freertos_init(ble_sniffer_host_task);
}

void ble_sniffer_notify_frame(uint8_t direction, const uint8_t *frame, size_t frame_len)
{
    if (s_conn_handle == BLE_HS_CONN_HANDLE_NONE || !s_notify_enabled) {
        return; /* no subscriber connected -- nothing to deliver */
    }
    if (frame_len > 250) {
        /* Defensive only: current frames are well under this. Guards the
         * fixed-size stack buffer below against ever being overrun if the
         * parser's own frame size cap is ever raised without updating this. */
        return;
    }

    uint8_t record[2 + 250];
    record[0] = direction;
    record[1] = (uint8_t)frame_len;
    memcpy(&record[2], frame, frame_len);

    struct os_mbuf *om = ble_hs_mbuf_from_flat(record, 2 + frame_len);
    if (om == NULL) {
        return;
    }

    /* Return code intentionally not surfaced further: a BLE notification
     * is inherently best-effort (no subscriber, congested link, MTU too
     * small, ...) -- any of those just mean this one frame is dropped,
     * same as the rest of Bluetooth's own notify semantics. */
    ble_gatts_notify_custom(s_conn_handle, s_rx_log_val_handle, om);
}
