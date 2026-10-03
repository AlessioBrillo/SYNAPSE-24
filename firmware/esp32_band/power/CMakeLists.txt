idf_component_register(SRCS "synapse_power.c"
                       INCLUDE_DIRS "."
                       REQUIRES esp_adc config
                       PRIV_REQUIRES)
