# SYNAPSE-24 ESP32-S3 Head Pod (Tier 0) Firmware
# Architecture.md §23-31: Decoupled sensor pod, Tier 0 continuous H24
# Roadmap.md §138: Live PPG+IMU streaming, synchronized in LSL
# This pod: PPG (MAX30102) + IMU (ICM-20948) only, NO ECG
# Role: SYNC POD (receives wired sync pulse from forearm hub on GPIO 27)

cmake_minimum_required(VERSION 3.16)

set(TARGET_NAME "synapse_head_tier0")
set(ESP_IDF_VERSION_REQUIRED ">=5.0,<6.0")

project(${TARGET_NAME} VERSION 0.1.0 LANGUAGES CXX)

set(COMPONENT_REQUIRES
    esp_wifi
    esp_bt
    nimble
    driver
    i2c
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
    -DSYNAPSE_POD_HEAD=1
    -DSYNAPSE_TIER_0=1
)

add_link_options(
    -Wl,--gc-sections
    -Wl,--print-memory-usage
)

# NOTE: include dirs are provided by main/idf_component_register INCLUDE_DIRS.
# Do NOT use target_include_directories(${TARGET_NAME} ...) here: ESP-IDF names
# the app target "${PROJECT_NAME}.elf", so the bare project name is not a target.