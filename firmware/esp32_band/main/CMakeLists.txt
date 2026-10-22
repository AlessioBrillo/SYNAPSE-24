# The optional components (FIT storage, BLE provisioning, OTA) are always
# part of the component graph: their API is a no-op when the matching
# Kconfig flag is off (the guards live in the sources), so the graph can
# never disagree with the compiler. What the flags decide is the heavy
# dependency of each component (the Wi-Fi stack, the flash partition).
#
# The eFuse component was renamed in ESP-IDF 5.3 ("esp_efuse" -> "efuse"):
# pick the one this IDF version ships, so the band builds on every pinned
# version (5.2.1 in the firmware matrix, 5.5.5 in the canonical build).
set(SYNAPSE_MAIN_REQUIRES
    ble acquisition sensors edge_ai signal_quality config power sync
    fit provisioning ota
    # app_main.c reads the MAC from the eFuse and the OTA state from the
    # app_update component: both are required explicitly.
    app_update)
set(SYNAPSE_MAIN_PRIV_REQUIRES bt nvs_flash)

if(EXISTS "${IDF_PATH}/components/efuse")
    list(APPEND SYNAPSE_MAIN_REQUIRES efuse)
elseif(EXISTS "${IDF_PATH}/components/esp_efuse")
    list(APPEND SYNAPSE_MAIN_REQUIRES esp_efuse)
endif()

idf_component_register(SRCS "app_main.c"
                    INCLUDE_DIRS "."
                    REQUIRES ${SYNAPSE_MAIN_REQUIRES}
                    PRIV_REQUIRES ${SYNAPSE_MAIN_PRIV_REQUIRES})
