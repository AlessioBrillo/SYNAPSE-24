/**
 * @file ble_research_service.c
 * @brief Synapse Research Service implementation (NimBLE, ESP-IDF 5.x).
 *
 * Design notes:
 *  - No allocation on the hot path: every stream owns a fixed int32 ring.
 *  - `submit()` never blocks. If a notification cannot be sent it is counted in
 *    `dropped_frames`, so the host can detect the hole instead of guessing.
 *  - Frames are encoded by firmware/common/ble/synapse_raw_frame.c, whose
 *    host-parity test pins the wire format byte-for-byte.
 *  - Samples are quantised to fixed engineering units (see
 *    synapse_research_unit_t) so the host needs no runtime calibration
 *    metadata: divide by the same constant documented in this file.
 */

#include <string.h>
#include <math.h>
#include "esp_log.h"
#include "esp_timer.h"
#include "host/ble_hs.h"
#include "host/ble_gatt.h"
#include "host/ble_uuid.h"
#include "host/ble_hs_mbuf.h"
#include "services/gap/ble_svc_gap.h"
#include "services/gatt/ble_svc_gatt.h"

#include "ble_research_service.h"
#include "synapse_raw_frame.h"
#include "host/ble_hs_mbuf.h"

static const char *TAG = "SYNAPSE_RESEARCH";

// UUIDs (little-endian) — must match config/hardware.yaml
// `metadata.research_characteristics` for the forearm_hub pod.
static const ble_uuid128_t RESEARCH_SVC_UUID =
    BLE_UUID128_INIT(0x70, 0x4C, 0x3B, 0x2A, 0x1F, 0x0E, 0x9D, 0x8C,
                     0x7B, 0x4A, 0xF6, 0xE5, 0xD4, 0xC3, 0xB2, 0xA1);
static const ble_uuid128_t RESEARCH_CHAR_ECG_UUID =
    BLE_UUID128_INIT(0x71, 0x4C, 0x3B, 0x2A, 0x1F, 0x0E, 0x9D, 0x8C,
                     0x7B, 0x4A, 0xF6, 0xE5, 0xD4, 0xC3, 0xB2, 0xA1);
static const ble_uuid128_t RESEARCH_CHAR_PPG_UUID =
    BLE_UUID128_INIT(0x72, 0x4C, 0x3B, 0x2A, 0x1F, 0x0E, 0x9D, 0x8C,
                     0x7B, 0x4A, 0xF6, 0xE5, 0xD4, 0xC3, 0xB2, 0xA1);
static const ble_uuid128_t RESEARCH_CHAR_ACC_UUID =
    BLE_UUID128_INIT(0x73, 0x4C, 0x3B, 0x2A, 0x1F, 0x0E, 0x9D, 0x8C,
                     0x7B, 0x4A, 0xF6, 0xE5, 0xD4, 0xC3, 0xB2, 0xA1);
static const ble_uuid128_t RESEARCH_CHAR_GYRO_UUID =
    BLE_UUID128_INIT(0x74, 0x4C, 0x3B, 0x2A, 0x1F, 0x0E, 0x9D, 0x8C,
                     0x7B, 0x4A, 0xF6, 0xE5, 0xD4, 0xC3, 0xB2, 0xA1);
static const ble_uuid128_t RESEARCH_CHAR_MAG_UUID =
    BLE_UUID128_INIT(0x75, 0x4C, 0x3B, 0x2A, 0x1F, 0x0E, 0x9D, 0x8C,
                     0x7B, 0x4A, 0xF6, 0xE5, 0xD4, 0xC3, 0xB2, 0xA1);
static const ble_uuid128_t RESEARCH_CHAR_SYNC_UUID =
    BLE_UUID128_INIT(0x78, 0x4C, 0x3B, 0x2A, 0x1F, 0x0E, 0x9D, 0x8C,
                     0x7B, 0x4A, 0xF6, 0xE5, 0xD4, 0xC3, 0xB2, 0xA1);
