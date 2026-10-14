/* FreeRTOS surface stub for the host compile gate (mutex + timing only).
 *
 * The INA219 driver's logic is what is under test; a real scheduler is not.
 */
#ifndef FREERTOS_FREERTOS_H_STUB
#define FREERTOS_FREERTOS_H_STUB

#include <stdint.h>

typedef uint32_t TickType_t;

#define pdMS_TO_TICKS(ms) ((TickType_t)(ms))
#define pdTRUE 1
#define pdFALSE 0

#endif // FREERTOS_FREERTOS_H_STUB
