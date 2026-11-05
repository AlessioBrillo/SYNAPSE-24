/* Host wrappers over firmware/esp32_band/acquisition/synapse_night_window.h (static inline there). */
#include <stdint.h>

#include "synapse_night_window.h"

int host_clock_is_set(int64_t epoch_s) { return synapse_clock_is_set(epoch_s); }
int host_night_active(int64_t epoch_s, int start_hour, int end_hour)
{
    return synapse_night_window_active(epoch_s, start_hour, end_hour);
}
