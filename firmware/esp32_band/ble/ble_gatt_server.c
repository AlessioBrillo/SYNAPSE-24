/**
 * @file ble_gatt_server.c
 * @brief Synapse Band v1 - BLE GATT Server (Bluedroid, ESP-IDF 5.x)
 * Standard: HR (0x180D), Battery (0x180F), Device Info (0x180A)
 * Custom: Synapse Service 128-bit (feature stream notify, config R/W, provisioning W/N)
 *
 * Services are created sequentially; characteristics are chained via
 * ESP_GATTS_ADD_CHAR_EVT / ESP_GATTS_ADD_CHAR_DESCR_EVT.
 */

#include <string.h>
#include "esp_log.h"
#include "esp_bt.h"
#include "esp_bt_main.h"
#include "esp_bt_device.h"
#include "esp_gap_ble_api.h"
#include "esp_gatts_api.h"
#include "ble_gatt_server.h"
#include "ble_gatt_server_priv.h"
#include "synapse_provisioning.h"
#include "synapse_config.h"

static uint16_t g_conn_id = 0xFFFF;
static esp_gatt_if_t g_gatts_if = ESP_GATT_IF_NONE;
static bool g_connected = false;
static bool g_feat_subscribed = false;
static bool g_prov_subscribed = false;
static uint8_t g_feat_seq = 0;
static uint16_t g_mtu = 23;

// Handles: services
static uint16_t hr_svc_handle = 0;
static uint16_t batt_svc_handle = 0;
static uint16_t dis_svc_handle = 0;
static uint16_t syn_svc_handle = 0;
// Handles: characteristics + CCCDs
static uint16_t hr_meas_handle = 0, hr_meas_cccd = 0;
static uint16_t batt_lvl_handle = 0, batt_cccd = 0;
static uint16_t feat_handle = 0, feat_cccd = 0;
static uint16_t cfg_handle = 0;
static uint16_t prov_handle = 0, prov_cccd = 0;

// Static characteristic values
static uint8_t s_body_location = 1; // wrist
static uint8_t s_batt_level = 100;
static char s_mfr_name[32] = "Synapse";
static char s_model_num[32] = "Band v1";
static char s_fw_rev[32] = "1.0.0";

// Custom 128-bit UUIDs
static const uint8_t SYN_SVC_UUID[16] = {0x5D,0x4C,0x3B,0x2A,0x1F,0x0E,0x9D,0x8C,0x7B,0x4A,0xF6,0xE5,0xD4,0xC3,0xB2,0xA1};
static const uint8_t FEAT_UUID[16]    = {0x5E,0x4C,0x3B,0x2A,0x1F,0x0E,0x9D,0x8C,0x7B,0x4A,0xF6,0xE5,0xD4,0xC3,0xB2,0xA1};
static const uint8_t CFG_UUID[16]     = {0x5F,0x4C,0x3B,0x2A,0x1F,0x0E,0x9D,0x8C,0x7B,0x4A,0xF6,0xE5,0xD4,0xC3,0xB2,0xA1};
static const uint8_t PROV_UUID[16]    = {0x60,0x4C,0x3B,0x2A,0x1F,0x0E,0x9D,0x8C,0x7B,0x4A,0xF6,0xE5,0xD4,0xC3,0xB2,0xA1};

esp_ble_adv_params_t adv_params_normal = {
    .adv_int_min = 0x20, .adv_int_max = 0x40,
    .adv_type = ADV_TYPE_IND, .own_addr_type = BLE_ADDR_TYPE_PUBLIC,
    .channel_map = ADV_CHNL_ALL, .adv_filter_policy = ADV_FILTER_ALLOW_SCAN_ANY_CON_ANY,
};
esp_ble_adv_params_t adv_params_provisioning = {
    .adv_int_min = 0x20, .adv_int_max = 0x40,
    .adv_type = ADV_TYPE_IND, .own_addr_type = BLE_ADDR_TYPE_PUBLIC,
    .channel_map = ADV_CHNL_ALL, .adv_filter_policy = ADV_FILTER_ALLOW_SCAN_ANY_CON_ANY,
};

