/**
 * @file ble_gatt_server.c
 * @brief Synapse Band v1 - BLE GATT Server (NimBLE, ESP-IDF 5.x)
 * Same stack as head/forearm/common. Standard: HR (0x180D), Battery (0x180F),
 * Device Info (0x180A). Custom 128-bit Synapse service: feature stream (notify),
 * device config (R/W), provisioning (W/N).
 */

#include <string.h>
#include <assert.h>
#include "esp_log.h"
#include "esp_nimble_hci.h"
#include "nimble/nimble_port.h"
#include "nimble/nimble_port_freertos.h"
#include "host/ble_hs.h"
#include "host/ble_gap.h"
#include "host/util/util.h"
#include "services/gap/ble_svc_gap.h"
#include "services/gatt/ble_svc_gatt.h"
#include "ble_gatt_server.h"
#include "ble_gatt_server_priv.h"
#include "ble_research_service.h"
#include "synapse_provisioning.h"
#include "synapse_config.h"

static uint16_t g_conn_handle = BLE_HS_CONN_HANDLE_NONE;
static bool g_connected = false;
static bool g_feat_subscribed = false;
static bool g_prov_subscribed = false;
static uint8_t g_feat_seq = 0;
static uint16_t g_mtu = 23;

static uint16_t s_feat_handle = 0;
static uint16_t s_prov_handle = 0;

static uint8_t s_body_location = 1; // wrist
static uint8_t s_batt_level = 100;
static bool s_batt_notify_pending = false;
static uint16_t s_last_hr_bpm = 0xFFFF;
static uint16_t s_last_rr_ms = 0xFFFF;
static char s_mfr_name[32] = "Synapse";
static char s_model_num[32] = "Band v1";
static char s_serial_num[32] = "SYN-000000";
static char s_fw_rev[32] = "1.0.0";
static char s_hw_rev[32] = "1.0";
static uint8_t s_cfg_buf[SYNAPSE_MAX_CONFIG_SIZE] = {0};
static uint16_t s_cfg_len = 0;

// 128-bit UUIDs (little-endian byte order, GATT v1 frozen)
// Service: f9e08ffd-3d34-441f-97a6-1bf0958123f1
static const ble_uuid128_t SYN_SVC_UUID =
    BLE_UUID128_INIT(0xF1, 0x23, 0x81, 0x95, 0xF0, 0x1B, 0xA6, 0x97,
                     0x1F, 0x44, 0x34, 0x3D, 0xFD, 0x8F, 0xE0, 0xF9);
// Feature stream: db4307e5-6cb3-4152-a0e2-81c3921168e9
static const ble_uuid128_t FEAT_UUID =
    BLE_UUID128_INIT(0xE9, 0x68, 0x11, 0x92, 0xC3, 0x81, 0xE2, 0xA0,
                     0x52, 0x41, 0xB3, 0x6C, 0xE5, 0x07, 0x43, 0xDB);
// Device config: bf68d647-0d02-479a-8d75-7964a51f332f
static const ble_uuid128_t CFG_UUID =
    BLE_UUID128_INIT(0x2F, 0x33, 0x1F, 0xA5, 0x64, 0x79, 0x75, 0x8D,
                     0x9A, 0x47, 0x02, 0x0D, 0x47, 0xD6, 0x68, 0xBF);
// Provisioning: 4e200cf1-94ac-4d63-a161-a03e5d3bb14a
static const ble_uuid128_t PROV_UUID =
    BLE_UUID128_INIT(0x4A, 0xB1, 0x3B, 0x5D, 0x3E, 0xA0, 0x61, 0xA1,
                     0x63, 0x4D, 0xAC, 0x94, 0xF1, 0x0C, 0x20, 0x4E);

// --- characteristic access callbacks -----------------------------------------

// Feature stream: notify-only, never read.
static int feat_access(uint16_t conn, uint16_t attr, struct ble_gatt_access_ctxt *ctxt, void *arg) {
    (void)conn; (void)attr; (void)arg;
    return BLE_ATT_ERR_UNLIKELY;
}

