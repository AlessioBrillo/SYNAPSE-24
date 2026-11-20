/* Host wrappers over firmware/common/sensors/ecg_stuck.h (static inline there). */
#include <stdbool.h>
#include <stdint.h>

#include "ecg_stuck.h"

static ecg_stuck_t s_s;

void host_stuck_reset(void) { ecg_stuck_reset(&s_s); }
int host_stuck_feed(int raw) { return ecg_stuck_update(&s_s, raw) ? 1 : 0; }
uint32_t host_stuck_min_samples(void) { return ECG_STUCK_MIN_SAMPLES; }
int host_stuck_low(void) { return ECG_STUCK_RAW_LOW; }
int host_stuck_high(void) { return ECG_STUCK_RAW_HIGH; }
