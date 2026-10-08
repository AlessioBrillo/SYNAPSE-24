#include "sensor_scheduler.h"

static sensor_scheduler_t s_sched = {{50, 50, 50, 0}};
sensor_scheduler_t* g_sensor_scheduler_ptr = &s_sched;

esp_err_t sensor_scheduler_set_rate(sensor_scheduler_t* s, sensor_type_t type, uint32_t rate_hz) {
    s->rates_hz[type] = rate_hz;
    return ESP_OK;
}
uint32_t host_last_ppg_rate(void) { return s_sched.rates_hz[SENSOR_TYPE_PPG]; }
void host_set_ppg_rate(uint32_t hz) { s_sched.rates_hz[SENSOR_TYPE_PPG] = hz; }
