idf_component_register(SRCS "synapse_signal_quality.c"
                       INCLUDE_DIRS "." "../../common/sensors" "../../common/scheduler"
                       REQUIRES freertos driver esp_timer sensors edge_ai
                       PRIV_REQUIRES)
