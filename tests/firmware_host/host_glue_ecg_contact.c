/* Host wrappers over firmware/common/sensors/ecg_contact.h (static inline there). */
#include <stdbool.h>
#include <stdint.h>

#include "ecg_contact.h"

static ecg_contact_t s_c;

void host_contact_reset(void) { s_c.off_run = 0u; s_c.on_run = 0u; }
int host_contact_feed(int lead_off) { return ecg_contact_update(&s_c, lead_off != 0) ? 1 : 0; }
uint32_t host_contact_off_min(void) { return ECG_CONTACT_OFF_MIN_SAMPLES; }
uint32_t host_contact_settle(void) { return ECG_CONTACT_SETTLE_SAMPLES; }
