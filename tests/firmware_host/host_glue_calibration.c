/* In-memory NVS + identity for the calibration host gate. */
#include <stdbool.h>
#include <stdint.h>
#include <string.h>

#include "esp_err.h"
#include "freertos/task.h"
#include "nvs.h"
#include "synapse_calibration.h"
#include "synapse_identity.h"

static char s_serial[SYNAPSE_IDENTITY_SERIAL_MAX] = {0};
static uint32_t s_version = 0;
static synapse_calibration_t s_record;
static bool s_has_record = false;

const char *esp_err_to_name(esp_err_t e) {
    (void)e;
    return "host-stub";
}

const char *synapse_identity_serial(void) {
    return s_serial;
}

void host_set_identity(const char *serial) {
    s_serial[0] = '\0';
    if (serial != NULL) {
        strncpy(s_serial, serial, sizeof(s_serial) - 1);
    }
}

esp_err_t nvs_open(const char *name, int mode, nvs_handle_t *out) {
    (void)name; (void)mode;
    *out = 1;
    return ESP_OK;
}

esp_err_t nvs_get_u32(nvs_handle_t handle, const char *key, uint32_t *out) {
    (void)handle;
    if (strcmp(key, "cal_version") == 0 && s_version != 0) {
        *out = s_version;
        return ESP_OK;
    }
    return ESP_ERR_NOT_FOUND;
}

esp_err_t nvs_set_u32(nvs_handle_t handle, const char *key, uint32_t value) {
    (void)handle;
    if (strcmp(key, "cal_version") == 0) s_version = value;
    return ESP_OK;
}

esp_err_t nvs_get_blob(nvs_handle_t handle, const char *key, void *value, size_t *length) {
    (void)handle;
    if (strcmp(key, "record") != 0 || !s_has_record || *length < sizeof(s_record)) {
        return ESP_ERR_NOT_FOUND;
    }
    memcpy(value, &s_record, sizeof(s_record));
    *length = sizeof(s_record);
    return ESP_OK;
}

esp_err_t nvs_set_blob(nvs_handle_t handle, const char *key, const void *value, size_t length) {
    (void)handle;
    if (strcmp(key, "record") != 0 || length != sizeof(s_record)) return ESP_ERR_INVALID_SIZE;
    memcpy(&s_record, value, sizeof(s_record));
    s_has_record = true;
    return ESP_OK;
}

esp_err_t nvs_erase_all(nvs_handle_t handle) {
    (void)handle;
    s_has_record = false;
    s_version = 0;
    return ESP_OK;
}

esp_err_t nvs_commit(nvs_handle_t handle) {
    (void)handle;
    return ESP_OK;
}

void nvs_close(nvs_handle_t handle) { (void)handle; }

int xTaskCreate(TaskFunction_t fn, const char *name, unsigned stack, void *arg,
                int priority, TaskHandle_t *handle) {
    (void)fn; (void)name; (void)stack; (void)arg; (void)priority;
    if (handle != NULL) *handle = NULL;
    return 1;
}

void vTaskDelete(TaskHandle_t handle) { (void)handle; }

/* Host-only helpers used by the Python test. */
void host_store_record(const synapse_calibration_t *record) {
    memcpy(&s_record, record, sizeof(s_record));
    s_version = record->version;
    s_has_record = true;
}

bool host_has_record(void) { return s_has_record; }
