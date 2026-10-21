# The FIT writer is part of the product build only: the band-core variant
# (sdkconfig.defaults.core) has no FIT storage, and the acquisition code
# guards its FIT calls with the same CONFIG_ symbol (see main/CMakeLists.txt
# for the same mechanism).
include("${CMAKE_CURRENT_LIST_DIR}/../cmake/synapse_features.cmake")

set(SYNAPSE_ACQUISITION_REQUIRES
    freertos esp_timer driver config sensors power signal_quality)

synapse_feature_enabled(CONFIG_SYNAPSE_FEATURE_FIT_STORAGE SYNAPSE_HAS_FIT)
if(SYNAPSE_HAS_FIT)
    list(APPEND SYNAPSE_ACQUISITION_REQUIRES fit)
endif()

idf_component_register(SRCS "synapse_acquisition.c"
                       INCLUDE_DIRS "."
                       REQUIRES ${SYNAPSE_ACQUISITION_REQUIRES}
                       PRIV_REQUIRES)