// Power telemetry (INA219): voltage_mv, current_ma, power_mw, battery_pct at 1 Hz.
// Declared in config/hardware.yaml `research_characteristics.power`.
static const ble_uuid128_t RESEARCH_CHAR_POWER_UUID =
    BLE_UUID128_INIT(0x79, 0x4C, 0x3B, 0x2A, 0x1F, 0x0E, 0x9D, 0x8C,
                     0x7B, 0x4A, 0xF6, 0xE5, 0xD4, 0xC3, 0xB2, 0xA1);

// Value handles, filled in by ble_gatts_count_cfg/add_svcs at registration.
static uint16_t s_hdl_ecg;
static uint16_t s_hdl_ppg;
static uint16_t s_hdl_acc;
static uint16_t s_hdl_gyro;
static uint16_t s_hdl_mag;
static uint16_t s_hdl_power;
static uint16_t s_hdl_sync;

// Clock-sync response staged by the write path and read back by the host.
static uint8_t s_sync_response[SYNAPSE_SYNC_RESPONSE_SIZE];

// Notification staging buffer (one frame at a time).
static uint8_t s_notify_buf[SYNAPSE_RAW_FRAME_MAX_PAYLOAD];

static synapse_research_t s_research = {0};

// Stream indices into s_research.streams[].
enum {
    STREAM_ECG = 0,
    STREAM_PPG = 1,
    STREAM_ACC = 2,
    STREAM_GYRO = 3,
    STREAM_MAG = 4,
    STREAM_POWER = 5,
    STREAM_COUNT = 6,
};

// ---------------------------------------------------------------------------
// GATT access callbacks
// ---------------------------------------------------------------------------

static int stream_access(uint16_t conn, uint16_t attr, struct ble_gatt_access_ctxt *ctxt, void *arg);

/**
 * Read-after-write clock sync exchange.
 *
 * The host writes [seq][hub_send_us] and immediately reads back
 * [seq][pod_recv_us][pod_send_us]. Solving NTP-style with both directions
 * gives the offset without an external time source.
 */
static int sync_access(uint16_t conn, uint16_t attr, struct ble_gatt_access_ctxt *ctxt, void *arg)
{
    (void)conn;
    (void)attr;
    (void)arg;

    if (ctxt->op == BLE_GATT_ACCESS_OP_READ_CHR) {
        int rc = os_mbuf_append(ctxt->om, s_sync_response, sizeof(s_sync_response));
        return rc == 0 ? 0 : BLE_ATT_ERR_INSUFFICIENT_RES;
    }

    if (ctxt->op == BLE_GATT_ACCESS_OP_WRITE_CHR) {
        uint8_t req[SYNAPSE_SYNC_REQUEST_SIZE];
        uint16_t len = 0;
        int rc = ble_hs_mbuf_to_flat(ctxt->om, req, sizeof(req), &len);
        if (rc != 0 || len != SYNAPSE_SYNC_REQUEST_SIZE) {
            return BLE_ATT_ERR_INVALID_ATTR_VALUE_LEN;
        }

        int64_t recv_us = esp_timer_get_time();
        memcpy(s_sync_response, req, sizeof(uint32_t));  // echo the sequence
        memcpy(s_sync_response + 4, &recv_us, sizeof(int64_t));
        int64_t send_us = esp_timer_get_time();
        memcpy(s_sync_response + 12, &send_us, sizeof(int64_t));
        return 0;
    }

    return BLE_ATT_ERR_UNLIKELY;
}

// ---------------------------------------------------------------------------
// GATT database
// ---------------------------------------------------------------------------

static int stream_access(uint16_t conn, uint16_t attr, struct ble_gatt_access_ctxt *ctxt, void *arg)
{
    // Notify-only characteristics: the value lives in the stream ring, not in
    // the GATT database, so reads are refused rather than faked.
    (void)conn;
    (void)attr;
    (void)ctxt;
    (void)arg;
    return BLE_ATT_ERR_READ_NOT_PERMITTED;
}

