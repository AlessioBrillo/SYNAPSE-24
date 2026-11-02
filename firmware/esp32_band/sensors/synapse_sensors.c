/**
 * @file synapse_sensors.c
 * @brief Synapse Band v1 - Sensors Abstraction Layer Implementation
 * 
 * Integrates ECG, PPG, IMU (via firmware/common sensor_scheduler)
 * with GPS and Temperature drivers.
 */

#include <string.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/queue.h"
#include "freertos/semphr.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "synapse_sensors.h"
#include "synapse_config.h"
#include "sensor_scheduler.h"
#include "ecg_ad8232.h"
#include "ppg_max30102.h"
#include "imu_icm20948.h"
#include "gps_max_m10s.h"
#include "temp_tmp117.h"
#include "synapse_adc1.h"

#if defined(CONFIG_SYNAPSE_FEATURE_GPS) || defined(CONFIG_SYNAPSE_FEATURE_TEMPERATURE)
#define SYNAPSE_HAS_AUX_SENSORS 1
#endif

static const char *TAG = "SYNAPSE_SENSORS";

static synapse_sensors_t g_sensors = {0};

// The sensor tasks read their sensor_config_t for as long as they live, so the
// configs (and handles) must have static storage duration.
static sensor_config_t s_sensor_cfg[3];
static TaskHandle_t s_sensor_task[3];
// Bit n = sensor_type_t n initialised OK. A missing/dead sensor must not stop
// the band from booting and advertising (fail-soft).
static volatile uint8_t s_sensor_health;

// Convert synapse_hw_config_t to sensor driver configs
static void populate_sensor_configs(const synapse_hw_config_t *hw_cfg) {
    // ECG
    g_sensors.ecg_config.adc_channel = hw_cfg->ecg_adc_channel;
    g_sensors.ecg_config.gpio_drdy = hw_cfg->ecg_gpio_drdy;
    g_sensors.ecg_config.vref_mv = hw_cfg->ecg_vref_mv;
    g_sensors.ecg_config.gain = hw_cfg->ecg_gain;
    adc_oneshot_unit_handle_t adc1 = NULL;
    if (synapse_adc1_get(&adc1) == ESP_OK) {
        g_sensors.ecg_config.adc_handle = adc1;
    } else {
        ESP_LOGE(TAG, "ADC1 unavailable: ECG will fail init (fail-soft)");
    }

    // PPG
    g_sensors.ppg_config.i2c_port = (i2c_port_t)hw_cfg->ppg_i2c_port;
    g_sensors.ppg_config.i2c_addr = hw_cfg->ppg_i2c_addr;
    g_sensors.ppg_config.gpio_int = hw_cfg->ppg_gpio_int;
    g_sensors.ppg_config.sda_gpio_num = 8;  // S3-safe (no GPIO22-25 on ESP32-S3)
    g_sensors.ppg_config.scl_gpio_num = 9;
    g_sensors.ppg_config.led_current_red = hw_cfg->ppg_led_current_red;
    g_sensors.ppg_config.led_current_ir = hw_cfg->ppg_led_current_ir;
    g_sensors.ppg_config.led_current_green = hw_cfg->ppg_led_current_green;
    g_sensors.ppg_config.sample_rate = hw_cfg->ppg_sample_rate;
    g_sensors.ppg_config.pulse_width = hw_cfg->ppg_pulse_width;
    g_sensors.ppg_config.adc_range = hw_cfg->ppg_adc_range;

    // IMU
    g_sensors.imu_config.i2c_port = (i2c_port_t)hw_cfg->imu_i2c_port;
    g_sensors.imu_config.i2c_addr = hw_cfg->imu_i2c_addr;
    g_sensors.imu_config.gpio_int = hw_cfg->imu_gpio_int;
    g_sensors.imu_config.sda_gpio_num = 8;
    g_sensors.imu_config.scl_gpio_num = 9;
    g_sensors.imu_config.accel_fsr_g = hw_cfg->imu_accel_fsr_g;
    g_sensors.imu_config.gyro_fsr_dps = hw_cfg->imu_gyro_fsr_dps;
    g_sensors.imu_config.accel_odr_hz = hw_cfg->imu_accel_odr_hz;
    g_sensors.imu_config.gyro_odr_hz = hw_cfg->imu_gyro_odr_hz;

    // GPS
    g_sensors.gps_config.uart_port = hw_cfg->gps_uart_port;
    g_sensors.gps_config.tx_gpio = hw_cfg->gps_uart_tx_gpio;
    g_sensors.gps_config.rx_gpio = hw_cfg->gps_uart_rx_gpio;
    g_sensors.gps_config.baudrate = hw_cfg->gps_baudrate;
    g_sensors.gps_config.use_ubx = hw_cfg->gps_use_ubx;
    g_sensors.gps_config.uart_rx_buffer_size = 1024;

    // Temperature
    g_sensors.temp_config.i2c_port = hw_cfg->temp_i2c_port;
    g_sensors.temp_config.i2c_addr = hw_cfg->temp_i2c_addr;
    g_sensors.temp_config.gpio_alert = hw_cfg->temp_gpio_alert;
    g_sensors.temp_config.conversion_cycle_ms = 1000; // 1Hz updates
    g_sensors.temp_config.continuous_mode = true;
}

