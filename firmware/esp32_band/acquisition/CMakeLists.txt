# The FIT writer is always available (its API is a no-op when the flag is
# off), so the acquisition component never has to guess whether the header
# is on the include path.
idf_component_register(SRCS "synapse_acquisition.c"
                       INCLUDE_DIRS "."
                       REQUIRES freertos esp_timer driver config sensors power signal_quality fit
                       PRIV_REQUIRES)
