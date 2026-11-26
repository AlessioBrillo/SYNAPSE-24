/* FreeRTOS task stub for the host compile gate. */
#ifndef FREERTOS_TASK_H_STUB
#define FREERTOS_TASK_H_STUB

#include "FreeRTOS.h"

typedef void *TaskHandle_t;

typedef void (*TaskFunction_t)(void *);
int xTaskCreate(TaskFunction_t fn, const char *name, unsigned stack, void *arg,
                int priority, TaskHandle_t *handle);
void vTaskDelete(TaskHandle_t handle);

#endif // FREERTOS_TASK_H_STUB
