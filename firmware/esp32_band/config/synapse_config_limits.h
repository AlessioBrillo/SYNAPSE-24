/**
 * @file synapse_config_limits.h
 * @brief What a hardware configuration value is allowed to be. Pure C, no ESP-IDF.
 *
 * The rules synapse_config.c checks, and that the drivers rely on, kept in one place so that they
 * can be compiled and tested on the host (tests/firmware_host/host_glue_config_limits.c). Each
 * function answers "can the part do this?", never "is this a good idea?".
 *
 * GPIO rule (ESP32-S3-DevKitC-1 N8R8, the board of the kit): GPIO 22-25 do not exist, 26-32 are the
 * SPI flash and 33-37 the octal PSRAM. A pin there does not fail loudly, it just never works.
 */
#ifndef SYNAPSE_CONFIG_LIMITS_H
#define SYNAPSE_CONFIG_LIMITS_H

#include <stdbool.h>
#include <stdint.h>

/** A GPIO the N8R8 module exposes. `optional`: -1 ("not wired") is accepted too. */
static inline bool synapse_cfg_gpio_ok(int pin, bool optional)
{
    if (pin == -1) {
        return optional;
    }
    return (pin >= 0 && pin <= 21) || (pin >= 38 && pin <= 48);
}

/** A 7-bit I2C address a device can have (0x00-0x07 and 0x78-0x7F are reserved by the standard). */
static inline bool synapse_cfg_i2c_addr_ok(int addr)
{
    return addr >= 0x08 && addr <= 0x77;
}

/** The shared bus: two different usable pins and a clock the sensors on it support (10 kHz - 1 MHz). */
static inline bool synapse_cfg_i2c_bus_ok(int sda, int scl, uint32_t freq_hz)
{
    return synapse_cfg_gpio_ok(sda, false) && synapse_cfg_gpio_ok(scl, false) && sda != scl &&
           freq_hz >= 10000u && freq_hz <= 1000000u;
}

/** MAX30102 SPO2_CONFIG fields: sample-rate code 0-7, pulse-width code 0-3, ADC-range code 0-3. */
static inline bool synapse_cfg_ppg_codes_ok(unsigned sample_rate, unsigned pulse_width, unsigned adc_range)
{
    return sample_rate <= 7u && pulse_width <= 3u && adc_range <= 3u;
}

/** ICM-20948 full-scale ranges and output rates (base rates 1125 Hz accel, 1100 Hz gyro, divider >= 1). */
static inline bool synapse_cfg_imu_ok(int accel_fsr_g, int gyro_fsr_dps, int accel_odr_hz, int gyro_odr_hz)
{
    const bool a = accel_fsr_g == 2 || accel_fsr_g == 4 || accel_fsr_g == 8 || accel_fsr_g == 16;
    const bool g = gyro_fsr_dps == 250 || gyro_fsr_dps == 500 || gyro_fsr_dps == 1000 || gyro_fsr_dps == 2000;
    return a && g && accel_odr_hz >= 1 && accel_odr_hz <= 1125 && gyro_odr_hz >= 1 && gyro_odr_hz <= 1100;
}

#endif /* SYNAPSE_CONFIG_LIMITS_H */
