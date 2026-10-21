# SYNAPSE-24 feature gating for the CMake component graph.
#
# The project features (FIT storage, BLE provisioning, OTA) are Kconfig
# symbols: the *code* is gated with `#if defined(CONFIG_...)` and the
# *component graph* must agree, or the build ends up with a source that
# includes a header of a component that is not linked.
#
# The symbols of the project's own Kconfig are not always visible in the
# CMake scope (they depend on the ESP-IDF version and on when the
# sdkconfig.cmake is generated), while the generated sdkconfig.h always
# carries them: the header is what the compiler sees. So the build reads the
# header, and the two can never disagree.

function(synapse_feature_enabled feature_name out_var)
    set(_header "${CMAKE_BINARY_DIR}/config/sdkconfig.h")
    set(_enabled OFF)
    if(EXISTS "${_header}")
        file(READ "${_header}" _content)
        if(_content MATCHES "#define ${feature_name} 1")
            set(_enabled ON)
        endif()
    else()
        message(WARNING
            "synapse_feature_enabled: ${_header} not found, "
            "treating ${feature_name} as OFF")
    endif()
    set(${out_var} ${_enabled} PARENT_SCOPE)
endfunction()
