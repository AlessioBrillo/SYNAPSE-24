/**
 * @file synapse_raw_frame.h
 * @brief SYNAPSE-24 raw sample frame codec (BLE notification payload).
 *
 * Wire format for the Synapse Research Service. This module is deliberately
 * free of any ESP-IDF / FreeRTOS dependency so that the exact bytes the pod
 * transmits can be compiled and fuzzed on the CI host, and compared against
 * the Python decoder in src/synapse24/hardware/raw_codec.py.
 *
 * Design constraints (Architecture.md §55-62 "compress before transmit"):
 *   - fits in a single BLE notification (default MTU 247 -> 244 usable bytes);
 *   - carries raw samples only, zero derived features: the host owns the whole
 *     quality/fusion pipeline and the dataset stays complete;
 *   - carries a device timestamp so multi-pod alignment does not depend on the
 *     BLE connection event timing;
 *   - single codec for both ends, so firmware and host cannot drift.
 *
 * Frame layout (little-endian, packed):
 *
 *   off  size  field
 *     0     1  version          (SYNAPSE_RAW_FRAME_VERSION)
 *     1     1  stream_id        (synapse_raw_stream_t)
 *     2     2  sequence         uint16, wraps at 65535
 *     4     4  timestamp_us_lo  uint32, low 32 bits of device microseconds
 *     8     1  flags            bit0 = ts_epoch_wrapped
 *     9     1  sample_count     number of samples in this frame
 *    10     1  sample_format    (synapse_raw_stream_t format)
 *    11     1  dropped          samples lost for this stream since the previous
 *                               transmitted frame, saturating at 255 (0 = none)
 *    12  ...  samples          sample_count values, little-endian two's compl.
 *
 * Loss reporting: the sequence number is incremented when a frame is BUILT,
 * not when the notification is accepted, so a frame the BLE stack refused
 * leaves a hole in the sequence that the host's gap detection sees. The
 * `dropped` byte carries the magnitude of the loss (samples lost since the
 * previous transmitted frame), so a consumer can tell a one-sample hiccup
 * from a whole frame that never left the pod.
 */

#ifndef SYNAPSE_RAW_FRAME_H
#define SYNAPSE_RAW_FRAME_H

#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

#define SYNAPSE_RAW_FRAME_VERSION 1u

/** Fixed header size in bytes. */
#define SYNAPSE_RAW_FRAME_HEADER_SIZE 12u

/** Largest payload that fits a 247-byte MTU notification (ATT 3-byte header). */
#define SYNAPSE_RAW_FRAME_MAX_PAYLOAD 244u

/** Largest number of samples a single frame can carry. */
#define SYNAPSE_RAW_FRAME_MAX_SAMPLES synapse_raw_max_samples(SYNAPSE_RAW_FORMAT_S8)

/** Flag: the 32-bit device microsecond counter wrapped between frames. */
#define SYNAPSE_RAW_FLAG_TS_EPOCH_WRAPPED 0x01u

/** Largest loss magnitude the `dropped` header byte can carry. */
#define SYNAPSE_RAW_DROPPED_MAX 255u

/** Logical streams. One stream_id per LSL stream so the host can subscribe to
 *  exactly the channels it needs and stay inside the BLE/energy budget.
 *  Values are part of the contract (see config/hardware.yaml). */
typedef enum {
    SYNAPSE_RAW_STREAM_ECG = 0x01,   /**< AD8232 single-lead, 500 Hz        */
    SYNAPSE_RAW_STREAM_PPG = 0x02,   /**< MAX30102 red+IR, 50 Hz            */
    SYNAPSE_RAW_STREAM_ACC = 0x03,   /**< ICM-20948 accel 3-axis, 50 Hz     */
    SYNAPSE_RAW_STREAM_GYRO = 0x04,  /**< ICM-20948 gyro 3-axis, 50 Hz      */
    SYNAPSE_RAW_STREAM_MAG = 0x05,   /**< ICM-20948 mag 3-axis, 50 Hz       */
    SYNAPSE_RAW_STREAM_TEMP = 0x06,  /**< skin temperature, 1 Hz            */
    SYNAPSE_RAW_STREAM_POWER = 0x07, /**< INA219 telemetry, 1 Hz, 4 channels:
                                          voltage_mv, current_ma, power_mw,
                                          battery_pct (Architecture.md §55-62:
                                          actual consumption logged per session) */
    SYNAPSE_RAW_STREAM_SYNC = 0x10,  /**< clock-sync marker, irregular      */
} synapse_raw_stream_t;

