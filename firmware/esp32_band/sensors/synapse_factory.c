/**
 * @file synapse_factory.c
 * @brief The factory test (see synapse_factory.h).
 */

#include "synapse_factory.h"

#include <stdio.h>
#include "esp_log.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "imu_icm20948.h"
#include "ppg_max30102.h"
#include "synapse_config.h"
#include "synapse_diag.h"
#include "synapse_selftest.h"
#include "synapse_sensors.h"
#if defined(CONFIG_SYNAPSE_FEATURE_TEMPERATURE)
#include "temp_tmp117.h"
#endif
#if defined(CONFIG_SYNAPSE_FEATURE_POWER_MONITOR)
#include "ina219.h"
#endif

static const char *TAG = "FACTORY";

#define FACTORY_ACQ_SECONDS 10
#define FACTORY_ECG_HZ      500 /* the ECG is polled at a fixed 500 Hz (synapse_sensors.c) */
#define FACTORY_ECG_BIAS_S  1   /* the first second of ECG is spent measuring the mid-rail: no samples */
#define AK09916_ADDR        0x0C

/* One expected part on the bus. */
typedef struct {
    uint8_t addr;
    const char *item; /* report item name */
} expected_t;

static bool in_list(const uint8_t *list, int n, uint8_t addr)
{
    for (int i = 0; i < n; i++) {
        if (list[i] == addr) {
            return true;
        }
    }
    return false;
}

static void step_bus(st_report_t *r, const synapse_hw_config_t *cfg)
{
    uint8_t found[SYNAPSE_DIAG_MAX_FOUND];
    const int n = synapse_diag_i2c_scan(cfg->ppg_i2c_port, found, SYNAPSE_DIAG_MAX_FOUND);
    if (n < 0) {
        st_report_add(r, "i2c_scan", false, "the bus failed: %s", esp_err_to_name(-n));
        return;
    }
    const int listed = n < SYNAPSE_DIAG_MAX_FOUND ? n : SYNAPSE_DIAG_MAX_FOUND;
    for (int i = 0; i < listed; i++) {
        const char *name = synapse_diag_i2c_name(found[i]);
        ESP_LOGI(TAG, "bus %d: 0x%02X %s", cfg->ppg_i2c_port, found[i], name ? name : "(unknown device)");
        synapse_diag_reg_t regs[SYNAPSE_DIAG_MAX_REGS];
        const int nr = synapse_diag_dump_device(cfg->ppg_i2c_port, found[i], regs, SYNAPSE_DIAG_MAX_REGS);
        if (nr > 0) {
            char line[96];
            synapse_diag_format(regs, nr, line, sizeof(line));
            ESP_LOGI(TAG, "      registers: %s", line);
        }
    }

    expected_t expected[5] = {
        {(uint8_t)cfg->ppg_i2c_addr, "i2c_ppg"},
        {(uint8_t)cfg->imu_i2c_addr, "i2c_imu"},
        {AK09916_ADDR, "i2c_magnetometer"},
        {0, NULL},
        {0, NULL},
    };
    int ne = 3;
#if defined(CONFIG_SYNAPSE_FEATURE_TEMPERATURE)
    expected[ne++] = (expected_t){(uint8_t)cfg->temp_i2c_addr, "i2c_temperature"};
#endif
#if defined(CONFIG_SYNAPSE_FEATURE_POWER_MONITOR)
    expected[ne++] = (expected_t){(uint8_t)CONFIG_SYNAPSE_INA219_I2C_ADDR, "i2c_power_monitor"};
#endif
    for (int i = 0; i < ne; i++) {
        const bool seen = in_list(found, listed, expected[i].addr);
        st_report_add(r, expected[i].item, seen, seen ? "0x%02X answers" : "nothing answers at 0x%02X", expected[i].addr);
    }
    for (int i = 0; i < listed; i++) {
        bool wanted = false;
        for (int j = 0; j < ne; j++) {
            wanted = wanted || expected[j].addr == found[i];
        }
        if (!wanted) {
            ESP_LOGW(TAG, "0x%02X answers but no sensor of this build is configured there", found[i]);
        }
    }
}

static void run_self_test(st_report_t *r, const char *name, uint8_t health_bit, bool carried,
                          esp_err_t (*test)(char *, size_t))
{
    if (!carried) {
        return;
    }
    if (!(synapse_sensors_health_mask() & (1u << health_bit))) {
        st_report_add(r, name, false, "the sensor did not initialise, nothing to test");
        return;
    }
    char detail[ST_DETAIL_LEN] = "";
    const esp_err_t e = test(detail, sizeof(detail));
    st_report_add_result(r, name, ESP_OK, e, detail);
}

