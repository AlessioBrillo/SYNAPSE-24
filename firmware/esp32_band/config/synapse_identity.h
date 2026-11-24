/**
 * @file synapse_identity.h
 * @brief Stable per-device identity, stored in NVS and exposed over DIS 2A25.
 *
 * A serial derived at boot from the MAC but never stored would change after an
 * NVS erase and could not be printed on the box. The identity is created once,
 * from the full MAC, and kept across reflashes.
 */

#ifndef SYNAPSE_IDENTITY_H
#define SYNAPSE_IDENTITY_H

#include <stddef.h>
#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

#define SYNAPSE_IDENTITY_SERIAL_MAX 20u  /* "SYN-" + 12 hex + NUL */
#define SYNAPSE_IDENTITY_NAMESPACE "synapse_dev"

/**
 * @brief Load the serial from NVS, or create it from @p fallback and store it.
 * @param fallback Serial to persist on first boot, e.g. from the six MAC bytes.
 */
esp_err_t synapse_identity_init(const char *fallback);

/** @return the stable serial (never NULL after a successful init). */
const char *synapse_identity_serial(void);

#ifdef __cplusplus
}
#endif

#endif /* SYNAPSE_IDENTITY_H */
