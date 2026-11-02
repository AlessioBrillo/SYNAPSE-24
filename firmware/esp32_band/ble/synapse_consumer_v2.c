#include "synapse_consumer_v2.h"

#include <string.h>

static void put_u16(uint8_t *p, uint16_t v)
{
    p[0] = (uint8_t)(v & 0xFFu);
    p[1] = (uint8_t)(v >> 8);
}

static void put_u32(uint8_t *p, uint32_t v)
{
    put_u16(p, (uint16_t)(v & 0xFFFFu));
    put_u16(p + 2, (uint16_t)(v >> 16));
}

void synapse_feature_v2_init_na(synapse_feature_v2_t *f)
{
    memset(f, 0, sizeof(*f));
    f->heart_rate_bpm = SYNAPSE_CONTRACT_V2_FEATURE_NA_HEART_RATE_BPM;
    f->rr_last = SYNAPSE_CONTRACT_V2_FEATURE_NA_RR_LAST;
    f->rr_count = SYNAPSE_CONTRACT_V2_FEATURE_NA_RR_COUNT;
    f->ppg_sqi = SYNAPSE_CONTRACT_V2_FEATURE_NA_PPG_SQI;
    f->ecg_quality = SYNAPSE_CONTRACT_V2_FEATURE_NA_ECG_QUALITY;
    f->motion_level = SYNAPSE_CONTRACT_V2_FEATURE_NA_MOTION_LEVEL;
    f->battery_pct = SYNAPSE_CONTRACT_V2_FEATURE_NA_BATTERY_PCT;
    f->temperature_c100 = (int16_t)SYNAPSE_CONTRACT_V2_FEATURE_NA_TEMPERATURE_C100;
    f->rmssd_ms = SYNAPSE_CONTRACT_V2_FEATURE_NA_RMSSD_MS;
    f->sdnn_ms = SYNAPSE_CONTRACT_V2_FEATURE_NA_SDNN_MS;
    f->stress_level = 3; /* unknown: the model has not run */
}

size_t synapse_feature_v2_encode(const synapse_feature_v2_t *f, uint8_t *out, size_t cap)
{
    if (f == NULL || out == NULL || cap < SYNAPSE_CONTRACT_V2_FEATURE_BYTES) {
        return 0;
    }
    memset(out, 0, SYNAPSE_CONTRACT_V2_FEATURE_BYTES);
    out[SYNAPSE_CONTRACT_V2_FEATURE_OFF_VERSION] = SYNAPSE_CONTRACT_V2_FEATURE_VERSION;
    out[SYNAPSE_CONTRACT_V2_FEATURE_OFF_FLAGS] = f->flags;
    put_u16(out + SYNAPSE_CONTRACT_V2_FEATURE_OFF_SEQUENCE, f->sequence);
    put_u32(out + SYNAPSE_CONTRACT_V2_FEATURE_OFF_T_DEV_MS, f->t_dev_ms);
    put_u16(out + SYNAPSE_CONTRACT_V2_FEATURE_OFF_HEART_RATE_BPM, f->heart_rate_bpm);
    put_u16(out + SYNAPSE_CONTRACT_V2_FEATURE_OFF_RR_LAST, f->rr_last);
    out[SYNAPSE_CONTRACT_V2_FEATURE_OFF_RR_COUNT] = f->rr_count;
    out[SYNAPSE_CONTRACT_V2_FEATURE_OFF_PPG_SQI] = f->ppg_sqi;
    out[SYNAPSE_CONTRACT_V2_FEATURE_OFF_ECG_QUALITY] = f->ecg_quality;
    out[SYNAPSE_CONTRACT_V2_FEATURE_OFF_MOTION_LEVEL] = f->motion_level;
    out[SYNAPSE_CONTRACT_V2_FEATURE_OFF_STRESS_LEVEL] = f->stress_level;
    out[SYNAPSE_CONTRACT_V2_FEATURE_OFF_BATTERY_PCT] = f->battery_pct;
    put_u16(out + SYNAPSE_CONTRACT_V2_FEATURE_OFF_TEMPERATURE_C100, (uint16_t)f->temperature_c100);
    put_u16(out + SYNAPSE_CONTRACT_V2_FEATURE_OFF_RMSSD_MS, f->rmssd_ms);
    put_u16(out + SYNAPSE_CONTRACT_V2_FEATURE_OFF_SDNN_MS, f->sdnn_ms);
    out[SYNAPSE_CONTRACT_V2_FEATURE_OFF_SPORT_TYPE] = f->sport_type;
    out[SYNAPSE_CONTRACT_V2_FEATURE_OFF_SENSOR_HEALTH] = f->sensor_health;
    return SYNAPSE_CONTRACT_V2_FEATURE_BYTES;
}

