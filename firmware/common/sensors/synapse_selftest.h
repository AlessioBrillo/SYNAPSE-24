/**
 * @file synapse_selftest.h
 * @brief The verdicts of the sensor self-tests and the factory report. Pure C, no ESP-IDF.
 *
 * The drivers read their sensor; what a reading must look like to be believed, and how the
 * results add up into PASS / FAIL, lives here so that it can be compiled and tested on the host
 * (tests/firmware_host/host_glue_selftest.c). Every limit is a physical sanity bound, not a
 * calibration: "is this a working part sitting on a bench", never "is it accurate".
 *
 * A self-test function in a driver has the shape `esp_err_t x_self_test(char *detail, size_t len)`:
 *   ESP_OK    the checks passed (`detail` says what was measured)
 *   ESP_FAIL  a check failed (`detail` says which, with the number)
 *   other     the bus failed (`detail` has the error name)
 */
#ifndef SYNAPSE_SELFTEST_H
#define SYNAPSE_SELFTEST_H

#include <math.h>
#include <stdarg.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>

/* --- plausibility of one reading --------------------------------------------------------------- */

/** A temperature a part can show on a bench or on skin (TMP117 range -55..150, MAX30102 die -40..85). */
static inline bool st_temp_plausible_c(float celsius)
{
    return celsius >= -20.0f && celsius <= 85.0f;
}

/** MAX30102 die temperature: DIE_TEMP_INT (signed, degrees) + DIE_TEMP_FRAC (4 bits, 0.0625 C). */
static inline float st_max30102_temp_c(int8_t integer, uint8_t frac)
{
    return (float)integer + (float)(frac & 0x0F) * 0.0625f;
}

/** The FIFO write pointer moved between two reads 100 ms apart: the part is sampling. */
static inline bool st_fifo_advanced(uint8_t wr_before, uint8_t wr_after)
{
    return (uint8_t)((wr_after - wr_before) & 0x1Fu) != 0u;
}

/** A LiPo rail through the INA219 reads 3.0-4.4 V; the part itself accepts 0-32 V (bus range). */
static inline bool st_bus_voltage_plausible(float volts)
{
    return volts >= 1.0f && volts <= 40.0f;
}

/** IMU lying still (the factory station): |a| ~ 1 g, every gyro axis near zero (rad/s). */
static inline bool st_imu_rest_ok(float ax_g, float ay_g, float az_g, float gx_rads, float gy_rads, float gz_rads)
{
    const float a = sqrtf(ax_g * ax_g + ay_g * ay_g + az_g * az_g);
    return a >= 0.85f && a <= 1.15f && fabsf(gx_rads) < 0.2f && fabsf(gy_rads) < 0.2f && fabsf(gz_rads) < 0.2f;
}

/** The Earth's field is 25-65 uT; leave room for a bench and a magnet-free steel table. */
static inline bool st_mag_ok(float mx_ut, float my_ut, float mz_ut)
{
    const float m = sqrtf(mx_ut * mx_ut + my_ut * my_ut + mz_ut * mz_ut);
    return m >= 10.0f && m <= 100.0f;
}

/** A stream delivered at least 80 % of what its rate promises over `seconds`. */
static inline bool st_count_ok(uint32_t count, uint32_t expected_hz, uint32_t seconds)
{
    return (uint64_t)count * 10u >= (uint64_t)expected_hz * seconds * 8u;
}

/* --- the report -------------------------------------------------------------------------------- */

#define ST_MAX_ITEMS 24
#define ST_NAME_LEN  24
#define ST_DETAIL_LEN 72

typedef struct {
    char name[ST_NAME_LEN];
    bool pass;
    char detail[ST_DETAIL_LEN];
} st_item_t;

typedef struct {
    st_item_t items[ST_MAX_ITEMS];
    int n;
    int dropped; /* items that did not fit: a report that lost lines can never be a PASS */
} st_report_t;

static inline void st_report_init(st_report_t *r)
{
    memset(r, 0, sizeof(*r));
}

/** Append one check. Returns `pass`, so callers can chain on it. */
static inline bool st_report_add(st_report_t *r, const char *name, bool pass, const char *fmt, ...)
{
    if (r->n >= ST_MAX_ITEMS) {
        r->dropped++;
        return pass;
    }
    st_item_t *it = &r->items[r->n++];
    snprintf(it->name, sizeof(it->name), "%s", name);
    it->pass = pass;
    va_list ap;
    va_start(ap, fmt);
    vsnprintf(it->detail, sizeof(it->detail), fmt, ap);
    va_end(ap);
    return pass;
}

static inline int st_report_failed(const st_report_t *r)
{
    int failed = r->dropped; /* a lost line counts as a failure */
    for (int i = 0; i < r->n; i++) {
        if (!r->items[i].pass) {
            failed++;
        }
    }
    return failed;
}

/** PASS needs at least one check and no failure: an empty report proves nothing. */
static inline bool st_report_pass(const st_report_t *r)
{
    return r->n > 0 && st_report_failed(r) == 0;
}

/** Record the outcome of a driver self-test (ESP_OK / ESP_FAIL / bus error) as one report item. */
static inline bool st_report_add_result(st_report_t *r, const char *name, int esp_ok_code, int result,
                                        const char *detail)
{
    return st_report_add(r, name, result == esp_ok_code, "%s", detail);
}

#endif /* SYNAPSE_SELFTEST_H */
