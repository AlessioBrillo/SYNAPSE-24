/**
 * @file app_main.c
 * @brief Synapse Band v1 - Main Application Entry Point
 * 
 * Firmware per Synapse Band v1 (wrist wearable: ECG+PPG+IMU+GPS+Temp)
 * Architettura: FreeRTOS + ESP-IDF 5.2+, BLE GATT Server + FIT + Provisioning + OTA
 */

#include <string.h>
#include "sdkconfig.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/event_groups.h"
#include "esp_system.h"
#include "esp_log.h"
#include "esp_err.h"
#include "nvs_flash.h"
#include "esp_bt.h"
#include "esp_bt_main.h"
#include "esp_gap_ble_api.h"
#include "esp_gatts_api.h"
#include "esp_ota_ops.h"
#include "esp_partition.h"

#include "ble_gatt_server.h"
#include "fit_writer.h"
#include "synapse_provisioning.h"
#include "synapse_ota.h"
#include "synapse_acquisition.h"
#include "synapse_sensors.h"
#include "synapse_edge_ai.h"
#include "synapse_signal_quality.h"
#include "synapse_config.h"
#include "synapse_power.h"

static const char *TAG = "SYNAPSE_BAND";

// Event bits for system state
#define WIFI_CONNECTED_BIT      BIT0
#define PROVISIONING_DONE_BIT   BIT1
#define OTA_IN_PROGRESS_BIT     BIT2
#define ACQUISITION_RUNNING_BIT BIT3

static EventGroupHandle_t s_system_events;

void app_main(void)
{
    ESP_LOGI(TAG, "=== Synapse Band v1 Starting ===");
    ESP_LOGI(TAG, "Firmware version: %s", CONFIG_SYNAPSE_FIRMWARE_VERSION);
    ESP_LOGI(TAG, "Hardware version: %s", CONFIG_SYNAPSE_HARDWARE_VERSION);

    // 1. Initialize NVS
    esp_err_t ret = nvs_flash_init();
    if (ret == ESP_ERR_NVS_NO_FREE_PAGES || ret == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        ret = nvs_flash_init();
    }
    ESP_ERROR_CHECK(ret);

    // 2. Create system event group
    s_system_events = xEventGroupCreate();
    if (s_system_events == NULL) {
        ESP_LOGE(TAG, "Failed to create event group");
        return;
    }

    // 3. Load hardware configuration
    ret = synapse_config_load();
    if (ret != ESP_OK) {
        ESP_LOGW(TAG, "Config load failed, using defaults: %s", esp_err_to_name(ret));
    }

    // 4. Initialize sensors (ECG, PPG, IMU, GPS, Temp)
    ret = synapse_sensors_init();
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "Sensor init failed: %s", esp_err_to_name(ret));
        return;
    }

    // 5. Initialize signal quality module
    synapse_signal_quality_init();

    // 6. Initialize Edge AI models (stress triage, motion classifier)
    ret = synapse_edge_ai_init();
    if (ret != ESP_OK) {
        ESP_LOGW(TAG, "Edge AI init failed: %s", esp_err_to_name(ret));
    }

    // 7. Initialize FIT writer (LittleFS)
    ret = synapse_fit_init();
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "FIT init failed: %s", esp_err_to_name(ret));
        return;
    }

    // 8. Initialize BLE GATT Server
    ret = synapse_ble_gatt_init();
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "BLE GATT init failed: %s", esp_err_to_name(ret));
        return;
    }

    // 9. Initialize provisioning module
    ret = synapse_provisioning_init(s_system_events);
    if (ret != ESP_OK) {
        ESP_LOGW(TAG, "Provisioning init failed: %s", esp_err_to_name(ret));
    }

    // 10. Initialize OTA module
    ret = synapse_ota_init();
    if (ret != ESP_OK) {
        ESP_LOGW(TAG, "OTA init failed: %s", esp_err_to_name(ret));
    }

    // 11. Initialize acquisition FSM (T0/T1/T2)
    ret = synapse_acquisition_init(s_system_events);
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "Acquisition init failed: %s", esp_err_to_name(ret));
        return;
    }

    // 12. Initialize power management
    synapse_power_init();

    // 13. Check if provisioning is needed
    bool provisioned = synapse_provisioning_is_complete();
    if (!provisioned) {
        ESP_LOGI(TAG, "Device not provisioned, starting provisioning mode");
        synapse_ble_gatt_start_advertising_provisioning();
    } else {
        ESP_LOGI(TAG, "Device provisioned, starting normal operation");
        synapse_ble_gatt_start_advertising_normal();
        
        // Connect to WiFi and cloud
        synapse_provisioning_connect_wifi();
    }

    // 14. Main loop - runs acquisition FSM and handles events
    ESP_LOGI(TAG, "Entering main loop");
    while (1) {
        // Handle acquisition FSM tick (T0/T1/T2 state machine)
        synapse_acquisition_tick();

        // Handle provisioning if in progress
        synapse_provisioning_process();

        // Handle OTA if in progress
        synapse_ota_process();

        // Process BLE events
        synapse_ble_gatt_process_events();

        // Power management tick
        synapse_power_tick();

        vTaskDelay(pdMS_TO_TICKS(10));
    }
}

// Public API for other modules to get/set system events
EventGroupHandle_t synapse_get_system_events(void)
{
    return s_system_events;
}

void synapse_set_wifi_connected(bool connected)
{
    if (connected) {
        xEventGroupSetBits(s_system_events, WIFI_CONNECTED_BIT);
    } else {
        xEventGroupClearBits(s_system_events, WIFI_CONNECTED_BIT);
    }
}

bool synapse_is_wifi_connected(void)
{
    return (xEventGroupGetBits(s_system_events) & WIFI_CONNECTED_BIT) != 0;
}

void synapse_set_provisioning_done(bool done)
{
    if (done) {
        xEventGroupSetBits(s_system_events, PROVISIONING_DONE_BIT);
    } else {
        xEventGroupClearBits(s_system_events, PROVISIONING_DONE_BIT);
    }
}

bool synapse_is_provisioning_done(void)
{
    return (xEventGroupGetBits(s_system_events) & PROVISIONING_DONE_BIT) != 0;
}

void synapse_set_ota_in_progress(bool in_progress)
{
    if (in_progress) {
        xEventGroupSetBits(s_system_events, OTA_IN_PROGRESS_BIT);
    } else {
        xEventGroupClearBits(s_system_events, OTA_IN_PROGRESS_BIT);
    }
}

bool synapse_is_ota_in_progress(void)
{
    return (xEventGroupGetBits(s_system_events) & OTA_IN_PROGRESS_BIT) != 0;
}