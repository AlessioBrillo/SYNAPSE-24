/**
 * @file ble_lsl_bridge.cpp
 * @brief BLE LSL Bridge Implementation
 *
 * BLE Configuration:
 * - Service UUID: 0000ffe0-0000-1000-8000-00805f9b34fb (Nordic UART compatible)
 * - Characteristic UUID: 0000ffe1-0000-1000-8000-00805f9b34fb (TX notify)
 * - MTU: 512 bytes
 * - Connection interval: 15ms (fast for streaming)
 * - Peripheral role: Pod advertises, Hub connects
 */

#include "ble_lsl_bridge.h"
#include "esp_log.h"
#include "esp_bt.h"
#include "esp_gap_ble_api.h"
#include "esp_gatts_api.h"
#include "esp_bt_defs.h"
#include "esp_bt_main.h"
#include "esp_gatt_common_api.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/queue.h"
#include <cstring>
#include <cstdint>

static const char* TAG = "BLE_LSL";

// ============================================================================
// GATT PROFILE DEFINITIONS
// ============================================================================

#define SYNAPSE_GATTS_NUM_HANDLE     12
#define SYNAPSE_GATTS_SERVICE_UUID   0xFFE0
#define SYNAPSE_GATTS_CHAR_UUID      0xFFE1
#define SYNAPSE_GATTS_SYNC_CHAR_UUID 0xFFE2  // Sync marker characteristic

// GATT Attribute indices
enum {
    IDX_SVC = 0,
    IDX_CHAR_TX,       // Data TX (notify)
    IDX_CHAR_TX_VAL,
    IDX_CHAR_TX_CFG,   // CCCD for notifications
    IDX_CHAR_SYNC,     // Sync marker RX (write)
    IDX_CHAR_SYNC_VAL,
    IDX_CHAR_SYNC_CFG,
    IDX_NB
};

// Advertising data
static uint8_t adv_service_uuid16[2] = {
    (uint8_t)(SYNAPSE_GATTS_SERVICE_UUID & 0xFF),
    (uint8_t)(SYNAPSE_GATTS_SERVICE_UUID >> 8)
};

// ============================================================================
// GLOBAL STATE
// ============================================================================

static uint16_t s_gatts_if = 0;
static uint16_t s_conn_id = 0;
static uint16_t s_service_handle = 0;
static uint16_t s_char_tx_handle = 0;
static uint16_t s_char_sync_handle = 0;
static bool s_connected = false;
static bool s_notifications_enabled = false;

static QueueHandle_t s_ble_tx_queue = NULL;
static void (*s_sync_callback)(const synapse_sync_marker_t*) = NULL;

// GATT attribute table
static const uint16_t s_primary_service_uuid = ESP_GATT_UUID_PRI_SERVICE;
static const uint16_t s_character_declaration_uuid = ESP_GATT_UUID_CHAR_DECLARE;
static const uint16_t s_character_client_config_uuid = ESP_GATT_UUID_CHAR_CLIENT_CONFIG;
static const uint8_t s_char_prop_notify = ESP_GATT_CHAR_PROP_BIT_NOTIFY;
static const uint8_t s_char_prop_write = ESP_GATT_CHAR_PROP_BIT_WRITE;
static const uint8_t s_ccc_enable[2] = {0x00, 0x01}; // Notifications enabled
static const uint8_t s_ccc_disable[2] = {0x00, 0x00};

