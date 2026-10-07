idf_component_register(SRCS "synapse_sensors.c" "gps_max_m10s.c" "temp_tmp117.c"
                       "../../common/scheduler/sensor_scheduler.cpp"
                       "../../common/sensors/ecg_ad8232.cpp"
                       "../../common/sensors/ppg_max30102.cpp"
                       "../../common/sensors/ppg_sqi.cpp"
                       "../../common/sensors/ppg_processor.cpp"
                       "../../common/sensors/imu_icm20948.cpp"
                       "../../common/sensors/imu_processor.cpp"
                       INCLUDE_DIRS "." "../../common/scheduler" "../../common/sensors"
                       REQUIRES driver freertos esp_timer esp_adc nvs_flash config
                       PRIV_REQUIRES)
# Band IMU ODR default is 100 Hz (Kconfig); imu_processor defaults to 50 Hz.
target_compile_definitions(${COMPONENT_LIB} PUBLIC IMU_PROCESSOR_SAMPLE_RATE_HZ=100)
