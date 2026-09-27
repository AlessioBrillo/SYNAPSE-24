# Unit tests for ESP32 Tier 0 firmware
# Builds with ESP-IDF unity test framework

cmake_minimum_required(VERSION 3.16)

set(TEST_TARGET_NAME "synapse_tier0_test")

# Test source files
set(TEST_SRCS
    "ppg_processor_test.cpp"
    "imu_processor_test.cpp"
)

# Include directories
set(TEST_INCLUDE_DIRS
    ${CMAKE_CURRENT_SOURCE_DIR}/../main
    ${CMAKE_CURRENT_SOURCE_DIR}/../main/sensors
    ${CMAKE_CURRENT_SOURCE_DIR}/../main/scheduler
    ${CMAKE_CURRENT_SOURCE_DIR}/../main/sync
)

idf_component_register(
    SRCS ${TEST_SRCS}
    INCLUDE_DIRS ${TEST_INCLUDE_DIRS}
    REQUIRES
        unity
        freertos
        esp_timer
        log
        driver
        i2c
    PRIV_REQUIRES
        main
)

# Compiler flags for tests
target_compile_options(${TEST_TARGET_NAME} PRIVATE
    -Wall
    -Wextra
    -Wno-unused-parameter
    -fno-rtti
    -fno-exceptions
)