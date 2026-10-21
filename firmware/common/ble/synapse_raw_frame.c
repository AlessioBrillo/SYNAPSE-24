/**
 * @file synapse_raw_frame.c
 * @brief SYNAPSE-24 raw sample frame codec implementation.
 *
 * Pure C99, no platform dependencies: compiled both into the ESP32-S3 firmware
 * and into the CI host build (tests/firmware_host) so that the Python decoder
 * in src/synapse24/hardware/raw_codec.py is verified against the exact same
 * code the pod runs.
 */

#include "synapse_raw_frame.h"

#include <string.h>

uint8_t synapse_raw_format_size(uint8_t format)
{
    switch (format) {
    case SYNAPSE_RAW_FORMAT_S8:
        return 1u;
    case SYNAPSE_RAW_FORMAT_S16:
        return 2u;
    case SYNAPSE_RAW_FORMAT_S32:
        return 4u;
    default:
        return 0u;
    }
}

uint16_t synapse_raw_max_samples(uint8_t format)
{
    uint8_t size = synapse_raw_format_size(format);
    if (size == 0u) {
        return 0u;
    }
    return (uint16_t)((SYNAPSE_RAW_FRAME_MAX_PAYLOAD - SYNAPSE_RAW_FRAME_HEADER_SIZE) / size);
}

int synapse_raw_frame_encode(uint8_t stream_id,
                             uint16_t sequence,
                             uint32_t timestamp_us_lo,
                             uint8_t flags,
                             uint16_t dropped,
                             uint8_t format,
                             const void *samples,
                             uint8_t sample_count,
                             uint8_t *out,
                             size_t out_size)
{
    if (out == NULL) {
        return SYNAPSE_RAW_ERR_NULL;
    }
    if (samples == NULL && sample_count != 0u) {
        return SYNAPSE_RAW_ERR_NULL;
    }

    uint8_t size = synapse_raw_format_size(format);
    if (size == 0u) {
        return SYNAPSE_RAW_ERR_FORMAT;
    }

    if (sample_count == 0u) {
        return SYNAPSE_RAW_ERR_COUNT;
    }
    if ((uint16_t)sample_count > synapse_raw_max_samples(format)) {
        return SYNAPSE_RAW_ERR_COUNT;
    }

    size_t needed = (size_t)SYNAPSE_RAW_FRAME_HEADER_SIZE + (size_t)sample_count * size;
    if (out_size < needed) {
        return SYNAPSE_RAW_ERR_BUFFER;
    }

    out[0] = SYNAPSE_RAW_FRAME_VERSION;
    out[1] = stream_id;
    out[2] = (uint8_t)(sequence & 0xFFu);
    out[3] = (uint8_t)((sequence >> 8) & 0xFFu);
    out[4] = (uint8_t)(timestamp_us_lo & 0xFFu);
    out[5] = (uint8_t)((timestamp_us_lo >> 8) & 0xFFu);
    out[6] = (uint8_t)((timestamp_us_lo >> 16) & 0xFFu);
    out[7] = (uint8_t)((timestamp_us_lo >> 24) & 0xFFu);
    out[8] = flags;
    out[9] = sample_count;
    out[10] = format;
    /* Loss magnitude since the previous transmitted frame (0 = none). The
     * byte is saturated so a long outage cannot wrap around to "no loss". */
    out[11] = (dropped > SYNAPSE_RAW_DROPPED_MAX) ? (uint8_t)SYNAPSE_RAW_DROPPED_MAX
                                                   : dropped;

    if (sample_count != 0u) {
        /* The caller hands us native-endian samples; the wire format is
         * little-endian. On the LE targets we care about this is a memcpy,
         * but doing it explicitly keeps the codec portable and makes the
         * parity test meaningful on any host. */
        const uint8_t *src = (const uint8_t *)samples;
        uint8_t *dst = out + SYNAPSE_RAW_FRAME_HEADER_SIZE;
        for (uint16_t i = 0; i < (uint16_t)sample_count * size; i++) {
            dst[i] = src[i];
        }
    }

    return SYNAPSE_RAW_OK;
}