static esp_gatts_attr_db_t s_gatt_db[IDX_NB] = {
    // Service Declaration
    [IDX_SVC] = {{ESP_GATT_AUTO_RSP}, {ESP_UUID_LEN_16, (uint8_t*)&s_primary_service_uuid},
                 ESP_GATT_PERM_READ, sizeof(uint16_t), sizeof(adv_service_uuid16), adv_service_uuid16},

    // TX Characteristic Declaration (Data Out)
    [IDX_CHAR_TX] = {{ESP_GATT_AUTO_RSP}, {ESP_UUID_LEN_16, (uint8_t*)&s_character_declaration_uuid},
                     ESP_GATT_PERM_READ, sizeof(uint8_t), sizeof(uint8_t), (uint8_t*)&s_char_prop_notify},

    // TX Characteristic Value
    [IDX_CHAR_TX_VAL] = {{ESP_GATT_AUTO_RSP}, {ESP_UUID_LEN_16, (uint8_t*)&SYNAPSE_GATTS_CHAR_UUID},
                         ESP_GATT_PERM_READ, 512, 0, NULL},

    // TX CCCD
    [IDX_CHAR_TX_CFG] = {{ESP_GATT_AUTO_RSP}, {ESP_UUID_LEN_16, (uint8_t*)&s_character_client_config_uuid},
                         ESP_GATT_PERM_READ | ESP_GATT_PERM_WRITE, sizeof(uint16_t), sizeof(s_ccc_disable), (uint8_t*)s_ccc_disable},

    // Sync Characteristic Declaration (Sync Marker In)
    [IDX_CHAR_SYNC] = {{ESP_GATT_AUTO_RSP}, {ESP_UUID_LEN_16, (uint8_t*)&s_character_declaration_uuid},
                       ESP_GATT_PERM_READ, sizeof(uint8_t), sizeof(uint8_t), (uint8_t*)&s_char_prop_write},

    // Sync Characteristic Value
    [IDX_CHAR_SYNC_VAL] = {{ESP_GATT_AUTO_RSP}, {ESP_UUID_LEN_16, (uint8_t*)&SYNAPSE_GATTS_SYNC_CHAR_UUID},
                           ESP_GATT_PERM_WRITE, sizeof(synapse_sync_marker_t), 0, NULL},

    // Sync CCCD
    [IDX_CHAR_SYNC_CFG] = {{ESP_GATT_AUTO_RSP}, {ESP_UUID_LEN_16, (uint8_t*)&s_character_client_config_uuid},
                           ESP_GATT_PERM_READ | ESP_GATT_PERM_WRITE, sizeof(uint16_t), sizeof(s_ccc_disable), (uint8_t*)s_ccc_disable},
};

// ============================================================================
// TX QUEUE STRUCTURE
// ============================================================================

typedef struct {
    uint8_t type; // 0=ECG, 1=PPG, 2=IMU, 3=SYNC
    uint8_t data[512];
    uint16_t len;
} ble_tx_item_t;

// ============================================================================
// FORWARD DECLARATIONS
// ============================================================================

static void gap_event_handler(esp_gap_ble_cb_event_t event, esp_ble_gap_cb_param_t* param);
static void gatts_event_handler(esp_gatts_cb_event_t event, esp_gatt_if_t gatts_if,
                                 esp_ble_gatts_cb_param_t* param);
static void gatts_profile_event_handler(esp_gatts_cb_event_t event, esp_gatt_if_t gatts_if,
                                         esp_ble_gatts_cb_param_t* param);
static void ble_tx_task(void* pvParameters);
static esp_err_t send_notification(uint16_t handle, const uint8_t* data, uint16_t len);

// ============================================================================
// INITIALIZATION
// ============================================================================

esp_err_t ble_lsl_bridge_init(void) {
    ESP_LOGI(TAG, "Initializing BLE LSL Bridge...");

    // Create TX queue
    s_ble_tx_queue = xQueueCreate(SYNAPSE_BLE_TX_QUEUE_SIZE, sizeof(ble_tx_item_t));
    if (!s_ble_tx_queue) {
        ESP_LOGE(TAG, "Failed to create BLE TX queue");
        return ESP_FAIL;
    }

    // Initialize BLE controller (already done in main, but ensure)
    esp_err_t err = esp_ble_gap_register_callback(gap_event_handler);
    if (err != ESP_OK) return err;

    err = esp_ble_gatts_register_callback(gatts_event_handler);
    if (err != ESP_OK) return err;

    err = esp_ble_gatts_app_register(0);
    if (err != ESP_OK) return err;

    // Set MTU
    err = esp_ble_gatt_set_local_mtu(SYNAPSE_BLE_MTU);
    if (err != ESP_OK) return err;

    // Start TX task
    xTaskCreatePinnedToCore(ble_tx_task, "ble_tx", 8192, NULL, 3, NULL, 1);

    ESP_LOGI(TAG, "BLE LSL Bridge initialized (MTU=%d)", SYNAPSE_BLE_MTU);
    return ESP_OK;
}

