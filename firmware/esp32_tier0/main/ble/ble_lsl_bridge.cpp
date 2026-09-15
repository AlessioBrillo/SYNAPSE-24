#include "ble_lsl_bridge.h"
#include "esp_log.h"
#include "esp_nimble_hci.h"
#include "nimble/nimble_port.h"
#include "nimble/nimble_port_freertos.h"
#include "host/ble_hs.h"
#include "host/ble_gatt.h"
#include "services/gap/ble_svc_gap.h"
#include "services/gatt/ble_svc_gatt.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/queue.h"
#include <string.h>

static const char* TAG = "ble_lsl_bridge";

static ble_lsl_bridge_t* s_bridge = NULL;
static uint16_t s_chr_handles[BLE_LSL_CHAR_MAX] = {0};
static uint16_t s_ccc_handles[BLE_LSL_CHAR_MAX] = {0};

static const ble_uuid128_t gatt_svr_svc_uuid = BLE_UUID128_INIT(0x9E, 0xCA, 0xDC, 0x24, 0x0E, 0xE5, 0xA9, 0xE0, 0x93, 0xF3, 0xA3, 0xB5, 0x01, 0x00, 0x40, 0x6E);
static const ble_uuid128_t gatt_svr_chr_uuid_ppg = BLE_UUID128_INIT(0x9E, 0xCA, 0xDC, 0x24, 0x0E, 0xE5, 0xA9, 0xE0, 0x93, 0xF3, 0xA3, 0xB5, 0x02, 0x00, 0x40, 0x6E);
static const ble_uuid128_t gatt_svr_chr_uuid_ecg = BLE_UUID128_INIT(0x9E, 0xCA, 0xDC, 0x24, 0x0E, 0xE5, 0xA9, 0xE0, 0x93, 0xF3, 0xA3, 0xB5, 0x03, 0x00, 0x40, 0x6E);
static const ble_uuid128_t gatt_svr_chr_uuid_imu = BLE_UUID128_INIT(0x9E, 0xCA, 0xDC, 0x24, 0x0E, 0xE5, 0xA9, 0xE0, 0x93, 0xF3, 0xA3, 0xB5, 0x04, 0x00, 0x40, 0x6E);
static const ble_uuid128_t gatt_svr_chr_uuid_sync = BLE_UUID128_INIT(0x9E, 0xCA, 0xDC, 0x24, 0x0E, 0xE5, 0xA9, 0xE0, 0x93, 0xF3, 0xA3, 0xB5, 0x05, 0x00, 0x40, 0x6E);

static int gatt_svr_chr_access(uint16_t conn_handle, uint16_t attr_handle, struct ble_gatt_access_ctxt* ctxt, void* arg);
static void ble_lsl_notify_task_fn(void* arg);
static void ble_lsl_on_sync(void);
static void ble_lsl_on_reset(int reason);
static int ble_lsl_gap_event(struct ble_gap_event* event, void* arg);

static const struct ble_gatt_chr_def gatt_svr_chrs[] = {
    {
        .uuid = &gatt_svr_chr_uuid_ppg.u,
        .access_cb = gatt_svr_chr_access,
        .flags = BLE_GATT_CHR_F_READ | BLE_GATT_CHR_F_NOTIFY,
        .min_key_size = 0
    },
    {
        .uuid = &gatt_svr_chr_uuid_ecg.u,
        .access_cb = gatt_svr_chr_access,
        .flags = BLE_GATT_CHR_F_READ | BLE_GATT_CHR_F_NOTIFY,
        .min_key_size = 0
    },
    {
        .uuid = &gatt_svr_chr_uuid_imu.u,
        .access_cb = gatt_svr_chr_access,
        .flags = BLE_GATT_CHR_F_READ | BLE_GATT_CHR_F_NOTIFY,
        .min_key_size = 0
    },
    {
        .uuid = &gatt_svr_chr_uuid_sync.u,
        .access_cb = gatt_svr_chr_access,
        .flags = BLE_GATT_CHR_F_READ | BLE_GATT_CHR_F_WRITE | BLE_GATT_CHR_F_NOTIFY,
        .min_key_size = 0
    },
    { 0 }
};

