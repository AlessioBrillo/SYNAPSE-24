idf_component_register(SRCS "app_main.c"
                    INCLUDE_DIRS "."
                    REQUIRES ble fit provisioning ota acquisition sensors edge_ai signal_quality config power
                    PRIV_REQUIRES esp_ble freesp_gatts freesp_gattc freesp_bt freesp_blufi freesp_hidd freesp_spp freesp_gatt_common
                    EMBED_FILES "gatt_db.csv")