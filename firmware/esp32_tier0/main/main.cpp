/**
 * @file main.cpp
 * @brief SYNAPSE-24 ESP32-S3 Tier 0 Firmware Entry Point
 *
 * Architecture.md §23-31: Decoupled sensor pod, Tier 0 continuous H24
 * Architecture.md §33-43: Tiered acquisition with IMU-based promotion
 * Architecture.md §45-53: Edge triage (SNN/TinyML) for T0->T1 promotion
 * Architecture.md §92: Multi-node clock sync via markers + ACC cross-corr
 */

#include "synapse_tier0_config.h"
#include "sensors/ecg_ad8232.h"
#include "sensors/ppg_max30102.h"
#include "sensors/imu_icm20948.h"
#include "ble/ble_lsl_bridge.h"
#include "sync/sync_marker_handler.h"
#include "power/power_monitor.h"
#include "triage/triage_inference.h"

#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/queue.h"
#include "freertos/semphr.h"
#include "freertos/timers.h"
#include "esp_log.h"
#include "esp_system.h"
#include "esp_timer.h"
#include "esp_wifi.h"
#include "esp_bt.h"
#include "esp_mac.h"
#include "nvs_flash.h"
#include "driver/gpio.h"
#include "driver/adc.h"
#include <string.h>

// ============================================================================
// LOGGING
// ============================================================================
static const char* TAG = "SYNAPSE_T0";

// ============================================================================
// GLOBAL STATE
// ============================================================================
static synapse_system_state_t g_system_state = SYNAPSE_STATE_INIT;
static SemaphoreHandle_t g_state_mutex = NULL;

// Task handles for watchdog monitoring
static TaskHandle_t g_task_ecg = NULL;
static TaskHandle_t g_task_ppg = NULL;
static TaskHandle_t g_task_imu = NULL;
static TaskHandle_t g_task_ble = NULL;
static TaskHandle_t g_task_sync = NULL;
static TaskHandle_t g_task_triage = NULL;
static TaskHandle_t g_task_power = NULL;
static TaskHandle_t g_task_watchdog = NULL;

// Queues for inter-task communication
static QueueHandle_t g_queue_ecg = NULL;
static QueueHandle_t g_queue_ppg = NULL;
static QueueHandle_t g_queue_imu = NULL;
static QueueHandle_t g_queue_ble_tx = NULL;
static QueueHandle_t g_queue_sync = NULL;

// Watchdog timer
static TimerHandle_t g_watchdog_timer = NULL;
static volatile bool g_watchdog_fed = false;

// ============================================================================
// FORWARD DECLARATIONS
// ============================================================================
static void task_ecg_acquisition(void* pvParameters);
static void task_ppg_acquisition(void* pvParameters);
static void task_imu_acquisition(void* pvParameters);
static void task_ble_tx(void* pvParameters);
static void task_sync_markers(void* pvParameters);
static void task_triage_inference(void* pvParameters);
static void task_power_monitor(void* pvParameters);
static void task_watchdog(void* pvParameters);
static void watchdog_timer_callback(TimerHandle_t xTimer);
static esp_err_t init_hardware(void);
static esp_err_t init_rtos_objects(void);
static void set_state(synapse_system_state_t new_state);
static void print_memory_stats(void);

