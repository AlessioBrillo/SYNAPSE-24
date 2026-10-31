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
#include "esp_efuse.h"
// The MAC helper lives in esp_mac.h (the esp_hw_support component) since
// IDF 5.x: including only esp_efuse.h leaves the call undeclared.
#include "esp_mac.h"
#include "nvs_flash.h"
#include "esp_ota_ops.h"
#include "esp_partition.h"

#include "ble_gatt_server.h"
#include "ble_research_service.h"
#include "wired_sync_handler.h"
#if defined(CONFIG_SYNAPSE_FEATURE_FIT_STORAGE)
#include "fit_writer.h"
#endif
#if defined(CONFIG_SYNAPSE_FEATURE_PROVISIONING)
#include "synapse_provisioning.h"
#endif
#if defined(CONFIG_SYNAPSE_FEATURE_OTA)
#include "synapse_ota.h"
#endif
#include "synapse_acquisition.h"
#include "synapse_sensors.h"
#include "synapse_edge_ai.h"
#include "synapse_signal_quality.h"
#include "synapse_config.h"
#include "synapse_power.h"

static const char *TAG = "SYNAPSE_BAND";

#if defined(CONFIG_SYNAPSE_FEATURE_WIRED_SYNC)
static synapse_wired_sync_t s_wired_sync = {0};
#endif

// Event bits for system state
#define PROVISIONING_DONE_BIT   BIT1
#define OTA_IN_PROGRESS_BIT     BIT2
#define ACQUISITION_RUNNING_BIT BIT3

static EventGroupHandle_t s_system_events;

// Consumer feature stream (Averyn integration).
// synapse_ble_gatt_push_feature() is rate-limited to 1 Hz internally; we call
// it every main-loop tick and let it drop when the gate is closed, so the
// radio never dominates the power budget.

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
    //    band-core build: ECG + PPG + IMU only.
    ret = synapse_sensors_init();
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "Sensor init failed: %s", esp_err_to_name(ret));
        return;
    }

    // 4b. Start acquisition so the research service has samples to forward.
    ret = synapse_sensors_start();
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "Sensor start failed: %s", esp_err_to_name(ret));
        return;
    }

    // 5. Initialize signal quality module
    synapse_signal_quality_init();

    // 6. Initialize Edge AI models (stress triage, motion classifier).
    //    ponytail: off until the interpreter/arena bug is fixed (see Kconfig help).
#if defined(CONFIG_SYNAPSE_FEATURE_EDGE_AI)
    ret = synapse_edge_ai_init();
    if (ret != ESP_OK) {
        ESP_LOGW(TAG, "Edge AI init failed: %s", esp_err_to_name(ret));
    }
#endif

    // 7. Initialize FIT writer (LittleFS). Gated by the Kconfig flag: the
    //    band-core build (sdkconfig.defaults.core) ships without FIT storage,
    //    and a mount failure must not abort the boot of a device whose
    //    dataset path is the BLE research service, not the flash file.
#if defined(CONFIG_SYNAPSE_FEATURE_FIT_STORAGE)
    ret = synapse_fit_init();
    if (ret != ESP_OK) {
        ESP_LOGW(TAG, "FIT init failed (%s): FIT recording disabled",
                 esp_err_to_name(ret));
    }
#endif

    // 8. Initialize BLE GATT Server
    ret = synapse_ble_gatt_init();
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "BLE GATT init failed: %s", esp_err_to_name(ret));
        return;
    }

    // 8a. Device Information: serial derived from the public MAC so Averyn can
    //     bind device -> owner (serial printed on box/QR). HW rev from Kconfig.
    {
        uint8_t mac[6] = {0};
        esp_efuse_mac_get_default(mac);
        char serial[16];
        snprintf(serial, sizeof(serial), "SYN-%02X%02X%02X", mac[3], mac[4], mac[5]);
        synapse_ble_gatt_set_device_info("Synapse", "Band v1", serial,
                                         CONFIG_SYNAPSE_FIRMWARE_VERSION,
                                         CONFIG_SYNAPSE_HARDWARE_VERSION);
    }

    // 8b. The Synapse Research Service (raw samples + on-device timestamps)
    //     is registered by synapse_ble_gatt_init(), BEFORE the NimBLE host
    //     task starts: after the host starts, ble_gatts_add_svcs() fails
    //     with BLE_HS_EBUSY. It is the dataset path
    //     (config/hardware.yaml -> raw_frame) and is independent of the
    //     consumer feature stream registered above.

    // 9. Initialize provisioning module
