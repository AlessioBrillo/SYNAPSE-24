idf_component_register(SRCS "synapse_sensors.c" "gps_max_m10s.c" "temp_tmp117.c"
                       "../../common/scheduler/sensor_scheduler.cpp"
                       "../../common/sensors/ecg_ad8232.cpp"
                       "../../common/sensors/ppg_max30102.cpp"
                       "../../common/sensors/ppg_sqi.cpp"
                       "../../common/sensors/ppg_processor.cpp"
                       "../../common/sensors/imu_icm20948.cpp"
                       "../../common/sensors/imu_processor.cpp"
                       INCLUDE_DIRS "." "../../common/scheduler" "../../common/sensors" "../../common/sync" "../../common/triage" "../../common/power" "../../common/ble"
                       REQUIRES driver freertos esp_timer nvs_flash
                       PRIV_REQUIRES)