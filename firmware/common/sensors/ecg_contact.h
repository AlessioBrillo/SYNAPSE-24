/**
 * @file ecg_contact.h
 * @brief When the ECG bias must be measured again. Pure C, no ESP-IDF.
 *
 * The mid-rail (AD8232 output bias) is measured over the first second after boot and then frozen
 * (a tracking bias was a 0.016 Hz high-pass on a "raw" stream). A start-up with the electrodes off,
 * or a settling output, would then keep a wrong bias for the whole session. The lead-off input
 * tells when the electrodes go on: after a real lead-off (at least ECG_CONTACT_OFF_MIN_SAMPLES in
 * a row) followed by ECG_CONTACT_SETTLE_SAMPLES of steady contact, the bias is measured again.
 * A flickering contact (short lead-off) never triggers it, so the stream is not interrupted by it.
 * Compiled and run on the host (tests/firmware_host/host_glue_ecg_contact.c).
 */
#ifndef ECG_CONTACT_H
#define ECG_CONTACT_H

#include <stdbool.h>
#include <stdint.h>

#define ECG_CONTACT_OFF_MIN_SAMPLES 500u /* 1 s at the 500 Hz ECG rate */
#define ECG_CONTACT_SETTLE_SAMPLES  250u /* 0.5 s of steady contact */

typedef struct {
    uint32_t off_run; /* consecutive samples with the lead-off input asserted */
    uint32_t on_run;  /* consecutive samples with contact since then */
} ecg_contact_t;

/** Feed one sample's lead-off state; true = measure the bias again now. */
static inline bool ecg_contact_update(ecg_contact_t *c, bool lead_off)
{
    if (lead_off) {
        if (c->off_run < UINT32_MAX) {
            c->off_run++;
        }
        c->on_run = 0u;
        return false;
    }
    if (c->on_run < UINT32_MAX) {
        c->on_run++;
    }
    if (c->off_run >= ECG_CONTACT_OFF_MIN_SAMPLES && c->on_run >= ECG_CONTACT_SETTLE_SAMPLES) {
        c->off_run = 0u;
        return true;
    }
    return false;
}

#endif /* ECG_CONTACT_H */