static void start_advertising(const char *name, bool prov_mode) {
    esp_ble_gap_set_device_name(name);

    esp_ble_adv_data_t adv_data = {0};
    adv_data.set_scan_rsp = false;
    adv_data.include_name = true;
    adv_data.include_txpower = true;
    adv_data.flag = (ESP_BLE_ADV_FLAG_GEN_DISC | ESP_BLE_ADV_FLAG_BREDR_NOT_SPT);
    esp_ble_gap_config_adv_data(&adv_data);

    esp_ble_adv_data_t scan_rsp = {0};
    scan_rsp.set_scan_rsp = true;
    scan_rsp.include_name = true;
    esp_ble_gap_config_adv_data(&scan_rsp);

    esp_ble_gap_start_advertising(prov_mode ? &adv_params_provisioning : &adv_params_normal);
    ESP_LOGI(BLE_TAG, "Advertising started (%s)", prov_mode ? "provisioning" : "normal");
}

// --- helpers to add characteristics -----------------------------------------

static void add_char16(uint16_t svc, uint16_t uuid16, esp_gatt_perm_t perm,
                       esp_gatt_char_prop_t prop, uint8_t *val, uint16_t len) {
    esp_bt_uuid_t cu = {.len = ESP_UUID_LEN_16, .uuid = {.uuid16 = uuid16}};
    esp_attr_value_t attr = {0};
    esp_attr_control_t ctrl = {.auto_rsp = ESP_GATT_AUTO_RSP};
    if (val && len) {
        attr.attr_max_len = len;
        attr.attr_len = len;
        attr.attr_value = val;
    }
    esp_ble_gatts_add_char(svc, &cu, perm, prop, (val && len) ? &attr : NULL, &ctrl);
}

static void add_char128(uint16_t svc, const uint8_t uuid128[16], esp_gatt_perm_t perm,
                        esp_gatt_char_prop_t prop) {
    esp_bt_uuid_t cu = {.len = ESP_UUID_LEN_128};
    memcpy(cu.uuid.uuid128, uuid128, 16);
    esp_attr_control_t ctrl = {.auto_rsp = ESP_GATT_AUTO_RSP};
    esp_ble_gatts_add_char(svc, &cu, perm, prop, NULL, &ctrl);
}

static void add_cccd(uint16_t svc) {
    esp_bt_uuid_t du = {.len = ESP_UUID_LEN_16, .uuid = {.uuid16 = ESP_GATT_UUID_CHAR_CLIENT_CONFIG}};
    esp_attr_control_t ctrl = {.auto_rsp = ESP_GATT_AUTO_RSP};
    esp_ble_gatts_add_char_descr(svc, &du, ESP_GATT_PERM_READ | ESP_GATT_PERM_WRITE, NULL, &ctrl);
}

static void create_service16(uint16_t uuid16, uint8_t num_handles) {
    esp_gatt_srvc_id_t sid = {
        .is_primary = true,
        .id = {.inst_id = 0, .uuid = {.len = ESP_UUID_LEN_16, .uuid = {.uuid16 = uuid16}}},
    };
    esp_ble_gatts_create_service(g_gatts_if, &sid, num_handles);
}

static void create_synapse_service(void) {
    esp_gatt_srvc_id_t sid = {.is_primary = true, .id = {.inst_id = 0}};
    sid.id.uuid.len = ESP_UUID_LEN_128;
    memcpy(sid.id.uuid.uuid.uuid128, SYN_SVC_UUID, 16);
    esp_ble_gatts_create_service(g_gatts_if, &sid, 12);
}

// --- event handlers ----------------------------------------------------------

static void gatts_handler(esp_gatts_cb_event_t event, esp_gatt_if_t gatts_if,
                          esp_ble_gatts_cb_param_t *param);

