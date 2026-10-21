# The band-core build (sdkconfig.defaults.core) drops FIT storage, BLE
# provisioning and OTA. The components are therefore required only when their
# Kconfig flag is on: app_main.c guards the matching code with the same
# CONFIG_ symbols, so "band-core" really is a subset of "band-full" instead of
# a build that silently ignores its own configuration.
#
# CONFIG_ values come from the generated sdkconfig.cmake, which ESP-IDF
# includes at project scope before the components are processed (the same
# mechanism firmware/esp32_band/sensors/CMakeLists.txt already uses for
# CONFIG_SYNAPSE_FEATURE_GPS / _TEMPERATURE).
set(SYNAPSE_MAIN_REQUIRES
    ble acquisition sensors edge_ai signal_quality config power sync
    # app_main.c reads the MAC from the eFuse and the OTA state from the
    # app_update component: both are required explicitly, because the
    # provisioning and OTA components that used to pull them in transitively
    # are not part of the band-core build.
    efuse app_update)
set(SYNAPSE_MAIN_PRIV_REQUIRES bt nvs_flash)

if(CONFIG_SYNAPSE_FEATURE_FIT_STORAGE)
    list(APPEND SYNAPSE_MAIN_REQUIRES fit)
endif()

if(CONFIG_SYNAPSE_FEATURE_PROVISIONING)
    list(APPEND SYNAPSE_MAIN_REQUIRES provisioning)
endif()

if(CONFIG_SYNAPSE_FEATURE_OTA)
    list(APPEND SYNAPSE_MAIN_REQUIRES ota)
endif()

idf_component_register(SRCS "app_main.c"
                    INCLUDE_DIRS "."
                    REQUIRES ${SYNAPSE_MAIN_REQUIRES}
                    PRIV_REQUIRES ${SYNAPSE_MAIN_PRIV_REQUIRES})