static const struct ble_gatt_svc_def research_svcs[] = {
    {
        .type = BLE_GATT_SVC_TYPE_PRIMARY,
        .uuid = &RESEARCH_SVC_UUID.u,
        .characteristics = (struct ble_gatt_chr_def[]){
            {
                .uuid = &RESEARCH_CHAR_ECG_UUID.u,
                .access_cb = stream_access,
                .arg = &s_research.streams[STREAM_ECG],
                .flags = BLE_GATT_CHR_F_NOTIFY,
                .val_handle = &s_hdl_ecg,
            },
            {
                .uuid = &RESEARCH_CHAR_PPG_UUID.u,
                .access_cb = stream_access,
                .arg = &s_research.streams[STREAM_PPG],
                .flags = BLE_GATT_CHR_F_NOTIFY,
                .val_handle = &s_hdl_ppg,
            },
            {
                .uuid = &RESEARCH_CHAR_ACC_UUID.u,
                .access_cb = stream_access,
                .arg = &s_research.streams[STREAM_ACC],
                .flags = BLE_GATT_CHR_F_NOTIFY,
                .val_handle = &s_hdl_acc,
            },
            {
                .uuid = &RESEARCH_CHAR_GYRO_UUID.u,
                .access_cb = stream_access,
                .arg = &s_research.streams[STREAM_GYRO],
                .flags = BLE_GATT_CHR_F_NOTIFY,
                .val_handle = &s_hdl_gyro,
            },
            {
                .uuid = &RESEARCH_CHAR_MAG_UUID.u,
                .access_cb = stream_access,
                .arg = &s_research.streams[STREAM_MAG],
                .flags = BLE_GATT_CHR_F_NOTIFY,
                .val_handle = &s_hdl_mag,
            },
            {
                .uuid = &RESEARCH_CHAR_POWER_UUID.u,
                .access_cb = stream_access,
                .arg = &s_research.streams[STREAM_POWER],
                .flags = BLE_GATT_CHR_F_NOTIFY,
                .val_handle = &s_hdl_power,
            },
            {
                .uuid = &RESEARCH_CHAR_SYNC_UUID.u,
                .access_cb = sync_access,
                .flags = BLE_GATT_CHR_F_READ | BLE_GATT_CHR_F_WRITE,
                .val_handle = &s_hdl_sync,
            },
            {0},
        },
    },
    {0},
};

// ---------------------------------------------------------------------------
// Stream bookkeeping
// ---------------------------------------------------------------------------

