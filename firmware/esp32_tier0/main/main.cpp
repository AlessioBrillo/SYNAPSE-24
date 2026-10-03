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
#include "ppg_processor.h"
#include "imu_icm20948.h"
#include "imu_processor.h"
#include "ble_lsl_bridge.h"
#include "sync_marker_handler.h"
#include "wired_sync_handler.h"
#include "clock_sync.h"
#include "triage_inference.h"
#include "triage_features.h"
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
static TaskHandle_t g_ppg_feature_task = NULL;
static TaskHandle_t g_imu_feature_task = NULL;
static TaskHandle_t g_clock_sync_task = NULL;

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
    .sda_gpio_num = 8,
    .scl_gpio_num = 9,
    .led_current_red = 0x1F,
    .led_current_ir = 0x1F,
    .led_current_green = 0x0F,   // Green LED on for Tier 0 PPG
    .sample_rate = 0x02,         // 50 Hz (was 0x03 for ~64Hz higher rate)
    .pulse_width = 0x03,
    .adc_range = 0x03
};

static imu_icm20948_config_t g_imu_config = {
    .i2c_port = I2C_NUM_0,
    .i2c_addr = 0x68,
    .gpio_int = GPIO_NUM_6,
    .sda_gpio_num = 8,
    .scl_gpio_num = 9,
    .accel_fsr_g = 8,
    .gyro_fsr_dps = 500,
    .accel_odr_hz = 50,      // 50 Hz for Tier 0 (reduced power)
    .gyro_odr_hz = 50
};

// Motion gate state (Architecture.md §74)
static ppg_sqi_result_t g_latest_sqi = {};
static int g_consecutive_clean = 0;
static bool g_motion_gate_armed = false;