// ============================================================================
// MAIN ENTRY POINT
// ============================================================================
extern "C" void app_main(void) {
    ESP_LOGI(TAG, "===========================================");
    ESP_LOGI(TAG, "SYNAPSE-24 Tier 0 Firmware v%s", SYNAPSE_FIRMWARE_VERSION_STRING);
    ESP_LOGI(TAG, "Hardware: %s", SYNAPSE_HARDWARE_REV);
    ESP_LOGI(TAG, "Architecture: Decoupled Pod/Hub, Tiered Acquisition");
    ESP_LOGI(TAG, "===========================================");

    print_memory_stats();

    // Initialize NVS
    esp_err_t ret = nvs_flash_init();
    if (ret == ESP_ERR_NVS_NO_FREE_PAGES || ret == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        ret = nvs_flash_init();
    }
    ESP_ERROR_CHECK(ret);

    // Initialize hardware
    ESP_ERROR_CHECK(init_hardware());

    // Initialize RTOS objects (queues, mutexes, timers)
    ESP_ERROR_CHECK(init_rtos_objects());

    // Initialize subsystems
    ESP_ERROR_CHECK(ecg_ad8232_init());
    ESP_ERROR_CHECK(ppg_max30102_init());
    ESP_ERROR_CHECK(imu_icm20948_init());
    ESP_ERROR_CHECK(ble_lsl_bridge_init());
    ESP_ERROR_CHECK(sync_marker_handler_init());
    ESP_ERROR_CHECK(power_monitor_init());
    ESP_ERROR_CHECK(triage_inference_init());

    // Create tasks
    xTaskCreatePinnedToCore(task_ecg_acquisition, "ecg_acq", SYNAPSE_STACK_ECG, NULL, SYNAPSE_TASK_PRIO_ECG, &g_task_ecg, 0);
    xTaskCreatePinnedToCore(task_ppg_acquisition, "ppg_acq", SYNAPSE_STACK_PPG, NULL, SYNAPSE_TASK_PRIO_PPG, &g_task_ppg, 0);
    xTaskCreatePinnedToCore(task_imu_acquisition, "imu_acq", SYNAPSE_STACK_IMU, NULL, SYNAPSE_TASK_PRIO_IMU, &g_task_imu, 0);
    xTaskCreatePinnedToCore(task_ble_tx, "ble_tx", SYNAPSE_STACK_BLE, NULL, SYNAPSE_TASK_PRIO_BLE, &g_task_ble, 1);
    xTaskCreatePinnedToCore(task_sync_markers, "sync_mkr", SYNAPSE_STACK_SYNC, NULL, SYNAPSE_TASK_PRIO_SYNC, &g_task_sync, 1);
    xTaskCreatePinnedToCore(task_triage_inference, "triage", SYNAPSE_STACK_TRIAGE, NULL, SYNAPSE_TASK_PRIO_TRIAGE, &g_task_triage, 1);
    xTaskCreatePinnedToCore(task_power_monitor, "power", SYNAPSE_STACK_POWER, NULL, SYNAPSE_TASK_PRIO_POWER, &g_task_power, 1);
    xTaskCreatePinnedToCore(task_watchdog, "watchdog", SYNAPSE_STACK_WATCHDOG, NULL, SYNAPSE_TASK_PRIO_WATCHDOG, &g_task_watchdog, 0);

    // Start watchdog timer
    g_watchdog_timer = xTimerCreate("sys_wdt", pdMS_TO_TICKS(SYNAPSE_WDT_TIMEOUT_MS), pdTRUE, NULL, watchdog_timer_callback);
    xTimerStart(g_watchdog_timer, 0);

    // Transition to running state
    set_state(SYNAPSE_STATE_T0_RUNNING);

    ESP_LOGI(TAG, "All tasks started. Tier 0 acquisition running.");
    ESP_LOGI(TAG, "ECG: %d Hz, PPG: %d Hz, IMU: %d Hz", SYNAPSE_ECG_SAMPLING_RATE_HZ, SYNAPSE_PPG_SAMPLING_RATE_HZ, SYNAPSE_IMU_SAMPLING_RATE_HZ);
    ESP_LOGI(TAG, "Sync marker interval: %d ms", SYNAPSE_SYNC_MARKER_INTERVAL_MS);

    // Main loop - monitor system health
    while (true) {
        vTaskDelay(pdMS_TO_TICKS(10000)); // 10 second check interval

        // Feed watchdog
        g_watchdog_fed = true;

        // Log status periodically
        synapse_power_status_t power_status;
        synapse_get_power_status(&power_status);
        ESP_LOGI(TAG, "State: %d, Battery: %.0f mAh (%.1f h), Power: %.1f mW, T0: %.1f h, T1: %.1f h",
                 g_system_state, power_status.battery_remaining_mah, power_status.estimated_remaining_h,
                 power_status.current_power_mw, power_status.tier0_h_used, power_status.tier1_h_used);

        print_memory_stats();
    }
}

