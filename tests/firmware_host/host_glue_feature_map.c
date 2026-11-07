/* Host wrappers over firmware/esp32_band/main/synapse_feature_map.h (static inline there). */
#include <stdint.h>

#include "synapse_feature_map.h"
#include "synapse_hr_measurement.h"
#include "synapse_window.h"

uint8_t host_unit_to_u8(float v) { return synapse_unit_to_u8(v); }
uint16_t host_hr_to_u16(float bpm) { return synapse_hr_to_u16(bpm); }
int host_is_fresh(int64_t now_us, int64_t stamp_us, int64_t max_age_us)
{
    return synapse_is_fresh(now_us, stamp_us, max_age_us);
}
uint8_t host_hr_flags(int rr_present) { return synapse_hr_flags(rr_present); }
int host_hr_is_readable(int64_t now_us, int64_t last_us)
{
    return synapse_hr_is_readable(now_us, last_us);
}
int host_window_is_new(int64_t *last_us, int64_t stamp_us)
{
    return synapse_window_is_new(last_us, stamp_us);
}
int host_window_is_current(int64_t now_us, int64_t stamp_us)
{
    return synapse_window_is_current(now_us, stamp_us);
}
int64_t host_ppg_max_age_us(void) { return SYNAPSE_PPG_MAX_AGE_US; }
int host_rr_publishable(int fresh, float sqi, float motion)
{
    return synapse_rr_publishable(fresh, sqi, motion);
}
