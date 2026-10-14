/* FreeRTOS semaphore stub for the host compile gate.
 *
 * The host glue implements a single-threaded "mutex": the driver's behaviour
 * (take → give) is exercised, real mutual exclusion is not needed on a host
 * with one thread.
 */
#ifndef FREERTOS_SEMPHR_H_STUB
#define FREERTOS_SEMPHR_H_STUB

#include <stdint.h>

#include "FreeRTOS.h"

typedef int BaseType_t;
typedef void *SemaphoreHandle_t;

SemaphoreHandle_t xSemaphoreCreateMutex(void);
BaseType_t xSemaphoreTake(SemaphoreHandle_t mutex, TickType_t ticks);
void xSemaphoreGive(SemaphoreHandle_t mutex);
void vSemaphoreDelete(SemaphoreHandle_t mutex);

#endif // FREERTOS_SEMPHR_H_STUB