static void gap_handler(esp_gap_ble_cb_event_t event, esp_ble_gap_cb_param_t *param) {
    (void)param;
    if (event == ESP_GAP_BLE_ADV_START_COMPLETE_EVT) {
        ESP_LOGI(BLE_TAG, "ADV start complete");
    }
}

static bool uuid16_is(const esp_bt_uuid_t *u, uint16_t v) {
    return u->len == ESP_UUID_LEN_16 && u->uuid.uuid16 == v;
}

static void gatts_handler(esp_gatts_cb_event_t event, esp_gatt_if_t gatts_if,
                          esp_ble_gatts_cb_param_t *param) {
    switch (event) {
    case ESP_GATTS_REG_EVT:
        g_gatts_if = gatts_if;
        create_service16(0x180D, 6); // Heart Rate
        break;

    case ESP_GATTS_CREATE_EVT: {
        if (param->create.status != ESP_GATT_OK) {
            ESP_LOGE(BLE_TAG, "Create service failed");
            break;
        }
        uint16_t h = param->create.service_handle;
        const esp_bt_uuid_t *u = &param->create.service_id.id.uuid;
        if (uuid16_is(u, 0x180D)) {
            hr_svc_handle = h;
            add_char16(h, 0x2A37, ESP_GATT_PERM_READ,
                       ESP_GATT_CHAR_PROP_BIT_NOTIFY, NULL, 0); // HR Measurement
        } else if (uuid16_is(u, 0x180F)) {
            batt_svc_handle = h;
            add_char16(h, 0x2A19, ESP_GATT_PERM_READ,
                       ESP_GATT_CHAR_PROP_BIT_READ | ESP_GATT_CHAR_PROP_BIT_NOTIFY,
                       &s_batt_level, sizeof(s_batt_level)); // Battery Level
        } else if (uuid16_is(u, 0x180A)) {
            dis_svc_handle = h;
            add_char16(h, 0x2A29, ESP_GATT_PERM_READ, ESP_GATT_CHAR_PROP_BIT_READ,
                       (uint8_t *)s_mfr_name, sizeof(s_mfr_name)); // Manufacturer
        } else if (u->len == ESP_UUID_LEN_128) {
            syn_svc_handle = h;
            add_char128(h, FEAT_UUID, ESP_GATT_PERM_READ,
                        ESP_GATT_CHAR_PROP_BIT_NOTIFY); // Feature stream
        }
        break;
    }

    case ESP_GATTS_ADD_CHAR_EVT: {
        if (param->add_char.status != ESP_GATT_OK) {
            ESP_LOGE(BLE_TAG, "Add char failed");
            break;
        }
        uint16_t svc = param->add_char.service_handle;
        uint16_t ch = param->add_char.attr_handle;
        const esp_bt_uuid_t *u = &param->add_char.char_uuid;
        if (svc == hr_svc_handle && uuid16_is(u, 0x2A37)) {
            hr_meas_handle = ch;
            add_cccd(svc);
        } else if (svc == batt_svc_handle && uuid16_is(u, 0x2A19)) {
            batt_lvl_handle = ch;
            add_cccd(svc);
        } else if (svc == dis_svc_handle && uuid16_is(u, 0x2A29)) {
            add_char16(svc, 0x2A24, ESP_GATT_PERM_READ, ESP_GATT_CHAR_PROP_BIT_READ,
                       (uint8_t *)s_model_num, sizeof(s_model_num)); // Model
        } else if (svc == dis_svc_handle && uuid16_is(u, 0x2A24)) {
            add_char16(svc, 0x2A26, ESP_GATT_PERM_READ, ESP_GATT_CHAR_PROP_BIT_READ,
                       (uint8_t *)s_fw_rev, sizeof(s_fw_rev)); // FW rev
        } else if (svc == dis_svc_handle && uuid16_is(u, 0x2A26)) {
            esp_ble_gatts_start_service(dis_svc_handle);
            create_synapse_service();
        } else if (svc == syn_svc_handle && u->len == ESP_UUID_LEN_128 &&
                   memcmp(u->uuid.uuid128, FEAT_UUID, 16) == 0) {
            feat_handle = ch;
            add_cccd(svc);
        } else if (svc == syn_svc_handle && u->len == ESP_UUID_LEN_128 &&
                   memcmp(u->uuid.uuid128, CFG_UUID, 16) == 0) {
            cfg_handle = ch;
            add_char128(svc, PROV_UUID, ESP_GATT_PERM_WRITE,
                        ESP_GATT_CHAR_PROP_BIT_WRITE | ESP_GATT_CHAR_PROP_BIT_NOTIFY);
        } else if (svc == syn_svc_handle && u->len == ESP_UUID_LEN_128 &&
                   memcmp(u->uuid.uuid128, PROV_UUID, 16) == 0) {
            prov_handle = ch;
            add_cccd(svc);
        }
        break;
    }

    case ESP_GATTS_ADD_CHAR_DESCR_EVT: {
        if (param->add_descr.status != ESP_GATT_OK) break;
        uint16_t svc = param->add_descr.service_handle;
        uint16_t d = param->add_descr.attr_handle;
        if (svc == hr_svc_handle && !hr_meas_cccd) {
            hr_meas_cccd = d;
            add_char16(svc, 0x2A38, ESP_GATT_PERM_READ, ESP_GATT_CHAR_PROP_BIT_READ,
                       &s_body_location, sizeof(s_body_location)); // Body location
        } else if (svc == hr_svc_handle) {
            esp_ble_gatts_start_service(hr_svc_handle);
            create_service16(0x180F, 4); // Battery
        } else if (svc == batt_svc_handle && !batt_cccd) {
            batt_cccd = d;
            esp_ble_gatts_start_service(batt_svc_handle);
            create_service16(0x180A, 8); // Device Info
        } else if (svc == syn_svc_handle && !feat_cccd) {
            feat_cccd = d;
            add_char128(svc, CFG_UUID, ESP_GATT_PERM_READ | ESP_GATT_PERM_WRITE,
                        ESP_GATT_CHAR_PROP_BIT_READ | ESP_GATT_CHAR_PROP_BIT_WRITE);
        } else if (svc == syn_svc_handle && !prov_cccd) {
            prov_cccd = d;
            esp_ble_gatts_start_service(syn_svc_handle);
            ESP_LOGI(BLE_TAG, "All GATT services started");
        }
        break;
    }

    case ESP_GATTS_START_EVT:
        break;

    case ESP_GATTS_CONNECT_EVT:
        g_conn_id = param->connect.conn_id;
        g_connected = true;
        g_feat_seq = 0;
        esp_ble_gap_stop_advertising();
        ESP_LOGI(BLE_TAG, "Connected conn_id=%d", g_conn_id);
        break;

    case ESP_GATTS_DISCONNECT_EVT:
        g_connected = false;
        g_conn_id = 0xFFFF;
        g_feat_subscribed = false;
        g_prov_subscribed = false;
        ESP_LOGI(BLE_TAG, "Disconnected");
        if (!synapse_provisioning_is_complete()) start_advertising("SYNAPSE-BAND", true);
        else start_advertising("SYNAPSE-BAND", false);
        break;

    case ESP_GATTS_WRITE_EVT: {
        uint16_t h = param->write.handle;
        if (param->write.len == 2 && (h == hr_meas_cccd || h == feat_cccd || h == prov_cccd)) {
            uint16_t v = param->write.value[0] | ((uint16_t)param->write.value[1] << 8);
            bool en = (v & 0x0001) != 0;
            if (h == hr_meas_cccd || h == feat_cccd) g_feat_subscribed = en;
            if (h == prov_cccd) g_prov_subscribed = en;
        }
        if (h == prov_handle && param->write.len >= 4) {
            synapse_prov_packet_t *p = (synapse_prov_packet_t *)param->write.value;
            synapse_provisioning_process_cmd(p->cmd, p->data, p->data_len);
        } else if (h == cfg_handle && param->write.len > 0) {
            synapse_config_update_from_ble(param->write.value, param->write.len);
        }
        if (param->write.need_rsp) {
            esp_ble_gatts_send_response(gatts_if, param->write.conn_id, param->write.trans_id,
                                        ESP_GATT_OK, NULL);
        }
        break;
    }

    case ESP_GATTS_MTU_EVT:
        g_mtu = param->mtu.mtu;
        break;

    case ESP_GATTS_CONF_EVT:
        break;

    default:
        break;
    }
}

