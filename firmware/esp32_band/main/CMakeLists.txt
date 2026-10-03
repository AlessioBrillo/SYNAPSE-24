idf_component_register(SRCS "app_main.c"
                    INCLUDE_DIRS "."
                    REQUIRES ble fit provisioning ota acquisition sensors edge_ai signal_quality config power
                    PRIV_REQUIRES bt nvs_flash)