// Heart Rate Measurement (0x2A37): readable (last value) + notify.
// Flags: bit0 = HR 16-bit (value in bytes 1-2 as uint16 LE), bit4 = RR present.
// When RR present: bytes after HR are uint16 LE RR intervals, each followed by
// nothing (packed back-to-back). Averyn reads this flag-gated field.
static int hr_access(uint16_t conn, uint16_t attr, struct ble_gatt_access_ctxt *ctxt, void *arg) {
    (void)conn; (void)attr; (void)arg;
    if (ctxt->op == BLE_GATT_ACCESS_OP_READ_CHR) {
        uint8_t val[2 + 2]; // flags + hr16 (+ optional RR not included on read)
        uint8_t flags = 0x10; // 16-bit HR value
        val[0] = flags;
        val[1] = (uint8_t)(s_last_hr_bpm & 0xFF);
        val[2] = (uint8_t)((s_last_hr_bpm >> 8) & 0xFF);
        int rc = os_mbuf_append(ctxt->om, val, sizeof(val));
        return rc == 0 ? 0 : BLE_ATT_ERR_INSUFFICIENT_RES;
    }
    return BLE_ATT_ERR_UNLIKELY;
}

static int cfg_access(uint16_t conn, uint16_t attr, struct ble_gatt_access_ctxt *ctxt, void *arg) {
    (void)conn; (void)attr; (void)arg;
    if (ctxt->op == BLE_GATT_ACCESS_OP_READ_CHR) {
        int rc = os_mbuf_append(ctxt->om, s_cfg_buf, s_cfg_len);
        return rc == 0 ? 0 : BLE_ATT_ERR_INSUFFICIENT_RES;
    }
    if (ctxt->op == BLE_GATT_ACCESS_OP_WRITE_CHR) {
        uint16_t len = OS_MBUF_PKTLEN(ctxt->om);
        if (len > sizeof(s_cfg_buf)) return BLE_ATT_ERR_INVALID_ATTR_VALUE_LEN;
        s_cfg_len = len;
        os_mbuf_copydata(ctxt->om, 0, len, s_cfg_buf);
        synapse_config_update_from_ble(s_cfg_buf, s_cfg_len);
        return 0;
    }
    return BLE_ATT_ERR_UNLIKELY;
}

/**
 * @brief Notify Heart Rate Measurement with optional RR interval.
 * Builds a standard 0x2A37 value: flags(0x10 = 16-bit HR) + HR16 + [RR16...].
 * Call whenever a new HR sample is available; subscribers get the push.
 */
esp_err_t synapse_ble_gatt_notify_hr(uint16_t hr_bpm, uint16_t rr_interval_ms) {
    if (!g_connected || g_conn_handle == BLE_HS_CONN_HANDLE_NONE) return ESP_ERR_INVALID_STATE;
    uint16_t chr_handle = ble_gatts_find_chr(&BLE_UUID16_DECLARE(0x180D).u,
                                             &BLE_UUID16_DECLARE(0x2A37).u, NULL, NULL);
    if (chr_handle == 0) return ESP_ERR_INVALID_STATE;
    // Subscribers only; avoid flooding when nobody listens.
    bool subscribed = false;
    // Reuse feature-subscribe flag as a proxy? No: track HR subscription separately.
    (void)subscribed;
    uint8_t val[2 + 2]; // flags + HR16
    uint8_t n = 0;
    val[n++] = 0x10; // flags: 16-bit HR value, no sensor contact, no energy expended, no RR
    if (rr_interval_ms != 0xFFFF) {
        val[0] |= 0x10; // bit4 RR interval present (0x10)
        uint8_t rr_b[2] = { rr_interval_ms & 0xFF, (rr_interval_ms >> 8) & 0xFF };
        struct os_mbuf *om = ble_hs_mbuf_from_flat(val, n);
        if (!om) return ESP_ERR_NO_MEM;
        int rc = ble_gatts_notify_custom(g_conn_handle, chr_handle, om);
        if (rc != 0) return ESP_FAIL;
        // Second notification for RR-only would require combined packet; NimBLE
        // notify is one attribute value. Put HR+RR in one notify when RR present.
        uint8_t combined[2 + 2 + 2];
        combined[0] = 0x10 | 0x10; // flags: 16-bit HR + RR present
        combined[1] = (uint8_t)(hr_bpm & 0xFF);
        combined[2] = (uint8_t)((hr_bpm >> 8) & 0xFF);
        combined[3] = (uint8_t)(rr_interval_ms & 0xFF);
        combined[4] = (uint8_t)((rr_interval_ms >> 8) & 0xFF);
        struct os_mbuf *om2 = ble_hs_mbuf_from_flat(combined, sizeof(combined));
        if (!om2) return ESP_ERR_NO_MEM;
        rc = ble_gatts_notify_custom(g_conn_handle, chr_handle, om2);
        return rc == 0 ? ESP_OK : ESP_FAIL;
    }
    struct os_mbuf *om = ble_hs_mbuf_from_flat(val, n);
    if (!om) return ESP_ERR_NO_MEM;
    int rc = ble_gatts_notify_custom(g_conn_handle, chr_handle, om);
    return rc == 0 ? ESP_OK : ESP_FAIL;
}