static void step_self_tests(st_report_t *r)
{
    run_self_test(r, "selftest_ppg", SENSOR_TYPE_PPG, true, ppg_max30102_self_test);
    run_self_test(r, "selftest_imu", SENSOR_TYPE_IMU, true, imu_icm20948_self_test);
#if defined(CONFIG_SYNAPSE_FEATURE_TEMPERATURE)
    if (synapse_sensors_temperature_ok()) {
        char detail[ST_DETAIL_LEN] = "";
        const esp_err_t e = temp_tmp117_self_test(detail, sizeof(detail));
        st_report_add_result(r, "selftest_temperature", ESP_OK, e, detail);
    } else {
        st_report_add(r, "selftest_temperature", false, "the sensor did not initialise, nothing to test");
    }
#endif
#if defined(CONFIG_SYNAPSE_FEATURE_POWER_MONITOR)
    if (ina219_is_healthy()) {
        char detail[ST_DETAIL_LEN] = "";
        const esp_err_t e = ina219_self_test(detail, sizeof(detail));
        st_report_add_result(r, "selftest_power_monitor", ESP_OK, e, detail);
    } else {
        st_report_add(r, "selftest_power_monitor", false, "the monitor did not initialise, nothing to test");
    }
#endif
}

static void step_acquisition(st_report_t *r, const synapse_hw_config_t *cfg)
{
    uint32_t n_ecg = 0, n_ppg = 0, n_imu = 0;
    double ppg_ir_sum = 0.0;
    const int64_t end_us = esp_timer_get_time() + (int64_t)FACTORY_ACQ_SECONDS * 1000000;
    synapse_sensor_sample_t s;
    while (esp_timer_get_time() < end_us) {
        if (synapse_sensors_get_sample(&s) != ESP_OK) {
            vTaskDelay(pdMS_TO_TICKS(5));
            continue;
        }
        switch (s.base.type) {
        case SENSOR_TYPE_ECG: n_ecg++; break;
        case SENSOR_TYPE_PPG: n_ppg++; ppg_ir_sum += (double)s.base.data.ppg.ir; break;
        case SENSOR_TYPE_IMU: n_imu++; break;
        default: break;
        }
    }
    const uint32_t ppg_hz = synapse_ppg_rate_hz(cfg->ppg_sample_rate);
    const uint32_t imu_hz = (uint32_t)cfg->imu_accel_odr_hz;

    /* Delivery is judged; the ECG signal is only reported (no electrodes on a station). */
    st_report_add(r, "stream_ecg", st_count_ok(n_ecg, FACTORY_ECG_HZ, FACTORY_ACQ_SECONDS - FACTORY_ECG_BIAS_S),
                  "%lu samples in %d s (%d Hz, first %d s is the bias); lead-off=%d stuck=%d",
                  (unsigned long)n_ecg, FACTORY_ACQ_SECONDS, FACTORY_ECG_HZ, FACTORY_ECG_BIAS_S,
                  (int)synapse_sensors_ecg_lead_off(), (int)synapse_sensors_ecg_signal_stuck());
    st_report_add(r, "stream_ppg", st_count_ok(n_ppg, ppg_hz, FACTORY_ACQ_SECONDS) && ppg_ir_sum > 0.0,
                  "%lu samples in %d s (%lu Hz), mean IR %.0f", (unsigned long)n_ppg, FACTORY_ACQ_SECONDS,
                  (unsigned long)ppg_hz, n_ppg ? ppg_ir_sum / (double)n_ppg : 0.0);
    st_report_add(r, "stream_imu", st_count_ok(n_imu, imu_hz, FACTORY_ACQ_SECONDS),
                  "%lu samples in %d s (%lu Hz)", (unsigned long)n_imu, FACTORY_ACQ_SECONDS, (unsigned long)imu_hz);
}

bool synapse_factory_run(void)
{
    ESP_LOGI(TAG, "=== factory test (keep the band still on the bench, ~%d s) ===", FACTORY_ACQ_SECONDS + 3);
    static st_report_t report; /* ~2 kB: static, not on the main task's stack */
    st_report_init(&report);

    synapse_hw_config_t cfg;
    if (synapse_config_get(&cfg) != ESP_OK) {
        st_report_add(&report, "config", false, "hardware configuration unavailable");
    } else {
        const uint8_t mask = synapse_sensors_health_mask();
        st_report_add(&report, "init_ecg", mask & (1u << SENSOR_TYPE_ECG), "ECG %s",
                      (mask & (1u << SENSOR_TYPE_ECG)) ? "initialised" : "did not initialise");
        st_report_add(&report, "init_ppg", mask & (1u << SENSOR_TYPE_PPG), "PPG %s",
                      (mask & (1u << SENSOR_TYPE_PPG)) ? "initialised" : "did not initialise");
        st_report_add(&report, "init_imu", mask & (1u << SENSOR_TYPE_IMU), "IMU %s",
                      (mask & (1u << SENSOR_TYPE_IMU)) ? "initialised" : "did not initialise");
        step_bus(&report, &cfg);
        step_self_tests(&report);
        step_acquisition(&report, &cfg);
    }

    for (int i = 0; i < report.n; i++) {
        ESP_LOGI(TAG, "[%s] %s: %s", report.items[i].pass ? "PASS" : "FAIL", report.items[i].name,
                 report.items[i].detail);
    }
    if (report.dropped > 0) {
        ESP_LOGE(TAG, "%d check(s) did not fit in the report and count as failed", report.dropped);
    }
    const bool pass = st_report_pass(&report);
    if (pass) {
        ESP_LOGI(TAG, "FACTORY RESULT: PASS (%d checks)", report.n);
    } else {
        ESP_LOGE(TAG, "FACTORY RESULT: FAIL (%d of %d failed)", st_report_failed(&report), report.n);
    }
    return pass;
}
