# SYNAPSE-24 ESP32-S3 Tier 0 Firmware
# Architecture.md §23-31: Decoupled sensor pod, Tier 0 continuous H24
# Roadmap.md §138: Live ECG+PPG+IMU streaming, synchronized in LSL

cmake_minimum_required(VERSION 3.16)

set(TARGET_NAME "synapse_tier0")
set(ESP_IDF_VERSION_REQUIRED ">=5.0,<6.0")

# Project configuration
project(${TARGET_NAME} VERSION 0.1.0 LANGUAGES CXX)

# ESP-IDF component requirements
set(COMPONENT_REQUIRES
    esp_wifi
    esp_bt
    nimble
    driver
    i2c
    adc
    esp_timer
    freertos
    nvs_flash
    esp_system
    log
    spi_flash
    bootloader_support
    esp_common
    esp_rom
    hal
    soc
    esp_hw_support
    esp_partition
    esp_pm
    esp_netif
    lwip
    mbedtls
    console
    esp_console
    linenoise
)

# C++17 standard
set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

# Compiler flags for embedded
add_compile_options(
    -Wall
    -Wextra
    -Wpedantic
    -Wno-unused-parameter
    -Wno-missing-field-initializers
    -fno-rtti
    -fno-exceptions
    -Os
    -ffunction-sections
    -fdata-sections
)

# Linker flags for size optimization
add_link_options(
    -Wl,--gc-sections
    -Wl,--print-memory-usage
)

# Include directories
target_include_directories(${TARGET_NAME} PRIVATE
    ${CMAKE_CURRENT_SOURCE_DIR}/main
    ${CMAKE_CURRENT_SOURCE_DIR}/include
)

# Source files
set(SOURCES
    main/main.cpp
    main/sensors/ecg_ad8232.cpp
    main/sensors/ppg_max30102.cpp
    main/sensors/imu_icm20948.cpp
    main/ble/ble_lsl_bridge.cpp
    main/sync/sync_marker_handler.cpp
    main/power/power_monitor.cpp
    main/triage/triage_inference.cpp
)

# Add executable
idf_component_register(
    SRCS ${SOURCES}
    INCLUDE_DIRS "main" "include"
    REQUIRES ${COMPONENT_REQUIRES}
    PRIV_REQUIRES
)

# Flash size: 4MB (ESP32-S3 default)
# Partition table: default with 1.5MB app, 1.5MB OTA
# Target: ESP32-S3 (Xtensa LX7 dual-core, 240MHz, 512KB SRAM + 384KB ROM)