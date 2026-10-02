idf_component_register(SRCS "ble_gatt_server.c"
                    INCLUDE_DIRS "." "../provisioning" "../config"
                    REQUIRES bt nvs_flash provisioning config
                    PRIV_REQUIRES)