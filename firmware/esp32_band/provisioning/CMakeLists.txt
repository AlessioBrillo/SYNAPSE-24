# The provisioning component is always in the graph: its API is a no-op
# when CONFIG_SYNAPSE_FEATURE_PROVISIONING is off (the guards live in the
# sources), so the component graph and the compiler can never disagree.
#
# The Wi-Fi stack is not part of it: the provisioning code of the MVP does
# not call it (the connect is deferred), so the band-core build does not link
# it either.
idf_component_register(SRCS "synapse_provisioning.c"
                       INCLUDE_DIRS "."
                       REQUIRES nvs_flash esp_event
                       PRIV_REQUIRES)
