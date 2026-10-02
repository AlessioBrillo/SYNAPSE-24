idf_component_register(SRCS "synapse_ota.c"
                    INCLUDE_DIRS "."
                    REQUIRES app_update esp_https_ota
                    PRIV_REQUIRES)
