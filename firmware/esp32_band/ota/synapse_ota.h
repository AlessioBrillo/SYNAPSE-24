/**
 * @file synapse_ota.h
 * @brief Signed OTA updates with rollback
 */

#ifndef SYNAPSE_OTA_H
#define SYNAPSE_OTA_H

#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

esp_err_t synapse_ota_init(void);
void synapse_ota_process(void);

#ifdef __cplusplus
}
#endif

#endif