static void triage_task_fn(void* arg) {
    (void)arg;
    triage_input_t input = {};
    triage_output_t output = {};
    triage_features_t features = {};
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

// PPG Feature Processor Task: reads PPG samples from ring buffer at 64Hz, computes features at 10Hz
static void ppg_feature_task_fn(void* arg) {
    (void)arg;
    TickType_t last_wake = xTaskGetTickCount();
    const TickType_t period_ticks = pdMS_TO_TICKS(1000 / PPG_PROCESSOR_SAMPLE_RATE_HZ);  // 64Hz
    
    ESP_LOGI(TAG, "PPG Feature Task started (64Hz input, 10Hz output)");
    
    while (1) {
        vTaskDelayUntil(&last_wake, period_ticks);
        
        if (!g_scheduler.running) continue;
        
        // Pop PPG samples from ring buffer
        sensor_sample_t sample;
        sensor_ring_buffer_t* ppg_rb = &g_scheduler.buffers[SENSOR_TYPE_PPG];
        while (sensor_ring_buffer_pop(ppg_rb, &sample)) {
            ppg_sqi_result_t sqi;
            ppg_max30102_get_sqi(&sqi);
            
            ppg_features_t features;
            esp_err_t ret = ppg_processor_process_sample(
                sample.data.ppg.red, 
                sample.data.ppg.ir, 
                sample.timestamp_us, 
                &sqi, 
                &features
            );
            
            if (ret == ESP_OK && features.valid) {
                // Log feature output at 10Hz
                ESP_LOGD(TAG, "PPG Features: HR=%.1f BPM, RMSSD=%.1f ms, SDNN=%.1f ms, SNR=%.1f dB, PI=%.1f%%",
                         features.hr_bpm, features.rmssd_ms, features.sdnn_ms, features.snr_db, features.pi_percent);
                
                // TODO: Queue features for BLE transmission (compressed feature packet)
            }
        }
    }
}

// IMU Feature Processor Task: reads IMU samples from ring buffer at 100Hz, computes features at 1Hz
static void imu_feature_task_fn(void* arg) {
    (void)arg;
    TickType_t last_wake = xTaskGetTickCount();
    const TickType_t period_ticks = pdMS_TO_TICKS(1000 / IMU_PROCESSOR_SAMPLE_RATE_HZ);  // 100Hz
    
    ESP_LOGI(TAG, "IMU Feature Task started (100Hz input, 1Hz output)");
    
    while (1) {
        vTaskDelayUntil(&last_wake, period_ticks);
        
        if (!g_scheduler.running) continue;
        
        // Pop IMU samples from ring buffer
        sensor_sample_t sample;
        sensor_ring_buffer_t* imu_rb = &g_scheduler.buffers[SENSOR_TYPE_IMU];
        while (sensor_ring_buffer_pop(imu_rb, &sample)) {
            imu_features_t features;
            esp_err_t ret = imu_processor_process_sample(
                sample.data.imu.ax, sample.data.imu.ay, sample.data.imu.az,
                sample.data.imu.gx, sample.data.imu.gy, sample.data.imu.gz,
                sample.timestamp_us, &features
            );
            
            if (ret == ESP_OK) {
                // Log feature output at 1Hz
                ESP_LOGD(TAG, "IMU Features: motion=%.3fg, entropy=%.3f, dom_freq=%.2fHz, sleep_prob=%.3f, stationary=%d",
                         features.motion_intensity, features.spectral_entropy, features.dominant_freq_hz,
                         features.sleep_probability, features.is_stationary);
                
                // TODO: Use sleep_probability for Tier 0 -> Tier 1 promotion decision
                // TODO: Queue features for BLE transmission (compressed feature packet)
            }
        }
    }
}

// Clock Sync Task: 1Hz BLE timestamp exchange with linear drift model
static void clock_sync_task_fn(void* arg) {
    clock_sync_t* sync = (clock_sync_t*)arg;
    TickType_t last_wake = xTaskGetTickCount();
    const TickType_t period_ticks = pdMS_TO_TICKS(CLOCK_SYNC_EXCHANGE_INTERVAL_MS);  // 1Hz
    
    ESP_LOGI(TAG, "Clock Sync Task started (1Hz exchange)");
    
    while (1) {
        vTaskDelayUntil(&last_wake, period_ticks);
        
        if (!g_ble_bridge.running || !ble_lsl_bridge_is_connected(&g_ble_bridge)) continue;
        
        // Send sync request
        int64_t pod_send_us;
        if (clock_sync_pod_send_request(sync, &pod_send_us) == ESP_OK) {
            // Send via BLE sync characteristic
            // The hub will reply with its timestamps
            // For now, we just log - actual BLE write happens in ble_lsl_bridge
            ESP_LOGD(TAG, "Sync request sent: seq=%" PRIu32, sync->next_sequence);
        }
    }
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

    xTaskCreate(sensor_task_fn, "ppg_task", 4096, &ppg_sensor, 10, &ppg_task);
    xTaskCreate(sensor_task_fn, "imu_task", 4096, &imu_sensor, 10, &imu_task);

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

    // Initialize PPG feature processor (5s window, 10Hz output)
    ESP_ERROR_CHECK(ppg_processor_init());

    // Initialize IMU feature processor (10s window, 1Hz output)
    ESP_ERROR_CHECK(imu_processor_init());

    // Initialize clock sync (1Hz exchange, 60s drift model update)
    static clock_sync_t g_clock_sync;
    ESP_ERROR_CHECK(clock_sync_init(&g_clock_sync));

    // Initialize triage inference with embedded model
    ESP_ERROR_CHECK(triage_inference_init(&g_triage));

    // Start quality assessment task (100Hz PPG SQI output for motion gate)
    xTaskCreate(quality_task_fn, "quality_task", 4096, NULL, 5, &g_quality_task);

    // Start PPG feature processor task (reads from PPG ring buffer at 64Hz, outputs at 10Hz)
    static void ppg_feature_task_fn(void* arg);
    xTaskCreate(ppg_feature_task_fn, "ppg_feat_task", 4096, NULL, 5, &g_ppg_feature_task);

    // Start IMU feature processor task (reads from IMU ring buffer at 100Hz, outputs at 1Hz)
    static void imu_feature_task_fn(void* arg);
    xTaskCreate(imu_feature_task_fn, "imu_feat_task", 4096, NULL, 5, &g_imu_feature_task);

    // Start clock sync task (1Hz BLE timestamp exchange)
    static void clock_sync_task_fn(void* arg);
    xTaskCreate(clock_sync_task_fn, "clk_sync_task", 4096, &g_clock_sync, 5, &g_clock_sync_task);

    xTaskCreate(triage_task_fn, "triage_task", 8192, NULL, 5, &g_triage_task);

    ESP_ERROR_CHECK(sensor_scheduler_start(&g_scheduler));

    ESP_LOGI(TAG, "All subsystems started. Entering main loop...");

    uint32_t sample_counts[SENSOR_SCHEDULER_MAX_SENSORS] = {};
    uint32_t dropped_samples[SENSOR_SCHEDULER_MAX_SENSORS] = {};
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
            ESP_LOGI(TAG, "Stats: PPG=%" PRIu32 ", IMU=%" PRIu32 " | Dropped: PPG=%" PRIu32 ", IMU=%" PRIu32,
                     sample_counts[0], sample_counts[1],
                     dropped_samples[0], dropped_samples[1]);

            ESP_LOGI(TAG, "PPG SQI: sqi=%.3f, pi=%.3f%%, map=%.3f, gate_armed=%d, consecutive_clean=%d",
                     g_latest_sqi.sqi, g_latest_sqi.perfusion_index, g_latest_sqi.motion_artifact_prob,
                     g_motion_gate_armed, g_consecutive_clean);

            // PPG Features
            ppg_features_t ppg_feat;
            if (ppg_processor_get_latest(&ppg_feat) == ESP_OK && ppg_feat.valid) {
                ESP_LOGI(TAG, "PPG Features: HR=%.1f BPM, RMSSD=%.1f ms, SDNN=%.1f ms, SNR=%.1f dB, PI=%.1f%%, peaks=%d",
                         ppg_feat.hr_bpm, ppg_feat.rmssd_ms, ppg_feat.sdnn_ms, ppg_feat.snr_db, ppg_feat.pi_percent, ppg_feat.peak_count);
            }

            // IMU Features
            imu_features_t imu_feat;
            if (imu_processor_get_latest(&imu_feat) == ESP_OK) {
                ESP_LOGI(TAG, "IMU Features: motion=%.3fg, entropy=%.3f, dom_freq=%.2fHz, sleep_prob=%.3f, stationary=%d",
                         imu_feat.motion_intensity, imu_feat.spectral_entropy, imu_feat.dominant_freq_hz,
                         imu_feat.sleep_probability, imu_feat.is_stationary);
            }

            uint32_t markers;
            float drift;
            int64_t offset;
            sync_marker_handler_get_stats(&g_sync_handler, &markers, &drift, &offset);
            ESP_LOGI(TAG, "Sync: markers=%" PRIu32 ", drift=%.2f ppm, offset=%" PRId64 " us", markers, drift, offset);

            // Clock Sync
            uint32_t exchanges;
            float clk_drift;
            int64_t clk_offset;
            float last_rtt;
            clock_sync_get_stats(&g_clock_sync, &exchanges, &clk_drift, &clk_offset, &last_rtt);
            ESP_LOGI(TAG, "Clk Sync: exchanges=%" PRIu32 ", drift=%.2f ppm, offset=%" PRId64 " us, RTT=%.1f us",
                     exchanges, clk_drift, clk_offset, last_rtt);

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