// ============================================================================
// TASK IMPLEMENTATIONS
// ============================================================================

static void task_ecg_acquisition(void* pvParameters) {
    ESP_LOGI(TAG, "ECG acquisition task started (Core 0, Priority %d)", SYNAPSE_TASK_PRIO_ECG);
    synapse_ecg_sample_t sample;
    TickType_t last_wake = xTaskGetTickCount();
    const TickType_t period = pdMS_TO_TICKS(1000 / SYNAPSE_ECG_SAMPLING_RATE_HZ);

    while (true) {
        vTaskDelayUntil(&last_wake, period);

        if (g_system_state != SYNAPSE_STATE_T0_RUNNING &&
            g_system_state != SYNAPSE_STATE_T1_RUNNING &&
            g_system_state != SYNAPSE_STATE_T2_RUNNING) {
            continue;
        }

        // Read ECG sample
        esp_err_t err = ecg_ad8232_read_sample(&sample);
        if (err == ESP_OK) {
            // Push to queue (non-blocking, drop if full)
            xQueueSend(g_queue_ecg, &sample, 0);

            // Also feed directly to triage if buffer ready
            triage_inference_feed_ecg(&sample);
        } else if (err == ESP_ERR_TIMEOUT) {
            // ADC not ready, skip this cycle
        } else {
            ESP_LOGW(TAG, "ECG read error: %s", esp_err_to_name(err));
        }
    }
}

static void task_ppg_acquisition(void* pvParameters) {
    ESP_LOGI(TAG, "PPG acquisition task started (Core 0, Priority %d)", SYNAPSE_TASK_PRIO_PPG);
    synapse_ppg_sample_t sample;
    TickType_t last_wake = xTaskGetTickCount();
    const TickType_t period = pdMS_TO_TICKS(1000 / SYNAPSE_PPG_SAMPLING_RATE_HZ);

    while (true) {
        vTaskDelayUntil(&last_wake, period);

        if (g_system_state != SYNAPSE_STATE_T0_RUNNING &&
            g_system_state != SYNAPSE_STATE_T1_RUNNING &&
            g_system_state != SYNAPSE_STATE_T2_RUNNING) {
            continue;
        }

        esp_err_t err = ppg_max30102_read_sample(&sample);
        if (err == ESP_OK) {
            xQueueSend(g_queue_ppg, &sample, 0);
            triage_inference_feed_ppg(&sample);
        } else if (err != ESP_ERR_TIMEOUT) {
            ESP_LOGW(TAG, "PPG read error: %s", esp_err_to_name(err));
        }
    }
}

static void task_imu_acquisition(void* pvParameters) {
    ESP_LOGI(TAG, "IMU acquisition task started (Core 0, Priority %d)", SYNAPSE_TASK_PRIO_IMU);
    synapse_imu_sample_t sample;
    TickType_t last_wake = xTaskGetTickCount();
    const TickType_t period = pdMS_TO_TICKS(1000 / SYNAPSE_IMU_SAMPLING_RATE_HZ);

    while (true) {
        vTaskDelayUntil(&last_wake, period);

        if (g_system_state != SYNAPSE_STATE_T0_RUNNING &&
            g_system_state != SYNAPSE_STATE_T1_RUNNING &&
            g_system_state != SYNAPSE_STATE_T2_RUNNING) {
            continue;
        }

        esp_err_t err = imu_icm20948_read_sample(&sample);
        if (err == ESP_OK) {
            xQueueSend(g_queue_imu, &sample, 0);
            triage_inference_feed_imu(&sample);
            sync_marker_handler_feed_accel(sample.accel_x, sample.accel_y, sample.accel_z, sample.timestamp_us);
        } else if (err != ESP_ERR_TIMEOUT) {
            ESP_LOGW(TAG, "IMU read error: %s", esp_err_to_name(err));
        }
    }
}

