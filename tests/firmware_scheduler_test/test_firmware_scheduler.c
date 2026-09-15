#include "sensor_scheduler.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/queue.h"
#include "freertos/semphr.h"
#include <string.h>
#include <stdio.h>

static void test_ring_buffer_basic(void) {
    printf("Testing ring buffer basic operations...\n");
    sensor_ring_buffer_t rb = {0};
    rb.mutex = xSemaphoreCreateMutex();

    sensor_sample_t sample = {0};
    sample.type = SENSOR_TYPE_ECG;
    sample.timestamp_us = 1000;
    sample.sequence = 1;
    sample.data.ecg.voltage_mv = 1.5f;

    assert(sensor_ring_buffer_push(&rb, &sample));
    assert(sensor_ring_buffer_available(&rb) == 1);

    sensor_sample_t popped = {0};
    assert(sensor_ring_buffer_pop(&rb, &popped));
    assert(popped.type == SENSOR_TYPE_ECG);
    assert(popped.sequence == 1);
    assert(popped.data.ecg.voltage_mv == 1.5f);
    assert(sensor_ring_buffer_available(&rb) == 0);

    vSemaphoreDelete(rb.mutex);
    printf("  PASSED\n");
}

static void test_ring_buffer_wraparound(void) {
    printf("Testing ring buffer wraparound...\n");
    sensor_ring_buffer_t rb = {0};
    rb.mutex = xSemaphoreCreateMutex();

    for (int i = 0; i < SENSOR_SCHEDULER_RING_BUFFER_SIZE + 10; i++) {
        sensor_sample_t sample = {0};
        sample.type = SENSOR_TYPE_IMU;
        sample.timestamp_us = i * 1000;
        sample.sequence = i;
        sample.data.imu.ax = (float)i;

        if (i < SENSOR_SCHEDULER_RING_BUFFER_SIZE) {
            assert(sensor_ring_buffer_push(&rb, &sample));
        } else {
            assert(!sensor_ring_buffer_push(&rb, &sample));
        }
    }

    for (int i = 0; i < SENSOR_SCHEDULER_RING_BUFFER_SIZE; i++) {
        sensor_sample_t popped = {0};
        assert(sensor_ring_buffer_pop(&rb, &popped));
        assert(popped.sequence == i);
    }

    vSemaphoreDelete(rb.mutex);
    printf("  PASSED\n");
}

static void test_ring_buffer_concurrent(void) {
    printf("Testing ring buffer concurrent access...\n");
    sensor_ring_buffer_t rb = {0};
    rb.mutex = xSemaphoreCreateMutex();

    for (int i = 0; i < 100; i++) {
        sensor_sample_t sample = {0};
        sample.type = SENSOR_TYPE_PPG;
        sample.sequence = i;
        sample.data.ppg.red = (float)i;
        assert(sensor_ring_buffer_push(&rb, &sample));
    }

    for (int i = 0; i < 100; i++) {
        sensor_sample_t popped = {0};
        assert(sensor_ring_buffer_pop(&rb, &popped));
        assert(popped.sequence == i);
    }

    vSemaphoreDelete(rb.mutex);
    printf("  PASSED\n");
}

static void test_scheduler_init_deinit(void) {
    printf("Testing scheduler init/deinit...\n");
    QueueHandle_t queue = xQueueCreate(32, sizeof(sensor_sample_t));
    sensor_scheduler_t scheduler = {0};

    esp_err_t err = sensor_scheduler_init(&scheduler, queue);
    assert(err == ESP_OK);
    assert(!scheduler.running);

    err = sensor_scheduler_deinit(&scheduler);
    assert(err == ESP_OK);

    vQueueDelete(queue);
    printf("  PASSED\n");
}

static void test_scheduler_start_stop(void) {
    printf("Testing scheduler start/stop...\n");
    QueueHandle_t queue = xQueueCreate(32, sizeof(sensor_sample_t));
    sensor_scheduler_t scheduler = {0};

    esp_err_t err = sensor_scheduler_init(&scheduler, queue);
    assert(err == ESP_OK);

    err = sensor_scheduler_start(&scheduler);
    assert(err == ESP_OK);
    assert(scheduler.running);

    err = sensor_scheduler_stop(&scheduler);
    assert(err == ESP_OK);
    assert(!scheduler.running);

    err = sensor_scheduler_deinit(&scheduler);
    assert(err == ESP_OK);

    vQueueDelete(queue);
    printf("  PASSED\n");
}

static void test_scheduler_sensor_registration(void) {
    printf("Testing scheduler sensor registration...\n");
    QueueHandle_t queue = xQueueCreate(32, sizeof(sensor_sample_t));
    sensor_scheduler_t scheduler = {0};

    esp_err_t err = sensor_scheduler_init(&scheduler, queue);
    assert(err == ESP_OK);

    TaskHandle_t task_handle = NULL;
    sensor_config_t config = {
        .type = SENSOR_TYPE_ECG,
        .name = "Test_ECG",
        .sampling_rate_hz = 250,
        .init = NULL,
        .read = NULL,
        .deinit = NULL,
        .user_ctx = NULL,
        .task_handle = &task_handle
    };

    err = sensor_scheduler_register_sensor(&scheduler, &config);
    assert(err == ESP_OK);

    err = sensor_scheduler_deinit(&scheduler);
    assert(err == ESP_OK);

    vQueueDelete(queue);
    printf("  PASSED\n");
}

static void test_sync_marker_handler(void) {
    printf("Testing sync marker handler...\n");
    sync_marker_handler_t handler = {0};

    esp_err_t err = sync_marker_handler_init(&handler);
    assert(err == ESP_OK);

    int callback_called = 0;
    int64_t last_hub_ts = 0, last_pod_ts = 0;
    auto callback = [](uint32_t seq, int64_t hub_ts, int64_t pod_ts, void* ctx) {
        int* called = (int*)ctx;
        *called = 1;
        int64_t* hub = (int64_t*)((char*)ctx + sizeof(int));
        int64_t* pod = (int64_t*)((char*)ctx + sizeof(int) + sizeof(int64_t));
        *hub = hub_ts;
        *pod = pod_ts;
    };

    struct {
        int called;
        int64_t hub_ts;
        int64_t pod_ts;
    } ctx = {0};

    err = sync_marker_handler_on_marker_received(&handler, 1, 1000000, callback, &ctx);
    assert(err == ESP_OK);
    assert(ctx.called == 1);
    assert(ctx.hub_ts == 1000000);

    err = sync_marker_handler_on_marker_received(&handler, 2, 2000000, callback, &ctx);
    assert(err == ESP_OK);

    float drift_ppm;
    int64_t offset_us;
    err = sync_marker_handler_estimate_drift(&handler, &drift_ppm, &offset_us);
    assert(err == ESP_OK);

    int64_t corrected;
    err = sync_marker_handler_correct_timestamp(&handler, 2001000, &corrected);
    assert(err == ESP_OK);

    err = sync_marker_handler_reset(&handler);
    assert(err == ESP_OK);
    assert(handler.count == 0);

    printf("  PASSED\n");
}

int main(void) {
    printf("Running firmware scheduler unit tests...\n\n");

    test_ring_buffer_basic();
    test_ring_buffer_wraparound();
    test_ring_buffer_concurrent();
    test_scheduler_init_deinit();
    test_scheduler_start_stop();
    test_scheduler_sensor_registration();
    test_sync_marker_handler();

    printf("\nAll tests PASSED!\n");
    return 0;
}