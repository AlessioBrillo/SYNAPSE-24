idf_component_register(SRCS "synapse_acquisition.c"
                       INCLUDE_DIRS "." "../sensors" "../config" "../power" "../signal_quality" "../fit" "../../common/scheduler" "../../common/sensors"
                       REQUIRES freertos esp_timer
                       PRIV_REQUIRES)