static void task_ble_tx(void* pvParameters) {
    ESP_LOGI(TAG, "BLE TX task started (Core 1, Priority %d)", SYNAPSE_TASK_PRIO_BLE);

    // Buffer for batching samples
    static synapse_ecg_sample_t ecg_batch[SYNAPSE_ECG_QUEUE_SIZE];
    static synapse_ppg_sample_t ppg_batch[SYNAPSE_PPG_QUEUE_SIZE];
    static synapse_imu_sample_t imu_batch[SYNAPSE_IMU_QUEUE_SIZE];
    size_t ecg_count = 0, ppg_count = 0, imu_count = 0;

    while (true) {
        // Collect ECG samples
        while (ecg_count < SYNAPSE_ECG_QUEUE_SIZE &&
               xQueueReceive(g_queue_ecg, &ecg_batch[ecg_count], 0) == pdTRUE) {
            ecg_count++;
        }

        // Collect PPG samples
        while (ppg_count < SYNAPSE_PPG_QUEUE_SIZE &&
               xQueueReceive(g_queue_ppg, &ppg_batch[ppg_count], 0) == pdTRUE) {
            ppg_count++;
        }

        // Collect IMU samples
        while (imu_count < SYNAPSE_IMU_QUEUE_SIZE &&
               xQueueReceive(g_queue_imu, &imu_batch[imu_count], 0) == pdTRUE) {
            imu_count++;
        }

        // Send batches via BLE
        if (ecg_count > 0) {
            ble_lsl_bridge_send_ecg(ecg_batch, ecg_count);
            ecg_count = 0;
        }
        if (ppg_count > 0) {
            ble_lsl_bridge_send_ppg(ppg_batch, ppg_count);
            ppg_count = 0;
        }
        if (imu_count > 0) {
            ble_lsl_bridge_send_imu(imu_batch, imu_count);
            imu_count = 0;
        }

        // Small delay to prevent busy-waiting
        vTaskDelay(pdMS_TO_TICKS(10));
    }
}

static void task_sync_markers(void* pvParameters) {
    ESP_LOGI(TAG, "Sync marker task started (Core 1, Priority %d)", SYNAPSE_TASK_PRIO_SYNC);
    TickType_t last_broadcast = xTaskGetTickCount();
    const TickType_t interval = pdMS_TO_TICKS(SYNAPSE_SYNC_MARKER_INTERVAL_MS);

    while (true) {
        vTaskDelayUntil(&last_broadcast, interval);

        if (g_system_state == SYNAPSE_STATE_T0_RUNNING ||
            g_system_state == SYNAPSE_STATE_T1_RUNNING ||
            g_system_state == SYNAPSE_STATE_T2_RUNNING) {
            sync_marker_handler_broadcast();
        }
    }
}

static void task_triage_inference(void* pvParameters) {
    ESP_LOGI(TAG, "Triage inference task started (Core 1, Priority %d)", SYNAPSE_TASK_PRIO_TRIAGE);
    TickType_t last_run = xTaskGetTickCount();
    const TickType_t interval = pdMS_TO_TICKS(1000); // Run inference every 1 second

    while (true) {
        vTaskDelayUntil(&last_run, interval);

        if (g_system_state == SYNAPSE_STATE_T0_RUNNING) {
            synapse_triage_result_t result;
            if (triage_inference_run(&result)) {
                ESP_LOGI(TAG, "Triage: class=%d, conf=%.2f, latency=%.1fms",
                         result.classification, result.confidence, result.inference_ms);

                // Check for Tier 1 promotion
                if (result.classification == 1 && result.confidence > 0.8f) {
                    synapse_power_status_t power_status;
                    synapse_get_power_status(&power_status);
                    if (power_status.can_afford_tier1) {
                        ESP_LOGW(TAG, "Triage requests Tier 1 promotion (conf=%.2f)", result.confidence);
                        // In production, this would trigger BLE command to hub
                        // For now, log the event
                        set_state(SYNAPSE_STATE_T1_RUNNING);
                    }
                }
            }
        }
    }
}

