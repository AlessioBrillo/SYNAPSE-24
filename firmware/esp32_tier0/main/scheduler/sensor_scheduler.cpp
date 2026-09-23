#include "sensor_scheduler.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/queue.h"
#include "freertos/semphr.h"
#include <string.h>

static const char* TAG = "sensor_scheduler";

static void coordinator_timer_callback(void* arg);
static void coordinator_task_fn(void* arg);
static void sensor_task_fn(void* arg);

static inline int64_t get_time_us(void) {
    return esp_timer_get_time();
}

esp_err_t sensor_scheduler_init(sensor_scheduler_t* scheduler, QueueHandle_t ble_tx_queue) {
    if (!scheduler || !ble_tx_queue) {
        return ESP_ERR_INVALID_ARG;
    }

    memset(scheduler, 0, sizeof(sensor_scheduler_t));
    scheduler->ble_tx_queue = ble_tx_queue;

    for (int i = 0; i < SENSOR_SCHEDULER_MAX_SENSORS; i++) {
        sensor_ring_buffer_t* rb = &scheduler->buffers[i];
        rb->head = 0;
        rb->tail = 0;
        rb->mutex = xSemaphoreCreateMutex();
        if (!rb->mutex) {
            ESP_LOGE(TAG, "Failed to create mutex for sensor %d", i);
            for (int j = 0; j < i; j++) {
                vSemaphoreDelete(scheduler->buffers[j].mutex);
            }
            return ESP_ERR_NO_MEM;
        }
    }

    esp_timer_create_args_t timer_args = {
        .callback = coordinator_timer_callback,
        .arg = scheduler,
        .dispatch_method = ESP_TIMER_TASK,
        .name = "sensor_coord_timer",
        .skip_unhandled_events = true
    };
    esp_err_t err = esp_timer_create(&timer_args, &scheduler->coordinator_timer);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "Failed to create coordinator timer: %s", esp_err_to_name(err));
        for (int i = 0; i < SENSOR_SCHEDULER_MAX_SENSORS; i++) {
            vSemaphoreDelete(scheduler->buffers[i].mutex);
        }
        return err;
    }

    scheduler->running = false;
    ESP_LOGI(TAG, "Sensor scheduler initialized");
    return ESP_OK;
}

esp_err_t sensor_scheduler_register_sensor(sensor_scheduler_t* scheduler, const sensor_config_t* config) {
    if (!scheduler || !config || config->type >= SENSOR_SCHEDULER_MAX_SENSORS) {
        return ESP_ERR_INVALID_ARG;
    }

    if (scheduler->running) {
        ESP_LOGE(TAG, "Cannot register sensor while scheduler running");
        return ESP_ERR_INVALID_STATE;
    }

    if (config->init) {
        config->init(config->user_ctx);
    }

    scheduler->sensor_tasks[config->type] = *config->task_handle;
    ESP_LOGI(TAG, "Registered sensor: %s (type=%d, rate=%" PRIu32 " Hz)", config->name, config->type, config->sampling_rate_hz);
    return ESP_OK;
}

esp_err_t sensor_scheduler_start(sensor_scheduler_t* scheduler) {
    if (!scheduler || scheduler->running) {
        return ESP_ERR_INVALID_STATE;
    }

    for (int i = 0; i < SENSOR_SCHEDULER_MAX_SENSORS; i++) {
        if (scheduler->sensor_tasks[i]) {
            vTaskResume(scheduler->sensor_tasks[i]);
        }
    }

    esp_err_t err = esp_timer_start_periodic(scheduler->coordinator_timer, SENSOR_SCHEDULER_COORDINATOR_PERIOD_MS * 1000);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "Failed to start coordinator timer: %s", esp_err_to_name(err));
        return err;
    }

    scheduler->running = true;
    ESP_LOGI(TAG, "Sensor scheduler started (coordinator period=%d ms)", SENSOR_SCHEDULER_COORDINATOR_PERIOD_MS);
    return ESP_OK;
}

esp_err_t sensor_scheduler_stop(sensor_scheduler_t* scheduler) {
    if (!scheduler || !scheduler->running) {
        return ESP_ERR_INVALID_STATE;
    }

    esp_timer_stop(scheduler->coordinator_timer);

    for (int i = 0; i < SENSOR_SCHEDULER_MAX_SENSORS; i++) {
        if (scheduler->sensor_tasks[i]) {
            vTaskSuspend(scheduler->sensor_tasks[i]);
        }
    }

    scheduler->running = false;
    ESP_LOGI(TAG, "Sensor scheduler stopped");
    return ESP_OK;
}

esp_err_t sensor_scheduler_deinit(sensor_scheduler_t* scheduler) {
    if (!scheduler) {
        return ESP_ERR_INVALID_ARG;
    }

    if (scheduler->running) {
        sensor_scheduler_stop(scheduler);
    }

    if (scheduler->coordinator_timer) {
        esp_timer_delete(scheduler->coordinator_timer);
        scheduler->coordinator_timer = NULL;
    }

    for (int i = 0; i < SENSOR_SCHEDULER_MAX_SENSORS; i++) {
        if (scheduler->buffers[i].mutex) {
            vSemaphoreDelete(scheduler->buffers[i].mutex);
            scheduler->buffers[i].mutex = NULL;
        }
    }

    memset(scheduler, 0, sizeof(sensor_scheduler_t));
    ESP_LOGI(TAG, "Sensor scheduler deinitialized");
    return ESP_OK;
}

