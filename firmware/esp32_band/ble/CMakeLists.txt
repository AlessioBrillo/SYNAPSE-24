idf_component_register(SRCS "ble_gatt_server.c" "gatt_db.c"
                    INCLUDE_DIRS "."
                    REQUIRES esp_ble freesp_gatts freesp_gattc freesp_bt
                    PRIV_REQUIRES freesp_gatt_common
                    EMBED_FILES "gatt_db.csv")