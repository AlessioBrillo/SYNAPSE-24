/**
 * Standalone host test for the sensor-effective calibration CRC.
 *
 * Compiled with -Wall -Wextra -Werror together with synapse_calibration.c and
 * the in-memory NVS glue. Returns the number of failed checks.
 */
#include <stdio.h>
#include <string.h>

#include "synapse_calibration.h"
#include "synapse_identity.h"

void host_set_identity(const char *serial);

static int s_failures = 0;

#define CHECK(cond, label)                                \
    do {                                                  \
        if (!(cond)) {                                    \
            printf("FAIL %s\n", (label));                 \
            s_failures++;                                 \
        }                                                 \
    } while (0)

int calibration_effective_host_run_all(void) {
    host_set_identity("SYN-A1B2C3D4E5F6");

    synapse_calibration_t base;
    synapse_calibration_defaults(&base);
    strcpy(base.serial, "SYN-A1B2C3D4E5F6");
    strcpy(base.date, "2026-11-27");
    strcpy(base.operator, "tester");
    strcpy(base.instrument, "METER-0001");
    strcpy(base.fw_version, "1.0.0");
    base.gain_uncertainty_pct_x100 = 500;
    base.noise_std_uv_x10 = 42;
    base.ecg_gain_x100 = 110000;
    base.ppg_led_red = 31;
    base.ppg_led_ir = 32;
    base.battery_capacity_mah = 1000;
    base.accel_bias_mg[0] = 1;
    base.gyro_bias_mdps[0] = -2;
    synapse_calibration_seal(&base);
    CHECK(synapse_calibration_valid(&base), "base record valid");

    const uint32_t full = synapse_calibration_crc(&base);
    const uint32_t effective = synapse_calibration_effective_crc(&base);
    CHECK(full != effective, "full and effective CRCs differ");

    /* A USB `cal save` that only adds/changes provenance metadata. */
    synapse_calibration_t edited;
    memcpy(&edited, &base, sizeof(edited));
    strcpy(edited.serial, "SYN-METADATA0000000");
    strcpy(edited.date, "2026-11-28");
    strcpy(edited.operator, "another");
    strcpy(edited.instrument, "METER-0002");
    strcpy(edited.fw_version, "1.2.3");
    edited.gain_uncertainty_pct_x100 = 700;
    edited.noise_std_uv_x10 = 55;
    edited.ecg_midrail_mv = -1234;  /* metadata only: the driver re-measures it */
    synapse_calibration_seal(&edited);
    CHECK(synapse_calibration_valid(&edited), "metadata-only record still valid");
    CHECK(synapse_calibration_crc(&edited) != full, "full CRC follows metadata");
    CHECK(synapse_calibration_effective_crc(&edited) == effective,
          "effective CRC ignores metadata");

    /* A real sensor-effective edit must still be detected. */
    synapse_calibration_t applied;
    memcpy(&applied, &base, sizeof(applied));
    applied.ecg_gain_x100 = 112300;
    synapse_calibration_seal(&applied);
    CHECK(synapse_calibration_effective_crc(&applied) != effective,
          "effective CRC follows the gain");

    /* mark_applied stores the effective CRC, not the full-record CRC. */
    synapse_calibration_mark_applied(&base);
    CHECK(synapse_calibration_applied_crc() == effective, "mark_applied stores effective CRC");
    CHECK(synapse_calibration_applied_crc() != full, "mark_applied is not the full CRC");
    synapse_calibration_mark_applied(&edited);
    CHECK(synapse_calibration_applied_crc() == effective,
          "a metadata-only save is not a pending reboot");

    return s_failures;
}
