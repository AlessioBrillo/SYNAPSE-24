idf_component_register(SRCS "ble_gatt_server.c" "ble_research_service.c"
                       "../../common/ble/synapse_raw_frame.c"
                       INCLUDE_DIRS "." "../../common/ble"
                       REQUIRES bt nvs_flash provisioning config sensors esp_timer
                       PRIV_REQUIRES)
