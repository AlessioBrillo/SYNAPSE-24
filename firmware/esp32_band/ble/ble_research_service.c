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

// Value handles, filled in by ble_gatts_count_cfg/add_svcs at registration.
static uint16_t s_hdl_ecg;
static uint16_t s_hdl_ppg;
static uint16_t s_hdl_acc;
static uint16_t s_hdl_gyro;
static uint16_t s_hdl_mag;
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
    STREAM_COUNT = 5,
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
    uint16_t count = stream->pending;
    uint16_t limit = synapse_raw_max_samples(stream->format);
    if (limit != 0u && count > limit) {
        count = limit;
    }

    // Widen the int32 ring into the wire format's fixed-width samples.
    uint8_t staging[SYNAPSE_RAW_FRAME_MAX_PAYLOAD];
    size_t size = synapse_raw_format_size(stream->format);
    for (uint16_t i = 0; i < (uint16_t)(count * channels); i++) {
        int32_t value = stream->ring[i];
        memcpy(staging + (size_t)i * size, &value, size);
    }

    uint8_t flags = 0;
    if (stream->first_ts_us < r->last_sample_us - (int64_t)UINT32_MAX) {
        // The buffered sample predates the most recent 32-bit wrap.
        flags |= SYNAPSE_RAW_FLAG_TS_EPOCH_WRAPPED;
    }

    int rc = synapse_raw_frame_encode(
        stream->stream_id,
        stream->sequence,
        (uint32_t)((uint64_t)stream->first_ts_us & 0xFFFFFFFFu),
        flags,
        stream->format,
        staging,
        (uint8_t)count,
        s_notify_buf,
        sizeof(s_notify_buf));

    if (rc != SYNAPSE_RAW_OK) {
        r->dropped_frames++;
        ESP_LOGW(TAG, "encode failed for stream 0x%02x: %s", stream->stream_id,
                 synapse_raw_status_str(rc));
    } else {
        size_t frame_len = SYNAPSE_RAW_FRAME_HEADER_SIZE + (size_t)count * channels * size;
        struct os_mbuf *om = ble_hs_mbuf_from_flat(s_notify_buf, (uint16_t)frame_len);
        if (om == NULL) {
            r->dropped_frames++;
        } else if (ble_gatts_notify_custom(r->conn_handle, stream_handle(stream), om) != 0) {
            r->dropped_frames++;
            os_mbuf_free_chain(om);
        } else {
            stream->sequence++;
        }
    }

    uint16_t consumed = (uint16_t)(count * channels);
    uint16_t remaining = (uint16_t)(stream->pending * channels - consumed);
    if (remaining > 0) {
        memmove(stream->ring, &stream->ring[consumed], (size_t)remaining * sizeof(int32_t));
    }
    stream->pending = (uint16_t)(stream->pending - count);
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
        }
        break;
    }
    case SENSOR_TYPE_IMU: {
        synapse_research_stream_t *acc = &s_research.streams[STREAM_ACC];
        synapse_research_stream_t *gyr = &s_research.streams[STREAM_GYRO];
        synapse_research_stream_t *mag = &s_research.streams[STREAM_MAG];
        if (acc->pending >= SYNAPSE_RESEARCH_RING_DEPTH) {
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
    }
    ESP_LOGI(TAG, "connected (handle %u)", conn_handle);
}

void synapse_research_service_on_disconnect(void)
{
    s_research.connected = false;
    s_research.conn_handle = BLE_HS_CONN_HANDLE_NONE;
    for (size_t i = 0; i < s_research.stream_count; i++) {
        s_research.streams[i].pending = 0;
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
