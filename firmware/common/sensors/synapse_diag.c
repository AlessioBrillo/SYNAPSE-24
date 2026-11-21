/**
 * @file synapse_diag.c
 * @brief I2C scan and safe register dump (see synapse_diag.h).
 */

#include "synapse_diag.h"

#include <stdio.h>
#include <string.h>
#include "synapse_i2c.h"

typedef struct {
    uint8_t addr;
    const char *name;
    uint8_t width;        /* register width in bytes */
    uint8_t n;
    uint8_t regs[SYNAPSE_DIAG_MAX_REGS];
} device_t;

/* Only registers whose read has no side effect (see the header). */
static const device_t k_devices[] = {
    /* MAX30102: INT_EN1/2, FIFO write/overflow/read pointers, FIFO_CONFIG, MODE, SPO2, LED1/2, REV_ID, PART_ID.
     * NOT 0x00/0x01 (interrupt status, cleared on read) and NOT 0x07 (FIFO data, pops a sample). */
    {0x57, "MAX30102", 1, 12, {0x02, 0x03, 0x04, 0x05, 0x06, 0x08, 0x09, 0x0A, 0x0C, 0x0D, 0xFE, 0xFF}},
    /* ICM-20948 bank 0: WHO_AM_I, USER_CTRL, PWR_MGMT_1/2, INT_PIN_CFG. */
    {0x68, "ICM-20948", 1, 5, {0x00, 0x03, 0x06, 0x07, 0x0F}},
    {0x69, "ICM-20948 (AD0=1)", 1, 5, {0x00, 0x03, 0x06, 0x07, 0x0F}},
    /* AK09916 (the ICM's magnetometer, bypass mode): WIA1, WIA2, CNTL2. NOT ST2 (0x18). */
    {0x0C, "AK09916", 1, 3, {0x00, 0x01, 0x31}},
    /* TMP117: CONFIGURATION, DEVICE_ID. 16-bit. */
    {0x48, "TMP117", 2, 2, {0x01, 0x0F}},
    /* INA219: CONFIGURATION, CALIBRATION. 16-bit. */
    {0x40, "INA219", 2, 2, {0x00, 0x05}},
};

static const device_t *find_device(uint8_t addr)
{
    for (size_t i = 0; i < sizeof(k_devices) / sizeof(k_devices[0]); i++) {
        if (k_devices[i].addr == addr) {
            return &k_devices[i];
        }
    }
    return NULL;
}

const char *synapse_diag_i2c_name(uint8_t addr)
{
    const device_t *d = find_device(addr);
    return d ? d->name : NULL;
}

int synapse_diag_i2c_scan(int port, uint8_t *found, int max)
{
    if (found == NULL || max <= 0) {
        return -ESP_ERR_INVALID_ARG;
    }
    int n = 0;
    for (int addr = SYNAPSE_DIAG_SCAN_FIRST; addr <= SYNAPSE_DIAG_SCAN_LAST; addr++) {
        esp_err_t err = synapse_i2c_probe(port, (uint8_t)addr);
        if (err == ESP_OK) {
            if (n < max) {
                found[n] = (uint8_t)addr;
            }
            n++; /* keep counting past `max`: the caller learns the list was cut */
        } else if (err != ESP_ERR_NOT_FOUND) {
            return -(int)err; /* the bus failed: this is not "nobody answered" */
        }
    }
    return n;
}

int synapse_diag_dump_device(int port, uint8_t addr, synapse_diag_reg_t *out, int max)
{
    const device_t *d = find_device(addr);
    if (d == NULL || out == NULL || max <= 0) {
        return 0;
    }
    int n = 0;
    for (int i = 0; i < d->n && n < max; i++) {
        synapse_diag_reg_t *r = &out[n++];
        uint8_t buf[2] = {0, 0};
        r->reg = d->regs[i];
        r->width = d->width;
        r->value = 0;
        r->err = synapse_i2c_write_read(port, addr, &r->reg, 1, buf, d->width);
        if (r->err == ESP_OK) {
            r->value = (d->width == 2) ? (uint16_t)((buf[0] << 8) | buf[1]) : buf[0];
        } else if (n == 1) {
            return -(int)r->err; /* the very first read failed: the part is not answering */
        }
    }
    return n;
}

size_t synapse_diag_format(const synapse_diag_reg_t *regs, int n, char *buf, size_t len)
{
    if (buf == NULL || len == 0) {
        return 0;
    }
    size_t used = 0;
    buf[0] = '\0';
    for (int i = 0; i < n; i++) {
        char item[16];
        int w;
        if (regs[i].err != ESP_OK) {
            w = snprintf(item, sizeof(item), "%02X=?? ", regs[i].reg);
        } else if (regs[i].width == 2) {
            w = snprintf(item, sizeof(item), "%02X=%04X ", regs[i].reg, regs[i].value);
        } else {
            w = snprintf(item, sizeof(item), "%02X=%02X ", regs[i].reg, regs[i].value & 0xFFu);
        }
        if (w < 0 || used + (size_t)w >= len) {
            break;
        }
        memcpy(buf + used, item, (size_t)w + 1);
        used += (size_t)w;
    }
    if (used > 0) {
        buf[--used] = '\0'; /* drop the trailing space */
    }
    return used;
}
