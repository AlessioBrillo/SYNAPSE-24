/* Host wrappers over firmware/common/sensors/imu_odr.h (static inline there). */
#include <stdint.h>

#include "imu_odr.h"

uint32_t host_imu_div(uint32_t base_hz, uint32_t poll_hz) { return imu_smplrt_div_for(base_hz, poll_hz); }
uint32_t host_imu_odr_mhz(uint32_t base_hz, uint32_t div) { return imu_odr_mhz(base_hz, div); }
uint32_t host_imu_effective_odr_hz(uint32_t poll_hz, uint32_t base_hz, uint32_t max_div) {
    return imu_effective_odr_hz(poll_hz, base_hz, max_div);
}
