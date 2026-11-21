#pragma once

/**
 * @file synapse_diag.h
 * @brief Bring-up diagnostics on the shared I2C bus: who answers, and what are the key registers.
 *
 * "The PPG is absent from the boot log" has several causes (wiring, power, pull-ups, a wrong
 * address, a dead part) that look the same from the firmware. A bus scan separates "nobody
 * answers at 0x57" from "something answers at 0x56", and a register dump shows the configuration
 * the part really holds. Both go through synapse_i2c, so they work on the bus the sensors use and
 * are tested on the host against a fake bus (tests/firmware_host/host_glue_diag.c).
 *
 * The dump reads only registers that are safe to read while the band runs. Reading the MAX30102
 * FIFO data register pops a sample, its interrupt status registers clear on read, the AK09916
 * ST2 register releases the data protection, and the TMP117 configuration / result registers
 * clear its Data_Ready flag: none of those are in the table.
 *
 * The table knows each part at its usual address (MAX30102 0x57, ICM-20948 0x68/0x69, AK09916
 * 0x0C, TMP117 0x48, INA219 0x40). A part strapped to another address is listed by the scan but
 * not dumped (it shows as "unknown device").
 *
 * Errors are negative counts: the negated esp_err_t, with ESP_FAIL (-1, which cannot be negated
 * into a negative number) reported as ESP_ERR_INVALID_RESPONSE.
 */

#include <stddef.h>
#include <stdint.h>
#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

#define SYNAPSE_DIAG_SCAN_FIRST 0x08 /* 0x00-0x07 and 0x78-0x7F are reserved by the I2C standard */
#define SYNAPSE_DIAG_SCAN_LAST  0x77
#define SYNAPSE_DIAG_MAX_FOUND  16
#define SYNAPSE_DIAG_MAX_REGS   16

typedef struct {
    uint8_t reg;
    uint16_t value; /* 8-bit registers use the low byte; 16-bit ones are big-endian, as the parts send them */
    uint8_t width;  /* bytes: 1 or 2 */
    esp_err_t err;  /* ESP_OK if this register was read */
} synapse_diag_reg_t;

/** Name of a part the band can carry at `addr`, or NULL for a stranger. */
const char *synapse_diag_i2c_name(uint8_t addr);

/**
 * Probe SYNAPSE_DIAG_SCAN_FIRST..LAST on `port` (the bus must have been acquired by a driver).
 * Fills `found` with up to `max` acknowledging addresses and returns how many answered; a
 * negative value is the esp_err_t of a bus that failed instead of answering "nobody".
 */
int synapse_diag_i2c_scan(int port, uint8_t *found, int max);

/**
 * Read the safe key registers of the known part at `addr`. Returns how many were read (0 for a
 * part not in the table); individual failures are in `out[i].err`. A bus error on the first
 * register is returned as a negative esp_err_t.
 */
int synapse_diag_dump_device(int port, uint8_t addr, synapse_diag_reg_t *out, int max);

/** "09=03 0A=2B 0F=0117": the registers of a dump as one console line. Returns the length. */
size_t synapse_diag_format(const synapse_diag_reg_t *regs, int n, char *buf, size_t len);

#ifdef __cplusplus
}
#endif
