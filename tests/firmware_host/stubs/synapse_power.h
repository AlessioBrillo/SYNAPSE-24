#ifndef SYNAPSE_POWER_STUB_H
#define SYNAPSE_POWER_STUB_H

#include <stdbool.h>

/* Minimal host view of the power API: the research service needs only the
 * model flag, the INA219 bench needs the telemetry sink and the measurement
 * witness. The full firmware header pulls in ESP-IDF types the host gate
 * cannot compile. The symbols are implemented in each bench's host glue. */
typedef void (*synapse_power_telemetry_sink_t)(float voltage_mv, float current_ma,
                                               float battery_pct);

bool synapse_power_is_model(void);
bool synapse_power_has_measured_current(void);
void synapse_power_set_telemetry_sink(synapse_power_telemetry_sink_t sink);

#endif
