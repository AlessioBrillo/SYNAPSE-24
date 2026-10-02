idf_component_register(SRCS "fit_writer.c"
                    INCLUDE_DIRS "." "../ble"
                    REQUIRES spiffs esp_partition esp_timer ble
                    PRIV_REQUIRES)