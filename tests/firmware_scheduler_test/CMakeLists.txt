idf_component_register(
    SRCS "test_firmware_scheduler.c"
    INCLUDE_DIRS "."
    REQUIRES
        freertos
        esp_system
        log
    PRIV_REQUIRES
)