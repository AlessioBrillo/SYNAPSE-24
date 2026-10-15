/* Host-side compile + behaviour gate for the Synapse Band research service.
 *
 * This file is compiled together with firmware/esp32_band/ble/ble_research_service.c
 * to prove the translation unit links and, since Fase 1 of the pre-hardware
 * closure increment, that the service actually TRANSMITS. Three independent
 * preconditions gate every notification (transmitting, connected, subscribed);
 * all three were silently false on the real firmware — the pod registered the
 * characteristics and emitted nothing, and no test noticed. The bench below
 * drives the public API and captures the frames the service hands to
 * ble_gatts_notify_custom, so each precondition is proven required.
 *
 * The NimBLE/ESP-IDF implementations here are minimal but functional:
 *   - ble_gatts_add_svcs assigns the characteristic handles the way the real
 *     stack does (sequential per characteristic in registration order);
 *   - os_mbuf is a fixed pool so flush_stream's real code path runs;
 *   - ble_gatts_notify_custom copies each notification into the capture ring
 *     instead of discarding it;
 *   - esp_timer_get_time advances like a 500 Hz sample clock so frame
 *     timestamps are meaningful.
 */

#include "ble_research_service.h"
#include "synapse_raw_frame.h"

/* The bench walks the GATT tables to assign characteristic handles the way
 * the real registration path does, so it needs the complete stub types. */
#include "host/ble_gatt.h"

#include <assert.h>
#include <stddef.h>
#include <stdint.h>
#include <string.h>

/* --- NimBLE / ESP-IDF symbols the service calls ----------------------- */

int64_t esp_timer_get_time(void) {
    /* Deterministic 500 Hz sample clock: every read advances 2 ms. */
    static int64_t mock_us = 1000000;
    mock_us += 2000;
    return mock_us;
}

int ble_gatts_count_cfg(const struct ble_gatt_svc_def *s) { (void)s; return 0; }

static uint16_t g_next_handle = 1;

int ble_gatts_add_svcs(const struct ble_gatt_svc_def *svcs) {
    /* Walk the service tables exactly like the real registration path and
     * hand out sequential value handles, so subscribe routing can be tested
     * with the handles the service captured at init. */
    for (; svcs->uuid != NULL; svcs++) {
        for (const struct ble_gatt_chr_def *c = svcs->characteristics; c->uuid != NULL; c++) {
            if (c->val_handle != NULL) {
                *(c->val_handle) = g_next_handle++;
            }
        }
    }
    return 0;
}

/* Fixed mbuf pool: flush_stream allocates one frame at a time. */
struct os_mbuf {
    uint8_t data[SYNAPSE_RAW_FRAME_MAX_PAYLOAD];
    uint16_t len;
};

static struct os_mbuf g_mbuf_pool[4];
static size_t g_mbuf_next = 0;

#define BENCH_MAX_FRAMES 512

static struct {
    uint16_t len;
    uint8_t data[SYNAPSE_RAW_FRAME_MAX_PAYLOAD];
} g_capture[BENCH_MAX_FRAMES];
static int g_capture_count = 0;

int ble_gatts_notify_custom(uint16_t c, uint16_t h, struct os_mbuf *om)
{
    (void)c;
    (void)h;
    if (om == NULL) {
        return 1;  /* BLE_HS_ENOMEM-ish: the service counts a drop */
    }
    if (g_capture_count < BENCH_MAX_FRAMES && om->len <= sizeof(g_capture[0].data)) {
        g_capture[g_capture_count].len = om->len;
        memcpy(g_capture[g_capture_count].data, om->data, om->len);
        g_capture_count++;
    }
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
struct os_mbuf *ble_hs_mbuf_from_flat(const void *d, uint16_t l) {
    struct os_mbuf *om = &g_mbuf_pool[g_mbuf_next];
    g_mbuf_next = (g_mbuf_next + 1) % (sizeof(g_mbuf_pool) / sizeof(g_mbuf_pool[0]));
    if (l > sizeof(om->data)) {
        l = sizeof(om->data);
    }
    memcpy(om->data, d, l);
    om->len = l;
    return om;
}
void os_mbuf_free_chain(struct os_mbuf *m) { (void)m; }
int os_mbuf_append(struct os_mbuf *om, const void *d, uint16_t l) {
    if (om == NULL || (size_t)om->len + l > sizeof(om->data)) {
        return 1;
    }
    memcpy(om->data + om->len, d, l);
    om->len += l;
    return 0;
}
void ble_svc_gap_init(void) {}
void ble_svc_gatt_init(void) {}
const char *esp_err_to_name(esp_err_t e) { (void)e; return "stub"; }
int64_t esp_timer_get_time_monotonic(void) { return esp_timer_get_time(); }

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

    /* Power telemetry must also be safe before init, and must not depend on
     * the scheduler ring (it builds its own single frame). */
    synapse_research_service_submit_power(3700.0f, 120.0f, 80.0f);
    return 0;
}

/* --- behavioural bench ------------------------------------------------- */

/* mode bits: BIT0 keep transmitting (cleared = force set_transmitting(false)),
 *            BIT1 connected (call on_connect),
 *            BIT2 subscribe the biosignal streams (ECG/PPG/ACC/GYRO/MAG),
 *            BIT3 subscribe the POWER stream */
#define BENCH_TRANSMITTING 0x01
#define BENCH_CONNECTED    0x02
#define BENCH_BIO_ENABLED  0x04
#define BENCH_POWER_ENABLED 0x08
#define BENCH_SUBSCRIBE_PATH 0x10  /* route enable through the GATT subscribe
                                      handler instead of set_stream_enabled */

