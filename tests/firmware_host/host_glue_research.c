/* Host-side compile gate for the Synapse Band research service.
 *
 * This file is compiled together with firmware/esp32_band/ble/ble_research_service.c
 * to prove the translation unit links. It provides the NimBLE/ESP-IDF symbols
 * the service references, so a shared library can be produced on a host
 * without ESP-IDF.
 *
 * The interesting assertions live in tests/test_research_service_host.py, which
 * compiles this same set of sources with -Wall -Wextra -Werror.
 */

#include "ble_research_service.h"
#include "synapse_raw_frame.h"

#include <assert.h>
#include <stddef.h>
#include <stdint.h>

/* --- NimBLE / ESP-IDF symbols the service calls -----------------------
 * Minimal, side-effect free: the service must not depend on a real stack for
 * the behaviour this gate checks (registration idempotence, NULL safety,
 * stream-table consistency). */

struct os_mbuf; /* completed in the NimBLE stub headers */
struct ble_gatt_svc_def;
struct ble_gatt_chr_def;

int64_t esp_timer_get_time(void) { return 0; }

int ble_gatts_count_cfg(const struct ble_gatt_svc_def *s) { (void)s; return 0; }
int ble_gatts_add_svcs(const struct ble_gatt_svc_def *s) { (void)s; return 0; }
int ble_gatts_notify_custom(uint16_t c, uint16_t h, struct os_mbuf *om)
{
    (void)c;
    (void)h;
    (void)om;
    return 0;
}
int ble_hs_mbuf_to_flat(struct os_mbuf *om, void *dst, uint16_t max_len, uint16_t *out_len)
{
    (void)om;
    (void)dst;
    (void)max_len;
    if (out_len != NULL) {
        *out_len = 0;
    }
    return 0;
}
struct os_mbuf *ble_hs_mbuf_from_flat(const void *d, uint16_t l) { (void)d; (void)l; return NULL; }
void os_mbuf_free_chain(struct os_mbuf *m) { (void)m; }
int os_mbuf_append(struct os_mbuf *om, const void *d, uint16_t l) { (void)om; (void)d; (void)l; return 0; }
void ble_svc_gap_init(void) {}
void ble_svc_gatt_init(void) {}
const char *esp_err_to_name(esp_err_t e) { (void)e; return "stub"; }

int synapse_host_smoke_main(void);

int synapse_host_smoke_main(void)
{
    /* Sanity-check the shared codec the service depends on. The real framing
     * behaviour is verified against the Python decoder in
     * tests/test_raw_frame_parity.py. */
    assert(synapse_raw_format_size(SYNAPSE_RAW_FORMAT_S16) == 2u);
    assert(synapse_raw_max_samples(SYNAPSE_RAW_FORMAT_S16) != 0u);

    /* Calling the public API before init must be a no-op, not a crash: the
     * real call site is a FreeRTOS task. */
    synapse_research_service_submit(NULL);
    return 0;
}

