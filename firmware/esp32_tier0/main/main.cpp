#include "esp_log.h"
#include "esp_system.h"
#include "esp_timer.h"
#include "nvs_flash.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/queue.h"
#include "driver/gpio.h"

#include "sensor_scheduler.h"
#include "ecg_ad8232.h"
#include "ppg_max30102.h"
#include "ppg_sqi.h"
#include "imu_icm20948.h"
#include "ble_lsl_bridge.h"
#include "sync/sync_marker_handler.h"
#include "sync/wired_sync_handler.h"
#include "triage/triage_inference.h"
#include "triage/triage_features.h"
#include "power_monitor.h"

static const char* TAG = "synapse_tier0";

#define SCHEDULER_QUEUE_SIZE 64

static sensor_scheduler_t g_scheduler;
static ble_lsl_bridge_t g_ble_bridge;
static sync_marker_handler_t g_sync_handler;
static triage_inference_t g_triage;
static synapse_wired_sync_t g_wired_sync;
static QueueHandle_t g_scheduler_queue = NULL;

static TaskHandle_t g_main_task = NULL;
static TaskHandle_t g_triage_task = NULL;
static TaskHandle_t g_quality_task = NULL;

static ecg_ad8232_config_t g_ecg_config = {
    .adc_channel = ADC1_CHANNEL_0,
    .gpio_drdy = GPIO_NUM_4,
    .vref_mv = 1100.0f,
    .gain = 6.0f
};

static ppg_max30102_config_t g_ppg_config = {
    .i2c_port = I2C_NUM_0,
    .i2c_addr = 0x57,
    .gpio_int = GPIO_NUM_5,
    .led_current_red = 0x1F,
    .led_current_ir = 0x1F,
    .led_current_green = 0x00,
    .sample_rate = 0x03,
    .pulse_width = 0x03,
    .adc_range = 0x03
};

static imu_icm20948_config_t g_imu_config = {
    .i2c_port = I2C_NUM_0,
    .i2c_addr = 0x68,
    .gpio_int = GPIO_NUM_6,
    .accel_fsr_g = 8,
    .gyro_fsr_dps = 500,
    .accel_odr_hz = 100,
    .gyro_odr_hz = 100
};

// Motion gate state (Architecture.md §74)
static ppg_sqi_result_t g_latest_sqi = {0};
static int g_consecutive_clean = 0;
static bool g_motion_gate_armed = false;

static void triage_task_fn(void* arg) {
    (void)arg;
    triage_input_t input = {0};
    triage_output_t output = {0};
    triage_features_t features = {0};
    TickType_t last_wake = xTaskGetTickCount();

    while (1) {
        vTaskDelayUntil(&last_wake, pdMS_TO_TICKS(200));  // 5Hz inference

        if (!g_triage.initialized) continue;

        // Get features from ring buffers (matches Python extract_triage_features_live)
        if (triage_features_compute_live(&g_scheduler, NULL, &features) != ESP_OK) {
            continue;  // Not enough data
        }

        // Copy to triage input (quantization happens in triage_inference_run)
        input.timestamp_us = features.timestamp_us;
        input.feature_count = features.feature_count;
        for (int i = 0; i < TRIAGE_NUM_FEATURES; i++) {
            input.features[i] = features.features[i];
        }

        if (triage_inference_run(&g_triage, &input, &output) == ESP_OK && output.valid) {
            ESP_LOGI(TAG, "Triage: baseline=%.3f, stress=%.3f, artifact=%.3f, class=%d, time=%" PRId64 " us",
                     output.baseline_prob, output.stress_prob, output.artifact_prob, 
                     output.predicted_class, output.inference_time_us);
        }
    }
}

// Quality assessment task - outputs PPG SQI at 100Hz for motion gate (Architecture.md §74)
static void quality_task_fn(void* arg) {
    (void)arg;
    TickType_t last_wake = xTaskGetTickCount();
    const TickType_t period_ticks = pdMS_TO_TICKS(10);  // 100Hz

    ESP_LOGI(TAG, "Quality task started (100Hz PPG SQI for motion gate)");

    while (1) {
        vTaskDelayUntil(&last_wake, period_ticks);

        // Get latest SQI from PPG driver
        ppg_sqi_result_t sqi_result;
        if (ppg_max30102_get_sqi(&sqi_result) == ESP_OK) {
            g_latest_sqi = sqi_result;

            // Motion gate logic (Architecture.md §74: 2 consecutive clean assessments)
            bool clean = (sqi_result.sqi >= SYNAPSE_T0_PPG_SQI_MIN) && 
                         (sqi_result.motion_artifact_prob <= SYNAPSE_T0_PPG_MAP_MAX);
            
            if (clean) {
                g_consecutive_clean++;
                if (g_consecutive_clean >= 2) {
                    g_motion_gate_armed = true;
                }
            } else {
                g_consecutive_clean = 0;
                g_motion_gate_armed = false;
            }

            ESP_LOGD(TAG, "PPG SQI: sqi=%.3f, pi=%.3f, map=%.3f, clean=%d, gate_armed=%d",
                     sqi_result.sqi, sqi_result.perfusion_index, sqi_result.motion_artifact_prob,
                     clean, g_motion_gate_armed);
        }
    }
}

