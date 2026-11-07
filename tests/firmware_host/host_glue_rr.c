/* Host wrappers over firmware/esp32_band/signal_quality/synapse_rr.c (opaque state for ctypes). */
#include <stdbool.h>
#include <stdint.h>

#include "synapse_rr.h"

uint32_t host_rr_size(void) { return (uint32_t)sizeof(synapse_rr_t); }
void host_rr_init(void *st) { synapse_rr_init((synapse_rr_t *)st); }
int host_rr_push(void *st, float ir, int64_t ts_us)
{
    return synapse_rr_push((synapse_rr_t *)st, ir, ts_us) ? 1 : 0;
}
uint32_t host_rr_take(void *st, uint16_t *out, uint8_t max)
{
    return synapse_rr_take((synapse_rr_t *)st, out, max);
}
uint32_t host_rr_peek(const void *st, uint16_t *out, uint8_t max)
{
    return synapse_rr_peek((const synapse_rr_t *)st, out, max);
}
void host_rr_commit(void *st) { synapse_rr_commit((synapse_rr_t *)st); }
void host_rr_expire(void *st, int64_t now_us) { synapse_rr_expire((synapse_rr_t *)st, now_us); }
int host_rr_hrv(const void *st, float *rmssd, float *sdnn)
{
    return synapse_rr_hrv((const synapse_rr_t *)st, rmssd, sdnn) ? 1 : 0;
}
uint32_t host_rr_accepted(const void *st) { return ((const synapse_rr_t *)st)->accepted; }
uint32_t host_rr_rejected(const void *st) { return ((const synapse_rr_t *)st)->rejected; }
