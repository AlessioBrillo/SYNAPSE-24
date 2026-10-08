#include "sensor_scheduler.h"

static int s_dummy;
sensor_scheduler_t* g_sensor_scheduler_ptr = (sensor_scheduler_t*)&s_dummy;
static uint32_t s_ppg_rate;

esp_err_t sensor_scheduler_set_rate(sensor_scheduler_t* s, sensor_type_t type, uint32_t rate_hz) {
    (void)s;
    if (type == SENSOR_TYPE_PPG) s_ppg_rate = rate_hz;
    return ESP_OK;
}
uint32_t host_last_ppg_rate(void) { return s_ppg_rate; }