static void init_stream_table(synapse_research_t *r)
{
    // Rates and units follow config/hardware.yaml and the ICM-20948 driver
    // (accel in g, gyro in rad/s, mag in uT) and the AD8232 driver (mV).
    memset(r->streams, 0, sizeof(r->streams));

    // ECG 500 Hz, 1 ch, uV -> a frame every 100 ms.
    r->streams[STREAM_ECG] = (synapse_research_stream_t){
        .stream_id = SYNAPSE_RAW_STREAM_ECG,
        .format = SYNAPSE_RAW_FORMAT_S16,
        .channels = 1,
        .unit_scale = SYNAPSE_RESEARCH_UNIT_UV,
        .flush_at = 50,
    };
    // PPG 50 Hz, 2 ch (red + IR), raw ADC codes -> ~160 ms.
    r->streams[STREAM_PPG] = (synapse_research_stream_t){
        .stream_id = SYNAPSE_RAW_STREAM_PPG,
        .format = SYNAPSE_RAW_FORMAT_S32,
        .channels = 2,
        .unit_scale = SYNAPSE_RESEARCH_UNIT_COUNT,
        .flush_at = 8,
    };
    // IMU 50 Hz, 3 ch per sub-stream -> ~80 ms.
    r->streams[STREAM_ACC] = (synapse_research_stream_t){
        .stream_id = SYNAPSE_RAW_STREAM_ACC,
        .format = SYNAPSE_RAW_FORMAT_S16,
        .channels = 3,
        .unit_scale = SYNAPSE_RESEARCH_UNIT_MG,
        .flush_at = 4,
    };
    r->streams[STREAM_GYRO] = (synapse_research_stream_t){
        .stream_id = SYNAPSE_RAW_STREAM_GYRO,
        .format = SYNAPSE_RAW_FORMAT_S16,
        .channels = 3,
        .unit_scale = SYNAPSE_RESEARCH_UNIT_MRAD_S,
        .flush_at = 4,
    };
    r->streams[STREAM_MAG] = (synapse_research_stream_t){
        .stream_id = SYNAPSE_RAW_STREAM_MAG,
        .format = SYNAPSE_RAW_FORMAT_S16,
        .channels = 3,
        .unit_scale = SYNAPSE_RESEARCH_UNIT_CUT,
        .flush_at = 4,
    };
    // Power telemetry 1 Hz, 4 channels (voltage_mv, current_ma, power_mw,
    // battery_pct): one sample per frame, no buffering.
    r->streams[STREAM_POWER] = (synapse_research_stream_t){
        .stream_id = SYNAPSE_RAW_STREAM_POWER,
        .format = SYNAPSE_RAW_FORMAT_S32,
        .channels = 4,
        .unit_scale = SYNAPSE_RESEARCH_UNIT_MILLI,
        .flush_at = 1,
    };
    r->stream_count = STREAM_COUNT;
}

/** Resolve the value handle for a stream. */
static uint16_t stream_handle(const synapse_research_stream_t *stream)
{
    if (stream == &s_research.streams[STREAM_ECG]) return s_hdl_ecg;
    if (stream == &s_research.streams[STREAM_PPG]) return s_hdl_ppg;
    if (stream == &s_research.streams[STREAM_ACC]) return s_hdl_acc;
    if (stream == &s_research.streams[STREAM_GYRO]) return s_hdl_gyro;
    if (stream == &s_research.streams[STREAM_MAG]) return s_hdl_mag;
    if (stream == &s_research.streams[STREAM_POWER]) return s_hdl_power;
    return 0;
}

/** Clamp a float to the int16 range of the frame format. */
static int32_t quantise_s16(float value, float scale)
{
    float scaled = value * scale;
    if (scaled > 32767.0f) scaled = 32767.0f;
    if (scaled < -32768.0f) scaled = -32768.0f;
    return (int32_t)lroundf(scaled);
}

/**
 * @brief Account @p samples lost for @p stream.
 *
 * The loss is carried by the next frame's `dropped` header byte (the byte
 * that used to be a reserved field), so the host learns the magnitude of the
 * hole its sequence-gap detection found. The counter saturates at the header
 * limit: a byte cannot express more than 255 lost samples.
 */
static void research_note_drop(synapse_research_stream_t *stream, uint16_t samples)
{
    uint32_t total = (uint32_t)stream->dropped_pending + (uint32_t)samples;
    stream->dropped_pending = (uint16_t)(total > SYNAPSE_RAW_DROPPED_MAX
                                             ? (uint32_t)SYNAPSE_RAW_DROPPED_MAX
                                             : total);
}

