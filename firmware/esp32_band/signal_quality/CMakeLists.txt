idf_component_register(SRCS "synapse_signal_quality.c"
                       INCLUDE_DIRS "." "../sensors" "../../common/sensors" "../../common/scheduler"
                       REQUIRES freertos
                       PRIV_REQUIRES)