static void bench_submit_ecg(int n)
{
    for (int i = 0; i < n; i++) {
        sensor_sample_t s;
        memset(&s, 0, sizeof(s));
        s.type = SENSOR_TYPE_ECG;
        s.timestamp_us = esp_timer_get_time();
        s.data.ecg.voltage_mv = 0.5f + 0.1f * (float)i;
        synapse_research_service_submit(&s);
    }
}

static void bench_submit_ppg(int n)
{
    for (int i = 0; i < n; i++) {
        sensor_sample_t s;
        memset(&s, 0, sizeof(s));
        s.type = SENSOR_TYPE_PPG;
        s.timestamp_us = esp_timer_get_time();
        s.data.ppg.red = 90000.0f + (float)i;
        s.data.ppg.ir = 105000.0f + (float)i;
        synapse_research_service_submit(&s);
    }
}

static void bench_submit_imu(int n)
{
    for (int i = 0; i < n; i++) {
        sensor_sample_t s;
        memset(&s, 0, sizeof(s));
        s.type = SENSOR_TYPE_IMU;
        s.timestamp_us = esp_timer_get_time();
        s.data.imu.ax = 0.1f; s.data.imu.ay = 0.2f; s.data.imu.az = 9.81f;
        s.data.imu.gx = 0.01f; s.data.imu.gy = 0.02f; s.data.imu.gz = 0.03f;
        s.data.imu.mx = 25.0f; s.data.imu.my = 5.0f; s.data.imu.mz = 40.0f;
        synapse_research_service_submit(&s);
    }
}

/** Reset the service to a known state and clear the capture ring. Returns 0. */
int synapse_host_bench_reset(void)
{
    synapse_research_service_init();
    /* init() is idempotent after the first call, so restore the preconditions
     * explicitly: link down (also drops subscriptions and pending samples),
     * transmitting back on, counters zeroed. */
    synapse_research_service_on_disconnect();
    synapse_research_service_set_transmitting(true);
    synapse_research_service_reset_counters();
    g_capture_count = 0;
    return 0;
}

/** Submit a fixed workload under the given preconditions; return the number
 *  of notifications captured. @p ecg_samples sizes the ECG workload so the
 *  ring-overflow path can be exercised deterministically. */
int synapse_host_bench_run(int mode, int ecg_samples)
{
    g_capture_count = 0;

    if (!(mode & BENCH_TRANSMITTING)) {
        synapse_research_service_set_transmitting(false);
    }
    if (mode & BENCH_CONNECTED) {
        synapse_research_service_on_connect(7);
    }
    if (mode & BENCH_BIO_ENABLED) {
        if (mode & BENCH_SUBSCRIBE_PATH) {
            /* Handles are assigned sequentially from 1 in registration
             * order: ECG=1, PPG=2, ACC=3, GYRO=4, MAG=5, POWER=6, SYNC=7. */
            for (uint16_t h = 1; h <= 5; h++) {
                synapse_research_service_handle_subscribe(h, true);
            }
        } else {
            synapse_research_service_set_stream_enabled(SYNAPSE_RAW_STREAM_ECG, true);
            synapse_research_service_set_stream_enabled(SYNAPSE_RAW_STREAM_PPG, true);
            synapse_research_service_set_stream_enabled(SYNAPSE_RAW_STREAM_ACC, true);
            synapse_research_service_set_stream_enabled(SYNAPSE_RAW_STREAM_GYRO, true);
            synapse_research_service_set_stream_enabled(SYNAPSE_RAW_STREAM_MAG, true);
        }
    }
    if (mode & BENCH_POWER_ENABLED) {
        synapse_research_service_set_stream_enabled(SYNAPSE_RAW_STREAM_POWER, true);
    }

    bench_submit_ecg(ecg_samples);
    bench_submit_ppg(20);   /* > PPG flush threshold (8)  */
    bench_submit_imu(20);   /* > IMU flush threshold (4)  */
    if (mode & BENCH_POWER_ENABLED) {
        synapse_research_service_submit_power(3700.0f, 120.0f, 80.0f);
        synapse_research_service_submit_power(3700.0f, 121.0f, 80.0f);
    }
    return g_capture_count;
}

/** Subscribe path variant: only ECG, routed through handle_subscribe. */
int synapse_host_bench_run_subscribe_ecg(void)
{
    g_capture_count = 0;
    synapse_research_service_on_connect(7);
    synapse_research_service_handle_subscribe(1, true);  /* ECG handle */
    bench_submit_ecg(60);
    bench_submit_ppg(4);    /* below threshold: no PPG frame expected */
    return g_capture_count;
}

int synapse_host_bench_frame_count(void)
{
    return g_capture_count;
}

int synapse_host_bench_frame_len(int i)
{
    if (i < 0 || i >= g_capture_count) {
        return -1;
    }
    return (int)g_capture[i].len;
}

/** Copy frame @p i into @p out (capacity @p cap). Returns bytes copied. */
int synapse_host_bench_copy_frame(int i, uint8_t *out, int cap)
{
    if (i < 0 || i >= g_capture_count || out == NULL) {
        return -1;
    }
    int len = (int)g_capture[i].len;
    if (len > cap) {
        len = cap;
    }
    memcpy(out, g_capture[i].data, (size_t)len);
    return len;
}

uint32_t synapse_host_bench_dropped_frames(void)
{
    return synapse_research_service_dropped_frames();
}

uint32_t synapse_host_bench_ring_overflow(void)
{
    return synapse_research_service_ring_overflow();
}

uint32_t synapse_host_bench_power_dropped(void)
{
    return synapse_research_service_power_dropped();
}