/** Emit one frame for @p stream, draining up to its flush threshold. */
static void flush_stream(synapse_research_t *r, synapse_research_stream_t *stream)
{
    if (!r->transmitting || !stream->enabled || stream->pending == 0) {
        return;
    }
    if (r->conn_handle == BLE_HS_CONN_HANDLE_NONE) {
        return;
    }

    size_t channels = stream->channels ? stream->channels : 1;
    /* The wire codec's sample_count counts VALUES (channel-minor), not
     * physical samples: PPG red+IR is one sample but two values, the IMU
     * sub-streams one sample but three. Counting physical samples here made
     * the host decoder read only 1/channels of every multi-channel frame —
     * the C<->Python parity tests never caught it because they exercise the
     * codec directly, and only ECG (1 channel) was unaffected on the wire. */
    uint16_t samples = stream->pending;
    uint16_t limit_values = synapse_raw_max_samples(stream->format);
    if (limit_values != 0u && (uint16_t)(samples * channels) > limit_values) {
        samples = (uint16_t)(limit_values / channels);
    }
    uint16_t count = (uint16_t)(samples * channels);

    // Widen the int32 ring into the wire format's fixed-width samples.
    uint8_t staging[SYNAPSE_RAW_FRAME_MAX_PAYLOAD];
    size_t size = synapse_raw_format_size(stream->format);
    for (uint16_t i = 0; i < count; i++) {
        int32_t value = stream->ring[i];
        memcpy(staging + (size_t)i * size, &value, size);
    }

    uint8_t flags = 0;
    // Wrap witness: the frame's low-32 timestamp stepping backwards against
    // the previous frame of THIS stream. The old comparison against a 71.6
    // minute-old buffer could never fire, which made the flag dead code and
    // left the host relying on the numeric regression alone.
    uint32_t ts_low = (uint32_t)((uint64_t)stream->first_ts_us & 0xFFFFFFFFu);
    if (stream->has_flushed && ts_low < stream->last_flushed_ts_low) {
        flags |= SYNAPSE_RAW_FLAG_TS_EPOCH_WRAPPED;
    }
    stream->last_flushed_ts_low = ts_low;
    stream->has_flushed = true;

    /* Loss since the previous transmitted frame travels in this frame's
     * `dropped` byte (the byte that used to be reserved): the host learns
     * how much data it will never see, not only that something is missing. */
    uint8_t dropped = (stream->dropped_pending > SYNAPSE_RAW_DROPPED_MAX)
                          ? (uint8_t)SYNAPSE_RAW_DROPPED_MAX
                          : (uint8_t)stream->dropped_pending;

    /* The sequence number is consumed when the frame is BUILT, not when the
     * notification is accepted: a frame the BLE stack refuses must leave a
     * hole in the sequence, otherwise the host sees an uninterrupted stream
     * and cannot detect the loss at all. */
    uint16_t sequence = stream->sequence++;

    int rc = synapse_raw_frame_encode(
        stream->stream_id,
        sequence,
        (uint32_t)((uint64_t)stream->first_ts_us & 0xFFFFFFFFu),
        flags,
        dropped,
        stream->format,
        staging,
        (uint8_t)count,
        s_notify_buf,
        sizeof(s_notify_buf));

    if (rc != SYNAPSE_RAW_OK) {
        r->dropped_frames++;
        research_note_drop(stream, count);
        ESP_LOGW(TAG, "encode failed for stream 0x%02x: %s", stream->stream_id,
                 synapse_raw_status_str(rc));
    } else {
        size_t frame_len = SYNAPSE_RAW_FRAME_HEADER_SIZE + (size_t)count * channels * size;
        struct os_mbuf *om = ble_hs_mbuf_from_flat(s_notify_buf, (uint16_t)frame_len);
        if (om == NULL) {
            r->dropped_frames++;
            research_note_drop(stream, count);
        } else if (ble_gatts_notify_custom(r->conn_handle, stream_handle(stream), om) != 0) {
            r->dropped_frames++;
            research_note_drop(stream, count);
            os_mbuf_free_chain(om);
        } else {
            /* Transmitted: the loss counter has reached the host, reset it. */
            stream->dropped_pending = 0;
        }
    }

    uint16_t consumed = count;  /* ring values consumed by this frame */
    uint16_t remaining = (uint16_t)(stream->pending * channels - consumed);
    if (remaining > 0) {
        memmove(stream->ring, &stream->ring[consumed], (size_t)remaining * sizeof(int32_t));
    }
    stream->pending = (uint16_t)(stream->pending - samples);
    stream->first_ts_us = r->last_sample_us;
}

