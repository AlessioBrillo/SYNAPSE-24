idf_component_register(SRCS "synapse_power.c"
                       INCLUDE_DIRS "." "../config"
                       REQUIRES driver esp_adc_cal
                       PRIV_REQUIRES)