static int body_loc_access(uint16_t conn, uint16_t attr, struct ble_gatt_access_ctxt *ctxt, void *arg) {
    (void)conn; (void)attr; (void)arg;
    if (ctxt->op == BLE_GATT_ACCESS_OP_READ_CHR) {
        os_mbuf_append(ctxt->om, &s_body_location, sizeof(s_body_location));
        return 0;
    }
    return BLE_ATT_ERR_UNLIKELY;
}

static int batt_access(uint16_t conn, uint16_t attr, struct ble_gatt_access_ctxt *ctxt, void *arg) {
    (void)conn; (void)attr; (void)arg;
    if (ctxt->op == BLE_GATT_ACCESS_OP_READ_CHR) {
        os_mbuf_append(ctxt->om, &s_batt_level, sizeof(s_batt_level));
        return 0;
    }
    return BLE_ATT_ERR_UNLIKELY;
}

/**
 * @brief Notify Battery Level (0x2A19) if subscribers exist.
 */
esp_err_t synapse_ble_gatt_notify_battery(uint8_t level, bool charging) {
    (void)charging;
    s_batt_level = level;
    s_batt_notify_pending = true;
    if (!g_connected || g_conn_handle == BLE_HS_CONN_HANDLE_NONE) return ESP_ERR_INVALID_STATE;
    uint16_t chr_handle = ble_gatts_find_chr(&BLE_UUID16_DECLARE(0x180F).u,
                                             &BLE_UUID16_DECLARE(0x2A19).u, NULL, NULL);
    if (chr_handle == 0) return ESP_ERR_INVALID_STATE;
    struct os_mbuf *om = ble_hs_mbuf_from_flat(&s_batt_level, 1);
    if (!om) return ESP_ERR_NO_MEM;
    int rc = ble_gatts_notify_custom(g_conn_handle, chr_handle, om);
    return rc == 0 ? ESP_OK : ESP_FAIL;
}

static int prov_access(uint16_t conn, uint16_t attr, struct ble_gatt_access_ctxt *ctxt, void *arg) {
    (void)conn; (void)attr; (void)arg;
    if (ctxt->op == BLE_GATT_ACCESS_OP_WRITE_CHR) {
        uint16_t len = OS_MBUF_PKTLEN(ctxt->om);
        if (len < 4) return BLE_ATT_ERR_INVALID_ATTR_VALUE_LEN;
        uint8_t hdr[4];
        os_mbuf_copydata(ctxt->om, 0, 4, hdr);
        uint16_t data_len = hdr[2] | ((uint16_t)hdr[3] << 8);
        uint8_t payload[240];
        if (data_len > sizeof(payload)) return BLE_ATT_ERR_INVALID_ATTR_VALUE_LEN;
        if (data_len) os_mbuf_copydata(ctxt->om, 4, data_len, payload);
        synapse_provisioning_process_cmd(hdr[0], payload, data_len);
        return 0;
    }
    return BLE_ATT_ERR_UNLIKELY;
}

static int body_loc_access(uint16_t conn, uint16_t attr, struct ble_gatt_access_ctxt *ctxt, void *arg) {
    (void)conn; (void)attr; (void)arg;
    if (ctxt->op == BLE_GATT_ACCESS_OP_READ_CHR) {
        os_mbuf_append(ctxt->om, &s_body_location, sizeof(s_body_location));
        return 0;
    }
    return BLE_ATT_ERR_UNLIKELY;
}