// ---------------------------------------------------------------------------
// Public API
// ---------------------------------------------------------------------------

esp_err_t synapse_research_service_init(void)
{
    if (s_research.initialized) {
        return ESP_OK;
    }

    memset(&s_research, 0, sizeof(s_research));
    s_research.conn_handle = BLE_HS_CONN_HANDLE_NONE;
    init_stream_table(&s_research);
    // The service exists to transmit: leave it armed so a disabled default can
    // never again mean "the pod streams nothing and nothing complains". The
    // remaining preconditions are connect + per-stream subscription, wired in
    // ble_gatt_server.c and proven by tests/test_research_service_host.py.
    s_research.transmitting = true;

    int rc = ble_gatts_count_cfg(research_svcs);
    if (rc != 0) {
        ESP_LOGE(TAG, "ble_gatts_count_cfg failed: %d", rc);
        return ESP_FAIL;
    }
    rc = ble_gatts_add_svcs(research_svcs);
    if (rc != 0) {
        ESP_LOGE(TAG, "ble_gatts_add_svcs failed: %d", rc);
        return ESP_FAIL;
    }

    s_research.initialized = true;
    ESP_LOGI(TAG, "research service registered (%zu streams, raw samples only)",
             s_research.stream_count);
    return ESP_OK;
}

void synapse_research_service_submit(const sensor_sample_t *sample)
{
    if (sample == NULL || !s_research.initialized) {
        return;
    }

    int64_t now_us = sample->timestamp_us;
    if (now_us < s_research.last_sample_us) {
        return;  // stale sample, esp_timer is monotonic
    }
    if ((uint32_t)now_us < (uint32_t)s_research.last_sample_us) {
        s_research.epoch_wraps++;
    }
    s_research.last_sample_us = now_us;

    switch (sample->type) {
    case SENSOR_TYPE_ECG: {
        synapse_research_stream_t *s = &s_research.streams[STREAM_ECG];
        if (s->pending < SYNAPSE_RESEARCH_RING_DEPTH) {
            if (s->pending == 0) {
                s->first_ts_us = now_us;
            }
            s->ring[s->pending++] = quantise_s16(sample->data.ecg.voltage_mv,
                                                 (float)SYNAPSE_RESEARCH_UNIT_UV);
        } else {
            // Ring full: a notification could not be emitted (BLE budget).
            // The host's sequence-gap check will NOT see this, so count it
            // for the bench diagnostics instead of losing it silently, and
            // report its magnitude in the next frame's `dropped` byte.
            s_research.ring_overflow++;
            research_note_drop(s, 1u);
        }
        break;
    }
    case SENSOR_TYPE_PPG: {
        synapse_research_stream_t *s = &s_research.streams[STREAM_PPG];
        if (s->pending < SYNAPSE_RESEARCH_RING_DEPTH) {
            if (s->pending == 0) {
                s->first_ts_us = now_us;
            }
            s->ring[s->pending * 2 + 0] = (int32_t)lroundf(sample->data.ppg.red);
            s->ring[s->pending * 2 + 1] = (int32_t)lroundf(sample->data.ppg.ir);
            s->pending++;
        } else {
            s_research.ring_overflow++;
            research_note_drop(s, 1u);
        }
        break;
    }
    case SENSOR_TYPE_IMU: {
        synapse_research_stream_t *acc = &s_research.streams[STREAM_ACC];
        synapse_research_stream_t *gyr = &s_research.streams[STREAM_GYRO];
        synapse_research_stream_t *mag = &s_research.streams[STREAM_MAG];
        if (acc->pending >= SYNAPSE_RESEARCH_RING_DEPTH) {
            s_research.ring_overflow++;
            research_note_drop(acc, 1u);
            research_note_drop(gyr, 1u);
            research_note_drop(mag, 1u);
            return;
        }
        if (acc->pending == 0) {
            acc->first_ts_us = now_us;
            gyr->first_ts_us = now_us;
            mag->first_ts_us = now_us;
        }
        // ICM-20948 driver reports g / rad/s / uT (see imu_icm20948.cpp).
        acc->ring[acc->pending * 3 + 0] = quantise_s16(sample->data.imu.ax, (float)SYNAPSE_RESEARCH_UNIT_MG);
        acc->ring[acc->pending * 3 + 1] = quantise_s16(sample->data.imu.ay, (float)SYNAPSE_RESEARCH_UNIT_MG);
        acc->ring[acc->pending * 3 + 2] = quantise_s16(sample->data.imu.az, (float)SYNAPSE_RESEARCH_UNIT_MG);
        gyr->ring[gyr->pending * 3 + 0] = quantise_s16(sample->data.imu.gx, (float)SYNAPSE_RESEARCH_UNIT_MRAD_S);
        gyr->ring[gyr->pending * 3 + 1] = quantise_s16(sample->data.imu.gy, (float)SYNAPSE_RESEARCH_UNIT_MRAD_S);
        gyr->ring[gyr->pending * 3 + 2] = quantise_s16(sample->data.imu.gz, (float)SYNAPSE_RESEARCH_UNIT_MRAD_S);
        mag->ring[mag->pending * 3 + 0] = quantise_s16(sample->data.imu.mx, (float)SYNAPSE_RESEARCH_UNIT_CUT);
        mag->ring[mag->pending * 3 + 1] = quantise_s16(sample->data.imu.my, (float)SYNAPSE_RESEARCH_UNIT_CUT);
        mag->ring[mag->pending * 3 + 2] = quantise_s16(sample->data.imu.mz, (float)SYNAPSE_RESEARCH_UNIT_CUT);
        acc->pending++;
        gyr->pending++;
        mag->pending++;
        break;
    }
    default:
        return;
    }

    for (size_t i = 0; i < s_research.stream_count; i++) {
        synapse_research_stream_t *s = &s_research.streams[i];
        if (s->pending >= s->flush_at) {
            flush_stream(&s_research, s);
        }
    }
}

