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
#include "synapse_calibration.h"
#include "sensor_scheduler.h"
#include "ecg_ad8232.h"
#include "ppg_max30102.h"
#include "imu_icm20948.h"
#include "gps_max_m10s.h"
#include "temp_tmp117.h"
#include "synapse_adc1.h"
#include "synapse_diag.h"
#include "synapse_i2c.h"

#if defined(CONFIG_SYNAPSE_FEATURE_GPS) || defined(CONFIG_SYNAPSE_FEATURE_TEMPERATURE)
#define SYNAPSE_HAS_AUX_SENSORS 1
#endif

static const char *TAG = "SYNAPSE_SENSORS";

static synapse_sensors_t g_sensors = {0};
static synapse_calibration_t s_calibration;
static bool s_has_calibration = false;

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
    g_sensors.ecg_config.gpio_lead_off = hw_cfg->ecg_gpio_lead_off;
    g_sensors.ecg_config.gain = hw_cfg->ecg_gain;
    adc_oneshot_unit_handle_t adc1 = NULL;
    if (synapse_adc1_get(&adc1) == ESP_OK) {
        g_sensors.ecg_config.adc_handle = adc1;
    } else {
        ESP_LOGE(TAG, "ADC1 unavailable: ECG will fail init (fail-soft)");
    }

    // PPG
    // The bus wiring is one set of values for every sensor on it (CONFIG_SYNAPSE_I2C_*, default
    // SDA 8 / SCL 9: GPIO 22-25 do not exist on the ESP32-S3 and 26-37 are flash/PSRAM on the N8R8).
    g_sensors.ppg_config.i2c_port = hw_cfg->ppg_i2c_port;
    g_sensors.ppg_config.i2c_addr = hw_cfg->ppg_i2c_addr;
    g_sensors.ppg_config.gpio_int = hw_cfg->ppg_gpio_int;
    g_sensors.ppg_config.sda_gpio_num = hw_cfg->i2c_sda_gpio;
    g_sensors.ppg_config.scl_gpio_num = hw_cfg->i2c_scl_gpio;
    g_sensors.ppg_config.i2c_freq_hz = hw_cfg->i2c_freq_hz;
    g_sensors.ppg_config.led_current_red = hw_cfg->ppg_led_current_red;
    g_sensors.ppg_config.led_current_ir = hw_cfg->ppg_led_current_ir;
    g_sensors.ppg_config.sample_rate = hw_cfg->ppg_sample_rate;
    g_sensors.ppg_config.pulse_width = hw_cfg->ppg_pulse_width;
    g_sensors.ppg_config.adc_range = hw_cfg->ppg_adc_range;

    // IMU
    g_sensors.imu_config.i2c_port = hw_cfg->imu_i2c_port;
    g_sensors.imu_config.i2c_addr = hw_cfg->imu_i2c_addr;
    g_sensors.imu_config.gpio_int = hw_cfg->imu_gpio_int;
    g_sensors.imu_config.sda_gpio_num = hw_cfg->i2c_sda_gpio;
    g_sensors.imu_config.scl_gpio_num = hw_cfg->i2c_scl_gpio;
    g_sensors.imu_config.i2c_freq_hz = hw_cfg->i2c_freq_hz;
    g_sensors.imu_config.accel_fsr_g = hw_cfg->imu_accel_fsr_g;
    g_sensors.imu_config.gyro_fsr_dps = hw_cfg->imu_gyro_fsr_dps;
    g_sensors.imu_config.accel_odr_hz = hw_cfg->imu_accel_odr_hz;
    g_sensors.imu_config.gyro_odr_hz = hw_cfg->imu_gyro_odr_hz;
    if (s_has_calibration) {
        memcpy(g_sensors.imu_config.accel_bias_mg, s_calibration.accel_bias_mg,
               sizeof(g_sensors.imu_config.accel_bias_mg));
        memcpy(g_sensors.imu_config.gyro_bias_mdps, s_calibration.gyro_bias_mdps,
               sizeof(g_sensors.imu_config.gyro_bias_mdps));
    }

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
    g_sensors.temp_config.sda_gpio = hw_cfg->i2c_sda_gpio;
    g_sensors.temp_config.scl_gpio = hw_cfg->i2c_scl_gpio;
    g_sensors.temp_config.i2c_freq_hz = hw_cfg->i2c_freq_hz;
    g_sensors.temp_config.gpio_alert = hw_cfg->temp_gpio_alert;
    g_sensors.temp_config.conversion_cycle_ms = 1000; // 1Hz updates
    g_sensors.temp_config.continuous_mode = true;
}