// ============================================================================
// GAP EVENT HANDLER
// ============================================================================

static void gap_event_handler(esp_gap_ble_cb_event_t event, esp_ble_gap_cb_param_t* param) {
    switch (event) {
        case ESP_GAP_BLE_ADV_DATA_SET_COMPLETE_EVT:
            esp_ble_gap_start_advertising(nullptr);
            break;

        case ESP_GAP_BLE_ADV_START_COMPLETE_EVT:
            if (param->adv_start_cmpl.status == ESP_BT_STATUS_SUCCESS) {
                ESP_LOGI(TAG, "Advertising started");
            }
            break;

        case ESP_GAP_BLE_UPDATE_CONN_PARAMS_EVT:
            ESP_LOGI(TAG, "Conn params updated: status=%d, min_int=%d, max_int=%d, latency=%d, timeout=%d",
                     param->update_conn_params.status,
                     param->update_conn_params.min_int,
                     param->update_conn_params.max_int,
                     param->update_conn_params.latency,
                     param->update_conn_params.timeout);
            break;

        default:
            break;
    }
}

// ============================================================================
// GATTS EVENT HANDLER
// ============================================================================

static void gatts_event_handler(esp_gatts_cb_event_t event, esp_gatt_if_t gatts_if,
                                 esp_ble_gatts_cb_param_t* param) {
    if (event == ESP_GATTS_REG_EVT) {
        if (param->reg.status == ESP_GATT_OK) {
            s_gatts_if = gatts_if;
        } else {
            ESP_LOGE(TAG, "GATTS register failed: %d", param->reg.status);
        }
    }
    gatts_profile_event_handler(event, gatts_if, param);
}

static void gatts_profile_event_handler(esp_gatts_cb_event_t event, esp_gatt_if_t gatts_if,
                                         esp_ble_gatts_cb_param_t* param) {
    switch (event) {
        case ESP_GATTS_CREATE_EVT:
            // Create service
            esp_ble_gatts_create_service(gatts_if, &s_gatt_db[IDX_SVC], IDX_NB, 0);
            break;

        case ESP_GATTS_CREATE_SVC_EVT:
            if (param->create.status == ESP_GATT_OK) {
                s_service_handle = param->create.service_handle;
                esp_ble_gatts_start_service(s_service_handle);

                // Get characteristic handles
                s_char_tx_handle = s_gatt_db[IDX_CHAR_TX_VAL].att_num;
                s_char_sync_handle = s_gatt_db[IDX_CHAR_SYNC_VAL].att_num;
            }
            break;

        case ESP_GATTS_START_EVT:
            // Configure advertising
            {
                esp_ble_adv_data_t adv_data = {
                    .set_scan_rsp = false,
                    .include_name = true,
                    .include_txpower = true,
                    .min_interval = 0x0006, // 7.5ms
                    .max_interval = 0x0010, // 20ms
                    .appearance = 0x00,
                    .manufacturer_len = 0,
                    .p_manufacturer_data = NULL,
                    .service_data_len = 0,
                    .p_service_data = NULL,
                    .service_uuid_len = 2,
                    .p_service_uuid = adv_service_uuid16,
                    .flag = ESP_BLE_ADV_FLAG_GEN_DISC | ESP_BLE_ADV_FLAG_BREDR_NOT_SPT,
                };
                esp_ble_gap_config_adv_data(&adv_data);
            }
            break;

        case ESP_GATTS_CONNECT_EVT:
            s_conn_id = param->connect.conn_id;
            s_connected = true;
            s_notifications_enabled = false;
            ESP_LOGI(TAG, "BLE connected: conn_id=%d", s_conn_id);

            // Update connection parameters for low latency
            esp_ble_conn_update_params_t conn_params = {
                .bda = {0}, // Filled by stack
                .min_int = 12,  // 15ms
                .max_int = 16,  // 20ms
                .latency = 0,
                .timeout = 400  // 4 seconds
            };
            memcpy(conn_params.bda, param->connect.remote_bda, 6);
            esp_ble_gap_update_conn_params(&conn_params);
            break;

        case ESP_GATTS_DISCONNECT_EVT:
            s_connected = false;
            s_notifications_enabled = false;
            ESP_LOGI(TAG, "BLE disconnected, restarting advertising");
            esp_ble_gap_start_advertising(nullptr);
            break;

        case ESP_GATTS_WRITE_EVT:
            {
                uint16_t handle = param->write.handle;
                uint16_t len = param->write.len;
                uint8_t* value = param->write.value;

                // CCCD for TX notifications
                if (handle == s_gatt_db[IDX_CHAR_TX_CFG].att_num) {
                    if (len == 2) {
                        uint16_t ccc = value[0] | (value[1] << 8);
                        s_notifications_enabled = (ccc & 0x0001) != 0;
                        ESP_LOGI(TAG, "TX notifications %s", s_notifications_enabled ? "ENABLED" : "DISABLED");
                    }
                }
                // Sync marker characteristic write (from hub)
                else if (handle == s_gatt_db[IDX_CHAR_SYNC_VAL].att_num) {
                    if (len == sizeof(synapse_sync_marker_t) && s_sync_callback) {
                        synapse_sync_marker_t marker;
                        memcpy(&marker, value, sizeof(marker));
                        s_sync_callback(&marker);
                    }
                }
            }
            break;

        case ESP_GATTS_CONF_EVT:
            // Notification confirmed
            break;

        default:
            break;
    }
}

