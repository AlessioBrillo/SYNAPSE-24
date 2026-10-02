/**
 * @file ble_gatt_server_priv.h
 * @brief Internal BLE GATT definitions
 */

#ifndef BLE_GATT_SERVER_PRIV_H
#define BLE_GATT_SERVER_PRIV_H

#include "esp_gatts_api.h"
#include "esp_gap_ble_api.h"

#define BLE_TAG "SYNAPSE_BLE"
#define PROFILE_APP_ID 0

// Service indices
enum {
    IDX_SVC_HR = 0,
    IDX_CHAR_HR_MEAS,
    IDX_CHAR_HR_MEAS_CCCD,
    IDX_CHAR_BODY_LOC,
    HR_IDX_NB,

    IDX_SVC_BATT = 0,
    IDX_CHAR_BATT_LVL,
    IDX_CHAR_BATT_CCCD,
    BATT_IDX_NB,
};

extern esp_ble_adv_params_t adv_params_normal;
extern esp_ble_adv_params_t adv_params_provisioning;

#endif
