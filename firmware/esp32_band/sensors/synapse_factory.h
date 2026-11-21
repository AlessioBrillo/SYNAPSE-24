#ifndef SYNAPSE_FACTORY_H
#define SYNAPSE_FACTORY_H

/**
 * @file synapse_factory.h
 * @brief The factory test: every check a freshly assembled band must pass, one PASS / FAIL.
 *
 * Runs when CONFIG_SYNAPSE_FACTORY_MODE is on, once per boot, after the sensors are started and
 * before the band begins its normal work:
 *   1. every sensor the build carries initialised;
 *   2. an I2C scan finds each expected part at its address (and says what else answers);
 *   3. each part passes its self-test (identity, plausible readings, programmed registers);
 *   4. 10 s of acquisition: every stream delivers at least 80 % of its rate.
 * The report goes to the console, one line per check and a last `FACTORY RESULT: PASS|FAIL`.
 * The band should lie still on the bench: the IMU check wants 1 g and no rotation. The ECG
 * stream must deliver its samples, but its signal is only reported (lead-off, stuck): a station
 * has no electrodes, and the AD8232 output floats then.
 *
 * The BLE report and a runtime switch (NVS flag) come with the BLE configuration in contract 2.4.
 */

#include <stdbool.h>

#ifdef __cplusplus
extern "C" {
#endif

/** Run the factory test and log the report. Returns true on PASS. Blocks for about 13 seconds. */
bool synapse_factory_run(void);

#ifdef __cplusplus
}
#endif

#endif /* SYNAPSE_FACTORY_H */