// ============================================================================
// TX TASK
// ============================================================================

static void ble_tx_task(void* pvParameters) {
    ble_tx_item_t item;

    while (true) {
        if (xQueueReceive(s_ble_tx_queue, &item, pdMS_TO_TICKS(100)) == pdTRUE) {
            if (s_connected && s_notifications_enabled) {
                // Find correct handle based on type
                uint16_t handle = s_char_tx_handle;
                send_notification(handle, item.data, item.len);
            }
        }
    }
}

// ============================================================================
// SEND FUNCTIONS
// ============================================================================

static esp_err_t send_notification(uint16_t handle, const uint8_t* data, uint16_t len) {
    esp_ble_gatts_send_indicate(s_gatts_if, s_conn_id, handle, len, (uint8_t*)data, false);
    return ESP_OK;
}

esp_err_t ble_lsl_bridge_send_ecg(const synapse_ecg_sample_t* samples, size_t count) {
    if (!samples || count == 0 || !s_connected) return ESP_ERR_INVALID_STATE;

    // Pack samples into BLE payload (max 512 bytes / 6 bytes per sample = ~85 samples)
    // Format: [type:1][count:2][samples...]
    // Each ECG sample: timestamp(8) + value_mv(2) + lead_off(1) = 11 bytes (packed to 12)
    size_t max_samples = 40;
    size_t to_send = (count < max_samples) ? count : max_samples;

    ble_tx_item_t item;
    item.type = 0; // ECG
    item.data[0] = 0; // Type
    item.data[1] = (uint8_t)(to_send & 0xFF);
    item.data[2] = (uint8_t)(to_send >> 8);

    uint8_t* ptr = &item.data[3];
    for (size_t i = 0; i < to_send; i++) {
        // Pack timestamp (8 bytes)
        memcpy(ptr, &samples[i].timestamp_us, 8);
        ptr += 8;
        // Pack value_mv (2 bytes)
        memcpy(ptr, &samples[i].value_mv, 2);
        ptr += 2;
        // Pack lead_off (1 byte)
        *ptr++ = samples[i].lead_off ? 1 : 0;
        // Padding for alignment
        *ptr++ = 0;
    }
    item.len = (uint16_t)(ptr - item.data);

    xQueueSend(s_ble_tx_queue, &item, 0);
    return ESP_OK;
}

