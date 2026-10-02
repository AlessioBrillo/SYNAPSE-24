idf_component_register(SRCS "ble_gatt_server.c"
                    INCLUDE_DIRS "."
                    REQUIRES bt nvs_flash
                    PRIV_REQUIRES)