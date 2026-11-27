/**
 * @file cfg_json_helpers.h
 * @brief Host-test shim: the production parser lives in the firmware tree.
 *
 * Keep this indirection so tests/firmware_host compiles the exact same header
 * the band builds, without reaching into ESP-IDF or NimBLE headers.
 */
#ifndef SYNAPSE_CFG_JSON_HELPERS_SHIM_H
#define SYNAPSE_CFG_JSON_HELPERS_SHIM_H

#include "../../../firmware/esp32_band/config/synapse_config_json.h"

#endif /* SYNAPSE_CFG_JSON_HELPERS_SHIM_H */