#if defined(CONFIG_SYNAPSE_FEATURE_PROVISIONING)
    ret = synapse_provisioning_init(s_system_events);
    if (ret != ESP_OK) {
        ESP_LOGW(TAG, "Provisioning init failed: %s", esp_err_to_name(ret));
    }
#endif

    // 10. Initialize OTA module
#if defined(CONFIG_SYNAPSE_FEATURE_OTA)
    ret = synapse_ota_init();
    if (ret != ESP_OK) {
        ESP_LOGW(TAG, "OTA init failed: %s", esp_err_to_name(ret));
    }
#endif

    // 11. Initialize acquisition FSM (T0/T1/T2)
    ret = synapse_acquisition_init(s_system_events);
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "Acquisition init failed: %s", esp_err_to_name(ret));
        return;
    }

    // 11b. Start acquisition FSM task (1Hz tier transitions T0/T1/T2).
    // Without this the FSM stays inert and no tier change can ever occur.
    ret = synapse_acquisition_start();
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "Acquisition start failed: %s", esp_err_to_name(ret));
        return;
    }

    // 12. Initialize power management
    synapse_power_init();

    // 12b. Route measured power telemetry onto the research service's POWER
    //      stream. The power module stays BLE-agnostic: the wiring happens
    //      here (Architecture.md §23-31 decoupled pod/hub).
    synapse_power_set_telemetry_sink(synapse_research_service_submit_power);

    // 12c. Wired sync (GPIO 27) as HUB output: the forearm hub is the tier
    //      reference for a second pod. The driver ships now so the two-pod
    //      bench in the SOP can measure the pod↔pod residual; emitting the
    //      pulse is a G2 feature (real head pod on a second body site), so
    //      nothing drives the line during single-pod bringup.
#if defined(CONFIG_SYNAPSE_FEATURE_WIRED_SYNC)
    esp_err_t wired_rc = synapse_wired_sync_init(&s_wired_sync, SYNAPSE_SYNC_ROLE_HUB);
    if (wired_rc != ESP_OK) {
        ESP_LOGW(TAG, "wired sync init failed: %s", esp_err_to_name(wired_rc));
    }
#endif

    // 13. Check if provisioning is needed
#if defined(CONFIG_SYNAPSE_FEATURE_PROVISIONING)
    bool provisioned = synapse_provisioning_is_complete();
    if (!provisioned) {
        ESP_LOGI(TAG, "Device not provisioned, starting provisioning mode");
        synapse_ble_gatt_start_advertising_provisioning();
    } else {
        ESP_LOGI(TAG, "Device provisioned, starting normal operation");
        synapse_ble_gatt_start_advertising_normal();
    }
#else
    // Provisioning is compiled out (band-core): the device is always
    // provisioned, so it advertises in normal mode.
    ESP_LOGI(TAG, "Provisioning disabled, starting normal operation");
    synapse_ble_gatt_start_advertising_normal();
