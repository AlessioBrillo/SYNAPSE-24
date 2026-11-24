#ifndef SYNAPSE_POWER_STUB_H
#define SYNAPSE_POWER_STUB_H

#include <stdbool.h>

/* The research service only needs to know whether the POWER telemetry is a
 * model; the full firmware header pulls in ESP-IDF types the host gate cannot
 * compile. The symbol is implemented in host_glue_research.c. */
bool synapse_power_is_model(void);

#endif