esp_err_t synapse_ble_gatt_init(void) {
    esp_err_t ret;
    ESP_ERROR_CHECK(esp_bt_controller_mem_release(ESP_BT_MODE_CLASSIC_BT));
    esp_bt_controller_config_t cfg = BT_CONTROLLER_INIT_CONFIG_DEFAULT();
    ret = esp_bt_controller_init(&cfg);
    if (ret != ESP_OK) {
        ESP_LOGE(BLE_TAG, "BT controller init failed: %s", esp_err_to_name(ret));
        return ret;
    }
    ret = esp_bt_controller_enable(ESP_BT_MODE_BLE);
    if (ret != ESP_OK) {
        ESP_LOGE(BLE_TAG, "BT controller enable failed: %s", esp_err_to_name(ret));
        return ret;
    }
    ret = esp_bluedroid_init();
    if (ret != ESP_OK) {
        ESP_LOGE(BLE_TAG, "Bluedroid init failed: %s", esp_err_to_name(ret));
        return ret;
    }
    ret = esp_bluedroid_enable();
    if (ret != ESP_OK) {
        ESP_LOGE(BLE_TAG, "Bluedroid enable failed: %s", esp_err_to_name(ret));
        return ret;
    }
    ret = esp_ble_gatts_register_callback(gatts_handler);
    if (ret != ESP_OK) return ret;
    ret = esp_ble_gap_register_callback(gap_handler);
    if (ret != ESP_OK) return ret;
    ret = esp_ble_gatts_app_register(PROFILE_APP_ID);
    if (ret != ESP_OK) return ret;
    ESP_LOGI(BLE_TAG, "BLE GATT init OK (Bluedroid)");
    return ESP_OK;
}