size_t synapse_status_v2_encode(const synapse_status_v2_t *s, uint8_t *out, size_t cap)
{
    if (s == NULL || out == NULL || cap < SYNAPSE_CONTRACT_V2_STATUS_BYTES) {
        return 0;
    }
    memset(out, 0, SYNAPSE_CONTRACT_V2_STATUS_BYTES);
    out[SYNAPSE_CONTRACT_V2_STATUS_OFF_VERSION] = SYNAPSE_CONTRACT_V2_STATUS_VERSION;
    out[SYNAPSE_CONTRACT_V2_STATUS_OFF_TIER] = s->tier;
    out[SYNAPSE_CONTRACT_V2_STATUS_OFF_SENSOR_HEALTH] = s->sensor_health;
    out[SYNAPSE_CONTRACT_V2_STATUS_OFF_FLAGS] = s->flags;
    put_u16(out + SYNAPSE_CONTRACT_V2_STATUS_OFF_MTU, s->mtu);
    put_u32(out + SYNAPSE_CONTRACT_V2_STATUS_OFF_UPTIME_S, s->uptime_s);
    put_u16(out + SYNAPSE_CONTRACT_V2_STATUS_OFF_EPOCH_WRAPS, s->epoch_wraps);
    put_u32(out + SYNAPSE_CONTRACT_V2_STATUS_OFF_RING_OVERFLOW, s->ring_overflow);
    put_u32(out + SYNAPSE_CONTRACT_V2_STATUS_OFF_STALE_SAMPLES, s->stale_samples);
    put_u32(out + SYNAPSE_CONTRACT_V2_STATUS_OFF_DROPPED_FRAMES, s->dropped_frames);
    put_u32(out + SYNAPSE_CONTRACT_V2_STATUS_OFF_READ_ERRORS, s->read_errors);
    put_u32(out + SYNAPSE_CONTRACT_V2_STATUS_OFF_FW_BUILD, s->fw_build);
    return SYNAPSE_CONTRACT_V2_STATUS_BYTES;
}

size_t synapse_stream_metadata_encode(uint8_t *out, size_t cap)
{
    static const uint16_t table[SYNAPSE_CONTRACT_V2_STREAM_COUNT][6] = {
        SYNAPSE_CONTRACT_V2_STREAM_TABLE};
    const size_t total = 1u + (size_t)SYNAPSE_CONTRACT_V2_STREAM_COUNT * SYNAPSE_CONTRACT_V2_META_ENTRY_BYTES;
    if (out == NULL || cap < total) {
        return 0;
    }
    out[0] = SYNAPSE_CONTRACT_V2_STREAM_COUNT;
    for (size_t i = 0; i < SYNAPSE_CONTRACT_V2_STREAM_COUNT; i++) {
        uint8_t *e = out + 1u + i * SYNAPSE_CONTRACT_V2_META_ENTRY_BYTES;
        e[0] = (uint8_t)table[i][0]; /* stream_id */
        e[1] = (uint8_t)table[i][1]; /* format */
        e[2] = (uint8_t)table[i][2]; /* channels */
        e[3] = (uint8_t)table[i][3]; /* unit */
        put_u16(e + 4, table[i][4]); /* rate_hz */
        put_u16(e + 6, table[i][5]); /* unit_scale */
    }
    return total;
}

int synapse_control_apply(const uint8_t *data, size_t len, const synapse_control_ops_t *ops)
{
    if (data == NULL || len < 1u || ops == NULL) {
        return SYNAPSE_CONTRACT_V2_ERR_BAD_LENGTH;
    }
    switch (data[0]) {
    case SYNAPSE_CONTRACT_V2_OP_START:
    case SYNAPSE_CONTRACT_V2_OP_STOP:
        if (len != 1u) {
            return SYNAPSE_CONTRACT_V2_ERR_BAD_LENGTH;
        }
        if (ops->set_transmitting != NULL) {
            ops->set_transmitting(data[0] == SYNAPSE_CONTRACT_V2_OP_START, ops->ctx);
        }
        return 0;
    case SYNAPSE_CONTRACT_V2_OP_SET_FEATURE_RATE:
        if (len != 2u) {
            return SYNAPSE_CONTRACT_V2_ERR_BAD_LENGTH;
        }
        if (data[1] < 1u || data[1] > SYNAPSE_CONTRACT_V2_FEATURE_RATE_MAX_HZ) {
            return SYNAPSE_CONTRACT_V2_ERR_BAD_ARGUMENT;
        }
        if (ops->set_feature_rate != NULL) {
            ops->set_feature_rate(data[1], ops->ctx);
        }
        return 0;
    default:
        return SYNAPSE_CONTRACT_V2_ERR_BAD_OPCODE;
    }
}
