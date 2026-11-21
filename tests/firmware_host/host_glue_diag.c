/* Host gate for firmware/common/sensors/synapse_diag.c: the I2C scan and the safe register dump.
 *
 * synapse_i2c is replaced by a fake bus: which addresses answer, a register map per address, and a
 * log of every register that was read, so the test can prove that the dump never touches a register
 * whose read has a side effect (the MAX30102 FIFO data and interrupt-status registers, the AK09916
 * ST2 register).
 */
#include <stdint.h>
#include <string.h>

#include "synapse_diag.h"
#include "synapse_i2c.h"

static uint8_t s_present[128];
static uint8_t s_regs[128][256];
static uint8_t s_read[128][256]; /* 1 where a register was read */
static esp_err_t s_bus_error;    /* probe returns this for every address when set */
static int s_read_error_addr;    /* write_read to this address fails; -1 = none */

esp_err_t synapse_i2c_acquire(int port, int sda, int scl, uint32_t freq)
{
    (void)port; (void)sda; (void)scl; (void)freq;
    return ESP_OK;
}
void synapse_i2c_release(int port) { (void)port; }
esp_err_t synapse_i2c_write(int port, uint8_t addr, const uint8_t *d, size_t n)
{
    (void)port; (void)addr; (void)d; (void)n;
    return ESP_OK;
}

esp_err_t synapse_i2c_probe(int port, uint8_t addr)
{
    (void)port;
    if (s_bus_error != ESP_OK) {
        return s_bus_error;
    }
    return (addr < 128 && s_present[addr]) ? ESP_OK : ESP_ERR_NOT_FOUND;
}

esp_err_t synapse_i2c_write_read(int port, uint8_t addr, const uint8_t *w, size_t wn, uint8_t *r, size_t rn)
{
    (void)port;
    if (addr >= 128 || wn != 1 || rn == 0) {
        return ESP_ERR_INVALID_ARG;
    }
    if ((int)addr == s_read_error_addr) {
        return ESP_ERR_TIMEOUT;
    }
    for (size_t i = 0; i < rn; i++) {
        s_read[addr][(uint8_t)(w[0] + i)] = 1;
        r[i] = s_regs[addr][(uint8_t)(w[0] + i)];
    }
    return ESP_OK;
}

const char *esp_err_to_name(esp_err_t e) { (void)e; return "stub"; }

/* --- python-facing helpers ---------------------------------------------------------------------- */
void h_reset(void)
{
    memset(s_present, 0, sizeof(s_present));
    memset(s_regs, 0, sizeof(s_regs));
    memset(s_read, 0, sizeof(s_read));
    s_bus_error = ESP_OK;
    s_read_error_addr = -1;
}
void h_set_present(int addr, int on) { s_present[addr & 0x7F] = on ? 1 : 0; }
void h_set_reg(int addr, int reg, int value) { s_regs[addr & 0x7F][reg & 0xFF] = (uint8_t)value; }
void h_set_bus_error(int err) { s_bus_error = err; }
void h_set_read_error_addr(int addr) { s_read_error_addr = addr; }
int h_was_read(int addr, int reg) { return s_read[addr & 0x7F][reg & 0xFF]; }

int h_scan(uint8_t *out, int max) { return synapse_diag_i2c_scan(0, out, max); }
const char *h_name(int addr) { return synapse_diag_i2c_name((uint8_t)addr); }

/* Dump a device into parallel arrays; returns the count. */
int h_dump(int addr, uint8_t *regs, uint16_t *values, uint8_t *widths, int *errs, int max)
{
    synapse_diag_reg_t d[SYNAPSE_DIAG_MAX_REGS];
    int n = synapse_diag_dump_device(0, (uint8_t)addr, d, max < SYNAPSE_DIAG_MAX_REGS ? max : SYNAPSE_DIAG_MAX_REGS);
    for (int i = 0; i < n && i < SYNAPSE_DIAG_MAX_REGS; i++) {
        regs[i] = d[i].reg;
        values[i] = d[i].value;
        widths[i] = d[i].width;
        errs[i] = d[i].err;
    }
    return n;
}

/* The console line of a dump. */
int h_dump_text(int addr, char *buf, int len)
{
    synapse_diag_reg_t d[SYNAPSE_DIAG_MAX_REGS];
    int n = synapse_diag_dump_device(0, (uint8_t)addr, d, SYNAPSE_DIAG_MAX_REGS);
    if (n <= 0) {
        buf[0] = '\0';
        return n;
    }
    return (int)synapse_diag_format(d, n, buf, (size_t)len);
}

int h_first(void) { return SYNAPSE_DIAG_SCAN_FIRST; }
int h_last(void) { return SYNAPSE_DIAG_SCAN_LAST; }
int h_max_found(void) { return SYNAPSE_DIAG_MAX_FOUND; }
