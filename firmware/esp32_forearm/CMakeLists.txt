# SYNAPSE-24 ESP32-S3 Forearm Hub Firmware
# Architecture.md §23-31: Decoupled sensor pod, hub with ECG + PPG + IMU
# Roadmap.md §138: Live ECG+PPG+IMU streaming, synchronized in LSL
# This hub: ECG (AD8232) + PPG (MAX30102) + IMU (ICM-20948)
# Role: SYNC HUB (drives wired sync pulse on GPIO 27 to head pod)

cmake_minimum_required(VERSION 3.16)

set(TARGET_NAME "synapse_forearm_hub")
set(ESP_IDF_VERSION_REQUIRED ">=5.0,<6.0")

project(${TARGET_NAME} VERSION 0.1.0 LANGUAGES CXX)

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

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

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
    -DSYNAPSE_POD_FOREARM=1
    -DSYNAPSE_HUB=1
)

add_link_options(
    -Wl,--gc-sections
    -Wl,--print-memory-usage
)

target_include_directories(${TARGET_NAME} PRIVATE
    ${CMAKE_CURRENT_SOURCE_DIR}/main
    ${CMAKE_CURRENT_SOURCE_DIR}/include
    ${CMAKE_CURRENT_SOURCE_DIR}/../common/sensors
    ${CMAKE_CURRENT_SOURCE_DIR}/../common/scheduler
    ${CMAKE_CURRENT_SOURCE_DIR}/../common/sync
    ${CMAKE_CURRENT_SOURCE_DIR}/../common/ble
    ${CMAKE_CURRENT_SOURCE_DIR}/../common/triage
    ${CMAKE_CURRENT_SOURCE_DIR}/../common/power
)