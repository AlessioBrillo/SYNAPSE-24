/**
 * @file app_main.c
 * @brief Synapse Band v1 - Main Application Entry Point
 * 
 * Firmware per Synapse Band v1 (wrist wearable: ECG+PPG+IMU+GPS+Temp)
 * Architettura: FreeRTOS + ESP-IDF 5.2+, BLE GATT Server + FIT + Provisioning + OTA
 */

#include <string.h>
#include <sys/time.h>
#include <time.h>
#include "sdkconfig.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/event_groups.h"
#include "esp_system.h"
#include "esp_log.h"
#include "esp_timer.h"
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
#include "synapse_feature_map.h"
#include "synapse_rr.h"

static const char *TAG = "SYNAPSE_BAND";

#if defined(CONFIG_SYNAPSE_FEATURE_WIRED_SYNC)
static synapse_wired_sync_t s_wired_sync = {0};
#endif

// Event bits for system state
#define PROVISIONING_DONE_BIT   BIT1
#define OTA_IN_PROGRESS_BIT     BIT2
#define ACQUISITION_RUNNING_BIT BIT3

static EventGroupHandle_t s_system_events;

#if defined(CONFIG_SYNAPSE_RR_ENABLE)
static synapse_rr_t s_rr;  // beat detector fed from the infrared PPG samples
#endif

// Consumer contract v2 glue. The BLE layer owns the wire format; what the band knows
// (sensor health, counters, whether the raw streams run) is provided from here.
static bool s_transmitting = true;

// The phone sends the time on every connection (control point set_time): the band has no
// RTC and no network. Everything that needs a date (the night window, FIT timestamps)
// waits for it.
static void on_set_time(uint64_t epoch_ms)
{
    struct timeval tv = {
        .tv_sec = (time_t)(epoch_ms / 1000ULL),
        .tv_usec = (suseconds_t)((epoch_ms % 1000ULL) * 1000ULL),
    };
    if (settimeofday(&tv, NULL) == 0) {
        ESP_LOGI(TAG, "wall clock set from the phone: %llu ms", (unsigned long long)epoch_ms);
    } else {
        ESP_LOGW(TAG, "settimeofday failed");
    }
}

static void on_transmit_request(bool on)
{
    s_transmitting = on;
    (void)synapse_research_service_set_transmitting(on);
}

static void fill_status(synapse_status_v2_t *st)
{
    st->tier = 0;  // ponytail: the acquisition FSM does not expose its tier yet
    st->sensor_health = synapse_sensors_health_mask();
    st->flags = s_transmitting ? SYNAPSE_V2_FLAG_TRANSMITTING : 0;
    st->uptime_s = (uint32_t)(esp_timer_get_time() / 1000000);
    st->epoch_wraps = (uint16_t)synapse_research_service_epoch_wraps();
    // Both rings: the research service's and the scheduler's (a sample the scheduler could not
    // store never reached the research service, and used to vanish without a trace).
    st->ring_overflow = synapse_research_service_ring_overflow() + synapse_sensors_dropped_samples();
    st->stale_samples = synapse_research_service_stale_samples();
    st->dropped_frames = synapse_research_service_dropped_frames();
    st->read_errors = synapse_sensors_read_errors();
    st->fw_build = 0;  // ponytail: no build hash is embedded yet; 0 = unknown
    st->ppg_skipped = synapse_sensors_ppg_skipped();
    st->power_dropped = synapse_research_service_power_dropped();
}

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

    synapse_ble_gatt_set_status_provider(fill_status);
    synapse_ble_gatt_set_transmit_handler(on_transmit_request);
    synapse_ble_gatt_set_time_handler(on_set_time);

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
    synapse_power_set_battery_sink(synapse_ble_gatt_battery_changed);

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
#if defined(CONFIG_SYNAPSE_RR_ENABLE)
    synapse_rr_init(&s_rr);
#endif
    ESP_LOGI(TAG, "Entering main loop");
    while (1) {
        // Drain the sensor queue into the on-device processors AND the raw
        // research stream. The research service never derives features; it
        // forwards samples so the host can rebuild the full dataset.
        synapse_sensor_sample_t sample;
        for (int n = 0; n < 64 && synapse_sensors_get_sample(&sample) == ESP_OK; n++) {
            synapse_signal_quality_process(&sample.base);
#if defined(CONFIG_SYNAPSE_RR_ENABLE)
            if (sample.base.type == SENSOR_TYPE_PPG) {
                (void)synapse_rr_push(&s_rr, sample.base.data.ppg.ir, sample.base.timestamp_us);
            }
#endif
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
            // `valid` is never cleared by the quality module: without an age check the heart
            // rate and the quality freeze at the last value once the finger leaves the sensor.
            if (ppg.valid && synapse_is_fresh(esp_timer_get_time(), ppg.timestamp_us,
                                              SYNAPSE_PPG_MAX_AGE_US)) {
                // synapse_hr_to_u16: rounded, and 0 / NaN / implausible bpm is "invalid"
                // (0 bpm is not a legal Heart Rate Measurement Value either).
                hr_bpm = synapse_hr_to_u16(ppg.hr_bpm);
                ppg_sqi = synapse_unit_to_u8(ppg.sqi);
                ppg_map = synapse_unit_to_u8(ppg.motion_artifact_prob);
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

            // Consumer contract v2: the same measurements, with everything not measured
            // left at its NA sentinel (a consumer reads null, never 0).
            synapse_feature_v2_t v2;
            synapse_feature_v2_init_na(&v2);
            v2.t_dev_ms = (uint32_t)(esp_timer_get_time() / 1000);
            v2.heart_rate_bpm = hr_bpm;
            v2.ppg_sqi = ppg_sqi;
            v2.ecg_quality = ecg_quality;
            v2.motion_artifact = ppg_map;  // v1's ppg_map: the PPG window is motion-corrupted
            if (stress.valid) v2.stress_level = stress_level;  // otherwise it stays NA (null)
            v2.sensor_health = synapse_sensors_health_mask();
            if (s_transmitting) v2.flags |= SYNAPSE_V2_FLAG_TRANSMITTING;
#if defined(CONFIG_SYNAPSE_RR_ENABLE)
            // RR and HRV leave only with a frame that really goes out (the intervals are consumed
            // when reported) and only from a fresh, clean reading: otherwise they stay NA.
            if (synapse_ble_gatt_feature_v2_due()) {
                uint16_t rr_units[4];
                const uint32_t n_rr = synapse_rr_take(&s_rr, rr_units, 4);
                const int publishable = synapse_rr_publishable(
                    ppg_sqi != SYNAPSE_NA_U8, ppg.sqi, ppg.motion_artifact_prob);
                if (publishable && n_rr > 0) {
                    const uint32_t held = n_rr < 4 ? n_rr : 4;
                    v2.rr_last = rr_units[held - 1];
                    // How many intervals happened since the last frame: more than one means the
                    // others were not carried, and a consumer must not pretend they were.
                    v2.rr_count = (uint8_t)(n_rr < 254 ? n_rr : 254);
                }
                float rmssd = 0.0f, sdnn = 0.0f;
                if (publishable && synapse_rr_hrv(&s_rr, &rmssd, &sdnn)) {
                    v2.rmssd_ms = (uint16_t)(rmssd < 65534.0f ? rmssd + 0.5f : 65534.0f);
                    v2.sdnn_ms = (uint16_t)(sdnn < 65534.0f ? sdnn + 0.5f : 65534.0f);
                }
            }
#endif
            (void)synapse_ble_gatt_push_feature_v2(&v2);
            (void)synapse_ble_gatt_push_status();
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