idf_component_register(SRCS "synapse_provisioning.c"
                    INCLUDE_DIRS "."
                    REQUIRES nvs_flash esp_wifi esp_event
                    PRIV_REQUIRES)