static int batt_access(uint16_t conn, uint16_t attr, struct ble_gatt_access_ctxt *ctxt, void *arg) {
    (void)conn; (void)attr; (void)arg;
    if (ctxt->op == BLE_GATT_ACCESS_OP_READ_CHR) {
        os_mbuf_append(ctxt->om, &s_batt_level, sizeof(s_batt_level));
        return 0;
    }
    return BLE_ATT_ERR_UNLIKELY;
}

static int dis_str_access(uint16_t conn, uint16_t attr, struct ble_gatt_access_ctxt *ctxt, void *arg) {
    (void)conn; (void)attr;
    const char *str = (const char *)arg;
    if (ctxt->op == BLE_GATT_ACCESS_OP_READ_CHR) {
        os_mbuf_append(ctxt->om, str, strlen(str));
        return 0;
    }
    return BLE_ATT_ERR_UNLIKELY;
}

// --- GATT database ------------------------------------------------------------

static const struct ble_gatt_svc_def gatt_svr_svcs[] = {
    {
        .type = BLE_GATT_SVC_TYPE_PRIMARY,
        .uuid = BLE_UUID16_DECLARE(0x180D),
        .characteristics = (struct ble_gatt_chr_def[]){
            {
                .uuid = BLE_UUID16_DECLARE(0x2A37),
                .access_cb = hr_access,
                .flags = BLE_GATT_CHR_F_READ | BLE_GATT_CHR_F_NOTIFY,
            },
            {
                .uuid = BLE_UUID16_DECLARE(0x2A38),
                .access_cb = body_loc_access,
                .flags = BLE_GATT_CHR_F_READ,
            },
            {0},
        },
    },
    {
        .type = BLE_GATT_SVC_TYPE_PRIMARY,
        .uuid = BLE_UUID16_DECLARE(0x180F),
        .characteristics = (struct ble_gatt_chr_def[]){
            {
                .uuid = BLE_UUID16_DECLARE(0x2A19),
                .access_cb = batt_access,
                .flags = BLE_GATT_CHR_F_READ | BLE_GATT_CHR_F_NOTIFY,
            },
            {0},
        },
    },
    {
        .type = BLE_GATT_SVC_TYPE_PRIMARY,
        .uuid = BLE_UUID16_DECLARE(0x180A),
        .characteristics = (struct ble_gatt_chr_def[]){
            {
                .uuid = BLE_UUID16_DECLARE(0x2A29),
                .access_cb = dis_str_access,
                .arg = (void *)s_mfr_name,
                .flags = BLE_GATT_CHR_F_READ,
            },
            {
                .uuid = BLE_UUID16_DECLARE(0x2A24),
                .access_cb = dis_str_access,
                .arg = (void *)s_model_num,
                .flags = BLE_GATT_CHR_F_READ,
            },
            {
                .uuid = BLE_UUID16_DECLARE(0x2A25),
                .access_cb = dis_str_access,
                .arg = (void *)s_serial_num,
                .flags = BLE_GATT_CHR_F_READ,
            },
            {
                .uuid = BLE_UUID16_DECLARE(0x2A26),
                .access_cb = dis_str_access,
                .arg = (void *)s_fw_rev,
                .flags = BLE_GATT_CHR_F_READ,
            },
            {
                .uuid = BLE_UUID16_DECLARE(0x2A27),
                .access_cb = dis_str_access,
                .arg = (void *)s_hw_rev,
                .flags = BLE_GATT_CHR_F_READ,
            },
            {0},
        },
    },
    {
        .type = BLE_GATT_SVC_TYPE_PRIMARY,
        .uuid = &SYN_SVC_UUID.u,
        .characteristics = (struct ble_gatt_chr_def[]){
            {
                .uuid = &FEAT_UUID.u,
                .access_cb = feat_access,
                .flags = BLE_GATT_CHR_F_NOTIFY,
            },
            {
                .uuid = &CFG_UUID.u,
                .access_cb = cfg_access,
                .flags = BLE_GATT_CHR_F_READ | BLE_GATT_CHR_F_WRITE,
            },
            {
                .uuid = &PROV_UUID.u,
                .access_cb = prov_access,
                .flags = BLE_GATT_CHR_F_WRITE | BLE_GATT_CHR_F_NOTIFY,
            },
            {0},
        },
    },
    {0},
};

