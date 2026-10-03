# Synapse Band v1 - Root CMakeLists.txt (ESP-IDF project)
# Modular components: config, sensors, acquisition, signal_quality, edge_ai,
# power, ble, fit, provisioning, ota + main

cmake_minimum_required(VERSION 3.16)

# Local modules as extra components (mirrors head/forearm common pattern)
set(EXTRA_COMPONENT_DIRS
    ${CMAKE_CURRENT_SOURCE_DIR}/config
    ${CMAKE_CURRENT_SOURCE_DIR}/sensors
    ${CMAKE_CURRENT_SOURCE_DIR}/acquisition
    ${CMAKE_CURRENT_SOURCE_DIR}/signal_quality
    ${CMAKE_CURRENT_SOURCE_DIR}/edge_ai
    ${CMAKE_CURRENT_SOURCE_DIR}/power
    ${CMAKE_CURRENT_SOURCE_DIR}/ble
    ${CMAKE_CURRENT_SOURCE_DIR}/fit
    ${CMAKE_CURRENT_SOURCE_DIR}/provisioning
    ${CMAKE_CURRENT_SOURCE_DIR}/ota
)

# Include ESP-IDF build system
include($ENV{IDF_PATH}/tools/cmake/project.cmake)

project(synapse-band)