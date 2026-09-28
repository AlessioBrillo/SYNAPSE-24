idf_component_register(SRCS "fit_writer.c"
                    INCLUDE_DIRS "."
                    REQUIRES littlefs esp_partition esp_flash
                    PRIV_REQUIRES cjson mbedtls)