// A failing sensor must be reported and skipped, never ESP_ERROR_CHECK'd: that aborts and reboots
// the whole band, which then fails the same way on the next boot (a reboot loop that also hides
// the cause). The sensors that work keep streaming.
static void log_if_failed(esp_err_t err, const char *what) {
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "%s failed: %s (continuing)", what, esp_err_to_name(err));
    }
}

// Log who answers on the I2C bus. The bus may have no owner left (every driver failed and let go),
// so take a reference for the scan with the wiring the drivers use.
static void log_bus_scan(const synapse_hw_config_t *c) {
    if (synapse_i2c_acquire(c->ppg_i2c_port, c->i2c_sda_gpio, c->i2c_scl_gpio, c->i2c_freq_hz) != ESP_OK) {
        ESP_LOGE(TAG, "cannot scan the I2C bus (it could not be set up)");
        return;
    }
    uint8_t found[SYNAPSE_DIAG_MAX_FOUND];
    const int n = synapse_diag_i2c_scan(c->ppg_i2c_port, found, SYNAPSE_DIAG_MAX_FOUND);
    if (n < 0) {
        ESP_LOGE(TAG, "I2C scan failed: %s", esp_err_to_name(-n));
    } else if (n == 0) {
        ESP_LOGW(TAG, "I2C scan: nobody answers (SDA=%d SCL=%d): check the power, the wiring and the pull-ups",
                 c->i2c_sda_gpio, c->i2c_scl_gpio);
    } else {
        const int listed = n < SYNAPSE_DIAG_MAX_FOUND ? n : SYNAPSE_DIAG_MAX_FOUND;
        for (int i = 0; i < listed; i++) {
            const char *name = synapse_diag_i2c_name(found[i]);
            ESP_LOGW(TAG, "I2C scan: 0x%02X answers (%s)", found[i], name ? name : "unknown device");
        }
    }
    synapse_i2c_release(c->ppg_i2c_port);
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
#ifdef CONFIG_SYNAPSE_FEATURE_GPS
static bool s_gps_ok = false;
#endif
#ifdef CONFIG_SYNAPSE_FEATURE_TEMPERATURE
static bool s_temp_ok = false;
static void note_temp_init(esp_err_t err) {
    s_temp_ok = (err == ESP_OK);
    if (!s_temp_ok) {
        ESP_LOGE(TAG, "Temperature init failed: %s (continuing without it)", esp_err_to_name(err));
    }
}
#endif
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
    if (xTaskCreatePinnedToCore(sensor_task_fn, cfg->name, stack, &s_sensor_cfg[idx], prio,
                                &s_sensor_task[idx], SENSOR_TASK_CORE) != pdPASS) {
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

bool synapse_sensors_temperature_ok(void) {
#ifdef CONFIG_SYNAPSE_FEATURE_TEMPERATURE
    return s_temp_ok;
#else
    return false;
#endif
}

uint8_t synapse_sensors_health_mask(void) {
    return s_sensor_health;
}

bool synapse_sensors_ecg_lead_off(void) {
    // Only meaningful while the ECG initialised; otherwise nothing is measured and nothing is flagged.
    return (s_sensor_health & (1u << SENSOR_TYPE_ECG)) && ecg_ad8232_is_lead_off(NULL);
}

bool synapse_sensors_ecg_signal_stuck(void) {
    return (s_sensor_health & (1u << SENSOR_TYPE_ECG)) && ecg_ad8232_is_signal_stuck(NULL);
}

uint32_t synapse_sensors_ppg_skipped(void) {
    return ppg_max30102_skipped_samples();
}

uint32_t synapse_sensors_dropped_samples(void) {
    uint32_t total = 0;
    for (int i = 0; i < SENSOR_SCHEDULER_MAX_SENSORS; i++) {
        total += g_sensors.scheduler.dropped_samples[i];
    }
    return total;
}

uint32_t synapse_sensors_read_errors(void) {
    uint32_t total = 0;
    for (int i = 0; i < SENSOR_SCHEDULER_MAX_SENSORS; i++) {
        total += g_sensors.scheduler.read_errors[i];
    }
    return total;
}

esp_err_t synapse_sensors_init(void) {
    if (g_sensors.initialized) {
        ESP_LOGW(TAG, "Already initialized");
        return ESP_OK;
    }

    // Load hardware configuration, then overlay the per-unit calibration.
    // Kconfig stays the default for an uncalibrated band; the NVS record is
    // the measured truth for this serial. A corrupt record is reported and
    // ignored, never applied as an unknown scale factor.
    synapse_hw_config_t hw_cfg;
    esp_err_t cfg_err = synapse_config_get(&hw_cfg);
    if (cfg_err != ESP_OK) {
        ESP_LOGE(TAG, "Hardware configuration unavailable: %s", esp_err_to_name(cfg_err));
        return cfg_err;
    }
    s_has_calibration = false;
    esp_err_t cal_err = synapse_calibration_load(&s_calibration);
    if (cal_err != ESP_OK) {
        ESP_LOGW(TAG, "Calibration unavailable (%s): using Kconfig defaults", esp_err_to_name(cal_err));
    } else if (synapse_calibration_valid(&s_calibration)) {
        s_has_calibration = true;
        if (s_calibration.ecg_gain_x100 > 0u) {
            hw_cfg.ecg_gain = (float)s_calibration.ecg_gain_x100 / 100.0f;
        }
        if (s_calibration.ppg_led_red > 0u) {
            hw_cfg.ppg_led_current_red = s_calibration.ppg_led_red;
        }
        if (s_calibration.ppg_led_ir > 0u) {
            hw_cfg.ppg_led_current_ir = s_calibration.ppg_led_ir;
        }
        if (s_calibration.battery_capacity_mah > 0u) {
            hw_cfg.hub_battery_mah = (float)s_calibration.battery_capacity_mah;
        }
        ESP_LOGI(TAG, "Applied calibration for %s (source=%s)",
                 s_calibration.serial, synapse_calibration_state());
    }
    // Remember the sensor-effective values this boot applied. Both the USB
    // shell and the BLE whitelist write NVS; the readback compares this
    // effective CRC with the stored record so a profile cannot look applied
    // before a reboot, while a metadata-only edit does not create a false one.
    synapse_calibration_mark_applied(&s_calibration);
    // Publish the effective values through the config manager: the power
    // module and the device-config readback both read it, so neither can keep
    // reporting Kconfig defaults after a per-unit profile was applied. This
    // updates RAM only (the config manager is never saved here).
    esp_err_t set_err = synapse_config_set(&hw_cfg);
    if (set_err != ESP_OK) {
        ESP_LOGW(TAG, "Effective calibration config not published: %s", esp_err_to_name(set_err));
    }
    populate_sensor_configs(&hw_cfg);

    // Create sample queue
    // The deep queue is ~16 kB of internal RAM; if the heap is short, a shallower one (the old
    // depth) is better than a band that does not start.
    g_sensors.sample_queue = xQueueCreate(SENSOR_SCHEDULER_BLE_QUEUE_SIZE, sizeof(sensor_sample_t)) /* scheduler enqueues plain sensor_sample_t */;
    if (!g_sensors.sample_queue) {
        ESP_LOGW(TAG, "sample queue of %d does not fit: falling back to 64", SENSOR_SCHEDULER_BLE_QUEUE_SIZE);
        g_sensors.sample_queue = xQueueCreate(64, sizeof(sensor_sample_t));
    }
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
    esp_err_t sched_err = sensor_scheduler_init(&g_sensors.scheduler, g_sensors.sample_queue);
    if (sched_err != ESP_OK) {
        ESP_LOGE(TAG, "Sensor scheduler init failed: %s", esp_err_to_name(sched_err));
        vSemaphoreDelete(g_sensors.data_mutex);
        g_sensors.data_mutex = NULL;
        vQueueDelete(g_sensors.sample_queue);
        g_sensors.sample_queue = NULL;
        return sched_err;
    }

    // Register ECG sensor
    sensor_config_t ecg_sensor = {
        .type = SENSOR_TYPE_ECG,
        .name = "ECG_AD8232",
        .sampling_rate_hz = 500,
        .init = ecg_init_wrap,
        .read = ecg_read_wrap,
        .take_lost = ecg_ad8232_take_lost,
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
        .has_more = ppg_max30102_has_more,
        .take_lost = ppg_max30102_take_lost,
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
    // Initialize GPS: a missing chip must not take the whole band down at boot.
    s_gps_ok = (gps_max_m10s_init(&g_sensors.gps_config) == ESP_OK);
    if (!s_gps_ok) {
        ESP_LOGE(TAG, "GPS init failed (continuing without it)");
    }
#endif

#ifdef CONFIG_SYNAPSE_FEATURE_TEMPERATURE
    // Initialize Temperature: fail soft like ECG/PPG/IMU, and say so in sensor_health.
    note_temp_init(temp_tmp117_init(&g_sensors.temp_config));
#endif

    // A PPG or IMU that did not come up: show who does answer on the bus, so a wiring fault is
    // told from a wrong address or a missing part without a debugger. (The ECG is on the ADC.)
    if (!(s_sensor_health & (1u << SENSOR_TYPE_PPG)) || !(s_sensor_health & (1u << SENSOR_TYPE_IMU))) {
        log_bus_scan(&hw_cfg);
    }

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
    ESP_LOGI(TAG, "Sensors initialized (ECG:500Hz, PPG:%dHz, IMU:%dHz) [%s]",
             (int)synapse_ppg_rate_hz(hw_cfg.ppg_sample_rate), hw_cfg.imu_accel_odr_hz,
             CONFIG_SYNAPSE_BUILD_VARIANT);
#endif
    return ESP_OK;
}

esp_err_t synapse_sensors_start(void) {
    if (!g_sensors.initialized) return ESP_ERR_INVALID_STATE;
    if (g_sensors.running) return ESP_OK;

    // Start sensor scheduler (creates ECG/PPG/IMU tasks)
    esp_err_t start_err = sensor_scheduler_start(&g_sensors.scheduler);
    if (start_err != ESP_OK) {
        ESP_LOGE(TAG, "Sensor scheduler start failed: %s", esp_err_to_name(start_err));
        return start_err;
    }

#ifdef CONFIG_SYNAPSE_FEATURE_GPS
    // Start GPS (only if it initialised: s_gps_ok), a missing chip does not stop the rest
    if (s_gps_ok) {
        log_if_failed(gps_max_m10s_start(), "GPS start");
    }
#endif

#ifdef CONFIG_SYNAPSE_FEATURE_TEMPERATURE
    // Start Temperature (only if it initialised: s_temp_ok)
    if (s_temp_ok) {
        log_if_failed(temp_tmp117_start(), "Temperature start");
    }
#endif

    g_sensors.running = true;
    ESP_LOGI(TAG, "All sensors started");
    return ESP_OK;
}

esp_err_t synapse_sensors_stop(void) {
    if (!g_sensors.running) return ESP_OK;

    // Stop sensor scheduler
    log_if_failed(sensor_scheduler_stop(&g_sensors.scheduler), "Sensor scheduler stop");

#ifdef CONFIG_SYNAPSE_FEATURE_GPS
    // Stop GPS
    log_if_failed(gps_max_m10s_stop(), "GPS stop");
#endif

#ifdef CONFIG_SYNAPSE_FEATURE_TEMPERATURE
    // Stop Temperature
    log_if_failed(temp_tmp117_stop(), "Temperature stop");
#endif

    g_sensors.running = false;
    ESP_LOGI(TAG, "All sensors stopped");
    return ESP_OK;
}

esp_err_t synapse_sensors_deinit(void) {
    synapse_sensors_stop();

#ifdef CONFIG_SYNAPSE_FEATURE_GPS
    // Deinitialize GPS
    log_if_failed(gps_max_m10s_deinit(), "GPS deinit");
#endif

#ifdef CONFIG_SYNAPSE_FEATURE_TEMPERATURE
    // Deinitialize Temperature
    log_if_failed(temp_tmp117_deinit(), "Temperature deinit");
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
    log_if_failed(sensor_scheduler_deinit(&g_sensors.scheduler), "Sensor scheduler deinit");

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
    if (!s_gps_ok) return ESP_ERR_INVALID_STATE;  // init failed: never read an unstarted driver
    return gps_max_m10s_get_latest(gps);
}
#endif

#ifdef CONFIG_SYNAPSE_FEATURE_TEMPERATURE
esp_err_t synapse_sensors_get_temp(temp_data_t *temp) {
    if (!temp) return ESP_ERR_INVALID_ARG;
    if (!s_temp_ok) return ESP_ERR_INVALID_STATE;  // init failed: never read an unstarted driver
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
    if (s_gps_ok && gps_max_m10s_get_latest(&gps) == ESP_OK) {
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