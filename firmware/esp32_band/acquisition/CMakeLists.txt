idf_component_register(SRCS "synapse_acquisition.c"
                       INCLUDE_DIRS "."
                       REQUIRES freertos esp_timer driver config sensors power signal_quality fit
                       PRIV_REQUIRES)
