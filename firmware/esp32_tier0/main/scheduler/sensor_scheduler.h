#pragma once

#include <stdint.h>
#include <stdbool.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/queue.h"
#include "freertos/semphr.h"
#include "esp_timer.h"

#ifdef __cplusplus
extern "C" {
#endif

#define SENSOR_SCHEDULER_COORDINATOR_PERIOD_MS 5
#define SENSOR_SCHEDULER_MAX_SENSORS 4
#define SENSOR_SCHEDULER_RING_BUFFER_SIZE 256
#define SENSOR_SCHEDULER_BLE_QUEUE_SIZE 32

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
} sensor_scheduler_t;

typedef void (*sensor_read_fn_t)(sensor_sample_t* sample, void* user_ctx);
typedef void (*sensor_init_fn_t)(void* user_ctx);
typedef void (*sensor_deinit_fn_t)(void* user_ctx);

typedef struct {
    sensor_type_t type;
    const char* name;
    uint32_t sampling_rate_hz;
    sensor_init_fn_t init;
    sensor_read_fn_t read;
    sensor_deinit_fn_t deinit;
    void* user_ctx;
    TaskHandle_t* task_handle;
} sensor_config_t;

esp_err_t sensor_scheduler_init(sensor_scheduler_t* scheduler, QueueHandle_t ble_tx_queue);
esp_err_t sensor_scheduler_register_sensor(sensor_scheduler_t* scheduler, const sensor_config_t* config);
esp_err_t sensor_scheduler_start(sensor_scheduler_t* scheduler);
esp_err_t sensor_scheduler_stop(sensor_scheduler_t* scheduler);
esp_err_t sensor_scheduler_deinit(sensor_scheduler_t* scheduler);

bool sensor_ring_buffer_push(sensor_ring_buffer_t* rb, const sensor_sample_t* sample);
bool sensor_ring_buffer_pop(sensor_ring_buffer_t* rb, sensor_sample_t* sample);
size_t sensor_ring_buffer_available(const sensor_ring_buffer_t* rb);
void sensor_ring_buffer_reset(sensor_ring_buffer_t* rb);

void sensor_scheduler_get_stats(const sensor_scheduler_t* scheduler, uint32_t* sample_counts, uint32_t* dropped_samples);

#ifdef __cplusplus
}
#endif