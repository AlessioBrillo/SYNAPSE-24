idf_component_register(SRCS "synapse_edge_ai.c"
                       "../../common/triage/triage_inference.cpp"
                       "../../common/triage/triage_features.cpp"
                       "../../common/triage/triage_dsp.c"
                       INCLUDE_DIRS "." "../../common/triage" "../../common/scheduler" "../../common/sensors"
                       REQUIRES freertos esp_timer driver sensors
                       PRIV_REQUIRES)