void synapse_ble_gatt_start_advertising_provisioning(void) {
    start_advertising("SYNAPSE-BAND", true);
}

void synapse_ble_gatt_start_advertising_normal(void) {
    start_advertising("SYNAPSE-BAND", false);
}

void synapse_ble_gatt_process_events(void) {
}

esp_err_t synapse_ble_gatt_notify_features(const synapse_feature_stream_t *feature) {
    if (!g_connected || g_conn_id == 0xFFFF) return ESP_ERR_INVALID_STATE;
    if (!g_feat_subscribed) return ESP_ERR_INVALID_STATE;
    if (feat_handle == 0) return ESP_ERR_INVALID_STATE;
    synapse_feature_stream_t p = *feature;
    p.sequence = g_feat_seq++;
    return esp_ble_gatts_send_indicate(g_gatts_if, g_conn_id, feat_handle,
                                       sizeof(p), (uint8_t *)&p, false);
}

esp_err_t synapse_ble_gatt_notify_provisioning(const synapse_prov_packet_t *packet) {
    if (!g_connected || g_conn_id == 0xFFFF) return ESP_ERR_INVALID_STATE;
    if (!g_prov_subscribed) return ESP_ERR_INVALID_STATE;
    if (prov_handle == 0) return ESP_ERR_INVALID_STATE;
    return esp_ble_gatts_send_indicate(g_gatts_if, g_conn_id, prov_handle,
                                       sizeof(*packet), (uint8_t *)packet, false);
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
    s_batt_level = level;
    (void)charging;
    if (g_connected && batt_lvl_handle != 0) {
        esp_ble_gatts_set_attr_value(batt_lvl_handle, sizeof(level), &level);
    }
}