bool sensor_ring_buffer_push(sensor_ring_buffer_t* rb, const sensor_sample_t* sample) {
    if (!rb || !sample || !rb->mutex) {
        return false;
    }

    if (xSemaphoreTake(rb->mutex, pdMS_TO_TICKS(10)) != pdTRUE) {
        return false;
    }

    size_t next_head = (rb->head + 1) % SENSOR_SCHEDULER_RING_BUFFER_SIZE;
    bool full = (next_head == rb->tail);

    if (!full) {
        rb->buffer[rb->head] = *sample;
        rb->head = next_head;
    }

    xSemaphoreGive(rb->mutex);
    return !full;
}

bool sensor_ring_buffer_pop(sensor_ring_buffer_t* rb, sensor_sample_t* sample) {
    if (!rb || !sample || !rb->mutex) {
        return false;
    }

    if (xSemaphoreTake(rb->mutex, pdMS_TO_TICKS(10)) != pdTRUE) {
        return false;
    }

    bool empty = (rb->head == rb->tail);
    bool success = false;

    if (!empty) {
        *sample = rb->buffer[rb->tail];
        rb->tail = (rb->tail + 1) % SENSOR_SCHEDULER_RING_BUFFER_SIZE;
        success = true;
    }

    xSemaphoreGive(rb->mutex);
    return success;
}

size_t sensor_ring_buffer_available(const sensor_ring_buffer_t* rb) {
    if (!rb || !rb->mutex) {
        return 0;
    }

    if (xSemaphoreTake((SemaphoreHandle_t)rb->mutex, pdMS_TO_TICKS(5)) != pdTRUE) {
        return 0;
    }

    size_t available = (rb->head >= rb->tail) ? (rb->head - rb->tail) : (SENSOR_SCHEDULER_RING_BUFFER_SIZE - rb->tail + rb->head);
    xSemaphoreGive((SemaphoreHandle_t)rb->mutex);
    return available;
}

void sensor_ring_buffer_reset(sensor_ring_buffer_t* rb) {
    if (!rb || !rb->mutex) {
        return;
    }

    xSemaphoreTake(rb->mutex, portMAX_DELAY);
    rb->head = 0;
    rb->tail = 0;
    xSemaphoreGive(rb->mutex);
}

void sensor_scheduler_get_stats(const sensor_scheduler_t* scheduler, uint32_t* sample_counts, uint32_t* dropped_samples) {
    if (!scheduler) return;
    if (sample_counts) memcpy(sample_counts, scheduler->sample_counts, sizeof(scheduler->sample_counts));
    if (dropped_samples) memcpy(dropped_samples, scheduler->dropped_samples, sizeof(scheduler->dropped_samples));
}

static void coordinator_timer_callback(void* arg) {
    sensor_scheduler_t* scheduler = (sensor_scheduler_t*)arg;
    BaseType_t higher_priority_task_woken = pdFALSE;
    vTaskNotifyGiveFromISR(scheduler->coordinator_task, &higher_priority_task_woken);
    portYIELD_FROM_ISR(higher_priority_task_woken);
}

static void coordinator_task_fn(void* arg) {
    sensor_scheduler_t* scheduler = (sensor_scheduler_t*)arg;
    sensor_sample_t sample;

    while (1) {
        ulTaskNotifyTake(pdTRUE, portMAX_DELAY);

        if (!scheduler->running) continue;

        for (int i = 0; i < SENSOR_SCHEDULER_MAX_SENSORS; i++) {
            sensor_ring_buffer_t* rb = &scheduler->buffers[i];
            while (sensor_ring_buffer_pop(rb, &sample)) {
                if (xQueueSend(scheduler->ble_tx_queue, &sample, 0) != pdTRUE) {
                    scheduler->dropped_samples[i]++;
                }
            }
        }
    }
}

static void sensor_task_fn(void* arg) {
    sensor_config_t* config = (sensor_config_t*)arg;
    sensor_scheduler_t* scheduler = NULL;
    sensor_sample_t sample = {0};
    sample.type = config->type;
    TickType_t period_ticks = pdMS_TO_TICKS(1000 / config->sampling_rate_hz);
    TickType_t last_wake = xTaskGetTickCount();

    while (1) {
        vTaskDelayUntil(&last_wake, period_ticks);

        if (!scheduler || !scheduler->running) continue;

        sample.timestamp_us = get_time_us();
        sample.lsl_timestamp_us = sample.timestamp_us;  // Initially same, corrected by sync markers
        sample.sequence = scheduler->sample_counts[config->type]++;

        if (config->read) {
            config->read(&sample, config->user_ctx);
        }

        sensor_ring_buffer_push(&scheduler->buffers[config->type], &sample);
    }
}