esp_err_t ble_lsl_bridge_send_ppg(const synapse_ppg_sample_t* samples, size_t count) {
    if (!samples || count == 0 || !s_connected) return ESP_ERR_INVALID_STATE;

    size_t max_samples = 60;
    size_t to_send = (count < max_samples) ? count : max_samples;

    ble_tx_item_t item;
    item.type = 1; // PPG
    item.data[0] = 1;
    item.data[1] = (uint8_t)(to_send & 0xFF);
    item.data[2] = (uint8_t)(to_send >> 8);

    uint8_t* ptr = &item.data[3];
    for (size_t i = 0; i < to_send; i++) {
        memcpy(ptr, &samples[i].timestamp_us, 8);
        ptr += 8;
        memcpy(ptr, &samples[i].red, 2);
        ptr += 2;
        memcpy(ptr, &samples[i].ir, 2);
        ptr += 2;
    }
    item.len = (uint16_t)(ptr - item.data);

    xQueueSend(s_ble_tx_queue, &item, 0);
    return ESP_OK;
}

esp_err_t ble_lsl_bridge_send_imu(const synapse_imu_sample_t* samples, size_t count) {
    if (!samples || count == 0 || !s_connected) return ESP_ERR_INVALID_STATE;

    size_t max_samples = 25;
    size_t to_send = (count < max_samples) ? count : max_samples;

    ble_tx_item_t item;
    item.type = 2; // IMU
    item.data[0] = 2;
    item.data[1] = (uint8_t)(to_send & 0xFF);
    item.data[2] = (uint8_t)(to_send >> 8);

    uint8_t* ptr = &item.data[3];
    for (size_t i = 0; i < to_send; i++) {
        memcpy(ptr, &samples[i].timestamp_us, 8);
        ptr += 8;
        memcpy(ptr, &samples[i].accel_x, 2); ptr += 2;
        memcpy(ptr, &samples[i].accel_y, 2); ptr += 2;
        memcpy(ptr, &samples[i].accel_z, 2); ptr += 2;
        memcpy(ptr, &samples[i].gyro_x, 2); ptr += 2;
        memcpy(ptr, &samples[i].gyro_y, 2); ptr += 2;
        memcpy(ptr, &samples[i].gyro_z, 2); ptr += 2;
        memcpy(ptr, &samples[i].mag_x, 2); ptr += 2;
        memcpy(ptr, &samples[i].mag_y, 2); ptr += 2;
        memcpy(ptr, &samples[i].mag_z, 2); ptr += 2;
    }
    item.len = (uint16_t)(ptr - item.data);

    xQueueSend(s_ble_tx_queue, &item, 0);
    return ESP_OK;
}

esp_err_t ble_lsl_bridge_send_sync_marker(const synapse_sync_marker_t* marker) {
    if (!marker || !s_connected) return ESP_ERR_INVALID_STATE;

    ble_tx_item_t item;
    item.type = 3; // SYNC
    item.len = sizeof(synapse_sync_marker_t) + 1;
    item.data[0] = 3;
    memcpy(&item.data[1], marker, sizeof(synapse_sync_marker_t));

    xQueueSend(s_ble_tx_queue, &item, 0);
    return ESP_OK;
}

void ble_lsl_bridge_register_sync_callback(void (*callback)(const synapse_sync_marker_t*)) {
    s_sync_callback = callback;
}

bool ble_lsl_bridge_is_connected(void) {
    return s_connected && s_notifications_enabled;
}

void ble_lsl_bridge_deinit(void) {
    if (s_ble_tx_queue) {
        vQueueDelete(s_ble_tx_queue);
        s_ble_tx_queue = NULL;
    }
    s_connected = false;
    s_notifications_enabled = false;
    ESP_LOGI(TAG, "BLE LSL Bridge deinitialized");
}