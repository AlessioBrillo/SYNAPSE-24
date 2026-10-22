# The FIT writer is always in the graph: its API is a no-op when
# CONFIG_SYNAPSE_FEATURE_FIT_STORAGE is off (the guards live in
# fit_writer.c), so the component graph and the compiler can never disagree.
# The flash partition it uses is always linked for the same reason.
idf_component_register(SRCS "fit_writer.c"
                       INCLUDE_DIRS "."
                       REQUIRES spiffs esp_partition esp_timer ble
                       PRIV_REQUIRES)
