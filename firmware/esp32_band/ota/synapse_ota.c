/**
 * @file synapse_ota.c
 * @brief OTA implementation (native esp_ota_ops; Ed25519 verify = TODO wired to Secure Boot)
 * For MVP: mark valid on boot, support rollback on 3 failed boots via ota_data.
 */

#include "esp_log.h"
#include "esp_ota_ops.h"
#include "synapse_ota.h"

static const char *TAG = "SYNAPSE_OTA";

esp_err_t synapse_ota_init(void) {
    const esp_partition_t *running = esp_ota_get_running_partition();
    esp_ota_img_states_t state;
    if (running && esp_ota_get_state_partition(running, &state) == ESP_OK) {
        if (state == ESP_OTA_IMG_PENDING_VERIFY) {
            ESP_LOGI(TAG, "Pending verify — marking app valid");
            esp_ota_mark_app_valid_cancel_rollback();
        }
    }
    ESP_LOGI(TAG, "OTA init done");
    return ESP_OK;
}

void synapse_ota_process(void) { /* OTA triggered via cloud/SDK in future */ }