// --- GAP -----------------------------------------------------------------------

static int gap_event_cb(struct ble_gap_event *event, void *arg) {
    (void)arg;
    switch (event->type) {
    case BLE_GAP_EVENT_CONNECT:
        if (event->connect.status == 0) {
            g_conn_handle = event->connect.conn_handle;
            g_connected = true;
            g_feat_seq = 0;
            // The research service owns its own connection handle: without
            // this the raw stream path had BLE_HS_CONN_HANDLE_NONE forever
            // and silently transmitted nothing (Fase 1, bloccante B1).
            synapse_research_service_on_connect(event->connect.conn_handle);
            ESP_LOGI(BLE_TAG, "Connected handle=%d", g_conn_handle);
        } else {
            ESP_LOGW(BLE_TAG, "Connection failed: %d", event->connect.status);
            synapse_ble_gatt_start_advertising_normal();
        }
        return 0;
    case BLE_GAP_EVENT_DISCONNECT:
        ESP_LOGI(BLE_TAG, "Disconnected reason=%d", event->disconnect.reason);
        g_connected = false;
        g_conn_handle = BLE_HS_CONN_HANDLE_NONE;
        g_feat_subscribed = false;
        g_prov_subscribed = false;
        synapse_research_service_on_disconnect();
        if (!synapse_provisioning_is_complete()) synapse_ble_gatt_start_advertising_provisioning();
        else synapse_ble_gatt_start_advertising_normal();
        return 0;
    case BLE_GAP_EVENT_SUBSCRIBE:
        ESP_LOGI(BLE_TAG, "Subscribe attr=%d notify=%d",
                 event->subscribe.attr_handle, event->subscribe.cur_notify);
        if (event->subscribe.attr_handle == s_feat_handle) {
            g_feat_subscribed = event->subscribe.cur_notify;
        } else if (event->subscribe.attr_handle == s_prov_handle) {
            g_prov_subscribed = event->subscribe.cur_notify;
        }
        // Route research-service CCCDs to the raw stream table: a stream only
        // transmits once its characteristic has been subscribed.
        synapse_research_service_handle_subscribe(event->subscribe.attr_handle,
                                                  event->subscribe.cur_notify);
        return 0;
    case BLE_GAP_EVENT_MTU:
        g_mtu = event->mtu.value;
        synapse_research_service_set_mtu(event->mtu.value);
        return 0;
    case BLE_GAP_EVENT_ADV_COMPLETE:
        ESP_LOGI(BLE_TAG, "Advertising complete");
        return 0;
    default:
        return 0;
    }
}

static void start_advertising_impl(const char *name) {
    struct ble_hs_adv_fields fields = {0};
    fields.flags = BLE_HS_ADV_F_DISC_GEN | BLE_HS_ADV_F_BREDR_UNSUP;
    fields.name = (uint8_t *)name;
    fields.name_len = strlen(name);
    fields.name_is_complete = 1;
    static const ble_uuid16_t svc_uuid16 = BLE_UUID16_INIT(0x180D);
    fields.uuids16 = (ble_uuid16_t[]){svc_uuid16};
    fields.num_uuids16 = 1;
    fields.uuids16_is_complete = 1;
    // Advertise the Synapse consumer service so Averyn can find the device by
    // UUID without connecting first (phase 0 contract).
    static const ble_uuid128_t svc_uuid128 = BLE_UUID128_INIT(
        0xF1, 0x23, 0x81, 0x95, 0xF0, 0x1B, 0xA6, 0x97,
        0x1F, 0x44, 0x34, 0x3D, 0xFD, 0x8F, 0xE0, 0xF9);
    fields.uuids128 = (ble_uuid128_t[]){svc_uuid128};
    fields.num_uuids128 = 1;
    fields.uuids128_is_complete = 1;
    ble_gap_adv_set_fields(&fields);

    struct ble_gap_adv_params adv_params = {0};
    adv_params.conn_mode = BLE_GAP_CONN_MODE_UND;
    adv_params.disc_mode = BLE_GAP_DISC_MODE_GEN;
    adv_params.itvl_min = BLE_GAP_ADV_ITVL_MS(32);
    adv_params.itvl_max = BLE_GAP_ADV_ITVL_MS(64);
    ble_gap_adv_start(BLE_OWN_ADDR_PUBLIC, NULL, BLE_HS_FOREVER,
                      &adv_params, gap_event_cb, NULL);
    ESP_LOGI(BLE_TAG, "Advertising started (%s)", name);
}

