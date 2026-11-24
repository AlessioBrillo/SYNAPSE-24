/**
 * @file synapse_identity.c
 * @brief NVS-backed device serial (see synapse_identity.h).
 */

#include "synapse_identity.h"

#include <ctype.h>
#include <inttypes.h>
#include <stdio.h>
#include <string.h>

#include "esp_log.h"
#include "nvs_flash.h"

static const char *TAG = "SYNAPSE_ID";

#define SERIAL_KEY "serial"
#define VERSION_KEY "id_version"
#define IDENTITY_VERSION 1u

static char s_serial[SYNAPSE_IDENTITY_SERIAL_MAX] = "SYN-000000";

static bool serial_valid(const char *value) {
    if (value == NULL) {
        return false;
    }
    size_t len = strlen(value);
    if (len != 16u || strncmp(value, "SYN-", 4) != 0) { /* SYN- + 12 hex */
        return false;
    }
    for (size_t i = 4; i < len; i++) {
        if (!isxdigit((unsigned char)value[i])) {
            return false;
        }
    }
    return true;
}

esp_err_t synapse_identity_init(const char *fallback) {
    nvs_handle_t handle;
    esp_err_t err = nvs_open(SYNAPSE_IDENTITY_NAMESPACE, NVS_READWRITE, &handle);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "cannot open identity NVS: %s", esp_err_to_name(err));
        return err;
    }

    uint32_t version = 0;
    err = nvs_get_u32(handle, VERSION_KEY, &version);
    if (err == ESP_OK && version != IDENTITY_VERSION) {
        ESP_LOGW(TAG, "identity version %" PRIu32 " -> %u: regenerating", version, IDENTITY_VERSION);
        nvs_erase_all(handle);
        err = ESP_ERR_NVS_NOT_FOUND;
    }

    if (err == ESP_OK) {
        size_t len = sizeof(s_serial);
        char stored[SYNAPSE_IDENTITY_SERIAL_MAX] = {0};
        if (nvs_get_str(handle, SERIAL_KEY, stored, &len) == ESP_OK && serial_valid(stored)) {
            memcpy(s_serial, stored, sizeof(s_serial));
            nvs_close(handle);
            ESP_LOGI(TAG, "serial %s (from NVS)", s_serial);
            return ESP_OK;
        }
        ESP_LOGW(TAG, "stored serial missing or malformed: regenerating");
    }

    if (fallback == NULL || !serial_valid(fallback)) {
        nvs_close(handle);
        ESP_LOGE(TAG, "no valid fallback serial was supplied");
        return ESP_ERR_INVALID_ARG;
    }

    memcpy(s_serial, fallback, sizeof(s_serial) - 1u);
    s_serial[sizeof(s_serial) - 1u] = '\0';
    err = nvs_set_str(handle, SERIAL_KEY, s_serial);
    if (err == ESP_OK) {
        err = nvs_set_u32(handle, VERSION_KEY, IDENTITY_VERSION);
    }
    if (err == ESP_OK) {
        err = nvs_commit(handle);
    }
    nvs_close(handle);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "cannot persist serial: %s", esp_err_to_name(err));
        return err;
    }
    ESP_LOGI(TAG, "serial %s (new)", s_serial);
    return ESP_OK;
}

const char *synapse_identity_serial(void) {
    return s_serial;
}