static void task_power_monitor(void* pvParameters) {
    ESP_LOGI(TAG, "Power monitor task started (Core 1, Priority %d)", SYNAPSE_TASK_PRIO_POWER);
    TickType_t last_run = xTaskGetTickCount();
    const TickType_t interval = pdMS_TO_TICKS(60000); // Every minute

    while (true) {
        vTaskDelayUntil(&last_run, interval);
        power_monitor_update();
    }
}

static void task_watchdog(void* pvParameters) {
    ESP_LOGI(TAG, "System watchdog task started (Core 0, Priority %d)", SYNAPSE_TASK_PRIO_WATCHDOG);

    while (true) {
        vTaskDelay(pdMS_TO_TICKS(1000)); // Check every second

        if (!g_watchdog_fed) {
            ESP_LOGE(TAG, "Watchdog timeout! System reset.");
            esp_restart();
        }
        g_watchdog_fed = false;

        // Check task health
        // In production: check each task's last heartbeat
    }
}

static void watchdog_timer_callback(TimerHandle_t xTimer) {
    // Hardware watchdog backup
    if (!g_watchdog_fed) {
        esp_restart();
    }
}

// ============================================================================
// HELPER FUNCTIONS
// ============================================================================

static esp_err_t init_hardware(void) {
    // GPIO for status LED
    gpio_config_t led_conf = {
        .pin_bit_mask = (1ULL << SYNAPSE_STATUS_LED_PIN),
        .mode = GPIO_MODE_OUTPUT,
        .pull_up_en = GPIO_PULLUP_DISABLE,
        .pull_down_en = GPIO_PULLDOWN_DISABLE,
        .intr_type = GPIO_INTR_DISABLE
    };
    gpio_config(&led_conf);
    gpio_set_level(SYNAPSE_STATUS_LED_PIN, 1); // LED on = initializing

    // ADC for ECG and battery
    adc_oneshot_unit_init_cfg_t adc_cfg = {
        .unit_id = ADC_UNIT_1,
        .ulp_mode = ADC_ULP_MODE_DISABLE
    };
    adc_oneshot_handle_t adc_handle;
    ESP_ERROR_CHECK(adc_oneshot_new_unit(&adc_cfg, &adc_handle));

    // ADC channel config for ECG
    adc_oneshot_chan_cfg_t chan_cfg = {
        .atten = ADC_ATTEN_DB_12,
        .bitwidth = ADC_BITWIDTH_12,
    };
    ESP_ERROR_CHECK(adc_oneshot_config_channel(adc_handle, SYNAPSE_ECG_ADC_PIN, &chan_cfg));

    // ADC channel config for battery
    ESP_ERROR_CHECK(adc_oneshot_config_channel(adc_handle, SYNAPSE_BATTERY_ADC_PIN, &chan_cfg));

    // I2C for PPG and IMU (shared bus)
    i2c_config_t i2c_conf = {
        .mode = I2C_MODE_MASTER,
        .sda_io_num = SYNAPSE_PPG_I2C_SDA_PIN,
        .scl_io_num = SYNAPSE_PPG_I2C_SCL_PIN,
        .sda_pullup_en = GPIO_PULLUP_ENABLE,
        .scl_pullup_en = GPIO_PULLUP_ENABLE,
        .master = {
            .clk_speed = 400000, // 400kHz fast mode
        },
        .clk_flags = 0
    };
    ESP_ERROR_CHECK(i2c_param_config(I2C_NUM_0, &i2c_conf));
    ESP_ERROR_CHECK(i2c_driver_install(I2C_NUM_0, I2C_MODE_MASTER, 0, 0, 0));

    // BLE initialization
    ESP_ERROR_CHECK(esp_bt_controller_mem_release(ESP_BT_MODE_CLASSIC_BT));
    esp_bt_controller_config_t bt_cfg = BT_CONTROLLER_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_bt_controller_init(&bt_cfg));
    ESP_ERROR_CHECK(esp_bt_controller_enable(ESP_BT_MODE_BLE));
    ESP_ERROR_CHECK(esp_bluedroid_init());
    ESP_ERROR_CHECK(esp_bluedroid_enable());

    return ESP_OK;
}

