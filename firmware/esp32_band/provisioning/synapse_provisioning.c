/**
 * @file synapse_provisioning.c
 * @brief Provisioning implementation (NVS-backed, WiFi STA)
 */

#include <string.h>
#include "esp_log.h"
#include "esp_wifi.h"
#include "esp_event.h"
#include "nvs.h"
#include "freertos/event_groups.h"
#include "synapse_provisioning.h"

static const char *TAG = "SYNAPSE_PROV";
#define PROV_NS "synapse_prov"
#define KEY_DONE "prov_done"

static EventGroupHandle_t s_events = NULL;
static bool s_complete = false;

esp_err_t synapse_provisioning_init(EventGroupHandle_t system_events) {
    s_events = system_events;
    nvs_handle_t h;
    if (nvs_open(PROV_NS, NVS_READONLY, &h) == ESP_OK) {
        uint8_t done = 0;
        if (nvs_get_u8(h, KEY_DONE, &done) == ESP_OK && done) s_complete = true;
        nvs_close(h);
    }
    ESP_LOGI(TAG, "Provisioning init (complete=%d)", s_complete);
    return ESP_OK;
}

bool synapse_provisioning_is_complete(void) { return s_complete; }

esp_err_t synapse_provisioning_connect_wifi(void) {
    // MVP: WiFi connect deferred to future (requires creds via provisioning write).
    // Return OK to allow normal operation without WiFi (BLE-only MVP).
    ESP_LOGI(TAG, "WiFi connect deferred (BLE-only MVP)");
    return ESP_OK;
}

void synapse_provisioning_process(void) { /* no background work for MVP */ }

esp_err_t synapse_provisioning_process_cmd(uint8_t cmd, const uint8_t *data, uint16_t len) {
    ESP_LOGI(TAG, "Prov cmd=%d len=%d", cmd, len);
    if (cmd == 6) { // COMMIT
        nvs_handle_t h;
        if (nvs_open(PROV_NS, NVS_READWRITE, &h) == ESP_OK) {
            nvs_set_u8(h, KEY_DONE, 1);
            nvs_commit(h);
            nvs_close(h);
        }
        s_complete = true;
    } else if (cmd == 7) { // FACTORY RESET
        synapse_provisioning_factory_reset();
    }
    (void)data;
    return ESP_OK;
}

esp_err_t synapse_provisioning_factory_reset(void) {
    nvs_handle_t h;
    if (nvs_open(PROV_NS, NVS_READWRITE, &h) == ESP_OK) {
        nvs_erase_all(h);
        nvs_commit(h);
        nvs_close(h);
    }
    s_complete = false;
    ESP_LOGW(TAG, "Factory reset done");
    return ESP_OK;
}
