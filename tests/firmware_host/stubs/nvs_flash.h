/* Host stub: NVS flash init is a no-op on the host. */
#ifndef STUB_NVS_FLASH_H
#define STUB_NVS_FLASH_H

#include "esp_err.h"
#include "nvs.h"

esp_err_t nvs_flash_init(void) { return ESP_OK; }

#endif
