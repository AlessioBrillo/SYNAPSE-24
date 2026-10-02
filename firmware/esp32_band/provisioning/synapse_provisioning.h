/**
 * @file synapse_provisioning.h
 * @brief BLE Provisioning: WiFi + Cloud URL + mTLS cert (CSR flow)
 */

#ifndef SYNAPSE_PROVISIONING_H
#define SYNAPSE_PROVISIONING_H

#include <stdint.h>
#include <stdbool.h>
#include "esp_err.h"
#include "freertos/FreeRTOS.h"
#include "freertos/event_groups.h"

#ifdef __cplusplus
extern "C" {
#endif

esp_err_t synapse_provisioning_init(EventGroupHandle_t system_events);
bool synapse_provisioning_is_complete(void);
esp_err_t synapse_provisioning_connect_wifi(void);
void synapse_provisioning_process(void);
esp_err_t synapse_provisioning_process_cmd(uint8_t cmd, const uint8_t *data, uint16_t len);
esp_err_t synapse_provisioning_factory_reset(void);

#ifdef __cplusplus
}
#endif

#endif
