#pragma once
#include <stddef.h>
#include <stdint.h>
#include "esp_err.h"
#ifdef __cplusplus
extern "C" {
#endif
typedef enum { SENSOR_TYPE_ECG = 0, SENSOR_TYPE_PPG = 1, SENSOR_TYPE_IMU = 2 } sensor_type_t;
typedef struct sensor_scheduler sensor_scheduler_t;
extern sensor_scheduler_t* g_sensor_scheduler_ptr;
esp_err_t sensor_scheduler_set_rate(sensor_scheduler_t* s, sensor_type_t type, uint32_t rate_hz);
/* test hook: last PPG rate requested by the motion gate */
uint32_t host_last_ppg_rate(void);
#ifdef __cplusplus
}
#endif
