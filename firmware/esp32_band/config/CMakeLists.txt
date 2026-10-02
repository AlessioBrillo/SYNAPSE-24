idf_component_register(SRCS "synapse_config.c"
                       INCLUDE_DIRS "."
                       REQUIRES nvs_flash esp_timer driver
                       PRIV_REQUIRES)