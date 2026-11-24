idf_component_register(SRCS "synapse_config.c" "synapse_identity.c" "synapse_calibration.c"
                       INCLUDE_DIRS "."
                       REQUIRES nvs_flash esp_timer
                       PRIV_REQUIRES)