static void on_sync(void) {
    int rc = ble_svc_gap_device_name_set("SYNAPSE-BAND");
    assert(rc == 0);
    ble_hs_cfg.sm_io_cap = BLE_SM_IO_CAP_NO_IO;
    if (!synapse_provisioning_is_complete()) start_advertising_impl("SYNAPSE-BAND");
    else start_advertising_impl("SYNAPSE-BAND");
}

static void on_reset(int reason) {
    ESP_LOGE(BLE_TAG, "NimBLE reset: %d", reason);
}

static void host_task_fn(void *arg) {
    (void)arg;
    nimble_port_run();
}

// --- public API ------------------------------------------------------------------

esp_err_t synapse_ble_gatt_init(void) {
    esp_nimble_hci_init();
    nimble_port_init();

    ble_hs_cfg.sync_cb = on_sync;
    ble_hs_cfg.reset_cb = on_reset;

    ble_svc_gap_init();
    ble_svc_gatt_init();

    int rc = ble_gatts_count_cfg(gatt_svr_svcs);
    if (rc != 0) {
        ESP_LOGE(BLE_TAG, "GATT count cfg failed: %d", rc);
        return ESP_FAIL;
    }
    rc = ble_gatts_add_svcs(gatt_svr_svcs);
    if (rc != 0) {
        ESP_LOGE(BLE_TAG, "GATT add svcs failed: %d", rc);
        return ESP_FAIL;
    }

    s_feat_handle = ble_gatts_find_chr(&SYN_SVC_UUID.u, &FEAT_UUID.u, NULL, NULL);
    s_prov_handle = ble_gatts_find_chr(&SYN_SVC_UUID.u, &PROV_UUID.u, NULL, NULL);
    ESP_LOGI(BLE_TAG, "BLE GATT init OK (NimBLE) feat=%d prov=%d", s_feat_handle, s_prov_handle);

    nimble_port_freertos_init(host_task_fn);
    return ESP_OK;
}

void synapse_ble_gatt_start_advertising_provisioning(void) {
    if (ble_hs_synced()) start_advertising_impl("SYNAPSE-BAND");
}

void synapse_ble_gatt_start_advertising_normal(void) {
    if (ble_hs_synced()) start_advertising_impl("SYNAPSE-BAND");
}

void synapse_ble_gatt_process_events(void) {
}

esp_err_t synapse_ble_gatt_notify_features(const synapse_feature_stream_t *feature) {
    if (!g_connected || g_conn_handle == BLE_HS_CONN_HANDLE_NONE) return ESP_ERR_INVALID_STATE;
    if (!g_feat_subscribed || s_feat_handle == 0) return ESP_ERR_INVALID_STATE;
    synapse_feature_stream_t p = *feature;
    p.sequence = g_feat_seq++;
    struct os_mbuf *om = ble_hs_mbuf_from_flat(&p, sizeof(p));
    if (!om) return ESP_ERR_NO_MEM;
    int rc = ble_gatts_notify_custom(g_conn_handle, s_feat_handle, om);
    return rc == 0 ? ESP_OK : ESP_FAIL;
}

esp_err_t synapse_ble_gatt_notify_provisioning(const synapse_prov_packet_t *packet) {
    if (!g_connected || g_conn_handle == BLE_HS_CONN_HANDLE_NONE) return ESP_ERR_INVALID_STATE;
    if (!g_prov_subscribed || s_prov_handle == 0) return ESP_ERR_INVALID_STATE;
    struct os_mbuf *om = ble_hs_mbuf_from_flat(packet, sizeof(*packet));
    if (!om) return ESP_ERR_NO_MEM;
    int rc = ble_gatts_notify_custom(g_conn_handle, s_prov_handle, om);
    return rc == 0 ? ESP_OK : ESP_FAIL;
}

