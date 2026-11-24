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
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "synapse_calibration.h"

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
        "  cal serial <SYN-XXXXXXXXXXXX>     printed on the box / QR\r\n"
        "  cal date <YYYY-MM-DD>             calibration date\r\n"
        "  cal operator <name>               who calibrated it\r\n"
        "  cal save                          seal and persist the record\r\n"
        "  cal erase                         drop the record, use Kconfig\r\n");
}

static void dump_record(void) {
    printf(
        "{\"serial\":\"%s\",\"source\":\"%s\",\"version\":%u,"
        "\"ecg_gain_x100\":%u,\"ecg_midrail_mv\":%d,"
        "\"ppg_led_red\":%u,\"ppg_led_ir\":%u,"
        "\"accel_bias_mg\":[%d,%d,%d],\"gyro_bias_mdps\":[%d,%d,%d],"
        "\"date\":\"%s\",\"operator\":\"%s\"}\r\n",
        s_cal.serial, synapse_calibration_state(), (unsigned)s_cal.version,
        (unsigned)s_cal.ecg_gain_x100, (int)s_cal.ecg_midrail_mv,
        (unsigned)s_cal.ppg_led_red, (unsigned)s_cal.ppg_led_ir,
        (int)s_cal.accel_bias_mg[0], (int)s_cal.accel_bias_mg[1], (int)s_cal.accel_bias_mg[2],
        (int)s_cal.gyro_bias_mdps[0], (int)s_cal.gyro_bias_mdps[1], (int)s_cal.gyro_bias_mdps[2],
        s_cal.date, s_cal.operator);
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

static void handle_cal(char *args) {
    char *cmd = strtok(args, " \t\r\n");
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

    int a = 0, b = 0, c = 0;
    char *p1 = strtok(NULL, " \t\r\n");
    char *p2 = NULL;
    char *p3 = NULL;
    if (strcmp(cmd, "gain") == 0 && p1 && parse_int(p1, false, &a) == 0 && a > 0) {
        s_cal.ecg_gain_x100 = (uint32_t)a;
    } else if (strcmp(cmd, "midrail") == 0 && p1 && parse_int(p1, false, &a) == 0) {
        s_cal.ecg_midrail_mv = a;
    } else if (strcmp(cmd, "led") == 0 && p1 && parse_int(p1, true, &a) == 0 &&
               (p2 = strtok(NULL, " \t\r\n")) != NULL && parse_int(p2, true, &b) == 0 &&
               a >= 0 && a <= 255 && b >= 0 && b <= 255) {
        s_cal.ppg_led_red = (uint8_t)a;
        s_cal.ppg_led_ir = (uint8_t)b;
    } else if (strcmp(cmd, "accel_bias") == 0 && p1 && parse_int(p1, false, &a) == 0 &&
               (p2 = strtok(NULL, " \t\r\n")) != NULL && parse_int(p2, false, &b) == 0 &&
               (p3 = strtok(NULL, " \t\r\n")) != NULL && parse_int(p3, false, &c) == 0) {
        s_cal.accel_bias_mg[0] = (int16_t)a;
        s_cal.accel_bias_mg[1] = (int16_t)b;
        s_cal.accel_bias_mg[2] = (int16_t)c;
    } else if (strcmp(cmd, "gyro_bias") == 0 && p1 && parse_int(p1, false, &a) == 0 &&
               (p2 = strtok(NULL, " \t\r\n")) != NULL && parse_int(p2, false, &b) == 0 &&
               (p3 = strtok(NULL, " \t\r\n")) != NULL && parse_int(p3, false, &c) == 0) {
        s_cal.gyro_bias_mdps[0] = (int16_t)a;
        s_cal.gyro_bias_mdps[1] = (int16_t)b;
        s_cal.gyro_bias_mdps[2] = (int16_t)c;
    } else if (strcmp(cmd, "serial") == 0 && p1 && strlen(p1) < sizeof(s_cal.serial) &&
               strncmp(p1, "SYN-", 4) == 0) {
        snprintf(s_cal.serial, sizeof(s_cal.serial), "%s", p1);
    } else if (strcmp(cmd, "date") == 0 && p1 && strlen(p1) < sizeof(s_cal.date)) {
        snprintf(s_cal.date, sizeof(s_cal.date), "%s", p1);
    } else if (strcmp(cmd, "operator") == 0 && p1 && strlen(p1) < sizeof(s_cal.operator)) {
        snprintf(s_cal.operator, sizeof(s_cal.operator), "%s", p1);
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
        ESP_LOGW(TAG, "no calibration record: editing a new one");
        synapse_calibration_defaults(&s_cal);
    }
    xTaskCreate(cli_task, "cal_cli", 4096, NULL, 2, NULL);
}
