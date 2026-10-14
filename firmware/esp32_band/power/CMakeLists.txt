idf_component_register(SRCS "synapse_power.c"
                       INCLUDE_DIRS "." "../sensors"
                       REQUIRES esp_adc config sensors
                       PRIV_REQUIRES)
