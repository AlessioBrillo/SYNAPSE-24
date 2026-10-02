idf_component_register(SRCS "synapse_sensors.c" "gps_max_m10s.c" "temp_tmp117.c"
                       INCLUDE_DIRS "." "../../common/scheduler" "../../common/sensors" "../../common/sync" "../../common/triage" "../../common/power" "../../common/ble"
                       REQUIRES driver freertos esp_timer nvs_flash
                       PRIV_REQUIRES)