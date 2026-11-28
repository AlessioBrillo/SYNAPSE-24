/**
 * @file synapse_factory_cli.c
 * @brief Minimal USB-serial shell that writes the NVS calibration record.
 *
 * It is gated by CONFIG_SYNAPSE_FACTORY_MODE: a production image has no
 * calibration command at all. The host counterpart is scripts/factory_calibrate.py.
 */

#include "synapse_factory_cli.h"

#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "esp_log.h"
#include "esp_system.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "synapse_calibration.h"
#include "synapse_identity.h"

static const char *TAG = "SYNAPSE_CAL_CLI";
static synapse_calibration_t s_cal;

static void print_help(void) {
    printf(
        "SYNAPSE factory calibration (USB serial)\r\n"
        "  help                              this text\r\n"
        "  cal dump                          print the current record as JSON\r\n"
        "  cal gain <x100>                   measured ECG gain, e.g. 110000\r\n"
        "  cal midrail <mv>                  measured DC bias (metadata only)\r\n"
        "  cal led <red> <ir>                MAX30102 LED codes (0x00-0xFF)\r\n"
        "  cal accel_bias <mg> <mg> <mg>     static accel bias\r\n"
        "  cal gyro_bias <mdps> <mdps> <mdps> static gyro bias\r\n"
        "  date and serial come from the device identity and the host script\r\n"
        "  cal date <YYYY-MM-DD>             calibration date\r\n"
        "  cal operator <name>               who calibrated it\r\n"
        "  cal instrument <id>               serial of the generator / meter\r\n"
        "  cal fw <version>                  firmware used for the measurement\r\n"
        "  text fields take a quoted value: cal operator \"A. Brillo\"\r\n"
        "  cal uncertainty <pct_x100>        gain uncertainty, e.g. 500 = 5 %%\r\n"
        "  cal noise <uv_x10>                shorted-input ECG noise, 0 = not measured\r\n"
        "  cal battery <mah>                 fitted cell capacity (0 = Kconfig default)\r\n"
        "  cal save                          seal and persist the record\r\n"
        "  cal erase                         drop the record, use Kconfig\r\n"
        "  cal reboot                        restart the band (apply a new profile)\r\n");
}

static void dump_record(void) {
    printf(
        "{\"serial\":\"%s\",\"source\":\"%s\",\"version\":%u,"
        "\"ecg_gain_x100\":%u,\"ecg_midrail_mv\":%d,"
        "\"ppg_led_red\":%u,\"ppg_led_ir\":%u,"
        "\"accel_bias_mg\":[%d,%d,%d],\"gyro_bias_mdps\":[%d,%d,%d],"
        "\"date\":\"%s\",\"operator\":\"%s\","
        "\"instrument\":\"%s\",\"fw_version\":\"%s\","
        "\"gain_uncertainty_pct_x100\":%u,\"noise_std_uv_x10\":%d,"
        "\"battery_capacity_mah\":%u}\r\n",
        s_cal.serial, synapse_calibration_state(), (unsigned)s_cal.version,
        (unsigned)s_cal.ecg_gain_x100, (int)s_cal.ecg_midrail_mv,
        (unsigned)s_cal.ppg_led_red, (unsigned)s_cal.ppg_led_ir,
        (int)s_cal.accel_bias_mg[0], (int)s_cal.accel_bias_mg[1], (int)s_cal.accel_bias_mg[2],
        (int)s_cal.gyro_bias_mdps[0], (int)s_cal.gyro_bias_mdps[1], (int)s_cal.gyro_bias_mdps[2],
        s_cal.date, s_cal.operator, s_cal.instrument, s_cal.fw_version,
        (unsigned)s_cal.gain_uncertainty_pct_x100, (int)s_cal.noise_std_uv_x10,
        (unsigned)s_cal.battery_capacity_mah);
}

static int parse_int(const char *text, bool hex, int *out) {
    char *end = NULL;
    long value = strtol(text, &end, hex ? 16 : 10);
    if (end == text || (end != NULL && *end != '\0')) {
        return -1;
    }
    *out = (int)value;
    return 0;
}

static bool is_cal_space(char ch) {
    return ch == ' ' || ch == '\t' || ch == '\r' || ch == '\n';
}

/** Split off one whitespace-delimited token, NUL-terminating it in place.
 *  Quotes are ordinary characters here: only the text fields interpret them. */
static char *next_arg(char **cursor) {
    char *p = *cursor;
    while (is_cal_space(*p)) {
        p++;
    }
    if (*p == '\0') {
        *cursor = p;
        return NULL;
    }
    char *start = p;
    while (*p != '\0' && !is_cal_space(*p)) {
        p++;
    }
    if (*p != '\0') {
        *p = '\0';
        p++;
    }
    *cursor = p;
    return start;
}