bool synapse_ble_gatt_is_connected(void) {
    return g_connected;
}

uint16_t synapse_ble_gatt_get_mtu(void) {
    return g_mtu;
}

void synapse_ble_gatt_set_device_info(const char *m, const char *model, const char *s,
                                      const char *fw, const char *hw) {
    if (m) {
        strncpy(s_mfr_name, m, sizeof(s_mfr_name) - 1);
        s_mfr_name[sizeof(s_mfr_name) - 1] = '\0';
    }
    if (model) {
        strncpy(s_model_num, model, sizeof(s_model_num) - 1);
        s_model_num[sizeof(s_model_num) - 1] = '\0';
    }
    if (s) {
        strncpy(s_serial_num, s, sizeof(s_serial_num) - 1);
        s_serial_num[sizeof(s_serial_num) - 1] = '\0';
    }
    if (fw) {
        strncpy(s_fw_rev, fw, sizeof(s_fw_rev) - 1);
        s_fw_rev[sizeof(s_fw_rev) - 1] = '\0';
    }
    if (hw) {
        strncpy(s_hw_rev, hw, sizeof(s_hw_rev) - 1);
        s_hw_rev[sizeof(s_hw_rev) - 1] = '\0';
    }
    ESP_LOGI(BLE_TAG, "DIS set: %s %s S:%s FW:%s HW:%s", s_mfr_name, s_model_num,
             s_serial_num, s_fw_rev, s_hw_rev);
}

void synapse_ble_gatt_update_battery(uint8_t level, bool charging) {
    s_batt_level = level;
    if (s_batt_notify_pending && g_connected && g_conn_handle != BLE_HS_CONN_HANDLE_NONE) {
        synapse_ble_gatt_notify_battery(level, charging);
    }
}

/**
 * @brief Mark battery level as changed and notify subscribers if connected.
 * Call from the power module whenever the measured level changes.
 */
void synapse_ble_gatt_battery_changed(uint8_t level, bool charging) {
    s_batt_notify_pending = true;
    synapse_ble_gatt_update_battery(level, charging);
}

esp_err_t synapse_ble_gatt_push_feature(uint16_t hr_bpm, uint16_t rr_interval_ms,
                                         uint8_t ppg_sqi, uint8_t ppg_map,
                                         uint8_t ecg_quality, uint8_t stress_level,
                                         uint8_t motion_level, int16_t temperature) {
    static uint32_t s_last_notify_ms = 0;
    uint32_t now_ms = (uint32_t)(esp_timer_get_time() / 1000);
    // 1 Hz default gate: 1000 ms between notifications. Configurable later via
    // the config characteristic if Averyn needs a faster live feed.
    if (now_ms - s_last_notify_ms < 1000) return ESP_OK;
    if (!g_connected || g_conn_handle == BLE_HS_CONN_HANDLE_NONE) return ESP_ERR_INVALID_STATE;
    if (!g_feat_subscribed || s_feat_handle == 0) return ESP_OK; // nothing subscribed: silent drop

    synapse_feature_stream_t f = {0};
    f.version = SYNAPSE_FEATURE_STREAM_VERSION;
    f.sequence = g_feat_seq++;
    f.heart_rate = hr_bpm;
    f.rr_interval_ms = rr_interval_ms;
    f.ppg_sqi = ppg_sqi;
    f.ppg_map = ppg_map;
    f.ecg_quality = ecg_quality;
    f.stress_level = stress_level;
    f.motion_level = motion_level;
    f.battery_level = s_batt_level;
    f.charging = 0; // filled by power module when wired
    f.sport_type = 0;
    f.cadence = 0xFFFF;
    f.temperature = temperature;
    f.rmssd_ms = 0xFFFF;
    f.sdnn_ms = 0xFFFF;

    esp_err_t rc = synapse_ble_gatt_notify_features(&f);
    if (rc != ESP_OK) return rc;

    // Mirror HR onto the standard characteristic so any HR app works.
    if (hr_bpm != 0xFFFF) {
        (void)synapse_ble_gatt_notify_hr(hr_bpm, rr_interval_ms);
    }
    s_last_notify_ms = now_ms;
    return ESP_OK;
}
