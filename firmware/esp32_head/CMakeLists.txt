# SYNAPSE-24 ESP32-S3 Head Pod (Tier 0) Firmware
# Architecture.md §23-31: Decoupled sensor pod, Tier 0 continuous H24
# Roadmap.md §138: Live PPG+IMU streaming, synchronized in LSL
# This pod: PPG (MAX30102) + IMU (ICM-20948) only, NO ECG
# Role: SYNC POD (receives wired sync pulse from forearm hub on GPIO 27)

cmake_minimum_required(VERSION 3.16)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

# ESP-IDF build system (must be included BEFORE project()).
# This generates the "${PROJECT_NAME}.elf" app target from the 'main' component.
include($ENV{IDF_PATH}/tools/cmake/project.cmake)
project(synapse_head_tier0)

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
# Do NOT use target_include_directories(<project-name> ...) here: ESP-IDF names
# the app target "${PROJECT_NAME}.elf", so the bare project name is not a target.