static const struct ble_gatt_svc_def gatt_svr_svcs[] = {
    {
        .type = BLE_GATT_SVC_TYPE_PRIMARY,
        .uuid = &gatt_svr_svc_uuid.u,
        .characteristics = gatt_svr_chrs,
    },
    { 0 }
};

static int gatt_svr_chr_access(uint16_t conn_handle, uint16_t attr_handle, struct ble_gatt_access_ctxt* ctxt, void* arg) {
    (void)arg;
    ble_lsl_char_t chr_type = BLE_LSL_CHAR_MAX;

    for (int i = 0; i < BLE_LSL_CHAR_MAX; i++) {
        if (attr_handle == s_chr_handles[i] || attr_handle == s_ccc_handles[i]) {
            chr_type = (ble_lsl_char_t)i;
            break;
        }
    }

    if (chr_type == BLE_LSL_CHAR_MAX) {
        return BLE_ATT_ERR_INVALID_HANDLE;
    }

    switch (ctxt->op) {
        case BLE_GATT_ACCESS_OP_READ_CHR: {
            if (chr_type == BLE_LSL_CHAR_SYNC) {
                uint32_t seq = 0;
                os_mbuf_append(ctxt->om, &seq, sizeof(seq));
            }
            return 0;
        }
        case BLE_GATT_ACCESS_OP_WRITE_CHR: {
            if (chr_type == BLE_LSL_CHAR_SYNC && ctxt->om->om_len == sizeof(uint32_t) + sizeof(int64_t)) {
                uint32_t seq;
                int64_t hub_ts;
                os_mbuf_copydata(ctxt->om, 0, sizeof(seq), &seq);
                os_mbuf_copydata(ctxt->om, sizeof(seq), sizeof(hub_ts), &hub_ts);
                ESP_LOGD(TAG, "Sync marker received: seq=%" PRIu32 ", hub_ts=%" PRId64, seq, hub_ts);
            }
            return 0;
        }
        case BLE_GATT_ACCESS_OP_WRITE_DSC: {
            if (attr_handle == s_ccc_handles[chr_type]) {
                uint16_t value;
                os_mbuf_copydata(ctxt->om, 0, sizeof(value), &value);
                s_bridge->notifications_enabled[chr_type] = (value & 0x0001) != 0;
                ESP_LOGI(TAG, "Notifications %s for char %d", s_bridge->notifications_enabled[chr_type] ? "enabled" : "disabled", chr_type);
            }
            return 0;
        }
        default:
            return BLE_ATT_ERR_UNLIKELY;
    }
}

static void ble_lsl_notify_task_fn(void* arg) {
    ble_lsl_bridge_t* bridge = (ble_lsl_bridge_t*)arg;
    ble_lsl_notify_item_t item;

    while (1) {
        if (xQueueReceive(bridge->notify_queue, &item, portMAX_DELAY) == pdTRUE) {
            if (!bridge->running || !bridge->notifications_enabled[item.type]) continue;

            struct os_mbuf* om = ble_hs_mbuf_from_flat(&item.sample, sizeof(sensor_sample_t));
            if (!om) {
                ESP_LOGW(TAG, "Failed to allocate mbuf for notification");
                continue;
            }

            int rc = ble_gatts_notify_custom(bridge->conn_handle, s_chr_handles[item.type], om);
            if (rc != 0) {
                ESP_LOGW(TAG, "Notify failed for char %d: %d", item.type, rc);
            }
        }
    }
}