/** Sample encoding. Wide enough for the widest sensor so one codec covers all. */
typedef enum {
    SYNAPSE_RAW_FORMAT_S8 = 0x01,   /**< int8                              */
    SYNAPSE_RAW_FORMAT_S16 = 0x02,  /**< int16                             */
    SYNAPSE_RAW_FORMAT_S32 = 0x03,  /**< int32 (MAX30102 18-bit ADC range) */
} synapse_raw_format_t;

/** Result codes. Negative values are errors; zero is success. */
typedef enum {
    SYNAPSE_RAW_OK = 0,
    SYNAPSE_RAW_ERR_NULL = -1,
    SYNAPSE_RAW_ERR_VERSION = -2,
    SYNAPSE_RAW_ERR_STREAM = -3,
    SYNAPSE_RAW_ERR_FORMAT = -4,
    SYNAPSE_RAW_ERR_COUNT = -5,
    SYNAPSE_RAW_ERR_BUFFER = -6,
    SYNAPSE_RAW_ERR_SHORT = -7,
} synapse_raw_status_t;

/** Decoded frame header (samples remain encoded in @ref frame). */
typedef struct {
    uint8_t version;
    uint8_t stream_id;
    uint16_t sequence;
    uint32_t timestamp_us_lo;
    uint8_t flags;
    uint8_t sample_count;
    uint8_t sample_format;
    uint8_t dropped; /**< samples lost since the previous transmitted frame */
} synapse_raw_header_t;

/** Bytes per sample for a given format. Returns 0 for an invalid format. */
uint8_t synapse_raw_format_size(uint8_t format);

/** Samples that fit in one frame for a given format. Returns 0 if invalid. */
uint16_t synapse_raw_max_samples(uint8_t format);

/** Largest number of samples a single frame can carry (int8 samples). */
#define SYNAPSE_RAW_FRAME_MAX_SAMPLES synapse_raw_max_samples(SYNAPSE_RAW_FORMAT_S8)

/**
 * @brief Encode a frame of raw samples.
 *
 * @param stream_id       One of synapse_raw_stream_t.
 * @param sequence        Wrapping 16-bit sequence number. The caller
 *                        increments it when the frame is BUILT, so a frame the
 *                        BLE stack refuses still leaves a hole in the sequence.
 * @param timestamp_us_lo Low 32 bits of the device microsecond timestamp of
 *                        the *first* sample in the frame.
 * @param flags           OR of SYNAPSE_RAW_FLAG_* bits.
 * @param dropped         Samples lost for this stream since the previous
 *                        transmitted frame. The value is saturated at
 *                        SYNAPSE_RAW_DROPPED_MAX by the encoder, so a long
 *                        outage cannot wrap around to "no loss"; 0 means
 *                        "nothing was lost".
 * @param format          One of synapse_raw_format_t.
 * @param samples         Interleaved samples (channel-minor).
 * @param sample_count    Number of samples.
 * @param out             Destination buffer, at least
 *                        SYNAPSE_RAW_FRAME_HEADER_SIZE + count*size bytes.
 * @param out_size        Capacity of @p out in bytes.
 * @return SYNAPSE_RAW_OK, or a negative synapse_raw_status_t.
 */
int synapse_raw_frame_encode(uint8_t stream_id,
                             uint16_t sequence,
                             uint32_t timestamp_us_lo,
                             uint8_t flags,
                             uint16_t dropped,
                             uint8_t format,
                             const void *samples,
                             uint8_t sample_count,
                             uint8_t *out,
                             size_t out_size);

/**
 * @brief Decode a frame header and validate it.
 *
 * Fills @p header and returns the byte offset where the sample block begins
 * (>= SYNAPSE_RAW_FRAME_HEADER_SIZE) on success. The samples themselves are
 * left encoded and must be read with the format's endianness (little-endian,
 * two's complement) by the caller.
 *
 * @return Non-negative payload offset on success, negative status on failure.
 */
int synapse_raw_frame_decode(const uint8_t *frame,
                             size_t frame_size,
                             synapse_raw_header_t *header);

/**
 * @brief Read the n-th sample of a decoded frame.
 *
 * @param frame    Frame previously validated with synapse_raw_frame_decode.
 * @param payload  Offset returned by synapse_raw_frame_decode.
 * @param index    Sample index (0-based, < header.sample_count).
 * @param out      Receives the sign-extended 32-bit sample value.
 * @return SYNAPSE_RAW_OK or a negative status.
 */
int synapse_raw_frame_get_sample(const uint8_t *frame,
                                 size_t frame_size,
                                 size_t payload,
                                 size_t index,
                                 int32_t *out);

/** Human-readable status string (stable, used by logs and tests). */
const char *synapse_raw_status_str(int status);

#ifdef __cplusplus
}
#endif

#endif /* SYNAPSE_RAW_FRAME_H */
