idf_component_register(SRCS "synapse_edge_ai.c"
                       INCLUDE_DIRS "." "../sensors" "../../common/triage" "../../common/scheduler" "../../common/sensors"
                       REQUIRES freertos
                       PRIV_REQUIRES)