/** Extract a single text-field value from the rest of a `cal` line.
 *
 *  A double-quoted value is copied verbatim, spaces included, so provenance
 *  like `cal operator "A. Brillo"` is stored whole. An unquoted value keeps
 *  the legacy first-token behaviour. Returns the value length on success;
 *  negative values mean: -1 no/empty value, -2 unterminated quote,
 *  -3 characters after the closing quote, -4 longer than out_size,
 *  -5 a stray quote in an unquoted value (it would corrupt the JSON dump).
 *
 *  External linkage on purpose: the host-C gate compiles this exact function
 *  (not a Python reimplementation) to prove the quoted round trip.
 */
int synapse_factory_parse_text_arg(char *tail, char *out, size_t out_size);
int synapse_factory_parse_text_arg(char *tail, char *out, size_t out_size) {
    while (is_cal_space(*tail)) {
        tail++;
    }
    if (*tail == '\0') {
        return -1;
    }
    const char *start;
    if (*tail == '"') {
        start = ++tail;
        const char *end = strchr(tail, '"');
        if (end == NULL) {
            return -2;
        }
        const char *after = end + 1;
        while (is_cal_space(*after)) {
            after++;
        }
        if (*after != '\0') {
            return -3;
        }
        tail = (char *)end;  /* the closing quote itself is not part of the value */
    } else {
        start = tail;
        while (*tail != '\0' && !is_cal_space(*tail)) {
            tail++;
        }
        if (memchr(start, '"', (size_t)(tail - start)) != NULL) {
            return -5;
        }
    }
    const size_t len = (size_t)(tail - start);
    if (len == 0 || len >= out_size) {
        return -4;
    }
    memcpy(out, start, len);
    out[len] = '\0';
    return (int)len;
}

