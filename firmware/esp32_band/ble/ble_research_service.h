/**
 * @file ble_research_service.h
 * @brief Synapse Research Service — raw sample streaming over BLE GATT.
 *
 * This is the dataset path, and it is intentionally separate from the consumer
 * "feature stream" in ble_gatt_server.h.
 *
 * What it does: streams raw sensor samples (ECG/PPG/ACC/GYRO/MAG/TEMP) as
 * notifications, each frame carrying the pod's own microsecond timestamp, plus
 * a clock-sync characteristic the host writes to and reads back to estimate
 * offset and drift against the hub clock (Architecture.md §92).
 *
 * What it deliberately does NOT do: compute features. SQI, MAP, HRV and fusion
 * stay on the host, so the recorded XDF dataset is complete and independently
 * re-analysable, and the pod spends no power redoing work the host will redo
 * anyway (Architecture.md §55-62).
 *
 * Contract:
 *   - config/hardware.yaml   (pod metadata `research_characteristics`, `raw_frame`)
 *   - firmware/common/ble/synapse_raw_frame.[ch]  (wire format, host-parity tested)
 *   - src/synapse24/hardware/raw_codec.py          (host decoder)
 * tools/check_contract.py rejects any UUID or stream-id drift between them.
 */

#ifndef SYNAPSE_BLE_RESEARCH_SERVICE_H
#define SYNAPSE_BLE_RESEARCH_SERVICE_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include "esp_err.h"
#include "sensor_scheduler.h"
#include "synapse_raw_frame.h"

#ifdef __cplusplus
extern "C" {
#endif

/**
 * Clock sync protocol (NTP-style read-after-write exchange).
 *
 * The host writes a request, then reads the response, which the pod fills in
 * with its own receive/send timestamps:
 *   request : [uint32 seq][int64 hub_send_us]                     (12 bytes)
 *   response: [uint32 seq][int64 pod_recv_us][int64 pod_send_us]  (20 bytes)
 *
 * The 32-bit device microsecond counter wraps every ~4295 s. The service tracks
 * the wrap count so the host can rebuild a monotonic 64-bit timeline, and
 * reports it via SYNAPSE_RAW_FLAG_TS_EPOCH_WRAPPED on the next frame.
 */
#define SYNAPSE_SYNC_REQUEST_SIZE 12u
#define SYNAPSE_SYNC_RESPONSE_SIZE 20u

/** Ring depth per stream, in samples. At 50 Hz and a 4-sample flush threshold
 *  this is ~640 ms of buffering — enough to ride out a slow BLE event without
 *  dropping, small enough to stay in internal RAM. */
#define SYNAPSE_RESEARCH_RING_DEPTH 32u
#define SYNAPSE_RESEARCH_MAX_CHANNELS 3u

/** Unit scaling applied before quantising to the frame format. Values are
 *  physical quantities scaled so that 1 LSB is a fixed engineering unit; the
 *  host divides by the same constant. */
typedef enum {
    SYNAPSE_RESEARCH_UNIT_UV = 1000,      /**< microvolts   (ECG, from mV)   */
    SYNAPSE_RESEARCH_UNIT_MG = 1000,      /**< millig       (ACC, from g)    */
    SYNAPSE_RESEARCH_UNIT_MRAD_S = 1000,  /**< millirad/s   (GYRO, rad/s)    */
    SYNAPSE_RESEARCH_UNIT_CUT = 100,      /**< 0.01 uT      (MAG, from uT)   */
    SYNAPSE_RESEARCH_UNIT_COUNT = 1,      /**< raw ADC code (PPG)            */
    SYNAPSE_RESEARCH_UNIT_MILLI = 1,      /**< mV, mA, mW, % (POWER)         */
} synapse_research_unit_t;

/** Per-stream runtime state. */
typedef struct {
    uint8_t stream_id;      /**< synapse_raw_stream_t                       */
    uint8_t format;         /**< synapse_raw_format_t                       */
    uint8_t channels;       /**< channels interleaved per sample            */
    uint16_t unit_scale;    /**< float -> int multiplier (see unit enum);   *
                             *  16-bit because the scale reaches 1000, which*
                             *  would silently truncate to 232 in a uint8_t */
    bool enabled;           /**< host notified + subscribed                 */
    uint16_t sequence;      /**< wrapping 16-bit frame sequence             */
    uint16_t pending;       /**< buffered samples not yet framed            */
    uint16_t flush_at;      /**< emit a frame once pending reaches this     */
    int32_t ring[SYNAPSE_RESEARCH_RING_DEPTH * SYNAPSE_RESEARCH_MAX_CHANNELS];
    int64_t first_ts_us;    /**< device timestamp of the oldest buffered sample */
} synapse_research_stream_t;

/** Module state (single instance, owned by this translation unit). */
typedef struct {
    bool initialized;
    bool connected;
    bool transmitting;
    uint16_t conn_handle;
    uint16_t mtu;
    uint32_t epoch_wraps;              /**< 32-bit ts counter wrap count       */
    int64_t last_sample_us;
    synapse_research_stream_t streams[8];
    size_t stream_count;
    uint32_t dropped_frames;
} synapse_research_t;

/**
 * @brief Register the research service with the NimBLE host.
 *
 * Call after the BLE stack is up and before advertising starts. Idempotent.
 *
 * @return ESP_OK on success.
 */
esp_err_t synapse_research_service_init(void);

/**
 * @brief Feed one scheduler sample into the research stream buffers.
 *
 * Runs in the sensor task context, so it must stay allocation-free and cheap.
 * Emits a notification when a stream's flush threshold is reached.
 *
 * @param sample Sample produced by firmware/common scheduler drivers.
 */
void synapse_research_service_submit(const sensor_sample_t *sample);

/**
 * @brief Submit one power-telemetry sample (INA219) as a raw frame.
 *
 * The energy budget is a functional requirement (Architecture.md §55-62): a
 * bench PSU measurement is a one-off calibration, not the per-session
 * consumption the 24-hour feasibility claim rests on. At 1 Hz with four
 * channels the sample fits in one frame, so it bypasses the scheduler ring
 * rather than borrowing the ECG/PPG/IMU queues.
 *
 * Runs in the power task context: allocation-free.
 *
 * @param voltage_mv  Bus voltage in millivolts
 * @param current_ma  Shunt current in milliamperes (may be negative)
 * @param battery_pct State of charge, 0-100
 */
void synapse_research_service_submit_power(float voltage_mv, float current_ma, float battery_pct);

/** @brief Start or stop raw streaming (host writes the control characteristic). */
esp_err_t synapse_research_service_set_transmitting(bool enable);

/** @brief Notify the service that the GAP link came up. */
void synapse_research_service_on_connect(uint16_t conn_handle);

/** @brief Notify the service that the GAP link dropped. */
void synapse_research_service_on_disconnect(void);

/** @brief Negotiated MTU, or 0 when not connected. */
uint16_t synapse_research_service_get_mtu(void);

/**
 * @brief Frames dropped because a notification could not be queued.
 *
 * Non-zero means the BLE budget is exceeded: the host will observe a sequence
 * gap and the live validator must fail the recording rather than silently
 * accepting a hole in the dataset.
 */
uint32_t synapse_research_service_dropped_frames(void);

#ifdef __cplusplus
}
#endif

#endif /* SYNAPSE_BLE_RESEARCH_SERVICE_H */
