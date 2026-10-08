idf_component_register(SRCS "synapse_sensors.c" "gps_max_m10s.c" "temp_tmp117.c"
                       "../../common/scheduler/sensor_scheduler.cpp"
                       "../../common/sensors/ecg_ad8232.cpp"
                       "../../common/sensors/ppg_max30102.cpp"
                       "../../common/sensors/ppg_sqi.cpp"
                       "../../common/sensors/ppg_processor.cpp"
                       "../../common/sensors/ppg_dsp.c"
                       "../../common/sensors/imu_icm20948.cpp"
                       "../../common/sensors/imu_processor.cpp"
                       INCLUDE_DIRS "." "../../common/scheduler" "../../common/sensors"
                       REQUIRES driver freertos esp_timer esp_adc nvs_flash config
                       PRIV_REQUIRES)
# imu_processor must run at the real IMU ODR (Kconfig); triage needs 50 Hz (triage_features_init checks).
if(DEFINED CONFIG_SYNAPSE_IMU_ACCEL_ODR_HZ)
    target_compile_definitions(${COMPONENT_LIB} PUBLIC IMU_PROCESSOR_SAMPLE_RATE_HZ=${CONFIG_SYNAPSE_IMU_ACCEL_ODR_HZ})
else()
    message(FATAL_ERROR "CONFIG_SYNAPSE_IMU_ACCEL_ODR_HZ undefined: imu_processor rate would silently default")
endif()
