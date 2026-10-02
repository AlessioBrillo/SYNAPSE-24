idf_component_register(SRCS "fit_writer.c"
                    INCLUDE_DIRS "."
                    REQUIRES esp_littlefs esp_partition esp_timer
                    PRIV_REQUIRES)