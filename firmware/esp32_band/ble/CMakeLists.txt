# The BLE component always requires the provisioning component: the
# provisioning API is a no-op in the band-core build (the flag guards the
# code, see ble_gatt_server.c), so the header is always available and the
# component graph can never disagree with the compiler.
#
# The flag decides the heavy dependency (the Wi-Fi stack) inside the
# provisioning component, not the presence of the component.
idf_component_register(SRCS "ble_gatt_server.c" "ble_research_service.c"
                       "../../common/ble/synapse_raw_frame.c"
                       INCLUDE_DIRS "." "../../common/ble"
                       REQUIRES bt nvs_flash provisioning config sensors esp_timer
                       PRIV_REQUIRES)
