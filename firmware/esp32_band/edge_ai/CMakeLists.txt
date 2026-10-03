idf_component_register(SRCS "synapse_edge_ai.c"
                       "../../common/triage/triage_inference.cpp"
                       "../../common/triage/triage_features.cpp"
                       INCLUDE_DIRS "." "../sensors" "../../common/triage" "../../common/scheduler" "../../common/sensors"
                       REQUIRES freertos esp_timer
                       PRIV_REQUIRES)