static void ble_lsl_on_sync(void) {
    int rc = ble_svc_gap_device_name_set("SYNAPSE-Tier0");
    assert(rc == 0);

    ble_hs_id_infer_auto(0, &ble_hs_cfg.smp_io_cap);

    struct ble_gap_adv_params adv_params = {0};
    adv_params.conn_mode = BLE_GAP_CONN_MODE_UND;
    adv_params.disc_mode = BLE_GAP_DISC_MODE_GEN;
    adv_params.itvl_min = BLE_GAP_ADV_ITVL_MS(100);
    adv_params.itvl_max = BLE_GAP_ADV_ITVL_MS(200);
    adv_params.channel_map = 0;

    rc = ble_gap_adv_start(BLE_HS_ID_APP, NULL, BLE_HS_FOREVER, &adv_params, ble_lsl_gap_event, NULL);
    if (rc != 0) {
        ESP_LOGE(TAG, "Advertising start failed: %d", rc);
    } else {
        ESP_LOGI(TAG, "Advertising started");
    }
}

static void ble_lsl_on_reset(int reason) {
    ESP_LOGE(TAG, "NimBLE reset: %d", reason);
}

static int ble_lsl_gap_event(struct ble_gap_event* event, void* arg) {
    (void)arg;

    switch (event->type) {
        case BLE_GAP_EVENT_CONNECT: {
            if (event->connect.status == 0) {
                s_bridge->conn_handle = event->connect.conn_handle;
                ESP_LOGI(TAG, "Connected, handle=%d", s_bridge->conn_handle);
            } else {
                ESP_LOGW(TAG, "Connection failed: %d", event->connect.status);
            }
            return 0;
        }
        case BLE_GAP_EVENT_DISCONNECT: {
            ESP_LOGI(TAG, "Disconnected, reason=%d", event->disconnect.reason);
            s_bridge->conn_handle = BLE_HS_CONN_HANDLE_NONE;
            memset(s_bridge->notifications_enabled, 0, sizeof(s_bridge->notifications_enabled));
            ble_lsl_on_sync();
            return 0;
        }
        case BLE_GAP_EVENT_MTU: {
            ESP_LOGI(TAG, "MTU updated: %d", event->mtu.value);
            return 0;
        }
        default:
            return 0;
    }
}

esp_err_t ble_lsl_bridge_init(ble_lsl_bridge_t* bridge, QueueHandle_t scheduler_queue) {
    (void)scheduler_queue;

    if (!bridge) return ESP_ERR_INVALID_ARG;

    memset(bridge, 0, sizeof(ble_lsl_bridge_t));
    bridge->conn_handle = BLE_HS_CONN_HANDLE_NONE;
    bridge->notify_queue = xQueueCreate(BLE_LSL_NOTIFY_QUEUE_SIZE, sizeof(ble_lsl_notify_item_t));
    if (!bridge->notify_queue) {
        ESP_LOGE(TAG, "Failed to create notify queue");
        return ESP_ERR_NO_MEM;
    }

    s_bridge = bridge;

    esp_nimble_hci_init();
    nimble_port_init();

    ble_hs_cfg.sync_cb = ble_lsl_on_sync;
    ble_hs_cfg.reset_cb = ble_lsl_on_reset;
    ble_hs_cfg.gatts_register_cb = NULL;
    ble_hs_cfg.store_status_cb = NULL;

    ble_svc_gap_init();
    ble_svc_gatt_init();

    int rc = ble_gatts_count_cfg(gatt_svr_svcs);
    if (rc != 0) {
        ESP_LOGE(TAG, "GATT count cfg failed: %d", rc);
        return ESP_FAIL;
    }

    rc = ble_gatts_add_svcs(gatt_svr_svcs);
    if (rc != 0) {
        ESP_LOGE(TAG, "GATT add svcs failed: %d", rc);
        return ESP_FAIL;
    }

    for (int i = 0; i < BLE_LSL_CHAR_MAX; i++) {
        s_chr_handles[i] = ble_gatts_find_chr_handle(&gatt_svr_chrs[i].uuid);
        s_ccc_handles[i] = s_chr_handles[i] + 1;
    }

    ESP_LOGI(TAG, "BLE LSL bridge initialized");
    return ESP_OK;
}