// Scheduler-compatible wrappers (exact signature match, no casts)
static void note_init(sensor_type_t type, const char *name, esp_err_t err) {
    if (err == ESP_OK) {
        s_sensor_health |= (uint8_t)(1u << type);
    } else {
        s_sensor_health &= (uint8_t)~(1u << type);
        ESP_LOGE(TAG, "%s init failed: %s (continuing without it)", name, esp_err_to_name(err));
    }
}
static void ecg_init_wrap(void *ctx) {
    note_init(SENSOR_TYPE_ECG, "ECG", ecg_ad8232_init((const ecg_ad8232_config_t *)ctx));
}
static esp_err_t ecg_read_wrap(sensor_sample_t *sample, void *ctx) {
    return ecg_ad8232_read(sample, ctx);
}
static void ecg_deinit_wrap(void *ctx) {
    (void)ecg_ad8232_deinit(ctx);
}
static void ppg_init_wrap(void *ctx) {
    note_init(SENSOR_TYPE_PPG, "PPG", ppg_max30102_init((const ppg_max30102_config_t *)ctx));
}
static esp_err_t ppg_read_wrap(sensor_sample_t *sample, void *ctx) {
    return ppg_max30102_read(sample, ctx);
}
static void ppg_deinit_wrap(void *ctx) {
    (void)ppg_max30102_deinit(ctx);
}
static void imu_init_wrap(void *ctx) {
    note_init(SENSOR_TYPE_IMU, "IMU", imu_icm20948_init((const imu_icm20948_config_t *)ctx));
}
static esp_err_t imu_read_wrap(sensor_sample_t *sample, void *ctx) {
    return imu_icm20948_read(sample, ctx);
}
static void imu_deinit_wrap(void *ctx) {
    (void)imu_icm20948_deinit(ctx);
}

// Register a sensor, then spawn its polling task. Registration comes first so
// the task starts with the right rate; the task stays idle until the scheduler
// is started (it checks scheduler->running).
static esp_err_t register_and_spawn(int idx, const sensor_config_t *cfg, uint32_t stack, UBaseType_t prio) {
    s_sensor_cfg[idx] = *cfg;
    s_sensor_cfg[idx].task_handle = NULL;
    esp_err_t err = sensor_scheduler_register_sensor(&g_sensors.scheduler, &s_sensor_cfg[idx]);
    if (err != ESP_OK) {
        return err;
    }
    if (!(s_sensor_health & (1u << cfg->type))) {
        // Init failed (reported by note_init): keep the band up without polling a dead sensor.
        ESP_LOGW(TAG, "%s not initialised: no polling task", cfg->name);
        return ESP_OK;
    }
    if (xTaskCreate(sensor_task_fn, cfg->name, stack, &s_sensor_cfg[idx], prio, &s_sensor_task[idx]) != pdPASS) {
        ESP_LOGE(TAG, "Failed to create task for %s", cfg->name);
        s_sensor_task[idx] = NULL;
        return ESP_ERR_NO_MEM;
    }
    return sensor_scheduler_set_task(&g_sensors.scheduler, cfg->type, s_sensor_task[idx]);
}