void synapse_research_service_submit_power(float voltage_mv, float current_ma, float battery_pct)
{
    if (!s_research.initialized) {
        return;
    }

    synapse_research_stream_t *s = &s_research.streams[STREAM_POWER];
    // Nothing can leave the pod without a link, transmitting on, and a
    // subscriber. When any of those is missing, drop the 1 Hz sample into the
    // counter instead of parking it in `pending`, where it would block every
    // later sample with "a sample is still unflushed" and make the first
    // seconds after subscription look like data loss.
    if (!s_research.transmitting || !s->enabled) {
        s_research.power_dropped++;
        return;
    }
    if (s->pending != 0) {
        s_research.power_dropped++;  // 1 Hz: previous frame still unflushed
        return;
    }
    s->first_ts_us = esp_timer_get_time();
    // Four channels in one frame: voltage_mv, current_ma, power_mw,
    // battery_pct. Power is derived here rather than sent separately so the
    // host's energy accounting is one multiplication, not three fields to
    // reconcile. The ring carries up to RING_DEPTH * 3 int32 values, and a
    // single 4-channel sample fits well inside that.
    float power_mw = voltage_mv * current_ma / 1000.0f;
    s->ring[0] = quantise_s16(voltage_mv, (float)SYNAPSE_RESEARCH_UNIT_MILLI);
    s->ring[1] = quantise_s16(current_ma, (float)SYNAPSE_RESEARCH_UNIT_MILLI);
    s->ring[2] = quantise_s16(power_mw, (float)SYNAPSE_RESEARCH_UNIT_MILLI);
    s->ring[3] = quantise_s16(battery_pct, (float)SYNAPSE_RESEARCH_UNIT_MILLI);
    s->pending = 1;

    flush_stream(&s_research, s);
}