static esp_err_t init_rtos_objects(void) {
    g_state_mutex = xSemaphoreCreateMutex();
    if (!g_state_mutex) return ESP_FAIL;

    g_queue_ecg = xQueueCreate(SYNAPSE_ECG_QUEUE_SIZE, sizeof(synapse_ecg_sample_t));
    g_queue_ppg = xQueueCreate(SYNAPSE_PPG_QUEUE_SIZE, sizeof(synapse_ppg_sample_t));
    g_queue_imu = xQueueCreate(SYNAPSE_IMU_QUEUE_SIZE, sizeof(synapse_imu_sample_t));
    g_queue_ble_tx = xQueueCreate(SYNAPSE_BLE_TX_QUEUE_SIZE, 256); // Variable size
    g_queue_sync = xQueueCreate(SYNAPSE_SYNC_QUEUE_SIZE, sizeof(synapse_sync_marker_t));

    if (!g_queue_ecg || !g_queue_ppg || !g_queue_imu || !g_queue_ble_tx || !g_queue_sync) {
        return ESP_FAIL;
    }

    return ESP_OK;
}

static void set_state(synapse_system_state_t new_state) {
    if (xSemaphoreTake(g_state_mutex, pdMS_TO_TICKS(100)) == pdTRUE) {
        synapse_system_state_t old_state = g_system_state;
        g_system_state = new_state;
        xSemaphoreGive(g_state_mutex);

        if (old_state != new_state) {
            ESP_LOGI(TAG, "State transition: %d -> %d", old_state, new_state);
            power_monitor_on_tier_change(old_state, new_state);
        }
    }
}

static void print_memory_stats(void) {
    ESP_LOGI(TAG, "Free heap: %lu bytes, Min free: %lu bytes",
             esp_get_free_heap_size(), esp_get_minimum_free_heap_size());
    ESP_LOGI(TAG, "Free PSRAM: %lu bytes", esp_get_free_psram_size());

    // Task stack watermarks
    UBaseType_t watermark;
    #define CHECK_WM(handle, name) \
        if (handle) { \
            watermark = uxTaskGetStackHighWaterMark(handle); \
            ESP_LOGI(TAG, "  %s stack watermark: %u bytes", name, watermark * 4); \
        }
    CHECK_WM(g_task_ecg, "ECG");
    CHECK_WM(g_task_ppg, "PPG");
    CHECK_WM(g_task_imu, "IMU");
    CHECK_WM(g_task_ble, "BLE");
    CHECK_WM(g_task_sync, "SYNC");
    CHECK_WM(g_task_triage, "TRIAGE");
    CHECK_WM(g_task_power, "POWER");
    CHECK_WM(g_task_watchdog, "WDT");
    #undef CHECK_WM
}

// ============================================================================
// PUBLIC API IMPLEMENTATIONS
// ============================================================================

esp_err_t synapse_tier0_init(void) {
    // Already done in app_main
    return ESP_OK;
}

esp_err_t synapse_tier0_start(void) {
    set_state(SYNAPSE_STATE_T0_RUNNING);
    return ESP_OK;
}

esp_err_t synapse_tier0_stop(void) {
    set_state(SYNAPSE_STATE_INIT);
    return ESP_OK;
}

synapse_system_state_t synapse_get_state(void) {
    synapse_system_state_t state;
    if (xSemaphoreTake(g_state_mutex, pdMS_TO_TICKS(100)) == pdTRUE) {
        state = g_system_state;
        xSemaphoreGive(g_state_mutex);
    } else {
        state = SYNAPSE_STATE_ERROR;
    }
    return state;
}

void synapse_get_power_status(synapse_power_status_t* status) {
    power_monitor_get_status(status);
}

bool synapse_request_tier2(float duration_min) {
    synapse_power_status_t status;
    synapse_get_power_status(&status);
    return status.can_afford_tier2;
}

void synapse_handle_sync_marker(const synapse_sync_marker_t* marker) {
    xQueueSend(g_queue_sync, marker, 0);
}

bool synapse_get_triage_result(synapse_triage_result_t* result) {
    return triage_inference_get_result(result);
}