// A sensor that cannot be registered or spawned is reported and skipped: the
// band must stay up and advertise (fail-soft), not reboot-loop.
static void spawn_or_log(esp_err_t err, const char *name) {
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "%s disabled: %s", name, esp_err_to_name(err));
    }
}

uint8_t synapse_sensors_health_mask(void) {
    return s_sensor_health;
}

esp_err_t synapse_sensors_init(void) {
    if (g_sensors.initialized) {
        ESP_LOGW(TAG, "Already initialized");
        return ESP_OK;
    }

    // Load hardware configuration
    synapse_hw_config_t hw_cfg;
    ESP_ERROR_CHECK(synapse_config_get(&hw_cfg));
    populate_sensor_configs(&hw_cfg);

    // Create sample queue
    g_sensors.sample_queue = xQueueCreate(64, sizeof(sensor_sample_t)) /* scheduler enqueues plain sensor_sample_t */;
    if (!g_sensors.sample_queue) {
        ESP_LOGE(TAG, "Failed to create sample queue");
        return ESP_ERR_NO_MEM;
    }

    // Create data mutex
    g_sensors.data_mutex = xSemaphoreCreateMutex();
    if (!g_sensors.data_mutex) {
        ESP_LOGE(TAG, "Failed to create data mutex");
        vQueueDelete(g_sensors.sample_queue);
        return ESP_ERR_NO_MEM;
    }

    // Initialize sensor scheduler
    ESP_ERROR_CHECK(sensor_scheduler_init(&g_sensors.scheduler, g_sensors.sample_queue));

    // Register ECG sensor
    sensor_config_t ecg_sensor = {
        .type = SENSOR_TYPE_ECG,
        .name = "ECG_AD8232",
        .sampling_rate_hz = 500,
        .init = ecg_init_wrap,
        .read = ecg_read_wrap,
        .deinit = ecg_deinit_wrap,
        .user_ctx = &g_sensors.ecg_config,
        .task_handle = NULL
    };
    spawn_or_log(register_and_spawn(0, &ecg_sensor, 4096, SENSOR_TASK_PRIO_ECG), "ECG");

    // Register PPG sensor
    sensor_config_t ppg_sensor = {
        .type = SENSOR_TYPE_PPG,
        .name = "PPG_MAX30102",
        .sampling_rate_hz = synapse_ppg_rate_hz(hw_cfg.ppg_sample_rate),
        .init = ppg_init_wrap,
        .read = ppg_read_wrap,
        .deinit = ppg_deinit_wrap,
        .user_ctx = &g_sensors.ppg_config,
        .task_handle = NULL
    };
    spawn_or_log(register_and_spawn(1, &ppg_sensor, 4096, SENSOR_TASK_PRIO_PPG), "PPG");

    // Register IMU sensor
    sensor_config_t imu_sensor = {
        .type = SENSOR_TYPE_IMU,
        .name = "IMU_ICM20948",
        .sampling_rate_hz = hw_cfg.imu_accel_odr_hz,
        .init = imu_init_wrap,
        .read = imu_read_wrap,
        .deinit = imu_deinit_wrap,
        .user_ctx = &g_sensors.imu_config,
        .task_handle = NULL
    };
    spawn_or_log(register_and_spawn(2, &imu_sensor, 4096, SENSOR_TASK_PRIO_IMU), "IMU");

#ifdef CONFIG_SYNAPSE_FEATURE_GPS
    // Initialize GPS
    ESP_ERROR_CHECK(gps_max_m10s_init(&g_sensors.gps_config));
#endif

#ifdef CONFIG_SYNAPSE_FEATURE_TEMPERATURE
    // Initialize Temperature
    ESP_ERROR_CHECK(temp_tmp117_init(&g_sensors.temp_config));
#endif

    // Initialize latest data
#ifdef CONFIG_SYNAPSE_FEATURE_GPS
    memset(&g_sensors.latest_gps, 0, sizeof(gps_data_t));
#endif
#ifdef CONFIG_SYNAPSE_FEATURE_TEMPERATURE
    memset(&g_sensors.latest_temp, 0, sizeof(temp_data_t));
#endif

    g_sensors.initialized = true;
#ifdef CONFIG_SYNAPSE_FEATURE_GPS
 #ifdef CONFIG_SYNAPSE_FEATURE_TEMPERATURE
    ESP_LOGI(TAG, "Sensors initialized (ECG:500Hz, PPG:%dHz, IMU:%dHz, GPS, Temp)",
             (int)synapse_ppg_rate_hz(hw_cfg.ppg_sample_rate), hw_cfg.imu_accel_odr_hz);
 #else
    ESP_LOGI(TAG, "Sensors initialized (ECG:500Hz, PPG:%dHz, IMU:%dHz, GPS)",
             (int)synapse_ppg_rate_hz(hw_cfg.ppg_sample_rate), hw_cfg.imu_accel_odr_hz);
 #endif
#elif defined(CONFIG_SYNAPSE_FEATURE_TEMPERATURE)
    ESP_LOGI(TAG, "Sensors initialized (ECG:500Hz, PPG:%dHz, IMU:%dHz, Temp)",
             (int)synapse_ppg_rate_hz(hw_cfg.ppg_sample_rate), hw_cfg.imu_accel_odr_hz);
#else
    ESP_LOGI(TAG, "Sensors initialized (ECG:500Hz, PPG:%dHz, IMU:%dHz) [band-core]",
             (int)synapse_ppg_rate_hz(hw_cfg.ppg_sample_rate), hw_cfg.imu_accel_odr_hz);
#endif
    return ESP_OK;
}