static void sync_marker_callback(uint32_t sequence, int64_t hub_timestamp_us, int64_t pod_timestamp_us, void* user_ctx) {
    (void)user_ctx;
    ESP_LOGD(TAG, "Sync marker callback: seq=%" PRIu32 ", hub=%" PRId64 ", pod=%" PRId64, sequence, hub_timestamp_us, pod_timestamp_us);

    ble_lsl_bridge_send_sync_marker(&g_ble_bridge, sequence, pod_timestamp_us);
}

static void wired_sync_pulse_callback(int64_t timestamp_us, void* user_ctx) {
    (void)user_ctx;
    ESP_LOGD(TAG, "Wired sync pulse received at %" PRId64 " us", timestamp_us);
}

static void main_task_fn(void* arg) {
    (void)arg;
    sensor_sample_t sample;

    ESP_LOGI(TAG, "SYNAPSE-24 Tier 0 firmware starting...");

    ESP_ERROR_CHECK(nvs_flash_init());
    ESP_ERROR_CHECK(esp_timer_init());

    g_scheduler_queue = xQueueCreate(SCHEDULER_QUEUE_SIZE, sizeof(sensor_sample_t));
    if (!g_scheduler_queue) {
        ESP_LOGE(TAG, "Failed to create scheduler queue");
        return;
    }

    ESP_ERROR_CHECK(sensor_scheduler_init(&g_scheduler, g_scheduler_queue));

    TaskHandle_t ecg_task, ppg_task, imu_task;
    sensor_config_t ecg_sensor = {
        .type = SENSOR_TYPE_ECG,
        .name = "ECG_AD8232",
        .sampling_rate_hz = 500,  // Fixed: Architecture.md §34 requires 500Hz for Tier 0
        .init = (sensor_init_fn_t)ecg_ad8232_init,
        .read = (sensor_read_fn_t)ecg_ad8232_read,
        .deinit = (sensor_deinit_fn_t)ecg_ad8232_deinit,
        .user_ctx = &g_ecg_config,
        .task_handle = &ecg_task
    };

    sensor_config_t ppg_sensor = {
        .type = SENSOR_TYPE_PPG,
        .name = "PPG_MAX30102",
        .sampling_rate_hz = 64,
        .init = (sensor_init_fn_t)ppg_max30102_init,
        .read = (sensor_read_fn_t)ppg_max30102_read,
        .deinit = (sensor_deinit_fn_t)ppg_max30102_deinit,
        .user_ctx = &g_ppg_config,
        .task_handle = &ppg_task
    };

    sensor_config_t imu_sensor = {
        .type = SENSOR_TYPE_IMU,
        .name = "IMU_ICM20948",
        .sampling_rate_hz = 100,
        .init = (sensor_init_fn_t)imu_icm20948_init,
        .read = (sensor_read_fn_t)imu_icm20948_read,
        .deinit = (sensor_deinit_fn_t)imu_icm20948_deinit,
        .user_ctx = &g_imu_config,
        .task_handle = &imu_task
    };

    xTaskCreate(sensor_task_fn, "ecg_task", 4096, &ecg_sensor, 10, &ecg_task);
    xTaskCreate(sensor_task_fn, "ppg_task", 4096, &ppg_sensor, 10, &ppg_task);
    xTaskCreate(sensor_task_fn, "imu_task", 4096, &imu_sensor, 10, &imu_task);

    ESP_ERROR_CHECK(sensor_scheduler_register_sensor(&g_scheduler, &ecg_sensor));
    ESP_ERROR_CHECK(sensor_scheduler_register_sensor(&g_scheduler, &ppg_sensor));
    ESP_ERROR_CHECK(sensor_scheduler_register_sensor(&g_scheduler, &imu_sensor));

    ESP_ERROR_CHECK(ble_lsl_bridge_init(&g_ble_bridge, g_scheduler_queue));
    ESP_ERROR_CHECK(ble_lsl_bridge_start(&g_ble_bridge));

    ESP_ERROR_CHECK(sync_marker_handler_init(&g_sync_handler));

    // Initialize wired GPIO sync (Architecture.md §29, hardware_bringup.yaml GPIO 27)
    ESP_ERROR_CHECK(synapse_wired_sync_init(&g_wired_sync, SYNAPSE_SYNC_ROLE_HUB));
    g_wired_sync.on_sync_pulse = wired_sync_pulse_callback;
    ESP_LOGI(TAG, "Wired sync initialized on GPIO %d (hub role) — GPIO 21 reserved for I2C SDA", SYNAPSE_WIRED_SYNC_GPIO);

    // Initialize PPG SQI for motion gate (Architecture.md §74)
    ESP_ERROR_CHECK(ppg_sqi_init());

    // Initialize power budget monitor (Architecture.md §55-62)
    ESP_ERROR_CHECK(power_monitor_init());

    // Initialize triage inference with embedded model
    ESP_ERROR_CHECK(triage_inference_init(&g_triage));

    // Start quality assessment task (100Hz PPG SQI output for motion gate)
    xTaskCreate(quality_task_fn, "quality_task", 4096, NULL, 5, &g_quality_task);

    xTaskCreate(triage_task_fn, "triage_task", 8192, NULL, 5, &g_triage_task);

    ESP_ERROR_CHECK(sensor_scheduler_start(&g_scheduler));

    ESP_LOGI(TAG, "All subsystems started. Entering main loop...");

    uint32_t sample_counts[SENSOR_SCHEDULER_MAX_SENSORS] = {0};
    uint32_t dropped_samples[SENSOR_SCHEDULER_MAX_SENSORS] = {0};
    TickType_t last_stats = xTaskGetTickCount();
    TickType_t last_wake = xTaskGetTickCount();

    while (1) {
        if (xQueueReceive(g_scheduler_queue, &sample, pdMS_TO_TICKS(100)) == pdTRUE) {
            ble_lsl_bridge_send_sample(&g_ble_bridge, &sample);
        }

        // Periodic wired sync pulse (Tier 0: 60s interval per Architecture.md §92)
        static TickType_t last_sync_pulse = 0;
        if (xTaskGetTickCount() - last_sync_pulse >= pdMS_TO_TICKS(60000)) {
            synapse_wired_sync_send_pulse(&g_wired_sync);
            last_sync_pulse = xTaskGetTickCount();
        }

        if (xTaskGetTickCount() - last_wake >= pdMS_TO_TICKS(10000)) {
            sensor_scheduler_get_stats(&g_scheduler, sample_counts, dropped_samples);
            ESP_LOGI(TAG, "Stats: ECG=%" PRIu32 ", PPG=%" PRIu32 ", IMU=%" PRIu32 " | Dropped: ECG=%" PRIu32 ", PPG=%" PRIu32 ", IMU=%" PRIu32,
                     sample_counts[0], sample_counts[1], sample_counts[2],
                     dropped_samples[0], dropped_samples[1], dropped_samples[2]);

            ESP_LOGI(TAG, "PPG SQI: sqi=%.3f, pi=%.3f%%, map=%.3f, gate_armed=%d, consecutive_clean=%d",
                     g_latest_sqi.sqi, g_latest_sqi.perfusion_index, g_latest_sqi.motion_artifact_prob,
                     g_motion_gate_armed, g_consecutive_clean);

            uint32_t markers;
            float drift;
            int64_t offset;
            sync_marker_handler_get_stats(&g_sync_handler, &markers, &drift, &offset);
            ESP_LOGI(TAG, "Sync: markers=%" PRIu32 ", drift=%.2f ppm, offset=%" PRId64 " us", markers, drift, offset);

            size_t arena_used;
            triage_inference_get_model_info(&g_triage, NULL, &arena_used);
            ESP_LOGI(TAG, "TFLM arena used: %zu bytes", arena_used);

            // Power budget update (Architecture.md §55-62)
            power_monitor_update();

            last_stats = xTaskGetTickCount();
        }
    }
}

extern "C" void app_main(void) {
    ESP_LOGI(TAG, "SYNAPSE-24 ESP32-S3 Tier 0 Firmware v0.1.0");
    ESP_LOGI(TAG, "Architecture.md: Decoupled sensor pod, Tier 0 continuous H24");
    ESP_LOGI(TAG, "Roadmap.md: Live ECG+PPG+IMU streaming, synchronized in LSL");

    xTaskCreate(main_task_fn, "main_task", 8192, NULL, 5, &g_main_task);
}