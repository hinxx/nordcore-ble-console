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
#include "shared.h"
#include "uart_tx.h"

/*
 * UUIDs generated once (uuid4), fixed from here on. Deliberately distinct
 * from fw/ble-sniffer's -- this is a different device with different
 * behavior (it transmits commands; the sniffer never does), and a BLE
 * central should never confuse the two. NimBLE's BLE_UUID128_INIT takes
 * bytes least-significant-first, the reverse of the standard UUID string
 * form -- see fw/ble-sniffer/main/ble_gatt.c, where this convention was
 * first verified against a known-good reference (the Nordic UART Service
 * UUID) before use.
 *
 *   Service (Treadmill Controller): 72AE7218-753F-4F0A-BF85-2D4D66F92855
 *   Characteristic TELEMETRY:       7CEE917E-4E4E-4250-BBE9-35D678C2D721
 *   Characteristic CMD:             239F8516-6FDB-4B12-B127-F601C7043F16
 */
static const ble_uuid128_t s_svc_uuid =
    BLE_UUID128_INIT(0x55, 0x28, 0xf9, 0x66, 0x4d, 0x2d, 0x85, 0xbf,
                      0x0a, 0x4f, 0x3f, 0x75, 0x18, 0x72, 0xae, 0x72);

static const ble_uuid128_t s_chr_telemetry_uuid =
    BLE_UUID128_INIT(0x21, 0xd7, 0xc2, 0x78, 0xd6, 0x35, 0xe9, 0xbb,
                      0x50, 0x42, 0x4e, 0x4e, 0x7e, 0x91, 0xee, 0x7c);

static const ble_uuid128_t s_chr_cmd_uuid =
    BLE_UUID128_INIT(0x16, 0x3f, 0x04, 0xc7, 0x01, 0xf6, 0x27, 0xb1,
                      0x12, 0x4b, 0xdb, 0x6f, 0x16, 0x85, 0x9f, 0x23);

static const char *s_device_name = "TreadmillController";

static uint16_t s_telemetry_val_handle;
static uint16_t s_conn_handle = BLE_HS_CONN_HANDLE_NONE;
static bool s_notify_enabled = false;
static uint8_t s_own_addr_type;

/* CMD command bytes, written by the BLE client. */
#define CMD_PLAY        0x01
#define CMD_STOP        0x02
#define CMD_SET_SPEED   0x03  /* followed by 1 byte: tenths of km/h */

static int gatt_access_telemetry(uint16_t conn_handle, uint16_t attr_handle,
                                  struct ble_gatt_access_ctxt *ctxt, void *arg)
{
    /* NOTIFY-only: no READ/WRITE flag is set, so the stack should never
     * route an access here. */
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

    uint8_t buf[8];
    uint16_t om_len = OS_MBUF_PKTLEN(ctxt->om);
    if (om_len > sizeof(buf)) {
        return BLE_ATT_ERR_INVALID_ATTR_VALUE_LEN;
    }

    uint16_t len = 0;
    if (ble_hs_mbuf_to_flat(ctxt->om, buf, sizeof(buf), &len) != 0) {
        return BLE_ATT_ERR_UNLIKELY;
    }

    if (len < 1) {
        console_log("BLE CMD write: empty, ignored\n");
        return 0;
    }

    switch (buf[0]) {
    case CMD_PLAY:
        uart_tx_cmd_play();
        break;

    case CMD_STOP:
        uart_tx_cmd_stop();
        break;

    case CMD_SET_SPEED:
        if (len < 2) {
            console_log("BLE CMD SET_SPEED: missing tenths-km/h byte, ignored\n");
        } else {
            uart_tx_cmd_set_speed_tenths(buf[1]);
        }
        break;

    default:
        console_log("BLE CMD write: unknown command 0x%02X, ignored\n", buf[0]);
        break;
    }

    return 0;
}

