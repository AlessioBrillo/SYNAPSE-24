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
static char s_mfr_name[32] = "Synapse";
static char s_model_num[32] = "Band v1";
static char s_fw_rev[32] = "1.0.0";
static uint8_t s_cfg_buf[SYNAPSE_MAX_CONFIG_SIZE] = {0};
static uint16_t s_cfg_len = 0;

// 128-bit UUIDs (little-endian byte order as in original spec)
static const ble_uuid128_t SYN_SVC_UUID =
    BLE_UUID128_INIT(0x5D, 0x4C, 0x3B, 0x2A, 0x1F, 0x0E, 0x9D, 0x8C,
                     0x7B, 0x4A, 0xF6, 0xE5, 0xD4, 0xC3, 0xB2, 0xA1);
static const ble_uuid128_t FEAT_UUID =
    BLE_UUID128_INIT(0x5E, 0x4C, 0x3B, 0x2A, 0x1F, 0x0E, 0x9D, 0x8C,
                     0x7B, 0x4A, 0xF6, 0xE5, 0xD4, 0xC3, 0xB2, 0xA1);
static const ble_uuid128_t CFG_UUID =
    BLE_UUID128_INIT(0x5F, 0x4C, 0x3B, 0x2A, 0x1F, 0x0E, 0x9D, 0x8C,
                     0x7B, 0x4A, 0xF6, 0xE5, 0xD4, 0xC3, 0xB2, 0xA1);
static const ble_uuid128_t PROV_UUID =
    BLE_UUID128_INIT(0x60, 0x4C, 0x3B, 0x2A, 0x1F, 0x0E, 0x9D, 0x8C,
                     0x7B, 0x4A, 0xF6, 0xE5, 0xD4, 0xC3, 0xB2, 0xA1);

// --- characteristic access callbacks -----------------------------------------

static int feat_access(uint16_t conn, uint16_t attr, struct ble_gatt_access_ctxt *ctxt, void *arg) {
    (void)conn; (void)attr; (void)arg;
    // Notify-only characteristic
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
                .access_cb = feat_access,
                .flags = BLE_GATT_CHR_F_NOTIFY,
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
                .uuid = BLE_UUID16_DECLARE(0x2A26),
                .access_cb = dis_str_access,
                .arg = (void *)s_fw_rev,
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
        return 0;
    case BLE_GAP_EVENT_MTU:
        g_mtu = event->mtu.value;
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
    static const ble_uuid16_t svc_uuid = BLE_UUID16_INIT(0x180D);
    fields.uuids16 = (ble_uuid16_t[]){svc_uuid};
    fields.num_uuids16 = 1;
    fields.uuids16_is_complete = 1;
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
    if (fw) {
        strncpy(s_fw_rev, fw, sizeof(s_fw_rev) - 1);
        s_fw_rev[sizeof(s_fw_rev) - 1] = '\0';
    }
    ESP_LOGI(BLE_TAG, "DIS set: %s %s S:%s FW:%s HW:%s", s_mfr_name, s_model_num,
             s ? s : "?", s_fw_rev, hw ? hw : "?");
}

void synapse_ble_gatt_update_battery(uint8_t level, bool charging) {
    (void)charging;
    s_batt_level = level;
}
