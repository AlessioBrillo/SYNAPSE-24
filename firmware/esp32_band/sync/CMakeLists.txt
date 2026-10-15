idf_component_register(SRCS "../../common/sync/wired_sync_handler.cpp"
                       INCLUDE_DIRS "../../common/sync"
                       REQUIRES driver esp_timer
                       PRIV_REQUIRES)