static const struct ble_gatt_svc_def s_gatt_svcs[] = {
    {
        .type = BLE_GATT_SVC_TYPE_PRIMARY,
        .uuid = &s_svc_uuid.u,
        .characteristics = (struct ble_gatt_chr_def[]) {
            {
                .uuid = &s_chr_telemetry_uuid.u,
                .access_cb = gatt_access_telemetry,
                .val_handle = &s_telemetry_val_handle,
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

static void ble_advertise(void)
{
    struct ble_gap_adv_params adv_params;
    struct ble_hs_adv_fields fields;
    int rc;

    /* Flags + name only, same reasoning as fw/ble-sniffer: a legacy adv
     * packet is capped at 31 bytes, and the 128-bit service UUID alone
     * would take 18 of those. The service is still fully discoverable via
     * normal GATT service discovery once connected. */
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
            ble_advertise();
        }
        return 0;

    case BLE_GAP_EVENT_DISCONNECT:
        console_log("BLE: disconnected; reason=%d\n", event->disconnect.reason);
        s_conn_handle = BLE_HS_CONN_HANDLE_NONE;
        s_notify_enabled = false;
        ble_advertise();
        return 0;

    case BLE_GAP_EVENT_ADV_COMPLETE:
        ble_advertise();
        return 0;

    case BLE_GAP_EVENT_SUBSCRIBE:
        if (event->subscribe.attr_handle == s_telemetry_val_handle) {
            s_notify_enabled = event->subscribe.cur_notify;
            console_log("BLE: TELEMETRY notify %s\n", s_notify_enabled ? "enabled" : "disabled");
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

static void ble_on_sync(void)
{
    int rc = ble_hs_id_infer_auto(0, &s_own_addr_type);
    if (rc != 0) {
        console_log("BLE: error determining address type; rc=%d\n", rc);
        return;
    }
    ble_advertise();
}

static void ble_on_reset(int reason)
{
    console_log("BLE: host reset; reason=%d\n", reason);
}

static void ble_host_task(void *param)
{
    (void)param;
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

void ble_gatt_start(void)
{
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

    ble_hs_cfg.reset_cb = ble_on_reset;
    ble_hs_cfg.sync_cb = ble_on_sync;

    int rc = gatt_svr_init();
    if (rc != 0) {
        console_log("BLE: gatt_svr_init failed; rc=%d\n", rc);
        abort();
    }

    rc = ble_svc_gap_device_name_set(s_device_name);
    if (rc != 0) {
        console_log("BLE: ble_svc_gap_device_name_set failed; rc=%d\n", rc);
    }

    nimble_port_freertos_init(ble_host_task);
}

void ble_gatt_notify_telemetry(uint16_t speed_raw, uint8_t steps)
{
    if (s_conn_handle == BLE_HS_CONN_HANDLE_NONE || !s_notify_enabled) {
        return; /* no subscriber connected -- nothing to deliver */
    }

    /* raw/750: the proportional approximation from README's "Full-range
     * calibration corrects /775" -- a convenience estimate for display,
     * not a substitute for the exact 53-point table when precision
     * matters. Clamped to fit one byte (should never realistically get
     * close to 255 given the confirmed ~60-tenths max). */
    uint32_t tenths_est = ((uint32_t)speed_raw * 10) / 750;
    if (tenths_est > 255) {
        tenths_est = 255;
    }

    uint8_t record[5];
    record[0] = 0x01; /* format version */
    record[1] = (uint8_t)(speed_raw >> 8);
    record[2] = (uint8_t)(speed_raw & 0xFF);
    record[3] = (uint8_t)tenths_est;
    record[4] = steps;

    struct os_mbuf *om = ble_hs_mbuf_from_flat(record, sizeof(record));
    if (om == NULL) {
        return;
    }

    /* Best-effort, same as fw/ble-sniffer's RX_LOG: a dropped notification
     * just means this one update is missed, not a fault. */
    ble_gatts_notify_custom(s_conn_handle, s_telemetry_val_handle, om);
}
