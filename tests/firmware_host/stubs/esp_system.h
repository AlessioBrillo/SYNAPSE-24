#ifndef SYNAPSE_HOST_ESP_SYSTEM_H
#define SYNAPSE_HOST_ESP_SYSTEM_H

/* Host stub for the factory CLI's `cal reboot` command. A host test must not
 * restart anything: count the call instead, so a test can prove the shell
 * offered the command without needing a real chip. */
static int host_esp_restart_calls = 0;

static inline void esp_restart(void) { host_esp_restart_calls++; }

#endif /* SYNAPSE_HOST_ESP_SYSTEM_H */
