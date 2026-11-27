/**
 * Compiled host test for the BLE device-config whitelist parsers.
 *
 * Built with -Wall -Wextra -Werror by test_firmware_config_parser_host.py.
 * Returns the number of failed checks (0 = pass) and prints each failure.
 */
#include <stdio.h>

#include "cfg_json_helpers.h"

static int s_failures = 0;

#define CHECK(cond, label)                                    \
    do {                                                      \
        if (!(cond)) {                                        \
            printf("FAIL %s\n", (label));                     \
            s_failures++;                                     \
        }                                                     \
    } while (0)

int cfg_json_host_run_all(void) {
    uint32_t u = 0;
    int16_t v[3] = {0, 0, 0};

    /* Negative values must never wrap into a plausible positive uint32.
     * On the ESP32 `unsigned long` is 32-bit, so strtoul() would turn
     * -4294966296 into 1000 without ERANGE: the sign check is the guard. */
    CHECK(!json_u32("{\"battery_mah\":-4294966296}", "battery_mah", &u),
          "negative wrapping value");
    CHECK(!json_u32("{\"battery_mah\":  -4294966296}", "battery_mah", &u),
          "negative wrapping value after whitespace");
    CHECK(!json_u32("{\"battery_mah\":-1}", "battery_mah", &u), "negative one");
    CHECK(!json_u32("{\"battery_mah\":+1000}", "battery_mah", &u), "leading plus");
    CHECK(!json_u32("{\"battery_mah\":1000abc}", "battery_mah", &u), "trailing junk after value");

    /* ERANGE (beyond unsigned long) and the 64-bit host-only uint32 guard. */
    CHECK(!json_u32("{\"battery_mah\":18446744073709551616}", "battery_mah", &u), "ERANGE overflow");
    CHECK(!json_u32("{\"battery_mah\":4294967296}", "battery_mah", &u), "uint32 overflow");
    CHECK(json_u32("{\"battery_mah\":1000}", "battery_mah", &u) && u == 1000u, "plain uint32");

    /* Whole-payload validation. */
    CHECK(json_only_whitelisted("{\"led_red\":1,\"led_red\":2}") == CFG_JSON_ERR_DUPLICATE_KEY,
          "duplicate key");
    CHECK(json_only_whitelisted("{\"cal_serial\":\"SYN-1\"}") == CFG_JSON_ERR_UNKNOWN_KEY,
          "unknown key");
    CHECK(json_only_whitelisted("{\"led_red\":\"led_ir\"}") == CFG_JSON_ERR_MALFORMED,
          "quoted token as value");
    CHECK(json_only_whitelisted("{\"accel_bias_mg\":[\"1\",0,0]}") == CFG_JSON_ERR_MALFORMED,
          "quoted token inside array");
    CHECK(json_only_whitelisted("{\"led_red\":1}junk") == CFG_JSON_ERR_MALFORMED,
          "trailing junk after object");
    CHECK(json_only_whitelisted("{\"led_red\":1}{\"battery_mah\":2}") == CFG_JSON_ERR_MALFORMED,
          "two objects");
    CHECK(json_only_whitelisted("{\"led_red\":true}") == CFG_JSON_ERR_MALFORMED,
          "bare word value");
    CHECK(json_only_whitelisted("{\"led_red\":1") == CFG_JSON_ERR_MALFORMED, "unbalanced object");
    CHECK(json_only_whitelisted("{\"led_red\":1}") == CFG_JSON_OK, "single valid object");

    /* Arrays require a leading '[', commas and a closing ']'. */
    CHECK(!json_i16_array("{\"accel_bias_mg\":[1 2 3]}", "accel_bias_mg", v, 3),
          "array missing commas");
    CHECK(!json_i16_array("{\"accel_bias_mg\":[1,2,3}", "accel_bias_mg", v, 3),
          "array missing bracket");
    CHECK(!json_i16_array("{\"accel_bias_mg\":1,2,3}", "accel_bias_mg", v, 3),
          "array missing opening bracket");
    CHECK(!json_i16_array("{\"accel_bias_mg\":[1,2,3]junk}", "accel_bias_mg", v, 3),
          "array trailing junk");
    CHECK(!json_i16_array("{\"accel_bias_mg\":[40000,0,0]}", "accel_bias_mg", v, 3),
          "array element out of range");

    /* The complete profile must parse and keep every value. */
    static const char full[] =
        "{\"led_red\":31,\"led_ir\":32,\"gain_x100\":110000,\"battery_mah\":1000,"
        "\"feature_rate_hz\":2,\"accel_bias_mg\":[-1,0,1],\"gyro_bias_mdps\":[0,-2,3]}";
    CHECK(json_only_whitelisted(full) == CFG_JSON_OK, "full profile object");
    CHECK(json_u32(full, "led_red", &u) && u == 31u, "full profile led_red");
    CHECK(json_u32(full, "led_ir", &u) && u == 32u, "full profile led_ir");
    CHECK(json_u32(full, "gain_x100", &u) && u == 110000u, "full profile gain_x100");
    CHECK(json_u32(full, "battery_mah", &u) && u == 1000u, "full profile battery_mah");
    CHECK(json_u32(full, "feature_rate_hz", &u) && u == 2u, "full profile feature_rate_hz");
    CHECK(json_i16_array(full, "accel_bias_mg", v, 3) && v[0] == -1 && v[1] == 0 && v[2] == 1,
          "full profile accel_bias_mg");
    CHECK(json_i16_array(full, "gyro_bias_mdps", v, 3) && v[0] == 0 && v[1] == -2 && v[2] == 3,
          "full profile gyro_bias_mdps");

    return s_failures;
}
