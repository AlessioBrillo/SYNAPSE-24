#pragma once
#include <stddef.h>
#include <stdint.h>
#include <stdbool.h>
#include "esp_err.h"
#ifdef __cplusplus
extern "C" {
#endif
typedef enum { SENSOR_TYPE_ECG = 0, SENSOR_TYPE_PPG = 1, SENSOR_TYPE_IMU = 2, SENSOR_TYPE_EEG_IN_EAR = 3, SENSOR_TYPE_MAX = 4 } sensor_type_t;
struct sensor_scheduler { volatile uint32_t rates_hz[4]; };
typedef struct sensor_scheduler sensor_scheduler_t;
extern sensor_scheduler_t* g_sensor_scheduler_ptr;
esp_err_t sensor_scheduler_set_rate(sensor_scheduler_t* s, sensor_type_t type, uint32_t rate_hz);

/* Mirrors firmware/common/scheduler/sensor_scheduler.h so the Band research
 * service can be compiled and driven on the host. */
typedef struct {
    sensor_type_t type;
    int64_t timestamp_us;
    int64_t lsl_timestamp_us;
    uint16_t sequence;
    union {
        struct { float voltage_mv; } ecg;
        struct { float red; float ir; float green; } ppg;
        struct { float ax; float ay; float az; float gx; float gy; float gz; float mx; float my; float mz; } imu;
        struct { float ch1; float ch2; } eeg;
    } data;
} sensor_sample_t;

#ifdef __cplusplus
}
#endif