#endif

    // 14. Main loop - runs acquisition FSM and handles events
    ESP_LOGI(TAG, "Entering main loop");
    while (1) {
        // Drain the sensor queue into the on-device processors AND the raw
        // research stream. The research service never derives features; it
        // forwards samples so the host can rebuild the full dataset.
        synapse_sensor_sample_t sample;
        for (int n = 0; n < 64 && synapse_sensors_get_sample(&sample) == ESP_OK; n++) {
            synapse_signal_quality_process(&sample.base);
            synapse_research_service_submit(&sample.base);
        }

        // --- Consumer feature stream (Averyn integration) --------------------
        // Push a real feature frame every main-loop tick; push_feature() rate-
        // limits to 1 Hz internally and drops silently when nothing is
        // subscribed. Values come from the signal-quality and edge-AI modules
        // (no placeholders). When stress triage is not yet valid we emit
        // stress_level=3 (unknown) so Averyn never shows a fabricated class.
        {
            ppg_quality_t ppg = {0};
            stress_triage_t stress = {0};
            (void)synapse_signal_quality_get_ppg(&ppg);
#if defined(CONFIG_SYNAPSE_FEATURE_EDGE_AI)
            (void)synapse_edge_ai_get_latest_stress(&stress);
#endif

            uint8_t stress_level = SYNAPSE_STRESS_UNKNOWN;
            if (stress.valid) {
                if (stress.predicted_class == 0) stress_level = 0;   // baseline
                else if (stress.predicted_class == 1) stress_level = 1; // stress
                else if (stress.predicted_class == 2) stress_level = 2; // artifact
            }

            // 0xFF means "not available" and 0xFFFF means "invalid": a
            // fabricated 0 would be read as a perfect measurement (or, for
            // the heart rate, as a dead patient), which is worse than saying
            // "no value".
            uint16_t hr_bpm = 0xFFFF;   // invalid heart rate
            uint8_t ppg_sqi = 0xFF;     // not available
            uint8_t ppg_map = 0xFF;     // not available
            uint8_t ecg_quality = 0xFF; // the signal-quality module exposes no
                                       // validated ECG quality yet
            if (ppg.valid) {
                // 0 bpm is not a legal Heart Rate Measurement Value, so a
                // zero reading is reported as "invalid" instead.
                hr_bpm = (ppg.hr_bpm > 0.0f) ? (uint16_t)ppg.hr_bpm : 0xFFFF;
                ppg_sqi = (uint8_t)(ppg.sqi * 255.0f);
                ppg_map = (uint8_t)(ppg.motion_artifact_prob * 255.0f);
            }

            (void)synapse_ble_gatt_push_feature(
                hr_bpm,                     // hr_bpm (0xFFFF if invalid)
                0xFFFF,                     // rr_interval_ms (genuine RR only; not RMSSD)
                ppg_sqi,
                ppg_map,
                ecg_quality,
                stress_level,
                0xFF,                       // motion_level (0xFF = not available)
                0x7FFF                       // temperature 0.01°C (0x7FFF = invalid until TMP117 wired)
            );
        }
        // --------------------------------------------------------------------

        // Handle acquisition FSM tick (T0/T1/T2 state machine)
        synapse_acquisition_tick();

        // Handle provisioning if in progress
#if defined(CONFIG_SYNAPSE_FEATURE_PROVISIONING)
        synapse_provisioning_process();
#endif

        // Handle OTA if in progress
#if defined(CONFIG_SYNAPSE_FEATURE_OTA)
        synapse_ota_process();
#endif

        // Process BLE events
        synapse_ble_gatt_process_events();

        // Power management tick (self-limited to 1 Hz)
        synapse_power_tick();

        // Battery Service / feature frame: report the measured level. A reading
        // outside a single-cell LiPo range means the divider is not connected
        // (smoke kit on USB): keep the previous value instead of publishing noise.
        {
            static uint8_t s_last_batt_pct = 0xFF;
            synapse_power_status_t pst;
            if (synapse_power_get_status(&pst) == ESP_OK &&
                pst.voltage_v >= 3.0f && pst.voltage_v <= 4.4f &&
                pst.battery_level_pct != s_last_batt_pct) {
                s_last_batt_pct = pst.battery_level_pct;
                synapse_ble_gatt_battery_changed(pst.battery_level_pct, pst.charging);
            }
        }

        vTaskDelay(pdMS_TO_TICKS(10));
    }
}

// Public API for other modules to get/set system events
EventGroupHandle_t synapse_get_system_events(void)
{
    return s_system_events;
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