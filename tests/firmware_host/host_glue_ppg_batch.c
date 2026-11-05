/* Host wrappers over firmware/common/sensors/ppg_fifo_batch.h (static inline there). */
#include <stdint.h>

#include "ppg_fifo_batch.h"

uint32_t host_ppg_period_us(uint8_t code) { return ppg_period_us_from_code(code); }
int64_t host_ppg_batch_ts(int64_t poll_us, uint32_t count, uint32_t index, uint32_t period_us)
{
    return ppg_batch_timestamp_us(poll_us, count, index, period_us);
}
uint32_t host_ppg_batch_max(void) { return PPG_BATCH_MAX; }
