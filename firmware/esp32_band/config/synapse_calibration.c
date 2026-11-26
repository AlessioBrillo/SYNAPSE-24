/**
 * @file synapse_calibration.c
 * @brief NVS-backed per-unit calibration (see synapse_calibration.h).
 */

#include "synapse_calibration.h"

#include <inttypes.h>
#include <string.h>

#include "esp_log.h"
#include "nvs_flash.h"
#include "synapse_identity.h"

static const char *TAG = "SYNAPSE_CAL";

#define CAL_NAMESPACE "synapse_cal"
#define CAL_KEY "record"
#define CAL_VERSION_KEY "cal_version"

static const char *s_state = "defaults";

/* The CRC-32 of the record is computed here rather than through esp_crc32 so
 * the same code can be compiled by the host tests that check the wire layout. */
static uint32_t crc32_bytes(const uint8_t *data, size_t len) {
    uint32_t crc = 0xFFFFFFFFu;
    for (size_t i = 0; i < len; i++) {
        crc ^= data[i];
        for (int bit = 0; bit < 8; bit++) {
            const uint32_t mask = (uint32_t)-(int32_t)(crc & 1u);
            crc = (crc >> 1) ^ (0xEDB88320u & mask);
        }
    }
    return ~crc;
}

static uint32_t record_crc(const synapse_calibration_t *cal) {
    synapse_calibration_t copy;
    memcpy(&copy, cal, sizeof(copy));
    copy.crc32 = 0u;
    return crc32_bytes((const uint8_t *)&copy, sizeof(copy));
}

void synapse_calibration_defaults(synapse_calibration_t *cal) {
    if (cal == NULL) {
        return;
    }
    memset(cal, 0, sizeof(*cal));
    cal->version = SYNAPSE_CALIBRATION_VERSION;
    synapse_calibration_seal(cal);
    s_state = "defaults";
}

void synapse_calibration_seal(synapse_calibration_t *cal) {
    if (cal == NULL) {
        return;
    }
    cal->version = SYNAPSE_CALIBRATION_VERSION;
    cal->crc32 = 0u;
    cal->crc32 = record_crc(cal);
}

bool synapse_calibration_valid(const synapse_calibration_t *cal) {
    return cal != NULL && cal->version == SYNAPSE_CALIBRATION_VERSION &&
           cal->crc32 == record_crc(cal);
}

esp_err_t synapse_calibration_load(synapse_calibration_t *out) {
    if (out == NULL) {
        return ESP_ERR_INVALID_ARG;
    }
    synapse_calibration_defaults(out);

    nvs_handle_t handle;
    esp_err_t err = nvs_open(CAL_NAMESPACE, NVS_READONLY, &handle);
    if (err != ESP_OK) {
        ESP_LOGW(TAG, "no calibration record (%s): using Kconfig defaults", esp_err_to_name(err));
        s_state = "defaults";
        return err;
    }

    uint32_t version = 0;
    err = nvs_get_u32(handle, CAL_VERSION_KEY, &version);
    if (err != ESP_OK || version != SYNAPSE_CALIBRATION_VERSION) {
        ESP_LOGW(TAG, "calibration version mismatch (%" PRIu32 "): using defaults", version);
        nvs_close(handle);
        s_state = "invalid";
        return ESP_ERR_INVALID_VERSION;
    }

    synapse_calibration_t stored;
    size_t len = sizeof(stored);
    err = nvs_get_blob(handle, CAL_KEY, &stored, &len);
    nvs_close(handle);
    if (err != ESP_OK || len != sizeof(stored) || !synapse_calibration_valid(&stored)) {
        ESP_LOGE(TAG, "calibration blob corrupt (%s, %u bytes): using defaults",
                 esp_err_to_name(err), (unsigned)len);
        s_state = "invalid";
        return ESP_ERR_INVALID_CRC;
    }

    /* A per-unit record is manufacturing data for ONE serial. A flash image
     * cloned from another band, or a swapped NVS blob, must not silently
     * rescale this band's ECG by someone else's gain. Fail closed. */
    const char *device_serial = synapse_identity_serial();
    if (device_serial != NULL && device_serial[0] != '\0' &&
        strncmp(stored.serial, device_serial, sizeof(stored.serial)) != 0) {
        ESP_LOGE(TAG, "calibration serial mismatch (record=%.20s device=%s): using defaults",
                 stored.serial, device_serial);
        s_state = "invalid";
        return ESP_ERR_INVALID_STATE;
    }

    memcpy(out, &stored, sizeof(stored));
    s_state = "nvs";
    ESP_LOGI(TAG, "calibration loaded for %s (gain=%" PRIu32 "x100)",
             out->serial, out->ecg_gain_x100);
    return ESP_OK;
}

esp_err_t synapse_calibration_save(const synapse_calibration_t *cal) {
    if (!synapse_calibration_valid(cal)) {
        return ESP_ERR_INVALID_CRC;
    }
    nvs_handle_t handle;
    esp_err_t err = nvs_open(CAL_NAMESPACE, NVS_READWRITE, &handle);
    if (err != ESP_OK) {
        return err;
    }
    err = nvs_set_blob(handle, CAL_KEY, cal, sizeof(*cal));
    if (err == ESP_OK) {
        err = nvs_set_u32(handle, CAL_VERSION_KEY, SYNAPSE_CALIBRATION_VERSION);
    }
    if (err == ESP_OK) {
        err = nvs_commit(handle);
    }
    nvs_close(handle);
    if (err == ESP_OK) {
        s_state = "nvs";
    }
    return err;
}

esp_err_t synapse_calibration_erase(void) {
    nvs_handle_t handle;
    esp_err_t err = nvs_open(CAL_NAMESPACE, NVS_READWRITE, &handle);
    if (err != ESP_OK) {
        return err;
    }
    err = nvs_erase_all(handle);
    if (err == ESP_OK) {
        err = nvs_commit(handle);
    }
    nvs_close(handle);
    if (err == ESP_OK) {
        s_state = "defaults";
    }
    return err;
}

const char *synapse_calibration_state(void) {
    return s_state;
}
