idf_component_register(SRCS "synapse_sensors.c"
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

# band-core build (smoke kit) omits the sensors that are not yet procured.
# GPS/Temp were previously compiled unconditionally, which forced the bringup
# rig to wait for the MAX-M10S. CONFIG_SYNAPSE_FEATURE_GPS/TEMPERATURE now
# actually gate their drivers.
if(CONFIG_SYNAPSE_FEATURE_GPS)
    target_sources(${COMPONENT_LIB} PRIVATE "gps_max_m10s.c")
endif()
if(CONFIG_SYNAPSE_FEATURE_TEMPERATURE)
    target_sources(${COMPONENT_LIB} PRIVATE "temp_tmp117.c")
endif()
# imu_processor must run at the real IMU ODR (Kconfig); triage needs 50 Hz (triage_features_init checks).
if(DEFINED CONFIG_SYNAPSE_IMU_ACCEL_ODR_HZ)
    target_compile_definitions(${COMPONENT_LIB} PUBLIC IMU_PROCESSOR_SAMPLE_RATE_HZ=${CONFIG_SYNAPSE_IMU_ACCEL_ODR_HZ})
else()
    message(FATAL_ERROR "CONFIG_SYNAPSE_IMU_ACCEL_ODR_HZ undefined: imu_processor rate would silently default")
endif()