esp_err_t synapse_research_service_set_transmitting(bool enable)
{
    s_research.transmitting = enable;
    if (!enable) {
        for (size_t i = 0; i < s_research.stream_count; i++) {
            s_research.streams[i].pending = 0;
        }
    }
    ESP_LOGI(TAG, "raw transmission %s", enable ? "enabled" : "disabled");
    return ESP_OK;
}

void synapse_research_service_on_connect(uint16_t conn_handle)
{
    s_research.connected = true;
    s_research.conn_handle = conn_handle;
    s_research.mtu = 23;
    for (size_t i = 0; i < s_research.stream_count; i++) {
        s_research.streams[i].sequence = 0;
        s_research.streams[i].pending = 0;
        s_research.streams[i].dropped_pending = 0;
    }
    ESP_LOGI(TAG, "connected (handle %u)", conn_handle);
}

void synapse_research_service_on_disconnect(void)
{
    s_research.connected = false;
    s_research.conn_handle = BLE_HS_CONN_HANDLE_NONE;
    for (size_t i = 0; i < s_research.stream_count; i++) {
        // Subscriptions die with the link: pending samples are stale, and the
        // host must re-subscribe before anything streams again.
        s_research.streams[i].enabled = false;
        s_research.streams[i].pending = 0;
        // The sequence restarts at 0 on the next connect, so the loss
        // counter must restart with it: a stale count would be reported as
        // a loss the new host never suffered.
        s_research.streams[i].dropped_pending = 0;
    }
    ESP_LOGI(TAG, "disconnected");
}

uint16_t synapse_research_service_get_mtu(void)
{
    return s_research.connected ? s_research.mtu : 0;
}

uint32_t synapse_research_service_dropped_frames(void)
{
    return s_research.dropped_frames;
}

void synapse_research_service_set_stream_enabled(uint8_t stream_id, bool enabled)
{
    if (!s_research.initialized) {
        return;
    }
    for (size_t i = 0; i < s_research.stream_count; i++) {
        synapse_research_stream_t *s = &s_research.streams[i];
        if (s->stream_id == stream_id) {
            s->enabled = enabled;
            if (!enabled) {
                // Drop the pending buffer: stale samples must not be flushed as
                // soon as the host re-subscribes.
                s->pending = 0;
            }
            ESP_LOGI(TAG, "stream 0x%02x %s", stream_id,
                     enabled ? "enabled (subscribed)" : "disabled");
            return;
        }
    }
    ESP_LOGW(TAG, "stream 0x%02x not found in the stream table", stream_id);
}

void synapse_research_service_handle_subscribe(uint16_t attr_handle, bool notify)
{
    if (!s_research.initialized) {
        return;
    }
    for (size_t i = 0; i < s_research.stream_count; i++) {
        synapse_research_stream_t *s = &s_research.streams[i];
        uint16_t handle = stream_handle(s);
        if (handle != 0 && handle == attr_handle) {
            synapse_research_service_set_stream_enabled(s->stream_id, notify);
            return;
        }
    }
    // Handles outside the research service (feature stream, provisioning) are
    // not ours: ignore them.
}

void synapse_research_service_set_mtu(uint16_t mtu)
{
    s_research.mtu = mtu;
    ESP_LOGI(TAG, "negotiated ATT MTU: %u bytes", mtu);
}

uint32_t synapse_research_service_ring_overflow(void)
{
    return s_research.ring_overflow;
}

uint32_t synapse_research_service_power_dropped(void)
{
    return s_research.power_dropped;
}

void synapse_research_service_reset_counters(void)
{
    s_research.dropped_frames = 0;
    s_research.ring_overflow = 0;
    s_research.power_dropped = 0;
}
