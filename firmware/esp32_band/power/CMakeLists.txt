idf_component_register(SRCS "synapse_power.c"
                       INCLUDE_DIRS "." "../config"
                       REQUIRES esp_adc
                       PRIV_REQUIRES)