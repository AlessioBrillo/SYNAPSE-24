/**
 * @file imu_odr.h
 * @brief Output-rate dividers of the ICM-20948. Pure C, no ESP-IDF.
 *
 * The band has no IMU FIFO and no data-ready interrupt: the scheduler polls at the configured rate
 * and reads whatever the sensor holds. A sensor running at the same rate as the poll slips against
 * it (the two clocks differ by a percent or two), so some polls find the previous sample again, a
 * duplicate stamped as new, while the sensor's own sample goes unread. The sensor therefore runs
 * IMU_ODR_OVERSAMPLE times faster than the poll and every poll finds a new sample, at most one
 * sensor period old (the DLPF, set in the driver, keeps the faster ODR from aliasing).
 *
 * The old divider rounded to the NEAREST rate: 1125 Hz / 23 = 48.9 Hz for a 50 Hz poll.
 * Compiled and run on the host (tests/firmware_host/host_glue_imu_odr.c).
 */
#ifndef IMU_ODR_H
#define IMU_ODR_H

#include <stdint.h>

/** The sensor samples this many times per poll. */
#define IMU_ODR_OVERSAMPLE 2u

/** Rates before the sample-rate divider, with the DLPF enabled (ICM-20948). */
#define ICM20948_ACCEL_BASE_ODR_HZ 1125u
#define ICM20948_GYRO_BASE_ODR_HZ 1100u

/**
 * Divider register value for a poll of @p poll_hz on a sensor whose rate before the divider is
 * @p base_hz: ODR = base / (1 + div), and never below IMU_ODR_OVERSAMPLE * poll (clamped at the
 * base rate itself when the poll is that fast).
 */
static inline uint32_t imu_smplrt_div_for(uint32_t base_hz, uint32_t poll_hz)
{
    if (poll_hz == 0u) {
        poll_hz = 50u;
    }
    uint32_t n = base_hz / (poll_hz * IMU_ODR_OVERSAMPLE);  /* floor: base / n >= target */
    return n > 0u ? n - 1u : 0u;
}

/** Resulting output rate in millihertz, for the log line and the tests. */
static inline uint32_t imu_odr_mhz(uint32_t base_hz, uint32_t div)
{
    return (uint32_t)(((uint64_t)base_hz * 1000u) / (div + 1u));
}

/** The gyro sample-rate divider register is only 8 bits wide. */
#define IMU_GYRO_DIV_MAX 255u

/**
 * Effective sensor ODR in whole Hz for a configured poll rate, honouring the
 * driver's maximum divider (8-bit for the gyro). The BLE readback and the host
 * gate use this to report the physical rate, not the poll rate.
 */
static inline uint32_t imu_effective_odr_hz(uint32_t poll_hz, uint32_t base_hz, uint32_t max_div)
{
    uint32_t div = imu_smplrt_div_for(base_hz, poll_hz);
    if (div > max_div) {
        div = max_div;
    }
    return imu_odr_mhz(base_hz, div) / 1000u;
}

#endif /* IMU_ODR_H */