esp_err_t ble_lsl_bridge_start(ble_lsl_bridge_t* bridge) {
    if (!bridge || bridge->running) return ESP_ERR_INVALID_STATE;

    bridge->running = true;

    xTaskCreate(ble_lsl_notify_task_fn, "ble_notify", 4096, bridge, 5, &bridge->notify_task);
    nimble_port_freertos_init(ble_lsl_on_sync);

    ESP_LOGI(TAG, "BLE LSL bridge started");
    return ESP_OK;
}

esp_err_t ble_lsl_bridge_stop(ble_lsl_bridge_t* bridge) {
    if (!bridge || !bridge->running) return ESP_ERR_INVALID_STATE;

    bridge->running = false;
    ble_gap_adv_stop();

    if (bridge->notify_task) {
        vTaskDelete(bridge->notify_task);
        bridge->notify_task = NULL;
    }

    nimble_port_stop();

    ESP_LOGI(TAG, "BLE LSL bridge stopped");
    return ESP_OK;
}

esp_err_t ble_lsl_bridge_deinit(ble_lsl_bridge_t* bridge) {
    if (!bridge) return ESP_ERR_INVALID_ARG;

    if (bridge->running) {
        ble_lsl_bridge_stop(bridge);
    }

    if (bridge->notify_queue) {
        vQueueDelete(bridge->notify_queue);
        bridge->notify_queue = NULL;
    }

    nimble_port_deinit();
    esp_nimble_hci_deinit();

    if (s_bridge == bridge) {
        s_bridge = NULL;
    }

    memset(bridge, 0, sizeof(ble_lsl_bridge_t));
    ESP_LOGI(TAG, "BLE LSL bridge deinitialized");
    return ESP_OK;
}

esp_err_t ble_lsl_bridge_send_sample(ble_lsl_bridge_t* bridge, const sensor_sample_t* sample) {
    if (!bridge || !sample || !bridge->running) return ESP_ERR_INVALID_STATE;

    ble_lsl_char_t chr_type;
    switch (sample->type) {
        case SENSOR_TYPE_PPG: chr_type = BLE_LSL_CHAR_PPG; break;
        case SENSOR_TYPE_ECG: chr_type = BLE_LSL_CHAR_ECG; break;
        case SENSOR_TYPE_IMU: chr_type = BLE_LSL_CHAR_IMU; break;
        default: return ESP_ERR_INVALID_ARG;
    }

    if (!bridge->notifications_enabled[chr_type]) return ESP_OK;

    ble_lsl_notify_item_t item = {.type = chr_type, .sample = *sample};
    if (xQueueSend(bridge->notify_queue, &item, 0) != pdTRUE) {
        return ESP_ERR_NO_MEM;
    }
    return ESP_OK;
}

esp_err_t ble_lsl_bridge_send_sync_marker(ble_lsl_bridge_t* bridge, uint32_t sequence, int64_t hub_timestamp_us) {
    if (!bridge || !bridge->running) return ESP_ERR_INVALID_STATE;

    if (!bridge->notifications_enabled[BLE_LSL_CHAR_SYNC]) return ESP_OK;

    sensor_sample_t sample = {0};
    sample.type = SENSOR_TYPE_MAX;
    sample.timestamp_us = hub_timestamp_us;
    sample.sequence = sequence;

    ble_lsl_notify_item_t item = {.type = BLE_LSL_CHAR_SYNC, .sample = sample};
    if (xQueueSend(bridge->notify_queue, &item, 0) != pdTRUE) {
        return ESP_ERR_NO_MEM;
    }
    return ESP_OK;
}

bool ble_lsl_bridge_is_connected(const ble_lsl_bridge_t* bridge) {
    return bridge && bridge->conn_handle != BLE_HS_CONN_HANDLE_NONE;
}