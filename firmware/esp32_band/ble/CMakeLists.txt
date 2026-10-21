# The provisioning component (and the Wi-Fi stack it pulls in) is part of the
# product build only: the band-core variant (sdkconfig.defaults.core) has no
# provisioning, and the BLE code guards its calls with the same CONFIG_
# symbol (see app_main.c for the same mechanism).
set(SYNAPSE_BLE_REQUIRES bt nvs_flash config sensors esp_timer)
if(CONFIG_SYNAPSE_FEATURE_PROVISIONING)
    list(APPEND SYNAPSE_BLE_REQUIRES provisioning)
endif()

idf_component_register(SRCS "ble_gatt_server.c" "ble_research_service.c"
                       "../../common/ble/synapse_raw_frame.c"
                       INCLUDE_DIRS "." "../../common/ble"
                       REQUIRES ${SYNAPSE_BLE_REQUIRES}
                       PRIV_REQUIRES)
