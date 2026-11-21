/* Host wrappers over firmware/common/sensors/synapse_selftest.h (static inline there). */
#include <stdbool.h>
#include <stdint.h>

#include "synapse_selftest.h"

int h_temp_ok(float c) { return st_temp_plausible_c(c) ? 1 : 0; }
float h_max30102_temp(int integer, unsigned frac) { return st_max30102_temp_c((int8_t)integer, (uint8_t)frac); }
int h_fifo_advanced(unsigned a, unsigned b) { return st_fifo_advanced((uint8_t)a, (uint8_t)b) ? 1 : 0; }
int h_bus_v_ok(float v) { return st_bus_voltage_plausible(v) ? 1 : 0; }
int h_imu_rest(float ax, float ay, float az, float gx, float gy, float gz)
{
    return st_imu_rest_ok(ax, ay, az, gx, gy, gz) ? 1 : 0;
}
int h_mag_ok(float x, float y, float z) { return st_mag_ok(x, y, z) ? 1 : 0; }
int h_count_ok(unsigned count, unsigned hz, unsigned seconds) { return st_count_ok(count, hz, seconds) ? 1 : 0; }

/* The report: a few checks, then the verdict. */
static st_report_t s_r;
void h_report_init(void) { st_report_init(&s_r); }
void h_report_add(const char *name, int pass, const char *detail) { st_report_add(&s_r, name, pass != 0, "%s", detail); }
void h_report_add_n(int n)
{
    for (int i = 0; i < n; i++) {
        st_report_add(&s_r, "item", true, "ok %d", i);
    }
}
int h_report_pass(void) { return st_report_pass(&s_r) ? 1 : 0; }
int h_report_failed(void) { return st_report_failed(&s_r); }
int h_report_n(void) { return s_r.n; }
int h_report_dropped(void) { return s_r.dropped; }
const char *h_report_detail(int i) { return s_r.items[i].detail; }
const char *h_report_name(int i) { return s_r.items[i].name; }
int h_max_items(void) { return ST_MAX_ITEMS; }
int h_detail_len(void) { return ST_DETAIL_LEN; }