static void handle_cal(char *args) {
    char *cursor = args;
    char *cmd = next_arg(&cursor);
    if (cmd == NULL) {
        print_help();
        return;
    }
    if (strcmp(cmd, "dump") == 0) {
        dump_record();
        return;
    }
    if (strcmp(cmd, "erase") == 0) {
        esp_err_t err = synapse_calibration_erase();
        if (err != ESP_OK) {
            printf("ERR erase: %s\r\n", esp_err_to_name(err));
            return;
        }
        synapse_calibration_defaults(&s_cal);
        printf("OK erased\r\n");
        return;
    }
    if (strcmp(cmd, "save") == 0) {
        synapse_calibration_seal(&s_cal);
        esp_err_t err = synapse_calibration_save(&s_cal);
        if (err == ESP_OK) {
            printf("OK saved\r\n");
        } else {
            printf("ERR save: %s\r\n", esp_err_to_name(err));
        }
        return;
    }
    if (strcmp(cmd, "reboot") == 0) {
        /* The sensor layer reads the per-unit record only at boot. A bench tool
         * that writes LEDs and then records must be able to apply them without
         * asking the operator to unplug the cable. */
        printf("OK reboot\r\n");
        esp_restart();
        return;
    }

    /* Provenance fields carry human text, so they get the quote-aware parser.
     * Everything numeric keeps the original whitespace-only tokenisation. */
    if (strcmp(cmd, "operator") == 0 || strcmp(cmd, "instrument") == 0 ||
        strcmp(cmd, "fw") == 0 || strcmp(cmd, "date") == 0) {
        char value[SYNAPSE_CAL_INSTRUMENT_MAX];
        const int len = synapse_factory_parse_text_arg(cursor, value, sizeof(value));
        if (len < 0) {
            const char *why = len == -2   ? "unterminated quote"
                              : len == -3 ? "characters after closing quote"
                              : len == -4 ? "value too long"
                              : len == -5 ? "quote in unquoted value"
                                          : "missing value";
            printf("ERR bad %s value: %s\r\n", cmd, why);
            return;
        }
        char *field;
        size_t cap;
        if (strcmp(cmd, "operator") == 0) {
            field = s_cal.operator;
            cap = sizeof(s_cal.operator);
        } else if (strcmp(cmd, "instrument") == 0) {
            field = s_cal.instrument;
            cap = sizeof(s_cal.instrument);
        } else if (strcmp(cmd, "fw") == 0) {
            field = s_cal.fw_version;
            cap = sizeof(s_cal.fw_version);
        } else {
            field = s_cal.date;
            cap = sizeof(s_cal.date);
        }
        if ((size_t)len >= cap) {
            printf("ERR bad %s value: value too long\r\n", cmd);
            return;
        }
        snprintf(field, cap, "%s", value);
        printf("OK %s\r\n", cmd);
        return;
    }

    int a = 0, b = 0, c = 0;
    char *p1 = next_arg(&cursor);
    char *p2 = NULL;
    char *p3 = NULL;
    if (strcmp(cmd, "gain") == 0 && p1 && parse_int(p1, false, &a) == 0 &&
        a > 0 && a <= 1000000) {
        s_cal.ecg_gain_x100 = (uint32_t)a;
    } else if (strcmp(cmd, "midrail") == 0 && p1 && parse_int(p1, false, &a) == 0) {
        s_cal.ecg_midrail_mv = a;
    } else if (strcmp(cmd, "led") == 0 && p1 && parse_int(p1, true, &a) == 0 &&
               (p2 = next_arg(&cursor)) != NULL && parse_int(p2, true, &b) == 0 &&
               a >= 1 && a <= 255 && b >= 1 && b <= 255) {
        s_cal.ppg_led_red = (uint8_t)a;
        s_cal.ppg_led_ir = (uint8_t)b;
    } else if (strcmp(cmd, "accel_bias") == 0 && p1 && parse_int(p1, false, &a) == 0 &&
               (p2 = next_arg(&cursor)) != NULL && parse_int(p2, false, &b) == 0 &&
               (p3 = next_arg(&cursor)) != NULL && parse_int(p3, false, &c) == 0 && a >= -2000 && a <= 2000 && b >= -2000 && b <= 2000 && c >= -2000 && c <= 2000) {
        s_cal.accel_bias_mg[0] = (int16_t)a;
        s_cal.accel_bias_mg[1] = (int16_t)b;
        s_cal.accel_bias_mg[2] = (int16_t)c;
    } else if (strcmp(cmd, "gyro_bias") == 0 && p1 && parse_int(p1, false, &a) == 0 &&
               (p2 = next_arg(&cursor)) != NULL && parse_int(p2, false, &b) == 0 &&
               (p3 = next_arg(&cursor)) != NULL && parse_int(p3, false, &c) == 0 && a >= -20000 && a <= 20000 && b >= -20000 && b <= 20000 && c >= -20000 && c <= 20000) {
        s_cal.gyro_bias_mdps[0] = (int16_t)a;
        s_cal.gyro_bias_mdps[1] = (int16_t)b;
        s_cal.gyro_bias_mdps[2] = (int16_t)c;
    } else if (strcmp(cmd, "uncertainty") == 0 && p1 &&
               parse_int(p1, false, &a) == 0 && a >= 0 && a <= 10000) {
        s_cal.gain_uncertainty_pct_x100 = (uint16_t)a;
    } else if (strcmp(cmd, "noise") == 0 && p1 &&
               parse_int(p1, false, &a) == 0 && a >= 0 && a <= 32767) {
        s_cal.noise_std_uv_x10 = (int16_t)a;
    } else if (strcmp(cmd, "battery") == 0 && p1 &&
               parse_int(p1, false, &a) == 0 && a >= 100 && a <= 10000) {
        s_cal.battery_capacity_mah = (uint16_t)a;
    } else {
        printf("ERR bad command or arguments: %s\r\n", cmd);
        return;
    }
    printf("OK %s\r\n", cmd);
}

static void cli_task(void *arg) {
    (void)arg;
    char line[160];
    printf("SYNAPSE calibration shell ready (type 'help')\r\n");
    while (fgets(line, sizeof(line), stdin) != NULL) {
        char *p = line;
        while (*p == ' ' || *p == '\t') {
            p++;
        }
        if (*p == '\0' || *p == '\r' || *p == '\n') {
            continue;
        }
        char *first = p;
        while (*p != '\0' && *p != ' ' && *p != '\t' && *p != '\r' && *p != '\n') {
            p++;
        }
        const bool word_done = (*p != '\0');
        if (word_done) {
            *p = '\0';
            p++;  /* the rest of the line belongs to the subcommand */
        }
        if (strcmp(first, "help") == 0) {
            print_help();
        } else if (strcmp(first, "cal") == 0) {
            handle_cal(word_done ? p : (char *)"");
        } else {
            printf("ERR unknown command: %s\r\n", first);
        }
    }
    vTaskDelete(NULL);
}

void synapse_factory_cli_start(void) {
    synapse_calibration_defaults(&s_cal);
    if (synapse_calibration_load(&s_cal) != ESP_OK) {
        ESP_LOGW(TAG, "no calibration record (or it belongs to another band): editing a new one");
        synapse_calibration_defaults(&s_cal);
    }
    // The serial is never typed at the factory shell: it is the identity of
    // this exact band, derived from its MAC and stored in NVS at boot.
    const char *serial = synapse_identity_serial();
    if (serial != NULL && serial[0] != '\0') {
        snprintf(s_cal.serial, sizeof(s_cal.serial), "%s", serial);
    }
    xTaskCreate(cli_task, "cal_cli", 4096, NULL, 2, NULL);
}
