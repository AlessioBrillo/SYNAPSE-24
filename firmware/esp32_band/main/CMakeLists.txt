# The band-core build (sdkconfig.defaults.core) drops FIT storage, BLE
# provisioning and OTA. The components are therefore required only when their
# Kconfig flag is on: app_main.c guards the matching code with the same
# CONFIG_ symbols, so "band-core" really is a subset of "band-full" instead of
# a build that silently ignores its own configuration.
#
# The flags are read from the generated sdkconfig.h (see
# cmake/synapse_features.cmake): the compiler and the component graph must
# read the same value, or the build ends up with a source that includes the
# header of a component that is not linked.
include("${CMAKE_CURRENT_LIST_DIR}/../cmake/synapse_features.cmake")

set(SYNAPSE_MAIN_REQUIRES
    ble acquisition sensors edge_ai signal_quality config power sync
    # app_main.c reads the MAC from the eFuse and the OTA state from the
    # app_update component: both are required explicitly, because the
    # provisioning and OTA components that used to pull them in transitively
    # are not part of the band-core build.
    app_update)
set(SYNAPSE_MAIN_PRIV_REQUIRES bt nvs_flash)

# The eFuse component was renamed in ESP-IDF 5.3 ("esp_efuse" -> "efuse"):
# pick the one this IDF version ships, so the band builds on every pinned
# version (5.2.1 in the firmware matrix, 5.5.5 in the canonical build).
if(EXISTS "${IDF_PATH}/components/efuse")
    list(APPEND SYNAPSE_MAIN_REQUIRES efuse)
elseif(EXISTS "${IDF_PATH}/components/esp_efuse")
    list(APPEND SYNAPSE_MAIN_REQUIRES esp_efuse)
endif()

synapse_feature_enabled(CONFIG_SYNAPSE_FEATURE_FIT_STORAGE SYNAPSE_HAS_FIT)
if(SYNAPSE_HAS_FIT)
    list(APPEND SYNAPSE_MAIN_REQUIRES fit)
endif()

synapse_feature_enabled(CONFIG_SYNAPSE_FEATURE_PROVISIONING SYNAPSE_HAS_PROVISIONING)
if(SYNAPSE_HAS_PROVISIONING)
    list(APPEND SYNAPSE_MAIN_REQUIRES provisioning)
endif()

synapse_feature_enabled(CONFIG_SYNAPSE_FEATURE_OTA SYNAPSE_HAS_OTA)
if(SYNAPSE_HAS_OTA)
    list(APPEND SYNAPSE_MAIN_REQUIRES ota)
endif()

idf_component_register(SRCS "app_main.c"
                    INCLUDE_DIRS "."
                    REQUIRES ${SYNAPSE_MAIN_REQUIRES}
                    PRIV_REQUIRES ${SYNAPSE_MAIN_PRIV_REQUIRES})