esp_err_t synapse_sensors_start(void) {
    if (!g_sensors.initialized) return ESP_ERR_INVALID_STATE;
    if (g_sensors.running) return ESP_OK;

    // Start sensor scheduler (creates ECG/PPG/IMU tasks)
    ESP_ERROR_CHECK(sensor_scheduler_start(&g_sensors.scheduler));

#ifdef CONFIG_SYNAPSE_FEATURE_GPS
    // Start GPS
    ESP_ERROR_CHECK(gps_max_m10s_start());
#endif

#ifdef CONFIG_SYNAPSE_FEATURE_TEMPERATURE
    // Start Temperature
    ESP_ERROR_CHECK(temp_tmp117_start());
#endif

    g_sensors.running = true;
    ESP_LOGI(TAG, "All sensors started");
    return ESP_OK;
}

esp_err_t synapse_sensors_stop(void) {
    if (!g_sensors.running) return ESP_OK;

    // Stop sensor scheduler
    ESP_ERROR_CHECK(sensor_scheduler_stop(&g_sensors.scheduler));

#ifdef CONFIG_SYNAPSE_FEATURE_GPS
    // Stop GPS
    ESP_ERROR_CHECK(gps_max_m10s_stop());
#endif

#ifdef CONFIG_SYNAPSE_FEATURE_TEMPERATURE
    // Stop Temperature
    ESP_ERROR_CHECK(temp_tmp117_stop());
#endif

    g_sensors.running = false;
    ESP_LOGI(TAG, "All sensors stopped");
    return ESP_OK;
}

esp_err_t synapse_sensors_deinit(void) {
    synapse_sensors_stop();

#ifdef CONFIG_SYNAPSE_FEATURE_GPS
    // Deinitialize GPS
    ESP_ERROR_CHECK(gps_max_m10s_deinit());
#endif

#ifdef CONFIG_SYNAPSE_FEATURE_TEMPERATURE
    // Deinitialize Temperature
    ESP_ERROR_CHECK(temp_tmp117_deinit());
#endif

    // The sensor tasks hold pointers into the scheduler: delete them first.
    for (int i = 0; i < 3; i++) {
        if (s_sensor_task[i]) {
            vTaskDelete(s_sensor_task[i]);
            s_sensor_task[i] = NULL;
        }
    }
    s_sensor_health = 0;

    // Deinitialize sensor scheduler
    ESP_ERROR_CHECK(sensor_scheduler_deinit(&g_sensors.scheduler));

    // Cleanup
    if (g_sensors.sample_queue) {
        vQueueDelete(g_sensors.sample_queue);
        g_sensors.sample_queue = NULL;
    }
    if (g_sensors.data_mutex) {
        vSemaphoreDelete(g_sensors.data_mutex);
        g_sensors.data_mutex = NULL;
    }

    g_sensors.initialized = false;
    ESP_LOGI(TAG, "Sensors deinitialized");
    return ESP_OK;
}

