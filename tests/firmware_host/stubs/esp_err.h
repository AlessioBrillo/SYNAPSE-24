/* Host stub for esp_err.h: error codes and the name lookup.
 *
 * Implementations live in tests/firmware_host/host_glue_research.c so that a
 * shared library can link without ESP-IDF.
 *
 * The values mirror ESP-IDF's esp_err.h so that any code branching on a
 * specific code behaves identically on the host and on the pod. Only the codes
 * used by the compiled sources are defined; adding one is safe.
 */

#ifndef STUB_ESP_ERR_H
#define STUB_ESP_ERR_H

typedef int esp_err_t;

#define ESP_OK 0
#define ESP_FAIL -1
#define ESP_ERR_NO_MEM 0x101
#define ESP_ERR_INVALID_ARG 0x102
#define ESP_ERR_INVALID_STATE 0x103
#define ESP_ERR_INVALID_SIZE 0x104
#define ESP_ERR_NOT_FOUND 0x105
#define ESP_ERR_NOT_SUPPORTED 0x106
#define ESP_ERR_TIMEOUT 0x107
#define ESP_ERR_INVALID_RESPONSE 0x108
#define ESP_ERR_INVALID_CRC 0x109
#define ESP_ERR_INVALID_VERSION 0x10A
#define ESP_ERR_INVALID_MAC 0x10B
#define ESP_ERR_NOT_FINISHED 0x10C

const char *esp_err_to_name(esp_err_t e);

#endif /* STUB_ESP_ERR_H */
