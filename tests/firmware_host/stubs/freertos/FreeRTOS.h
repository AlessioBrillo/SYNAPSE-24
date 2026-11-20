/* FreeRTOS surface stub for the host compile gate (mutex + timing only).
 *
 * The drivers' logic is what is under test; a real scheduler is not.
 */
#ifndef FREERTOS_FREERTOS_H_STUB
#define FREERTOS_FREERTOS_H_STUB

#include <stdbool.h>
#include <stdint.h>

typedef uint32_t TickType_t;

#define pdMS_TO_TICKS(ms) ((TickType_t)(ms))
#define pdTRUE 1
#define pdFALSE 0
#define portMAX_DELAY 0xFFFFFFFFu

/* Spinlock critical section: single-threaded on the host, so it only has to compile. */
typedef int portMUX_TYPE;
#define portMUX_INITIALIZER_UNLOCKED 0
#define portENTER_CRITICAL(mux) ((void)(mux))
#define portEXIT_CRITICAL(mux) ((void)(mux))

#endif // FREERTOS_FREERTOS_H_STUB