int synapse_raw_frame_decode(const uint8_t *frame,
                             size_t frame_size,
                             synapse_raw_header_t *header)
{
    if (frame == NULL || header == NULL) {
        return SYNAPSE_RAW_ERR_NULL;
    }
    if (frame_size < SYNAPSE_RAW_FRAME_HEADER_SIZE) {
        return SYNAPSE_RAW_ERR_SHORT;
    }
    if (frame[0] != SYNAPSE_RAW_FRAME_VERSION) {
        return SYNAPSE_RAW_ERR_VERSION;
    }

    uint8_t format = frame[10];
    uint8_t size = synapse_raw_format_size(format);
    if (size == 0u) {
        return SYNAPSE_RAW_ERR_FORMAT;
    }

    uint8_t count = frame[9];
    if (count == 0u) {
        return SYNAPSE_RAW_ERR_COUNT;
    }

    size_t needed = (size_t)SYNAPSE_RAW_FRAME_HEADER_SIZE + (size_t)count * size;
    if (frame_size < needed) {
        return SYNAPSE_RAW_ERR_SHORT;
    }

    header->version = frame[0];
    header->stream_id = frame[1];
    header->sequence = (uint16_t)((uint16_t)frame[2] | ((uint16_t)frame[3] << 8));
    header->timestamp_us_lo =
        (uint32_t)frame[4] | ((uint32_t)frame[5] << 8) | ((uint32_t)frame[6] << 16) | ((uint32_t)frame[7] << 24);
    header->flags = frame[8];
    header->sample_count = count;
    header->sample_format = format;
    header->dropped = frame[11];

    return (int)SYNAPSE_RAW_FRAME_HEADER_SIZE;
}

int synapse_raw_frame_get_sample(const uint8_t *frame,
                                 size_t frame_size,
                                 size_t payload,
                                 size_t index,
                                 int32_t *out)
{
    if (frame == NULL || out == NULL) {
        return SYNAPSE_RAW_ERR_NULL;
    }

    uint8_t size = synapse_raw_format_size(frame[10]);
    if (size == 0u) {
        return SYNAPSE_RAW_ERR_FORMAT;
    }

    size_t needed = (size_t)SYNAPSE_RAW_FRAME_HEADER_SIZE + (size_t)frame[9] * size;
    if (frame_size < needed) {
        return SYNAPSE_RAW_ERR_SHORT;
    }
    if (index >= frame[9]) {
        return SYNAPSE_RAW_ERR_COUNT;
    }

    const uint8_t *src = frame + payload + index * size;
    uint32_t raw = 0;
    for (uint8_t i = 0; i < size; i++) {
        raw |= (uint32_t)src[i] << (8u * i);
    }

    /* Sign-extend from the width of the sample format. */
    uint32_t sign_bit = 1u << (8u * size - 1u);
    uint32_t mask = (size == 4u) ? 0xFFFFFFFFu : ((1u << (8u * size)) - 1u);
    if ((raw & sign_bit) != 0u) {
        raw |= ~mask;
    }

    int32_t value;
    memcpy(&value, &raw, sizeof(value));
    *out = value;
    return SYNAPSE_RAW_OK;
}

const char *synapse_raw_status_str(int status)
{
    switch (status) {
    case SYNAPSE_RAW_OK:
        return "ok";
    case SYNAPSE_RAW_ERR_NULL:
        return "null argument";
    case SYNAPSE_RAW_ERR_VERSION:
        return "unsupported frame version";
    case SYNAPSE_RAW_ERR_STREAM:
        return "unknown stream id";
    case SYNAPSE_RAW_ERR_FORMAT:
        return "unsupported sample format";
    case SYNAPSE_RAW_ERR_COUNT:
        return "invalid sample count";
    case SYNAPSE_RAW_ERR_BUFFER:
        return "output buffer too small";
    case SYNAPSE_RAW_ERR_SHORT:
        return "frame shorter than declared";
    default:
        return "unknown status";
    }
}
