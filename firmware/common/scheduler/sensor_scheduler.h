#pragma once

#include <stdint.h>
#include <stdbool.h>
#include <inttypes.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/queue.h"
#include "freertos/semphr.h"
#include "esp_err.h"
#include "esp_timer.h"
#include "sdkconfig.h"

#ifdef __cplusplus
extern "C" {
#endif

#define SENSOR_SCHEDULER_COORDINATOR_PERIOD_MS 5
// Sensors the band polls: ECG, PPG, IMU (sensor_type_t 0..2). The in-ear EEG type (3) belongs to
// another pod, and its unused slot cost 16 KB of internal RAM (256 samples x 64 B).
#define SENSOR_SCHEDULER_MAX_SENSORS 3
#define SENSOR_SCHEDULER_RING_BUFFER_SIZE 256
// Samples between the scheduler and the main loop, which drains 64 per 10 ms tick but also runs the
// power and quality tick. 32 (and the 64 that was used) was ~100 ms of production at 600 samples/s:
// one slow I2C timeout in that tick overflowed it. 256 = ~430 ms.
#define SENSOR_SCHEDULER_BLE_QUEUE_SIZE 256

// Core the sensor tasks and the coordinator run on. NimBLE (host and controller), esp_timer and the
// main task sit on core 0; sharing it with them added scheduling jitter to the 500 Hz ECG.
#if defined(CONFIG_FREERTOS_UNICORE)
#define SENSOR_TASK_CORE 0  // a single-core build has only core 0
#else
#define SENSOR_TASK_CORE 1
#endif

// Task priorities (FreeRTOS) - higher number = higher priority
// ECG at 500Hz needs highest priority for hard real-time
#define SENSOR_TASK_PRIO_ECG            6
#define SENSOR_TASK_PRIO_PPG            5
#define SENSOR_TASK_PRIO_IMU            5
#define SENSOR_TASK_PRIO_COORDINATOR    4

typedef enum {
    SENSOR_TYPE_ECG = 0,
    SENSOR_TYPE_PPG = 1,
    SENSOR_TYPE_IMU = 2,
    SENSOR_TYPE_EEG_IN_EAR = 3,
    SENSOR_TYPE_MAX = 4
} sensor_type_t;

// The arrays of the scheduler are indexed by sensor_type_t. A polled type outside them would be
// rejected at run time (silently, no data): make it a build error instead.
#ifdef __cplusplus
static_assert(SENSOR_TYPE_IMU < SENSOR_SCHEDULER_MAX_SENSORS, "polled sensor types must fit the scheduler arrays");
#else
_Static_assert(SENSOR_TYPE_IMU < SENSOR_SCHEDULER_MAX_SENSORS, "polled sensor types must fit the scheduler arrays");
#endif
typedef struct {
    sensor_type_t type;
    int64_t timestamp_us;       // esp_timer_get_time() at sample acquisition
    int64_t lsl_timestamp_us;   // LSL clock domain timestamp (synced via markers)
    uint16_t sequence;
    union {
        struct { float voltage_mv; } ecg;
        struct { float red; float ir; float green; } ppg;
        struct { float ax; float ay; float az; float gx; float gy; float gz; float mx; float my; float mz; } imu;
        struct { float ch1; float ch2; } eeg;
    } data;
} sensor_sample_t;

typedef struct {
    sensor_sample_t buffer[SENSOR_SCHEDULER_RING_BUFFER_SIZE];
    volatile size_t head;
    volatile size_t tail;
    SemaphoreHandle_t mutex;
} sensor_ring_buffer_t;

typedef struct {
    QueueHandle_t ble_tx_queue;
    sensor_ring_buffer_t buffers[SENSOR_SCHEDULER_MAX_SENSORS];
    TaskHandle_t coordinator_task;
    TaskHandle_t sensor_tasks[SENSOR_SCHEDULER_MAX_SENSORS];
    esp_timer_handle_t coordinator_timer;
    bool running;
    uint32_t sample_counts[SENSOR_SCHEDULER_MAX_SENSORS];
    uint32_t dropped_samples[SENSOR_SCHEDULER_MAX_SENSORS];
    uint32_t read_errors[SENSOR_SCHEDULER_MAX_SENSORS];  // reads that did not deliver a sample
    volatile uint32_t rates_hz[SENSOR_SCHEDULER_MAX_SENSORS];
} sensor_scheduler_t;

// Returns ESP_OK only when `sample` holds a fresh reading; anything else is dropped.
// ESP_ERR_NOT_FOUND means "no new sample yet" (empty FIFO, calibrating): dropped, not an error.
// Any other code is counted in read_errors.
typedef esp_err_t (*sensor_read_fn_t)(sensor_sample_t* sample, void* user_ctx);
typedef void (*sensor_init_fn_t)(void* user_ctx);
typedef void (*sensor_deinit_fn_t)(void* user_ctx);
// Optional: true when the last read() left more ready samples (a FIFO burst), so the task
// reads again at once instead of waiting for the next period.
typedef bool (*sensor_has_more_fn_t)(void* user_ctx);
// Optional: samples the driver lost BEFORE it could hand them over (a FIFO that overflowed)
// since the previous call; the call resets that count. The scheduler skips that many sequence
// numbers, so the loss shows up as a hole the research service reports in the `dropped` byte.
typedef uint32_t (*sensor_take_lost_fn_t)(void* user_ctx);

typedef struct {
    sensor_type_t type;
    const char* name;
    uint32_t sampling_rate_hz;
    sensor_init_fn_t init;
    sensor_read_fn_t read;
    sensor_deinit_fn_t deinit;
    void* user_ctx;
    TaskHandle_t* task_handle;
    sensor_has_more_fn_t has_more;  // optional, see above
    sensor_take_lost_fn_t take_lost;  // optional, see above
} sensor_config_t;

esp_err_t sensor_scheduler_init(sensor_scheduler_t* scheduler, QueueHandle_t ble_tx_queue);
esp_err_t sensor_scheduler_register_sensor(sensor_scheduler_t* scheduler, const sensor_config_t* config);
esp_err_t sensor_scheduler_start(sensor_scheduler_t* scheduler);
esp_err_t sensor_scheduler_stop(sensor_scheduler_t* scheduler);
esp_err_t sensor_scheduler_deinit(sensor_scheduler_t* scheduler);
esp_err_t sensor_scheduler_set_rate(sensor_scheduler_t* scheduler, sensor_type_t type, uint32_t rate_hz);
// Publish the polling task of a registered sensor (needed by start/stop to resume/suspend it).
esp_err_t sensor_scheduler_set_task(sensor_scheduler_t* scheduler, sensor_type_t type, TaskHandle_t task);

bool sensor_ring_buffer_push(sensor_ring_buffer_t* rb, const sensor_sample_t* sample);
bool sensor_ring_buffer_pop(sensor_ring_buffer_t* rb, sensor_sample_t* sample);
bool sensor_ring_buffer_peek(const sensor_ring_buffer_t* rb, sensor_sample_t* sample, size_t offset);
size_t sensor_ring_buffer_available(const sensor_ring_buffer_t* rb);
void sensor_ring_buffer_reset(sensor_ring_buffer_t* rb);

void sensor_scheduler_get_stats(const sensor_scheduler_t* scheduler, uint32_t* sample_counts, uint32_t* dropped_samples);

// FreeRTOS sensor polling task (one instance per registered sensor).
// Must be non-static: created from app_main via xTaskCreate.
void sensor_task_fn(void* arg);

// Global scheduler pointer for sensor tasks (set on init, cleared on deinit).
extern sensor_scheduler_t* g_sensor_scheduler_ptr;

#ifdef __cplusplus
}
#endif