esp_err_t synapse_sensors_get_sample(synapse_sensor_sample_t *sample) {
    if (!sample) return ESP_ERR_INVALID_ARG;
    if (!g_sensors.initialized || !g_sensors.sample_queue) return ESP_ERR_INVALID_STATE;

    // Items are sensor_sample_t (what the scheduler sends); GPS/temp stay zero here -
    // use synapse_sensors_get_gps/get_temp. xQueueReceive returns pdTRUE(1), not ESP_OK.
    sensor_sample_t base;
    if (xQueueReceive(g_sensors.sample_queue, &base, 0) != pdTRUE) return ESP_ERR_NOT_FOUND;
    memset(sample, 0, sizeof(*sample));
    sample->base = base;
    return ESP_OK;
}

#ifdef CONFIG_SYNAPSE_FEATURE_GPS
esp_err_t synapse_sensors_get_gps(gps_data_t *gps) {
    if (!gps) return ESP_ERR_INVALID_ARG;
    return gps_max_m10s_get_latest(gps);
}
#endif

#ifdef CONFIG_SYNAPSE_FEATURE_TEMPERATURE
esp_err_t synapse_sensors_get_temp(temp_data_t *temp) {
    if (!temp) return ESP_ERR_INVALID_ARG;
    return temp_tmp117_get_latest(temp);
}
#endif

esp_err_t synapse_sensors_get_stats(uint32_t *sample_counts, uint32_t *dropped_samples) {
    if (!g_sensors.initialized) return ESP_ERR_INVALID_STATE;
    if (!sample_counts || !dropped_samples) return ESP_ERR_INVALID_ARG;
    sensor_scheduler_get_stats(&g_sensors.scheduler, sample_counts, dropped_samples);
    return ESP_OK;
}

bool synapse_sensors_is_running(void) {
    return g_sensors.running;
}

esp_err_t synapse_sensors_set_enabled(sensor_type_t sensor_type, bool enable) {
    if (!g_sensors.initialized) return ESP_ERR_INVALID_STATE;
    // The sensor_scheduler doesn't have dynamic enable/disable yet
    // Would need to be added to sensor_scheduler API
    ESP_LOGW(TAG, "Dynamic sensor enable/disable not yet implemented");
    return ESP_ERR_NOT_SUPPORTED;
}

#ifdef CONFIG_SYNAPSE_FEATURE_GPS
esp_err_t synapse_sensors_set_gps_power_mode(bool low_power) {
    if (!g_sensors.initialized) return ESP_ERR_INVALID_STATE;

    if (low_power) {
        // Reduce GPS rate to 0.2Hz (5s interval)
        return gps_max_m10s_set_nav_rate(5000, 1, 0);
    } else {
        // Normal 1Hz
        return gps_max_m10s_set_nav_rate(1000, 1, 0);
    }
}
#endif

// Periodic update for GPS/Temp data (call from main loop or acquisition FSM)
void synapse_sensors_update_aux_data(void) {
    if (!g_sensors.initialized) return;

#ifdef SYNAPSE_HAS_AUX_SENSORS
 #ifdef CONFIG_SYNAPSE_FEATURE_GPS
    gps_data_t gps;
    if (gps_max_m10s_get_latest(&gps) == ESP_OK) {
        if (xSemaphoreTake(g_sensors.data_mutex, pdMS_TO_TICKS(10)) == pdTRUE) {
            g_sensors.latest_gps = gps;
            xSemaphoreGive(g_sensors.data_mutex);
        }
    }
 #endif

 #ifdef CONFIG_SYNAPSE_FEATURE_TEMPERATURE
    temp_data_t temp;
    if (temp_tmp117_get_latest(&temp) == ESP_OK) {
        if (xSemaphoreTake(g_sensors.data_mutex, pdMS_TO_TICKS(10)) == pdTRUE) {
            g_sensors.latest_temp = temp;
            xSemaphoreGive(g_sensors.data_mutex);
        }
    }
 #endif
#else
    // band-core build: no auxiliary sensors, nothing to refresh.
    (void)0;
#endif
}